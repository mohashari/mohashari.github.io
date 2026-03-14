---
layout: post
title: "SQL Query Optimization: Writing Queries That Scale"
tags: [database, sql, performance, postgresql, backend]
description: "Practical SQL query optimization techniques — from understanding execution plans to rewriting slow queries for 100x speedups."
---

A poorly written query that works fine on 1,000 rows becomes a production nightmare at 10 million rows. SQL optimization is one of the highest-leverage skills a backend engineer can have. Here's the essential knowledge.

## Understanding Query Execution Order

SQL is declarative — you state what you want, not how to get it. But knowing the logical execution order helps you write better queries:


<script src="https://gist.github.com/mohashari/c17db7b0e61c55b370855f4e7f45de72.js?file=snippet.sql"></script>


This is why you can't use a column alias defined in SELECT within WHERE — WHERE runs before SELECT.

## EXPLAIN ANALYZE: Your Most Important Tool

Never optimize blind. Always profile first:


<script src="https://gist.github.com/mohashari/c17db7b0e61c55b370855f4e7f45de72.js?file=snippet-2.sql"></script>


What to look for:
- `Seq Scan` on a large table → likely missing index
- `actual rows=10000` vs `rows=1` estimate → stale statistics, run `ANALYZE`
- High `Buffers: read` → data not cached, disk I/O bottleneck
- `Sort (...)` with `Disk: true` → sort spilling to disk, increase `work_mem`

## Common Anti-Patterns and Fixes

### Anti-Pattern 1: Function on Indexed Column


<script src="https://gist.github.com/mohashari/c17db7b0e61c55b370855f4e7f45de72.js?file=snippet-3.sql"></script>


### Anti-Pattern 2: Wildcard at Start of LIKE


<script src="https://gist.github.com/mohashari/c17db7b0e61c55b370855f4e7f45de72.js?file=snippet-4.sql"></script>


### Anti-Pattern 3: Implicit Type Conversion


<script src="https://gist.github.com/mohashari/c17db7b0e61c55b370855f4e7f45de72.js?file=snippet-5.sql"></script>


### Anti-Pattern 4: SELECT * in Subqueries


<script src="https://gist.github.com/mohashari/c17db7b0e61c55b370855f4e7f45de72.js?file=snippet-6.sql"></script>


### Anti-Pattern 5: N+1 Queries in Application Code


<script src="https://gist.github.com/mohashari/c17db7b0e61c55b370855f4e7f45de72.js?file=snippet.go"></script>


## Efficient Aggregations


<script src="https://gist.github.com/mohashari/c17db7b0e61c55b370855f4e7f45de72.js?file=snippet-7.sql"></script>


## Window Functions: Powerful, Often Overlooked


<script src="https://gist.github.com/mohashari/c17db7b0e61c55b370855f4e7f45de72.js?file=snippet-8.sql"></script>


## Bulk Operations


<script src="https://gist.github.com/mohashari/c17db7b0e61c55b370855f4e7f45de72.js?file=snippet-9.sql"></script>


## Partitioning for Very Large Tables

When a table exceeds ~100M rows, consider table partitioning:


<script src="https://gist.github.com/mohashari/c17db7b0e61c55b370855f4e7f45de72.js?file=snippet-10.sql"></script>


The most impactful SQL optimizations, in order:
1. Add missing indexes (especially on foreign keys and WHERE columns)
2. Fix N+1 queries
3. Use EXPLAIN ANALYZE and fix what you find
4. Replace expensive queries with materialized views
5. Partition enormous tables

Measure before and after every change. A 50ms query on a small dataset may be fine — context always matters.
