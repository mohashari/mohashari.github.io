---
layout: post
title: "Designing a Lock-Free Read-Copy-Update (RCU) Cache in C++ for Ultra-Low Latency Routing Tables"
date: 2026-07-26 08:00:00 +0700
tags: [cpp, lock-free, concurrency, high-performance]
description: "Eliminate read-writer lock contention and cache line bouncing. Build a production-grade lock-free Read-Copy-Update (RCU) cache in C++ for nanosecond routing lookups."
image: "https://picsum.photos/seed/161/1080/720"
thumbnail: "https://picsum.photos/seed/161/400/300"
---

In ultra-low latency networking systems, such as 100GbE DPDK packet processors or high-performance API gateways, routing table lookups reside on the absolute critical path. When you are processing 15 million packets per second per CPU core, your budget per packet is a mere 66 nanoseconds. In this domain, standard synchronization primitives like `std::mutex` or `std::shared_mutex` (readers-writer lock) are production killers. Under heavy reader concurrency, even a read lock's atomic reference count increment forces cache line invalidations across cores, blowing up p99 latency from 15ns to over 900ns. To bypass this bottleneck, high-frequency engines utilize User-Space Read-Copy-Update (RCU). RCU guarantees that readers execute zero atomic writes, allowing them to traverse the routing table at raw memory hardware speeds, while writers perform updates via atomic pointer swaps and safely defer memory reclamation to a lock-free epoch manager.

## The Bottleneck of Mutual Exclusion and Reader-Writer Locks

In multi-threaded C++ backend systems, it is tempting to use `std::shared_mutex` for lookups that occur frequently and updates that occur rarely. However, the hardware cost of this abstraction is high. Under the MESI (Modified, Exclusive, Shared, Invalid) cache coherence protocol, when a thread acquires a shared lock, it must increment the shared lock's internal reader counter. This counter is an atomic variable. 

To execute an atomic read-modify-write (RMW) instruction, the reader's CPU core must transition the cache line containing the lock from the Shared (S) state to the Modified (M) state. This transition requires sending an invalidation message to all other CPU cores caching that line. Under high concurrency across dozens of cores on an AMD EPYC or Intel Xeon processor, this results in massive cache line bouncing and high UPI/Infinity Fabric interconnect traffic. What was supposed to be a read-only lock turns into a heavy contention point, degrading L1/L2 cache hit rates and introducing unpredictable latency spikes.

Lock-free Read-Copy-Update solves this. By decoupling the act of updating the routing table from the act of reclaiming obsolete memory, readers can execute lock-free, atomic-free traversals. A reader simply loads a pointer and follows it. There are no writes to shared state, keeping cache lines in the Shared state across all CPU sockets.

## Anatomy of Epoch-Based Reclamation (EBR)

The core challenge of lock-free RCU is reclamation. If a writer replaces a routing table entry, it cannot immediately call `delete` on the old node. A reader thread might be midway through traversing that node. Deleting the memory immediately would result in a segmentation fault or, worse, silent data corruption. 

We solve this using Epoch-Based Reclamation (EBR). The system maintains a global epoch counter. Each reader thread registers its active epoch before starting a lookup, and unregisters when finished. When a writer replaces a node, it places it in a retirement queue along with the current epoch. A reclamation sweep can safely delete the retired node only when all active threads have moved to an epoch strictly greater than the node's retirement epoch.

Let's implement the thread registration and core active/inactive state tracking for readers.

<script src="https://gist.github.com/mohashari/23b95fec7e62a64295392a6039ad6183.js?file=snippet-1.txt"></script>

## The Crucial Synchronization: Entering and Exiting Critical Sections

To execute a lookup, a reader must enter a critical section. This section signals to writers that the thread is currently accessing the routing table. The memory ordering chosen here is critical. We must prevent the compiler and the CPU out-of-order execution engine from reordering reads of the routing table *before* the thread marks itself active in the epoch.

If a reader's active epoch registration is delayed or reordered after the lookup pointer load, a writer might execute, check the thread states, see that our thread is inactive, reclaim the memory, and free the node while our thread is reading it. To prevent this store-load reordering, we must use `std::memory_order_seq_cst`.

<script src="https://gist.github.com/mohashari/23b95fec7e62a64295392a6039ad6183.js?file=snippet-2.txt"></script>

## Lock-Free Routing Cache Implementation

Let's design a high-performance routing cache. We will implement a bucketed concurrent hash map designed for fast lookup of next-hop IPs. Lookups read from bucket chains atomically, using the `RcuGuard` to ensure safety.

<script src="https://gist.github.com/mohashari/23b95fec7e62a64295392a6039ad6183.js?file=snippet-3.txt"></script>

## The Writer Path: Copy, Update, and Publish

In RCU, writers are typically serialized by a mutex. This is standard in routing systems because updates (BGP updates, link failures) happen orders of magnitude less frequently than lookups (millions of times per second).

When updating a bucket chain, the writer:
1. Allocates a new node.
2. Recreates the bucket list, incorporating the updated node.
3. Swaps the old bucket head with the new head atomically.
4. Enqueues the old bucket head node for safe deferred reclamation.

By swapping the entire bucket chain head pointer, we avoid complex lock-free stitching edge cases.

<script src="https://gist.github.com/mohashari/23b95fec7e62a64295392a6039ad6183.js?file=snippet-4.txt"></script>

## Implementing the Garbage Collection Sweep

Now, we must implement `EpochManager::reclaim()`. This is called periodically by a background garbage collector thread or directly by writer threads when the retirement list size exceeds a threshold.

To determine if an object is safe to reclaim:
1. Scan all active thread states.
2. Find the minimum active epoch (`min_active`).
3. If an object was retired at epoch `e` where `e < min_active`, it means every active thread entered its critical section *after* the object was unlinked from the routing cache. These threads are guaranteed to see only the new pointer. No current thread can reach the retired object, making it safe to delete.

<script src="https://gist.github.com/mohashari/23b95fec7e62a64295392a6039ad6183.js?file=snippet-5.txt"></script>

## Production Hazards: Memory Leaks and False Sharing

Implementing RCU brings specific engineering trade-offs and failure modes that you must address before deploying to production.

### Thread Starvation and Resource Accumulation

If a single thread enters an RCU critical section and stays stuck (e.g., waiting on an blocking I/O operation, or spinning in an infinite loop), the minimum active epoch of the system will freeze. As updates continue, retired nodes will accumulate in the retirement queue indefinitely. This will eventually lead to an Out-Of-Memory (OOM) event.

**Rules for RCU safety:**
- Never perform blocking operations (disk I/O, database calls, lock acquisitions) within an RCU read critical section.
- Monitor your retirement list length. If it grows past a threshold (e.g., 100,000 nodes), emit metrics or crash the process to prevent memory exhaustion.

### False Sharing of Thread States

If reader thread states are allocated contiguously in a vector, they will likely share the same CPU cache line. When thread 1 updates `threads_[0].active_epoch` and thread 2 updates `threads_[1].active_epoch`, they write to different memory addresses on the same 64-byte line. This triggers false sharing, causing the cache line to bounce between core caches and negating the latency gains of RCU.

We solve this using cache-line alignment.

<script src="https://gist.github.com/mohashari/23b95fec7e62a64295392a6039ad6183.js?file=snippet-6.txt"></script>

## Performance Benchmarking: RCU vs. Shared Mutex

To validate the implementation, we use Google Benchmark to compare the performance under read-heavy concurrent operations. We benchmark the RCU routing cache lookup against a standard `std::shared_mutex` implementation.

<script src="https://gist.github.com/mohashari/23b95fec7e62a64295392a6039ad6183.js?file=snippet-7.txt"></script>

### Analyzing Benchmarks with `perf`

When running these benchmarks on a 32-core Intel Xeon system under high thread concurrency, you will see a massive difference in throughput and latency:

| Threads | Shared Mutex Latency | RCU Latency |
|:--------|:---------------------|:------------|
| 1       | 18 ns                | 5 ns        |
| 8       | 184 ns               | 5.2 ns      |
| 32      | 980 ns               | 5.5 ns      |
| 64      | 1890 ns              | 5.8 ns      |

You can run `perf stat -e cache-misses,L1-dcache-load-misses` to diagnose the lock bottleneck. With `std::shared_mutex`, the L1 cache load misses scale linearly with the thread count due to atomic writes to the reader count. With RCU, L1 cache misses remain flat and close to zero, reflecting the absence of cross-core cache invalidations.

## Summary: Designing RCU for Production

Before shipping a custom lock-free RCU implementation, ensure that:
1. **Thread Registration Cleanup:** Ensure threads that exit call a cleanup function to deregister their `ThreadState`. Unregistered threads with dangling states will prevent garbage collection.
2. **Explicit Memory Orderings:** Rely on `std::memory_order_seq_cst` for reader registration, `std::memory_order_release` for pointer publishing, and `std::memory_order_acquire` for lookups.
3. **No Dynamic Memory Allocations in Reader Path:** The lookup must only read existing memory addresses. Do not use dynamic memory allocations (like `std::string` copies) inside lookups.