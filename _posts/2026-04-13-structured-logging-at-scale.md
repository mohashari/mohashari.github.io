---
layout: post
title: "Structured Logging at Scale: Patterns, Pipelines, and Best Practices"
date: 2026-04-13 07:00:00 +0700
tags: [observability, logging, backend, elk, structured-logs]
description: "Design structured logging strategies that make logs queryable, correlatable, and actionable across distributed services."
---

Every on-call engineer has lived this nightmare: a production incident at 2 AM, a cascade of failures across five services, and a sea of log lines that look like `ERROR: something went wrong` scattered across four different log files, each formatted differently, none of them correlated. You grep, you tail, you squint. By the time you've assembled enough context to understand what happened, the SLA is already breached. Structured logging is the discipline that turns logs from a last-resort debugging tool into a first-class observability signal — one that you can query, correlate, and alert on with the same rigor you'd apply to metrics or traces.

## What "Structured" Actually Means

A structured log is not a log with a timestamp slapped in front of a string. It is a machine-readable document — typically JSON — where every field is a typed, named key. Instead of `"user 42 failed to purchase item 99"`, you emit `{"event":"purchase_failed","user_id":42,"item_id":99,"reason":"insufficient_funds","duration_ms":14}`. The difference sounds cosmetic until you're querying a billion events in Elasticsearch and the query `event:purchase_failed AND reason:insufficient_funds` returns results in 200ms instead of you scanning files with `grep`.

The foundational principle is that **logs are data**, and they deserve a schema. That schema should be consistent, backward-compatible, and enriched with context at every layer of your stack.

## Defining a Log Schema

Before writing a single log line, define what fields every log entry in your system must carry. This becomes a contract between services and your log aggregation pipeline. Standardizing at the schema level is what makes cross-service correlation possible.

A canonical set of base fields for a distributed system should include trace identifiers, service metadata, and a severity level that follows a defined vocabulary.

<script src="https://gist.github.com/mohashari/e10a0fc72386d7b05559f059c909ab30.js?file=snippet.go"></script>

Go's `log/slog` package (introduced in 1.21) outputs native JSON and supports structured attributes natively, making it an excellent foundation without pulling in third-party dependencies.

## Middleware: Enriching Logs at the HTTP Layer

The most efficient place to inject request-scoped context is your HTTP middleware. Rather than threading individual fields through every function call, store a contextual logger in the request context and retrieve it downstream.

<script src="https://gist.github.com/mohashari/e10a0fc72386d7b05559f059c909ab30.js?file=snippet-2.go"></script>

This pattern gives every log line emitted during a request the same `trace_id`, which is the primitive that makes distributed tracing across services possible even without a dedicated tracing backend.

## Shipping Logs: The Fluent Bit Pipeline

Raw JSON on stdout is only half the story. In a containerized environment, you need a lightweight log shipper to collect, parse, and forward logs to your aggregation backend. Fluent Bit is the industry standard for this: it runs as a DaemonSet in Kubernetes and adds minimal overhead.

<script src="https://gist.github.com/mohashari/e10a0fc72386d7b05559f059c909ab30.js?file=snippet-3.yaml"></script>

The `Merge_Log On` directive is critical: it tells Fluent Bit to parse the JSON your application emits and merge it into the top-level document rather than nesting it under a `log` key, which would break all your field-level queries in Elasticsearch.

## Querying Logs in Elasticsearch

Once logs are indexed with a consistent schema, you can write queries that would be impossible against flat text. This query finds all failed purchase events where the upstream payment service was slow, grouped by failure reason.

<script src="https://gist.github.com/mohashari/e10a0fc72386d7b05559f059c909ab30.js?file=snippet-4.json"></script>

The key insight here is the `reason.keyword` field: Elasticsearch automatically creates both an analyzed text field and an unanalyzed `keyword` sub-field for string properties, and aggregations must use the `.keyword` variant to work correctly.

## Avoiding Log Cardinality Explosions

One of the most damaging anti-patterns in structured logging is writing high-cardinality values — UUIDs, user IDs, raw URLs with query parameters — into field names rather than field values. This destroys Elasticsearch's ability to build efficient field mappings and can cause index performance to crater.

<script src="https://gist.github.com/mohashari/e10a0fc72386d7b05559f059c909ab30.js?file=snippet-5.go"></script>

Path normalization is a discipline you need to enforce at the router layer. Most HTTP frameworks (Chi, Echo, Gin) expose the matched route pattern — always use that instead of `r.URL.Path`.

## Sampling High-Volume Log Streams

In a high-throughput service, logging every successful database query or cache hit will overwhelm your pipeline and inflate storage costs. Probabilistic sampling lets you retain statistical fidelity while shedding volume. The key rule: always log at full rate for errors and slow operations, sample aggressively only for successful high-frequency events.

<script src="https://gist.github.com/mohashari/e10a0fc72386d7b05559f059c909ab30.js?file=snippet-6.go"></script>

Adding `"sampled": true` and `"sample_rate": 0.01` to sampled events lets you account for the sampling factor when computing rates in dashboards: a count of 500 sampled cache hits at 1% represents approximately 50,000 actual events.

## Testing Your Log Schema

Log schemas drift silently over time — a field gets renamed, a type changes from integer to string, and suddenly your Kibana dashboards break with no warning. Treat your log output like an API and write tests that assert on its structure.

<script src="https://gist.github.com/mohashari/e10a0fc72386d7b05559f059c909ab30.js?file=snippet-7.go"></script>

Running these tests in CI catches schema regressions before they reach production. For more comprehensive coverage, consider golden file tests that snapshot the full JSON output of a request lifecycle and diff against it on every build.

---

Structured logging is not a library choice or a configuration detail — it is an architectural discipline. The payoff compounds: every field you standardize across services today is a correlation you can draw instantly at 2 AM six months from now. Start with a base schema, enforce it at the middleware layer, ship it through a stateless log pipeline, and query it like the data it is. The engineers who instrument their systems well are the ones who fix incidents in minutes, not hours — because they built their observability before they needed it.