---
layout: post
title: "Cache Invalidation Strategies: Solving the Hardest Problem in Computer Science"
date: 2026-03-15 07:00:00 +0700
tags: [caching, redis, backend, architecture, performance]
description: "Compare write-through, write-behind, cache-aside, and event-driven invalidation patterns to keep caches consistent at scale."
---

There's an old joke in computer science: the two hardest problems are cache invalidation, naming things, and off-by-one errors. The joke is tired, but the problem isn't. Every distributed system eventually runs into a moment where a user sees stale data — a product price that was updated three minutes ago, a profile picture that still shows the old one, a permissions change that hasn't propagated yet. These aren't just embarrassing bugs; in financial or security-sensitive systems, they're liabilities. Cache invalidation is hard not because the individual patterns are complex, but because each one makes a different trade-off between consistency, latency, throughput, and operational complexity. Understanding when to use which strategy — and why — separates engineers who bolt Redis onto a system from engineers who design systems that stay correct under pressure.

## The Problem Space

Before comparing strategies, it's worth being precise about what we're solving. A cache becomes invalid when the underlying data changes and the cache doesn't reflect that change. The window between the change and the cache update is called **staleness**. Every invalidation strategy is an attempt to minimize staleness while avoiding the three failure modes: thundering herd (too many cache misses at once), write amplification (too many synchronous writes), and phantom reads (serving data that was already deleted).

The four patterns we'll cover — cache-aside, write-through, write-behind (write-back), and event-driven invalidation — each draw a different line across these trade-offs.

## Cache-Aside (Lazy Loading)

Cache-aside is the most common pattern because it's the most intuitive. The application checks the cache first, and on a miss, loads from the database, populates the cache, and returns the result. The cache is never written to directly on updates — instead, the application invalidates (deletes) the cache entry when the underlying data changes.

This function shows a typical implementation in Go using a read-through-with-delete approach. The key insight is that on update, we delete rather than update the cache entry, letting the next read lazily repopulate it.

<script src="https://gist.github.com/mohashari/8a60b6ef83e35f8c4e63497a823c64f9.js?file=snippet.go"></script>

The delete-on-write approach avoids a race condition where a stale value is written to the cache after a newer one. The downside is that the first request after invalidation always pays the full database latency — the **cold start penalty**.

## Write-Through

Write-through eliminates the cold start penalty by updating the cache synchronously on every write. The application writes to both the cache and the database in the same operation. The cache is always warm; there are no misses after a write.

This Redis pipeline approach keeps both writes atomic from the caller's perspective, though they're not truly transactional across Redis and Postgres. For critical systems, you'd wrap this in a saga or use a distributed transaction coordinator.

<script src="https://gist.github.com/mohashari/8a60b6ef83e35f8c4e63497a823c64f9.js?file=snippet-2.go"></script>

Write-through is excellent for read-heavy workloads where you can afford slightly higher write latency. It's a poor fit for write-heavy workloads because every write now incurs both a database round-trip and a cache round-trip synchronously.

## Write-Behind (Write-Back)

Write-behind flips the latency trade-off: writes go to the cache immediately and are flushed to the database asynchronously. The application gets sub-millisecond write acknowledgment. The risk is data loss if the cache node dies before the flush.

A Redis Stream makes a clean write-behind queue. The application writes to Redis and enqueues a stream event; a background worker drains the stream into Postgres. The TTL on the cache key and the stream retention together define your durability window.

<script src="https://gist.github.com/mohashari/8a60b6ef83e35f8c4e63497a823c64f9.js?file=snippet-3.go"></script>

## Event-Driven Invalidation

The most scalable and decoupled approach uses database change events (CDC — Change Data Capture) to drive cache invalidation. Rather than the application layer deciding when to invalidate, the database itself emits an event on every committed write, and a separate consumer invalidates the cache. This works even when writes come from migration scripts, admin tools, or other services that bypass your application code.

Debezium with PostgreSQL's logical replication slot is the standard stack. Here's a minimal consumer that handles the invalidation side:

<script src="https://gist.github.com/mohashari/8a60b6ef83e35f8c4e63497a823c64f9.js?file=snippet-4.go"></script>

This Debezium connector configuration wires the Postgres `users` table to a Kafka topic that the consumer above reads from:

<script src="https://gist.github.com/mohashari/8a60b6ef83e35f8c4e63497a823c64f9.js?file=snippet-5.yaml"></script>

## Choosing the Right Pattern

No single strategy dominates. A realistic production system often combines them: cache-aside for most reads (low complexity, tolerates a brief cold start), write-through for user-facing entities where stale reads are visible and embarrassing, and event-driven invalidation as a safety net that catches writes from any source — including schema migrations run at 2am.

The decision checklist is short: if your write path is already latency-sensitive, eliminate synchronous cache writes (use cache-aside or write-behind). If your data is written from multiple sources outside your application, event-driven CDC is the only strategy that stays correct. If you have a single write path and need zero cache misses on the read side, write-through is clean and easy to reason about. Most importantly, set aggressive TTLs regardless of strategy — a cache that expires on its own is a cache that heals on its own, and that operational property is worth more than the few extra cache hits you'd get from a longer TTL.