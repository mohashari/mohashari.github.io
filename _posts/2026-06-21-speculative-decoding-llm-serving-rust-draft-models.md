---
layout: post
title: "Speculative Decoding in Custom LLM Servicing: Implementing Draft Models in Rust"
date: 2026-06-21 08:00:00 +0700
tags: [rust, llm-inference, performance, speculative-decoding]
description: "A production-focused guide to implementing speculative decoding with draft models in Rust, featuring tensor operations and KV cache rollback."
image: "https://picsum.photos/seed/9441/1080/720"
thumbnail: "https://picsum.photos/seed/9441/400/300"
---

Autoregressive LLM generation in production is a memory bandwidth nightmare. When serving a model like Llama-3-70B in FP16, generating a single token requires reading 140 GB of weights from High Bandwidth Memory (HBM) to the GPU's SRAM. At a theoretical memory bandwidth limit of 2.0 TB/s on an NVIDIA A100 GPU, you are capped at roughly 14.5 tokens per second for a single request, leaving the tensor cores idling most of the time. While continuous batching helps amortize this cost across concurrent requests, single-user latency remains bound by memory bandwidth. Speculative decoding breaks this bottleneck. By using a lightweight draft model (e.g., Llama-3-8B) to speculatively generate a sequence of tokens, we can verify them in parallel in a single forward pass of the target model. This shifts the target model's execution from memory-bound to compute-bound, cutting latency by 1.8x to 2.5x in real-world API services.

## The Memory Bandwidth Wall and Speculative Math

To understand why speculative decoding works, we must analyze the arithmetic intensity of LLM generation. In the autoregressive generation phase, the model processes a single token at a time. The batch size is effectively small, meaning the ratio of floating-point operations to memory bytes transferred is extremely low. The GPU spends most of its time waiting for weights to load from HBM.

Speculative decoding bypasses this constraint by introducing a draft model $D$ (fast but less accurate) and a target model $T$ (slow but accurate). If we want to generate a sequence of tokens, we perform the following steps:
1. The draft model $D$ sequentially generates $K$ candidate tokens: $x_1, x_2, \dots, x_K$. This is fast because $D$ has far fewer parameters (e.g., 8B vs. 70B).
2. The target model $T$ runs a single forward pass on the prefix combined with all $K$ candidate tokens.
3. We stochastically verify the candidate tokens using the output distributions of both models.

Let $p(x)$ be the probability distribution of the target model, and $q(x)$ be the probability distribution of the draft model. For each candidate token $x_i$, we accept it with probability:

$$\alpha = \min\left(1.0, \frac{p(x_i)}{q(x_i)}\right)$$

If a token $x_i$ is accepted, we proceed to check $x_{i+1}$. If a token $x_i$ is rejected, we discard $x_i$ and all subsequent candidate tokens, and sample a new token from the adjusted distribution:

$$P'(x) = \frac{\max(0, p(x) - q(x))}{\sum_y \max(0, p(y) - q(y))}$$

This mathematical formulation guarantees that the final output distribution is identical to sampling directly from the target model. Thus, speculative decoding is a lossless optimization.

## Rust for Low-Latency LLM Servicing

While Python is the standard for machine learning prototyping, it introduces substantial overhead in production serving. Garbage collection pauses, Global Interpreter Lock (GIL) contention, and high memory footprints make it difficult to guarantee tight p99 latency SLAs. Custom Rust inference engines—leveraging libraries like `tch-rs` (Rust bindings for libtorch) or `candle`—allow developers to build memory-safe, ultra-low-latency serving systems. Rust gives us direct control over GPU allocations, memory layout, thread scheduling, and asynchronous task execution, which are critical when managing the concurrent execution of multiple models.

## KV Cache Rollback Mechanics

The primary engineering challenge in speculative decoding is managing the Key-Value (KV) cache. During standard autoregressive generation, keys and values are appended to the cache step-by-step. However, in speculative decoding, we append $K$ draft tokens to the cache, run validation, and then potentially reject some or all of them.

When a rejection occurs, we must roll back the KV cache pointer of both the draft and target models to match the number of accepted tokens. If we do not roll back the cache, the discarded tokens will pollute the attention context of future generation steps.

In a custom Rust engine using `tch-rs`, we pre-allocate contiguous KV cache tensors to avoid runtime allocation overhead. We track the logical length of the active sequence and use tensor slicing to overwrite rejected tokens in subsequent steps.

<script src="https://gist.github.com/mohashari/4dee5272c7cc30bea7e27e102b089523.js?file=snippet-1.txt"></script>

## The Verification Algorithm

The verification step takes the draft model's generated tokens, the draft model's probability distributions, and the target model's output logits. Since the target model evaluates all $K$ draft tokens in parallel, its output tensor includes the logits for $K+1$ positions (representing the prediction for the step after each draft token, plus the step following the final draft token).

We implement the stochastic verification algorithm using GPU operations. While it is tempting to copy these tensors back to the CPU to execute the loop, doing so introduces host-device synchronization barriers that destroy inference performance. Keep the distributions on the GPU, sample using multinomial operations, and only transfer the final accepted token count and correction token ID to the host.

<script src="https://gist.github.com/mohashari/4dee5272c7cc30bea7e27e102b089523.js?file=snippet-2.txt"></script>

## Specialized Causal Masking for Parallel Validation

When the target model validates the $K$ draft tokens, we submit them as a single sequence of length $K$ (or $K+1$ if we include the starting token). The self-attention mechanism must ensure that draft token $i$ can attend to the historical KV cache and draft tokens $0 \dots i-1$, but cannot attend to draft tokens $i+1 \dots K-1$.

To achieve this, we construct a custom speculative attention mask. Given historical KV cache length $L$ and draft sequence length $K$, the mask shape must be `[batch_size, 1, K, L + K]`.

For query position $q$ (where $0 \le q < K$) and key-value position $kv$ (where $0 \le kv < L + K$):
- If $kv < L$, it is a historical token; attention is allowed.
- If $kv \ge L$, it is a speculative draft token; attention is allowed only if $kv - L \le q$.

<script src="https://gist.github.com/mohashari/4dee5272c7cc30bea7e27e102b089523.js?file=snippet-3.txt"></script>

## Orchestrating the Inference Loop

To implement speculative decoding without adding latency overhead, we must avoid a common design pitfall: running a separate single-step target forward pass to compute the KV cache entry for the correction token. 

Instead, we structure the target model's input in the next iteration to contain the correction token as its first element, followed by the new $K$ draft tokens. The batch size to the target model becomes $K+1$. The target model computes the KV projections for the correction token in parallel with the new verification pass.

Here is the implementation of the orchestration engine in Rust:

<script src="https://gist.github.com/mohashari/4dee5272c7cc30bea7e27e102b089523.js?file=snippet-4.txt"></script>

## Production Pitfalls & Mitigation Strategies

While speculative decoding offers huge latency gains on paper, deploying it in high-throughput production environments exposes several critical bottlenecks:

### 1. CPU-GPU Synchronization Barriers

If your speculative engine checks the acceptance criteria on the CPU, you will force host-device synchronizations (e.g., calling `.double_value()` on GPU tensors inside the loop). In Rust, each sync blocks the execution thread and stalls the GPU pipeline.

**Mitigation:** Write the acceptance checks using PyTorch/LibTorch operators that run entirely on the GPU. Only return a tiny 2-element tensor to the CPU containing the `accepted_count` and `correction_token`.

### 2. KV Cache Memory Fragmentation

Under continuous batching, pre-allocating contiguous tensor segments per request leads to severe memory fragmentation. If a request rolls back its KV cache, the contiguous block is partially unused, but cannot be easily shared.

**Mitigation:** Implement a block-based memory allocator (PagedAttention). The KV cache is divided into fixed-size physical blocks (e.g., block size = 16 tokens). When rolling back, the block manager simply frees the physical blocks that map to indices beyond the rollback length.

### 3. Acceptance Rate Degradation

Speculative decoding relies on high acceptance rates. If the draft model accepts fewer than 1.5 tokens per step on average, the overhead of draft generation and parallel validation exceeds the latency of standard autoregressive generation. Acceptance rates drop dramatically during highly technical prompts, code generation, or when the temperature is set high.

**Mitigation:** Implement an adaptive draft lookahead $K$. Monitor the moving average acceptance rate per request. If it falls below a threshold (e.g., 40%), dynamically reduce $K$ from 5 to 2. If it increases, scale it back up.

To monitor these conditions, you must instrument your inference loop. The following Rust snippet shows how to integrate Prometheus metrics to track acceptance rates and phase latencies:

<script src="https://gist.github.com/mohashari/4dee5272c7cc30bea7e27e102b089523.js?file=snippet-5.txt"></script>