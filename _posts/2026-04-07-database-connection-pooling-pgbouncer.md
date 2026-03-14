---
layout: post
title: "Database Connection Pooling: PgBouncer and Beyond"
date: 2026-04-07 07:00:00 +0700
tags: [postgresql, pgbouncer, performance, backend, database]
description: "Understand why connection pooling is critical at scale and configure PgBouncer to handle thousands of concurrent database clients efficiently."
---

Every PostgreSQL connection spawns a dedicated backend process consuming roughly 5–10 MB of memory and requiring a full TCP handshake, authentication exchange, and process fork. At low scale this is invisible. But when your application deploys dozens of pods, each maintaining a connection pool of twenty clients, you're staring down hundreds of simultaneous database processes doing nothing but waiting. PostgreSQL starts thrashing under that pressure — context switching overhead climbs, shared memory contention grows, and the connection limit (`max_connections`) becomes a hard ceiling that walls off your entire system. PgBouncer exists precisely to absorb this mismatch between how applications want to connect and how databases can realistically serve them.

## Why Native Connection Pooling Falls Short

Most database drivers and ORMs offer built-in pooling. Go's `database/sql`, Node's `pg`, SQLAlchemy — they all maintain a pool of persistent connections per application instance. This is genuinely useful, but it doesn't solve the fundamental problem: each pooled connection still holds an open socket to PostgreSQL. Multiply one pool of 10 across 50 app pods and you have 500 live connections, regardless of how much actual SQL traffic flows through them.

The following Go snippet shows a typical application-side pool configuration that feels safe but quietly accumulates backend processes:

<script src="https://gist.github.com/mohashari/b2b10c9b5ccd822e8a257a29d288fcec.js?file=snippet.go"></script>

With PgBouncer in front, your application still sees 20 connections per pod, but PgBouncer multiplexes all 50 pods down to a shared server-side pool of perhaps 50–100 actual PostgreSQL connections. The application gets fast, local socket access; the database sees a manageable number of backend processes.

## Installing and Running PgBouncer

The simplest production-grade deployment runs PgBouncer as a sidecar or dedicated service. Here's a minimal Dockerfile that ships a hardened PgBouncer instance:

<script src="https://gist.github.com/mohashari/b2b10c9b5ccd822e8a257a29d288fcec.js?file=snippet-2.dockerfile"></script>

## Core Configuration

PgBouncer's `pgbouncer.ini` controls everything meaningful. The three pooling modes — `session`, `transaction`, and `statement` — represent fundamentally different connection lifecycle contracts. Transaction pooling delivers the best multiplexing but prohibits session-level features like `SET`, advisory locks, and prepared statements that persist across transactions.

<script src="https://gist.github.com/mohashari/b2b10c9b5ccd822e8a257a29d288fcec.js?file=snippet-3.txt"></script>

User credentials live in a separate `userlist.txt` using SCRAM-SHA-256 hashes. Generate entries with psql:

<script src="https://gist.github.com/mohashari/b2b10c9b5ccd822e8a257a29d288fcec.js?file=snippet-4.sql"></script>

Paste the output line directly into `userlist.txt`. PgBouncer never stores plaintext passwords, and with `auth_type = scram-sha-256` it performs the full challenge-response against this file without touching PostgreSQL at all.

## Tuning `server_pool_size`

The right server pool size depends on your PostgreSQL CPU count and workload mix. A conservative starting formula is `2 × CPU cores` for CPU-bound OLTP workloads, scaling up toward `4 × CPU cores` for I/O-bound workloads with significant wait time. Monitor the `sv_idle`, `sv_used`, and `cl_waiting` columns in the admin console to find the equilibrium point where clients rarely queue.

<script src="https://gist.github.com/mohashari/b2b10c9b5ccd822e8a257a29d288fcec.js?file=snippet-5.sh"></script>

The `cl_waiting` column in `SHOW POOLS` is your most important signal. Any sustained non-zero value means clients are queuing behind an exhausted server pool, and `server_pool_size` should be increased — or your slow queries should be investigated first.

## Health Checks and Reconnect Behavior

PgBouncer probes server connections using `server_check_query` before handing them to clients. The default `SELECT 1` is fine, but if your application uses `search_path` heavily, a no-op query that validates the session state is safer:

<script src="https://gist.github.com/mohashari/b2b10c9b5ccd822e8a257a29d288fcec.js?file=snippet-6.txt"></script>

A full failover scenario — primary database restart, replica promotion, or a network partition — requires PgBouncer to drain and reconnect its server pool. The `RECONNECT` admin command forces an immediate graceful drain without dropping clients:

<script src="https://gist.github.com/mohashari/b2b10c9b5ccd822e8a257a29d288fcec.js?file=snippet-7.sh"></script>

## Beyond PgBouncer: pgpool-II and Odyssey

PgBouncer is deliberately minimal. For read/write splitting, automatic failover, or load balancing across replicas, consider pgpool-II or Citusdata's Odyssey. Odyssey was built by Yandex to handle millions of connections and supports true multi-threaded architecture where PgBouncer is single-threaded. For most services handling under ten thousand concurrent clients, PgBouncer's simplicity is the right tradeoff — fewer moving parts, predictable behavior, and fifteen years of battle-tested deployments. Start with PgBouncer, reach for Odyssey when you have profiled evidence that single-threaded event processing is your bottleneck, not your query patterns.

Connection pooling is not a performance optimization you add later — it is a correctness constraint for multi-instance deployments. Set `pool_mode = transaction`, size your `server_pool_size` against measured CPU and I/O wait ratios, and treat `cl_waiting` as a leading indicator before connection exhaustion becomes a production incident. The ten minutes it takes to drop PgBouncer into your stack will return hours of on-call time over the life of the system.