---
layout: post
title: "PostgreSQL Table Partitioning: Strategies for Billion-Row Tables"
date: 2026-03-23 08:00:00 +0700
tags: [postgresql, database, performance, backend, partitioning]
description: "Range, list, and hash partitioning strategies for billion-row PostgreSQL tables—with a zero-downtime migration framework."
---

Your `events` table hit 800 million rows last Tuesday. Vacuum hasn't finished in three days, your BRIN indexes are useless because writers scatter hot rows across the heap, and the query planner is choosing sequential scans on a 400GB table because your statistics are stale. You add more indexes, bloat gets worse, autovacuum falls further behind, and the whole thing compounds. Partitioning isn't a performance optimization at this scale—it's infrastructure hygiene. This post covers the three partitioning strategies PostgreSQL offers, how to choose the right one for your access pattern, the silent failure mode that kills partition pruning, and a concrete migration path that doesn't require a maintenance window.

## What PostgreSQL Declarative Partitioning Actually Does

PostgreSQL's declarative partitioning (introduced in PG 10, production-ready by PG 11) creates a parent table that acts as a logical routing layer. The parent holds no rows—all data lives in child partition tables. The planner uses partition metadata to prune irrelevant partitions from query plans, which is the entire value proposition.

Three mechanisms matter in practice:

**Partition pruning at plan time** — when the partition key condition is a literal value or a stable function result, the planner eliminates irrelevant partitions before execution begins. A query like `WHERE created_at >= '2026-01-01'` on a range-partitioned table by month might go from scanning 36 partitions down to 3.

**Runtime pruning** — introduced in PG 11, this handles parameterized queries (prepared statements, functions with parameters). Without it, partition pruning only worked with literal values, making it nearly useless for application workloads.

**Constraint exclusion** — the older mechanism, still relevant for partitions created before declarative partitioning or for inheritance-based setups. It's slower than partition pruning and should be considered a fallback.

## Range Partitioning: The Right Tool for Time-Series Data

Range partitioning is the correct choice when your dominant query pattern filters on a monotonically increasing key—timestamps, sequence IDs, date buckets. It's the strategy you want for audit logs, metrics, event streams, and order history.

<script src="https://gist.github.com/mohashari/2d449ae2e9081caa5e6fb9dd63df7360.js?file=snippet-1.sql"></script>

Always create a default partition. The alternative—a hard error on out-of-range inserts—is a production incident waiting to happen when a timezone bug sends a row with `created_at = '1970-01-01'` or a clock skew pushes a timestamp into an uncreated future partition.

Partition size is a judgment call. Monthly partitions work well for tables that grow 5–50GB/month. Weekly partitions make sense for very high-volume streams. The rule of thumb: each partition should be small enough that `VACUUM` completes in under an hour and the partition's indexes fit in `shared_buffers` during peak query load.

## List Partitioning: Routing by Discrete Values

List partitioning makes sense when you have a bounded set of partition key values and your queries almost always filter by that key. Multi-tenant SaaS applications partitioned by `region` or `tier` are the canonical case. It also works for sharding by enum-like values where each value has meaningfully different data characteristics.

<script src="https://gist.github.com/mohashari/2d449ae2e9081caa5e6fb9dd63df7360.js?file=snippet-2.sql"></script>

The failure mode for list partitioning is cardinality creep. If your `region` column starts with 3 values and grows to 200 over 18 months, you've built yourself a management nightmare. List partitioning has a hard ceiling around 10–20 distinct values before operational overhead (partition creation, DDL on each partition, monitoring) outweighs the benefit. Beyond that, hash partitioning is usually the better choice.

## Hash Partitioning: Even Distribution When You Have No Natural Boundaries

Hash partitioning distributes rows based on a hash of the partition key, giving you even data distribution without needing a natural range or discrete categories. Use it when you have a high-cardinality key (user IDs, UUIDs) and your queries don't have a natural hot partition—you just want to cut table size for vacuum, index maintenance, and parallel query performance.

<script src="https://gist.github.com/mohashari/2d449ae2e9081caa5e6fb9dd63df7360.js?file=snippet-3.sql"></script>

Hash partitioning eliminates partition pruning for range queries—`WHERE user_id BETWEEN 1 AND 1000` can't prune hash partitions because the planner can't know which buckets contain which ID ranges. If your queries use point lookups (`WHERE user_id = $1`), pruning works perfectly. If they use ranges, reconsider your strategy.

You also cannot add hash partitions later without a full table rewrite. Plan your modulus upfront. 16 partitions is a reasonable floor for most tables; 64 is reasonable if you're planning for aggressive growth.

## The Partition Pruning Failure Mode You Will Hit

This is the part that burns teams in production. Partition pruning silently fails when there's a type mismatch between the query predicate and the partition key. PostgreSQL will not error—it will execute correctly, scan all partitions, and your query will be 10x slower than expected. The `EXPLAIN` output is the only way to catch it.

<script src="https://gist.github.com/mohashari/2d449ae2e9081caa5e6fb9dd63df7360.js?file=snippet-4.sql"></script>

The rule: never wrap the partition key column in a function, and always match the exact type. If your partition key is `TIMESTAMPTZ`, your application must pass `TIMESTAMPTZ`—not `TEXT`, not `DATE`, not `TIMESTAMP`. Check this in CI with `EXPLAIN` assertions, not by trusting the ORM.

PostgreSQL 14 improved implicit cast handling for some cases, but the safe path is explicit casts everywhere.

## Indexing Partitioned Tables

Indexes are local to each partition—there's no global index structure in PostgreSQL declarative partitioning (unlike Oracle's global indexes). This is both a feature and a constraint.

<script src="https://gist.github.com/mohashari/2d449ae2e9081caa5e6fb9dd63df7360.js?file=snippet-5.sql"></script>

The local-index-only design means `VACUUM` is also local—each partition vacuums independently. This is the major operational advantage over a monolithic table: autovacuum can make progress on individual partitions concurrently, old partitions can be detached and dropped atomically, and dead tuple bloat is bounded by partition size.

## Zero-Downtime Migration from a Monolithic Table

Retrofitting partitioning onto an existing 800M-row table without downtime requires a specific sequence. The naive approach—`CREATE TABLE ... PARTITION BY ...`, copy data, swap—locks the original table and causes downtime. The production-safe approach uses logical replication or a shadow table with a trigger.

<script src="https://gist.github.com/mohashari/2d449ae2e9081caa5e6fb9dd63df7360.js?file=snippet-6.sql"></script>

This migration takes hours to days depending on data volume, but the application sees no downtime. The trigger adds ~5–15% write overhead during the migration window—acceptable for most workloads, but monitor write latency carefully.

For very high write rates (>50K inserts/second), consider using logical replication with `pglogical` or Postgres 16's built-in logical replication to the partitioned target instead of a trigger. The trigger approach serializes writes; logical replication can be tuned for parallelism.

## Partition Management in Production

Declarative partitioning doesn't manage itself. You need automation for partition creation and archival.

<script src="https://gist.github.com/mohashari/2d449ae2e9081caa5e6fb9dd63df7360.js?file=snippet-7.sh"></script>

`DETACH PARTITION CONCURRENTLY` (PG 14+) is essential here—the older `DETACH` takes a brief `ACCESS EXCLUSIVE` lock on the parent table. On a high-traffic table, even a brief lock can queue hundreds of connections. Always use the concurrent variant in production.

## Decision Framework

Choose your partitioning strategy based on three questions:

**What does your dominant query predicate look like?**
- Filter on time/sequence → Range
- Filter on discrete category (region, tenant tier, status) with <20 values → List
- Point lookup on high-cardinality key (user_id, UUID) → Hash
- Mixed: range + secondary filter → Range on time, partial indexes on secondary column

**Do you need to archive or drop old data?**
- Yes → Range partitioning. Detaching and dropping old partitions is O(1) and doesn't bloat. Deleting rows from a monolith causes write amplification and bloat.
- No → Hash or List depending on access pattern.

**Is partition key cardinality stable?**
- Yes → List is viable
- No / unbounded → Range or Hash

The most common mistake is using List partitioning on a column that seems categorical but grows over time (tenant IDs, product categories, country codes). Start with Hash partitioning if you have any doubt about future cardinality. Hash partitions are operationally boring, which is what you want.

Partitioning is not a substitute for proper indexing, query tuning, or connection pooling. It's a maintenance and scale enabler—it makes vacuum fast, makes archival cheap, and makes the planner's job tractable. Apply it once your table is large enough that these operational costs are real problems, not as premature optimization. The threshold in practice: above 100GB or 200M rows, or whenever `pg_stat_user_tables.n_dead_tup` for a table is consistently above 10% of live tuples.
```