---
layout: post
title: "CQRS Pattern: Separating Reads and Writes for Performance and Scale"
date: 2026-03-16 07:00:00 +0700
tags: [cqrs, architecture, databases, distributed-systems, backend]
description: "Explore how Command Query Responsibility Segregation decouples read and write models to independently scale and optimize each path."
---

Every high-traffic application eventually hits the same wall: the database becomes a bottleneck because reads and writes compete for the same resources, use the same schema, and demand the same indexes. You add a read replica, but your ORM still issues `SELECT *` with twelve joins to render a dashboard. You optimize the write path, but now your reporting queries slow down inserts. The root cause is that you've modeled reads and writes identically — one schema, one model, one abstraction — when in reality they have completely different shapes, throughput requirements, and consistency needs. Command Query Responsibility Segregation (CQRS) addresses this directly by treating the write side and the read side as separate concerns, each with its own model, its own storage, and its own optimization strategy.

## The Core Idea

CQRS draws a hard boundary between two responsibilities. **Commands** mutate state — they represent intent ("place this order", "transfer these funds"). **Queries** return data — they represent questions ("what are the top ten products by revenue this week?"). In a traditional layered architecture, both flow through the same domain model and the same database tables. CQRS splits them at the application layer, so command handlers write to a normalized, consistency-first write store, while query handlers read from a denormalized, performance-first read store.

The read store is typically updated asynchronously by projecting events or change data out of the write store. This introduces eventual consistency — a trade-off you consciously accept in exchange for independently scalable, purpose-built query models.

## A Minimal Go Command Handler

On the write side, you model commands as explicit value objects and run them through handlers that enforce business rules. Here's a straightforward order placement handler:

<script src="https://gist.github.com/mohashari/7712f03b7bf3ab6e7b4031632ff00475.js?file=snippet.go"></script>

Notice that this handler knows nothing about how orders are read back. It only enforces invariants and persists the aggregate.

## Publishing Domain Events After a Write

After a command succeeds, the write side publishes a domain event that downstream projectors consume to build read models. Using a transactional outbox pattern prevents the dual-write problem:

<script src="https://gist.github.com/mohashari/7712f03b7bf3ab6e7b4031632ff00475.js?file=snippet-2.sql"></script>

The command handler inserts into `orders`, `order_items`, and `outbox_events` inside a single transaction. A separate relay process polls the outbox and forwards unpublished events to a message broker.

## The Projector: Building a Read Model

A projector subscribes to domain events and maintains a denormalized read table optimized for a specific query pattern. Here's a Go projector that materializes an order summary view:

<script src="https://gist.github.com/mohashari/7712f03b7bf3ab6e7b4031632ff00475.js?file=snippet-3.go"></script>

The `order_summaries` table is a read-optimized projection. There are no joins at query time — the projection did that work upfront when the event arrived.

## The Read Store Schema

The read model is intentionally denormalized. Indexes are added for read patterns, not for write correctness:

<script src="https://gist.github.com/mohashari/7712f03b7bf3ab6e7b4031632ff00475.js?file=snippet-4.sql"></script>

Because this table is append-and-update only — never used for writes from the business domain — you can add or drop indexes freely without worrying about insert overhead.

## A Query Handler That Reads From the Read Store

Query handlers are thin. They translate query parameters into SQL and return DTOs — no domain logic, no aggregates:

<script src="https://gist.github.com/mohashari/7712f03b7bf3ab6e7b4031632ff00475.js?file=snippet-5.go"></script>

This query completes with a single indexed scan. No joins, no aggregation at read time.

## Wiring It Together With Docker Compose

A typical CQRS stack separates the write database, the read database, and the message broker. Here's a minimal local setup:

<script src="https://gist.github.com/mohashari/7712f03b7bf3ab6e7b4031632ff00475.js?file=snippet-6.yaml"></script>

The command service connects to `write-db`, the projector consumes from Kafka and writes to `read-db`, and the query service only touches `read-db`.

## Rebuilding a Projection From Scratch

One of the most powerful properties of CQRS with an event log is that you can rebuild any read model by replaying history. This shell script replays the outbox from the write database into Kafka for a projector to reprocess:

<script src="https://gist.github.com/mohashari/7712f03b7bf3ab6e7b4031632ff00475.js?file=snippet-7.sh"></script>

This is a superpower unavailable in traditional CRUD systems: you can introduce a new read model at any time and populate it instantly by replaying past events.

CQRS is not a silver bullet, and it adds real complexity — eventual consistency, projection lag, and more moving parts to operate. But for systems where reads and writes have fundamentally different load profiles or data shapes, it pays significant dividends. Start by identifying your most expensive read patterns and asking whether they could be pre-computed. Build one projection, measure the latency improvement, and expand from there. The architecture scales incrementally: you don't have to separate every model on day one. The point is to stop forcing a single schema to serve two opposing masters, and instead give each side the exact data structure it deserves.