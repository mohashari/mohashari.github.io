---
layout: post
title: "Custom MySQL Binlog Parsers: Building a Real-Time Outbox Pattern Processor in Go"
date: 2026-07-04 08:00:00 +0700
tags: [golang, mysql, system-design, microservices, cdc]
description: "Ditch database polling. Build a high-throughput, low-latency transactional outbox processor in Go by parsing MySQL binlogs directly using GTID checkpoints."
image: "https://picsum.photos/seed/4051/1080/720"
thumbnail: "https://picsum.photos/seed/4051/400/300"
---

At a scale of 10,000 writes per second, the traditional transactional outbox pattern implemented via database polling is a silent killer. Running a continuous query like `SELECT * FROM outbox WHERE status = 'PENDING' LIMIT 1000 FOR UPDATE` inflicts severe write amplification, causes catastrophic index fragmentation on UUID/primary keys, and keeps database CPU utilization pegged at 90% due to lock contention. Worse, it introduces a business-impacting latency floor of 100ms to 500ms. If you attempt to solve this by polling faster, you trigger lock escalation and deadlocks. If you poll slower, your upstream event brokers sit starved. To achieve sub-5ms event propagation without placing any query load on your primary MySQL instance, you must shift from polling to push-based Change Data Capture (CDC) by tailing and parsing the MySQL binary log (binlog) directly in Go.

## The Architectural Shift: Pull vs. Push CDC

Traditional outbox processors pull data from the database. A separate worker process queries the outbox table, processes the rows, publishes them to an event broker like Apache Kafka or RabbitMQ, and then deletes or marks the rows as processed. While conceptually simple, this introduces a tight dependency between event dispatch latency and database query throughput.

By leveraging MySQL's replication protocol, we can treat our Go processor as a replica node. Instead of querying tables, the processor sits in a lightweight socket loop, receiving binary log events streamed directly from MySQL's memory space. 

This approach yields immediate architectural advantages:
- **Zero Query Overhead:** The primary database performs no `SELECT`, `UPDATE`, or `DELETE` queries to manage the outbox lifecycle.
- **Write-Only Outbox:** The outbox table becomes a write-only sink. The application inserts outbox records within its business transactions, and a background database utility (like `pt-archiver` or a simple partition drop) prunes them asynchronously.
- **Sub-Millisecond Latency:** Events are pushed to the Go processor the millisecond the database transaction commits, reducing end-to-end event propagation latency from over 150ms to less than 3ms.

## Designing the Outbox Schema for CDC

To parse events cleanly, the outbox table must be designed to avoid schema ambiguity and provide enough metadata for downstream consumers. We avoid using columns that change values during processing (like `status` fields) because updating them generates additional binlog events, creating unnecessary duplicate processing cycles.

Here is a production-grade SQL schema for a write-only transactional outbox table:

<script src="https://gist.github.com/mohashari/8bea6df7f987881847765a1a3f8b73b8.js?file=snippet-1.sql"></script>

By ensuring that the application only performs `INSERT` operations on this table, we ensure that the binlog stream will only contain `WRITE_ROWS_EVENT` payloads for this schema, completely bypassing the need to parse `UPDATE` or `DELETE` events.

## Bootstrapping the Go Binlog Subscriber

To interact with MySQL's replication stream, we will use the `github.com/go-mysql-org/go-mysql` library. It implements the MySQL client-server protocol and handles binlog replication streams, including GTID (Global Transaction Identifier) state management.

First, we set up our subscriber using the `canal` package, which abstracts the low-level connection management and maintains an up-to-date in-memory representation of table schemas.

<script src="https://gist.github.com/mohashari/8bea6df7f987881847765a1a3f8b73b8.js?file=snippet-2.go"></script>

## Parsing Row Events and Reconstructing payloads

MySQL row-based replication (`binlog_format=ROW`) writes changes as raw binary tuples rather than SQL strings. A `WRITE_ROWS_EVENT` contains an array of rows, where each row is represented as an array of interface values indexed by their column position in the database schema.

Because column positions can change during migrations (e.g., if a column is added or dropped), we must dynamically map column names to their respective indexes using the schema metadata cached by our subscriber.

<script src="https://gist.github.com/mohashari/8bea6df7f987881847765a1a3f8b73b8.js?file=snippet-3.go"></script>

## High-Throughput Dispatch: Partition-Aware Worker Pools

Parsing events synchronously in a single thread creates a throughput bottleneck. However, throwing unstructured go-routines at event dispatching is dangerous; if events for the same `aggregate_id` are processed concurrently, network jitter can cause out-of-order delivery.

To achieve throughput scales of 50,000+ events per second without violating ordering guarantees, we build a partition-aware worker pool. Events are routed to specific workers by hashing the `aggregate_id`. This ensures that all events for a specific domain entity are processed in strict chronological order by the same thread.

<script src="https://gist.github.com/mohashari/8bea6df7f987881847765a1a3f8b73b8.js?file=snippet-4.go"></script>

## Fault Tolerance: GTID Tracking and Position Resumption

A production pipeline must handle crashes, database restarts, and network partitions. To prevent event loss, our processor must keep track of its read position and resume cleanly.

While you can track positions using file names and offsets (e.g., `mysql-bin.000142:15632`), this mechanism fails during a database failover. If the active database primary changes, file names and offsets are not preserved across replica nodes.

We solve this using Global Transaction Identifiers (GTIDs). A GTID represents a unique transaction identifier across the entire replication topology. By checkpointing the GTID set to a persistent, atomic key-value store (like Redis or Consul) whenever we process events, we can resume processing seamlessly after any restart or failover.

<script src="https://gist.github.com/mohashari/8bea6df7f987881847765a1a3f8b73b8.js?file=snippet-5.go"></script>

## Production Failure Modes & Mitigation

Running a custom binlog parser in production requires planning for specific operational failure modes that do not occur in traditional application layers.

### 1. Schema Migrations (DDL Changes)
If an engineer runs `ALTER TABLE outbox_events ADD COLUMN payload_hash VARCHAR(64) AFTER payload;`, the physical structure of the row representation inside the binlog changes. 

If the Go parser does not adapt, one of two things will occur:
1. The parser attempts to read the index structure of the old schema, resulting in out-of-bounds errors or type coercion panic crashes.
2. The columns shift, silently mapping data to incorrect variables.

To prevent this, the parser's cached metadata schema must be updated. `go-mysql`'s `canal` handles schema fetching dynamically. It intercepts DDL statements in the binary log stream and invalidates its internal schema cache for the affected tables. However, you must handle the scenario where column additions change your custom parsing mappings. 

Here is how you handle unexpected column configurations safely:

<script src="https://gist.github.com/mohashari/8bea6df7f987881847765a1a3f8b73b8.js?file=snippet-6.go"></script>

### 2. Binlog Retention & Purging
MySQL purges binary logs based on your configured `binlog_expire_logs_seconds` (commonly set to 24 or 48 hours). If your Go processor crashes or experiences network isolation for longer than the expiry period, the stored GTID checkpoint will reference transactions that no longer exist in MySQL's binlog files.

When the processor restarts and requests replication to start from that expired GTID, MySQL will return a fatal error:

`Fatal error: The replication receiver thread cannot start because the master has purged binary logs...`

**Mitigation:** 
Implement alerting on the age of the last processed transaction timestamp (`created_at` latency vs local clock). If replication fails due to purged logs, you must fall back to a recovery mechanism:
1. Temporarily spin up a polling-based recovery script to drain the current table state.
2. Advance the GTID position manually using the replication master's current GTID range (`SELECT @@gtid_purged`), and accept that missing events must be recovered from application audit logs.

### 3. Idle Connection Drop & Heartbeats
If your application experiences low write activity (for example, in a staging environment or during off-peak hours), the replication connection will not receive any events. If the connection remains idle longer than the configured MySQL read timeout parameters (`net_read_timeout` or `interactive_timeout`), the database engine will terminate the TCP connection.

**Mitigation:**
Configure binary log heartbeats. The Go client can request the master to send periodic heartbeat packets to keep the connection alive:

<script src="https://gist.github.com/mohashari/8bea6df7f987881847765a1a3f8b73b8.js?file=snippet-7.go"></script>

## Conclusion

Tailing binlogs in Go shifts the scale limits of the Transactional Outbox pattern. By migrating from polling to event-driven streaming, we reduce database lock times, lower propagation latency, and isolate our read processes from application databases. 

When deploying this architecture, ensure you have:
1. Set database logs to `binlog_format=ROW` and `binlog_row_image=FULL`.
2. Verified that GTIDs are globally enabled on your database cluster.
3. Written automated integration tests that perform DDL alterations to verify your parser handles schema shifts gracefully.