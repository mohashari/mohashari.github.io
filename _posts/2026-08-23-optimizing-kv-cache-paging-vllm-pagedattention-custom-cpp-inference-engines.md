---
layout: post
title: "Optimizing KV-Cache Paging with vLLM PagedAttention in Custom C++ Inference Engines"
date: 2026-08-23 08:00:00 +0700
tags: [ai-engineering, c-plus-plus, cuda, memory-management]
description: "Build a custom C++ physical block allocator and PagedAttention scheduler to eliminate VRAM fragmentation and prevent OOM crashes in production."
image: "https://picsum.photos/seed/5390/1080/720"
thumbnail: "https://picsum.photos/seed/5390/400/300"
---

In large-scale LLM deployment, the single greatest bottleneck to high concurrency isn’t raw compute throughput—it is VRAM consumption. In a standard Llama-3-70B deployment with FP16 weights, serving a 32-request concurrent batch using static, contiguous KV-cache allocation requires pre-allocating VRAM for the maximum context length (e.g., 8,192 tokens) for every request. This naive model reserves up to 2.5 GB of VRAM per request for the key-value cache alone, resulting in a staggering 80 GB dedicated solely to active sessions. Worse, because real-world query outputs are highly dynamic and rarely hit the maximum length, over 60% of this VRAM is wasted due to internal and external memory fragmentation, leading to frequent Out-of-Memory (OOM) crashes and severely degraded throughput. To bypass these limitations and achieve maximum hardware utilization, custom C++ inference engines must implement a dynamic page-table mechanism using vLLM's PagedAttention strategy, decoupling logical sequence lengths from physical GPU memory allocation.

![Optimizing KV-Cache Paging with vLLM PagedAttention in Custom C++ Inference Engines Diagram](/images/diagrams/optimizing-kv-cache-paging-vllm-pagedattention-custom-cpp-inference-engines.svg)

## The Mathematics of KV-Cache Overhead in Production

To understand why contiguous memory allocation fails, we must examine the memory profile of modern Transformer-based LLMs. The KV cache stores the key and value activation vectors for all tokens in a sequence to avoid redundant re-computation during the iterative autoregressive generation phase. For each token processed, we store a tensor representation for every layer of the network.

The formula for calculating the bytes required by the KV cache per token is:

$$\text{Bytes Per Token} = 2 \times L \times H_{\text{kv}} \times D \times P$$

Where:
* **2** represents the distinct Key and Value states.
* **$L$** is the number of layers in the model (e.g., 80 for Llama-3-70B, 32 for Llama-3-8B).
* **$H_{\text{kv}}$** is the number of key-value heads. Modern architectures use Grouped-Query Attention (GQA) or Multi-Query Attention (MQA) to reduce this. Llama-3-70B uses 8 KV heads (with 64 query heads), whereas legacy architectures using Multi-Head Attention (MHA) set this equal to the query heads.
* **$D$** is the head dimension (typically 128).
* **$P$** is the precision in bytes (e.g., 2 for FP16 or BF16, 1 for FP8 quantized formats).

For Llama-3-70B running in BF16:

$$\text{Bytes Per Token} = 2 \times 80 \times 8 \times 128 \times 2 = 327,680 \text{ bytes } (\approx 320 \text{ KB})$$

If we pre-allocate contiguously for a maximum context length of 8,192 tokens:

$$\text{VRAM Per Request} = 8,192 \times 320 \text{ KB} = 2,621,440 \text{ KB} \approx 2.5 \text{ GB}$$

In a multi-tenant serving cluster, a request might only generate 150 tokens. With static allocation, 8,042 tokens worth of cache space—roughly 2.45 GB—remains reserved but unused. If you scale this across a concurrent user limit of 64 requests, you waste over 156 GB of GPU memory across your cluster. This waste prevents the engine from scaling its batch size to saturate the tensor cores of modern GPUs like the NVIDIA H100 or A100, which require high batch sizes to transition LLM decoding from a memory-bandwidth-bound state to a compute-bound state.

## Designing a C++ Physical Block Allocator

To solve fragmentation, we partition the KV cache into fixed-size physical blocks that correspond to a set number of tokens (typically 16 or 32). Instead of allocating a single contiguous block of memory for the lifetime of a sequence, we allocate physical blocks from a pre-allocated GPU tensor pool as the sequence grows. 

A high-performance C++ implementation must manage these physical blocks using a thread-safe page allocator. The allocator tracks which physical blocks are free and handles the reference counting required for structural sharing (e.g., sharing prompt prefixes between multiple concurrent sessions or branches in speculative decoding).

Below is the implementation of a C++ `BlockAllocator` that manages a pre-allocated block pool.

<script src="https://gist.github.com/mohashari/08d31110420646907c950957004626b7.js?file=snippet-1.txt"></script>

## Logical-to-Physical Mapping: The Scheduler's Responsibility

Each running sequence is assigned a `LogicalBlockTable` that records the mapping between its logical token positions and physical VRAM locations. For example, if our block size is 16, token index 35 maps to logical block 2 (`35 / 16 = 2`) at an offset of 3 (`35 % 16 = 3`). The `LogicalBlockTable` uses the `BlockAllocator` to resolve this logical block index to a physical block ID inside the GPU pool.

When implementing parallel sampling (e.g., beam search, fork-join workloads, or prefix caching), multiple sequences may initially share the same physical blocks. To prevent writing to a shared page and corrupting another sequence's history, the engine must implement Copy-on-Write (CoW) semantics.

Here is the C++ interface managing logical block mapping and Copy-on-Write logic:

<script src="https://gist.github.com/mohashari/08d31110420646907c950957004626b7.js?file=snippet-2.txt"></script>

## Launching the PagedAttention CUDA Kernel: Host-Side Coordination

Because the physical block IDs are allocated dynamically, they are not contiguous. When preparing to launch a CUDA attention kernel, we cannot pass a single contiguous base pointer for the key-value sequence. Instead, we must collect the list of physical block IDs for every sequence in our batch, construct a 2D block table representing this mapping, and copy it to GPU memory. 

This host-side coordination is critical: the grid launch must happen asynchronously with minimal latency to prevent CUDA stream starvation. 

Below is the code for the host-side scheduler, which serializes the block mappings of active requests and updates the GPU-bound metadata buffer.

<script src="https://gist.github.com/mohashari/08d31110420646907c950957004626b7.js?file=snippet-3.txt"></script>

## Writing the PagedAttention CUDA Kernel: Memory Address Calculations

Within the CUDA kernel, threads compute attention over past tokens by iterating through the logical index space. In a standard continuous KV-cache implementation, accessing key and value states is done by adding a linear offset to the start of the sequence. In a paged layout, the kernel must use the physical block tables to resolve the exact segment of memory where each token is stored.

To maximize memory bandwidth, the physical key and value caches are stored in a specialized, non-contiguous layout: `[num_blocks, num_heads, head_size / x, block_size, x]`. Here, `x` represents the vector size (e.g., 8 for FP16/BF16 values). This structure ensures that 128-bit memory loads align perfectly with global memory transactions on the GPU, avoiding uncoalesced memory accesses.

The following CUDA device function implements the address resolution logic:

<script src="https://gist.github.com/mohashari/08d31110420646907c950957004626b7.js?file=snippet-4.txt"></script>

## Scheduling and Preemption: Handling VRAM Exhaustion

LLM generation happens in two distinct operational phases:
1. **Prefill**: Processes the input prompt (parallel, compute-bound).
2. **Decode**: Generates output tokens one by one (sequential, memory-bound).

As decode requests progress, they dynamically allocate physical pages from the physical pool. Because request lengths are unpredictable, the system may run out of physical blocks before any active request finishes. If the allocator returns `-1`, the scheduler must preempt running requests to release VRAM and avoid memory allocation errors (which would crash the entire process).

There are two primary policies for request preemption:
* **Re-computation**: Drop the preempted request's KV cache entirely and release all its physical blocks. When VRAM pressure subsides, restart the request from the prefill phase.
* **Swapping (Offloading)**: Copy the physical blocks of preempted requests from GPU VRAM to host CPU RAM over the PCIe bus. When the request is scheduled to resume, swap the blocks back.

Swapping introduces latency overhead due to PCIe transfer limits. A PCIe Gen4 x16 link has a theoretical bandwidth limit of 32 GB/s. For a sequence containing 2,048 tokens in a Llama-3-70B model, copying the KV cache (approximately 650 MB) takes roughly 20ms. This is comparable to the latency of re-running a short prefill, but swapping avoids wasting compute cycles on long-context sequences.

The following C++ class implements an inference scheduler state machine that handles page allocations and applies the re-computation strategy to handle VRAM saturation.

<script src="https://gist.github.com/mohashari/08d31110420646907c950957004626b7.js?file=snippet-5.txt"></script>

## Production Benchmarks and Performance Tuning

Deploying a custom C++ inference engine with PagedAttention changes key system metrics:

* **VRAM Overhead Reduction**: Physical memory fragmentation drops from over 60% with static allocation to less than 4% under sustained load.
* **Throughput Scaling**: Serving throughput increases by $2\times$ to $4.5\times$ compared to static allocations on the same hardware, as the engine can pack more active sequences into the same physical footprint.
* **Tail Latency (p99)**: Eliminating large, contiguous allocations reduces the frequency of garbage collection pauses and dynamic memory fragmentation, stabilizing response times under peak concurrent loads.

### Fine-Tuning Block Sizes
* **Block Size 8**: Minimizes internal fragmentation within blocks, but increases the size of the block table. This can lead to L1 cache misses and overhead in the CUDA kernel because threads must repeatedly resolve page lookups.
* **Block Size 16**: The recommended trade-off for FP16 and BF16 models. It aligns with the memory fetch coalescing patterns of modern GPUs and maintains low block-table management overhead.
* **Block Size 32**: Improves kernel execution speeds slightly on short sequences, but increases internal fragmentation when requests finish with partially filled blocks. Use this size when serving quantized formats (like FP8 or INT4) to optimize block boundaries for vectorized hardware loads.