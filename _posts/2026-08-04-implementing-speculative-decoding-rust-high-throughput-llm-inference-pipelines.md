---
layout: post
title: "Implementing Speculative Decoding in Rust for High-Throughput LLM Inference Pipelines"
date: 2026-08-04 08:00:00 +0700
tags: [rust, llm-inference, performance, speculative-decoding, ai-engineering]
description: "A production-focused guide to building a high-throughput LLM token verification engine in Rust, including KV-cache rollback and adaptive speculation."
image: "https://picsum.photos/seed/9481/1080/720"
thumbnail: "https://picsum.photos/seed/9481/400/300"
---

At production scale, LLM autoregressive decoding is fundamentally memory-bandwidth bound. When serving a 70B parameter model in FP16, every single token generated requires reading approximately 140 GB of model weights from high-bandwidth memory (HBM) to the GPU’s SRAM. On an NVIDIA H100 SXM5 with a memory bandwidth of 3.35 TB/s, the absolute physical limit for a single batch-size-1 stream is roughly 24 tokens per second, regardless of how much compute the GPU has. Speculative decoding breaks this memory-bandwidth bottleneck. By using a draft model (e.g., a 1B or 3B parameter model) to speculatively generate a sequence of $K$ candidate tokens, we can verify all of them in a single forward pass of the target 70B model. Because the target model’s weights are loaded only once to verify multiple tokens, we shift the execution profile from memory-bound to compute-bound, yielding 2x to 3.5x throughput improvements in production.

Implementing this architecture requires a systems-level programming language capable of microsecond-precision scheduling, direct GPU memory management, and zero-overhead abstraction. This post details how to implement a high-throughput speculative decoding pipeline in Rust, addressing token verification math, KV-cache rollback, async orchestration, and adaptive speculation heuristics.

## The Mathematics of Speculative Verification

The core challenge of speculative decoding is ensuring that the final output distribution matches the target model's output distribution *exactly*, preserving mathematical equivalence. We achieve this using a modified rejection sampling scheme. 

Let the draft model distribution at step $i$ be $q(x)$ and the target model distribution be $p(x)$. When the draft model proposes a token $x$, we accept it with the probability:

$$P(\text{accept}) = \min\left(1, \frac{p(x)}{q(x)}\right)$$

If the token is accepted, we proceed to verify the next proposed token. If a token $x$ is rejected at index $j$ (where $j \le K$), we discard all subsequent proposed tokens from $j+1$ to $K$. We then sample a replacement token from the normalized difference distribution:

$$p'(x) = \frac{\max(0, p(x) - q(x))}{\sum_{y} \max(0, p(y) - q(y))}$$

This guarantees that the sampled token corrects the draft model’s error while maintaining the target model’s statistical fidelity.

Here is the implementation of this verification engine in Rust. It operates on raw logits, applies temperature scaling, calculates acceptance probabilities, and performs the fallback sampling when a draft token fails validation.

<script src="https://gist.github.com/mohashari/f7ecb84dd4f0b4d5fc3c9b1e142f386e.js?file=snippet-1.txt"></script>

## KV Cache Rollback Dynamics

When the target model evaluates the $K$ draft tokens, it processes them in a single, parallel forward pass. To do this, the target model's key-value (KV) cache is populated with activation states for all $K$ tokens. 

If the verification engine rejects a token at index $j$, the KV cache entries corresponding to the rejected speculative positions ($j+1$ to $K$) are dirty and contain invalid data. Before generating the next draft block, these invalid slots must be rolled back. 

In a high-throughput engine utilizing PagedAttention, physical GPU memory is divided into blocks, mapped via a page table. The speculative verification must update the slot allocations and truncate the logical page table to the point of rejection.

Here is the implementation of a memory-efficient physical KV cache manager in Rust that handles logical block allocation, tracking, and speculative rollback.

<script src="https://gist.github.com/mohashari/f7ecb84dd4f0b4d5fc3c9b1e142f386e.js?file=snippet-2.txt"></script>

## The Multi-threaded Orchestration Loop

In a production inference pipeline, the draft model and the target model should run concurrently on separate CUDA streams, orchestrated by an asynchronous task runner. 

The pipeline works in cycles:
1. The orchestrator triggers the draft model to generate $K$ tokens.
2. The generated token IDs, along with their soft distribution statistics, are pushed to the target queue.
3. The target model executes verification on the GPU.
4. The verification determines the accepted prefix, invokes the `KVCacheManager` to clean up the states, and updates the token buffer.

If step 1 and step 3 are not executed cleanly, thread contention can saturate the tokio executor, introducing latency spikes. Below is a production-style async orchestration pipeline running the speculative loops using thread-safe coordination primitives.

<script src="https://gist.github.com/mohashari/f7ecb84dd4f0b4d5fc3c9b1e142f386e.js?file=snippet-3.txt"></script>

## Adaptive Speculation Engine

Speculative decoding is not a guaranteed latency win under all distributions. If the draft model’s generation quality drops—such as when translating code, processing complex math, or experiencing input domain shifts—the target model will frequently reject proposals. 

When the acceptance rate drops below a mathematical threshold, speculative decoding becomes slower than standard decoding. This is because we incur the overhead of running both the draft model and the target verification pass without gaining multi-token generation steps.

$$\text{Latency Step Ratio} = \frac{t_{\text{draft}} \cdot K + t_{\text{target}}}{M}$$

Where:
* $t_{\text{draft}}$: Average forward pass time of the draft model.
* $t_{\text{target}}$: Average forward pass time of the target model.
* $K$: Speculative draft length.
* $M$: Actual number of accepted tokens (plus the corrected token).

To avoid performance degradation, we implement a PID-like or heuristic adaptive controller that monitors the running acceptance rate and dynamically scales the speculative window $K$ at runtime.

<script src="https://gist.github.com/mohashari/f7ecb84dd4f0b4d5fc3c9b1e142f386e.js?file=snippet-4.txt"></script>

## Production Profiling and Failure Modes

Deploying speculative decoding in a production cluster requires monitoring hardware utilization metrics. Here are the failure modes to watch for, based on engineering experience.

### 1. The Dual-Model Memory Overhead
Speculative decoding requires hosting *two* models on the same accelerator. If you run a 70B parameter model in FP16, it takes up ~140 GB of VRAM. A 8B draft model requires another ~16 GB. 

If this pushes your GPU over its memory capacity, you force model weights onto the system RAM, triggering PCIe bandwidth limitations. 

Always calculate your target allocation envelope:

$$\text{VRAM}_{\text{total}} = \text{VRAM}_{\text{target\_weights}} + \text{VRAM}_{\text{draft\_weights}} + \text{VRAM}_{\text{KV\_cache}} + \text{VRAM}_{\text{system}}$$

If the KV cache pool size is reduced too much to fit the draft model weights, the system will trigger frequent page eviction failures, degrading throughput under high concurrent load.

### 2. GPU Kernel Launch Overhead
When the draft model runs $K$ steps sequentially, it launches $K$ separate forward pass kernels. Because draft models are small (e.g. 1B), these kernels execute in sub-millisecond durations. 

If your host CPU driver overhead is high, the time spent dispatching the GPU kernel exceeds the kernel execution time. 

To mitigate this, you must run the draft model using CUDA Graphs to record and replay the execution sequences, reducing host CPU interaction overhead.

<script src="https://gist.github.com/mohashari/f7ecb84dd4f0b4d5fc3c9b1e142f386e.js?file=snippet-5.txt"></script>

### 3. Dynamic Batching Synchronization Bottleneck
When running multiple speculative requests in a single batch, different requests will accept different numbers of tokens. For example:
* Request A accepts 4 tokens.
* Request B accepts 0 tokens.
* Request C accepts 2 tokens.

This introduces a jagged array structure. If you pad requests to maintain uniform execution shapes, you waste memory and compute. If you do not pad, you must split requests and run them on separate streams, which introduces scheduling overhead. 

In a production engine, you must use a scheduler that supports *PagedAttention-based batch compaction*. This dynamically modifies the block sequence lists inside the execution page tables after every verification step, ensuring optimal hardware utilization without padding.

## Architectural Deployment Matrix

Below is a reference summary to help guide deployment decisions based on different infrastructure setups:

| System Parameter | Small Draft (1B - 3B) + Large Target (70B) | Medium Draft (7B) + Large Target (70B) |
| :--- | :--- | :--- |
| **Typical Target HBM Bandwidth** | $\ge 2.0\text{ TB/s}$ (A100 / H100) | $\ge 3.0\text{ TB/s}$ (H100 / H200) |
| **Draft-to-Target Speed Ratio** | $10:1 \text{ to } 15:1$ | $4:1 \text{ to } 6:1$ |
| **Optimal Speculation Depth ($K$)** | $4 - 6$ | $2 - 4$ |
| **Critical Bottleneck** | Host CPU kernel launch overhead | VRAM capacity constraints |

By keeping these performance characteristics and system limits in mind, you can build speculative decoding pipelines in Rust that maintain low tail latency under high-throughput production workloads.