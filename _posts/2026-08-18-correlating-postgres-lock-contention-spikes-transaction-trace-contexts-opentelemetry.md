---
layout: post
title: "Correlating PostgreSQL Lock Contention Spikes with Distributed Trace Contexts in OpenTelemetry"
date: 2026-08-18 08:00:00 +0700
tags: [postgresql, opentelemetry, database-tuning, distributed-tracing, engineering]
description: "Resolve the database observability blindspot by propagating W3C traceparent contexts into SQL query comments to map pg_locks and blocking PIDs directly to HTTP traces."
image: "https://picsum.photos/seed/8957/1080/720"
thumbnail: "https://picsum.photos/seed/8957/400/300"
---

Imagine this production scenario: at 09:14 UTC, your checkout service experiences a sudden spike in p99 latency, cascading into a barrage of HTTP 504 Gateway Timeouts across your API gateway. You open your APM dashboard, but it paints an ambiguous picture—PostgreSQL CPU utilization is sitting at a comfortable 22%, the active connection pool is saturated but not exhausted, and your slowest query dashboards show a simple `UPDATE users SET balance = balance - $1 WHERE id = $2` taking upwards of 15 seconds. In isolation, the query itself should run in sub-millisecond times; it is slow because it is waiting for a row-level lock. However, because traditional application performance monitoring (APM) tools only trace the application thread executing the query, you are blind to the root cause: a long-running background batch transaction that acquired a lock on the same row earlier and has been held up by external network calls. Bridging this gap requires correlating database-level lock waiters and lock holders directly with W3C distributed trace contexts, allowing you to trace lock contention back to the exact user request or asynchronous worker that triggered it.

![Correlating PostgreSQL Lock Contention Spikes with Distributed Trace Contexts in OpenTelemetry Diagram](/images/diagrams/correlating-postgres-lock-contention-spikes-transaction-trace-contexts-opentelemetry.svg)

## The Observability Gap in Database Lock Contention

For years, database observability has operated in a silo separate from application distributed tracing. When a database transaction blocks waiting for a lock, it yields execution time back to the database engine's scheduler, resulting in zero CPU utilization for that backend process. Traditional database monitoring tools like `pg_stat_activity` and `pg_stat_statements` capture the query text, the waiting state, and the wait event class (e.g., `Lock`). However, these tools possess no understanding of the upstream application context. They do not know if the query was executed by a critical user-facing checkout request, a background analytics job, or an administrative script.

On the application side, modern distributed tracing tools (like OpenTelemetry SDKs combined with database driver instrumentation) capture spans for database queries. While they successfully record the duration of the query, they only capture the symptoms. The span shows a database operation taking 15 seconds and failing with a `context.DeadlineExceeded` error. The trace tells you *that* you blocked, but it cannot tell you *who* blocked you. The blocking transaction belongs to a completely different application thread, running on a different container, under a different trace context.

This disconnect is a recipe for finger-pointing during high-severity production incidents. The database administrator (DBA) points to the application team, claiming the application is holding transactions open too long. The application engineers point to the database, claiming query planning degradation or CPU starvation. To break this impasse, we must treat the database lock manager as a first-class citizen in our distributed tracing pipelines. We must find a way to propagate W3C trace contexts into PostgreSQL, map the blocked-by relationships between transactions, and export these relationships back into our observability platform as span links.

## Injecting Distributed Trace Context into SQL Queries

To correlate database lock states with application traces, we need to pass trace context to PostgreSQL. The most seamless, non-intrusive way to achieve this is by injecting the W3C `traceparent` header into the SQL text itself as an inline comment. 

The W3C `traceparent` header consists of four fields:
1. **Version**: A 2-hex-character version field (currently `00`).
2. **Trace ID**: A 32-hex-character unique identifier for the entire distributed trace.
3. **Parent ID/Span ID**: A 16-hex-character unique identifier for the calling span.
4. **Trace Flags**: An 8-bit field (represented by 2 hex characters) controlling tracing options, primarily sampling (e.g., `01` for sampled).

By formatting this string as `/*traceparent=00-trace_id-span_id-flags*/` and prefixing or suffixing it to our SQL statements, the trace context becomes visible in the PostgreSQL `pg_stat_activity` system catalog. The query string, including the comments, is retained in the active query log of the database while the statement is executing.

To implement this without cluttering application code with manual comment string construction, we can write a database driver decorator in Go. By wrapping the standard database driver's query execution hooks, we can automatically extract the active trace context from the Go `context.Context` and prefix it to the outgoing query text.

<script src="https://gist.github.com/mohashari/3f01b86753a6d5cc2896ec1b5caafb3a.js?file=snippet-1.go"></script>

Once the driver wrapper is written, we can register it with the Go database runtime initialization logic. The following example demonstrates registering this custom driver to decorate connection logic using the popular `github.com/jackc/pgx` driver.

<script src="https://gist.github.com/mohashari/3f01b86753a6d5cc2896ec1b5caafb3a.js?file=snippet-2.go"></script>

## Inspecting pg_locks and pg_stat_activity with Trace Context Awareness

With traceparents propagating to active query strings inside PostgreSQL, we can query database catalog tables to inspect lock contention and extract the trace contexts of the waiter and blocker backends.

PostgreSQL tracks active locks in the `pg_locks` system catalog view. When a session is waiting for a lock, it has an entry in `pg_locks` with `granted = false`. The engine also provides the `pg_blocking_pids(pid)` function, which returns an array of PIDs that are blocking the session with the specified PID. This function is highly optimized as it interrogates the lock manager's internal dependency graphs directly.

We can run a query that:
1. Filters active sessions in `pg_stat_activity` that are blocked on a `Lock` event type.
2. Unnests the blocking PIDs using `pg_blocking_pids()`.
3. Joins the blocker's PID back to `pg_stat_activity` to retrieve the blocker's active query.
4. Uses regular expressions to extract the traceparent comments from both the waiter and blocker query strings.

```sql
-- // snippet-3
SELECT
    -- Waiter details
    waiter.pid AS waiter_pid,
    waiter.usename AS waiter_user,
    waiter.application_name AS waiter_app,
    clock_timestamp() - waiter.query_start AS wait_duration,
    waiter.query AS waiter_query,
    substring(waiter.query FROM '/\*traceparent=([0-9a-f\-]{55})\*/') AS waiter_traceparent,
    
    -- Blocker details
    blocker.pid AS blocker_pid,
    blocker.usename AS blocker_user,
    blocker.application_name AS blocker_app,
    blocker.query AS blocker_query,
    substring(blocker.query FROM '/\*traceparent=([0-9a-f\-]{55})\*/') AS blocker_traceparent,
    
    -- Catalog metadata
    lock.locktype AS lock_type,
    lock.mode AS lock_mode,
    coalesce(relation.relname, 'unknown') AS locked_relation
FROM
    pg_stat_activity waiter
CROSS JOIN LATERAL
    unnest(pg_blocking_pids(waiter.pid)) AS blocker_pid
INNER JOIN
    pg_stat_activity blocker ON blocker.pid = blocker_pid
LEFT JOIN
    pg_locks lock ON lock.pid = waiter.pid AND NOT lock.granted
LEFT JOIN
    pg_class relation ON lock.relation = relation.oid
WHERE
    waiter.wait_event_type = 'Lock'
    AND waiter.state = 'active';
```

There is an important production detail to address here: **idle-in-transaction sessions**. If a blocker transaction has completed its database query but remains uncommitted (for instance, because the application is waiting on a slow downstream API or network call), `pg_stat_activity.state` will show `idle in transaction`. 

In PostgreSQL, the `query` field in `pg_stat_activity` continues to display the *last statement executed* by that backend. Since all queries within a single transaction in a web request are typically executed under the same parent span, the traceparent comment in this last query text remains an accurate indicator of the transaction's trace ID. Even when the blocker process is idle, we can still extract its trace context to locate the transaction boundary held open by the application.

## Exporting DB Locks as Structured Events or Span Links in OpenTelemetry

To consume these lock contention correlations, we need a background daemon—a database sentinel—to poll the catalog query above and push the results to an OpenTelemetry collector. Polling intervals should strike a balance: too short (e.g., under 100ms) will create query overhead on the catalog tables, while too long (e.g., over 5s) will miss short-lived lock contention bursts. A 1-second interval is standard for production detection.

When lock contention is found, our daemon will create a new span representing the lock wait. However, rather than creating a disconnected trace, the daemon uses the extracted `waiter_traceparent` to assign the span's parent. The daemon then inserts an OpenTelemetry **Span Link** referencing the blocker's trace context. Span links are designed precisely for this: they define relationships between distinct, asynchronous traces (the waiter request and the blocker transaction) without forcing them into a single parent-child hierarchy.

<script src="https://gist.github.com/mohashari/3f01b86753a6d5cc2896ec1b5caafb3a.js?file=snippet-4.go"></script>

This sentinel program connects directly to the OpenTelemetry SDK collector. To process these spans downstream, we construct an OpenTelemetry Collector configuration. This config maps incoming spans, marks the traces as errors if lock contention is critical, and forwards them to Jager or Honeycomb for visualization.

```yaml
# snippet-5
receivers:
  otlp:
    protocols:
      grpc:
        endpoint: 0.0.0.0:4317
      http:
        endpoint: 0.0.0.0:4318

processors:
  batch:
    timeout: 1s
    send_batch_size: 1024

  # Tag trace status as Error if lock wait duration exceeds 2000ms
  transform:
    error_mode: ignore
    trace_statements:
      - context: span
        statements:
          - set(attributes["service.name"], "postgresql-sentinel")
          - set(attributes["db.system"], "postgresql")
          - set(attributes["otel.status_code"], 2) 
          - set(status.message, "Severe DB lock contention detected")
            where attributes["db.wait_duration_ms"] > 2000

exporters:
  otlp/honeycomb:
    endpoint: "api.honeycomb.io:443"
    headers:
      "x-honeycomb-team": "${HONEYCOMB_API_KEY}"
  otlp/jaeger:
    endpoint: "jaeger-collector:4317"
    tls:
      insecure: true

service:
  pipelines:
    traces:
      receivers: [otlp]
      processors: [transform, batch]
      exporters: [otlp/honeycomb, otlp/jaeger]
```

## Mitigating Lock Contention in Application Design

Correlating locks is critical for diagnosing incidents, but mitigating contention requires proper transaction discipline and defensive application coding. Lock contention spikes usually occur when transactions violate one of these core principles:
1. **Never perform external I/O inside a database transaction**: If your code contacts an external API (like a payment processor, email client, or partner service) while inside a database transaction, the transaction stays open for the duration of that network call. Any rows locked during that transaction remain locked, creating a queue of blocked threads.
2. **Apply strict timeouts**: Always apply a local `lock_timeout` and `statement_timeout` on database connections to prevent queries from waiting indefinitely.

In PostgreSQL, setting `lock_timeout` specifies the maximum time a statement will wait to acquire a lock. If the lock is not granted within this window, the statement aborts with error code `55P03` (`lock_not_available`). This prevents lock queues from cascading into connection pool exhaustion.

The following repository pattern demonstrates defensive locking using `SET LOCAL lock_timeout` and GORM-free standard query executors to handle contention gracefully.

<script src="https://gist.github.com/mohashari/3f01b86753a6d5cc2896ec1b5caafb3a.js?file=snippet-6.go"></script>

For workloads involving high-concurrency booking or queue operations, you can bypass waiting queues entirely using PostgreSQL's `NOWAIT` or `SKIP LOCKED` modifiers:
- `SELECT ... FOR UPDATE NOWAIT`: Aborts immediately if the row is already locked.
- `SELECT ... FOR UPDATE SKIP LOCKED`: Skips any locked rows and acts only on free ones. This is particularly useful for building lightweight, scalable worker pools that consume jobs out of database tables without colliding.

## Summary

Correlating database lock contention spikes with distributed traces represents a major shift in database observability. By injecting traceparents into query comments and parsing lock dependencies using `pg_blocking_pids`, you bridge the gap between application traces and database catalog tables. During incidents, instead of investigating slow queries in isolation, you can trace the blockage back to the exact request path holding the lock. Implement these techniques inside your driver layer, set strict connection-level timeouts, and protect your database from cascade degradation.