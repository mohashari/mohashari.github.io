---
layout: post
title: "Implementing a Custom Transaction Coordinator for Distributed Sagas with Outbox Pattern in Rust"
date: 2026-08-27 08:00:00 +0700
tags: [rust, distributed-systems, microservices, postgresql, kafka]
description: "Build a high-performance, sub-millisecond transaction coordinator in Rust using the transactional outbox pattern and PostgreSQL SKIP LOCKED."
image: "https://picsum.photos/seed/7877/1080/720"
thumbnail: "https://picsum.photos/seed/7877/400/300"
---

Imagine building a high-volume checkout flow processing 500+ transactions per second. In a distributed microservices environment, ensuring transactional integrity without distributed locks (which kill throughput) or 2-Phase Commit (which blocks resources and degrades reliability) is one of the most painful design challenges you'll face. The moment a network call fails midway through a series of HTTP requests, you are left with a split-brain state: an order is marked as "paid" in the billing service, but the inventory service failed to reserve the stock, leaving your customer in limbo. Distributed Sagas solve this by using compensating actions to achieve eventual consistency. However, using off-the-shelf orchestration tools like Temporal or AWS Step Functions introduces massive latency overhead, complex runtime dependencies, and vendor lock-in. A custom Transaction Coordinator (TC) built in Rust, powered by PostgreSQL and a Transactional Outbox pattern, offers sub-millisecond execution overhead, zero-cost state transitions, and a strict guarantee of message dispatching even if the coordinator crashes during a network partition.

![Implementing a Custom Transaction Coordinator for Distributed Sagas with Outbox Pattern in Rust Diagram](/images/diagrams/implementing-custom-transaction-coordinator-distributed-sagas-outbox-pattern-rust.svg)

## Anatomy of a Resilient Saga State Machine

An orchestrator-based saga coordinates execution flow by explicitly directing participants on what actions to take. To avoid spaghetti event chains, the coordinator must maintain a centralized state machine. In Rust, we represent this state machine using algebraic data types (`enum`), ensuring that state transitions are deterministic, type-safe, and exhaustive. 

Every saga step consists of an execution phase and a corresponding compensation phase. If a step fails, the coordinator transitions into a compensation state, executing the compensation actions in reverse order. The transitions must be modeled as a pure function: given the current state of the saga and an incoming event, the state machine must return the next state and a set of command events to publish.

Here is how we model the saga state and steps in Rust:

<script src="https://gist.github.com/mohashari/fc5078a649af8c6e0cd6fb1dad96e632.js?file=snippet-1.txt"></script>

## The Transactional Outbox Pattern: Bridging State and Events

A common failure mode in distributed systems is the "dual-write" problem. If the coordinator updates the saga state in the database and then immediately publishes an event to Kafka via a network call, a crash or network partition between those two actions leaves the system in an inconsistent state. If the database update fails but the event is published, downstream services act on phantom events. If the database update succeeds but event publishing fails, the saga halts permanently.

To solve this, we implement the Transactional Outbox pattern. Within a single PostgreSQL ACID transaction, we write the updated saga state to the `saga_instances` table and write the command event to an `outbox` table. PostgreSQL guarantees that either both writes succeed or both are rolled back.

```sql
-- snippet-2
CREATE TABLE saga_instances (
    saga_id UUID PRIMARY KEY,
    order_id UUID NOT NULL,
    customer_id UUID NOT NULL,
    total_amount BIGINT NOT NULL,
    items TEXT[] NOT NULL,
    status VARCHAR(50) NOT NULL,
    version INT NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE outbox_table (
    event_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    saga_id UUID NOT NULL,
    aggregate_type VARCHAR(100) NOT NULL,
    payload JSONB NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Index for high-performance outbox polling
CREATE INDEX idx_outbox_pending 
ON outbox_table (created_at) 
WHERE status = 'pending';

-- Optimistic locking index
CREATE UNIQUE INDEX idx_saga_instances_id_version 
ON saga_instances (saga_id, version);
```

## Implementing the Core Coordinator Engine in Rust

The coordinator engine must ingest events from a queue, load the saga instance, apply the state machine transition, and persist the results atomically. To handle high concurrency, we use optimistic concurrency control (OCC). Every saga instance has a `version` field. When updating the instance, we assert that the version matches what we loaded. If it doesn't, another thread has modified the instance, and we abort and retry.

Here is the implementation of the transition handler utilizing `sqlx` and PostgreSQL:

<script src="https://gist.github.com/mohashari/fc5078a649af8c6e0cd6fb1dad96e632.js?file=snippet-3.txt"></script>

## The Outbox Publisher: Skip-Locked Polling with Tokio

With the events written to the database, a background worker must poll the `outbox_table`, publish the events to the message broker, and mark them as published. A naive query like `SELECT * FROM outbox_table WHERE status = 'pending'` will cause severe lock contention if scaled horizontally across multiple application nodes.

To run this background process concurrently without nodes blocking each other or processing duplicate messages, we use PostgreSQL’s `SKIP LOCKED` modifier. This allows worker threads to query the database, lock only the rows they intend to process, and skip any rows that are currently locked by other instances of the worker.

<script src="https://gist.github.com/mohashari/fc5078a649af8c6e0cd6fb1dad96e632.js?file=snippet-4.txt"></script>

## Handling Dual-Writes and Crash Recovery

The Transactional Outbox pattern guarantees that events are written to the database. However, network partitions between the publisher and the Kafka broker will occur. If the worker crashes *after* publishing the event to Kafka but *before* committing the updated status to the database, the same message will be published again when the service restarts.

To maintain system integrity, we must enforce two strict rules:

1. **At-Least-Once Delivery**: The outbox worker must keep retrying until the broker acknowledges receipt.
2. **Idempotence**: Downstream services (inventory, billing) must implement idempotency. They must store the processed `saga_id` (or unique request key) and ignore duplicate command events.

If a payment charge fails, our coordinator must trigger compensation. The compensating step changes the saga state to `InventoryReleasing` and inserts the `ReleaseInventory` command into the outbox in a single transaction.

<script src="https://gist.github.com/mohashari/fc5078a649af8c6e0cd6fb1dad96e632.js?file=snippet-5.txt"></script>

## Performance Optimization: Low Latency Event Loops

Polling the database using standard interval timers (`sleep(100ms)`) creates an undesirable trade-off between processor overhead and system latency. Under low traffic, a 100ms sleep introduces latency. Under heavy load, rapid polling consumes database connections and increases engine CPU usage.

To achieve sub-millisecond dispatch times without database overhead, we combine polling with PostgreSQL’s asynchronous notification system (`LISTEN/NOTIFY`). When a new record is inserted into the `outbox_table`, a database trigger fires a `NOTIFY` signal. The Outbox Publisher listens to this channel on a dedicated thread and processes the pending batch instantly.

<script src="https://gist.github.com/mohashari/fc5078a649af8c6e0cd6fb1dad96e632.js?file=snippet-6.txt"></script>

To configure this trigger in your database, execute the following SQL migration script:

```sql
-- snippet-7
CREATE OR REPLACE FUNCTION notify_outbox_insertion()
RETURNS TRIGGER AS $$
BEGIN
  PERFORM pg_notify('outbox_inserted', NEW.event_id::text);
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER outbox_insert_trigger
AFTER INSERT ON outbox_table
FOR EACH ROW
EXECUTE FUNCTION notify_outbox_insertion();
```

## Tuning Postgres Connection Pools & Kafka Delivery for High Throughput

To scale this coordinator to production-level throughput, configuration tuning is required. You cannot rely on default pool limits or Kafka publisher defaults.

### PostgreSQL Pool Tuning

When initializing the connection pool with `sqlx`, configure a dedicated execution pool separate from your API web server connection pool. 

- `max_connections`: Keep this aligned with your database processor cores. For a PostgreSQL DB with 8 vCPUs, set this to `50` to prevent thread context switching.
- `min_connections`: Pre-warm `10` connections to eliminate connection establishment latency on cold starts.
- `acquire_timeout`: Set this to `2000ms`. If your application cannot acquire a database connection in 2 seconds under peak load, fail fast to allow the system to apply backpressure.

<script src="https://gist.github.com/mohashari/fc5078a649af8c6e0cd6fb1dad96e632.js?file=snippet-8.txt"></script>

### Kafka Producer Settings

To guarantee that events written to the database outbox are successfully written to the partition replicas, configure your producer properties to match your consistency requirements:

- `acks=all`: The coordinator waits until the leader broker and all in-sync replicas acknowledge the message.
- `retries=2147483647`: Instruct the publisher to retry indefinitely when experiencing network hiccups.
- `max.in.flight.requests.per.connection=1`: Guarantees message ordering. If message batch A fails and batch B succeeds, batch A is retried without out-of-order writes.
- `compression.codec=zstd`: Reduces network payload sizes by up to 40% with minimal CPU overhead.

By shifting distributed coordination logic directly into the database engine through Rust's safe abstractions, we bypass the need for external workflow orchestrators, keeping our system architecture lean, fast, and simple to debug in production.