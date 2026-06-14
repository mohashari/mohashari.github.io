---
layout: post
title: "Semantic Caching for LLM Gateways using Redis Vector Similarity Search"
date: 2026-06-14 08:00:00 +0700
tags: [ai-engineering, redis, vector-search, go, caching]
description: "Build a high-performance, low-latency semantic cache for LLM gateways using Redis VSS to slash API costs and achieve sub-15ms response times."
image: "https://picsum.photos/1080/720?random=9231"
thumbnail: "https://picsum.photos/400/300?random=9231"
---

In high-throughput conversational AI architectures and automated customer-facing agents, redundant LLM queries represent a major source of financial drain and latency degradation. A typical LLM invocation to an external model like OpenAI’s GPT-4o or Anthropic’s Claude 3.5 Sonnet costs anywhere from $0.01 to $0.05 per request and adds 1,000ms to 3,000ms of latency to user interfaces. In production systems serving thousands of active users, a massive proportion of these queries are semantically identical (e.g., "How do I reset my password?", "Help me reset password", "Forgotten password steps"). Traditional exact-match key-value caching (such as hashing the exact prompt string to MD5) fails completely here, yielding cache hit rates below 5% because users express the same intent in infinitely varied phrasing. To achieve real cost reductions and deliver sub-15ms response times on repetitive requests, gateways must bridge the semantic gap. Using Redis Vector Similarity Search (VSS) configured with a Hierarchical Navigable Small World (HNSW) index, we can build a production-grade semantic cache that matches queries based on vector embeddings, intercepting redundant traffic before it hits the LLM provider.

![Semantic Cache Flow for LLM Gateway using Redis VSS Diagram](/images/diagrams/semantic-caching-llm-gateways-redis.svg)

## The Semantic Gap of Key-Value Store Caching

Traditional caching models are simple: a hash function (like SHA-256) maps the incoming request payload to a static key. If a user asks "How do I update my profile?" and another asks "profile update how-to", the hashes diverge completely:

```text
SHA256("How do I update my profile?") -> 8f9b...
SHA256("profile update how-to")      -> 2a7c...
```

To a key-value store, these are two entirely unrelated entities, resulting in a cache miss. However, when we transform these prompts into dense vector embeddings—floating-point arrays representing the semantic meaning of the text in high-dimensional space—their coordinates in vector space align closely.

By measuring the distance between these vectors (typically using cosine similarity or inner product), we can quantify how semantically similar the prompts are. If the similarity score is above a highly calibrated threshold (e.g., cosine distance $< 0.12$ for OpenAI's `text-embedding-3-small` model), we can bypass the LLM entirely and return the cached response.

Redis is uniquely suited for this role. While traditional vector databases (like Milvus or Qdrant) are excellent for offline retrieval and massive retrieval-augmented generation (RAG) datasets, they introduce significant architectural overhead. Redis VSS runs directly in-memory, bringing sub-millisecond query performance to vector operations, enabling us to run VSS inline on the critical request path of our LLM gateway without blowing up our latency budget.

## Designing the HNSW Vector Index in Redis

To support semantic searches, we configure Redis using the RediSearch module (included in Redis Stack). We have a choice between two vector index types:

1. **FLAT:** A brute-force, exact search. It computes the distance between the query vector and every vector in the index. While it has perfect recall, its search latency scales linearly ($O(N)$), making it unacceptable for caches containing millions of entries.
2. **HNSW (Hierarchical Navigable Small World):** An approximate nearest neighbor search that organizes vectors into a multi-layered graph. It achieves logarithmic search complexity ($O(\log N)$) at the cost of slight recall degradation and higher memory footprint. 

For an LLM cache gateway, HNSW is the clear winner: sub-10ms query speeds are more critical than absolute mathematical recall. Below is the `redis-cli` command to define the HNSW vector index:

<script src="https://gist.github.com/mohashari/1ae64a0b6c237d74c18878e274db8ebf.js?file=snippet-1.sh"></script>

Let's break down the configuration choices:
* `PREFIX 2 llm_cache: llm_inflight:` tells the index to track keys starting with either prefix. We will use the `llm_inflight:` prefix to prevent cache stampedes.
* `status TAG` is a tag field allowing us to filter search results by the lifecycle status of the cached entry (`cached` vs `in_progress`).
* `DIM 1536` represents the output dimensions of OpenAI’s `text-embedding-3-small` and `text-embedding-ada-002` models.
* `DISTANCE_METRIC COSINE` measures cosine distance (where $0.0$ is identical and $1.0$ is orthogonal/opposite).
* `M 16` and `ef_construction 200` configure the HNSW graph density. Higher values improve index accuracy but increase index construction time and memory overhead.
* `ef_runtime 10` is the search-time parameter determining how many nodes to traverse during nearest-neighbor queries.

To manage this index from our Go-based LLM gateway, we utilize the `go-redis` client to check and construct the index programmatically during boot:

<script src="https://gist.github.com/mohashari/1ae64a0b6c237d74c18878e274db8ebf.js?file=snippet-2.go"></script>

## Implementing the Query Path: KNN Search and Metric Parsing

To execute a vector query, the prompt's float32 embedding slice must be converted into a raw binary byte array (little-endian representation) and queried using K-Nearest Neighbors (KNN). The following code implements this byte-encoding conversion and queries the Redis index for the nearest matching entry:

<script src="https://gist.github.com/mohashari/1ae64a0b6c237d74c18878e274db8ebf.js?file=snippet-3.go"></script>

## Orchestrating the Gateway: Similarity Scoring and Async Write-Back

The core LLM gateway process coordinates the downstream calls. Upon receiving a prompt, the gateway converts the string into an embedding. If a cache query returns an entry within the similarity distance threshold, the gateway returns the cached response, saving downstream computational cycles. If the query results in a cache miss, the gateway calls the LLM, returns the generated completion to the caller, and starts a background routine to store the new embedding and completion back to Redis.

<script src="https://gist.github.com/mohashari/1ae64a0b6c237d74c18878e274db8ebf.js?file=snippet-4.go"></script>

## Eliminating the Semantic Cache Stampede

When traffic surges, semantic caches are vulnerable to **cache stampedes** (or thundering herd problems). If 50 semantically equivalent requests (e.g., "reset my password", "can you help reset password") hit the gateway concurrently before the first response has been generated, all 50 requests will register a cache miss and trigger 50 concurrent expensive API calls to OpenAI or Anthropic.

In traditional caching, this is solved with in-memory lock structures like singleflight. However, in a distributed environment with multiple gateway instances, a local singleflight is insufficient. 

We can solve this at scale by using Redis to register **in-flight queries**. When a gateway instance registers a cache miss, it creates a temporary HASH in Redis under the prefix `llm_inflight:` that contains the query's vector embedding and sets the status tag to `in_progress`. Concurrent queries generate their embeddings, query Redis, and discover a matching vector inside the `llm_inflight:` subspace. Instead of calling the downstream LLM, they subscribe to a Redis Pub/Sub channel based on the matching in-flight ID and block until the processing instance completes the query, writes the cached result, and publishes the answer.

<script src="https://gist.github.com/mohashari/1ae64a0b6c237d74c18878e274db8ebf.js?file=snippet-5.go"></script>

## Production Deployment: Tuning Redis Stack for Vector Workloads

To host Redis VSS in production, you must use **Redis Stack** or have the RediSearch module loaded (`redisearch.so`). To prevent search latency regressions during massive query peaks, configure the container image limits and indexing strategies. Here is a production-hardened Docker Compose environment configuration:

<script src="https://gist.github.com/mohashari/1ae64a0b6c237d74c18878e274db8ebf.js?file=snippet-6.yaml"></script>

### Key Performance Configurations Explained:
* `--maxmemory 4gb`: Ensures Redis does not balloon and trigger OS out-of-memory (OOM) killer terminations. HNSW graphs run completely in-memory, requiring strict boundaries.
* `--maxmemory-policy volatile-lru`: Sets eviction to target keys with an expiration set (like `llm_cache:*`). It leaves index structure keys intact.
* `--save ""`: Disables traditional snapshotting. On massive write throughputs, snapshot forks (`bgsave`) trigger page faults and degrade real-time vector search performance. High-availability caching gateways should instead rely on replication pairs or active-active configurations.

## Hardening for Scale: Critical Failures and Resolutions

Deploying a semantic cache into enterprise traffic routes exposes architectural edge cases that will quickly crash your latency budgets or return garbage responses if left unmitigated.

### 1. Threshold Calibration and Semantic Drift
Setting the similarity threshold is the most critical operational decision in semantic caching. 
* **If it's too loose:** The cache returns false-positive hits, serving the response of "How do I delete my account?" to the query "How do I log out of my account?".
* **If it's too tight:** The hit rate drops to zero, and the system wastes processing cycles executing useless vector searches.

Different embedding models output different vector distributions. For OpenAI's `text-embedding-3-small` (normalized, 1536 dimensions):
* **Cosine Distance $\le 0.08$**: Represents near-identical queries with minor syntax variance ("Reset password" vs "How to reset my password"). Zero false-positive risk.
* **Cosine Distance $0.09 - 0.14$**: Represents semantically equivalent intents with different structures. This is the optimal production threshold range.
* **Cosine Distance $\ge 0.15$**: Introduces high risk of semantic drift. Related topics begin overlapping.

> [!WARNING]
> You must continually run offline validation scripts against production logs. Log prompts, generated embeddings, matching keys, and the resulting user-feedback markers (like thumbs up/down clicks) to dynamically tune your gateway's threshold value.

### 2. The Latency Paradox: Local ONNX Embeddings vs OpenAI API
Calling OpenAI's `/v1/embeddings` API endpoint to embed the prompt of a user requires an external HTTP round-trip. This call typically takes **100ms to 250ms**. If our cache lookup itself takes 2ms, but we must spend 200ms calling OpenAI just to retrieve the vector, we have introduced a major latency floor. For fast queries, this defeats the purpose of the cache.

To optimize the path to sub-15ms latency, run local embedding execution inside the gateway service or sidecar. By running a lightweight, quantized embedding model (such as `bge-small-en-v1.5` or `all-MiniLM-L6-v2`) via the ONNX Runtime inside the gateway pod, we bypass external network requests.
* **External Embedding API:** 100ms - 250ms network latency.
* **Local ONNX Embedding (CPU):** 4ms - 8ms execution latency.
* **Local ONNX Embedding (GPU/NPU):** <2ms execution latency.

### 3. Vector Space Invalidation Strategies
Traditional caches invalidate keys directly when underlying resources change. In semantic space, however, updating a system resource doesn't map directly to a single cache key. If a customer changes their billing model, any cached response mentioning their old pricing plan must be evicted.

To solve this, leverage tag filtering. In addition to a `status` tag, add `tenant_id` and `entity_tag` fields to your Redis VSS schema. When an entity is updated:
1. Identify the updated entity (e.g. `pricing`).
2. Run a Redis Search query to retrieve matching keys: `FT.SEARCH llm_cache_idx "@tenant_id:{tenant_123} @entity_tag:{pricing}" RETURN 0`.
3. Evict the returned keys.

## Measuring Success: An Evaluation Script

To tune the threshold and verify performance locally, you can simulate search behaviors using a Python script. This test bed populates Redis, queries it using varying similarity levels, and prints the latency and similarity distributions.

<script src="https://gist.github.com/mohashari/1ae64a0b6c237d74c18878e274db8ebf.js?file=snippet-7.py"></script>

## Conclusion

Semantic caching is a game changer for optimizing high-scale LLM gateway installations. By combining Redis Vector Similarity Search with local ONNX embedding generation and distributed in-flight locks, you can handle duplicate client intents efficiently. This architecture drops average request latencies from seconds to milliseconds while cutting downstream LLM token costs by 90% on repetitive user paths. Whether scaling transaction support interfaces or conversational database tools, moving your caching layer from exact matching to vector similarity search is a crucial step toward building production-resilient AI services.