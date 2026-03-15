---
layout: post
title: "Chaos Engineering: Building Confidence Through Controlled Failure"
date: 2026-03-15 07:00:00 +0700
tags: [chaos-engineering, reliability, sre, distributed-systems, resilience]
description: "Design and run chaos experiments to proactively expose weaknesses in your distributed system before they become production incidents."
---

Production systems don't fail on schedule. They fail at 2 AM during a holiday weekend, under a load pattern you didn't anticipate, after a dependency you forgot about silently degrades. Traditional testing validates that your system works under expected conditions — chaos engineering asks the harder question: does your system *survive* when conditions are not expected? The discipline, pioneered at Netflix and now practiced across the industry, is not about breaking things for sport. It is about building confidence by designing controlled, hypothesis-driven experiments that expose real weaknesses before your users do. If you have never deliberately injected failure into your production-adjacent environment, you don't actually know how your system behaves under failure — you only believe you do.

## The Hypothesis-First Mindset

Chaos engineering is science, not vandalism. Every experiment begins with a falsifiable hypothesis: "Given normal traffic, when the payment service latency increases to 500ms, the checkout flow should degrade gracefully and complete within 2 seconds via the async fallback path." You define steady-state behavior using measurable signals — p99 latency, error rate, queue depth — before injecting any fault. If your system holds steady state, your hypothesis is confirmed and you gain confidence. If it doesn't, you found a real bug before it found your users.

## Defining Steady State

Before running any experiment, instrument your service so you can observe steady state quantitatively. The following Go snippet shows a minimal health metrics server that exposes the signals you will watch during an experiment.

<script src="https://gist.github.com/mohashari/f7d0a79cabca78332fd63354463fb13a.js?file=snippet.go"></script>

## Fault Injection at the Network Layer

The most realistic chaos experiments happen at the network layer, where real failures occur. Linux Traffic Control (`tc`) lets you inject latency, packet loss, and corruption without touching application code — exactly the kind of failure a flaky cloud link or a saturated downstream service produces.

<script src="https://gist.github.com/mohashari/f7d0a79cabca78332fd63354463fb13a.js?file=snippet-2.sh"></script>

Run this against a staging host while your load generator runs and watch whether your circuit breaker opens, whether retries cause a thundering herd, and whether your timeouts are calibrated correctly.

## Circuit Breakers in Go

Network-level faults only expose weaknesses if your application code handles them. A circuit breaker is the canonical resilience pattern: after a threshold of failures, stop trying immediately and fail fast, giving the downstream service time to recover.

<script src="https://gist.github.com/mohashari/f7d0a79cabca78332fd63354463fb13a.js?file=snippet-3.go"></script>

## Experiment Automation with a YAML Manifest

Ad-hoc chaos is chaos. Repeatable chaos is engineering. Define experiments declaratively so they can be reviewed, version-controlled, and run in CI against a staging environment.

<script src="https://gist.github.com/mohashari/f7d0a79cabca78332fd63354463fb13a.js?file=snippet-4.yaml"></script>

## Querying Failure Impact in PostgreSQL

After an experiment run, correlate your chaos window against application-level metrics stored in your time-series or OLAP store. This query finds order failure rates during a specific chaos window versus the hour before.

<script src="https://gist.github.com/mohashari/f7d0a79cabca78332fd63354463fb13a.js?file=snippet-5.sql"></script>

## Containerizing the Chaos Agent

Running fault injection as a sidecar container keeps the chaos tooling isolated, auditable, and easy to terminate without touching your application pods.

<script src="https://gist.github.com/mohashari/f7d0a79cabca78332fd63354463fb13a.js?file=snippet-6.dockerfile"></script>

## Runbook Integration

Chaos experiments should live alongside your runbooks. When an experiment reveals that a circuit breaker wasn't configured, write the fix *and* add the experiment to your CI pipeline so the regression never reappears silently.

<script src="https://gist.github.com/mohashari/f7d0a79cabca78332fd63354463fb13a.js?file=snippet-7.sh"></script>

## The Path Forward

Chaos engineering is not a one-time audit — it is a practice that matures alongside your system. Start small: pick one dependency your service calls, write a hypothesis, measure steady state, inject one failure mode, observe. Automate the experiment and run it on every deploy. Expand the blast radius incrementally as your confidence — and your tooling — grows. The goal is never to prove your system is perfect. It is to replace the dangerous assumption of resilience with the hard-won evidence of it. Engineers who practice chaos consistently stop being surprised by production incidents, because they have already seen most of them in a controlled environment where they could observe, learn, and fix.