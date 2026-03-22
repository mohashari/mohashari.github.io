---
layout: post
title: "Redis Cluster and Sharding: Horizontal Scaling Without Data Loss"
date: 2026-03-22 08:00:00 +0700
tags: [redis, distributed-systems, backend, infrastructure, database]
description: "How Redis Cluster hash slots, replica promotion, and keyspace design decisions affect data safety at scale."
---

You're running a single Redis instance at 60GB memory, 80% CPU, and 150k ops/sec. You've tuned `maxmemory-policy`, scaled the VM vertically twice, and the ops team is starting to ask uncomfortable questions about your runbooks. The obvious answer is Redis Cluster — but "obvious" is doing a lot of work there. Clusters introduce failure modes that don't exist on single nodes: cross-slot multi-key operations silently fail, hash tag abuse concentrates load onto a single shard, and misconfigured replication means a partition event can permanently lose writes you were told were acknowledged. This post is about understanding the mechanics well enough to avoid those failure modes before they happen in production.

## How Hash Slots Actually Work

Redis Cluster partitions the keyspace into exactly 16,384 hash slots. Every key maps to a slot via `CRC16(key) % 16384`, and every node in the cluster owns a contiguous or fragmented range of those slots. With a 6-node cluster (3 primary, 3 replica), a typical layout gives each primary roughly 5,461 slots.

```bash
# snippet-1
# Inspect slot distribution across a live cluster
redis-cli -h redis-cluster-primary-1 -p 6379 cluster nodes | awk '{print $1, $2, $9}' | column -t

# Check which slot a key maps to
redis-cli -h redis-cluster-primary-1 -p 6379 cluster keyslot "user:session:abc123"
# Output: (integer) 7638

# Count keys per slot on a specific node
redis-cli -h redis-cluster-primary-1 -p 6379 cluster getkeysinslot 7638 100
```

The distribution assumes your keys are evenly distributed across the CRC16 space, which is a reasonable assumption if your keys are random UUIDs or have high cardinality prefixes. Where this breaks down: if you're storing sessions keyed by `session:{user_id}` and 40% of your traffic is from 200 power users, you have a hot-key problem that cluster topology alone cannot solve.

Slot ownership is tracked via the gossip protocol — each node broadcasts its slot assignments and node state to a random subset of peers every second. Convergence after a topology change typically happens within 1-2 seconds on a well-connected cluster, but during that window, clients with stale slot maps will receive `MOVED` redirects. Modern Redis clients (go-redis, Jedis, ioredis) handle this transparently by following the redirect and updating their slot cache. Clients that don't handle `MOVED` — and there are still some in the wild — will throw exceptions.

## Replica Promotion and the Write Loss Window

Redis Cluster uses asynchronous replication by default. A write is acknowledged to the client as soon as the primary writes it to its in-memory data structure and AOF (if enabled). The replica receives the replication stream asynchronously. This means there's always a replication lag window, and if a primary fails during that window, the writes in that gap are gone.

The sequence during a node failure:

1. Primary P1 goes down.
2. Replicas detect the failure via gossip — specifically, when `cluster-node-timeout` milliseconds pass without a pong response (default: 15000ms).
3. The remaining primaries vote on whether P1 is truly down (`PFAIL` → `FAIL`).
4. P1's replica initiates an election, requests votes from primaries, and promotes itself if it gets a majority.
5. The new primary starts accepting writes.

The total time from failure to recovery is typically `cluster-node-timeout` + election time (a few hundred milliseconds). During that entire window, clients get `CLUSTERDOWN` errors for slots owned by P1. After promotion, any writes that were replicated to the old replica but not yet sent are recovered. Anything that was in-flight to P1 at the moment of failure is gone.

```yaml
# snippet-2
# redis.conf tuning for cluster nodes — production-relevant parameters
cluster-enabled yes
cluster-config-file nodes.conf
cluster-node-timeout 5000          # Reduce from default 15s for faster failover
cluster-replica-validity-factor 10  # Replica must not lag > node-timeout * factor ms
cluster-migration-barrier 1         # Keep at least 1 replica per primary
cluster-require-full-coverage no    # Critical: allow partial cluster to serve requests
                                    # Default is yes — which means a single shard failure
                                    # makes the ENTIRE cluster refuse writes
appendonly yes
appendfsync everysec
no-appendfsync-on-rewrite no
```

`cluster-require-full-coverage no` deserves emphasis. The default `yes` means that if any primary has no replica and goes down, the entire cluster stops accepting writes — not just the affected shard. For most production use cases, you want `no`, accepting that you lose access to the data in the failed shard while the rest continues working. The alternative is a total outage while you recover one shard.

## Cross-Slot Operations: The Silent Failure

This is where teams get surprised. Redis Cluster does not support multi-key operations across different slots. `MGET`, `MSET`, `SUNIONSTORE`, `EVAL` with multiple keys, transactions (`MULTI`/`EXEC`) — all of these work only when every key involved maps to the same slot.

```python
# snippet-3
import redis.cluster

client = redis.RedisCluster(
    startup_nodes=[{"host": "redis-cluster-1", "port": 6379}],
    decode_responses=True,
)

# This will raise CrossSlotError if keys hash to different slots
try:
    pipe = client.pipeline()
    pipe.set("user:1001:balance", "500")
    pipe.set("user:1002:balance", "300")
    pipe.execute()
except redis.exceptions.ResponseError as e:
    print(f"Cross-slot error: {e}")
    # CROSSSLOT Keys in request don't hash to the same slot

# The correct pattern: use hash tags to force co-location
# {user:1001} is the hash tag — only the content inside {} is hashed
pipe = client.pipeline()
pipe.set("{user:1001}:balance", "500")
pipe.set("{user:1001}:profile", '{"name": "Alice"}')
pipe.set("{user:1001}:sessions", "[]")
pipe.execute()  # Works — all three keys hash to the same slot as "user:1001"
```

Hash tags (`{...}`) let you force specific keys to the same slot. Only the substring inside the first `{}` pair is used for slot calculation. This is the correct tool for ensuring atomicity across related keys.

The abuse pattern: using a single hash tag for too many keys. If you tag everything with `{global}`, all those keys land on one slot, one primary, and you've eliminated all horizontal scaling benefit. I've seen teams do this to "fix" cross-slot errors without understanding the consequence — they end up with a 12-node cluster where 11 nodes are idle and one is on fire.

## Keyspace Design for Even Distribution

Before you move data, model your distribution. Hash slot assignment is deterministic, so you can compute it offline.

<script src="https://gist.github.com/mohashari/e4df2dfd6fd878bef377251d3067e168.js?file=snippet-4.go"></script>

Run this simulation against your actual key patterns before scaling. A ratio above 2x between your hottest and average slot is a warning sign. Above 5x, you have a problem that topology changes won't fix.

## Live Resharding Without Data Loss

When you need to add or remove nodes from a running cluster, `redis-cli --cluster reshard` migrates slots one key at a time using `MIGRATE`. The operation is safe but not zero-impact.

```bash
# snippet-5
# Add a new node to the cluster
redis-cli --cluster add-node \
  new-node:6379 \
  existing-primary:6379 \
  --cluster-slave  # Add as replica first, then promote

# Reshard: move 1000 slots from existing primaries to the new node
redis-cli --cluster reshard existing-primary:6379 \
  --cluster-from all \
  --cluster-to <new-node-id> \
  --cluster-slots 1000 \
  --cluster-yes \
  --cluster-pipeline 20  # Batch size for MIGRATE — tune based on your key sizes

# Verify cluster is healthy after resharding
redis-cli --cluster check existing-primary:6379
# Look for: [OK] All nodes agree about slots configuration.
# And: [OK] All 16384 slots covered.

# Monitor migration in real time
watch -n 1 "redis-cli -h existing-primary -p 6379 cluster info | grep -E 'cluster_state|cluster_slots'"
```

During `MIGRATE`, the source slot is temporarily in `MIGRATING` state and the destination is in `IMPORTING` state. Clients that access a key in a migrating slot get either the data (if it hasn't moved yet) or an `ASK` redirect (if it has). `ASK` is different from `MOVED` — it's transient and should not update the client's slot cache. Most clients handle this correctly, but it's worth validating your client library's behavior under migration before doing this in production.

One concrete failure mode: if your average key size is large (>10KB) and you set `--cluster-pipeline 20`, you're moving 200KB per batch across the network. Under load, this causes latency spikes on the migrating node. Tune `--cluster-pipeline` down (5-10) for large keys, and schedule migrations during low-traffic windows.

## Validating Cluster Health

Before and after any topology change, you need a validation checklist that goes beyond `cluster info`.

```bash
# snippet-6
#!/bin/bash
# cluster-health-check.sh — run before/after topology changes

PRIMARY="redis-cluster-1"
PORT=6379

echo "=== Cluster Info ==="
redis-cli -h $PRIMARY -p $PORT cluster info | grep -E \
  'cluster_state|cluster_slots_assigned|cluster_known_nodes|cluster_size'

echo ""
echo "=== Node States ==="
redis-cli -h $PRIMARY -p $PORT cluster nodes | awk '{
  split($3, flags, ",")
  for (f in flags) {
    if (flags[f] == "fail" || flags[f] == "pfail") {
      print "WARN: Node " $2 " in state " flags[f]
    }
  }
}'

echo ""
echo "=== Replication Lag ==="
redis-cli -h $PRIMARY -p $PORT cluster nodes | grep "slave" | while read line; do
  node_ip=$(echo $line | awk '{print $2}' | cut -d@ -f1)
  host=$(echo $node_ip | cut -d: -f1)
  port=$(echo $node_ip | cut -d: -f2)
  lag=$(redis-cli -h $host -p $port info replication | grep master_repl_offset | cut -d: -f2 | tr -d '\r')
  echo "Replica $host:$port offset: $lag"
done

echo ""
echo "=== Slot Coverage ==="
redis-cli --cluster check $PRIMARY:$PORT 2>&1 | tail -5
```

Pay specific attention to `master_repl_offset` drift between primary and replica. A lag of more than a few thousand bytes under normal load suggests network issues or replica resource contention. During migrations, lag will spike — that's expected — but it should converge within seconds of the migration batch completing.

## The WAIT Command and Synchronous Writes

For operations where you cannot afford to lose a write — financial transactions, idempotency keys, distributed locks — Redis provides `WAIT numreplicas timeout`. It blocks until the specified number of replicas have acknowledged the write, up to `timeout` milliseconds.

```python
# snippet-7
import redis.cluster
import contextlib

class DurableRedisClient:
    def __init__(self, cluster_client: redis.RedisCluster):
        self.client = cluster_client
        self.min_replicas = 1
        self.wait_timeout_ms = 100

    def durable_set(self, key: str, value: str, ex: int = None) -> bool:
        """
        Write with replica acknowledgment. Returns False if durability
        could not be guaranteed within the timeout.
        """
        pipe = self.client.pipeline(transaction=False)
        pipe.set(key, value, ex=ex)
        # WAIT is executed on the same connection/shard as the preceding write
        # In cluster mode, go-redis and redis-py handle shard routing automatically
        pipe.execute_command("WAIT", self.min_replicas, self.wait_timeout_ms)
        results = pipe.execute()

        ack_count = results[1]  # WAIT returns number of replicas that acknowledged
        if ack_count < self.min_replicas:
            # Write succeeded on primary but replica acknowledgment timed out
            # Decide: accept the risk or roll back depending on your consistency requirements
            return False
        return True

    @contextlib.contextmanager
    def distributed_lock(self, resource: str, ttl_ms: int = 5000):
        lock_key = f"lock:{{{resource}}}"  # Hash tag ensures atomicity with SET NX
        acquired = self.client.set(lock_key, "1", px=ttl_ms, nx=True)
        if not acquired:
            raise RuntimeError(f"Could not acquire lock for {resource}")
        try:
            yield
        finally:
            self.client.delete(lock_key)
```

`WAIT` does not make Redis synchronously replicated in the traditional sense — the primary doesn't hold the write until replicas confirm. It's a post-write check. The write is already committed on the primary. If the replica never responds (network partition), `WAIT` returns 0 after the timeout, but the write persists on the primary. This is still useful: it lets you detect — and optionally retry or alert on — situations where your durability guarantees are degraded.

## When Redis Cluster Is the Wrong Answer

Redis Cluster is operationally non-trivial. Before committing to it, consider:

**Proxy-based sharding** (Twemproxy, Codis, KeyDB Proxy): You get horizontal scaling with a single endpoint, no client-side cluster awareness required, and multi-key operations work across shards at the proxy layer. The tradeoff is the proxy as a single point of failure and additional latency hop.

**Read replicas instead of sharding**: If your bottleneck is read throughput rather than write throughput or memory, a primary with multiple replicas behind a read-routing proxy (or client-side read distribution) gets you horizontal read scaling without any of the cross-slot complexity.

**Keyspace analysis first**: Run `redis-cli --hotkeys` (requires `maxmemory-policy allkeys-lfu` or `volatile-lfu`) against your production instance before assuming you need a cluster. You might have 3 hot keys and 50GB of cold data — the right answer is key-level caching or TTL tuning, not sharding.

Redis Cluster fits when you genuinely need to exceed the memory or write throughput of a single node, when your key access patterns are reasonably uniform, and when your team has the operational capacity to manage topology changes, monitor replication lag, and maintain client library versions that handle `MOVED`/`ASK` correctly. If those conditions hold, it's an excellent solution. If they don't, you're adding complexity without proportional benefit.
```