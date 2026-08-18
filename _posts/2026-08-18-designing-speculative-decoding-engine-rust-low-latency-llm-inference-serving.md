---
layout: post
title: "Designing a Speculative Decoding Engine in Rust for Low-Latency LLM Inference Serving"
date: 2026-08-18 08:00:00 +0700
tags: [rust, llm-inference, system-programming, performance]
description: "A deep dive into building a high-performance speculative decoding engine in Rust, featuring concurrent GPU scheduling, paged KV cache rollback, and speculative sampling."
---

Deploying large language models (LLMs) like Llama-3-70B in production often hits a hard wall: inter-token latency. For single-user streams, autoregressive generation is strictly bottlenecked by GPU memory bandwidth rather than compute. Every single generated token requires reading the entire 140GB model (for FP16 weights) from High Bandwidth Memory (HBM) to the SRAM of the streaming processors, resulting in a typical throughput of only 15–20 tokens per second per GPU. Speculative decoding bypasses this memory bottleneck by pairing the large, high-quality "target" model with a much smaller, faster "draft" model (such as a 1B or 8B parameter distilled variant). The draft model generates a sequence of $K$ candidate tokens at high speed, and the target model verifies them in a single, parallel forward pass. By amortizing the massive weight-loading cost of the target model over $K$ tokens, we shift the execution regime from memory-bound to compute-bound, achieving a 2x to 3x reduction in inter-token latency in real-world serving pipelines.

![Designing a Speculative Decoding Engine in Rust for Low-Latency LLM Inference Serving Diagram](/images/diagrams/designing-speculative-decoding-engine-rust-low-latency-llm-inference-serving.svg)

## The Memory Bandwidth Bottleneck of LLM Inference

To understand why speculative decoding is so effective, we must analyze the hardware constraints of modern GPUs like the NVIDIA H100 SXM5 (80GB). The H100 boasts approximately 3.35 TB/s of memory bandwidth and 2,000 TFLOPS of half-precision tensor core compute. 

During standard autoregressive generation, we process a single token at a time with a batch size of 1. Let $P$ be the number of parameters in the model. A forward pass requires loading $2P$ bytes of weights (in FP16 precision) and performing $2P$ floating-point operations per token. For Llama-3-70B:
* **Memory read per token:** $70 \times 10^9 \text{ parameters} \times 2 \text{ bytes} \approx 140 \text{ GB}$
* **Compute per token:** $2 \times 70 \times 10^9 \text{ FLOPs} \approx 140 \text{ GFLOPs}$

The time taken to load the weights is:
$$\text{Time}_{\text{memory}} = \frac{140 \text{ GB}}{3.35 \text{ TB/s}} \approx 41.8 \text{ ms}$$

The time taken to perform the compute is:
$$\text{Time}_{\text{compute}} = \frac{140 \text{ GFLOPs}}{2000 \text{ TFLOPS}} \approx 0.07 \text{ ms}$$

The ratio of memory-to-compute time is roughly 600:1. The tensor cores are idle 99.8% of the time, waiting for weights to arrive from HBM. 

Speculative decoding changes this dynamics. If the draft model generates $K = 5$ tokens, the target model evaluates all $5$ tokens in parallel in a single forward pass. Because we batch the tokens along the sequence dimension, we load the target model weights from HBM *exactly once*, but use them to perform compute across all $5$ tokens. The memory access remains at 140 GB, but the compute increases to 700 GFLOPs. The execution remains memory-bound, but the weight loading is amortized. If the target model accepts all 5 tokens, we generate 5 tokens in roughly the same time it would have taken to generate a single token, reducing the latency per token by up to 80%.

## Structuring the Concurrent Engine in Rust

Implementing this engine requires tight coordination between two distinct neural network execution contexts. The draft model runs sequentially for $K$ steps, while the target model waits. Once $K$ draft tokens are produced, the draft model pauses, and the target model executes its verification pass. 

Rust is uniquely suited for this architecture due to its zero-cost abstractions, predictable execution profile, and data-race prevention. To avoid the overhead and scheduling jitter of generic async runtimes like Tokio when dealing with heavy CPU/GPU coordination, we structure the engine using dedicated OS threads that communicate via lock-free channels. The engine uses a coordinator thread to orchestrate the execution loop, dispatching commands to dedicated worker threads managing the draft and target models.

Below is the design of the engine's coordinator loop.

<script src="https://gist.github.com/mohashari/6404eeecdd6aac689410c3e8e94286cd.js?file=snippet-1.txt"></script>

## High-Performance KV Cache Alignment and Rollback

Managing the Key-Value (KV) cache is the primary point of failure when designing speculative engines. As the draft model runs auto-regressively, it populates its KV cache up to index $T + K$. If the target model subsequently rejects the draft tokens starting at index $n$ (where $n < K$), both the draft model and target model must discard the invalid key-value activations from step $n$ onwards.

In high-throughput serving systems, we cannot afford contiguous memory allocations. Instead, we use a paged KV cache (similar to vLLM), where keys and values are stored in non-contiguous physical blocks managed by a block table. In speculative decoding, the block table must support rolling back logical blocks and updating active slot offsets.

Here is how we implement a paged KV cache manager in Rust that handles speculative rollbacks.

<script src="https://gist.github.com/mohashari/6404eeecdd6aac689410c3e8e94286cd.js?file=snippet-2.txt"></script>

## Implementing the Verification Kernels and Speculative Sampling

Speculative decoding does not compromise output quality because it uses modified nucleus/temperature sampling to ensure the final token distribution matches the target distribution *exactly*.

Let $p(x)$ be the probability of token $x$ under the target model, and $q(x)$ be the probability of token $x$ under the draft model. For each proposed draft token $t_i$:
1. We evaluate the target model in parallel to find target probabilities $p(t_i)$.
2. We accept $t_i$ with probability:
   $$\alpha = \min\left(1.0, \frac{p(t_i)}{q(t_i)}\right)$$
3. If $t_i$ is accepted, we proceed to test the next token $t_{i+1}$.
4. If $t_i$ is rejected, we discard all subsequent tokens ($t_{i+1} \dots t_K$) and sample a new correction token $t'_i$ from the adjusted distribution:
   $$p'(x) = \text{normalize}\left(\max\left(0, p(x) - q(x)\right)\right)$$

This logic must be implemented in high-performance Rust, optimizing for hardware vectorized operations while avoiding costly memory allocations on the hot path.

<script src="https://gist.github.com/mohashari/6404eeecdd6aac689410c3e8e94286cd.js?file=snippet-3.txt"></script>

## Handling Tensor Parallelism and Async GPU Execution

In production environments, target models (such as Llama-3-70B) are split across multiple GPUs using Tensor Parallelism (TP) to fit into memory and parallelize GEMM kernels. The draft model is small enough to run on a single GPU (or even a fraction of one). This creates an execution challenge: the target workers must wait for the draft worker to finish generation, but transfer overhead over PCIe can bottleneck performance.

To minimize latency, we use non-blocking CUDA streams via the `cudarc` Rust crate to enqueue the target model's input copy operations before the draft execution has finished. The engine schedules GPU kernels asynchronously on separate CUDA streams, aligning them with CUDA events to prevent host-side synchronization roundtrips.

<script src="https://gist.github.com/mohashari/6404eeecdd6aac689410c3e8e94286cd.js?file=snippet-4.txt"></script>

## Production Failure Modes and Tuning Speculative Parameters

While speculative decoding sounds like a free lunch, implementing it in production reveals several critical failure modes:

### 1. Draft-Target Mismatch (Rejection Collapse)
If the draft model's output distribution deviates significantly from the target model's distribution (e.g., when generating code, JSON, or specialized domain content), the acceptance rate $\alpha$ collapses. 
If $\alpha$ drops below a certain threshold, the latency of speculative decoding becomes *worse* than vanilla autoregressive generation. For example, if the draft model takes 10ms per token, the target model takes 40ms to verify, and $K=5$, a complete rejection means we spent $5 \times 10\text{ms} + 40\text{ms} = 90\text{ms}$ to output a single token, which is twice as slow as the target model generating a token alone (40ms). 

To prevent this, the engine must monitor the acceptance rate using an Exponential Moving Average (EMA) and dynamically scale $K$.

### 2. High Sequence Length Penalty
As the sequence length grows, the KV cache lookup costs increase. If the draft model runs on the CPU while the target model runs on the GPU, host-to-device (H2D) synchronization latencies over PCIe can consume all performance gains. Production engines should run both models on the same GPU physical partition using separate CUDA Streams and Shared Memory (IPC) pointers.

Here is the implementation of a dynamic controller that adapts speculation parameters in real-time.

<script src="https://gist.github.com/mohashari/6404eeecdd6aac689410c3e8e94286cd.js?file=snippet-5.txt"></script>

By pairing a concurrent Rust execution loop with a paged KV cache manager, low-overhead CUDA stream synchronization, and a dynamic parameter controller, backend systems can scale LLM generation to support high-throughput, low-latency applications on modern GPU clusters.