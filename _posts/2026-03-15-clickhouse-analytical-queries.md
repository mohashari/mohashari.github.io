---
layout: post
title: "ClickHouse for Backend Engineers: Analytical Queries at Billions of Rows Per Second"
date: 2026-03-15 07:00:00 +0700
tags: [clickhouse, olap, databases, performance, analytics]
description: "Design schemas, optimize queries, and operate ClickHouse to power real-time analytics workloads at massive scale."
---

Your PostgreSQL dashboard query takes 4.2 seconds to return when the table has 500 million rows. You've added indexes, partitioned the table, thrown read replicas at it, and still your product manager is complaining that the analytics page is "too slow to be useful." This is not a PostgreSQL problem — it's a workload mismatch. Transactional databases are built for point lookups and short writes; analytical queries scanning billions of rows, grouping by dozens of dimensions, and computing aggregates across months of data are a fundamentally different beast. ClickHouse is built for exactly this: columnar storage, vectorized query execution, and aggressive compression that makes scanning 10 billion rows in under a second a realistic production target, not a benchmark trick.

## How ClickHouse Stores Data Differently

ClickHouse stores data column by column rather than row by row. When you run `SELECT count(*) WHERE status = 'error'`, a row-oriented database reads every column of every row. ClickHouse reads only the `status` column — often 10–50x less I/O. Combined with LZ4 or ZSTD compression on each column (which compresses dramatically better when values are the same type), a 1TB PostgreSQL table might shrink to 80GB in ClickHouse.

The primary engine you'll use is `MergeTree` and its variants. Here's a realistic schema for an event tracking system:

<script src="https://gist.github.com/mohashari/4820b578906e011877bcf3facd6a3820.js?file=snippet.sql"></script>

`LowCardinality(String)` is a dictionary-encoded type that cuts storage and speeds up GROUP BY significantly for columns with fewer than ~10,000 distinct values. `ORDER BY` defines the primary key — ClickHouse uses a sparse index over this, storing one index entry per 8,192 rows (one "granule"). Queries that filter on leading columns of the ORDER BY skip granules entirely, turning a full scan into a targeted read.

## Materialized Views for Pre-Aggregation

ClickHouse's killer feature for real-time analytics is the materialized view with a `SummingMergeTree` or `AggregatingMergeTree` target. Instead of scanning raw events at query time, you pre-aggregate on write.

<script src="https://gist.github.com/mohashari/4820b578906e011877bcf3facd6a3820.js?file=snippet-2.sql"></script>

With this in place, your hourly rollup query reads from a table with orders of magnitude fewer rows. The `uniqState` / `uniqMerge` pattern uses HyperLogLog under the hood — approximate cardinality at a fraction of the memory cost of exact `COUNT(DISTINCT ...)`.

<script src="https://gist.github.com/mohashari/4820b578906e011877bcf3facd6a3820.js?file=snippet-3.sql"></script>

## Inserting Data from Go

ClickHouse performs best with large batches. The native protocol is significantly faster than HTTP for writes. Use the official `clickhouse-go` v2 driver:

<script src="https://gist.github.com/mohashari/4820b578906e011877bcf3facd6a3820.js?file=snippet-4.go"></script>

Target batch sizes of 10,000–100,000 rows per insert. Each tiny insert creates a new data part on disk that must be merged — too many small inserts trigger the "too many parts" error and degrade performance significantly.

## Partitioning and TTL

Data lifecycle management is a first-class feature. You can automatically expire old data and move cold partitions to cheaper storage:

<script src="https://gist.github.com/mohashari/4820b578906e011877bcf3facd6a3820.js?file=snippet-5.sql"></script>

The storage policy backing this references disk volumes defined in the server config:

<script src="https://gist.github.com/mohashari/4820b578906e011877bcf3facd6a3820.js?file=snippet-6.xml"></script>

This moves parts older than 30 days to S3 automatically — you pay NVMe prices only for hot data.

## Diagnosing Slow Queries

When a query underperforms, reach for `EXPLAIN` and the system tables:

<script src="https://gist.github.com/mohashari/4820b578906e011877bcf3facd6a3820.js?file=snippet-7.sql"></script>

If `EXPLAIN` shows a large number of granules being read despite a predicate on your ORDER BY column, you likely have a cardinality problem — the leading column (`tenant_id`) has too many distinct values relative to your granule size, or your query is filtering on a non-leading column. Projections (essentially embedded materialized views with a different sort order) can solve this without duplicating tables.

## Running ClickHouse in Docker for Development

Getting a local instance up is straightforward:

<script src="https://gist.github.com/mohashari/4820b578906e011877bcf3facd6a3820.js?file=snippet-8.dockerfile"></script>

<script src="https://gist.github.com/mohashari/4820b578906e011877bcf3facd6a3820.js?file=snippet-9.sh"></script>

The `nofile` ulimit is not optional — ClickHouse opens many file descriptors during merges and without it you'll see cryptic errors under load.

ClickHouse is not a drop-in replacement for your OLTP database, and it shouldn't be. The right architecture uses Postgres (or MySQL) for transactional writes, streams those events to ClickHouse via Kafka or direct inserts, and routes all analytical reads to ClickHouse. Once you internalize the MergeTree mental model — choose your ORDER BY for your most common filter, pre-aggregate with materialized views, batch your inserts, and let TTL manage retention — you'll find that queries that were previously impossible to run interactively become millisecond responses. The engineering effort to adopt ClickHouse is real, but so is the payoff: dashboards your users actually trust because the numbers update in real time and the page loads instantly.