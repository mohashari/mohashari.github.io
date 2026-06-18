---
layout: post
title: "Mitigating Redis Cluster Slot Resharding Latency Spikes: Client Redirection and Pipelining Patterns"
date: 2026-06-18 08:00:00 +0700
tags: [redis, distributed-systems, performance, caching]
description: "Eliminate Redis Cluster resharding latency spikes by mastering slot-aligned pipelining, redirection mechanics, and singleflight cache refresh."
image: "https://picsum.photos/seed/redis-reshard/1080/720"
thumbnail: "https://picsum.photos/seed/redis-reshard/400/300"
---

It is 2:00 PM on Black Friday, your system is humming along at 150,000 queries per second, and you decide to scale out your Redis Cluster from 16 to 32 shards to absorb the evening surge. Ten minutes into the resharding process, your pager triggers. Your API gateway’s p99 latency has exploded from 2.5 milliseconds to 1,200 milliseconds, triggering downstream circuit breakers and causing a cascade of HTTP 504 Gateway Timeouts. Despite your Redis instances showing healthy CPU and memory utilization, your application log is flooded with `ASK` and `MOVED` redirection errors, and connection pool queues are saturated. This is the slot resharding latency trap: during keyspace migration, naive clients serialize pipelined batches, trigger slot cache stampedes, and saturate connection pools, transforming a routine cluster scaling operation into a major production outage.

![Mitigating Redis Cluster Slot Resharding Latency Spikes: Client Redirection and Pipelining Patterns Diagram](/images/diagrams/redis-cluster-slot-resharding-latency-client-redirection.svg)

## The Anatomy of Redis Cluster Partitioning and Migration

To understand the breakdown, we must first look at how Redis Cluster manages data distribution. The database keyspace is divided into exactly 16,384 logical partitions called hash slots. When a client performs any operations on a key, it determines the slot by running the key through a CRC16 hashing algorithm and applying a modulo operation:

$$\text{slot} = \text{CRC16}(key) \pmod{16384}$$

For keys containing curly braces (hash tags), such as `{user:1000}:profile` and `{user:1000}:orders`, only the substring inside `{}` is hashed. This allows backend engineers to force related records onto the same hash slot and node, ensuring multi-key operations (like MGET, transactional execution, or Lua scripts) succeed in a shared context.

During a cluster scale-out or scale-in, slots are moved between shards to balance load. This resharding is orchestrated by a coordinator tool (such as `redis-cli --cluster reshard`) and executes in a distinct, transitional sequence for each slot:

1. **State Initialization**: The coordinator transitions the source node’s slot state to `MIGRATING` (informing it that keys are leaving for a target node). Simultaneously, the target node’s slot state is set to `IMPORTING`.
2. **Key Migration Loop**: The coordinator queries a batch of keys from the source node using `CLUSTER GETKEYSINSLOT <slot> <count>`. It then migrates them using the `MIGRATE` command. The `MIGRATE` operation is a blocking, transaction-like command that serializes keys, transmits them to the target node, verifies receipt, and deletes them from the source node.
3. **Slot Finalization**: Once the slot is empty, the coordinator broadcasts a `CLUSTER SETSLOT <slot> NODE <target_node_id>` command to all cluster nodes, updating the official routing table.

Because migration operates incrementally, a hash slot exists in a hybrid state for minutes at a time. This transitional state introduces severe complexity for clients attempting to read or write keys within migrating slots.

## Redirection Mechanics: MOVED vs. ASK

During slot migration, clients cannot rely entirely on their cached local slot mapping. If a client attempts to query a key in a migrating slot, it will encounter either a `MOVED` or `ASK` redirection response depending on the exact location of that key.

### The MOVED Redirection

A `MOVED` redirection is a permanent error. It indicates that the slot has been completely transferred to another node, but the client’s local cache is stale. 

```
Client -> Node A (GET user:100)
Node A -> Client (-MOVED 100 10.0.0.2:6379)
```

Upon receiving `-MOVED 100 10.0.0.2:6379`, the client must:
1. Invalidate its local routing cache.
2. Re-route the current command to `10.0.0.2:6379`.
3. Rebuild the slot topology cache in the background to avoid future redirections.

### The ASK Redirection

An `ASK` redirection is a transient error. It indicates that the slot is in a migrating state, and the key being queried has already been moved to the target node, but other keys in the same slot still reside on the source node.

```
Client -> Node A (GET user:100)
Node A -> Client (-ASK 100 10.0.0.2:6379)
```

Upon receiving `-ASK 100 10.0.0.2:6379`, the client must:
1. Keep the local slot mapping unchanged (future queries for this slot must still default to Node A).
2. Establish or fetch a connection to Node B (`10.0.0.2:6379`).
3. Send an `ASKING` command to Node B, followed immediately by the actual query.
4. Execute the command. The `ASKING` command acts as a one-time bypass on Node B. Without the `ASKING` prefix, Node B would reject the command with a `MOVED` error because Node B does not yet officially own that slot.

The latency penalty of these redirections is severe. A standard key retrieval requires 1 RTT (Round Trip Time) between the application client and Redis. If an `ASK` redirection occurs, it requires:
1. 1 RTT to query the source node.
2. 1 RTT to receive the redirection error.
3. 1 RTT to send `ASKING` and the query to the target node.
4. 1 RTT to receive the payload from the target.

This turns a single RTT into a minimum of 3 RTTs. Under peak load, if connection pooling is not tuned correctly, establishing the connection to Node B adds TCP and TLS handshakes, inflating latency to over 100ms.

## Why Pipelining Shatters Under Resharding

Redis pipelining is a critical optimization pattern. It allows clients to batch dozens of commands together and send them over a single socket write, bypassing the network RTT penalty for each individual key. Under normal cluster operations, clients sort keys by hash slot and target the pipeline at the node owning those slots.

However, during slot resharding, this optimization breaks down. Consider a pipeline of 50 `GET` commands sent to Node A for keys belonging to slot 100. If slot 100 is undergoing migration, and 5 of those keys have already moved to Node B, Node A’s pipeline response buffer will look like this:

```
[0] => "value_0"
[1] => "value_1"
...
[10] => "-ASK 100 10.0.0.2:6379"
...
[25] => "-ASK 100 10.0.0.2:6379"
```

A naive Redis client library handles this scenario in one of three disastrous ways:
1. **Total Batch Failure**: The library encounters an error in the multi-bulk response and throws a top-level runtime exception, failing all 50 commands even though 45 succeeded on the node.
2. **Sequential Fallback Retry**: The library catches the redirection error, invalidates the connection, and retries all 50 commands sequentially. This converts a single pipeline round-trip into 50 individual RTT network calls, causing a latency spike that starves worker pools.
3. **Unresolved Redirections**: The library returns the raw `-ASK` or `-MOVED` error string to the application level, resulting in data pollution or application-level logic crashes.

To mitigate this in high-performance architectures, the client must use a migration-aware pipelining pattern that dynamically groups, parses, and retries redirected keys without breaking the pipeline boundary or degrading into sequential requests.

## Implementing Migration-Aware Pipelining

A robust Go implementation of a migration-aware Redis client must parse key slot groups, handle multi-bulk pipeline returns, and route redirections in parallel.

First, we must calculate the exact slot hash, handling curly-brace tags to maintain key alignment:

<script src="https://gist.github.com/mohashari/e515fb33381e929190827bfd84b9bf4b.js?file=snippet-1.go"></script>

Next, our client groups commands before sending them. This prevents cross-slot command serialization errors:

<script src="https://gist.github.com/mohashari/e515fb33381e929190827bfd84b9bf4b.js?file=snippet-2.go"></script>

We must process raw Redis protocol errors inside the bulk response. Redirection errors appear directly inside the pipeline arrays as strings:

<script src="https://gist.github.com/mohashari/e515fb33381e929190827bfd84b9bf4b.js?file=snippet-3.go"></script>

Now, the coordinator pattern splits pipelines by node, fires them concurrently using goroutines, collects results, and retries redirected keys:

<script src="https://gist.github.com/mohashari/e515fb33381e929190827bfd84b9bf4b.js?file=snippet-4.go"></script>

## Eliminating the Slot Cache Stampede

When a slot is finalized, the source node begins returning `-MOVED` errors for all incoming queries on that slot. Under highly concurrent workloads, hundreds or thousands of concurrent application threads will receive a `-MOVED` error simultaneously.

If every application goroutine immediately invokes the `CLUSTER SLOTS` command to rebuild the local cache, it causes a cache stampede:

1. **Redis Node Overload**: The `CLUSTER SLOTS` command is expensive. The Redis process must build and serialize the entire cluster topology. In a cluster with 100 master nodes, serializing this payload thousands of times in a short window spikes Redis CPU usage to 100%, choking key processing.
2. **Network Saturation**: The topology description payload is several kilobytes. Executing this concurrently across hundreds of application containers saturates backend switch links.
3. **Cascading Failures**: The CPU spike on Redis slows down slot migrations, which leads to further timeouts, more redirections, and additional cache updates.

To prevent this, clients must implement a **Singleflight Pattern** to deduplicate concurrent cache updates, rate-limiting the topology requests to a maximum of one request per update cycle.

<script src="https://gist.github.com/mohashari/e515fb33381e929190827bfd84b9bf4b.js?file=snippet-5.go"></script>

## Designing Resilient Connection Pools and Timeout Budgets

During slot migration, your connection profile shifts dynamically. Naive configurations optimize for a static state where a service instance communicates heavily with a limited set of master nodes. When slot resharding starts, clients are forced to talk to target nodes that they rarely accessed before.

This behavior exposes two connection configuration failure modes:

1. **Connection Pool Starvation**: When requests are redirected, the client attempts to acquire connections to the target node. If connection pool limits are set too low (or connection pre-warming is disabled), application threads block waiting for a connection from the pool. This wait time consumes client timeout budgets before the command even hits the wire.
2. **Aggressive Connection Timeout Cascades**: If you configure a low Dial Timeout (e.g. 5ms) to catch system glitches, establishing a new connection to a target node under heavy system load will fail. This failure leads to retries, and the resulting connection setup queues block client worker pools.

For high-throughput setups, configure the connection pool with pre-warmed idle connections across all cluster shards, and provide enough read-timeout headroom to absorb the 2x-3x RTT penalty of redirections.

<script src="https://gist.github.com/mohashari/e515fb33381e929190827bfd84b9bf4b.js?file=snippet-6.go"></script>

## Monitoring and Resharding Runbook Best Practices

Even with optimized clients, operational configuration of the resharding process is critical to preventing latency cascades.

### Resharding Bandwidth and Speed Control

Never execute resharding at maximum speed. By default, `redis-cli --cluster reshard` migrates slots continuously, which can lock up single-threaded Redis processes.

*   **Throttle Chunk Size**: Set the key batch size per migration step low. In high-traffic scenarios, migrate no more than 100 keys per batch. This minimizes the duration the slot is locked during `MIGRATE` execution.
*   **Introduce Delays**: Use a tool or wrapper script to inject a sleep (e.g., 50–100ms) between migration batches. This gives Redis CPU cycles to process backlogged read/write traffic.

### Critical Metrics to Monitor

Set up alerting pipelines for client-side and server-side metrics to catch degradation early:

*   **Redirection Spikes**: Track the rates of `redis.commands.redirections.ask.count` and `redis.commands.redirections.moved.count` on your client side. A sharp rise in `ASK` counts indicates ongoing migration, while a persistent rise in `MOVED` errors without returning to zero indicates a broken slot topology cache update pipeline.
*   **Connection Pool Wait Time**: Monitor client-side pool wait durations (`redis.connection.pool.wait_duration`). A spike indicates that the pool size is insufficient for the dynamic redirection routing.
*   **Redis CPU Engine Stats**: Monitor `used_cpu_sys` and `used_cpu_user` on Redis. Compare these against `cmdstat_cluster` execution times. If `cmdstat_cluster` is elevated, singleflight deduplication on the client side is failing.
*   **Engine Command Latency**: Keep an eye on `cmdstat_migrate` response times on the source nodes. If this metric exceeds 50ms, network path limits or massive payload allocations for single keys (e.g. hash sets or sorted sets with >100,000 members) are causing blocking behavior.

By optimizing your client routing pipelines, protecting against cache update stampedes, and throttling slot migration scripts, you can safely scale out your Redis Cluster in production without triggering latency spikes or impacting active workloads.