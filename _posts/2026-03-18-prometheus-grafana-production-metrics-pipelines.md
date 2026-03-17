---
layout: post
title: "Prometheus and Grafana: Building Production-Grade Metrics Pipelines"
date: 2026-03-18 07:00:00 +0700
tags: [observability, prometheus, grafana, monitoring, sre]
description: "Design a scalable metrics pipeline with Prometheus remote write, recording rules, alerting, and Grafana dashboards for production backends."
---

Production backends fail silently. A service degrading under load rarely throws an exception — it just gets slower, queues back up, and by the time an engineer notices, users have already churned. The teams that catch these regressions early share a common trait: they've invested in a metrics pipeline that doesn't just collect data, but actively surfaces anomalies before they become incidents. Prometheus and Grafana have become the de facto standard for this in cloud-native environments, but most teams use only a fraction of their capability — scraping metrics and building a few dashboards, then wondering why their on-call rotation is still painful. This post walks through building a production-grade metrics pipeline: from instrumentation and remote write, through recording rules and alerting, to Grafana dashboards that tell a coherent story about your system's health.

## Instrument Your Go Service Correctly

The foundation is instrumentation. The Prometheus client library for Go gives you counters, gauges, histograms, and summaries. For request-oriented backends, histograms are the workhorse: they let you compute quantiles across arbitrary time windows at query time, which is something summaries cannot do once the data is aggregated.

Label cardinality is the most common instrumentation mistake. Every unique combination of label values creates a new time series. Adding a `user_id` label to a request histogram will explode your TSDB and bring down your Prometheus instance. Labels should describe the shape of traffic, not its identity: `method`, `route`, `status_code`, `region`.

<script src="https://gist.github.com/mohashari/a1a0571bd33ba5e7f83e1c83619f8b86.js?file=snippet.go"></script>

## Configure Remote Write for Durability

A single Prometheus instance is a single point of failure and a storage bottleneck. Remote write lets you ship metrics to a durable long-term store — Thanos, Cortex, Mimir, or a managed solution like Grafana Cloud — while keeping local Prometheus as a fast, ephemeral query layer. Queue configuration matters: the defaults are conservative and will drop metrics under sustained load spikes if your remote endpoint is slow.

<script src="https://gist.github.com/mohashari/a1a0571bd33ba5e7f83e1c83619f8b86.js?file=snippet-2.yaml"></script>

The `write_relabel_configs` block drops noisy Go runtime metrics before they ever leave the scraper — a simple optimization that can cut your remote write volume by 20–30% on Go-heavy fleets.

## Define Recording Rules to Tame Query Cost

PromQL can compute anything, but expensive queries run at dashboard load time will make your Grafana feel sluggish and hammer your Prometheus. Recording rules pre-compute expensive aggregations on the evaluation interval, materializing them as new time series. The convention `level:metric:operations` keeps rule names readable and queryable.

<script src="https://gist.github.com/mohashari/a1a0571bd33ba5e7f83e1c83619f8b86.js?file=snippet-3.yaml"></script>

## Write Alerts That Page on Symptoms, Not Causes

Alerting on causes — high CPU, elevated GC pause time, connection pool exhaustion — produces alert storms that train engineers to ignore their phones. Alert on symptoms: latency above SLO, error rate above budget, availability below threshold. The cause is your job to find once you're paged; the alert's job is to tell you that users are experiencing a degraded service.

<script src="https://gist.github.com/mohashari/a1a0571bd33ba5e7f83e1c83619f8b86.js?file=snippet-4.yaml"></script>

## Provision Grafana Dashboards as Code

Clicking through the Grafana UI to build dashboards is a trap: they live in a database, drift over time, and disappear when someone's well-intentioned edit goes wrong. Grafana's provisioning system lets you declare dashboards as JSON files committed to your repository, loaded on startup. Pair this with a Jsonnet library like `grafonnet` for maintainable, DRY dashboard definitions.

<script src="https://gist.github.com/mohashari/a1a0571bd33ba5e7f83e1c83619f8b86.js?file=snippet-5.sh"></script>

## Wire It Together with Docker Compose

For local development and testing, a compose file gives the whole stack in a single command. This is also a useful template for understanding the network topology before translating it to Kubernetes manifests.

<script src="https://gist.github.com/mohashari/a1a0571bd33ba5e7f83e1c83619f8b86.js?file=snippet-6.yaml"></script>

## Query Patterns for Actionable Dashboards

A dashboard that shows raw counters without context is noise. The most useful panels show rates, ratios, and comparisons against historical baselines. This PromQL snippet powers an SLO burn rate panel — it shows how fast you're consuming your monthly error budget relative to the safe burn rate.

<script src="https://gist.github.com/mohashari/a1a0571bd33ba5e7f83e1c83619f8b86.js?file=snippet-7.txt"></script>

The denominator normalizes the error ratio against the SLO budget fraction, then scales by the ratio of the burn window to the SLO window — a 1h window over a 720h month. Any value above 14.4 triggers Google's fast-burn threshold, meaning you'd exhaust your entire monthly budget in under 2 days at the current rate.

The difference between a team that firefights constantly and one that operates calmly at scale is rarely the sophistication of their tooling — it's the discipline to instrument on symptoms, store metrics durably, pre-compute what they query, and keep dashboards in version control. Start with the four golden signals — latency, traffic, errors, saturation — instrument them correctly from the first service, and treat your recording rules and alert definitions with the same rigor you apply to application code. The pipeline described here gives you the scaffolding; the insight comes from knowing your system well enough to ask it the right questions.