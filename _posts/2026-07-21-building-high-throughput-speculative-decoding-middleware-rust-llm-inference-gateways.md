---
layout: post
title: "Building a High-Throughput Speculative Decoding Middleware in Rust for LLM Inference Gateways"
date: 2026-07-21 08:00:00 +0700
tags: [rust, llm-inference, speculative-decoding, distributed-systems, ai-engineering]
description: "A deep dive into building a high-throughput, low-latency speculative decoding proxy in Rust to slash LLM inter-token latency without wasting H100 compute."
image: "https://picsum.photos/seed/7629/1080/720"
thumbnail: "https://picsum.photos/seed/7629/400/300"
---

When running large language models (LLMs) in production, you inevitably hit the memory-bandwidth wall: autoregressive generation requires loading the entire target model’s weights (e.g., Llama-3-70B) from GPU VRAM to SRAM for *every single token*. Under low batch sizes, an expensive H100 SXM5 instance (boasting 3.35 TB/s VRAM bandwidth) sits idle at 5% compute utilization, capped by memory-transfer speeds at a sluggish 40ms per token. While speculative decoding theoretically bypasses this by using a lightweight draft model (like Llama-3-8B) to generate candidate tokens that the target model verifies in parallel, embedding this logic inside the primary inference server couples compute tiers and creates massive scaling issues. If your draft model runs on the same GPU node, it steals VRAM and compute cycles from the target model; if you try to scale draft and target tiers independently, standard inference engines lack the proper isolation boundaries. 

This post details how to design and build an external, high-throughput speculative decoding middleware in Rust at the gateway level. By running the draft engine locally or on cheap L4 GPU nodes and validating proposals against a target H100 cluster via optimized gRPC validation pipelines, we decouple token generation from validation and achieve up to a 60% reduction in inter-token latency without altering target model weights.

## The Architecture of Gateway-Level Speculation

To implement speculative decoding at the gateway, we must intercept the client request and split the generation loop into two distinct loops: the **Proposer Loop** and the **Verification Loop**.

```
[ Client ] <--- (Tokens Stream) --- [ Rust Gateway Middleware ]
                                     |                    ^
                            (Draft Tokens)        (Validation Logits)
                                     v                    |
                           [ Local Draft Engine ]   [ Target H100 Cluster ]
                             (Llama-3-8B on L4)      (Llama-3-70B on TP-2)
```

1. **Draft Generation**: The gateway forwards the current token history to a local, low-latency draft engine (running a fast model like Llama-3-8B or an n-gram caching lookup) to produce $K$ candidate tokens.
2. **Parallel Validation**: The gateway appends the $K$ candidate tokens to the prompt history and sends this sequence to the target model in a single validation request. Because the target model evaluates these tokens simultaneously, it runs a compute-bound prefill operation rather than a memory-bound decode operation, verifying all $K$ candidates in nearly the same duration as a single token execution.
3. **Logits Verification**: The gateway receives the target model's logits, runs speculative verification (either greedy matching or stochastic Gibbs sampling), determines how many candidates ($M \le K$) are accepted, appends the target model's correction token for the first rejected position, and streams the accepted tokens back to the user.
4. **Iteration**: The gateway uses the accepted prefix to initiate the next draft iteration.

To manage the session state across these async boundaries, we design a thread-safe speculative session manager in Rust.

<script src="https://gist.github.com/mohashari/4ec30171a76b2ea3ff604e60d83c1acf.js?file=snippet-1.txt"></script>

## The Draft Proposer Client

The draft engine must be highly optimized. If the draft engine takes more than a few milliseconds to yield candidates, it eats into the latency savings of the target model's parallel verification pass. We wrap the draft engine in an asynchronous client that communicates with a local lightweight server (like a local `llama.cpp` instance or a lightweight local vLLM server running on an L4 GPU). 

We configure the HTTP client with raw TCP performance tweaks, disabling Nagle's algorithm (`tcp_nodelay(true)`) and setting aggressive timeouts to prevent draft failure from hanging the gateway.

<script src="https://gist.github.com/mohashari/4ec30171a76b2ea3ff604e60d83c1acf.js?file=snippet-2.txt"></script>

## High-Performance Target Validation Client via gRPC

To validate the $K$ candidate tokens in a single target pass, we avoid standard OpenAI-compatible REST APIs due to serialization overhead. A typical model vocabulary contains 128,000+ tokens. Returning complete logprob distributions for $K$ tokens via JSON requests can saturate the gateway's network interfaces, pushing networking overhead to 10ms+ and defeating the latency savings.

Instead, we use a custom gRPC service via Tonic. This protocol lets the gateway specify a `validation_offset`, telling the target engine to only compute and return logits for the speculative suffix (plus the token immediately preceding it).

<script src="https://gist.github.com/mohashari/4ec30171a76b2ea3ff604e60d83c1acf.js?file=snippet-3.txt"></script>

## Implementing Speculative Verification in Rust

Once we have the draft token logprobs and the target model’s logits, we run the verification algorithm. If the generation temperature is $0.0$, we execute **Greedy Verification** (argmax match). If the temperature $> 0.0$, we use **Stochastic Verification** (Gibbs sampling rule). 

Stochastic verification guarantees that the mathematical distribution of the output matches the target model's distribution *exactly*, ensuring zero degradation in text generation quality:
- Accept candidate token $x_i$ with probability $\min\left(1, \frac{P(x_i \mid x_{<i})}{Q(x_i \mid x_{<i})}\right)$, where $P$ and $Q$ are target and draft probability distributions.
- If rejected, sample the replacement token from the normalized difference distribution: $P'(x) = \frac{\max(0, P(x) - Q(x))}{\sum_y \max(0, P(y) - Q(y))}$.

<script src="https://gist.github.com/mohashari/4ec30171a76b2ea3ff604e60d83c1acf.js?file=snippet-4.txt"></script>

## The Core Orchestration Pipeline

With our components established, we construct the orchestration pipeline. The pipeline accepts input prompt tokens, manages the loop state, invokes the draft client, validates against the target cluster, processes the results, and writes verified tokens to a Tokio channel for streaming responses to clients.

<script src="https://gist.github.com/mohashari/4ec30171a76b2ea3ff604e60d83c1acf.js?file=snippet-5.txt"></script>

## Dynamic Speculation Controller

A static lookahead size ($K$) introduces a latency vulnerability. If the draft model aligns perfectly with the target model (e.g., repeating simple phrases, template structures, or clear code formatting), we want a high lookahead ($K = 7$) to maximize token throughput per round-trip. However, when the model enters a complex reasoning phase (e.g., mathematics or logic shifts), the draft model's predictions diverge from the target model's.

If the target model constantly rejects the first proposed token, you pay the latency cost of the draft proposals *plus* the target verification pass for only one returned token, degrading system performance to slower than baseline autoregressive generation.

To solve this, we implement a **Dynamic Speculation Controller** that monitors the rolling acceptance rate and dynamically shifts the speculative lookahead.

<script src="https://gist.github.com/mohashari/4ec30171a76b2ea3ff604e60d83c1acf.js?file=snippet-6.txt"></script>

## Production Latency Analysis: The Numbers

To demonstrate the impact of this architecture in production, let's look at the latency math. 

Consider a Llama-3-70B target model evaluated over Tensor Parallel 2 (TP-2) on A100 GPUs, paired with a Llama-3-8B draft model running on a local L4 GPU.

*   **Baseline Autoregressive Decode (Target Only)**:
    *   Time per token (TPT): **15ms**
    *   Evaluating 5 tokens sequentially: $5 \times 15\text{ms} = \mathbf{75\text{ms}}$

*   **Speculative Pipeline (With Lookahead $K = 5$)**:
    *   Local draft generation time (8B): **4ms per token** $\rightarrow 5 \times 4\text{ms} = 20\text{ms}$ total draft phase latency.
    *   Target validation time (70B) for 5 tokens: **18ms** (parallel processing makes validation compute-bound and highly parallelized).
    *   Total round-trip step time: $20\text{ms} + 18\text{ms} = \mathbf{38\text{ms}}$ (plus sub-millisecond networking overhead via gRPC).

### Latency Outcomes Based on Draft Acceptance Rate:

| Accepted Count ($M$) | Tokens Returned ($M+1$) | Step Latency | Effective TPT | Speedup vs. Baseline |
| :--- | :--- | :--- | :--- | :--- |
| **5 (Full Acceptance)** | 6 tokens | 38ms | **6.3ms** | **2.38x** |
| **3 (Average Case)** | 4 tokens | 38ms | **9.5ms** | **1.58x** |
| **1 (Low Alignment)** | 2 tokens | 38ms | **19.0ms** | **0.79x** (Slower) |
| **0 (Complete Reject)**| 1 token | 38ms | **38.0ms** | **0.39x** (Severe overhead) |

This distribution illustrates the threat of **Speculation Collapse** and explains why the `DynamicSpeculationController` is vital. By dropping the lookahead parameter $K$ down to 1 during reasoning phases, the step latency falls to $4\text{ms} + 15\text{ms} = 19\text{ms}$. If accepted, we generate 2 tokens in 19ms (9.5ms/token); if rejected, we generate 1 token in 19ms, mitigating worst-case penalties to just 4ms over the baseline.

## Production Failure Modes & Mitigations

Deploying a speculative decoding middleware introduces unique failure modes that do not exist in traditional inference setups.

### 1. KV-Cache Fragmentation and Out-of-Sequence Rollbacks
When the target model evaluates the prompt plus the $K$ candidate tokens, its key-value (KV) cache grows by $K+1$ entries. However, if the verification algorithm only accepts $M < K$ tokens, the target model's KV cache is now out-of-sync, holding invalid cache entries for the rejected speculative tokens.

*   **Failure Mode**: The engine continues generation from an corrupted KV-cache state, causing gibberish output.
*   **Mitigation**: The target model's inference engine must support KV-cache truncation via physical block reclamation. When using vLLM, configure the runner with API-driven sequence rollbacks, or force state tracking via **Radix Attention (Prefix Caching)**. Radix attention caches KV blocks by hashing the exact token prefixes. When a rollback occurs, the gateway requests evaluation of the accepted prefix. The engine hits the prefix cache, automatically evicting the invalid speculative branch.

### 2. Network Deserialization Bottlenecks
A naive implementation that requests all token logprobs from the target model will exhaust network capacity. 

*   **Failure Mode**: Deserializing a vector of floats size `[lookahead + 1, 128256]` (approximately $6 \times 128,256 \times 4\text{ bytes} \approx 3\text{MB}$ per validation pass) over gRPC adds 15-20ms of serialization delay, erasing the performance gains of speculation.
*   **Mitigation**: Restrict logits retrieval. The gateway already knows the draft tokens it proposed. The target service API should only return:
    1. The logits corresponding to the indices of the proposed draft tokens (for verification).
    2. The full probability distribution *only* for the single token index where draft proposals fail (to sample the replacement token).
    This cuts payload sizes down to less than 1KB, dropping network transport and deserialization overhead to sub-millisecond levels.