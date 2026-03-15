---
layout: post
title: "CQRS and Event Sourcing: A Practical Implementation Guide"
date: 2026-03-15 07:00:00 +0700
tags: [cqrs, event-sourcing, architecture, distributed-systems, backend]
description: "Implement Command Query Responsibility Segregation and Event Sourcing patterns to build auditable, scalable backend systems."
---

# CQRS and Event Sourcing: A Practical Implementation Guide

Most backend systems start with a single database model that handles both reads and writes. This works fine at first — you write an order, you read it back, everyone is happy. But as systems grow, cracks appear: your write-heavy order processing competes with read-heavy reporting queries, debugging production issues means reconstructing state from incomplete logs, and rolling back a bad deployment requires guessing which rows were affected. Command Query Responsibility Segregation (CQRS) and Event Sourcing are two patterns that, when combined, fundamentally change how your system stores and exposes state — trading the familiar simplicity of a mutable row for an immutable, auditable ledger of facts.

## The Core Idea

CQRS splits your application model into two distinct paths: commands (writes that change state) and queries (reads that return state). These paths have different consistency requirements, different scaling needs, and benefit from different data models. Event Sourcing takes this further by storing the *sequence of events that caused state changes* rather than the current state itself. Instead of a row that says `balance: 340`, you store `AccountOpened(500)`, `MoneyDebited(200)`, `MoneyDeposited(40)` — and derive current state by replaying those events. The append-only event log becomes your source of truth.

## Defining Commands and Events

Start by modeling your domain in terms of intent (commands) and facts (events). Commands express what a user *wants to happen*; events record what *did happen*. This distinction matters — a command can be rejected, but an event is immutable history.

<script src="https://gist.github.com/mohashari/b2cba66f922d5b78dc1e1d1ae0c8daa9.js?file=snippet.go"></script>

`★ Insight ─────────────────────────────────────`
Commands and events are structurally similar but semantically opposite. Commands are imperative and can fail validation; events are declarative and represent committed history. Keeping them as separate types prevents accidental mutation of the event log.
`─────────────────────────────────────────────────`

## The Event Store

The event store is the backbone of Event Sourcing. It is an append-only log of domain events, keyed by aggregate ID and ordered by sequence number. Nothing is ever updated or deleted.

<script src="https://gist.github.com/mohashari/b2cba66f922d5b78dc1e1d1ae0c8daa9.js?file=snippet-2.sql"></script>

The `UNIQUE (aggregate_id, sequence_num)` constraint is your optimistic concurrency guard. If two concurrent commands try to append event #5 for the same aggregate, only one wins — the other gets a unique violation and must retry.

## Implementing the Aggregate

An aggregate in Event Sourcing has two responsibilities: validating and applying commands to produce events, and replaying events to reconstruct current state. The aggregate never reads from the query side — it lives entirely in the write model.

<script src="https://gist.github.com/mohashari/b2cba66f922d5b78dc1e1d1ae0c8daa9.js?file=snippet-3.go"></script>

`★ Insight ─────────────────────────────────────`
`RehydrateOrder` replays every stored event to rebuild current state — this is the core mechanism of Event Sourcing. For aggregates with long histories, you introduce snapshots: periodically serialize current state so replay only covers events since the last snapshot.
`─────────────────────────────────────────────────`

## The Command Handler

The command handler wires together loading an aggregate from the event store, dispatching a command, and persisting the resulting events. This is the write path.

<script src="https://gist.github.com/mohashari/b2cba66f922d5b78dc1e1d1ae0c8daa9.js?file=snippet-4.go"></script>

## Building Read Models with Projections

The read side consumes events from the write side and builds denormalized, query-optimized views — these are called projections. Projections are rebuilt entirely from events, which means you can add new projections retroactively by replaying history.

<script src="https://gist.github.com/mohashari/b2cba66f922d5b78dc1e1d1ae0c8daa9.js?file=snippet-5.go"></script>

The critical insight here: `order_summaries` is completely disposable. If you change the projection logic, drop the table and replay all events from the beginning. This is the superpower of Event Sourcing — your history is always available for reprocessing.

## Wiring It Together with Docker Compose

A minimal CQRS stack needs a write database (Postgres as event store), a message broker (Kafka or NATS for event propagation), and a read database (Postgres or Redis for projections). Here is a compose file that gets you running locally:

<script src="https://gist.github.com/mohashari/b2cba66f922d5b78dc1e1d1ae0c8daa9.js?file=snippet-6.yaml"></script>

## Replaying History to Rebuild a Projection

One of the most practical operational benefits of Event Sourcing is the ability to rebuild any projection from scratch. This shell script triggers a full replay against your projection worker:

<script src="https://gist.github.com/mohashari/b2cba66f922d5b78dc1e1d1ae0c8daa9.js?file=snippet-7.sh"></script>

This is routine maintenance in an Event Sourced system — you might replay projections when you fix a bug in projection logic, add a new read model, or migrate to a new schema. In a traditional CRUD system, this kind of retroactive correction is often impossible.

## Closing Thoughts

CQRS and Event Sourcing are not silver bullets — they add operational complexity, eventual consistency between write and read sides, and require your team to think in events rather than state. But for systems where auditability matters (finance, healthcare, logistics), where reads and writes have drastically different scaling needs, or where you need the ability to reconstruct historical state for debugging or compliance, the trade-off is worth it. Start with Event Sourcing on a single bounded context, keep your event schemas versioned from day one, and build the discipline of thinking in immutable facts. Once you have debugged a production incident by replaying events to the exact moment of failure, you will never want to go back to overwriting rows.