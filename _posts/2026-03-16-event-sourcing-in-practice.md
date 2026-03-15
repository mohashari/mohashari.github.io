---
layout: post
title: "Event Sourcing in Practice: Building an Audit-Ready System of Record"
date: 2026-03-16 07:00:00 +0700
tags: [event-sourcing, distributed-systems, databases, architecture, backend]
description: "Learn how to model application state as an immutable sequence of events, enabling full audit trails, temporal queries, and reliable projections."
---

Every production system eventually faces the same uncomfortable question: *what exactly happened, and when?* A bug corrupts account balances at 2 AM, a compliance team needs a full history of a user's consent changes, or you need to replay a week of transactions against a new pricing model. Traditional CRUD systems — where writes overwrite state in place — cannot answer these questions without bolting on fragile audit tables or digging through application logs. Event sourcing inverts this design: instead of storing the current state, you store the sequence of events that *produced* that state. Current state becomes a derived view, reconstructed on demand. The log is the truth.

## What Is an Event, Really?

An event is a fact — something that happened in your domain, expressed as an immutable record. It is not a command ("place order") or a query ("get balance"). It is past tense: `OrderPlaced`, `PaymentProcessed`, `InventoryReserved`. Events carry all the data needed to reconstruct what occurred, including who did it, when, and what changed.

Each event belongs to a *stream* — typically scoped to a single aggregate, like `order-8821` or `account-4402`. Streams are append-only. You never update or delete an event; you append a corrective event instead.

The schema for an event store is deceptively simple. Here is a PostgreSQL definition that handles the core requirements — ordering, optimistic concurrency, and metadata:

<script src="https://gist.github.com/mohashari/c4bff72a0ae6271932075862be888080.js?file=snippet.sql"></script>

The `UNIQUE` constraint on `(stream_id, stream_seq)` is your optimistic concurrency guard. If two writers try to append at the same sequence position, one will get a unique violation — no phantom overwrites, no lost updates.

## Appending Events Safely

In Go, a typical append operation loads the current stream version, applies domain logic to produce new events, then writes them with the expected version. The database constraint enforces correctness without a distributed lock:

<script src="https://gist.github.com/mohashari/c4bff72a0ae6271932075862be888080.js?file=snippet-2.go"></script>

Notice that `expectedSeq` is the last known sequence number for the stream. If another writer has already appended at that position, you get a clean error rather than silent data corruption.

## Rebuilding State from Events

Reading an aggregate back means loading its event stream and applying each event in order through a reducer function. This is the core loop of event sourcing:

<script src="https://gist.github.com/mohashari/c4bff72a0ae6271932075862be888080.js?file=snippet-3.go"></script>

Every call to `LoadOrder` produces the current state deterministically. If you want to know the state of the order *at any point in time*, you simply stop replaying events at a given `occurred_at` timestamp — a capability traditional systems cannot offer without significant infrastructure.

## Projections and Read Models

Loading from the full event log on every read is fine for low-volume aggregates, but for dashboards and search you need *projections*: pre-computed read models updated as events arrive. A lightweight projection worker polls for new events and updates a denormalized table:

<script src="https://gist.github.com/mohashari/c4bff72a0ae6271932075862be888080.js?file=snippet-4.go"></script>

This pattern — sometimes called a *catch-up subscription* — means projections can be rebuilt from scratch at any time by resetting `lastProcessedID` to zero. Schema migrations on read models become trivial: drop the table, replay the log, rebuild. No data migration scripts, no risk of losing history.

## Temporal Audit Queries

One of the most powerful properties of an event store is that audit queries are just SQL. Finding every state change to an account between two timestamps requires no special tooling:

<script src="https://gist.github.com/mohashari/c4bff72a0ae6271932075862be888080.js?file=snippet-5.sql"></script>

For compliance exports across all streams of a given type, add an index on `event_type` and push results to a report:

<script src="https://gist.github.com/mohashari/c4bff72a0ae6271932075862be888080.js?file=snippet-6.sql"></script>

Regulators, auditors, and on-call engineers all get their answers from the same table. There is no separate audit log to maintain or synchronize — it *is* the log.

## Snapshotting for Performance

For long-lived aggregates with thousands of events, replaying from the beginning on each load becomes expensive. Snapshots solve this without sacrificing the audit trail:

<script src="https://gist.github.com/mohashari/c4bff72a0ae6271932075862be888080.js?file=snippet-7.go"></script>

Snapshots are a pure performance optimization. You can delete every snapshot and the system remains fully correct — just slower. That safety property is worth internalizing.

Event sourcing is not a silver bullet. It adds real complexity: event versioning, eventual consistency in projections, and operational discipline around never deleting records. But for systems where auditability, replayability, and temporal correctness are genuine requirements — financial ledgers, compliance-sensitive workflows, distributed sagas — it pays its overhead back many times over. The key discipline is treating events as first-class domain objects with stable, versioned schemas, keeping projections clearly separated from the event log, and leaning on your database's concurrency guarantees rather than inventing your own. Start with a single aggregate, prove the pattern locally, and let the architecture grow outward only where the domain demands it.