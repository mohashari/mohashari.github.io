---
layout: post
title: "Implementing a Custom Storage Engine in Go: LSM-Trees, Memtables, and SSTable Merging"
date: 2026-06-20 08:00:00 +0700
tags: [go, database, storage]
description: "An in-depth guide to implementing a custom LSM-tree storage engine in Go, covering Memtables, WALs, Bloom filters, SSTables, and compaction."
image: "https://picsum.photos/seed/76/1080/720"
thumbnail: "https://picsum.photos/seed/76/400/300"
---

At scale, traditional B-tree-based relational and key-value databases fall off a performance cliff under write-heavy workloads. As concurrent insertions and updates trigger random disk writes, split-leaf disk pages, and deep lock contention inside the engine, write throughput degrades from tens of thousands of writes per second to a crawl. In modern high-throughput environments—such as time-series metrics ingestion, distributed event streaming buffers, or real-time tracking systems—we need a storage architecture optimized for sequential write speeds. This is where Log-Structured Merge-Trees (LSM-trees) shine. By transforming random writes into sequential memory inserts and batch-flushing them to disk as immutable sorted tables, LSM engines bypass the B-tree write bottleneck. However, this write speed comes at a cost: read path amplification, file descriptor churn, memory overhead, and unpredictable background compaction pauses. This post breaks down how to implement a custom, production-grade LSM-tree storage engine in Go, optimizing memory overhead, disk structure, and the concurrent merging pipelines.

![Implementing a Custom Storage Engine in Go: LSM-Trees, Memtables, and SSTable Merging Diagram](/images/diagrams/implementing-lsm-tree-storage-engine-go-memtable-sstable.svg)

## The Architecture of an LSM-Tree Storage Engine

An LSM-tree storage engine decouples the write path from the read path by structuring data into multiple components across memory and disk. The write path is strictly append-only. When a client performs a `Put(key, value)` operation, the engine writes the entry to an append-only Write-Ahead Log (WAL) on disk to guarantee durability. Simultaneously, it inserts the record into a memory-resident sorted structure called the Memtable. 

Because the Memtable is always sorted, search operations in memory are extremely fast. However, memory is finite. When the Memtable reaches a pre-configured size threshold—typically 32MB or 64MB in production systems—it is marked as immutable, and a new active Memtable is spawned to accept incoming writes. A background flusher thread then serializes the immutable Memtable to disk as a Level 0 (L0) Sorted String Table (SSTable). 

Once written to disk, SSTables are immutable. Because L0 SSTables are flushed directly from memory at different times, their key ranges overlap. As more L0 SSTables accumulate, search latency degrades because a read query might have to search every single L0 file. To resolve this, a background compaction process periodically merges multiple SSTables using a multiway merge-sort algorithm, removing duplicate keys and deletion markers (tombstones) while moving the consolidated data to lower levels (Level 1, Level 2, etc.) where key ranges are strictly non-overlapping.

## Designing a Memory-Efficient Memtable with SkipLists

The Memtable must support fast, concurrent reads, writes, and in-order scans. While a balanced binary search tree (like a Red-Black tree) is a common choice, its rebalancing phases under high concurrency require complex locking schemes. A SkipList provides a probabilistic alternative with $O(\log n)$ search, insertion, and deletion complexities. It utilizes a layered hierarchy of forward pointers, allowing queries to skip large sections of the list.

However, implementing a SkipList in Go poses a major threat to garbage collection (GC) performance. Go's runtime GC is a concurrent mark-and-sweep collector. It scans the heap by tracing pointers. If our Memtable stores millions of small key-value records using naive pointer-heavy nodes, the GC will spend milliseconds scanning these pointers, leading to latency spikes. To combat this, we must track the exact byte allocations of our Memtable, enabling the engine to freeze the Memtable before it exceeds bounds and triggers heap fragmentation.

<script src="https://gist.github.com/mohashari/9a2273491ad785989f624c93c9a16687.js?file=snippet-1.go"></script>

## Write-Ahead Logging (WAL) for Crash Recovery

Because the Memtable resides in volatile memory, any power loss or process crash will result in data loss. To satisfy durability requirements, writes must be committed to a Write-Ahead Log (WAL) on disk before updating the Memtable. 

The WAL is an append-only file. Each log entry is structured with a length prefix, a timestamp, a tombstone boolean, and a CRC32 checksum. If the system crashes mid-write, the database parses the WAL during bootstrap, recalculates checksums, and restores the Memtable to its pre-crash state. 

However, calling a disk write for every single entry introduces massive disk I/O bottlenecks. In production systems, we use an OS-level file cache, writing updates using a buffered channel, and periodically executing `fsync` (either on a timer like every 10ms, or when the buffer reaches a threshold) to flush the kernel page cache to physical storage.

<script src="https://gist.github.com/mohashari/9a2273491ad785989f624c93c9a16687.js?file=snippet-2.go"></script>

## On-Disk Storage: SSTable Layout and Sparse Indexes

An SSTable is an immutable file that stores sorted key-value pairs. Since keys are sorted, we can optimize disk searches by grouping records into blocks (typically 4KB) and keeping a Sparse Index in memory. 

Rather than indexing the disk offset of every key, the Sparse Index only stores the starting key and file offset of each data block. To search for a key, we perform a binary search on the Sparse Index in memory to identify the specific block containing the key. Then, we execute a single random disk seek to load the 4KB block and scan it sequentially. This reduces memory usage by orders of magnitude compared to indexing every single record.

<script src="https://gist.github.com/mohashari/9a2273491ad785989f624c93c9a16687.js?file=snippet-3.go"></script>

## Maximizing Read Performance with Bloom Filters

Even with sparse indexing, searching multiple levels of SSTables for a non-existent key requires a disk seek per SSTable. This read path penalty is called **Read Amplification**. 

To mitigate this, we prepend a Bloom Filter to each SSTable. A Bloom Filter is a space-efficient probabilistic data structure that can check whether an element is a member of a set. It never yields false negatives (if it says a key is not present, it is guaranteed not to be there), but it can yield false positives. Before querying the disk block, we query the Bloom Filter. If it returns false, we bypass the SSTable entirely.

<script src="https://gist.github.com/mohashari/9a2273491ad785989f624c93c9a16687.js?file=snippet-4.go"></script>

## Serializing and Writing SSTables to Disk

When an active Memtable overflows, the flusher iterates through the sorted keys and serializes the dataset into a single SSTable file. To optimize disk usage, key and value lengths are written using variable-length integer encoding (varint). 

The output file consists of three segments:
1. **Data Blocks**: Contiguous lists of key-value pairs formatted into 4KB blocks.
2. **Index Block**: Located near the end of the file, containing serialized Sparse Index entries mapping block ranges.
3. **Bloom Filter Block**: Containing the filter's parameters and the bit array.
4. **Footer**: A fixed 24-byte tail containing the byte offsets of the Index and Bloom Filter blocks, terminated by an LSM magic number.

<script src="https://gist.github.com/mohashari/9a2273491ad785989f624c93c9a16687.js?file=snippet-5.go"></script>

## The Read Path: Cascading Query Resolution

Because the data is spread across the active Memtable, the immutable Memtable, and multiple SSTable files on disk, read queries must execute a cascading query resolution sequence. 

The engine checks storage components in reverse chronological order (newest to oldest):
1. Query the active Memtable.
2. Query the immutable Memtable (if present).
3. Query Level 0 SSTables (newest file first). Since L0 SSTables overlap, we must query all of them.
4. Query Level 1 and lower SSTables. Since higher levels do not overlap, we locate the single file whose key range covers the target key, and query only that file.

If a key is found, the engine checks if the record is a tombstone. If so, it returns a "not found" response, hiding the record as deleted.

<script src="https://gist.github.com/mohashari/9a2273491ad785989f624c93c9a16687.js?file=snippet-6.go"></script>

## SSTable Merging: Multiway Merge-Sort and Compaction

As L0 SSTables accumulate, read performance degrades because the engine must search multiple files. Additionally, deleted keys (tombstones) and obsolete versions of keys still consume disk space. 

To clean this up, a background compaction worker performs a multiway merge-sort on selected SSTables. Since SSTables are sorted internally, we can merge $N$ files in $O(M \log N)$ time, where $M$ is the total number of records across all files, using a Min-Heap. The Min-Heap stores the head of each SSTable iterator. We pop the smallest key, write it to the new SSTable (discarding older versions of the same key based on timestamps), and advance the corresponding iterator.

<script src="https://gist.github.com/mohashari/9a2273491ad785989f624c93c9a16687.js?file=snippet-7.go"></script>

## Production Failure Modes and Performance Tuning in Go

When running an LSM-Tree storage engine in production under high concurrency and sustained load, three primary failure modes occur:

### 1. Write Stalls
If write volume is high, the active Memtable will fill up faster than the background flusher can serialize the immutable Memtable to disk. When this happens, the engine runs out of memory or blocks incoming writes to prevent OOM errors. This is known as a **Write Stall**. 
* **Mitigation**: Introduce write rate-limiting at the API layer, increase the number of background flusher workers, or write-throttling inside the `Put` path when the count of pending L0 files exceeds a limit (e.g., more than 8 L0 files).

### 2. File Descriptor Exhaustion
Since every SSTable file represents a physical file on disk, having thousands of uncompacted SSTables will exhaust the operating system's file descriptor limits (typically `ulimit -n` defaults to 1024 on Linux). 
* **Mitigation**: Implement strict leveled compaction to keep the file count bound, and design an internal `TableCache` using an LRU cache scheme to pool open file descriptors across queries.

### 3. Garbage Collection Latency
Go's garbage collector traces heap pointers. Storing millions of small byte slices and structs in memory inside the SkipList can increase GC pause times to tens of milliseconds. 
* **Mitigation**: Store key-value pairs in a pre-allocated contiguous memory slab (an Arena allocator). Instead of allocating new byte slices for each key and value, copy the data to the Arena and reference offsets. This reduces the number of heap objects visible to the garbage collector to a single large slab.