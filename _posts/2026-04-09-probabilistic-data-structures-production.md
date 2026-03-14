---
layout: post
title: "Probabilistic Data Structures: Bloom Filters, HyperLogLog, and Count-Min Sketch"
date: 2026-04-09 07:00:00 +0700
tags: [data-structures, algorithms, redis, backend, performance]
description: "Apply Bloom filters, HyperLogLog, and Count-Min Sketch to solve large-scale membership, cardinality, and frequency problems with minimal memory."
---

At a certain scale, exact answers become a luxury you can't afford. When your Redis cluster is tracking whether a URL has been crawled, your fraud detection system is checking if an IP has been seen before, or your analytics pipeline is counting unique visitors across billions of events — the memory cost of perfect accuracy becomes prohibitive. A hash set of 100 million strings might consume gigabytes of RAM. Yet for many of these problems, a confident "probably yes" or a count that's 99.5% accurate is operationally indistinguishable from the truth. Probabilistic data structures exploit this insight ruthlessly: by accepting a small, mathematically bounded error rate, they achieve orders-of-magnitude better space efficiency than their exact counterparts. This post walks through three battle-tested structures — Bloom filters, HyperLogLog, and Count-Min Sketch — with practical Go and Redis examples you can drop into production systems today.

## Bloom Filters: Membership Testing Without the Memory Tax

A Bloom filter answers one question: "Have I seen this element before?" It can return false positives (claiming it has seen something it hasn't), but it will *never* return false negatives. If a Bloom filter says "no," the element is definitively absent. This asymmetry is what makes it useful — and safe — for cache bypass decisions, duplicate suppression, and pre-filtering expensive lookups.

The mechanism is elegant: a bit array of size `m`, and `k` independent hash functions. When you insert an element, you set bits at the `k` positions produced by hashing it. To query, you check if *all* those positions are set. A single unset bit means the element was never inserted.

Here's a minimal, correct Bloom filter in Go using double-hashing (a common trick to simulate `k` hash functions from two):

<script src="https://gist.github.com/mohashari/7b54583a778321bbd3beb04e9b5d0de4.js?file=snippet.go"></script>

For distributed systems, you almost certainly want Redis-backed Bloom filters rather than in-process ones. RedisBloom (now part of Redis Stack) gives you a persistent, cluster-safe implementation:

<script src="https://gist.github.com/mohashari/7b54583a778321bbd3beb04e9b5d0de4.js?file=snippet-2.sh"></script>

## HyperLogLog: Counting Distinct Elements at Scale

Counting unique visitors, unique IPs, or distinct query terms across billions of events is a cardinality estimation problem. A naive set grows linearly with the number of distinct elements. HyperLogLog uses a remarkable observation about the distribution of leading zeros in hash values to estimate cardinality using only ~12 KB of memory regardless of the dataset size, with a typical error of about 0.81%.

The intuition: if you hash elements uniformly and track the maximum number of leading zeros seen, you can estimate how many distinct elements you've processed. HyperLogLog extends this by partitioning into many sub-streams and averaging, which dramatically reduces variance.

Redis has native HyperLogLog support with `PFADD` and `PFCOUNT`. The `PF` prefix honors Philippe Flajolet, who developed the algorithm:

<script src="https://gist.github.com/mohashari/7b54583a778321bbd3beb04e9b5d0de4.js?file=snippet-3.go"></script>

A common pattern is daily HLL keys that get merged into weekly and monthly aggregates on a schedule. This gives you time-windowed cardinality with negligible storage overhead.

## Count-Min Sketch: Frequency Estimation Under Heavy Hitters

When you need to answer "how often has this item appeared?" — think rate limiting, trending topic detection, or heavy-hitter analysis — Count-Min Sketch is the right tool. It's a 2D array of counters (depth `d`, width `w`) with `d` hash functions. On each update, you increment one cell per row. To query the frequency of an item, you take the minimum across all `d` rows. The minimum eliminates hash collision inflation, giving an estimate that is always an overcount, but bounded.

<script src="https://gist.github.com/mohashari/7b54583a778321bbd3beb04e9b5d0de4.js?file=snippet-4.go"></script>

For an API rate limiter using Count-Min Sketch, you can track per-IP request frequency without maintaining per-IP state that grows unboundedly:

<script src="https://gist.github.com/mohashari/7b54583a778321bbd3beb04e9b5d0de4.js?file=snippet-5.go"></script>

## Putting It Together: When to Reach for Each

The decision matrix is straightforward. Use a **Bloom filter** when you need membership tests and can tolerate false positives but not false negatives — crawl deduplication, spam detection, cache bypass optimization. Use **HyperLogLog** when you need cardinality estimates over large streams — unique visitor counts, A/B test reach, distinct query terms in analytics. Use **Count-Min Sketch** when you need frequency estimates — heavy-hitter detection, rate limiting, trending content identification.

The unifying principle across all three is that they trade exactness for space-efficiency within a mathematically guaranteed error bound. Unlike approximations that fail unpredictably, these structures let you reason about the worst-case error at design time and tune the parameters — false positive rate for Bloom filters, standard error for HyperLogLog, epsilon and delta for Count-Min Sketch — to match your application's actual tolerance. In practice, a Redis instance running all three for a high-traffic application will use megabytes where a naive exact approach would demand gigabytes. That's not an implementation detail — it's the difference between a system that scales and one that doesn't.