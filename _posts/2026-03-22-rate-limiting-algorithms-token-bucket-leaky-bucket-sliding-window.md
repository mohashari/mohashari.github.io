---
layout: post
title: "Rate Limiting Algorithms: Token Bucket, Leaky Bucket, Sliding Window"
date: 2026-03-22 08:00:00 +0700
tags: [backend, distributed-systems, redis, performance, api-design]
description: "How to choose between token bucket, leaky bucket, and sliding window rate limiting — with real tradeoffs, Redis implementations, and a decision framework."
---

You deploy a new API endpoint, write a quick fixed-window rate limiter with Redis INCR, and call it done. Three weeks later your on-call gets paged at 2am: a single enterprise customer fires 1000 requests in the first millisecond of every minute, slamming the database with a coordinated burst that your fixed-window counter resets exactly never protects against. The counter hit zero at 00:00.000, the requests came in at 00:00.001, and the DB melted. You didn't choose the wrong _implementation_ — you chose the wrong _algorithm_ for the access pattern. This post is about making that choice deliberately.

![Rate Limiting Algorithms: Token Bucket, Leaky Bucket, Sliding Window Diagram](/images/diagrams/rate-limiting-algorithms-production.svg)

## The Fixed Window Problem Nobody Talks About

Before dissecting the three main algorithms, it's worth naming why naive fixed-window counters fail so reliably. A fixed window allows 2× the intended limit at the seam of two windows. If your limit is 100 req/min and a client sends 100 requests at 00:59 and 100 more at 01:01, both windows see exactly 100 — never exceeded. The downstream service just absorbed 200 requests in two seconds.

This is not an edge case. Cron jobs, batch processors, and any client with retry logic will naturally cluster at window boundaries. Every algorithm below was invented specifically to address this failure mode in different ways.

## Token Bucket: Burst-Tolerant Throttling

The token bucket algorithm maintains a counter (tokens) that refills at a fixed rate up to a maximum capacity. Each request consumes one token. When tokens run out, requests are rejected.

```go
// snippet-1
package ratelimit

import (
	"context"
	"fmt"
	"time"

	"github.com/redis/go-redis/v9"
)

// TokenBucket implements a distributed token bucket using a Lua script
// for atomic read-modify-write. The script runs in a single Redis command,
// so no WATCH/MULTI/EXEC needed.
var tokenBucketScript = redis.NewScript(`
local key       = KEYS[1]
local capacity  = tonumber(ARGV[1])
local refill    = tonumber(ARGV[2])  -- tokens per second
local now       = tonumber(ARGV[3])  -- unix ms

local bucket = redis.call("HMGET", key, "tokens", "last_refill")
local tokens     = tonumber(bucket[1]) or capacity
local last_refill = tonumber(bucket[2]) or now

-- compute refill
local elapsed = math.max(0, now - last_refill) / 1000.0
local new_tokens = math.min(capacity, tokens + elapsed * refill)

if new_tokens < 1 then
  -- update last_refill even on reject so elapsed resets correctly
  redis.call("HMSET", key, "tokens", new_tokens, "last_refill", now)
  redis.call("PEXPIRE", key, 86400000)
  return 0
end

redis.call("HMSET", key, "tokens", new_tokens - 1, "last_refill", now)
redis.call("PEXPIRE", key, 86400000)
return 1
`)

type TokenBucketLimiter struct {
	rdb      *redis.Client
	capacity int64
	refill   float64 // tokens per second
}

func NewTokenBucketLimiter(rdb *redis.Client, capacity int64, refillPerSec float64) *TokenBucketLimiter {
	return &TokenBucketLimiter{rdb: rdb, capacity: capacity, refill: refillPerSec}
}

func (l *TokenBucketLimiter) Allow(ctx context.Context, key string) (bool, error) {
	nowMs := time.Now().UnixMilli()
	result, err := tokenBucketScript.Run(ctx, l.rdb, []string{
		fmt.Sprintf("tb:%s", key),
	}, l.capacity, l.refill, nowMs).Int64()
	if err != nil {
		// fail open: if Redis is down, let the request through
		return true, err
	}
	return result == 1, nil
}
```

The critical implementation detail is **atomic read-modify-write via Lua**. If you implement this with separate GET and SET calls, you have a race condition under concurrent load. Two goroutines can both read `tokens=1`, both decrement, and both allow the request — effectively doubling your limit. The Lua script executes atomically on the Redis server.

**Where token bucket shines**: API gateways where clients can legitimately burst. A mobile app syncing 50 messages after coming back online is a valid burst. Nginx's `limit_req` module uses token bucket semantics. AWS API Gateway's per-method throttling is token bucket with a 5000-token burst capacity on top of a steady-state RPS limit.

**Where it fails**: If you need to protect a downstream service from overload — say, a legacy SQL database that falls over above 200 concurrent queries — token bucket gives you no guarantee about the _output rate_. A fully-stocked bucket lets a client fire all 100 requests in the same millisecond.

## Leaky Bucket: Guaranteed Output Rate

The leaky bucket algorithm queues incoming requests and processes them at a fixed rate. The "bucket" is a FIFO queue; it "leaks" at a constant drip rate regardless of how fast requests pour in.

```python
# snippet-2
import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Awaitable

@dataclass
class LeakyBucket:
    """
    Single-process leaky bucket. For distributed use, push to a Redis list
    (LPUSH) and have a dedicated consumer process BRPOP at the desired rate.
    
    rate: requests per second to drain
    capacity: max queue depth before overflow
    """
    rate: float
    capacity: int
    _queue: deque = field(default_factory=deque, init=False)
    _last_drain: float = field(default_factory=time.monotonic, init=False)

    async def submit(self, handler: Callable[[], Awaitable[None]]) -> bool:
        """Returns False if queue is full (caller should return 503)."""
        if len(self._queue) >= self.capacity:
            return False
        self._queue.append(handler)
        return True

    async def run(self):
        """Drain loop — run as a background task."""
        interval = 1.0 / self.rate
        while True:
            now = time.monotonic()
            elapsed = now - self._last_drain
            to_drain = int(elapsed * self.rate)

            for _ in range(min(to_drain, len(self._queue))):
                if self._queue:
                    handler = self._queue.popleft()
                    asyncio.create_task(handler())

            self._last_drain = now
            await asyncio.sleep(interval)
```

In practice, pure in-process leaky bucket only works in single-node setups. For distributed deployments the canonical approach is a Redis list as the queue and a separate consumer:

```python
# snippet-3
import redis.asyncio as aioredis
import asyncio
import json

# Producer (API handler) — O(1), non-blocking
async def enqueue_request(rdb: aioredis.Redis, queue_key: str, payload: dict, max_depth: int) -> bool:
    pipe = rdb.pipeline()
    pipe.llen(queue_key)
    pipe.lpush(queue_key, json.dumps(payload))
    length_before, _ = await pipe.execute()
    
    if length_before >= max_depth:
        # Undo the push — we checked after push, so trim
        await rdb.ltrim(queue_key, 0, max_depth - 1)
        return False  # caller returns 503
    return True

# Consumer (separate worker process) — processes at fixed rate
async def drain_worker(rdb: aioredis.Redis, queue_key: str, rate_per_sec: float, processor):
    interval = 1.0 / rate_per_sec
    while True:
        start = asyncio.get_event_loop().time()
        item = await rdb.rpop(queue_key)
        if item:
            payload = json.loads(item)
            await processor(payload)
        elapsed = asyncio.get_event_loop().time() - start
        await asyncio.sleep(max(0, interval - elapsed))
```

**Where leaky bucket shines**: Protecting fragile downstream services. If you're proxying to a third-party API with a hard 100 RPS contract, leaky bucket guarantees you never exceed it even if your own traffic spikes. Envoy's global rate limiting filter uses leaky-bucket semantics for exactly this reason when you configure `max_tokens` with a tight `tokens_per_fill`.

**The real cost**: Added latency and operational complexity. Every request now spends time in a queue before being processed. Your p99 latency at the queue goes from ~0ms to `queue_depth / rate` ms. With 1000 items queued at 100 RPS, the last item waits 10 seconds. That's acceptable for async job processing; it's catastrophic for a synchronous API. Also: the consumer process is now a critical path component. It goes down, your queue fills, all clients start getting 503.

## Sliding Window: The Best of Both

Sliding window counter is the algorithm you want for per-user API quotas — the "1000 requests per hour" use case. It eliminates the boundary burst problem of fixed windows while keeping O(1) memory overhead.

The trick is using two fixed-window counters (previous and current) and computing a weighted interpolation:

```
count = prev_count × (1 - elapsed_in_current_window / window_size) + curr_count
```

If the window is 60 seconds and you're 45 seconds into the current window, the previous window contributes 25% of its count (`1 - 45/60 = 0.25`). This approximation is surprisingly accurate — the error is bounded and never causes the 2× boundary spike of fixed windows.

```go
// snippet-4
package ratelimit

import (
	"context"
	"fmt"
	"time"

	"github.com/redis/go-redis/v9"
)

var slidingWindowScript = redis.NewScript(`
local curr_key  = KEYS[1]
local prev_key  = KEYS[2]
local limit     = tonumber(ARGV[1])
local window_ms = tonumber(ARGV[2])
local now_ms    = tonumber(ARGV[3])

local window_start = now_ms - (now_ms % window_ms)
local elapsed      = now_ms - window_start
local weight       = 1.0 - (elapsed / window_ms)

local prev_count = tonumber(redis.call("GET", prev_key) or 0)
local curr_count = tonumber(redis.call("GET", curr_key) or 0)

local estimated = math.floor(prev_count * weight) + curr_count

if estimated >= limit then
  return {0, estimated}
end

local new_count = redis.call("INCR", curr_key)
if new_count == 1 then
  redis.call("PEXPIRE", curr_key, window_ms * 2)
end
return {1, estimated + 1}
`)

type SlidingWindowLimiter struct {
	rdb      *redis.Client
	limit    int64
	windowMs int64
}

func NewSlidingWindowLimiter(rdb *redis.Client, limit int64, window time.Duration) *SlidingWindowLimiter {
	return &SlidingWindowLimiter{
		rdb:      rdb,
		limit:    limit,
		windowMs: window.Milliseconds(),
	}
}

// Allow returns (allowed bool, currentCount int64, error)
func (l *SlidingWindowLimiter) Allow(ctx context.Context, userID string) (bool, int64, error) {
	nowMs := time.Now().UnixMilli()
	windowStart := nowMs - (nowMs % l.windowMs)
	prevWindowStart := windowStart - l.windowMs

	currKey := fmt.Sprintf("sw:%s:%d", userID, windowStart)
	prevKey := fmt.Sprintf("sw:%s:%d", userID, prevWindowStart)

	res, err := slidingWindowScript.Run(ctx, l.rdb, []string{currKey, prevKey},
		l.limit, l.windowMs, nowMs).Int64Slice()
	if err != nil {
		return true, 0, err // fail open
	}
	return res[0] == 1, res[1], nil
}
```

**Sliding window log** (exact variant): Instead of two counters, store a sorted set of request timestamps per user. On each request, remove entries older than the window, count remaining entries, and add the new timestamp if under limit. This is perfectly accurate but costs O(n) memory per user where n = requests in window. At 1000 req/hour per user with 100k active users, that's 100 million sorted set members — a 6GB Redis footprint just for rate limit state. The counter approach costs 2 keys per user: roughly 400 bytes per 100k users. Use the log variant only for very low-volume limits where accuracy is critical (e.g., 3 failed login attempts per 10 minutes).

## Distributed Atomicity: Where Implementations Break

All three algorithms require atomic read-modify-write. In a single-process system this is a mutex. In a distributed system, every node hitting Redis concurrently, you need Lua scripts or Redis transactions.

A common mistake is implementing token bucket with two separate commands:

```go
// snippet-5
// WRONG: race condition under concurrent load
// This pattern allows more requests than the limit
func (l *BrokenLimiter) Allow(ctx context.Context, key string) (bool, error) {
    tokens, err := l.rdb.HGet(ctx, key, "tokens").Int64()
    if err != nil || tokens < 1 {
        return false, err
    }
    // Another goroutine can read tokens=1 here and also proceed
    l.rdb.HIncrBy(ctx, key, "tokens", -1)
    return true, nil
}

// CORRECT: use Lua script or a library that wraps atomicity
// github.com/go-redis/redis_rate uses Lua internally for GCRA (leaky bucket variant)
// github.com/throttled/throttled uses Redis for both token bucket and GCRA
```

For Go, `go-redis/redis_rate` v10 implements GCRA (Generic Cell Rate Algorithm — a variant of leaky bucket) with a Redis Lua script and exposes a clean API. For Python, `limits` library supports both token bucket and sliding window with Redis backends. Don't roll your own unless you have very specific semantics — the atomicity bugs are subtle and won't show up until you're at 10k RPS.

## Multi-Layer Rate Limiting in Practice

Production systems typically need multiple rate limiting layers simultaneously. An e-commerce API might look like:

```yaml
# snippet-6
# Envoy sidecar config: leaky bucket at the edge for downstream protection
rate_limit_service:
  grpc_service:
    envoy_grpc:
      cluster_name: rate_limit_cluster

# Per-route descriptor: protects the payment service
route_config:
  virtual_hosts:
    - name: payment_api
      rate_limits:
        - actions:
            - remote_address: {}  # per-IP leaky bucket
          stage: 0
        - actions:
            - request_headers:
                header_name: "X-User-Id"
                descriptor_key: "user_id"  # per-user sliding window
          stage: 1

# Ratelimit service config (Lyft ratelimit)
domain: payment
descriptors:
  - key: remote_address
    rate_limit:
      unit: SECOND
      requests_per_unit: 50   # IP-level: leaky bucket semantics
  - key: user_id
    rate_limit:
      unit: HOUR
      requests_per_unit: 1000  # user quota: sliding window
```

Layer 1 (IP-level, leaky bucket): Prevents a single IP from overwhelming the service. Smooth output rate protects payment processor integration. Layer 2 (user-level, sliding window): Enforces the per-user quota in the SLA without boundary burst problems.

## The Decision Framework

| Scenario | Algorithm | Reasoning |
|---|---|---|
| Protect a downstream service from overload | Leaky Bucket | Guaranteed output rate; queue absorbs bursts |
| Per-user quota (1000 req/hour) | Sliding Window Counter | No boundary burst; O(1) memory |
| API clients with legitimate burst patterns | Token Bucket | Allows burst up to capacity, smooths over time |
| Login attempt throttling (exact accuracy) | Sliding Window Log | Low cardinality, exact count matters |
| Edge/CDN layer (Nginx, Envoy) | Token Bucket | Native support, low CPU overhead |
| Internal microservice mesh | Leaky Bucket via GCRA | Predictable load on each service |

Three questions to narrow the choice:

**1. Does the downstream service have a hard throughput ceiling?** If yes, leaky bucket. You need output rate control, not input rate control.

**2. Do you need burst tolerance for legitimate use cases?** If yes, token bucket. Users who batch actions (mobile sync, bulk import) will get punished by leaky bucket's queue latency.

**3. Is the limit per-user over a rolling window?** If yes, sliding window counter. It's the only algorithm that eliminates boundary bursts while keeping memory flat.

One failure mode that catches engineers off guard: **clock skew in distributed token bucket**. If your rate limiter instances have clocks skewed by 100ms, the refill calculation (`elapsed * rate`) will diverge. Either use Redis as the single time source (ARGV[3] from the server's TIME command, not the client clock) or use a monotonic counter approach like GCRA where time is implicit in the key structure.

Rate limiting is not a feature you bolt on — it's a contract with your consumers about what load you'll accept and how you'll behave when they exceed it. A misconfigured algorithm that lets 2× burst at window boundaries, or a leaky bucket with inadequate queue depth that silently drops requests, is worse than no rate limiting at all because it gives you false confidence. Know what guarantee each algorithm actually provides before you ship it.
