---
layout: post
title: "Tracing Async Kafka Consumer Group Rebalance Latencies via OpenTelemetry and eBPF"
date: 2026-08-22 08:00:00 +0700
tags: [kafka, opentelemetry, ebpf, observability]
description: "Diagnose silent Kafka consumer group rebalance storms and async worker queue stalls by combining kernel-level eBPF socket tracing with OpenTelemetry."
image: "https://picsum.photos/seed/5850/1080/720"
thumbnail: "https://picsum.photos/seed/5850/400/300"
---

It’s 3:00 AM. Your high-throughput payment processing pipeline begins to experience a consumer lag spike on your Kafka clusters. CPU utilization is flat, network throughput is normal, and your standard application performance monitoring (APM) dashboard shows a complete void of spans—no errors, just silence. The logs reveal the culprit: your consumer group has entered a continuous loop of rebalancing, a state known as a "rebalance storm." Because you run an asynchronous consumer pattern—polling on a main thread and delegating processing to a worker pool—classic tracing tools are blind. They cannot correlate the long-tail latency of your worker queue drain with the kernel-level socket waits of the `JoinGroup` and `SyncGroup` protocols. To diagnose this, we must cross the boundary between user-space execution and kernel-level network tracing by combining OpenTelemetry with eBPF.

## The Async Kafka Rebalance Black Box

In high-performance backend systems, synchronous Kafka message consumption is a bottleneck. To maximize throughput, engineers implement an asynchronous worker pattern: a single thread manages the Kafka poll loop (`poll()`), placing records into an internal memory queue, while a pool of worker goroutines or threads processes those records concurrently. 

Under normal operations, this works brilliantly. However, during a consumer group rebalance, this separation of concerns introduces a critical observability gap. The Apache Kafka protocol coordinates group membership through three primary phases:

1. **`JoinGroup`**: Consumers announce their presence and submit protocol metadata to the Group Coordinator broker.
2. **`SyncGroup`**: The group leader assigns partition layouts, and all group members retrieve their assignments.
3. **`Heartbeat`**: Consumers send periodic background pings to confirm they are alive.

Before a consumer can safely send a `JoinGroup` request, it must revoke its current partitions. In an asynchronous consumer model, partition revocation is not instantaneous. To maintain processing guarantees (such as at-least-once delivery), the consumer must freeze ingestion and block the revocation callback (`onPartitionsRevoked`) until the worker pool has finished processing and committing all in-flight messages for the affected partitions. 

This is the "drain phase." If a downstream database query slows down or a thread pool becomes saturated, the drain duration can exceed the Kafka client's configured `max.poll.interval.ms`. When this happens, the coordinator broker assumes the consumer is dead, kicks it out of the group, and triggers another rebalance. This creates a destructive loop—a rebalance storm. 

Standard OpenTelemetry auto-instrumentation only captures the boundary of message execution spans or the outer `poll()` loop. It cannot see the silent queue drain times, nor can it inspect the internal network handshake of the Kafka client library, which is often implemented in compiled C wrappers like `librdkafka`. To gain visibility, we must instrument both the application lifecycle and the kernel socket state.

## OpenTelemetry: Instrumenting the Rebalance Event Lifecycle

To diagnose user-space queue stalls during rebalances, we must implement custom OpenTelemetry spans around our partition revocation and assignment callbacks. These callbacks block the main consumer loop, making them the ideal place to measure drain latencies.

The following Go code snippet demonstrates how to instrument an asynchronous worker pool drain during partition revocation using the OpenTelemetry API.

<script src="https://gist.github.com/mohashari/cde2ca858156ac08254ae236d7f774a0.js?file=snippet-1.go"></script>

This code forces the consumer thread to wait until the worker queue is empty, recording the entire duration within the `kafka.rebalance.revoked` span. If a rebalance takes 20 seconds because a worker is hung, this span will clearly capture that delay.

## eBPF: Profiling the Kernel-Level Network and Thread State

While the application-level tracing tells us how long the queue drain took, it cannot measure network-level round-trip times to the Kafka broker for protocol handshakes. This is particularly problematic when the application uses a C-shared library like `librdkafka`, which spawns its own background OS threads to manage group state machine transitions and heartbeats.

Kafka's protocol uses a binary protocol format over TCP. Each request packet starts with a 4-byte size followed by an API Key (2 bytes), an API Version (2 bytes), and a Correlation ID (4 bytes).
The primary API Keys we care about are:
* **11**: `JoinGroup`
* **12**: `Heartbeat`
* **14**: `SyncGroup`

We can write an eBPF program utilizing BPF CO-RE (Compile Once – Run Everywhere) that hooks the `sys_enter_write` and `sys_exit_read` tracepoints. This allows us to intercept outbound TCP writes targeting our Kafka broker's port, parse the protocol header to identify the API Key and Correlation ID, and record the request start time. When a response with the matching Correlation ID is read, we compute the elapsed time.

<script src="https://gist.github.com/mohashari/cde2ca858156ac08254ae236d7f774a0.js?file=snippet-2.txt"></script>

## Correlating Kernel Spans with OTel Traces

The biggest hurdle with eBPF is correlation. How do we link the kernel event back to the application trace? Because the Kafka protocol headers are fixed, we cannot inject trace context headers into `JoinGroup` and `SyncGroup` requests without crashing the Kafka broker.

We solve this using a shared BPF map (`thread_traces`) as a bridge.
1. **Thread Pinning**: When Go multiplexes goroutines onto OS threads, the thread executing a goroutine can change across yield points. To map a trace ID to a thread ID (TID) reliably, we must pin the executing goroutine to its current OS thread using `runtime.LockOSThread()`.
2. **Context Publishing**: We write the active OTel `TraceID` and `SpanID` directly to the `thread_traces` map.
3. **Trace Retrieval**: When the eBPF tracepoint fires, it looks up the current TID in the map. If it matches, the trace context is embedded in the event sent to the user-space daemon.

Here is the implementation of the Go helper function that updates the BPF map:

<script src="https://gist.github.com/mohashari/cde2ca858156ac08254ae236d7f774a0.js?file=snippet-3.go"></script>

Now, we need a user-space daemon to poll the eBPF ring buffer and publish these kernel-level events to our OpenTelemetry Collector as spans, linking them to the parent trace.

<script src="https://gist.github.com/mohashari/cde2ca858156ac08254ae236d7f774a0.js?file=snippet-4.go"></script>

## Tail Sampling: Preventing Trace Bloat

In a production environment, tracking every Single `Heartbeat` socket write will drown your storage in low-value traces. To manage cost and performance, we implement a **tail-based sampling** policy on our OpenTelemetry Collector. Instead of making sampling decisions when a trace begins, the collector buffers spans and makes decisions based on the completed trace's attributes.

We set up a tail-sampling processor that evaluates traces after a 10-second buffer window. We sample all traces containing errors or any trace where our custom `kafka.latency_ms` attribute exceeds 5,000 milliseconds (5 seconds).

<script src="https://gist.github.com/mohashari/cde2ca858156ac08254ae236d7f774a0.js?file=snippet-5.yaml"></script>

## A Production Case Study: The 45-Second Rebalance Mystery

Let's look at how this telemetry setup diagnosed a critical failure on a core payment service.

### The Symptom
The payment processing consumer group experienced sudden throughput drops every 10–15 minutes, with consumer lag skyrocketing on critical partitions. Logs reported that the consumer was being repeatedly kicked from the group for failing to respond to heartbeats. The team initially suspected network congestion between the consumer containers and the Kafka brokers.

### The Analysis
Using the combined OTel and eBPF tracing data, we inspected the trace dashboard for the affected period. We located a trace flagged by the tail sampler:

```
[Trace: 7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d]
└── kafka.rebalance.revoked (Duration: 42.1s) [Status: Error]
    ├── kafka.worker_pool.drain (Duration: 42.0s) [Status: Error]
    │   ├── waiting_for_drain (Event: remaining_messages=452)
    │   ├── waiting_for_drain (Event: remaining_messages=210)
    │   └── timeout (Event: "drain timeout exceeded")
    └── kafka.socket.JoinGroup (Duration: 18ms) [Trace Link: kernel socket trace]
```

Looking at the spans, the network round-trip time (`kafka.socket.JoinGroup`) was only 18 milliseconds, which ruled out the network congestion hypothesis. 

Instead, the bottleneck was the worker pool drain: it took 42 seconds to clear the worker queue. Because `max.poll.interval.ms` was set to 30 seconds, the consumer group coordinator broker assumed the client had hung and evicted it midway through the drain. 

We clicked down into the concurrent worker spans running during the drain and found that they were all blocked waiting for a connection pool from our database:

```
└── database.query: SELECT * FROM accounts WHERE id = ?
    └── db.connection_wait (Duration: 8.5s)
```

### The Fix
The eBPF trace proved that the network connection to Kafka was healthy. The root cause was database connection pool starvation under load, which delayed worker thread execution and delayed partition revocation. 

We resolved the issue in production by:
1. Increasing the database connection pool size and adding strict query execution timeouts.
2. Increasing `max.poll.interval.ms` to 90 seconds to provide a wider safety margin during high-traffic queue drains.
3. Migrating our consumer group from the `RangeAssignor` to the `CooperativeStickyAssignor`, reducing the partition count revoked during routine deployments.

## Summary

Observing asynchronous systems requires measuring the state transitions between scheduling queues, application threads, and kernel-space networks. By combining OpenTelemetry spans inside your partition lifecycle callbacks with eBPF socket tracing at the network syscall boundary, you can systematically locate performance issues:

* **Short worker drain times + long `SyncGroup`/`JoinGroup` spans** mean it is time to optimize Kafka broker configurations, check coordinator node health, or resolve broker network bottlenecks.
* **Long worker drain times + short network spans** mean your application code, database performance, or worker pool queue sizing is the bottleneck.