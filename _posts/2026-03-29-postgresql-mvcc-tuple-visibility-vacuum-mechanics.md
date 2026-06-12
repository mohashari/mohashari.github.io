---
layout: post
title: "PostgreSQL MVCC Internals: Tuple Visibility, Dead Rows, and VACUUM Mechanics"
date: 2026-03-29 08:00:00 +0700
tags: [postgresql, databases, internals, performance, backend]
description: "Deep dive into how PostgreSQL MVCC creates dead tuples, how visibility checks work at the byte level, and what VACUUM actually does to reclaim space."
image: ""
thumbnail: ""
---

A production system I worked on hit a wall at around 80 million orders. Queries that used to take 5ms started taking 400ms. `EXPLAIN ANALYZE` showed seq scans on a table that should have been index-scanned. The culprit wasn't missing indexes—the indexes were there. It was table bloat: 60% of rows in the orders table were dead tuples that PostgreSQL couldn't reclaim fast enough because autovacuum was tuned with factory defaults on a 200GB table. Understanding why this happens—at the storage level—is the difference between cargo-culting `VACUUM FULL` every week and actually fixing the root cause.

![PostgreSQL MVCC Internals: Tuple Visibility, Dead Rows, and VACUUM Mechanics Diagram](/images/diagrams/postgresql-mvcc-tuple-visibility-vacuum-mechanics.svg)

## What MVCC Actually Means at the Byte Level

Multi-Version Concurrency Control is not a black box. Every row in a PostgreSQL heap file is stored as a **tuple**, and every tuple carries metadata baked into its header. The two fields that drive MVCC are `xmin` and `xmax`:

- `xmin` — the transaction ID (XID) that **inserted** this tuple version
- `xmax` — the transaction ID that **deleted or updated** this tuple version (zero if still live)

When you `UPDATE` a row, PostgreSQL does *not* modify the existing bytes in place. It writes a new tuple with a fresh `xmin`, and stamps the old tuple's `xmax` with the updating transaction's XID. Both versions exist on disk simultaneously. When you `DELETE`, only `xmax` is set on the existing tuple—nothing is physically removed.

You can see this directly:

```sql
-- snippet-1
-- Inspect raw tuple headers (requires pageinspect extension)
CREATE EXTENSION IF NOT EXISTS pageinspect;

SELECT
    t_ctid,
    t_xmin,
    t_xmax,
    t_infomask::bit(16) AS infomask,
    t_data
FROM heap_page_items(get_raw_page('orders', 0))
WHERE t_xmin IS NOT NULL
LIMIT 20;
```

The `t_infomask` field carries flags like `HEAP_XMIN_COMMITTED`, `HEAP_XMAX_COMMITTED`, and `HEAP_HOT_UPDATED`. These flags are lazily set: the first backend that visits a tuple after its transaction commits will flip the committed bit so future visitors skip the `pg_cxid` lookup.

## Snapshot-Based Visibility: The Algorithm

Every transaction acquires a **snapshot** at start (or at each statement, depending on isolation level). The snapshot captures:

- `xmin` — the lowest active XID at snapshot time (all XIDs below this are committed or rolled back)
- `xmax` — one past the highest assigned XID (all XIDs ≥ this are future transactions)
- `xip` — the list of in-progress XIDs

For a tuple to be visible to a transaction, both conditions must hold:

1. **Inserter committed before snapshot**: `tuple.xmin < snapshot.xmin` AND not in `xip`, OR `tuple.xmin == current_txid`
2. **Deleter had not committed**: `tuple.xmax == 0` OR `tuple.xmax >= snapshot.xmax` OR `tuple.xmax` is in `xip` (in-progress, its delete isn't final)

```sql
-- snippet-2
-- Check your current transaction's snapshot
SELECT
    txid_current() AS current_xid,
    txid_current_snapshot() AS snapshot,
    txid_snapshot_xmin(txid_current_snapshot()) AS snap_xmin,
    txid_snapshot_xmax(txid_current_snapshot()) AS snap_xmax;

-- Watch live tuple counts vs dead tuple accumulation
SELECT
    schemaname,
    relname,
    n_live_tup,
    n_dead_tup,
    round(n_dead_tup::numeric / NULLIF(n_live_tup + n_dead_tup, 0) * 100, 2) AS dead_pct,
    last_vacuum,
    last_autovacuum,
    last_analyze
FROM pg_stat_user_tables
WHERE n_dead_tup > 1000
ORDER BY n_dead_tup DESC;
```

The snapshot mechanism is why long-running transactions are dangerous. If transaction 500 is still running when transactions 501–600 finish, all the tuples deleted by 501–600 remain visible to 500 and **cannot be reclaimed by VACUUM**. This is the single most common cause of uncontrolled bloat in OLTP systems that mix analytics queries with write-heavy workloads.

## HOT Updates: When PostgreSQL Is Clever About It

Not every UPDATE creates equally expensive dead tuples. If you update a column that is *not* part of any index, and the new tuple fits on the same heap page as the old one, PostgreSQL uses a **Heap-Only Tuple (HOT)** update:

1. The old tuple's `xmax` is stamped as usual
2. The new tuple is written on the same page
3. The old tuple's `t_ctid` is redirected to point at the new tuple
4. **No new index entry is written**

The significance: HOT chains can be pruned during page access (during any read) without a full vacuum cycle. The index still points to the original slot; PostgreSQL follows the HOT chain to find the live version. When all transactions that could see the old version are gone, the page can be cleaned inline.

HOT updates fail to trigger when:

- The updated column is indexed (common with `updated_at` columns that people reflexively index)
- There's no free space on the same page (fillfactor matters here)

```sql
-- snippet-3
-- Check HOT update efficiency per table
SELECT
    relname,
    n_tup_upd AS total_updates,
    n_tup_hot_upd AS hot_updates,
    round(n_tup_hot_upd::numeric / NULLIF(n_tup_upd, 0) * 100, 2) AS hot_pct
FROM pg_stat_user_tables
WHERE n_tup_upd > 0
ORDER BY n_tup_upd DESC;

-- Set fillfactor to leave room for HOT updates on write-heavy tables
ALTER TABLE orders SET (fillfactor = 70);
-- This leaves 30% of each page free, dramatically increasing HOT update rate
```

A hot_pct below 20% on a frequently-updated table is a red flag. Check if `updated_at` or `status` columns are indexed unnecessarily.

## What VACUUM Actually Does (and Doesn't Do)

`VACUUM` (not `VACUUM FULL`) performs these steps in order:

**Phase 1: Heap scan.** Sequentially reads heap pages. For each dead tuple (xmax committed and no active snapshot can see it), records the item pointer in a `dead_tuples` array in shared memory (`maintenance_work_mem`).

**Phase 2: Index cleanup.** For each index on the table, scans the index and removes any entries pointing to dead item pointers collected in Phase 1. This is often the most I/O-intensive part for tables with many indexes.

**Phase 3: Heap cleanup.** Revisits the heap pages from Phase 1 and marks dead item pointers as free, updating the Free Space Map (FSM) so new insertions can reuse the space.

**Phase 4: Truncation.** If the trailing pages of the relation are entirely empty, VACUUM can truncate the file—but only after acquiring a brief exclusive lock.

**Phase 5: Update statistics.** Updates `pg_class.relpages` and triggers ANALYZE if the change threshold is met.

Critically, regular `VACUUM` does **not** compact the table. A page with one live tuple at offset 7 and six dead tuples at offsets 1–6 has five reclaimed item slots, but the page still occupies 8KB on disk. The live bytes do not move. Only `VACUUM FULL` or `pg_repack` physically compacts and rewrites the heap—at the cost of an `ACCESS EXCLUSIVE` lock for the duration.

```bash
# snippet-4
# Run pg_repack without taking a full table lock (production-safe)
# Requires pg_repack extension installed

pg_repack \
  --host=localhost \
  --port=5432 \
  --username=postgres \
  --dbname=production \
  --table=orders \
  --no-kill-backend \
  --wait-timeout=5 \
  2>&1 | tee /var/log/pg_repack_orders.log

# Monitor progress via pg_stat_activity in another session:
# SELECT pid, state, query, now() - pg_stat_activity.query_start AS duration
# FROM pg_stat_activity
# WHERE query LIKE '%repack%';
```

## Autovacuum Tuning: The Default Settings Will Betray You

The default autovacuum trigger fires when:

```
dead_tuples > autovacuum_vacuum_threshold + autovacuum_vacuum_scale_factor × reltuples
```

With defaults (`threshold=50`, `scale_factor=0.2`), a 10-million-row table tolerates 2,000,050 dead tuples before autovacuum even starts. On a high-write table processing 5,000 UPDATEs/sec, that's 400 seconds of dead tuple accumulation. Then autovacuum runs as a single worker constrained to `autovacuum_vacuum_cost_delay=2ms` to avoid impacting OLTP latency.

For large, write-heavy tables, per-table autovacuum settings are mandatory:

```sql
-- snippet-5
-- Aggressive autovacuum for high-write tables
ALTER TABLE orders SET (
    autovacuum_vacuum_scale_factor = 0.01,   -- trigger at 1% dead (vs 20%)
    autovacuum_vacuum_threshold = 1000,       -- minimum 1000 dead tuples
    autovacuum_vacuum_cost_delay = 0,         -- no throttling (careful on busy systems)
    autovacuum_vacuum_cost_limit = 800,       -- more I/O budget per autovacuum worker
    autovacuum_analyze_scale_factor = 0.005   -- keep stats fresh
);

-- And globally, consider more workers
-- In postgresql.conf:
-- autovacuum_max_workers = 6          (default 3)
-- autovacuum_vacuum_cost_delay = 2ms  (reduce for less IO-constrained systems)
```

Watch out for the `autovacuum_freeze_max_age` threshold (default 200M transactions). When `age(relfrozenxid)` approaches this, autovacuum will aggressively vacuum even well-maintained tables to freeze old XIDs. If your primary is under heavy load, this can cause unexpected autovacuum spikes.

## XID Wraparound: The Silent Killer

PostgreSQL uses 32-bit transaction IDs. The XID space wraps around at ~2.1 billion. Because MVCC compares XIDs with modular arithmetic (XIDs within 2^31 are "in the past"; beyond 2^31 are "in the future"), any unfrozen tuple with a sufficiently old XID could appear to be from the future—making it invisible to all queries.

PostgreSQL prevents this by **freezing** old tuples: replacing their `xmin` with `FrozenTransactionId` (XID=2), which is universally considered "in the past" by all snapshots. VACUUM triggers freezing when a tuple's age exceeds `vacuum_freeze_min_age` (default 50M transactions).

```sql
-- snippet-6
-- Monitor XID age across all databases — this is a P0 alert metric
SELECT
    datname,
    age(datfrozenxid) AS xid_age,
    2100000000 - age(datfrozenxid) AS xids_remaining,
    round(age(datfrozenxid)::numeric / 2100000000 * 100, 1) AS pct_consumed
FROM pg_database
ORDER BY age(datfrozenxid) DESC;

-- Per-table freeze status
SELECT
    schemaname,
    relname,
    age(relfrozenxid) AS table_xid_age,
    pg_size_pretty(pg_total_relation_size(schemaname||'.'||relname)) AS size
FROM pg_class
JOIN pg_namespace ON pg_namespace.oid = pg_class.relnamespace
WHERE relkind = 'r'
  AND age(relfrozenxid) > 150000000   -- warn at 150M, panic at 1.6B
ORDER BY age(relfrozenxid) DESC;
```

At 1.6 billion XIDs consumed, PostgreSQL will shut the cluster down to read-only mode and log: `database is not accepting commands to avoid wraparound data loss`. This is not a graceful degradation. The fix—running `VACUUM FREEZE` on all tables—can take hours on large datasets. The only safe approach is monitoring XID age and never letting it get above 500M without intervention.

## The Visibility Map and Index-Only Scans

The Visibility Map is a compact bitmap (1–2 bits per heap page) that tracks two properties:

- **All-visible**: every tuple on this page is visible to all current and future transactions
- **All-frozen**: every tuple on this page has been frozen

These flags unlock two critical optimizations. First, when VACUUM scans the heap, it skips all-visible pages entirely—this is why a well-vacuumed table has near-zero vacuum overhead. Second, **index-only scans** depend entirely on the visibility map. When a query can be answered from an index alone (covering index), PostgreSQL still needs to check if the heap tuple is visible. If the page is marked all-visible, it skips the heap fetch entirely. If bloat has caused the visibility map to go stale, index-only scans silently fall back to heap fetches.

```sql
-- snippet-7
-- Check whether index-only scans are actually skipping heap fetches
SELECT
    relname,
    indexrelname,
    idx_scan,
    idx_tup_read,
    idx_tup_fetch,  -- non-zero means heap fetches are happening
    round((idx_tup_fetch::numeric / NULLIF(idx_tup_read, 0)) * 100, 2) AS heap_fetch_pct
FROM pg_stat_user_indexes
WHERE idx_scan > 1000
  AND idx_tup_fetch > 0
ORDER BY idx_tup_fetch DESC;

-- If heap_fetch_pct is high, run VACUUM to restore visibility map:
VACUUM (VERBOSE, ANALYZE) orders;
```

## Putting It Together: A Production Monitoring Checklist

Understanding MVCC internals translates directly into operational practice:

**Always alert on:**
- `age(datfrozenxid) > 500,000,000` — you have less than ~1.5B XIDs before emergency shutdown
- `n_dead_tup / n_live_tup > 0.15` on tables over 1M rows — autovacuum is losing
- Long-running transactions over 1 hour (`pg_stat_activity` + `now() - xact_start`) — they're blocking VACUUM from reclaiming dead tuples
- `autovacuum_count` plateauing while `n_dead_tup` keeps climbing — autovacuum is throttled or skipping tables

**Always configure per large table:**
- Smaller `autovacuum_vacuum_scale_factor` (0.01–0.05)
- Appropriate `fillfactor` (70–80 for write-heavy, 90 for read-heavy)
- Remove unnecessary indexes on `updated_at` and other frequently-updated columns to maximize HOT update rate

**Never:**
- Run `VACUUM FULL` in production without a maintenance window—it takes an exclusive lock
- Ignore XID age on standby replicas—hot standbys inherit the primary's need to freeze
- Assume autovacuum is running just because it's enabled—check `pg_stat_user_tables.last_autovacuum`

The PostgreSQL storage engine is remarkably transparent once you know where to look. `pageinspect`, `pg_stat_user_tables`, `pg_stat_user_indexes`, and `pg_class` give you a complete picture of MVCC health. The teams that run PostgreSQL at scale without incidents aren't the ones with the most hardware—they're the ones who actually read the tuple headers.
