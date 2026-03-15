---
layout: post
title: "Hexagonal Architecture in Practice: Decoupling Your Backend from Frameworks and Databases"
date: 2026-04-13 07:00:00 +0700
tags: [architecture, hexagonal, clean-architecture, backend, design-patterns]
description: "Apply hexagonal (ports and adapters) architecture to isolate your core domain logic from infrastructure concerns, improving testability and flexibility."
---

Most backend applications start simple: a handler calls a service, the service calls the database, done. Then the requirements grow. You swap Postgres for DynamoDB, or you need to run the same logic via HTTP *and* a message queue, or your unit tests start spinning up real databases and taking 40 seconds to run. The core business logic — the thing that actually matters — becomes buried under layers of framework glue, ORM magic, and infrastructure assumptions. Hexagonal architecture, also called Ports and Adapters, was designed precisely for this problem: draw a hard boundary around your domain, define the interfaces it needs, and let infrastructure plug in from the outside.

## The Core Idea: Ports and Adapters

The domain is the hexagon. It knows nothing about HTTP, Postgres, Kafka, or any specific technology. It expresses what it *needs* through **ports** — Go interfaces, in our case — and the outside world provides **adapters** that implement those ports. There are two kinds of ports: **driving ports** (how the outside world calls into your domain) and **driven ports** (how your domain calls out to infrastructure). An HTTP handler is a driving adapter. A Postgres repository is a driven adapter.

Let's build a small order-processing service to make this concrete.

## Defining the Domain

Start with pure domain types and logic — no imports from any framework or database library.

<script src="https://gist.github.com/mohashari/79bcbde0fbc4f38f3b28eb620b25ccca.js?file=snippet.go"></script>

Notice zero external imports. This code is entirely unit-testable without any infrastructure.

## Defining the Driven Ports

The domain declares what it needs from the outside world as interfaces. These are the driven ports. The domain owns these interfaces; adapters implement them.

<script src="https://gist.github.com/mohashari/79bcbde0fbc4f38f3b28eb620b25ccca.js?file=snippet-2.go"></script>

## The Application Service (Use Cases)

The application service implements the driving port and coordinates domain objects with driven ports. It expresses *what the application does*, not how infrastructure works.

<script src="https://gist.github.com/mohashari/79bcbde0fbc4f38f3b28eb620b25ccca.js?file=snippet-3.go"></script>

The service has no idea whether `repo` is Postgres, Redis, or an in-memory map. That's the entire point.

## A Driven Adapter: Postgres Repository

Now we write the Postgres adapter that implements `domain.OrderRepository`. This lives in an `infrastructure` or `adapters` package and is the *only* place that knows about `pgx` or `database/sql`.

<script src="https://gist.github.com/mohashari/79bcbde0fbc4f38f3b28eb620b25ccca.js?file=snippet-4.go"></script>

## A Driving Adapter: HTTP Handler

The HTTP handler is a driving adapter — it translates an HTTP request into a call on the application service's driving port.

<script src="https://gist.github.com/mohashari/79bcbde0fbc4f38f3b28eb620b25ccca.js?file=snippet-5.go"></script>

You could replace this entire file with a gRPC handler or a Kafka consumer and the application service would not change at all.

## Testing Without Infrastructure

The payoff arrives in tests. Implement the driven port with an in-memory fake and test the application service at full speed — no Docker, no migrations, no network.

<script src="https://gist.github.com/mohashari/79bcbde0fbc4f38f3b28eb620b25ccca.js?file=snippet-6.go"></script>

Your test now wires the service with the in-memory adapter and exercises the full use-case logic in microseconds.

## Wiring It All Together

The composition root — typically `main.go` — is the only place that knows about all adapters at once. It's the seam where technology choices get made.

<script src="https://gist.github.com/mohashari/79bcbde0fbc4f38f3b28eb620b25ccca.js?file=snippet-7.go"></script>

Every technology decision is a one-line swap in `main.go`.

## Takeaway

Hexagonal architecture is not about adding layers for their own sake — it's about making your core domain logic the center of gravity and treating every external system as a replaceable plugin. The discipline of writing interfaces in the domain package, implementing them only in adapter packages, and keeping `main.go` as the sole composition root will pay dividends the first time you need to swap a database, add a new entry point, or write a test suite that finishes in under a second. The architecture does not prevent complexity; it localizes it. Infrastructure concerns stay in infrastructure packages, domain rules stay in the domain, and the two never meet except through the clean contract of a Go interface.