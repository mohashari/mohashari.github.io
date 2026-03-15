---
layout: post
title: "Redis Streams: Building Reliable Event Pipelines with Consumer Groups"
date: 2026-03-16 07:00:00 +0700
tags: [redis, streaming, backend, distributed-systems, messaging]
description: "Use Redis Streams and consumer groups to build durable, at-least-once event pipelines that rival Kafka for moderate-throughput workloads."
---

Every distributed system eventually faces the same unglamorous problem: you have work that needs to happen, but the service doing that work goes down mid-flight. Message acknowledged, database not updated, email never sent. You can reach for Kafka, but that means a ZooKeeper cluster, broker replication configuration, and a team willing to operate it. For many backend teams running moderate-throughput workloads — tens of thousands of messages per second or fewer — Redis Streams offers a compelling middle ground: persistent, consumer-group-aware event pipelines built on infrastructure you probably already have.

## What Redis Streams Actually Are

Redis Streams, introduced in Redis 5.0, are an append-only log data structure with a twist: each entry has a unique auto-generated ID based on millisecond timestamp and sequence number. Unlike pub/sub (which is fire-and-forget) or lists (which lack consumer group semantics), streams provide durable storage, consumer groups with acknowledgement tracking, and a Pending Entries List (PEL) that lets you reclaim messages from crashed consumers. Think of it as a lightweight Kafka topic living in your Redis keyspace.

Before writing any application code, it helps to understand the shape of a stream at the command level. You can interact with it directly via `redis-cli` to verify behavior end-to-end.

<script src="https://gist.github.com/mohashari/0370a891bca0c25b4e05da9dc0b5ce64.js?file=snippet.sh"></script>

The `>` symbol in `XREADGROUP` is special — it means "give me messages that haven't been delivered to any consumer in this group yet." This is how Redis ensures each message goes to exactly one consumer within a group.

## Producing Events from Go

In a real system, your producer is typically a service responding to user actions. The key discipline here is writing to the stream inside the same database transaction as your business logic change, or as close to atomically as your architecture allows. Here we use `go-redis/v9`.

<script src="https://gist.github.com/mohashari/0370a891bca0c25b4e05da9dc0b5ce64.js?file=snippet-2.go"></script>

`MaxLen` with `Approx: true` is important in production. Without it your stream grows forever. The approximate trim lets Redis batch the cleanup, costing far less CPU than exact trimming on every write.

## Setting Up Consumer Groups on Startup

Consumer groups must exist before consumers can read. A safe pattern is to create the group idempotently at application startup — attempting creation and ignoring the "already exists" error.

<script src="https://gist.github.com/mohashari/0370a891bca0c25b4e05da9dc0b5ce64.js?file=snippet-3.go"></script>

Passing `"0"` instead of `"$"` means the group will read from the beginning of the stream on first creation — useful when you want new deployments to process backlogged messages rather than skip them.

## The Core Consumer Loop

A robust consumer needs to handle two cases: new messages (using `>`) and messages stuck in the PEL from a previous crash (using `"0"` with `XAUTOCLAIM` or manual `XPENDING`). Here is a single-pass loop covering both:

<script src="https://gist.github.com/mohashari/0370a891bca0c25b4e05da9dc0b5ce64.js?file=snippet-4.go"></script>

Notice the intentional asymmetry: `XACK` is only called on success. Failed messages remain in the PEL and will be reclaimed by the dead-letter handler below. This is how you achieve at-least-once delivery — messages stay pending until you explicitly confirm them.

## Reclaiming Stuck Messages

Consumers crash. Pods get OOM-killed. Without a reclaim loop, messages pile up in the PEL forever. `XAUTOCLAIM` (Redis 6.2+) atomically reassigns messages idle longer than a threshold to a new consumer:

<script src="https://gist.github.com/mohashari/0370a891bca0c25b4e05da9dc0b5ce64.js?file=snippet-5.go"></script>

Run this reclaim loop on a ticker — every 30–60 seconds is typically sufficient. Pair it with a dead-letter stream (`orders:dlq`) so poison messages don't block healthy processing indefinitely.

## Monitoring Stream Lag

Operational visibility is non-negotiable. Stream lag — the gap between the latest entry and the last acknowledged entry per consumer group — is your primary health signal.

<script src="https://gist.github.com/mohashari/0370a891bca0c25b4e05da9dc0b5ce64.js?file=snippet-6.go"></script>

`Lag` is the count of entries in the stream not yet delivered to the group. If this number grows monotonically, your consumers are falling behind and you need to scale out — which means adding consumers with distinct names to the same group. Redis automatically load-balances delivery across all active consumers in a group.

## Running Locally with Docker Compose

A minimal local environment for development and integration testing:

<script src="https://gist.github.com/mohashari/0370a891bca0c25b4e05da9dc0b5ce64.js?file=snippet-7.yaml"></script>

Note the `maxmemory-policy`. In stream-heavy workloads you want `noeviction` if durability is critical — `allkeys-lru` would silently evict stream entries under memory pressure, which defeats the durability guarantee. Choose based on whether Redis is your system of record or a processing buffer with another durable store downstream.

Redis Streams won't replace Kafka for high-throughput, multi-datacenter replication scenarios, but for the vast majority of backend event pipelines — order processing, notification queues, audit trails, async job dispatch — they offer a remarkably solid foundation. The consumer group model gives you exactly-once-per-group delivery semantics, the PEL gives you crash recovery, and `XAUTOCLAIM` gives you automatic failure handling, all without any infrastructure beyond the Redis instance you're likely already depending on. Start with a single consumer group per stream, add reclaim logic from day one, expose lag as a metric, and you'll have a production-grade event pipeline running before your Kafka cluster finishes bootstrapping.