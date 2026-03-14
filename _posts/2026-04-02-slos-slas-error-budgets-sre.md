---
layout: post
title: "SLOs, SLAs, and Error Budgets: The SRE Approach to Reliability"
date: 2026-04-02 07:00:00 +0700
tags: [sre, observability, reliability, devops, backend]
description: "Define meaningful SLOs and error budgets to balance feature velocity with system reliability using Google's proven SRE methodology."
---

Every production system will fail. The question isn't whether your service will have downtime — it's how much downtime is acceptable, who decides that threshold, and what happens when you cross it. Most engineering teams discover this the hard way: either they over-engineer reliability at the cost of shipping features, or they ship too fast and erode user trust through repeated incidents. Google's Site Reliability Engineering discipline offers a principled middle path through Service Level Objectives, Service Level Agreements, and Error Budgets — a framework that turns the abstract goal of "reliability" into a measurable, negotiable contract between engineering and the business.

## Defining Your Service Level Indicators

Before setting targets, you need to measure the right signals. A Service Level Indicator (SLI) is a carefully chosen metric that reflects user experience. For most services, this means request latency, error rate, and availability. The key insight: not every metric is an SLI. CPU usage is an implementation detail. The fraction of requests served in under 200ms is an SLI.

Here's a Go HTTP middleware that instruments SLIs using Prometheus — tracking both error rate and latency distributions as they happen:

<script src="https://gist.github.com/mohashari/1ae74e1944f1700bbf0592419c099901.js?file=snippet.go"></script>

## Expressing SLOs as Prometheus Recording Rules

An SLO is a target value for an SLI over a rolling time window — for example, "99.5% of requests return a non-5xx response over any 28-day window." Raw counters don't give you this directly; you need recording rules that compute the ratio continuously. Pre-computing these ratios keeps your dashboards and alerting fast regardless of data volume.

<script src="https://gist.github.com/mohashari/1ae74e1944f1700bbf0592419c099901.js?file=snippet-2.yaml"></script>

## Calculating the Error Budget

The error budget is the mathematical inverse of your SLO. If you commit to 99.9% availability, you're permitting 0.1% failure — over 28 days that's roughly 40 minutes of bad requests. This budget is the team's "license to ship": while budget remains, you can deploy aggressively. Once it's exhausted, reliability work takes priority over features. This Go function makes the arithmetic explicit and embeds it in your tooling rather than a spreadsheet:

<script src="https://gist.github.com/mohashari/1ae74e1944f1700bbf0592419c099901.js?file=snippet-3.go"></script>

## Multi-Window Burn Rate Alerts

A single threshold alert on error rate generates too many false positives for slow burns and misses fast budget exhaustion. Google's approach uses two burn rate windows simultaneously: a short window catches fast burns (a 1-hour outage depleting 5% of monthly budget), while a long window catches slow persistent degradation. Alerting on both avoids notification fatigue without sacrificing coverage.

<script src="https://gist.github.com/mohashari/1ae74e1944f1700bbf0592419c099901.js?file=snippet-4.yaml"></script>

## Tracking Budget Consumption in PostgreSQL

For reporting, compliance, and SLA audits you often need historical error budget data beyond Prometheus's retention window. Writing a daily snapshot to PostgreSQL gives you a queryable audit trail and lets you correlate deployments, incidents, and budget consumption across quarters.

<script src="https://gist.github.com/mohashari/1ae74e1944f1700bbf0592419c099901.js?file=snippet-5.sql"></script>

## Automating Budget-Gated Deployments

Error budgets only change team behavior if they're integrated into deployment workflows. This shell script wraps a deploy command with a budget gate — if less than 10% of the monthly budget remains, it blocks the deploy and forces an explicit override. Encoding this in CI prevents the budget from being an ignored metric on a dashboard nobody watches.

<script src="https://gist.github.com/mohashari/1ae74e1944f1700bbf0592419c099901.js?file=snippet-6.sh"></script>

## The SLA Is the Contract, the SLO Is the Buffer

One crucial distinction that teams often blur: an SLA is an external commitment with financial or legal consequences — "we guarantee 99.9% uptime or you receive a credit." An SLO is an internal target that must be stricter than the SLA to absorb measurement uncertainty and reaction time. If your SLA is 99.9%, your SLO should target 99.95% or better. The gap between them is your operational buffer. Setting these targets without data is guesswork; set your first SLOs based on your rolling 90-day baseline, then tighten them quarterly as reliability improves.

Reliability engineering stops being an art and starts being an engineering discipline the moment you quantify it. SLIs give you the signal, SLOs give you the target, and error budgets give you the mechanism for an honest conversation between product and platform: not "can we ship this?" but "do we still have the budget to?" Start with one critical service, define three SLIs (availability, latency p99, error rate), wire up the recording rules and burn-rate alerts, and measure for 30 days before setting any hard targets. The data will tell you what's achievable — and what it will actually cost your users when you miss it.