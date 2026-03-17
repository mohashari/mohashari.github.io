---
layout: post
title: "Redis Advanced Patterns: Streams, Pub/Sub, and Lua Scripting"
date: 2026-03-18 07:00:00 +0700
tags: [redis, caching, streaming, backend, performance]
description: "Go beyond key-value storage with Redis Streams for event logs, Pub/Sub for fan-out messaging, and atomic Lua scripts for complex operations."
---

Most engineers treat Redis as a fast key-value store — a place to cache database results, store session tokens, and call it a day. That mental model is leaving serious capability on the table. Redis ships with primitives powerful enough to replace entire categories of infrastructure: message queues, event logs, real-time fan-out pipelines, and transactional workflows. The three features that unlock this are Streams, Pub/Sub, and Lua scripting. Together, they let you build systems that are not only fast but architecturally simpler — fewer moving parts, fewer failure modes, and a single operational surface to monitor. This post digs into each one with production-oriented examples, showing when to reach for each and what pitfalls to avoid.

## Redis Streams: Durable Event Logs

Streams were introduced in Redis 5.0 and represent the most underused feature in the entire project. A Stream is an append-only log — conceptually similar to a Kafka topic, but embedded directly in your Redis instance. Each entry gets an auto-generated ID in the form `timestamp-sequence`, and consumers can read from any offset, replay history, and participate in consumer groups for parallel processing with at-least-once delivery semantics.

The key architectural insight is that Streams persist entries until you explicitly trim or delete them. Unlike Pub/Sub (which we'll cover next), a message published to a Stream exists whether or not anyone is listening. This makes Streams appropriate for audit logs, event sourcing, and async job queues where message durability matters.

Here's how to append events and read them with a consumer group in Go:

<script src="https://gist.github.com/mohashari/d301f13151b59862114d0866a7ae8b22.js?file=snippet.go"></script>

The `>` special ID means "give me only new messages not yet delivered to this consumer group." After processing, `XACK` removes the message from the pending entries list (PEL). If a worker crashes before acknowledging, you can use `XPENDING` and `XCLAIM` to reclaim stale messages — a complete at-least-once delivery mechanism built entirely in Redis.

To prevent unbounded stream growth, cap it with `MAXLEN`:

<script src="https://gist.github.com/mohashari/d301f13151b59862114d0866a7ae8b22.js?file=snippet-2.sh"></script>

## Pub/Sub: Fan-Out Messaging Without Persistence

Pub/Sub is the right tool when you need real-time fan-out to many subscribers and message durability is explicitly *not* required. Think: broadcasting configuration changes to a fleet of workers, pushing live score updates to WebSocket handlers, or invalidating cache entries across multiple application nodes.

The critical difference from Streams: if no subscriber is listening when a message is published, it is gone. This fire-and-forget model is a feature, not a bug — it keeps memory usage predictable and latency deterministic.

Pattern-based subscriptions using `PSUBSCRIBE` let you subscribe to channels matching a glob pattern:

<script src="https://gist.github.com/mohashari/d301f13151b59862114d0866a7ae8b22.js?file=snippet-3.go"></script>

A common production mistake is running Pub/Sub over the same connection pool used for regular commands. Redis connections in `SUBSCRIBE` state can only receive messages — they cannot execute other commands. Always maintain a dedicated connection (or connection pool) for subscriptions.

## Lua Scripting: Atomic Complex Operations

Redis executes Lua scripts atomically. No other command can execute between the first and last line of your script — the entire cluster effectively pauses for the duration of the script. This is how you implement operations that require read-modify-write semantics without races, without distributed locks, and without the overhead of MULTI/EXEC transactions.

A practical example: rate limiting. The naive approach reads a counter, checks it, then increments it — three round trips with a race condition between steps one and two. The Lua approach collapses this into a single atomic operation:

<script src="https://gist.github.com/mohashari/d301f13151b59862114d0866a7ae8b22.js?file=snippet-4.txt"></script>

Loading and calling this script from Go using `EVALSHA` (which caches the script by its SHA1 hash, avoiding re-sending the script body on every call):

<script src="https://gist.github.com/mohashari/d301f13151b59862114d0866a7ae8b22.js?file=snippet-5.go"></script>

`redis.NewScript` in the Go client automatically handles `EVALSHA` with a fallback to `EVAL` on cache miss. The important constraint: all keys your script accesses must be declared in `KEYS[]`. This is enforced in cluster mode — Redis needs to know which slot your script touches so it can route it to the correct node.

## Combining Patterns: Idempotent Job Processing

The real power emerges when you compose these primitives. Here's a pattern for idempotent job deduplication using a Lua script alongside Streams:

<script src="https://gist.github.com/mohashari/d301f13151b59862114d0866a7ae8b22.js?file=snippet-6.go"></script>

This script atomically checks a seen-set, adds the job ID if new, and appends to the stream — all in one round trip, with no possibility of a duplicate entry racing in between.

## Operational Considerations

Memory and persistence configuration matter enormously when moving beyond pure caching. Streams and the Pub/Sub subscriber state consume memory proportional to throughput. For production deployments, run Redis with an explicit `maxmemory` policy and monitor `XLEN` on active streams. For Streams backing durable workloads, enable AOF persistence (`appendonly yes` in redis.conf) — RDB snapshots alone are too coarse for event logs.

<script src="https://gist.github.com/mohashari/d301f13151b59862114d0866a7ae8b22.js?file=snippet-7.sh"></script>

Redis Streams, Pub/Sub, and Lua scripting are not exotic features reserved for Redis power users — they are production-grade primitives that solve real infrastructure problems. Streams give you a durable, replayable event log without Kafka's operational overhead. Pub/Sub gives you sub-millisecond fan-out when durability is not the constraint. Lua scripting gives you atomic multi-step operations without the complexity of distributed locking. The engineers who get the most out of Redis are the ones who learn which tool fits which problem — and have the pattern library to reach for the right one without hesitation.