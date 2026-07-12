---
layout: post
title: "Implementing Dynamic Chunking and Sparse Vector Search in Qdrant for Multi-Tenant RAG"
date: 2026-07-12 08:00:00 +0700
tags: [qdrant, vector-search, rag, ai-engineering]
description: "A production-focused guide to implementing dynamic, syntax-aware chunking and hybrid dense-sparse vector search in Qdrant with hard tenant isolation."
image: "https://picsum.photos/seed/3609/1080/720"
thumbnail: "https://picsum.photos/seed/3609/400/300"
---

## The Production Reality of Naive RAG at Scale

In a production environment, a naive Retrieval-Augmented Generation (RAG) system initialized with simple recursive character splitting (e.g., splitting every 512 tokens with a 50-token overlap) and pure dense vector search will collapse under operational stress. When your application transitions from a single-tenant demo to an enterprise-grade multi-tenant platform handling millions of documents, three critical failure modes emerge:

1. **Semantic Mutilation:** Fixed-size chunking splits code blocks, SQL tables, and structured lists right down the middle. When retrieved, these truncated fragments are missing key context (e.g., table headers or enclosing JSON structures), leading to severe LLM hallucinations.
2. **The Vocabulary Mismatch Problem:** Dense embeddings (such as OpenAI's `text-embedding-3-small` or BGE) are excellent at capturing abstract semantic intent but fail catastrophically when queries rely on exact keywords—like product serial numbers, unique variable names, API error codes (e.g., `ERR_CONN_REFUSED`), or database keys.
3. **Tenant Data Leakage:** In multi-tenant SaaS architectures, retrieving data belonging to Tenant B for a query from Tenant A is an absolute security breach. Implementing vector search without mathematically guaranteed tenant isolation is a critical security vulnerability.

At 100,000 documents, exact key lookups (e.g., searching for a specific IP address or GUID) fail to return the correct document in up to 40% of cases because dense embeddings prioritize semantic neighbors rather than character-level equivalence. To build a resilient, enterprise-grade RAG pipeline, we must combine three core methodologies: **Dynamic Structurally-Aware Chunking**, **Hybrid Search (Dense + Sparse Vectors)**, and **Hard Tenant Isolation via Qdrant Payload Indexing**.

---

## Anatomy of the Dual-Vector Space: Dense vs. Sparse

To achieve high recall across both semantic queries and exact keyword lookups, we configure Qdrant to support dual-vector spaces. 

Dense vectors compress text into a continuous multi-dimensional space (e.g., 384 dimensions for BAAI/bge-small-en-v1.5). These models excel at identifying conceptual similarity but struggle with lexical specificity.

Sparse vectors represent text in a high-dimensional space matching the vocabulary size of the tokenizer (e.g., 30,522 dimensions for BERT-based models). Unlike classic BM25 which is computed at query time, modern sparse vectors are generated using neural models like SPLADE (Sparse Lexical and Expansion). SPLADE uses a masked language model to predict token relevance, expanding the document's vocabulary with synonyms and related concepts. For example, a document containing the word "latency" will have non-zero weights for "speed", "performance", and "delay" even if those terms never appeared in the source text.

---

## Dynamic Chunking: Structural and Semantic Parsing

Instead of blind character counting, dynamic chunking parses the document's syntax (e.g., Markdown headers, code fences, HTML sections) to determine logical splits. For instance, a Markdown table must remain a single semantic block, and its parent headings must be prepended to the chunk text to preserve hierarchy.

Below is a production-grade Python parser that recursively splits Markdown documents by their headers, maintaining a cumulative path of parent headings as context metadata for each chunk.

<script src="https://gist.github.com/mohashari/5cf01109a64803aa304677fa2a2c2df6.js?file=snippet-1.py"></script>

By prepending `Context: {header_context}` to each chunk, we force the dense embedding models to preserve long-range contextual relationships that are typically lost when sub-sections are stripped of their parent titles.

---

## Dual-Embedding Generation Pipeline

We use Qdrant's `fastembed` library to generate both dense and sparse representations locally without incurring the latency overhead of external HTTP API calls. `fastembed` runs on ONNX Runtime and is optimized for high-throughput CPU inference.

<script src="https://gist.github.com/mohashari/5cf01109a64803aa304677fa2a2c2df6.js?file=snippet-2.py"></script>

---

## Configuring Qdrant with HNSW Optimizations

In Qdrant, we must define separate vector configurations for dense and sparse models within the same collection. Crucially, we must also set up a payload index on `tenant_id` of the `keyword` type. 

Without a payload index, Qdrant would perform a full scan of all points to apply the filter post-search, which destroys performance. If it attempts pre-filtering without segment optimizations, query latency will spike. Defining a `keyword` index forces Qdrant to build a payload index that resolves tenant isolation filters at the index segment level.

<script src="https://gist.github.com/mohashari/5cf01109a64803aa304677fa2a2c2df6.js?file=snippet-3.py"></script>

---

## Robust Multi-Tenant Ingestion Pipeline

To prevent out-of-memory errors and ensure database stability during bulk ingestion, we process documents in batches, construct the payload dictionaries containing our tenant boundaries, and upsert them safely with backoff and retry mechanisms.

<script src="https://gist.github.com/mohashari/5cf01109a64803aa304677fa2a2c2df6.js?file=snippet-4.py"></script>

---

## Executing Tenant-Isolated Hybrid Search

To query Qdrant with hybrid search, we execute prefetch queries for both the dense vector and the sparse vector, and then apply Reciprocal Rank Fusion (RRF) to merge the ranked lists. 

Crucially, **the tenant filter must be applied inside the prefetch stages**. If you apply the tenant filter only at the root query level, the prefetch stages might retrieve points from other tenants, leaving you with empty or irrelevant results after the final intersection.

<script src="https://gist.github.com/mohashari/5cf01109a64803aa304677fa2a2c2df6.js?file=snippet-5.py"></script>

---

## Under the Hood: Resolving the HNSW Filtering Trap

When performing filtered searches inside HNSW indexes (which dense vectors use), Qdrant walks a proximity graph of points. If your collection contains 10,000 tenants, and each tenant owns only 0.01% of the total vectors, the points belonging to a single tenant will be highly sparse inside the global HNSW graph. 

If Qdrant tries to traverse the global graph using a standard filter, it will quickly hit a dead end because adjacent nodes in the graph belong to other tenants and are filtered out. This causes search recalls to drop to near zero.

To solve this, Qdrant utilizes a dual-index strategy:
- A structural HNSW index for vector similarity.
- Payload indexes (like the `KEYWORD` index we created on `tenant_id`) that track list structures mapping payload values to point IDs.

At query execution time, Qdrant's planner estimates the selectivity of the filter:
1. **High Selectivity (Small Tenant):** If the filter matches a small subset of the collection (e.g., less than the threshold configured via `indexing_threshold_kb`), Qdrant bypasses the HNSW graph traversal entirely. Instead, it uses the payload index to retrieve the matched point IDs and performs a flat search (direct vector comparison) over just those points. This yields a 100% recall rate with extremely low latency.
2. **Low Selectivity (Large Tenant):** If the filter matches a large portion of the collection, Qdrant traverses the HNSW graph directly, using the filter to prune nodes during the search path.

To configure this behavior globally for a production environment, modify your Qdrant configuration file (`config.yaml`):

<script src="https://gist.github.com/mohashari/5cf01109a64803aa304677fa2a2c2df6.js?file=snippet-6.yaml"></script>

---

## Production Tuning & Pitfalls

When moving this system to production, you will encounter tradeoffs between memory usage, ingestion throughput, and query latency. Use these rules of thumb to optimize your deployment:

### Sparse Vector Memory Footprints
Because sparse vectors can contain thousands of active elements per document, they consume significant memory.
- Set `on_disk=True` in `SparseIndexParams`. This keeps the sparse vector index stored on disk rather than RAM. While this adds a minor I/O lookup cost, it saves up to 60% of overall RAM usage in collections exceeding 10 million vectors.
- Ensure that your host machine has sufficient SSD IOPS capacity, as on-disk index lookups rely on fast random read operations.

### Multi-Tenant Payload Segment Leakage
If you delete and rewrite tenant data frequently, Qdrant segments can become fragmented. Ensure your cleanup pipelines use the partition delete API rather than batch updates:

<script src="https://gist.github.com/mohashari/5cf01109a64803aa304677fa2a2c2df6.js?file=snippet-7.py"></script>

This ensures that the space allocated within index segments for the target tenant is properly marked for garbage collection and consolidated during the next optimization pass.

### Dynamic Chunking Corner Cases
Dynamic structural chunking can occasionally produce chunks that exceed your LLM context window limits if a document contains an extremely large Markdown table or pre-formatted code block. 
- Implement a fallback splitter inside `_create_chunk` that splits the content recursively by line breaks if the raw text exceeds the `max_chunk_size` limit.
- Keep table structures intact by replacing empty cells with placeholders (e.g., `N/A`) before embedding to prevent SPLADE from generating noisy zero-value weights.

---

## Conclusion

Implementing a reliable, multi-tenant hybrid RAG system requires shifting away from naive text splitting and single-vector search. By utilizing dynamic structural parsing, you ensure that relevant semantic relationships are preserved at ingestion time. Combining BGE-dense embeddings with SPLADE-based sparse embeddings provides a robust query fallback, guaranteeing high recall on both semantic and keyword lookups. Finally, routing queries through payload-indexed tenant filters in Qdrant ensures absolute data boundary isolation without the overhead of maintaining thousands of independent collections. Use the HNSW settings, dynamic partitioning, and memory configurations outlined in this guide to build a robust, scalable system.