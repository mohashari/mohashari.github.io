---
layout: post
title: "Architecting a Real-Time Hybrid Search System: Combining Dense Vectors and BM25 Indexes in Elasticsearch"
date: 2026-07-15 08:00:00 +0700
tags: [elasticsearch, hybrid-search, vector-search, ai-engineering, systems-architecture]
description: "A production-focused guide to designing, indexing, and tuning a high-throughput hybrid search engine combining dense vectors and BM25 in Elasticsearch."
image: "https://picsum.photos/seed/7049/1080/720"
thumbnail: "https://picsum.photos/seed/7049/400/300"
---

Every engineering team starting out with semantic search goes through the same naive phase: they spin up a pretrained BERT or OpenAI embedding model, index their product catalog into a vector database, and run vector similarity queries. Within days of going live in production, they are hit with a wave of customer complaints. Users searching for exact SKU numbers, specific part numbers like "DB-1823", or precise brands ("Apple MacBook Air M3") are presented with highly "semantically similar" but completely incorrect products. This is the zero-shot generalization failure of dense retrievers. While vector search is brilliant at understanding human intent and synonyms—mapping "winter jacket" to "warm parka"—it is mathematically incapable of handling high-precision exact keyword matches, SKU codes, and negation filters. In production, a reliable search engine cannot rely on vectors alone. You must combine the semantic strength of dense vectors with the lexical precision of BM25 (best matching 25) text indexes. Architecting this hybrid search system at scale requires tackling index-time bottlenecking, query-time latency, and the massive RAM footprint of HNSW graphs.

## The Production Architecture: Decoupling Vector Inference

To implement hybrid search in a production system handling hundreds of write transactions per second (TPS) and thousands of query transactions per second (QPS), you must decouple the vector generation process from both the database transaction boundary and the user's primary write path. Generating a vector embedding requires passing text through a deep learning model. This is a highly compute-intensive, CPU- or GPU-bound operation. Under load, synchronous vector generation in the request lifecycle is a recipe for API gateway timeouts and thread pool starvation.

The primary database (e.g., PostgreSQL or MongoDB) should emit change-data-capture (CDC) events via Debezium to a Kafka topic, or the application should write directly to a message broker like RabbitMQ. An asynchronous worker pool reads documents from the queue, batches them, queries a dedicated embedding server (such as HuggingFace Text Embeddings Inference (TEI) running on dedicated hardware with GPU acceleration or optimized CPU instructions like AVX-512), attaches the resulting float arrays, and writes them to Elasticsearch using the Bulk API.

At query time, the search API receives a query, hashes the normalized query string, and queries Redis. If the embedding is cached, it retrieves it instantly (<1ms). On a cache miss, it calls the embedding server, retrieves the vector, and caches it. Next, the search API constructs a single Elasticsearch hybrid search request, passing the vector to the vector search block and the query string to the text block. Elasticsearch executes both in parallel and merges the results.

## Index Schema Mapping and HNSW Parameter Tuning

Elasticsearch 8.x handles dense vector search using Lucene's implementation of HNSW (Hierarchical Navigable Small World) graphs. HNSW graphs are stored outside the JVM heap in direct memory, and their memory footprint can be massive. 

An index with 10 million documents using 768-dimensional float32 vectors (e.g., `bge-base-en-v1.5`) consumes:
$$10,000,000 \times 768 \times 4 \text{ bytes} = 30.72 \text{ GB}$$
of raw vector data. Lucene's HNSW graph overhead adds another 50% to 100% memory, meaning the index requires 45–60 GB of RAM. To mitigate this, we use scalar quantization (`int8`). This converts 32-bit floats to 8-bit signed integers during indexing, compressing the memory footprint by 75% ($10,000,000 \times 768 \times 1 \text{ byte} = 7.68 \text{ GB}$) with less than a 1–2% degradation in recall.

<script src="https://gist.github.com/mohashari/38ec7748e057c4c7aab078124cf3ce68.js?file=snippet-1.json"></script>

In this schema, `m` is the number of bi-directional links per node in the HNSW graph (default is 16, higher improves recall at the expense of memory and index time), and `ef_construction` represents the size of the dynamic candidate list evaluated during graph building (higher values increase index accuracy but dramatically slow down write speed). Setting `similarity` to `cosine` is recommended for text search when lengths vary, but `dot_product` is faster if your embedding pipeline guarantees unit-normalized vectors.

## The Ingestion Pipeline: Bulk Vector Generation & Asynchronous Loading

When indexing vectors, Elasticsearch builds HNSW segments. If segments are flushed too frequently (due to a short `refresh_interval` like 1s), Lucene builds dozens of small HNSW graphs. This results in heavy CPU usage during segment merges and poor query recall because traversing multiple small graphs is less accurate than traversing one consolidated graph.

The production-grade worker below batches incoming documents, requests embeddings in batch from a HuggingFace TEI server, and uses `helpers.bulk` to index.

<script src="https://gist.github.com/mohashari/38ec7748e057c4c7aab078124cf3ce68.js?file=snippet-2.py"></script>

## Executing Hybrid Search: RRF vs. Linear Scoring

There are two primary strategies for merging BM25 and vector results: Reciprocal Rank Fusion (RRF) and Linear Combination.

### 1. Reciprocal Rank Fusion (RRF)
RRF is a rank-based algorithm that discards the raw scores of individual search systems and scores documents based on their position in the result sets. It calculates a unified score using the formula:
$$RRFScore(d) = \sum_{m \in M} \frac{1}{k + r_m(d)}$$
where $r_m(d)$ is the rank of document $d$ in search system $m$, and $k$ is a constant (typically 60). RRF is ideal when the scores of the sub-searches are on entirely different scales and cannot be easily compared or normalized. 

<script src="https://gist.github.com/mohashari/38ec7748e057c4c7aab078124cf3ce68.js?file=snippet-3.py"></script>

The downside of RRF is that it throws away the actual similarity score. If your application needs to filter out irrelevant matches (e.g., only show results with a match score above a certain confidence threshold), RRF fails because it returns normalized rank-based scores like $0.032$.

### 2. Linear Combination
If you need score-based thresholds, you must use a linear combination. The formula is:
$$Score_{final} = (\alpha \times Score_{BM25}) + (\beta \times Score_{Vector})$$
Elasticsearch 8.x allows this by specifying a standard `query` and a parallel `knn` block at the root level, each with its own `boost` coefficient ($\alpha$ and $\beta$).

<script src="https://gist.github.com/mohashari/38ec7748e057c4c7aab078124cf3ce68.js?file=snippet-4.json"></script>

Tuning these boosts requires building a Search Query Evaluation set using tools like Ragas or custom test suites. Because BM25 scores are unbounded and depend on index statistics, while vector cosine similarity is bounded between -1 and 1, a raw linear combination can be highly unstable if the BM25 scores spike on rare terms. In production, normalize your BM25 scores before combining them, or apply log-normalization to BM25 outputs.

## Elasticsearch Tuning & Resource Optimization

To achieve high indexing throughput during initial bulk loading or major re-indexing, we must change Elasticsearch index settings. Replicating HNSW graphs requires the replica node to rebuild the HNSW index from scratch, which doubles the CPU cost. Therefore, replicas should be disabled during bulk load.

<script src="https://gist.github.com/mohashari/38ec7748e057c4c7aab078124cf3ce68.js?file=snippet-5.txt"></script>

Force-merging segments down to `max_num_segments=1` is critical before opening the index to heavy query traffic. Searching a single large HNSW graph is much faster and produces higher recall than searching across 20 small HNSW graphs, as the search path does not get stuck in local minima between independent graphs.

## Query-Side Latency & Redis Cache

Model inference is the single biggest bottleneck in a search engine. Even a fast model on an NVIDIA A10G GPU adds 5–10ms of latency. On a CPU, it can add 20–50ms. If your target SLA for search is under 30ms p99, model inference will eat up your entire latency budget.

To mitigate this, implement a caching layer. In a typical e-commerce or documentation search system, 40–60% of search queries are exact duplicates (e.g., "shoes", "iphone 15 case", "billing support"). By caching the query vector in Redis, you bypass model inference entirely for cached queries.

<script src="https://gist.github.com/mohashari/38ec7748e057c4c7aab078124cf3ce68.js?file=snippet-6.py"></script>

## Production Failure Modes and Operational Runbooks

When running a real-time hybrid search system in production, you will eventually encounter one of these three failure modes.

### 1. The Memory Cliff (Direct Memory OOM)
Unlike standard Lucene inverted indexes that rely heavily on the OS page cache for read caching, the HNSW graphs must reside in RAM to perform well. If they are paged to disk, search latency will spike from ~15ms to 2000ms+. 
A common failure mode is configuring the Elasticsearch JVM heap to 70–80% of system memory. Because HNSW memory is allocated outside the JVM Heap via Direct Memory, this leaves almost no memory for the graphs. The kernel will eventually trigger the Out-Of-Memory (OOM) killer on the Elasticsearch process once Direct Memory allocation spikes.
* **Runbook:** Set the JVM heap to 30% of system memory (never exceed 32GB to keep compressed ordinary object pointers active) and leave 70% of the memory for direct memory and the OS page cache. Use scalar quantization to reduce the raw graph memory size.

### 2. The Index Refresh Storm
When write workloads have the default `refresh_interval` set to `1s`, Lucene creates hundreds of tiny segments. During merges, the CPU hits 100% as Elasticsearch repeatedly rebuilds HNSW graphs for the merged segments. Search latency climbs from 20ms to 800ms.
* **Runbook:** Increase the `refresh_interval` to `30s` or `60s` to allow larger segments to build in memory before flushing. If you are running high-velocity live updates, leverage client-side caching to hide the slight index latency from users.

### 3. Embedding Drift (Model Mismatch)
If you deploy a code change that uses a 1536-dimension model to query an index built with a 768-dimension model, Elasticsearch will reject the query. However, if you switch models but keep the same dimensions (e.g., switching from `bge-base-en-v1.5` to `all-mpnet-base-v2`, which are both 768 dimensions), Elasticsearch won't error out, but the search results will be complete garbage because the vector space mapping is entirely different.
* **Runbook:** Implement a versioned index strategy (`products_v1`, `products_v2`) and use aliases to switch queries atomically. Never overwrite an index schema directly when changing the model.