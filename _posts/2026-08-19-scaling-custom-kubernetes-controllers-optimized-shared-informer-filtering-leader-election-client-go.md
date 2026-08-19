---
layout: post
title: "Scaling Custom Kubernetes Controllers: Implementing Optimized Shared Informer Filtering and Leader Election with client-go"
date: 2026-08-19 08:00:00 +0700
tags: [kubernetes, go, cncf]
description: "Learn how to scale client-go controllers by reducing memory footprints with filtered and metadata-only informers, and establishing resilient leader election leases."
image: "https://picsum.photos/seed/7999/1080/720"
thumbnail: "https://picsum.photos/seed/7999/400/300"
---

Imagine a production Kubernetes cluster with 15,000 namespaces, containing over 150,000 active services, secrets, and custom resources. Your custom Kubernetes controller runs smoothly in staging, but within minutes of being deployed to this scale in production, its memory usage spikes exponentially. The Pod is hit with a SIGKILL (OOM-Killed) by the kernel after crossing its 4Gi memory limit. It restarts, triggers a massive `LIST` request against the API server, hits the `429 Too Many Requests` API server rate limits, and throws the control plane into a cascade of timeouts. This is a classic failure mode for naive client-go controller implementations. Developers frequently underestimate the cost of the client-go cache. By default, a `SharedInformerFactory` constructs a reflector that performs a `LIST` and `WATCH` on all resources of a given type across the entire cluster, caching every field of every object in memory.

To scale controllers in massive environments, you must adopt two pillars of high-scale controller architecture: optimized, filtered SharedInformers to slash the memory footprint, and highly resilient, fail-safe leader election to guarantee high availability without split-brain reconciliation loops.

## Under the Hood: Why client-go Caches Everything by Default

To understand why your controller is consuming gigabytes of memory, you must look at how client-go's caching machinery functions. The `Reflector` runs a `LIST` query to retrieve the current state of a resource and then starts a long-polling HTTP `WATCH` connection to receive stream deltas (additions, updates, deletions). These objects are deserialized from raw JSON bytes into concrete Go structs (such as `corev1.Pod` or custom CRD structs).

These Go structs are stored in an in-memory cache managed by `cache.Indexer`. The memory cost is not just the raw payload size; the overhead of pointer-heavy Go structs, Go runtime garbage collector tracking metadata, and the index keys themselves leads to a massive memory footprint expansion factor (often 5x to 10x the serialized JSON size). If your controller only needs to reconcile 50 specific resources matching a particular label out of 100,000 total resources, caching the remaining 99,950 resources is a waste of memory and bandwidth.

## Level 1: Filtering Shared Informers at the Factory Level

The first line of defense is filtering the stream of objects before they ever hit the client-go deserialization pipeline and indexer cache. The `client-go/informers` package provides a powerful option through `NewSharedInformerFactoryWithOptions` using `WithTweakListOptions`.

By defining a custom tweak function, you inject selectors directly into the API request's query parameters (`labelSelector` or `fieldSelector`). This shifts the filtering burden to the Kubernetes API server (which is backed by etcd's indexing). The controller only receives objects it actually cares about, reducing network transfer and local memory consumption.

Let's look at a production-grade configuration that limits the informer factory to watch only objects within a specific namespace, and filters by a custom label selector.

<script src="https://gist.github.com/mohashari/75d633285b7353795befbf1518122807.js?file=snippet-1.go"></script>

When writing tweak functions, ensure your controllers handle label updates properly. If an object is modified such that its labels no longer match the selector, or if an untracked object is modified to match, the informer's reflector will send delete and create events, respectively. Your reconcile loop must handle these transitions gracefully.

## Level 2: Drastically Reducing Memory with Metadata-Only Informers

What if you need to watch a resource across the entire cluster, but you don't need its entire payload? For example, a garbage collection controller or a dependency tracker might only need to check owner references, labels, annotations, or the creation timestamp.

Historically, developers were forced to download the entire object, including huge fields like `status` or embedded pod specs. In Kubernetes 1.15 and later, you can use the Metadata-Only Client and Metadata Informers. Metadata informers issue `LIST` and `WATCH` requests specifying `Accept: application/json;as=PartialObjectMetadataList;g=meta.k8s.io;v=v1`, which tells the API Server to strip the spec and status, returning only the metadata headers.

This drops memory consumption by up to 90%. For example, caching 10,000 raw Pods might consume 200MB, whereas caching only their metadata consumes less than 20MB.

Here is how you initialize and run a metadata-only informer using the metadata client interface:

<script src="https://gist.github.com/mohashari/75d633285b7353795befbf1518122807.js?file=snippet-2.go"></script>

By leveraging `metav1.PartialObjectMetadata`, the controller can run its business logic using identifying details without holding large payload configurations in RAM.

## Resilient Leader Election Setup

To achieve High Availability (HA) for your controller, you will typically deploy multiple replicas (e.g., `replicas: 3` in your Deployment spec). However, running multiple active controller loops concurrently will lead to race conditions, double writes, and inconsistent object states. You need an active-passive topology: only one replica is actively executing the reconciliation loop, while the other replicas act as warm standbys, waiting to take over if the leader fails.

The standard pattern in Kubernetes is resource-locking using the `coordination.k8s.io` `Lease` object. Historically, controllers used `ConfigMap` or `Endpoints` locks. These are now deprecated and highly discouraged because updating a ConfigMap generates significant etcd write amplification and triggers watches on every node watching the namespace. The `Lease` resource is lightweight, optimized, and designed precisely for heartbeats.

Here is a resilient implementation of client-go Leader Election utilizing the `LeaseLock` type:

<script src="https://gist.github.com/mohashari/75d633285b7353795befbf1518122807.js?file=snippet-3.go"></script>

### The Math Behind Resilient Timing Configurations

Tuning leader election parameters is a trade-off between failover time and controller stability.
* **`LeaseDuration`**: The amount of time that a lease is valid. In production, setting this to `15s` allows quick failovers if a node physically dies or loses power.
* **`RenewDeadline`**: The acting leader will attempt to update the lease within this timeframe. We set this to `10s`. If the API server is heavily loaded, the leader has 10 seconds to succeed at least once.
* **`RetryPeriod`**: The frequency of attempts to renew the lease. Setting this to `2s` ensures that if a renew request fails (due to minor network jitter or API rate limiting), client-go can retry 5 times within the 10-second `RenewDeadline`.

If you configure these parameters too tightly (e.g., 5s LeaseDuration, 4s RenewDeadline, 1s RetryPeriod), temporary network congestion or a garbage collection pause in your controller can cause it to drop leadership prematurely, triggering a cascade of unnecessary failovers.

## The Failover Handshake: Wiring Informers with Leader Election

A common architectural debate is whether to start the informer factories *before* or *after* the leader lease is acquired:
1. **Warm Standby (Caches Synced on all Replicas)**: All replicas run the informer factory and sync their caches from day one. When leader failover occurs, the new leader has a fully warmed cache and can start reconciling instantly. The drawback is high memory consumption on all replica nodes.
2. **Cold Standby (Caches Start After Leadership)**: Replicas run idle, consuming negligible memory. When a failover occurs, the chosen leader initializes the informer factory, executes a full `LIST` request, and blocks reconciliation until the cache is fully synchronized. The drawback is slow failovers and a stampede of API server requests during failover.

In high-scale environments, a hybrid approach or a disciplined startup pipeline is required. Below is a complete implementation that initializes the controller structure, sets up a clean shutdown mechanism using context cancellation, and ensures that the informer factory starts when leadership is confirmed, preventing writes during stale cache states.

<script src="https://gist.github.com/mohashari/75d633285b7353795befbf1518122807.js?file=snippet-4.go"></script>

Notice the call to `os.Exit(0)` inside `OnStoppedLeading`. This is an essential practice in production systems. If the leader fails to renew its lease (e.g., due to an extreme GC pause or network partition), the leader election library will cancel the `leaderCtx` and call `OnStoppedLeading`. Because Go lacks a mechanism to forcibly terminate running goroutines, leaving the process running can lead to zombie reconcile loops performing unauthorized operations. Forcing a process exit guarantees safety.

## Tuning client-go Client Rate Limits for Scale

By default, the client-go configuration restricts API calls to `5 QPS` and `10 Burst` requests per second. If your controller reconciles a resource that requires querying or updating related resources, a surge in events can quickly exhaust this limit. When the client-go rate limiter throttles requests, you will observe log lines indicating API requests taking precisely several seconds, leading to reconciliations falling behind.

For scale, you must adjust the `rest.Config` limits based on the cluster size and target reconciliation throughput. Here is how to construct a configured clientset:

<script src="https://gist.github.com/mohashari/75d633285b7353795befbf1518122807.js?file=snippet-5.go"></script>

With `QPS = 100` and `Burst = 150`, the controller can quickly dispatch writes during large batch updates without hitting client-side throttling. Be careful not to set these values too high (e.g., > 500 QPS) without consulting cluster administrators, as it can overwhelm the Kubernetes API server control plane.

## Production Failure Modes & Operational Runbook

Deploying optimized controllers at scale introduces unique operational challenges. Below are the most common production failure modes and how to address them.

### 1. The Split-Brain Reconciler
* **Symptom**: Two replicas are concurrently writing changes to the same resource, causing resource version conflicts (`Conflict: Operation cannot be fulfilled on ...`).
* **Root Cause**: A temporary network partition prevents the leader from communicating with the API server, but it continues processing its local workqueue. Because it cannot renew its lease, the standby pod acquires the lease. The old leader eventually recovers and attempts writes before the context cancellation propagates or process exit triggers.
* **Remedy**: 
  1. Use `ReleaseOnCancel: true` in your leader election config to explicitly release the lease during graceful shutdown.
  2. Rely strictly on the passed `leaderCtx` in your reconciler's HTTP calls. All clientset write operations must use context-aware methods (`Create(ctx, ...)`, `Update(ctx, ...)`) to instantly abort if leadership is lost.

### 2. The Failover Stampede
* **Symptom**: When a leader pod is rescheduled, the API server CPU spikes to 100%, and other controllers experience API latency increases.
* **Root Cause**: The standby replica acquires the lease, starts its informer factory, and issues heavy `LIST` requests to populate its empty cache.
* **Remedy**: 
  * Implement Metadata-Only informers to minimize the memory and serialized data payload.
  * Configure client-go to cache objects in namespace limits or apply strict label filtering if a single controller instance does not need cluster-wide visibility.

### 3. Monitoring Metrics
To prevent blind spots, export and alert on these Prometheus metrics exposed by the `client-go` instrumentation package:
* `rest_client_request_duration_seconds`: Monitors API call latency to detect throttling.
* `leader_election_lease_duration_seconds`: Shows the duration of the current lease lock. Monitor if this value becomes dangerously close to the `RenewDeadline`.
* `workqueue_depth`: Tracks the number of items waiting in the reconciler queue. A constantly rising depth indicates that the queue processing is bottlenecked by CPU or API server response times.