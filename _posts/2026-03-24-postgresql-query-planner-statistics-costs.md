---
layout: post
title: "PostgreSQL Query Planner Internals: Statistics, Costs, and Plan Forcing"
date: 2026-03-24 08:00:00 +0700
tags: [postgresql, databases, performance, backend, sql]
description: "How PostgreSQL's query planner makes decisions, why it gets them wrong, and how to force better plans in production."
image: ""
thumbnail: ""
---

You're three weeks post-launch. A query that ran in 8ms during staging is now taking 4 seconds in production. The schema is identical. The indexes are identical. The query is identical. What changed? Data. The planner made a cost estimate based on stale statistics, chose a nested loop join over a hash join, and your users are staring at a spinner. This is not a hypothetical — it's the most common class of production performance regression I've seen in Postgres-backed systems, and understanding the planner's internals is the only reliable way to diagnose and fix it.

## How the Planner Actually Works

Before you can fix plan instability, you need a mental model of how Postgres picks a plan. The planner is a cost-based optimizer. It enumerates candidate execution strategies, assigns a cost to each, and picks the cheapest. Cost is measured in arbitrary units where 1.0 = the cost of reading one 8kB page from disk (`seq_page_cost`). CPU operations are cheaper than I/O by default (`cpu_tuple_cost = 0.01`).

The critical insight: **cost estimates are only as good as statistics**. The planner doesn't run your query to find out how many rows a predicate will return. It consults `pg_statistic` — a table of histograms, most-common-values, and correlation coefficients built by `ANALYZE`. If those statistics are stale or misleading, the cost model breaks down.

```sql
-- snippet-1
-- Inspect statistics for a column
SELECT
    attname,
    n_distinct,
    correlation,
    most_common_vals,
    most_common_freqs,
    histogram_bounds
FROM pg_stats
WHERE tablename = 'orders'
  AND attname = 'status';

-- n_distinct > 0: exact estimate
-- n_distinct < 0: fraction of total rows (e.g., -0.05 = 5% of rows are distinct)
-- correlation: 1.0 = physically ordered, 0 = random — affects index scan cost
```

The `correlation` value is particularly impactful. If `correlation` for your `created_at` column is 0.97, Postgres knows that an index scan will hit pages in roughly sequential order — cheap. If it's 0.12 after a bulk backfill that inserted rows out of order, the planner may correctly decide the index is worse than a seq scan. Most engineers never look at this number.

## Where Statistics Break Down

**Problem 1: Default statistics target is too low.** `default_statistics_target = 100` means Postgres builds histograms with 100 buckets. For a column with 50 million distinct values and a heavily skewed distribution, 100 buckets means the planner is averaging across ranges that span millions of rows. You get row count estimates that are off by orders of magnitude.

```sql
-- snippet-2
-- Raise statistics target for high-cardinality, skewed columns
ALTER TABLE events ALTER COLUMN user_id SET STATISTICS 500;
ALTER TABLE events ALTER COLUMN event_type SET STATISTICS 500;

-- Then re-analyze to actually collect the new statistics
ANALYZE events;

-- Verify the change took effect
SELECT attname, attstattarget
FROM pg_attribute
WHERE attrelid = 'events'::regclass
  AND attname IN ('user_id', 'event_type');
```

Setting statistics to 500 increases the histogram resolution at the cost of longer `ANALYZE` runs. For a table with 100M rows and critical queries, this trade-off is almost always worth it. Start with 300 for columns appearing in WHERE clauses with range predicates; go to 500 for columns with known multi-modal distributions.

**Problem 2: Multi-column correlations are invisible.** `pg_statistic` stores per-column statistics. If `status = 'pending'` AND `created_at > now() - interval '1 hour'` is highly selective (because pending orders are always recent), the planner doesn't know that — it multiplies the individual selectivities and gets the wrong answer.

This was fixed in PostgreSQL 10 with extended statistics:

```sql
-- snippet-3
-- Create extended statistics for correlated columns
CREATE STATISTICS orders_status_created_stats (dependencies)
ON status, created_at
FROM orders;

-- Also create ndistinct stats if you have GROUP BY on these columns
CREATE STATISTICS orders_status_region_ndistinct (ndistinct)
ON status, region_id
FROM orders;

ANALYZE orders;

-- Verify Postgres is using the extended stats
EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT)
SELECT * FROM orders
WHERE status = 'pending'
  AND created_at > now() - interval '2 hours';
-- Look for "Statistics used: orders_status_created_stats" in the output
```

Extended statistics are underused in production systems. If you have composite indexes and still see bad row estimates on multi-column predicates, this is your first stop.

## Reading EXPLAIN Output Like a Senior Engineer

Most engineers read `EXPLAIN ANALYZE` looking for slow nodes. That's table stakes. The real signal is the gap between estimated and actual rows.

```sql
-- snippet-4
-- The plan that killed production
EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)
SELECT
    u.id,
    u.email,
    COUNT(o.id) AS order_count,
    SUM(o.total_cents) AS lifetime_value
FROM users u
JOIN orders o ON o.user_id = u.id
WHERE u.created_at > '2025-01-01'
  AND o.status IN ('completed', 'refunded')
GROUP BY u.id, u.email
HAVING SUM(o.total_cents) > 10000;

-- What you're looking for in JSON output:
-- "Plan Rows" vs "Actual Rows" -- if ratio > 10x, stats are bad
-- "Plan Width" -- if way off, you may have stale type stats
-- "Shared Hit Blocks" vs "Shared Read Blocks" -- cache hit rate
-- Join type: Hash Join (good for large sets) vs Nested Loop (good when outer is small)
```

A 10x mismatch between estimated and actual rows isn't a warning — it's a guarantee that downstream join and sort decisions are made on wrong assumptions. The planner chose your join algorithm based on the estimated row count. If it thought 200 rows and got 200,000, you're paying for that mistake in execution time.

Use `EXPLAIN (ANALYZE, BUFFERS)` in staging but be careful in production — it executes the query. For read-heavy analytical queries on replicas, this is fine. For write-heavy OLTP queries, use `auto_explain` with sampling:

```sql
-- snippet-5
-- auto_explain configuration for production query capture
-- In postgresql.conf or via ALTER SYSTEM:
shared_preload_libraries = 'auto_explain'

-- Session-level for debugging:
LOAD 'auto_explain';
SET auto_explain.log_min_duration = 500;  -- log queries taking > 500ms
SET auto_explain.log_analyze = true;
SET auto_explain.log_buffers = true;
SET auto_explain.log_nested_statements = true;
SET auto_explain.sample_rate = 0.01;  -- 1% sampling in production

-- The output lands in PostgreSQL logs, parseable by pgBadger or log_fdw
```

## The GUCs That Actually Matter for Plan Forcing

Sometimes statistics are correct but the planner still makes a suboptimal choice due to configuration defaults that don't match your hardware. The three most impactful:

- `random_page_cost`: default 4.0, assumes HDDs. On SSDs, set to 1.1-1.5. This single change fixes index-scan avoidance on fast storage more often than any statistics tuning.
- `effective_cache_size`: default 4GB. Set this to ~75% of your available RAM. The planner uses this to estimate how much of the working set fits in OS page cache, directly influencing whether it chooses index scans.
- `work_mem`: default 4MB. Hash joins and sorts spill to disk when they exceed this. If you see "Batches: 16" in hash join nodes, your `work_mem` is too low. Set it per-session for analytics workloads rather than globally to avoid OOM.

When the planner is stubbornly wrong and you've exhausted statistics improvements, reach for plan forcing. The nuclear option:

```sql
-- snippet-6
-- Force specific join strategies when the planner is wrong
-- Disable nested loops when you know the result set is large
SET enable_nestloop = off;
SET enable_hashjoin = on;

-- Force a specific index
SET enable_seqscan = off;  -- expensive hammer — prefer index hints via extension

-- pg_hint_plan extension gives surgical control:
-- Install: CREATE EXTENSION pg_hint_plan;
SELECT /*+ HashJoin(o u) IndexScan(o orders_user_id_idx) */
    u.id,
    COUNT(o.id)
FROM users u
JOIN orders o ON o.user_id = u.id
WHERE u.tier = 'enterprise'
GROUP BY u.id;

-- Document WHY you're forcing — include the bad plan and the ticket number
-- These hints become technical debt the moment statistics improve
```

The `pg_hint_plan` extension is the right tool for production plan forcing. It lets you attach hints at the query level rather than session level, which means you're not accidentally degrading other queries that run in the same connection. The comment-based syntax is ugly but explicit.

## Automating Statistics Health Monitoring

Stale statistics are a leading cause of plan regressions after data migrations, bulk loads, or rapid table growth. The default autovacuum triggers ANALYZE when 20% of a table changes — on a 100M-row table, that means 20M row changes before statistics update. This is too coarse for tables with hot partitions.

```sql
-- snippet-7
-- Identify tables with potentially stale statistics
SELECT
    schemaname,
    relname,
    n_live_tup,
    n_dead_tup,
    last_analyze,
    last_autoanalyze,
    ROUND(100.0 * n_dead_tup / NULLIF(n_live_tup + n_dead_tup, 0), 2) AS dead_pct,
    CASE
        WHEN last_analyze IS NULL THEN 'NEVER ANALYZED'
        WHEN last_analyze < now() - interval '24 hours'
         AND n_live_tup > 100000 THEN 'STALE'
        ELSE 'OK'
    END AS stats_health
FROM pg_stat_user_tables
WHERE n_live_tup > 10000
ORDER BY n_live_tup DESC
LIMIT 50;

-- Tables to tune autovacuum for hot-write workloads:
ALTER TABLE events SET (
    autovacuum_analyze_scale_factor = 0.01,  -- 1% instead of 20%
    autovacuum_analyze_threshold = 1000
);
```

Run this query as a Grafana panel or a Datadog custom check. Alert when high-traffic tables cross the `STALE` threshold. Add a post-deploy step that runs `ANALYZE` on tables touched by your migration. These two practices eliminate the majority of post-deploy plan regressions.

## When to Use Partitioning to Control Planner Behavior

Partition pruning is the planner's most powerful statistics bypass. If you have a `created_at` range predicate on a partitioned table and the planner knows at parse time which partitions to skip, it doesn't need accurate statistics — it just doesn't read those partitions.

The failure mode: partition pruning only works when the partition key is present in the WHERE clause and the value is a constant or bound parameter known at plan time. `WHERE created_at > now() - interval '30 days'` works. `WHERE created_at > (SELECT MAX(ts) FROM last_sync)` forces partition scanning because the subquery result isn't known until execution.

This matters architecturally: design your partition keys around your most selective, highest-cardinality predicates. Time-series data partitioned by month with queries filtered by `created_at` is the canonical case. User-facing multi-tenant data partitioned by `tenant_id` is another. In both cases, you're trading planner flexibility for guaranteed partition elimination.

## Putting It Together: A Production Debugging Checklist

When a query regresses in production:

1. Run `EXPLAIN (ANALYZE, BUFFERS)` and find nodes where estimated rows diverge from actual by more than 5x.
2. Check `pg_stats` for the columns in your WHERE clause. Look at `n_distinct`, `most_common_freqs`, and `correlation`.
3. Run `ANALYZE` on the affected table and re-run. If the plan changes, statistics were stale. If not, the statistics target may be too low.
4. If multiple columns appear in correlated predicates, create extended statistics.
5. Verify `random_page_cost`, `effective_cache_size`, and `work_mem` match your hardware profile.
6. If statistics are accurate but the plan is still wrong, use `pg_hint_plan` to force the correct plan, document the reason, and file a ticket to revisit when data distribution changes.

The planner is not magic and it's not broken — it's a probabilistic system making decisions from incomplete information. Your job is to give it better information first, and only reach for forcing when you've exhausted statistical improvements. Plans you force today are plans you debug in six months when the data changes. Statistics you invest in now make the planner smarter for every query on that table, forever.