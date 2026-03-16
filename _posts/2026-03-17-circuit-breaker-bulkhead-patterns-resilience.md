---
layout: post
title: "Circuit Breaker and Bulkhead Patterns: Resilience in Distributed Systems"
date: 2026-03-17 07:00:00 +0700
tags: [resilience, distributed-systems, microservices, patterns, fault-tolerance]
description: "Implement circuit breakers and bulkheads to isolate failures, prevent cascade outages, and keep your distributed system operational under partial degradation."
---

Your payment service calls an inventory service, which calls a pricing service, which calls a third-party tax API that happens to be having a bad day. One slow external dependency cascades into thread pool exhaustion, timeouts ripple upstream, and suddenly your entire checkout flow is down — not because your code is broken, but because you had no way to contain the blast radius of a single failing component. This is the fundamental fragility of distributed systems: tight coupling means one struggling dependency can drag everything else down with it. Circuit breakers and bulkheads are the two most effective patterns for building systems that degrade gracefully instead of collapsing completely.

## The Circuit Breaker Pattern

The circuit breaker is borrowed from electrical engineering. In a circuit, a breaker trips when current exceeds a safe threshold — it stops the flow before things overheat. In software, a circuit breaker monitors calls to a downstream dependency and, when failures exceed a configured threshold, it "trips" and stops sending requests to that dependency entirely. Instead of waiting for timeouts that take 30 seconds each, it fails fast and returns an error (or a cached fallback) immediately.

A circuit breaker has three states. **Closed** is normal operation — requests flow through and failures are counted. **Open** means the breaker has tripped — all requests fail immediately without hitting the downstream service. **Half-open** is a probe state — after a cooldown period, a single request is allowed through to test if the dependency has recovered. If it succeeds, the breaker closes; if it fails, it opens again.

Here is a minimal circuit breaker implementation in Go that demonstrates the state machine:

<script src="https://gist.github.com/mohashari/33fa6d485421e47069b644ef9a244023.js?file=snippet.go"></script>

Notice that the mutex wraps the entire state check and function call together. This is deliberate — you cannot afford a race between reading the state and executing the function, or two goroutines could both slip through a half-open breaker simultaneously and flip it back open on a fluke.

In production, you typically want request-level metrics rather than a simple counter, because a single slow period shouldn't permanently damage your failure rate. A sliding window over the last N requests or the last N seconds is more robust:

<script src="https://gist.github.com/mohashari/33fa6d485421e47069b644ef9a244023.js?file=snippet-2.go"></script>

This sliding window approach means a burst of failures five minutes ago won't hold the circuit open indefinitely once the dependency recovers.

## The Bulkhead Pattern

While a circuit breaker protects a specific dependency, a bulkhead isolates entire resource pools so that pressure in one area cannot starve another. The name comes from the watertight compartments in a ship's hull — if one compartment floods, the others remain sealed and the ship stays afloat.

In practice, bulkheads in software mean separate thread pools, connection pools, or goroutine semaphores for different downstream services. If your inventory service starts accepting requests slowly and your shared HTTP client pool fills up waiting for it, your payment and user service calls queue behind it and eventually time out too. Dedicated pools prevent this.

Here is how to implement a semaphore-based bulkhead in Go using a buffered channel as a counting semaphore:

<script src="https://gist.github.com/mohashari/33fa6d485421e47069b644ef9a244023.js?file=snippet-3.go"></script>

The `default` branch in the first `select` is crucial — it makes the bulkhead non-blocking on acquisition. A blocking acquire would defeat the purpose entirely: you'd be queueing up goroutines waiting for a slot, which is the same unbounded growth you're trying to prevent.

## Combining Both Patterns

Circuit breakers and bulkheads are complementary. The bulkhead limits how many concurrent calls you send to a dependency; the circuit breaker stops sending calls once failures cross a threshold. Together, they give you both concurrency control and failure isolation:

<script src="https://gist.github.com/mohashari/33fa6d485421e47069b644ef9a244023.js?file=snippet-4.go"></script>

The bulkhead wraps the circuit breaker wraps the actual call. Rejections from the bulkhead do not count as failures in the circuit breaker — the downstream service never saw that request, so it shouldn't influence your view of that service's health. This ordering matters.

## Observability Is Not Optional

Neither pattern is useful without visibility. You need to know how often the circuit is tripping, how full your bulkheads are running, and what the failure rates look like over time. Expose these as metrics your monitoring system can scrape:

<script src="https://gist.github.com/mohashari/33fa6d485421e47069b644ef9a244023.js?file=snippet-5.go"></script>

Alert on `circuit_breaker_state > 0` for more than two minutes on any critical service. That's your signal that something is wrong and your system is compensating — not necessarily an outage, but definitely a conversation worth having.

## Configuration as Code

Hardcoded thresholds are a maintenance problem. Externalize them so you can tune without redeploying:

<script src="https://gist.github.com/mohashari/33fa6d485421e47069b644ef9a244023.js?file=snippet-6.yaml"></script>

Different services warrant different tolerances. Payment is business-critical and intolerant of flakiness, so its threshold is lower and its cooldown longer. An internal inventory check can be more forgiving.

Circuit breakers and bulkheads will not make your dependencies more reliable — but they will make their unreliability survivable. The goal is a system that communicates degraded state clearly, shedding load gracefully rather than absorbing it until collapse. Start by identifying your most critical downstream dependencies, wrap them with both patterns, instrument the state transitions, and set meaningful alerts. The first time a third-party API has an incident and your dashboards show the breaker tripping cleanly while everything else keeps running, the investment pays for itself.