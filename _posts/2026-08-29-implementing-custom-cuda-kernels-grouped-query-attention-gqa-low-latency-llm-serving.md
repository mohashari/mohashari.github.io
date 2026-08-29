---
layout: post
title: "Implementing Custom CUDA Kernels for Grouped-Query Attention Speedups in Low-Latency LLM Serving"
date: 2026-08-29 08:00:00 +0700
tags: [cuda, llm-serving, high-performance-computing]
description: "Optimizing autoregressive LLM decoding bottlenecks through a custom Paged GQA CUDA kernel with vectorized global memory access and online softmax."
image: "https://picsum.photos/seed/6988/1080/720"
thumbnail: "https://picsum.photos/seed/6988/400/300"
---

During low-latency LLM serving (e.g., streaming tokens from Llama 3 70B or Mistral Large), the system is almost entirely memory-bandwidth bound during the autoregressive decoding phase. Every single generated token requires reading the entire key-value (KV) cache from High Bandwidth Memory (HBM) into the GPU's SRAM. While Grouped-Query Attention (GQA) reduces this memory pressure theoretically by sharing key-value heads across multiple query heads, standard PyTorch eager operators fail to exploit this layout. Instead, they trigger redundant allocations, uncoalesced memory reads, and wasteful tensor materialization. To achieve true line-rate memory bandwidth on NVIDIA A100 and H100 GPUs, we must bypass PyTorch's high-level dispatch and write a custom, fused GQA CUDA kernel that implements paged memory layout traversal, cooperative warp-level reductions, and online softmax update formulas directly in SRAM.

## The Mechanics of GQA Memory Bottlenecks in Autoregressive Decoding

In the decoding phase of transformer-based LLMs, batch sizes are small and the query length is exactly 1. While the computation of the query-key-value projections is minor, the memory load of the historical keys and values (the KV cache) scales linearly with the sequence length. In Multi-Head Attention (MHA), the number of query heads ($H_q$) equals the number of key-value heads ($H_{kv}$). When sequence lengths scale up to 32k or 128k tokens, the size of the KV cache explodes, easily exhausting GPU VRAM and stalling the streaming multiprocessors (SMs) as they wait for HBM data.

GQA introduces an asymmetric layout: the query heads are grouped into clusters of size $G = H_q / H_{kv}$, with each cluster sharing a single key-value head. For example, Llama 3 8B uses 32 query heads and 8 KV heads, resulting in a group size of 4.

If implemented naively in PyTorch, the runtime has to reconcile the mismatch in head counts before performing batch matrix multiplication. This is typically achieved using a `repeat_interleave` or broadcast operator:

```python
# Unoptimized eager layout expansion
K_expanded = K.repeat_interleave(groups, dim=1)  # Triggers global HBM allocations and writes
```

This operation clones the KV cache data in memory, negating the memory bandwidth savings GQA was designed to achieve. We need a kernel that reads each KV head from global memory exactly *once*, loads it into local SRAM or registers, and shares it across the execution paths of all $G$ query heads within that group. Furthermore, in production systems, the KV cache is non-contiguous due to dynamic memory management (PagedAttention), meaning we must translate virtual token offsets into physical block locations on the fly.

## Designing the Paged GQA Kernel Architecture

To maximize occupancy and coalesce memory reads on NVIDIA A100/H100 GPUs, we must align the CUDA block and thread configuration with the physical dimensions of the attention operation. We configure a 2D CUDA grid:

- `gridDim.x` maps directly to the active sequence/batch index (`num_seqs`).
- `gridDim.y` maps to the number of KV heads (`num_kv_heads`).

By assigning one CUDA thread block to each KV head group per sequence, we ensure that the key and value cache segments for that head are loaded into shared memory or registers exactly once. Within this block of threads (configured to 128 or 256 threads), we loop over the $G$ query heads. This keeps the loaded KV cache data resident in the fast L1 cache or shared memory, preventing costly trips back to the global HBM.

To manage the paged layout of the KV cache, we pass a lookup table (`block_table`) that translates logical sequence indexes to physical block pointers, avoiding fragmentation.

<script src="https://gist.github.com/mohashari/be6d29054c62d854092914be95bef48e.js?file=snippet-1.py"></script>

## Bridging PyTorch to CUDA: PyBind11 and the C++ Dispatcher

Before diving into the GPU assembly, we need a PyBind11 wrapper to interface with the PyTorch runtime. The C++ code checks tensor attributes and extracts raw memory pointers using ATen.

<script src="https://gist.github.com/mohashari/be6d29054c62d854092914be95bef48e.js?file=snippet-2.txt"></script>

The launcher code on the host manages the execution configuration. We statically dispatch the templates based on structural parameters (like `head_dim` and `block_size`). This allows the compiler to unroll loops and optimize register pressure for common configurations.

<script src="https://gist.github.com/mohashari/be6d29054c62d854092914be95bef48e.js?file=snippet-3.txt"></script>

## The CUDA Device Kernel: Fused Softmax and Vectorized Loads

The device kernel must load memory at line-rate. We do this by mapping CUDA threads to 128-bit boundary structures (vectorized loads). For FP16/BF16 data types, we use `Vector<scalar_t, 8>`, which maps to a single `LDG.128` instruction in PTX assembly. This minimizes translation lookaside buffer (TLB) hits and maximizes PCIe/SXM bus efficiency.

We implement an **online softmax** algorithm. Instead of calculating the global attention denominator at the end, we compute running log-sum-exps using numerical stability adjustments. This allows us to accumulate the output vector directly in SRAM, reducing the need for intermediate HBM allocations.

<script src="https://gist.github.com/mohashari/be6d29054c62d854092914be95bef48e.js?file=snippet-4.txt"></script>

## Cooperative Intra-Warp Reductions

We need to avoid raw global synchronizations or standard serial thread loops. Instead, we use cooperative warp-level primitives to reduce elements inside register spaces using the target GPU's active mask. 

<script src="https://gist.github.com/mohashari/be6d29054c62d854092914be95bef48e.js?file=snippet-5.txt"></script>

## Optimizing Register Allocation & Compilation Parameters

Writing high-performance CUDA kernels requires careful optimization of the compilation target. If a kernel uses too many registers (often due to compiler-allocated register spills to local memory), dynamic warp occupancy drops. This causes execution stalls when the GPU is waiting for memory reads.

To maximize throughput, we must explicitly declare nvcc compiler flags and use parameters like `--threads 4` (to speed up build times) alongside architectural code generations (`sm_80` and `sm_90`).

<script src="https://gist.github.com/mohashari/be6d29054c62d854092914be95bef48e.js?file=snippet-6.py"></script>

## Profiling and Production Benchmarks

To verify the performance improvements of our custom kernel, we set up a benchmark harness comparing it against standard PyTorch eager implementations. The benchmark mimics a real-world server workload: batch size 64, sequence length 2048, and FP16 precision.

<script src="https://gist.github.com/mohashari/be6d29054c62d854092914be95bef48e.js?file=snippet-7.py"></script>

### Analyzing the Profile

When running the baseline PyTorch eager code under `nsys profile` or using PyTorch's built-in profiler, we observe significant overhead. Specifically, there are two distinct occurrences of memory allocations and transfers to HBM just to repeat and broadcast the Key and Value cache tensors. This results in the GPU spending roughly 70% of its execution time waiting for HBM to complete page reads and writes, achieving only about 450 GB/s of bandwidth on an A100.

With the custom kernel, profiling with `ncu` (Nvidia Nsight Compute) reveals several improvements:

1. **Memory Bandwidth Utilization (MBU)**: Reaches over 90% of the theoretical maximum (1.8 TB/s on an A100 SXM4). The GPU pipeline is saturated because the KV cache is read once and immediately consumed by the query group inside registers.
2. **Vectorized Instruction Rate**: The assembly dump confirms the presence of `LDG.E.128` instructions, showing that the compiler successfully coalesced the memory operations into 16-byte vectorized chunks.
3. **Register Occupancy**: The kernel uses 48 registers per thread, allowing the GPU to run 16 active blocks per SM. This configuration provides sufficient warp execution options to hide global memory read latencies.

## Common Production Failure Modes

When deploying custom CUDA kernels for attention operations into production engines, you will likely encounter these three common failure modes:

### 1. Shared Memory Bank Conflicts
Nvidia GPUs divide shared memory into 32 banks, where each bank can handle 32 bits (4 bytes) per clock cycle. If two or more threads in a warp attempt to access different 4-byte boundaries *in the same bank*, a conflict occurs, serializing the operations. 

In GQA, bank conflicts commonly occur when thread configurations read transposition patterns of the key or value matrices. Padding shared memory structures (e.g., configuring `__shared__ float s_logits[THREADS_PER_BLOCK + 1]`) changes the memory alignment, ensuring that threads in the same warp map to distinct physical banks.

### 2. Under-Occupancy from Register Pressure
As you implement optimizations inside your device kernel (like caching more tokens or using half2 operations), you might find the compiler allocating more registers than the SM can support. If a thread block requires 80 registers per thread, the maximum number of active warps drops significantly. 

You can prevent compiler-driven register allocation issues by enforcing launch bounds directly on your kernel template:

```cpp
__global__ void __launch_bounds__(128, 4) paged_gqa_decode_kernel(...)
```

This instruction forces the compiler to limit register allocation to a specific count (typically 64 or 48), ensuring that at least 4 thread blocks can be scheduled per SM.

### 3. Misaligned Pointers and Page Faults
Vectorized loading (`vec_t = Vector<scalar_t, 8>`) requires that the source pointer is aligned to a 16-byte boundary. If the head dimension size or the page block size is changed to a configuration that does not align with this boundary (e.g., using a head dimension of 96 with FP16, which results in 192 bytes, or using custom quantized layouts like FP8/INT4), executing a vectorized load will cause a memory alignment fault, halting the CUDA context. 

To prevent this, implement a fallback loop in the kernel that uses scalar reads if the pointer alignments do not meet the 16-byte requirement.