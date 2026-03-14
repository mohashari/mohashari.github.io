---
layout: post
title: "Microservices Architecture Patterns Every Engineer Should Know"
tags: [microservices, architecture, backend]
description: "The essential microservices patterns — from service decomposition to inter-service communication and fault tolerance."
---

Microservices aren't a silver bullet. They solve real problems while creating new ones. This post covers the patterns that make microservices work in practice — and the pitfalls that bring them down.

![Microservices Architecture Diagram](/images/diagrams/microservices-architecture.svg)

## When to Go Microservices

Don't start with microservices. Start with a monolith, then extract services when you feel the pain:

- **Independent scaling** — One service needs 10x more instances than others
- **Independent deployment** — Teams are blocked waiting for release coordination
- **Technology diversity** — One component genuinely needs a different language/database
- **Organizational boundaries** — Conway's Law: structure follows org chart

The "microservices first" approach almost always leads to distributed monolith hell.

## Decomposition Patterns

### Decompose by Business Capability

Organize around business functions, not technical layers:


<script src="https://gist.github.com/mohashari/2906ab214d1e67848fb2ba58cfa21bcf.js?file=snippet.txt"></script>


### Decompose by Subdomain (DDD)

Use Domain-Driven Design to find service boundaries. Each bounded context becomes a service candidate.

## Communication Patterns

### Synchronous (REST/gRPC)

Use for operations requiring an immediate response:


<script src="https://gist.github.com/mohashari/2906ab214d1e67848fb2ba58cfa21bcf.js?file=snippet-2.txt"></script>


Problem: Coupling. If payment service is down, the whole chain fails.

### Asynchronous (Message Queue)

Use for operations that don't require immediate response:


<script src="https://gist.github.com/mohashari/2906ab214d1e67848fb2ba58cfa21bcf.js?file=snippet-3.txt"></script>


Benefits: Decoupling, resilience, natural retry mechanism.


<script src="https://gist.github.com/mohashari/2906ab214d1e67848fb2ba58cfa21bcf.js?file=snippet.go"></script>


## The API Gateway Pattern

Never expose your internal services directly. Use an API Gateway as the single entry point:


<script src="https://gist.github.com/mohashari/2906ab214d1e67848fb2ba58cfa21bcf.js?file=snippet-4.txt"></script>


The gateway handles:
- Authentication/Authorization
- Rate limiting
- Request routing
- SSL termination
- Response aggregation

## The Saga Pattern for Distributed Transactions

ACID transactions don't work across services. Use sagas — a sequence of local transactions with compensating transactions for rollback.

### Choreography-based Saga

Services react to events:


<script src="https://gist.github.com/mohashari/2906ab214d1e67848fb2ba58cfa21bcf.js?file=snippet-5.txt"></script>


### Orchestration-based Saga

A central coordinator drives the steps:


<script src="https://gist.github.com/mohashari/2906ab214d1e67848fb2ba58cfa21bcf.js?file=snippet-2.go"></script>


## Circuit Breaker Pattern

Prevent cascading failures when a downstream service is struggling:


<script src="https://gist.github.com/mohashari/2906ab214d1e67848fb2ba58cfa21bcf.js?file=snippet-3.go"></script>


Use libraries like `gobreaker` (Go) or `resilience4j` (Java) in production.

## Service Mesh

For complex microservices deployments, consider a service mesh (Istio, Linkerd):

- **mTLS** between all services
- **Distributed tracing** without code changes
- **Traffic management** (canary, weighted routing)
- **Circuit breaking** and **retries** at the infrastructure level

## The Most Important Rule

**Don't build microservices you don't need yet.** The architecture cost is real. Embrace the monolith, identify the seams, and extract thoughtfully when the business justifies it.
