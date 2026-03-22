---
layout: post
title: "REST API Design: Principles Senior Engineers Follow"
date: 2026-03-22 08:00:00 +0700
tags: [api-design, backend, rest, versioning, architecture]
description: "Beyond CRUD: versioning strategies, idempotency guarantees, and backward compatibility patterns that keep APIs maintainable at scale."
---

The breaking change that woke you up at 2am probably wasn't a bug — it was a design decision made months earlier when someone renamed a field, removed a status enum value, or changed a 200 to a 201 without thinking about the 40 clients already in production. REST APIs accrue breaking changes the way legacy codebases accrue technical debt: slowly, then suddenly. The difference between an API that scales across teams and one that requires a migration guide every quarter isn't framework choice or documentation quality. It's a handful of concrete design decisions made before the first endpoint ships.

## Resource Modeling Is Where You Win or Lose

The resource model is your API's schema — get it wrong and every subsequent decision compounds the mistake. Senior engineers know that REST resources are not database tables. Mapping your ORM models directly to endpoints is the most common mistake, and it shows up as anemic APIs that force clients to assemble business entities from five round trips.

Model resources around client workflows, not your storage layer. If your clients consistently need an order with its line items, shipping address, and payment status in a single view, that's your resource — not three separate normalized entities. The `/orders/{id}/detail` vs `/orders/{id}` debate is a symptom of having modeled the database, not the domain.

Naming is load-bearing. Use plural nouns for collections (`/users`, `/invoices`), and avoid verbs in resource paths. `/processPayment` is an RPC endpoint wearing REST clothing. The action lives in the HTTP method or, for complex state transitions, in a sub-resource: `POST /invoices/{id}/payments` rather than `POST /invoices/{id}/pay`. This distinction matters because it forces you to think about what you're creating — a payment resource — which has its own lifecycle, idempotency requirements, and audit trail.

Hierarchical paths should reflect genuine ownership, not convenient grouping. `/users/{id}/orders` makes sense if orders cannot exist without users. If orders are independently addressable entities that happen to belong to a user, expose them at the root and filter: `GET /orders?user_id={id}`. Deep nesting beyond two levels is almost always a sign that you're modeling a query, not a resource.

## Versioning: Pick a Strategy Before You Need One

Every API eventually breaks backward compatibility. The question isn't whether you'll version — it's whether you planned for it. URI versioning (`/v1/`, `/v2/`) is operationally simple and immediately visible in logs, proxies, and documentation. Header versioning (`Accept: application/vnd.myapi.v2+json`) is cleaner but creates invisible complexity in caches, load balancers, and debugging sessions. Custom header versioning (`X-API-Version: 2`) is the worst of both worlds.

URI versioning wins in practice because the version is observable everywhere without inspecting headers. Route it at the infrastructure layer and your application code doesn't need to know:

<script src="https://gist.github.com/mohashari/245f19565cfa0664a3c9e5ef0e57a2a4.js?file=snippet-1.txt"></script>

Maintain at minimum N-1 versions under active support. Deprecate with `Sunset` and `Deprecation` headers per RFC 8594 — this gives clients machine-readable signals without relying on them reading your changelog:

<script src="https://gist.github.com/mohashari/245f19565cfa0664a3c9e5ef0e57a2a4.js?file=snippet-2.txt"></script>

Clients that don't handle these headers will eventually break on the sunset date — that's fine. The ones who are paying attention will migrate. Track header delivery in your observability stack. If 80% of your v1 traffic comes from three internal services, coordinate directly rather than waiting for the sunset.

## Idempotency Is a Contract, Not a Best Effort

Idempotency failures cause duplicate charges, double-shipped orders, and ghost records that are painful to reconcile. `GET`, `HEAD`, `OPTIONS`, `PUT`, and `DELETE` are idempotent by definition. `POST` is not — but it should be made so for any state-changing operation that a client might retry.

The pattern: require clients to send an idempotency key (`Idempotency-Key` header), store the key with its response, and return the cached response for duplicates. Set a retention window (Stripe uses 24 hours) and document it:

<script src="https://gist.github.com/mohashari/245f19565cfa0664a3c9e5ef0e57a2a4.js?file=snippet-3.go"></script>

Two important constraints: reject requests where the same idempotency key is used with different request bodies (return 422), and use distributed locking when processing the first request to handle concurrent duplicates hitting the same key simultaneously. A Redis `SET NX` with a short TTL works here — hold the lock for the duration of the request, then replace it with the cached response.

`DELETE` operations warrant special treatment. `DELETE /users/123` is idempotent — a second call should return 204 or 404, but never 500. If your delete is soft (tombstoning), return 204 regardless. If it's hard delete and the resource is gone, 404 is acceptable but document it explicitly. Clients should not have to handle both.

## Error Contracts Are Part of Your API Surface

Your error responses are as much a public contract as your success responses. Clients will write code against your error shapes. If you return inconsistent structures — sometimes `{"error": "..."}`, sometimes `{"message": "..."}`, sometimes an HTML 500 page from your load balancer — every consumer writes defensive parsing code, and that code will fail in interesting ways.

RFC 9457 (Problem Details for HTTP APIs) gives you a standard error envelope. Adopt it:

<script src="https://gist.github.com/mohashari/245f19565cfa0664a3c9e5ef0e57a2a4.js?file=snippet-4.json"></script>

The `type` URI should be a real URL that resolves to documentation. The `instance` field is a URI uniquely identifying this occurrence — include a timestamp or request ID. Always include a `trace_id` that clients can provide in support requests. Make this a hard requirement: no error response ships without a trace ID.

Map your error space deliberately. Use 400 for syntactically invalid requests, 422 for semantically invalid ones (valid JSON but business rule violations), 409 for conflicts, 429 for rate limiting with `Retry-After`, and 503 for intentional unavailability with `Retry-After`. Do not use 500 when you mean 503. Do not use 400 when you mean 422. Clients write retry logic against these codes — precision here prevents bugs downstream.

## Pagination and Filtering at Scale

Offset-based pagination (`?page=3&per_page=20`) breaks under concurrent writes. By the time a client requests page 3, inserts and deletes have shifted the result set. Items are skipped or duplicated. At low data volumes this is invisible. At scale it surfaces as "why are some orders never showing up in the export?"

Cursor-based pagination solves this. The cursor is an opaque token encoding the last-seen position (typically a timestamp + ID pair for stable ordering). Clients don't know what's inside it — they just pass it back:

```python
# snippet-5
# Cursor pagination using timestamp+id encoding — stable under concurrent writes
import base64
import json
from datetime import datetime

def encode_cursor(created_at: datetime, id: str) -> str:
    payload = {"ts": created_at.isoformat(), "id": id}
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()

def decode_cursor(cursor: str) -> tuple[datetime, str]:
    payload = json.loads(base64.urlsafe_b64decode(cursor.encode()))
    return datetime.fromisoformat(payload["ts"]), payload["id"]

def paginate_orders(cursor: str | None, limit: int = 20) -> dict:
    query = Order.objects.filter(deleted_at__isnull=True).order_by("-created_at", "-id")
    
    if cursor:
        ts, last_id = decode_cursor(cursor)
        # Keyset pagination: fetch items older than cursor position
        query = query.filter(
            models.Q(created_at__lt=ts) |
            models.Q(created_at=ts, id__lt=last_id)
        )
    
    items = list(query[:limit + 1])  # fetch one extra to detect next page
    has_next = len(items) > limit
    items = items[:limit]
    
    next_cursor = None
    if has_next and items:
        last = items[-1]
        next_cursor = encode_cursor(last.created_at, str(last.id))
    
    return {
        "data": [serialize_order(o) for o in items],
        "pagination": {
            "next_cursor": next_cursor,
            "has_next": has_next,
            "limit": limit
        }
    }
```

Filtering and sorting get their own design decisions. Expose filter parameters explicitly rather than accepting arbitrary query strings. Document exactly which fields are filterable and sortable — this lets you add indexes for exactly those fields rather than trying to make everything fast. Compound filters (`?status=active&created_after=2026-01-01`) should be ANDed. OR logic belongs in a POST body search endpoint, not query strings.

## Backward Compatibility Under Live Traffic

The rule: never remove a field, never change a field's type, never remove an enum value from a response. You can add fields, add enum values, and add new optional request parameters. Adding is backward compatible. Removing is not.

When you need to rename a field, run both names in parallel during a transition period. Return both `user_name` and the new `username` in responses. Accept both in requests. Add the `Deprecation` header for the old field. Remove the old one after your sunset date:

<script src="https://gist.github.com/mohashari/245f19565cfa0664a3c9e5ef0e57a2a4.js?file=snippet-6.go"></script>

Enum values require extra care. If you add a new status to an order (`PENDING_REVIEW`), clients that use exhaustive switch statements will break if they weren't written defensively. Document in your API contract that consumers must handle unknown enum values gracefully. Return unknown values to callers rather than mapping them to a default — the client deserves to know what state the server actually observed.

Database-level migrations under live traffic follow the same expand-contract pattern: add the new column, populate it, start writing to both, stop reading the old one, stop writing the old one, drop the old one. Each step is a separate deploy with a validation period. Never migrate a field in a single deployment.

## Deprecation as a Process, Not an Event

Deprecation that actually works requires tracking who's still using the old thing. Log every request that hits deprecated endpoints or uses deprecated fields. Aggregate by client identifier (`X-Client-ID`, API key, or OAuth client). Build a dashboard that shows you which clients are still using v1 endpoints sorted by request volume. This turns "we need to deprecate v1" from a guess into a migration checklist.

Set a sunset date that gives your slowest client enough time to migrate — 90 days minimum for external consumers, 30 days is reasonable for internal services if you coordinate directly. Communicate through multiple channels: `Sunset` headers, changelog entries, direct outreach to high-traffic clients. At T-30 days, drop the sunset date into your API gateway's access logs as a structured field so your ops team can see it in dashboards without parsing headers.

On sunset day, return 410 Gone with a body pointing to the migration guide. Not 404 — that's ambiguous. Not 301 — clients will follow the redirect and hit the new API with v1 payloads. 410 is unambiguous: this resource is gone intentionally, here's where to go instead.

The APIs that survive long-term aren't the ones that never change — they're the ones that change predictably, communicate breaking points clearly, and give clients enough lead time to adapt. That's not a technical problem. It's a discipline problem. Build the discipline into your process before the first external consumer signs up, and it stays cheap. Try to retrofit it after you have 200 clients depending on undocumented behavior, and you're booking that 2am incident.
```