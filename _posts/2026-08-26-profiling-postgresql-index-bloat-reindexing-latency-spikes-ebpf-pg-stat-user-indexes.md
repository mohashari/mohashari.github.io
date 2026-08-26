---
layout: post
title: "Profiling PostgreSQL Index Bloat and Reindexing Latency Spikes Using Linux eBPF and pg_stat_user_indexes"
date: 2026-08-26 08:00:00 +0700
tags: [postgresql, ebpf, database-observability, performance-tuning]
description: "Detect PostgreSQL B-Tree index bloat, analyze write amplification, and profile real-time reindexing block I/O latency bottlenecks using Linux eBPF."
image: "https://picsum.photos/seed/3633/1080/720"
thumbnail: "https://picsum.photos/seed/3633/400/300"
---

In high-throughput PostgreSQL databases processing upwards of 10,000 write operations per second, silent index bloat is a production killer that masquerades as slow query execution. Under PostgreSQL’s Multi-Version Concurrency Control (MVCC) architecture, update and delete operations do not mutate rows in-place; they write new tuple versions and mark older versions as dead. If your update-heavy workloads bypass Heap-Only Tuple (HOT) optimizations, every write forces a new index entry to be created. As autovacuum cleans up dead heap tuples, the corresponding index pointers are marked as reusable, but the physical storage pages allocated to the B-Tree index are rarely reclaimed or merged. This leaves you with sparse, bloated index pages that force the database engine to load massive amounts of redundant data into `shared_buffers` during read queries. When database operators reactively trigger a `REINDEX CONCURRENTLY` or run `pg_repack` to reclaim space, the resulting sequential table scans, sorting operations, and massive WAL generation can saturate block storage IOPS, causing catastrophic read/write latency spikes and connection pool exhaustion.

## Quantifying Index Bloat: Beyond the Basics

To understand index bloat, we must examine the physical structure of a B-Tree page in PostgreSQL. Each page is a fixed size (typically 8KB). It consists of a page header (24 bytes), followed by an array of line pointers (4 bytes each) pointing to the actual index tuples (which contain the indexed key and the physical Heap TID). When updates trigger page splits, PostgreSQL allocates a new page and shifts roughly half of the keys to it. If the workload contains high delete rates or frequent updates to indexed columns, these pages become sparse. Autovacuum cannot consolidate sparse index pages unless a page becomes completely empty, at which point it is placed in the index Free Space Map (FSM) but still not returned to the operating system filesystem.

The standard catalog views, such as `pg_stat_user_indexes`, do not record page-level density. They track read operations (`idx_scan`, `idx_tup_read`, `idx_tup_fetch`) but are blind to the physical layout. To accurately quantify bloat, we must calculate the theoretical minimum page requirement based on active tuples and compare it to the actual page count stored in `pg_class.relpages`.

<script src="https://gist.github.com/mohashari/4fc5f82337c27fcab3ba015145348de7.js?file=snippet-1.sql"></script>

Identifying physical bloat is only the first step. You must cross-reference this information with actual usage statistics. A heavily bloated index that is never scanned should be dropped entirely rather than reindexed, eliminating write overhead. Conversely, a bloated index that serves thousands of scans per second must be prioritized for reindexing. We can combine `pg_stat_user_indexes` (usage statistics) and `pg_statio_user_indexes` (buffer cache and disk read metrics) to build a clear picture of index read/write efficiency.

<script src="https://gist.github.com/mohashari/4fc5f82337c27fcab3ba015145348de7.js?file=snippet-2.sql"></script>

Analyze the `read_write_efficiency` metric. If an index has high write overhead but a value near 0, it means your writes are paying a tax to update an index that the query planner rarely selects. Drop it. If the index size is massive, cache hits are low, and scans are high, reindexing will likely restore query performance by shrinking the physical disk footings and reducing index leaf node traversals.

## Tracing the Reindexing Latency Spikes at the OS Level

The PostgreSQL storage engine interacts with the operating system storage using the virtual file descriptor (VFD) cache and the `smgr` (storage manager) interface. Functions like `smgrwrite` write relation blocks to the filesystem, while `smgread` reads them. During a concurrent reindexing process, the backend worker process executes a sequential scan of the table, builds the index tuples in memory, and writes them to the new index relation using `smgrwrite`.

If the system's disk write bandwidth or queue depth is saturated, these `smgrwrite` calls block. This blocking spreads: other backend processes executing transactions must also write dirty buffers or read data blocks, and they block on the same physical I/O resources. Standard tools like `iotop` or `iostat` only present a system-wide or aggregate disk snapshot. They cannot tell you if a specific Postgres query is blocking inside the database code itself or how long a write call is taking at the interface boundaries.

To bridge this visibility gap, we use Linux eBPF (Extended Berkeley Packet Filter). We can attach userspace probes (`uprobes`) to the Postgres binary. In `bpftrace`, we can target `smgrwrite` and `smgread` to profile the exact latency distribution of storage manager writes.

<script src="https://gist.github.com/mohashari/4fc5f82337c27fcab3ba015145348de7.js?file=snippet-3.txt"></script>

When running this `bpftrace` script during a `REINDEX CONCURRENTLY` run, look closely at the shape of `@write_lat_hist_us`. A healthy SSD setup should show a distribution concentrated under 2,000 microseconds (2ms). If you see a bimodal distribution with a second peak above 20,000 microseconds (20ms), it indicates storage saturation or page cache writeback throttling in the Linux kernel.

To determine if the latency is a database-level bottleneck (e.g. file lock contention) or a physical hardware bottleneck, we must trace the block device requests. We can use eBPF’s BCC toolset to attach to the kernel’s block layer tracepoints: `block_rq_issue` (when a block request is sent to the physical device) and `block_rq_complete` (when the device driver signals completion). By filtering for processes starting with `postgres`, we isolate the exact storage layer latency contribution of our database workers.

<script src="https://gist.github.com/mohashari/4fc5f82337c27fcab3ba015145348de7.js?file=snippet-4.py"></script>

If the output of this kernel trace mirrors the high latencies found in your userspace `bpftrace` profiling, the kernel itself is backing up waiting for physical block devices to acknowledge writes. This points to insufficient IOPS or throughput configuration on your cloud storage, or incorrect writeback limits (e.g., `vm.dirty_background_ratio` and `vm.dirty_ratio` being too high, causing the OS to flush huge chunks of data all at once rather than continuously).

## Mitigating Reindexing Overhead: Production Strategies

When you run `REINDEX CONCURRENTLY`, PostgreSQL uses a multi-phase validation process:
1. It builds a new index catalog record and does a full sequential scan of the table to insert keys. It acquires a `ShareUpdateExclusiveLock`, which blocks other schema changes but allows concurrent updates/inserts/deletes.
2. It scans the table again to catch up with any changes made during the first phase.
3. It validates the new index and marks the old index as invalid.
4. It drops the old index.

Because it must wait for all transactions that started before Phase 2 to complete, any long-running transactions (such as analytical queries or dangling application transactions) will block the index build. While the command blocks, it holds a lock. If you issue multiple DDL commands or if subsequent transactions wait for this lock, you can exhaust your application's connection pool.

To run a safe concurrent reindex, you must execute the steps outside of a transaction block, configure strict timeouts, and clean up if the operation fails.

<script src="https://gist.github.com/mohashari/4fc5f82337c27fcab3ba015145348de7.js?file=snippet-5.sh"></script>

Note that `REINDEX CONCURRENTLY` cannot run inside a standard SQL transaction block (such as a `BEGIN/COMMIT` or a PL/pgSQL block). Doing so throws a runtime exception because Postgres must commit transaction phases internally to move between scans. Therefore, session parameters like `lock_timeout` must be passed via the `PGOPTIONS` environment variable when invoking the connection client.

In modern service architectures, you should automate this. A cron job or worker thread can execute index maintenance during off-peak hours. However, hardcoding a time is not enough. The worker must be context-aware: it should inspect the database's health, replication lag, and active transaction count before running the build. If a replica is lagging behind or the primary is handling a traffic surge, the worker should gracefully back off.

<script src="https://gist.github.com/mohashari/4fc5f82337c27fcab3ba015145348de7.js?file=snippet-6.go"></script>

By putting these checks, scripts, and profiling routines in place, you move away from reactive database tuning and towards proactive, zero-downtime storage management. You can spot the bloat, confirm its dynamic impact, and schedule safe rebuilds without degrading active user traffic.