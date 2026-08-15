---
layout: post
title: "Implementing a Custom Reranking Engine in C++ with SIMD-Accelerated Cosine Similarity"
date: 2026-08-15 08:00:00 +0700
tags: [c-plus-plus, performance-optimization, simd, vector-search, ai-engineering]
description: "Build an ultra-fast vector reranking engine in C++ using AVX2 and AVX-512 SIMD intrinsics to bypass latency bottlenecks at scale."
image: "https://picsum.photos/seed/7663/1080/720"
thumbnail: "https://picsum.photos/seed/7663/400/300"
---

When scaling Retrieval-Augmented Generation (RAG) or search systems to thousands of requests per second (RPS), you quickly hit the latency wall of the two-stage retrieval pipeline. A vector database (e.g., Qdrant or Milvus) retrieves the top 1,000 to 10,000 raw candidate documents using approximate nearest neighbor search (ANN). To get accurate rankings, you must score these candidates against the query using a high-precision similarity metric. Running this second-stage reranking in Go or Python using standard nested loops easily consumes over 120ms of CPU time at p99, creating an unsustainable latency budget and driving infrastructure costs through the roof. Offloading this compute-heavy bottleneck to a custom C++ engine leveraging SIMD (AVX2/AVX-512) intrinsics and zero-copy FFI wrappers drops that tail latency to under 3ms, allowing you to scale throughput by a factor of 10 on the same hardware.

## The Anatomy of Cosine Similarity and Vector Layouts

Cosine similarity is mathematically defined as:

$$ \text{similarity}(A, B) = \frac{A \cdot B}{\|A\| \|B\|} = \frac{\sum_{i=1}^{D} A_i B_i}{\sqrt{\sum_{i=1}^{D} A_i^2} \sqrt{\sum_{i=1}^{D} B_i^2}} $$

In an optimized production pipeline, we precalculate the query vector's norm once. If document norms are static, we can also precompute and store them in our metadata index. However, when document vectors are updated dynamically or norms are not precalculated, we must compute the dot product and the document's norm on the fly. 

To feed the CPU’s vector registers at maximum throughput, we must layout our vectors contiguously in memory. Standard `std::vector<float>` allocations are placed on the heap, but their alignment is only guaranteed to `alignof(std::max_align_t)`, which is typically 16 bytes. AVX2 instructions operate on 256-bit registers (32 bytes), while AVX-512 uses 512-bit registers (64 bytes). Loading unaligned memory into these registers using instructions like `_mm256_loadu_ps` causes the CPU to execute split-cache-line loads when the boundary is crossed, leading to CPU stalls. By enforcing alignment using `std::aligned_alloc`, we guarantee that every load utilizes aligned memory operations (`_mm256_load_ps`), bypassing unaligned load penalties.

// snippet-1
<script src="https://gist.github.com/mohashari/39299c73d2160b8cc3116ee66c7f7a90.js?file=snippet-1.txt"></script>

## Accelerating Cosine Similarity with AVX2

AVX2 features 16 vector registers, each 256 bits wide, capable of holding eight 32-bit single-precision floats. Instead of performing 8 distinct multiplications and additions in sequence, we can process them all in a single cycle using Fused Multiply-Accumulate (FMA) instructions, which compute $A \times B + C$.

To write high-performance C++ code, we must avoid the common pitfall of storing vector register contents back to an array in the middle of our hot loop to sum them up. Writing to memory and reading it back causes store-to-load forwarding stalls in the CPU pipeline. Instead, we accumulate products in separate `__m256` registers and perform a horizontal reduction only at the very end of the calculation.

// snippet-2
<script src="https://gist.github.com/mohashari/39299c73d2160b8cc3116ee66c7f7a90.js?file=snippet-2.txt"></script>

## Scaling Up to AVX-512

AVX-512 introduces 512-bit registers, doubling the float throughput per register from 8 to 16. In addition, it increases the total number of physical SIMD registers from 16 to 32. This expanded register space allows the compiler to perform aggressive loop unrolling without spilling registers to the stack. We can process two 512-bit chunks per iteration (unrolling by 2) to hide instruction latency and maximize the execution pipeline saturation.

Historically, AVX-512 had a bad reputation due to aggressive CPU downclocking (thermal throttling). Modern architectures, such as Intel Sapphire Rapids and AMD EPYC (Zen 4), have solved this by using native AVX-512 execution blocks that do not trigger severe frequency drops, making AVX-512 safe and highly recommended for modern production clusters.

// snippet-3
<script src="https://gist.github.com/mohashari/39299c73d2160b8cc3116ee66c7f7a90.js?file=snippet-3.txt"></script>

## Handling the Tail for Arbitrary Dimensions

While standard embeddings like Text-Embedding-3-Large (OpenAI) default to dimensions like 1536 or 3072 (which are clean multiples of 8 and 16), production models are often truncated to arbitrary dimensions (e.g., 300, 512, or 960) to optimize storage space.

If we load memory past the end of the array, we risk a segmentation fault. There are two ways to handle the remaining elements (the tail):
1. **Padding**: Pad your vector allocations with zeros up to the next multiple of the register size. This is the fastest method, as the SIMD loop runs uninterrupted.
2. **Scalar Fallback / Masked Loads**: If memory padding is not possible, we process the chunk multiple of 8 with SIMD, and handle the tail using a scalar loop or AVX-512 masked operations.

The following implementation runs AVX2 on the bulk of the vector and falls back to a clean scalar loop for arbitrary tail ends.

// snippet-4
<script src="https://gist.github.com/mohashari/39299c73d2160b8cc3116ee66c7f7a90.js?file=snippet-4.txt"></script>

## Multi-Core Scaling and Batch Reranking

Reranking involves comparing one query vector against $N$ candidate documents. This is an embarrassingly parallel operation. However, creating a OS thread per comparison or scaling with `std::thread` dynamically is highly inefficient. 

For batch reranking, we utilize OpenMP to distribute the candidates evenly across a static CPU thread pool. We precalculate the query's norm before entering the parallel region, avoiding redundant calculations. We then rank all candidates and use `std::partial_sort` to extract only the top $K$ items. Using `std::partial_sort` has a time complexity of $O(N \log K)$ instead of $O(N \log N)$ for a full sort, which is a major optimization when $K = 100$ and $N = 10,000$.

// snippet-5
<script src="https://gist.github.com/mohashari/39299c73d2160b8cc3116ee66c7f7a90.js?file=snippet-5.txt"></script>

## Integrating with Go/Python via Zero-Copy FFI

To use this high-performance engine in your Go or Python service, you must compile it as a shared library (`.so`) and integrate it via Foreign Function Interface (FFI). 

A common mistake when designing FFI bounds is copying vector structures across the runtime border. If you allocate standard vectors in Go, pass them to C++, copy them into aligned structures, and run SIMD, the overhead of memory copy and allocation will easily wipe out the speedups of SIMD optimization.

Instead, we use a zero-copy design. The parent application (e.g., in Go or Python) passes raw pointers of its underlying continuous float arrays directly to C++. Since we cannot guarantee that the calling application has aligned memory to 32-byte boundaries, we modify our SIMD loader to use unaligned loads (`_mm256_loadu_ps`). Modern CPUs handle unaligned loads extremely well if the address happens to be aligned, and only incur a small performance penalty if they cross cache-line boundaries—which is still orders of magnitude faster than copying the memory.

// snippet-6
<script src="https://gist.github.com/mohashari/39299c73d2160b8cc3116ee66c7f7a90.js?file=snippet-6.txt"></script>

## Production Failure Modes & Optimization Flags

Writing optimized C++ code is only half the battle. If your compilation and execution environment are misconfigured, you will run into severe production issues:

### 1. Subnormal (Denormal) Numbers Throttling
In production, embedding vectors may contain values very close to zero. When floating-point values drop below the minimum representable limit of standard normalized floats (called denormal or subnormal numbers), x86 processors handle calculations in microcode, stalling the hardware pipeline. This causes a sudden 100x latency spike when scoring documents with sparse vectors.
To fix this, you must set the **Flush-to-Zero (FTZ)** and **Denormals-are-Zero (DAZ)** flags in the CPU's control register (`MXCSR`). This forces the CPU to treat subnormals as pure zero, resolving the stalls.

```cpp
#include <xmmintrin.h>
#include <pmmintrin.h>

void enable_daz_ftz() {
    _MM_SET_FLUSH_ZERO_MODE(_MM_FLUSH_ZERO_ON);
    _MM_SET_DENORMALS_ZERO_MODE(_MM_DENORMALS_ZERO_ON);
}
```

### 2. Compiler Optimization Flags
By default, compilers do not generate SIMD instructions. You must compile your library with target-specific optimization flags. Passing the wrong target options will prevent the compiler from generating FMA or AVX512 code.

```bash
# Compilation flags for AVX2 + FMA with fast-math enabled
g++ -O3 -shared -fPIC -mavx2 -mfma -ffast-math -fopenmp -o librerank.so reranker.cpp
```

*   `-O3`: Enables high-level compiler optimizations, loop vectorization, and inline expansions.
*   `-ffast-math`: Allows algebraic simplifications that break strict IEEE 754 compliance (e.g., assuming no NaNs/Infs). This is required for the compiler to optimize mathematical reductions effectively.
*   `-mavx2 -mfma`: Explicitly instructs the compiler to generate AVX2 and Fused Multiply-Accumulate assembly.

### 3. L3 Cache Exhaustion
If your batch size is too large, you will saturate the memory bus. A 1536-dimensional float vector takes 6KB of memory. If you try to compare a batch of 50,000 vectors simultaneously, you will request 300MB of data. If your server CPU (e.g., an AWS `c6i.xlarge` instance) only has a 12MB L3 cache, the CPU will continuously fetch data from main system RAM (DRAM). Because DRAM latency is ~100x higher than cache latency, the execution will stall waiting for memory. 
To prevent this, tile your batches: split the candidate list into chunks that easily fit within the CPU's L3 cache (e.g., processing candidates in blocks of 2,048), keeping the operations entirely cache-resident.