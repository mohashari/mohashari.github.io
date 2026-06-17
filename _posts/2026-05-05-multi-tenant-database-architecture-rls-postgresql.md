---
layout: post
title: "Architecting Multi-Tenant DBs: Row-Level Security (RLS) vs. Schema-per-Tenant in PostgreSQL"
date: 2026-05-05 09:00:00 +0700
tags: [architecture, saas, database, postgresql, multi-tenancy]
description: "A deep architectural comparison of multi-tenant database designs in PostgreSQL: analyzing Row-Level Security (RLS) vs. Schema-per-Tenant models."
image: "https://picsum.photos/seed/5174/1080/720"
thumbnail: "https://picsum.photos/seed/5174/400/300"
---

When building a Software-as-a-Service (SaaS) application, one of the most critical foundational decisions you must make is: **How will we isolate tenant data?**

A data leak—where Tenant A can view Tenant B's data—is a catastrophic security failure that can instantly destroy trust. Yet, over-isolating data can make database migrations nightmarish, blow up operational costs, and degrade performance.

In PostgreSQL, multi-tenancy usually boils down to two main competing architectures:
1.  **Shared Database, Shared Schema with Row-Level Security (RLS)**
2.  **Shared Database, Separate Schemas (Schema-per-Tenant)**

In this article, we'll dive deep into the internals of both approaches, analyzing the performance of RLS, schema scaling issues, connection pooling implications (like PgBouncer), and operational management overhead.

---

## 1. Shared Schema with Row-Level Security (RLS)

In the **Shared Schema** model, all tenants share the same database tables. Every table includes a `tenant_id` column to identify ownership.

```
Table: accounts
┌───────────┬──────────────┬─────────┐
│ tenant_id │ account_name │ balance │
├───────────┼──────────────┼─────────┤
│ tenant_A  │ "Checking"   │ 1200.00 │
│ tenant_B  │ "Savings"    │ 5000.00 │
└───────────┴──────────────┴─────────┘
```

To prevent human errors (like developer forgetting to add `WHERE tenant_id = X` to a query), PostgreSQL provides **Row-Level Security (RLS)**.

### How RLS Works Under the Hood

When RLS is enabled on a table, PostgreSQL automatically modifies incoming SQL queries at runtime, appending security filter conditions before executing them.

#### Step 1: Enable RLS and Create Policies
```sql
ALTER TABLE accounts ENABLE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation_policy ON accounts
    FOR ALL
    USING (tenant_id = current_setting('app.current_tenant_id'));
```

#### Step 2: Set the Tenant Context in Transactions
Before running queries on behalf of a tenant, your application sets a session configuration variable within a database transaction block:

```sql
BEGIN;
SET LOCAL app.current_tenant_id = 'tenant_A';

-- The application runs a simple SELECT:
SELECT * FROM accounts;

COMMIT;
```

#### Step 3: Query Rewriting
Behind the scenes, the PostgreSQL query planner intercepts the `SELECT` and rewrites it to:
```sql
SELECT * FROM accounts WHERE tenant_id = 'tenant_A';
```
This query rewriting is handled at the engine level, guaranteeing that even if a developer writes `SELECT * FROM accounts`, they can *never* accidentally expose Tenant B's records.

### Performance & Scaling
*   **Indexes:** You must create composite indexes starting with `tenant_id` (e.g., `INDEX (tenant_id, id)`) on every isolated table to ensure fast lookups.
*   **Query Planner:** RLS has minimal CPU overhead (a few microseconds per query rewrite). However, dynamic GUC (Grand Unified Configuration) variables like `current_setting()` prevent the planner from using static statistics as effectively in complex joins.

---

## 2. Schema-per-Tenant Architecture

In the **Schema-per-Tenant** model, each tenant has its own isolated PostgreSQL namespace (schema) within a single database instance.

```
Database: saas_db
  ├── Schema: tenant_A (contains tables: accounts, users, orders...)
  └── Schema: tenant_B (contains tables: accounts, users, orders...)
```

### Switching Tenant Context
To direct a query to a specific tenant's tables, the application sets the database **Search Path**:

```sql
SET search_path TO tenant_A;
SELECT * FROM accounts; -- Resolves to tenant_A.accounts
```

### The Massive Catalog Scaling Trap
At first glance, schema-per-tenant seems ideal: it provides hard database-level separation of tables and avoids the risk of shared-index pollution. 

However, it hides a dangerous operational trap: **PostgreSQL System Catalog Bloat**.

For every table, index, column, constraint, view, and function created in a schema, PostgreSQL adds rows to its internal system tables (like `pg_class`, `pg_attribute`, `pg_depend`).

If your application has 100 tables/indexes and you scale to 5,000 tenants:
$$5,000 \text{ tenants} \times 100 \text{ tables} = 500,000 \text{ physical tables}$$

When system catalogs bloat to hundreds of thousands of classes:
1.  **Memory Consumption:** PostgreSQL must cache these table definitions in memory. Ram consumption spikes.
2.  **Slow Startup:** Simple connection establishment becomes slow because PostgreSQL needs to read massive catalog files.
3.  **Vacuum & Maintenance:** Simple administrative tasks like `VACUUM` or running `pg_dump` backup scripts take hours or crawl to a halt.

---

## Connection Pooling and PgBouncer Implications

SaaS applications generally use connection poolers like **PgBouncer** to multiplex database connections. This is where the chosen multi-tenancy model significantly impacts capacity planning.

```
                    ┌──────────────┐
                    │ Application  │ (thousands of connections)
                    └──────┬───────┘
                           ▼
                    ┌──────────────┐
                    │  PgBouncer   │ (Transaction Mode)
                    └──────┬───────┘
                           ▼
                    ┌──────────────┐
                    │  PostgreSQL  │ (few physical connections)
                    └──────────────┘
```

PgBouncer operates in three modes: **Session**, **Transaction**, or **Statement** mode. To achieve maximum scaling, **Transaction Mode** is preferred, where PgBouncer re-assigns physical server connections to different clients on *every individual transaction*.

### The Conflict with Schema-per-Tenant
In Transaction Mode, PgBouncer is state-blind. If a transaction sets `SET search_path TO tenant_A`, PgBouncer might return that same connection to a different client for a different tenant *without resetting the search path*. 
*   This will cause Tenant B's transaction to execute against Tenant A's schema, resulting in a silent, massive security breach.
*   **Result:** You *cannot* use standard PgBouncer Transaction Mode with `SET search_path` unless you force PgBouncer to track session states (which defeats the performance gains of transaction pooling) or use specialized pooling setups.

### The RLS Advantage
With RLS, since we use `SET LOCAL app.current_tenant_id = 'tenant_A'`, the GUC variable is automatically scoped to `LOCAL` (meaning it auto-resets when the current transaction ends / commits).
*   PgBouncer can safely clean and recycle that connection for a different tenant's transaction.
*   **Result:** RLS is highly compatible with PgBouncer's high-performance transaction pooling mode.

---

## Migration Management Overhead

Running database schema migrations is another major differentiator.

### Shared Schema (RLS)
*   **Migration Complexity:** Low. To add a column, you run a single `ALTER TABLE` statement. The migration completes in milliseconds.
*   **Downside:** A bad migration (like locking a large table) affects all tenants simultaneously.

### Schema-per-Tenant
*   **Migration Complexity:** Extreme. To add a column, you must loop through all 5,000 schemas and run the migration statement 5,000 times.
*   **Failure Recovery:** If the migration fails on tenant 4,211, your database is left in a partially migrated state. You must build robust schema-version tracking systems to recover.

---

## Comparison Matrix

| Architectural Dimension | Shared Schema + RLS | Schema-per-Tenant |
| :--- | :--- | :--- |
| **Isolation Level** | Logical (Policy-driven) | Namespace (Schema-driven) |
| **Catalog Scalability** | **Excellent** (Consistent tables) | Poor (Bloats at $>1,000$ tenants) |
| **Migration Simplicity** | **Excellent** (Single migration) | Poor (Requires multi-schema loops) |
| **Connection Pooling** | **Transaction Mode (Safe)** | Session Mode Only (High overhead) |
| **Shared Analytics** | Easy (Cross-tenant aggregates) | Hard (Requires `UNION` or ETL) |
| **Backup & Restore** | Hard (Per-tenant restore is complex) | Easy (Can backup single schema) |

## Conclusion

For small SaaS projects with a few dozen large enterprise customers requiring strict database separation and separate backups, **Schema-per-Tenant** is a viable, conceptually simple model.

However, if you are building a modern, highly scalable B2B SaaS aiming for thousands of tenants, **Shared Schema with Row-Level Security (RLS)** is the vastly superior choice. It scales gracefully, integrates perfectly with high-throughput connection poolers, simplifies schema management, and keeps your operational database footprints lean and easy to maintain.
