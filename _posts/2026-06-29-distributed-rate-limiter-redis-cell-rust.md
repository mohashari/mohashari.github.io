---
layout: post
title: "Designing a High-Throughput Distributed Rate Limiter Using Redis Cell and Rust"
date: 2026-06-29 08:00:00 +0700
tags: [rust, redis, rate-limiting, system-design]
description: "An in-depth guide to building a resilient, low-latency distributed rate limiter using the GCRA engine in Redis Cell and asynchronous Rust."
image: "/images/diagrams/distributed-rate-limiter-redis-cell-rust.svg"
thumbnail: "/images/diagrams/distributed-rate-limiter-redis-cell-rust.svg"
---

Imagine an early Monday morning when an uncoordinated botnet starts scraping your `/api/v1/checkout` endpoint at 85,000 requests per second. Within 12 seconds, your PostgreSQL database transaction pool is fully saturated. Threads start queuing, your backend servers run out of socket descriptors, health checks fail, and NGINX begins throwing 502 Bad Gateway responses to legitimate, paying customers. Distributed rate limiting is your system's first line of defense against this flavor of catastrophic cascade failure. However, traditional implementations utilizing standard Redis Lua scripts or sorted sets become performance bottlenecks themselves under extreme load, causing Redis CPU usage to hit 100% and introducing multi-millisecond queuing latencies. To solve this, we must build a high-throughput, low-overhead distributed rate limiter using the Generic Cell Rate Algorithm (GCRA) via the Redis Cell module, combined with an asynchronous, type-safe Rust application layer.

![Designing a High-Throughput Distributed Rate Limiter Using Redis Cell and Rust Diagram](/images/diagrams/distributed-rate-limiter-redis-cell-rust.svg)

## The Concurrency Bottlenecks in Traditional Distributed Rate Limiters

Most distributed rate limiters deployed in production rely on one of three common Redis-based strategies. Each of them degrades significantly under high-throughput conditions.

1. **Token Bucket via HINCRBY / MULTI-EXEC**: This method stores token counts and timestamps in Redis hashes. To check a limit, the application fetches the key, calculates token replenishment in the application layer, and writes the updated values back. Under high concurrency, executing these steps via transactions (`MULTI/EXEC`) leads to high abort rates due to optimistic locking conflicts. Abandoning transactions and using non-atomic writes introduces race conditions where clients bypass the limit entirely.
2. **Sliding Window Log via Sorted Sets (ZSET)**: To enforce exact rate limits, developers write each request's timestamp into a Redis sorted set using `ZADD` and remove expired logs using `ZREMRANGEBYSCORE`. While highly accurate, this is a memory hog. If a single IP address sends 1,000 requests per minute, storing 1,000 elements in a sorted set consumes approximately 30 kilobytes of RAM. Multiply this by 200,000 active IP addresses, and Redis will quickly run out of memory (OOM) or trigger aggressive eviction policies. Furthermore, executing `ZREMRANGEBYSCORE` on every request introduces $O(\log N + M)$ time complexity, leading to CPU starvation.
3. **Custom Lua Scripting**: Lua scripts solve race conditions by executing token-bucket math atomically inside the Redis engine. However, because Redis is single-threaded, executing complex Lua math (including floating-point divisions and ceiling operations) for 50,000+ requests per second blocks the event loop. The resulting queuing delay inflates database latency across the entire system, degrading unrelated cache operations.

To handle high-volume systems without these trade-offs, we must transition to a rate-limiting algorithm that maintains atomic execution with minimal memory and processing overhead.

## The Math Behind GCRA and Redis Cell

The Generic Cell Rate Algorithm (GCRA) is a rate-limiting algorithm originally designed for traffic shaping in Asynchronous Transfer Mode (ATM) telecommunication networks. It is a highly optimized variant of the leaky bucket algorithm.

Unlike traditional token buckets, which require calculating token generation relative to the current timestamp on every request, GCRA schedules when the next request is *allowed* to arrive. It does this by tracking a single timestamp value called the **Theoretical Arrival Time (TAT)**.

When a request arrives at time $t$, the algorithm evaluates:
- **Emission Interval ($T$)**: The time offset representing the inverse of our rate limit. For instance, if the limit is 100 requests per 60 seconds, then $T = 60 / 100 = 0.6$ seconds.
- **Limit ($L$)**: The maximum burst allowance. If we allow a burst of 20 requests, then $L = T \times (20 + 1) = 12.6$ seconds.

The algorithm checks if the request is conforming:
1. If the current arrival time $t$ is less than $TAT - L$, the request arrives too early and is rejected.
2. If $t \ge TAT - L$, the request is accepted. We calculate the new $TAT$ as:
   $$\text{New } TAT = \max(t, TAT) + T$$

Because GCRA only needs to store the $TAT$ value in Redis, it occupies a tiny memory footprint—typically around 80 bytes per key. It requires no sorted sets, no list operations, and no background cleanup cron jobs.

Redis Cell wraps this algorithm inside a single Rust-compiled module, exposing the `CL.THROTTLE` command:

```bash
CL.THROTTLE <key> <max_burst> <count> <period> [<quantity>]
```

The arguments are:
- `key`: The rate limit identifier (e.g., `rate_limit:user_1234`).
- `max_burst`: The maximum burst capacity (minus 1).
- `count` & `period`: The steady-state rate (e.g., 30 requests per 60 seconds).
- `quantity`: The number of tokens required for this request (defaults to 1).

The command returns an array of five integers:
1. `allowed`: `0` if allowed, `1` if blocked.
2. `limit`: The absolute capacity of the rate limiter (burst capacity + 1).
3. `remaining`: The remaining requests allowed in the current burst window.
4. `retry_after`: The number of seconds the client must wait before retrying (returns `-1` if allowed).
5. `reset_after`: The number of seconds until the rate limiter resets to full capacity.

## Building the Core Rust Architecture

To leverage this engine under high loads, our application tier must handle network connections efficiently. We will build our gateway layer in Rust using the `axum` web framework and the `bb8-redis` asynchronous connection pool. Unlike blocking pools like `r2d2`, `bb8` uses Tokio's non-blocking I/O, allowing a single thread to multiplex multiple queries to Redis without blocking the executor.

First, let's configure the asynchronous Redis connection pool manager:

<script src="https://gist.github.com/mohashari/7169ac580143f04741e43b7c8fc4ce08.js?file=snippet-1.txt"></script>

Next, we write the client query layer to execute the low-level `CL.THROTTLE` command and deserialize the bulk array response:

<script src="https://gist.github.com/mohashari/7169ac580143f04741e43b7c8fc4ce08.js?file=snippet-2.txt"></script>

## Hooking the Rate Limiter into Axum Middleware

To intercept API traffic, we construct a custom Axum middleware. The middleware identifies the user (using their client IP or API key), queries Redis Cell, and determines whether to allow the request. 

We will return standard, RFC-compliant rate limit headers:
- `X-RateLimit-Limit`: Maximum requests allowed per window.
- `X-RateLimit-Remaining`: Requests left in the current window.
- `X-RateLimit-Reset`: Time in seconds until the current window resets.
- `Retry-After`: If blocked, tells the client how long to wait (in seconds).

<script src="https://gist.github.com/mohashari/7169ac580143f04741e43b7c8fc4ce08.js?file=snippet-3.txt"></script>

## Resiliency: Hybrid Fail-Open with Local Fallback

If your Redis node goes down, how should your application respond? 
- **Fail-Open**: Let all traffic pass. This keeps your API functional, but exposes downstream databases to instant connection exhaustion if a traffic spike occurs during the outage.
- **Fail-Closed**: Block all traffic. This protects your database, but turns a Redis outage into a complete API outage.

A better solution is a **hybrid approach**. If Redis is unreachable, we fall back to a thread-safe, local, in-memory token bucket limiter. This fallback allows traffic to flow but caps the throughput of individual application nodes to prevent them from overwhelming downstream systems. We'll use a `DashMap` to store our local buckets, ensuring high-concurrency read and write operations.

<script src="https://gist.github.com/mohashari/7169ac580143f04741e43b7c8fc4ce08.js?file=snippet-4.txt"></script>

## Managing Multi-Tenant Limits dynamically

In production, rate limits aren't static. Different API tiers (e.g., Free, Standard, Premium) require different rate limit configurations. Let's design a dynamic configuration schema using `serde` to handle this mapping:

<script src="https://gist.github.com/mohashari/7169ac580143f04741e43b7c8fc4ce08.js?file=snippet-6.txt"></script>

## Integration Testing under Tokio

To guarantee our rate limiter runs correctly against a live Redis instance loaded with Redis Cell, we write a Tokio integration test:

<script src="https://gist.github.com/mohashari/7169ac580143f04741e43b7c8fc4ce08.js?file=snippet-5.txt"></script>

## Production Operational Realities & Failure Modes

Running a high-throughput rate limiter in production requires understanding how Redis behaves under stress. Pay attention to the following configurations:

### 1. Redis Memory Management and Eviction
Redis Cell automatically sets a Time-To-Live (TTL) on each key, calculated using the `reset_after` response value. When a bucket is full and inactive, the key expires, freeing up memory. 
However, if Redis memory runs out due to system-wide data growth, you must ensure your rate limiting keys are not prematurely evicted. If Redis evicts your rate limiter keys, the user's bucket state is lost, allowing them to bypass the rate limits.
To prevent this, set your Redis `maxmemory-policy` to `volatile-lru` or `volatile-ttl`. This configuration ensures that only keys with an active TTL are eligible for eviction, protecting your persistent cache keys.

### 2. Redis Cluster and Hash Tags
Redis Cluster shards keys across 16,384 slots. If you request a multi-key transaction or run scripts that use different key names, Redis Cluster will throw a cross-slot error if the keys hash to different nodes. 
Because `CL.THROTTLE` relies on single keys, you must ensure that key names are grouped into the same slot using **Redis Hash Tags**. Wrap the unique user identifier in curly brackets:
- Incorrect: `rate_limit:user_1234:endpoint_checkout`
- Correct: `rate_limit:{user_1234}:endpoint_checkout`

This forces Redis to hash only the content inside the curly brackets (`user_1234`), ensuring that all limits for that user reside on the same physical shard.

### 3. Network Latency & Timeouts
A rate limiter should protect downstream databases, not slow down your HTTP traffic. If querying Redis introduces 15ms of latency, your rate limiter has failed.
Configure a strict timeout in your connection manager. In our Rust client, set:
- `connect_timeout` to `20ms`
- `read_timeout` to `10ms`

If Redis takes longer than 10ms to respond, the connection pool throws an error, triggering our local in-memory fallback. This isolates your application from Redis latency spikes.

## Conclusion

By offloading rate-limiting math to the GCRA-powered Redis Cell module, we minimize CPU usage on our database nodes and scale out our rate-limiting layer. When combined with Rust's asynchronous execution model and in-memory fallback routing, this architecture guarantees that your services can handle millions of requests while protecting your systems from sudden traffic spikes.