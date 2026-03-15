---
layout: post
title: "Building Resilient Systems with the Circuit Breaker Pattern"
date: 2026-03-15 07:00:00 +0700
tags: [circuit-breaker, resilience, microservices, patterns, go]
description: "Implement the Circuit Breaker pattern to prevent cascading failures and gracefully degrade when downstream services become unavailable."
---

# Building Resilient Systems with the Circuit Breaker Pattern

In distributed systems, failure is not a question of *if* but *when*. A single slow database query, an overloaded payment gateway, or a degraded third-party API can cascade into a full system outage within seconds. Engineers often reach for retry logic as the first line of defense, but naive retries under failure conditions can amplify the problem — hammering an already struggling service with a flood of redundant requests. The Circuit Breaker pattern, borrowed from electrical engineering, offers a more principled approach: detect failure early, stop sending requests when a service is struggling, and allow it time to recover before trying again.

## The Three States of a Circuit Breaker

A circuit breaker wraps calls to an external dependency and tracks their outcomes. It operates in three states: **Closed** (normal operation, requests pass through), **Open** (failures exceeded threshold, requests are rejected immediately without hitting the downstream service), and **Half-Open** (a probe state where a limited number of requests are allowed through to test if the service has recovered). Understanding this state machine is the foundation for any implementation.

The transition logic is straightforward: accumulate failures in the Closed state until a threshold is crossed, then trip to Open. After a configured timeout, transition to Half-Open and allow a single probe request. If it succeeds, reset to Closed; if it fails, return to Open and restart the timer.

Let's implement this from scratch in Go, starting with the core state machine:

<script src="https://gist.github.com/mohashari/6e6a5ffd382e39bb314633706cba36a8.js?file=snippet.go"></script>

## Recording Outcomes and Transitioning State

The breaker needs to react to both successes and failures. A critical implementation detail is that state transitions must be atomic — a `sync.Mutex` prevents races when multiple goroutines share the same breaker instance. Each call to `Execute` wraps the upstream call and updates the breaker's internal state based on the outcome.

<script src="https://gist.github.com/mohashari/6e6a5ffd382e39bb314633706cba36a8.js?file=snippet-2.go"></script>

## Wrapping an HTTP Client

In practice, circuit breakers protect calls to downstream HTTP services. Here's how to integrate the breaker into an HTTP client that calls a payment processing API. Notice the fallback — returning a cached or degraded response instead of propagating the error to the caller:

<script src="https://gist.github.com/mohashari/6e6a5ffd382e39bb314633706cba36a8.js?file=snippet-3.go"></script>

## Exposing Breaker State via Metrics

Observability is non-negotiable for circuit breakers in production. You need to know when breakers trip, how often, and for how long. Prometheus metrics provide exactly that visibility. Instrument the state transitions so your alerting rules can page on-call when a critical dependency is persistently open:

<script src="https://gist.github.com/mohashari/6e6a5ffd382e39bb314633706cba36a8.js?file=snippet-4.go"></script>

## Configuration via Environment

Circuit breaker parameters are highly environment-dependent — a staging environment tolerates more failures before tripping than production. Externalizing configuration prevents costly redeployments when tuning thresholds:

<script src="https://gist.github.com/mohashari/6e6a5ffd382e39bb314633706cba36a8.js?file=snippet-5.go"></script>

## Testing State Transitions

Circuit breakers are stateful, which makes them excellent candidates for table-driven tests. Each row exercises a specific sequence of outcomes and asserts the resulting state:

<script src="https://gist.github.com/mohashari/6e6a5ffd382e39bb314633706cba36a8.js?file=snippet-6.go"></script>

## Putting It All Together

The circuit breaker pattern is one of those rare abstractions that pays dividends far beyond the lines of code it requires. By building explicit failure budgets into your service call paths — rather than letting every retry storm propagate freely through your dependency graph — you trade unbounded latency for fast, predictable failure. The key operational insight is that an open circuit is not a bug to be fixed immediately; it's a signal that a downstream service needs breathing room. Pair your breakers with meaningful metrics, configure realistic thresholds per dependency criticality, and implement graceful degradation so users see a degraded experience rather than a hard error. In production, a well-tuned circuit breaker is often the difference between a 5-minute incident and a 45-minute outage.