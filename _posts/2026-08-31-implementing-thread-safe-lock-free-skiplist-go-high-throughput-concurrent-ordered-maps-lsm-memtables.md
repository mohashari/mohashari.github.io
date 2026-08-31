---
layout: post
title: "Implementing a Thread-Safe Lock-Free Skiplist in Go: Designing High-Throughput Concurrent Ordered Maps for LSM Memtables"
date: 2026-08-31 08:00:00 +0700
tags: [go, database-systems, concurrency, performance]
description: "Learn how to implement a high-throughput, lock-free concurrent skiplist in Go using arena allocation to build zero-GC LSM memtables."
image: "https://picsum.photos/seed/7838/1080/720"
thumbnail: "https://picsum.photos/seed/7838/400/300"
---

Under heavy write loads, standard mutex-protected maps or B-trees become the primary bottleneck in Log-Structured Merge-tree (LSM) storage engines like Pebble, RocksDB, or Badger. In a production environment handling 100,000+ writes per second, thread contention on the write-ahead log (WAL) is painful, but lock contention on the in-memory write buffer (the memtable) will completely stall the ingestion pipeline. High-throughput ingestion requires an ordered concurrent data structure that handles concurrent inserts with zero global locks, allowing concurrent writes to scale linearly with CPU cores. A lock-free concurrent skiplist solves this by relying on atomic Compare-And-Swap (CAS) operations rather than structural locks, unlocking massive concurrent ingestion rates.

![Implementing a Thread-Safe Lock-Free Skiplist in Go: Designing High-Throughput Concurrent Ordered Maps for LSM Memtables Diagram](/images/diagrams/implementing-thread-safe-lock-free-skiplist-go-high-throughput-concurrent-ordered-maps-lsm-memtables.svg)

## The Anatomy of an LSM Memtable: Why Skiplists?

To understand why a skiplist is the standard choice for LSM memtables, we must look at the concurrency requirements of an LSM-tree. The memtable is the entry point for all writes. It must:
1. Support concurrent insertions from multiple goroutines.
2. Keep keys sorted to facilitate efficient range scans and eventual serialization to SSTables (Sorted String Tables) on disk.
3. Provide fast point lookups.

If we were to use a self-balancing binary search tree (like a Red-Black Tree or AVL Tree), a concurrent lock-free implementation would be practically impossible. Balancing a tree requires structural rotations that modify multiple pointers across different branches of the tree simultaneously. Coordinating these multi-pointer updates lock-free requires complex software transactional memory (STM) or incredibly complex multi-word CAS protocols. 

B-trees are similarly problematic. While they are highly cache-friendly, inserting a key can trigger node splits or merges that propagate up the tree, necessitating broad latching strategies that kill parallel write throughput.

A skiplist bypasses these problems by replacing strict structural balancing with probabilistic balancing. In a skiplist, nodes are distributed across a hierarchy of linked lists (levels). Each node is assigned a random height when created. Crucially, inserting a node only requires updating local forward pointers at each level the node exists on. Because these pointer updates are isolated and localized, they can be performed using standard, hardware-level single-word atomic operations (`CompareAndSwap`).

## Overcoming Go's GC: Off-Heap Arena Allocation

Implementing a lock-free skiplist in Go introduces a silent performance killer: the Go Garbage Collector. 

A traditional skiplist implementation represents nodes as heap-allocated structs containing pointers to other nodes. In a database memtable holding millions of keys, this layout generates millions of individual objects on the Go heap. When the Go GC runs its concurrent mark-and-sweep phase, it must scan every single one of these pointers to verify they are still reachable. This causes CPU usage to spike, triggers long GC pauses, and degrades read/write latencies (often resulting in p99.9 latency spikes exceeding 200ms).

Furthermore, in a production LSM engine, we do not need to delete individual nodes from the memtable. Deletions in an LSM-tree are represented as *tombstones*—normal key-value inserts with a special metadata tag indicating deletion. The memtable itself is write-only until it reaches a size threshold (e.g., 64MB), at which point it is marked read-only, flushed to disk as an SSTable, and discarded in its entirety.

We can exploit this lifecycle by using a **contiguous byte-slice Arena** to allocate all nodes. Instead of allocating nodes individually on the heap, we allocate a single large block of memory (e.g., 64MB) and partition it sequentially. Nodes are referenced not by raw Go pointers, but by `uint32` offsets from the start of the arena. This design offers massive advantages:
1. **GC Bypass:** The entire arena is a single byte slice. The Go GC sees only one object to scan, reducing GC overhead from hundreds of milliseconds to near-zero.
2. **Data Locality:** Nodes are packed contiguously in memory, yielding far better CPU cache hit rates compared to disjointed heap objects.
3. **No Garbage Collection for Nodes:** When the memtable is flushed, we drop the reference to the arena, reclaiming megabytes of memory in a single sweep.

Let's look at the implementation of such an Arena in Go.

<script src="https://gist.github.com/mohashari/d558f3e9e9f3b6c3e87dc57494a373ca.js?file=snippet-1.go"></script>

## Node Layout: Packing Data for Cache Efficiency

To minimize memory overhead and maximize cache locality, we must carefully pack our node data in the arena. A node contains:
- The length and offset of the key.
- The length and offset of the value.
- The height of the node.
- An array of next-pointer offsets (one `uint32` offset per level).

Instead of storing these in a struct with a Go slice header (which introduces 24 bytes of slice overhead plus a heap pointer), we compute a custom layout. The header of the node is fixed at 20 bytes, followed immediately by the tower of forward pointers.

```go
// Node layout in the arena:
// +-------------------+-----------------+-------------------+-----------------+-----------------+----------------------+
// | Key Offset (4B)   | Key Size (4B)   | Val Offset (4B)   | Val Size (4B)   | Height (4B)     | Next Offsets (H * 4) |
// +-------------------+-----------------+-------------------+-----------------+-----------------+----------------------+
```

Here is how we represent this layout and interact with it atomically:

<script src="https://gist.github.com/mohashari/d558f3e9e9f3b6c3e87dc57494a373ca.js?file=snippet-2.go"></script>

## Lock-Free Search Path

To search for a key or find the correct insertion coordinates, we traverse the skiplist from the highest active level down. At each level, we scan forward until we find a node whose key is greater than or equal to the target key. We record the last node visited at each level. 

Because we do not delete nodes, this search path is extremely simple and fast. We do not have to worry about our current node being concurrently deleted or recycled underneath us (which would require complex hazard pointer tracking). If a concurrent thread is inserting a node, our atomic loads will either read the old forward pointer or the new forward pointer. In both cases, the search returns a valid, monotonically increasing sequence.

<script src="https://gist.github.com/mohashari/d558f3e9e9f3b6c3e87dc57494a373ca.js?file=snippet-3.go"></script>

## Lock-Free Insertion: The CAS Link Loop

Inserting a new node requires a two-step phase:
1. **Prepare Phase:** We compute a random height for the node, allocate the node in the arena, and populate the node's local forward pointers based on a call to `findSplits`.
2. **Publish Phase:** We attempt to insert the node starting from the bottom level (Level 0) up to the node's random height. 

Linking level 0 first is a critical correctness constraint. Once a node is linked at Level 0, it is officially member to the skiplist and visible to range scans. Higher-level links act merely as bypass express lanes to optimize search speeds.

If the CAS operation fails at Level 0, it means another thread has concurrently inserted a node at that exact position. We must discard our search results, re-evaluate our position using `findSplits`, and retry the insertion. Once Level 0 is successfully linked, we proceed to link the higher levels. If a CAS fails on a higher level, we simply update our predecessor/successor pointers for that level and retry—we do not need to restart the entire insertion because the node is already safely integrated at Level 0.

<script src="https://gist.github.com/mohashari/d558f3e9e9f3b6c3e87dc57494a373ca.js?file=snippet-4.go"></script>

## Eliminating Random State Contention

A subtle but severe performance bottleneck in concurrent skiplists is the height generator. Standard implementations use a random number generator like Go's `math/rand` to determine node heights. Under high concurrency, calling a shared `math/rand` instance causes severe thread contention because the internal state update is protected by a global mutex.

Even using a thread-local random state (e.g., via a pool of `math/rand` objects) introduces overhead. To achieve peak throughput, we must generate random heights using a cheap, thread-safe pseudo-random number generator that requires zero locks and zero CAS loops.

In Go, we can call the runtime's internal fast-random generator (`runtime.fastrand`) using the `go:linkname` compiler directive. This function retrieves a pre-computed state from the current Goroutine's local storage (`g` struct) without acquiring locks or causing cache-line bouncing.

<script src="https://gist.github.com/mohashari/d558f3e9e9f3b6c3e87dc57494a373ca.js?file=snippet-5.go"></script>

## Production Failure Modes and Hard-Won Lessons

Designing a data structure with lock-free semantics looks great in academic benchmarks, but production workloads will surface nasty real-world bottlenecks. Here are the primary issues you will encounter and how to mitigate them.

### 1. Cache Line Bouncing on CAS hot-spots
If multiple write threads attempt to insert keys into the exact same key range (for example, keys prefixed with an auto-incrementing database sequence number or a timestamp), they will all attempt to CAS the same predecessor's next pointer. 
This triggers **cache line bouncing**. The CPU core hosting the predecessor node must repeatedly invalidate the L1/L2 caches of all other cores, turning your lock-free data structure into a serial bottleneck.

**Mitigation:** 
- In LSM engines, keys should be structured to distribute inserts evenly across the key space (e.g., using a hash prefix for partition keys if sorted order is only required within a shard).
- If sequential keys are a hard requirement, introduce a thread-local write buffer that aggregates insertions before CASing them in batches.

### 2. The Go GC "Scavenger" Panic
While the Arena allocation strategy prevents Go's garbage collector from scanning individual skiplist nodes, allocating massive byte slices (e.g., keeping multiple 64MB memtables active in memory) can cause the Go runtime memory allocator to fragment virtual memory. The GC scavenger may struggle to release this memory back to the OS fast enough under heavy churn, leading to Out-Of-Memory (OOM) panics.

**Mitigation:**
- Recycle arenas. Once a memtable is flushed to disk, do not let it get garbage collected. Instead, put the underlying byte slice back into a `sync.Pool`. Reuse these byte slices for new active memtables. This eliminates heap allocation entirely after the system warms up.

### 3. Read-After-Write Consistency and CAS Ordering
A reader traversing a lock-free skiplist might read a new node at Level 0, but fail to see it at Level 1 or Level 2 because those higher levels have not been CAS-linked yet. While this is structurally safe, it can cause inconsistent search behavior if the reader starts a range query from a higher level instead of starting from the top and descending correctly.

**Mitigation:**
- Ensure that range iterators always initialize by searching from the top level down to Level 0 to locate their starting boundary, and then *only* traverse Level 0 (the bottom-most linked list) sequentially. Never use higher levels for sequential traversal; they are purely search accelerators.