---
layout: post
title: "Redis Cluster and Sharding: Horizontal Scaling Without Data Loss"
date: 2026-03-22 08:00:00 +0700
tags: [redis, distributed-systems, scaling, backend, infrastructure]
description: "How Redis Cluster distributes data across nodes using hash slots, handles failover, and avoids the pitfalls that cost teams hours of downtime."
---

You're running a single Redis instance at 40GB memory, 80% CPU, and your p99 latency has crept from 1ms to 18ms during peak traffic. You throw more memory at the box — problem solved, temporarily. Then you hit 120GB. Then you need cross-AZ redundancy. Then your ops team tells you the instance is a single point of failure. This is the moment Redis Cluster stops being a nice-to-have and becomes the only rational path forward. The problem isn't that Redis is slow — it's that vertical scaling has a ceiling, and you're about to hit it.

## How Redis Cluster Distributes Data

Redis Cluster uses a fixed keyspace of **16384 hash slots**. Every key maps to a slot via `CRC16(key) % 16384`, and each node in the cluster owns a contiguous range of those slots. A three-node cluster typically looks like:

- Node A: slots 0–5460
- Node B: slots 5461–10922
- Node C: slots 10923–16383

This is not consistent hashing in the Dynamo/Cassandra sense — there's no virtual node ring. The slot assignment is explicit and deterministic, which makes rebalancing predictable but also means you need to understand the mapping when debugging hotspots.

```bash
# snippet-1
# Inspect cluster slot assignment and node topology
redis-cli -h redis-cluster-lb -p 6379 cluster nodes | awk '{print $1, $2, $3, $9}' | column -t

# Check which node owns a specific key
redis-cli -h redis-cluster-lb -p 6379 cluster keyslot "user:session:abc123"

# Get full slot coverage (should show all 16384 slots covered)
redis-cli -h redis-cluster-lb -p 6379 cluster info | grep -E "cluster_state|cluster_slots_ok|cluster_size"
```

One thing that bites teams immediately: **multi-key operations break across slot boundaries**. `MGET user:1 user:2` works only if both keys hash to the same node. `SUNIONSTORE`, `EVAL` with multiple keys, transactions — all of these will throw a `CROSSSLOT` error if the keys land on different nodes.

The solution is hash tags. Wrapping part of your key in `{}` forces Redis to use only that substring for slot calculation:

```redis
# snippet-2
# Without hash tags — these may land on different nodes
SET user:1001:profile "..."
SET user:1001:sessions "..."

# With hash tags — both keys hash to slot for "1001"
SET {user:1001}:profile "..."
SET {user:1001}:sessions "..."

# Now this works without CROSSSLOT error:
MGET {user:1001}:profile {user:1001}:sessions

# Lua scripts also respect this — group keys by logical entity
EVAL "
  local profile = redis.call('GET', KEYS[1])
  local sessions = redis.call('GET', KEYS[2])
  return {profile, sessions}
" 2 {user:1001}:profile {user:1001}:sessions
```

Use hash tags deliberately. Over-use collapses your data onto a small number of slots — you'll create hotspots worse than the single-node problem you started with. A team I know tagged everything with `{tenant_id}`, then discovered their largest tenant's traffic saturated one node at 95% while others sat at 10%.

## Cluster Setup and Configuration That Actually Matters

Spinning up a cluster with `redis-cli --cluster create` takes two minutes. The defaults that will hurt you in production take longer to find.

```bash
# snippet-3
# Create a 3-primary, 3-replica cluster (minimum viable for HA)
redis-cli --cluster create \
  10.0.1.10:6379 10.0.1.11:6379 10.0.1.12:6379 \
  10.0.1.13:6379 10.0.1.14:6379 10.0.1.15:6379 \
  --cluster-replicas 1 \
  --cluster-yes

# Verify: each primary should have exactly one replica
redis-cli -h 10.0.1.10 -p 6379 cluster nodes | grep master
redis-cli -h 10.0.1.10 -p 6379 cluster nodes | grep slave
```

Critical `redis.conf` settings for cluster nodes that most tutorials skip:

```ini
# snippet-4
# /etc/redis/redis.conf — cluster-specific production settings

cluster-enabled yes
cluster-config-file /var/lib/redis/nodes.conf
cluster-node-timeout 5000          # ms before a node is considered down; 5s is sane
cluster-announce-ip 10.0.1.10      # CRITICAL in cloud/container envs with NAT
cluster-announce-port 6379
cluster-announce-bus-port 16379    # gossip port = data port + 10000

# Replication lag threshold — replica won't serve stale reads if behind by more than this
cluster-migration-barrier 1        # replicas won't migrate away if primary has only 1 left

# Persistence: RDB for snapshots, AOF for durability
save 900 1
save 300 10
appendonly yes
appendfsync everysec               # balance between durability and throughput

# Memory management
maxmemory 12gb                     # leave headroom — Redis needs ~20% for fragmentation
maxmemory-policy allkeys-lru       # or volatile-lru if you use TTLs exclusively
activedefrag yes                   # online defrag, keeps fragmentation ratio below 1.5
```

`cluster-announce-ip` is the one that destroys container deployments silently. Without it, nodes advertise their internal container IP. Other nodes can reach them during gossip but your clients can't. You'll see intermittent `MOVED` redirects pointing to unreachable IPs.

## Client-Side Cluster Handling

Your client library needs to understand cluster topology. It must follow `MOVED` and `ASK` redirects, cache the slot-to-node mapping, and handle partial cluster failures gracefully.

<script src="https://gist.github.com/mohashari/49b68d4e14223e778814cd5a253d32be.js?file=snippet-5.go"></script>

`RouteByLatency` is particularly valuable in multi-AZ deployments. Your application in us-east-1a should prefer the replica in the same AZ over a primary in us-east-1c. The latency difference (0.3ms vs 2ms) compounds under load.

## Failover Mechanics and What Actually Goes Wrong

When a primary fails, replicas detect the absence via the gossip protocol after `cluster-node-timeout` milliseconds. At least a quorum of other primaries must agree the node is unreachable before promoting a replica. This means:

1. Node goes down at T=0
2. Replicas stop receiving heartbeats
3. At T=5000ms (cluster-node-timeout), replicas mark the primary as PFAIL
4. At T=10000ms, enough nodes agree: FAIL state
5. Replica with most up-to-date replication offset wins election
6. Cluster reconfigures, clients get `MOVED` to new primary

During this window — **5 to 15 seconds** — writes to that shard fail. This is not configurable away; it's the cost of consensus. If your SLA requires sub-second failover, you need a different architecture (Sentinel with a single shard, or a proxy layer like Envoy with aggressive circuit-breaking).

The failure mode that catches everyone off-guard is **split-brain during network partition**. If your cluster loses majority quorum — say a 3-node cluster loses 2 nodes — it will stop accepting writes entirely. This is the correct behavior; it prevents two sides of a partition from diverging. But it means availability is sacrificed for consistency. Know this before you commit to Cluster.

```bash
# snippet-6
# Simulate and diagnose failover
# Force a manual failover on a replica (graceful — waits for replication to catch up)
redis-cli -h 10.0.1.13 -p 6379 cluster failover

# Takeover — doesn't wait for replication sync (use when primary is dead)
redis-cli -h 10.0.1.13 -p 6379 cluster failover takeover

# Check replication lag before any planned failover
redis-cli -h 10.0.1.10 -p 6379 info replication | grep -E "role|connected_slaves|slave0"

# Monitor cluster health continuously
watch -n 1 'redis-cli -h 10.0.1.10 -p 6379 cluster info | grep -E "state|slots|size|known_nodes"'

# After failover, verify all slots are covered
redis-cli -h 10.0.1.10 -p 6379 cluster check 10.0.1.10:6379
```

## Resharding Without Downtime

Adding capacity to a running cluster requires moving hash slots between nodes. Redis handles this with `MIGRATE` commands under the hood, and the cluster protocol handles the transition state with `ASK` redirects — clients transparently follow these during migration.

```bash
# snippet-7
# Add a new node to the cluster
redis-cli --cluster add-node 10.0.1.16:6379 10.0.1.10:6379

# Add it as a replica of a specific primary
redis-cli --cluster add-node 10.0.1.17:6379 10.0.1.10:6379 \
  --cluster-slave --cluster-master-id <master-node-id>

# Rebalance slots across all primaries (use --pipeline for throughput)
redis-cli --cluster rebalance 10.0.1.10:6379 \
  --cluster-use-empty-masters \
  --cluster-pipeline 20 \
  --cluster-threshold 1.0

# Manual slot migration — move 1000 slots from node A to node D
redis-cli --cluster reshard 10.0.1.10:6379 \
  --cluster-from <source-node-id> \
  --cluster-to <destination-node-id> \
  --cluster-slots 1000 \
  --cluster-yes \
  --cluster-pipeline 20
```

`--cluster-pipeline 20` controls how many keys migrate in a single round-trip. The default is 10. Bumping to 20–50 speeds up migration significantly but increases momentary latency on the source node. On a cluster handling 100K ops/sec, I'd run resharding during your lowest-traffic window and set `--cluster-pipeline 10` to minimize impact. It takes longer but doesn't spike your latency.

**Do not reshard while your memory usage is above 70%.** During migration, keys exist on both source and destination temporarily. At 80% usage, you'll trigger eviction on one or both nodes mid-migration, which corrupts your data distribution silently.

## Monitoring Cluster Health in Production

The metrics that matter for a Redis Cluster are different from a standalone instance:

```python
# snippet-8
import redis
import prometheus_client as prom
from redis.cluster import RedisCluster

CLUSTER_SLOTS_OK = prom.Gauge('redis_cluster_slots_ok', 'Hash slots with OK status')
CLUSTER_KNOWN_NODES = prom.Gauge('redis_cluster_known_nodes', 'Total cluster nodes')
NODE_USED_MEMORY = prom.Gauge('redis_node_used_memory_bytes', 'Memory used', ['node'])
NODE_REPL_LAG = prom.Gauge('redis_node_replication_lag_bytes', 'Replication offset lag', ['replica'])

def collect_cluster_metrics(startup_nodes):
    rc = RedisCluster(startup_nodes=startup_nodes, decode_responses=True)

    # Cluster-level health
    info = rc.cluster_info()
    CLUSTER_SLOTS_OK.set(info['cluster_slots_ok'])
    CLUSTER_KNOWN_NODES.set(info['cluster_known_nodes'])

    if info['cluster_state'] != 'ok':
        # Alert immediately — cluster_state:fail means writes are rejected
        raise RuntimeError(f"Cluster state degraded: {info['cluster_state']}")

    # Per-node metrics
    for node in rc.get_nodes():
        client = node.redis_connection
        node_info = client.info()
        node_addr = f"{node.host}:{node.port}"

        NODE_USED_MEMORY.labels(node=node_addr).set(node_info['used_memory'])

        # Track replication lag per replica
        for i in range(node_info.get('connected_slaves', 0)):
            slave_info = node_info.get(f'slave{i}', '')
            if 'lag=' in slave_info:
                lag = int(slave_info.split('lag=')[1].split(',')[0])
                NODE_REPL_LAG.labels(replica=f"{node_addr}/slave{i}").set(lag)
```

Three alerts you need before anything else:

1. `cluster_state != ok` — cluster is rejecting writes. Page immediately.
2. `cluster_slots_ok < 16384` — some slots have no coverage. Data for those keys is inaccessible.
3. Any node's `used_memory / maxmemory > 0.85` — you're 15 minutes from eviction causing data loss.

The replication lag metric saves you from promoting a lagging replica. A replica 500MB behind its primary means 500MB of writes will be lost if you failover to it. Always check lag before planned failovers.

## The Operational Reality

Redis Cluster solves horizontal scaling elegantly, but it shifts complexity from infrastructure to application code. The hash slot model is simple enough to reason about, but hash tags require discipline across your entire team — one developer ignoring the convention creates cross-slot errors in production that are annoying to debug. Multi-key operations, Lua scripts, and transactions all demand awareness of slot locality.

The tradeoffs worth internalizing: cluster-node-timeout of 5s means 5–15s of write unavailability during primary failure. Split-brain protection means majority quorum loss = full write unavailability. Resharding is online but not zero-cost — do it during low-traffic windows with conservative pipeline sizes.

For workloads under 40GB with modest throughput requirements, a well-tuned Sentinel setup is operationally simpler. Cluster becomes the right call when you need to shard data across nodes for memory reasons, need true horizontal write throughput, or need to survive AZ failures without manual intervention. Know which problem you're actually solving.
```