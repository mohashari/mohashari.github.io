---
layout: post
title: "Designing a Distributed Schedulable Job Queue with PostgreSQL Skip Locked and pg_cron"
date: 2026-06-23 08:00:00 +0700
tags: [postgresql, go, distributed-systems, database]
description: "Build a highly resilient, distributed, transactional job queue using PostgreSQL's SKIP LOCKED and pg_cron without adding infrastructure complexity."
image: "https://picsum.photos/seed/4009/1080/720"
thumbnail: "https://picsum.photos/seed/4009/400/300"
---

Imagine this: a high-traffic e-commerce system processes thousands of orders per minute. A customer completes a checkout, the billing service successfully updates the database, but the network call to publish a "send_invoice_email" event to a Redis-backed queue fails. The database transaction committed, but the background job was lost. Alternatively, the opposite occurs: the background job enqueues successfully, but the primary database transaction rolls back due to a serialization conflict. The worker immediately runs the job, tries to query the database for the order details, and crashes because the order does not exist. This is the classic dual-write problem. 

In production, resolving this using separate datastores (like Postgres for OLTP and Redis or RabbitMQ for queues) requires complex coordination patterns such as the Transactional Outbox pattern or two-phase commits (2PC). By leveraging PostgreSQL's native `FOR UPDATE SKIP LOCKED` locking modifier and the `pg_cron` background worker extension, we can design a highly resilient, distributed, and schedulable job queue directly within our existing database. This approach guarantees atomic consistency—the job is committed if and only if the business transaction commits—and handles concurrency across distributed workers without the overhead of additional infrastructure.

## Transactional Enqueuing: The Outbox Guarantee

The fundamental advantage of a database-backed job queue is transactional safety. By keeping your business data and your job queue in the same physical database, you can enqueue jobs inside the same transaction that modifies your business models. If the business write succeeds, the job is guaranteed to be scheduled. If the write rolls back, the job is rolled back with it. No sidecars, no polling of transaction logs (like Debezium), and no external state engines are required.

To build this system, we need a table structured to support high-concurrency dequeue operations, scheduling, retries, and failure tracking.

<script src="https://gist.github.com/mohashari/bc7bc57ee1c7b8e70247c6b53d9aead3.js?file=snippet-1.sql"></script>

### The Indexing Strategy Explained

A common mistake in database-backed queues is creating a standard composite index on `(status, run_at)`. Because the vast majority of jobs in a healthy system will be in the `completed` or `failed` state, this index will grow proportionally to the size of the table, eventually leading to high disk footprint and slower search times.

Instead, we define a partial index: `idx_jobs_dequeue ON jobs (run_at ASC, id ASC) WHERE status = 'pending'`. 

This partial index contains only the rows representing active, pending jobs. In a system running millions of jobs daily, the index size remains tiny (often a few kilobytes or megabytes), allowing it to stay completely cached within PostgreSQL's `shared_buffers` memory. We include `id ASC` as a secondary sort key to ensure deterministic ordering and prevent lock thrashing when multiple workers poll the queue.

## Dequeuing at Scale: SKIP LOCKED Mechanics

If multiple independent instances of our Go application attempt to fetch jobs simultaneously, they must do so without interfering with each other or locking the entire table.

A standard `SELECT ... FOR UPDATE` statement locks the selected rows. If Worker A executes the query to pick up the top 5 jobs, Worker B running the same query will block, waiting for Worker A's transaction to commit or roll back. This completely destroys horizontal scalability and degrades the queue into a serialized bottleneck.

By appending the `SKIP LOCKED` modifier, we instruct PostgreSQL to bypass any rows that are already locked by another transaction. Worker A locks the first 5 jobs, and Worker B immediately skips those 5 and locks the next 5. 

To execute this atomically in a single round-trip, we use a Common Table Expression (CTE) that selects the target IDs using `SKIP LOCKED` and immediately updates their status to `running`.

<script src="https://gist.github.com/mohashari/bc7bc57ee1c7b8e70247c6b53d9aead3.js?file=snippet-2.sql"></script>

This single query accomplishes three crucial tasks:
1. It locates the oldest pending jobs scheduled for execution.
2. It locks those rows to prevent other workers from accessing them.
3. It updates the state of those rows to prevent them from appearing in subsequent queries, and returns the payload to the worker.

Because the `UPDATE` is bound to the CTE, the lock duration on the rows is minimal, allowing high throughput.

## Implementing the Go Dequeue Client

To execute this query in Go, we must parse the results safely and handle serialization errors. Below is the implementation of the Go data access method.

<script src="https://gist.github.com/mohashari/bc7bc57ee1c7b8e70247c6b53d9aead3.js?file=snippet-3.go"></script>

## Building a Resilient Worker Pool in Go

A robust background worker must not only retrieve jobs but also execute them concurrently, handle system panics, implement exponential backoff for retries, and shut down gracefully when a deploy signal is received.

The critical architectural detail here is **releasing the database connection immediately after dequeuing**. The worker must not hold the database transaction open while performing long-running logic (e.g., calling an external payment gateway). Instead, the worker pool:
1. Dequeues the job (committing the `UPDATE jobs SET status = 'running'` transaction immediately).
2. Executes the job payload in memory using application goroutines.
3. Opens a new transaction to mark the job as `completed` or `failed` (backoff).

<script src="https://gist.github.com/mohashari/bc7bc57ee1c7b8e70247c6b53d9aead3.js?file=snippet-4.go"></script>

## Scheduling the Future: pg_cron to the Rescue

A complete job queue system requires scheduling recurring tasks (e.g., executing a billing run every midnight, generating system statistics hourly, or cleaning up old logs). 

Coordinating these schedules across multiple stateless application containers is notoriously difficult. If 10 container replicas run a local cron scheduler, they will all trigger the midnight job at the exact same millisecond, leading to duplicated work and database deadlocks.

Instead of managing distributed leader election at the application level, we delegate the cron scheduling to PostgreSQL using the `pg_cron` extension. This extension runs directly inside the database engine as a background worker process, reading schedules from a dedicated configuration table.

To prevent database overhead, we enforce a strict rule: **`pg_cron` must never execute heavy business logic.** It must only write a lightweight, pending job payload into our `jobs` table. The distributed Go workers then pick up and execute the job asynchronously.

<script src="https://gist.github.com/mohashari/bc7bc57ee1c7b8e70247c6b53d9aead3.js?file=snippet-5.sql"></script>

By utilizing `cron.schedule_in_database`, the scheduler runs globally in the PostgreSQL controller, but inserts the target record directly into our application database (`app_db`). This maintains isolation and keeps the scheduler decoupled from the application logic.

## Operational Maintenance and Stuck Job Sweepers

In a distributed environment, servers crash, Kubernetes pods get killed by the OOM (Out Of Memory) killer, and networks partition. When a worker dies while processing a job, the job record remains in the `running` state indefinitely, locked out of future dequeue cycles.

To ensure reliability, we must build a self-healing mechanism that identifies abandoned jobs and resets them. We can schedule this maintenance routine using `pg_cron`.

Additionally, database tables used as queues generate a high volume of historical records. We must prune successfully completed jobs to prevent performance degradation.

<script src="https://gist.github.com/mohashari/bc7bc57ee1c7b8e70247c6b53d9aead3.js?file=snippet-6.sql"></script>

## Production Failure Modes and Performance Tuning

While PostgreSQL is highly capable of running a job queue, treating a relational database like an append-only log introduces unique performance characteristics that you must actively manage in production.

### 1. PostgreSQL MVCC Table Bloat
PostgreSQL implements Multi-Version Concurrency Control (MVCC). When a worker updates a job status from `pending` to `running`, and then to `completed`, Postgres does not modify the row in place. Instead, it marks the old row version (tuple) as dead and inserts a new one. 

Under a load of 1,000 jobs per minute, this system generates 1.4 million dead tuples a day. If your autovacuum settings are left at their defaults, the table will bloat rapidly. The database will have to scan pages of dead tuples during every dequeue operation, leading to skyrocketing CPU usage and query times.

To mitigate this, you must tune autovacuum specifically for the `jobs` table to ensure dead tuples are cleaned up aggressively:

```sql
ALTER TABLE jobs SET (
    autovacuum_vacuum_scale_factor = 0.05,
    autovacuum_vacuum_threshold = 500,
    autovacuum_vacuum_cost_limit = 1000,
    autovacuum_vacuum_cost_delay = 2
);
```

These parameters reduce the scale factor from the default `0.20` (20%) to `0.05` (5%), triggering the autovacuum daemon much sooner. The increased `cost_limit` allows the vacuum worker to consume more IOPS, completing the cleanup cycle faster.

### 2. Lock Contention and Deadlocks
Even with `SKIP LOCKED`, deadlocks can occur if the sorting order of the subquery is not matched by the indexes, or if the update lock paths conflict. 

Always ensure your dequeue query includes `ORDER BY run_at ASC, id ASC`. Matching this order exactly in the partial index `idx_jobs_dequeue` guarantees that workers scan the physical index tuples in the identical order, resolving potential index scan deadlocks.

### 3. Idle Connection Resets
If your Go worker pool poll interval is long, or if there are periods of zero traffic, PostgreSQL may close idle connections. If your database driver (e.g. `pgx`) does not verify connection liveness before initiating the transaction containing the `FOR UPDATE SKIP LOCKED` query, you will encounter `write: broken pipe` or `connection reset by peer` errors.

Configure your connection pool settings to recycle idle connections proactively:

```go
db.SetConnMaxIdleTime(10 * time.Minute)
db.SetConnMaxLifetime(30 * time.Minute)
```

Ensure the idle timeout is lower than your database's `wait_timeout` setting.

## Summary

Designing a distributed job queue using PostgreSQL `SKIP LOCKED` and `pg_cron` provides strict transactional integrity, operational simplicity, and dynamic scheduling capability. By executing updates inside a fast CTE and keeping business logic out of `pg_cron`, you eliminate the complexity of running separate message queues for transactional data. Combined with aggressive autovacuum tuning, this architecture easily scales to handle millions of jobs per day on standard database hardware.