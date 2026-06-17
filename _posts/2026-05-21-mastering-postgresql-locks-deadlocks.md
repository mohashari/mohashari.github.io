---
layout: post
title: "Mastering PostgreSQL Locks: Row-Level, Table-Level, and Avoiding Deadlocks in Production"
date: 2026-05-21 08:00:00 +0700
tags: [postgresql, database, locks, concurrency, backend]
description: "A deep dive into PostgreSQL locking mechanisms, comparing exclusive, shared, and advisory locks, with concrete strategies to prevent deadlocks."
image: "https://picsum.photos/seed/6910/1080/720"
thumbnail: "https://picsum.photos/seed/6910/400/300"
---

At low traffic, relational databases operate smoothly without developers worrying about concurrency. But as your application scales, more concurrent requests hit the database, attempting to modify the same resources simultaneously. At this point, **locks** become both your best friend (preventing data corruption) and your worst enemy (causing query timeouts, latency spikes, and deadlocks).

PostgreSQL has one of the most sophisticated locking engines of any relational database. Yet, misuse of its locking features can bring production environments to a complete standstill. 

In this guide, we'll dive deep into PostgreSQL's lock hierarchy (table-level, row-level, and advisory locks), explain how lock conflicts happen, and provide concrete, battle-tested strategies to prevent deadlocks and connection pooling starvation in production.

---

## 1. Table-Level Locks: The Multi-Mode Hierarchy

When you execute a query, PostgreSQL automatically acquires table-level locks. It is a common misconception that table-level locks only happen during schema migrations (like `ALTER TABLE`). In fact, *every* `SELECT`, `INSERT`, `UPDATE`, and `DELETE` acquires a table-level lock, but they use different **lock modes** to maximize concurrency.

PostgreSQL supports **8 table-level lock modes**. Here are the most critical ones you need to understand:

| Lock Mode | Acquired By | Conflicts With |
|---|---|---|
| **AccessShareLock** | `SELECT` | `AccessExclusiveLock` |
| **RowShareLock** | `SELECT ... FOR UPDATE` | `AccessExclusiveLock` |
| **RowExclusiveLock** | `UPDATE`, `DELETE`, `INSERT` | `ShareLock`, `AccessExclusiveLock` (and others) |
| **AccessExclusiveLock** | `ALTER TABLE`, `DROP TABLE`, `TRUNCATE` | **All other lock modes** |

### The Silent Killer: AccessExclusiveLock

The `AccessExclusiveLock` is the most restrictive lock in PostgreSQL. It is acquired by DDL commands like `DROP TABLE`, `TRUNCATE`, and many `ALTER TABLE` operations. 

If a long-running reporting `SELECT` query is executing, it holds an `AccessShareLock`. If you attempt to run a migration (like adding a non-nullable column) concurrently, that migration will block waiting for the `AccessExclusiveLock`. 

Even worse, **all subsequent queries trying to access that table will queue behind the blocked migration**, causing your application's connection pool to starve within seconds.

---

## 2. Row-Level Locks: Concurrency on the Micro Scale

Row-level locks are used to serialize updates to individual rows. Unlike table-level locks, row-level locks do not block readers. A `SELECT` query does not block an `UPDATE` on the same row, and vice versa (thanks to **MVCC**).

PostgreSQL offers 4 row-level lock modes:

1.  **FOR UPDATE:** Fully locks the row for updates. No other transaction can update, delete, or lock this row until the holding transaction commits or rolls back.
2.  **FOR NO KEY UPDATE:** A weaker version of `FOR UPDATE` that allows locks like `FOR KEY SHARE` to coexist. Acquired automatically by standard `UPDATE` statements that do not modify unique key columns.
3.  **FOR SHARE:** Acquired when you want to read a row but ensure no other transaction can modify it.
4.  **FOR KEY SHARE:** A weaker version of `FOR SHARE` used by foreign key checks to make sure the referenced key doesn't change.

### Handling Queueing with `NOWAIT` and `SKIP LOCKED`

In high-concurrency environments, queries like `SELECT ... FOR UPDATE` can block your application threads. To write resilient systems, use modern SQL clauses to handle lock conflicts gracefully:

```sql
-- snippet-1
-- Fail immediately if the row is already locked by another transaction
SELECT id, balance 
FROM accounts 
WHERE id = 42 
FOR UPDATE NOWAIT;
```

If the row is locked, PostgreSQL immediately raises a `55P03 (lock_not_available)` error, allowing your application to retry or fail gracefully instead of blocking.

Alternatively, if you are building a custom task queue:

```sql
-- snippet-2
-- Skip any locked rows and grab the first available free task
UPDATE tasks 
SET status = 'processing' 
WHERE id = (
    SELECT id 
    FROM tasks 
    WHERE status = 'pending' 
    LIMIT 1 
    FOR UPDATE SKIP LOCKED
)
RETURNING *;
```

Using `SKIP LOCKED` completely eliminates lock contention in concurrent background workers.

---

## 3. Advisory Locks: Application-Level Distributed Locking

Advisory locks are application-defined locks created inside PostgreSQL. They do not lock tables or rows; instead, they lock an arbitrary 64-bit integer. This makes them perfect for implementing distributed locks (e.g., ensuring a cron job only runs on one node at a time) without adding external systems like Redis.

```sql
-- snippet-3
-- Acquire a transaction-level advisory lock (automatically released on COMMIT/ROLLBACK)
SELECT pg_advisory_xact_lock(148209823);

-- Perform critical business logic
UPDATE users SET points = points + 100 WHERE id = 5;
```

Advisory locks are highly efficient, require no database writes, and are automatically managed by PostgreSQL's transaction lifecycle.

---

## 4. Understanding & Preventing Deadlocks

A **deadlock** occurs when Transaction A holds Lock 1 and wants Lock 2, while Transaction B holds Lock 2 and wants Lock 1. Neither transaction can proceed, creating a permanent circular dependency.

```
Transaction A                       Transaction B
Holds: Lock 1                       Holds: Lock 2
Wants: Lock 2                       Wants: Lock 1
   │                                   │
   └──────────► [Blocked] ◄────────────┘
```

PostgreSQL automatically runs a deadlock detection thread when a query waits longer than the `deadlock_timeout` (default: 1 second). Once detected, PostgreSQL aborts one of the transactions, raising a `40P01 (deadlock_detected)` exception.

### Strategies to Eliminate Deadlocks

1.  **Strict Operation Ordering:** Always modify resources in the exact same order across all database transactions. If Transaction A and B both update `accounts` 12 and 15, both must update 12 *before* 15.
2.  **Short Transactions:** Keep transactions as brief as possible. Avoid making network calls (like calling a payment gateway or third-party API) or running expensive CPU computations inside database transactions.
3.  **Add Indexes on Foreign Keys:** PostgreSQL locks the parent table's keys when updating child table records. Adding indexes on all foreign key columns drastically reduces lock escalation and search times.
4.  **Monitor Lock Contention:** Frequently inspect active locks using `pg_locks` to find hot spots in your schema:

```sql
-- snippet-4
-- Find all queries currently waiting for locks
SELECT 
    blocked_locks.pid     AS blocked_pid,
    blocked_activity.usename  AS blocked_user,
    blocking_locks.pid    AS blocking_pid,
    blocking_activity.usename AS blocking_user,
    blocked_activity.query    AS blocked_statement,
    blocking_activity.query   AS blocking_statement
FROM  pg_catalog.pg_locks         blocked_locks
JOIN pg_catalog.pg_stat_activity blocked_activity ON blocked_activity.pid = blocked_locks.pid
JOIN pg_catalog.pg_locks         blocking_locks 
    ON blocking_locks.locktype = blocked_locks.locktype
    AND blocking_locks.database IS NOT DISTINCT FROM blocked_locks.database
    AND blocking_locks.relation IS NOT DISTINCT FROM blocked_locks.relation
    AND blocking_locks.page IS NOT DISTINCT FROM blocked_locks.page
    AND blocking_locks.tuple IS NOT DISTINCT FROM blocked_locks.tuple
    AND blocking_locks.virtualxid IS NOT DISTINCT FROM blocked_locks.virtualxid
    AND blocking_locks.transactionid IS NOT DISTINCT FROM blocked_locks.transactionid
    AND blocking_locks.classid IS NOT DISTINCT FROM blocked_locks.classid
    AND blocking_locks.objid IS NOT DISTINCT FROM blocked_locks.objid
    AND blocking_locks.objsubid IS NOT DISTINCT FROM blocked_locks.objsubid
    AND blocking_locks.pid != blocked_locks.pid
JOIN pg_catalog.pg_stat_activity blocking_activity ON blocking_activity.pid = blocking_locks.pid
WHERE NOT blocked_locks.granted;
```

## Summary

Locking issues are a natural consequence of database scale. By designing schema migrations with low timeouts, structuring update patterns sequentially, and leveraging modern SQL constructs like `SKIP LOCKED` and `NOWAIT`, you can maintain highly responsive and rock-solid relational backends.
