---
layout: post
title: "Data Structures Every Software Engineer Uses in Production"
date: 2026-03-15 07:00:00 +0700
tags: [algorithms, data-structures, backend, performance, computer-science]
description: "Beyond arrays and hash maps — bloom filters, skip lists, LRU caches, tries, and the data structures that power real production systems."
---

CS courses teach arrays, linked lists, and binary trees. Production systems use bloom filters, skip lists, and consistent hash rings. Here's what actually matters.

## Hash Map — The Foundation

You know this. But do you know when it breaks?

<script src="https://gist.github.com/mohashari/d65b6be9e9da674575373ad9a6b98bbf.js?file=snippet.go"></script>

**Production gotcha**: map iteration order is randomized in Go intentionally. Don't rely on insertion order.

## LRU Cache — Bounded Memory with Smart Eviction

<script src="https://gist.github.com/mohashari/d65b6be9e9da674575373ad9a6b98bbf.js?file=snippet-2.go"></script>

Used in: HTTP response caches, DNS resolvers, CPU caches, database buffer pools.

## Bloom Filter — Probabilistic Set Membership

"Is this email in the spam list?" Checking a million-entry database for every incoming email is expensive. A bloom filter answers "definitely not" or "probably yes" in O(1) with very little memory.

<script src="https://gist.github.com/mohashari/d65b6be9e9da674575373ad9a6b98bbf.js?file=snippet-3.go"></script>

Used in: Cassandra (avoid disk reads for missing keys), Chrome (safe browsing), Bitcoin (SPV nodes), CDNs (cache key existence).

**False positive rate** with m=1M bits, k=7 hash functions, n=100k items ≈ 0.8%.

## Trie — Prefix-Based Lookup

<script src="https://gist.github.com/mohashari/d65b6be9e9da674575373ad9a6b98bbf.js?file=snippet-4.go"></script>

Used in: autocomplete, IP routing tables (longest prefix match), spell checkers, HTTP router matching.

## Ring Buffer — Zero-Allocation Queue

<script src="https://gist.github.com/mohashari/d65b6be9e9da674575373ad9a6b98bbf.js?file=snippet-5.go"></script>

Used in: network packet buffers, audio streaming, log pipelines, LMAX Disruptor pattern.

## Skip List — Ordered Data with O(log n) Operations

Redis's sorted sets use skip lists. They provide O(log n) insert/delete/search like balanced trees, but are simpler to implement and cache-friendly.

<script src="https://gist.github.com/mohashari/d65b6be9e9da674575373ad9a6b98bbf.js?file=snippet.txt"></script>

Each element has a random "height". Search starts at the top level and drops down when the next element is too large.

## Consistent Hash Ring — Distributed Key Routing

<script src="https://gist.github.com/mohashari/d65b6be9e9da674575373ad9a6b98bbf.js?file=snippet-6.go"></script>

Adding or removing a node only remaps `1/n` of keys instead of all keys. Used in: Cassandra, DynamoDB, Memcached, CDN edge routing.

## When to Use What

| Structure | Best For | Avoid When |
|-----------|----------|------------|
| Hash Map | O(1) lookup by exact key | Ordered iteration needed |
| LRU Cache | Bounded memory hot-path cache | All data must be retained |
| Bloom Filter | Probabilistic existence check | False positives unacceptable |
| Trie | Prefix search, autocomplete | Random key access |
| Ring Buffer | High-throughput fixed-size queue | Variable-size messages |
| Consistent Hash | Distributed key routing | Single-node systems |

Knowing these structures — and when to reach for them — separates engineers who optimize for the problem from engineers who optimize prematurely.
