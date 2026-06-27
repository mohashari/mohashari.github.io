---
layout: post
title: "PostgreSQL WAL Internals: Structuring Custom Parse Parsers for CDC Replication"
date: 2026-06-27 08:00:00 +0700
tags: [postgresql, go, cdc, database-internals, replication]
description: "An in-depth engineering guide to building custom logical replication parsers in Go, addressing TOAST columns, schema drift, and LSN stream durability."
image: "https://picsum.photos/seed/3401/1080/720"
thumbnail: "https://picsum.photos/seed/3401/400/300"
---
When scaling production databases past 50,000 write operations per second, generic Change Data Capture (CDC) tools like Debezium or AWS Database Migration Service (DMS) frequently become operational bottlenecks. Under heavy write pressure, the JVM memory overhead, garbage collection pauses, and multi-layered serialization pipelines of standard CDC frameworks can introduce replication lag spikes exceeding 120 seconds. This lag degrades downstream systems like search indexers, fraud detection engines, and caching layers that rely on real-time database state. By bypassing heavy middleware and writing a custom logical replication receiver in Go that directly consumes and parses the low-level `pgoutput` wire protocol, you can process high-throughput event streams with sub-15ms end-to-end latency while maintaining a memory footprint under 80MB.

![PostgreSQL WAL Internals: Structuring Custom Parse Parsers for CDC Replication Diagram](/images/diagrams/postgresql-wal-internals-custom-parse-parsers-cdc-replication.svg)

## The Anatomy of pgoutput Logical Replication Messages

PostgreSQL logical replication operates on top of the physical Write-Ahead Log (WAL). A logical decoding plugin (such as the default `pgoutput` introduced in PostgreSQL 10) decodes physical WAL segments—typically 16MB ring files stored under `pg_wal`—into a streaming logical protocol. When a transaction commits, the logical replication slot streams these events to the connected consumer using PostgreSQL's Frontend/Backend Protocol V3.0 wrapped inside `CopyData` packets.

To parse this wire protocol, your consumer must implement a state machine that processes raw byte buffers. Every transaction boundary is marked by a `Begin` ('B') message and closed by a `Commit` ('C') message. Between these boundaries lie `Relation` ('R') metadata messages and data operations: `Insert` ('I'), `Update` ('U'), and `Delete` ('D').

When a data modification occurs, the stream does not send column names or schema details inline. Instead, it sends a transient 4-byte `Relation ID`. The client must associate this ID with the schema definitions sent in preceding `Relation` messages. The relation message details the namespace (schema name), relation name (table name), replica identity flag, and an array of column definitions including their PostgreSQL Data Type Object Identifiers (OIDs).

The following snippet demonstrates a production-grade parser for the `'R'` (Relation) message structure, handling null-terminated strings and variable-length field arrays.

<script src="https://gist.github.com/mohashari/67426401557e17212f1988b9a0c2ba2a.js?file=snippet-1.go"></script>

## Handling the TOAST: Reconstructing Partial Tuples

The most common point of failure for custom CDC implementations is the handling of PostgreSQL's TOAST (The Oversized-Attribute Storage Technique) mechanism. 

Under the hood, PostgreSQL splits tables into 8KB disk blocks. If a variable-length column (such as `text`, `varchar`, or `jsonb`) exceeds the `TOAST_TUPLE_THRESHOLD` (normally 2KB), the storage engine automatically compresses the value and writes it to an auxiliary table named `pg_toast.pg_toast_<oid>`. This process is completely transparent to SQL queries, but it poses a major challenge for logical replication. 

When an `UPDATE` event occurs, columns stored in TOAST that are *not* modified are omitted from the WAL. In the `pgoutput` wire stream, these are marked with a byte indicator `'u'` (Unchanged TOAST). If your parser receives `'u'` and blindly writes it downstream, it will overwrite the existing value in your search index or key-value store with null or placeholder junk.

```
Logical stream representation of values:
'n' -> Null value
'u' -> Unchanged TOAST value (data is NOT sent over the wire)
't' -> Text-formatted value (followed by 4-byte length + payload)
```

To parse data tuples safely, you must intercept `'u'` flags and decide how to resolve them:

1. **REPLICA IDENTITY FULL**: Setting `ALTER TABLE <name> REPLICA IDENTITY FULL` forces PostgreSQL to write the full contents of all columns to the WAL on updates. While this resolves the `'u'` problem by sending the old value in a separate block, it introduces a severe production hazard: database write amplification. Every single update write-locks more pages and writes massive overhead to the WAL, choking your disk I/O.
2. **State Merging in the Parser**: Configure the CDC client to cache historical records, or perform a point-in-time read back to the database for the missing column when `'u'` is detected. A fallback database query introduces an extra network roundtrip (~1-3ms) but avoids the write amplification of replica identity full.

Here is the parser logic that safely identifies and extracts unchanged TOAST columns.

<script src="https://gist.github.com/mohashari/67426401557e17212f1988b9a0c2ba2a.js?file=snippet-2.go"></script>

## Schema Cache Invalidation under Concurrent DDL

In a production environment, database schema changes (e.g., `ALTER TABLE ... ADD COLUMN`) happen online. PostgreSQL handles this in the logical stream by sending a new `'R'` message with the updated schema right before streaming any data changes containing the new columns.

This means your parsing engine must maintain a dynamically updated, thread-safe cache mapping `RelationID` to `RelationMessage`. If your parsing engine processes events concurrently, a classic race condition emerges: if a worker thread parses an `'I'` (Insert) message using an outdated schema cache entry, it will experience type mismatch errors or index out-of-bounds panics because the raw byte slice contains more values than columns registered in the cached relation.

To prevent this, you must structure the relation cache to handle concurrent reads and atomic invalidation. 

<script src="https://gist.github.com/mohashari/67426401557e17212f1988b9a0c2ba2a.js?file=snippet-3.go"></script>

When building a high-throughput pipeline, rather than wrapping all message decoding inside a global lock, decouple the stream receiver from the worker threads. A single event-loop thread should read from the PostgreSQL connection. If the event-loop thread encounters an `'R'` message, it updates the `SchemaCache` synchronously. Since replication records within a physical WAL are strictly ordered, processing the schema update in the main receiver thread before forwarding subsequent data records to worker pools guarantees schema alignment.

## Writing a Resilient WAL Consumer in Go

To establish the replication link, the client issues a `START_REPLICATION` command over a replication-enabled PostgreSQL connection. Below, we implement the consumer client using the pgx driver framework, focusing on the event loop and keepalive management.

A critical failure mode of logical replication slots is client-side backpressure. If the consumer blocks on writing events downstream (e.g., waiting for Kafka metadata brokers to respond), it stops checking the network socket. This halts keepalive acknowledgements back to the PostgreSQL database. If no keepalive is received within the `wal_sender_timeout` limit (typically 60 seconds), the PostgreSQL server terminates the connection, drops the stream, and logs a `terminating connection due to replication timeout` error.

To avoid this, keepalive responses must be processed asynchronously or handled with strict deadline guarantees.

<script src="https://gist.github.com/mohashari/67426401557e17212f1988b9a0c2ba2a.js?file=snippet-4.go"></script>

The data payload wrapped inside the `CopyData` message must be inspected. The first byte tells us whether it is a physical write payload (`'w'`) or a server-initiated keepalive packet (`'k'`). 

<script src="https://gist.github.com/mohashari/67426401557e17212f1988b9a0c2ba2a.js?file=snippet-5.go"></script>

## Monitoring and Guarding Against Database Disk Exhaustion

Replication slots provide a guarantee: PostgreSQL will not discard WAL segments until they have been acknowledged as flushed by the replication client. This prevents data loss during network partitions or downstream outages.

However, this guarantee creates a major disk vulnerability. If your custom Go consumer crashes and fails to reconnect, or if it stops acknowledging the flushed LSN due to an unhandled internal loop error, the PostgreSQL primary server will accumulate physical WAL files indefinitely in its `/pg_wal` directory. On a busy write database generating 20GB of WAL files per hour, this can fill up the disk mount in a matter of hours, causing PostgreSQL to panic and crash hard.

To prevent disk crash outages:

1. **Replication Slot Limits**: Set `max_slot_wal_keep_size` in your `postgresql.conf` (e.g., to `64GB`). If the replication slot falls behind by more than this threshold, PostgreSQL automatically invalidates the slot and cleans up old WAL files, prioritizing database availability over CDC consumer delivery.
2. **Replication Lag Monitoring**: Query database health regularly to identify lagging slots. The replication lag can be computed in bytes by subtracting the confirmed LSN from the database engine's current system WAL LSN.

The SQL statement below should be configured as a Prometheus gauge metric inside your alerting systems to trigger on replication lag exceeding 10GB.

```sql
-- snippet-6
SELECT
    slot_name,
    active,
    active_pid,
    wal_status,
    pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), confirmed_flush_lsn)) AS replication_lag_bytes,
    pg_wal_lsn_diff(pg_current_wal_lsn(), confirmed_flush_lsn) AS replication_lag_bytes_raw,
    pg_size_pretty(safe_wal_size) AS safe_wal_limit
FROM pg_replication_slots;
```

When building a custom CDC parser, you transition from high-level application code directly into low-level systems engineering. By managing the network socket buffers, parsing byte payloads, and designing thread-safe schema registries, your CDC client can scale to meet the throughput requirements of high-demand production platforms, safely bypassing the architectural bloat of standard database connectors.