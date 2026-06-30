---
layout: post
title: "Optimizing Vector Search Retrieval: Implementing HNSW Index Graphs with Custom Distance Metrics"
date: 2026-06-30 08:00:00 +0700
tags: [vector-search, database-internals, performance-tuning, rust]
description: "Learn how to optimize vector search by implementing custom distance metrics directly inside HNSW graphs using Rust and AVX-512 vectorization."
image: "https://picsum.photos/seed/7606/1080/720"
thumbnail: "https://picsum.photos/seed/7606/400/300"
---

In production recommendation systems and real-time retrieval pipelines scaling past 50 million high-dimensional vectors, relying on standard distance metrics like Cosine or Euclidean distance is a fast track to architectural failure. When business constraints—such as real-time pricing penalties, regional availability, or category-matching rules—must influence the similarity score, engineers typically resort to a two-stage retrieval pipeline: retrieve the top-1000 items via standard vector search, and then apply custom scoring logic in a post-filtering step. This approach triggers a severe failure mode known as the "single-category wall" or "recall collapse." If the semantic space matches items that are out of stock or geographically restricted, post-filtering might prune 98% of the candidates, leaving the client with an empty or highly sub-optimal result set. The only resilient solution is to inject custom distance metrics directly into the index traversal step of Hierarchical Navigable Small World (HNSW) graphs, evaluating multi-attribute constraints during graph traversal.

![Optimizing Vector Search Retrieval: Implementing HNSW Index Graphs with Custom Distance Metrics Diagram](/images/diagrams/optimizing-vector-search-hnsw-graphs-custom-distance-metrics.svg)

## Anatomizing HNSW Graph Traversal and Cache Locality

HNSW indexes operate by constructing a multi-layer graph where the top layers act as a sparse skip-list for fast routing, and the bottom layer (Layer 0) contains all vectors with dense local connections. During a search, the query vector starts at an entry point in the highest layer, greedily traverses nodes that minimize distance to the query, and drops down layer by layer until it performs a local search in Layer 0. 

In a production database processing 10,000 queries per second (QPS), the absolute bottleneck of this traversal is CPU cache misses and memory bus saturation. A single query traversing an HNSW graph with a maximum link configuration ($M = 16$) and a search budget ($efSearch = 64$) requires evaluating hundreds of distance computations. If each vector is a 512-dimensional 32-bit float array ($2 \text{ KB}$ of data), and the graph nodes are represented as pointer-heavy objects scattered across the heap, the CPU spends up to 75% of its execution cycles waiting for RAM to load vector data into L1/L2 cache lines.

To mitigate this, production vector databases store vectors in a contiguous, aligned memory block (a flat vector table) separate from the graph topology. Nodes in the graph store only 32-bit or 64-bit offsets into this table. This memory layout allows the CPU to fetch vector data using prefetching instructions and process dimensions sequentially.

Below is a production-grade implementation of this aligned layout in Rust, showcasing how to represent HNSW graph nodes and vectors to maintain strict cache alignment and avoid pointer-chasing overhead.

<script src="https://gist.github.com/mohashari/259008930abd725c488814d8e341983a.js?file=snippet-1.txt"></script>

## Designing the Custom Distance Metric Trait

To support custom distance metrics without introducing dynamic dispatch (`dyn Metric`) overhead in the inner loops of the search routing, we must leverage Rust’s monomorphization. By defining a generic `DistanceMetric` trait, the compiler can inline the distance evaluation directly into the HNSW routing code, enabling loop unrolling, vectorization, and registers allocation.

When defining a custom metric for production, we must write a baseline implementation that supports compiler autovectorization. The compiler can automatically generate SIMD instructions (AVX2/AVX-512) for simple loops if the slices are guaranteed to be of equal length and loop iterations are independent.

<script src="https://gist.github.com/mohashari/259008930abd725c488814d8e341983a.js?file=snippet-2.txt"></script>

## Implementing Vectorized Custom Metrics with AVX2 and FMA

While compiler autovectorization is highly effective for trivial loops, complex custom distance metrics containing conditional logic, dynamic weighting arrays, or business penalty offsets require manual SIMD optimization to achieve sub-millisecond p99 search latencies. 

Consider a custom distance metric that computes a weighted L2 (Euclidean) distance, where the weight factors adjust the significance of specific semantic dimensions based on user context (e.g., matching user affinity scores to item categories). Additionally, if the item fails a business constraint (e.g., out-of-stock status indicated by a metadata flag), we apply a severe scaling penalty.

For x86_64 server architectures, we can write an optimized implementation utilizing 256-bit AVX2 registers and Fused Multiply-Add (FMA) instructions, which execute a multiplication and an addition in a single clock cycle.

<script src="https://gist.github.com/mohashari/259008930abd725c488814d8e341983a.js?file=snippet-3.txt"></script>

## The Greedy HNSW Search Traversal Engine

Now that we have designed the cache-friendly vector table layout and the monomorphized `DistanceMetric` interface, we must construct the core traversal engine of HNSW. 

The `search_layer` function traverses a specific layer of the HNSW graph using a greedy search strategy. It keeps track of visited nodes using a fast bitset or hash set, maintains a candidate list (a min-heap containing nodes sorted by their distance to the query), and stores the best found nodes in a result heap (a max-heap containing the closest nodes).

The implementation below demonstrates this hot-loop traversal. Minimizing allocations within this loop is critical; allocating memory for the visited set or priority queue inside `search_layer` on a per-query basis will trigger heap fragmentation and degrade p99 performance under high concurrent loads.

<script src="https://gist.github.com/mohashari/259008930abd725c488814d8e341983a.js?file=snippet-4.txt"></script>

## Designing Allocation-Free Query Context Pools

To achieve high concurrency and sub-millisecond retrieval execution, we must entirely eliminate allocations from the query execution path. During standard traversal, tracking visited nodes requires a lookup table. If we instantiate a fresh `Vec<bool>` or `HashSet<usize>` per query, the allocator will exhaust heap limits at scale. 

A high-performance strategy uses thread-local search contexts. We allocate a reusable memory block containing pre-sized buffers (heaps and visited flags sized up to the maximum configuration limits, such as `max_nodes_count` and `efSearch`). Threads borrow a context from a global lock-free pool, execute the traversal, reset the counters, and return the context back to the pool.

<script src="https://gist.github.com/mohashari/259008930abd725c488814d8e341983a.js?file=snippet-5.txt"></script>

## Production Failure Modes and Mitigations

Implementing inline distance calculation within HNSW graphs introduces distinct architectural risks that do not exist in standard vector retrieval engines. 

### 1. Violation of the Triangle Inequality and Local Minima Traps
HNSW’s search logic relies fundamentally on the assumption that if node $A$ is close to node $B$, and node $B$ is close to node $C$, then $A$ is close to $C$. This is the triangle inequality:

$$d(A, C) \le d(A, B) + d(B, C)$$

When you implement a custom distance metric containing dynamic business rules (e.g., penalizing items based on stock levels, category mismatch, or geo-distance), the metric often violates this inequality. Under these conditions, the graph traversal algorithm can encounter local minima traps: a query path will abort because all neighbors of the current node have a higher distance, even though a cluster of lower-distance nodes lies just beyond them.

*   **Mitigation:** If your custom distance metric violates the triangle inequality, you must compensate during index construction and retrieval. During construction, utilize the HNSW heuristic connections strategy rather than greedy connection selection. During queries, scale the `efSearch` parameters to larger values (e.g., from $efSearch = 64$ to $efSearch = 256$) to keep a wider search frontier active, allowing the algorithm to bypass localized clusters of high-distance nodes.

### 2. NUMA Node Contention and Thread Affinity
In dual-socket server systems (e.g., AMD EPYC or Intel Xeon scalable processors), memory access latency is non-uniform (NUMA). If the flat vector table is allocated in the physical memory pool of Socket 0, but a query thread executing on Socket 1 attempts to traverse the graph, every distance computation incurs an expensive Inter-Connect (UPI/Infinity Fabric) transit penalty. P99 traversal latencies can jump from $1.5 \text{ ms}$ to over $12 \text{ ms}$.

*   **Mitigation:** Pin worker threads to dedicated CPU cores using core-affinity libraries and allocate a local copy of the `VectorTable` and `HNSWGraph` on each socket’s NUMA memory block. Alternatively, implement a sharded index architecture where each socket hosts a partition of the dataset, and merge final results in a lightweight coordinator layer.

### 3. Register Throttling and CPU Frequency Scaling
Running tight SIMD loops (specifically AVX-512 FMA) on modern CPUs forces the core to draw higher current. To protect the silicon, server processors downclock the core's frequency when they detect sustained AVX-512 instruction density. This frequency drop can degrade performance for non-vectorized tasks running on the same core.

*   **Mitigation:** Profile the application under load. If CPU frequency scaling causes overall throughput drops, compile the vector search component to use AVX2 exclusively, which incurs a negligible performance penalty for dimensions under 512, while maintaining standard thermal limits and CPU clocks.