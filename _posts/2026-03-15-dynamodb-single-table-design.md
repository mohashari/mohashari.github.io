---
layout: post
title: "DynamoDB Design Patterns: Single-Table Design and Access Pattern Modeling"
date: 2026-03-15 07:00:00 +0700
tags: [dynamodb, nosql, aws, databases, architecture]
description: "Master single-table design in DynamoDB to model complex access patterns efficiently without sacrificing performance or cost."
---

Most engineers approaching DynamoDB for the first time make the same mistake: they model their data the way they would in PostgreSQL, creating one table per entity — a `users` table, an `orders` table, a `products` table — then watch their costs balloon and their latency spike as they scatter related data across separate tables and round-trips. DynamoDB is not a relational database with worse SQL. It is a fundamentally different tool, one that rewards you handsomely when you design around access patterns first and penalizes you severely when you design around entity normalization. Single-table design is the practice of collapsing your entire data model into one DynamoDB table, using carefully crafted partition keys and sort keys to serve every access pattern your application needs — with a single query, every time.

## Why Single-Table Design Exists

In a relational database, joins are cheap (relatively) because the engine co-locates data on the same server and can execute them in memory. In DynamoDB, there are no joins. Each additional read is a network round-trip to a distributed key-value store priced per request. If you need to load a user and their five most recent orders in a relational system, one query does it. In a naively-modeled DynamoDB setup with separate tables, that's six reads. Single-table design eliminates this by denormalizing related items into the same partition, making one `Query` call sufficient for even complex hierarchical data.

The core mechanism is the composite primary key: a **partition key** (PK) that routes the request to the correct shard, and a **sort key** (SK) that allows range queries within that partition. By using structured, prefixed values for both keys, you can store heterogeneous item types in the same partition and retrieve subsets of them with sort key conditions.

## Designing the Key Schema

Before writing a single line of code, you must enumerate your access patterns. This is not optional — it is the entire design exercise. A typical e-commerce service might need: fetch a user by ID, list all orders for a user, fetch an order with its line items, look up a product by SKU, and find all orders in a given status.

From these patterns, you derive a key schema. A common convention uses generic attribute names (`PK`, `SK`) to avoid coupling the schema to entity semantics, with structured prefixes (`USER#`, `ORDER#`) to namespace item types:

<script src="https://gist.github.com/mohashari/df71fc53c7409cf84d9ee55747891609.js?file=snippet.txt"></script>

This layout lets you fetch a user's profile and paginate their orders with a single `Query` on `PK = USER#u-123` and `SK begins_with ORDER#`.

## Implementing Item Types in Go

In practice, you represent each entity type as a Go struct and use a shared marshaling layer to map fields to DynamoDB attribute names. The `aws-sdk-go-v2` library with `attributevalue` makes this ergonomic.

<script src="https://gist.github.com/mohashari/df71fc53c7409cf84d9ee55747891609.js?file=snippet-2.go"></script>

The `UserProfileKeys` function encapsulates the key construction logic. Every access pattern gets a corresponding key-builder function — this is the single-table design equivalent of a query builder.

## Querying a User's Orders with a Date Range

Because order sort keys include an ISO-8601 timestamp, you can apply sort key conditions to filter by date range without a scan. DynamoDB's `Query` with `KeyConditionExpression` is the workhorse of single-table design.

<script src="https://gist.github.com/mohashari/df71fc53c7409cf84d9ee55747891609.js?file=snippet-3.go"></script>

The `\xFF` suffix trick is a common pattern: appended to a date prefix, it sorts lexicographically after any valid timestamp for that day, giving you an inclusive upper bound without knowing the exact SK values in advance.

## Global Secondary Indexes for Orthogonal Access Patterns

Not every access pattern maps naturally to your base table's partition key. Looking up all orders in `PENDING` status is one such pattern — orders are distributed across thousands of user partitions. This is where a **Global Secondary Index (GSI)** comes in. You project a `status` attribute and an `updated_at` timestamp, then define a GSI with `status` as its partition key and `updated_at` as its sort key.

<script src="https://gist.github.com/mohashari/df71fc53c7409cf84d9ee55747891609.js?file=snippet-4.go"></script>

The GSI attributes (`GSI1PK`, `GSI1SK`) are written alongside the base item. When DynamoDB replicates the item into the GSI, you can query `GSI1PK = STATUS#PENDING ORDER BY GSI1SK DESC` to get the most recently updated pending orders globally — a pattern impossible to implement efficiently on the base table alone.

## Table Definition via AWS CloudFormation

Infrastructure-as-code is non-negotiable for DynamoDB in production. The table schema, billing mode, GSI projections, and TTL configuration should all live in version control.

<script src="https://gist.github.com/mohashari/df71fc53c7409cf84d9ee55747891609.js?file=snippet-5.yaml"></script>

Notice that only attributes used in key schemas are declared in `AttributeDefinitions` — DynamoDB is schemaless for all other attributes. The `TTL` configuration allows you to set an epoch timestamp on any item and have DynamoDB automatically delete it, which is invaluable for session tokens, caches, or temporary workflow state.

## Transactional Writes for Multi-Item Consistency

Single-table design often means writing multiple items atomically — for example, creating an order record, updating the user's order count, and decrementing product inventory in a single operation. DynamoDB's `TransactWriteItems` provides all-or-nothing semantics across up to 100 items in the same or different tables.

<script src="https://gist.github.com/mohashari/df71fc53c7409cf84d9ee55747891609.js?file=snippet-6.go"></script>

The `ConditionExpression: attribute_not_exists(PK)` on the `Put` is a critical idempotency guard — it causes the entire transaction to fail if the order already exists, preventing duplicate creation on retries. This pattern, combined with client-generated UUIDs, gives you safe at-least-once retry semantics.

## Local Testing with DynamoDB Local

Never develop against a live DynamoDB endpoint if you can avoid it. AWS provides an official Docker image of DynamoDB Local that faithfully emulates the API, including GSIs and transactions, with zero cost and no network dependency.

<script src="https://gist.github.com/mohashari/df71fc53c7409cf84d9ee55747891609.js?file=snippet-7.sh"></script>

Point your application at `http://localhost:8000` with any dummy credentials (`AWS_ACCESS_KEY_ID=local AWS_SECRET_ACCESS_KEY=local`) and your integration tests run entirely offline.

Single-table design in DynamoDB is fundamentally about shifting the complexity from query time to design time. The discipline of enumerating access patterns before writing a schema, choosing key structures that serve those patterns, and using GSIs for orthogonal lookups will feel unfamiliar at first — but it produces a data layer that scales to millions of requests per second with single-digit millisecond latency, at costs that remain predictable under load. The investment in upfront modeling pays compounding dividends: fewer round-trips, lower costs, and a codebase where data access logic is explicit, testable, and fast.