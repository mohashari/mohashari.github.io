---
layout: post
title: "Background Job Processing: Patterns, Queues, and Reliability in Production"
date: 2026-03-17 07:00:00 +0700
tags: [background-jobs, queues, reliability, backend, workers]
description: "Design robust background job systems with at-least-once delivery, idempotent workers, dead-letter queues, and observable retry strategies."
---

Every distributed system eventually accumulates work that shouldn't block the request-response cycle. Sending an email, resizing an uploaded image, syncing data to a third-party API, generating a report — these are tasks that users trigger but don't need to wait on. The naive solution is a goroutine or a thread pool, which works until it doesn't: process restarts lose in-flight jobs, spikes overwhelm workers, failures vanish silently, and nobody knows the queue depth until customers complain. Building background job processing that survives production requires thinking carefully about delivery guarantees, worker design, failure handling, and observability — none of which are free.

## The Delivery Guarantee You Actually Want

Most queues offer *at-least-once* delivery: a message is redelivered if the consumer doesn't acknowledge it within a visibility timeout. This is safer than at-most-once (which can lose jobs) and far more practical than exactly-once (which is expensive and often illusory). The implication is that your workers must be idempotent — running the same job twice must produce the same outcome as running it once.

The simplest idempotency mechanism is a deduplication key stored in the database. Before doing any real work, the worker checks whether this job has already been completed.

<script src="https://gist.github.com/mohashari/8babfcd9adf1da9ba91e15ff3ee0129e.js?file=snippet.go"></script>

The `ON CONFLICT DO NOTHING` clause prevents duplicate inserts in the race where two workers pick up the same message simultaneously — a real scenario when visibility timeouts are short relative to processing time.

## Structuring the Queue Schema

If you're using PostgreSQL as a queue (common in smaller stacks before introducing Redis or SQS), the schema needs to support concurrent workers without lock contention. The classic approach uses `SELECT ... FOR UPDATE SKIP LOCKED`.

<script src="https://gist.github.com/mohashari/8babfcd9adf1da9ba91e15ff3ee0129e.js?file=snippet-2.sql"></script>

Workers claim jobs with a single atomic statement:

<script src="https://gist.github.com/mohashari/8babfcd9adf1da9ba91e15ff3ee0129e.js?file=snippet-3.sql"></script>

`SKIP LOCKED` means concurrent workers skip rows already claimed by another transaction, avoiding thundering herd problems and unnecessary contention.

## Exponential Backoff with Jitter

Retrying immediately after a failure is almost always wrong. If a downstream service is struggling, hammering it with retries makes things worse. Exponential backoff with jitter spaces retries out while avoiding retry synchronization across workers.

<script src="https://gist.github.com/mohashari/8babfcd9adf1da9ba91e15ff3ee0129e.js?file=snippet-4.go"></script>

Full jitter (random in `[0, backoff]`) distributes retries more evenly than adding a small random delta to an exponential base. At high concurrency this matters — AWS's "Exponential Backoff and Jitter" post from 2015 demonstrated a 7x improvement in completion time under load.

## The Dead-Letter Queue

Jobs that exhaust retries shouldn't disappear — they should move to a dead-letter queue (DLQ) for inspection, alerting, and manual replay. With the schema above, `status = 'dead'` serves this role. You need tooling around it.

<script src="https://gist.github.com/mohashari/8babfcd9adf1da9ba91e15ff3ee0129e.js?file=snippet-5.go"></script>

Expose this via an internal admin endpoint, not a public API. Dead jobs often failed for environmental reasons — a third-party API was down, a credential rotated — and replaying them after the root cause is fixed is a routine operational task.

## Worker Configuration and Graceful Shutdown

A worker process needs to handle `SIGTERM` gracefully: stop accepting new jobs, finish in-flight work, and exit cleanly. Kubernetes sends `SIGTERM` before killing a pod, and ignoring it means jobs get abandoned mid-execution.

<script src="https://gist.github.com/mohashari/8babfcd9adf1da9ba91e15ff3ee0129e.js?file=snippet-6.go"></script>

Pair this with a `terminationGracePeriodSeconds` in your Kubernetes deployment that exceeds your longest expected job duration. Thirty seconds is rarely enough for non-trivial workloads.

## Observability: The Metrics That Matter

You can't operate a job system you can't see. At minimum, expose these metrics in Prometheus format:

<script src="https://gist.github.com/mohashari/8babfcd9adf1da9ba91e15ff3ee0129e.js?file=snippet-7.go"></script>

Alert on queue depth growth (jobs enqueued faster than processed), dead-letter accumulation (persistent failures), and p99 job duration exceeding SLO thresholds. A queue that's silently growing is often the first signal of a capacity or dependency problem, and catching it before it becomes a customer-visible outage is the whole point.

## Closing Thoughts

Background job processing sits at the intersection of distributed systems theory and operational reality. At-least-once delivery forces idempotent workers; idempotency forces explicit deduplication keys; deduplication keys require durable state; durable state requires careful schema design. Exponential backoff with jitter protects downstream services; dead-letter queues preserve visibility into what failed and why; graceful shutdown prevents data loss during deploys. None of these are optional production concerns — they compound. A system that lacks any one of them will fail in ways that are hard to diagnose and expensive to recover from. Start with the simplest implementation that gets all of them right, instrument it thoroughly, and iterate on throughput only after correctness is established.