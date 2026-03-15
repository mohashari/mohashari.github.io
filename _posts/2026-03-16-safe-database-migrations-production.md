---
layout: post
title: "Safe Database Migrations in Production: Schema Changes Without Downtime"
date: 2026-03-16 07:00:00 +0700
tags: [databases, postgresql, devops, migrations, backend]
description: "Master the techniques—expand-contract, online DDL, and shadow tables—that let you evolve your database schema safely in a live system."
---

Every backend engineer has felt that particular dread: a schema migration is running in production, the deployment is paused mid-flight, and somewhere upstream a queue of requests is building up against a table that's locked for an ALTER. Maybe the migration finishes in forty seconds. Maybe it takes twenty minutes on a 200-million-row table. You won't know until it's too late. The good news is that this entire class of problem is avoidable—not by avoiding schema changes, but by learning to make them safely. The techniques are not exotic; they're disciplined, incremental, and repeatable. This post walks through the ones that matter most: expand-contract, online DDL, and shadow tables with dual-write.

## The Core Problem: Locks and Coupling

Most schema migrations fail safely in development and dangerously in production because of scale and concurrency. An `ALTER TABLE` in PostgreSQL that adds a NOT NULL column with no default acquires an `AccessExclusiveLock`, which blocks every read and write against that table until it finishes. On a small table this is invisible. On a large one it's an outage.

The second problem is coupling between application code and schema. If you deploy code that depends on column `user_email_verified` existing before the migration that creates it runs, you get errors. If you run the migration that drops column `legacy_token` before you deploy the code that stops referencing it, you also get errors. Migrations and deploys have to be carefully sequenced, and that sequencing needs a pattern to follow.

## Pattern 1: Expand-Contract

Expand-contract (sometimes called parallel-change) is the most broadly applicable technique. It splits a breaking schema change into three phases deployed across multiple releases.

**Phase 1: Expand.** Add the new structure without removing the old. The application writes to both and reads from the old.

<script src="https://gist.github.com/mohashari/066077b8dfb572aa271a295fbae8eb64.js?file=snippet.sql"></script>

`CREATE INDEX CONCURRENTLY` is PostgreSQL's mechanism for building an index without holding a lock that blocks writes. It takes longer and uses more resources, but it keeps the table live. Always prefer it in production.

**Phase 2: Migrate.** Backfill existing rows in batches. Never run an unbounded `UPDATE` across a large table in one transaction—it locks rows for the duration and generates a massive WAL write that can lag your replicas.

<script src="https://gist.github.com/mohashari/066077b8dfb572aa271a295fbae8eb64.js?file=snippet-2.go"></script>

The keyset cursor (`WHERE id > $1`) is more reliable than `OFFSET` because it stays fast as the dataset grows. The sleep gives other transactions breathing room.

**Phase 3: Contract.** Once all code is reading from the new column and the old column is fully drained, remove it.

<script src="https://gist.github.com/mohashari/066077b8dfb572aa271a295fbae8eb64.js?file=snippet-3.sql"></script>

Each phase is a separate deploy. The total elapsed time might be a week. That's fine—correctness and availability are worth it.

## Pattern 2: Online DDL with Lock Timeouts

Some changes can't easily be restructured as expand-contract—adding a NOT NULL constraint to an existing column, for example. For these, PostgreSQL offers `lock_timeout` and `statement_timeout` as safety rails. If the lock can't be acquired quickly, fail fast rather than queue behind a long-running transaction.

<script src="https://gist.github.com/mohashari/066077b8dfb572aa271a295fbae8eb64.js?file=snippet-4.sql"></script>

Wrap this in your migration framework so it's applied consistently. With `lock_timeout = '2s'`, if any concurrent transaction holds a conflicting lock, the ALTER fails immediately and your migration tool can retry or alert. This converts a potential minutes-long stall into a deterministic failure that your on-call rotation can handle.

For truly large tables, tools like `pg_repack` or `pgroll` perform online restructuring by building a shadow copy of the table in the background, syncing changes via triggers, then doing a fast rename swap. Here's a shell invocation pattern wrapped in a deployment step:

<script src="https://gist.github.com/mohashari/066077b8dfb572aa271a295fbae8eb64.js?file=snippet-5.sh"></script>

`pg_repack` works by creating a new version of the table, installing row-level triggers to capture concurrent writes during the copy, then swapping the tables under a very brief lock. The final swap lock is measured in milliseconds.

## Pattern 3: Shadow Tables and Dual-Write

When migrating a table's fundamental structure—splitting a wide table, changing a primary key type from integer to UUID, or denormalizing for read performance—shadow tables give you a rollback path.

The idea: create the new table in parallel, write to both old and new from application code, verify consistency, then cut reads over incrementally.

<script src="https://gist.github.com/mohashari/066077b8dfb572aa271a295fbae8eb64.js?file=snippet-6.go"></script>

During the dual-write phase, run a consistency checker as a background job. Compare row counts, spot-check individual records, and verify derived fields. Only when the checker reports zero discrepancies do you flip reads to the new table.

<script src="https://gist.github.com/mohashari/066077b8dfb572aa271a295fbae8eb64.js?file=snippet-7.sql"></script>

Zero rows across all three counts means you're clean to cut over.

## Operationalizing the Process

Safe migrations require tooling discipline, not just SQL knowledge. Enforce these practices in your migration framework configuration:

<script src="https://gist.github.com/mohashari/066077b8dfb572aa271a295fbae8eb64.js?file=snippet-8.yaml"></script>

Every migration file should pass a review checklist before merging: Does it use `CONCURRENTLY` for index operations? Does any `UPDATE` touch more than a bounded number of rows per transaction? Does the deploy sequence ensure the code consuming this change ships before or after the migration, not simultaneously?

Schema migrations are not an afterthought to feature development—they are the deployment. The engineers who internalize this ship faster, break less, and sleep better. Expand-contract gives you the discipline to sequence changes across releases. Online DDL techniques give you the PostgreSQL primitives to make individual statements safe. Shadow tables give you the safety net for structural rewrites. Combine all three with short lock timeouts, batched backfills, and a consistency validator, and you have a repeatable system for evolving your schema without ever taking your application offline.