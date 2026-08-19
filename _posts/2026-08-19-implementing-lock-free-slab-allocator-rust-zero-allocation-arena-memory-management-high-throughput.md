---
layout: post
title: "Implementing a Lock-Free Slab Allocator in Rust: Zero-Allocation Arena Memory Management for High-Throughput Packet Processing"
date: 2026-08-19 08:00:00 +0700
tags: [rust, memory-management, lock-free, systems-programming, performance]
description: "Build a lock-free, zero-allocation slab arena in Rust with thread-local caching to bypass jemalloc locks in high-throughput network pipelines."
image: "https://picsum.photos/seed/3680/1080/720"
thumbnail: "https://picsum.photos/seed/3680/400/300"
---

During a load test of our ingress gateway processing 2.4 million packets per second (Mpps) over a 10GbE network interface card (NIC), we observed a catastrophic latency profile: while our median latency (P50) sat at a clean 95 microseconds, the P99.99 latency skyrocketed to 45 milliseconds. CPU profiling using `perf record -g` revealed that our application threads spent nearly 40% of their time parked or spinning inside `jemalloc`'s internal mutexes. In high-concurrency Rust services, network threads typically receive packets, write them into heap-allocated buffers, and pass them to worker threads via channels. When those worker threads deallocate the buffers, they cross thread boundaries, defeating the thread-local caches of general-purpose allocators like `jemalloc`. Under high core counts (e.g., 64-core AMD EPYC servers), threads spend up to 40% of their CPU cycles fighting over global arena locks. If you are building high-throughput packet processing pipelines or low-latency gRPC proxies, you cannot afford dynamic heap allocation on the hot path. You need a dedicated, lock-free slab allocator.

## The Limits of General-Purpose Allocators

General-purpose allocators like `jemalloc` or `mimalloc` are marvels of software engineering. They divide memory into small, large, and huge bin sizes, maintaining separate arenas to reduce lock contention across threads. Each thread is assigned to an arena, and thread-local caches (`tcache`) handle the majority of allocations and deallocations without locking. 

However, thread caches are size-limited. When a worker thread processes incoming packets and deallocates them, it accumulates free blocks. Because the worker thread is not allocating at the same rate it is deallocating, its `tcache` quickly fills up. Once the cache limit is hit, the thread must return these blocks to the global arena. Concurrently, the I/O thread is solely allocating, depleting its own `tcache` and forcing it to fetch blocks from the global arena. This mismatch converts our lock-free local paths into a highly contended synchronized bottleneck. 

Furthermore, dynamic allocators must handle fragmentation. They must search for free slots of appropriate sizes using best-fit or first-fit algorithms. This search has non-deterministic time complexity. For packet processing, we know the size of our buffers in advance: standard MTU size is 1500 bytes, which fits comfortably inside a 2048-byte buffer. By utilizing a fixed-size slab allocator (also known as a memory pool), we eliminate fragmentation and reduce the allocation complexity to a true $O(1)$ operation.

## Architecture of a Lock-Free Slab Allocator

A slab allocator pre-allocates a contiguous segment of virtual memory and divides it into fixed-size blocks (slots). Because the memory is contiguous, we can represent each slot using a simple integer index rather than a raw 64-bit pointer. This optimization reduces cache footprint and enables atomic packing.

The core of our allocator is a lock-free free-list implemented as a Treiber stack. Instead of storing pointers to free blocks, the stack stores indices of free slots. When a thread requests a block, it pops an index from the stack. When it frees a block, it pushes the index back onto the stack.

However, lock-free stacks are vulnerable to the ABA problem. Consider a scenario where thread A reads the top of the stack (index 3, pointing to index 7). Thread A is preempted. Thread B pops 3, then pops 7, and then frees 3. The top of the stack is 3 again. When thread A resumes, it sees 3 at the top and successfully swaps it with 7. But 7 is already in use by thread B, leading to silent memory corruption.

To mitigate the ABA problem, we pack a 32-bit generation counter and a 32-bit slot index into a single atomic `u64`. Each time a slot is popped or pushed, we increment the generation counter. Even if the slot index returns to the top of the stack, the generation counter will be different, causing the CAS of any preempted thread to fail safely.

## Memory Layout and Initialization

We start by defining our core structures. We align the allocator's control structure to 64 bytes (`#[repr(align(64))]`) to prevent false sharing, which occurs when multiple CPU cores invalidate each other's L1/L2 cache lines because unrelated data fields reside in the same 64-byte block.

<script src="https://gist.github.com/mohashari/05f5ed0867bd7085a3ce7503346336cc.js?file=snippet-1.txt"></script>

During initialization, we allocate the virtual memory chunk as a single aligned block. In production, aligning to page boundaries (e.g., 4096 bytes) is a prerequisite for mapping memory into hugepages (`hugetlbfs`) which reduces TLB misses.

<script src="https://gist.github.com/mohashari/05f5ed0867bd7085a3ce7503346336cc.js?file=snippet-2.txt"></script>

## Implementing Lock-Free CAS Loops

The `alloc` function pops a free index from the stack by executing a lock-free Compare-And-Swap (CAS) loop. We load the `head` pointer using `Acquire` ordering to synchronize with prior writes. If the index extracted from `head` is `u32::MAX`, the allocator is exhausted.

<script src="https://gist.github.com/mohashari/05f5ed0867bd7085a3ce7503346336cc.js?file=snippet-3.txt"></script>

Why `compare_exchange_weak`? On x86 architectures, CAS is typically implemented using the `lock cmpxchg` instruction, which has strong semantics. However, on LL/SC architectures (ARM64, POWER), a weak CAS can fail spuriously due to cache line eviction or hardware interrupts, but it avoids the internal loop overhead that a strong CAS requires. Since we are already running in an explicit loop, using the weak variant translates to cleaner assembly and better performance on architectures like ARM64.

To deallocate a slot, we push the index back onto the stack. We must write the `next` link on the node *before* performing the CAS. If we update the head first, another thread could immediately pop the index, read the outdated next link, and corrupt the stack.

<script src="https://gist.github.com/mohashari/05f5ed0867bd7085a3ce7503346336cc.js?file=snippet-4.txt"></script>

## Mitigating Cache Bouncing: Thread-Local Caching

While our allocator is entirely lock-free, it is not immune to hardware-level bottlenecks. If we run 64 worker threads, all threads are constantly reading and writing to `self.head`. Under the MESI cache coherency protocol, an atomic store invalidates the cache line across all other CPU cores. The L1/L2 caches spend their cycles transferring the cache line containing `self.head` back and forth, stalling the CPU pipelines. This is called cache line bouncing.

To solve this, we introduce a Thread-Local Cache (TLC). Threads allocate and deallocate from a local stack containing a small array of indices. We only access the global allocator in batches, dramatically reducing cache line synchronization.

<script src="https://gist.github.com/mohashari/05f5ed0867bd7085a3ce7503346336cc.js?file=snippet-5.txt"></script>

When a thread frees a slot, it first attempts to push it into its TLC. If the TLC is full, we evict half of the cache back to the global allocator. This ensures that subsequent operations can proceed without immediate eviction.

<script src="https://gist.github.com/mohashari/05f5ed0867bd7085a3ce7503346336cc.js?file=snippet-6.txt"></script>

## Guaranteeing Operational Safety with RAII

Using raw u32 indices in our network pipeline exposes us to classic memory errors: double-frees, leaks, or use-after-free. In Rust, we wrap these indices inside a `BufferGuard` that implements `Deref` and `DerefMut` to expose the underlying raw byte slice safely, and `Drop` to automate deallocation.

<script src="https://gist.github.com/mohashari/05f5ed0867bd7085a3ce7503346336cc.js?file=snippet-7.txt"></script>

With `BufferGuard`, we can construct safe, zero-allocation packet loops. The buffer is leased from the allocator, filled directly via the socket, processed in-place, and automatically returned to the TLC when the guard goes out of scope.

<script src="https://gist.github.com/mohashari/05f5ed0867bd7085a3ce7503346336cc.js?file=snippet-8.txt"></script>

## Production Failure Modes and Diagnostics

Deploying a lock-free slab allocator to production requires careful planning. Here are three real failure modes we encountered and how to debug them:

### 1. Asymmetric Worker Contention (Pipeline Contention)

If your architecture has dedicated receiver threads (only allocating) and worker threads (only processing and dropping), the thread-local caches will be completely ineffective. The receiver thread's TLC will always be empty, forcing it to call the global allocator for every `REFILL_BATCH` items. The worker threads will constantly exceed their TLC limits and evict packets back to the global pool.

*   **Diagnostics**: Monitor the cache hit/miss ratio by introducing thread-local counters. If the miss rate is near 100% on the allocation path, your worker topology is asymmetric.
*   **Mitigation**: Implement a dual-mempool system or increase `REFILL_BATCH` and `CACHE_CAPACITY` limits to amortize the global CAS operations.

### 2. Async Cancellation Memory Leaks

In asynchronous Rust (e.g., Tokio), futures can be cancelled at any `.await` boundary. If your `BufferGuard` is held across an `.await` point and the future is dropped by the executor (e.g., due to a client timeout), the `BufferGuard`'s `Drop` implementation will run. This is correct. However, if you bypass `BufferGuard` and manually track raw indices, future cancellation will lead to silent index leaks. The allocator will slowly drain until it is permanently starved.

*   **Diagnostics**: Expose the active allocation count via a global atomic counter in the allocator. If active allocations rise continuously while traffic is stable, you have a leak.
*   **Mitigation**: Never expose raw indices to the business logic. Encapsulate all raw index operations inside safe RAII wrappers.

### 3. Cache Line Bouncing on Node Metadata

Even with TLC, if threads write to their TLC indices but must update the shared `nodes` metadata array on refills or evictions, cache lines can still bounce. The `nodes` vector is contiguous. If thread A writes to `self.nodes[0]` and thread B writes to `self.nodes[1]`, they are writing to separate elements, but because those elements share the same L1 cache line, the CPU cores invalidate each other's caches. This is false sharing.

*   **Diagnostics**: Run `perf c2c record -F 60000 -- ./your_binary` followed by `perf c2c report`. Look for high hitm (hit modified) counts on the memory addresses corresponding to the `nodes` vector.
*   **Mitigation**: Pad the `Node` structure or ensure that slots are allocated in larger contiguous blocks to group thread-local modifications on the same cache line.

## Performance Benchmarks

In our synthetic workloads simulating 10GbE network packet processing on a 64-core EPYC 7763 processor, we observed the following performance characteristics comparing `jemalloc` with our lock-free cached slab allocator:

| Metric | jemalloc (Default) | Cached Slab Allocator | Improvement |
| :--- | :--- | :--- | :--- |
| **Throughput (Mpps)** | 1.4 Mpps | 6.8 Mpps | **4.8x** |
| **P50 Latency** | 110 µs | 42 µs | **2.6x** |
| **P99.9 Latency** | 8.2 ms | 98 µs | **83x** |
| **P99.99 Latency** | 45.4 ms | 145 µs | **313x** |

By eliminating dynamic size checks, lock synchronization, and mitigating cache-line bouncing through thread-local caching, the lock-free slab allocator provides flat, deterministic tail latency. Memory management ceases to be the bottleneck, allowing your packet processing pipelines to run at true line rate.