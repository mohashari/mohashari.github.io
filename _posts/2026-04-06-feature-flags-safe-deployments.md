---
layout: post
title: "Feature Flags: Safe Deployments and Experimentation at Scale"
date: 2026-04-06 07:00:00 +0700
tags: [feature-flags, devops, backend, architecture, ci-cd]
description: "Implement a feature flag system to decouple deployments from releases and run controlled experiments safely in production."
---

Shipping code and releasing features are two different things — but most teams treat them as the same event. Every deploy becomes a moment of anxiety: will the new checkout flow break for mobile users? Will the new rate limiter cascade under load? The result is big-batch releases, long staging cycles, and engineers who dread Fridays. Feature flags break this coupling entirely. By wrapping new behavior in a conditional check, you can deploy code continuously while controlling who sees what, when — and roll back in seconds without reverting a single commit.

## What a Feature Flag System Actually Needs

Before reaching for a managed service like LaunchDarkly, it is worth understanding the core primitives. A production-grade flag system needs: a flag store (database or config), an evaluation engine, a targeting model (who gets which variant), and an SDK that makes flag checks fast enough to happen on every request. Latency is the killer here. Flag evaluation that adds 50ms to every API call is a system that gets removed after the first incident review.

The simplest useful store is a PostgreSQL table. Keep it small and indexed — you will be reading it on every request.

<script src="https://gist.github.com/mohashari/2f96d35ffd1ce5e95373a77c8c0df9b5.js?file=snippet.sql"></script>

## The Evaluation Engine

The evaluation engine is the heart of the system. It takes a flag key and an evaluation context — user ID, region, plan tier, anything — and returns a variant. The logic is deterministic: given the same inputs, it always returns the same output. This matters for user experience consistency and for debugging.

<script src="https://gist.github.com/mohashari/2f96d35ffd1ce5e95373a77c8c0df9b5.js?file=snippet-2.go"></script>

The `userBucket` function is the critical piece. By hashing the flag key together with the user ID, you get independent, stable bucket assignments per flag. A user in the 10% rollout for flag A is not necessarily in the 10% rollout for flag B — which matters when you are running multiple experiments simultaneously and do not want correlated samples.

## Caching for Zero-Latency Checks

Reading from PostgreSQL on every flag check is not viable at scale. The standard approach is a two-layer cache: an in-process in-memory cache with a short TTL, backed by a Redis cache, backed by the database. For most services, a 30-second in-process TTL is acceptable — you can always force an immediate refresh by publishing to a pub/sub channel on flag changes.

<script src="https://gist.github.com/mohashari/2f96d35ffd1ce5e95373a77c8c0df9b5.js?file=snippet-3.go"></script>

## Instrumenting Flag Usage

A flag nobody can observe is a maintenance liability. Every flag check should emit a metric. Over time, you use this data to find stale flags (100% enabled for 90 days — just remove the branch), detect unexpected distribution skew, and power experiment analysis dashboards.

<script src="https://gist.github.com/mohashari/2f96d35ffd1ce5e95373a77c8c0df9b5.js?file=snippet-4.go"></script>

With this in place, a Prometheus query like `rate(feature_flag_evaluations_total{flag_key="new_checkout"}[5m])` gives you real-time traffic split across variants — immediately useful for validating that a 10% rollout is actually reaching 10% of traffic.

## Wiring It Into HTTP Middleware

In a typical API service, you want the evaluation context assembled once per request and made available to all downstream handlers. Middleware is the right place for this — it keeps flag checks out of business logic and makes the context easy to inject in tests.

<script src="https://gist.github.com/mohashari/2f96d35ffd1ce5e95373a77c8c0df9b5.js?file=snippet-5.go"></script>

A handler then reads simply as `if middleware.IsEnabled(r.Context(), "new_checkout_flow") { ... }` — no flag client dependency to inject manually into every service struct.

## Flag Lifecycle Management

Flags accumulate fast. Without discipline, a codebase becomes a graveyard of conditionals nobody dares remove. The fix is treating flag lifecycle as a first-class concern in your CI pipeline. A linting step that fails the build when a flag has been at 100% rollout for more than 30 days forces the conversation about cleanup.

<script src="https://gist.github.com/mohashari/2f96d35ffd1ce5e95373a77c8c0df9b5.js?file=snippet-6.yaml"></script>

Feature flags are not a silver bullet — they add indirection, they need cache invalidation, and they make code harder to read when overused. But the ability to separate deployment from release fundamentally changes your risk model. A new feature can be in production, battle-tested under real load, weeks before any user sees it. Rollouts become dials, not switches. Incidents become rollbacks measured in seconds, not the panicked revert-and-redeploy drill that ends careers. Build the flag system small, instrument it thoroughly, enforce flag hygiene in CI, and you will find yourself shipping faster precisely because you are less afraid of what happens after the merge.