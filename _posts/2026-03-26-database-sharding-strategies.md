---
layout: post
title: "Database Sharding Strategies: Horizontal Scaling Done Right"
date: 2026-03-26 07:00:00 +0700
tags: [database, sharding, scalability, postgresql, backend]
description: "Choose and implement the right sharding strategy — range, hash, or directory-based — to scale your database beyond a single node."
---

`★ Insight ─────────────────────────────────────`
- This blog post uses Go for orchestration logic because Go's explicit error handling and struct-based modeling maps naturally to sharding key decisions
- SQL examples will target PostgreSQL's native partitioning syntax, which serves as a useful analogy for application-level sharding
- Directory-based sharding is the most flexible but requires a coordination layer — worth showcasing with a simple in-memory lookup table
`─────────────────────────────────────────────────`

At some point, every successful database hits a wall. Writes queue up, read replicas can't keep pace, and your single Postgres node is burning through IOPS like it's trying to win a race against hardware physics. Vertical scaling — throwing more RAM, faster disks, bigger CPUs at the problem — buys time but never solves it. The real answer is horizontal scaling: splitting your data across multiple independent database nodes, each responsible for a subset of the total dataset. This is sharding, and while the concept is simple, the implementation choices you make early will determine whether your system scales gracefully to ten shards or collapses under the weight of cross-shard queries and uneven data distribution. This post walks through the three dominant sharding strategies — range-based, hash-based, and directory-based — with working code examples so you can evaluate the trade-offs before you commit.

## The Core Problem: Choosing a Shard Key

Before picking a strategy, you need a shard key — the column (or composite of columns) that determines which shard owns a given row. A bad shard key causes hotspots: one shard handles 80% of traffic while the rest sit idle. A good shard key distributes load evenly and aligns with your most common query patterns so cross-shard scatter is rare.

<script src="https://gist.github.com/mohashari/a0db896f837da67ac2d07e752ac0ea43.js?file=snippet.sql"></script>

## Strategy 1: Range-Based Sharding

Range sharding assigns contiguous key ranges to shards. Shard 0 owns `user_id` 1–1,000,000, shard 1 owns 1,000,001–2,000,000, and so on. The advantage is locality: range queries like "all orders created this month" can often be routed to a single shard. The disadvantage is that monotonically increasing keys — auto-incrementing IDs, timestamps — create write hotspots because all new data lands on the highest shard.

<script src="https://gist.github.com/mohashari/a0db896f837da67ac2d07e752ac0ea43.js?file=snippet-2.go"></script>

PostgreSQL's declarative partitioning mirrors range sharding and is a good way to prototype the layout before distributing across hosts:

<script src="https://gist.github.com/mohashari/a0db896f837da67ac2d07e752ac0ea43.js?file=snippet-3.sql"></script>

## Strategy 2: Hash-Based Sharding

Hash sharding applies a hash function to the shard key and takes the modulo of the shard count. Distribution is statistically even regardless of key shape, which eliminates write hotspots with auto-increment IDs. The trade-off is that range queries become scatter-gather operations — to answer "orders between IDs 5000 and 7000" you must query every shard and merge results. Hash sharding is ideal when point lookups dominate and range queries are rare.

<script src="https://gist.github.com/mohashari/a0db896f837da67ac2d07e752ac0ea43.js?file=snippet-4.go"></script>

A critical operational concern with hash sharding is **resharding**: adding a new shard invalidates the modulo mapping and requires migrating a large fraction of data. The standard mitigation is consistent hashing, which minimizes data movement when the ring grows:

<script src="https://gist.github.com/mohashari/a0db896f837da67ac2d07e752ac0ea43.js?file=snippet-5.go"></script>

## Strategy 3: Directory-Based Sharding

Directory sharding uses a lookup table — often stored in a fast key-value store like Redis — to map shard keys to shard locations explicitly. This is the most flexible strategy: you can move individual tenants between shards without a full resharding operation, and you can co-locate related data deliberately. The cost is the lookup table itself becoming a critical dependency and potential bottleneck.

<script src="https://gist.github.com/mohashari/a0db896f837da67ac2d07e752ac0ea43.js?file=snippet-6.go"></script>

## Choosing in Practice

The choice between strategies is rarely absolute. Large SaaS platforms often layer them: consistent hashing at the coarse level for global distribution, with directory-based overrides for enterprise tenants that need isolation guarantees. Time-series workloads almost always reach for range sharding because pruning old data means simply dropping old shard files. OLTP systems with uniform random access are a natural fit for hash sharding.

Whichever strategy you choose, resist the temptation to shard prematurely. Start with a single well-indexed Postgres instance, add read replicas when reads dominate, and introduce connection pooling via PgBouncer before you split data at all. Sharding introduces real operational complexity — distributed transactions become coordination problems, schema migrations must run across every shard, and debugging requires aggregating logs from multiple nodes. When you do shard, encode the shard key in your application's routing layer cleanly, keep cross-shard queries explicit rather than transparent, and monitor per-shard write throughput from day one so you catch hotspots before they become incidents.

`★ Insight ─────────────────────────────────────`
- Consistent hashing reduces data movement on reshard from O(n) to O(n/k) where k is the new number of nodes — this is the reason Cassandra, DynamoDB, and Riak all use it
- PostgreSQL's native partitioning is not the same as sharding (partitions still live on one server) but it's a low-risk way to validate your key choice before distributing
- The directory pattern is effectively how multi-tenant SaaS platforms implement "tenant isolation" — each enterprise customer can be pinned to a dedicated shard for compliance or performance SLA reasons
`─────────────────────────────────────────────────`