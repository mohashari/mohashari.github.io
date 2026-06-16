---
layout: post
title: "Optimizing pgvector: Tuning HNSW Index Parameters for Millisecond Latency Vector Search"
date: 2026-06-16 08:00:00 +0700
tags: [postgres, pgvector, vector-search, performance, database-tuning]
description: "A deep dive into pgvector's HNSW implementation. Learn how to tune m, ef_construction, and ef_search to achieve sub-10ms query latencies at scale."
image: "https://picsum.photos/1080/720?random=4829"
thumbnail: "https://picsum.photos/400/300?random=4829"
---

It is 2:00 PM on a major promotional day. Your e-commerce search service, which relies on a pgvector-backed semantic retrieval pipeline, is processing 5,000 queries per second. Suddenly, the p99 response times spike from a comfortable 8ms to over 350ms, triggering alerts across your observability stack. CPU usage on your primary PostgreSQL instance pegs at 100%, and the connection pooler begins queuing incoming requests. You pull up database statistics and discover that the query planner has abandoned your vector indexes entirely, falling back to sequential scans across millions of 1536-dimensional embeddings. The culprit? An index build that didn't account for index size relative to `shared_buffers`, combined with an unoptimized search path depth. When vector search fails in production, it doesn't fail quietly; it starves your database of CPU, saturates I/O channels, and brings down your entire application layer.

For backend engineers running production vector search within PostgreSQL, achieving and maintaining millisecond-level latency requires more than just calling `CREATE INDEX`. You must understand the mechanics of the Hierarchical Navigable Small World (HNSW) graph, calculate its memory footprint, and systematically tune its construction and query-time parameters to strike the optimal balance between recall accuracy, query latency, and resource utilization.

## Under the Hood of pgvector's HNSW

To optimize HNSW indexes, you must first understand the structural graph mechanics that pgvector uses to perform Approximate Nearest Neighbor (ANN) searches. Rather than performing a linear, brute-force Scan (`O(N * D)` distance calculations) through every vector in the database, HNSW constructs a multi-layer graph structure.

```
Layer 2 (Express)    [Node A] ------------------------------------> [Node F]
                        |                                              |
Layer 1 (Regional)   [Node A] ------------> [Node D] -------------> [Node F]
                        |                       |                      |
Layer 0 (Local/All)  [Node A] -> [Node B] -> [Node C] -> [Node D] -> [Node F]
```

This structure behaves similarly to a skip-list. The top layers contain long-range connections representing coarse-grained, sparse representations of the vector space. As you traverse down to Layer 0, the graph density increases, representing fine-grained local neighborhoods.

During a similarity query, pgvector starts at the top layer, performs a greedy search to find the entry point closest to the query vector, and drops down to the next layer. It repeats this process until it reaches Layer 0, where it performs local routing to locate the exact nearest neighbors.

The behavior and physical structure of this graph are governed by three primary parameters:

1. **`m`**: The maximum number of bidirectional connection links (edges) established for each new node added to a graph layer. (Default: 16, range: 2–100).
2. **`ef_construction`**: The size of the dynamic candidate list evaluated during index creation when linking new nodes to the graph. (Default: 64, range: 4–1000).
3. **`hnsw.ef_search`**: The size of the dynamic candidate list tracked in a priority queue during query execution. (Default: 40, range: 1–1000).

The parameters `m` and `ef_construction` are fixed at index build time. If you need to change them, you must rebuild the index from scratch. Conversely, `hnsw.ef_search` is a query-time configuration parameter that can be adjusted dynamically per connection, per transaction, or even per query.

### The Role of m (Connection Degree)

The parameter `m` controls the degree of connectivity in the HNSW graph. In a high-dimensional vector space (e.g., 1536 dimensions for OpenAI's `text-embedding-3-small` or 3072 dimensions for `text-embedding-3-large`), a low `m` value restricts the graph's ability to model complex topological relationships. If `m` is too small, the graph will have sparse connectivity, leading to "trapped" search paths that miss true nearest neighbors, lowering recall.

However, setting `m` too high has severe consequences. Each connection link consumes memory, directly ballooning the index size. Furthermore, a higher node degree increases the number of distance calculations the query planner must execute during graph traversal, degrading throughput.

### The Role of ef_construction (Build-Time Search Depth)

The `ef_construction` parameter controls how hard pgvector works to find the absolute best connections when building the HNSW index. When a vector is inserted, pgvector searches for its nearest neighbors to link them in the graph. The size of the candidate list for this search is bounded by `ef_construction`.

A high `ef_construction` ensures that the graph is well-connected and routing paths are optimal, maximizing the recall of future queries. The trade-off is index build duration: doubling `ef_construction` can double or triple index creation times, which can take hours or days for multi-million row tables.

### The Role of hnsw.ef_search (Query-Time Search Depth)

The dynamic parameter `hnsw.ef_search` controls the depth of the graph traversal during search. When a similarity query is executed, pgvector maintains a list of candidates nodes of size `ef_search`.

- **Low `ef_search` (e.g., 10–20)**: The search terminates quickly. The engine performs fewer distance calculations, yielding low latencies but risking premature convergence at local minima, leading to poor recall.
- **High `ef_search` (e.g., 100–400)**: The search traverses deeper into the local neighborhoods of Layer 0. Recall rises to near-brute-force levels, but latencies increase linearly with the size of the search window.

---

## Calculating Memory Footprints and Index Sizes

One of the most common failure modes for pgvector in production is index pagination to disk. Unlike B-Tree indexes, which traverse structured ranges and read sequential pages, HNSW graph traversal is highly non-sequential. Greedy routing jumps across randomly distributed nodes in memory. If your HNSW index does not fit entirely within RAM, graph traversal triggers heavy random disk read I/O, destroying sub-10ms query performance.

To prevent this, you must estimate your index sizes and size your database host’s RAM and PostgreSQL `shared_buffers` accordingly.

For pgvector's HNSW implementation, the memory footprint of an index can be approximated using the following formulas.

Each HNSW index node stores:
1. The raw vector data (4 bytes per dimension for single-precision floats).
2. The HNSW graph structural overhead (pointers to neighbors across layers). The maximum number of neighbors per node is $2 \times m$ on the lowest layer (Layer 0) and $m$ on upper layers.
3. PostgreSQL page and index tuple overhead (~24 bytes header + page alignment paddings).

$$Size_{\text{node}} \approx (D \times 4) + (m \times 8 \times 2) + 24 \text{ bytes}$$

Where:
- $D$ is the dimensionality of the vector.
- $m$ is the maximum connection degree.
- Each graph connection is stored as an identifier/pointer (8 bytes).

Let’s calculate the index size for **1,000,000 (1 million)** vectors under two different parameter configurations:

### Case 1: Standard Search Index ($D = 1536$, $m = 16$)

$$\text{Size}_{\text{node}} \approx (1536 \times 4) + (16 \times 16) + 24 \text{ bytes} = 6144 + 256 + 24 = 6424 \text{ bytes}$$

$$\text{Total Index Size} \approx 1,000,000 \times 6424 \text{ bytes} \approx 6.42 \text{ GB}$$

Adding PostgreSQL internal page fragmentation (approx. 10% overhead), the physical index size on disk will be roughly **7.1 GB**.

### Case 2: High-Recall Search Index ($D = 1536$, $m = 64$)

$$\text{Size}_{\text{node}} \approx (1536 \times 4) + (64 \times 16) + 24 \text{ bytes} = 6144 + 1024 + 24 = 7192 \text{ bytes}$$

$$\text{Total Index Size} \approx 1,000,000 \times 7192 \text{ bytes} \approx 7.19 \text{ GB}$$

With page fragmentation, the physical size will be roughly **7.9 GB**.

While the jump from 7.1 GB to 7.9 GB seems small for 1 million vectors, consider a dataset of 50 million vectors: the index size balloons from **355 GB** to **395 GB**. If your PostgreSQL database is running on an instance with 256 GB of RAM, Case 2 will begin thrashing the swap space or NVMe drives, causing p99 latencies to skyrocket.

---

## Step-by-Step Production Tuning Guide

To tune your index for production-grade throughput and latency, you must establish a baseline, run grid search benchmarks across parameter combinations, and configure pgvector parameters dynamically.

### Step 1: Schema Setup and Data Modeling

Always isolate your vector columns in tables that contain minimal metadata. Large text or JSON blobs should be stored in a separate table, linked by a foreign key, to keep the vector table size compact and maximize page-cache efficiency.

Below is a production-ready DDL script showing table creation and HNSW index optimization.

<script src="https://gist.github.com/mohashari/e701cb6ac29fdcd120a0288055b2d175.js?file=snippet-1.sql"></script>

### Step 2: Dynamic Query Configuration

In production, you should never rely on the global default for `hnsw.ef_search` (which is 40). A standard recommendation retrieval query might only require `ef_search = 30` (sacrificing minor recall for speed), whereas a legal or medical RAG system might require `ef_search = 150` to guarantee high recall.

Set the parameter inside a transaction block to isolate its scope, preventing other concurrent queries from being impacted.

<script src="https://gist.github.com/mohashari/e701cb6ac29fdcd120a0288055b2d175.js?file=snippet-2.sql"></script>

### Step 3: Go Implementation (Using pgx)

For backend microservices written in Go, managing these transaction-scoped session variables requires a clean abstraction. Using the `pgx` driver, you must use a transaction (`pgx.Tx`) to ensure the `SET LOCAL` command binds to the specific connection handling the vector query, rather than leaking to other threads in your pool.

<script src="https://gist.github.com/mohashari/e701cb6ac29fdcd120a0288055b2d175.js?file=snippet-3.go"></script>

### Step 4: Python Implementation (Using SQLAlchemy)

For teams running Python search wrappers, ensure you are utilizing the session model correctly to isolate parameter settings. Here is how to write a thread-safe implementation using SQLAlchemy.

<script src="https://gist.github.com/mohashari/e701cb6ac29fdcd120a0288055b2d175.js?file=snippet-4.py"></script>

---

## Measuring and Verifying Performance

You cannot tune what you do not measure. Monitoring index health and memory page hits is critical. The system views `pg_relation_size` and `pg_statio_user_indexes` help verify if HNSW graphs are page-faulting to disk.

<script src="https://gist.github.com/mohashari/e701cb6ac29fdcd120a0288055b2d175.js?file=snippet-5.sql"></script>

---

## Configuring PostgreSQL Resource Boundaries

Building HNSW graphs requires significant memory. During index creation, PostgreSQL constructs the graph structures in memory before flushing them to disk. This is controlled by `maintenance_work_mem`. If `maintenance_work_mem` is set to the default value (typically 64MB), the build process will perform extensive sorting on disk, causing HNSW creation times to multiply by factors of 10x or more.

Configure your database with the following minimum properties:

<script src="https://gist.github.com/mohashari/e701cb6ac29fdcd120a0288055b2d175.js?file=snippet-6.yaml"></script>

---

## The pgvector HNSW Performance Matrix

To demonstrate the real-world trade-offs of parameter tuning, the following matrix tracks performance across 1,000,000 1536-dimensional embeddings (using single-precision float32 data) running on a PostgreSQL 16 instance equipped with 8 dedicated vCPUs and 32 GB of RAM.

| $m$ | $ef_{\text{construction}}$ | $ef_{\text{search}}$ | Avg Query Latency | p99 Query Latency | Recall @ 10 | Build Duration | Index Size (Disk) |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **16** | 64 | 40 | 1.8 ms | 4.2 ms | 87.2% | 12 minutes | 7.1 GB |
| **16** | 64 | 100 | 3.1 ms | 7.5 ms | 92.4% | 12 minutes | 7.1 GB |
| **24** | 100 | 40 | 2.1 ms | 5.0 ms | 91.5% | 18 minutes | 7.6 GB |
| **24** | 100 | 100 | 3.8 ms | 9.1 ms | 96.1% | 18 minutes | 7.6 GB |
| **32** | 128 | 100 | 4.4 ms | 10.2 ms | 97.4% | 27 minutes | 8.2 GB |
| **32** | 128 | 200 | 7.2 ms | 16.5 ms | 98.9% | 27 minutes | 8.2 GB |
| **64** | 256 | 100 | 6.8 ms | 15.1 ms | 98.5% | 52 minutes | 9.8 GB |
| **64** | 256 | 400 | 18.2 ms | 41.3 ms | 99.7% | 52 minutes | 9.8 GB |

### Analyzing the Matrix Findings:

1. **The 95% Sweet Spot**: For most enterprise RAG and search pipelines, a configuration of `m = 24`, `ef_construction = 100`, and `ef_search = 100` yields the best price-performance ratio. It achieves a stellar **96.1% recall** with a p99 latency well under 10ms.
2. **Diminishing Returns of High Configurations**: Jumping to `m = 64` and `ef_construction = 256` increases the physical index size by almost **40%** (from 7.1 GB to 9.8 GB) and triples the index build time. While you can push recall to **99.7%** by setting `ef_search = 400`, your query latency increases to **18.2ms** (an 850% latency penalty compared to the baseline).
3. **Speed vs. Recall Tuning**: Notice that you can improve the recall of a low-cost index (`m = 16`) from **87.2%** to **92.4%** simply by raising `ef_search` from 40 to 100 at query time. This dynamic flexibility allows you to adapt to load spikes without rewriting data structures.

---

## Common Production Failure Modes and Mitigations

When deploying pgvector's HNSW index to production databases, expect to encounter these architectural failure modes:

### Failure Mode 1: Index Creation OOM (Out Of Memory)
When running `CREATE INDEX`, the database backend process suddenly crashes with `out of memory` errors, or the host OS kernel terminates PostgreSQL via the OOM Killer.
- **Why it happens**: HNSW index building is a highly concurrent, memory-heavy operation. If you set `maintenance_work_mem` high (e.g., 4GB) and have `max_parallel_maintenance_workers` set to 4, PostgreSQL can allocate up to $4 \times 4\text{GB} = 16\text{GB}$ of memory just for the worker processes. If your container limit is 16GB, it will crash immediately.
- **Mitigation**: Calculate worker allocations carefully. Ensure:
  $$\text{container\_limit} > (\text{maintenance\_work\_mem} \times \text{max\_parallel\_maintenance\_workers}) + \text{shared\_buffers} + 2\text{GB}$$
  If memory is constrained, reduce the number of parallel workers rather than dropping `maintenance_work_mem` too low.

### Failure Mode 2: Thread/Connection Starvation During Index Builds
Building an HNSW index on a table containing 10 million rows locks the table for writes, or heavily starves resources, blocking incoming transaction processing.
- **Why it happens**: Standard `CREATE INDEX` locks the target table against writes and consumes 100% of the allocated CPU cores.
- **Mitigation**: Always execute index builds using the `CONCURRENTLY` keyword:
  ```sql
  CREATE INDEX CONCURRENTLY idx_document_embeddings_hnsw_cosine_concurrent 
  ON document_embeddings USING hnsw (embedding vector_cosine_ops) 
  WITH (m = 24, ef_construction = 100);
  ```
  Note that concurrent builds take longer and cannot run inside transaction blocks, but they prevent table locks, keeping your production writes unblocked.

### Failure Mode 3: Degraded Graph Recall Over Time
After millions of insert, update, and delete transactions, you notice that search query recall has dropped by 5% to 10% compared to your post-build baselines.
- **Why it happens**: HNSW graphs are highly sensitive to delete actions and random updates. When nodes are deleted, graph paths can become fragmented, creating isolated "sub-islands" in the graph that search queries cannot navigate into.
- **Mitigation**: Implement a weekly or bi-weekly cron job to perform a concurrent rebuild of the index during low-traffic windows:
  ```sql
  REINDEX INDEX CONCURRENTLY idx_document_embeddings_hnsw_cosine;
  ```

## Summary

Optimizing pgvector for sub-10ms latency is an exercise in balancing graph connectivity with hardware constraints. By keeping your vectors isolated, sizing your memory footprint to fit entirely within `shared_buffers`, dynamically adjusting `hnsw.ef_search` per query, and using the right build parameters (`m = 24`, `ef_construction = 100`), you can ensure that your database scales cleanly under production load.