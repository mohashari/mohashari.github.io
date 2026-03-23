---
layout: post
title: "gRPC Bidirectional Streaming: Backpressure, Flow Control, and Production Patterns"
date: 2026-03-23 08:00:00 +0700
tags: [grpc, go, distributed-systems, backend, performance]
description: "How HTTP/2 flow control works in gRPC bidi streams, why slow consumers will OOM your server, and the patterns that actually hold up in production."
image: ""
thumbnail: ""
---

You deploy a gRPC bidirectional streaming service for real-time event delivery. Load testing looks clean. Then production traffic arrives, a handful of mobile clients on flaky 4G connections slow down, and within 20 minutes your server process is consuming 14 GB of heap and climbing. No alerts fire because throughput looks fine — messages are queuing, not dropping. This is the backpressure problem, and gRPC's default behavior makes it easy to stumble into.

This post is about what actually happens inside a bidi stream, how HTTP/2 flow control propagates backpressure (and when it fails to), and the patterns that keep streaming services stable under real-world consumer behavior.

## How HTTP/2 Flow Control Actually Works

gRPC runs on HTTP/2, and HTTP/2 has a built-in flow control mechanism based on WINDOW_UPDATE frames. Every stream starts with a receive window of 65,535 bytes (the spec default). The sender may not transmit more bytes than the current window allows. When the receiver consumes data from its buffer, it sends a WINDOW_UPDATE frame to credit the sender more bytes.

gRPC Go exposes two separate knobs:

- **Stream-level window**: per individual RPC stream, default 64 KB
- **Connection-level window**: across all streams on a connection, default 64 KB

These defaults were designed for request/response patterns. For high-throughput streaming, they are nearly always wrong.

<script src="https://gist.github.com/mohashari/62f2c88290e5438cf9be1a2de07902af.js?file=snippet-1.go"></script>

When a slow consumer stops ACKing WINDOW_UPDATE frames, the sender's window drains to zero and `stream.Send()` blocks. This is correct behavior — this is backpressure working as intended. The problem is what your server code does while blocked.

## The Goroutine Leak Pattern

The naive streaming server looks like this: one goroutine reads from a database or message queue, calls `stream.Send()` in a loop, and another goroutine (or select case) handles incoming client messages. When the client slows down and `Send()` blocks indefinitely, that goroutine is stuck. If you have 10,000 connected clients and 500 of them slow down, you have 500 goroutines holding queue consumers, database connections, or in-memory buffers open.

<script src="https://gist.github.com/mohashari/62f2c88290e5438cf9be1a2de07902af.js?file=snippet-2.go"></script>

The `stream.Context().Done()` case only fires when the client explicitly cancels or the connection drops. A live-but-slow client keeps the context open. You need a send timeout.

<script src="https://gist.github.com/mohashari/62f2c88290e5438cf9be1a2de07902af.js?file=snippet-3.go"></script>

This is still not ideal — the goroutine spawned for `stream.Send()` cannot be cancelled once started, and if it's blocked on HTTP/2 flow control, it holds until the connection drops. The real fix is to set a deadline on the underlying connection, not spawn goroutines.

## gRPC Send Timeout via Context Wrapping

gRPC Go doesn't expose a per-send deadline. The correct lever is to keep your stream context tightly scoped and use an application-level send buffer with a capacity limit.

<script src="https://gist.github.com/mohashari/62f2c88290e5438cf9be1a2de07902af.js?file=snippet-4.go"></script>

The bounded channel at 256 messages is the explicit backpressure signal. When it fills, you're dropping events rather than accumulating them in memory. Whether dropping is acceptable depends on your semantics — for financial events you'd want to signal the client to slow its request rate instead. For metrics or log tailing, dropping is usually fine.

## Bidirectional Flow: Handling Client Messages

Bidi streaming means the client also sends messages on the same stream. `stream.Recv()` is synchronous and blocks waiting for the next client message. You cannot call both `Send()` and `Recv()` from the same goroutine. The canonical pattern is to wrap `Recv()` in a goroutine and send results on a channel:

<script src="https://gist.github.com/mohashari/62f2c88290e5438cf9be1a2de07902af.js?file=snippet-5.go"></script>

One important detail: `stream.Recv()` returns `io.EOF` when the client has closed its send side (half-close). This is normal and does not mean the stream is done — the server should continue sending until it's finished. Only when the server also calls `return nil` (or returns an error) is the full stream closed.

## Production Failure Modes

**Memory explosion from unbounded internal buffers**: Some message broker clients buffer unconsumed messages in memory. If your broker subscriber has no max-inflight limit and your `stream.Send()` is blocked, the broker buffer grows without bound. Always configure max-inflight or prefetch limits on your broker client (e.g., `nats.MaxInflight(500)` or Kafka consumer `max.poll.records`).

**Head-of-line blocking across streams**: HTTP/2 multiplexes streams over a single TCP connection. If one stream has a large message that fills the connection-level window, all other streams on that connection stall. Increasing `InitialConnWindowSize` mitigates this, but the real fix for high fan-out scenarios is to use one connection per client rather than connection pooling. In gRPC Go, `grpc.WithDisableServiceConfig()` and a custom balancer can force this.

**Goroutine stacks under load**: Each blocked goroutine consumes at least 2-8 KB of stack (growing to megabytes under recursion). At 50,000 concurrent streams with sender goroutines, you're looking at 100-400 MB just for stacks. Profile goroutine count with `runtime.NumGoroutine()` and expose it as a metric.

**Recv buffer starvation**: If the server sends faster than it reads client messages, the client's send window drains to zero and the client blocks. This is symmetric — both sides can exert backpressure. If client messages carry acknowledgments or rate hints, failing to read them promptly means you're flying blind on consumer health.

## Observability for Streaming Services

Standard gRPC interceptor metrics don't tell you much about long-lived streams. You need custom per-stream metrics:

<script src="https://gist.github.com/mohashari/62f2c88290e5438cf9be1a2de07902af.js?file=snippet-6.go"></script>

Alert on `send_duration_seconds p99 > 500ms` — that's your slow consumer signal before memory pressure appears. Alert on `dropped_events_total rate > 0` as a warning, not a page, since some dropping is expected. Page on `active_streams` growing monotonically (goroutine leak indicator).

## Graceful Shutdown

Shutting down a server with active bidi streams needs care. `grpc.Server.GracefulStop()` stops accepting new connections and RPCs, then waits for existing RPCs to complete. For long-lived streams that never complete on their own, this blocks indefinitely.

```bash
# snippet-7
# Check active streams before shutdown decision in a Kubernetes preStop hook
# This gives existing streams up to 30s to drain before SIGKILL

lifecycle:
  preStop:
    exec:
      command:
        - /bin/sh
        - -c
        - |
          # Signal app to stop accepting new streams
          kill -USR1 1
          # Wait for active stream count to drop or timeout
          deadline=30
          elapsed=0
          while [ $elapsed -lt $deadline ]; do
            count=$(curl -sf http://localhost:9090/metrics | grep 'grpc_bidi_active_streams' | awk '{print $2}')
            [ "$count" = "0" ] && exit 0
            sleep 1
            elapsed=$((elapsed + 1))
          done
          exit 0
```

In the Go server, handle `SIGUSR1` to close your broker subscriptions and stop feeding the send buffers. Streams will drain their internal buffers and close cleanly within your message timeout window.

## The Mental Model That Prevents Production Incidents

Think of a bidi stream as two independent pipes with flow-controlled capacity. Each pipe has a window. When a pipe's window is exhausted, the writer blocks — at the HTTP/2 layer, not in your application code. Your application code blocks inside `Send()` or `Recv()`. Every resource held while blocked (goroutines, broker connections, memory) accumulates proportionally to how many slow consumers you have.

The invariants to enforce are:
1. `Send()` must never block longer than your application-level message timeout
2. Your send buffer must have a hard cap — drop or error, never grow unbounded  
3. Every stream must have exactly one goroutine owning `Send()` and one owning `Recv()`
4. Stream count must be a visible metric with alerting

These aren't exotic requirements. They're the same invariants you'd apply to any producer-consumer system. gRPC makes them easy to miss because the blocking happens inside an opaque `Send()` call, not in obvious queue code. Once you internalize that `Send()` is `queue.Put()` with implicit backpressure, the patterns fall into place.
```