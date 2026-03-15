---
layout: post
title: "Saga Pattern: Managing Distributed Transactions Without Two-Phase Commit"
date: 2026-03-15 07:00:00 +0700
tags: [saga, microservices, distributed-systems, patterns, backend]
description: "Implement the Saga pattern — choreography vs. orchestration — to maintain data consistency across microservices without distributed locks."
---

When you decompose a monolith into microservices, you trade one hard problem for another. The relational database gave you ACID transactions for free — a single `BEGIN`/`COMMIT` block kept your data consistent across every table. In a distributed system, that guarantee evaporates. An `ORDER` service, an `INVENTORY` service, and a `PAYMENT` service each own their own database. When a customer places an order, all three must succeed — or none of them should. Two-Phase Commit (2PC) is the textbook answer, but it introduces a distributed lock that blocks progress across every participant while the coordinator waits for votes. Under load, that becomes a latency and availability nightmare. The Saga pattern is the production-grade alternative: break the transaction into a sequence of local transactions, and define a compensating transaction for each step that can undo its effect if something downstream fails.

## The Two Flavors: Choreography vs. Orchestration

A Saga can be coordinated in one of two ways. In **choreography**, each service publishes an event after completing its local transaction, and downstream services react to those events autonomously. There is no central coordinator — the business flow emerges from the event chain. In **orchestration**, a dedicated Saga orchestrator sends commands to each service and tracks state explicitly. Choreography is simpler to deploy but harder to reason about as the number of services grows. Orchestration adds a new service but makes the flow observable and easier to debug.

## Modeling State Explicitly

Before writing any code, define the states a Saga can inhabit. Every step either succeeds and moves forward, or fails and triggers backward compensation. In Go, this maps naturally to a typed state machine.

<script src="https://gist.github.com/mohashari/6c562f77e999d4d47a5178e830b97f5b.js?file=snippet.go"></script>

Persisting this state in a `sagas` table means that even if the orchestrator crashes mid-flight, it can recover and resume from the last known checkpoint.

## Persisting the Saga Log

Every state transition writes a record. This is your audit trail and your recovery mechanism. The schema keeps it simple — a JSONB payload column lets you store arbitrary per-step context without schema migrations every time a step changes.

<script src="https://gist.github.com/mohashari/6c562f77e999d4d47a5178e830b97f5b.js?file=snippet-2.sql"></script>

## The Orchestrator

The orchestrator is the nerve center of the Saga. It receives the initial trigger, issues commands to downstream services via an event bus or HTTP, listens for responses, and advances — or rolls back — the state machine accordingly.

<script src="https://gist.github.com/mohashari/6c562f77e999d4d47a5178e830b97f5b.js?file=snippet-3.go"></script>

The key discipline here: every call to `advanceSaga` is a local database write. The orchestrator never assumes a command succeeded — it only advances state when it receives a confirmation event back from the downstream service.

## Compensating Transactions

Compensation is not a rollback in the database sense. You cannot undo a charge that already hit a payment processor. Instead, you issue a new transaction that logically reverses the effect — a refund, a release of a reservation, a status update. Each step in the forward path must have a corresponding compensating action defined before you implement the forward step.

<script src="https://gist.github.com/mohashari/6c562f77e999d4d47a5178e830b97f5b.js?file=snippet-4.go"></script>

## Idempotency at Every Step

Because messages can be redelivered (Kafka, RabbitMQ, and SQS all guarantee at-least-once delivery), every handler must be idempotent. The cleanest way to enforce this is an idempotency key table — check before you act.

<script src="https://gist.github.com/mohashari/6c562f77e999d4d47a5178e830b97f5b.js?file=snippet-5.go"></script>

The `ON CONFLICT DO NOTHING` pattern combined with a unique index makes this handler safe to call any number of times for the same command — subsequent deliveries are no-ops.

## Wiring It Together with Docker Compose

For local development, you need the orchestrator, the three downstream services, PostgreSQL, and a message broker all running together. A minimal Compose file makes this reproducible.

<script src="https://gist.github.com/mohashari/6c562f77e999d4d47a5178e830b97f5b.js?file=snippet-6.yaml"></script>

## Observing In-Flight Sagas

Stuck or timed-out sagas are the operational hazard to watch for. A background sweeper queries for sagas that have not advanced in too long and either retries or marks them permanently failed — your on-call team's best friend.

<script src="https://gist.github.com/mohashari/6c562f77e999d4d47a5178e830b97f5b.js?file=snippet-7.sql"></script>

Alert on this query's row count. A sudden spike means either an upstream service is down or a bug in a compensating handler is leaving sagas in an unrecoverable limb state.

The Saga pattern will not make distributed transactions simple — nothing will. What it does is make failures explicit, recoverable, and observable. You trade the invisible guarantee of ACID for a visible state machine whose every transition is logged and auditable. The compensating transaction discipline forces you to think about failure modes before you write the happy path, which is exactly the mindset shift that separates services that survive production from services that merely pass local tests. Start with orchestration when you are new to the pattern — the explicit state machine is worth the extra service — and graduate to choreography only when the operational overhead of the orchestrator becomes your bottleneck.