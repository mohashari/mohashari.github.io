---
layout: post
title: "Optimizing Vector Database Retrieval: HNSW Index Tuning and Product Quantization in pgvector"
date: 2026-07-29 08:00:00 +0700
tags: [pgvector, postgresql, database-performance, hnsw, vector-search]
description: "A deep dive into scaling pgvector in production, covering HNSW index tuning, halfvec storage, and high-performance two-stage binary quantization."
image: "https://picsum.photos/seed/9054/1080/720"
thumbnail: "https://picsum.photos/seed/9054/400/300"
---

It is 3:00 AM on a Friday, and your company's semantic search service is throwing OOM (Out Of Memory) errors. Your PostgreSQL replica instance, which serves 8 million embeddings of 1536 dimensions using `pgvector`, has hit a wall. As concurrent search queries spike, the OS page cache thrashing goes through the roof, query latency climbs from a comfortable 30ms to a catastrophic 1200ms, and the Linux OOM killer finally puts the database process out of its misery. The root cause is simple: your HNSW vector index has grown to 55 GB, exceeding the instance's available `shared_buffers` and RAM, forcing Postgres to page graph nodes from disk for every single search step. In high-scale production vector search, RAM is your most expensive constraint, and raw HNSW graphs are memory-hungry beasts. This post details how to tune pgvector's HNSW parameters for production workloads, reduce storage footprints with FP16 scalars (`halfvec`), clarify the state of Product Quantization in the pgvector ecosystem, and achieve up to a 32x reduction in index size using two-stage binary quantization retrieval.

## The Cost of Naive Vector Search in PostgreSQL

By default, searching for similar vectors without an index in `pgvector` performing an exact k-nearest neighbor (KNN) search. While this yields 100% recall (the exact closest matches), it performs a full table scan, calculating the distance between the query vector and every single row in the database. For a table of 10,000 vectors, exact KNN is fast enough to go unnoticed. For 10 million vectors, it is a complete CPU bottleneck, locking up database connections and taking seconds to return.

To make vector search usable at scale, we must transition to Approximate Nearest Neighbor (ANN) search. The two index types in pgvector are `IVFFlat` (Inverted File Flat) and `HNSW` (Hierarchical Navigable Small World). 

While `IVFFlat` has faster build times and smaller memory overhead, it suffers from poor recall-vs-latency trade-offs under high query loads and requires training on representative data. If your data distribution shifts, the IVFFlat centroids become outdated, necessitating a full rebuild. HNSW, on the other hand, builds a multi-layered graph index that doesn't require a separate training phase, updates incrementally with new inserts, and offers superior recall and lower latency at high queries-per-second (QPS). However, HNSW is resource-intensive.

## Deep Dive: HNSW Graph Parameters in pgvector

HNSW builds a hierarchical graph where the bottom layer contains all vectors and higher layers contain subsets of vectors forming a "highway system" for fast traversal. When creating an HNSW index, two parameters determine graph density and search path complexity:

*   **`m`**: The maximum number of bidirectional connection pointers created for each new node. A higher `m` improves recall in high-dimensional spaces (like 1536-dimension embeddings) because nodes have more neighbors to traverse, preventing search paths from getting stuck in local minima. However, higher `m` increases index size and memory footprint.
*   **`ef_construction`**: The size of the dynamic candidate list evaluated during index construction. A higher value increases search accuracy during build time, ensuring nodes are connected to the absolute closest neighbors. This yields a better-quality graph, but increases build times exponentially.

<script src="https://gist.github.com/mohashari/d52c6362f45ca5da3031b8fb9588523c.js?file=snippet-1.sql"></script>

## Query-Time Tuning: The ef_search Tradeoff

Unlike build-time parameters which are baked into the physical index on disk, `hnsw.ef_search` is a dynamic GUC (Grand Unified Configuration) variable that can be changed per session, transaction, or even query. It dictates how many nearest neighbors are tracked in the dynamic candidate list during graph traversal.

*   `hnsw.ef_search` defaults to 40.
*   If we decrease `ef_search` (e.g., to 16 or 24), we speed up queries significantly by limiting graph traversal depth. But recall suffers.
*   If we increase `ef_search` (e.g., to 120 or 200), we explore more branches of the graph, pushing recall towards 99% at the expense of CPU cycles and search latency.

<script src="https://gist.github.com/mohashari/d52c6362f45ca5da3031b8fb9588523c.js?file=snippet-2.sql"></script>

## The Memory Wall: Calculating HNSW Footprint

Let's walk through a concrete calculation to illustrate the scaling problem. Consider a dataset of 5,000,000 vectors with 1536 dimensions (typical for OpenAI's `text-embedding-3-small` or standard Cohere models). Each vector dimension is stored as a 32-bit floating-point number (4 bytes).

*   Raw vector size: `1536 dimensions * 4 bytes = 6,144 bytes per row`
*   For 5M vectors: `5,000,000 * 6,144 bytes = 30.72 GB`
*   In PostgreSQL, page header and tuple overhead add ~40 bytes per row, bringing the table size to roughly 31 GB.

Now, let's look at the HNSW index overhead. Under the hood, pgvector stores the HNSW graph as a collection of index pages. For each vector in the index, we must store:
1.  The vector data itself (to compute distances during traversal): `6,144 bytes`
2.  The list of connections (pointers) for each layer. The number of connections per node is bounded by `m` at the base layer, and `2 * m` at upper layers.

With `m = 24`, the index needs to store up to 24 pointers (each being a 4-byte or 8-byte item identifier) plus internal node metadata. In practice, the HNSW index size for a `vector(1536)` dataset with `m = 24` is approximately 1.2x to 1.5x the size of the raw table.

*   HNSW Index Size: `~45 GB`
*   Total required RAM to keep both the table and index in memory: `~76 GB`

If your database instance only has 64 GB of RAM, and you allocate 16 GB to `shared_buffers`, the remaining 48 GB is left for the OS page cache. Once your active dataset and indexes exceed this limit, PostgreSQL cannot hold the HNSW graph in memory. When a query runs, the database engine must traverse the graph across different layers. Since HNSW traversal is path-dependent (you cannot predict which node you will visit next until you compute distances for current neighbors), Postgres cannot pre-fetch pages. It is forced to perform random disk I/O reads.

If a single query requires fetching 20 index pages from SSD, and each SSD read takes 1.5ms, your query latency spikes from 3ms to 30ms. Under concurrency, these disk reads queue up, CPU wait times skyrocket, connection pools exhaust, and your replica database falls over.

## Scalar Quantization: Cutting Storage in Half with `halfvec`

To solve the memory wall, we must reduce the footprint of the vector representations. PostgreSQL `pgvector` 0.5.0 introduced the `halfvec` data type. `halfvec` stores vectors using 16-bit half-precision floats (FP16). Instead of 4 bytes per dimension, FP16 uses 2 bytes.

Let's see what happens to our 5,000,000 vector dataset:
*   Raw vector size: `1536 dimensions * 2 bytes = 3,072 bytes per row`
*   For 5M vectors: `5,000,000 * 3,072 = 15.36 GB` (down from 30.72 GB)
*   The HNSW index built on `halfvec` also shrinks proportionally because it stores 16-bit floats in its graph nodes. The index size drops from 45 GB to ~22 GB.
*   Total RAM footprint: `~37 GB` (completely fits within a standard 64 GB RAM instance, leaving plenty of room for system cache and `shared_buffers`).

The reduction from FP32 to FP16 represents a loss of precision, but in practice, semantic embeddings do not utilize the extreme dynamic range of 32-bit floats. Extensive testing shows that using `halfvec` maintains a cosine similarity recall of over 99.5% compared to the original FP32 vectors.

<script src="https://gist.github.com/mohashari/d52c6362f45ca5da3031b8fb9588523c.js?file=snippet-3.sql"></script>

## The Quantization Landscape: PQ vs. SQ in pgvector

A common source of confusion is whether `pgvector` supports **Product Quantization (PQ)**. Product Quantization splits a vector into $M$ sub-vectors, runs K-means clustering on each sub-vector space to build a codebook of centroids, and then represents each vector as an array of centroid indices (typically 1 byte per sub-vector). This compresses a 1536-dimensional vector (6,144 bytes) to just 96 bytes (a 64x reduction!).

As of mid-2026, **`pgvector` does not natively support Product Quantization (PQ)** for its HNSW or IVFFlat indexes. Native PQ requires storing and distributing codebooks, calculating asymmetric distances on the fly (comparing a query vector with codebook centroids), and rebuilding codebooks when data distributions shift. 

To avoid this complexity, the pgvector maintainers prioritized:
1.  **Scalar Quantization (SQ)**: Via the `halfvec` data type, which compresses 32-bit floats to 16-bit floats.
2.  **Binary Quantization (BQ)**: Via expression indexes mapping float values to 1-bit strings.

If your architecture absolutely requires true Product Quantization natively inside PostgreSQL, you must look at alternative Postgres extensions such as **`pgvecto.rs`** (written in Rust), which natively implements HNSW with PQ. Alternatively, you can implement application-side PQ using libraries like Faiss, storing the quantized codebooks and code bytes in standard Postgres table columns, and performing asymmetric distance calculations using custom PL/pgSQL functions. However, for most production environments, pgvector's native Binary Quantization combined with a two-stage retrieval pipeline is a much simpler and highly effective alternative.

## 32x Compression: Binary Quantization and Two-Stage Retrieval

For massive datasets or highly memory-constrained environments, even FP16 is too large. This is where **Binary Quantization (BQ)** comes in. 

BQ maps each dimension of a vector to a single bit. If the dimension value is greater than 0, it maps to `1`. If it is less than or equal to 0, it maps to `0`. 

Let's look at the math for 1536 dimensions:
*   Standard FP32 representation: `1536 * 32 bits = 49,152 bits (6,144 bytes)`
*   BQ representation: `1536 * 1 bit = 1,536 bits (192 bytes)`
*   Compression ratio: **32x reduction** in raw vector footprint.

For 5M vectors, the quantized representation takes only 960 MB. An HNSW index built on these binary vectors (using Hamming distance to count bit-level differences) is incredibly small—often less than 2 GB. The entire index fits comfortably in RAM on a cheap, entry-level database instance.

However, BQ destroys a massive amount of vector information. Using BQ alone for retrieval typically drops recall to 40%–60%. To achieve high accuracy while maintaining the speed and memory benefits of BQ, we implement a **two-stage retrieval pipeline** (also known as retrieve-and-rerank):

1.  **Stage 1 (Coarse Retrieval)**: Search the binary-quantized HNSW index using Hamming distance (`<~>`). Retrieve a larger candidate pool of results (e.g., `LIMIT 100` or `LIMIT 200`). This is extremely fast because Hamming distance is calculated using hardware-accelerated bitwise operations (POPCNT), and the entire index is cached in memory.
2.  **Stage 2 (Re-ranking)**: Perform exact distance calculations using the original FP32 vectors, but only on the 100 candidates returned in Stage 1. This re-ranking step happens in milliseconds because the search space has been pruned from millions of rows to just 100.

By using this two-stage approach, we can achieve over 95%–98% recall of the original FP32 index while saving up to 90% of index RAM.

<script src="https://gist.github.com/mohashari/d52c6362f45ca5da3031b8fb9588523c.js?file=snippet-4.sql"></script>

<script src="https://gist.github.com/mohashari/d52c6362f45ca5da3031b8fb9588523c.js?file=snippet-5.sql"></script>

## Python Integration for Dynamic SLA Gating

In a production API, you must handle typing and parameter passing carefully. The following script shows how to implement the two-stage query using `psycopg` (v3). It scopes `hnsw.ef_search` to the transaction block, preventing database-wide resource exhaustion, and casts parameters explicitly.

<script src="https://gist.github.com/mohashari/d52c6362f45ca5da3031b8fb9588523c.js?file=snippet-6.py"></script>

## Monitoring and Maintenance

To manage these optimization strategies, you need visibility into how your indexes are scaling. The following query helps you monitor table and index sizing to verify memory savings.

<script src="https://gist.github.com/mohashari/d52c6362f45ca5da3031b8fb9588523c.js?file=snippet-7.sql"></script>

## Production Failure Modes and Best Practices

When deploying HNSW indexes at scale, watch out for these three silent production killers:

### 1. The Index Build Memory Crash
When building HNSW indexes on large tables, PostgreSQL uses `maintenance_work_mem`. If this is left at the default (typically 64MB or 256MB), Postgres will exhaust it within the first few hundred thousand vectors. It will then spill temporary files to disk. Because HNSW relies on random graph lookups to insert new nodes, spilling to disk causes severe thrashing. An index build that should take 15 minutes can run for 30 hours, or crash the database engine due to high I/O wait times triggering health check failures.
*   **Fix**: Always scale `maintenance_work_mem` to at least the size of the vectors being indexed. If you index 5 million 1536-dim vectors, that's roughly 30GB of raw vector data. Setting `maintenance_work_mem` to `16GB` or `24GB` prevents disk writes during construction.

### 2. The "Missing" Index Scan (Index Filter Suppressed)
If your application uses query-level filters (e.g., `WHERE user_id = 'xyz' AND tenant_id = 4`), and you have a general HNSW index on the vector column, PostgreSQL's query planner will often decide to ignore the HNSW index if it believes filtering by `tenant_id` first is more efficient. Or, if it uses the HNSW index, it may have to scan many nodes that do not match the filter, leading to poor recall because the HNSW graph search terminates early without finding enough matching nodes.
*   **Fix**: Create composite partial HNSW indexes or partition your tables. For instance, if you partition by `tenant_id`, each partition gets its own smaller HNSW index. This reduces graph traversal complexity, keeps index sizes small enough to fit in RAM, and guarantees correct query execution.

### 3. Graph Bloat and REINDEX
Because HNSW indexes in pgvector are updated incrementally when rows are updated or deleted, the graph can become fragmented. Deleted nodes leave holes, and updates cause new node insertions without optimal connection restructuring. Over time, search recall degrades and index size increases.
*   **Fix**: Set up a cron job or scheduled task to run `REINDEX INDEX CONCURRENTLY idx_products_hnsw_embedding` during low-traffic periods. This rebuilds the graph cleanly in the background without locking queries.