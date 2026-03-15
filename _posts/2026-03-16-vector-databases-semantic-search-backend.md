---
layout: post
title: "Vector Databases: Powering Semantic Search in Backend Systems"
date: 2026-03-16 07:00:00 +0700
tags: [databases, ai, search, performance, backend]
description: "Understand how vector databases like pgvector, Qdrant, and Weaviate store and query high-dimensional embeddings for semantic search and RAG applications."
---

Traditional keyword search breaks down the moment users start searching the way they think. A user querying "affordable places to stay near the beach" won't match a document titled "budget coastal accommodations" — the words don't overlap, but the meaning is identical. This semantic gap is where vector databases shine. Instead of indexing tokens, they index *meaning* as high-dimensional numeric vectors called embeddings, enabling queries that find conceptually similar content regardless of exact wording. For backend engineers building recommendation engines, document retrieval systems, or retrieval-augmented generation (RAG) pipelines, understanding how to store, index, and query embeddings efficiently is now a core skill.

## How Embeddings Work

An embedding model — like OpenAI's `text-embedding-3-small` or a local model via Ollama — transforms text, images, or audio into a dense float vector, typically 768 to 3072 dimensions. Two semantically similar inputs produce vectors that are geometrically close in that high-dimensional space. The search problem then becomes: given a query vector, find the *k* nearest neighbors (kNN) in your stored vectors. Naive brute-force kNN is O(n·d) per query, which quickly becomes impractical at scale — this is why purpose-built approximate nearest neighbor (ANN) indexes like HNSW (Hierarchical Navigable Small World) exist.

## Generating Embeddings with Go

Before you can store anything, you need to produce the vectors. Here's a minimal Go function that calls the OpenAI embeddings endpoint and returns a float slice ready for storage.

<script src="https://gist.github.com/mohashari/2357e8548c60f2f6bbb5793d2b937a04.js?file=snippet.go"></script>

## pgvector: Semantic Search Inside PostgreSQL

If you're already running Postgres, `pgvector` is the fastest path to production. It adds a native `vector` type and HNSW or IVFFlat indexes. No new infrastructure, no new operational burden — just an extension.

Enable the extension and create your schema:

<script src="https://gist.github.com/mohashari/2357e8548c60f2f6bbb5793d2b937a04.js?file=snippet-2.sql"></script>

Once indexed, a semantic search query looks like this — note how it reads almost like plain SQL while performing ANN search under the hood:

<script src="https://gist.github.com/mohashari/2357e8548c60f2f6bbb5793d2b937a04.js?file=snippet-3.sql"></script>

The `<=>` operator is cosine distance. pgvector also supports inner product (`<#>`) and L2 distance (`<->`). Cosine distance is usually the right choice for text embeddings because it's magnitude-invariant — only the direction of the vector matters.

## Running Qdrant for High-Scale Workloads

When your collection grows beyond a few million vectors or you need fine-grained payload filtering at query time without sacrificing ANN performance, a dedicated vector database like Qdrant becomes compelling. Qdrant stores vectors and arbitrary JSON payloads together, and its HNSW implementation is written in Rust — it's fast and memory-efficient.

Spin up a local Qdrant instance for development:

<script src="https://gist.github.com/mohashari/2357e8548c60f2f6bbb5793d2b937a04.js?file=snippet-4.yaml"></script>

Now upsert a document and its embedding using Qdrant's REST API from Go:

<script src="https://gist.github.com/mohashari/2357e8548c60f2f6bbb5793d2b937a04.js?file=snippet-5.go"></script>

## Hybrid Search: Combining Vector and Keyword Signals

Pure semantic search isn't always optimal. Product searches benefit from exact SKU matching; legal document retrieval needs precise clause citations. Hybrid search blends dense vector similarity with sparse keyword scores (BM25). Qdrant supports this natively through sparse vectors alongside dense ones. In pgvector, you can approximate it by combining a full-text search rank with cosine similarity and tuning the blend weight:

<script src="https://gist.github.com/mohashari/2357e8548c60f2f6bbb5793d2b937a04.js?file=snippet-6.sql"></script>

## RAG Pipeline: Putting It Together

Retrieval-augmented generation wires semantic search directly into an LLM prompt. The pattern is: embed the user's question, retrieve top-k relevant chunks from your vector store, inject them as context, then pass the augmented prompt to the LLM. This is the core loop in most production AI assistants.

<script src="https://gist.github.com/mohashari/2357e8548c60f2f6bbb5793d2b937a04.js?file=snippet-7.go"></script>

## Indexing Strategy and Operational Tuning

The HNSW index trades memory for query speed. Its two key parameters deserve attention. `m` (graph connectivity, typically 8–64) controls index size and recall — higher values mean better recall but more RAM. `ef_construction` (search depth during build, typically 64–200) controls build quality. At query time, `ef_search` controls the recall/latency trade-off without requiring a rebuild. For most workloads, start with `m=16, ef_construction=64` and tune `ef_search` under load.

A shell script to benchmark recall against latency as you tune:

<script src="https://gist.github.com/mohashari/2357e8548c60f2f6bbb5793d2b937a04.js?file=snippet-8.sh"></script>

## Summary

Vector databases are not a replacement for your existing data layer — they're a complement to it. For most teams, the right starting point is pgvector inside an existing Postgres deployment: zero new infrastructure, SQL you already know, and surprisingly good performance up to tens of millions of vectors. When you outgrow that, Qdrant and Weaviate offer purpose-built ANN engines with richer filtering and multi-tenancy primitives. The real leverage comes from the pipeline around the index: generating clean, chunked embeddings, choosing the right distance metric, and blending semantic retrieval with structured filters. Nail those fundamentals and semantic search becomes a reliable, low-latency primitive you can build serious features on.