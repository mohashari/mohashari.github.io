---
layout: post
title: "Concurrency Patterns for Backend Engineers"
date: 2026-03-18 07:00:00 +0700
tags: [concurrency, go, backend, performance, patterns]
description: "Worker pools, fan-out/fan-in, pipelines, semaphores — practical concurrency patterns every backend engineer needs to handle high-throughput workloads."
---

Concurrency is one of the highest-leverage tools in backend engineering. Done right, it makes your system handle more load with the same hardware. Done wrong, it introduces race conditions and deadlocks that only appear in production. Here are the patterns that work.

## Worker Pool — Bounded Parallelism

The most important pattern. Process N items with at most M goroutines.

<script src="https://gist.github.com/mohashari/f1fdbd5bd390d3ff557f67345a8bbff7.js?file=snippet.go"></script>

Why bounded? Spawning one goroutine per item with 100k items hammers your CPU, exhausts file descriptors, and causes GC pressure.

## Pipeline — Stage-by-Stage Processing

Connect stages where the output of one feeds the input of the next.

<script src="https://gist.github.com/mohashari/f1fdbd5bd390d3ff557f67345a8bbff7.js?file=snippet-2.go"></script>

Each stage is independently testable and can run concurrently. Context propagation ensures clean shutdown.

## Fan-Out / Fan-In — Parallel Processing with Aggregation

Fan out to N workers, fan in results to a single channel.

<script src="https://gist.github.com/mohashari/f1fdbd5bd390d3ff557f67345a8bbff7.js?file=snippet-3.go"></script>

Use case: parallel API calls to multiple providers, aggregate responses.

## Semaphore — Limit Concurrent External Calls

Prevent thundering herd against your database or downstream APIs.

<script src="https://gist.github.com/mohashari/f1fdbd5bd390d3ff557f67345a8bbff7.js?file=snippet-4.go"></script>

The standard library alternative: `golang.org/x/sync/semaphore` with context support.

## errgroup — Concurrent Tasks with Error Propagation

<script src="https://gist.github.com/mohashari/f1fdbd5bd390d3ff557f67345a8bbff7.js?file=snippet-5.go"></script>

If any goroutine returns an error, the context is cancelled and `Wait()` returns the first error. Three sequential API calls become one parallel call — latency drops by ~66%.

## Rate Limiter — Token Bucket

<script src="https://gist.github.com/mohashari/f1fdbd5bd390d3ff557f67345a8bbff7.js?file=snippet-6.go"></script>

## Patterns Summary

| Pattern | Use When |
|---------|----------|
| Worker Pool | Process large batches with bounded resources |
| Pipeline | Multi-stage data transformation |
| Fan-Out/In | Parallel calls to same/different services |
| Semaphore | Limit concurrent access to a resource |
| errgroup | Multiple independent async tasks |
| Rate Limiter | Respect external API limits |

The key insight: **goroutines are cheap, but uncontrolled goroutines are dangerous**. Always use one of these patterns to bound your concurrency.
