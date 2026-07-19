---
layout: post
title: "Designing a Distributed Rate Limiter in Go Using Sliding Window Logs and Redis Cluster"
date: 2026-07-19 08:00:00 +0700
tags: [go, redis, system-design, rate-limiting, architecture]
description: "An in-depth guide to building a scalable, thread-safe distributed rate limiter in Go using sliding window logs and Redis Cluster with Lua scripting."
image: "/images/diagrams/designing-distributed-rate-limiter-go-sliding-window-logs-redis-cluster.svg"
thumbnail: "/images/diagrams/designing-distributed-rate-limiter-go-sliding-window-logs-redis-cluster.svg"
---

Under a sudden surge of API requests—for example, during a ticket sale, a Black Friday flash sale, or a targeted credential stuffing attack—traditional rate limiters using token bucket or leaky bucket algorithms can suffer from burstiness boundaries or fail to enforce strict sliding-window guarantees. If a malicious client or a misconfigured webhook floods the service with 10,000 requests in the last second of a minute window, and another 10,000 in the first second of the next window, the backend experiences a transient spike of 20,000 requests within a two-second interval. In a high-throughput microservices architecture, this can exhaust database connection pools or trigger cascading out-of-memory (OOM) failures in downstream systems. The sliding window log algorithm solves this by maintaining a chronological list of timestamps for each user, allowing us to enforce rate limits on a rolling basis. Implementing this at scale requires Go's high-concurrency primitives combined with Redis Cluster's distributed storage, slot-aware pipelining, and transactional multi-commands.

![Designing a Distributed Rate Limiter in Go Using Sliding Window Logs and Redis Cluster Diagram](/images/diagrams/designing-distributed-rate-limiter-go-sliding-window-logs-redis-cluster.svg)

## The Scaling Failure Modes of Traditional Rate Limiters

Before diving into the sliding window log implementation, we must understand the architectural compromises inherent in alternative algorithms like Token Bucket, Leaky Bucket, and Fixed Window.

### 1. Token Bucket Burstiness
Token bucket rate limiters allow a maximum burst of requests up to the bucket's capacity. While this is useful for accommodating sudden but legitimate spikes in traffic, it can be devastating when protecting downstream legacy databases (e.g., PostgreSQL with a hard-capped connection pool of 50 connections). If a client has a bucket capacity of 100, they can execute 100 requests concurrently within 1 millisecond. In high-concurrency systems, this burst passes through the gateway unmodified, causing database thread starvation or lock contention.

### 2. Leaky Bucket Queue Latency
The leaky bucket algorithm smoothens traffic by queuing incoming requests and processing them at a constant, uniform rate. However, queueing introduces significant latency. For user-facing REST or gRPC APIs requiring sub-100ms response times, holding requests in a gateway-level buffer degrades the user experience. Under sustained overload, the queue fills up, forcing the gateway to drop requests anyway, but only after wasting memory and holding client connections open.

### 3. Fixed Window Boundary Bursting
Fixed-window algorithms segment time into static blocks (e.g., 1-minute blocks). The rate limit counter is reset at the start of each block. The primary weakness of this approach is the "double-limit burst" at the boundary. A client limited to 100 requests per minute can send 100 requests at `11:59:59` and another 100 requests at `12:00:01`. The backend is forced to process 200 requests within a 2-second window, violating the intended rate limit of 1.6 requests per second.

### 4. Sliding Window Log to the Rescue
The sliding window log algorithm provides strict enforcement. Instead of incrementing a static counter, it tracks the exact timestamp of every request. When a request arrives, the algorithm evicts all timestamps older than `now - window_size`. The request is allowed only if the remaining log size is less than the limit. This guarantees that at no point in time will a client exceed the limit within *any* arbitrary rolling window.

The primary disadvantage is memory consumption. Storing individual timestamps in a distributed cache like Redis requires significantly more space than a single counter. We must design our data structures to minimize this overhead.

## Mechanics of the Sliding Window Log

In Redis, the sliding window log is represented using a Sorted Set (ZSET). The ZSET keys are structured to target specific clients, using a naming pattern such as `{tenant_id}:ratelimit`. The score and the member of the ZSET are used strategically to maintain the log:
* **Score**: The Unix epoch timestamp with millisecond precision.
* **Member**: A unique value. If we used the timestamp as both the member and the score, concurrent requests arriving at the exact same millisecond would overwrite each other due to ZSET's member uniqueness guarantee, leading to under-counting. To solve this, we generate a unique member by appending a random suffix or a UUID to the timestamp.

To enforce the rate limit, the client performs the following steps in Redis:
1. **Eviction**: Remove all members with scores less than `now - window_size` using `ZREMRANGEBYSCORE`.
2. **Cardinality Check**: Retrieve the number of remaining elements using `ZCARD`.
3. **Evaluation**: If `ZCARD` is less than the limit, append the current request's timestamp using `ZADD` and set the TTL on the key using `EXPIRE` to clean up inactive keys. Otherwise, reject the request.

## The Pipelined Implementation and Its Bottlenecks

Let's begin by implementing this naive approach in Go using a Redis Pipeline. A pipeline combines multiple commands into a single network round-trip, which significantly reduces network latency.

<script src="https://gist.github.com/mohashari/e86ffef7bee38a68f76ee69fa045322d.js?file=snippet-1.go"></script>

### Critical Flaws in the Pipelined Approach:
1. **Cross-Slot Errors in Redis Cluster**: Redis Cluster partitions key-value pairs across 16,384 slots. Transactions (`MULTI`/`EXEC` or `TxPipeline`) can only execute keys that reside on the same cluster node. If our middleware doesn't enforce hash tags (e.g., placing curly braces around the sharding key, like `{user_123}:ratelimit`), `go-redis` will route commands to different nodes, returning a `CROSSSLOT Keys in request don't hash to the same slot` error.
2. **Race Conditions (Check-Then-Act)**: A pipeline is not atomic on the server side unless wrapped in a transaction block. However, even with `TxPipeline` (which uses `MULTI`/`EXEC`), we cannot inspect the output of `ZCARD` *during* the pipeline's execution to decide whether we should perform the `ZADD`. In Snippet 1, the `ZADD` executes regardless. If the user exceeds the rate limit, we must manually prune their over-limit entries, or risk filling the ZSET with rejected requests, creating a denial-of-service vector where a blocked user blocks themselves indefinitely.

## Atomicity via Lua Scripting

To solve the check-then-act race condition, we must move the decision logic directly to the Redis server. Redis executes Lua scripts natively and atomically within its single-threaded execution context. This guarantees that no other commands can run between our read (`ZCARD`) and write (`ZADD`) operations.

Here is the production-grade Lua script for the sliding window log rate limiter:

```lua
-- snippet-2
-- KEYS[1]: Rate limit key, e.g., "{usr_99a2}:ratelimit"
-- ARGV[1]: Current timestamp (Unix milliseconds)
-- ARGV[2]: Window size (milliseconds)
-- ARGV[3]: Max limit allowed
-- ARGV[4]: Unique member identifier (timestamp + random string)

local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local member = ARGV[4]
local clear_before = now - window

-- 1. Remove all old requests outside the current rolling window
redis.call('ZREMRANGEBYSCORE', key, '-inf', clear_before)

-- 2. Fetch the number of requests in the current window
local current_requests = redis.call('ZCARD', key)

-- 3. Determine if the request exceeds the rate limit
if current_requests < limit then
    -- Add the request to the log
    redis.call('ZADD', key, now, member)
    -- Set/Reset the TTL to ensure memory is reclaimed when the user becomes inactive
    -- The TTL is converted from milliseconds to seconds (rounded up)
    redis.call('EXPIRE', key, math.ceil(window / 1000))
    
    -- Return allowed=1 and remaining tokens
    return {1, limit - current_requests - 1}
else
    -- Return allowed=0 and 0 remaining tokens
    return {0, 0}
end
```

By keeping the execution inside Redis, we eliminate the network round-trips for the check-then-act logic. Let's write the Go struct that loads and executes this Lua script.

<script src="https://gist.github.com/mohashari/e86ffef7bee38a68f76ee69fa045322d.js?file=snippet-3.go"></script>

## Go Middleware Architecture for HTTP Handlers

To apply this rate limiting logic across our API endpoints, we wrap the limiter in an HTTP middleware. The middleware extracts the client's identifier (either a validated JSON Web Token tenant ID or the remote IP address), formats it with Redis hash tags, invokes the limiter, and configures standard HTTP rate-limiting headers.

<script src="https://gist.github.com/mohashari/e86ffef7bee38a68f76ee69fa045322d.js?file=snippet-4.go"></script>

## Redis Cluster Slot Routing and Topology Failures

When running a Redis Cluster in production (such as AWS ElastiCache, GCP Memorystore, or a self-hosted stateful Kubernetes cluster), operations are distributed across multiple shards.

### Cluster Slot Mapping and Routing
Redis uses 16,384 slots. Each cluster master node is responsible for a subset of these slots. When using the `go-redis` library, the `ClusterClient` maintains an in-memory routing table mapping slots to nodes. When a query is initiated, `go-redis` hashes the key (or the bracketed hash tag portion of the key) using the CRC16 algorithm:
$$\text{slot} = \text{CRC16}(key) \pmod{16384}$$
It then dispatches the TCP packet directly to the responsible master node.

### Production Routing Errors
During cluster topology changes (e.g., node failover, scaling up, or slot rebalancing), slots migrate between nodes. During migrations, a node may return:
* **`MOVED slot node-ip:port`**: Tells the client that the slot has permanently moved. The `go-redis` client automatically intercepts this, updates its internal slot routing map, and retries the query on the target node.
* **`ASK slot node-ip:port`**: Tells the client that the slot is temporarily located on another node during an active migration. The client must execute an `ASKING` command on the destination node first, then retry the query.

To survive these transient routing states without dropping HTTP requests, we must optimize the connection pool configuration, implement strict timeouts, and integrate circuit-breakers.

<script src="https://gist.github.com/mohashari/e86ffef7bee38a68f76ee69fa045322d.js?file=snippet-5.go"></script>

## Resilience: Local Fallback Limiter

If the Redis Cluster experiences a total partition, network partition, or a failover that lasts longer than the configured timeouts, our Go API Gateway will fall back to its fail-open state. In high-load systems, this exposes downstream resources directly to attacks.

To mitigate this risk, we can use a hybrid fallback pattern: when Redis is unreachable, we fall back to an in-memory token bucket rate limiter tracking requests locally on a per-instance basis. While this doesn't enforce global limits accurately, it prevents a single client from overwhelming an application instance during a caching tier failure.

<script src="https://gist.github.com/mohashari/e86ffef7bee38a68f76ee69fa045322d.js?file=snippet-6.go"></script>

## Production Metrics, Memory Overhead, and Redis Tuning

Deploying a sliding window log rate limiter changes the resource profile of your Redis instances. Because we store individual timestamp entries inside a ZSET instead of a single counter, memory usage grows linearly with the volume of concurrent requests.

### Memory Overhead Calculation
Each element in a Redis Sorted Set consumes memory for:
* The member string (e.g., `"1721347140012:abcdefgh"` $\approx 24$ bytes)
* The score (double-precision floating-point number $\approx 8$ bytes)
* Pointer overhead inside the internal skip list and hash table data structures.

In Redis, a ZSET with a small number of elements is optimized using a compact layout: **Listpack** (or **Ziplist** in older versions). When the size of a ZSET is smaller than the configuration parameter `zset-max-listpack-entries` (default: 128) and the members' lengths are smaller than `zset-max-listpack-value` (default: 64), Redis bypasses the memory-heavy skip list structure and stores the entries sequentially in a contiguous array.
* **Listpack representation**: ~40 bytes per entry.
* **Skip list/Hash table representation**: ~120 bytes per entry.

For a tenant limited to $100$ requests per minute, the ZSET size will stay below $100$. This fits within the default listpack limits, using about $4$ KB of memory.
For $100,000$ active tenants, the total memory consumption is around $400$ MB. However, if a tenant has a limit of $2,000$ requests per minute, the ZSET will scale up to the skip list structure, consuming around $240$ KB per tenant. For $100,000$ active tenants, this requires $24$ GB of memory.

### Redis Eviction Policy Configuration
Under high memory pressure, Redis may hit its memory ceiling. If your Redis Cluster is shared with other applications (e.g., acting as a general-purpose cache), you must configure your memory eviction policy carefully:
* **`maxmemory-policy noeviction`**: (Recommended) When memory is full, Redis returns an out-of-memory error for write operations (`OOM command not allowed when used memory > 'maxmemory'`). This causes our limiter to fail open (or fall back to the in-memory limiter), protecting database resources.
* **`maxmemory-policy allkeys-lru` / `volatile-lru`**: Redis evicts rate limiter keys before their TTL has expired. This can cause clients to bypass their rate limits.

To monitor your rate limiters in production, you should track these core metrics:
1. **Redis Latency (`command_duration_seconds`)**: Monitor the execution latency of the Lua script. The p99 latency should remain under 5ms.
2. **Rejection Rate (`rate_limit_rejections_total`)**: Track the rate of `429 Too Many Requests` responses. A sudden spike indicates a client error, script issue, or a potential DDoS attack.
3. **Fallback Activations (`rate_limit_fallback_activations_total`)**: Monitor how often your application instances fall back to the local in-memory limiter, which serves as a metric for Redis Cluster network partition alerts.