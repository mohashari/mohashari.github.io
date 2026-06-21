---
layout: post
title: "Enforcing Read-Your-Own-Writes Consistency using MySQL GTID Tracking in Go"
date: 2026-06-21 08:00:00 +0700
tags: [go, mysql, database, replication, consistency]
description: "Enforce session consistency in Go applications using MySQL GTID tracking to eliminate replica lag issues and dynamically route database reads."
image: "https://picsum.photos/seed/8167/1080/720"
thumbnail: "https://picsum.photos/seed/8167/400/300"
---

Imagine this: a user edits their shipping address, clicks "Save," and the page reloads, only to show the old address. Confused, the user clicks "Save" again, submitting a duplicate request, or immediately submits a support ticket. In production, this classic race condition is caused by asynchronous replication lag. Under load, your write goes to the MySQL Primary, but the subsequent read query is routed to a Read Replica that is lagging even just 150 milliseconds behind. While eventual consistency is acceptable for background reports or product catalogues, it is unacceptable for user profile updates, shopping carts, or order placements. Enforcing \"Read-Your-Own-Writes\" (RYOW)—also known as session consistency—is critical for a professional user experience. Rather than reverting to the expensive anti-pattern of routing all reads to the primary database, we can utilize MySQL's Global Transaction Identifiers (GTIDs) and Go's connection session tracking to guarantee consistency dynamically.

![Enforcing Read-Your-Own-Writes Consistency using MySQL GTID Tracking in Go Diagram](/images/diagrams/read-your-own-writes-mysql-gtid-go.svg)

## Asynchronous Replication and the Consistency Problem

In high-throughput architectures, scaling database read operations typically involves setting up a primary-replica topology. The primary handles all writes (inserts, updates, deletes) and writes them to its binary log (`binlog`). One or more replicas pull these binlog events asynchronously to replay them locally. 

This asynchronous nature is essential for write performance; the primary commits transactions without waiting for acknowledgment from replicas. However, this creates a time window where a replica is behind the primary. This delay is known as **replication lag**. Under normal load, replication lag stays under 10 milliseconds. But during traffic spikes, massive batch updates, database migrations, or high-cardinality index modifications, replication lag can easily spike to seconds or even minutes.

To solve this, developers often use suboptimal workarounds:
1. **Force all reads to the Primary**: This defeats the entire purpose of horizontal read scaling, concentrating load and risking primary CPU saturation.
2. **Arbitrary Sticky Session Routing**: Routing all reads from a user to the primary for a fixed duration (e.g., 5 seconds) after any write. This is a blind heuristic; it will fail if the lag exceeds 5 seconds, and it unnecessarily loads the primary if the replica is already caught up.
3. **Session-level Read-Your-Own-Writes (RYOW)**: Ensuring that a client's reads are guaranteed to see their own writes, while other users can observe eventual consistency. 

MySQL GTIDs provide the perfect foundation for a dynamic, precise RYOW system.

## The Anatomy of MySQL Global Transaction Identifiers (GTID)

A Global Transaction Identifier (GTID) is a unique identifier created and associated with each transaction committed on the primary database server. This identifier is not only unique on the originating server but is unique across all databases in a replication topology.

A GTID is represented as:
```text
GTID = source_id:transaction_id
```
Where `source_id` is the `server_uuid` of the originating primary server, and `transaction_id` is a monotonically increasing integer sequence number indicating the order in which the transaction was committed (e.g., `3e11fa47-71ca-11e1-9e33-c80aa9429562:1405`).

To use GTIDs, your MySQL configuration (`my.cnf`) must enable them on all nodes:
```ini
gtid_mode = ON
enforce_gtid_consistency = ON
binlog_format = ROW
```

When a write transaction completes, the primary updates its local state variable `@@global.gtid_executed`—which represents all transactions executed on that node. In the context of the client session that initiated the write, MySQL tracks the specific transaction GTID generated in the session variable `@@session.last_gtid`.

If the Go application can capture `@@session.last_gtid` immediately after a write transaction commits, it can pass this identifier to the client. On subsequent read requests, the client passes this GTID back. The Go application then queries the replica using the built-in function:
```sql
SELECT WAIT_FOR_EXECUTED_GTID_SET('gtid_set', timeout)
```
This function blocks the connection on the replica until the replica has replayed and committed all transactions matching the specified GTID set. If the replica catches up within the timeout, the function returns `0` and execution proceeds. If the timeout is reached, it returns `1`.

## The Go database/sql Connection Pool Trap

Implementing GTID tracking in Go presents a subtle but severe trap regarding how Go's `database/sql` package handles connection pooling.

In Go, `sql.DB` is not a single database connection; it is a connection pool manager. When you run a query using `db.QueryRow` or `db.Exec`, the manager leases a connection from the pool, runs the query, and immediately returns it. 

If you execute a write transaction and then query the session variable `@@session.last_gtid` using standard pool queries, you will experience connection leakage across queries:

```go
// BUG: This will fail to retrieve the correct GTID
tx, _ := db.BeginTx(ctx, nil)
tx.ExecContext(ctx, "INSERT INTO users ...")
tx.Commit() // The connection is immediately returned to the pool here!

var gtid string
// This query leases a random connection, which is highly unlikely to be 
// the same connection that executed the transaction above!
db.QueryRowContext(ctx, "SELECT @@session.last_gtid").Scan(&gtid) 
```

Because `tx.Commit()` releases the connection back to the pool, the subsequent call to `db.QueryRowContext` will execute on a different connection session. As a result, `@@session.last_gtid` will return an empty string or the GTID of a completely unrelated transaction committed by another goroutine sharing that connection.

To guarantee that the write transaction and the session query run on the exact same physical connection, we must explicitly checkout a dedicated connection from the pool using `db.Conn(ctx)`.

Here is the production-ready Go implementation of the write sequence:

<script src="https://gist.github.com/mohashari/bed07d5cbd417f0a643bcef150c49481.js?file=snippet-1.go"></script>

## Verifying Replica Catch-up via WAIT_FOR_EXECUTED_GTID_SET

Once we have successfully obtained the GTID of our write, we must implement the check on the read replicas.

The function `WAIT_FOR_EXECUTED_GTID_SET` accepts two arguments:
1. `gtid_set`: A list of GTIDs (or a single GTID, which is parsed as a set) that the replica must have executed.
2. `timeout`: A duration in seconds (which can be a fractional value like `0.2` for 200 milliseconds) specifying how long the replica should wait for replication to catch up before giving up.

Calling this function on a lagging replica blocks the Go goroutine and holds the database connection open. While this consumes a pool connection, it consumes no database CPU on the replica because MySQL registers the query in its replication listener queue and wakes it up once the replication thread reaches the target sequence number.

Here is the implementation of the wait block:

<script src="https://gist.github.com/mohashari/bed07d5cbd417f0a643bcef150c49481.js?file=snippet-2.go"></script>

## Building a Smart Read/Write DB Router in Go

With both the write-tracking and replica-waiting mechanics ready, we can combine them into a single, clean database routing component. This component dynamically decides whether to execute a query on a replica or fall back to the primary if replication lag exceeds our threshold.

<script src="https://gist.github.com/mohashari/bed07d5cbd417f0a643bcef150c49481.js?file=snippet-3.go"></script>

## Propagating GTIDs across the Network Boundary

To make the system end-to-end, we must propagate the GTID from our Go API back to the client, and accept it on subsequent requests. This is typically achieved via HTTP headers.

We will define an HTTP middleware that extracts the GTID from a request header (`X-MySQL-GTID`), injects it into the Go `context.Context` (so our handlers can read it), and passes it downstream. If our handlers generate a new GTID during a write, the middleware injects the new GTID into the outgoing HTTP response headers.

<script src="https://gist.github.com/mohashari/bed07d5cbd417f0a643bcef150c49481.js?file=snippet-4.go"></script>

Next, let's look at a concrete HTTP handler implementation utilizing this system to handle both a resource creation endpoint (generating a new GTID) and a read resource list endpoint (waiting for the client-supplied GTID).

<script src="https://gist.github.com/mohashari/bed07d5cbd417f0a643bcef150c49481.js?file=snippet-5.go"></script>

## Defending the Primary: Replica Lag Storms & Fallback Circuit Breaking

While the fallback routing in Snippet 3 ensures consistency, it introduces a major production vulnerability: **cascading primary overload**.

If a read replica lags significantly (e.g., due to a disk I/O bottleneck or replication crash), calling `WAIT_FOR_EXECUTED_GTID_SET` will consistently fail. Our code will handle this by falling back to the primary database for every single read query. 

Under heavy traffic, this immediate redirection can create a storm of read requests that hammers the primary database. The primary, already busy with write traffic, may quickly experience CPU exhaustion, connection pool saturation, and crash. 

To protect the primary, we must rate-limit the fallbacks using a token bucket rate limiter (e.g., using `golang.org/x/time/rate`). If the fallback rate is exceeded, we must reject the fallback and degrade gracefully: either return an error informing the client of replica lag, or force a stale read on the replica.

<script src="https://gist.github.com/mohashari/bed07d5cbd417f0a643bcef150c49481.js?file=snippet-6.go"></script>

## Production Gotchas

Enforcing RYOW using GTIDs is highly effective but requires careful operational tuning. Keep these three production failure modes in mind:

### 1. Connection Pool Exhaustion on Replicas
When a replica lags, incoming read requests specifying a fresh GTID will call `WAIT_FOR_EXECUTED_GTID_SET` and block. This keeps the database connection checked out of the Go application's connection pool.

If your replica timeout is set too high (e.g., 2 seconds) and you receive 1,000 read requests per second, Go's connection pool will quickly hit `MaxOpenConns`. Once the pool is full, new queries will block waiting for a connection to become available, starving your application.

**Mitigation**: Set a very tight timeout for replica catch-up (e.g., 50ms - 200ms). If a replica cannot catch up in 100ms, it is better to trigger the primary fallback rate limiter or serve stale reads than to block client threads and exhaust the application connection pools.

### 2. Multi-Source Replication and Complex GTID Sets
In master-slave topologies that use multi-source replication or active-active primary nodes, the GTID set returned by `@@session.last_gtid` will eventually contain ranges from multiple servers (e.g., `3e11fa47-71ca-11e1-9e33-c80aa9429562:1-12,5b22fb48-72ca-11e1-9f44-d80ab9429563:1-44`).

Your Go parsing code must treat GTIDs as opaque strings representing set collections. Do not attempt to parse a GTID as a single integer sequence; use MySQL's native set comparison functions instead.

### 3. Idle Connection Heartbeats
MySQL connection states can time out if they are idle. If a connection in the pool is idle for too long and you execute `WAIT_FOR_EXECUTED_GTID_SET` on it, the driver might trigger a connection reset error. Ensure your Go driver's `SetConnMaxLifetime` and `SetConnMaxIdleTime` are configured to be lower than MySQL's `wait_timeout` (which defaults to 8 hours but is often set to 60 seconds in production).

## Summary

Enforcing session consistency using GTID tracking provides a robust, developer-friendly model for handling replication lag. By leveraging Go's connection session pinning via `db.Conn` and protecting database clusters from lag-induced storms using rate-limiting, you can deliver a reliable and consistent user experience at scale.