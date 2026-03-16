---
layout: post
title: "Rate Limiting at Scale: Token Buckets, Sliding Windows, and Redis-Based Strategies"
date: 2026-03-17 07:00:00 +0700
tags: [rate-limiting, redis, backend, api, distributed-systems]
description: "Compare rate limiting algorithms and implement distributed, Redis-backed strategies that protect your APIs under real-world traffic bursts."
---

Every API you've ever shipped is one viral moment away from becoming unusable. A single high-traffic partner, a misbehaving client, or a coordinated scraping campaign can saturate your backend faster than any autoscaling policy can react. Rate limiting is the circuit breaker that stands between a normal Tuesday and a 3 AM incident. Yet most engineers reach for the first algorithm they find, bolt it onto a single Redis key, and call it done — only to discover edge cases in production: burst traffic that slips through, thundering herds after a limit resets, or limits that aren't actually shared across your fleet of API servers. Building rate limiting that works at scale means choosing the right algorithm for your traffic shape, understanding the tradeoffs of each approach, and implementing them correctly against a distributed store.

## The Four Core Algorithms

Before writing a line of code, you need to choose a model. The four most common are fixed window, sliding window log, sliding window counter, and token bucket. Each makes a different trade-off between accuracy, memory usage, and burst tolerance.

**Fixed window** is the simplest: divide time into discrete buckets (say, one minute each), increment a counter for each request, and reject when you hit the cap. The problem is the boundary condition — a client can send half their quota in the final seconds of one window and half in the opening seconds of the next, doubling their effective rate right at the seam.

**Sliding window log** solves the boundary problem by storing a timestamp for every request in a sorted set, then counting only entries within the trailing window. Accuracy is perfect, but memory scales linearly with request volume — a bad fit for high-throughput APIs.

**Sliding window counter** is the pragmatic middle ground. It blends the current and previous window counts using a weighted average based on how far you are into the current window. Memory stays constant, and accuracy is close enough for most APIs: the error bound is at most the rate limit itself over the window boundary, which in practice is acceptable.

**Token bucket** is the most expressive model. A bucket holds up to `capacity` tokens. Tokens replenish at a fixed rate. Each request consumes one token; if the bucket is empty, the request is rejected. Token buckets naturally accommodate bursts up to the bucket capacity while enforcing a sustainable average rate — exactly how real network traffic behaves.

## Implementing Token Bucket in Go

The token bucket is straightforward to implement in-process, and Go's standard library makes the math clean. The `golang.org/x/time/rate` package ships a battle-tested implementation built on `time.Now()` that avoids floating-point drift.

<script src="https://gist.github.com/mohashari/788a0bb019b5f1b785e50a397eaa285e.js?file=snippet.go"></script>

This works perfectly for a single server. The moment you run two instances, though, each maintains its own in-memory state — and clients effectively get double the quota. You need a shared store.

## Distributed Rate Limiting with Redis

Redis is the de facto standard for distributed rate limiting because atomic operations (via Lua scripts or `MULTI/EXEC`) eliminate the race conditions that would let clients slip past the limit. The sliding window counter algorithm maps naturally to Redis operations.

The following Lua script runs atomically on the Redis server, ensuring no two API servers can simultaneously read-then-write a stale value:

<script src="https://gist.github.com/mohashari/788a0bb019b5f1b785e50a397eaa285e.js?file=snippet-2.txt"></script>

Calling this script from Go keeps your application logic clean:

<script src="https://gist.github.com/mohashari/788a0bb019b5f1b785e50a397eaa285e.js?file=snippet-3.go"></script>

## Setting Correct Response Headers

A rate limiter without informative headers is a black box to your clients. RFC 6585 and the emerging `RateLimit` header draft define conventions that let clients back off gracefully instead of hammering your API.

<script src="https://gist.github.com/mohashari/788a0bb019b5f1b785e50a397eaa285e.js?file=snippet-4.go"></script>

## Layered Rate Limits with Nginx

For high-volume APIs, push coarse-grained rate limiting to the edge before requests even touch your Go service. Nginx's `ngx_http_limit_req_module` implements a leaky bucket at the proxy layer, and its configuration is declarative:

<script src="https://gist.github.com/mohashari/788a0bb019b5f1b785e50a397eaa285e.js?file=snippet-5.conf"></script>

The `nodelay` flag is critical: without it, Nginx queues excess requests and adds artificial latency rather than rejecting them, which can make your p99 tail latency deceptively high.

## Deploying Redis for Rate Limiting

Rate limiting state needs low latency and high availability, but it doesn't need durability — a fresh Redis instance is a perfectly valid starting state after a restart. This changes your deployment posture. A Redis cluster with `appendonly no` and `save ""` trades persistence for pure throughput:

<script src="https://gist.github.com/mohashari/788a0bb019b5f1b785e50a397eaa285e.js?file=snippet-6.dockerfile"></script>

The `allkeys-lru` eviction policy matters: if memory pressure causes Redis to evict rate limit keys, the client's counter resets — effectively a brief amnesty. That's almost always preferable to Redis refusing writes and causing your application to either fail open or fail closed at the worst possible moment.

## Load Testing Your Limits

Before shipping, verify your limits behave correctly under concurrent load. `hey` or `vegeta` will tell you whether your Lua atomics are actually holding:

<script src="https://gist.github.com/mohashari/788a0bb019b5f1b785e50a397eaa285e.js?file=snippet-7.sh"></script>

A healthy result shows your 429 rate climbing sharply once the limit is hit and stabilizing — not creeping up gradually, which would indicate a race condition in your counter logic.

Rate limiting is one of those features that looks simple until it isn't. The jump from a local in-process limiter to a correct, distributed implementation involves careful attention to atomicity, clock synchronization, and failure modes. Choose token bucket when you need burst tolerance with a hard average; choose sliding window counter when you want smooth enforcement with fixed memory. Push Nginx limits to the edge for cheap, high-volume filtering. Keep your Redis ephemeral, your Lua scripts atomic, and your response headers informative — clients that can read `Retry-After` will back off cleanly instead of hammering a retried 429 into a DDoS of your own making.