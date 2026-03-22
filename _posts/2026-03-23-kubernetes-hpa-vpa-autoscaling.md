---
layout: post
title: "Kubernetes HPA and VPA: Autoscaling That Actually Works"
date: 2026-03-23 08:00:00 +0700
tags: [kubernetes, infrastructure, devops, backend, scaling]
description: "Cut through HPA/VPA defaults with custom metrics, stabilization windows, and a concrete strategy for running both without conflicts."
---

Your service gets 10x traffic during a flash sale, HPA kicks in, but the new pods die immediately because their memory requests are 128Mi while the actual working set is 600Mi. Or the opposite: you've got 40 replicas serving 20 RPS because the cooldown window is too aggressive and scaling-down never completes before the next scale-up event. Both are symptoms of the same problem—autoscaling configured at the surface level, not tuned to how your workload actually behaves.

This post walks through HPA and VPA beyond the defaults: custom metrics that reflect real load, stabilization windows that prevent thrash, VPA's role in keeping resource requests honest, and the specific interaction bugs you'll hit when running both simultaneously.

## Why CPU Utilization Is a Terrible Default Scaling Signal

HPA's default metric—CPU utilization at 50%—was chosen because it's universally available, not because it's meaningful. For I/O-bound services (most backend APIs), CPU sits at 5% while the request queue is 200ms deep and users are screaming. For CPU-bound batch processors, 50% utilization might mean the pods are perfectly healthy.

The fundamental issue is that CPU utilization measures *resource consumption*, not *service pressure*. What you actually want to scale on is *demand*: requests per second, queue depth, active connections, or p99 latency. These signals tell you when to add capacity before users feel it.

Here's the standard CPU-only HPA that teams deploy and never revisit:

```yaml
# snippet-1
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: api-service
  namespace: production
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: api-service
  minReplicas: 3
  maxReplicas: 50
  metrics:
  - type: Resource
    resource:
      name: cpu
      target:
        type: Utilization
        averageUtilization: 60
  behavior:
    scaleDown:
      stabilizationWindowSeconds: 300
      policies:
      - type: Percent
        value: 10
        periodSeconds: 60
    scaleUp:
      stabilizationWindowSeconds: 0
      policies:
      - type: Percent
        value: 100
        periodSeconds: 15
      - type: Pods
        value: 4
        periodSeconds: 15
      selectPolicy: Max
```

The `behavior` block here is doing real work: aggressive scale-up (max of 100% or 4 pods every 15 seconds, whichever adds more), conservative scale-down (10% per minute with a 5-minute stabilization window). This prevents the thrash pattern where traffic spikes cause rapid scale-up, brief lull triggers scale-down, next spike hits underprovisioned, repeat.

## Custom Metrics: Scaling on What Actually Matters

The `autoscaling/v2` API supports three metric sources: Resource (CPU/memory), Pods (custom per-pod metrics), and External (metrics from outside the cluster). The practical path for most teams is exposing custom metrics through Prometheus and the [kube-state-metrics](https://github.com/kubernetes/kube-state-metrics) + [prometheus-adapter](https://github.com/kubernetes-sigs/prometheus-adapter) stack.

First, instrument your application to expose the right signals. For a Go HTTP service:

<script src="https://gist.github.com/mohashari/22bcbcbb402b70f08cc3772223da1fc1.js?file=snippet-2.go"></script>

Then configure prometheus-adapter to expose these as Kubernetes custom metrics:

```yaml
# snippet-3
# prometheus-adapter ConfigMap
apiVersion: v1
kind: ConfigMap
metadata:
  name: adapter-config
  namespace: monitoring
data:
  config.yaml: |
    rules:
    - seriesQuery: 'http_requests_active{namespace!="",pod!=""}'
      resources:
        overrides:
          namespace: {resource: "namespace"}
          pod: {resource: "pod"}
      name:
        matches: "^(.*)$"
        as: "${1}"
      metricsQuery: 'avg_over_time(http_requests_active{<<.LabelMatchers>>}[2m])'
    
    - seriesQuery: 'worker_queue_depth{namespace!="",pod!=""}'
      resources:
        overrides:
          namespace: {resource: "namespace"}
          pod: {resource: "pod"}
      name:
        matches: "^(.*)$"
        as: "${1}"
      metricsQuery: 'sum(worker_queue_depth{<<.LabelMatchers>>}) by (pod, namespace)'
```

Now your HPA can target actual load:

```yaml
# snippet-4
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: api-service
  namespace: production
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: api-service
  minReplicas: 3
  maxReplicas: 50
  metrics:
  - type: Resource
    resource:
      name: cpu
      target:
        type: Utilization
        averageUtilization: 70
  - type: Pods
    pods:
      metric:
        name: http_requests_active
      target:
        type: AverageValue
        averageValue: "25"  # scale up when avg active requests per pod exceeds 25
  - type: Pods
    pods:
      metric:
        name: worker_queue_depth
      target:
        type: AverageValue
        averageValue: "100"
  behavior:
    scaleDown:
      stabilizationWindowSeconds: 300
      policies:
      - type: Percent
        value: 10
        periodSeconds: 60
    scaleUp:
      stabilizationWindowSeconds: 0
      policies:
      - type: Percent
        value: 100
        periodSeconds: 15
      - type: Pods
        value: 5
        periodSeconds: 15
      selectPolicy: Max
```

HPA evaluates all metrics independently and scales to the *maximum* replica count suggested by any single metric. If CPU says 10 replicas but queue depth says 20, you get 20. This is the correct behavior for safety—you always want to satisfy the most constrained resource.

## VPA: The Right Tool for Right-Sizing

Vertical Pod Autoscaler solves a different problem. HPA adds capacity by adding pods. VPA adds capacity by making pods bigger. But its real value in most production systems isn't automated vertical scaling—it's *recommendations*.

VPA in `Off` mode (or `Initial` mode) watches your pods, runs OOM and CPU analysis over time, and tells you what your resource requests should actually be. This is invaluable because most teams set resource requests based on vibes during initial deployment and never revisit them.

```yaml
# snippet-5
apiVersion: autoscaling.k8s.io/v1
kind: VerticalPodAutoscaler
metadata:
  name: api-service-vpa
  namespace: production
spec:
  targetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: api-service
  updatePolicy:
    updateMode: "Off"  # Recommendations only — don't auto-mutate pods
  resourcePolicy:
    containerPolicies:
    - containerName: api-service
      minAllowed:
        cpu: 100m
        memory: 128Mi
      maxAllowed:
        cpu: 4
        memory: 4Gi
      controlledResources:
      - cpu
      - memory
      controlledValues: RequestsAndLimits
```

After running for a few days under real traffic, check the recommendations:

```bash
# snippet-6
# Get VPA recommendations
kubectl get vpa api-service-vpa -n production -o jsonpath='{.status.recommendation}' | jq .

# Example output:
# {
#   "containerRecommendations": [
#     {
#       "containerName": "api-service",
#       "lowerBound": {"cpu": "412m", "memory": "512Mi"},
#       "target": {"cpu": "680m", "memory": "768Mi"},
#       "uncappedTarget": {"cpu": "680m", "memory": "768Mi"},
#       "upperBound": {"cpu": "1200m", "memory": "1200Mi"}
#     }
#   ]
# }

# Update your deployment's resource requests based on VPA target
kubectl set resources deployment api-service \
  -n production \
  --containers=api-service \
  --requests=cpu=680m,memory=768Mi \
  --limits=cpu=2,memory=1536Mi
```

The `target` recommendation is where VPA thinks you should be. The `lowerBound` and `upperBound` give you a confidence interval. For memory, I set requests to `target` and limits to `upperBound * 1.5` to give headroom. For CPU, limits should be uncapped or set very high—CPU throttling is silent and destroys latency in ways that are hard to debug.

## The HPA + VPA Conflict Problem

Running HPA and VPA simultaneously is officially supported but genuinely broken in `Auto` mode for VPA. Here's what happens: HPA scales out to 20 pods under load, VPA simultaneously tries to adjust the pod spec and triggers rolling restarts, which temporarily drops capacity, which makes HPA add more pods, which VPA then wants to restart. You end up with a slow-motion thundering herd of your own making.

The rules:
1. **Never run VPA in `Auto` or `Recreate` mode alongside HPA on the same deployment.** Use `Off` for observation, `Initial` for setting requests on new pods only.
2. **If you must use VPA Auto, disable HPA's CPU metric.** VPA Auto adjusting CPU requests while HPA scales on CPU utilization creates circular feedback.
3. **VPA `Initial` mode is the sweet spot.** New pods get correct sizing, existing pods aren't restarted mid-traffic, HPA controls replica count unimpeded.

The recommended production setup:

```yaml
# snippet-7
# VPA in Initial mode — sets requests on pod creation, never restarts existing pods
apiVersion: autoscaling.k8s.io/v1
kind: VerticalPodAutoscaler
metadata:
  name: api-service-vpa
  namespace: production
spec:
  targetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: api-service
  updatePolicy:
    updateMode: "Initial"
  resourcePolicy:
    containerPolicies:
    - containerName: api-service
      minAllowed:
        cpu: 250m
        memory: 256Mi
      maxAllowed:
        cpu: 2
        memory: 2Gi
      controlledValues: RequestsOnly  # Don't touch limits
---
# HPA scales replica count based on custom metrics, not CPU
# (CPU-based HPA + VPA adjusting CPU requests = conflict)
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: api-service-hpa
  namespace: production
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: api-service
  minReplicas: 3
  maxReplicas: 50
  metrics:
  - type: Pods
    pods:
      metric:
        name: http_requests_active
      target:
        type: AverageValue
        averageValue: "25"
  behavior:
    scaleDown:
      stabilizationWindowSeconds: 300
      policies:
      - type: Percent
        value: 10
        periodSeconds: 60
    scaleUp:
      stabilizationWindowSeconds: 30
      policies:
      - type: Percent
        value: 100
        periodSeconds: 15
      - type: Pods
        value: 5
        periodSeconds: 15
      selectPolicy: Max
```

## Scaling Stabilization: Preventing Thrash

The `behavior` block deserves more attention than it gets. The default stabilization window for scale-down in HPA is 300 seconds, which is why your replicas stay high for 5 minutes after traffic drops. This is intentional—it prevents scaling down into a traffic spike that's just in a brief valley.

For scale-up, the default `stabilizationWindowSeconds` is 0, meaning HPA acts on the first sample showing elevated metrics. This is usually right for user-facing services. For batch processing or background workers where you don't need to react to single spikes, add a 30-60 second window to avoid scaling up for transient bursts.

The `policies` array with `selectPolicy: Max` is critical for scale-up: it means "use whichever policy would add the most replicas." The dual policy (100% increase OR 5 pods, whichever is more) means a service at 3 replicas can jump to 6 immediately, while a service at 40 replicas adds 5 per period rather than doubling. This gives you fast scale-up at low replica counts and controlled growth at high replica counts.

For scale-down, `selectPolicy: Min` (the default when you specify multiple policies) is usually correct—you want the most conservative scale-down to avoid removing too much capacity at once.

## KEDA: When You've Exhausted HPA's Native Metrics

If your scaling signal lives outside of Prometheus—Kafka lag, SQS queue depth, Redis list length, database connection pool saturation—[KEDA](https://keda.sh/) is the right tool. KEDA replaces the HPA controller for your workload and supports 50+ scalers out of the box.

The operational model is the same: target replicas are computed from metric value / target per replica. The difference is KEDA can also scale to zero, which matters for event-driven workers that should have zero cost when idle.

The tradeoff: KEDA is another operator to run, version, and operate. For teams already invested in Prometheus and prometheus-adapter, extending HPA with custom metrics is simpler. For teams with significant event-driven workloads or multi-cloud metric sources, KEDA pays for itself.

## Making It Production-Ready

A few operational details that bite teams after deployment:

**minReplicas should reflect your failure tolerance, not your cost target.** If a single pod handles your load at 3 AM, you're one pod eviction away from a brief outage. Three replicas spread across zones is the minimum meaningful configuration for anything user-facing.

**HPA scaling decisions are sampled every 15 seconds by default.** The `--horizontal-pod-autoscaler-sync-period` flag on kube-controller-manager controls this. For services with very spiky traffic (payment processing, auth flows), consider whether 15 seconds is fast enough before reaching for KEDA.

**Watch for the "forbidden" scaling zones.** HPA won't scale below `minReplicas` or above `maxReplicas`, but it also won't scale at all if the metrics server is unavailable. Set up alerting on `kube_horizontalpodautoscaler_status_condition` for `ScalingActive=false` to catch metric pipeline failures before they become capacity incidents.

**VPA recommendation drift is real.** A VPA in `Off` mode trained on your Black Friday traffic patterns will recommend very different requests than one trained on typical Tuesday traffic. Check recommendations quarterly and after any significant traffic pattern changes. The VPA history window defaults to 8 days, which is usually too short for services with weekly seasonality.

The goal isn't perfect autoscaling—it's autoscaling that fails predictably and degrades gracefully. CPU as the sole HPA signal, untuned stabilization windows, and unverified resource requests are the three things that make autoscaling feel like it doesn't work. Fix those, add one meaningful custom metric, and the system starts behaving like infrastructure instead of a liability.
```