---
layout: post
title: "Message Queues: When to Use Kafka vs RabbitMQ"
tags: [kafka, rabbitmq, messaging, backend, architecture]
description: "A practical comparison of Kafka and RabbitMQ to help you choose the right message broker for your use case."
---

Message queues are the backbone of resilient, decoupled systems. But choosing between Kafka and RabbitMQ can feel overwhelming. This guide cuts through the marketing to give you a practical decision framework.

## The Core Difference

**RabbitMQ** is a traditional message broker. Messages are pushed to consumers, and once consumed and acknowledged, they're gone.

**Kafka** is a distributed event streaming platform. Messages are written to a log and retained for a configurable time. Consumers read at their own pace and can replay messages.

```
RabbitMQ:
Producer → Queue → Consumer (message deleted after ack)

Kafka:
Producer → Topic/Partition → Consumer Group A (reads at offset X)
                           → Consumer Group B (reads at offset Y)
                           (messages retained for N days)
```

## When to Choose RabbitMQ

RabbitMQ excels at **task queues** and **complex routing**:

- **Work queue / task distribution** — Background jobs, email sending, report generation
- **Complex routing logic** — Route messages to different queues based on headers, topic, fanout
- **Per-message TTL and priority** — Dead letter queues built-in
- **When you need guaranteed delivery** with ack/nack
- **Lower message volume** (< 50K msg/s)

```python
# RabbitMQ: Simple work queue
import pika

connection = pika.BlockingConnection(pika.ConnectionParameters('localhost'))
channel = connection.channel()

channel.queue_declare(queue='email_jobs', durable=True)

# Producer
channel.basic_publish(
    exchange='',
    routing_key='email_jobs',
    body=json.dumps({'to': 'user@example.com', 'template': 'welcome'}),
    properties=pika.BasicProperties(delivery_mode=2)  # Persistent
)

# Consumer
def process_email(ch, method, properties, body):
    job = json.loads(body)
    send_email(job['to'], job['template'])
    ch.basic_ack(delivery_tag=method.delivery_tag)

channel.basic_qos(prefetch_count=1)
channel.basic_consume(queue='email_jobs', on_message_callback=process_email)
channel.start_consuming()
```

## When to Choose Kafka

Kafka excels at **event streaming** and **high-throughput** scenarios:

- **Event sourcing** — Store a complete history of what happened
- **High throughput** — Millions of messages per second
- **Multiple consumers** needing the same messages independently
- **Stream processing** — Real-time analytics, ETL pipelines
- **Audit log** — Immutable, replayable event history
- **Microservices event bus** — Decouple services via events

```go
// Kafka: Event producer
writer := kafka.NewWriter(kafka.WriterConfig{
    Brokers: []string{"localhost:9092"},
    Topic:   "user-events",
    Balancer: &kafka.LeastBytes{},
})

err := writer.WriteMessages(ctx, kafka.Message{
    Key:   []byte(userID),
    Value: json.Marshal(UserLoggedInEvent{
        UserID:    userID,
        Timestamp: time.Now(),
        IPAddress: ip,
    }),
})

// Kafka: Consumer group
reader := kafka.NewReader(kafka.ReaderConfig{
    Brokers:  []string{"localhost:9092"},
    Topic:    "user-events",
    GroupID:  "analytics-service",
    MinBytes: 10e3,
    MaxBytes: 10e6,
})

for {
    msg, err := reader.ReadMessage(ctx)
    if err != nil {
        break
    }
    processEvent(msg.Value)
}
```

## Kafka Key Concepts

### Partitions and Consumer Groups

Kafka scales horizontally via **partitions**. A topic has N partitions. A consumer group has M consumers, each reading from a subset of partitions.

```
Topic: user-events (3 partitions)
├── Partition 0 → Consumer A (in group "analytics")
├── Partition 1 → Consumer B (in group "analytics")
└── Partition 2 → Consumer C (in group "analytics")

Same topic, different group:
├── Partition 0,1,2 → Consumer X (in group "audit-log")
```

**Rule: # consumers ≤ # partitions in a group**. Extra consumers sit idle.

### Message Keys for Ordering

Kafka guarantees order **within a partition**. Use a key to ensure related messages go to the same partition:

```go
// All events for user 42 go to the same partition
kafka.Message{
    Key:   []byte("user-42"),  // Hashed to determine partition
    Value: eventData,
}
```

### Consumer Offsets

Kafka tracks where each consumer group is in each partition via **offsets**. You can reset to replay:

```bash
# Reset consumer group to beginning to replay all events
kafka-consumer-groups.sh --bootstrap-server localhost:9092 \
  --group my-consumer-group \
  --topic user-events \
  --reset-offsets --to-earliest --execute
```

## Comparison Table

| Feature | RabbitMQ | Kafka |
|---------|----------|-------|
| Message retention | Until consumed | Time/size based (days) |
| Message replay | No | Yes |
| Throughput | High (100K/s) | Very high (1M+/s) |
| Consumer model | Push | Pull |
| Routing | Flexible (exchange types) | Topic/Partition |
| Ordering | Per-queue | Per-partition |
| Use case | Task queues | Event streaming |
| Complexity | Lower | Higher |

## Decision Framework

```
Is your primary use case:

Background jobs / task distribution?
  → RabbitMQ

Real-time event streaming / high throughput?
  → Kafka

Multiple independent consumers of same data?
  → Kafka

Complex routing (topic, header, fanout)?
  → RabbitMQ

Need to replay events / audit history?
  → Kafka

Simple pub/sub at moderate scale?
  → Either (RabbitMQ simpler to operate)
```

## Don't Forget Dead Letter Queues

Both systems support handling messages that can't be processed. Always configure DLQs:

```python
# RabbitMQ DLQ
channel.queue_declare(
    queue='email_jobs',
    arguments={
        'x-dead-letter-exchange': 'dlx',
        'x-dead-letter-routing-key': 'email_jobs.failed',
        'x-message-ttl': 3600000  # 1 hour
    }
)
```

Messages that fail (after retries) land in the dead letter queue for inspection and manual reprocessing.

Choose based on your actual requirements, not hype. Both are excellent tools for what they're designed for.
