---
layout: post
title: "Distributed Rate Limiting at Scale: Token Bucket vs Sliding Window in Redis and Envoy"
date: 2026-04-01 08:00:00 +0700
tags: [redis, distributed-systems, rate-limiting, envoy, backend]
description: "A production engineer's guide to token bucket vs sliding window rate limiting in Redis and Envoy, with real failure modes and concrete implementation tradeoffs."
image: "https://picsum.photos/1080/720?random=1396"
thumbnail: "https://picsum.photos/400/300?random=1396"
---

Your payment service just got hammered by a retry storm from a misbehaving mobile client: 40,000 requests per second against an endpoint designed for 200. Your database connection pool exhausted in 11 seconds, cascading into a full outage that took 45 minutes to recover. The worst part? You had rate limiting configured — a simple in-memory counter per pod, which meant each of your 20 API server instances allowed 200 req/s independently, effectively permitting 4,000 req/s through. Distributed rate limiting isn't just about counting requests; it's about enforcing coherent, cluster-wide policies across stateless processes that share no memory.

This post covers what actually matters in production: the algorithmic tradeoffs between token bucket and sliding window, how to implement them in Redis without race conditions, and when to push this responsibility into Envoy's rate limit service instead.

## Why In-Process Rate Limiting Fails at Scale

The failure mode above is embarrassingly common. Per-process rate limiting works fine in single-instance deployments, but the moment you scale horizontally, your "200 req/s" limit becomes `200 * N req/s` where N is your replica count — a number that changes with autoscaling.

There are two honest solutions: centralize state in a shared store (Redis), or enforce limits at the edge before traffic fans out (Envoy/API gateway). Both are valid; they're not mutually exclusive. What determines your choice is where your traffic enters the system, your latency budget for the rate limit check itself, and whether your limiting logic needs to be aware of application-level context like user tier or authenticated identity.

Redis-backed rate limiting gives you flexibility and context-awareness at the cost of adding a network hop to every request. Envoy's rate limit service gives you enforcement before application code runs but requires externalizing your policy into a sidecar topology.

## Token Bucket: Bursty Traffic Friendly, But Drift is Real

The token bucket algorithm maintains a counter representing available tokens. Each request consumes one token. Tokens regenerate at a fixed rate up to a maximum capacity. A bucket with capacity 1000 and refill rate 100/s lets a burst of 1000 requests through immediately, then sustains 100/s afterward.

The Redis implementation using atomic Lua scripts is the only correct approach. A read-modify-write done in two separate commands races with concurrent clients:

<script src="https://gist.github.com/mohashari/3cc1d3add876738a2e8d66477bf4070a.js?file=snippet-1.txt"></script>

The `PEXPIRE` call is critical — without it, keys for inactive users accumulate indefinitely. Set TTL to at least 2× the bucket refill time so a fully drained bucket has time to recover before expiring.

One production gotcha: fractional tokens. If your refill rate is 10/s and you check every 50ms, you're adding 0.5 tokens per check. Store tokens as a float, not an integer, or you'll lose precision and users will consistently get fewer requests than their quota allows. This manifests as users at exactly the rate limit experiencing ~30% more rejections than expected.

## Sliding Window: Precise but Memory-Heavy

The fixed window algorithm (counter resets every N seconds) has a notorious edge case: a client can fire 2× the rate limit by sending max requests at the end of one window and max at the start of the next. Sliding window eliminates this by tracking every request timestamp within the window, giving you true rate enforcement.

The Redis sorted set implementation is idiomatic:

<script src="https://gist.github.com/mohashari/3cc1d3add876738a2e8d66477bf4070a.js?file=snippet-2.py"></script>

The memory cost is real. Every request that passes generates a sorted set entry. For 1M requests/minute across 100K users, you're storing 10 entries per user on average — manageable. But if you have high-volume endpoints (webhooks, polling), a single key can accumulate thousands of entries per window. Monitor your Redis memory per key, not just total memory.

A common optimization is sliding window log with counter approximation: instead of storing individual timestamps, store per-second buckets as hash fields. This reduces memory by 60-80% at the cost of ~1s resolution at window boundaries.

## Sliding Window Counter: The Production Compromise

Most production systems don't need millisecond-granular sliding windows. The sliding window counter approximation trades a small error margin for dramatically lower memory and CPU:

<script src="https://gist.github.com/mohashari/3cc1d3add876738a2e8d66477bf4070a.js?file=snippet-3.go"></script>

This is the algorithm behind Cloudflare's rate limiting. The approximation error is bounded: in the worst case, you'll allow up to `limit * (1 + (1/window_duration))` requests — for a 60-second window, about 1.7% over limit. That's acceptable for most use cases and costs two Redis commands instead of potentially thousands of sorted set operations.

## Envoy Rate Limit Service: When to Delegate

If your infrastructure runs on Envoy (Istio, AWS App Mesh, or direct Envoy deployments), the rate limit service moves enforcement upstream of your application entirely. Envoy calls the rate limit service via gRPC before forwarding traffic. Your application never sees rejected requests.

The descriptor-based configuration is expressive but has a learning curve:

<script src="https://gist.github.com/mohashari/3cc1d3add876738a2e8d66477bf4070a.js?file=snippet-4.yaml"></script>

The `descriptors` system composes hierarchically: the most specific matching descriptor wins. This lets you express policies like "free users get 100 req/min globally, but only 10 req/min to the payment endpoint" without application code changes.

The Envoy rate limit service (the reference implementation from Lyft/Envoy) uses Redis under the hood with a fixed window algorithm. If you need sliding window semantics at the Envoy layer, you'll need a custom gRPC service implementing the `RateLimitService` interface. The protocol is simple:

<script src="https://gist.github.com/mohashari/3cc1d3add876738a2e8d66477bf4070a.js?file=snippet-5.txt"></script>

The `hits_addend` field is underused but important: for endpoints where you know the cost upfront (e.g., a bulk operation that will fan out to 50 downstream calls), increment by cost, not count. This prevents request amplification attacks that stay under count-based limits while overwhelming downstream systems.

## Failure Mode Catalog

**Redis unavailability**: Your rate limiter must have a defined behavior when Redis is down. Fail open (allow all traffic) preserves availability but defeats the purpose during an attack. Fail closed (reject all traffic) protects backends but takes down your service. The right answer depends on your threat model. Most payment systems fail closed; most API platforms fail open with aggressive alerting.

**Clock skew**: Distributed systems with NTP drift up to 100ms will cause token buckets and sliding windows to behave inconsistently across nodes. Use Redis server time (`TIME` command) as the authoritative clock source instead of client-side `time.Now()`:

<script src="https://gist.github.com/mohashari/3cc1d3add876738a2e8d66477bf4070a.js?file=snippet-6.txt"></script>

**Key cardinality explosion**: Rate limiting by IP + user_id + endpoint produces `IPs × users × endpoints` keys. For a service with 10K concurrent users, 50 endpoints, and dynamic IP allocation, you can generate 500K+ keys per window cycle. Redis key memory per sorted set entry is ~100 bytes overhead — monitor cardinality actively and consider coarser granularity (rate limit by user tier, not user ID) for low-value endpoints.

**The thundering herd on recovery**: When a rate limit lifts (window resets or tokens refill), all queued clients retry simultaneously. Build exponential backoff with jitter into your client SDKs and return a `Retry-After` header with the exact reset time. Precise retry timing paradoxically causes worse storms than approximate timing with jitter.

## Instrumentation You Actually Need

Rate limiting without observability is security theater. The metrics that matter:

<script src="https://gist.github.com/mohashari/3cc1d3add876738a2e8d66477bf4070a.js?file=snippet-7.py"></script>

Alert on `rate_limit_requests_total{result="denied"} / rate_limit_requests_total > 0.05` sustained for 5 minutes. A spike followed by recovery is an attack being mitigated. A sustained 10% denial rate often means your limit is too tight for your actual traffic patterns, or a legitimate use case (a partner's batch job, a mobile app version with aggressive polling) is hitting limits you didn't account for.

## Choosing Between Them

Token bucket when: you have bursty-but-legitimate traffic patterns (batch jobs, mobile clients coming online after offline periods), you want to allow reasonable bursts without penalizing well-behaved clients, your limit semantics are "sustained rate over time."

Sliding window when: your contract with users is a strict per-window count, you need to prevent the dual-window burst attack, you're in a regulated environment where exceeding stated limits — even briefly — is a compliance violation.

Sliding window counter approximation when: you need sliding window semantics with token bucket-level performance and can tolerate ~2% over-limit error.

Envoy rate limit service when: you're already running Envoy, your limits don't need application context (authenticated user identity, account tier), and you want protection before application code runs.

The correct answer for most production systems at scale is layered: coarse IP-based limits at Envoy to absorb DDoS-level traffic, fine-grained user/tier/endpoint limits in Redis with sliding window counter approximation for accurate enforcement, and circuit breakers at the downstream service level as the last line of defense. None of these alone is sufficient; together, they handle everything from a misconfigured client to a coordinated credential-stuffing attack.
```