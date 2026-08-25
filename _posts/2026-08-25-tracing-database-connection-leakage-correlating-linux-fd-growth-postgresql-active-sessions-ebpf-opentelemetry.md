---
layout: post
title: "Tracing Database Connection Leakage: Correlating Linux File Descriptor Growth with PostgreSQL Active Sessions using eBPF and OpenTelemetry"
date: 2026-08-25 08:00:00 +0700
tags: [ebpf, postgresql, opentelemetry, observability]
description: "Trace database connection leaks by correlating host-level file descriptor growth with PostgreSQL sessions using eBPF and OpenTelemetry."
image: "https://picsum.photos/seed/6766/1080/720"
thumbnail: "https://picsum.photos/seed/6766/400/300"
---

At 2:00 AM, the alerts go off: your primary PostgreSQL instance has exhausted its connection pool, rejecting incoming queries with the dreaded `pq: sorry, too many clients already` error. On the application side, pods are crashing with `panic: too many open files` as their Linux file descriptor (FD) limits are breached. The immediate trigger is obvious—a database connection leak—but finding the root cause in a distributed, high-throughput microservice architecture is a nightmare. Standard application connection pool metrics might report that the pool is healthy, while raw socket allocations steadily climb in the background. Polling the filesystem's `/proc` directory under heavy production loads degrades kernel VFS performance, making traditional tracing methods unusable. Resolving this requires a low-overhead, system-wide correlation strategy: linking system-level file descriptor growth with database-level active sessions in real time. By deploying eBPF probes at the system call level and aggregating the telemetry alongside database internals using OpenTelemetry, you can pinpoint the exact application path leaking sockets before your system degrades.

![Tracing Database Connection Leakage: Correlating Linux File Descriptor Growth with PostgreSQL Active Sessions using eBPF and OpenTelemetry Diagram](/images/diagrams/tracing-database-connection-leakage-correlating-linux-fd-growth-postgresql-active-sessions-ebpf-opentelemetry.svg)

## The Mechanics of a Database Connection Leak

In a high-throughput Linux environment, database connections are not abstract abstractions; they are concrete operating system resources. When a Go, Java, or Node.js backend client establishes a connection to PostgreSQL, the lifecycle involves multiple layers of the system stack:

1. **VFS Allocation**: The application requests a connection. The operating system kernel allocates a file descriptor (FD) via the `sys_socket` system call.
2. **Network Connection**: The application invokes `sys_connect` to initiate a TCP handshake to port 5432. The file descriptor now maps to an active TCP socket in the kernel's socket table.
3. **Database Forking**: On the database host, the PostgreSQL `postmaster` daemon accepts the incoming connection and spawns a dedicated backend worker process (`postgres: user db host(port) state`).

This 1:1 mapping between a client-side socket file descriptor and a database-side backend process is the foundation of database session management. A connection leak occurs when the client application discards its socket handle without executing a clean shutdown (`sys_close`). 

Leaked connections generally manifest in three ways:

* **Unclosed Transaction Blocks**: An application executes a SQL statement inside a `BEGIN` block but crashes, times out, or fails to execute a `COMMIT` or `ROLLBACK` due to unhandled error paths. The connection remains in the `idle in transaction` state on the database.
* **Orphaned Sockets**: A goroutine or thread is blocked indefinitely on a read or write operation without a timeout. The client runtime loses the reference to the database connection, but because the executing thread is alive, the socket descriptor is never reclaimed by the garbage collector.
* **Driver and ORM Bypasses**: Sub-systems bypassing connection pools (e.g., executing raw queries on a manually opened connection and failing to close the connection in a `defer` or `finally` block).

In these scenarios, the client application pool metrics might show the pool as idle and healthy because the leak exists outside the connection pool's scope. However, the client OS will show a steady climb in active sockets, and the database will show a corresponding climb in active backends. Eventually, you hit one of two walls:

1. **Client-Side Exhaustion (EMFILE)**: The client process hits its `ulimit -n` limit (often set to 1024 or 4096 in default container runtimes). The application can no longer open new files, resolve DNS, or accept incoming requests.
2. **Server-Side Exhaustion**: The database hits `max_connections` (e.g., 500). All application instances are locked out of the database, causing cascading outages across the microservice mesh.

## eBPF: Low-Overhead Socket Tracking in Kernel Space

To detect a leak before it triggers an outage, you must track the creation and destruction of socket file descriptors. Traditional monitoring tools rely on polling `/proc/<pid>/fd/` or parsing `/proc/net/tcp` at regular intervals. While functional, this approach scales poorly: scanning thousands of file descriptors under high concurrency causes significant lock contention on the kernel's Virtual File System (VFS), introducing system call latency spikes.

eBPF (Extended Berkeley Packet Filter) bypasses this overhead by executing sandboxed C code directly inside the Linux kernel in response to tracepoints. By hooking into system calls like `connect` and `close`, we can trace socket lifespans with microsecond precision and near-zero CPU overhead.

The following eBPF program hooks into the `sys_enter_connect` and `sys_exit_connect` tracepoints to capture socket file descriptors bound to PostgreSQL (port 5432) and streams the events to userspace via a BPF ring buffer.

<script src="https://gist.github.com/mohashari/823158727af1dfdebbc5258a4bd0c8d9.js?file=snippet-1.txt"></script>

To consume these events in userspace, we use a Go application using the `cilium/ebpf` library. This program reads from the eBPF ring buffer, updates an in-memory map of active sockets, and exposes the data as Prometheus metrics.

<script src="https://gist.github.com/mohashari/823158727af1dfdebbc5258a4bd0c8d9.js?file=snippet-2.go"></script>

## OpenTelemetry Application Instrumentation

While eBPF gives you absolute visibility into kernel file descriptors and sockets, it cannot inspect the internal state of the application's database connection pool. To determine whether a connection leak is happening because of pool misconfiguration or because connections are bypassing the pool altogether, you need to instrument the application.

Using the OpenTelemetry metric API, we can hook into database drivers (such as Go's `database/sql` using the `pgx` driver) to emit metrics describing the internal state of the pool, including:
* The number of active connections currently checked out.
* The number of idle connections sitting in the pool.
* The number of threads blocked waiting for a connection to become available.

The following snippet demonstrates how to wrap a standard Go database connection with OpenTelemetry database metrics using the `otelsql` library.

<script src="https://gist.github.com/mohashari/823158727af1dfdebbc5258a4bd0c8d9.js?file=snippet-3.go"></script>

## Querying PostgreSQL Session State

At the other end of the socket connection, the database server maintains metadata about every active and idle backend process. In PostgreSQL, this data is exposed via the `pg_stat_activity` system catalog view.

When troubleshooting connection leaks, raw connection counts are insufficient. You must identify the state of the backends. The most critical state to monitor is `idle in transaction`. When a backend process is marked as `idle in transaction`, it means the client opened a transaction block (`BEGIN`), but the database is waiting for the client to execute another query or send a final `COMMIT` or `ROLLBACK` statement.

This state is exceptionally destructive for two reasons:
1. **Locks**: The session keeps locks on any tables or rows modified during the transaction, blocking other queries and causing application thread pool starvation.
2. **Table Bloat**: PostgreSQL uses Multi-Version Concurrency Control (MVCC) to manage transaction isolation. A backend process holding an open transaction prevents the `autovacuum` daemon from reclaiming dead tuples created after the start of that transaction. This results in disk space exhaustion and query degradation due to table and index bloat.

To isolate leaked and stuck transactions, you can run the following SQL query on your PostgreSQL primary. It identifies sessions that have been in an idle or active state beyond acceptable SLAs and computes the exact duration of the leak.

<script src="https://gist.github.com/mohashari/823158727af1dfdebbc5258a4bd0c8d9.js?file=snippet-4.sql"></script>

## Correlating Telemetry inside the OpenTelemetry Collector

Having metrics generated in three distinct layers (eBPF kernel space, application runtime, and database system views) is only half the battle. To extract actionable intelligence, you need to aggregate and correlate these metrics.

The OpenTelemetry Collector serves as this correlation engine. By routing all metrics through a single pipeline, we can standardize attributes (like Kubernetes pod names, host IPs, and environment identifiers) and ensure that metrics from the host operating system map perfectly to the target database and the corresponding application pods.

The following configuration sets up an OpenTelemetry Collector to ingest OTLP metrics from the application, scrape host eBPF metrics, monitor PostgreSQL internals, and output the consolidated data to a Prometheus exporter.

<script src="https://gist.github.com/mohashari/823158727af1dfdebbc5258a4bd0c8d9.js?file=snippet-5.yaml"></script>

## Setting up Prometheus Alerting and Grafana Visualization

Once the correlated metrics are stored in Prometheus, you can write alerts that catch connection leaks.

If you only alert on raw database connection counts, you will trigger false positives during traffic spikes. If you only alert on client file descriptors, you will miss instances where the application is leaking database connections but has not yet hit its operating system limits.

The solution is a multi-dimensional PromQL query that monitors three conditions:
1. The kernel-level database sockets (tracked via eBPF) must be growing.
2. The delta between kernel sockets and the application pool's internal used connections must be widening (proving the leak is bypass-based or orphaned).
3. The database itself must show a corresponding elevation in `idle_in_transaction` sessions.

<script src="https://gist.github.com/mohashari/823158727af1dfdebbc5258a4bd0c8d9.js?file=snippet-6.yaml"></script>

## Production Remediation Strategies

When your alerting system detects a connection leak, your immediate goal is to prevent a system-wide database crash, followed by permanent structural fixes.

### 1. Emergency Triage (Kill the Leaked Backends)
When database slots are exhausted, your application will fail to start new transactions. To free up slots immediately, identify the offending PIDs using the query in **Snippet 4** and terminate them:

* **Cancel Backend**: `SELECT pg_cancel_backend(pid);`
  This sends a `SIGINT` to the backend. It stops the query currently executing but does *not* close the client socket. If the client is stuck in an `idle in transaction` loop, this command will have no effect.
* **Terminate Backend**: `SELECT pg_terminate_backend(pid);`
  This sends a `SIGTERM` to the backend. It forcibly closes the database session, drops the TCP connection, and frees the connection slot. This is the command you need to resolve connection leaks.

### 2. Defending the Database (Server-Side Timeouts)
Do not rely entirely on the application to clean up its connections. Configure safety boundaries inside your `postgresql.conf` configuration file:

```sql
# Close any transaction block that remains inactive/idle for more than 60 seconds
idle_in_transaction_session_timeout = 60000

# Terminate any query that runs longer than 5 minutes (excluding analytical databases)
statement_timeout = 300000

# Configure aggressive TCP keepalive timeouts to detect dead sockets
tcp_keepalives_idle = 60
tcp_keepalives_interval = 10
tcp_keepalives_count = 6
```

If a client container crashes silently or encounters a network partition, these kernel-level TCP keepalives force the database server to clean up the orphaned backend processes within two minutes.

### 3. Application Hardening
Fix the root cause within the application code:
* **Strict Resource Reclamation**: Ensure all database queries and transaction blocks are wrapped in cleanup guarantees. In Go, always use `defer rows.Close()` immediately after checking the query error.
* **Pool Age Lifespans**: Enforce limits on the maximum duration a connection can exist. By setting `SetConnMaxLifetime` and `SetConnMaxIdleTime`, you instruct the pool to systematically retire and close sockets, which limits the lifespan of any leaked file descriptors.