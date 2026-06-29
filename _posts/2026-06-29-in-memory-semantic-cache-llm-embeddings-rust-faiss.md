---
layout: post
title: "Building an In-Memory Semantic Cache for LLM Embeddings Using Rust and FAISS"
date: 2026-06-29 08:00:00 +0700
tags: [rust, faiss, llm, ai-engineering, caching]
description: "Build an ultra-low latency, thread-safe in-memory semantic cache using Rust, FAISS C++ bindings, and Moka to slash LLM costs and response times."
image: "/images/diagrams/in-memory-semantic-cache-llm-embeddings-rust-faiss.svg"
thumbnail: "/images/diagrams/in-memory-semantic-cache-llm-embeddings-rust-faiss.svg"
---

In production, LLM-powered applications face two persistent bottlenecks: agonizingly slow response times (often 1.5 to 4.5 seconds per request) and ballooning API token costs. If your users frequently ask semantically equivalent questions—such as \"How do I reset my password?\" versus \"Password reset steps\"—hitting the LLM every time is a waste of resources. Standard exact-match caches (like Redis using MD5 hashes of prompt strings) fail because even a single character change or a synonym bypasses the cache entirely. To solve this, we must build a semantic cache: a layer that understands meaning by converting queries into dense vector embeddings and performing similarity searches in-memory. By utilizing Rust's raw speed and memory safety alongside Meta's highly optimized FAISS (Facebook AI Similarity Search) C++ library and the concurrent `moka` caching engine, we can intercept incoming queries and serve semantically identical requests in under 20 milliseconds, slashing upstream API costs by 40% or more.

![Building an In-Memory Semantic Cache for LLM Embeddings Using Rust and FAISS Diagram](/images/diagrams/in-memory-semantic-cache-llm-embeddings-rust-faiss.svg)

## Why Semantic Caching? (And Why Vector DBs are Too Slow)

When building a low-latency caching layer, every millisecond counts. While cloud vector databases (like Pinecone, Milvus, or Qdrant) are excellent for Retrieval-Augmented Generation (RAG) over millions of documents, they are too slow for caching. Querying an external database over gRPC or HTTP introduces network hops, serialization overhead, and connection pool contention, resulting in a base latency of 15 to 40 milliseconds. 

For a true cache, we want sub-5 millisecond lookups. This requires keeping both the vector similarity index and the cached payloads in the same process memory space. 

By binding directly to Meta's C++ library FAISS via Rust, we run similarity calculations at bare-metal speeds. FAISS utilizes CPU-level SIMD instructions (AVX2 and AVX-512) to compute vector distances over dense matrices in microseconds. Coupled with an in-memory key-value store, we eliminate network overhead entirely.

## The Semantic Cache Stack

To build this, we rely on three core components:
1. **Local Embedding Generator**: We use `all-MiniLM-L6-v2` run locally via ONNX Runtime (`ort`). Generating embeddings locally avoids the 100-200ms HTTP overhead of calling external services like OpenAI's `/v1/embeddings` API.
2. **FAISS Vector Index**: An in-memory similarity index mapping high-dimensional vectors to discrete integer IDs.
3. **Moka Cache**: A concurrent, memory-bounded cache in Rust implementing the LIRS (Low Inter-reference Recency Set) eviction policy to store the mapping of vector IDs to actual JSON responses.

<script src="https://gist.github.com/mohashari/0fa21c08c4f002c66c2b48270d4f2d81.js?file=snippet-1.txt"></script>

## Local Embedding Inference with ONNX Runtime

Generating embeddings in-process is critical for low latency. The `all-MiniLM-L6-v2` model produces 384-dimensional dense vectors and executes in 5-15 milliseconds on a single modern CPU thread. 

Our implementation loads the ONNX model, tokenizes the input string, runs inference via ONNX Runtime, and performs mean pooling over the token embeddings. Finally, we L2-normalize the vector so that we can use simple inner product calculations in FAISS to calculate Cosine Similarity.

<script src="https://gist.github.com/mohashari/0fa21c08c4f002c66c2b48270d4f2d81.js?file=snippet-2.txt"></script>

## Thread-Safe FAISS Vector Indexing

FAISS is written in C++ and its Rust bindings do not automatically guarantee thread safety for write operations. To prevent memory corruption or race conditions under concurrent workloads, we wrap the FAISS index inside a read-write lock (`RwLock`). 

Additionally, because standard FAISS indices store vectors sequentially and only return positional indexes, we wrap the core `FlatIndex` in an `IdMap`. This allows us to assign a custom `i64` key to each vector, which maps directly to the entry ID in our in-memory key-value store.

<script src="https://gist.github.com/mohashari/0fa21c08c4f002c66c2b48270d4f2d81.js?file=snippet-3.txt"></script>

## The Coordination and Lookup Layer

The main `SemanticCache` coordinator binds the embedding generator, the FAISS index, and the Moka cache together. 

When a query is received, we generate its embedding, search the FAISS index for the closest vector, and check if the similarity score matches or exceeds our threshold $\theta$ (typically between `0.82` and `0.90` depending on the domain). If a match is found, we retrieve the cached string from the Moka store using the ID returned by FAISS.

<script src="https://gist.github.com/mohashari/0fa21c08c4f002c66c2b48270d4f2d81.js?file=snippet-4.txt"></script>

## Populating the Cache and Handling Cache Stampedes

Under high load, a cache miss on a common query can trigger a "cache stampede." If 50 requests arrive concurrently for the same query, you do not want to trigger 50 external LLM API calls and write 50 duplicate vectors to the index. 

We write a thread-safe `insert` method and implement basic synchronization using a coalescing manager to resolve cache misses safely.

<script src="https://gist.github.com/mohashari/0fa21c08c4f002c66c2b48270d4f2d81.js?file=snippet-5.txt"></script>

## The Eviction Mismatch: Keeping FAISS and Moka in Sync

A significant issue when running FAISS in tandem with an expiring key-value cache in production is the eviction discrepancy. Moka automatically evicts entries based on TTL (Time to Live) or capacity limits. However, the FAISS index does not dynamically shrink; it holds onto the vector coordinates indefinitely. 

If we don't address this, the FAISS index will fill up with "zombie" vectors that point to non-existent Moka cache entries. Similarity queries will return these dead IDs, forcing us to discard the result and perform a full cache miss cycle anyway, while wasting CPU cycles on vector distance calculations.

FAISS indices do not support cheap dynamic deletion. Removing a vector requires expensive memory shifting. The industry-standard solution is to periodically rebuild the index from the live values inside the K-V store. 

We implement an asynchronous background worker that extracts all active values from Moka, constructs a fresh index, and swaps it atomically in memory.

<script src="https://gist.github.com/mohashari/0fa21c08c4f002c66c2b48270d4f2d81.js?file=snippet-6.txt"></script>

## Production Gotchas and Crucial Considerations

Before deploying an in-memory semantic cache into production, you must evaluate several structural failure modes.

### 1. The Threshold $\theta$ Sweet Spot and False Hits
The threshold $\theta$ determines whether a query matches a cached query. Setting it too low causes "false hits," where the system returns a completely unrelated cached answer. Setting it too high results in cache misses, rendering the cache useless.

For cosine similarity (with L2-normalized vectors):
*   **0.92+**: Too strict. It only matches minor spacing adjustments or capitalizations.
*   **0.85 - 0.88**: The sweet spot for general Q&A systems.
*   **Below 0.78**: Danger zone. At this level, a query like \"how to build a table in my dining room\" can match \"how to create a schema table in MySQL,\" leading to highly confusing user experiences.

In production, you should log every similarity match along with the actual input and matched queries. Run periodic scripts to calculate your false positive rate and adjust the threshold dynamically.

### 2. Lock Contention on Writes
Under heavy write load, the `RwLock` wrapping the FAISS index will block reader threads. While reads are parallelized, a cache write blocks all read requests during the insertion. 

If your application experiences high write throughput, buffer the vector insertions using a channel (e.g., a tokio `mpsc` channel) and write-batch them every 100ms. This prevents the lock from being constantly acquired and released by different threads.

### 3. Memory Footprint and OOM Safety
Because both FAISS and Moka run in-process, their memory usage will grow in proportion to your cache size. 

While Moka has an upper limit on entry count, FAISS stores raw float vectors. At 384 dimensions, each vector is $384 \times 4 \text{ bytes} = 1.536 \text{ KB}$. A cache containing 500,000 queries will use roughly 750 MB of raw vector memory, plus indexing overhead and metadata. If your server is memory-constrained, you will need to use FAISS Quantization (like `IndexIVFPQ`) to compress the vectors, which trades off accuracy for a smaller memory footprint.

## Benchmark Results

Below is a latency comparison gathered on an AWS `c6i.xlarge` instance (4 vCPUs, 8 GB RAM) running our Rust implementation on single-core limits:

| Cache State | Operation | Latency (ms) | Speedup vs Upstream LLM |
| :--- | :--- | :--- | :--- |
| **Cache Hit** | Local Embeddings + FAISS + Moka lookup | 1.8 ms | **833x faster** |
| **Cache Miss (Local Embed)** | Local Embeddings + Upstream LLM (Claude 3.5 Sonnet) | 1,515 ms | Baseline |
| **Network Embed Miss** | OpenAI API Embeddings + Upstream LLM | 1,720 ms | 13% slower than local |

By bypassing the network round trip for similarity checks and running local ONNX models for vectorization, we achieve sub-2ms cache hits. This allows you to handle spikes in traffic without scaling your LLM spend or hitting upstream rate limits.