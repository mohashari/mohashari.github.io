---
layout: post
title: "Multi-Tenancy Architecture: Designing SaaS Backends That Scale"
date: 2026-03-15 07:00:00 +0700
tags: [multi-tenancy, saas, architecture, database, backend]
description: "Choose between silo, pool, and bridge multi-tenancy models and implement tenant isolation, data partitioning, and per-tenant scaling."
---

Every SaaS backend eventually faces the same reckoning: you built your system for one customer, and now you have a hundred. Some want their data isolated in a dedicated database. Others don't care as long as the price is right. A few enterprise deals will come with compliance requirements that make shared infrastructure a dealbreaker. Multi-tenancy is not a single architectural decision — it's a spectrum of trade-offs between cost, isolation, complexity, and scalability that you'll negotiate tenant by tenant as your business grows. Getting this wrong early means painful migrations later; getting it right means a platform that can accommodate every customer tier without a rewrite.

## The Three Models: Silo, Pool, and Bridge

The foundation of any multi-tenancy discussion starts with three canonical models. **Silo** gives each tenant their own database instance — maximum isolation, maximum cost. **Pool** puts all tenants in a shared schema with a `tenant_id` column everywhere — cheapest to operate, hardest to guarantee isolation. **Bridge** (often called schema-per-tenant in Postgres) sits in the middle: one database server, but each tenant gets a dedicated schema with its own tables.

For most SaaS products, the bridge model scales well from seed to Series B. Here's how you'd structure tenant routing in Go using the bridge model:

<script src="https://gist.github.com/mohashari/fa286dc528f73cfddde65853c4d31844.js?file=snippet.go"></script>

Notice that `SET search_path` is session-scoped in Postgres. For a connection pool, you need to set it on every acquired connection before use — a subtle but critical detail.

## Tenant Resolution Middleware

Tenants reach your backend through subdomains (`acme.app.io`), JWT claims, or API key lookups. HTTP middleware is the right place to resolve tenant identity before any business logic runs.

<script src="https://gist.github.com/mohashari/fa286dc528f73cfddde65853c4d31844.js?file=snippet-2.go"></script>

This keeps tenant resolution decoupled from your handlers. Every downstream function simply calls `tenant.FromContext(ctx)` — no HTTP headers or global state leaking through your call stack.

## Provisioning Tenant Schemas

When a new customer signs up, you need to create their schema and run migrations atomically. Using Go's `database/sql` with raw DDL, combined with a migration library like `golang-migrate`, gives you reproducible provisioning:

<script src="https://gist.github.com/mohashari/fa286dc528f73cfddde65853c4d31844.js?file=snippet-3.go"></script>

Wrapping schema creation and migrations in a single transaction means you never have a half-provisioned tenant. If the migration fails, the schema is rolled back entirely.

## Row-Level Security as a Safety Net

Even with schema isolation, defense in depth matters. In the pool model — or any model where your application might accidentally omit a `tenant_id` filter — Postgres Row-Level Security (RLS) is your last line of defense:

<script src="https://gist.github.com/mohashari/fa286dc528f73cfddde65853c4d31844.js?file=snippet-4.sql"></script>

The `FORCE ROW LEVEL SECURITY` directive ensures even the table owner is subject to policies — critical for preventing accidental data leaks during administrative operations.

## Per-Tenant Rate Limiting with Redis

Isolation isn't only about data — it's about compute resources too. A noisy-neighbor tenant hammering your API shouldn't degrade service for others. A sliding-window rate limiter keyed by `tenant_id` in Redis handles this cleanly:

<script src="https://gist.github.com/mohashari/fa286dc528f73cfddde65853c4d31844.js?file=snippet-5.go"></script>

Each tenant gets an independent sorted set. The pipeline executes atomically, giving you accurate counts without race conditions under high concurrency.

## Kubernetes Namespace-per-Tenant for Enterprise Silo

For high-value enterprise tenants who demand full infrastructure isolation, you can automate silo provisioning with a Kubernetes namespace and a dedicated deployment:

<script src="https://gist.github.com/mohashari/fa286dc528f73cfddde65853c4d31844.js?file=snippet-6.yaml"></script>

The `NetworkPolicy` enforces that pods in `tenant-acme` cannot communicate with pods in any other tenant namespace — a hard network boundary that complements your application-layer isolation.

## Tenant-Aware Database Migrations

Running migrations across hundreds of tenant schemas requires careful orchestration. A naive sequential loop will timeout on large fleets; a parallel runner with controlled concurrency is the production-grade approach:

<script src="https://gist.github.com/mohashari/fa286dc528f73cfddde65853c4d31844.js?file=snippet-7.go"></script>

The semaphore limits database connection pressure during migration runs. In production, you'd extend this with per-tenant migration state tracking so a failed migration can be retried without re-running successful ones.

Multi-tenancy architecture is not a problem you solve once — it's a set of constraints you actively manage as your customer base and pricing tiers evolve. Start with the bridge model (schema-per-tenant) for most workloads: it balances isolation and operational cost without the overhead of per-tenant infrastructure. Layer in RLS as a safety net against application bugs, rate-limit at the tenant level from day one, and design your provisioning pipeline to be idempotent and transactional. When enterprise deals arrive demanding full silo isolation, your architecture will already have the seams in the right places to accommodate them without rebuilding from scratch.