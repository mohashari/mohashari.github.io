---
layout: post
title: "PostgreSQL Performance Tuning: From Slow to Lightning Fast"
tags: [postgresql, database, performance, backend]
description: "Practical PostgreSQL performance tuning — configuration, query optimization, and monitoring techniques that actually move the needle."
---

PostgreSQL out-of-the-box is configured for broad compatibility, not maximum performance. With the right tuning, you can dramatically improve throughput and latency. Here's what to actually do.

## Start with Configuration

PostgreSQL's default `postgresql.conf` is conservative. Tune these settings based on your hardware.

### Memory Settings


<script src="https://gist.github.com/mohashari/1507318d28ff50f85ceca4664f2b223d.js?file=snippet.ini"></script>


### Write Performance


<script src="https://gist.github.com/mohashari/1507318d28ff50f85ceca4664f2b223d.js?file=snippet-2.ini"></script>


### Connection Settings


<script src="https://gist.github.com/mohashari/1507318d28ff50f85ceca4664f2b223d.js?file=snippet-3.ini"></script>


Never set `max_connections = 1000`. Each connection uses ~10MB of RAM and PostgreSQL doesn't handle thousands of connections well. Use **PgBouncer** in transaction mode.

## Query Optimization

### EXPLAIN ANALYZE is Your Best Friend


<script src="https://gist.github.com/mohashari/1507318d28ff50f85ceca4664f2b223d.js?file=snippet.sql"></script>


Read the output:
- **Seq Scan** on large tables = missing index
- **Hash Join** vs **Nested Loop** — optimizer choice based on statistics
- **actual rows** much different from **estimated rows** = outdated statistics (`ANALYZE`)
- **Buffers: hit** = from cache, **read** = from disk

### Common Query Patterns to Optimize

#### Avoid SELECT *


<script src="https://gist.github.com/mohashari/1507318d28ff50f85ceca4664f2b223d.js?file=snippet-2.sql"></script>


#### Use CTEs Wisely

In older PostgreSQL, CTEs were "optimization fences". In PG 12+, CTEs inline by default:


<script src="https://gist.github.com/mohashari/1507318d28ff50f85ceca4664f2b223d.js?file=snippet-3.sql"></script>


#### Batch Operations


<script src="https://gist.github.com/mohashari/1507318d28ff50f85ceca4664f2b223d.js?file=snippet-4.sql"></script>


#### Optimize Pagination


<script src="https://gist.github.com/mohashari/1507318d28ff50f85ceca4664f2b223d.js?file=snippet-5.sql"></script>


## Vacuuming and Table Bloat

PostgreSQL uses MVCC — old row versions accumulate. VACUUM reclaims them.


<script src="https://gist.github.com/mohashari/1507318d28ff50f85ceca4664f2b223d.js?file=snippet-6.sql"></script>


Configure autovacuum aggressively for high-write tables:


<script src="https://gist.github.com/mohashari/1507318d28ff50f85ceca4664f2b223d.js?file=snippet-7.sql"></script>


## Monitoring Queries


<script src="https://gist.github.com/mohashari/1507318d28ff50f85ceca4664f2b223d.js?file=snippet-8.sql"></script>


## Connection Pooling with PgBouncer

Install and configure PgBouncer in transaction mode:


<script src="https://gist.github.com/mohashari/1507318d28ff50f85ceca4664f2b223d.js?file=snippet-4.ini"></script>


Your app connects to PgBouncer on port 6432; PgBouncer maintains a pool of 20 real connections to PostgreSQL.

## Quick Wins Checklist

- [ ] Tune `shared_buffers`, `work_mem`, `effective_cache_size`
- [ ] Deploy PgBouncer for connection pooling
- [ ] Enable `pg_stat_statements` and find your top slow queries
- [ ] Add missing indexes on foreign keys
- [ ] Check for table bloat and tune autovacuum
- [ ] Replace `OFFSET` pagination with cursor-based
- [ ] Use `EXPLAIN ANALYZE` before every schema/index change
