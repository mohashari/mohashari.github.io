---
layout: post
title: "Designing a Resilient Outbox Pattern: Dealing with Dual-Writes in Microservices"
date: 2026-04-20 09:00:00 +0700
tags: [microservices, system-design, cdc, debezium, kafka]
description: "How to avoid the microservices dual-write problem. A deep dive into designing the Transactional Outbox Pattern with Change Data Capture (CDC), Debezium, and Kafka."
image: ""
thumbnail: ""
---

In a microservices architecture, operations frequently cross service boundaries. A common requirement is for a service to update its local database *and* notify other services by publishing an event to a message broker (like Apache Kafka or RabbitMQ).

For example, when an order is created, the `Order Service` must:
1.  Insert an order record into the `orders` database table.
2.  Publish an `OrderCreated` event to Kafka so the `Inventory Service` can reserve stock.

This seemingly simple flow hides a major architectural trap: **The Dual-Write Problem**.

In this article, we'll analyze why dual-writes break distributed systems, look at the **Transactional Outbox Pattern** as a solution, and explore how to implement it using **Change Data Capture (CDC)** with **Debezium** and **Kafka**.

---

## 1. The Dual-Write Problem

A "dual-write" occurs when an application attempts to write to two different state stores (e.g., a relational database and a message broker) without a distributed transaction manager.

```
                  ┌────────────────────────┐
                  │     Order Service      │
                  └────┬──────────────┬────┘
                       │              │
        (Write DB)     │              │     (Publish Event)
                       ▼              ▼
           ┌──────────────┐        ┌──────────────┐
           │  PostgreSQL  │        │ Apache Kafka │
           └──────────────┘        └──────────────┘
```

Because databases and message brokers do not share a transaction coordinator, you cannot update both atomically. One of the writes can fail, leaving your system in an inconsistent state:

*   **Scenario A (DB first, Message second):** You write the order to the database. The network blips, or Kafka is down, and publishing the event fails.
    *   **Result:** The database contains the order, but the inventory service is never notified. The customer pays, but the stock is never reserved.
*   **Scenario B (Message first, DB second):** You publish the event to Kafka first. The database write then fails due to a constraint violation or connection timeout.
    *   **Result:** Kafka has the event, so the inventory service reserves stock, but the order is never actually saved in the order database.

### Why Not Two-Phase Commit (2PC)?
To solve this, some try using two-phase commit (2PC) or XA transactions. However, 2PC is a blocking protocol. If the coordinator crashes during the prepare phase, resources remain locked indefinitely. It also destroys throughput and is not supported by modern scalable message brokers like Kafka.

---

## 2. The Transactional Outbox Pattern

The **Transactional Outbox Pattern** solves the dual-write problem by leveraging a fundamental database guarantee: **ACID transactions**.

Instead of writing to the database and broker separately, the application performs both writes inside the *same* local database transaction. 

### How It Works

1.  **Define an Outbox Table:** You create a specialized table called `outbox` in the same database schema as your domain entities.
2.  **Atomic Transaction:** When a business event occurs, the application inserts the domain entity (the order) into the `orders` table *and* writes a corresponding event record into the `outbox` table inside the same transaction block.

```sql
BEGIN TRANSACTION;

INSERT INTO orders (id, customer_id, total) VALUES ('ord-991', 'cust-45', 129.99);

INSERT INTO outbox (id, aggregate_type, aggregate_id, event_type, payload) 
VALUES (
    'evt-505', 
    'Order', 
    'ord-991', 
    'OrderCreated', 
    '{"id": "ord-991", "total": 129.99}'
);

COMMIT;
```

Because both inserts happen in the same database transaction, they are guaranteed to either both succeed or both fail. No partial state is possible.

3.  **Outbox Publisher:** An independent background process (the Publisher) reads the events from the `outbox` table and publishes them to the message broker.
4.  **Mark/Delete as Sent:** Once the broker acknowledges receipt, the publisher marks the event as processed or deletes it from the `outbox` table.

---

## 3. Publisher Implementations: Polling vs. CDC

How should the Outbox Publisher read from the outbox table? There are two main strategies:

### Option A: Polling Publisher
The simplest approach is to have a background thread query the `outbox` table at regular intervals (e.g., every 500ms).

```sql
SELECT * FROM outbox WHERE processed = false ORDER BY id LIMIT 100 FOR UPDATE;
```

*   **Pros:** Easy to implement, requires no extra infrastructure.
*   **Cons:**
    *   **Database overhead:** Constant polling degrades database performance.
    *   **Latency:** There is a delay between database commit and event delivery equal to the polling interval.
    *   **Lock contention:** `FOR UPDATE` queries lock database rows, which can impact application transactions.

### Option B: Change Data Capture (CDC) with Debezium (Recommended)
Rather than active polling, **Change Data Capture** intercepts changes directly from the database's internal transaction log (e.g., PostgreSQL's **WAL** or MySQL's **binlog**).

**Debezium** is an open-source distributed CDC platform built on top of Kafka Connect.

```
PostgreSQL (WAL) ──► Debezium (Kafka Connect) ──► Kafka Topic (outbox.events)
```

1.  **Direct Log Scanning:** Debezium monitors the PostgreSQL WAL (Write-Ahead Log) in real-time. 
2.  **No Polling Overhead:** When PostgreSQL appends the `INSERT` into the `outbox` table to the WAL, Debezium reads it asynchronously, without running any SQL queries or acquiring locks.
3.  **Zero Loss:** Even if your application crashes, the WAL persists. Once Debezium restarts, it resumes reading from its last recorded log sequence number (LSN).
4.  **Kafka Routing:** Debezium translates the database log change into a JSON or Avro record and publishes it directly to a Kafka topic.

---

## 4. Handling At-Least-Once Delivery Semantics

No matter how resilient your publisher is, distributed networks guarantee **At-Least-Once Delivery**. 

If the publisher sends an event to Kafka, gets a network timeout, and retries, Kafka might receive the event twice. Alternatively, if the outbox publisher crashes *after* sending to Kafka but *before* marking the database outbox row as processed, the restarted publisher will send the event again.

Therefore, the receiving microservice **must be idempotent**.

### Implementing Idempotence in Consumers

To guarantee idempotency, the consumer service must track which events it has already processed.

```
Consumer Service ──(Event: evt-505)──► Checks "processed_events" Table
   │
   ├───► Event ID Exists? ──► Abort & Acknowledge (Duplicate)
   │
   └───► Event ID New?   ───► Process business logic & Insert Event ID inside one Transaction
```

1.  **Idempotency Key:** Every event must carry a unique ID (e.g., the `outbox.id`).
2.  **Processed Events Table:** The consumer database maintains a `processed_events` table tracking consumed event IDs.
3.  **Single Transaction Execution:** Inside the consumer's business transaction:
    *   Check if the event ID already exists in `processed_events`.
    *   If yes, ignore the event and immediately return success (acknowledging the broker).
    *   If no, process the business logic (e.g., deduct inventory) and insert the event ID into `processed_events`.

```sql
BEGIN TRANSACTION;

-- Check and insert in one step (fails if primary key constraint violated)
INSERT INTO processed_events (event_id, processed_at) VALUES ('evt-505', NOW());

-- Deduct inventory
UPDATE inventory SET stock = stock - 1 WHERE item_id = 'item-88' AND stock > 0;

COMMIT;
```

If the event is redelivered, the `INSERT INTO processed_events` fails with a unique key violation, rolling back the transaction and preventing duplicate inventory deductions.

---

## Conclusion

The dual-write problem is one of the most common vectors for silent data corruption in microservices. Attempting to write to two network endpoints sequentially is a race condition waiting to happen.

By implementing the **Transactional Outbox Pattern** with **Debezium CDC**, you move the synchronization complexity from unstable application code to the database's hardened transaction engine. Combined with **idempotent consumers** utilizing transactional deduplication, you build a bulletproof, eventual-consistency pipeline that scales without compromising correctness.
