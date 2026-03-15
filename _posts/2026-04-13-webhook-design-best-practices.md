---
layout: post
title: "Webhook Design Best Practices: Reliability, Security, and Delivery Guarantees"
date: 2026-04-13 07:00:00 +0700
tags: [webhooks, apis, backend, reliability, security]
description: "Design robust webhook systems with guaranteed delivery, signature verification, retry logic, and consumer-friendly payloads."
---

Every distributed system eventually needs to push state changes to external consumers. Polling is wasteful, WebSockets are stateful, and message queues require shared infrastructure. Webhooks fill this gap elegantly — an HTTP POST fired when something happens. But the simplicity is deceptive. In production, webhooks fail silently, arrive out of order, get replayed by impatient consumers, and carry signatures that nobody verifies until after the breach. Building a webhook system that is reliable, secure, and genuinely useful to the engineers consuming it requires deliberate design across every layer: delivery semantics, payload shape, authentication, retry strategy, and observability. This post covers the patterns that separate toy webhook implementations from ones that survive real traffic.

## Payload Design: Thin Events vs. Fat Events

The first decision is how much information to include in the webhook body. A thin event carries only an identifier — `{"event": "order.completed", "id": "ord_123"}` — forcing consumers to call back for state. A fat event embeds the full resource snapshot. Thin events are simpler to produce but add a round-trip and create race conditions if the resource changes between the webhook and the fetch. Fat events solve this but bloat payloads and complicate schema evolution.

The pragmatic middle ground is a **fat event with a version field**. Include enough state that most consumers never need to call back, but document a stable schema version so you can evolve the shape without breaking existing subscribers.

<script src="https://gist.github.com/mohashari/4243a83c52245e2e39b8f2493b913aa0.js?file=snippet.go"></script>

Note the distinction between `id` (unique per delivery attempt) and `event_id` (stable). Consumers use `event_id` for idempotency; you use `id` for debugging specific deliveries.

## Signature Verification: HMAC-SHA256

Never send an unsigned webhook. Without a signature, any system that discovers your endpoint can forge events. The industry standard is HMAC-SHA256 computed over the raw request body with a shared secret, delivered in a header like `X-Webhook-Signature: sha256=<hex>`.

<script src="https://gist.github.com/mohashari/4243a83c52245e2e39b8f2493b913aa0.js?file=snippet-2.go"></script>

On the consumer side, always read the raw body before parsing JSON — once you decode into a struct, you've lost the bytes that were actually signed. Include a timestamp in the signed payload (not just the header) to reject replayed requests older than five minutes.

<script src="https://gist.github.com/mohashari/4243a83c52245e2e39b8f2493b913aa0.js?file=snippet-3.go"></script>

## Delivery Guarantees and the Outbox Pattern

The hardest part of webhooks is not signing them — it is guaranteeing they get sent at all. The naive approach of firing an HTTP request inline with a database write will eventually miss events when the process crashes between the write and the POST, or when the downstream endpoint is temporarily unavailable.

The outbox pattern solves this. Write the webhook payload to an `outbox` table inside the same database transaction as the domain event. A separate worker polls the outbox and delivers pending events, marking them delivered only after receiving a 2xx response.

<script src="https://gist.github.com/mohashari/4243a83c52245e2e39b8f2493b913aa0.js?file=snippet-4.sql"></script>

The index on `(status, next_attempt_at)` makes polling cheap even with millions of rows. The partial `WHERE status = 'pending'` keeps the index small as delivered rows accumulate.

## Retry Logic with Exponential Backoff

Consumers go down. Networks partition. Your retry strategy must handle both transient failures and genuinely dead endpoints without hammering a struggling service or silently dropping events.

<script src="https://gist.github.com/mohashari/4243a83c52245e2e39b8f2493b913aa0.js?file=snippet-5.go"></script>

After `MaxAttempts`, move the record to a dead-letter table rather than deleting it. Operators need to be able to inspect and replay failed deliveries. Expose a `POST /webhooks/deliveries/:id/replay` endpoint so consumers can trigger redelivery after fixing their handler.

## Consumer Idempotency

Because you will retry on any non-2xx response — including timeouts where the consumer *did* process the event — consumers must be idempotent. The canonical pattern is an `idempotency_keys` table that records processed `event_id` values.

<script src="https://gist.github.com/mohashari/4243a83c52245e2e39b8f2493b913aa0.js?file=snippet-6.go"></script>

The `ON CONFLICT DO NOTHING` with a subsequent rows-affected check is an atomic guard — no separate SELECT that could race under concurrent delivery.

## Endpoint Health Tracking

Long-term, you want to automatically disable endpoints that consistently return errors, rather than keeping them in the retry queue indefinitely. Track consecutive failures and pause delivery after a threshold.

<script src="https://gist.github.com/mohashari/4243a83c52245e2e39b8f2493b913aa0.js?file=snippet-7.sql"></script>

Send an email to the endpoint owner before disabling, and provide a self-service re-activation flow. Silently dropping events because an endpoint is unhealthy — without telling anyone — is one of the most common causes of data loss in webhook-based integrations.

Webhooks are deceptively simple to sketch on a whiteboard and deceptively hard to operate at scale. The patterns here — fat versioned payloads, HMAC signatures verified in constant time, transactional outbox writes, exponential backoff with jitter, consumer-side idempotency, and endpoint health tracking — address the failure modes that show up in every production system eventually. Implement them from the start rather than retrofitting reliability after an incident. Your consumers will trust your platform more, your on-call rotation will sleep better, and debugging a failed delivery will take minutes instead of days.