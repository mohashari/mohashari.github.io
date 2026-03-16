---
layout: post
title: "Event Sourcing in Practice: Modeling State as an Immutable Log"
date: 2026-03-17 07:00:00 +0700
tags: [event-sourcing, distributed-systems, architecture, databases, cqrs]
description: "Implement event sourcing to capture every state change as an immutable event, enabling full audit trails, time-travel debugging, and reliable projections."
---

Every time a user updates their shipping address in your e-commerce platform, you overwrite the old one. When a payment fails and then succeeds after a retry, you update a status column. When an order is cancelled, you set a flag. Three months later, your support team asks: "Why did this order ship to the wrong address?" You have no answer. The database reflects the current state of the world, but the history — the *why* and *how* you got there — is gone forever. This is the fundamental limitation of state-oriented persistence, and it's the problem that event sourcing was designed to solve.

## What Event Sourcing Actually Is

Event sourcing inverts the storage model. Instead of persisting the current state of an entity, you persist the sequence of *events* that caused it to reach that state. The current state becomes a derived artifact — a projection you compute by replaying events. An `Order` isn't a row in a database; it's the result of applying `OrderPlaced`, `ItemAdded`, `PaymentProcessed`, and `OrderShipped` in sequence.

This model has three profound consequences. First, you get a complete audit trail by default — not as an afterthought. Second, you can reconstruct the state of any entity at any point in time by replaying events up to that moment. Third, you decouple the write model (the event log) from the read model (projections), which maps naturally onto CQRS (Command Query Responsibility Segregation).

## Defining the Event Schema

Events are immutable facts. They use past-tense names because they describe something that *has already happened*. Each event carries enough data to be self-describing without requiring a lookup elsewhere.

<script src="https://gist.github.com/mohashari/bde9507444e207a284bed45f81fde80b.js?file=snippet.go"></script>

Notice the `Version` field — this is critical. It enforces optimistic concurrency by ensuring that no two writes can produce the same version number for a given aggregate. Without it, two concurrent commands could produce conflicting events that both appear valid.

## The Event Store

The event store is the heart of the system. It's an append-only log — events are never updated or deleted. You load an aggregate's history by reading its events in order, and you persist new state by appending new events.

<script src="https://gist.github.com/mohashari/bde9507444e207a284bed45f81fde80b.js?file=snippet-2.sql"></script>

The `UNIQUE (aggregate_id, version)` constraint is your optimistic concurrency guard at the database level. If two transactions try to append version 5 for the same aggregate simultaneously, one will win and the other will receive a constraint violation — which your application layer translates into a concurrency conflict to be retried.

## Loading and Saving Aggregates

The repository pattern for event-sourced aggregates differs from traditional ones. You load by replaying events, and you save by appending only the new events generated since the aggregate was loaded.

<script src="https://gist.github.com/mohashari/bde9507444e207a284bed45f81fde80b.js?file=snippet-3.go"></script>

## The Aggregate and Its Apply Method

The aggregate reconstructs itself by applying each event in order. The `Apply` method is a pure function — no side effects, no I/O, just state transitions. This makes aggregates trivially testable.

<script src="https://gist.github.com/mohashari/bde9507444e207a284bed45f81fde80b.js?file=snippet-4.go"></script>

## Building Read-Model Projections

Because the event log is the source of truth, you can build as many read models as you need. A projection subscribes to the event stream and materializes a denormalized view optimized for querying — think of it as a continuously updated materialized view.

<script src="https://gist.github.com/mohashari/bde9507444e207a284bed45f81fde80b.js?file=snippet-5.go"></script>

If your projection logic has a bug, you can fix it, drop the projection table, and replay all historical events to rebuild it from scratch. This is one of event sourcing's most powerful operational properties.

## Wiring Up an Event Bus with PostgreSQL LISTEN/NOTIFY

For a practical starting point without a full message broker, PostgreSQL's `LISTEN/NOTIFY` can propagate events to projection workers in near-real-time. A trigger fires after each insert into the event store.

<script src="https://gist.github.com/mohashari/bde9507444e207a284bed45f81fde80b.js?file=snippet-6.sql"></script>

This keeps your projection workers decoupled from the write path while avoiding the operational overhead of a full Kafka or RabbitMQ deployment during early development. When throughput demands grow, you can swap `LISTEN/NOTIFY` for a proper stream without changing the projection handlers.

## Time-Travel Debugging in Practice

One of the most compelling features is the ability to reconstruct state at any point in time — invaluable when investigating production incidents.

<script src="https://gist.github.com/mohashari/bde9507444e207a284bed45f81fde80b.js?file=snippet-7.go"></script>

With this function, answering "What was the state of order `abc-123` at 14:32 UTC on the day the customer complained?" becomes a two-line query and a function call rather than a forensic archaeology expedition through backup snapshots and log files.

Event sourcing is not a silver bullet — it introduces real complexity around schema evolution (what do you do when an event's structure needs to change?), snapshot strategies for long-lived aggregates with thousands of events, and the eventual consistency inherent in asynchronous projections. These are tractable problems, each with established patterns. But if you operate systems where auditability, correctness, and the ability to understand *why* your data is the way it is matter — and they should — the immutable event log is a foundation worth building on. Start with a single bounded context, validate the model against your domain, and let the projections proliferate from there.