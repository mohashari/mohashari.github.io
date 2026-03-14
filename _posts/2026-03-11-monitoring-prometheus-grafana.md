---
layout: post
title: "Monitoring Your Backend with Prometheus and Grafana"
tags: [monitoring, prometheus, grafana, devops, backend]
description: "Set up production-grade observability for your backend services using Prometheus metrics and Grafana dashboards."
---

You can't improve what you can't measure. Prometheus + Grafana is the gold standard for backend observability. This guide gets you from zero to a production-ready monitoring stack.

## The Four Golden Signals

Before instrumenting anything, know what to measure:

1. **Latency** — How long requests take (distinguish success vs error latency)
2. **Traffic** — How many requests per second
3. **Errors** — Rate of failed requests
4. **Saturation** — How "full" your service is (CPU, memory, queue depth)

Everything else is secondary.

## Setting Up Prometheus


<script src="https://gist.github.com/mohashari/96e124f0c3e3a58fc8fd3f3e7a0607eb.js?file=snippet.yaml"></script>



<script src="https://gist.github.com/mohashari/96e124f0c3e3a58fc8fd3f3e7a0607eb.js?file=snippet-2.yaml"></script>


## Instrumenting Your Go Service


<script src="https://gist.github.com/mohashari/96e124f0c3e3a58fc8fd3f3e7a0607eb.js?file=snippet.go"></script>


## Business Metrics

Beyond HTTP metrics, track business-level metrics:


<script src="https://gist.github.com/mohashari/96e124f0c3e3a58fc8fd3f3e7a0607eb.js?file=snippet-2.go"></script>


## Essential PromQL Queries


<script src="https://gist.github.com/mohashari/96e124f0c3e3a58fc8fd3f3e7a0607eb.js?file=snippet.txt"></script>


## Alerting Rules


<script src="https://gist.github.com/mohashari/96e124f0c3e3a58fc8fd3f3e7a0607eb.js?file=snippet-3.yaml"></script>


## Grafana Dashboard JSON

Create dashboards as code using Grafana's JSON model and store them in version control. Use the Grafana dashboard provisioning:


<script src="https://gist.github.com/mohashari/96e124f0c3e3a58fc8fd3f3e7a0607eb.js?file=snippet-4.yaml"></script>


## The Three Pillars of Observability

Metrics alone aren't enough:

| Pillar | Tool | Use For |
|--------|------|---------|
| **Metrics** | Prometheus + Grafana | Trends, dashboards, alerting |
| **Logs** | Loki / Elasticsearch | Debugging, search, audit |
| **Traces** | Jaeger / Tempo | Distributed request flow |

Add structured logging and distributed tracing alongside your metrics for complete observability.

Start with the four golden signals. Get alerts working. Then expand to business metrics. You don't need everything on day one.
