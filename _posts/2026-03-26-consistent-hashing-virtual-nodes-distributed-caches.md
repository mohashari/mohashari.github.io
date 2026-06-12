---
layout: post
title: "Consistent Hashing with Virtual Nodes: Load Distribution in Distributed Caches"
date: 2026-03-26 08:00:00 +0700
tags: [distributed-systems, caching, redis, architecture, backend]
description: "How virtual nodes in consistent hashing prevent hotspots and minimize key redistribution when your cache cluster changes."
image: ""
thumbnail: ""
---

You have a 12-node Redis Cluster. One node takes a hardware failure at 2 AM. Your on-call gets paged because 8% of your cache misses spike to 100% — not 8%, not even 16%, but 100% — for a narrow slice of your keyspace. Every key that was hashed to that node is now a miss, and a thundering herd of database queries follows. You add a replacement node. Now the cluster rebalances, and for the next 40 minutes, key routing is in flux. The incident stretches to three hours. The root cause wasn't the hardware failure. It was naive modular hashing: `node = hash(key) % N`. When N changes, almost every key maps to a different node.

![Consistent Hashing with Virtual Nodes: Load Distribution in Distributed Caches Diagram](/images/diagrams/consistent-hashing-virtual-nodes-distributed-caches.svg)

Consistent hashing solves the redistribution problem. Virtual nodes solve the uneven distribution problem that plain consistent hashing introduces. Together, they're the basis of how production-grade distributed caches — Cassandra, DynamoDB, Riak, and Redis Cluster (with a slot-based variant) — manage their key spaces at scale.

## The Problem with Modular Hashing

Before diving into consistent hashing, it's worth being precise about what breaks with modular hashing.

Given a cache key `user:profile:42`, a typical implementation looks like:

```python
# snippet-1
import hashlib

def get_node_modular(key: str, nodes: list[str]) -> str:
    hash_val = int(hashlib.md5(key.encode()).hexdigest(), 16)
    return nodes[hash_val % len(nodes)]

nodes = ["cache-01:6379", "cache-02:6379", "cache-03:6379"]

# hash(key) % 3 = 2 → cache-03
print(get_node_modular("user:profile:42", nodes))  # cache-03

# Add cache-04. Now hash(key) % 4
nodes.append("cache-04:6379")
print(get_node_modular("user:profile:42", nodes))  # cache-01 ← different node!
```

Adding one node to a 3-node cluster causes approximately `(N-1)/N` = 75% of keys to remap. At scale, that's a cold cache event across your entire fleet simultaneously.

## Consistent Hashing: The Core Idea

Consistent hashing places both nodes and keys on a conceptual ring spanning `[0, 2^32)`. A key's responsible node is the first node encountered moving clockwise from the key's position on the ring.

```go
// snippet-2
package consistenthash

import (
	"crypto/sha256"
	"encoding/binary"
	"fmt"
	"sort"
	"sync"
)

type Ring struct {
	mu       sync.RWMutex
	vnodes   []uint32          // sorted hash positions
	nodeMap  map[uint32]string // hash position → physical node
	replicas int               // virtual nodes per physical node
}

func New(replicas int) *Ring {
	return &Ring{
		vnodes:   []uint32{},
		nodeMap:  make(map[uint32]string),
		replicas: replicas,
	}
}

func (r *Ring) AddNode(node string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	for i := range r.replicas {
		key := fmt.Sprintf("%s#%d", node, i)
		h := hashKey(key)
		r.vnodes = append(r.vnodes, h)
		r.nodeMap[h] = node
	}
	sort.Slice(r.vnodes, func(i, j int) bool { return r.vnodes[i] < r.vnodes[j] })
}

func (r *Ring) RemoveNode(node string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	for i := range r.replicas {
		key := fmt.Sprintf("%s#%d", node, i)
		h := hashKey(key)
		delete(r.nodeMap, h)
	}
	r.vnodes = r.vnodes[:0]
	for h := range r.nodeMap {
		r.vnodes = append(r.vnodes, h)
	}
	sort.Slice(r.vnodes, func(i, j int) bool { return r.vnodes[i] < r.vnodes[j] })
}

func (r *Ring) Get(key string) string {
	r.mu.RLock()
	defer r.mu.RUnlock()
	if len(r.vnodes) == 0 {
		return ""
	}
	h := hashKey(key)
	idx := sort.Search(len(r.vnodes), func(i int) bool { return r.vnodes[i] >= h })
	if idx == len(r.vnodes) {
		idx = 0 // wrap around the ring
	}
	return r.nodeMap[r.vnodes[idx]]
}

func hashKey(key string) uint32 {
	digest := sha256.Sum256([]byte(key))
	return binary.BigEndian.Uint32(digest[:4])
}
```

When you remove a node, only the keys between that node's predecessor and the node itself on the ring need to move. For a cluster of N nodes, removing one node redistributes approximately `1/N` of the total keyspace — not `(N-1)/N`.

## Why Basic Consistent Hashing Still Fails in Production

The naive ring with one point per physical node has a critical flaw: the distribution is non-uniform. With N nodes, the ring is divided into N arcs. In expectation, each arc covers `1/N` of the keyspace, but the variance is high. With 10 nodes and SHA-256, you can easily get one node owning 20% of the ring while another owns 3%. That 7x imbalance translates directly to memory and QPS imbalance.

Worse: when you add a node with more capacity, you want it to absorb proportionally more load. With a single ring point per node, there's no way to express that.

## Virtual Nodes: The Fix

Virtual nodes (vnodes) assign each physical node multiple positions on the ring — typically 100–250 vnodes per node in production systems. The key insight: with enough vnodes, the law of large numbers takes over and the distribution converges to near-uniform.

```python
# snippet-3
import hashlib
import math
from collections import defaultdict

def simulate_distribution(num_nodes: int, vnodes_per_node: int, num_keys: int = 1_000_000) -> dict:
    """Simulate keyspace distribution across nodes with varying vnode counts."""
    ring = {}
    for node_id in range(num_nodes):
        for v in range(vnodes_per_node):
            vnode_key = f"node-{node_id}#vnode-{v}"
            h = int(hashlib.sha256(vnode_key.encode()).hexdigest(), 16) % (2**32)
            ring[h] = f"node-{node_id}"

    sorted_ring = sorted(ring.keys())
    counts = defaultdict(int)

    for i in range(num_keys):
        key_hash = int(hashlib.sha256(f"key:{i}".encode()).hexdigest(), 16) % (2**32)
        idx = next((j for j, h in enumerate(sorted_ring) if h >= key_hash), 0)
        counts[ring[sorted_ring[idx]]] += 1

    total = sum(counts.values())
    fractions = {n: c / total for n, c in counts.items()}
    std_dev = math.sqrt(sum((f - 1/num_nodes)**2 for f in fractions.values()) / num_nodes)
    return {"distribution": fractions, "std_dev_pct": std_dev * 100}

# 3 nodes, 1 vnode each: std_dev ~12%
# 3 nodes, 150 vnodes each: std_dev ~0.9%
for vnodes in [1, 10, 50, 150, 250]:
    result = simulate_distribution(num_nodes=3, vnodes_per_node=vnodes)
    print(f"vnodes={vnodes:3d}: std_dev={result['std_dev_pct']:.2f}%")
```

With 150 vnodes per node, you get roughly 1% standard deviation in load distribution across a 3-node cluster — acceptable for production. With 1 vnode per node, you're at 12%+ deviation.

The other benefit of vnodes is proportional weighting. A node with twice the RAM can be assigned twice the vnodes, absorbing twice the keyspace share without any special routing logic.

## Implementing Weighted Capacity

```go
// snippet-4
package consistenthash

// NodeConfig allows proportional vnode assignment based on capacity.
type NodeConfig struct {
	Addr     string
	Weight   int // relative weight; 1 = standard node, 2 = double capacity
}

func (r *Ring) AddWeightedNode(cfg NodeConfig) {
	vnodeCount := r.replicas * cfg.Weight
	r.mu.Lock()
	defer r.mu.Unlock()
	for i := range vnodeCount {
		key := fmt.Sprintf("%s#%d", cfg.Addr, i)
		h := hashKey(key)
		r.vnodes = append(r.vnodes, h)
		r.nodeMap[h] = cfg.Addr
	}
	sort.Slice(r.vnodes, func(i, j int) bool { return r.vnodes[i] < r.vnodes[j] })
}

// Usage:
// ring.AddWeightedNode(NodeConfig{Addr: "cache-01:6379", Weight: 1}) // 32GB RAM
// ring.AddWeightedNode(NodeConfig{Addr: "cache-02:6379", Weight: 2}) // 64GB RAM, gets 2x vnodes
// ring.AddWeightedNode(NodeConfig{Addr: "cache-03:6379", Weight: 1}) // 32GB RAM
// Total vnodes: 1*150 + 2*150 + 1*150 = 600. cache-02 owns ~50% of keyspace.
```

This is exactly how Cassandra handles heterogeneous hardware in a cluster. You don't need all nodes to be identical — you just adjust the vnode count at provisioning time.

## The Rebalancing Story: Numbers That Matter

The production argument for consistent hashing isn't theoretical elegance — it's how much cache traffic you shed during topology changes.

```bash
# snippet-5
# Measuring redistribution with a 10-node cluster

# Modular hashing: add 1 node (10 → 11)
# Keys that remap = (N_old / N_new) fraction remain + new node absorbs rest
# Fraction that moves ≈ 1 - (10/11) = 90.9% stay, but key-to-node mapping changes for ~90%
python3 -c "
N_old, N_new = 10, 11
# For modular hash, keys that stay on same node:
# key stays if (hash % N_old == hash % N_new), which is rare
# Approximation: fraction that moves ≈ (N_new - N_old) / N_new * N_old
fraction_moves_modular = 1 - (1/N_new)
fraction_moves_consistent = 1 / N_new  # only the new node's slice moves

print(f'Modular hash: {fraction_moves_modular*100:.1f}% of keys remap')
print(f'Consistent hash: {fraction_moves_consistent*100:.1f}% of keys remap')

# At 10M cached items, 1KB avg value:
items = 10_000_000
item_size_kb = 1
print(f'\\nModular: ~{items * fraction_moves_modular / 1e6:.1f}M items invalidated')
print(f'Consistent: ~{items * fraction_moves_consistent / 1e6:.1f}M items redistributed')
"

# Output:
# Modular hash: 90.9% of keys remap
# Consistent hash: 9.1% of keys remap
#
# Modular: ~9.1M items invalidated
# Consistent: ~0.9M items redistributed
```

A 10x reduction in cache invalidation during a topology change. On a high-traffic system with 100K requests/second and a 20ms database read, briefly cold-caching 9M vs 0.9M items is the difference between a brownout and a blip.

## Real-World Failure Modes

**Vnode count too low.** Twitch reported hotspots in their Cassandra clusters when they ran with 32 vnodes per node. Bumping to 256 was a non-trivial migration but resolved persistent 2-3x load imbalance on certain nodes. The math is clear: with 32 vnodes across 10 nodes, you have only 320 ring segments. Statistical variance is still meaningful. With 256, you have 2560 segments and variance becomes negligible.

**Hash function collisions.** Using MD5 truncated to 32 bits for a 500-node cluster with 250 vnodes each means 125,000 ring positions in a 4B space. Collision probability isn't zero. SHA-256 truncated to 64 bits is the safe choice — birthday paradox math puts collisions at astronomically unlikely levels even at 10M vnodes.

**Uneven key distribution at the application level.** Virtual nodes handle node-level distribution, not key-level distribution. If your application generates 80% of requests for keys starting with `user:premium:*` and those keys all land in a 10% slice of hash space — a hotkey problem, not a vnode problem. Consistent hashing doesn't fix application-level hot keys; you need key sharding, local caching, or explicit replication for that.

**Ring metadata size.** With 50 nodes and 250 vnodes each, you store 12,500 ring entries. Each entry is a 4-byte hash + a pointer to the node config. That's ~50KB for the ring metadata. Trivial. But some implementations naively serialize the full ring on every configuration change and broadcast it across the cluster — at 50KB per node per change, this can create network storms during rapid topology changes (autoscaling events, rolling restarts).

## Applying This to Redis

Redis Cluster uses a fixed 16,384 hash slots rather than a continuous ring — a pragmatic optimization that makes slot assignment metadata compact (2KB bitmap) and simplifies rebalancing tooling. The underlying principle is identical to vnodes: each node owns a configurable subset of slots, nodes can be assigned more or fewer slots, and adding a node only migrates the relevant slots.

```bash
# snippet-6
# Redis Cluster slot management — checking and rebalancing slot distribution

# Check current slot distribution
redis-cli --cluster check 127.0.0.1:6379

# Rebalance slots proportionally (respects weights if set)
redis-cli --cluster rebalance 127.0.0.1:6379 --cluster-use-empty-masters

# Add a new node and migrate slots (only ~1/N slots move)
redis-cli --cluster add-node 127.0.0.1:6382 127.0.0.1:6379

# Assign specific slot range to new node (manual control)
redis-cli --cluster reshard 127.0.0.1:6379 \
  --cluster-from <source-node-id> \
  --cluster-to <new-node-id> \
  --cluster-slots 1365  # 1/12th of 16384

# Monitor slot migration progress
watch -n1 "redis-cli --cluster check 127.0.0.1:6379 2>&1 | grep slots"
```

Redis Cluster's 16,384 slots are effectively a fixed vnode count. The minimum granularity of rebalancing is one slot, but in practice you move ranges of hundreds of slots at a time.

## Choosing Vnode Count in Practice

The right number of vnodes depends on your cluster size and how often topology changes happen:

- **3–5 nodes**: Use 150–250 vnodes per node. Small clusters have the highest variance risk.
- **10–20 nodes**: 100–150 vnodes per node. Standard Cassandra production default.
- **50+ nodes**: 64–100 vnodes per node. The law of large numbers helps; fewer vnodes keeps ring metadata manageable.
- **Heterogeneous hardware**: Use vnodes proportional to node capacity (RAM, CPU). A 128GB node gets 2x the vnodes of a 64GB node.

One often-overlooked constraint: the vnode count must be set at node provisioning time in most systems. Changing it requires a full node decommission and re-add. Make a deliberate choice upfront.

## Consistent Hashing Is Not the Whole Story

Virtual nodes solve load distribution. They don't solve data durability, replication, or split-brain. A complete distributed cache design also needs:

- **Replication factor**: Each key should exist on R nodes (typically R=3). The ring determines primary ownership; the next R-1 nodes clockwise own replicas.
- **Failure detection**: If a node is down, your client needs to know to skip it and go to its replica. Most production clients (jedis, lettuce, go-redis) handle this automatically for Redis Cluster.
- **Quorum reads/writes**: For caches, you usually don't need quorum — eventual consistency is fine. For session stores or anything requiring strict consistency, you need to think through CAP implications carefully.

The virtual node ring is a routing substrate. The correctness guarantees of your distributed cache sit on top of it, not inside it.

## Summary

Consistent hashing with virtual nodes solves two distinct problems: it minimizes key redistribution during cluster topology changes (from `(N-1)/N` with modular hashing to `1/N`), and it ensures near-uniform load distribution across nodes by leveraging the central limit theorem through many small ring segments per physical node. The practical floor is 100–150 vnodes per node for clusters up to 20 nodes. Below that, load imbalance becomes visible in metrics and capacity planning gets harder. Above 250 vnodes per node, the marginal improvement is minimal and ring metadata size becomes a consideration. Pick a number in that range, set it consistently across your cluster, and let the math handle the rest.
