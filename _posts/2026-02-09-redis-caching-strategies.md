---
layout: post
title: "Redis Caching Strategies for High-Performance Applications"
tags: [redis, caching, backend, performance]
description: "Master Redis caching patterns — from simple key-value caching to advanced strategies like cache-aside, write-through, and cache stampede prevention."
---

Redis is the Swiss Army knife of backend engineering. At its core it's a blazing-fast in-memory data store, but the real power is in how you design your caching strategy. Let's explore the patterns that matter.

![Redis Cache-Aside Pattern](/images/diagrams/redis-cache-aside.svg)

## Why Cache?

Simple math: a database query might take 50ms. A Redis hit takes 0.1ms. That's a 500x speedup. For endpoints that serve millions of requests, caching is not optional — it's survival.

## Caching Patterns

### 1. Cache-Aside (Lazy Loading)

The application manages the cache. Most common pattern.

```go
func GetUser(ctx context.Context, userID string) (*User, error) {
    // 1. Check cache
    cacheKey := fmt.Sprintf("user:%s", userID)
    cached, err := redis.Get(ctx, cacheKey).Result()
    if err == nil {
        var user User
        json.Unmarshal([]byte(cached), &user)
        return &user, nil
    }

    // 2. Cache miss — query database
    user, err := db.GetUser(ctx, userID)
    if err != nil {
        return nil, err
    }

    // 3. Populate cache
    data, _ := json.Marshal(user)
    redis.Set(ctx, cacheKey, data, 15*time.Minute)

    return user, nil
}
```

**Pros:** Only caches what's actually requested. Tolerates cache failures.
**Cons:** First request always hits the database (cold start).

### 2. Write-Through

Update cache whenever you update the database.

```go
func UpdateUser(ctx context.Context, user *User) error {
    // 1. Update database
    if err := db.UpdateUser(ctx, user); err != nil {
        return err
    }

    // 2. Update cache immediately
    cacheKey := fmt.Sprintf("user:%s", user.ID)
    data, _ := json.Marshal(user)
    redis.Set(ctx, cacheKey, data, 15*time.Minute)

    return nil
}
```

**Pros:** Cache is always up-to-date.
**Cons:** Write penalty; you cache data that might never be read.

### 3. Write-Behind (Write-Back)

Write to cache immediately, persist to database asynchronously.

```
Client → Redis (immediate) → Queue → Database (async)
```

**Pros:** Extremely fast writes.
**Cons:** Risk of data loss if Redis goes down before persisting.

## Cache Invalidation Strategies

> "There are only two hard things in Computer Science: cache invalidation and naming things." — Phil Karlton

### TTL-Based Invalidation

Simplest approach. Set an expiry and let it expire naturally:

```go
// Cache with TTL
redis.Set(ctx, "product:42", data, 5*time.Minute)

// Use longer TTLs for stable data
redis.Set(ctx, "config:features", data, 1*time.Hour)
```

### Event-Based Invalidation

Invalidate on data change:

```go
func OnOrderStatusChanged(orderID string) {
    // Invalidate all related caches
    pipe := redis.Pipeline()
    pipe.Del(ctx, fmt.Sprintf("order:%s", orderID))
    pipe.Del(ctx, fmt.Sprintf("user:orders:%s", order.UserID))
    pipe.Del(ctx, "dashboard:summary")
    pipe.Exec(ctx)
}
```

### Tag-Based Invalidation

Group related cache keys with tags for bulk invalidation:

```go
// Store user's cache keys in a set
func CacheWithTag(ctx context.Context, key string, tag string, data interface{}, ttl time.Duration) {
    pipe := redis.Pipeline()
    serialized, _ := json.Marshal(data)
    pipe.Set(ctx, key, serialized, ttl)
    pipe.SAdd(ctx, fmt.Sprintf("tag:%s", tag), key)
    pipe.Exec(ctx)
}

// Invalidate all caches for a user
func InvalidateTag(ctx context.Context, tag string) {
    tagKey := fmt.Sprintf("tag:%s", tag)
    keys, _ := redis.SMembers(ctx, tagKey).Result()
    if len(keys) > 0 {
        redis.Del(ctx, keys...)
    }
    redis.Del(ctx, tagKey)
}
```

## Cache Stampede Prevention

When a hot cache key expires, hundreds of requests simultaneously hit the database. This is a **cache stampede** and can take down your service.

### Solution: Probabilistic Early Expiration

```go
func GetWithStampedeProtection(ctx context.Context, key string) ([]byte, error) {
    type cachedValue struct {
        Data    []byte    `json:"data"`
        Expiry  time.Time `json:"expiry"`
        Delta   float64   `json:"delta"`
    }

    raw, err := redis.Get(ctx, key).Bytes()
    if err != nil {
        return nil, err // Cache miss
    }

    var cv cachedValue
    json.Unmarshal(raw, &cv)

    // Probabilistically recompute before expiry
    ttl := time.Until(cv.Expiry).Seconds()
    if -cv.Delta*math.Log(rand.Float64()) >= ttl {
        return nil, nil // Trigger early recomputation
    }

    return cv.Data, nil
}
```

### Solution: Mutex/Lock

Only one goroutine recomputes; others wait:

```go
func GetWithLock(ctx context.Context, key string, fetch func() ([]byte, error)) ([]byte, error) {
    // Try cache first
    if val, err := redis.Get(ctx, key).Bytes(); err == nil {
        return val, nil
    }

    // Acquire lock
    lockKey := key + ":lock"
    acquired, _ := redis.SetNX(ctx, lockKey, "1", 10*time.Second).Result()

    if !acquired {
        // Wait for lock holder to populate cache
        time.Sleep(50 * time.Millisecond)
        return GetWithLock(ctx, key, fetch)
    }
    defer redis.Del(ctx, lockKey)

    // Double-check after acquiring lock
    if val, err := redis.Get(ctx, key).Bytes(); err == nil {
        return val, nil
    }

    // Fetch and cache
    val, err := fetch()
    if err != nil {
        return nil, err
    }
    redis.Set(ctx, key, val, 5*time.Minute)
    return val, nil
}
```

## Key Design Best Practices

```
# Use colon-separated namespaces
user:{id}
user:{id}:posts
product:{id}:variants
session:{token}

# Include version for schema changes
v2:user:{id}

# Include tenant for multi-tenant apps
tenant:{tenant_id}:user:{user_id}
```

## Monitoring Cache Health

Track these metrics:

```
cache_hit_rate = hits / (hits + misses)  # Target: >90%
cache_memory_usage                        # Stay under 80%
eviction_rate                             # High = cache too small
```

A cache hit rate below 80% means your caching strategy needs work. Start there.
