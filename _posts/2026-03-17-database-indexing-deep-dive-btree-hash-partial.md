---
layout: post
title: "Database Indexing Deep Dive: B-Trees, Hash Indexes, and Partial Indexes"
date: 2026-03-17 07:00:00 +0700
tags: [databases, indexing, postgresql, performance, internals]
description: "Explore how B-tree, hash, GIN, and partial indexes work under the hood and develop a systematic strategy for choosing the right index for every query pattern."
---

Every production database eventually hits the same wall: queries that ran in milliseconds during development suddenly take seconds under real load. You add an index, performance improves, and you move on — but most engineers never develop a mental model for *why* a particular index helps, when it hurts, or which type to reach for. The result is systems littered with unused indexes that slow down writes, missing indexes that kill reads, and a vague unease that the database is somehow working against you. Understanding how indexes are structured internally transforms indexing from guesswork into a deliberate engineering discipline.

## B-Tree Indexes: The Default Workhorse

PostgreSQL creates a B-tree index when you run `CREATE INDEX` without specifying a type. A B-tree is a self-balancing tree where every leaf node sits at the same depth, and each internal node stores keys that guide traversal. For a table with 10 million rows, a B-tree typically has 4-5 levels, meaning any single row lookup touches at most 5 pages instead of scanning millions. B-trees support equality (`=`), range queries (`<`, `>`, `BETWEEN`), and `ORDER BY` operations — making them appropriate for roughly 90% of indexing needs.

The key insight is that a B-tree index stores values in sorted order on disk. When PostgreSQL evaluates `WHERE created_at BETWEEN '2025-01-01' AND '2025-03-01'`, it descends the tree to the first matching leaf, then scans forward sequentially. This sequential scan of leaf pages is cache-friendly and fast. The same property enables index-only scans: if the index covers all columns in `SELECT` and `WHERE`, PostgreSQL never touches the heap (the actual table data) at all.

<script src="https://gist.github.com/mohashari/e4d8841017bd245eb8326275801bd933.js?file=snippet.sql"></script>

Column order inside a composite B-tree index matters enormously. The index on `(user_id, status, total_amount)` above supports queries filtering on `user_id` alone, on `user_id + status`, or on all three columns — but it cannot support a query filtering only on `status`. The leftmost prefix rule: PostgreSQL can use the index as long as your predicates form a contiguous prefix of the index's column list.

## Hash Indexes: Fast Equality, Nothing Else

Hash indexes store a hash of each indexed value alongside the heap pointer. A lookup computes the hash, finds the bucket, and returns results in O(1) — theoretically faster than B-tree's O(log n) for pure equality queries. Before PostgreSQL 10, hash indexes weren't WAL-logged and didn't survive crashes, so they were largely avoided. That limitation is gone, but the fundamental constraint remains: hash indexes only support `=` comparisons. No ranges, no sorting, no `LIKE`, no `BETWEEN`.

<script src="https://gist.github.com/mohashari/e4d8841017bd245eb8326275801bd933.js?file=snippet-2.sql"></script>

In practice, B-trees on high-cardinality columns are usually fast enough that hash indexes provide marginal benefit. Reserve hash indexes for UUID or token lookup tables with extremely high read volume where that O(1) vs O(log n) difference is measurable.

## GIN Indexes for Composite Values

Generalized Inverted Indexes (GIN) are designed for columns that contain multiple values per row — arrays, JSONB documents, full-text `tsvector` columns. A GIN index inverts the structure: instead of mapping row → value, it maps each element → set of rows containing it. This makes "does this array contain element X?" queries fast, where a B-tree would require scanning every row.

<script src="https://gist.github.com/mohashari/e4d8841017bd245eb8326275801bd933.js?file=snippet-3.sql"></script>

GIN indexes are larger and slower to build than B-trees, and updates are more expensive because modifying one row may touch many index entries. PostgreSQL uses a "pending list" to batch small updates, flushing them to the main index during `VACUUM` or when the pending list exceeds `gin_pending_list_limit`. For write-heavy workloads, tune this parameter carefully.

## Partial Indexes: Index Only What You Query

A partial index includes only rows satisfying a `WHERE` clause. If your application queries a `jobs` table almost exclusively for rows where `status = 'pending'` — and 99% of rows are `status = 'completed'` — a full index on `status` is enormous and mostly useless. A partial index on only the pending rows is a fraction of the size and fits entirely in shared buffers.

<script src="https://gist.github.com/mohashari/e4d8841017bd245eb8326275801bd933.js?file=snippet-4.sql"></script>

Partial indexes compose well with other strategies. You can create a partial covering index — partial predicate plus multiple columns — to handle a specific hot query path with minimal storage overhead.

## Diagnosing Index Usage in Go Services

Understanding which indexes are used (or unused) in a running service is an operational discipline, not just a schema design concern. In Go services backed by PostgreSQL, use `pg_stat_user_indexes` and `EXPLAIN (ANALYZE, FORMAT JSON)` to build visibility into index health.

<script src="https://gist.github.com/mohashari/e4d8841017bd245eb8326275801bd933.js?file=snippet-5.go"></script>

## Choosing the Right Index: A Decision Framework

With the internals clear, the decision process becomes systematic. Start with the query pattern: equality-only on a high-cardinality column suggests a B-tree (or hash if volume is extreme); range queries, sorting, or prefix matching need a B-tree; composite values — arrays, JSONB, full-text — need GIN. Then ask whether a partial index can reduce size. Finally, check whether the index can be made covering by adding `INCLUDE` columns, enabling index-only scans.

<script src="https://gist.github.com/mohashari/e4d8841017bd245eb8326275801bd933.js?file=snippet-6.sql"></script>

The goal is never to have the most indexes — it's to have exactly the right ones. Every index you create is a write-time tax paid on every `INSERT`, `UPDATE`, and `DELETE` to that table. An unused 2GB index on a high-write table is actively harmful. Run the unused index query above against any production database and you will almost certainly find candidates for removal. Build indexes intentionally, verify they're used with `EXPLAIN ANALYZE`, and audit `pg_stat_user_indexes` regularly. Indexing is not a one-time setup task — it's an ongoing conversation between your query patterns and your schema.