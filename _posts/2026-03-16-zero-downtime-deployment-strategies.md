---
layout: post
title: "Zero-Downtime Deployments: Blue-Green, Canary, and Rolling Strategies Compared"
date: 2026-03-16 07:00:00 +0700
tags: [devops, deployment, kubernetes, reliability, backend]
description: "A practical comparison of blue-green, canary, and rolling deployment strategies for shipping backend changes without service interruption."
---

Every backend engineer has felt that stomach-drop moment: a deployment goes out, a health check silently fails, and suddenly half your users are hitting 502s while you scramble to roll back. The business loses revenue, on-call engineers lose sleep, and trust in the deployment pipeline erodes a little more. Zero-downtime deployment isn't a luxury reserved for hyperscalers — it's a discipline built from choosing the right strategy for your workload, your team's risk tolerance, and your infrastructure constraints. Blue-green, canary, and rolling deployments each solve the same problem from a different angle, and understanding those angles is the difference between a Friday afternoon deploy that lands clean and one that ruins your weekend.

## The Core Problem: State, Traffic, and Time

Before comparing strategies, it helps to name what we're actually solving. A deployment event has three moving parts: the old version of your binary, the new version, and the live traffic flowing between them. The goal is to transition traffic from old to new without any request seeing an error it wouldn't have seen otherwise. Complications arise from database schema changes, in-flight requests, sticky sessions, and the simple reality that the new binary might be broken in ways your staging environment never revealed.

## Blue-Green Deployments

Blue-green maintains two identical production environments — "blue" (current) and "green" (new). You deploy to green while blue serves all traffic, run smoke tests and synthetic checks against green, then flip the load balancer. If green breaks, you flip back in seconds.

The traffic switch is typically done at the load balancer or DNS level. Here's a simple shell script that swaps an AWS target group behind an Application Load Balancer:

<script src="https://gist.github.com/mohashari/d3acadb1795f1edd20b2063bb6d205a2.js?file=snippet.sh"></script>

The major advantage is instant, full rollback. The cost is doubled infrastructure for the duration of the deploy. For stateful services, you also need to handle session affinity — requests in flight on blue shouldn't suddenly land on green mid-transaction.

Blue-green pairs naturally with database migration strategies that decouple schema changes from application deploys. The expand-contract pattern (add the new column, deploy, backfill, remove the old column) is essential here:

<script src="https://gist.github.com/mohashari/d3acadb1795f1edd20b2063bb6d205a2.js?file=snippet-2.sql"></script>

## Canary Deployments

Canary releases route a small percentage of real traffic to the new version before promoting it fully. The name comes from the coal-mining practice of using canaries to detect toxic gas — your canary deployment detects toxic bugs before they spread to all users.

In Kubernetes, you can implement a simple canary using two Deployments behind a single Service, controlling the ratio via replica counts:

<script src="https://gist.github.com/mohashari/d3acadb1795f1edd20b2063bb6d205a2.js?file=snippet-3.yaml"></script>

More sophisticated canary logic belongs in a service mesh or ingress controller. Here's an NGINX ingress annotation that sends exactly 10% of traffic to the canary regardless of replica count:

<script src="https://gist.github.com/mohashari/d3acadb1795f1edd20b2063bb6d205a2.js?file=snippet-4.yaml"></script>

The power of canary deployments is that you can gate promotion on real metrics. A Go health-check endpoint that your canary promotion script polls before widening traffic:

<script src="https://gist.github.com/mohashari/d3acadb1795f1edd20b2063bb6d205a2.js?file=snippet-5.go"></script>

## Rolling Deployments

Rolling deployments replace instances one-by-one (or in small batches), with the scheduler waiting for each new instance to pass its readiness probe before terminating an old one. Kubernetes does this natively via `RollingUpdate` strategy:

<script src="https://gist.github.com/mohashari/d3acadb1795f1edd20b2063bb6d205a2.js?file=snippet-6.yaml"></script>

The `preStop` sleep is critical and frequently omitted. Without it, Kubernetes removes the pod from the Service endpoints and sends SIGTERM simultaneously — requests already routed to the pod fail. The sleep gives the load balancer time to stop sending new connections before the process exits.

Rolling updates are the lowest-overhead strategy — no doubled infrastructure, no separate traffic routing layer. The trade-off is that during the rollout window, both versions serve traffic concurrently. Your API must be backward-compatible for the duration: new code reading old data formats, old clients calling new endpoints.

## Choosing Your Strategy

The right strategy depends on your blast radius tolerance. **Blue-green** is ideal when you need instant, full rollback and can afford the infrastructure cost — database migrations, major API changes, or any deploy where partial rollout creates correctness problems. **Canary** is best when you want empirical validation with real traffic before committing, particularly for changes that affect conversion, latency, or error rates in subtle ways you can't catch in tests. **Rolling** is the pragmatic default for most stateless services where backward compatibility is maintained and you want simplicity over surgical control.

In practice, most mature teams layer these: rolling updates for routine deploys, canary for anything touching critical paths, and blue-green reserved for breaking changes or database migrations. The common thread across all three is that zero-downtime is not free — it requires disciplined readiness probes, graceful shutdown handling, backward-compatible schema changes, and metrics gates that actually reflect user experience. Get those primitives right, and the strategy you choose becomes almost secondary.