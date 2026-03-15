---
layout: post
title: "Change Data Capture with Debezium: Real-Time Data Pipelines from Your Database"
date: 2026-04-13 07:00:00 +0700
tags: [cdc, debezium, kafka, postgresql, data-pipelines]
description: "Stream database changes to downstream systems in real time using Debezium and Kafka Connect for event-driven data synchronization."
---

Every time a row changes in your database, something downstream probably needs to know about it. Maybe it's a search index that needs reindexing, a cache that needs invalidation, an analytics warehouse that needs the update, or a microservice that reacts to customer state changes. The naive solution — polling on a schedule or firing events from application code — is brittle. Polling adds latency and load. Application-level events get lost during crashes, missed in batch imports, or simply forgotten when a new engineer writes a migration script directly against the database. Change Data Capture (CDC) solves this at the infrastructure level: instead of asking your app to emit events, you read the database's own write-ahead log and turn every committed change into a stream of facts. Debezium, built on top of Kafka Connect, is the production-grade open-source engine that makes this practical.

## How Debezium Works

Debezium operates as a Kafka Connect source connector. It connects to your database using the same replication protocol that replicas use — for PostgreSQL that means the logical replication API, for MySQL it's binlog streaming, for MongoDB it's the oplog. Because it reads the WAL directly, it captures every INSERT, UPDATE, and DELETE with zero application-code changes. Each event is published as a structured JSON or Avro message to a Kafka topic named after the originating table. Downstream consumers treat these messages like a reliable changelog: they can replay from the beginning, catch up after downtime, or join multiple streams to reconstruct state.

## Setting Up PostgreSQL for Logical Replication

Before Debezium can read your WAL, PostgreSQL needs to be configured for logical replication. This requires setting the replication level and creating a replication slot.

<script src="https://gist.github.com/mohashari/8236b974027778312eef34e72b8f1415.js?file=snippet.sql"></script>

A restart is required after changing `wal_level`. The replication slot itself is created automatically by Debezium on first connect — you do not need to create it manually.

## Running Debezium with Docker Compose

For local development, the fastest path is Docker Compose. This spins up Zookeeper, Kafka, Kafka Connect with the Debezium plugin, and PostgreSQL as a single stack.

<script src="https://gist.github.com/mohashari/8236b974027778312eef34e72b8f1415.js?file=snippet-2.yaml"></script>

## Registering the PostgreSQL Connector

Once the stack is running, you register a connector by POSTing a JSON configuration to the Kafka Connect REST API. This tells Debezium which database to watch and which tables to capture.

<script src="https://gist.github.com/mohashari/8236b974027778312eef34e72b8f1415.js?file=snippet-3.sh"></script>

The `ExtractNewRecordState` transform is worth noting: by default Debezium wraps each event in an envelope containing both the `before` and `after` state. The unwrap transform flattens it to just the `after` state, which is simpler for most consumers. For DELETE events it emits a tombstone with a null value.

## Understanding the Event Structure

A raw Debezium event for an UPDATE on an `orders` table looks like this before the unwrap transform. Understanding the envelope is important when you need the `before` state for auditing or delta computation.

<script src="https://gist.github.com/mohashari/8236b974027778312eef34e72b8f1415.js?file=snippet-4.json"></script>

The `op` field is `c` for create, `u` for update, `d` for delete, and `r` for a snapshot read (the initial full-table scan Debezium performs on first connect). The `source.lsn` is the WAL position, which you can use to verify exactly-once processing or resume from a known offset.

## Consuming Events in Go

A Go consumer uses the `confluent-kafka-go` library to read from the Debezium topic and route events to downstream systems — here, an Elasticsearch indexer and a Redis cache invalidator.

<script src="https://gist.github.com/mohashari/8236b974027778312eef34e72b8f1415.js?file=snippet-5.go"></script>

## Monitoring Connector Lag

Connector health is operationally critical — a stalled connector means your WAL is accumulating unconsumed data, which can eventually fill your disk. You should monitor replication slot lag in PostgreSQL alongside Kafka consumer group lag.

<script src="https://gist.github.com/mohashari/8236b974027778312eef34e72b8f1415.js?file=snippet-6.sql"></script>

If `confirmed_flush_lsn` stops advancing while your application is writing, the connector has stalled. Pair this query with an alert: if lag exceeds a threshold (say, 500 MB), page on-call before PostgreSQL disk fills.

## Handling Schema Evolution

One of the more subtle operational challenges with Debezium is schema changes. If you add a nullable column to `orders`, Debezium handles it gracefully. But dropping a column or changing a type will break consumers that haven't been updated. The recommended pattern is to run Debezium with Confluent Schema Registry using Avro serialization — every schema version is registered and consumers can fetch the exact schema that was in effect when an event was written.

<script src="https://gist.github.com/mohashari/8236b974027778312eef34e72b8f1415.js?file=snippet-7.sh"></script>

CDC with Debezium is one of the most reliable ways to build event-driven data pipelines because it treats the database's own durability guarantees — not your application code — as the source of truth. The WAL is already there, already consistent, already ordered. Debezium just makes it readable. The practical takeaway: if you have more than two downstream systems that need to react to database changes, the operational cost of running Debezium is almost always lower than the cumulative cost of maintaining bespoke event emission across your application layer. Start with a single table, validate the event structure against your consumers, then expand. The replication slot and topic model scales cleanly, and the Kafka consumer group mechanism gives you independent lag tracking per downstream system at no extra cost.