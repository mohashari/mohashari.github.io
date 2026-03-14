---
layout: post
title: "Event-Driven Architecture: Building Reactive Backend Systems"
tags: [architecture, backend, event-driven, messaging]
description: "Learn how event-driven architecture works, its patterns, benefits, and how to avoid the common pitfalls that sink EDA implementations."
---

Event-driven architecture (EDA) is a paradigm shift in how services communicate. Instead of direct calls ("do this now"), services emit events ("this happened") and other services react. The result: systems that are more resilient, scalable, and evolvable.

## The Core Concept

In a synchronous world:

```
Order Service → [HTTP POST] → Payment Service → [HTTP POST] → Inventory Service
```

In an event-driven world:

```
Order Service → emits "OrderPlaced" event
                          ↓
              Payment Service (subscribes) → processes payment → emits "PaymentCompleted"
              Inventory Service (subscribes) → reserves stock
              Email Service (subscribes) → sends confirmation
              Analytics Service (subscribes) → records sale
```

The Order Service doesn't know or care who's listening. Adding a new subscriber requires zero changes to the Order Service.

## Domain Events vs Integration Events

**Domain Events** represent something meaningful that happened within a bounded context:

```go
// Something happened in the Order domain
type OrderPlaced struct {
    OrderID    string    `json:"order_id"`
    CustomerID string    `json:"customer_id"`
    Items      []Item    `json:"items"`
    Total      float64   `json:"total"`
    PlacedAt   time.Time `json:"placed_at"`
}

type OrderCancelled struct {
    OrderID    string    `json:"order_id"`
    Reason     string    `json:"reason"`
    CancelledAt time.Time `json:"cancelled_at"`
}
```

**Integration Events** cross service boundaries. They're domain events published to a shared event bus.

## Event Sourcing

Instead of storing current state, store the sequence of events that led to it:

```
Traditional:
┌─────────────────────────────┐
│ Order Table                 │
│ id: 42, status: "shipped"   │
│ total: 99.99, items: [...]  │
└─────────────────────────────┘

Event Sourced:
┌──────────────────────────────────────────┐
│ Order Events Table                       │
│ 1. OrderPlaced   (2026-01-10 10:00)     │
│ 2. PaymentTaken  (2026-01-10 10:01)     │
│ 3. ItemsReserved (2026-01-10 10:02)     │
│ 4. OrderShipped  (2026-01-10 14:30)     │
└──────────────────────────────────────────┘
```

Current state = fold/reduce all events:

```go
func ReplayOrder(events []Event) Order {
    var order Order
    for _, event := range events {
        switch e := event.(type) {
        case OrderPlaced:
            order.ID = e.OrderID
            order.Status = "pending"
            order.Items = e.Items
            order.Total = e.Total
        case PaymentTaken:
            order.Status = "paid"
            order.PaidAt = e.Timestamp
        case OrderShipped:
            order.Status = "shipped"
            order.TrackingCode = e.TrackingCode
        }
    }
    return order
}
```

**Benefits:** Complete audit trail, time-travel debugging, event replay.
**Costs:** Query complexity (need projections/read models), eventual consistency.

## The Outbox Pattern: Ensuring Reliable Event Publishing

The #1 mistake in EDA: publishing events separately from database transactions.

```go
// WRONG: Race condition — DB succeeds but event publish fails
func PlaceOrder(order Order) error {
    db.Save(order)                    // DB commit
    eventBus.Publish("OrderPlaced")   // What if this fails?
    return nil
}
```

**Solution: Transactional Outbox**

```go
func PlaceOrder(ctx context.Context, order Order) error {
    return db.Transaction(func(tx *sql.Tx) error {
        // 1. Save the order
        if err := tx.Exec(`INSERT INTO orders ...`, order); err != nil {
            return err
        }

        // 2. Write event to outbox IN THE SAME TRANSACTION
        event := OrderPlacedEvent{OrderID: order.ID, ...}
        eventJSON, _ := json.Marshal(event)
        tx.Exec(`INSERT INTO outbox (event_type, payload, status)
                 VALUES (?, ?, 'pending')`, "OrderPlaced", eventJSON)

        return nil
    })
}

// Separate outbox processor (runs periodically)
func ProcessOutbox(ctx context.Context) {
    for {
        events, _ := db.Query(`SELECT id, event_type, payload
                                FROM outbox WHERE status = 'pending'
                                ORDER BY created_at LIMIT 100`)
        for _, event := range events {
            if err := eventBus.Publish(event.Type, event.Payload); err != nil {
                continue // Will retry next cycle
            }
            db.Exec(`UPDATE outbox SET status = 'published' WHERE id = ?`, event.ID)
        }
        time.Sleep(100 * time.Millisecond)
    }
}
```

## Idempotency in Event Consumers

Events can be delivered more than once (at-least-once delivery). Consumers must be idempotent:

```go
func HandleOrderPlaced(ctx context.Context, event OrderPlacedEvent) error {
    // Check if already processed
    if processed, _ := processedEvents.Has(event.EventID); processed {
        return nil  // Already handled, skip
    }

    // Process the event
    if err := paymentService.Charge(ctx, event); err != nil {
        return err
    }

    // Mark as processed
    processedEvents.Set(event.EventID, 24*time.Hour)
    return nil
}
```

## CQRS with Event-Driven Architecture

Command Query Responsibility Segregation pairs naturally with EDA:

```
Write Side (Commands):
User → PlaceOrder command → Order Service → stores events → publishes OrderPlaced

Read Side (Queries):
OrderPlaced event → Projection Service → updates read model (denormalized SQL/Redis)
User → GetOrderStatus query → reads from read model (fast!)
```

The read model is eventually consistent but optimized for query performance.

## Common Pitfalls

### 1. Making Events Too Granular
```go
// Too granular — causes chatty event storms
UserFirstNameChanged
UserLastNameChanged
UserEmailChanged

// Better — meaningful business event
UserProfileUpdated { Changes: [{field: "email", newValue: "..."}] }
```

### 2. Event Coupling via Shared Types
Don't share event classes across service boundaries. Each service owns its own event schema.

### 3. Long Event Chains
Deep event chains make debugging a nightmare. If you have 8 services reacting to each other's events, reconsider the design.

### 4. No Dead Letter Queue
Always have a DLQ for events that fail processing. Without it, you lose data silently.

```go
// After N retries, send to DLQ instead of dropping
func handleWithRetry(event Event, maxRetries int) {
    for attempt := 0; attempt < maxRetries; attempt++ {
        if err := processEvent(event); err == nil {
            return
        }
        time.Sleep(backoff(attempt))
    }
    dlq.Publish(event)  // Park for manual inspection
}
```

EDA is powerful but adds complexity. Use it where the benefits — loose coupling, scalability, audit trail — clearly outweigh the operational overhead.
