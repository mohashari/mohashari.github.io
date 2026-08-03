---
layout: post
title: "Mitigating PostgreSQL MVCC Write Amplification via Heap-Only Tuples (HOT) Optimization"
date: 2026-08-03 08:00:00 +0700
tags: [postgresql, database-performance, mvcc, indexing, backend-engineering]
description: "Eliminate PostgreSQL index write amplification and page bloat by understanding and tuning Heap-Only Tuples (HOT) and table fillfactors."
image: "https://picsum.photos/seed/8496/1080/720"
thumbnail: "https://picsum.photos/seed/8496/400/300"
---

In high-throughput write environments—such as updating user session heartbeats, incrementing real-time inventory counters, or updating state fields in task queues—PostgreSQL database clusters often suffer from sudden, catastrophic disk I/O bottlenecks and transaction throughput collapses. This performance degradation is rooted in PostgreSQL’s Multi-Version Concurrency Control (MVCC) architecture. Because Postgres implements updates by marking the old row version (tuple) as deleted and inserting a new version elsewhere in the heap, the physical location of the row (Tuple Identifier, or TID) changes. Under standard operation, this change forces PostgreSQL to update every single index on the table to point to the new TID—even if the modified columns are completely unrelated to those indexes. For tables with five or more indexes, this "write amplification" multiplies disk write volume, pollutes the `shared_buffers` cache with dirty index pages, triggers massive write-ahead log (WAL) volume, and traps the database in a perpetual loop of autovacuum choke.

![Mitigating PostgreSQL MVCC Write Amplification via Heap-Only Tuples (HOT) Optimization Diagram](/images/diagrams/mitigating-postgresql-mvcc-write-amplification-heap-only-tuples-optimization.svg)

## The Mechanics of Write Amplification in Postgres MVCC

To appreciate how Heap-Only Tuples (HOT) resolve this issue, we must first examine what occurs under the hood during a standard update. In PostgreSQL, tables are stored as arrays of fixed-size pages (typically 8KB). Each page contains a page header, an array of line pointers (also called item pointers), and the actual tuple data (heap tuples). 

Indexes, such as standard B-Trees, contain search keys paired with a TID. The TID is a 6-byte value consisting of two parts: the block (page) number and the line pointer offset within that page (e.g., `(page 45, item 3)`). 

When a standard update is executed:
1. PostgreSQL writes a new version of the row (v2) into a page that has free space. This might be a completely different page or the same page.
2. The old version (v1) has its header field `t_xmax` set to the current transaction ID, marking it as invisible to future transactions.
3. The new version (v2) has its header field `t_xmin` set to the current transaction ID.
4. Because the new version resides at a new location, it has a new TID (e.g., `(page 46, item 1)`).
5. PostgreSQL must now insert a new entry into *every* index defined on the table, pointing to the new TID.

If you have a table with 10 million rows, 5 B-Tree indexes, and a write load of 1,000 updates per second, a single update requiring index modifications generates:
* 1 heap page modification (writing the new tuple).
* 5 index page modifications (inserting 5 new index pointers).

This equates to 6 page writes for a single row change—a 6x database-level write amplification. If these index modifications cause B-Tree leaf pages to split, the actual physical disk write volume scales even higher. Furthermore, these index updates pollute PostgreSQL's `shared_buffers`. Modified index pages are marked as dirty, forcing the background writer or checkpoint processes to flush them to disk, which consumes valuable I/O operations per second (IOPS).

To identify if your production database is suffering from this write amplification, you can query the PostgreSQL statistics collector. The view `pg_stat_user_tables` contains metrics tracking total updates versus HOT updates.

<script src="https://gist.github.com/mohashari/04d9fc0d70245567f29f66ac5a30b476.js?file=snippet-1.sql"></script>

A low `hot_update_ratio_percentage` (e.g., below 80%) on a table with high `n_tup_upd` indicates that the vast majority of updates are modifying index pointers, creating a prime target for optimization.

## Enter Heap-Only Tuples (HOT): The Savior of I/O

Introduced to solve this MVCC limitation, Heap-Only Tuples (HOT) eliminate index updates when row modifications do not alter indexed columns. 

When a HOT update occurs, the new tuple version is placed on the **exact same page** as the old version. Instead of inserting a new pointer in the indexes, PostgreSQL creates a redirection chain directly within the page's line pointer array.

1. The index continues to point to the original line pointer (the "HOT Root"), which remains at its original offset (e.g., `item 1`).
2. PostgreSQL marks the original line pointer as redirected (`LP_REDIRECT`), pointing to the line pointer of the new tuple version (e.g., `item 2`) on the same page.
3. The new tuple (v2) is marked with the `HEAP_ONLY_TUPLE` flag in its `t_infomask` header, signifying that no index entry points directly to it.
4. The old tuple (v1) is marked with the `HEAP_HOT_UPDATED` flag.

When a query searches via the index, it retrieves the TID for `item 1`. Upon reading the page header, the engine sees that `item 1` is redirected to `item 2`. It follows the redirect to retrieve the live tuple (v2) immediately. The index remains entirely untouched.

### The Two Mandatory Pre-requisites for HOT

An update is only eligible for HOT optimization if it meets two strict criteria:

1. **No Indexed Columns are Modified**: The `UPDATE` statement must not change the value of any column covered by *any* index on the table. This includes partial indexes, expression indexes, and columns defined in the `INCLUDE` clause of cover indexes.
2. **Same-Page Space Availability**: There must be sufficient free space on the existing page to accommodate the new tuple version. If the page is full and PostgreSQL must place the new tuple version on a different page, the update falls back to a standard non-HOT update, breaking the chain and requiring new index entries across all indexes.

Below is an example of creating a database schema optimized for HOT updates by isolating indexed search keys from high-frequency updates, and configuring the table's layout parameter.

<script src="https://gist.github.com/mohashari/04d9fc0d70245567f29f66ac5a30b476.js?file=snippet-2.sql"></script>

## The Fatal Roommate: Fillfactor Configuration and Page Space

By default, PostgreSQL tables have a `fillfactor` of 100. This means that during `INSERT` operations, PostgreSQL will pack each 8KB page completely full. 

If a page is filled to 100% capacity:
* Any subsequent `UPDATE` to a row on that page cannot fit its new tuple version on the same page.
* The update is forced to allocate space on a different page.
* The update fails to utilize HOT, and the database must write new entries to all indexes.

To resolve this, we must lower the table's `fillfactor`. Lowering the fillfactor reserves a percentage of space on each page during initial inserts, keeping that space open for future updates.

### Selecting the Right Fillfactor

Choosing the correct fillfactor is a trade-off between read scan efficiency, table size, and write performance:
* **Fillfactor 100 (Default)**: Best for read-heavy tables with very few or no updates. It maximizes data density, meaning fewer pages are loaded into memory during sequential scans.
* **Fillfactor 90–95**: Suitable for tables with light to moderate update volumes.
* **Fillfactor 70–85**: Necessary for high-frequency write-heavy tables. If a row is updated multiple times between autovacuum runs, lowering the fillfactor to 80 reserves 20% of the page, allowing several generations of updates to stay on-page.

Lowering fillfactor increases the physical size of the table on disk and in memory because rows are spread across more pages. For example, setting fillfactor to 50 doubles the table's storage footprint. You should only lower fillfactor on tables identified as HOT update bottlenecks.

### Rebuilding Tables to Apply Fillfactor

Altering the fillfactor using `ALTER TABLE` is metadata-only; it does not rewrite existing pages. The reserved space will only apply to new inserts or when existing rows are updated. 

To apply the fillfactor immediately to all existing pages, you must rebuild the table. In high-traffic production systems, running a standard `VACUUM FULL` or `CLUSTER` is unacceptable because they acquire an `ACCESS EXCLUSIVE` lock, blocking all reads and writes. A safer alternative is rebuilding indexes concurrently and using tools like `pg_repack` to rewrite the table online, or detecting index bloat to measure the impact of write amplification:

<script src="https://gist.github.com/mohashari/04d9fc0d70245567f29f66ac5a30b476.js?file=snippet-3.sql"></script>

## Indexing Anti-Patterns: The Silent HOT Killers

A major reason systems fail to achieve high HOT update ratios is the existence of unnecessary, redundant, or poorly planned indexes. 

### The `updated_at` Timestamp Trap

A classic mistake is defining an index on an `updated_at` or `last_modified` timestamp column:

```sql
CREATE INDEX idx_user_sessions_updated_at ON user_sessions(updated_at);
```

If your application updates a row, it typically sets `updated_at = NOW()`. Because `updated_at` is indexed, its value changes on every update. This changes the indexed key value, disabling HOT optimization for *all* updates on that table. The database must insert a new leaf node entry in `idx_user_sessions_updated_at` and propagate new pointers to all other indexes, even if no other indexed values changed.

*Solution*: Remove indexes on timestamp columns unless they are absolutely necessary for critical query paths. If they are required for batch jobs, consider replacing them with partial indexes targeting only unprocessed records.

### The Cover Index (`INCLUDE`) Pitfall

Covering indexes, created using the `INCLUDE` clause (introduced in PostgreSQL 11), append payload columns to the leaf nodes of a B-Tree index:

```sql
CREATE INDEX idx_users_email ON users(email) INCLUDE (last_login_ip);
```

While this allows for index-only scans, if the included column `last_login_ip` is updated frequently, HOT is disabled for those updates because the index leaf node contains the column's value and must be rewritten.

To keep HOT operational, run audits to identify write-heavy, scan-light indexes:

<script src="https://gist.github.com/mohashari/04d9fc0d70245567f29f66ac5a30b476.js?file=snippet-4.sql"></script>

## HOT Chain Pruning and Autovacuum Interactions

HOT updates do not build redirection chains infinitely. If a row is updated continuously, you obtain a chain: `v1 (Root) -> v2 -> v3 -> v4`. 

If a HOT chain grows too long, read performance degrades because PostgreSQL must traverse multiple redirections within the page to find the current version. To prevent this, PostgreSQL performs **HOT chain pruning** (also known as single-page cleanup).

### How Single-Page Cleanup Works

During normal operations (such as a standard `SELECT` or `UPDATE` query accessing the page), PostgreSQL checks if any old tuple versions in a HOT chain are no longer visible to any active transaction. 

If they are dead (i.e., their `t_xmax` is older than the oldest active transaction's `xmin`):
1. PostgreSQL removes the dead intermediate tuples from the page.
2. It defragments the page storage, shifting live tuples together to create contiguous free space.
3. The root line pointer is redirected to point directly to the oldest visible tuple (e.g., `item 1` now points directly to `item 4`).

This cleanup process occurs entirely in-memory within `shared_buffers` and is extremely fast. It does not acquire heavy locks or write to indexes.

### The Impact of Long-Running Transactions

Single-page pruning relies on the database being able to determine that a tuple is dead. If there are long-running transactions (such as analytical queries, unclosed developer client sessions, or stalled replication slots), the database's minimum active transaction ID (`xmin`) remains old. 

As long as a transaction is active, PostgreSQL cannot prune any tuple version created after that transaction started. As updates continue:
* The page fills up with unprunable dead tuples.
* HOT updates fail due to lack of page space.
* The system reverts to standard updates, causing index write amplification and index bloat.

To prevent this failure mode, monitor transactions that are holding back the database `xmin` age:

<script src="https://gist.github.com/mohashari/04d9fc0d70245567f29f66ac5a30b476.js?file=snippet-5.sql"></script>

If replication slots or long-lived idle transactions are blocking cleanup, you must terminate them or configure database boundaries like `max_standby_archive_delay` and `max_standby_streaming_delay`.

To monitor the performance of your optimizations in real-time, you can execute a differential query against the statistics collector during load testing or production deployments.

<script src="https://gist.github.com/mohashari/04d9fc0d70245567f29f66ac5a30b476.js?file=snippet-6.sql"></script>

## The ORM Anti-Pattern: Full-Row Updates

Object-Relational Mapping (ORM) frameworks like GORM, Hibernate, or Prisma are common contributors to write amplification. By default, many ORMs perform updates by sending all fields of an entity back to the database, even if only a single, non-indexed field changed:

```sql
-- ORM default behavior: updates all fields
UPDATE users SET email = 'user@example.com', password_hash = '...', last_login_ip = '192.168.1.50', updated_at = '2026-08-03 08:00:00' WHERE id = 123;
```

Even if `email` and `updated_at` did not change, their inclusion in the `SET` clause can force PostgreSQL to verify their index status. If any of those fields are indexed, the update may fail to utilize HOT.

To prevent this, you must configure your application or ORM to perform **selective updates** (sometimes called dynamic updates), ensuring only changed columns are sent in the `UPDATE` query:

<script src="https://gist.github.com/mohashari/04d9fc0d70245567f29f66ac5a30b476.js?file=snippet-7.go"></script>

By ensuring your application layer only sends modified columns to the database, and by pairing this with a tuned `fillfactor` on write-heavy tables, you allow PostgreSQL to utilize HOT updates effectively. This reduces disk write volume, stabilizes autovacuum frequency, and prevents index bloat from degrading performance in production.