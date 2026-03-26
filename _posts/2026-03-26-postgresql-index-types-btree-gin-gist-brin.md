---
layout: post
title: "PostgreSQL Index Types: B-tree, GIN, GiST, and BRIN Selection Strategies"
date: 2026-03-26 08:00:00 +0700
tags: [postgresql, database, performance, indexing, backend]
description: "A production-focused guide to choosing between B-tree, GIN, GiST, and BRIN indexes in PostgreSQL based on query patterns and data characteristics."
image: "https://picsum.photos/1080/720?random=589"
thumbnail: "https://picsum.photos/400/300?random=589"
---

You're staring at a query that's doing a sequential scan on a 200-million-row table. You've already added an index — a B-tree, because that's what everyone adds — but `EXPLAIN ANALYZE` still shows Seq Scan. The planner isn't using your index. You check cardinality, check statistics, re-run `ANALYZE`, and it still won't budge. Three hours later you discover the column is a `tsvector` and B-tree can't index it at all. You needed GIN. This kind of mismatch between index type and workload is one of the most common causes of performance regressions in PostgreSQL-backed systems, and it's almost always invisible until you're on-call at 2 AM.

PostgreSQL ships with five index types: B-tree, Hash, GIN, GiST, and BRIN. Hash is mostly academic at this point. The other four have distinct performance envelopes, and picking the wrong one can cost you an order of magnitude in query latency or storage overhead. This post breaks down when to use each, what failure modes look like, and how to verify your choice actually works.

## B-tree: The Workhorse You're Already Over-Using

B-tree is the default for a reason. It handles equality (`=`), range (`<`, `>`, `BETWEEN`), and prefix matching (`LIKE 'foo%'`). The planner can use it for `ORDER BY` and `MIN`/`MAX` aggregates. It's also the only type that supports unique constraints and primary keys. For most OLTP workloads on scalar data types — integers, timestamps, UUIDs, short strings — B-tree is correct.

Where engineers go wrong is treating B-tree as universally applicable. It cannot index arrays. It cannot efficiently support `LIKE '%foo%'` (leading wildcard) or `LIKE '%foo'`. It has no concept of containment or overlap operators. It compares values by total order, so anything that doesn't have a natural linear ordering is either broken or dramatically slower than the alternatives.

```sql
-- snippet-1
-- B-tree works well here: range query on a monotonically increasing column
CREATE INDEX CONCURRENTLY idx_orders_created_at
    ON orders (created_at DESC);

-- Verify the planner uses it:
EXPLAIN (ANALYZE, BUFFERS)
SELECT id, user_id, total
FROM orders
WHERE created_at > NOW() - INTERVAL '7 days'
ORDER BY created_at DESC
LIMIT 100;

-- You want to see: Index Scan using idx_orders_created_at
-- Red flag: Seq Scan or Bitmap Heap Scan with high Buffers hit
```

One thing that catches teams off guard: B-tree index entries are stored in sorted order, and maintaining that order under heavy write load is expensive. PostgreSQL uses page-level locking during splits, and a table that receives 50k inserts/second on a sequential primary key will see significant index bloat as pages fill and split. Monitor `pg_stat_user_indexes.idx_blks_read` and check index bloat with `pgstattuple` quarterly on high-write tables.

## GIN: For When Your Data Has Many Things Inside It

GIN (Generalized Inverted Index) inverts the relationship between index entries and rows. Instead of mapping a row to its key, GIN maps each key to all rows containing it. This is exactly the structure you need for full-text search, arrays, JSONB, and `pg_trgm` trigram matching.

The classic use case is `tsvector`/`tsquery` full-text search. If you're not using GIN on a `tsvector` column, you're doing sequential scans on every text search. No exceptions.

```sql
-- snippet-2
-- GIN for full-text search with a stored tsvector
ALTER TABLE articles ADD COLUMN search_vector tsvector;

UPDATE articles
SET search_vector = to_tsvector('english', 
    coalesce(title, '') || ' ' || coalesce(body, ''));

CREATE INDEX CONCURRENTLY idx_articles_search
    ON articles USING GIN (search_vector);

-- Trigger to keep it fresh
CREATE OR REPLACE FUNCTION articles_search_vector_update()
RETURNS trigger AS $$
BEGIN
    NEW.search_vector := to_tsvector('english',
        coalesce(NEW.title, '') || ' ' || coalesce(NEW.body, ''));
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER articles_search_vector_trigger
BEFORE INSERT OR UPDATE ON articles
FOR EACH ROW EXECUTE FUNCTION articles_search_vector_update();

-- Query that will use the index
SELECT id, title, ts_rank(search_vector, query) AS rank
FROM articles, to_tsquery('english', 'postgres & index') query
WHERE search_vector @@ query
ORDER BY rank DESC
LIMIT 20;
```

GIN also shines for JSONB containment queries using the `@>` operator. If you're doing `jsonb_column @> '{"status": "active"}'`, you need a GIN index — a B-tree index on the whole JSONB column is useless for this.

```sql
-- snippet-3
-- GIN for JSONB containment and key existence
CREATE INDEX CONCURRENTLY idx_events_payload_gin
    ON events USING GIN (payload jsonb_path_ops);

-- jsonb_path_ops is more compact than the default jsonb_ops
-- but only supports @> operator, not ? (key exists) or ?| / ?&
-- Use default jsonb_ops if you need key existence checks:
-- CREATE INDEX ... USING GIN (payload);

-- Queries that use jsonb_path_ops index:
SELECT id FROM events
WHERE payload @> '{"type": "purchase", "currency": "USD"}';

-- Does NOT use jsonb_path_ops (needs default jsonb_ops):
SELECT id FROM events WHERE payload ? 'user_id';
```

The tradeoff with GIN is write amplification. A single row update might touch dozens of index entries. In high-update workloads, GIN indexes accumulate a "pending list" of unflushed entries in `pg_gin_pending_list_limit` (default 4MB). The planner has to check both the main index and the pending list on every query, which degrades performance. Run `VACUUM` frequently on GIN-indexed tables or lower `gin_pending_list_limit` at the index level. If you're seeing GIN indexes on tables that receive thousands of updates per second, expect bloat and plan for periodic `REINDEX CONCURRENTLY`.

## GiST: For Spatial, Geometric, and Proximity Queries

GiST (Generalized Search Tree) is a framework for building custom index strategies. The built-in implementations handle geometric types, range types, nearest-neighbor search, and (via `pg_trgm`) substring search. If your query uses operators like `&&` (overlap), `<->` (distance), `@>` / `<@` (containment for ranges), or `~` (regex), you're in GiST territory.

The most production-relevant GiST use case outside of PostGIS is the `pg_trgm` extension for `LIKE '%foo%'` queries. B-tree cannot handle leading wildcards. `pg_trgm` with a GiST or GIN index can — by decomposing strings into trigrams and intersecting the result sets.

```sql
-- snippet-4
-- pg_trgm for substring search (handles LIKE '%foo%', ILIKE, ~)
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE INDEX CONCURRENTLY idx_users_email_trgm
    ON users USING GiST (email gist_trgm_ops);

-- Now this works efficiently:
SELECT id, email FROM users
WHERE email ILIKE '%@gmail.com';

-- Or regex:
SELECT id, email FROM users
WHERE email ~ '^[a-z]+\.[a-z]+@';

-- GIN is often faster for static data; GiST faster for writes
-- Benchmark both: GIN is ~3x larger but queries run faster
-- For frequently updated tables, GiST has lower write overhead
```

For teams using PostGIS or the built-in geometric types, GiST is non-negotiable. A query like "find all restaurants within 5km" without a GiST index on the geometry column will scan every row. With a GiST index, it becomes a fast bounding-box filter followed by precise distance calculation.

```sql
-- snippet-5
-- GiST for range type overlap — critical for scheduling/booking systems
CREATE TABLE room_bookings (
    id          BIGSERIAL PRIMARY KEY,
    room_id     INT NOT NULL,
    booked_at   TSTZRANGE NOT NULL,
    EXCLUDE USING GiST (room_id WITH =, booked_at WITH &&)
);
-- The exclusion constraint automatically creates a GiST index
-- This enforces no overlapping bookings at the DB level
-- Trying to insert an overlapping range will fail with a constraint violation

-- Explicit index for range queries:
CREATE INDEX CONCURRENTLY idx_bookings_range
    ON room_bookings USING GiST (booked_at);

SELECT room_id, booked_at
FROM room_bookings
WHERE booked_at && '[2026-04-01, 2026-04-03)'::tstzrange;
```

GiST vs GIN for `pg_trgm` is a common decision point. GIN builds a full inverted index and is typically 2-3x faster at query time but costs more storage and has higher write overhead. GiST uses an approximation (lossy compression) that's smaller and cheaper to maintain but occasionally returns false positives that require a recheck. For read-heavy search tables, use GIN. For tables with constant inserts and moderate query load, use GiST.

## BRIN: The Index That Doesn't Store What You Think

BRIN (Block Range Index) doesn't store individual key values. It stores the minimum and maximum value of a column for each range of physical disk blocks (128 blocks by default). This makes it extremely compact — a BRIN index on a 500GB time-series table can be under 1MB — but it only works when the indexed column is highly correlated with physical storage order.

The canonical use case is an append-only event log with a `created_at` timestamp. New rows land at the end of the heap, so recent timestamps are always in the last few blocks. A BRIN index can eliminate 99% of the table from a time-range scan while adding almost no storage overhead.

```sql
-- snippet-6
-- BRIN for append-only time-series data
CREATE INDEX CONCURRENTLY idx_events_created_brin
    ON events USING BRIN (created_at)
    WITH (pages_per_range = 64);  -- default 128; smaller = more precise, larger index

-- Check physical correlation before committing to BRIN
SELECT correlation
FROM pg_stats
WHERE tablename = 'events' AND attname = 'created_at';
-- You want correlation > 0.9 (ideally > 0.99) for BRIN to be effective
-- Below 0.5, BRIN will frequently scan most of the table

-- Compare index sizes:
SELECT
    indexname,
    pg_size_pretty(pg_relation_size(indexrelid)) AS index_size
FROM pg_stat_user_indexes
WHERE relname = 'events';
-- B-tree on created_at: ~12GB
-- BRIN on created_at: ~400KB
```

The failure mode with BRIN is invisible degradation over time. BRIN is only effective when data is physically ordered. If you bulk-load historical data out of order, run a large update, or use `pg_repack` without rebuilding BRIN, the correlation drops and the planner starts scanning entire block ranges unnecessarily. The query still "works" — it returns correct results — but it may be doing 10x more I/O than expected. Run `SELECT correlation FROM pg_stats` on your BRIN-indexed columns quarterly.

BRIN is also useless for point lookups or high-selectivity queries where you're looking for a specific value in a large table. If you need to fetch a row by `user_id = 12345`, BRIN can't help you. It's specifically for "give me everything in this time range" on append-only data.

## Decision Framework

The question isn't "which index is best" but "which index matches my access pattern." Here's how to think through it:

**Start with the operator.** What operator does your WHERE clause use? `=`, `<`, `>`, `BETWEEN`, `LIKE 'foo%'` → B-tree. `@@`, `@>`, `?`, `&&`, `ANY()` on arrays → GIN. Geometric/spatial operators, range overlap, `LIKE '%foo%'` → GiST. You're filtering time-ranges on append-only data → BRIN.

**Check cardinality and selectivity.** B-tree is most effective when the indexed column has high cardinality (many distinct values). A B-tree on a boolean column is almost always a mistake — the planner will ignore it for queries returning more than ~5% of rows. Partial indexes (`WHERE status = 'pending'`) often outperform full indexes on low-cardinality columns.

**Measure physical correlation before using BRIN.** Query `pg_stats.correlation`. If it's below 0.9, BRIN will under-perform. If your table is clustered by insert order (event logs, audit trails), BRIN is likely your best option.

**Verify with EXPLAIN (ANALYZE, BUFFERS).** An index that the planner ignores is worse than no index — it wastes storage and slows writes for no benefit. Check that `Index Scan` or `Bitmap Index Scan` appears in the plan, and check `Buffers: shared hit` to confirm block reads are actually being reduced.

```sql
-- snippet-7
-- Systematic index verification workflow
-- 1. Check what indexes exist and their usage
SELECT
    schemaname,
    tablename,
    indexname,
    idx_scan,
    idx_tup_read,
    idx_tup_fetch,
    pg_size_pretty(pg_relation_size(indexrelid)) AS size
FROM pg_stat_user_indexes
WHERE tablename = 'your_table'
ORDER BY idx_scan DESC;

-- 2. Find unused indexes (candidates for removal)
SELECT schemaname, tablename, indexname, idx_scan
FROM pg_stat_user_indexes
WHERE idx_scan = 0
  AND indexrelid NOT IN (
    SELECT conindid FROM pg_constraint WHERE contype IN ('p', 'u')
  )
ORDER BY pg_relation_size(indexrelid) DESC;

-- 3. Check for missing indexes (sequential scans on large tables)
SELECT
    schemaname,
    relname,
    seq_scan,
    seq_tup_read,
    idx_scan,
    pg_size_pretty(pg_total_relation_size(relid)) AS total_size
FROM pg_stat_user_tables
WHERE seq_scan > 100
  AND pg_total_relation_size(relid) > 10 * 1024 * 1024  -- > 10MB
ORDER BY seq_tup_read DESC;
```

## Compound Indexes and Index-Only Scans

One last thing worth mentioning: composite B-tree indexes follow column order strictly. An index on `(user_id, created_at)` supports queries filtering on `user_id` alone, or both columns together, but not on `created_at` alone. Put the equality-filtered column first, the range column second.

Index-only scans — where PostgreSQL satisfies the query entirely from the index without touching the heap — require that all columns referenced in the query appear in the index. If you're doing `SELECT id, status FROM orders WHERE user_id = ? AND created_at > ?`, an index on `(user_id, created_at) INCLUDE (id, status)` enables an index-only scan. On high-traffic queries, this can cut latency by 50% or more by eliminating heap fetches entirely.

The `INCLUDE` clause was added in PostgreSQL 11. If you're still on 10 or earlier, you'd add those columns as regular index columns, which bloats the index size but achieves the same effect. Upgrade your Postgres.

Index selection is one of the highest-leverage database optimization decisions you can make. A wrong index wastes storage, slows writes, and delivers no read benefit. A right index can turn a 30-second query into a 5-millisecond one without touching application code. Know your operators, check your correlation, and always verify with EXPLAIN.
```