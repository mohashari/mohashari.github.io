---
layout: post
title: "Saga Pattern: Managing Distributed Transactions Without Two-Phase Commit"
date: 2026-03-18 07:00:00 +0700
tags: [distributed-systems, microservices, patterns, transactions, reliability]
description: "Implement choreography and orchestration-based sagas to maintain data consistency across microservices without locking or two-phase commit."
---

Every distributed system eventually confronts the same brutal reality: you need to update data across multiple services atomically, but you cannot lock rows in three different databases owned by three different teams. Two-phase commit (2PC) promises atomicity, but it comes with a coordinator that becomes a single point of failure, locks held across network round-trips, and a blocking protocol that crumbles under partial failures. The Saga pattern offers a different contract — instead of preventing inconsistency through locking, it embraces the possibility of intermediate states and provides compensating transactions to recover from them. It is not a compromise; it is an acknowledgment of how distributed systems actually behave.

## The Core Idea

A saga is a sequence of local transactions. Each step publishes an event or sends a command that triggers the next step. If any step fails, the saga executes compensating transactions for all previously completed steps in reverse order. There are two coordination styles: **choreography**, where services react to events without a central coordinator, and **orchestration**, where a dedicated saga orchestrator directs each participant.

Choreography works well for simple flows with two or three participants. Orchestration becomes essential when you have complex branching logic, need clear visibility into saga state, or want to avoid tight event-coupling between services.

## Modeling the Saga State Machine

Before writing any infrastructure code, model your saga as an explicit state machine. An order fulfillment saga might move through states like `PENDING`, `INVENTORY_RESERVED`, `PAYMENT_AUTHORIZED`, `ORDER_CONFIRMED`, and compensating states like `PAYMENT_VOIDED`, `INVENTORY_RELEASED`, `ORDER_CANCELLED`.

<script src="https://gist.github.com/mohashari/d763fa7edbd90a9d954bd14c0bc7f006.js?file=snippet.go"></script>

Explicit state machines prevent illegal transitions from corrupting your saga log and make debugging dramatically easier — you always know exactly where a saga failed.

## Persisting Saga State with Optimistic Locking

The saga orchestrator must persist its state durably. Use a version column for optimistic locking to prevent concurrent workers from processing the same saga twice, which would cause duplicate compensations.

<script src="https://gist.github.com/mohashari/d763fa7edbd90a9d954bd14c0bc7f006.js?file=snippet-2.sql"></script>

The partial index on active sagas keeps the recovery worker's query fast — it only scans sagas that have not yet reached a terminal state.

## The Orchestrator Step Handler

The orchestrator advances the saga by issuing commands and updating state transactionally. The critical pattern here is the **transactional outbox**: write the command to an outbox table in the same database transaction that updates saga state, then a separate relay process publishes it. This eliminates the dual-write problem where you update state but fail to send the message.

<script src="https://gist.github.com/mohashari/d763fa7edbd90a9d954bd14c0bc7f006.js?file=snippet-3.go"></script>

Because the outbox write and the saga state update share a transaction, you get exactly-once delivery semantics from the database's perspective. The relay delivers at-least-once, and each step handler is idempotent.

## Writing Idempotent Compensating Transactions

Compensating transactions must be idempotent. The compensation for reserving inventory is releasing that reservation, but if the release message is delivered twice, the second delivery should be a no-op rather than releasing inventory that has already been re-allocated.

<script src="https://gist.github.com/mohashari/d763fa7edbd90a9d954bd14c0bc7f006.js?file=snippet-4.go"></script>

Notice the `FOR UPDATE` lock followed by status checks. This prevents a race where two concurrent compensation messages both observe `HELD` status and both proceed to release.

## Choreography-Based Saga with Event Sourcing

For simpler flows, choreography eliminates the orchestrator entirely. Each service listens for events and emits its own. The saga's progress is implicit in the event stream rather than stored in a saga table.

<script src="https://gist.github.com/mohashari/d763fa7edbd90a9d954bd14c0bc7f006.js?file=snippet-5.go"></script>

Choreography is elegant but hides the overall saga flow across service codebases. Debugging a failed saga means reconstructing its timeline from events scattered across multiple message queues and logs.

## Recovery: Finding and Retrying Stuck Sagas

Sagas can get stuck when a service crashes mid-step or a message is lost. A recovery worker periodically scans for sagas that have been in a non-terminal state too long and re-publishes their expected outgoing command.

<script src="https://gist.github.com/mohashari/d763fa7edbd90a9d954bd14c0bc7f006.js?file=snippet-6.go"></script>

The recovery worker is what transforms the saga pattern from a theory into a reliable production system. Without it, stuck sagas accumulate silently and customer data ends up in limbo.

## Choosing Between Choreography and Orchestration

The choice comes down to flow complexity and observability requirements. Choreography distributes logic across services — good for simple, stable flows where teams want full autonomy. Orchestration centralizes logic in one place — better for complex flows with conditional branching, timeouts, and human approval steps, and far easier to monitor with a single saga state table. Many production systems use both: choreography for high-throughput simple flows (order item state transitions) and orchestration for complex, multi-step business processes (onboarding, fulfillment, refunds).

The Saga pattern does not give you distributed transactions — it gives you a structured way to manage distributed failures. Embrace the intermediate states, make every step idempotent, write your compensations before you write your forward transactions, and build the recovery worker before you go to production. The systems that handle failures gracefully are the ones that were designed expecting them.