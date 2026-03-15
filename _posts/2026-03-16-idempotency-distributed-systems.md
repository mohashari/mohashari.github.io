---
layout: post
title: "Idempotency in Distributed Systems: APIs That Can Be Safely Retried"
date: 2026-03-16 07:00:00 +0700
tags: [distributed-systems, apis, reliability, backend, patterns]
description: "Design idempotent APIs and background jobs that tolerate duplicate requests without corrupting state or triggering side effects."
---

# Idempotency in Distributed Systems: APIs That Can Be Safely Retried

Your payment service times out. The client doesn't know if the charge went through, so it retries. The charge goes through twice. The customer calls support. Your on-call engineer is now debugging a race condition at 2am. This is the idempotency problem, and it haunts every distributed system that handles money, sends emails, or mutates shared state. Networks are unreliable, processes crash mid-flight, and clients retry — that's not a bug, it's a feature of distributed computing. The only way to make your system behave correctly in the face of retries is to design idempotency in from the start, not bolt it on after your first incident.

## The Core Principle

An operation is idempotent if applying it multiple times produces the same result as applying it once. HTTP `GET` is naturally idempotent. `PUT` with a full resource representation is idempotent. `POST` to create a resource is not — unless you make it so. The key insight is that idempotency is not about the HTTP verb, it's about the semantics you build into your handler.

The standard mechanism is the **idempotency key**: a client-generated unique identifier (typically a UUID v4) sent with every mutating request. The server stores the result of the first execution and returns it for any subsequent request with the same key, skipping all side effects.

Here's a minimal Go struct for storing idempotency records:

<script src="https://gist.github.com/mohashari/46778081c298a96fad45a1d2cdba5574.js?file=snippet.go"></script>

## Storing the Key Before Work Begins

The critical ordering mistake is to do the work first and then save the idempotency record. If your process crashes after the work but before the save, you've lost the deduplication guarantee. Instead, write the record with a `processing` status *before* executing side effects, then update it to `complete` when done.

<script src="https://gist.github.com/mohashari/46778081c298a96fad45a1d2cdba5574.js?file=snippet-2.sql"></script>

If this insert returns zero rows, the key already exists and you fetch the existing record instead. If the existing record is `processing`, the first request is still in-flight — you return `409 Conflict` or wait and poll. If it's `complete`, you return the cached response immediately.

## Locking With Advisory Locks

Between the insert check and the actual work, a race condition can occur if two requests with the same key arrive simultaneously and both see no existing record. Postgres advisory locks are a lightweight solution — they're transaction-scoped and automatically released on commit or rollback.

<script src="https://gist.github.com/mohashari/46778081c298a96fad45a1d2cdba5574.js?file=snippet-3.sql"></script>

Wrap your entire handler in a transaction, acquire this lock at the top, and only one request will proceed. The second will get `false` from `pg_try_advisory_xact_lock` and can immediately return `409 Conflict`.

## A Complete HTTP Handler

Putting it together in Go, the handler follows a strict sequence: check for existing record, acquire lock, re-check (double-checked locking), do work, persist result.

<script src="https://gist.github.com/mohashari/46778081c298a96fad45a1d2cdba5574.js?file=snippet-4.go"></script>

## Idempotent Background Jobs

APIs are only half the problem. Background job processors face the same challenge — a job queue like Redis or SQS delivers messages at-least-once, so your workers must be idempotent too. The pattern mirrors the API approach: use the message ID as the idempotency key and use `INSERT ... ON CONFLICT DO NOTHING` to detect duplicates before processing.

<script src="https://gist.github.com/mohashari/46778081c298a96fad45a1d2cdba5574.js?file=snippet-5.go"></script>

## Natural Idempotency With Upserts

Sometimes you can sidestep the bookkeeping entirely by making the underlying mutation naturally idempotent. Instead of `INSERT` followed by a uniqueness check, an upsert achieves the same result regardless of how many times it runs. This is the right approach for synchronization operations where the desired end state is more important than the creation event.

<script src="https://gist.github.com/mohashari/46778081c298a96fad45a1d2cdba5574.js?file=snippet-6.sql"></script>

The `WHERE` clause on the update is important: it prevents a delayed duplicate from overwriting a newer legitimate update.

## Expiring Old Records

Idempotency records don't need to live forever — 24 hours is typical for payment APIs, 7 days for lower-frequency operations. Rather than a cleanup cron job, a partial index on `expires_at` keeps the table lean and makes the cleanup a simple delete.

<script src="https://gist.github.com/mohashari/46778081c298a96fad45a1d2cdba5574.js?file=snippet-7.sql"></script>

## Making Idempotency a Contract

Document idempotency requirements explicitly in your API contract. If a client doesn't send an `Idempotency-Key`, reject the request immediately with `400 Bad Request` rather than silently processing it. This forces clients to think about retry behavior upfront. For internal services, enforce the header at the gateway level so no handler can accidentally skip it.

The compound guarantee — write-ahead record, advisory lock, upsert semantics, and explicit key validation — means that even if your process is killed mid-request, the next attempt will either complete the work once or replay the cached result. That's the bar every mutating API in a distributed system should meet. Retries stop being dangerous and become a routine part of your reliability story.