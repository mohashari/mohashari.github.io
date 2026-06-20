---
layout: post
title: "Designing Idempotent APIs: Handling Double-Submits and Distributed Locks in Payment Gateways"
date: 2026-06-20 08:00:00 +0700
tags: [software-engineering, backend, system-design, redis, payments]
description: "Learn how to build bulletproof payment APIs by implementing distributed locks and transactional idempotency patterns to prevent double charges."
image: "https://picsum.photos/seed/4009/1080/720"
thumbnail: "https://picsum.photos/seed/4009/400/300"
---
At 09:14:02 UTC, a user attempts to purchase a subscription. The request traverses an unstable mobile network, lagging for 15 seconds until the client-side HTTP library times out and throws an error. The user, seeing an error spinner, aggressively clicks the "Pay Now" button a second time. Simultaneously, the mobile SDK’s automatic retry logic fires a duplicate HTTP POST request. These two requests arrive at your API gateway exactly 42 milliseconds apart. Without a strict idempotency and concurrency control layer, both requests bypass local validation, query the database, find that no invoice has been paid, and concurrently invoke the Stripe API. Stripe charges the user’s credit card twice for $149.99, while your database records only a single successful transaction. The second payment is orphaned, leading to a frantic support ticket, chargeback penalties, and a direct hit to your business's bottom line.

![Designing Idempotent APIs: Handling Double-Submits and Distributed Locks in Payment Gateways Diagram](/images/diagrams/designing-idempotent-apis-double-submits-distributed-locks.svg)

## The Anatomy of a Double-Submit: Why HTTP Retries Break Systems

In distributed networks, the network itself is the most unreliable component. Packets are dropped, routing tables shift, and TCP connections fail. In a payment system, the lack of idempotency turns these network anomalies into financial liabilities. We classify duplicates into two categories:

1. **Concurrent Double-Submits (The Race Condition):** Multiple requests with the identical payload arrive almost simultaneously. This happens when clients lack UI-level button disabling, or when aggressive client-side retries fire multiple threads concurrently before the first response has finished processing.
2. **Sequential Retries (The Network Drop):** A request is successfully processed by the payment service and the downstream Payment Service Provider (PSP), but the TCP connection drops on the return path. The client receives a connection timeout (e.g., 504 Gateway Timeout) and retries the request seconds or minutes later.

To make an API safe, we must ensure that no matter how many times a payment request is received, the side effects (charging the user, updating the database ledger, sending email receipts) occur exactly once. 

Many engineers assume they can rely on database transactions (e.g., standard relational databases running at `READ COMMITTED` isolation) to solve this. They are wrong. If two identical payment requests enter the application layer concurrently, they will execute parallel select queries, check if the payment exists, find nothing, and execute parallel inserts. Even at higher isolation levels like `REPEATABLE READ` or `SERIALIZABLE`, one transaction will succeed while the other will fail with a serialization anomaly. This aborts the database transaction but does not prevent the application from having already invoked the external PSP API. Thus, we require a multi-tier design: a fast distributed locking tier to handle concurrency, and a persistent database transactional tier to store idempotency state.

## Designing Idempotency Keys: Scope, Hashing, and Payload Validation

The foundation of idempotency is the idempotency key. A common industry mistake is allowing the client to send a raw UUIDv4 as the key and accepting it blindly. Consider a malicious or buggy client that generates a UUID, attaches it as `Idempotency-Key: f47ac10b-58cc-4372-a567-0e02b2c3d479`, and then sends a request to pay $10.00. If the client retries but changes the payload to $100.00 while using the same key, your backend could retrieve the cached success response for the $10.00 payment and mistakenly mark the $100.00 payment as completed.

To prevent this collision, you must implement **Payload Fingerprinting**. The application server must calculate a cryptographic hash of the request body, method, and target URI, and associate it with the idempotency key. If the incoming key matches a recorded transaction but the fingerprint does not, the system must immediately reject the request with a `400 Bad Request` code.

Snippet 1 illustrates a production-ready Go middleware that extracts the body, computes a SHA-256 fingerprint, and restores the request body reader so downstream controllers can parse it.

<script src="https://gist.github.com/mohashari/0ca46665190b6647899caffa3aec6636.js?file=snippet-1.go"></script>

## Distributed Locking vs. Idempotency State: Two Different Problems

Architecting a resilient API requires separation of concerns between distributed locking and idempotency state tracking:

* **Distributed Lock:** A short-lived, low-latency construct. Its purpose is to guarantee mutual exclusion. When a payment starts, a lock is acquired. It prevents a second thread from executing the business logic simultaneously. If a lock cannot be acquired, the system returns an immediate status code (`429 Too Many Requests` or `409 Conflict`).
* **Idempotency Store:** A long-lived, persistent ledger. It records the final outcome of the operation (e.g., Stripe charge ID, HTTP status code, and response JSON). If a request comes in 10 hours later with the same key, the idempotency store serves the cached response without touching external payment processors.

Without the distributed lock, concurrent requests will enter the critical path at the same time. The database checks for both will execute before either has inserted a record, bypassing database constraints and triggering duplicate external calls.

To implement the distributed lock, we use Redis. While the Redlock algorithm is suitable for multi-master Redis setups, a single-master Redis cluster with sentinel failover is often sufficient when combined with a secure `SET key token NX PX duration` operation. Snippet 2 implements a robust Redis lock wrapper using `go-redis/v9` that leverages a Lua script to guarantee atomic, safe release.

<script src="https://gist.github.com/mohashari/0ca46665190b6647899caffa3aec6636.js?file=snippet-2.go"></script>

## Designing the Storage Layer for Idempotency

While Redis is ideal for fast locks and short-term caching, it is an in-memory database. If your Redis cluster runs out of memory, it may evict keys based on LRU policies. If a payment record is evicted, a retry request will treat it as a new transaction and double-charge the client. Therefore, the source of truth for idempotency must live in a persistent relational database like PostgreSQL.

We model this by introducing an `idempotency_records` table. This table must support a state machine of three states: `PENDING` (request is currently executing), `SUCCESS` (request finished, response cached), and `FAILED` (request failed, safe to retry).

Snippet 3 shows the PostgreSQL DDL defining this schema, complete with foreign key references and partial indexing to allow periodic partition pruning or purging of stale, completed keys.

<script src="https://gist.github.com/mohashari/0ca46665190b6647899caffa3aec6636.js?file=snippet-3.sql"></script>

When a request arrives, the service must perform an atomic check-and-insert. In PostgreSQL, we can use a transaction or a locking `SELECT ... FOR UPDATE` query to verify if the idempotency record exists. If it does not, we insert it with a status of `PENDING`.

Snippet 4 details the PostgreSQL data-access layer using `pgx` in Go, showing how to read the state or register the `PENDING` insert atomically.

<script src="https://gist.github.com/mohashari/0ca46665190b6647899caffa3aec6636.js?file=snippet-4.go"></script>

## The Payment Processor Integration Flow

Once the locking and state structures are defined, we assemble the payments orchestrator. The sequence of actions is critical:

1. **Acquire Distributed Lock:** Stop duplicate requests instantly at the edge.
2. **Execute Database State Check:** Run `GetOrCreateIdempotency` inside a PostgreSQL transaction.
   * If state is `SUCCESS`, return cached response.
   * If state is `PENDING`, return an error or block/wait.
3. **Invoke External PSP:** Make the outbound network call to Stripe/Adyen. Always forward the idempotency key as Stripe's `Idempotency-Key` HTTP header. This protects you in case of internal failovers where your server makes duplicate downstream requests.
4. **Persist Transaction & Update Idempotency State:** Inside a local database transaction, create the payment ledger entry and transition the idempotency state from `PENDING` to `SUCCESS` or `FAILED`.
5. **Release Distributed Lock:** Free the lock for subsequent client queries.

Snippet 5 shows the complete orchestration handler.

<script src="https://gist.github.com/mohashari/0ca46665190b6647899caffa3aec6636.js?file=snippet-5.go"></script>

## Advanced Edge Cases: Slow Downstreams and Lock Extensions

In real systems, things fail dynamically. Consider this failure mode: 

You acquire a Redis lock with a Time-To-Live (TTL) of 15 seconds. You execute the database registration and call Stripe. However, Stripe is suffering from degraded latency, and their API takes 22 seconds to respond. At T=15s, the Redis lock expires. At T=16s, the user retries. This retry request bypasses the expired lock, reads the database where the idempotency record is still marked as `PENDING`, and calls Stripe a second time with the same key. Stripe’s own idempotency layer might catch this, but if your own retry was mapped to a new transaction on the gateway side, you risk creating duplicate operations.

To prevent this, you can choose between two designs:
1. **Aggressive Request Timeout Tuning:** Enforce a hard HTTP client timeout on downstream calls (e.g., 10 seconds) that is strictly less than the lock TTL (e.g., 15 seconds).
2. **Lock Auto-Renewal (Heartbeat Pattern):** Acquire the lock with a safe default TTL, and spin up a background worker goroutine that periodically extends the TTL of the lock (e.g., every 3 seconds) until the orchestrator process completes and stops the worker.

Snippet 6 demonstrates a lock renewal system in Go.

<script src="https://gist.github.com/mohashari/0ca46665190b6647899caffa3aec6636.js?file=snippet-6.go"></script>

## Offline Reconciliation: The Ultimate Safety Net

No software architecture is infallible. Under heavy load, your application pod might be killed by a Kubernetes Out-Of-Memory (OOM) event right after it called Stripe but before it saved the success status to PostgreSQL. In this situation, the database record remains stuck in `PENDING` indefinitely, and the client receives a connection failure.

To resolve these cases, you must implement an asynchronous reconciliation worker. The worker runs on a cron schedule, queries all payments stuck in the `PENDING` state for longer than 15 minutes, calls Stripe’s search endpoint to verify if a charge associated with the idempotency key exists, and updates the database state accordingly.

Snippet 7 is a Go worker implementation of this pattern.

<script src="https://gist.github.com/mohashari/0ca46665190b6647899caffa3aec6636.js?file=snippet-7.go"></script>