---
layout: post
title: "Kafka Exactly-Once Semantics: Transactions, Idempotent Producers, and EOS Pitfalls"
date: 2026-03-29 08:00:00 +0700
tags: [kafka, distributed-systems, messaging, backend, reliability]
description: "How Kafka's EOS guarantees work under the hood, where they break in production, and what you actually need to configure to ship reliable pipelines."
image: "https://picsum.photos/1080/720?random=8013"
thumbnail: "https://picsum.photos/400/300?random=8013"
---

Your payment processing pipeline has been running clean for months. Then one Tuesday at 2 AM, a broker leader election takes 12 seconds instead of the usual 2. The producer retries. The message lands twice. You've charged a customer twice, your deduplication table misses it because the idempotency key was partitioned to a different shard, and now you're spending Friday writing an incident report instead of shipping features.

This is the problem Kafka's Exactly-Once Semantics (EOS) was designed to solve. But "exactly-once" is one of the most overloaded terms in distributed systems, and Kafka's implementation comes with a precise set of guarantees — and equally precise ways to violate them. This post covers how idempotent producers and transactions actually work, what EOS does and doesn't guarantee, and the specific production pitfalls that will catch you if you treat it as a magic bullet.

## What "Exactly-Once" Actually Means in Kafka's Context

Let's be precise. Kafka EOS guarantees **atomic writes across multiple partitions** and **idempotent delivery from a single producer instance**. It does not guarantee that your downstream consumer processes a message exactly once — that part is on you.

There are three delivery semantics worth naming:

- **At-most-once**: Fire and forget. Messages can be lost. No retries. `acks=0`.
- **At-least-once**: Retries enabled, but duplicates are possible. The default if you set `acks=all` without idempotence.
- **Exactly-once**: No loss, no duplicates — within Kafka's boundaries.

The critical phrase is "within Kafka's boundaries." EOS covers the producer-to-broker path and the broker-to-consumer path when using Kafka Streams or a transactional consumer with `isolation.level=read_committed`. It does not cover side effects in your application logic, external database writes, or HTTP calls you make from your consumer.

## Idempotent Producers: The Foundation

Idempotence in Kafka is implemented at the producer level using a **Producer ID (PID)** assigned by the broker and a **sequence number** attached to each message. The broker deduplicates based on `(PID, partition, sequence_number)`.

<script src="https://gist.github.com/mohashari/e07c1776095307cd6c2b8b161a5cd522.js?file=snippet-1.txt"></script>

Setting `enable.idempotence=true` automatically enforces `acks=all`, `retries=MAX_INT`, and `max.in.flight.requests.per.connection≤5`. If you try to configure these inconsistently (e.g., `acks=1` with idempotence enabled), the producer throws a `ConfigException` at startup — not silently degrades.

The sequence number is per-partition. If the broker receives sequence 42 again after already persisting it, it acknowledges the duplicate and discards the write. Sequence gaps (receiving 44 before 43) cause an `OutOfOrderSequenceException`.

One important caveat: **PIDs are ephemeral across restarts.** If your producer restarts, it gets a new PID, and the deduplication window resets. This means idempotence alone only covers in-flight retries within a single producer session — not crashes or restarts.

## Transactions: Atomic Multi-Partition Writes

For cross-partition atomicity and crash recovery, you need transactions. Kafka implements this using a **two-phase commit** protocol mediated by a **Transaction Coordinator** — a special broker role backed by the `__transaction_state` internal topic.

The flow:
1. Producer calls `initTransactions()` — broker assigns a `transactional.id` and increments the **epoch** for that ID, fencing any zombie producers with the same ID.
2. `beginTransaction()` — local state change only, no broker round-trip.
3. `send()` — messages are written but marked as part of an open transaction.
4. `commitTransaction()` — coordinator writes a `PREPARE_COMMIT` marker, waits for replication, then writes `COMMIT` markers to each involved partition.
5. On crash/abort: coordinator writes `ABORT` markers, consumers with `read_committed` skip those messages.

<script src="https://gist.github.com/mohashari/e07c1776095307cd6c2b8b161a5cd522.js?file=snippet-2.txt"></script>

The `transactional.id` is the key to crash recovery. When a producer restarts and calls `initTransactions()` with the same ID, the coordinator bumps the epoch. Any other producer instance using an old epoch gets `ProducerFencedException` immediately — this is the zombie fencing mechanism.

## Consumer-Side: read_committed Isolation

Transactions only matter if consumers respect them. The `isolation.level` setting controls this:

<script src="https://gist.github.com/mohashari/e07c1776095307cd6c2b8b161a5cd522.js?file=snippet-3.txt"></script>

With `read_uncommitted` (the default), consumers see messages from aborted transactions. In a payment pipeline, this means a consumer could act on a message from a transaction that later gets rolled back — exactly the scenario you were trying to prevent.

The performance cost of `read_committed` is real: consumers track the **Last Stable Offset (LSO)** rather than the High Watermark (HW). If a transaction is left open (e.g., a slow or stuck producer), LSO stops advancing and consumers stall. This is one of the most common EOS-related outages in production.

## The Consume-Transform-Produce Pattern

The canonical EOS use case in Kafka Streams and stream processing generally is **consume-transform-produce**: read from topic A, process, write to topic B, commit offset — all atomically.

<script src="https://gist.github.com/mohashari/e07c1776095307cd6c2b8b161a5cd522.js?file=snippet-4.txt"></script>

`sendOffsetsToTransaction` is the glue. It writes the consumer group offset into `__consumer_offsets` as part of the same atomic transaction. If the transaction commits, both the output messages and the offset advance atomically. If it aborts, neither does.

## Production Pitfalls That Will Bite You

**1. Transaction timeout vs. processing time mismatch**

`transaction.timeout.ms` defaults to 60 seconds. The Transaction Coordinator aborts any transaction open longer than this. If your processing logic does any I/O (database lookups, HTTP calls) inside a transaction, you can easily exceed this. The abort happens broker-side — your producer doesn't know until the next `send()` fails with `InvalidTxnStateException`.

Fix: Keep transactions short. Do heavy computation before `beginTransaction()`. Set `transaction.timeout.ms` to match your actual P99 processing time plus headroom, not just the default.

**2. Zombie producers and epoch collisions**

If you run multiple instances of a service sharing the same `transactional.id` (e.g., a botched blue-green deploy), only one will survive. The first to call `initTransactions()` wins; the other gets `ProducerFencedException`. This is correct behavior — but if your deployment automation doesn't handle it, you'll have services crashing in a loop.

Fix: Make `transactional.id` truly unique per logical producer. Include the partition assignment or instance ID. In Kubernetes, the pod name works well.

**3. LSO stall from long-running transactions**

A single stuck producer with an open transaction can stall all consumers reading with `read_committed`, even consumers reading unrelated partitions on the same broker. The LSO is per-partition, but a slow transaction causes consumers to wait at fetch time.

Monitor `kafka.server:type=FetcherLagMetrics` and alert on `LastStableOffsetLag` exceeding your acceptable latency threshold. If LSO consistently lags HW by more than a few seconds, you have a producer holding transactions open.

**4. EOS doesn't protect external side effects**

This deserves emphasis: if your consumer makes a database write, sends an email, or calls an external API, Kafka's EOS does nothing to protect those operations. A consumer can commit its offset inside a Kafka transaction and still have failed halfway through updating your PostgreSQL.

```python
# snippet-5
# This is NOT exactly-once end-to-end — the DB write and offset commit are not atomic
def process_payment(consumer, db_conn):
    records = consumer.poll(timeout_ms=100)
    for record in records:
        # If this succeeds but the commit below fails, you process twice on restart
        db_conn.execute("INSERT INTO payments ...", record.value)
        db_conn.commit()
    
    # If this fails after the DB write, you've written to DB but not committed offset
    consumer.commit()

# For true end-to-end EOS with an external DB, you need outbox pattern or
# transactional outbox with a change data capture pipeline back into Kafka
```

For true end-to-end exactly-once with external systems, you need the **transactional outbox pattern**: write to your database and to an outbox table in a single DB transaction, then use CDC (Debezium, for example) to capture the outbox and publish to Kafka. This moves the atomicity boundary to where you can control it.

**5. Rebalances invalidating producer state**

When a consumer group rebalances during a transaction, the partition assignment changes. If your produce-with-offsets pattern ties a specific partition to a specific producer instance, a rebalance mid-transaction requires aborting the current transaction before releasing the partition.

<script src="https://gist.github.com/mohashari/e07c1776095307cd6c2b8b161a5cd522.js?file=snippet-6.txt"></script>

## Kafka Streams: EOS Built In

If you're using Kafka Streams, EOS is much simpler to enable. Since version 2.6, `exactly_once_v2` is the recommended setting:

```yaml
# snippet-7
# application.properties for Kafka Streams
application.id: payment-stream-processor
bootstrap.servers: kafka1:9092,kafka2:9092,kafka3:9092

# exactly_once_v2 uses one transactional producer per stream task (not per partition)
# Reduces broker load compared to the original exactly_once setting
processing.guarantee: exactly_once_v2

# With EOS, commit intervals control transaction frequency vs. latency tradeoff
# Lower = more transactions, more overhead, lower latency
commit.interval.ms: 100

# Buffer size affects how many records accumulate before a transaction commits
# Larger buffers = fewer transactions = better throughput but higher latency
cache.max.bytes.buffering: 10485760
```

Kafka Streams handles the `sendOffsetsToTransaction` dance, rebalance listeners, and producer epoch management for you. If you're building a stream processing application on Kafka, use Kafka Streams or flink with Kafka connectors rather than reimplementing this yourself.

## Monitoring EOS in Production

The metrics that matter for EOS health:

- **`producer-metrics:txn-abort-rate`**: Frequent aborts signal processing time issues or fencing.
- **`kafka.server:type=BrokerTopicMetrics,name=ProduceMessageConversionsPerSec`**: Idempotent produce overhead.
- **`LastStableOffsetLag` per partition**: Leading indicator for LSO stall.
- **`kafka.coordinator.transaction:type=TransactionMarkerChannelMetrics`**: Transaction coordinator health.
- **`UnderMinIsrPartitionCount`**: If replication is degraded, transactions will be slow or fail.

Set alerts on `LastStableOffsetLag > 5000` (messages) and `txn-abort-rate > 0.01` (per second). Both indicate EOS machinery under stress before it becomes a visible outage.

## When Not to Use Transactions

EOS is not free. Each transaction adds two extra round-trips to the coordinator (prepare + commit), and `read_committed` consumers carry the LSO overhead. For high-throughput, low-latency use cases where at-least-once is acceptable (metrics pipelines, clickstream data, log aggregation), idempotent producers without transactions give you most of the protection without the overhead.

Use transactions when:
- You write to multiple topics and partial failure is unacceptable
- You need consume-transform-produce atomicity
- Downstream systems cannot tolerate duplicates at all

Skip transactions when:
- Single-topic writes where idempotence alone covers your failure modes
- High-throughput pipelines where the coordinator overhead degrades latency
- Your consumers don't use `read_committed` anyway (transactional producers with naive consumers give you no additional guarantee)

EOS in Kafka is genuinely powerful when you understand what it actually covers. The mistake engineers make is treating it as an end-to-end guarantee rather than a Kafka-internal guarantee. Know the boundary, instrument the right metrics, and handle external side effects separately — and you can build payment-grade reliability on top of it.
```