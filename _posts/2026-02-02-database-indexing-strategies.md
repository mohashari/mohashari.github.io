---
layout: post
title: "Database Indexing Strategies That Actually Matter"
tags: [database, postgresql, performance, backend]
description: "Understanding database indexes deeply — how they work, when to use them, and the common mistakes that kill performance."
---

Slow queries are responsible for more production incidents than almost anything else. And the solution is almost always the same: proper indexing. Let's go deep on how indexes work and how to use them effectively.

![B-Tree Index vs Full Table Scan](/images/diagrams/database-indexing.svg)

## How Indexes Work (The Mental Model)

Think of a database table as a book and an index as the book's index at the back. Without an index, finding all mentions of "Redis" means reading every single page (full table scan). With an index, you jump directly to the right pages.

In PostgreSQL (and most databases), the default index type is a **B-Tree** (Balanced Tree). It keeps data sorted, making it fast for:
- Exact matches: `WHERE id = 42`
- Range queries: `WHERE created_at > '2026-01-01'`
- Sorting: `ORDER BY last_name`

## Types of Indexes in PostgreSQL

### B-Tree (Default)
Best for most cases — equality, ranges, sorting.


<script src="https://gist.github.com/mohashari/42ed858dddc9f02021880443120f21c4.js?file=snippet.sql"></script>


### Hash Index
Only for exact equality matches. Faster than B-Tree for this case but limited.


<script src="https://gist.github.com/mohashari/42ed858dddc9f02021880443120f21c4.js?file=snippet-2.sql"></script>


### GIN (Generalized Inverted Index)
For full-text search and array/JSONB columns.


<script src="https://gist.github.com/mohashari/42ed858dddc9f02021880443120f21c4.js?file=snippet-3.sql"></script>


### GiST and BRIN
For geometric data and very large sequential tables respectively.

## Composite Indexes: Order Matters!

A composite index on `(a, b, c)` can serve queries on:
- `WHERE a = ...`
- `WHERE a = ... AND b = ...`
- `WHERE a = ... AND b = ... AND c = ...`

But NOT:
- `WHERE b = ...` (doesn't start from leftmost)
- `WHERE c = ...`


<script src="https://gist.github.com/mohashari/42ed858dddc9f02021880443120f21c4.js?file=snippet-4.sql"></script>


**Rule: Put the most selective column first in composite indexes.**

## Covering Indexes

If your index contains all the columns a query needs, PostgreSQL never has to touch the main table — this is an "index-only scan" and is extremely fast:


<script src="https://gist.github.com/mohashari/42ed858dddc9f02021880443120f21c4.js?file=snippet-5.sql"></script>


## Partial Indexes

Index only a subset of rows. Smaller, faster:


<script src="https://gist.github.com/mohashari/42ed858dddc9f02021880443120f21c4.js?file=snippet-6.sql"></script>


## Functional Indexes

Index the result of a function:


<script src="https://gist.github.com/mohashari/42ed858dddc9f02021880443120f21c4.js?file=snippet-7.sql"></script>


## Diagnosing Slow Queries


<script src="https://gist.github.com/mohashari/42ed858dddc9f02021880443120f21c4.js?file=snippet-8.sql"></script>


Find slow queries from logs:


<script src="https://gist.github.com/mohashari/42ed858dddc9f02021880443120f21c4.js?file=snippet-9.sql"></script>


## Common Indexing Mistakes

### 1. Over-indexing
Every index slows down writes (INSERT, UPDATE, DELETE). Don't add indexes "just in case".

### 2. Not indexing foreign keys
Always index foreign keys — they're used in JOINs constantly:


<script src="https://gist.github.com/mohashari/42ed858dddc9f02021880443120f21c4.js?file=snippet-10.sql"></script>


### 3. Ignoring index bloat
Indexes can become bloated over time. Rebuild them periodically:


<script src="https://gist.github.com/mohashari/42ed858dddc9f02021880443120f21c4.js?file=snippet-11.sql"></script>


### 4. Using functions on indexed columns in WHERE


<script src="https://gist.github.com/mohashari/42ed858dddc9f02021880443120f21c4.js?file=snippet-12.sql"></script>


## Quick Decision Guide

| Scenario | Solution |
|----------|----------|
| `WHERE email = ?` | B-Tree index on `email` |
| Case-insensitive lookup | Functional index on `LOWER(email)` |
| `WHERE user_id = ? AND status = ?` | Composite index `(user_id, status)` |
| Full-text search | GIN index on `tsvector` |
| Sparse column (many NULLs) | Partial index `WHERE col IS NOT NULL` |
| JSONB queries | GIN index on the JSONB column |

Indexing is part science, part art. Profile first, index based on real query patterns, and measure the impact.
