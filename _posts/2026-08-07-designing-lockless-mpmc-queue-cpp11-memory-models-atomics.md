---
layout: post
title: "Designing a Lock-Free MPMC Queue using C++11 Memory Models and Atomics"
date: 2026-08-07 08:00:00 +0700
tags: [cpp, lock-free, concurrency, performance]
description: "A production-grade guide to building a high-throughput, cache-aligned MPMC queue using fine-grained C++11 atomics and memory barriers."
image: "https://picsum.photos/seed/6209/1080/720"
thumbnail: "https://picsum.photos/seed/6209/400/300"
---
Imagine your production telemetry ingestion pipeline or order-matching engine handling 1,000,000 requests per second. Under baseline traffic, your service responds in sub-milliseconds. But during traffic spikes, your tail latency ($p_{99.9}$) balloons to 45 milliseconds. You profile the service and find the culprit: lock contention. A simple `std::mutex` wrapping a `std::queue` causes threads to get parked by the Linux scheduler, triggering context switches that consume up to 5 microseconds each. When dozens of threads fight for the same lock, your CPU cores spend more time thrashing cache lines and context switching than executing application logic. Replacing this lock-bound queue with a cache-aligned, lock-free Multi-Producer Multi-Consumer (MPMC) queue drops tail latency to single-digit microseconds. However, designing a lock-free queue that operates correctly under heavy concurrency is exceptionally difficult. A single misplaced memory barrier or false-sharing oversight will result in subtle, non-deterministic data corruption or catastrophic performance degradation in production.

![Designing a Lock-Free MPMC Queue using C++11 Memory Models and Atomics Diagram](/images/diagrams/designing-lockless-mpmc-queue-cpp11-memory-models-atomics.svg)

## The Cost of Locks: Why Mutexes Fail at Scale

To understand why traditional synchronization fails under high concurrency, we must examine what happens at the hardware level. In C++, a standard queue protected by `std::mutex` relies on the operating system's thread scheduler. On Linux, `std::mutex` is implemented via Futexes (fast userspace mutexes). If a thread attempts to acquire an uncontended lock, it executes a quick atomic operation in userspace taking roughly 10 to 15 nanoseconds. However, under high contention, the thread fails to acquire the lock and must make a `sys_futex` system call to transition into a blocked state, prompting the kernel to park the thread.

Thread parking incurs severe overhead:
* **Context Switching:** The CPU must save the active thread’s register state, flush the translation lookaside buffer (TLB), schedule a new thread, and load the new thread's registers. This sequence takes between 1,500 and 5,000 nanoseconds.
* **Cache Eviction:** The rescheduled thread starts with a cold L1/L2 cache, meaning it must retrieve its working set from the slower L3 cache or main memory, incurring hundreds of nanoseconds of latency.
* **Cacheline Bouncing:** The cache line containing the mutex lock word is repeatedly invalidated across cores. Under the MESI (Modified, Exclusive, Shared, Invalid) cache coherence protocol, when Core A modifies the lock state, it must send an invalidation message over the interconnect bus to Core B, Core C, and Core D. This forces subsequent reads on those cores to stall while fetching the line from RAM.

Lock-free programming avoids OS-level scheduling entirely. Instead of parking threads, lock-free data structures use atomic operations—like Compare-And-Swap (CAS)—to coordinate access directly in userspace. If a thread’s CAS operation fails due to contention, it spins or backs off, keeping its execution context loaded on the CPU core.

## Anatomy of a Lock-Free MPMC Ring Buffer

The most robust and performant architecture for a bounded MPMC queue is Dmitry Vyukov's array-based ring buffer. The design allocates a contiguous array of cells, where each cell contains the payload data and a versioned sequence number. 

The queue tracks two global positions: `enqueue_pos_` and `dequeue_pos_`. Unlike simple ring buffers, these indices increment monotonically and are never wrapped using the modulo operator until indexing the array. To avoid expensive modulo division (`%`), which can take 10 to 40 CPU cycles, the queue capacity $N$ must be a power of two. This restriction allows index wrapping via a bitwise AND operation: `index = pos & (capacity - 1)`.

The core mechanics rely on the synchronization of the cell's sequence number with the enqueue and dequeue counters:
1. **Enqueueing:** A producer reads the global `enqueue_pos_` and loads the sequence number of the corresponding cell. If the sequence matches the enqueue position, the cell is empty. The producer attempts a CAS on `enqueue_pos_` to claim the slot. If successful, it writes the data and stores `pos + 1` back to the cell's sequence. The write to the sequence publishes the data.
2. **Dequeueing:** A consumer reads the global `dequeue_pos_` and loads the cell's sequence number. If the sequence matches `pos + 1`, the cell has valid data. The consumer attempts a CAS on `dequeue_pos_` to claim the slot. If successful, it reads the data and stores `pos + buffer_mask_ + 1` back to the cell's sequence, marking the cell as empty for the next round of enqueueing.

## Layout Optimization: Preventing False Sharing

In highly concurrent systems, performance is dictated by how data aligns with the CPU's cache lines. Modern CPU architectures load memory into cache lines that are typically 64 bytes in size. If two variables used by different threads share the same cache line, they will suffer from false sharing. 

In our MPMC queue, the write-heavy atomic pointers `enqueue_pos_` and `dequeue_pos_` are constantly modified by producers and consumers. If they share a cache line, a producer writing to `enqueue_pos_` will invalidate the cache line for a consumer reading `dequeue_pos_`, causing cache line bouncing.

We use `alignas(64)` to isolate these pointers onto their own cache lines.

<script src="https://gist.github.com/mohashari/b2be557cf524c5dfd66ced752335d060.js?file=snippet-1.txt"></script>

In the queue constructor, we allocate the cell buffer and initialize the sequence numbers. Each cell's sequence is set to its index. This initial state allows the first pass of enqueue operations to proceed without blocking.

<script src="https://gist.github.com/mohashari/b2be557cf524c5dfd66ced752335d060.js?file=snippet-2.txt"></script>

## Memory Consistency: The C++11 Memory Model

By default, atomic operations in C++ use sequential consistency (`std::memory_order_seq_cst`). While safe, sequential consistency requires the compiler to emit expensive hardware memory fences (such as `MFENCE` on x86 or `DMB` on ARM) to establish a global order of operations. This severely limits compiler optimizations and stalls the CPU’s store buffers.

To maximize throughput, we must use fine-grained memory orderings:
* **`std::memory_order_relaxed`:** Guarantees atomicity but provides no synchronization or ordering guarantees relative to other memory locations. We use this for the CAS on `enqueue_pos_` and `dequeue_pos_` because we only need to claim a slot. The position trackers do not publish the payload data.
* **`std::memory_order_acquire`:** Prevents subsequent reads and writes from being reordered before this operation. We load the cell's sequence number with acquire memory order to ensure that we see the writer's payload data before we attempt to read it.
* **`std::memory_order_release`:** Prevents preceding reads and writes from being reordered after this operation. We store the updated sequence number with release memory order to guarantee that our write/read of the cell payload is visible to other threads before they observe the updated sequence number.

On strongly ordered architectures like x86-64, acquire and release operations are implemented at the compiler level and do not emit extra CPU fence instructions. On weakly ordered architectures like ARM64, they translate to hardware instructions like `LDAR` (Load-Acquire) and `STLR` (Store-Release), which are much faster than full system memory barriers.

## Implementing Enqueue and Dequeue

Here is the implementation of the copy-based `enqueue` method. The loop spins until it successfully claims a slot and updates the sequence.

<script src="https://gist.github.com/mohashari/b2be557cf524c5dfd66ced752335d060.js?file=snippet-3.txt"></script>

To support modern move-only types like `std::unique_ptr` and avoid unnecessary object copies, we also implement a move-enabled version of `enqueue`.

<script src="https://gist.github.com/mohashari/b2be557cf524c5dfd66ced752335d060.js?file=snippet-4.txt"></script>

The `dequeue` method is symmetric. It reads the data from the cell and updates the sequence number to notify producers that the slot is available again.

<script src="https://gist.github.com/mohashari/b2be557cf524c5dfd66ced752335d060.js?file=snippet-5.txt"></script>

## Production Gotchas: False Sharing, CPU Spinning, and wrapping

While this queue is mathematically sound, running it in high-contention production environments exposes hardware-specific failure modes that require active mitigation.

### 1. Loop Spinning CPU Exhaustion
If a thread repeatedly fails its CAS operation or is waiting for space in a full queue, it will spin in a tight loop. This consumes 100% of a CPU core, generating heat and starving other threads of CPU time—especially in virtualized environments where cores are overcommitted.

To prevent this, we must implement a thread backoff strategy. The backoff mechanism should spin a few times using hardware-specific instructions, then yield the thread, and eventually sleep if contention persists.

<script src="https://gist.github.com/mohashari/b2be557cf524c5dfd66ced752335d060.js?file=snippet-6.txt"></script>

Using `__builtin_ia32_pause()` on x86-64 hints to the processor that the thread is in a spin-wait loop. This avoids pipeline flushes caused by speculative execution when the loop terminates, reducing power consumption and saving CPU execution resources for other hardware threads sharing the same physical core via Hyper-Threading.

### 2. Cache Line Sharing Between Adjacent Cells
If `sizeof(Cell)` is small (for example, if `T` is a pointer or `uint64_t`), multiple cells will reside on the same 64-byte L1 cache line. When Producer A writes to `Cell[0]` and Consumer B reads from `Cell[1]`, they will experience false sharing. 

To eliminate this bounce, you can apply `alignas(64)` to the `Cell` struct itself. However, this introduces a substantial memory overhead:
* If `T` is `uint64_t`, a raw `Cell` uses 16 bytes. Aligning it to 64 bytes increases memory consumption by **400%**.
* For a queue capacity of 1,000,000 items, this increases memory allocation from 16 MB to 64 MB.

In production, unless you are targeting absolute minimum latency at any memory cost, it is usually better to leave the cells unaligned. Because the producer and consumer positions naturally drift apart (the queue remains partially filled), they rarely operate on the exact same cache line simultaneously under steady-state conditions.

### 3. Mathematical Elegance of Sequence Wrapping
You might worry that `pos + buffer_mask_ + 1` or sequence increments will eventually overflow. On a 64-bit platform, a 64-bit integer incremented 10,000,000 times per second takes over 58,000 years to overflow. 

However, even if it does overflow (or if you are running on a 32-bit system where `size_t` overflows in a matter of minutes), the arithmetic remains correct. Because C++ guarantees wrap-around behavior for unsigned integers under two's complement arithmetic, the subtraction:
`intptr_t diff = static_cast<intptr_t>(seq) - static_cast<intptr_t>(pos);`
will yield the correct signed difference even if one of the values has wrapped around the integer boundary.

## Testing and Validation in Production

Lock-free programming is notorious for hiding race conditions that only manifest under specific load conditions or thread interleavings. Standard testing will not catch these. You must validate your implementation using dedicated systems tools.

### ThreadSanitizer (TSan)
Always compile your test suite with ThreadSanitizer (`-fsanitize=thread` in GCC or Clang) during development. TSan monitors memory access patterns and will detect if any thread reads a cell's data before the acquire barrier has established a happens-before relationship. Run TSan tests with optimization flags (`-O2` or `-O3`) to ensure compiler reordering is active.

### Stress Testing with Thread Affinity
Under normal conditions, the OS scheduler moves threads between cores, which masks synchronization bugs. To stress-test lock-free code, force threads onto specific cores using CPU pinning:
```bash
# Pin 2 producers and 2 consumers to specific CPU cores to force hard contention
taskset -c 0,1,2,3 ./queue_stress_test
```
By pinning threads to adjacent cores (sharing L2 or L3 caches) and across physical sockets, you expose memory propagation delays, forcing any incorrect assumptions about atomic ordering to surface immediately.

### Profiling Cache Bouncing
Use Linux `perf` to measure cache invalidations and identify if false sharing is bottlenecking your queue:
```bash
# Record cache-to-cache data transfers (false sharing)
perf c2c record -F 60000 -- ./queue_benchmark
# View the report to find hot cache lines
perf c2c report --stdio
```
If `perf c2c` shows high hitm (HIT Modified) rates on the memory addresses of `enqueue_pos_` or `dequeue_pos_`, your cache line alignment is failing, and you must review your structure layout and compiler padding attributes.