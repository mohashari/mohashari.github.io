---
layout: post
title: "Distributed Tracing with OpenTelemetry: From Zero to Production"
date: 2026-03-15 07:00:00 +0700
tags: [observability, opentelemetry, tracing, microservices, backend]
description: "Set up distributed tracing with OpenTelemetry in Go — instrument your services, propagate context across network boundaries, and visualize traces in Jaeger."
---

In a microservices system, a single user request touches 5–10 services. When something is slow or broken, how do you know which service is responsible? Distributed tracing gives you the complete picture.

## Core Concepts

- **Trace**: A complete end-to-end journey of one request through your system
- **Span**: A single operation within a trace (one HTTP call, one DB query)
- **Context propagation**: Passing trace metadata (trace ID, span ID) across service boundaries via headers
- **Baggage**: Key-value pairs that travel with the trace (e.g., user ID, request ID)

## OpenTelemetry Setup in Go

<script src="https://gist.github.com/mohashari/84f4318adcf699a815f21d9fda87f4f3.js?file=snippet.sh"></script>

### Initialize the Tracer Provider

<script src="https://gist.github.com/mohashari/84f4318adcf699a815f21d9fda87f4f3.js?file=snippet.go"></script>

### Instrument HTTP Handlers

<script src="https://gist.github.com/mohashari/84f4318adcf699a815f21d9fda87f4f3.js?file=snippet-2.go"></script>

### Create Custom Spans

<script src="https://gist.github.com/mohashari/84f4318adcf699a815f21d9fda87f4f3.js?file=snippet-3.go"></script>

### Propagate Context Across HTTP Calls

<script src="https://gist.github.com/mohashari/84f4318adcf699a815f21d9fda87f4f3.js?file=snippet-4.go"></script>

## Docker Compose for Jaeger

<script src="https://gist.github.com/mohashari/84f4318adcf699a815f21d9fda87f4f3.js?file=snippet.yaml"></script>

Open `http://localhost:16686` to see traces.

## Production Sampling Strategy

<script src="https://gist.github.com/mohashari/84f4318adcf699a815f21d9fda87f4f3.js?file=snippet-5.go"></script>

For critical paths (payment, auth), use `AlwaysSample`. For high-volume health checks, use `NeverSample`.

## What Good Traces Tell You

1. **Latency breakdown**: Which service/query is slow?
2. **Error propagation**: Where did the error originate?
3. **Dependency map**: Which services call which?
4. **Bottlenecks**: Sequential calls that could be parallelized

A single trace showing `DB query: 2.3s` out of a `total: 2.5s` request tells you exactly where to optimize — no guesswork.
