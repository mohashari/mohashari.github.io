---
layout: post
title: "Bloom Filters and Probabilistic Data Structures for Backend Engineers"
date: 2026-03-18 07:00:00 +0700
tags: [data-structures, algorithms, performance, backend, caching]
description: "Apply Bloom filters, HyperLogLog, Count-Min Sketch, and T-Digest to solve large-scale deduplication, cardinality estimation, and frequency tracking problems efficiently."
---

# Bloom Filters and Probabilistic Data Structures for Backend Engineers

At scale, exact answers become expensive. When you're tracking which of 500 million users have seen a notification, counting unique visitors across a distributed cluster, or estimating the frequency of URLs hitting your rate limiter, the naive approach — a hash set, a full HyperLogLog table, or a sorted count map — demands memory and latency your system can't afford. Probabilistic data structures trade exactness for efficiency, accepting a controlled, mathematically bounded error in exchange for orders-of-magnitude improvements in space and speed. This isn't engineering sloppiness; it's a deliberate design choice that powers systems at Redis, Cassandra, Google BigQuery, and Cloudflare. Understanding when and how to apply Bloom filters, HyperLogLog, Count-Min Sketch, and T-Digest is the difference between a system that scales and one that collapses under its own bookkeeping.

---

## Bloom Filters: Membership Testing Without the Memory

A Bloom filter answers one question: "Have I seen this element before?" It can return false negatives — never. It can return false positives — with a configurable probability. The underlying structure is a bit array of size `m` and `k` independent hash functions. On insertion, all `k` hashes set bits; on query, all `k` bits must be set to return "probably yes." The false positive rate is tunable: more bits per element means lower error.

The canonical use case is deduplication before expensive I/O — checking whether a URL is already indexed before hitting your database, or whether a cache key exists before a network call.

<script src="https://gist.github.com/mohashari/b7f9cca0185869cc1b16e14a1e44142b.js?file=snippet.go"></script>

In production, use Redis's native `BF.ADD` / `BF.EXISTS` commands (RedisBloom module), which persist the filter and support scaling parameters without maintaining in-process state.

<script src="https://gist.github.com/mohashari/b7f9cca0185869cc1b16e14a1e44142b.js?file=snippet-2.sh"></script>

---

## HyperLogLog: Counting Uniques Without Counting Them

HyperLogLog estimates the cardinality of a set using a fixed ~1.5 KB of memory regardless of how many elements you've seen — whether 1,000 or 1 billion. It hashes each element, observes the position of the leading zero bits (a proxy for rarity), and uses that distribution to estimate total unique count. The standard error is approximately 1.04/√m where `m` is the number of registers.

PostgreSQL ships with a mature HyperLogLog extension. This is how you'd estimate daily active users across a distributed event stream without a full `COUNT(DISTINCT ...)` scan:

<script src="https://gist.github.com/mohashari/b7f9cca0185869cc1b16e14a1e44142b.js?file=snippet-3.sql"></script>

The `hll_union` call is the key insight: HyperLogLog sketches are *mergeable*. You can shard writes across 16 Postgres replicas, store one sketch per shard, and union them at query time. No coordinator node needed during ingestion.

---

## Count-Min Sketch: Frequency Estimation Under Pressure

A Count-Min Sketch (CMS) answers: "How many times have I seen this element?" It's a 2D array of counters with `d` rows (each using an independent hash) and `w` columns. Insertion increments `d` cells; query returns the minimum across those `d` cells. The minimum bound eliminates overcounting from hash collisions — you may overestimate, never underestimate.

The primary use case is heavy-hitter detection: which API endpoints, IP addresses, or product IDs are spiking right now?

<script src="https://gist.github.com/mohashari/b7f9cca0185869cc1b16e14a1e44142b.js?file=snippet-4.go"></script>

In a real rate limiter, you'd run this CMS in a sidecar process, flushing counts to Redis Sorted Sets every 10 seconds to surface the top-K offenders for alerting.

---

## T-Digest: Accurate Tail Percentiles on Streaming Data

Measuring p99 latency on a stream of 10 million requests per minute is impossible with exact methods — you'd need to store every data point. T-Digest solves this by maintaining a compact summary of centroids, deliberately allocating more precision at the tails (p99, p999) where it matters most for SLOs, and less in the middle.

Redis also ships T-Digest natively. Here's how you'd instrument a Go HTTP handler to feed it:

<script src="https://gist.github.com/mohashari/b7f9cca0185869cc1b16e14a1e44142b.js?file=snippet-5.go"></script>

The design insight in T-Digest — variable precision concentrated at the extremes — is the right tradeoff for SLO monitoring. You care deeply whether p99 is 180ms or 220ms; you care almost nothing whether p50 is 12ms or 14ms.

---

## Composing Them: A Deduplication Pipeline

A realistic deduplication pipeline for a webhook delivery system combines all three structures. The Bloom filter gates expensive database lookups; the Count-Min Sketch detects retry storms from individual sources; the HyperLogLog tracks unique receiving endpoints without a full cardinality scan.

<script src="https://gist.github.com/mohashari/b7f9cca0185869cc1b16e14a1e44142b.js?file=snippet-6.go"></script>

---

Probabilistic data structures are not approximations you settle for — they are tools you choose deliberately when the cost of exactness exceeds the cost of bounded error. A Bloom filter with a 0.1% false positive rate means one in a thousand duplicate events slips through, which is almost always acceptable compared to the alternative of a 50GB Redis set. HyperLogLog gives you daily active user counts accurate to within 2% using 12 KB of memory per sketch. Count-Min Sketch detects traffic anomalies in real time without ever materializing a full frequency map. T-Digest computes your p99.9 SLO with less than 1% error on a single digit of kilobytes. The next time a scaling problem arrives dressed as a data problem, consider whether you need the exact answer — or just a reliable one.