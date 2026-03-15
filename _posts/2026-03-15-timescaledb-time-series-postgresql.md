---
layout: post
title: "TimescaleDB: Time-Series Data at Scale on Top of PostgreSQL"
date: 2026-03-15 07:00:00 +0700
tags: [timescaledb, postgresql, time-series, databases, performance]
description: "Store, query, and compress time-series data efficiently using TimescaleDB hypertables, continuous aggregates, and retention policies."
---

Every backend engineer eventually hits the wall with time-series data. You start with a simple `events` table in PostgreSQL, add a timestamp index, and everything feels fine — until you're ingesting millions of rows per day from IoT sensors, application metrics, or financial ticks. Queries that once took milliseconds now crawl. Your storage costs balloon. `DELETE` statements for old data lock the table. You consider migrating to InfluxDB or Prometheus, but then you lose the relational model, JOINs, and the rich PostgreSQL ecosystem you rely on. TimescaleDB solves this without making you abandon PostgreSQL. It's a PostgreSQL extension that adds time-series superpowers — automatic partitioning, columnar compression, continuous aggregates, and data retention — while keeping everything fully SQL-compatible.

## Installing TimescaleDB

The fastest path to a working setup is the official Docker image, which ships with TimescaleDB pre-installed. For production, the TimescaleDB APT/YUM packages extend your existing PostgreSQL installation.

<script src="https://gist.github.com/mohashari/7dd08199fd2ae8be01a0f912709085f3.js?file=snippet.dockerfile"></script>

After the container starts, enable the extension in your database. This is a one-time step per database.

<script src="https://gist.github.com/mohashari/7dd08199fd2ae8be01a0f912709085f3.js?file=snippet-2.sql"></script>

## Creating a Hypertable

A hypertable looks exactly like a regular PostgreSQL table but is automatically partitioned into chunks by time under the hood. TimescaleDB chooses the chunk interval based on your data volume, but you can override it. The `time` column becomes the partition key.

<script src="https://gist.github.com/mohashari/7dd08199fd2ae8be01a0f912709085f3.js?file=snippet-3.sql"></script>

The `create_hypertable` call converts the existing table into a hypertable. Each day of data becomes an independent chunk — a regular PostgreSQL table — which means `DROP` on old chunks is instant and lock-free, unlike `DELETE` on a monolithic table.

## Ingesting Data from Go

The wire protocol is pure PostgreSQL, so your existing `pgx` or `database/sql` code works without modification. For high-throughput ingestion, batch inserts with `COPY` protocol are the right tool. Here's a production pattern using `pgx/v5`:

<script src="https://gist.github.com/mohashari/7dd08199fd2ae8be01a0f912709085f3.js?file=snippet-4.go"></script>

`CopyFrom` uses PostgreSQL's binary `COPY` protocol, which bypasses row-by-row parsing overhead and can sustain hundreds of thousands of rows per second on modest hardware. Batch sizes between 1,000 and 10,000 rows strike the best balance between latency and throughput.

## Querying with Time Bucketing

TimescaleDB's `time_bucket` function is the workhorse of time-series analytics. It's conceptually similar to `DATE_TRUNC` but supports arbitrary intervals and integrates with query planning optimizations that standard PostgreSQL aggregation cannot apply.

<script src="https://gist.github.com/mohashari/7dd08199fd2ae8be01a0f912709085f3.js?file=snippet-5.sql"></script>

TimescaleDB uses chunk exclusion to skip entire daily chunks outside the `WHERE` range. On a table with years of data, a 7-day query touches only 7 chunks rather than scanning the full table — the query plan reflects this with a `Custom Scan (ChunkAppend)` node.

## Continuous Aggregates

Recomputing hourly aggregates on every dashboard request wastes CPU proportional to your raw data volume. Continuous aggregates materialize the results and refresh incrementally, touching only newly arrived data since the last refresh.

<script src="https://gist.github.com/mohashari/7dd08199fd2ae8be01a0f912709085f3.js?file=snippet-6.sql"></script>

The `start_offset` and `end_offset` define a window of uncertainty — late-arriving data within the last 3 hours can still update the aggregate, while data older than 3 hours is considered stable and won't trigger a full recompute.

## Columnar Compression

TimescaleDB's native compression converts row-oriented chunks into columnar format. For time-series workloads with many numeric columns, compression ratios of 10x to 20x are common. Compression also speeds up analytical queries because less data moves from disk to memory.

<script src="https://gist.github.com/mohashari/7dd08199fd2ae8be01a0f912709085f3.js?file=snippet-7.sql"></script>

The `compress_segmentby` column groups related rows together before compression. Choosing a high-cardinality column like `sensor_id` keeps each segment small enough to remain useful for point queries while maximizing compression within a segment.

## Data Retention Policies

Old time-series data is often worthless after a retention window. Rather than scheduling cron jobs that `DELETE` rows and fragment the table, use TimescaleDB's drop-chunk policy. Dropping a chunk is an instantaneous metadata operation equivalent to `DROP TABLE` on the underlying partition.

<script src="https://gist.github.com/mohashari/7dd08199fd2ae8be01a0f912709085f3.js?file=snippet-8.sql"></script>

This two-tier strategy is the standard pattern: retain raw data for short-term debugging and anomaly investigation, then rely on pre-aggregated views for long-term trend analysis. Raw storage stays bounded while historical insight is preserved indefinitely.

TimescaleDB earns its place in the backend toolkit by threading the needle between operational simplicity and analytical power. You keep the full PostgreSQL feature set — transactions, foreign keys, JOINs with relational tables, `pg_dump`, your existing ORM — while gaining automatic partitioning, sub-second compressed analytics, and maintenance policies that run without operator intervention. The migration path for an existing PostgreSQL time-series table is a single `create_hypertable` call. Start there, add a compression policy once chunks start accumulating, layer in a continuous aggregate when dashboard queries feel slow, and add a retention policy when storage costs become visible. Each feature is independently adoptable, which means you can tune the system incrementally as your data volume grows rather than committing to a full rewrite upfront.