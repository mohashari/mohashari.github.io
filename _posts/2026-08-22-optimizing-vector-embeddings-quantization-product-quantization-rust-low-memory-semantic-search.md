---
layout: post
title: "Optimizing Vector Embeddings Quantization (Product Quantization) in Rust for Low-Memory Semantic Search"
date: 2026-08-22 08:00:00 +0700
tags: [rust, vector-search, quantization, performance]
description: "Learn how to implement a high-performance Product Quantization (PQ) engine in Rust, reducing vector memory footprints by up to 96% with SIMD optimizations."
image: "https://picsum.photos/seed/4795/1080/720"
thumbnail: "https://picsum.photos/seed/4795/400/300"
---

Scaling vector search to hundreds of millions of high-dimensional embeddings (such as 1536-dimensional OpenAI vectors or 1024-dimensional Cohere vectors) inevitably leads to a massive infrastructure bottleneck: the RAM bill. Storing 100 million 1536-dimensional `f32` vectors in-memory requires 614 GB of raw RAM, translating to expensive, multi-node cluster deployments in AWS or GCP that are highly complex to maintain. While vector databases like Qdrant and Milvus mitigate this with native quantization, building a custom search service or squeezing maximum throughput out of product quantization (PQ) requires diving into bare-metal optimizations. Product Quantization reduces memory usage by up to 96% by splitting vectors into sub-vectors and mapping them to learned centroids, but if implemented naively, the decoding overhead during query time will completely destroy search throughput.

![Optimizing Vector Embeddings Quantization (Product Quantization) in Rust for Low-Memory Semantic Search Diagram](/images/diagrams/optimizing-vector-embeddings-quantization-product-quantization-rust-low-memory-semantic-search.svg)

## The Mechanics of Product Quantization (PQ)

Product Quantization operates on the principle of decomposing a high-dimensional vector space into a Cartesian product of lower-dimensional subspaces and quantizing each subspace individually. Instead of finding a single set of centroids for the entire high-dimensional space (which is computationally impossible and prone to the curse of dimensionality), we slice the vector into $M$ orthogonal subspaces.

For a vector of dimension $D$:
1. We divide it into $M$ sub-vectors, each of dimension $d = D/M$.
2. For each subspace, we run K-Means clustering on a representative training dataset to learn a codebook of $K^*$ centroids (usually $K^* = 256$, so that each centroid index fits exactly in a single `u8` byte).
3. Any vector is then quantized by splitting it into $M$ sub-vectors, finding the nearest centroid for each sub-vector in its respective subspace, and storing the $M$ centroid indices as an array of $M$ bytes.

This achieves a massive compression ratio. For example, a 1536-dimensional `f32` vector (6144 bytes) quantized with $M = 96$ and $K^* = 256$ becomes a 96-byte array. This yields a $64\times$ reduction in memory usage.

### Asymmetric vs. Symmetric Distance Computation

When querying the index, we can compute distances in two ways:
* **Symmetric Distance Computation (SDC)**: Both the query vector and the database vectors are quantized. The distance is then computed by looking up pre-computed distances between centroid indices. SDC suffers from high quantization noise.
* **Asymmetric Distance Computation (ADC)**: The query vector is kept in its raw `f32` format, while only the database vectors are quantized. Because the query vector remains unquantized, the distance approximation is far more accurate.

For ADC, we construct a Query Lookup Table (LUT) at query time. For each of the $M$ subspaces, we compute the distance between the query's sub-vector and all 256 centroids in that subspace's codebook. This constructs an $M \times 256$ table of float distances. The distance between the query and any quantized database vector is then computed by performing $M$ lookups in this table and summing the values. This replaces floating-point multiply-accumulate loops with simple L1 cache lookups.

## Cache-Friendly Data Structures in Rust

To implement this efficiently in Rust, we must ensure our memory layout is contiguous. Naive implementations using nested `Vec<Vec<f32>>` introduce pointer chasing, which degrades L1 cache locality.

Below is our structured representation of a `ProductQuantizer` and its associated components:

<script src="https://gist.github.com/mohashari/f5c8cb03286e0b0d7418dd0c18bbf473.js?file=snippet-1.txt"></script>

By laying out the centroids of each subspace codebook as a flat `Vec<f32>`, we ensure that finding the nearest centroid during both quantization and distance calculation utilizes contiguous memory slices, allowing the CPU to leverage automatic prefetching.

## Parallel K-Means Codebook Training

Training the codebook requires running K-Means on a subset of the vector corpus. Because K-Means is highly parallelizable, we can implement it using Rust's concurrency library, `rayon`. This allows us to parallelize centroid assignment across all available CPU threads.

<script src="https://gist.github.com/mohashari/f5c8cb03286e0b0d7418dd0c18bbf473.js?file=snippet-2.txt"></script>

## The Quantization Pipeline

To quantize (encode) a raw vector, we slice it along its dimensions and determine the nearest centroid index for each slice. This is an $O(M \times K^* \times d)$ operation per vector. Since $K^* = 256$ is relatively small, this can be performed on the fly during data ingestion.

<script src="https://gist.github.com/mohashari/f5c8cb03286e0b0d7418dd0c18bbf473.js?file=snippet-3.txt"></script>

## Implementing Asymmetric Distance Lookup (ADC)

During the query execution phase, calculating the exact distance between the query vector and millions of quantized vectors requires a fast lookup table. 

First, we generate the Lookup Table (LUT) containing the squared Euclidean distances from the query's sub-vectors to all centroids. Once generated, calculating the distance to any database vector is done by iterating through the vector's bytes, matching each byte to the index of the table row, and accumulating the sum.

<script src="https://gist.github.com/mohashari/f5c8cb03286e0b0d7418dd0c18bbf473.js?file=snippet-4.txt"></script>

## SIMD and Cache-Line Optimization

While the lookup table method is mathematically elegant, its performance on modern CPUs is bound by memory access patterns. Specifically, when we loop through quantized vectors, we perform arbitrary lookups in the LUT based on the code byte (`centroid_idx`). Since these bytes are distributed stochastically, this leads to L1/L2 cache misses.

To resolve this, we can score vectors in batches of 8 using SIMD (Single Instruction, Multiple Data). By structuring our lookup table to fit cleanly into L1 cache and processing 8 quantized vectors concurrently, we allow the CPU to fetch indices and perform parallel gather-like lookups. 

We implement this below using Rust's nighty `portable_simd` API:

<script src="https://gist.github.com/mohashari/f5c8cb03286e0b0d7418dd0c18bbf473.js?file=snippet-5.txt"></script>

### CPU-Level Alignment and Prefetching

For maximum performance, compile the binary with target-specific optimizations:

```bash
RUSTFLAGS="-C target-cpu=native -C opt-level=3" cargo build --release
```

This configuration ensures the compiler utilizes AVX2/AVX-512 vector execution registers. Additionally, aligning the lookup tables and ensuring the database vector bytes (`codes`) are aligned on 64-byte boundaries prevents cache-line splits.

## Memory vs. Recall Trade-Offs

When migrating from raw `f32` Euclidean distance search to Product Quantization in production, you must balance memory usage, compute latency, and accuracy.

| Quantization Mode | Bytes/Vector (1024-dim) | Memory Footprint (10M Vectors) | Compression Ratio | Recall@10 | Relative QPS |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Raw f32** | 4096 B | 40.96 GB | 1.0x | 100% | 1.0x |
| **FP16** | 2048 B | 20.48 GB | 2.0x | ~99.9% | 1.2x |
| **Scalar (SQ8)** | 1024 B | 10.24 GB | 4.0x | ~98.5% | 2.5x |
| **PQ (M=128)** | 128 B | 1.28 GB | 32.0x | ~94.2% | 12.0x |
| **PQ (M=64)** | 64 B | 0.64 GB | 64.0x | ~89.5% | 18.5x |

### Failure Mode: Subspace Dimension Correlation

A common failure mode in PQ is slicing the vector space into subspaces where features are highly correlated. If dimension $0$ and dimension $1$ are strongly correlated, but they are split across different subspaces, the Cartesian product assumption breaks down. This degrades the codebook quality, resulting in a drop in recall.

**Mitigation**: Before quantizing, apply a random rotation matrix or a Principal Component Analysis (PCA) transform to the vector dataset. This decorrelates the features across dimensions, ensuring that subspace splitting is orthogonal and information loss is minimized.

## Putting It All Together: A Benchmarked Search Loop

Here is the complete search pipeline. It accepts a list of quantized database vectors, constructs a query lookup table, performs batched SIMD scoring, and utilizes a max-heap to extract the Top-K nearest neighbors.

<script src="https://gist.github.com/mohashari/f5c8cb03286e0b0d7418dd0c18bbf473.js?file=snippet-6.txt"></script>

This pipeline can process millions of quantized vectors per second on a single core. By combining Rust's memory alignment controls with SIMD-vectorized lookups and a contiguous memory layout, you can scale semantic search systems to larger volumes of embeddings at a fraction of the hardware cost.