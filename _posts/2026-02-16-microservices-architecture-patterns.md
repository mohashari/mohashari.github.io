---
layout: post
title: "Microservices Architecture Patterns Every Engineer Should Know"
tags: [microservices, architecture, backend]
description: "The essential microservices patterns — from service decomposition to inter-service communication and fault tolerance."
---

Microservices aren't a silver bullet. They solve real problems while creating new ones. This post covers the patterns that make microservices work in practice — and the pitfalls that bring them down.

## When to Go Microservices

Don't start with microservices. Start with a monolith, then extract services when you feel the pain:

- **Independent scaling** — One service needs 10x more instances than others
- **Independent deployment** — Teams are blocked waiting for release coordination
- **Technology diversity** — One component genuinely needs a different language/database
- **Organizational boundaries** — Conway's Law: structure follows org chart

The "microservices first" approach almost always leads to distributed monolith hell.

## Decomposition Patterns

### Decompose by Business Capability

Organize around business functions, not technical layers:

```
# Bad (technical layers)
frontend-service
business-logic-service
database-service

# Good (business capabilities)
user-service
payment-service
notification-service
inventory-service
order-service
```

### Decompose by Subdomain (DDD)

Use Domain-Driven Design to find service boundaries. Each bounded context becomes a service candidate.

## Communication Patterns

### Synchronous (REST/gRPC)

Use for operations requiring an immediate response:

```
Client → Order Service → [HTTP] → Inventory Service → [HTTP] → Payment Service
```

Problem: Coupling. If payment service is down, the whole chain fails.

### Asynchronous (Message Queue)

Use for operations that don't require immediate response:

```
Order Service → [Kafka] → Payment Service
                       → Inventory Service
                       → Notification Service
```

Benefits: Decoupling, resilience, natural retry mechanism.

```go
// Producer
func PlaceOrder(order Order) error {
    if err := db.Save(order); err != nil {
        return err
    }

    event := OrderPlacedEvent{
        OrderID:   order.ID,
        UserID:    order.UserID,
        Items:     order.Items,
        Total:     order.Total,
        Timestamp: time.Now(),
    }

    return kafka.Publish("orders.placed", event)
}

// Consumer (in payment-service)
func HandleOrderPlaced(event OrderPlacedEvent) error {
    return paymentService.ChargeUser(event.UserID, event.Total, event.OrderID)
}
```

## The API Gateway Pattern

Never expose your internal services directly. Use an API Gateway as the single entry point:

```
                    ┌─────────────────┐
Clients ──────────→ │   API Gateway   │
                    └────────┬────────┘
                             │
              ┌──────────────┼──────────────┐
              ↓              ↓              ↓
        user-service   order-service  product-service
```

The gateway handles:
- Authentication/Authorization
- Rate limiting
- Request routing
- SSL termination
- Response aggregation

## The Saga Pattern for Distributed Transactions

ACID transactions don't work across services. Use sagas — a sequence of local transactions with compensating transactions for rollback.

### Choreography-based Saga

Services react to events:

```
1. Order Service: Creates order → publishes "order.created"
2. Payment Service: Charges card → publishes "payment.processed"
                    (if fails) → publishes "payment.failed"
3. Inventory Service: Reserves stock → publishes "stock.reserved"
                       (if fails) → publishes "stock.insufficient"
4. Order Service:
   - On "payment.failed" → cancels order
   - On "stock.insufficient" → refunds payment, cancels order
```

### Orchestration-based Saga

A central coordinator drives the steps:

```go
type OrderSaga struct{}

func (s *OrderSaga) Execute(orderID string) error {
    steps := []SagaStep{
        {Execute: paymentService.Charge,    Compensate: paymentService.Refund},
        {Execute: inventoryService.Reserve, Compensate: inventoryService.Release},
        {Execute: shippingService.Schedule, Compensate: shippingService.Cancel},
    }

    var completed []SagaStep
    for _, step := range steps {
        if err := step.Execute(orderID); err != nil {
            // Rollback in reverse order
            for i := len(completed) - 1; i >= 0; i-- {
                completed[i].Compensate(orderID)
            }
            return err
        }
        completed = append(completed, step)
    }
    return nil
}
```

## Circuit Breaker Pattern

Prevent cascading failures when a downstream service is struggling:

```go
type CircuitBreaker struct {
    failures   int
    threshold  int
    lastFailed time.Time
    timeout    time.Duration
    state      string // "closed", "open", "half-open"
}

func (cb *CircuitBreaker) Call(fn func() error) error {
    if cb.state == "open" {
        if time.Since(cb.lastFailed) < cb.timeout {
            return errors.New("circuit breaker open")
        }
        cb.state = "half-open"
    }

    err := fn()
    if err != nil {
        cb.failures++
        cb.lastFailed = time.Now()
        if cb.failures >= cb.threshold {
            cb.state = "open"
        }
        return err
    }

    cb.failures = 0
    cb.state = "closed"
    return nil
}
```

Use libraries like `gobreaker` (Go) or `resilience4j` (Java) in production.

## Service Mesh

For complex microservices deployments, consider a service mesh (Istio, Linkerd):

- **mTLS** between all services
- **Distributed tracing** without code changes
- **Traffic management** (canary, weighted routing)
- **Circuit breaking** and **retries** at the infrastructure level

## The Most Important Rule

**Don't build microservices you don't need yet.** The architecture cost is real. Embrace the monolith, identify the seams, and extract thoughtfully when the business justifies it.
