---
layout: post
title: "PostgreSQL Performance Tuning: From Slow to Lightning Fast"
tags: [postgresql, database, performance, backend]
description: "Practical PostgreSQL performance tuning — configuration, query optimization, and monitoring techniques that actually move the needle."
---

PostgreSQL out-of-the-box is configured for broad compatibility, not maximum performance. With the right tuning, you can dramatically improve throughput and latency. Here's what to actually do.

## Start with Configuration

PostgreSQL's default `postgresql.conf` is conservative. Tune these settings based on your hardware.

### Memory Settings

```ini
# shared_buffers: 25% of total RAM
shared_buffers = 4GB

# effective_cache_size: 75% of total RAM
# Tells the query planner how much OS cache is available
effective_cache_size = 12GB

# work_mem: RAM per sort/hash operation per query
# Be careful — this is per operation, not per connection
# Formula: (RAM * 0.25) / (max_connections * 3)
work_mem = 64MB

# maintenance_work_mem: for VACUUM, CREATE INDEX, etc.
maintenance_work_mem = 1GB
```

### Write Performance

```ini
# checkpoint_completion_target: spread checkpoint writes
checkpoint_completion_target = 0.9

# wal_buffers: WAL write buffer (usually auto)
wal_buffers = 16MB

# synchronous_commit: set to off for async writes (risk: lose ~100ms of data on crash)
# Use only if you can tolerate this trade-off
synchronous_commit = off

# max_wal_size: allow more WAL before checkpoint
max_wal_size = 4GB
```

### Connection Settings

```ini
# max_connections: lower than you think you need — use a connection pooler!
max_connections = 100

# Use PgBouncer or pgpool-II for connection pooling
```

Never set `max_connections = 1000`. Each connection uses ~10MB of RAM and PostgreSQL doesn't handle thousands of connections well. Use **PgBouncer** in transaction mode.

## Query Optimization

### EXPLAIN ANALYZE is Your Best Friend

```sql
EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT)
SELECT u.name, COUNT(o.id) as order_count
FROM users u
JOIN orders o ON o.user_id = u.id
WHERE u.created_at > '2026-01-01'
GROUP BY u.id, u.name
ORDER BY order_count DESC
LIMIT 10;
```

Read the output:
- **Seq Scan** on large tables = missing index
- **Hash Join** vs **Nested Loop** — optimizer choice based on statistics
- **actual rows** much different from **estimated rows** = outdated statistics (`ANALYZE`)
- **Buffers: hit** = from cache, **read** = from disk

### Common Query Patterns to Optimize

#### Avoid SELECT *

```sql
-- Bad: fetches all columns, more data transferred
SELECT * FROM orders WHERE user_id = 42;

-- Good: only fetch what you need
SELECT id, total, status, created_at FROM orders WHERE user_id = 42;
```

#### Use CTEs Wisely

In older PostgreSQL, CTEs were "optimization fences". In PG 12+, CTEs inline by default:

```sql
-- PG 12+: this is fine and inlines automatically
WITH recent_orders AS (
    SELECT * FROM orders WHERE created_at > NOW() - INTERVAL '7 days'
)
SELECT user_id, COUNT(*) FROM recent_orders GROUP BY user_id;

-- Force materialization (barrier) when needed
WITH MATERIALIZED expensive_query AS (
    SELECT ...
)
```

#### Batch Operations

```sql
-- Bad: N individual inserts
INSERT INTO events (type, data) VALUES ('click', '{}');
-- ... repeated 10,000 times

-- Good: single batch insert
INSERT INTO events (type, data)
VALUES ('click', '{}'), ('view', '{}'), ('purchase', '{}')
-- ... up to thousands of rows at once
;

-- Even better for large batches: COPY
COPY events (type, data) FROM STDIN WITH (FORMAT CSV);
```

#### Optimize Pagination

```sql
-- Bad: OFFSET pagination is slow on large tables
-- PostgreSQL must scan and discard 100,000 rows
SELECT * FROM posts ORDER BY created_at DESC LIMIT 20 OFFSET 100000;

-- Good: Cursor-based pagination
SELECT * FROM posts
WHERE created_at < '2026-01-15 10:30:00'  -- cursor from previous page
ORDER BY created_at DESC
LIMIT 20;
```

## Vacuuming and Table Bloat

PostgreSQL uses MVCC — old row versions accumulate. VACUUM reclaims them.

```sql
-- Check table bloat
SELECT
    schemaname,
    tablename,
    pg_size_pretty(pg_total_relation_size(quote_ident(tablename))) AS total_size,
    n_dead_tup,
    n_live_tup,
    round(n_dead_tup::numeric / NULLIF(n_live_tup, 0) * 100, 2) AS dead_ratio
FROM pg_stat_user_tables
ORDER BY n_dead_tup DESC;

-- Manual vacuum on a hot table
VACUUM (ANALYZE, VERBOSE) orders;

-- Reclaim disk space (locks table!)
VACUUM FULL orders;

-- Better for production: use pg_repack
pg_repack -t orders -d mydb
```

Configure autovacuum aggressively for high-write tables:

```sql
ALTER TABLE orders SET (
    autovacuum_vacuum_scale_factor = 0.01,   -- Vacuum at 1% dead tuples
    autovacuum_analyze_scale_factor = 0.005  -- Analyze at 0.5%
);
```

## Monitoring Queries

```sql
-- Install pg_stat_statements extension
CREATE EXTENSION pg_stat_statements;

-- Top 10 slowest queries
SELECT
    LEFT(query, 100) as query,
    calls,
    round(mean_exec_time::numeric, 2) AS avg_ms,
    round(total_exec_time::numeric, 2) AS total_ms,
    round(stddev_exec_time::numeric, 2) AS stddev_ms
FROM pg_stat_statements
ORDER BY mean_exec_time DESC
LIMIT 10;

-- Queries with highest total time (most impactful to optimize)
SELECT
    LEFT(query, 100) as query,
    calls,
    round(total_exec_time::numeric / 1000, 2) AS total_seconds
FROM pg_stat_statements
ORDER BY total_exec_time DESC
LIMIT 10;
```

## Connection Pooling with PgBouncer

Install and configure PgBouncer in transaction mode:

```ini
# pgbouncer.ini
[databases]
mydb = host=localhost port=5432 dbname=mydb

[pgbouncer]
listen_addr = *
listen_port = 6432
auth_type = scram-sha-256
pool_mode = transaction
max_client_conn = 1000
default_pool_size = 20
```

Your app connects to PgBouncer on port 6432; PgBouncer maintains a pool of 20 real connections to PostgreSQL.

## Quick Wins Checklist

- [ ] Tune `shared_buffers`, `work_mem`, `effective_cache_size`
- [ ] Deploy PgBouncer for connection pooling
- [ ] Enable `pg_stat_statements` and find your top slow queries
- [ ] Add missing indexes on foreign keys
- [ ] Check for table bloat and tune autovacuum
- [ ] Replace `OFFSET` pagination with cursor-based
- [ ] Use `EXPLAIN ANALYZE` before every schema/index change
