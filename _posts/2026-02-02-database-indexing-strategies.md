---
layout: post
title: "Database Indexing Strategies That Actually Matter"
tags: [database, postgresql, performance, backend]
description: "Understanding database indexes deeply — how they work, when to use them, and the common mistakes that kill performance."
---

Slow queries are responsible for more production incidents than almost anything else. And the solution is almost always the same: proper indexing. Let's go deep on how indexes work and how to use them effectively.

![B-Tree Index vs Full Table Scan](/images/diagrams/database-indexing.svg)

## How Indexes Work (The Mental Model)

Think of a database table as a book and an index as the book's index at the back. Without an index, finding all mentions of "Redis" means reading every single page (full table scan). With an index, you jump directly to the right pages.

In PostgreSQL (and most databases), the default index type is a **B-Tree** (Balanced Tree). It keeps data sorted, making it fast for:
- Exact matches: `WHERE id = 42`
- Range queries: `WHERE created_at > '2026-01-01'`
- Sorting: `ORDER BY last_name`

## Types of Indexes in PostgreSQL

### B-Tree (Default)
Best for most cases — equality, ranges, sorting.

```sql
CREATE INDEX idx_users_email ON users(email);
CREATE INDEX idx_orders_created ON orders(created_at);
```

### Hash Index
Only for exact equality matches. Faster than B-Tree for this case but limited.

```sql
CREATE INDEX idx_sessions_token ON sessions USING HASH (token);
```

### GIN (Generalized Inverted Index)
For full-text search and array/JSONB columns.

```sql
-- Full text search
CREATE INDEX idx_articles_search ON articles USING GIN (to_tsvector('english', content));

-- JSONB
CREATE INDEX idx_users_metadata ON users USING GIN (metadata);
```

### GiST and BRIN
For geometric data and very large sequential tables respectively.

## Composite Indexes: Order Matters!

A composite index on `(a, b, c)` can serve queries on:
- `WHERE a = ...`
- `WHERE a = ... AND b = ...`
- `WHERE a = ... AND b = ... AND c = ...`

But NOT:
- `WHERE b = ...` (doesn't start from leftmost)
- `WHERE c = ...`

```sql
-- This index serves both queries below efficiently
CREATE INDEX idx_orders_user_status ON orders(user_id, status, created_at);

-- Uses the index
SELECT * FROM orders WHERE user_id = 42 AND status = 'pending';

-- Also uses the index (partial)
SELECT * FROM orders WHERE user_id = 42;

-- Does NOT use the index efficiently
SELECT * FROM orders WHERE status = 'pending';
```

**Rule: Put the most selective column first in composite indexes.**

## Covering Indexes

If your index contains all the columns a query needs, PostgreSQL never has to touch the main table — this is an "index-only scan" and is extremely fast:

```sql
-- Query needs user_id, status, and total
SELECT user_id, status, total FROM orders WHERE user_id = 42;

-- Covering index includes all needed columns
CREATE INDEX idx_orders_covering ON orders(user_id) INCLUDE (status, total);
```

## Partial Indexes

Index only a subset of rows. Smaller, faster:

```sql
-- Only index unprocessed jobs (90% are already processed)
CREATE INDEX idx_jobs_pending ON jobs(created_at)
  WHERE status = 'pending';

-- Only index active users
CREATE INDEX idx_users_active_email ON users(email)
  WHERE deleted_at IS NULL;
```

## Functional Indexes

Index the result of a function:

```sql
-- Case-insensitive email lookup
CREATE INDEX idx_users_lower_email ON users(LOWER(email));

-- Now this query is fast
SELECT * FROM users WHERE LOWER(email) = LOWER('User@Example.com');
```

## Diagnosing Slow Queries

```sql
-- Enable timing
\timing

-- See the query plan
EXPLAIN ANALYZE
SELECT * FROM orders WHERE user_id = 42 AND status = 'pending';

-- Look for:
-- "Seq Scan" — full table scan (bad for large tables)
-- "Index Scan" — good
-- "Index Only Scan" — best
-- actual time vs estimated time
```

Find slow queries from logs:

```sql
-- Find the most time-consuming queries
SELECT query, mean_exec_time, calls, total_exec_time
FROM pg_stat_statements
ORDER BY total_exec_time DESC
LIMIT 10;
```

## Common Indexing Mistakes

### 1. Over-indexing
Every index slows down writes (INSERT, UPDATE, DELETE). Don't add indexes "just in case".

### 2. Not indexing foreign keys
Always index foreign keys — they're used in JOINs constantly:

```sql
-- After creating the foreign key
ALTER TABLE orders ADD CONSTRAINT fk_user FOREIGN KEY (user_id) REFERENCES users(id);

-- Always add this index
CREATE INDEX idx_orders_user_id ON orders(user_id);
```

### 3. Ignoring index bloat
Indexes can become bloated over time. Rebuild them periodically:

```sql
REINDEX INDEX CONCURRENTLY idx_orders_user_id;
```

### 4. Using functions on indexed columns in WHERE

```sql
-- This CANNOT use an index on created_at
WHERE DATE(created_at) = '2026-01-01'

-- This CAN
WHERE created_at >= '2026-01-01' AND created_at < '2026-01-02'
```

## Quick Decision Guide

| Scenario | Solution |
|----------|----------|
| `WHERE email = ?` | B-Tree index on `email` |
| Case-insensitive lookup | Functional index on `LOWER(email)` |
| `WHERE user_id = ? AND status = ?` | Composite index `(user_id, status)` |
| Full-text search | GIN index on `tsvector` |
| Sparse column (many NULLs) | Partial index `WHERE col IS NOT NULL` |
| JSONB queries | GIN index on the JSONB column |

Indexing is part science, part art. Profile first, index based on real query patterns, and measure the impact.
