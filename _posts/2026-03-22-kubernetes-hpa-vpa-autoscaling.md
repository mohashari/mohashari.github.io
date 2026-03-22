---
layout: post
title: "Kubernetes HPA and VPA: Autoscaling That Actually Works"
date: 2026-03-22 08:00:00 +0700
tags: [kubernetes, devops, backend, infrastructure, performance]
description: "A production engineer's guide to HPA and VPA — configuration patterns, failure modes, and how to combine both without shooting yourself in the foot."
---

Your pod is OOMKilled at 2 AM because traffic spiked and your resource requests were set by whoever wrote the initial Helm chart six months ago based on local load tests. Meanwhile, your on-call engineer is manually scaling deployments, your Slack is on fire, and your SLA is bleeding. This is the exact problem Kubernetes autoscaling is supposed to prevent — but most teams use HPA and VPA in ways that make the situation worse, not better. Misconfigured cooldown periods cause thrashing. VPA restarts pods mid-traffic. HPA scales on the wrong metric. This post covers how to configure both to actually work in production.

## Understanding What HPA and VPA Are Actually Doing

HPA (Horizontal Pod Autoscaler) adds or removes pod replicas based on observed metrics. VPA (Vertical Pod Autoscaler) adjusts CPU and memory requests/limits for existing pods. They solve different problems, and conflating them leads to chaos.

HPA is for traffic-driven workloads: APIs, consumers, batch workers — anything where the right response to load is "more instances." VPA is for resource calibration: it watches actual consumption and recommends (or sets) better request/limit values. Using HPA for a stateful service that can't scale horizontally, or using VPA auto mode on a critical deployment without understanding that it restarts pods, are the two most common ways teams get burned.

The third option is KEDA (Kubernetes Event-Driven Autoscaling), which extends HPA with external metric sources — SQS queue depth, Kafka consumer lag, Prometheus queries. For event-driven workloads, KEDA is almost always the right answer over raw HPA. But let's get the fundamentals right first.

## HPA: Beyond CPU Percentage

The default CPU-based HPA is fine for compute-bound workloads, but most backend services are I/O-bound. Scaling on CPU when your bottleneck is database connection pool exhaustion doesn't help. Here's a properly configured HPA using custom metrics:

<script src="https://gist.github.com/mohashari/3a64a93cd95ef183d3de17f4c60589c9.js?file=snippet-1.yaml"></script>

The `behavior` block is where most production configurations fall short. Without it, HPA uses defaults: 5-minute stabilization for scale-down, 3-minute for scale-up, and no rate limits. For a sudden 10x traffic spike that window is too slow. For scale-down, it's often too fast — you scale down aggressively and then spike again, creating a thrash cycle.

The config above scales up aggressively (30-second window, up to 50% increase per minute) but scales down conservatively (5-minute window, max 2 pods per 2 minutes). This matches the asymmetry of real traffic: spikes are sudden, lulls are gradual.

The `selectPolicy: Max` for scale-up means use whichever policy gives the larger number of pods. `selectPolicy: Min` for scale-down means use the most conservative policy. This is intentional.

## The Metric That Actually Matters: RPS per Pod

CPU utilization as a scaling metric has a fundamental problem: it measures what happened, not what's happening. By the time CPU spikes to 80%, you've already been serving degraded latency for the past 30-60 seconds. RPS (requests per second) per pod is a leading indicator — you can scale before saturation hits.

To expose RPS as a custom metric, you need the Prometheus adapter configured. Here's the adapter ConfigMap entry for an RPS metric from a service instrumented with the standard `http_requests_total` counter:

<script src="https://gist.github.com/mohashari/3a64a93cd95ef183d3de17f4c60589c9.js?file=snippet-2.yaml"></script>

This exposes `http_requests_per_second` as a pod-level metric that HPA can consume. The `[2m]` window smooths out micro-spikes while still being responsive. Use `[30s]` only if you have very steady traffic patterns and need faster reaction; shorter windows amplify noise.

## VPA: Use Recommendation Mode, Not Auto Mode

VPA has four modes: `Off`, `Initial`, `Recreate`, and `Auto`. In production, unless you have a very specific use case, you should be running `Off` mode (recommendations only) or `Initial` mode (apply on pod creation, not mid-life).

`Recreate` and `Auto` modes will evict and restart your pods to apply new resource values. This happens during active traffic. Yes, it respects PodDisruptionBudgets, but it's still a restart. For a service with 10 replicas rolling restart of 2 pods at a time, VPA-driven restarts on top of your normal deployment cycle is unnecessary operational complexity.

Here's the correct production VPA setup for getting recommendations without the restart risk:

<script src="https://gist.github.com/mohashari/3a64a93cd95ef183d3de17f4c60589c9.js?file=snippet-3.yaml"></script>

With `updateMode: "Off"`, VPA collects metrics and generates recommendations but never touches your pods. You query recommendations via `kubectl describe vpa api-service-vpa` and apply them through your normal Helm/GitOps workflow.

After two weeks of production traffic, check what VPA learned:

<script src="https://gist.github.com/mohashari/3a64a93cd95ef183d3de17f4c60589c9.js?file=snippet-4.sh"></script>

The `target` value is what VPA would set. The `uncappedTarget` is what VPA would set if your `maxAllowed` weren't capping it — if this is significantly higher than `target`, your `maxAllowed` boundary is too restrictive. The `lowerBound` is the minimum VPA thinks you need; if your current requests are well above this, you're over-provisioning.

## The HPA + VPA Conflict Problem

Running HPA and VPA simultaneously on the same deployment breaks things in a subtle way: VPA's recommendations are based on per-pod resource consumption, but if HPA is changing the number of pods, VPA's view of "normal" consumption shifts constantly. They fight each other.

The rules are:
1. **HPA on CPU/memory + VPA**: Don't. They will conflict. VPA increases memory requests, pods restart, HPA sees reduced capacity, scales up, VPA re-evaluates with more pods, changes recommendations again.
2. **HPA on custom metrics + VPA in Off/Initial mode**: Fine. HPA scales based on RPS or queue depth (not resource metrics), VPA calibrates the per-pod resource values independently.
3. **VPA only, no HPA**: Good for non-horizontally-scalable workloads. Run VPA in `Recreate` mode with a maintenance window, or use `Initial` mode and roll deployments periodically.

The cleanest production setup for a typical API service: HPA on RPS metric with conservative scale-down behavior, VPA in `Off` mode for quarterly resource tuning.

## KEDA for Event-Driven Workloads

For queue consumers, HPA's pull-based metric polling doesn't work well. You want to scale based on SQS queue depth or Kafka consumer lag — metrics that live outside the cluster. KEDA handles this natively:

<script src="https://gist.github.com/mohashari/3a64a93cd95ef183d3de17f4c60589c9.js?file=snippet-5.yaml"></script>

`queueLength: "10"` means KEDA targets 10 messages per pod replica. With 500 messages in the queue, you get 50 replicas (capped at `maxReplicaCount`). `cooldownPeriod: 120` prevents scale-down for 2 minutes after the last scaling event — important for bursty queues where the queue drains and refills rapidly.

The `identityOwner: operator` means KEDA uses the KEDA operator's IAM role via IRSA, not a pod-level role. This is the correct pattern for AWS; don't store SQS credentials as environment variables in the ScaledObject.

## Resource Request Sizing: The Foundation Everything Else Needs

Autoscaling only works correctly when your resource requests are accurate. HPA uses requests to calculate node capacity for scheduling. VPA exists specifically to fix bad requests. If your memory request is 256Mi but your pod actually uses 1.2Gi at P99, you will have OOMKills, bin-packing problems, and autoscaling decisions made on bad data.

The workflow for a new service:
1. Deploy with intentionally over-provisioned requests (2x your estimate)
2. Run VPA in `Off` mode for 2+ weeks under real traffic
3. Check VPA recommendations, cross-reference with your APM (Datadog, Grafana, whatever you have)
4. Update Helm values to match VPA target, apply through GitOps
5. Repeat quarterly or after any significant traffic pattern change

For CPU limits specifically: consider not setting them at all, or setting them very high. CPU limits cause throttling when the pod hits the limit even if node CPU is available. This is one of the most common sources of latency spikes that aren't caught by CPU utilization metrics. Memory limits should always be set — unbounded memory growth will kill your node.

## Debugging Autoscaling Decisions

When HPA isn't scaling and you expect it to:

<script src="https://gist.github.com/mohashari/3a64a93cd95ef183d3de17f4c60589c9.js?file=snippet-6.sh"></script>

Common failure modes:
- **`unable to get metrics`**: Prometheus adapter is misconfigured or the metric series doesn't exist yet. Check adapter logs.
- **`FailedGetScale`**: RBAC issue. The HPA controller needs `get` on the target resource.
- **Scaling correctly but pods pending**: Node autoscaler (Cluster Autoscaler or Karpenter) isn't provisioning nodes fast enough, or your pods have resource requests that no current node can satisfy.
- **Scaling down too aggressively**: Your `stabilizationWindowSeconds` for scale-down is too short. Default is 300s — if you've overridden it lower, put it back.

The HPA status conditions section in `kubectl describe` is your first stop. It will tell you exactly why scaling is or isn't happening, with human-readable condition messages that map directly to the controller code.

## Production Checklist

Before shipping any autoscaling configuration to production:

- Minumum replicas ≥ 2 (single pod minimum means zero redundancy during scale-up)
- PodDisruptionBudget defined with `minAvailable` or `maxUnavailable` that matches your SLA
- Resource requests based on observed data, not estimates
- Scale-down stabilization window ≥ 5 minutes for traffic-serving workloads
- HPA not scaling on CPU/memory if VPA is also running
- VPA `maxAllowed` set to something sane — uncapped VPA can recommend 16 CPU for a pod that had one traffic spike
- Custom metrics tested manually via `kubectl get --raw` before trusting HPA to use them
- Alert on HPA `ScalingLimited` condition — it means you're hitting `maxReplicas` and your ceiling is too low

Autoscaling is not a "set it and forget it" system. Traffic patterns change, services get new dependencies, infrastructure costs make you revisit `maxReplicas`. Treat your HPA and VPA configs with the same rigor as your deployment manifests — they're just as critical to production stability.
```