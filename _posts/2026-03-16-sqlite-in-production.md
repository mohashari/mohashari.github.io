---
layout: post
title: "SQLite in Production: When the Simplest Database Is the Right Choice"
date: 2026-03-16 07:00:00 +0700
tags: [databases, sqlite, backend, performance, architecture]
description: "Understand when SQLite's single-file, zero-configuration model outperforms client-server databases and how to run it reliably at scale with Litestream and read replicas."
---

# SQLite in Production: When the Simplest Database Is the Right Choice

Every backend engineer has reached for PostgreSQL or MySQL reflexively, treating client-server databases as the obvious default. But there's a class of production workload — single-server applications, edge deployments, read-heavy services, embedded analytics — where SQLite quietly outperforms its more complex cousins. Not because it's a toy, but because it eliminates an entire category of network latency, connection pooling overhead, and operational complexity. The SQLite authors put it plainly: it is the most widely deployed database engine in the world. Your phone has dozens of SQLite databases on it right now. The question isn't whether SQLite is production-grade — it's whether your workload fits its model.

## Understanding SQLite's Concurrency Model

SQLite's reputation for poor concurrency stems from misunderstanding its locking model. By default, SQLite uses a coarse-grained writer lock: one writer at a time, but multiple concurrent readers. This is fine for most applications. The key lever is enabling WAL (Write-Ahead Logging) mode, which decouples readers from writers entirely, allowing reads to proceed unblocked while a write is in progress.

<script src="https://gist.github.com/mohashari/6a91649b3768a23a3fd9203429f0d72a.js?file=snippet.sql"></script>

These pragmas are not optional for production. `busy_timeout` prevents immediate SQLITE_BUSY errors under contention by spinning for up to 5 seconds. `synchronous = NORMAL` is safe with WAL mode and dramatically reduces fsync calls. `mmap_size` lets the OS page cache handle reads without copying data into SQLite's own buffers.

## Connection Pool Configuration in Go

Because SQLite serializes writes at the file level, your connection pool strategy is different from PostgreSQL. You want multiple read connections but exactly one write connection — or at minimum, a short queue depth for writers.

<script src="https://gist.github.com/mohashari/6a91649b3768a23a3fd9203429f0d72a.js?file=snippet-2.go"></script>

The `mode=ro` query parameter on the read connection enforces read-only access at the driver level — a safety net that prevents accidental writes from read paths during refactors.

## Streaming Replication with Litestream

The most common objection to SQLite in production is durability: if the disk dies, the data is gone. Litestream solves this by continuously replicating the WAL to object storage — S3, GCS, or Azure Blob — with sub-second latency and zero application changes.

<script src="https://gist.github.com/mohashari/6a91649b3768a23a3fd9203429f0d72a.js?file=snippet-3.yaml"></script>

The `retention` setting controls how far back you can restore to. Combined with `snapshot-interval`, Litestream creates periodic full snapshots so restore time stays bounded — you never need to replay 30 days of WAL segments to recover from yesterday's backup.

## Dockerfile: Running App and Litestream Together

In containerized environments, the idiomatic pattern is running Litestream as a supervisor process that launches your application after the database is restored.

<script src="https://gist.github.com/mohashari/6a91649b3768a23a3fd9203429f0d72a.js?file=snippet-4.dockerfile"></script>

The `-exec` flag is the key insight: Litestream forks your application as a child process, intercepts SIGTERM, flushes the final WAL frames to object storage, and only then exits. Your application gets clean shutdown without data loss.

## Embedding SQLite in a Multi-Tenant Service

SQLite's single-file model is a feature when you need hard tenant isolation. Each tenant gets their own database file, eliminating cross-tenant query interference entirely. Schema migrations run per-file, so you can migrate tenants incrementally without a multi-hour table lock on a shared database.

<script src="https://gist.github.com/mohashari/6a91649b3768a23a3fd9203429f0d72a.js?file=snippet-5.go"></script>

An LRU eviction policy on `s.pool` keeps file descriptor usage bounded. With 10,000 tenants and typical access patterns, you'll hold 50-100 open connections at any given time — well within OS limits.

## Online Backup for Point-in-Time Recovery

Beyond Litestream, SQLite's built-in backup API provides a safe, online hot-backup without taking a write lock for the full duration. This is useful for shipping snapshots to a staging environment or triggering manual recovery points before risky migrations.

<script src="https://gist.github.com/mohashari/6a91649b3768a23a3fd9203429f0d72a.js?file=snippet-6.go"></script>

## When to Draw the Line

SQLite becomes the wrong choice when your write throughput exceeds roughly 1,000 transactions per second on commodity hardware, when you need horizontal write scaling across multiple nodes, or when your team operates PostgreSQL and already has tooling, runbooks, and expertise invested there. It also struggles with very large datasets where the single-file model becomes an operational burden — vacuuming a 500GB SQLite file is a multi-hour offline operation.

For everything else — internal tools, small SaaS applications, edge nodes, analytics sidecars, CLI tools that need structured storage, and any service where each entity naturally owns its data — SQLite with WAL mode, Litestream replication, and a sensible connection pool is not a compromise. It is the correct architecture. You gain transactional integrity, full SQL expressiveness, zero network hops, and a backup story that fits on a single YAML file. The database that ships inside every smartphone is more than ready for your backend service.