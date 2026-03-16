---
layout: post
title: "Structured Logging in Go: Context Propagation, Sampling, and Log Pipelines"
date: 2026-03-17 07:00:00 +0700
tags: [go, logging, observability, backend, structured-logs]
description: "Build a production logging system in Go with slog, trace-context propagation, adaptive sampling, and exporters that feed Loki or Elasticsearch pipelines."
---

Most backend engineers have been there: a production incident at 2am, logs scattered across services, no trace IDs tying requests together, and half the log lines missing fields that would have made the root cause obvious in seconds. Go's standard library logging has historically been a blunt instrument — unstructured, context-unaware, and pipeline-hostile. With `slog` landing in Go 1.21 as a first-class structured logging API, there's finally a clear path to building logging infrastructure that's observable at scale. This post walks through the full stack: wiring `slog` for context propagation, implementing adaptive sampling to control log volume, and building exporters that feed real pipelines like Loki and Elasticsearch.

## Setting Up slog with a Custom Handler

The foundation of any production logging system is a handler that controls how log records are serialized and where they go. The default `slog.JSONHandler` gets you 80% of the way, but production systems need control over field names, timestamps, and output destinations.

This example creates a structured logger with consistent field naming and a writer that can be swapped at runtime — critical for test isolation and for routing to different backends.

<script src="https://gist.github.com/mohashari/e8d3eb5ccffb7e29e1d87de9b3f8eb0e.js?file=snippet.go"></script>

## Context Propagation with Trace IDs

The most painful gap in naive logging setups is losing the thread of a request as it moves through middleware, handlers, and downstream service calls. The solution is to store a logger — or at minimum a set of contextual attributes — directly in the `context.Context` and extract it at each log site. This eliminates the need to pass a logger through every function signature.

<script src="https://gist.github.com/mohashari/e8d3eb5ccffb7e29e1d87de9b3f8eb0e.js?file=snippet-2.go"></script>

Now any handler can call `logging.FromContext(ctx).Info("user created", "user_id", id)` and the trace IDs are automatically included — no passing loggers down the call chain.

## HTTP Middleware for Automatic Trace Injection

With the context helpers in place, a single middleware layer can inject trace context for every inbound request. If you're running OpenTelemetry, you extract the span from `trace.SpanFromContext`; if not, you generate a correlation ID from the request headers.

<script src="https://gist.github.com/mohashari/e8d3eb5ccffb7e29e1d87de9b3f8eb0e.js?file=snippet-3.go"></script>

## Adaptive Sampling

High-throughput services can generate millions of log lines per minute. Sending everything to Loki or Elasticsearch at that volume is expensive and often counterproductive — it buries the signal in noise. Adaptive sampling lets you log 100% of errors and slow requests while dropping a configurable fraction of healthy, fast requests.

The key design insight is that sampling decisions should happen at the handler level, not scattered throughout business logic. This `SamplingHandler` wraps any `slog.Handler` and applies rate-based sampling for levels below `Warn`.

<script src="https://gist.github.com/mohashari/e8d3eb5ccffb7e29e1d87de9b3f8eb0e.js?file=snippet-4.go"></script>

## Writing to Loki via the Push API

Grafana Loki ingests logs through a simple HTTP push endpoint. Rather than running a Promtail sidecar for every service, you can push directly from Go during development or in environments where sidecars are impractical. The payload format is a JSON stream of log entries grouped by label set.

<script src="https://gist.github.com/mohashari/e8d3eb5ccffb7e29e1d87de9b3f8eb0e.js?file=snippet-5.go"></script>

## Buffered Async Handler to Avoid Blocking Hot Paths

Synchronous log writes block the goroutine producing the log record — a real problem when the downstream is a remote HTTP endpoint with variable latency. A channel-backed async handler decouples your application's hot path from I/O, at the cost of potentially losing in-flight records on a hard crash. For most services, this trade-off is correct; for financial audit trails, it is not.

<script src="https://gist.github.com/mohashari/e8d3eb5ccffb7e29e1d87de9b3f8eb0e.js?file=snippet-6.go"></script>

## Wiring It All Together

The complete pipeline composes the handlers in order: async buffering wraps sampling, which wraps the JSON writer.

<script src="https://gist.github.com/mohashari/e8d3eb5ccffb7e29e1d87de9b3f8eb0e.js?file=snippet-7.go"></script>

The `shutdown` function is the critical detail here. Registering it with `defer` or tying it to a `signal.NotifyContext` cancellation ensures the async handler drains before the process exits — otherwise you silently drop the last few hundred log records, which are often the most important ones during a crash.

Structured logging with `slog` is not just a stylistic upgrade — it's the prerequisite for everything else in your observability stack. Trace IDs become queryable fields in Loki and Elasticsearch. Sampling prevents cost explosions as traffic grows. The async handler keeps your p99 latency honest. And because the handler interface is composable, you can layer in field redaction, PII masking, or Elasticsearch bulk indexing without touching a single call site in your business logic. Start with `slog.JSONHandler`, add context propagation on day one, and reach for sampling and async buffering when you have the metrics to justify the added complexity.