---
layout: post
title: "Debugging Database Connection Pool Starvation in Go: Correlating PgBouncer Metrics with Async Event Loop Latency"
date: 2026-08-19 08:00:00 +0700
tags: [go, postgresql, pgbouncer, observability, latency]
description: "Diagnose and resolve database connection pool starvation in Go apps by correlating PgBouncer metrics with scheduler and event loop latency."
image: "https://picsum.photos/seed/7119/1080/720"
thumbnail: "https://picsum.photos/seed/7119/400/300"
---

It is a classic production nightmare: under a sudden 3x traffic spike, your Go API response times balloon from a crisp 15 milliseconds to a catastrophic 5 seconds, causing upstream load balancers to drop connections and return 504 Gateway Timeouts. Your team looks at the PostgreSQL instance—CPU utilization is sitting idle at 12%, and disk I/O is nominal. You check PgBouncer—CPU is low, but client connection requests are backing up. You look at Go—goroutine count is climbing, and context deadline errors are flooding the logs. In a panic, engineers try to scale the database or increase PgBouncer's maximum connection pool size, only to make the starvation worse. What you are witnessing is not a database bottleneck, but database connection pool starvation driven by Go runtime scheduler and event loop latency. When CPU-bound tasks or garbage collection pauses delay the Go scheduler, active goroutines stall while holding checked-out database connections. This increases connection hold time, starves the application’s local pool, and saturates PgBouncer's client waiting queue, cascading into a cluster-wide outage.

![Debugging Database Connection Pool Starvation in Go: Correlating PgBouncer Metrics with Async Event Loop Latency Diagram](/images/diagrams/debugging-database-connection-pool-starvation-go-correlating-pgbouncer-metrics-async-event-loop-latency.svg)

## The Anatomy of the Cascade: How Go Scheduler Latency Starves the Pool

In a typical Go application, concurrency is managed by the Go runtime's scheduler, which implements the Work-Stealing GMP model. Under this model, Goroutines (`G`) represent the logical threads of execution, Machine (`M`) represents OS threads, and Processor (`P`) represents logical processors or execution contexts (configured via `GOMAXPROCS`). When your Go code interacts with a database, it checks out a connection from a pool (such as `pgxpool` or `database/sql`) and assigns it to a goroutine to execute SQL commands.

When load spikes, CPU saturation can prevent goroutines from being scheduled in a timely manner. While Go uses signal-based preemptive scheduling (since version 1.14), certain runtime operations—particularly Garbage Collection (GC) mark/sweep phases, contention on runtime mutexes, or heavy non-preempted CPU loops—still introduce latency spikes. If a goroutine is preempted or suspended *after* checking out a database connection but *before* releasing it, that connection is held open but inactive in the database.

In a high-throughput system running at 10,000 queries per second, a 50-millisecond scheduler delay on a few dozen goroutines is disastrous. While those goroutines are stuck in the scheduler's run queue, their active database connections remain checked out. To the local connection pool, these connections are 'in use' or busy. As other concurrent handlers attempt to execute database queries, they find the pool empty. They block on the pool's wait queue, waiting for a connection to be returned.

This is pool starvation. If your HTTP handlers are configured with a pool size of 50, and 50 goroutines are stalled by scheduler latency or GC pauses, the application's capacity drops to zero. As requests pile up, the Go runtime attempts to spawn more goroutines to handle incoming requests, further saturating the CPU, increasing scheduler queue length, and exacerbating the latency cascade. The starvation propagates downstream, saturating PgBouncer's queue as well.

The snippet below demonstrates this failure pattern and shows how to refactor your code to release connections back to the pool as quickly as possible.

<script src="https://gist.github.com/mohashari/e9b67426624eb4b28985f4525ed36e21.js?file=snippet-1.go"></script>

## PgBouncer Metrics: Separating App Latency from DB Saturation

When pool starvation occurs, the database itself is often blamed first. However, if PostgreSQL's CPU utilization remains low, the issue is not database capacity, but connection allocation. In high-density environments, PgBouncer sits between your Go service and PostgreSQL, acting as an asynchronous connection proxy.

PgBouncer uses a single-threaded event loop driven by `libevent` to multiplex thousands of client connections onto a small pool of PostgreSQL server backends. In `transaction` mode (`pool_mode = transaction`), PgBouncer assigns a server connection only when a client starts a transaction (e.g., executing a query or an explicit `BEGIN` statement) and returns it as soon as the transaction completes (`COMMIT` or `ROLLBACK`).

If a Go application stalls, PgBouncer is caught in the middle. When a goroutine checks out a connection and starts a query, PgBouncer assigns an active backend connection (`sv_active`). If the Go application's runtime halts execution due to scheduler latency or if the application performs blocking network calls within the transaction boundary, the PgBouncer server connection remains locked to that specific client connection. Because PgBouncer’s backend server pool size is strictly limited (typically set to a value like `pool_size = 20` per database/user), a handful of stalled Go instances can easily saturate all available backend connections.

When all backend connections are busy (`sv_active` equals `pool_size`), any new queries sent by other Go instances are queued. PgBouncer increments the `cl_waiting` (waiting clients) counter. This is the single most critical metric for diagnosing pool starvation. A high `cl_waiting` value indicates that clients are active and trying to talk to the database, but PgBouncer has no server connections left to give them.

To diagnose this, you must query PgBouncer's virtual administration database. You connect using the `pgbouncer` database name and execute administrative commands like `SHOW POOLS` and `SHOW STATS`. The following Go code demonstrates how to query PgBouncer's internal state to monitor these metrics programmatically.

<script src="https://gist.github.com/mohashari/e9b67426624eb4b28985f4525ed36e21.js?file=snippet-3.go"></script>

## Instrumenting Go Runtime Metrics for Scheduler Latency

To prove that PgBouncer pool starvation is driven by client-side latency rather than database sluggishness, you must capture Go's internal runtime scheduling behavior. Traditional metrics like CPU percentage, memory allocation, and thread counts are too coarse to identify transient scheduler pauses.

Go 1.16 introduced the `runtime/metrics` package, providing a stable, high-performance API to inspect the runtime’s internal state. Two key metrics are vital for diagnosing connection starvation:

1. `/sched/latencies:seconds`: A cumulative histogram of the time goroutines spend in the runnable state before being scheduled to run on a logical processor (`P`). Under normal conditions, this latency should be in the microsecond range. During scheduler starvation or CPU saturation, this value can spike to tens or hundreds of milliseconds.
2. `/gc/pauses:seconds`: A histogram of individual Garbage Collector Stop-The-World (STW) pauses. High GC pause times halt all application goroutines, including those holding database connections, stalling transaction completion.

By collecting and analyzing these histograms, you can calculate specific percentiles (e.g., P99) of scheduler latency. If you observe a spike in P99 scheduler latency that correlates with a drop in application database query throughput and a spike in PgBouncer waiting clients, you have confirmed a client-side scheduling bottleneck. The code below shows how to extract percentile metrics from Go's native runtime metrics system.

<script src="https://gist.github.com/mohashari/e9b67426624eb4b28985f4525ed36e21.js?file=snippet-2.go"></script>

## Correlating the Metrics with PromQL and Alerting

Once you have instrumented the Go application to expose scheduler metrics and configured Prometheus to scrape your PgBouncer instances (using tools like the Prometheus `pgbouncer_exporter`), you can correlate the data.

When troubleshooting an active incident, plot these metrics side-by-side on a dashboard. If the database itself is the bottleneck (e.g., a missing index on a heavy query), you will see high PgBouncer transaction times (`total_xact_time` in `SHOW STATS`) and high PostgreSQL CPU utilization, but Go's scheduler latency will remain low and flat.

If the client application is the bottleneck, the sequence of events looks like this:

1. Go scheduler latency (`go_sched_latencies_seconds`) or GC pause times (`go_gc_pauses_seconds`) spike.
2. Go client pool checkouts begin to time out.
3. PgBouncer's `pgbouncer_pools_client_waiting_connections` (representing `cl_waiting`) spikes as server connections are saturated by stalled clients.
4. Database CPU usage drops to near-zero as it waits for stalled clients to send commits or rollbacks.

To automate the detection of this specific failure cascade, you can write Prometheus alerts that look for this correlation. The Prometheus rule below triggers a critical alert when PgBouncer client queues are piling up while Go scheduler latency is elevated.

<script src="https://gist.github.com/mohashari/e9b67426624eb4b28985f4525ed36e21.js?file=snippet-4.yaml"></script>

## Production-Ready Connection Pool Configuration in Go

Preventing database connection pool starvation requires configuring your Go database client pool with production-ready constraints. Using default settings in Go's `database/sql` or `pgxpool` is a liability under load. By default, `database/sql` has no limit on the number of open connections, which can lead to your application opening thousands of connections and crashing PgBouncer or PostgreSQL due to thread exhaustion.

When configuring `pgxpool.Pool`, you must balance three variables: pool size, checkout timeouts, and connection lifecycles.

1. `MaxConns`: This should be strictly capped. Setting it too high increases context switching and locks in PostgreSQL. Setting it too low causes app-side bottlenecking. If PgBouncer has a pool size of 20 server connections per user/db, setting `MaxConns` on a single Go instance to 50 is acceptable because PgBouncer multiplexes client connections. However, across multiple application instances, the sum of all client pools must not overwhelm PgBouncer's `max_client_conn` or PostgreSQL's `max_connections`.
2. `ConnectTimeout` and `AcquireTimeout`: Never allow a query to wait indefinitely for a connection. You must set a strict context timeout when calling `Acquire()` or executing queries. If the application cannot check out a connection within 500ms to 1s, it should fail fast and return a 503 Service Unavailable, preserving resource capacity.
3. `MaxConnLifetime` and `MaxConnIdleTime`: Recycle connections periodically to prevent memory leaks and clear stale TCP sockets that might have silent network drops.

Below is a production-grade configuration pattern using the `pgxpool` library.

<script src="https://gist.github.com/mohashari/e9b67426624eb4b28985f4525ed36e21.js?file=snippet-5.go"></script>

## Tuning PgBouncer for High-Concurrency Go Services

PgBouncer must be configured to handle high-frequency connection checkouts and protect the underlying PostgreSQL server. In a typical Go deployment with multiple microservices, each service instance opens its own connection pool. If you scale your Go deployment to 100 pods, and each pod has a pool size of 50, your application will attempt to open up to 5,000 client connections to PgBouncer.

To support this topology, tune the following configuration variables:

1. `pool_mode = transaction`: This is mandatory. Session pooling (`session`) locks a backend PostgreSQL connection to a client socket for its entire lifetime. Under session mode, PgBouncer provides no multiplexing benefits for Go pools, and connection starvation will occur immediately.
2. `max_client_conn`: This must be set high enough to accommodate the aggregate pool size of all Go instances plus a buffer for admin connections. A value of 5,000 to 10,000 is common in production.
3. `default_pool_size`: This determines the maximum number of active server connections PgBouncer will maintain to the PostgreSQL backend for a single database. This should be set based on PostgreSQL's hardware limits. For example, if your DB server has 8 vCPUs, a pool size of 20 to 40 connections is optimal. Setting it higher increases disk and CPU contention.
4. `reserve_pool_size` and `reserve_pool_timeout`: These act as a safety valve. If clients are waiting longer than `reserve_pool_timeout` (e.g., 2 seconds), PgBouncer opens up to `reserve_pool_size` additional backend connections to clear the queue.

The configuration file below is a standard production configuration for PgBouncer running in front of a high-throughput Go service.

<script src="https://gist.github.com/mohashari/e9b67426624eb4b28985f4525ed36e21.js?file=snippet-6.txt"></script>

## Summary and Checklist

Debugging database connection starvation in Go requires looking beyond the database. When PgBouncer metrics indicate that client connections are waiting (`cl_waiting` is high) while the database is idle, the bottleneck is downstream in your application runtime.

Use this checklist to audit and secure your Go-to-PgBouncer-to-PostgreSQL pipeline:

* [ ] **Transaction boundaries**: Keep transactions as short as possible. Never execute HTTP requests, slow file I/O, or complex CPU-bound algorithms within a transaction block.
* [ ] **Runtime instrumentation**: Export Go scheduler latencies (`/sched/latencies:seconds`) and GC pause times to Prometheus. Set up dashboards to correlate these with PgBouncer's client waiting metric.
* [ ] **Strict client pools**: Capping `MaxConns` and specifying a short checkout timeout (e.g., 1000ms) on `pgxpool` configs.
* [ ] **PgBouncer transaction pooling**: Enforcing `pool_mode = transaction` and ensuring `max_client_conn` is scaled to match the total client connection capacity of all Go pods.
* [ ] **Query timeouts**: Using statement and lock timeouts both on the client context and in the database configuration to prevent rogue operations from blocking connections indefinitely.

By establishing clear observability into both the Go runtime scheduler and PgBouncer's connection queues, you can detect pool starvation before it cascades into a production outage, ensuring your high-throughput systems remain resilient under load.