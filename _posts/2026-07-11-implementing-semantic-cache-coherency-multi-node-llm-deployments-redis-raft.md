---
layout: post
title: "Implementing Semantic Cache Coherency in Multi-Node LLM Deployments using Redis Raft"
date: 2026-07-11 08:00:00 +0700
tags: [redis, raft, llm-cache, ai-infrastructure]
description: "Build a strongly consistent, multi-node semantic cache for LLM deployments using Redis Raft to eliminate cache drift and stampedes."
image: "https://picsum.photos/seed/6955/1080/720"
thumbnail: "https://picsum.photos/seed/6955/400/300"
---

In a production LLM gateway serving 500+ requests per second, semantic caching is the difference between a $15,000 monthly API bill and a $150,000 disaster. However, when scaling this gateway across multiple nodes in different availability zones, standard Redis deployment models fall apart. Because Redis replication is asynchronously active-passive by default, network partitions or rapid invalidation cycles introduce replication lag. This lag causes downstream nodes to serve stale, hallucinated, or outright incorrect cached responses to users. If a prompt invalidation event occurs on Node A (for example, due to a database update or user permission revocation) and Node B serves a cached response for a semantically similar query 50 milliseconds later due to replication lag, you have a cache coherency failure. 

To solve this, we must couple approximate nearest neighbor (ANN) vector search with a strongly consistent, linearizable metadata coordinator. This post details how to implement a production-ready, multi-node semantic cache using a split-plane architecture: a vector database (e.g., Qdrant) for distance calculations, and Redis Raft for replicated state machine consistency and atomic locking.

## The Core Challenge: Why Semantic Invalidation is Hard

Unlike traditional key-value caching where invalidating `user:123:profile` is a simple `DEL` command, semantic caching operates in a continuous vector space. A query is mapped to a high-dimensional vector (e.g., 1536 dimensions using `text-embedding-3-small`), and the cache is hit if a new query falls within a predefined hypersphere of radius $\epsilon$ around an existing cached query.

```
                  Hypersphere Invalidation (Radius ε)
                        
                              [Cached Vector V1] (Stale)
                                  o  \
                                 /    \  < ε
                                /      \
                       [New Query V_q]--o [Cached Vector V2] (Stale)
```

If the underlying context changes (e.g., a service status changes from "operational" to "degraded"), all cached responses in that region of the vector space become stale. To invalidate this cache, we cannot simply delete a specific key. We must:
1. Identify all vector IDs within the distance threshold of the invalidation intent.
2. Atomically delete or mark those cache entries as invalid across all nodes.
3. Ensure that no concurrent gateway requests can read a partially invalidated state (linearizability).

If your metadata layer relies on standard Redis Sentinel or Redis Cluster, a write acknowledged by the master node may not have reached the replicas. During a network partition or failover, a replica can be promoted to master without the invalidation write, leading to silent cache drift where nodes serve stale data indefinitely.

## The Architecture: Split-Plane Vector + Consensus Cache

We divide our semantic cache into two planes:
1. **The Vector Search Plane (AP - Available/Partition-Tolerant):** Stores query embeddings and performs fast similarity searches. We accept eventual consistency here; if a vector is indexed slightly late, it merely causes a temporary cache miss.
2. **The Metadata and Coordination Plane (CP - Consistent/Partition-Tolerant):** Managed by **Redis Raft**. It stores the actual LLM responses, locks, and cache state metadata. Writes and reads are run through the Raft consensus engine, guaranteeing that once an invalidation is acknowledged, no node can read the stale state.

Here is the request flow through the API gateway:

```
[Client] ---> [LLM Gateway Node] 
                   |
                   +---> (1) Generate Embedding (V_q)
                   |
                   +---> (2) Query Vector Store (Qdrant) ---> Returns Vector ID (UUID)
                   |
                   +---> (3) Check Redis Raft (Consensus Read) for UUID Metadata
                               |
                               +---> [HIT]  --> Verify TTL & ACLs --> Return Cached JSON
                               |
                               +---> [MISS] --> Acquire Raft-based Lease (Mutex)
                                                     |
                                                     +--> Call LLM Provider
                                                     +--> Write Vector to Qdrant
                                                     +--> Write Payload to Redis Raft (Raft Log Commit)
```

## Step 1: Querying the Vector Space for Candidate Matches

The first step is embedding the user's prompt and querying the vector store. We use Qdrant for this step because it supports payload filtering and fast ANN search. The search returns a list of candidate document IDs and their similarity scores.

<script src="https://gist.github.com/mohashari/90c717cb858328eef2bc46b8d61181e4.js?file=snippet-1.py"></script>

## Step 2: The Core Gateway Loop (Go Gateway Implementation)

In the gateway, we must coordinate the vector database lookup, handle lock acquisition to prevent cache stampedes, and fetch the actual payload from Redis Raft. We write this coordinator in Go for its high concurrency model.

<script src="https://gist.github.com/mohashari/90c717cb858328eef2bc46b8d61181e4.js?file=snippet-2.go"></script>

## Step 3: Atomic Cache Commit via Redis Raft Lua Scripting

To commit the new cache entry to Redis Raft, we must write the metadata, set the TTL, and release the lock in a single transaction. Since Redis Raft guarantees ACID transactions and executes Lua scripts deterministically through its replicated log, we can guarantee that no node sees a partial state.

<script src="https://gist.github.com/mohashari/90c717cb858328eef2bc46b8d61181e4.js?file=snippet-3.go"></script>

## Step 4: Configuring the Redis Raft Cluster

Setting up Redis Raft requires running a multi-node Redis setup with the `redisraft.so` module loaded. Below is a production-style `docker-compose.yml` demonstrating a three-node setup with Raft persistence and heartbeat configurations optimized to prevent election storms under high CPU/network stress.

<script src="https://gist.github.com/mohashari/90c717cb858328eef2bc46b8d61181e4.js?file=snippet-4.yaml"></script>

## Step 5: Handling Semantic Invalidation and Reconciliation

When business logic dictates that context has changed, we must execute a semantic invalidation. Because vector store indices cannot guarantee transactional isolation, our invalidation pipeline does the following:
1. Queries Qdrant for all vector IDs within the invalidation target's similarity threshold.
2. Performs an atomic transaction on Redis Raft to delete the corresponding keys.
3. Issues asynchronous deletes to the vector store.

If the vector store deletion fails, it is a non-critical error because subsequent lookups for those vectors will result in cache misses at the Redis Raft level (which has been purged).

<script src="https://gist.github.com/mohashari/90c717cb858328eef2bc46b8d61181e4.js?file=snippet-5.py"></script>

## Production Failure Modes & Operational Mitigations

### 1. Raft Leader Election Storms under High LLM Latency
In standard applications, backend servers respond within 10-50 milliseconds. LLM generation requests routinely take between 2,000 and 15,000 milliseconds. 
If your gateway node holds a Redis Raft connection open in a single-threaded blocking script, or if the Redis engine runs out of execution threads because of blocked sockets, it can trigger false heartbeats timeouts in the Raft state machine. 

To mitigate this:
*   **Never execute blocking operations inside a Redis Raft transaction.** The LLM call must occur externally. Redis Raft must only handle the locking and key mutation.
*   Configure the Raft properties: set `raft.election-timeout` to at least `2000` milliseconds and `raft.heartbeat-interval` to `400` milliseconds. This provides enough buffer to withstand garbage collection sweeps and network jitter without triggering a cascade of leader elections.

### 2. The Dual-Write Split Brain
If a write to Redis Raft succeeds but the write to the Vector Database fails:
*   **Impact:** A cache miss will continue to occur because the vector cannot be resolved, meaning the database is functionally correct (no stale data served), but we waste token usage.
*   **Mitigation:** Run a background reconciliation worker that sweeps Redis Raft key patterns (`cache:*`) and verifies their existence in the Vector collection. If a vector ID is missing from Qdrant, the worker re-embeds the cached payload and writes it back to the vector plane.

### 3. Semantic Drift and False Positives
Over time, the semantic space of your cache will become crowded. A similarity threshold of `0.88` might be perfect for generic inquiries but highly inaccurate for programmatic API commands (e.g., distinguishing between `kubectl delete pod A` and `kubectl delete pod B`).
*   **Mitigation:** Partition your cache keyspaces by scope. Maintain separate vector collections for distinct domains (e.g., `vector_billing`, `vector_code_execution`) and tune the threshold $\epsilon$ individually. Implement a metadata layer in Redis Raft that maps specific user roles or tenant IDs to cache keys to ensure user-scoped security parameters are checked on every read hit.