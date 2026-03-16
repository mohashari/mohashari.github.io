---
layout: post
title: "GraphQL for Backend Engineers: Schema Design, DataLoader, and Performance"
date: 2026-03-17 07:00:00 +0700
tags: [graphql, api, performance, backend, n+1]
description: "Master GraphQL schema design, eliminate N+1 query problems with DataLoader, and tune resolver performance for production-grade backends."
---

Every backend engineer eventually faces the same rude awakening: you've built a REST API, your mobile team wants to fetch a user with their orders and each order's line items, and suddenly you're making seventeen round trips or returning a bloated payload that transfers half a megabyte for what should be a simple screen. GraphQL promises to fix this. But adopting it naively—slapping resolvers on top of an ORM and calling it a day—trades your REST performance problems for a new set of GraphQL-shaped ones. This post is about doing it right: designing schemas that scale, eliminating the N+1 problem with DataLoader, and tuning resolvers for production workloads.

## Schema Design Starts with the Domain, Not the Database

The most common mistake is treating GraphQL as a thin JSON wrapper over your database tables. Your schema should model your domain, not your persistence layer. Think in terms of entities and their relationships from the consumer's perspective.

Here's a schema for an e-commerce domain that's designed for real query patterns—not a 1:1 mapping of SQL tables:

<script src="https://gist.github.com/mohashari/abab239b1bef515aee557b16ff51bbdf.js?file=snippet.txt"></script>

Notice the use of Relay-style cursor pagination on `OrderConnection`. Offset pagination (`limit`/`offset`) breaks down at scale—it's inconsistent under concurrent inserts and forces a full table scan to calculate offsets. Cursor-based pagination is stable and composable with indexes.

## The N+1 Problem Is Real and It Will Destroy You

When a client queries ten orders and each order has a `lineItems` resolver that fires a SQL query, you've just sent eleven queries to the database: one for orders, ten for line items. At a hundred orders, that's a hundred and one queries. This is the N+1 problem, and it's the most critical performance issue in any GraphQL implementation.

The standard solution is DataLoader—a batching and caching utility that collects individual load calls within a single tick of the event loop and fires them as a single batched query.

Here's a Go implementation of a DataLoader for products, using the `graph-gophers/dataloader` library:

<script src="https://gist.github.com/mohashari/abab239b1bef515aee557b16ff51bbdf.js?file=snippet-2.go"></script>

The key insight: `Load` is called per `LineItem` resolver, but the actual SQL query only fires once per request tick. Ten line items with ten different products result in exactly one `SELECT ... WHERE id IN (...)` query.

The SQL that backing store fires looks like this—no loops, no per-row queries:

<script src="https://gist.github.com/mohashari/abab239b1bef515aee557b16ff51bbdf.js?file=snippet-3.sql"></script>

The `ANY($1)` syntax with a parameter array is critical for both safety (no SQL injection) and performance (the query plan is cached by the planner for different array lengths).

## DataLoaders Must Be Request-Scoped

A subtle but critical point: DataLoaders cache results within their lifetime. If you use a singleton DataLoader across requests, user A's product data can be served to user B. DataLoaders must be instantiated per-request and injected via context.

<script src="https://gist.github.com/mohashari/abab239b1bef515aee557b16ff51bbdf.js?file=snippet-4.go"></script>

Your `LineItem` resolver then looks like this—one line to trigger a batched load:

<script src="https://gist.github.com/mohashari/abab239b1bef515aee557b16ff51bbdf.js?file=snippet-5.go"></script>

## Complexity Limits Prevent Abuse

GraphQL lets clients ask for whatever they want, which means a malicious or careless client can craft a deeply nested query that joins dozens of tables and returns gigabytes. You need query complexity analysis before this hits production.

<script src="https://gist.github.com/mohashari/abab239b1bef515aee557b16ff51bbdf.js?file=snippet-6.go"></script>

Assign higher complexity costs to fields that fan out into multiple rows. An `orders` field that can return a list should cost more than a scalar `email` field. Most GraphQL libraries allow per-field complexity hints in the schema configuration.

## Persisted Queries Reduce Latency and Attack Surface

In production, mobile clients benefit enormously from persisted queries—the client sends a hash instead of the full query string, saving bandwidth and preventing arbitrary query injection. Here's the pattern using automatic persisted queries (APQ):

<script src="https://gist.github.com/mohashari/abab239b1bef515aee557b16ff51bbdf.js?file=snippet-7.sh"></script>

Once a query is registered, the server validates and executes only the stored query plan—unknown hashes are rejected outright, closing off a significant attack surface.

## Monitoring Resolvers in Production

Instrument each resolver with field-level timing so you can identify slow resolvers without waiting for user complaints. Structured logs that include the field path, duration, and error status make this tractable at scale.

<script src="https://gist.github.com/mohashari/abab239b1bef515aee557b16ff51bbdf.js?file=snippet-8.go"></script>

GraphQL's structural execution model makes this especially powerful—you get per-field observability rather than per-endpoint, so you can precisely identify which nested resolver is responsible for a slow query.

Getting GraphQL right in production is fundamentally about understanding its execution model: every field is resolved independently, which creates batching opportunities (DataLoader) but also abuse vectors (complexity, depth limits). Design your schema around domain queries rather than database tables, batch every list-type relationship behind a DataLoader, scope those loaders to the request lifecycle, and add complexity limits before any client outside your team touches the endpoint. Done this way, GraphQL stops being a source of N+1 nightmares and becomes what it was always meant to be—a precise, efficient contract between your backend and your consumers.