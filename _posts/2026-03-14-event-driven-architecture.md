---
layout: post
title: "Event-Driven Architecture: Building Reactive Backend Systems"
tags: [architecture, backend, event-driven, messaging]
description: "Learn how event-driven architecture works, its patterns, benefits, and how to avoid the common pitfalls that sink EDA implementations."
---

Event-driven architecture (EDA) is a paradigm shift in how services communicate. Instead of direct calls ("do this now"), services emit events ("this happened") and other services react. The result: systems that are more resilient, scalable, and evolvable.

![Event-Driven Architecture with Outbox Pattern](/images/diagrams/event-driven-architecture.svg)

## The Core Concept

In a synchronous world:


<script src="https://gist.github.com/mohashari/0e5008c6abbf53aaa487bbffeff55922.js?file=snippet.txt"></script>


In an event-driven world:


<script src="https://gist.github.com/mohashari/0e5008c6abbf53aaa487bbffeff55922.js?file=snippet-2.txt"></script>


The Order Service doesn't know or care who's listening. Adding a new subscriber requires zero changes to the Order Service.

## Domain Events vs Integration Events

**Domain Events** represent something meaningful that happened within a bounded context:


<script src="https://gist.github.com/mohashari/0e5008c6abbf53aaa487bbffeff55922.js?file=snippet.go"></script>


**Integration Events** cross service boundaries. They're domain events published to a shared event bus.

## Event Sourcing

Instead of storing current state, store the sequence of events that led to it:


<script src="https://gist.github.com/mohashari/0e5008c6abbf53aaa487bbffeff55922.js?file=snippet-3.txt"></script>


Current state = fold/reduce all events:


<script src="https://gist.github.com/mohashari/0e5008c6abbf53aaa487bbffeff55922.js?file=snippet-2.go"></script>


**Benefits:** Complete audit trail, time-travel debugging, event replay.
**Costs:** Query complexity (need projections/read models), eventual consistency.

## The Outbox Pattern: Ensuring Reliable Event Publishing

The #1 mistake in EDA: publishing events separately from database transactions.


<script src="https://gist.github.com/mohashari/0e5008c6abbf53aaa487bbffeff55922.js?file=snippet-3.go"></script>


**Solution: Transactional Outbox**


<script src="https://gist.github.com/mohashari/0e5008c6abbf53aaa487bbffeff55922.js?file=snippet-4.go"></script>


## Idempotency in Event Consumers

Events can be delivered more than once (at-least-once delivery). Consumers must be idempotent:


<script src="https://gist.github.com/mohashari/0e5008c6abbf53aaa487bbffeff55922.js?file=snippet-5.go"></script>


## CQRS with Event-Driven Architecture

Command Query Responsibility Segregation pairs naturally with EDA:


<script src="https://gist.github.com/mohashari/0e5008c6abbf53aaa487bbffeff55922.js?file=snippet-4.txt"></script>


The read model is eventually consistent but optimized for query performance.

## Common Pitfalls

### 1. Making Events Too Granular

<script src="https://gist.github.com/mohashari/0e5008c6abbf53aaa487bbffeff55922.js?file=snippet-6.go"></script>


### 2. Event Coupling via Shared Types
Don't share event classes across service boundaries. Each service owns its own event schema.

### 3. Long Event Chains
Deep event chains make debugging a nightmare. If you have 8 services reacting to each other's events, reconsider the design.

### 4. No Dead Letter Queue
Always have a DLQ for events that fail processing. Without it, you lose data silently.


<script src="https://gist.github.com/mohashari/0e5008c6abbf53aaa487bbffeff55922.js?file=snippet-7.go"></script>


EDA is powerful but adds complexity. Use it where the benefits — loose coupling, scalability, audit trail — clearly outweigh the operational overhead.
