---
layout: post
title: "Speculative Decoding and Continuous Batching for High-Throughput LLM Serving"
date: 2026-03-30 08:00:00 +0700
tags: [llm, inference, gpu, vllm, ai-engineering]
description: "How speculative decoding and continuous batching eliminate GPU idle time and latency bubbles in production LLM serving."
image: "https://picsum.photos/seed/2875/1080/720"
thumbnail: "https://picsum.photos/seed/2875/400/300"
---

You deploy a 70B LLM behind an API. Peak traffic hits, GPU utilization reads a healthy 90%, but p99 latency is 12 seconds for a 200-token response. You dig in and find two separate problems: the GPU spends most of its time waiting on autoregressive decode steps that are inherently serial (one token per forward pass), and your batching strategy stalls new requests behind long-running ones. These are not the same problem, but they compound each other badly. Speculative decoding attacks the first by amortizing forward passes; continuous batching attacks the second by eliminating batch-level head-of-line blocking. Together they can push a well-configured vLLM deployment to 3–5× the throughput of a naive FastAPI + HuggingFace `generate()` setup, at lower latency.

![Speculative Decoding and Continuous Batching for High-Throughput LLM Serving Diagram](/images/diagrams/speculative-decoding-continuous-batching-llm-serving-throughput.svg)

## Why Autoregressive Decoding Is the Bottleneck

Transformer inference has two distinct phases. Prefill processes the entire prompt in one parallel forward pass — compute-bound, efficient, GPU loves it. Decode generates one token at a time, each pass attending over a growing KV cache — memory-bandwidth-bound, serial, GPU mostly sits idle waiting on memory reads. For a typical 70B model on an A100 80GB, prefill for a 512-token prompt takes ~50ms. Generating 200 output tokens takes ~4 seconds, and every one of those 200 forward passes loads the full model weights plus the KV cache from HBM. The ratio of useful FLOPS to memory bandwidth consumed collapses.

The root cause: batch size during decode is small relative to prefill. Each request contributes one token to the batch per decode step. If you have 8 concurrent requests, your decode batch size is 8 — far below the hundreds or thousands needed to saturate A100 tensor cores. This is why TTFT (time to first token) looks fine in benchmarks, but TBT (time between tokens) and E2E latency are the real production pain.

## Speculative Decoding: Draft Fast, Verify Cheap

Speculative decoding reframes the problem. Instead of generating one token with the large model per step, you:

1. Run a small draft model to generate `k` candidate tokens speculatively (k=4 is common).
2. Run the large target model once to verify all `k` tokens in parallel.
3. Accept the longest prefix of draft tokens that the target model agrees with; resample the first rejected token from the target's distribution; discard the rest.

The key insight is that verification is cheap. The target model can evaluate `k` tokens in a single forward pass by running them through the attention layers simultaneously — this is just a wider prefill. If the draft model has a high accept rate (>80%), you go from `k` target forward passes down to ~1, netting you close to `k`× throughput on the target model. The output distribution is mathematically identical to running the target model alone — this is lossless, not approximate.

**Draft model selection matters a lot.** The draft model must be fast enough that drafting `k` tokens costs less than one target forward pass. In practice this means the draft model should be 7–13B when the target is 70B, or a dedicated small model trained to match the target's distribution (e.g., Google's Medusa heads, which are lightweight MLP heads attached to the target model itself, eliminating draft model latency entirely). vLLM 0.4+ ships native speculative decoding; you enable it with `--speculative-model` and `--num-speculative-tokens`.

**Accept rate is domain-sensitive.** For predictable outputs — code completion, structured JSON, templated responses — draft accept rates of 85–92% are realistic, yielding 2.5–4× speedup. For open-ended creative generation where the output distribution is flat and the draft model diverges from the target, accept rates drop to 50–60% and speculative decoding may actually hurt due to draft model overhead. Measure per-workload before committing.

**The failure mode nobody talks about:** speculation depth `k` interacts badly with KV cache pressure. Each speculative token allocates KV cache blocks speculatively; if the token is rejected, those blocks are freed, but the allocator still needed to reserve them. Under high concurrency, aggressive speculation can starve the scheduler of KV cache blocks, causing unnecessary preemption of in-flight requests. vLLM handles this with a speculative token budget; watch the `spec_decode_draft_acceptance_rate` and `kv_cache_usage_perc` metrics together.

## Continuous Batching: Eliminate the Wait

Traditional static batching works like a bus: you wait until the bus is full or a timer fires, then you dispatch the batch. All requests in the batch start together and the batch isn't done until the longest sequence finishes. A 20-token request and a 2000-token request entered the same batch — the 20-token one finishes in 1 second, but the GPU slot sits idle for another 19 seconds waiting for the long one to complete. New incoming requests queue behind the entire batch. This is head-of-line blocking at the batch level.

Continuous batching (sometimes called iteration-level or in-flight batching) schedules at the granularity of a single decode step, not the whole batch. After every forward pass:

- Requests that just produced their final token (EOS) are evicted immediately.
- New requests waiting in the queue are inserted into the next decode step.
- The batch composition changes every iteration.

This means a 20-token request that finishes in step 20 immediately frees its GPU memory and attention compute budget for a new arrival, without waiting for any co-batched long request. GPU utilization goes from "spiky with long idle tails" to "nearly flat at ~85–95%." Orca (the 2022 paper) demonstrated this yielded 36× throughput improvement over FIFO static batching on production traces.

## PagedAttention: The Memory Management Layer

Continuous batching only works efficiently if memory allocation is flexible. The naive KV cache implementation pre-allocates a contiguous buffer per request sized to the maximum sequence length — a 2048-token max means every request reserves 2048 × 2 × num_layers × d_head × bytes whether it uses them or not. That wastes 50–80% of GPU memory on most workloads and makes dynamic batch changes expensive.

vLLM's PagedAttention borrows the OS virtual memory paging concept: KV cache is divided into fixed-size blocks (16 or 32 tokens each). Requests are allocated blocks on demand, blocks can be physically non-contiguous in GPU memory, and the attention kernel uses a block table to find them. This achieves near-zero KV cache waste and enables:

- **Copy-on-write for beam search**: multiple beam candidates share KV cache blocks until they diverge.
- **Prefix caching**: if two requests share a common system prompt, their prefix KV blocks are computed once and reused — huge win for high-volume chat APIs where every request starts with the same 500-token system prompt.
- **Preemption without waste**: when a long request must be preempted to make room for higher-priority traffic, its KV blocks are swapped to CPU RAM block-by-block and restored later, rather than discarding and recomputing the entire context.

In benchmarks on ShareGPT traces (representative of real chat workloads), vLLM with PagedAttention wastes ~4% of KV cache memory vs. ~60–80% for pre-allocation strategies. That directly translates into fitting more concurrent requests in GPU memory, which is the primary lever on throughput.

## Putting It Together in vLLM

Here is a production-ready vLLM startup command that combines all three techniques:

```bash
python -m vllm.entrypoints.openai.api_server \
  --model meta-llama/Meta-Llama-3-70B-Instruct \
  --tensor-parallel-size 4 \
  --gpu-memory-utilization 0.90 \
  --max-model-len 8192 \
  --speculative-model meta-llama/Meta-Llama-3-8B-Instruct \
  --num-speculative-tokens 5 \
  --speculative-draft-tensor-parallel-size 1 \
  --enable-prefix-caching \
  --block-size 16 \
  --max-num-seqs 256 \
  --disable-log-requests
```

A few non-obvious tuning decisions here:

`--gpu-memory-utilization 0.90` leaves 10% GPU memory headroom. vLLM profiles the model at startup and pre-allocates KV cache blocks to fill the remaining memory. Too high and you'll OOM on unusually long requests; too low and you leave throughput on the table. 0.90 is a safe starting point; adjust based on your p99 sequence length distribution.

`--speculative-draft-tensor-parallel-size 1` keeps the draft model on a single GPU (TP=1) while the target model uses TP=4. Draft models are small enough that tensor parallelism adds communication overhead without compute benefit.

`--max-num-seqs 256` caps concurrent sequences. Beyond a certain point, adding sequences to the batch increases memory pressure and reduces per-request throughput. Profile your specific model and hardware to find the sweet spot — it's rarely as high as the hardware could theoretically fit.

## Measuring What Actually Matters

Benchmarking LLM serving is easy to get wrong. Don't use `time curl` on a single request — that measures TTFT, not throughput. The metrics that matter in production:

**Throughput (tokens/second/GPU)**: measure at sustained load, not peak burst. Use a load generator (Locust, wrk, or vLLM's built-in `benchmark_serving.py`) sending requests at 80% of theoretical max concurrency. Under-saturated benchmarks will always look better than real production.

**TPOT (time per output token)**: this is the TBT (time between tokens) from the user's perspective. For interactive chat, users perceive 50ms TPOT as smooth streaming; above 200ms it feels laggy. For batch/async workloads, TPOT can be higher.

**KV cache hit rate** (for prefix caching): `vllm_cache_ops_total{type="prefix_cache_hit"}` divided by total cache ops. Below 30% means your prompt structure is too varied for prefix caching to help; above 70% is excellent.

**Speculative decode acceptance rate**: `spec_decode_draft_acceptance_rate`. Below 70% on your workload means the draft model is too misaligned and you should either retrain it or disable speculation entirely.

## What Not to Reach For First

Tensor parallelism beyond the minimum required to fit the model adds all-reduce latency to every forward pass. A 70B model on 4× A100 80GB runs fine. Moving to 8× to "go faster" often reduces per-GPU throughput because attention all-reduce dominates. Prefer pipeline parallelism across nodes for scaling beyond 4 GPUs on a single model, or deploy multiple independent replicas behind a load balancer.

Quantization (AWQ, GPTQ, fp8) is complementary, not a replacement. INT4/fp8 models roughly double the number of tokens that fit in the KV cache, which directly improves batching efficiency. But quantization without fixing your batching strategy first is optimization theater — you'll quantize from a 10% GPU utilization baseline to a 15% GPU utilization baseline and wonder why throughput only went up 1.5×.

Fix continuous batching and KV cache memory management first. Add speculative decoding second if your workload has high draft accept rates. Quantize third to pack more concurrency. Profile between each step.
