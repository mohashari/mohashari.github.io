---
layout: post
title: "PgBouncer Internals: Transaction vs Session Pooling, Prepared Statements, and Connection Multiplexing"
date: 2026-03-31 08:00:00 +0700
tags: [postgresql, pgbouncer, database, backend, performance]
description: "A deep dive into PgBouncer's pooling modes, prepared statement pitfalls, and how connection multiplexing actually works under load."
image: "https://picsum.photos/1080/720?random=8018"
thumbnail: "https://picsum.photos/400/300?random=8018"
---

Your PostgreSQL server is sitting at 800 idle connections, each consuming ~5–10 MB of memory, while your application servers are timing out waiting for a slot. You scale the instance, the connections spike again, and you're back where you started. This is the `max_connections` death spiral, and it's one of the most predictable capacity failure modes in production Postgres deployments. PgBouncer exists to break that spiral — but reaching for it without understanding its internals is how you trade a connection problem for a correctness problem.

This post covers how PgBouncer actually multiplexes connections, why the choice between transaction and session pooling isn't just a performance knob, and the specific failure modes around prepared statements that will bite you in production if you're not paying attention.

## How PgBouncer Multiplexes Connections

PgBouncer is a single-threaded connection proxy. It maintains two sets of sockets: client-facing sockets (from your app) and server-facing sockets (to Postgres). The core insight is that most application connections are idle most of the time. Even at high concurrency, the fraction of connections actively executing a query at any instant is small.

PgBouncer maintains a pool of server connections and assigns them to client connections on demand. When a client is idle, its server connection goes back into the pool and becomes available for another client. This is connection multiplexing — N application connections sharing M server connections where N >> M.

The internal structure is straightforward: PgBouncer uses `libevent` for async I/O and a state machine per connection. Each client connection object tracks its current server assignment, the pool it belongs to, and which pooling mode governs its behavior. The interesting complexity is entirely in _when_ a server connection is released back to the pool.

## Session vs Transaction Pooling: The Real Difference

This is where most engineers get into trouble.

**Session pooling** assigns a server connection to a client for the lifetime of the client's connection. The server connection is only released when the client disconnects. This is the safest mode because it preserves all session-level state — `SET` commands, advisory locks, temporary tables, `LISTEN/NOTIFY` subscriptions, and prepared statements all work correctly. The tradeoff: you get almost no multiplexing benefit unless clients connect and disconnect frequently.

**Transaction pooling** releases the server connection back to the pool at the end of every transaction. This is the mode that actually buys you connection reduction — a pool of 50 server connections can serve hundreds of application threads because each thread only holds a connection for the duration of its transaction, not the idle time between queries.

The problem is everything that PostgreSQL attaches to a session. Transaction pooling breaks:

- `SET` commands (session-level state is lost when the server connection is reassigned)
- Advisory locks (`pg_advisory_lock` is session-scoped)
- `LISTEN/NOTIFY` (subscription state is session-scoped)
- Temporary tables
- Named prepared statements (this one deserves its own section)

```ini
# snippet-1
# /etc/pgbouncer/pgbouncer.ini — production transaction pooling config
[databases]
myapp = host=postgres-primary port=5432 dbname=myapp

[pgbouncer]
pool_mode = transaction
max_client_conn = 2000
default_pool_size = 50
min_pool_size = 10
reserve_pool_size = 5
reserve_pool_timeout = 3
server_idle_timeout = 600
server_lifetime = 3600
client_idle_timeout = 0

# Critical for transaction pooling correctness
server_reset_query = DISCARD ALL
server_check_query = SELECT 1
server_check_delay = 30

# Auth
auth_type = scram-sha-256
auth_file = /etc/pgbouncer/userlist.txt
```

The `server_reset_query` setting matters more than most people realize. When PgBouncer reassigns a server connection to a new client, it runs `DISCARD ALL` to clean up any leftover session state. Without this, you can end up with session-level `search_path` changes or other state leaking across client connections — a subtle data correctness bug that's hard to reproduce.

## The Prepared Statement Problem

Named prepared statements in PostgreSQL are session-scoped. When your application calls `PREPARE foo AS SELECT ...`, that prepared statement exists only on the specific server connection that executed it. In session pooling mode this is fine — the same client always gets the same server connection. In transaction pooling mode, your next transaction might land on a different server connection that has never seen `foo`, and you get:

```
ERROR: prepared statement "foo" does not exist
```

This error is intermittent and load-dependent. Under low concurrency, you might always get the same server connection by chance. Under load, the pool reassigns connections and the bug surfaces. It's the kind of failure that doesn't show up in staging.

Most modern ORMs and database drivers use prepared statements by default for performance — psycopg3, asyncpg, SQLAlchemy, GORM, Prisma. You need to handle this explicitly.

**Option 1: Disable named prepared statements in your driver**

```python
# snippet-2
# asyncpg — disable prepared statement caching for PgBouncer transaction mode
import asyncpg

async def create_pool():
    return await asyncpg.create_pool(
        dsn="postgresql://user:pass@pgbouncer:5432/myapp",
        # Disable statement cache — asyncpg won't use named prepared statements
        statement_cache_size=0,
        # Use protocol-level prepared statements per-connection only
        # This falls back to simple query protocol for all queries
        max_cached_statement_lifetime=0,
    )
```

<script src="https://gist.github.com/mohashari/ae4d5ac3e2435487cb899f5877b586e2.js?file=snippet-3.go"></script>

**Option 2: Use `pgbouncer_prepared_statement` mode (PgBouncer 1.21+)**

PgBouncer 1.21 added experimental support for tracking prepared statements at the proxy layer. It intercepts `PREPARE` and `EXECUTE` protocol messages and maintains a per-pool mapping. This lets you keep prepared statements in your driver while using transaction pooling, but it adds overhead and the implementation has edge cases. As of 1.22 it's still marked experimental. Worth watching but not yet production-ready for high-stakes workloads.

**Option 3: Use anonymous (protocol-level) prepared statements**

The extended query protocol in PostgreSQL allows preparing and executing a statement in the same message cycle without naming it. The driver sends `Parse` (with an empty statement name), `Bind`, and `Execute` in one roundtrip. This gets you the type-checking benefits of prepared statements without the session-scoping problem. pgx v5 supports this via `QueryExecModeCacheDescribe`.

## Connection Pool Sizing

The default `default_pool_size = 20` in PgBouncer is too small for most production workloads and too large for some. The right number depends on your Postgres server's CPU count, not your application's concurrency.

The classic formula from connection pool research: `pool_size = (core_count * 2) + effective_spindle_count`. For a 16-core Postgres instance with SSDs (spindle count ≈ 1), that's roughly 33 server connections per pool. Many teams run 40–80 and find that sweet spot empirically.

```bash
# snippet-4
# Monitor pool utilization to tune pool_size
# Run against your PgBouncer admin socket

psql -h /var/run/pgbouncer -p 6432 -U pgbouncer pgbouncer -c "SHOW POOLS;"

# Key columns:
# cl_active  — clients currently executing a query
# cl_waiting — clients blocked waiting for a server connection (THIS IS YOUR ALERT)
# sv_active  — server connections currently executing a query
# sv_idle    — server connections available in pool
# sv_used    — server connections returned but not yet cleaned

# If cl_waiting > 0 under normal load, increase pool_size
# If sv_idle / (sv_active + sv_idle) > 0.7 consistently, decrease pool_size

psql -h /var/run/pgbouncer -p 6432 -U pgbouncer pgbouncer -c "SHOW STATS;"
# avg_query_time, avg_wait_time — watch avg_wait_time spike under load
```

The `cl_waiting` metric is your primary health indicator. Waiting clients mean the pool is exhausted — queries are queued at the proxy layer, adding latency. If you're consistently seeing waiting clients, you either need a larger pool (if Postgres can handle the load) or you need to optimize your long-running transactions.

## Long Transactions and Pool Starvation

Transaction pooling's Achilles heel is long-running transactions. A client that opens a transaction and then does slow application logic while holding it is occupying a server connection for the entire duration. Under high concurrency, this saturates the pool and causes `cl_waiting` to spike.

```sql
-- snippet-5
-- Find long-running transactions in Postgres (run on Postgres, not PgBouncer)
SELECT
    pid,
    now() - xact_start AS txn_duration,
    now() - query_start AS query_duration,
    state,
    wait_event_type,
    wait_event,
    left(query, 80) AS query_preview
FROM pg_stat_activity
WHERE xact_start IS NOT NULL
  AND state != 'idle'
  AND now() - xact_start > interval '5 seconds'
ORDER BY txn_duration DESC;
```

If you're seeing pool starvation from long transactions, PgBouncer's `query_timeout` and `transaction_timeout` settings (added in 1.21) let you enforce limits:

```ini
# snippet-6
# Enforce transaction time limits — prevents pool starvation from runaway transactions
[pgbouncer]
# Disconnect clients with transactions exceeding 30 seconds
transaction_timeout = 30

# Kill individual queries taking too long
query_timeout = 25

# How long a client can wait for a server connection before being rejected
query_wait_timeout = 10
```

Be careful with `query_timeout` — it kills the connection when a query exceeds the limit, which rolls back the transaction. This is correct behavior, but your application needs to handle the resulting `FATAL: query_timeout` error gracefully rather than surfacing it to the user as a 500.

## SSL Termination and Authentication

PgBouncer sits between your application and Postgres, which means it participates in the SSL handshake on both sides independently. The common production topology has SSL from app to PgBouncer, and SSL from PgBouncer to Postgres:

```ini
# snippet-7
# SSL configuration for both client and server sides
[pgbouncer]
# Client (app → PgBouncer) SSL
client_tls_sslmode = require
client_tls_cert_file = /etc/pgbouncer/server.crt
client_tls_key_file = /etc/pgbouncer/server.key
client_tls_ca_file = /etc/ssl/certs/ca-certificates.crt

# Server (PgBouncer → Postgres) SSL
server_tls_sslmode = verify-full
server_tls_ca_file = /etc/ssl/certs/ca-certificates.crt

# HBA-style auth
auth_type = scram-sha-256
auth_file = /etc/pgbouncer/userlist.txt

# OR use auth_query to delegate auth to Postgres directly
# auth_user = pgbouncer_auth
# auth_query = SELECT username, password FROM pgbouncer.get_auth($1)
```

The `auth_query` approach is cleaner for large teams — credentials live in Postgres and PgBouncer doesn't need a local `userlist.txt` that you have to keep synchronized.

## Observability and Alerting

PgBouncer exposes a virtual `pgbouncer` database on the admin port with several `SHOW` commands. Wire these into your metrics pipeline:

```python
# snippet-8
# Prometheus metrics scraper for PgBouncer
# Run as a sidecar or separate exporter

import asyncpg
import asyncio
from prometheus_client import Gauge, start_http_server

POOL_WAITING = Gauge('pgbouncer_client_waiting', 'Clients waiting for connection', ['database', 'user'])
POOL_ACTIVE = Gauge('pgbouncer_client_active', 'Clients with active server connection', ['database', 'user'])
POOL_IDLE = Gauge('pgbouncer_server_idle', 'Idle server connections', ['database', 'user'])
AVG_WAIT = Gauge('pgbouncer_avg_wait_ms', 'Average wait time in ms', ['database'])

async def scrape():
    conn = await asyncpg.connect(
        host='/var/run/pgbouncer',
        port=6432,
        user='pgbouncer',
        database='pgbouncer',
    )
    try:
        rows = await conn.fetch('SHOW POOLS')
        for row in rows:
            labels = [row['database'], row['user']]
            POOL_WAITING.labels(*labels).set(row['cl_waiting'])
            POOL_ACTIVE.labels(*labels).set(row['cl_active'])
            POOL_IDLE.labels(*labels).set(row['sv_idle'])

        stats = await conn.fetch('SHOW STATS')
        for row in stats:
            AVG_WAIT.labels(row['database']).set(row['avg_wait_time'] / 1000)
    finally:
        await conn.close()

async def main():
    start_http_server(9127)
    while True:
        await scrape()
        await asyncio.sleep(15)

asyncio.run(main())
```

Alert on `pgbouncer_client_waiting > 0` for more than 30 seconds. Alert on `pgbouncer_avg_wait_ms > 50`. These are your leading indicators of capacity problems.

## When Not to Use PgBouncer

PgBouncer is not the right tool for every workload. If you're running long-lived streaming queries, `LISTEN/NOTIFY` consumers, or logical replication slots — these need session-level state that transaction pooling destroys. Use separate dedicated connections for these, bypassing PgBouncer entirely.

Similarly, if your application already uses a proper connection pool (HikariCP, pgxpool, asyncpg's pool) with sane limits, you may not need PgBouncer at all. The problem PgBouncer solves is _total connection count to Postgres_ — if you have 5 application servers each with a pool of 10, you have 50 connections, which Postgres handles fine. PgBouncer adds value when you can't control the connection behavior of all clients (e.g., multiple services, third-party tools, ad-hoc analysts hitting the same instance).

The production decision tree: if `SHOW pg_stat_activity` shows hundreds of idle connections and you're approaching `max_connections`, deploy PgBouncer in transaction mode and fix the prepared statement issues. If you're under 200 connections and Postgres is healthy, fix the connection pool settings in your application layer first.
```