---
layout: post
title: "PostgreSQL Partitioning: Managing Billions of Rows Efficiently"
date: 2026-03-15 07:00:00 +0700
tags: [postgresql, database, performance, partitioning, backend]
description: "Use PostgreSQL table partitioning to dramatically improve query performance and manageability on tables with billions of rows."
---

When your PostgreSQL table crosses the hundred-million-row threshold, queries that once returned in milliseconds start crawling. Indexes balloon in memory, VACUUM struggles to keep up, and your on-call rotation starts getting paged at 3am. The instinct is to throw more hardware at the problem — bigger instances, faster disks — but the real lever is architectural: table partitioning. PostgreSQL's native declarative partitioning, mature since version 10 and significantly improved through versions 12–15, lets you split a single logical table into physical child tables while keeping your application queries unchanged. Done right, it can turn a 45-second analytical query into a 400-millisecond one by eliminating entire partitions before the planner even touches an index.

## Understanding the Partition Types

PostgreSQL supports three partitioning strategies: range, list, and hash. Range partitioning is the most common for time-series and event data — you carve the table into chunks by date or numeric range. List partitioning works when you have a low-cardinality categorical column like `region` or `status`. Hash partitioning distributes rows evenly across a fixed number of buckets, which is ideal when you have no natural partition key but want to spread I/O.

The planner uses a feature called **partition pruning** to skip irrelevant child tables entirely. If your query filters on the partition key, PostgreSQL reads the partition metadata at plan time (static pruning) or at execution time (dynamic pruning) and ignores partitions whose ranges can't contain matching rows. This is why choosing the right partition key is the most consequential decision you'll make.

## Setting Up Range Partitioning on an Events Table

Start with the parent table declaration. Note that you do not define storage — the parent is purely logical.

<script src="https://gist.github.com/mohashari/2f70300567e3589dd02bd4f534800e7b.js?file=snippet.sql"></script>

Now create monthly child partitions. In production you'll automate this, but the raw SQL looks like:

<script src="https://gist.github.com/mohashari/2f70300567e3589dd02bd4f534800e7b.js?file=snippet-2.sql"></script>

## Automating Partition Creation in Go

Manually creating partitions is a recipe for a 2am incident when a partition doesn't exist and inserts start failing. Here's a Go function that creates the next month's partition if it doesn't already exist — run it from a cron job or a startup check.

<script src="https://gist.github.com/mohashari/2f70300567e3589dd02bd4f534800e7b.js?file=snippet-3.go"></script>

## Verifying Partition Pruning with EXPLAIN

Before you ship, confirm the planner is actually pruning. An unintentional sequential scan across all partitions is worse than no partitioning at all.

<script src="https://gist.github.com/mohashari/2f70300567e3589dd02bd4f534800e7b.js?file=snippet-4.sql"></script>

Look for `Partitions selected` in the output. You want to see only one or two partitions listed under `Append`, not all of them. If you see `Partitions selected: 1 (of 15)`, partition pruning is working. If you see `Partitions selected: 15 (of 15)` on a date-filtered query, check that `enable_partition_pruning` is on and that your filter column matches the partition key exactly — casting or wrapping in a function defeats pruning.

## Dropping Old Data Without VACUUM Pain

One of the killer features of partitioning is instant data expiry. Instead of running a DELETE that churns through millions of rows and leaves dead tuples for VACUUM to clean up, you detach and drop an entire partition — a metadata-only operation that takes milliseconds.

<script src="https://gist.github.com/mohashari/2f70300567e3589dd02bd4f534800e7b.js?file=snippet-5.sql"></script>

The `CONCURRENTLY` clause on DETACH (available since PostgreSQL 14) means the operation doesn't hold a lock on the parent table, so reads and writes continue uninterrupted during the detach.

## Monitoring Partition Size and Row Distribution

Keep an eye on partition skew — one oversized partition undermines the whole scheme.

<script src="https://gist.github.com/mohashari/2f70300567e3589dd02bd4f534800e7b.js?file=snippet-6.sql"></script>

Run this weekly and alert if any single partition is more than three times the average size. Skew usually signals either a bad partition key or a burst of backfilled data landing in a single range.

## Configuring Connection Pooling for Partition-Heavy Workloads

When partitions hit the hundreds, the PostgreSQL planner's work to enumerate partition metadata during planning can add latency. PgBouncer with transaction-mode pooling keeps connection overhead low, and you should also tune `max_parallel_workers_per_gather` to let parallel query exploit multiple partitions simultaneously.

<script src="https://gist.github.com/mohashari/2f70300567e3589dd02bd4f534800e7b.js?file=snippet-7.txt"></script>

And in `postgresql.conf`, give the planner room to parallelize across partitions:

<script src="https://gist.github.com/mohashari/2f70300567e3589dd02bd4f534800e7b.js?file=snippet-8.txt"></script>

## The Practical Takeaway

Partitioning is not a magic performance pill — it is a deliberate trade-off. You gain fast pruning, instant data expiry, and manageable index sizes, but you introduce operational complexity: partitions must be pre-created, indexes must be maintained per-partition, and foreign keys pointing *into* a partitioned table require careful design. The right starting point is a range partition on your most selective time-based filter column, monthly granularity for data older than a year, and an automated partition-creation job running at least 48 hours ahead of schedule. Instrument your `EXPLAIN ANALYZE` output in staging before going to production, and build the detach-and-drop workflow before you need it — not the night your disk fills up.