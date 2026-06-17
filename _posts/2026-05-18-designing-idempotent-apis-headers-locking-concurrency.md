---
layout: post
title: "Designing an Idempotent API: Header Specs, Distributed Locking, and Race Condition Mitigation"
date: 2026-05-18 09:00:00 +0700
tags: [api-design, system-design, backend, reliability, concurrency]
description: "A comprehensive guide to building bulletproof idempotent APIs: covering draft IETF header specs, Redis distributed locks, and handling concurrent in-flight requests."
image: "https://picsum.photos/seed/429/1080/720"
thumbnail: "https://picsum.photos/seed/429/400/300"
---

In network programming, failures are an inevitability. Sockets timeout, connections blip, and routers restart. When a client sends a request to make a payment or book a flight and receives a network timeout, it faces a dilemma: **Did the server process the request before the timeout, or did it fail entirely?**

If the client blindly retries a non-idempotent operation (like charging a credit card), it risks double-billing the user.

An **Idempotent API** ensures that a client can make the same call repeatedly, and the server will produce the same side-effect and return the same cached response as the initial successful execution.

In this guide, we'll walk through the architectural blueprint of a robust idempotency engine: covering the **IETF draft header specifications**, **distributed locking with Redis**, and **handling concurrent in-flight race conditions**.

---

## 1. The IETF Idempotency-Key Header Specification

To design a clean developer interface, your API should follow the draft **IETF HTTP Idempotency-Key** specification.

Clients pass a unique, client-generated string inside the request header:

```http
POST /v1/payments HTTP/1.1
Host: api.stripe.com
Idempotency-Key: 7b8b209d-cb9e-4e44-bbbb-f24b553e1a0b
Content-Type: application/json

{
  "amount": 1000,
  "currency": "usd"
}
```

The server uses this key to uniquely identify the operation. If a second request arrives with the same `Idempotency-Key`, the server intercepts it and returns the cached result without executing the business logic again.

---

## 2. The Idempotency Workflow & State Machine

A resilient idempotency engine must support three distinct states for any given key:

```
                  ┌──────────────────────┐
                  │   Incoming Request   │
                  └──────────┬───────────┘
                             │ (Checks Redis for key)
           ┌─────────────────┼─────────────────┐
           ▼ (Key Not Found) ▼ (State: Started)▼ (State: Resolved)
      ┌──────────┐     ┌───────────┐     ┌───────────┐
      │  START   │     │ CONCURRENT│     │ RETURNED  │
      │  PROCESS │     │  CONFLICT │     │  CACHED   │
      └──────────┘     └───────────┘     └───────────┘
```

1.  **STARTED (In-Flight):** The server has received the request and is currently processing it.
2.  **RESOLVED (Completed):** The server has completed the operation and saved both the response code and payload.
3.  **FAILED:** The operation crashed before making any changes. The client is free to retry.

---

## 3. Step-by-Step Implementation with Redis

Let's design the locking and storage mechanism using **Redis** for fast, atomic state tracking. We will use a `Lua` script to guarantee atomic "Check-and-Set" operations.

### Step 1: Request Arrival & Distributed Locking

When a request arrives, the server must atomically check if the key exists, and if not, acquire a lock and set the status to `"started"`. This protects against **concurrent retries** (where a client sends the same key twice in rapid succession due to poor client-side concurrency control).

We can do this atomically using the following Redis command:

```bash
# Set key with value if it does not exist (NX), with a 2-hour TTL (EX)
SET idempotency:7b8b209d "{\"status\":\"started\",\"req_hash\":\"a2b3c4\"}" NX EX 7200
```

*   **NX:** Guarantees that the key is only created if it doesn't already exist.
*   **Request Hashing:** We store a hash of the request body (`req_hash`). If a client sends the same `Idempotency-Key` but changes the request body, the server must reject it with a `400 Bad Request` conflict. This prevents "Key Hijacking" (reusing a key to execute a completely different action).

#### Case A: Key Already Exists with Status `"started"`
If the key exists and the status is `"started"`, another server thread is currently executing the request.
*   **Action:** The server must return a `409 Conflict` (or wait and poll the key using a backoff loop) because the operation is still in-flight.

#### Case B: Key Exists with Status `"resolved"`
If the key exists and the status is `"resolved"`, the request has completed.
*   **Action:** The server parses the cached response payload and returns it instantly to the client, along with a custom header to indicate the cached hit:

```http
HTTP/1.1 200 OK
X-Cache-Lookup: HIT - Idempotency
Content-Type: application/json

{
  "payment_id": "pay_992",
  "status": "succeeded"
}
```

---

### Step 2: Executing the Transaction

If the key was successfully locked (`status = started`), we run our actual business logic inside a database transaction.

```sql
BEGIN TRANSACTION;

-- Perform business logic (e.g. deduct account balance)
UPDATE balances SET amount = amount - 1000 WHERE user_id = 'user_45';

-- Insert domain record
INSERT INTO payments (id, amount, status) VALUES ('pay_992', 1000, 'succeeded');

COMMIT;
```

---

### Step 3: Saving the Resolution

Once the database transaction is successfully committed, we update the Redis state from `"started"` to `"resolved"`, caching the API response:

```bash
# Overwrite key with the resolved status and the API response payload
SET idempotency:7b8b209d "{\"status\":\"resolved\",\"req_hash\":\"a2b3c4\",\"response_code\":200,\"response_body\":\"{\\\"payment_id\\\":\\\"pay_992\\\",\\\"status\\\":\\\"succeeded\\\"}\"}" EX 86400
```

We set a longer TTL (e.g., 24 hours) for resolved keys to ensure the client can safely retrieve the cached response even if they retry much later.

---

### Step 4: Handling Server Failures (The Crash Recovery)

What happens if our server crashes *after* setting the state to `"started"` in Redis but *before* committing the database transaction?
*   The Redis key will remain stuck in the `"started"` state, blocking all future retries.
*   **Solution:** In our initial `SET` command, we use a short TTL for the `"started"` state (e.g., 60 seconds). If the server crashes, the lock will automatically expire in 60 seconds. The client can then retry, and the new server instance will treat it as a fresh request.

---

## 4. Mitigating Edge-Case Race Conditions

Even with the above flow, high-throughput systems face complex race conditions.

### The Double-Submit (Atomic Ingestion)
Suppose a user double-taps a "Pay" button, sending two identical requests to your gateway milliseconds apart.
*   Request A hits Pod 1.
*   Request B hits Pod 2.
*   Both check Redis simultaneously.

Because Redis executes commands in a **single-threaded event loop**, the `SET ... NX` command is guaranteed to be fully atomic.
*   Pod 1's request succeeds in setting the key and gets the lock.
*   Pod 2's request immediately fails to set the key because it already exists. Pod 2 halts execution and returns a conflict or blocks.

By placing the idempotency check at the very entrance of your API gateway (before running any business logic or hitting downstream services), you shield your system from concurrency-related double-processing bugs.

---

## Summary of the Idempotent Protocol

| Scenario | Redis Key State | Server Action | HTTP Response |
| :--- | :--- | :--- | :--- |
| **First Request** | Not Found | Acquire Lock (`started`), Run Business Logic, Save `resolved` | `200 OK` (Fresh Execution) |
| **Concurrent Retry** | `started` | Reject (Operation In-Flight) | `409 Conflict` |
| **Successful Retry** | `resolved` | Skip Execution, Return Cached Payload | `200 OK` + `X-Cache-Lookup: HIT` |
| **Payload Tampering**| `started` / `resolved` | Reject (Hash mismatch) | `400 Bad Request` |
| **Server Crash** | Expired (TTL) | Treat as First Request | `200 OK` (Recovers and runs) |

## Conclusion

Idempotency is not an optional add-on or a minor feature; it is a **core architectural requirement** for any robust distributed API. Leaving idempotency to client-side logic is a recipe for data duplication and billing bugs.

By leveraging a standard **Idempotency-Key** header, enforcing atomic state transitions with **Redis locking**, and using request hashing to prevent hijacking, you provide your clients with absolute safety, enabling them to retry failed connections with zero fear of side-effects.
