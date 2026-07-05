---
layout: post
title: "Speculative Decoding in Production: Accelerating LLM Inference via Custom Draft Models"
date: 2026-07-05 08:00:00 +0700
tags: [ai-engineering, llm-inference, performance-optimization, speculative-decoding]
description: "Accelerate LLM serving throughput and reduce inter-token latency using custom-distilled draft models and parallel verification."
image: "https://picsum.photos/seed/5672/1080/720"
thumbnail: "https://picsum.photos/seed/5672/400/300"
---

In high-scale LLM production serving, the bottleneck is rarely compute capacity—it is memory bandwidth. When serving a Llama-3-70B model at high concurrency, the GPU spends the vast majority of its clock cycles idle, waiting to fetch model parameters (amounting to 140 GB in FP16) from High Bandwidth Memory (HBM) for every single generated token. This autoregressive execution model leads to an extremely low Model Flops Utilization (MFU) of around 3% to 7% during decoding, causing high Inter-Token Latency (ITL) and ballooning infrastructure costs. Speculative decoding bypasses this physical bottleneck by using a tiny, low-latency "draft" model to speculatively generate a sequence of $K$ candidate tokens, which are then verified by the large target model in a single, parallel forward pass. Instead of executing $K$ separate, memory-bound sequential passes on the target model, we run only one, effectively trading underutilized compute for memory bandwidth saved and achieving up to a 2.5x speedup without altering the final output distribution.

![Speculative Decoding in Production: Accelerating LLM Inference via Custom Draft Models Diagram](/images/diagrams/speculative-decoding-production-accelerating-llm-inference-custom-draft-models.svg)

## The Memory Bandwidth Bottleneck of Autoregressive LLM Inference

To understand why speculative decoding is necessary, we must look at the arithmetic intensity of autoregressive generation. In the decoding phase, an LLM processes one token at a time. For each token, the GPU must load every single parameter of the model from HBM to SRAM to perform the matrix-vector multiplications. 

Consider an Nvidia H100 SXM5 GPU, which provides 3.35 TB/s of memory bandwidth and 989 TFLOPS of FP16 tensor core compute. If we serve a Llama-3-70B model in FP16, the parameter size is approximately $70 \times 10^9 \times 2 \text{ bytes} = 140 \text{ GB}$. 
The minimum time required to read these weights once from memory is:

$$\text{Latency}_{\text{min}} = \frac{140 \text{ GB}}{3350 \text{ GB/s}} \approx 41.8 \text{ ms}$$

During this time, the mathematical operations performed per token are $2 \times 70 \times 10^9 = 140 \text{ GFLOPs}$. The compute capability allows us to process this in:

$$\text{Compute Time} = \frac{140 \times 10^9 \text{ FLOPs}}{989 \times 10^{12} \text{ FLOPs/s}} \approx 0.14 \text{ ms}$$

The ratio of compute time to memory transit time is less than 0.5%. The tensor cores are starved for data, waiting for the HBM to stream weights. This is the definition of a memory-bound workload. 

Speculative decoding changes this equation by utilizing the concept of batching. While generating a single token is memory-bound, verifying multiple tokens in parallel is compute-bound. By using a draft model (e.g., Llama-3.2-1B) that can generate tokens at a fraction of the cost (~2ms per token), we can construct a speculative block of length $K$. The target model then runs a single forward pass on the batch of $K$ tokens. Since the parameters are loaded once and applied to $K$ tokens in parallel, the memory transfer cost is amortized across the sequence.

## Speculative Decoding Mechanics: The Mathematics of Verification

The primary requirement of speculative decoding in production is *mathematical equivalence*. The output distribution of the speculative system must be identical to the output distribution of the target model running standalone. We achieve this by using a rejection sampling mechanism.

Let $P(x)$ be the probability distribution over the vocabulary computed by the target model, and $Q(x)$ be the probability distribution computed by the draft model. For a draft token $x$ sampled from $Q(x)$, we accept the token with probability:

$$\alpha = \min\left(1.0, \frac{P(x)}{Q(x)}\right)$$

If the token is accepted, we proceed to verify the next token in the speculative sequence. If the token is rejected, we discard the current token and all subsequent draft tokens in the window. To maintain the target distribution, we sample a replacement token from the adjusted distribution:

$$P'(x) = \frac{\max(0, P(x) - Q(x))}{\sum_{y} \max(0, P(y) - Q(y))}$$

This guarantees that the combined system outputs tokens with the exact probability distribution $P(x)$. Below is the PyTorch implementation of this validation algorithm.

<script src="https://gist.github.com/mohashari/3ca5cf2a7c77c67984c82e2cc0717514.js?file=snippet-1.py"></script>

## Designing and Distilling a Custom Draft Model

While using a standard off-the-shelf small model (e.g., Llama-3.2-1B as a draft for Llama-3-70B) is a common starting point, it often yields suboptimal results in production. If the draft model tokenizer does not align perfectly with the target tokenizer, speculative decoding is impossible without complex mapping layers that degrade latency. Furthermore, the draft model must mimic the target model's output distribution closely; otherwise, the acceptance rate collapses, and the draft step becomes pure overhead.

To maximize the acceptance rate, you should distill a custom draft model directly from the target model's outputs on your target domain. The distillation objective combines standard cross-entropy loss with Kullback-Leibler (KL) divergence to match the soft target logits of the target model.

Below is a production distillation step using KL-Divergence loss to align the student (draft) logits with the teacher (target) logits.

<script src="https://gist.github.com/mohashari/3ca5cf2a7c77c67984c82e2cc0717514.js?file=snippet-2.py"></script>

## Serving Architecture and KV Cache Syncing

Integrating speculative decoding into a high-throughput inference engine like vLLM or TensorRT-LLM introduces architectural complexity, particularly around Key-Value (KV) cache management. 

In speculative decoding, both the draft model and the target model maintain their own KV caches. For every decoding step:
1. The draft model generates $K$ tokens. Its KV cache grows by $K$ slots.
2. The target model evaluates the $K$ draft tokens plus the prompt context in one parallel forward pass.
3. The engine runs verification. If only $M$ tokens are accepted (where $M < K$), the engine must rollback the KV cache for both the draft and target models, evicting the $K - M$ invalid tokens.

If you are using vLLM, this is managed natively using PagedAttention, but you must configure the engine to allocate appropriate memory to both models and set the correct speculation limits.

<script src="https://gist.github.com/mohashari/3ca5cf2a7c77c67984c82e2cc0717514.js?file=snippet-3.py"></script>

To run benchmarks on this system and track inter-token latency (ITL) and throughput, you can use an asynchronous client script that simulates concurrent traffic.

<script src="https://gist.github.com/mohashari/3ca5cf2a7c77c67984c82e2cc0717514.js?file=snippet-4.py"></script>

## Production Monitoring and Observability

In production, speculative decoding is not a guaranteed win. If the draft model's acceptance rate drops below a certain threshold, the system will perform worse than standard autoregressive generation because of the overhead of running the draft model and throwing away its work. 

The threshold for speculative decoding to be viable is defined by:

$$\text{Acceptance Rate} > \frac{\text{Latency}_{\text{Draft}}}{\text{Latency}_{\text{Target}}}$$

For Llama-3-70B (latency $\approx 25\text{ms}$ on 4x H100) and Llama-3.2-1B (latency $\approx 2\text{ms}$), the ratio is $2/25 = 0.08$. If your acceptance rate falls below 8%, speculative decoding becomes a net latency penalty.

To keep track of this in production, you must export metrics to Prometheus. Specifically, monitor:
- Draft Acceptance Rate (average number of accepted tokens per step)
- Step Latency
- KV Cache evictions

<script src="https://gist.github.com/mohashari/3ca5cf2a7c77c67984c82e2cc0717514.js?file=snippet-5.py"></script>

## Production Failure Modes and Mitigation Strategies

Deploying speculative decoding introduces several failure modes unique to dual-model inference engines. Below are the primary failure modes encountered in production and how to mitigate them:

### 1. Draft Acceptance Rate Collapse (Entropy Shift)
* **The Symptom:** Latency increases during periods of creative writing, code generation, or formatting tasks. 
* **The Root Cause:** High temperature settings ($> 0.8$) or highly technical domains (where token probability distribution is flat or has high entropy) cause the draft model to choose paths that the target model rejects. 
* **Mitigation:** Implement dynamic window sizing. If the running average of accepted tokens drops below 1.5 over the last 10 requests, dynamically reduce the speculative window size $K$ from 5 down to 1 or 2, minimizing the draft model's execution overhead.

### 2. GPU Memory Fragmentation and Out-of-Memory (OOM)
* **The Symptom:** Inference instances randomly OOM under high concurrency, even when vLLM limits target model memory.
* **The Root Cause:** Standard vLLM instances reserve memory based on a single model. When running speculative decoding, the engine must reserve space for the draft model's parameters and its independent KV cache. If not partitioned correctly, the draft model competes for block allocations, causing fragmentation.
* **Mitigation:** Cap the draft model's memory footprint by setting a lower KV cache page limit for the draft engine, and allocate a maximum of 10% of the total GPU memory to the draft model parameters and cache.

### 3. Tensor Parallelism Synchronization Overhead
* **The Symptom:** Speedup is minimal when running target models across large tensor parallel sizes (e.g., TP=8).
* **The Root Cause:** In a TP configuration, the draft model typically runs on a single GPU to avoid communication overhead (since communication cost on a 1B model dominates its compute cost). However, synchronization between the single GPU running the draft model and the multiple GPUs running the target model introduces NVLink/PCIe transit latencies that consume the speedup.
* **Mitigation:** Ensure the draft model resides on GPU 0 (the primary orchestrator) and copy the draft tokens directly into the target model's input tensor buffers via unified CUDA memory space rather than going through CPU RAM host hops.