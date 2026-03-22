---
layout: post
title: "PostgreSQL JSONB: When to Use It and When to Avoid It"
date: 2026-03-22 08:00:00 +0700
tags: [postgresql, database, backend, performance, data-modeling]
description: "JSONB is a sharp tool for genuine schema uncertainty — not a shortcut around proper data modeling."
---

You've seen it before: a PR that adds a `metadata JSONB` column to a core table because the requirements are "still fluid." Six months later, that column contains 47 different shapes of data, three downstream services parse it differently, and a query filtering on `metadata->>'tenant_id'` is doing a sequential scan across 80 million rows. JSONB didn't cause that mess — the decision to use it as a crutch did. This post is about knowing the difference between a legitimate use case and a slow-moving disaster.

## What JSONB Actually Is

JSONB stores JSON as a decomposed binary format. Unlike the `json` type (which stores raw text and re-parses on every access), JSONB parses at write time and stores a normalized binary representation. This means key ordering is not preserved, duplicate keys are deduplicated (last value wins), and reads are fast because there's no parsing overhead.

The real leverage comes from its indexing capabilities. GIN indexes on JSONB support containment (`@>`), key existence (`?`), and path existence (`?|`, `?&`) operators. You can also build expression indexes on specific extracted paths. Neither is free — GIN indexes are large and slow to build — but they make JSONB queries viable at scale when you design for them upfront.

## When JSONB Is the Right Call

**Sparse attribute sets.** If you have entities with hundreds of possible attributes but each entity uses only a handful, normalized tables become a liability. A product catalog with category-specific attributes is the canonical example: a laptop has `cpu_cores`, `ram_gb`, `storage_type`; a shirt has `size`, `color`, `material`. Forcing these into a shared schema means either an EAV nightmare or a table with 200 nullable columns.

<script src="https://gist.github.com/mohashari/2f0a8d136825f6f4640d1dbd65f4c3fd.js?file=snippet-1.sql"></script>

The GIN index makes the containment check fast. The expression index on `brand` handles the equality filter. This works because you know the query patterns ahead of time and index accordingly.

**Semi-structured external data.** Webhooks, third-party API responses, and event payloads from systems you don't control are natural fits. You need to store the full payload for auditability, replay, or debugging — but you only reliably query a few top-level fields.

<script src="https://gist.github.com/mohashari/2f0a8d136825f6f4640d1dbd65f4c3fd.js?file=snippet-2.sql"></script>

Notice: you're not querying inside `payload`. The fields you filter on — `source`, `event_type`, `processed_at` — are proper columns with proper indexes. The JSONB column is a storage container, not a query surface.

**Dynamic user-defined metadata.** Multi-tenant SaaS products often let customers attach arbitrary key-value data to their own objects — tags, custom fields, integration-specific metadata. You cannot know the schema at build time.

<script src="https://gist.github.com/mohashari/2f0a8d136825f6f4640d1dbd65f4c3fd.js?file=snippet-3.sql"></script>

The `@>` containment operator handles arbitrary user-defined shapes without schema changes. This is the JSONB sweet spot: structured uncertainty, with query patterns you can bound.

## When JSONB Will Hurt You

**Relational data in disguise.** The most expensive mistake I've seen is using JSONB to store what is clearly a normalized table. Order line items inside an order. Team members inside a project. Permissions inside a role. As soon as you need to query across those embedded objects — "find all orders containing product SKU X" — you've built a document store on top of a relational database and gotten the benefits of neither.

<script src="https://gist.github.com/mohashari/2f0a8d136825f6f4640d1dbd65f4c3fd.js?file=snippet-4.sql"></script>

**Heavy filtering on deeply nested paths.** GIN indexes support containment queries efficiently, but they are not magic. If your access pattern requires filtering on `payload->'user'->'address'->>'city'`, you're extracting a scalar from a nested path. This forces Postgres to evaluate that expression for every row unless you've built a dedicated expression index — which means you're back to planning your schema ahead of time, at which point you might as well have a column.

Nested JSONB access also breaks query planner statistics. Postgres collects statistics on columns, not on JSON paths. The planner has no visibility into the distribution of values inside your JSONB blobs, which leads to bad row estimates and suboptimal plans.

**Anything needing referential integrity.** JSONB values are opaque to the constraint system. You cannot add a foreign key on `attributes->>'category_id'`. You cannot enforce that a required field exists at the database level (triggers are a workaround, not a solution). If your JSONB stores IDs that reference other tables, you've created implicit references that will silently dangle when rows are deleted.

**High-churn data with write amplification.** JSONB updates are not surgical. Postgres has no way to update a single field within a JSONB value in place — every update rewrites the entire column value. On a table with frequent updates to a large JSONB blob, this generates substantial dead tuple bloat and increases VACUUM pressure. If you're updating `user_preferences->>'theme'` on every page load, that should be a column.

## Index Design Is Not Optional

If you're using JSONB without thinking about indexes, you're doing full table scans and you probably don't know it yet. `EXPLAIN (ANALYZE, BUFFERS)` on queries touching JSONB columns is non-negotiable during development.

<script src="https://gist.github.com/mohashari/2f0a8d136825f6f4640d1dbd65f4c3fd.js?file=snippet-5.sql"></script>

GIN indexes on wide JSONB blobs can be 2-5x the size of the table itself. This is not a theoretical concern — it directly affects your RAM requirements for keeping hot indexes in `shared_buffers` and impacts checkpoint frequency and WAL volume.

## Schema Drift Is a Team Problem

JSONB enables schema drift by design. There is no enforcement mechanism preventing one service from writing `{"user_id": 123}` and another from writing `{"userId": "abc-123"}` into the same column. After 18 months in production, you'll have multiple naming conventions, inconsistent types for the same logical field, and no single source of truth.

Mitigate this in application code, not in the database:

```python
# snippet-6
from pydantic import BaseModel, Field
from typing import Optional
import uuid

class ContactCustomFields(BaseModel):
    """
    Enforced at the application layer — every write goes through this.
    The database stores whatever you give it; validation is your job.
    """
    tags: list[str] = Field(default_factory=list)
    deal_stage: Optional[str] = None
    owner_id: Optional[uuid.UUID] = None
    hubspot_contact_id: Optional[str] = None

    class Config:
        extra = "allow"  # allow unknown fields for forward compatibility
        # But validate known fields strictly

def upsert_contact_custom_fields(
    contact_id: uuid.UUID,
    fields: ContactCustomFields,
    db: Connection,
) -> None:
    # Always serialize through the model — never raw dict writes
    validated = fields.model_dump(exclude_none=True)
    db.execute(
        """
        UPDATE contacts
        SET custom_fields = custom_fields || $1::jsonb
        WHERE id = $2
        """,
        [json.dumps(validated), str(contact_id)],
    )
```

Use `||` for merging to avoid overwriting existing keys the caller didn't include. Enforce your JSONB schema at the application boundary, not the database boundary — and keep that enforcement centralized.

## Migrating JSONB to Columns When You've Grown Into Structure

A common lifecycle: you start with JSONB because requirements are uncertain, and after a year you realize 80% of rows have the same five fields and you're doing expression indexes on all of them anyway. The migration is not painful if you do it incrementally.

<script src="https://gist.github.com/mohashari/2f0a8d136825f6f4640d1dbd65f4c3fd.js?file=snippet-7.sql"></script>

This incremental approach lets you migrate without downtime and gives you a verification gate before committing. The trigger doubles write overhead temporarily — budget for that.

## The Decision Framework

Before reaching for JSONB, answer these three questions:

**1. Do I know the query patterns?** If yes, and they require filtering on specific paths with high selectivity, you need columns and indexes. JSONB is for cases where you genuinely cannot enumerate the query patterns at design time.

**2. Is the structure truly open-ended, or just currently unknown?** "We don't know the schema yet" is different from "the schema is user-defined and unbounded." The first is a planning problem — gather requirements. The second is a legitimate JSONB use case.

**3. Does the data have relationships that need enforcement?** If yes, it belongs in a table. FK constraints, cascade deletes, and join performance are not things you want to implement yourself in application code.

JSONB earns its place for external payloads you store but mostly don't query, for user-defined metadata with bounded query patterns, and for genuinely sparse attribute sets where normalization would produce more harm than good. Outside those cases, it's a flexibility trap that defers modeling decisions until they're far more expensive to fix.

The engineers I've seen get burned by JSONB aren't lazy — they're optimistic. They reach for it thinking it buys optionality. It does, for a while. The bill comes when the data grows, the query patterns solidify, and refactoring a heavily-loaded production table requires the kind of careful incremental migration described above. The right question isn't "can I use JSONB here?" It's "what would I lose by using proper columns?" If the answer is nothing, use proper columns.
```