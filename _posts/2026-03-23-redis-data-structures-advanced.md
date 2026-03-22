---
layout: post
title: "Advanced Redis Data Structures for Backend Engineers"
date: 2026-03-23 08:00:00 +0700
tags: [redis, backend, distributed-systems, performance, architecture]
description: "Move beyond key-value caching: Sorted Sets, HyperLogLog, Streams, and Bitmaps solve real backend bottlenecks most engineers handle wrong."
---

Your Redis cluster is running at 40% memory and your DBA is asking why the `user_activity` table has 800 million rows. You have a leaderboard query that does a full table scan every 30 seconds, a Kafka cluster running for a single event type with three consumers, and a feature flag service backed by Postgres that gets hit 50,000 times per minute. This is the production state that most teams end up in when they treat Redis as a dumb cache instead of a data structure server. The structures you need already exist in Redis—you just haven't reached for them.

## Sorted Sets: Leaderboards Without the Database

The canonical sorted set use case is leaderboards, but the real value is anywhere you need a ranked collection with O(log N) insertion and range queries. A standard Postgres approach to a leaderboard looks like `SELECT user_id, score FROM scores ORDER BY score DESC LIMIT 100`—fine until you have 10 million users and that query starts showing up in `pg_stat_activity` with a 2-second runtime.

Sorted sets store members with a floating-point score, maintain order automatically, and give you range queries by rank or by score. `ZADD` is O(log N), `ZRANGE` with `REV` and `WITHSCORES` is O(log N + M) where M is the returned elements. For a 10 million user leaderboard, you're looking at microseconds, not seconds.

```go
// snippet-1
package leaderboard

import (
    "context"
    "fmt"
    "github.com/redis/go-redis/v9"
)

type LeaderboardService struct {
    rdb *redis.Client
    key string
}

func (s *LeaderboardService) AddScore(ctx context.Context, userID string, delta float64) error {
    return s.rdb.ZIncrBy(ctx, s.key, delta, userID).Err()
}

func (s *LeaderboardService) TopN(ctx context.Context, n int64) ([]redis.Z, error) {
    return s.rdb.ZRangeArgsWithScores(ctx, redis.ZRangeArgs{
        Key:     s.key,
        Start:   0,
        Stop:    n - 1,
        Rev:     true,
    }).Result()
}

func (s *LeaderboardService) Rank(ctx context.Context, userID string) (int64, float64, error) {
    pipe := s.rdb.Pipeline()
    rankCmd := pipe.ZRevRank(ctx, s.key, userID)
    scoreCmd := pipe.ZScore(ctx, s.key, userID)
    if _, err := pipe.Exec(ctx); err != nil && err != redis.Nil {
        return 0, 0, fmt.Errorf("rank lookup failed: %w", err)
    }
    rank, _ := rankCmd.Result()
    score, _ := scoreCmd.Result()
    return rank + 1, score, nil
}
```

The pattern above pipelines rank and score lookups into a single round trip. Without pipelining, you're paying two network hops for what should be an atomic read. In practice, rank lookups happen far more often than writes, so this matters.

Where sorted sets surprise engineers is in range-by-score queries. Imagine expiring time-based data: store timestamps as scores and use `ZRANGEBYSCORE` with `-inf` and `now-TTL` to find expired members, then `ZREMRANGEBYSCORE` to clean them up in a single command. This pattern replaces a scheduled job that queries an indexed timestamp column.

```python
# snippet-2
import time
import redis

rdb = redis.Redis(host='localhost', port=6379, decode_responses=True)

def schedule_job(job_id: str, run_at: float, payload: str) -> None:
    pipe = rdb.pipeline()
    pipe.zadd('job_queue', {job_id: run_at})
    pipe.set(f'job:payload:{job_id}', payload, exat=int(run_at) + 3600)
    pipe.execute()

def claim_due_jobs(limit: int = 100) -> list[dict]:
    now = time.time()
    # Atomic: get jobs due now and remove them in one transaction
    with rdb.pipeline() as pipe:
        while True:
            try:
                pipe.watch('job_queue')
                jobs = pipe.zrangebyscore('job_queue', '-inf', now, start=0, num=limit)
                if not jobs:
                    pipe.reset()
                    return []
                pipe.multi()
                pipe.zremrangebyscore('job_queue', '-inf', now)
                pipe.execute()
                break
            except redis.WatchError:
                continue
    
    results = []
    for job_id in jobs:
        payload = rdb.get(f'job:payload:{job_id}')
        results.append({'id': job_id, 'payload': payload})
    return results
```

This is a distributed job queue that handles 100k+ jobs without a database. The `WATCH`/`MULTI`/`EXEC` pattern ensures that two workers don't claim the same job. For higher throughput, replace the optimistic locking with a Lua script or use the Streams structure described below.

## HyperLogLog: Cardinality at Scale Without the Memory Bill

Counting unique visitors is a trap. The naive approach—a Redis `SET` per day—works until your DAU hits 50 million and each `SET` consumes 400MB of RAM. A Postgres `COUNT(DISTINCT user_id)` with an index works until your events table is 2 billion rows and the query takes 45 seconds. The correct tool is HyperLogLog.

HyperLogLog gives you cardinality estimation with a standard error of 0.81% using a fixed 12KB per counter—regardless of whether you're counting 100 or 100 million unique elements. For most analytics use cases, the error is irrelevant. You don't need to know you had exactly 4,382,017 unique visitors; you need to know whether that number is 4.3 million or 8.6 million.

```go
// snippet-3
package analytics

import (
    "context"
    "fmt"
    "time"
    "github.com/redis/go-redis/v9"
)

type UniqueVisitorTracker struct {
    rdb *redis.Client
}

func (t *UniqueVisitorTracker) Track(ctx context.Context, userID, pageID string) error {
    day := time.Now().UTC().Format("2006-01-02")
    keys := []string{
        fmt.Sprintf("hll:page:%s:%s", pageID, day),
        fmt.Sprintf("hll:site:%s", day),
    }
    pipe := t.rdb.Pipeline()
    for _, key := range keys {
        pipe.PFAdd(ctx, key, userID)
        pipe.Expire(ctx, key, 30*24*time.Hour)
    }
    _, err := pipe.Exec(ctx)
    return err
}

func (t *UniqueVisitorTracker) UniqueVisitors(ctx context.Context, pageID, date string) (int64, error) {
    key := fmt.Sprintf("hll:page:%s:%s", pageID, date)
    return t.rdb.PFCount(ctx, key).Result()
}

// Rolling 7-day unique visitors across multiple pages
func (t *UniqueVisitorTracker) WeeklyUniques(ctx context.Context, pageIDs []string) (int64, error) {
    var keys []string
    for i := 0; i < 7; i++ {
        day := time.Now().UTC().AddDate(0, 0, -i).Format("2006-01-02")
        for _, pageID := range pageIDs {
            keys = append(keys, fmt.Sprintf("hll:page:%s:%s", pageID, day))
        }
    }
    // PFCOUNT merges multiple HLLs and returns combined cardinality
    return t.rdb.PFCount(ctx, keys...).Result()
}
```

The `PFMERGE` and multi-key `PFCOUNT` operations are where HyperLogLog becomes genuinely powerful. You can maintain per-page, per-campaign, and per-day counters separately, then merge them on read for arbitrary dimensional queries—without pre-aggregating every combination. A 30-day rolling window of 10,000 pages costs 10,000 × 30 × 12KB = 3.6GB in the worst case, versus the tens of gigabytes a `SET`-based approach would require.

The failure mode to watch: HyperLogLog counts are probabilistic and you cannot enumerate members. If you need "show me which users visited page X," you cannot use HyperLogLog. Use it only when you need the count, not the set.

## Streams: Event Sourcing Without Running Kafka

Kafka is the right tool when you have millions of events per second, need multi-datacenter replication, or require 30-day retention for compliance. It is not the right tool when you have one service producing 10k events per hour and two consumers. The operational cost—ZooKeeper or KRaft, brokers, partitioning decisions, consumer group offset management—is enormous relative to the value. Redis Streams provide consumer groups, at-least-once delivery, and persistent logs with none of that overhead.

```go
// snippet-4
package events

import (
    "context"
    "fmt"
    "time"
    "github.com/redis/go-redis/v9"
)

const streamKey = "events:user-actions"
const consumerGroup = "analytics-processors"

type EventBus struct {
    rdb *redis.Client
}

func (b *EventBus) Publish(ctx context.Context, eventType string, payload map[string]interface{}) (string, error) {
    values := map[string]interface{}{
        "type":      eventType,
        "timestamp": time.Now().UnixMilli(),
    }
    for k, v := range payload {
        values[k] = v
    }
    // MAXLEN with ~ is approximate trimming—much faster than exact
    return b.rdb.XAdd(ctx, &redis.XAddArgs{
        Stream: streamKey,
        MaxLen: 100000,
        Approx: true,
        Values: values,
    }).Result()
}

func (b *EventBus) StartConsumer(ctx context.Context, consumerName string, handler func(redis.XMessage) error) error {
    // Create group if it doesn't exist; "0" reads from beginning
    b.rdb.XGroupCreateMkStream(ctx, streamKey, consumerGroup, "0")

    for {
        streams, err := b.rdb.XReadGroup(ctx, &redis.XReadGroupArgs{
            Group:    consumerGroup,
            Consumer: consumerName,
            Streams:  []string{streamKey, ">"},
            Count:    50,
            Block:    2 * time.Second,
        }).Result()
        if err == redis.Nil {
            continue
        }
        if err != nil {
            return fmt.Errorf("xreadgroup failed: %w", err)
        }
        for _, stream := range streams {
            for _, msg := range stream.Messages {
                if err := handler(msg); err != nil {
                    // Leave in PEL for retry; implement dead letter after N retries
                    continue
                }
                b.rdb.XAck(ctx, streamKey, consumerGroup, msg.ID)
            }
        }
    }
}
```

The `>` special ID means "give me messages not yet delivered to this consumer group." Messages stay in the Pending Entries List (PEL) until acknowledged. If a consumer dies mid-processing, use `XAUTOCLAIM` (Redis 6.2+) to reassign stale PEL entries to healthy consumers. This gives you at-least-once delivery semantics without a separate offset store.

One operational detail that bites teams: `XLEN` always returns the full stream length. For monitoring, use `XPENDING` to check PEL depth—a growing PEL means consumers are behind or crashing. Set up an alert on `XPENDING > 10000` before you go to production.

The memory profile is linear with message count. With `MAXLEN ~ 100000` and average message size of 500 bytes, you're looking at ~50MB per stream. For most internal event buses, this is negligible.

## Bitmaps: Feature Flags and Presence at Byte Scale

A feature flag service backed by Postgres is usually a mistake. At 50k RPS, you're either caching the flags in application memory (losing dynamic updates) or hammering the database. Redis Bitmaps—really just bit manipulation on strings—let you encode per-user boolean state at one bit per user. 100 million users × 1 feature flag = 12.5MB. 100 flags = 1.25GB. That's the entire feature flag state for a large-scale application in memory.

```python
# snippet-5
import redis
from typing import Optional

rdb = redis.Redis(host='localhost', port=6379, decode_responses=False)

class FeatureFlagService:
    def __init__(self, rdb: redis.Redis):
        self.rdb = rdb
    
    def enable_for_user(self, flag_name: str, user_id: int) -> None:
        self.rdb.setbit(f'flag:{flag_name}', user_id, 1)
    
    def disable_for_user(self, flag_name: str, user_id: int) -> None:
        self.rdb.setbit(f'flag:{flag_name}', user_id, 0)
    
    def is_enabled(self, flag_name: str, user_id: int) -> bool:
        return bool(self.rdb.getbit(f'flag:{flag_name}', user_id))
    
    def bulk_check(self, flag_names: list[str], user_id: int) -> dict[str, bool]:
        pipe = self.rdb.pipeline(transaction=False)
        for flag in flag_names:
            pipe.getbit(f'flag:{flag}', user_id)
        results = pipe.execute()
        return {flag: bool(val) for flag, val in zip(flag_names, results)}
    
    def count_enabled(self, flag_name: str) -> int:
        """BITCOUNT counts set bits in O(N) where N is byte length."""
        return self.rdb.bitcount(f'flag:{flag_name}')
    
    def rollout_percentage(self, flag_name: str, pct: float, total_users: int) -> None:
        """Enable flag for first pct% of user IDs."""
        cutoff = int(total_users * pct / 100)
        # Use BITFIELD for bulk operations; much faster than N SETBIT calls
        pipe = self.rdb.pipeline()
        chunk_size = 64
        for start in range(0, cutoff, chunk_size):
            end = min(start + chunk_size, cutoff)
            for uid in range(start, end):
                pipe.setbit(f'flag:{flag_name}', uid, 1)
            pipe.execute()
            pipe = self.rdb.pipeline()
```

`BITCOUNT` with byte range arguments lets you count presence within a date range if your bit offset encodes a day number. Store `setbit day_active:<user_id> <day_of_year> 1` and `BITCOUNT` gives you active days in a year range. This is the standard "days active in the last 30 days" analytics query at zero Postgres cost.

The `BITOP` command (`AND`, `OR`, `XOR`, `NOT`) operates on multiple bitmaps and stores the result. Finding users who have flag A enabled AND flag B disabled is a single `BITOP AND dest flag:A flag:NOT-B` operation. For segmentation-heavy use cases, this replaces multi-join SQL queries.

## The Decision Framework

Stop reaching for the wrong primitive by mapping your problem to these four questions:

**Do you need ranking or time-ordered retrieval?** → Sorted Set. If your query has `ORDER BY` and a `LIMIT`, and the data fits in memory, a sorted set will outperform any database index.

**Do you need to count distinct things at scale?** → HyperLogLog. If the error tolerance is above 1% and you never need to enumerate members, HyperLogLog is the only correct answer. A `SET` for cardinality estimation is always wrong at scale.

**Do you need durable, replayable event delivery to multiple consumers?** → Streams. If your event volume is under ~100k/second and you don't need cross-datacenter replication, Streams replaces Kafka with 10% of the operational surface area.

**Do you need per-entity boolean state at high read volume?** → Bitmaps. If your flags, presence indicators, or boolean attributes can be indexed by integer ID, bitmaps give you constant-time reads and `BITCOUNT` analytics that no column store can match at that memory density.

The common trap is treating these as replacements for your primary data store. Sorted sets don't replace your `users` table—they replace the `ORDER BY score DESC LIMIT 100` query that runs against it. HyperLogLog doesn't replace your event log—it replaces the `COUNT(DISTINCT)` query you run against it. Redis is most powerful as a computed view layer sitting in front of your source of truth, not as the source of truth itself.

Memory budgeting before you commit: a sorted set with 10M members costs roughly 800MB (80 bytes per member including score and pointer overhead). A HyperLogLog is always 12KB. A Stream at 100k messages of 500 bytes each is 50MB. A bitmap for 100M users is 12.5MB. Run these numbers before your architecture review, not after your first OOM kill.
```