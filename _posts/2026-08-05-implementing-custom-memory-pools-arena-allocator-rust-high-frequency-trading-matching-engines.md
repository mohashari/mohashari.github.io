---
layout: post
title: "Implementing Custom Memory Pools (Arena Allocator) in Rust for High-Frequency Trading Matching Engines"
date: 2026-08-05 08:00:00 +0700
tags: [rust, systems-programming, low-latency, hft]
description: "A deep dive into implementing high-performance generational memory pools in Rust for sub-microsecond HFT matching engines."
image: "https://picsum.photos/seed/1471/1080/720"
thumbnail: "https://picsum.photos/seed/1471/400/300"
---

A single heap allocation in the critical execution path of an order matching engine is a bug. While general-purpose allocators like `jemalloc` or `mimalloc` are highly optimized for multi-threaded throughput, they introduce non-deterministic tail latencies (p99.99) that are fatal in high-frequency trading (HFT) environments. Under load, a standard heap allocation can run into thread-cache exhaustion, requiring a lock on a global arena or triggering a `brk`/`mmap` syscall. This forces a kernel context switch or page fault, ballooning a 15-nanosecond memory request into a 10-to-50 microsecond latency spike. In high-frequency trading, a 50-microsecond delay is an eternity; it is the difference between executing a trade at the top of the book and being adversely selected. To achieve deterministic, sub-microsecond latencies, matching engines must run in a zero-allocation state during the trading session, relying on custom memory pools pre-allocated and warmed at startup.

## Why Standard Rust Arenas Fall Short in HFT

Rust developers naturally reach for crates like `bumpalo` or `typed-arena` when seeking custom memory allocation. However, these libraries fail when subjected to matching engine requirements.

First, typical bump allocators grow monotonically and only free memory all at once when resetting the entire arena. A matching engine cannot simply clear its memory mid-session; it must maintain the state of active orders while continuously inserting, canceling, and filling orders. We need dynamic, slot-level reclamation.

Second, safe reference management in Rust relies on lifetimes (`'a`). Propagating lifetimes through a complex limit order book (LOB) structure causes severe compiler friction. Price levels in an LOB typically maintain a doubly-linked list of orders to enforce FIFO priority. Building a doubly-linked list using normal Rust references (`&'a mut Order`) leads to self-referential structures that the borrow checker cannot resolve without resorting to `Rc<RefCell<Order>>`. 

Third, `Rc` and `RefCell` introduce reference counting and borrow checks at runtime, which translate to CPU overhead. Furthermore, dynamic allocations of individual orders scatter the data across physical RAM, destroying L1/L2 cache efficiency.

To solve this, we design a custom **Generational Arena**. It pre-allocates a flat block of memory and uses copyable `Index` structures (combining an array index and a generation counter) rather than raw pointers or references. This guarantees O(1) allocation and deallocation, solves self-referential struct compilation, protects against the ABA problem (stale reference dereferencing), and enforces cache locality.

## Designing the Generational Slot Layout

Our memory pool must represent elements efficiently without incurring size overhead. A standard Rust enum like `Option<T>` or a customized tagged enum introduces tag bits and alignment padding, bloating the memory size of each slot. 

To maximize the density of slots in our CPU cache lines (64 bytes), we use a `union` structure. When a slot is inactive, its memory holds a `u32` index pointing to the next free slot, maintaining a linked list of free nodes. When active, it stores the payload `T`. 

<script src="https://gist.github.com/mohashari/c719541a047ea22d0c9ed751a8f1b3c1.js?file=snippet-1.txt"></script>

By wrapping `T` inside `MaybeUninit<T>`, we prevent the compiler from generating drop calls on uninitialized data and bypass initialization overhead at startup.

## Zero-Allocation Insertion and Deallocation

The `PreAllocatedArena` manages the free list within the slots vector itself. At initialization, every slot is configured as `Free`, with its `next_free` union field pointing to the subsequent slot index. 

When `alloc` is called, we fetch the slot at the `free_head` index, update `free_head` to the slot's `next_free` value, and write the element into the union. When `dealloc` is called, we read the value, push the slot back onto the head of the free list, and increment its slot generation counter. 

<script src="https://gist.github.com/mohashari/c719541a047ea22d0c9ed751a8f1b3c1.js?file=snippet-2.txt"></script>

Notice the use of `get_unchecked_mut` and `get_unchecked` in the critical paths. Since we explicitly guard slot index bounds against `self.capacity` upon entry, we can safely bypass the compiler's default vector bounds checks. This eliminates branching instructions, improving instruction cache density and preventing branch predictor misses.

## Order Book Integration using Arena Offsets

With a generational memory pool, we can build the limit order book. A price level contains links to the head and tail orders. By using `Index` offsets instead of pointers, we implement a classic doubly-linked list without the overhead of reference counting or pointer tracking.

<script src="https://gist.github.com/mohashari/c719541a047ea22d0c9ed751a8f1b3c1.js?file=snippet-3.txt"></script>

By storing orders consecutively inside the pre-allocated vector, we achieve spatial cache locality. When the matching engine traverses the list of orders at a specific price level, adjacent orders are loaded together in 64-byte L1 CPU cache lines, minimizing main memory retrieval cycles.

## Thread Pinning and Zero-Lock Concurrency

A memory pool's benefits are lost if threads contend for allocations or get context-switched by the operating system scheduler. When the kernel suspends the matching engine thread, the CPU registers are saved, translation lookaside buffers (TLBs) are flushed, and local cache lines are overwritten by other processes. When the thread resumes, it suffers severe latency penalties (up to 200 microseconds) due to a cold cache.

To prevent this, production matching engines employ a single-threaded architecture pinned to a specific core isolated from the OS scheduler via boot configurations (e.g., `isolcpus` in GRUB). This thread polls a lock-free queue in a tight busy-wait spin loop, processing messages without context switching or kernel calls.

<script src="https://gist.github.com/mohashari/c719541a047ea22d0c9ed751a8f1b3c1.js?file=snippet-4.txt"></script>

Since the matching session is single-threaded and pinned, the `PreAllocatedArena` is accessed by one CPU core. This guarantees that its data structure does not cross CPU core sockets, completely bypassing the need for thread synchronization primitives, atomic memory fences, or `Arc`/`Mutex` wraps. The compiler can translate all indexing operations into simple offset assembly.

## Defeating OS Demand Paging and Swapping

A subtle danger of pre-allocating memory via `Vec::with_capacity` is that the OS kernel operates on a lazy demand-paging model. The physical memory frames are not allocated to your virtual address space until the matching engine performs its first write to those memory addresses. Consequently, the first transaction of the day would trigger a series of page fault interrupts, introducing latency spikes.

Furthermore, if the host OS experiences memory pressure, the kernel might swap out portions of the order book memory to disk. To prevent this, we must perform a two-step initialization process at startup:

1. **Pre-warming**: Write a dummy value to every memory page in the arena to force the kernel to allocate physical memory frames immediately.
2. **RAM Locking**: Use the Unix syscalls `mlock` and `madvise` via the `libc` crate to lock the memory page range into RAM, preventing swapping and requesting huge pages to optimize TLB cache translation.

<script src="https://gist.github.com/mohashari/c719541a047ea22d0c9ed751a8f1b3c1.js?file=snippet-5.txt"></script>

The call to `std::hint::black_box` is critical. Without it, the Rust compiler's optimizer identifies that `sum` is unused and strips out the initialization read loop, rendering the pre-warming step useless.

## Preventing Memory Leaks in Unit Tests

Because we use `MaybeUninit<T>` inside the slots vector, the compiler does not know which slots contain active data when our `PreAllocatedArena` is dropped. If we simply rely on the default drop semantics of `Vec<Slot<T>>`, the active instances of `T` will leak. 

In a production setting, this is negligible because the matching engine process runs continuously until the trading session closes, at which point the OS reclaims all resources during fast process exit. However, this leak is highly problematic for continuous integration (CI) environments and test suites, where memory leaks will pollute resource usage and mask genuine memory issues.

To resolve this, we must implement a custom `Drop` trait that traverses the free list, maps out the unused index slots, and explicitly drops the remaining active elements.

<script src="https://gist.github.com/mohashari/c719541a047ea22d0c9ed751a8f1b3c1.js?file=snippet-6.txt"></script>

This custom drop implementation scans the pre-allocated region. By building a bitmask of the free-list links, we deduce the active items and drop them in place. The allocation layout remains optimized, and we retain leak-free memory management in our unit tests.

## Production Pitfalls and Mitigation Strategies

While a generational memory pool provides stable sub-microsecond latencies, you must design around three production operational realities:

### 1. Capacity Limits vs. Resizing Failures
If the matching engine exceeds its pool capacity during high market volatility, we cannot resize the arena. Reallocating a `Vec` changes its physical memory location, which invalidates raw indices and triggers a latency penalty. 

To mitigate this, size the arena using historical data. If the historical peak order rate is 500,000 active orders, configure the session capacity to 2,500,000. It is safer to reject incoming orders with an Out of Memory (OOM) error code than to allow the matching engine to block, lag, or panic mid-session.

### 2. Cache Prefetcher Degradation
When the arena is clean, allocation occurs sequentially. As orders are cancelled and created throughout a trading session, the free list becomes fragmented. Over time, allocations become scattered across the vector, degrading cache line prefetching. 

To mitigate this, schedule compaction phases. During quiet trading periods or daily maintenance windows, sweep the order book to allocate a fresh contiguous set of slots, resetting the free list to a sequential structure.

### 3. Core Affinitization Conflicts
Pinning the matching engine to an isolated CPU core means the thread will run at 100% CPU utilization. Ensure that network input/output threads (such as those handling FIX protocol parsers or SBE decoders) are pinned to different, non-overlapping cores. 

If they share the same physical core, context switching will occur, causing L1 cache eviction and negating the latency benefits of the memory pool.