---
layout: post
title: "CQRS in Practice: Separating Reads and Writes for Scalable Backends"
date: 2026-03-18 07:00:00 +0700
tags: [cqrs, architecture, patterns, scalability, backend]
description: "Apply Command Query Responsibility Segregation to decouple read and write models, enabling independent scaling and optimized query performance."
---

Most backend systems start with a single database model that handles everything: writes from API mutations, reads from dashboards, reports, and search endpoints. This works fine at low scale, but as traffic grows, the model breaks down. Writes need strong consistency and transactional guarantees. Reads need speed, denormalization, and complex joins. Trying to serve both from one model creates index bloat, lock contention, and query plans that optimize for neither. Command Query Responsibility Segregation (CQRS) solves this by splitting your application into two explicit paths: one that mutates state, one that queries it. Each side can be modeled, optimized, and scaled independently.

## The Core Idea

CQRS draws a hard line between **commands** (intents to change state) and **queries** (requests for data). A command like `PlaceOrder` carries intent and produces domain events. A query like `GetOrderSummary` reads from a purpose-built projection. The write side enforces business rules and maintains consistency. The read side is optimized purely for retrieval — it can be denormalized, cached, or served from a different database entirely.

The pattern pairs naturally with **event sourcing**, but it doesn't require it. You can implement CQRS against a traditional relational database, using separate read models that are updated via triggers, background workers, or change data capture.

## Defining Commands and Queries

Start by defining your command and query types explicitly. In Go, this means concrete structs that carry their payload and nothing else.

Commands express intent without returning domain data. Queries are pure data requests with no side effects.

<script src="https://gist.github.com/mohashari/02b3706dc9beedb386a68cf0dda5d67a.js?file=snippet.go"></script>

## The Command Handler

Command handlers own the write path. They load the aggregate from the write store, apply business rules, and persist the result. They return only an error — never domain data. This enforces the separation: callers that want to see the result of a command must issue a subsequent query.

<script src="https://gist.github.com/mohashari/02b3706dc9beedb386a68cf0dda5d67a.js?file=snippet-2.go"></script>

`★ Insight ─────────────────────────────────────`
- The command handler deliberately returns no domain data — this enforces the CQRS boundary at the type level and prevents callers from accidentally bypassing the read model.
- Publishing an event after a successful write is the bridge between the write and read sides. The event carries just enough data to update projections without requiring a round-trip read.
`─────────────────────────────────────────────────`

## The Write Schema: Normalized for Consistency

The write database is normalized. It prioritizes referential integrity, transactional correctness, and minimal redundancy. It doesn't need to answer complex read queries efficiently.

<script src="https://gist.github.com/mohashari/02b3706dc9beedb386a68cf0dda5d67a.js?file=snippet-3.sql"></script>

## The Read Model: Denormalized for Speed

The read schema is built specifically to serve queries. It flattens joins, pre-computes aggregates, and can live in a completely different database — Postgres read replica, Redis, Elasticsearch, even a materialized view.

<script src="https://gist.github.com/mohashari/02b3706dc9beedb386a68cf0dda5d67a.js?file=snippet-4.sql"></script>

## The Projection Builder

A projection builder listens to domain events and updates the read model. It runs asynchronously — either as a message queue consumer or a CDC worker. This is the "eventual consistency" part of CQRS: the read side may lag slightly behind the write side, which is an acceptable trade-off for most use cases.

<script src="https://gist.github.com/mohashari/02b3706dc9beedb386a68cf0dda5d67a.js?file=snippet-5.go"></script>

## The Query Handler

The query handler reads exclusively from the read model. It can use different connection pools, different databases, or even in-memory caches. Because there are no writes here, you can scale this path with read replicas without any coordination logic.

<script src="https://gist.github.com/mohashari/02b3706dc9beedb386a68cf0dda5d67a.js?file=snippet-6.go"></script>

## Wiring It Together with Separate Connection Pools

The infrastructure layer enforces the separation at the database level. Write operations use the primary. Reads use replicas. In a containerized deployment, you scale the read replica count independently.

<script src="https://gist.github.com/mohashari/02b3706dc9beedb386a68cf0dda5d67a.js?file=snippet-7.go"></script>

`★ Insight ─────────────────────────────────────`
- Separate `sql.DB` pools for reads and writes let you tune connection limits independently — writes typically need tighter limits to avoid lock pile-ups, while reads can tolerate higher concurrency.
- Pointing the read pool at a replica DSN is often all you need to shift 80% of database traffic away from the primary, buying significant headroom without changing any application logic.
- The projection builder is the only code allowed to write to the read database — treating read stores as "owned by projections" prevents drift and makes rebuilding them deterministic.
`─────────────────────────────────────────────────`

## Keeping Projections Rebuilable

One underappreciated benefit of CQRS is that read models are disposable. If you introduce a new query requirement or discover a projection has drifted, you can truncate the table and replay all historical events from the beginning. This makes schema migrations on the read side low-risk.

<script src="https://gist.github.com/mohashari/02b3706dc9beedb386a68cf0dda5d67a.js?file=snippet-8.sh"></script>

CQRS is not a silver bullet, and it adds operational complexity: you now own two schemas, an event bus, and eventual consistency semantics. But the payoff is real. Write throughput no longer competes with read scalability. Query models can be tuned without touching business logic. New read requirements — reports, search indexes, analytics — become additive projections rather than risky schema changes on the hot write path. Start by identifying your most read-heavy endpoints, extract a dedicated read model backed by a replica, and build a simple projection worker. That one change, applied to the right bottleneck, will teach you more about CQRS than any whitepaper.