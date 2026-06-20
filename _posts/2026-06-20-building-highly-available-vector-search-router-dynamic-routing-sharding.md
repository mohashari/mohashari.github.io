---
layout: post
title: "Building a Highly Available Vector Search Router: Dynamic Routing, Index Replication, and Query Sharding"
date: 2026-06-20 08:00:00 +0700
tags: [vector-search, distributed-systems, go, high-availability]
description: "A production-grade guide to designing a low-latency, resilient vector search router in Go featuring dynamic replica routing, EWMA metrics, circuit breaking, and parallel scatter-gather query sharding."
image: "/images/diagrams/building-highly-available-vector-search-router-dynamic-routing-sharding.svg"
thumbnail: "/images/diagrams/building-highly-available-vector-search-router-dynamic-routing-sharding.svg"
---

Imagine a production semantic search system processing 25,000 queries per second (QPS) across 150 million 1536-dimensional vector embeddings. During a routine index rebuild or a sudden network micro-partition, a single node's CPU usage spikes to 100%, causing response times on that node to rise from 15ms to over 3,000ms. Because traditional round-robin load balancers are blind to application-level query latency and HNSW (Hierarchical Navigable Small World) index traversal characteristics, they continue to pump traffic into the degraded node. Upstream connection pools saturate, memory allocations balloon due to queued requests, and a localized node slowdown escalates into a catastrophic, cluster-wide outage. Solving this at scale requires more than just throwing hardware at the problem. It requires building an intelligent, vector-aware search router that orchestrates dynamic load balancing, handles index replication resilience, and implements query sharding with low-latency parallel execution.

![Building a Highly Available Vector Search Router: Dynamic Routing, Index Replication, and Query Sharding Diagram](/images/diagrams/building-highly-available-vector-search-router-dynamic-routing-sharding.svg)

## The Core Problem: Why Traditional Load Balancing Fails Vector Databases

Traditional Layer 4 or Layer 7 load balancers (like HAProxy, NGINX, or AWS ALB) rely on network-level metrics (e.g., active connection counts) or simple round-robin strategies to distribute traffic. While this works well for stateless HTTP applications or standard CRUD operations, it is fundamentally incompatible with the dynamics of vector search engines like Qdrant, Milvus, or pgvector.

Vector search is extremely CPU and memory-bandwidth intensive. A single query involves traversing a high-dimensional HNSW graph, evaluating distance metrics (such as Cosine or Euclidean distance), and potentially executing complex metadata filtering. The CPU load of a search query is highly variable, depending on parameters like the search depth ($ef\_search$), the number of vectors matched by metadata filters, and whether the index has been warmed up in memory. 

If a load balancer routes a series of complex, filter-heavy queries to a node that is currently performing segment compaction or index builds, that node will quickly experience resource starvation. Once requests begin queuing on a single node, its latency spikes exponentially. A traditional round-robin proxy will continue sending traffic, creating a "herd effect" that crashes the node. 

To prevent cascading failures, we must implement an intelligent routing layer. This layer must track the health and latency of every replica individually, short-circuit requests to degraded nodes, and split large vector indices across multiple shards to distribute the computational load.

## Defining the Topology: Shard and Replica Structs

To build a router in Go, we must first model our cluster topology. The cluster is divided into **shards**, where each shard is responsible for a distinct slice of the total vector dataset. To achieve high availability, each shard is backed by a group of **replicas**. Each replica maintains an independent copy of the shard's index.

Our Go router must track the address, state, and real-time performance statistics of each replica. In Snippet 1, we define the structures for our `VectorReplica`, `Shard`, and overall `ClusterTopology`.

<script src="https://gist.github.com/mohashari/2bb9c6180c0b4852930e205422495120.js?file=snippet-1.go"></script>

## Dynamic Replica Routing and Self-Healing Circuit Breakers

A resilient router must dynamically select the best replica for a given query. Rather than relying on simple health checks that only probe nodes every few seconds, we calculate an **Exponentially Weighted Moving Average (EWMA)** of the latency for every query execution. This allows the router to detect micro-stutters and redirect traffic to faster replicas in real-time.

Additionally, we pair EWMA with a **Circuit Breaker** pattern. If a replica encounters consecutive network timeouts or gRPC errors, the router trips the circuit for that node, immediately failing fast or falling back to healthy nodes without hitting the network timeout.

Snippet 2 shows how we calculate the EWMA latency and implement the replica selection logic, including transitioning nodes to `StateHalfOpen` to safely probe recovery.

<script src="https://gist.github.com/mohashari/2bb9c6180c0b4852930e205422495120.js?file=snippet-2.go"></script>

## Implementing Query Sharding: The Scatter-Gather Engine

When a vector index grows to tens of millions of records, keeping it on a single machine is unsafe and inefficient. The HNSW index consumes significant memory (often $1.5\times$ to $2\times$ the size of the raw vector floats), and searching it requires serial graph traversal. Sharding breaks the dataset into multiple disjoint subsets, allowing us to perform searches in parallel across different machines.

The router executes this via a **Scatter-Gather** engine:
1. **Scatter Phase**: The incoming vector query is sent to all shards simultaneously. We spawn Go routines to query a selected replica for each shard concurrently.
2. **Gather Phase**: The router waits for all shard responses, enforcing strict context-driven timeouts to ensure slow shards do not drag down global query latency.

Snippet 3 shows the concurrent execution logic of the Scatter-Gather engine in Go.

<script src="https://gist.github.com/mohashari/2bb9c6180c0b4852930e205422495120.js?file=snippet-3.go"></script>

## The Gather Phase: K-Way Merge for Score Re-Ranking

Once the scatter phase yields results from all shards, we must merge them into a single global top-K list. Each shard returns its own local top-K results, sorted descending by similarity score. 

A naive merge approach would involve concatenating all results into a single array and sorting it. However, if we have $M$ shards and are retrieving top-$K$ items, sorting the concatenated array of size $M \times K$ takes $O(M \cdot K \log (M \cdot K))$ time. 

For high-throughput backends, a more efficient solution is to implement a **K-Way Merge** using a **Min-Heap**. We maintain a Min-Heap of size $K$. As we iterate through the results from each shard, we compare their similarity scores to the minimum score in the heap. If a score is higher, we pop the minimum and push the new result. This reduces the time complexity to $O(M \cdot K \log K)$ and consumes minimal memory. 

Furthermore, because vectors can be duplicated across shards during indexing partitions, we must implement deduplication during the merge step. Snippet 4 shows this implementation using Go's `container/heap` library.

<script src="https://gist.github.com/mohashari/2bb9c6180c0b4852930e205422495120.js?file=snippet-4.go"></script>

## Active Health Checking and Dynamic Topology Updating

While passive monitoring via EWMA catches issues mid-query, it leaves the router vulnerable when a previously failed node recovers. A dropped node needs an active, background mechanism to probe its status, verify that its indices are fully loaded (vector databases can take minutes to load HNSW index segments into RAM on startup), and safely re-introduce it into the active replica pool.

We run a background health-check loop that executes an HTTP `GET /healthz` call to all replicas. If a replica is in `StateHalfOpen` or `StateTripped` and successfully passes the health checks, we reset its metrics and mark it as healthy.

<script src="https://gist.github.com/mohashari/2bb9c6180c0b4852930e205422495120.js?file=snippet-5.go"></script>

## Router Topology Configuration

A production configuration must specify explicit timeouts, decay factors, and sharding parameters. Hardcoding these in the Go router is a recipe for operational failure. In Snippet 6, we define a declarative configuration structure in YAML that governs the router.

```yaml
# snippet-6
router:
  port: 8090
  read_timeout: 500ms
  write_timeout: 1000ms
  max_connections: 10000

vector_cluster:
  sharding:
    total_shards: 2
    replica_factor: 2
  circuit_breaker:
    failure_threshold: 5
    consecutive_failures_limit: 5
    cooldown_period: 30s
    ewma_decay_factor: 0.2
  health_check:
    interval: 5s
    timeout: 1s
    ping_endpoint: "/healthz"

shards:
  - id: 0
    replicas:
      - addr: "10.0.1.10:8502"
        role: "primary"
      - addr: "10.0.2.10:8502"
        role: "replica"
  - id: 1
    replicas:
      - addr: "10.0.1.11:8502"
        role: "primary"
      - addr: "10.0.2.11:8502"
        role: "replica"
```

## Production Considerations and Operational Trade-Offs

When deploying a vector search router in a high-volume environment, engineers must account for several operational trade-offs and edge cases:

1. **Slow-Node Poisoning**: In a scattered query pattern, **your search latency is bound by the slowest shard**. If three out of four shards respond in 8ms, but the fourth shard has replica degradation and takes 250ms, the entire search operation takes 250ms. Dynamic routing using EWMA helps prevent this by identifying degraded replicas early and directing queries to healthier alternatives.
2. **Partial Search Success Policies**: If a shard becomes completely unavailable (i.e. all its replicas trip their circuit breakers), should the router return a HTTP 503 error, or should it return partial results? In production systems like e-commerce search, it is often better to return partial results (representing $N-1$ shards) with a warning header rather than failing the request entirely. This allows for a degraded but functional user experience.
3. **Write Path vs. Read Path Consistency**: Vector database indices are often updated asynchronously. Replicas may exhibit minor latency in updating their HNSW index segments, resulting in slight inconsistencies in similarity scores. If your application requires strict read-after-write consistency, the router must support routing writes to a designated master node and directing reads to replicas that have confirmed synchronization offsets.
4. **HNSW RAM Saturation**: Always monitor RAM utilization on backend replica nodes. Vector databases use memory-mapped files (mmap) to access the vector files. If RAM is saturated, the OS kernel will begin thrashing pages to and from disk, causing query latencies to spike from 10ms to several hundred milliseconds. Ensure that your sharding strategy splits indices so that the combined size of vectors and graphs fits entirely in the server's RAM.

By implementing this architecture—dynamic routing with EWMA tracking, active circuit breaking, parallel scatter-gather sharding, and heap-based K-Way merging—you can scale vector similarity search to millions of users while maintaining predictable millisecond latencies.