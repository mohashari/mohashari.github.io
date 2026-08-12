---
layout: post
title: "Implementing a Lock-Free B-Link Tree for High-Concurrency In-Memory Databases in C++20"
date: 2026-08-12 08:00:00 +0700
tags: [cpp20, concurrency, database, lock-free, systems-programming]
description: "A deep dive into implementing a production-grade lock-free B-Link Tree in C++20, detailing epoch reclamation, hardware alignment, and ARM64 memory models."
image: "https://picsum.photos/seed/1948/1080/720"
thumbnail: "https://picsum.photos/seed/1948/400/300"
---

Consider a high-frequency trading matching engine or a real-time gaming session store running on a 64-core AMD EPYC server. Under write-heavy workloads of 10 million transactions per second, your p99.9 latencies suddenly spike from 50 microseconds to 25 milliseconds. When you attach `perf` or run `bt` in `gdb`, the culprit is immediately obvious: hundreds of threads are blocked on a reader-writer lock (`std::shared_mutex`) during index page splits. Standard B-Trees and skip-lists fall off a performance cliff here because writing threads must hold exclusive locks on parent nodes while propagating splits upward, blocking readers entirely. To scale throughput linearly with core count, we must design an index that allows readers to traverse splitting nodes without holding locks of any kind. The Lehman-Yao B-Link Tree solves this by introducing a right-sibling pointer and a high key to every node. This article details how to implement a production-grade, latch-free B-Link Tree in C++20, utilizing hardware-aware alignment, lock-free search traversal, and custom Epoch-Based Reclamation (EBR) to prevent user-after-free crashes under heavy concurrency.

![Implementing a Lock-Free B-Link Tree for High-Concurrency In-Memory Databases in C++20 Diagram](/images/diagrams/implementing-lock-free-blink-tree-high-concurrency-in-memory-databases-cpp20.svg)

## The Core Architecture of a Lehman-Yao B-Link Tree

The fundamental innovation of the Lehman-Yao B-Link Tree is that every node (both leaf and internal) contains a right-link pointer to its immediate right sibling and a high key. The high key represents the maximum key value stored in that node's subtree. If a thread searches for a key that exceeds the node's high key, the thread knows a concurrent split has moved the target key to the right sibling. Instead of backtracking or restarting the search from the root, the reader simply traverses the right-link.

To build this in production-grade C++20, we must satisfy two critical hardware constraints: avoiding false sharing and ensuring cache-line alignment. If a reader is scanning a node's keys while a concurrent writer is updating that node's metadata (such as the key count), the cache line will bounce between cores, destroying L1/L2 cache efficiency. We use `alignas(64)` to align our node structures to standard CPU cache line boundaries and organize the memory layout to keep keys contiguous for vectorized linear scans.

<script src="https://gist.github.com/mohashari/44d723a794d73f81cd05c45dc3aff992.js?file=snippet-1.txt"></script>

## Concurrency Control and the Right-Link Design

In a traditional B-Tree, concurrent reads and writes are typically managed using latch crabbing. A reader locks the parent, acquires the lock on the child, and then releases the parent lock. This creates massive memory traffic as lock states are repeatedly written to memory. In our B-Link Tree, readers require absolutely no locks or read-latches. 

When a reader accesses a node, it first checks if the target key is greater than the node's high key. If the high key is set and the target key exceeds it, the reader follows the `right_link` pointer. This traversal can occur while a writer thread is actively splitting the node. Because the writer updates the right-link pointer atomically after splitting the node, the reader is guaranteed to either find the key in the current node or find it by traversing right.

<script src="https://gist.github.com/mohashari/44d723a794d73f81cd05c45dc3aff992.js?file=snippet-2.txt"></script>

## Lock-Free Splits and the Structural Modification Protocol (SMP)

A node split occurs when a write operation exceeds the `MaxKeys` limit of a node. In a lock-free B-Link Tree, a split is performed in a bottom-up, two-phase process:

1. **Phase 1 (Atomic Sibling Insertion):** Allocate a new right sibling node. Copy the upper half of the keys and values from the original node to the new sibling. Set the new sibling's right-link to the original node's old right-link. Set the new sibling's high key to the original node's old high key. Next, set the original node's new high key to the split boundary (the lowest key in the sibling node) using `std::memory_order_release`. Finally, atomically update the original node's right-link to point to the new sibling. Once the right-link is updated, the new sibling becomes visible to readers traversing horizontally.
2. **Phase 2 (Parent Key Propagation):** Insert the split boundary key and the pointer to the new sibling into the parent node. If the parent node is also full, this split propagates recursively up the tree.

During the window between Phase 1 and Phase 2, the tree structure is technically incomplete because the parent does not yet point to the new sibling. However, the tree is still search-compatible. If a reader traverses down to the parent and follows the pointer to the original node, it will read the original node, notice that the search key is higher than the original node's new high key, and follow the right-link to the new sibling. 

<script src="https://gist.github.com/mohashari/44d723a794d73f81cd05c45dc3aff992.js?file=snippet-3.txt"></script>

## Safe Memory Reclamation: Epoch-Based Reclamation (EBR)

In a lock-free index, deleting nodes is highly hazardous. A writer thread might split a node or delete a key range and want to free the old node memory. However, reader threads might still be mid-traversal inside that node. Freeing the memory immediately will result in segmentation faults or data corruption. Standard smart pointers (`std::shared_ptr`) are a complete non-starter here; atomic reference counting involves writing to shared memory on every read, which creates massive cache-line bouncing and limits scalability.

Instead, we implement Epoch-Based Reclamation (EBR). EBR groups memory reclamation into three cyclic epochs (0, 1, 2). The system tracks a global epoch. When a thread wants to search or mutate the index, it enters the current global epoch and registers its active status. When a node is deleted, it is retired to a local list tagged with the current global epoch. The memory is only freed once all active threads have moved past the epoch in which the node was retired.

<script src="https://gist.github.com/mohashari/44d723a794d73f81cd05c45dc3aff992.js?file=snippet-4.txt"></script>

## Memory Ordering and C++20 Atomic Refinement

To achieve maximum read performance, we must avoid default sequentially consistent atomic operations (`std::memory_order_seq_cst`). On multi-socket architectures, `seq_cst` generates expensive CPU bus locking instructions (such as `mfence` on x86-64 or explicit `dmb` barriers on ARM64). Instead, we must construct our lock-free operations using precise Acquire-Release semantics. 

When searching for a key, the reader starts by acquiring the root node reference. The pointer to the child is loaded with `std::memory_order_acquire`. This ensures that all memory writes to the child node (performed by a writer during initialization or splitting) are fully visible to the reader.

<script src="https://gist.github.com/mohashari/44d723a794d73f81cd05c45dc3aff992.js?file=snippet-5.txt"></script>

## Production Failure Modes and Diagnostics

Building a lock-free B-Link Tree in C++20 is deceptively complex. The compiler's optimizer and modern CPU architectures will aggressively reorder memory operations if you don't declare atomic memory order boundaries correctly. There are three key failure modes that routinely bring down lock-free in-memory databases in production.

### 1. Epoch Drift and Stranded Memory
The most common production vulnerability with Epoch-Based Reclamation is epoch drift. If your database engine spawns a thread that enters the EBR epoch (e.g., to run a background query) and then gets stuck in a CPU-bound loop or goes to sleep without calling `exit()`, the global epoch will never advance. 
Because the global epoch is blocked, no retired memory can be freed. The retired lists of your worker threads will grow indefinitely, resulting in a slow memory leak that eventually triggers the Linux kernel's Out-Of-Memory (OOM) killer.

To diagnose this in production:
* Build a diagnostic counter that tracks the age of the oldest active epoch registration.
* Set hard limits on local retired list sizes. If a thread's retired list exceeds 100,000 nodes, force a yield or throw an alert to detect stalled worker threads.

### 2. Cache-Line Bouncing (False Sharing)
Even when code is functionally correct, memory contention can degrade throughput. If your `BLinkNode` does not isolate variables that are frequently mutated from read-only data, threads will experience severe performance bottlenecks. For example, if a writer thread frequently updates `num_keys` via atomic CAS operations, and that variable shares a cache line with the node's search keys, the CPU core performing the write will invalidate the L1/L2 cache lines of all reader cores.

To diagnose cache-line bouncing:
* Profile your application using `perf c2c` (cache-to-cache). Look for high HITM (Hit in Modified Cache) rates on your B-Tree node structures.
* Ensure keys, children pointers, and metadata are aligned. Use `alignas(64)` to align crucial nodes, and group fields by access patterns (e.g., placing the read-mostly keys and high keys together, separated from the write-mostly control structures).

### 3. Memory Reordering Bugs on ARM64
A classic issue in lock-free system programming is development on x86-64 followed by deployment to ARM64 cloud instances (such as AWS Graviton3). Because x86-64 provides strong memory ordering (TSO - Total Store Order), relaxed atomic operations (`std::memory_order_relaxed`) often run correctly even when they are conceptually buggy. When the same code is compiled for ARM64, the CPU's weak memory model allows instructions to be reordered, causing intermittent segment faults under high thread counts.

To eliminate memory reordering bugs:
* Never assume correctness based on successful tests on an x86 machine.
* Test your code using ThreadSanitizer (TSan) using:
  ```bash
  clang++ -std=c++20 -fsanitize=thread -O2 main.cpp -o main
  ```
* Integrate automated execution tests on native ARM64 servers into your continuous integration pipeline. Ensure you run target workloads for hours under stress testing frameworks to capture rare race conditions.