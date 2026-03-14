---
layout: post
title: "SQL Query Optimization: Writing Queries That Scale"
tags: [database, sql, performance, postgresql, backend]
description: "Practical SQL query optimization techniques — from understanding execution plans to rewriting slow queries for 100x speedups."
---

A poorly written query that works fine on 1,000 rows becomes a production nightmare at 10 million rows. SQL optimization is one of the highest-leverage skills a backend engineer can have. Here's the essential knowledge.

## Understanding Query Execution Order

SQL is declarative — you state what you want, not how to get it. But knowing the logical execution order helps you write better queries:

```sql
SELECT    -- 6. Select columns
FROM      -- 1. Choose tables
JOIN      -- 2. Join tables
WHERE     -- 3. Filter rows
GROUP BY  -- 4. Group
HAVING    -- 5. Filter groups
ORDER BY  -- 7. Sort
LIMIT     -- 8. Limit results
```

This is why you can't use a column alias defined in SELECT within WHERE — WHERE runs before SELECT.

## EXPLAIN ANALYZE: Your Most Important Tool

Never optimize blind. Always profile first:

```sql
EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT)
SELECT
    u.name,
    COUNT(o.id) as total_orders,
    SUM(o.amount) as total_spent
FROM users u
LEFT JOIN orders o ON o.user_id = u.id
WHERE u.created_at > '2025-01-01'
GROUP BY u.id, u.name
HAVING SUM(o.amount) > 1000
ORDER BY total_spent DESC
LIMIT 20;
```

What to look for:
- `Seq Scan` on a large table → likely missing index
- `actual rows=10000` vs `rows=1` estimate → stale statistics, run `ANALYZE`
- High `Buffers: read` → data not cached, disk I/O bottleneck
- `Sort (...)` with `Disk: true` → sort spilling to disk, increase `work_mem`

## Common Anti-Patterns and Fixes

### Anti-Pattern 1: Function on Indexed Column

```sql
-- BAD: Can't use index on email column
SELECT * FROM users WHERE LOWER(email) = 'alice@example.com';

-- GOOD option 1: Store emails lowercase
SELECT * FROM users WHERE email = 'alice@example.com';

-- GOOD option 2: Functional index
CREATE INDEX idx_users_email_lower ON users(LOWER(email));
SELECT * FROM users WHERE LOWER(email) = 'alice@example.com'; -- Now uses index
```

### Anti-Pattern 2: Wildcard at Start of LIKE

```sql
-- BAD: Can't use B-Tree index (leading wildcard)
SELECT * FROM products WHERE name LIKE '%laptop%';

-- GOOD: Full-text search
SELECT * FROM products
WHERE to_tsvector('english', name) @@ plainto_tsquery('english', 'laptop');

-- Or use trigram index for arbitrary LIKE
CREATE EXTENSION pg_trgm;
CREATE INDEX idx_products_name_trgm ON products USING GIN (name gin_trgm_ops);
SELECT * FROM products WHERE name LIKE '%laptop%'; -- Now uses trigram index
```

### Anti-Pattern 3: Implicit Type Conversion

```sql
-- BAD: user_id is integer, but we pass string → full table scan
SELECT * FROM orders WHERE user_id = '42';

-- GOOD: match types
SELECT * FROM orders WHERE user_id = 42;
```

### Anti-Pattern 4: SELECT * in Subqueries

```sql
-- BAD: fetches all columns just to check existence
SELECT name FROM products
WHERE id IN (SELECT * FROM featured_products);

-- GOOD: use EXISTS or specific column
SELECT name FROM products p
WHERE EXISTS (
    SELECT 1 FROM featured_products fp WHERE fp.product_id = p.id
);
```

### Anti-Pattern 5: N+1 Queries in Application Code

```go
// BAD: 1 query for users + N queries for their orders
users := db.Query("SELECT * FROM users WHERE active = true")
for _, user := range users {
    user.Orders = db.Query("SELECT * FROM orders WHERE user_id = ?", user.ID)
}

// GOOD: 1 query with JOIN or 2 queries with IN
users := db.Query("SELECT * FROM users WHERE active = true")
userIDs := extractIDs(users)
orders := db.Query("SELECT * FROM orders WHERE user_id = ANY(?)", userIDs)
// Group orders by user ID in application code
```

## Efficient Aggregations

```sql
-- Slow: sorts all rows to find top 10
SELECT user_id, SUM(amount) as total
FROM orders
GROUP BY user_id
ORDER BY total DESC
LIMIT 10;

-- Add partial index on high-value orders to speed up common queries
CREATE INDEX idx_orders_amount_high ON orders(user_id, amount)
WHERE amount > 100;

-- Use materialized views for expensive aggregations
CREATE MATERIALIZED VIEW user_order_stats AS
SELECT
    user_id,
    COUNT(*) as order_count,
    SUM(amount) as total_spent,
    AVG(amount) as avg_order,
    MAX(created_at) as last_order_at
FROM orders
GROUP BY user_id;

CREATE UNIQUE INDEX ON user_order_stats(user_id);

-- Query the materialized view (instant)
SELECT * FROM user_order_stats WHERE total_spent > 1000;

-- Refresh when needed (can be done concurrently)
REFRESH MATERIALIZED VIEW CONCURRENTLY user_order_stats;
```

## Window Functions: Powerful, Often Overlooked

```sql
-- Rank users by spending within each country
SELECT
    user_id,
    name,
    country,
    total_spent,
    RANK() OVER (PARTITION BY country ORDER BY total_spent DESC) as country_rank,
    ROUND(total_spent / SUM(total_spent) OVER (PARTITION BY country) * 100, 2) as country_pct
FROM user_order_stats u
JOIN users USING (user_id);

-- Running total (cumulative sum)
SELECT
    date,
    daily_revenue,
    SUM(daily_revenue) OVER (ORDER BY date) as cumulative_revenue
FROM daily_stats;

-- Moving average (last 7 days)
SELECT
    date,
    daily_active_users,
    AVG(daily_active_users) OVER (
        ORDER BY date
        ROWS BETWEEN 6 PRECEDING AND CURRENT ROW
    ) as dau_7day_avg
FROM daily_metrics;

-- Get previous/next row values
SELECT
    order_id,
    created_at,
    amount,
    LAG(amount) OVER (PARTITION BY user_id ORDER BY created_at) as prev_order_amount,
    amount - LAG(amount) OVER (PARTITION BY user_id ORDER BY created_at) as change
FROM orders;
```

## Bulk Operations

```sql
-- Slow: individual updates in a loop
UPDATE products SET price = 9.99 WHERE id = 1;
UPDATE products SET price = 14.99 WHERE id = 2;
-- ... 10,000 more times

-- Fast: batch update with VALUES
UPDATE products SET price = new_prices.price
FROM (VALUES (1, 9.99), (2, 14.99), (3, 19.99)) AS new_prices(id, price)
WHERE products.id = new_prices.id;

-- Fast bulk insert with COPY
COPY products (name, price, category_id)
FROM '/tmp/products.csv'
WITH (FORMAT CSV, HEADER true);

-- Upsert (insert or update)
INSERT INTO user_stats (user_id, login_count, last_login)
VALUES (42, 1, NOW())
ON CONFLICT (user_id) DO UPDATE SET
    login_count = user_stats.login_count + 1,
    last_login = EXCLUDED.last_login;
```

## Partitioning for Very Large Tables

When a table exceeds ~100M rows, consider table partitioning:

```sql
-- Partition orders by year/month
CREATE TABLE orders (
    id BIGSERIAL,
    user_id INT NOT NULL,
    amount DECIMAL NOT NULL,
    created_at TIMESTAMP NOT NULL
) PARTITION BY RANGE (created_at);

CREATE TABLE orders_2025 PARTITION OF orders
    FOR VALUES FROM ('2025-01-01') TO ('2026-01-01');

CREATE TABLE orders_2026 PARTITION OF orders
    FOR VALUES FROM ('2026-01-01') TO ('2027-01-01');

-- Queries on recent data only scan the relevant partition
SELECT * FROM orders WHERE created_at > '2026-01-01';
-- → Only scans orders_2026, not orders_2025 (partition pruning)
```

The most impactful SQL optimizations, in order:
1. Add missing indexes (especially on foreign keys and WHERE columns)
2. Fix N+1 queries
3. Use EXPLAIN ANALYZE and fix what you find
4. Replace expensive queries with materialized views
5. Partition enormous tables

Measure before and after every change. A 50ms query on a small dataset may be fine — context always matters.
