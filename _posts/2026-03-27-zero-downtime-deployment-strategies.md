---
layout: post
title: "Zero-Downtime Deployments: Blue-Green, Canary, and Rolling Strategies"
date: 2026-03-27 07:00:00 +0700
tags: [devops, deployment, kubernetes, ci-cd, reliability]
description: "Master blue-green, canary, and rolling deployment patterns to ship code continuously without disrupting production traffic."
---

Every engineer has been there: a Friday afternoon deploy goes sideways, traffic spikes against a half-initialized service, and the on-call phone starts ringing. The root cause isn't bad code — it's a deployment strategy that treats production like a light switch. Flip it off, swap the binary, flip it back on. In the era of distributed systems and user expectations measured in milliseconds of availability, that approach is a liability. Zero-downtime deployment isn't a luxury reserved for FAANG-scale teams; it's an operational discipline that any backend engineer can implement with the right patterns and tooling.

## The Core Problem: State Transitions Under Live Traffic

Before diving into strategies, understand what makes deployments dangerous. At the moment of cutover, three things can go wrong simultaneously: in-flight requests hit an unready instance, database schema diverges from application expectations, and health checks lie because they test shallow readiness instead of deep dependency availability. Good deployment strategies solve all three, not just the first.

## Blue-Green Deployments

Blue-green runs two identical production environments — blue (live) and green (idle). You deploy to green, validate it fully, then shift 100% of traffic atomically. The old blue environment stays warm as an instant rollback target.

The traffic shift in Kubernetes is as simple as updating a Service selector. Here's a shell script that orchestrates the swap and validates readiness before committing:

<script src="https://gist.github.com/mohashari/b46a517c4464fbe891f667a00c215cea.js?file=snippet.sh"></script>

The deep health check endpoint that smoke test is hitting matters enormously. A shallow `/ping` that returns 200 without touching a database connection pool will lie to you. Here's a Go handler that validates real dependency readiness:

<script src="https://gist.github.com/mohashari/b46a517c4464fbe891f667a00c215cea.js?file=snippet-2.go"></script>

## Canary Deployments

Canary releases route a small percentage of real traffic to the new version before full rollout. This surfaces subtle bugs — memory leaks, p99 latency regressions, edge-case panics — that staging environments never catch because they lack production's traffic shape and data variety.

Kubernetes doesn't natively support weighted traffic splitting at the Service level, but you can approximate it with replica ratios. A more precise approach uses an Ingress controller with traffic weighting annotations:

<script src="https://gist.github.com/mohashari/b46a517c4464fbe891f667a00c215cea.js?file=snippet-3.yaml"></script>

This routes 5% of traffic to the canary, but also lets QA engineers force-route all their requests to the canary via the `X-Canary: always` header — a pattern that's invaluable for manual validation before widening the rollout.

During canary analysis, you need automated gates comparing error rates and latency percentiles between the canary and baseline. Here's a Go snippet that queries a Prometheus-compatible API to make that call:

<script src="https://gist.github.com/mohashari/b46a517c4464fbe891f667a00c215cea.js?file=snippet-4.go"></script>

## Rolling Deployments

Rolling updates replace instances one at a time, ensuring a mix of old and new versions handle traffic during the transition. Kubernetes' default deployment strategy is rolling, but its default settings are often too aggressive. This manifest shows a conservative configuration appropriate for a stateful API service:

<script src="https://gist.github.com/mohashari/b46a517c4464fbe891f667a00c215cea.js?file=snippet-5.yaml"></script>

The `preStop` sleep is often overlooked but critical: it gives the load balancer time to de-register the pod before the process actually starts shutting down, preventing a flood of connection-refused errors on in-flight requests.

## Database Migrations: The Hard Part

All three strategies share a common constraint: during the transition window, both old and new application versions run simultaneously against the same database. This makes destructive migrations — renaming columns, dropping tables, changing types — instant outage sources.

The expand-contract pattern solves this. Migrations happen in three separate deploys:

<script src="https://gist.github.com/mohashari/b46a517c4464fbe891f667a00c215cea.js?file=snippet-6.sql"></script>

## Putting It Together

Zero-downtime deployment is a stack of decisions: pick a traffic-shifting strategy based on your risk tolerance (blue-green for maximum rollback speed, canary for data-driven confidence, rolling for resource efficiency), instrument your health checks to test real dependencies, implement automated analysis gates before widening canary traffic, and treat database migrations as a first-class citizen that must be backward-compatible across at least one deploy cycle. None of these techniques is complex in isolation — the discipline is in composing them consistently, automating the validation steps so humans stop being the bottleneck, and building the organizational habit of shipping small changes frequently enough that any single deploy carries minimal blast radius.