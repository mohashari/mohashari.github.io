---
layout: post
title: "Database Replication Internals: WAL, Logical Replication, and Read Scaling"
date: 2026-03-17 07:00:00 +0700
tags: [postgresql, databases, replication, scalability, internals]
description: "Demystify WAL-based and logical replication in PostgreSQL to build read replicas, CDC pipelines, and zero-downtime migration strategies."
---

Every time your primary PostgreSQL instance starts sweating under read-heavy traffic, the instinct is to throw more hardware at it. But before you vertically scale your way into a budget crisis, there's a more elegant path: understanding how PostgreSQL actually moves data between nodes, and using that machinery intentionally. WAL-based replication is not just a high-availability feature — it's a fundamental primitive for read scaling, change data capture, and zero-downtime migrations. Most engineers treat it as infrastructure someone else configured. This post is about understanding it deeply enough to use it as a tool.

## How the Write-Ahead Log Works

PostgreSQL never writes directly to heap files on disk. Every change — INSERT, UPDATE, DELETE, even VACUUM — is first recorded sequentially into the Write-Ahead Log. Only after the WAL record is flushed to disk does PostgreSQL consider a transaction committed. This sequential write pattern is what makes PostgreSQL both crash-safe and replicatable: the WAL is a total-ordered, append-only record of every state transition in the database.

Each WAL record contains the LSN (Log Sequence Number), a monotonically increasing pointer into the WAL stream. When a replica connects to a primary, it simply tells the primary its current LSN and asks for everything after that. The primary streams WAL records, the replica replays them, and the replica's state converges to match the primary's.

You can inspect your current WAL position and replication lag directly:

<script src="https://gist.github.com/mohashari/5ea093f989c5dd4f18975cbd96844f04.js?file=snippet.sql"></script>

The difference between `sent_lsn` and `replay_lsn` is your replication lag in bytes. `replay_lag` gives you the time dimension. Monitoring both is critical — a replica can be caught up in bytes but still be seconds behind if it's CPU-bound replaying complex transactions.

## Setting Up Streaming Replication

Physical (streaming) replication ships WAL at the byte level. The replica is a byte-for-byte copy of the primary — same tablespace layout, same version, same schema. This is the right choice for read replicas and hot standbys. Configuration is minimal but the semantics matter.

<script src="https://gist.github.com/mohashari/5ea093f989c5dd4f18975cbd96844f04.js?file=snippet-2.sh"></script>

<script src="https://gist.github.com/mohashari/5ea093f989c5dd4f18975cbd96844f04.js?file=snippet-3.txt"></script>

On the replica, a `standby.signal` file in the data directory signals PostgreSQL to start in standby mode. PostgreSQL 12+ uses `primary_conninfo` in `postgresql.conf` directly instead of the old `recovery.conf`. The replica begins streaming from where the base backup left off.

## Logical Replication: Row-Level, Schema-Selective

Physical replication is all-or-nothing. Logical replication operates at the row level: you publish specific tables, and subscribers consume a decoded stream of row changes. This enables selective replication between different PostgreSQL versions, cross-schema migrations, and feeding change data capture pipelines.

<script src="https://gist.github.com/mohashari/5ea093f989c5dd4f18975cbd96844f04.js?file=snippet-4.sql"></script>

The key operational concern with logical replication slots is that they hold WAL until the subscriber consumes it. An inactive slot with a lagging subscriber will cause WAL to accumulate indefinitely and eventually fill your disk. Always monitor `pg_replication_slots` in production.

## Building a CDC Pipeline with pgoutput

Logical replication's `pgoutput` plugin (built into PostgreSQL 10+) is the foundation for change data capture. You can write a Go consumer that connects directly to the replication protocol and processes row-level changes in real time — no polling, no triggers.

<script src="https://gist.github.com/mohashari/5ea093f989c5dd4f18975cbd96844f04.js?file=snippet-5.go"></script>

This consumer receives a decoded stream of typed row changes. In production you'd route these events to Kafka, update a search index, or invalidate cache entries — all without polling or touching application code.

## Connection Routing for Read Scaling

With replicas in place, you need application-level routing to direct read queries to replicas and writes to the primary. PgBouncer can handle connection pooling, but routing logic lives at the application layer. A clean Go pattern uses separate database handles with explicit routing:

<script src="https://gist.github.com/mohashari/5ea093f989c5dd4f18975cbd96844f04.js?file=snippet-6.go"></script>

One critical nuance: after a write, don't immediately read from a replica. Replication lag means the replica might not have the row yet. For read-your-own-writes consistency, either route post-write reads to the primary for a short window, or use synchronous replication for those specific transactions.

## Zero-Downtime Migration with Logical Replication

The most powerful use of logical replication is migrating between PostgreSQL versions or major schema changes without downtime. The pattern: spin up a new instance running the target version, replicate all data, apply schema changes on the new instance, then cut over traffic with a brief write pause.

<script src="https://gist.github.com/mohashari/5ea093f989c5dd4f18975cbd96844f04.js?file=snippet-7.sql"></script>

The WAL-based replication stack in PostgreSQL is one of the most underutilized tools in the backend engineer's toolkit. Physical replication gives you read scale and high availability with minimal operational overhead. Logical replication gives you version-safe migrations, selective sync, and a real-time change stream that can feed every downstream system in your architecture. The investment is learning the LSN model, monitoring replication slots religiously, and thinking carefully about read-your-own-writes consistency at the application layer. Once those patterns are internalized, you stop treating replication as infrastructure and start treating it as a first-class feature.