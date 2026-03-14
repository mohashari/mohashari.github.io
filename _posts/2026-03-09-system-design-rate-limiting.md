---
layout: post
title: "System Design: Rate Limiting Algorithms Explained"
tags: [system-design, backend, architecture, performance]
description: "A deep dive into rate limiting algorithms — fixed window, sliding window, token bucket, and leaky bucket — with implementation examples."
---

Rate limiting is one of those topics that appears in every system design interview — and for good reason. Every production API needs it. Let's explore the algorithms and implementation strategies.

## Why Rate Limit?

- **Prevent abuse** — Stop one bad actor from hammering your API
- **Ensure fairness** — Every user gets a fair share of resources
- **Protect downstream** — Prevent cascading failures from traffic spikes
- **Cost control** — Many cloud services bill by the request

## Algorithm 1: Fixed Window Counter

The simplest approach. Count requests in fixed time windows.

```
Window: 0-59 seconds → 100 requests allowed
Window: 60-119 seconds → 100 requests allowed
```

```go
func (rl *FixedWindowLimiter) Allow(key string) bool {
    now := time.Now()
    windowKey := fmt.Sprintf("%s:%d", key, now.Unix()/60)  // 1-minute windows

    count, _ := redis.Incr(ctx, windowKey).Result()
    if count == 1 {
        redis.Expire(ctx, windowKey, 60*time.Second)
    }

    return count <= rl.limit
}
```

**Problem: Boundary burst**. A user can make 100 requests at 11:59:59 and 100 more at 12:00:00 — effectively 200 requests in 2 seconds.

## Algorithm 2: Sliding Window Log

Track exact timestamps of each request. No boundary burst problem.

```go
func (rl *SlidingWindowLogLimiter) Allow(key string, userID string) bool {
    now := time.Now().UnixMilli()
    windowStart := now - rl.windowMs

    pipe := redis.Pipeline()
    // Remove old entries
    pipe.ZRemRangeByScore(ctx, key, "0", strconv.FormatInt(windowStart, 10))
    // Count current window
    countCmd := pipe.ZCard(ctx, key)
    // Add current request
    pipe.ZAdd(ctx, key, redis.Z{Score: float64(now), Member: now})
    pipe.Expire(ctx, key, time.Duration(rl.windowMs)*time.Millisecond)
    pipe.Exec(ctx)

    return countCmd.Val() < rl.limit
}
```

**Problem:** Memory intensive. Stores every request timestamp.

## Algorithm 3: Sliding Window Counter (Best for Most Cases)

Combines fixed window simplicity with sliding approximation. Low memory, no boundary burst.

```
Current window count + Previous window count × (overlap ratio)
```

```go
func (rl *SlidingWindowCounter) Allow(key string) bool {
    now := time.Now()
    currentWindow := now.Unix() / int64(rl.windowSec)
    prevWindow := currentWindow - 1

    currentKey := fmt.Sprintf("%s:%d", key, currentWindow)
    prevKey := fmt.Sprintf("%s:%d", key, prevWindow)

    pipe := redis.Pipeline()
    currentCmd := pipe.Get(ctx, currentKey)
    prevCmd := pipe.Get(ctx, prevKey)
    pipe.Exec(ctx)

    currentCount, _ := strconv.ParseFloat(currentCmd.Val(), 64)
    prevCount, _ := strconv.ParseFloat(prevCmd.Val(), 64)

    // How far into the current window are we? (0.0 to 1.0)
    windowProgress := float64(now.Unix()%int64(rl.windowSec)) / float64(rl.windowSec)

    // Weighted count
    estimatedCount := currentCount + prevCount*(1-windowProgress)

    if estimatedCount >= float64(rl.limit) {
        return false
    }

    // Increment current window
    redis.Incr(ctx, currentKey)
    redis.Expire(ctx, currentKey, time.Duration(rl.windowSec*2)*time.Second)
    return true
}
```

## Algorithm 4: Token Bucket

Tokens accumulate at a constant rate up to a maximum. Each request consumes one token. **Allows bursts up to bucket size.**

```
Rate: 10 tokens/second, Capacity: 100 tokens
- Quiet period → bucket fills to 100
- Burst → user can fire 100 requests instantly
- Steady state → 10 requests/second sustained
```

```go
type TokenBucket struct {
    rate     float64   // tokens per second
    capacity float64
    tokens   float64
    lastTime time.Time
    mu       sync.Mutex
}

func (tb *TokenBucket) Allow() bool {
    tb.mu.Lock()
    defer tb.mu.Unlock()

    now := time.Now()
    elapsed := now.Sub(tb.lastTime).Seconds()
    tb.lastTime = now

    // Add tokens based on elapsed time
    tb.tokens = math.Min(tb.capacity, tb.tokens+elapsed*tb.rate)

    if tb.tokens >= 1 {
        tb.tokens--
        return true
    }
    return false
}
```

Distributed implementation using Redis + Lua (atomic):

```lua
-- token_bucket.lua
local key = KEYS[1]
local rate = tonumber(ARGV[1])
local capacity = tonumber(ARGV[2])
local now = tonumber(ARGV[3])

local bucket = redis.call('HMGET', key, 'tokens', 'last_time')
local tokens = tonumber(bucket[1]) or capacity
local last_time = tonumber(bucket[2]) or now

local elapsed = now - last_time
tokens = math.min(capacity, tokens + elapsed * rate)

if tokens >= 1 then
    tokens = tokens - 1
    redis.call('HMSET', key, 'tokens', tokens, 'last_time', now)
    redis.call('EXPIRE', key, 3600)
    return 1
else
    redis.call('HMSET', key, 'tokens', tokens, 'last_time', now)
    return 0
end
```

## Algorithm 5: Leaky Bucket

Requests queue up. They're processed at a fixed rate. The queue has a maximum size (excess requests dropped).

```
Incoming requests → [ Queue (max 100) ] → Process at fixed rate (10/s)
```

Good for **smoothing bursty traffic** into a steady stream. Used at the infrastructure layer.

## Choosing the Right Algorithm

| Algorithm | Burst Allowed | Memory | Accuracy | Best For |
|-----------|---------------|--------|----------|---------|
| Fixed Window | Yes (boundary) | Low | Low | Simple throttling |
| Sliding Window Log | No | High | Perfect | Low-traffic, strict limits |
| Sliding Window Counter | Minimal | Low | ~95% | Most API rate limiting |
| Token Bucket | Yes (controlled) | Low | High | APIs with burst allowance |
| Leaky Bucket | No | Medium | High | Traffic shaping |

## Implementation in HTTP Middleware

```go
func RateLimitMiddleware(limiter Limiter) func(http.Handler) http.Handler {
    return func(next http.Handler) http.Handler {
        return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
            // Use IP + user ID as key (or just IP for unauthenticated)
            key := getRateLimitKey(r)

            result := limiter.Allow(key)
            if !result.Allowed {
                w.Header().Set("X-RateLimit-Limit", strconv.Itoa(result.Limit))
                w.Header().Set("X-RateLimit-Remaining", "0")
                w.Header().Set("X-RateLimit-Reset", strconv.FormatInt(result.ResetAt.Unix(), 10))
                w.Header().Set("Retry-After", strconv.Itoa(int(result.RetryAfter.Seconds())))
                http.Error(w, "Too Many Requests", http.StatusTooManyRequests)
                return
            }

            w.Header().Set("X-RateLimit-Limit", strconv.Itoa(result.Limit))
            w.Header().Set("X-RateLimit-Remaining", strconv.Itoa(result.Remaining))
            next.ServeHTTP(w, r)
        })
    }
}
```

## Rate Limit Tiers

Real APIs often have tiered limits:

```go
func getRateLimit(userTier string) (limit int, window time.Duration) {
    switch userTier {
    case "free":
        return 100, time.Hour
    case "pro":
        return 1000, time.Hour
    case "enterprise":
        return 10000, time.Hour
    default:
        return 60, time.Hour  // Unauthenticated
    }
}
```

Rate limiting is one of those features that seems trivial until you're under attack. Build it in from day one.
