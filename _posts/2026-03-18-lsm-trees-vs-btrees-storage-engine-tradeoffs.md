---
layout: post
title: "LSM Trees vs B-Trees: Understanding Storage Engine Trade-offs"
date: 2026-03-18 07:00:00 +0700
tags: [databases, internals, storage, performance, backend]
description: "Compare Log-Structured Merge Trees and B-Tree storage engines to make informed decisions when choosing or tuning databases like RocksDB, LevelDB, and PostgreSQL."
---

Every time you insert a row into PostgreSQL or write a key to RocksDB, a fundamentally different machine is doing the work underneath. PostgreSQL reaches into a balanced tree, finds the right page, and modifies it in place. RocksDB writes your data sequentially to a log, then lets a background process sort and merge it later. These aren't just implementation details — they're architectural commitments that cascade into real-world behavior: how fast your writes land, how much disk amplification you absorb, and whether your read latencies stay predictable under load. Understanding the internal mechanics of B-Trees and LSM Trees gives you a mental model for choosing the right database, tuning the right knobs, and diagnosing the right bottlenecks.

## B-Trees: Predictable Reads, In-Place Mutations

A B-Tree organizes data into fixed-size pages, typically 8KB in PostgreSQL or 16KB in MySQL InnoDB. Each internal page holds keys and child pointers; leaf pages hold the actual rows. Traversal is O(log n) with a fanout of hundreds, so even a billion-row table requires only three or four page reads. Writes, however, require locating the target page and modifying it in place — which means random I/O. That page must be read into the buffer pool, dirtied, and eventually flushed back to disk. Under concurrent writes, B-Trees rely on page-level latches and write-ahead logs (WAL) to maintain consistency.

Let's look at a PostgreSQL table designed for B-Tree efficiency. Primary keys and frequently filtered columns belong in indexes; the trick is keeping index fan-out high so tree depth stays shallow.

<script src="https://gist.github.com/mohashari/2f45f9aea3fd4fb63cd757e35330415e.js?file=snippet.sql"></script>

The `INCLUDE` clause demonstrates a B-Tree optimization: by embedding payload columns in a covering index, the query planner can satisfy the entire read from the index leaf page without touching the heap. This trades index size for fewer random I/Os.

## LSM Trees: Write-Optimized, Amortized Reads

An LSM Tree converts random writes into sequential ones. Incoming writes land in an in-memory buffer called a MemTable. When the MemTable fills, it's flushed as an immutable Sorted String Table (SSTable) to disk. Over time, a background compaction process merges and sorts these SSTables into levels. Reads must check the MemTable, then Level 0, then Level 1, and so on — potentially touching multiple files before finding the most recent version of a key. Bloom filters at each level short-circuit most negative lookups, but reads are inherently more expensive than B-Tree point lookups at steady state.

RocksDB exposes its compaction strategy through a rich configuration surface. Here's a Go snippet using the `grocksdb` bindings to open a database tuned for write-heavy workloads:

<script src="https://gist.github.com/mohashari/2f45f9aea3fd4fb63cd757e35330415e.js?file=snippet-2.go"></script>

The relationship between `WriteBufferSize`, `Level0FileNumCompactionTrigger`, and `MaxBytesForLevelBase` is the central tuning triangle of any LSM engine. Push write buffers too small and you flood Level 0; push them too large and you delay visibility and increase recovery time after a crash.

## Write Amplification: The Hidden Cost

Both trees amplify writes, but in opposite directions. B-Trees amplify because modifying a single key potentially dirties an entire 8KB page plus its WAL record. LSM Trees amplify because a key written once may be rewritten dozens of times across compaction rounds as it migrates from L0 to Lmax.

You can measure write amplification empirically in RocksDB by sampling the statistics ticker:

<script src="https://gist.github.com/mohashari/2f45f9aea3fd4fb63cd757e35330415e.js?file=snippet-3.sh"></script>

In PostgreSQL, write amplification shows up as WAL volume relative to actual data change:

<script src="https://gist.github.com/mohashari/2f45f9aea3fd4fb63cd757e35330415e.js?file=snippet-4.sql"></script>

## Read Amplification and Bloom Filters

LSM read paths scan multiple SSTables per level. Bloom filters eliminate most disk reads for absent keys by probabilistically answering "this key definitely does not exist here." The false positive rate determines how much wasted I/O you trade for memory. A 1% FPR Bloom filter costs roughly 10 bits per key.

Here's how to benchmark the difference in Go using a synthetic workload — important for validating that your tuning choices actually help your access pattern:

<script src="https://gist.github.com/mohashari/2f45f9aea3fd4fb63cd757e35330415e.js?file=snippet-5.go"></script>

## Choosing Based on Access Patterns

The decision matrix is straightforward once you characterize your workload. Use a B-Tree engine (PostgreSQL, MySQL InnoDB) when your reads and writes are balanced, your data is frequently updated in place, and you need strong transactional semantics with predictable read latency. Use an LSM engine (RocksDB, Cassandra, TiKV) when writes dramatically outnumber reads, your keys are mostly append-only or time-series shaped, and you can tolerate slightly higher read latency in exchange for dramatically higher write throughput.

<script src="https://gist.github.com/mohashari/2f45f9aea3fd4fb63cd757e35330415e.js?file=snippet-6.yaml"></script>

The deepest insight from studying storage engines is that there is no universally better structure — only better fits. A time-series metrics pipeline writing millions of events per second will be crushed by B-Tree page contention but will thrive on RocksDB's sequential flush model. A financial ledger with complex joins and point-in-time queries will suffer under LSM's read amplification but will leverage PostgreSQL's buffer pool and index efficiency beautifully. Knowing which machine sits beneath your database lets you stop treating performance problems as mysteries and start treating them as predictable consequences of structural choices — ones you can measure, tune, and reason about systematically.