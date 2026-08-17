---
layout: post
title: "Implementing a Custom PostgreSQL Storage Engine using the Table Access Method API"
date: 2026-08-17 08:00:00 +0700
tags: [postgresql, database-internals, systems-programming, c, database-engines]
description: "Learn how to build a high-performance, write-optimized custom storage engine in PostgreSQL using the Table Access Method (TAM) API to slash write amplification."
image: "https://picsum.photos/seed/5861/1080/720"
thumbnail: "https://picsum.photos/seed/5861/400/300"
---

At a scale of 150,000 writes per second, standard PostgreSQL heap storage (`heapam`) starts to show its architectural limits. The classic MVCC model—where updates write new tuple versions to 8KB pages and leaves autovacuum to clean up the dead tuples later—creates significant write amplification, leading to SSD degradation, bloated shared buffers, and runaway IOPS costs. When handling high-throughput telemetry, append-only logs, or columnar time-series workloads, forcing data into the default heap page layout is an expensive anti-pattern. While Foreign Data Wrappers (FDW) provide a path to query external data, they do not integrate with Postgres' native index access methods, buffer manager, or transaction pipeline. The Table Access Method (TAM) API, introduced in PostgreSQL 12, solves this by decoupling the query executor from physical storage, allowing developers to plug in custom storage engines—such as columnar stores, log-structured merge-trees (LSM), or memory-mapped tables—directly into the engine core, reducing disk writes by up to 8x and cutting query latency for analytical aggregation by orders of magnitude.

![Implementing a Custom PostgreSQL Storage Engine using the Table Access Method API Diagram](/images/diagrams/implementing-custom-postgresql-storage-engine-table-access-method-api-c.svg)

## The Table Access Method (TAM) Architecture

PostgreSQL's execution engine is built around the concept of abstract operations on tables. The planner generates a tree of execution nodes (e.g., `SeqScan`, `IndexScan`, `ModifyTable`), which execute by requesting tuples from the access method layer. Prior to PostgreSQL 12, these nodes were hardcoded to interact with heap storage. The TAM API abstractly encapsulates these interactions into a single structure of function pointers: [`TableAmRoutine`](file:///home/muklis/Documents/exploring/blog/src/logam/logam.c#L20-L50).

When you run a query like `SELECT * FROM telemetry_log`, the executor does not read blocks directly. Instead, it calls the `relation_beginscan` callback to initialize a scan context, repeatedly calls `scan_getnextslot` to retrieve tuples, and finally calls `relation_endscan` to clean up resources. The physical structure of the table—whether it is stored as raw sequential logs, columnar groups, or compressed blocks—is completely hidden behind this interface.

To support this abstraction, the executor communicates using `TupleTableSlot` containers. A slot holds a tuple in a format the executor understands, regardless of the underlying storage layout. For instance, the default heap engine uses `TTSOpsBufferHeapTuple` slots, which pin a shared buffer holding the physical heap page. If your engine bypasses the buffer pool or uses an in-memory layout, you can use `TTSOpsVirtual` or define a custom `TupleTableSlotOps` structure to avoid shared buffer lock contention.

## Designing a Minimal Custom Storage Engine: Append-Only Log Store

To demonstrate the power of the TAM API, we will design and implement a minimal, write-optimized, append-only log storage engine named `logam`. Unlike the default heap storage, which maintains free space maps and inserts rows into empty slots across arbitrary pages, `logam` appends data sequentially to the end of the file. It omits page-level transaction visibility headers on disk to save space, relying on a lightweight header structure and generic WAL logging.

The implementation consists of a PostgreSQL C extension. The core files include the main implementation [`logam.c`](file:///home/muklis/Documents/exploring/blog/src/logam/logam.c) and its header file [`logam.h`](file:///home/muklis/Documents/exploring/blog/src/logam/logam.h).

Below, we detail the core architecture, registration callbacks, scanning mechanisms, DML operations, and transaction integration.

## Extension Initialization & Handler Setup

Every custom storage engine must register itself via a handler function. This function returns a static [`TableAmRoutine`](file:///home/muklis/Documents/exploring/blog/src/logam/logam.c#L20-L50) struct populated with the function pointers for each callback.

<script src="https://gist.github.com/mohashari/48c949ac9b5eedf2172e3bdd558e2915.js?file=snippet-1.txt"></script>

The [`logam_slot_callbacks`](file:///home/muklis/Documents/exploring/blog/src/logam/logam.c#L40-L45) returns a pointer to `TTSOpsBufferHeapTuple`. This specifies that the query executor should allocate buffer-backed heap tuple slots when executing operations on our tables. This is the simplest path to compatibility because it integrates cleanly with PostgreSQL's expression evaluation engine and existing B-tree index access methods.

## Table Creation & Physical Allocation

When a user executes `CREATE TABLE ... USING logam`, the engine triggers the [`relation_set_new_filenode`](file:///home/muklis/Documents/exploring/blog/src/logam/logam.c#L60-L80) callback. Here, we must initialize the physical storage. PostgreSQL tables are represented as one or more "forks" on disk. The main fork contains the actual data, while other forks handle free space maps (`FSM`) or visibility maps (`VM`). Because `logam` is a sequential append-only log, we omit FSM and VM forks entirely and initialize only the main data file via the Storage Manager (`smgr`) interface.

<script src="https://gist.github.com/mohashari/48c949ac9b5eedf2172e3bdd558e2915.js?file=snippet-2.txt"></script>

By pre-allocating page block 0, we ensure that insertion logic doesn't fail on an empty file boundary. The `smgrextend` call writes an empty page of size `BLCKSZ` (typically 8KB) directly through the operating system's filesystem interface, skipping the shared buffers cache during the initial creation phase to prevent cache pollution.

## Implementing Scan Operations

Sequential scanning is the primary method of data retrieval. The [`relation_beginscan`](file:///home/muklis/Documents/exploring/blog/src/logam/logam.c#L90-L115) function constructs a scan descriptor where we keep track of the current block, tuple offset, and pinned shared buffer. The core retrieval logic lies within the [`scan_getnextslot`](file:///home/muklis/Documents/exploring/blog/src/logam/logam.c#L120-L150) callback.

<script src="https://gist.github.com/mohashari/48c949ac9b5eedf2172e3bdd558e2915.js?file=snippet-3.txt"></script>

The loop inside [`logam_scan_getnextslot`](file:///home/muklis/Documents/exploring/blog/src/logam/logam.c#L120-L150) shows how the custom engine reads blocks into the shared buffer pool using `ReadBuffer`. Locking the buffer with `BUFFER_LOCK_SHARE` is essential to prevent another transaction from updating or writing to the block concurrently while we decode the offset entries. Once a visible tuple is found, we call `ExecStoreBufferTuple` to associate the memory directly with the shared buffer, preventing Postgres from copying the tuple payload into private backend memory.

## Implementing Tuple Insertion

Writing data sequentially requires identifying the last page block, checking if it has enough space, and if not, extending the relation with a new block. To maintain ACID guarantees, we log writes using the Generic WAL (`GenericXLog`) interface.

<script src="https://gist.github.com/mohashari/48c949ac9b5eedf2172e3bdd558e2915.js?file=snippet-4.txt"></script>

The insert method [`logam_tuple_insert`](file:///home/muklis/Documents/exploring/blog/src/logam/logam.c#L160-L210) serializes the row, checks page boundary space, and performs standard `PageAddItem` operations. A crucial detail is wrapping the state changes in a `START_CRIT_SECTION()` block. If a crash occurs between `MarkBufferDirty` and logging the WAL record, Postgres will panic and shut down to prevent disk corruption. The `GenericXLog` functions track modifications on `layout_page` and automatically write standard diff-based WAL records.

## Transaction Visibility & MVCC Checks

To integrate with PostgreSQL's transactional system, our engine must implement the [`tuple_satisfies_snapshot`](file:///home/muklis/Documents/exploring/blog/src/logam/logam.c#L220-L245) callback. This routine determines if a given row is visible to a reader's specific database snapshot.

<script src="https://gist.github.com/mohashari/48c949ac9b5eedf2172e3bdd558e2915.js?file=snippet-5.txt"></script>

Using `HeapTupleSatisfiesVisibility` allows our engine to handle standard Postgres transaction isolation levels (Read Committed, Repeatable Read, and Serializable) out of the box. If our engine were a memory-mapped columnar engine that bypassed HeapTuple structures, we would instead read transaction OIDs from custom metadata zones and run transactional range queries against the Transaction Status Log (CLOG) via `TransactionIdDidCommit`.

## DML Support: Updates and Deletions

Because `logam` is an append-only log storage engine, updating or deleting rows cannot perform in-place mutations. A delete operations marks a tuple as deleted by setting its transaction `xmax` to the current transaction ID. Updates are implemented as a logical deletion of the old tuple followed by an insertion of a new tuple.

<script src="https://gist.github.com/mohashari/48c949ac9b5eedf2172e3bdd558e2915.js?file=snippet-6.txt"></script>

## Integrating and Compiling the Extension

Once compiled as a shared library, the custom storage engine is declared to the Postgres catalog using SQL.

```sql
-- snippet-7
-- 1. Load the shared library and register handler function
CREATE OR REPLACE FUNCTION logam_handler(internal)
RETURNS table_am_handler
AS 'MODULE_PATHNAME', 'logam_handler'
LANGUAGE C STRICT;

-- 2. Define the new access method
CREATE ACCESS METHOD logam TYPE TABLE HANDLER logam_handler;

-- 3. Create a high-throughput logging table using our new engine
CREATE TABLE device_metrics (
    ts timestamptz NOT NULL,
    device_id uuid NOT NULL,
    cpu_usage double precision,
    mem_usage double precision
) USING logam;

-- 4. Confirm the table uses the custom storage engine
SELECT relname, amname 
FROM pg_class c 
JOIN pg_am am ON c.relam = am.oid 
WHERE relname = 'device_metrics';
```

## Production Failure Modes & Performance Bottlenecks

Implementing custom storage extensions inside PostgreSQL introduces production risks that developers must address to prevent data corruption and database crashes.

### 1. The WAL Amplification Overhead of GenericXLog
While using `GenericXLog` makes engine development safe and straightforward, it carries a high cost. Standard heap writes record small diffs to the WAL logs. `GenericXLog`, however, records entire page states or large block segments if it cannot compute clean diffs. Under heavy concurrent writes, this increases WAL generation rates by 3x to 5x. 

For high-performance production workloads, you must bypass `GenericXLog` and implement a Custom Resource Manager (`Rmgr`). This requires:
- Requesting a custom Resource Manager ID (via PG extension registration ranges).
- Writing a custom WAL parser to redo logging changes during crash recovery.
- Recompiling PostgreSQL with your extension built-in, as custom resource managers cannot be dynamically loaded at runtime in standard PostgreSQL distributions.

### 2. TID Mapping and Index Split Collisions
PostgreSQL index structures (such as B-tree and GiST) do not know about custom physical data layouts. They expect a 6-byte Tuple Identifier (`TID`), which represents a 4-byte `BlockNumber` and a 2-byte `OffsetNumber`. 

If your custom engine uses variable-length columnar blocks or compresses data sequentially, there is no physical page-offset alignment. You are forced to virtualize TIDs. Generating virtual TIDs introduces two critical issues:
- **TID Indirection Overhead**: You must maintain an in-memory index or hash table mapping virtual TIDs to physical byte locations, introducing lookup latency.
- **Index Split Collisions**: If your layout shifts records dynamically (for example, during background compaction or segment merging), you must rewrite all downstream index pointers. In standard heap, Heap-Only Tuple (`HOT`) updates mitigate this by chaining pages. Without a similar custom pointer chain, a simple background compaction can cause massive index bloat.

### 3. Autovacuum and Page-level Locking Deadlocks
The autovacuum daemon reads metadata directly from `pg_class` and queries the physical storage engine to check for bloat. Because `logam` does not register a `relation_vacuum` callback in our first-pass pointer struct, autovacuum will bypass this table entirely. Over time, dead tuples (those with visible `xmax` values) will accumulate on disk without being reclaimed, leading to disk space exhaustion.

To support space reclamation, your custom engine must implement `relation_vacuum`. The vacuum thread must acquire exclusive page locks to consolidate blocks. This introduces locking deadlocks: if a long-running transaction holds a shared buffer lock via a query scan while `relation_vacuum` attempts to lock and rearrange pages, the query engine can stall, blocking incoming application connections.

## Conclusion

Custom storage engines built on PostgreSQL's TAM API offer a robust path for optimizing database performance. By bypassing the heap structure, engines can be tailored to match the hardware footprint of specific workloads. However, bypassing Postgres defaults shifts the responsibility of lock safety, WAL efficiency, index consistency, and visibility management to the extension code. When designed carefully, custom storage engines can transform PostgreSQL into a specialized database engine that meets the demands of high-throughput production environments.