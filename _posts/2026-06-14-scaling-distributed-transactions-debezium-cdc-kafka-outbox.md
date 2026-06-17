---
layout: post
title: "Scaling Distributed Transactions with Debezium CDC and Kafka Outbox"
date: 2026-06-14 08:00:00 +0700
tags: [microservices, system-design, cdc, kafka, go]
description: "Solve the dual-write problem and scale distributed transactions to 20k+ TPS with Debezium CDC, Kafka Outbox, and idempotent Go consumers."
image: "https://picsum.photos/seed/6158/1080/720"
thumbnail: "https://picsum.photos/seed/6158/400/300"
---

In modern high-throughput microservices architectures, updating a relational database and publishing corresponding events to Apache Kafka is a fundamental necessity. Yet, performing this dual-write operations naively introduces a critical distributed systems trap: there is no shared transaction coordinator, and a failure on either side leads to silent data corruption, orphaned records, or missed event notifications. When handling over 20,000 transactions per second (TPS), attempting to solve this with heavy, blocking two-phase commits (2PC) destroys throughput and introduces catastrophic single-point-of-failures. Scaling distributed transactions without compromising database sanity or broker reliability requires decoupling the process entirely: using the Transactional Outbox pattern backed by low-latency Change Data Capture (CDC) via Debezium to bridge the gap asynchronously and with zero lock contention.

![Scaling Distributed Transactions with Debezium CDC and Kafka Outbox Diagram](/images/diagrams/scaling-distributed-transactions-debezium-cdc-kafka-outbox.svg)

## The High-Throughput Dual-Write Crisis

At the core of the microservices operational model is the event-driven reaction. When a user checks out an order, the `Order Service` writes to its relational database (PostgreSQL) and sends an `OrderPlaced` event to Kafka. Naively, developers solve this with sequential HTTP or TCP network calls: first writing to PostgreSQL, and if that succeeds, publishing the message to Kafka. 

Under real production workloads—when traffic spikes to tens of thousands of requests per second—this structure breaks down in two distinct, catastrophic failure modes:

1. **Database Writes Succeed, Message Publication Fails:**
   The database commits the order record. Right before the application publishes the message to Kafka, the message broker undergoes a partition rebalance, suffers a network blip, or the service pod runs out of memory and crashes. The event is lost forever. The database state remains updated, but downstream microservices (like payment, shipping, or inventory) are never notified. Data inconsistency must now be resolved via manual support intervention or complex reconciliation scripts.
   
2. **Message Publication Succeeds, Database Commit Fails:**
   To avoid missing events, some developers try writing to Kafka first, followed by the database write. However, if the database write fails due to a constraint violation, database deadlock, or connection pool exhaustion, the message has already been published. Downstream services will begin processing an event for an order that technically does not exist in the primary system of record.

### The Myth of Two-Phase Commit (2PC) and XA Transactions

Historically, two-phase commit protocols (2PC) and XA transactions were used to ensure atomic updates across heterogeneous resources. 2PC coordinates the database and the message broker under a single global transaction manager. However, 2PC is a blocking protocol that requires all participants to lock resources and await consensus. 

In a cloud-native environment, 2PC is a performance killer:
* **Latency Inflation:** Locking resources across network boundaries increases transaction holding times from milliseconds to seconds.
* **Reduced Availability:** If the transaction coordinator crashes during the prepare phase, resources remain locked indefinitely, triggering cascading failures.
* **Lack of Support:** Modern, high-performance distributed streaming systems like Apache Kafka do not natively support XA or 2PC from database transaction managers.

To scale distributed transactions to 20k+ TPS, we must embrace *eventual consistency*. Instead of attempting to write to two systems in a single blocking action, we must write to one reliable system of record first, and asynchronously replicate that state change to the second system.

## The Transactional Outbox Pattern to the Rescue

The Transactional Outbox pattern guarantees that database writes and event publishing occur atomically. Instead of sending messages directly to Kafka, the application writes both the domain entity (e.g., the order) and an event log (the outbox event) to the *same* database using local ACID transactions.

Because both operations occur within the same local transaction block, they are guaranteed to either both succeed or both fail. There is no middle ground.

### Optimizing the Outbox Table Schema

At high write volumes, the outbox table can quickly become a bottleneck due to lock contention and table bloat. We must design the table schema to maximize write throughput and support clean partitioning.

// snippet-1
CREATE TABLE outbox_events (
    id UUID NOT NULL,
    aggregate_type VARCHAR(100) NOT NULL,
    aggregate_id VARCHAR(100) NOT NULL,
    event_type VARCHAR(100) NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
    PRIMARY KEY (id, created_at)
) PARTITION BY RANGE (created_at);

-- Create partition index to support clean lookups and partitioning detachments
CREATE INDEX idx_outbox_events_lookup ON outbox_events(aggregate_type, aggregate_id);

### Atomic Write Implementation in Go

The application logic must wrap both operations within a single database transaction. The following production-grade Go code utilizes a PostgreSQL transaction to insert the order record and write the outbox event payload as a single atomic step.

// snippet-2
package repository

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"time"

	"github.com/google/uuid"
)

type Order struct {
	ID         uuid.UUID
	CustomerID uuid.UUID
	Total      float64
	Status     string
}

type OrderEvent struct {
	OrderID uuid.UUID `json:"order_id"`
	Total   float64   `json:"total"`
	Status  string    `json:"status"`
}

type OrderRepository struct {
	db *sql.DB
}

func (r *OrderRepository) CreateOrder(ctx context.Context, order *Order) error {
	// Begin a local database transaction
	tx, err := r.db.BeginTx(ctx, &sql.TxOptions{Isolation: sql.LevelReadCommitted})
	if err != nil {
		return fmt.Errorf("failed to begin transaction: %w", err)
	}
	defer tx.Rollback() // Automatically rolls back if Commit is not called

	// 1. Insert order domain record
	orderQuery := `
		INSERT INTO orders (id, customer_id, total, status, created_at) 
		VALUES ($1, $2, $3, $4, $5)`
	now := time.Now()
	_, err = tx.ExecContext(ctx, orderQuery, order.ID, order.CustomerID, order.Total, order.Status, now)
	if err != nil {
		return fmt.Errorf("failed to insert order: %w", err)
	}

	// 2. Prepare event payload
	eventPayload := OrderEvent{
		OrderID: order.ID,
		Total:   order.Total,
		Status:  order.Status,
	}
	payloadBytes, err := json.Marshal(eventPayload)
	if err != nil {
		return fmt.Errorf("failed to marshal event payload: %w", err)
	}

	// 3. Insert into the outbox table inside the same transaction
	outboxQuery := `
		INSERT INTO outbox_events (id, aggregate_type, aggregate_id, event_type, payload, created_at)
		VALUES ($1, $2, $3, $4, $5, $6)`
	eventID := uuid.New()
	_, err = tx.ExecContext(ctx, outboxQuery, eventID, "Order", order.ID.String(), "OrderCreated", payloadBytes, now)
	if err != nil {
		return fmt.Errorf("failed to insert outbox event: %w", err)
	}

	// Commit transaction atomically
	if err := tx.Commit(); err != nil {
		return fmt.Errorf("failed to commit transaction: %w", err)
	}
	return nil
}

## Tailing the Log: Why Polling Fails and CDC Wins

Once events are written to the database outbox table, they must be exported to Apache Kafka. The choice of how to read these events is critical for system scaling.

### The Downside of Polling Publishers

A naive solution involves a background worker query polling the outbox table at short intervals:

```sql
SELECT * FROM outbox_events WHERE processed = false ORDER BY created_at LIMIT 100 FOR UPDATE SKIP LOCKED;
```

While this works for small systems, it introduces severe bottlenecks at scale:
* **Table Bloat & Index Degradation:** Constant updates to set a `processed` column generate a massive volume of dead tuples in MVCC engines like PostgreSQL. This triggers intensive autovacuum cycles, causing database CPU spikes and IO bottlenecks.
* **High Query Overhead:** Run at 50ms intervals, this query puts constant read-write pressure on the database, eating connection pool capacity.
* **Latency Slop:** If the polling interval is 200ms, event propagation latency is at least 200ms, which fails real-time delivery SLAs.

### The CDC Approach: Low-Overhead Log Tailing

Change Data Capture (CDC) reads database modifications directly from the transaction log—the Write-Ahead Log (WAL) in PostgreSQL or the Binary Log (binlog) in MySQL.

**Debezium** is an open-source platform designed for this. Acting as a consumer of PostgreSQL logical replication, Debezium captures inserts to the `outbox_events` table asynchronously as they are written to the WAL.

* **No Queries or Reads:** Debezium does not execute `SELECT` statements on the outbox table. It tails the raw log files, completely bypassing SQL parsing and query planning.
* **No Locks:** Since it reads the replication stream, it acquires no locks on the database tables, allowing application write queries to proceed at full speed.
* **Microsecond Latency:** State changes are captured and pushed to Kafka Connect within milliseconds of the local transaction committing.

## Scaling CDC: Tuning Debezium and Kafka for Maximum Throughput

Running Debezium in a high-volume environment requires proper configuration. Below is a production configuration using Strimzi (Kubernetes Operator for Kafka Connect) designed to tail PostgreSQL and utilize the Debezium **Outbox Event Router** Single Message Transform (SMT).

### Debezium Connector Configuration with SMT Event Router

The Outbox Event Router SMT is crucial. It intercepts Debezium's default structured CDC message (which contains complex metadata, before-and-after values, and transaction IDs) and flattens it. It routes the payload directly to a Kafka topic named after the `aggregate_type` column (e.g., `outbox.events.Order`) and uses `aggregate_id` as the message key.

// snippet-3
apiVersion: kafka.strimzi.io/v1beta2
kind: KafkaConnector
metadata:
  name: pg-outbox-connector
  labels:
    strimzi.io/cluster: my-connect-cluster
spec:
  class: io.debezium.connector.postgresql.PostgresConnector
  tasksMax: 1
  config:
    connector.class: io.debezium.connector.postgresql.PostgresConnector
    database.hostname: postgres-primary.db.svc.cluster.local
    database.port: "5432"
    database.user: debezium_cdc_user
    database.password: "${file:/opt/kafka/secrets/db-credentials.properties:password}"
    database.dbname: orders_db
    database.server.name: dbserver1
    plugin.name: pgoutput
    table.include.list: public.outbox_events
    tombstones.on.delete: "false"
    
    # Configure Outbox Single Message Transform (SMT)
    transforms: outbox
    transforms.outbox.type: io.debezium.transforms.outbox.EventRouter
    transforms.outbox.route.by.field: aggregate_type
    transforms.outbox.route.topic.replacement: outbox.events.${routedByValue}
    transforms.outbox.table.field.event.key: aggregate_id
    transforms.outbox.table.field.event.payload: payload
    transforms.outbox.table.field.event.id: id

### Performance Tuning for 20k+ TPS

To scale Debezium throughput, we must adjust internal buffer allocations, connection timeouts, and batch sizes. Tailing PostgreSQL's WAL means handling bursts of writes smoothly.

// snippet-4
# High-performance tuning parameters for Debezium Kafka Connect worker
connector.class: io.debezium.connector.postgresql.PostgresConnector
tasks.max: "1" # Must be 1 to guarantee strict global ordering of WAL parsing

# Buffer and Batching Tuning
max.batch.size: "8192"       # Maximum records in each batch sent to Kafka Connect
max.queue.size: "32768"      # Capacity of the internal memory queue buffer
poll.interval.ms: "10"       # Poll wait limit when buffer is not full

# Kafka Producer Client overrides for reliability and compression
producer.acks: "all"
producer.enable.idempotence: "true"
producer.max.in.flight.requests.per.connection: "5"
producer.compression.type: "zstd"
producer.linger.ms: "20"
producer.batch.size: "131072" # 128KB client-side producer batch size

By setting `tasks.max` to `1`, we preserve the strict physical sequence of PostgreSQL's WAL stream. Attempting to run multiple tasks on a single PostgreSQL connector is not supported by Debezium anyway, as logical replication slots can only be active for a single client connection.

Setting `producer.acks` to `all` and `producer.enable.idempotence` to `true` ensures that once Debezium receives an offset confirmation, the event is guaranteed to be persisted in Kafka's partition replicas without duplication or ordering corruption.

## At-Least-Once Delivery: Designing Bulletproof Idempotent Consumers

While the outbox pattern ensures the database and the broker are in sync, the network remains unreliable. If a network partition prevents Kafka from acknowledging Debezium's producer request before it retries, or if the Kafka Connect worker crashes after writing to Kafka but before saving its source offsets, the same event will be sent to Kafka twice.

Because of this, microservices architectures must operate under **At-Least-Once Delivery** guarantees. To ensure eventual consistency without corrupting downstream state, consumers must be strictly **Idempotent**.

### Transactional Deduplication

The consumer must track which events it has already processed. By maintaining a `processed_events` table inside the consumer's local database schema, we can run the idempotency check and business processing inside a single database transaction.

The following Go consumer uses `segmentio/kafka-go` and PostgreSQL transactional controls to guarantee event deduplication.

// snippet-5
package consumer

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"log"
	"time"

	"github.com/google/uuid"
	"github.com/segmentio/kafka-go"
)

type InventoryConsumer struct {
	reader *kafka.Reader
	db     *sql.DB
}

type OutboxPayload struct {
	OrderID uuid.UUID `json:"order_id"`
	Total   float64   `json:"total"`
	Status  string    `json:"status"`
}

func (c *InventoryConsumer) Start(ctx context.Context) {
	for {
		msg, err := c.reader.FetchMessage(ctx)
		if err != nil {
			if errors.Is(err, context.Canceled) {
				return
			}
			log.Printf("Error fetching message: %v", err)
			continue
		}

		// Retrieve Event ID from Headers (set by Outbox EventRouter SMT)
		var eventID uuid.UUID
		for _, h := range msg.Headers {
			if h.Key == "id" {
				eventID, err = uuid.Parse(string(h.Value))
				break
			}
		}

		if err != nil || eventID == uuid.Nil {
			log.Printf("Missing or invalid event ID: %v", err)
			_ = c.reader.CommitMessages(ctx, msg)
			continue
		}

		// Process event with transactional deduplication
		if err := c.processEventWithDeduplication(ctx, eventID, msg.Value); err != nil {
			log.Printf("Failed to process event %s: %v", eventID, err)
			// Retrying here relies on Kafka's offset management; backoff logic should be added
			continue
		}

		// Commit offsets once processing is guaranteed
		if err := c.reader.CommitMessages(ctx, msg); err != nil {
			log.Printf("Failed to commit offset: %v", err)
		}
	}
}

func (c *InventoryConsumer) processEventWithDeduplication(ctx context.Context, eventID uuid.UUID, payloadBytes []byte) error {
	tx, err := c.db.BeginTx(ctx, &sql.TxOptions{Isolation: sql.LevelReadCommitted})
	if err != nil {
		return err
	}
	defer tx.Rollback()

	// 1. Transactional Deduplication Check
	// If the event ID has already been processed, this unique constraint insert will fail.
	dedupQuery := `INSERT INTO processed_events (event_id, processed_at) VALUES ($1, $2)`
	_, err = tx.ExecContext(ctx, dedupQuery, eventID, time.Now())
	if err != nil {
		// Detect unique violation error code (PostgreSQL code 23505)
		if isUniqueConstraintViolation(err) {
			log.Printf("Duplicate event %s detected. Skipping logic.", eventID)
			return nil // Acknowledge success to skip processing
		}
		return err
	}

	// 2. Unmarshal business payload
	var payload OutboxPayload
	if err := json.Unmarshal(payloadBytes, &payload); err != nil {
		return err
	}

	// 3. Execute core business logic
	businessQuery := `UPDATE inventory_stock SET quantity = quantity - 1 WHERE order_id = $1 AND quantity > 0`
	res, err := tx.ExecContext(ctx, businessQuery, payload.OrderID)
	if err != nil {
		return err
	}
	rowsAffected, _ := res.RowsAffected()
	if rowsAffected == 0 {
		return errors.New("cannot process order: insufficient stock or invalid order ID")
	}

	// Commit atomically. If this fails, the deduplication insertion is rolled back too.
	return tx.Commit()
}

func isUniqueConstraintViolation(err error) bool {
	// 23505 is the PostgreSQL SQLState for unique_violation
	// This string check represents standard pgx/lib-pq driver behavior
	return err.Error() == `pq: duplicate key value violates unique constraint "processed_events_pkey"`
}

## The Outbox Cleanup Dilemma: Preventing Database Bloat

A major issue when scaling the Transactional Outbox pattern is that the `outbox_events` table is write-heavy and grows indefinitely. If we simply execute standard `DELETE` statements after the events are published, we encounter performance issues.

Deleting rows via `DELETE FROM outbox_events WHERE id = $1` creates massive index page splits and table fragmentation. Because of MVCC, rows are only marked as dead, necessitating aggressive vacuum runs that lock pages and degrade primary transaction write speeds.

### The Partitioning Solution

Instead of deleting individual rows, we partition the `outbox_events` table by time range (e.g., daily or weekly partitions). Once Debezium confirms that it has processed all events up to a certain LSN, we can drop the old partitions entirely. Dropping a table partition is a metadata-only operation in PostgreSQL; it bypasses MVCC table scanning, releases disk space instantly, and causes zero table bloat.

### Creating and Rotating Partitions

// snippet-6
-- Example: Creating and managing partitions for the outbox table

-- 1. Create a partition for week 25 of 2026
CREATE TABLE outbox_events_w2026_25 PARTITION OF outbox_events
    FOR VALUES FROM ('2026-06-15 00:00:00+00') TO ('2026-06-22 00:00:00+00');

-- 2. Detach an old partition (Week 24) when retention expires
-- Detaching is instantaneous and prevents locks on active writes
ALTER TABLE outbox_events DETACH PARTITION outbox_events_w2026_24;

-- 3. Drop the detached partition table in the background
DROP TABLE outbox_events_w2026_24;

To automate this in a production cluster, run a cron script that executes early before the week/day boundary, pre-allocates future partitions, and detaches/drops partitions that fall outside the retention SLA (e.g., 7 days).

// snippet-7
#!/usr/bin/env bash
# Database Outbox Partition Rotator
set -euo pipefail

DB_URL="postgres://postgres:secure_pass@postgres-primary:5432/orders_db?sslmode=disable"
RETENTION_DAYS=7

# Calculate time boundaries
NEXT_WEEK_START=$(date -d "+7 days" +"%Y-%m-%d 00:00:00+00")
NEXT_WEEK_END=$(date -d "+14 days" +"%Y-%m-%d 00:00:00+00")
PARTITION_NAME="outbox_events_w$(date -d "+7 days" +"%Y_w%V")"

echo "Pre-allocating partition: ${PARTITION_NAME} [${NEXT_WEEK_START} to ${NEXT_WEEK_END}]"
psql "${DB_URL}" -c "
    CREATE TABLE IF NOT EXISTS ${PARTITION_NAME} 
    PARTITION OF outbox_events
    FOR VALUES FROM ('${NEXT_WEEK_START}') TO ('${NEXT_WEEK_END}');
"

# Identify outdated partitions to drop
OLD_PARTITION="outbox_events_w$(date -d "-${RETENTION_DAYS} days" +"%Y_w%V")"
echo "Dropping expired partition: ${OLD_PARTITION}"

psql "${DB_URL}" -c "
    ALTER TABLE outbox_events DETACH PARTITION IF EXISTS ${OLD_PARTITION};
    DROP TABLE IF EXISTS ${OLD_PARTITION};
"

## Production Failure Modes and Mitigation Strategies

Scaling Debezium and Kafka Outbox pipelines exposes infrastructure-level edge cases that application code cannot hide from.

### 1. Replication Slot LSN Lag and Disk Exhaustion

PostgreSQL replication slots keep track of the LSN (Log Sequence Number) consumed by Debezium. If Debezium crashes or Kafka goes offline for hours, PostgreSQL cannot discard WAL files that have not yet been consumed.

* **The Problem:** The `pg_wal` directory grows rapidly. If disk space is exhausted, PostgreSQL shuts down, stopping the entire system.
* **Mitigation:**
  * Define `max_slot_wal_keep_size` in `postgresql.conf`. This setting limits the maximum disk space WAL files can occupy before the replica slot is invalidated. While this invalidates the replication slot (forcing a Debezium re-snapshot later), it protects the database server from crashing due to a full disk.
  * Implement active monitoring alerts on the PostgreSQL query:
  ```sql
  SELECT slot_name, pg_wal_lsn_diff(pg_current_wal_lsn(), confirmed_flush_lsn) AS lag_bytes FROM pg_replication_slots;
  ```
  Set critical alerts if `lag_bytes` exceeds 50GB.

### 2. Poison Pill Events in the Outbox

A poison pill occurs when a malformed record is inserted into the outbox table (e.g., due to an application bug or structural JSON schema mismatch).

* **The Problem:** Debezium reads the record but fails to serialize it to Kafka. It retries indefinitely, halting the CDC connector task.
* **Mitigation:**
  * Configure Kafka Connect's Error Handling properties to redirect records to a Dead Letter Queue (DLQ).
  * In the Kafka Connect worker properties, enable:
  ```properties
  errors.tolerance = all
  errors.deadletterqueue.topic.name = outbox.connector.dlq
  errors.deadletterqueue.context.headers.enable = true
  ```
  This isolates the failing record, sending it to the DLQ for offline analysis, while allowing the connector to process subsequent LSN offsets.

## Conclusion

Scaling distributed transactions requires shifting the synchronization complexity out of volatile application networks and into the hardened transaction log layer of databases. The Transactional Outbox pattern, driven by Debezium Change Data Capture, provides a decoupled, non-blocking pipeline capable of processing over 20,000 TPS without table locking or MVCC bloat. By coupling this log-tailing architecture with transactional idempotency checks on the consumer side, you build a resilient, eventually consistent pipeline that maintains absolute data integrity under extreme production workloads.