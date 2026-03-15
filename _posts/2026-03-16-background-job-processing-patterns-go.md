---
layout: post
title: "Background Job Processing: Patterns for Reliable Async Work in Go"
date: 2026-03-16 07:00:00 +0700
tags: [go, async, queues, reliability, backend]
description: "Design robust background job systems in Go using worker pools, dead-letter queues, retries, and observability to handle async workloads reliably."
---

Every backend system eventually needs to do work outside the request-response cycle. Sending emails, resizing images, processing payments, syncing data to third-party APIs — these are tasks that are too slow, too risky, or too retry-heavy to run inline. Yet background job systems are where reliability bugs hide: jobs that silently disappear, workers that deadlock under load, retries that hammer a downstream service into the ground. Getting this right requires more than spinning up a goroutine and hoping for the best. This post walks through building a production-grade background job system in Go, covering worker pools, retry strategies, dead-letter queues, and observability.

## The Worker Pool Foundation

The naive approach — `go processJob(job)` — has no backpressure. Under load, you spawn thousands of goroutines and exhaust memory. A worker pool bounds concurrency by pre-allocating a fixed number of goroutines that pull work from a shared channel.

<script src="https://gist.github.com/mohashari/659b6d4464cbbc9aa4733479673f12a3.js?file=snippet.go"></script>

The buffer size of `concurrency*2` absorbs small bursts without blocking the producer, while still applying meaningful backpressure when the pool is genuinely saturated. `Shutdown` drains in-flight jobs before returning, which is critical for graceful process termination.

## Retry Logic with Exponential Backoff

Transient failures — network timeouts, database connection hiccups, rate limit errors — should be retried. Permanent failures — malformed payloads, missing records — should not. The trick is distinguishing between them and backing off intelligently to avoid thundering-herd problems.

<script src="https://gist.github.com/mohashari/659b6d4464cbbc9aa4733479673f12a3.js?file=snippet-2.go"></script>

Jitter is essential. Without it, all workers that failed at the same moment retry simultaneously — exactly the thundering herd you were trying to avoid. Even deterministic jitter (based on attempt number or job ID) distributes load meaningfully.

## Persisting Jobs with PostgreSQL

In-memory queues evaporate when your process crashes. For durability, persist jobs to a database and use a polling or LISTEN/NOTIFY pattern to dispatch them. PostgreSQL's `SKIP LOCKED` is purpose-built for this: multiple workers can dequeue without blocking each other.

<script src="https://gist.github.com/mohashari/659b6d4464cbbc9aa4733479673f12a3.js?file=snippet-3.sql"></script>

The partial index on `status` keeps the index small and fast as your `completed` jobs accumulate.

<script src="https://gist.github.com/mohashari/659b6d4464cbbc9aa4733479673f12a3.js?file=snippet-4.sql"></script>

`SKIP LOCKED` means if another worker already locked a row, this query simply moves on rather than waiting. You get horizontal scalability for free without any external coordination service.

## Dead-Letter Queue Handling

After `max_attempts` retries, a job should move to a dead-letter queue (DLQ) rather than being silently discarded. The DLQ serves as an audit trail and enables manual replay once the underlying issue is fixed.

<script src="https://gist.github.com/mohashari/659b6d4464cbbc9aa4733479673f12a3.js?file=snippet-5.go"></script>

## Observability: Metrics and Structured Logging

A job system without metrics is a black box. At minimum, track queue depth, job duration, and error rates. The standard library's `log/slog` and a Prometheus counter get you most of the way.

<script src="https://gist.github.com/mohashari/659b6d4464cbbc9aa4733479673f12a3.js?file=snippet-6.go"></script>

Wrap your handler to record these automatically, and expose `/metrics` via `promhttp.Handler()`. Pair queue depth with an alert: if it grows monotonically for five minutes, your workers are falling behind and you need to scale out or investigate.

## Graceful Shutdown

A process that gets `SIGTERM` mid-job should finish what it's doing before exiting. Signal handling ties the shutdown lifecycle together.

<script src="https://gist.github.com/mohashari/659b6d4464cbbc9aa4733479673f12a3.js?file=snippet-7.go"></script>

`signal.NotifyContext` is the idiomatic Go 1.16+ pattern. It converts OS signals into context cancellation, which propagates cleanly through the call stack — your handlers can check `ctx.Done()` to abort long-running work early if needed.

---

A reliable background job system is not a single feature — it's a composition of small, well-understood guarantees: bounded concurrency to protect resources, durable persistence to survive crashes, intelligent retry with backoff to handle transience, dead-letter queues to surface permanent failures, and observability to know what's happening at all times. Start with the PostgreSQL-backed queue and a fixed worker pool; add metrics from day one; build the DLQ before you need it. Each of these patterns is independently simple, but together they give you a system you can trust to run unattended.