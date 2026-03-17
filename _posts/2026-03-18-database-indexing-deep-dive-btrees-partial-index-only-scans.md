---
layout: post
title: "Database Indexing Deep Dive: B-Trees, Partial Indexes, and Index-Only Scans"
date: 2026-03-18 07:00:00 +0700
tags: [databases, postgresql, indexing, performance, sql]
description: "Understand how PostgreSQL index types work internally, when to use partial and covering indexes, and how to diagnose missing or bloated indexes in production."
---

Every backend engineer has debugged a slow query at 2am, staring at a `EXPLAIN ANALYZE` output that reveals a sequential scan across 50 million rows. You add an index, the query drops from 4 seconds to 8 milliseconds, and you feel like a wizard. But most engineers stop there — they treat indexes as magic switches rather than data structures with real costs, tradeoffs, and failure modes. Understanding *how* indexes work internally transforms you from someone who cargo-cults `CREATE INDEX` to someone who designs schemas that stay fast under production load.

## How B-Tree Indexes Work Internally

PostgreSQL's default index type is a B-tree (balanced tree). Every leaf node holds a sorted array of `(key, heap_tuple_pointer)` pairs. Internal nodes hold separator keys that route searches down the tree. The "balanced" property guarantees that every leaf is at the same depth, so lookups, range scans, and inserts are all `O(log n)` regardless of data distribution.

This structure has a subtle but critical implication: the index stores keys in sorted order on disk. That's why a B-tree index on `(created_at)` can satisfy `ORDER BY created_at LIMIT 10` without a sort step — the database just reads the first 10 leaf entries.

<script src="https://gist.github.com/mohashari/30763d6a9bc7a31e76b01a0b18faf896.js?file=snippet.sql"></script>

The `Buffers: shared hit=4` tells you this entire operation touched four 8KB pages — roughly 32KB of I/O to serve the query, independent of table size.

## Composite Index Column Order Matters

A composite index on `(a, b)` is sorted first by `a`, then by `b` within each `a` group. This means the index is useful for queries filtering on `a` alone, or on `(a, b)` together, but *not* for queries filtering on `b` alone. The leading column rule is one of the most frequently misunderstood aspects of indexing.

<script src="https://gist.github.com/mohashari/30763d6a9bc7a31e76b01a0b18faf896.js?file=snippet-2.sql"></script>

When you have high-cardinality columns like `user_id`, put them first if your dominant query pattern filters by user. Put low-cardinality columns like `status` first only when most queries filter by status as the primary predicate.

## Partial Indexes: Indexing Only What You Query

A partial index includes only rows matching a `WHERE` condition. If 95% of your orders have `status = 'completed'` and you only ever query `pending` orders, a full index on `status` wastes space and write overhead maintaining entries you never read.

<script src="https://gist.github.com/mohashari/30763d6a9bc7a31e76b01a0b18faf896.js?file=snippet-3.sql"></script>

Partial indexes are particularly powerful for soft-delete patterns (`WHERE deleted_at IS NULL`) and queue-like tables where you repeatedly process a small active subset.

## Covering Indexes and Index-Only Scans

An index-only scan is PostgreSQL's most efficient read path: it answers the query entirely from the index without touching the heap (the actual table). For this to work, every column referenced in the query must be present in the index. The `INCLUDE` clause (available since PostgreSQL 11) lets you add non-key columns to the index leaf pages without affecting sort order.

<script src="https://gist.github.com/mohashari/30763d6a9bc7a31e76b01a0b18faf896.js?file=snippet-4.sql"></script>

`Heap Fetches: 0` is the goal. When this number is non-zero, it usually means the visibility map for those pages hasn't been updated yet — running `VACUUM` will fix it and restore the index-only path.

## Diagnosing Bloat and Unused Indexes in Production

Indexes aren't free. Every `INSERT`, `UPDATE`, and `DELETE` must maintain all indexes on the table. Unused indexes burn write amplification and shared memory with no query benefit. PostgreSQL tracks index usage in `pg_stat_user_indexes`.

<script src="https://gist.github.com/mohashari/30763d6a9bc7a31e76b01a0b18faf896.js?file=snippet-5.sql"></script>

<script src="https://gist.github.com/mohashari/30763d6a9bc7a31e76b01a0b18faf896.js?file=snippet-6.sql"></script>

If `dead_pct` is above 20%, schedule a `REINDEX CONCURRENTLY` during a low-traffic window. Bloated indexes degrade scan performance because PostgreSQL must traverse more pages to find live entries.

Here's a Go helper you might embed in a database health-check service to surface these stats over time:

<script src="https://gist.github.com/mohashari/30763d6a9bc7a31e76b01a0b18faf896.js?file=snippet-7.go"></script>

Indexing strategy is never a one-time decision. The right index for a table at 100K rows may be wrong at 100M rows, and query patterns shift as features ship. Build `EXPLAIN ANALYZE` into your code review process, query `pg_stat_user_indexes` weekly in production, and treat index bloat the same way you treat memory leaks — as a slow bleed that compounds until it becomes a crisis. The engineers who ship consistently fast databases aren't the ones who know the most SQL tricks; they're the ones who instrument, measure, and iterate on their storage layer as a first-class concern.