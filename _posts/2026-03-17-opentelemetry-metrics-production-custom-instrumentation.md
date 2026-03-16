---
layout: post
title: "OpenTelemetry Metrics in Production: Custom Instrumentation Beyond the Basics"
date: 2026-03-17 07:00:00 +0700
tags: [opentelemetry, observability, metrics, backend, instrumentation]
description: "Go beyond auto-instrumentation and build custom OpenTelemetry metric pipelines with exemplars, histograms, and multi-backend exporters for deep backend visibility."
---

## OpenTelemetry Metrics in Production: Custom Instrumentation Beyond the Basics

Auto-instrumentation gets you to 60% visibility in about 10 minutes. The other 40% — the part that actually tells you *why* your p99 latency spiked on Tuesday at 2am, or why your database pool exhausts under a specific traffic pattern — requires intentional, hand-crafted instrumentation. Most teams never get there. They ship a sidecar collector, scrape some HTTP middleware metrics, and call it done. But when the incident hits and you're staring at a wall of generic `http_server_duration` histograms with no context, you realize that observability without domain-specific signals is just expensive log shipping. This post is about building the instrumentation layer that actually answers business questions: custom histograms with exemplars, multi-dimensional counters, per-tenant gauges, and a collector pipeline that routes signals to the right backend without coupling your application code to any of them.

### Setting Up a Meter Provider with Resource Attribution

Before writing a single metric, you need a properly configured `MeterProvider`. The resource attributes you attach here propagate to every data point your service emits — skipping this step means your metrics land in Prometheus or Tempo with no `service.name`, `deployment.environment`, or `k8s.pod.name`, making cross-service correlation impossible.

<script src="https://gist.github.com/mohashari/7ea4d119a8632664085113cab0eebe2b.js?file=snippet.go"></script>

### Histograms with Custom Bucket Boundaries

The default histogram bucket boundaries in OpenTelemetry (`[0, 5, 10, 25, 50, 75, 100, 250, 500, 1000]` ms) are reasonable for general HTTP latency but terrible for domain-specific operations. A database query that takes 5ms is fast; a payment authorization that takes 5ms is suspiciously fast. Define boundaries that match your SLOs, not generic ones. The `WithExplicitBucketBoundaries` view lets you override defaults per-instrument without changing the SDK globally.

<script src="https://gist.github.com/mohashari/7ea4d119a8632664085113cab0eebe2b.js?file=snippet-2.go"></script>

### Attaching Exemplars to Connect Metrics and Traces

Exemplars are the missing link between metrics aggregations and individual trace IDs. When your `p99` histogram bucket breaches an SLO, exemplars let you jump directly to a representative trace that landed in that bucket — no log searching, no time-range guessing. In Go, the SDK automatically attaches the current span context as an exemplar when you record a measurement inside an active span, but only if you use the W3C baggage-aware context correctly.

<script src="https://gist.github.com/mohashari/7ea4d119a8632664085113cab0eebe2b.js?file=snippet-3.go"></script>

### Multi-Backend Collector Pipeline

Separating your application from exporter logic is what makes OTel's architecture genuinely useful. Your service speaks OTLP; the collector handles routing. This pipeline fans out to Prometheus (for alerting), Tempo (for exemplar-linked traces), and an S3-backed long-term store, without a single change to application code when you swap backends.

<script src="https://gist.github.com/mohashari/7ea4d119a8632664085113cab0eebe2b.js?file=snippet-4.yaml"></script>

### Per-Tenant Observable Gauges with Callbacks

Some metrics can't be pushed — they need to be polled. Connection pool depths, queue lengths, and cache sizes are best modeled as observable (asynchronous) gauges that the SDK samples at collection time. This pattern also works well for per-tenant cardinality, where you enumerate tenants dynamically rather than pre-registering label combinations.

<script src="https://gist.github.com/mohashari/7ea4d119a8632664085113cab0eebe2b.js?file=snippet-5.go"></script>

### Querying Exemplars in Prometheus

Once exemplars flow through your pipeline, you need to query them. Prometheus exposes exemplars via a dedicated endpoint — standard PromQL won't show them. Use the exemplar query API to find the trace IDs embedded in your slowest histogram buckets.

<script src="https://gist.github.com/mohashari/7ea4d119a8632664085113cab0eebe2b.js?file=snippet-6.sh"></script>

### Validating Cardinality Before Production

High-cardinality attributes — user IDs, request IDs, raw URLs — will explode your time-series database. Before promoting new instrumentation to production, audit expected cardinality with a dry-run against your staging collector using the `prom_label_values` analysis query.

<script src="https://gist.github.com/mohashari/7ea4d119a8632664085113cab0eebe2b.js?file=snippet-7.sql"></script>

The gap between "we have observability" and "observability tells us something actionable" is almost entirely filled by custom instrumentation decisions made at development time, not at incident time. The patterns here — domain-aligned histogram buckets, exemplar linkage to traces, observable gauges for pull-based signals, and cardinality validation before promotion — form a repeatable framework rather than a collection of one-off tricks. The collector pipeline decouples your application from backend choices entirely, which means you can evolve your observability stack without touching service code. Start with one domain — payment flows, job processing, cache behavior — instrument it deliberately, and measure the time-to-diagnosis difference during your next on-call rotation. That delta is the business case for everything else.