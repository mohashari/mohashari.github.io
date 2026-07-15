---
layout: post
title: "Tracing Asynchronous Database Transactions Across Thread Pools with OpenTelemetry and Java Agent"
date: 2026-07-15 08:00:00 +0700
tags: [opentelemetry, java, tracing, database]
description: "Solve broken distributed traces and missing database transaction contexts across asynchronous thread pools using OpenTelemetry and Java Agent."
image: "https://picsum.photos/seed/4305/1080/720"
thumbnail: "https://picsum.photos/seed/4305/400/300"
---

Imagine this: Your system's p99 latency spikes from 45ms to 8,200ms during a flash sale. Database connections are saturated, and the thread pool queue is overflowing. You open your APM dashboard in Grafana Tempo or Datadog to diagnose the issue, only to find that the slowest HTTP spans report a sub-millisecond execution time, showing absolutely no database calls. Meanwhile, your query trace logs show thousands of orphaned, unconnected SQL execution spans with no parent context. The critical link between the incoming HTTP request and the asynchronous database worker thread is broken. In high-throughput, multi-threaded Java applications, tracing transactions across thread pool boundaries is a notorious blind spot. Standard auto-instrumentation often fails when queries are dispatched to custom executor pools, reactive pipelines, or non-blocking event loops, leaving you blind during critical production outages.

## The Root Cause: ThreadLocals and Context Propagation

OpenTelemetry (and almost all Java-based APM tracing libraries) relies on `ThreadLocal` variables to store the current span and trace context. In a synchronous, thread-per-request model—such as a classic Spring MVC application running on Apache Tomcat—this works seamlessly. The HTTP thread receives the request, writes the `TraceID` and `SpanID` to its `ThreadLocal` context, executes the database query on the same thread, and reads the context to link the database span to the HTTP span.

However, in modern enterprise architectures, blocking database operations are isolated to prevent thread starvation on the Web server. We offload heavy SQL queries, reporting generation, or asynchronous writes to dedicated thread pools (`ThreadPoolExecutor` or `ForkJoinPool`). The moment a task is submitted to an `ExecutorService`, the execution context switches threads. The worker thread in the thread pool has its own clean `ThreadLocal` map. As a result, the SQL query runs either without any trace context (creating orphaned traces) or, even worse, inherits stale context from a recycled thread that was previously processing a completely different request. This context leak corrupts telemetry data, associating one user's database transactions with another's request trace—a compliance and debugging nightmare.

To bridge this gap, the active telemetry context must be captured from the parent thread, transmitted across the thread boundary, and set as the active context on the worker thread for the duration of the task's execution.

<script src="https://gist.github.com/mohashari/031b17bb0536e752ea4ebd89a3ed4fda.js?file=snippet-1.txt"></script>

In snippet-1, `Context.current().wrap(Runnable)` acts as the bridge. Under the hood, this method creates a wrapper runnable that captures the context at the time of wrapping. When the task is run on the worker thread, the wrapper makes the captured context current, executes the original task, and ensures that the context is cleaned up immediately after execution. The `try-with-resources` block is critical here; failing to close a `Scope` can lead to thread contamination, where subsequent executions on that recycled thread inherit stale trace headers.

## Automating Propagation: Java Agent and ByteBuddy Transformations

While manual wrapping works, relying on developers to manually wrap every asynchronous task in a large codebase is a recipe for broken traces. The OpenTelemetry Java Agent (`opentelemetry-javaagent.jar`) automates this context propagation by dynamically altering the bytecode of target classes during JVM startup.

The agent uses ByteBuddy to intercept constructors and task submission methods of standard JDK executors (`java.util.concurrent.ExecutorService`, `ThreadPoolExecutor`, `ForkJoinPool`, and Scheduled Executors). When a task is submitted, the agent injects a private field containing the current active context into the runnable or callable object. When the worker thread executes the task, the agent interceptor reads the context from the task object, sets it as the active context on the worker thread, runs the task, and restores the worker thread's original context when the task completes.

However, standard auto-instrumentation has limitations. If your architecture uses custom schedulers, wrappers around executors, or hand-rolled thread queues, the agent may fail to recognize them. You must configure the agent to target these custom classes explicitly.

<script src="https://gist.github.com/mohashari/031b17bb0536e752ea4ebd89a3ed4fda.js?file=snippet-2.txt"></script>

In snippet-2, the configuration setting `otel.instrumentation.executors.include` is essential. If your codebase wraps standard executors in custom classes to manage priorities or handle resource isolation, you must declare their fully qualified class names here. Without this, the agent will skip bytecode transformation for those classes, and trace contexts will be dropped at the thread boundary.

## Eliminating the Database Connection Pool Gap with HikariCP

Database queries do not execute in isolation; they rely on connection pools like HikariCP to manage connections efficiently. This introduces another asynchronous barrier: connection acquisition.

When a query is offloaded to a worker thread, it must first check out a connection from HikariCP. If the pool is saturated, the worker thread will block. This block time must be tracked. If your database spans are slow, you need to know if the database itself is bottlenecked, or if your application threads are waiting 2,000ms just to acquire a connection from the pool.

To trace connection pool dynamics, you should programmatically wrap your `DataSource` with the OpenTelemetry wrapper class `OpenTelemetryDataSource`.

<script src="https://gist.github.com/mohashari/031b17bb0536e752ea4ebd89a3ed4fda.js?file=snippet-3.txt"></script>

By wrapping the data source in snippet-3, every connection checkout attempt becomes an instrumented operation. OpenTelemetry generates a span for the connection checkout, recording how long the thread waited for a connection. This is vital when troubleshooting connection leaks or sizing your connection pool relative to your thread pools.

## The Reactive and Non-Blocking Boundary: Project Reactor & R2DBC

When building reactive applications (e.g., using Spring WebFlux or Project Reactor with R2DBC), threads are completely decoupled from execution stages. A single pipeline might execute its initial step on a Netty event loop, map data on a parallel scheduler thread, and execute database operations on an R2DBC driver pool.

Because the execution context switches threads continuously during pipeline execution, `ThreadLocal` storage is ineffective. Project Reactor solves this by using its own internal `Context` which is propagated along the stream's subscriber chain.

The OpenTelemetry Java Agent instruments Reactor out-of-the-box, but when you combine reactive pipelines with legacy blocking JDBC drivers or custom multi-threaded handlers, the trace context can easily get lost. In these cases, you must manually bridge the OpenTelemetry trace context and Reactor's reactive context.

<script src="https://gist.github.com/mohashari/031b17bb0536e752ea4ebd89a3ed4fda.js?file=snippet-4.txt"></script>

Snippet-4 provides helper methods to bind OpenTelemetry's trace context to Reactor's context. When you transition from a reactive flow to a blocking executor pool, calling `withTraceContext` ensures that the active trace context is carried along and made current on the worker thread when the data signals are processed.

## Building a Resilient, Propagating Thread Pool Wrapper

If you are running in environments where Java Agents are not viable—such as GraalVM Native Image builds, serverless containers (e.g., AWS Lambda) where startup latency is critical, or restricted cloud environments—you must implement context propagation programmatically at the application level.

A clean design pattern is to create a custom propagating executor wrapper that implements the standard `Executor` interface, ensuring that context propagation is handled transparently whenever tasks are executed.

<script src="https://gist.github.com/mohashari/031b17bb0536e752ea4ebd89a3ed4fda.js?file=snippet-5.txt"></script>

In snippet-5, `ContextPropagatingExecutor` captures the context from the submitting thread. When the delegate executor assigns a worker thread to execute the command, the wrapper sets the captured context as the active thread context, runs the command, and automatically clears it when finished. Using `Context.taskWrapping` provides a similar wrapping mechanism for standard Java `ExecutorService` instances, guaranteeing context flow without manual task wrapping.

## Advanced Manual Instrumentation: Fine-Grained Transaction Tracking

Automatic instrumentation is highly effective, but it is often limited to tracking SQL query execution time. In critical, low-latency financial or transactional microservices, you need deeper insights. You need to distinguish between connection acquisition time, transaction preparation, actual table updates, and commit/rollback operations.

By writing custom spans with specific OpenTelemetry semantic conventions, you can trace the inner workings of database transactions, exposing exactly where bottlenecks occur.

<script src="https://gist.github.com/mohashari/031b17bb0536e752ea4ebd89a3ed4fda.js?file=snippet-6.txt"></script>

Snippet-6 implements granular transaction tracing. It creates child spans for connection acquisition and SQL query execution, mapping JDBC errors directly to span exceptions. The spans utilize the standard OpenTelemetry database semantic conventions (e.g., `db.system`, `db.statement`). APM platforms parse these attributes to compute statistics on query performance and show complete visual breakdowns of transaction execution paths.

> [!IMPORTANT]
> Never log raw parameter values inside the `db.statement` attribute. Logging sensitive parameters like user passwords, credit card numbers, or transaction values violates data compliance regulations (e.g., PCI-DSS, GDPR). Always write the parameterized query template to the span attributes instead.

## Troubleshooting & Validating Context Propagation in Production

Once you configure your instrumentation and context wrappers, you must validate that trace context propagates correctly under load. Here are four strategies to debug and confirm trace continuity:

### 1. Enable Agent Transformation Logs
If you suspect that the OpenTelemetry Java Agent is ignoring your custom thread pools, enable verbose class-loading logs by passing `-Dotel.javaagent.debug=true` during JVM startup. 

Search the logs for transformations targeting your package name:
`[otel.javaagent] Transformed com.example.observability.CustomThreadPoolExecutor`

If you do not see this output, the agent is not transforming your class. Check for package name typos or ensure that the executor class is loaded by a class loader reachable by the Java Agent.

### 2. Log Correlation with MDC (Mapped Diagnostic Context)
A direct way to verify trace propagation is to cross-reference log files. Ensure that your Logging framework (Logback or Log4j2) extracts OpenTelemetry trace context. 

If a log statement executed inside a database worker thread shows empty trace parameters:
`[db-worker-pool-2] INFO  c.e.o.DbService - Processing query - trace_id= span_id=`

but the web thread that received the request logged:
`[tomcat-handler-3] INFO  c.e.o.HttpController - Request received - trace_id=a8f9c1d2e3f4g5h6 span_id=987654321`

then context propagation is failing at the thread pool boundary. Verify that your executor service is wrapped or declared in the agent configurations.

### 3. Check for ThreadLocal Memory Leaks
Improperly closed scopes are a common cause of memory leaks and corrupted telemetry. If the agent or custom code fails to call `Scope.close()` when tasks complete, contexts accumulate. In high-throughput environments, this causes memory consumption to rise and can result in random garbage collection pauses.

Monitor your JVM heap using tools like JDK Flight Recorder (JFR) or VisualVM. Look for high allocation rates of `io.opentelemetry.context.ArrayBackedContext` or `ThreadLocalMap$Entry` references. This indicates that scopes are not being closed in `finally` blocks, leaving stale context on recycled threads.

### 4. Manage Sampling Rates Under Heavy Load
Propagating trace context across thread pools creates transient wrapper objects, which can increase memory allocation rates. In low-latency, high-throughput applications processing more than 10,000 requests/second, tracing every transaction can degrade performance.

Configure dynamic or tail-based sampling using the OpenTelemetry Collector, or set the application-level sampler:
`otel.traces.sampler=parentbased_always_on`

This configuration ensures that trace details are captured for transactions initiated by web requests while minimizing overhead for background tasks that do not require tracking.

## Summary of Best Practices for Production

To maintain tracing visibility across thread boundaries in production Java deployments:

1. **Name your Thread Pools:** Use custom `ThreadFactory` implementations to assign descriptive names to your threads, making them easier to identify in your logs and traces.
2. **Explicitly Register Custom Executors:** Ensure that all custom executor classes are declared in the agent's configuration properties under `otel.instrumentation.executors.include`.
3. **Wrap DataSources:** Programmatically wrap your connection pools with `OpenTelemetryDataSource` to track connection checkout times and identify database resource constraints.
4. **Use Safe Scope Patterns:** Always manage scopes with try-with-resources or try-finally blocks to avoid context leakage and prevent trace corruption.
5. **Monitor Telemetry Overhead:** Profile allocations under high-throughput conditions and tune sampling rates to balance tracing visibility with JVM garbage collection overhead.

By addressing the asynchronous trace gap, you gain end-to-end visibility into your database operations, allowing you to trace requests as they cross thread pool boundaries and resolve latency issues quickly.