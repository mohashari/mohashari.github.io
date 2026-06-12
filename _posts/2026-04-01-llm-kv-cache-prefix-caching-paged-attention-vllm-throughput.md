---
layout: post
title: "LLM KV Cache Internals: Prefix Caching, PagedAttention, and Throughput Optimization in vLLM"
date: 2026-04-01 08:00:00 +0700
tags: [ai-engineering, llm, vllm, inference, gpu]
description: "How vLLM's KV cache management — prefix caching and PagedAttention — works under the hood, and why it matters for production LLM serving throughput."
image: ""
thumbnail: ""
---

You deploy a 70B LLaMA model on an A100 80GB. You give every request the same 512-token system prompt describing your assistant's persona and a few-shot example. Each new request recomputes the key-value projections for that prompt from scratch — 512 tokens × 80 transformer layers × 128 KV head dimensions × float16 = ~10MB of KV state per request, before the user even types a word. At 50 concurrent requests you've burned 500MB on redundant computation you've already done thousands of times before. This is the problem prefix caching solves. Combined with PagedAttention's non-contiguous memory management, vLLM can serve 3–4x more requests per second on the same hardware with zero model changes.

![LLM KV Cache Internals: Prefix Caching, PagedAttention, and Throughput Optimization in vLLM Diagram](/images/diagrams/llm-kv-cache-prefix-caching-paged-attention-vllm-throughput.svg)

## What the KV Cache Actually Is

During the prefill phase of an autoregressive transformer, each token attends to all previous tokens via the attention mechanism: `Attention(Q, K, V) = softmax(QK^T / sqrt(d_k)) * V`. The key matrix K and value matrix V for every previously processed token get cached so the decode phase doesn't recompute them — only the new token's Q, K, V projections need to be computed on each step.

The memory cost is linear in sequence length. For LLaMA-3-70B (fp16, GQA with 8 KV heads, d_head=128):

```
KV per token = 2 layers × n_kv_heads × d_head × bytes_per_element × n_layers
             = 2 × 8 × 128 × 2 × 80
             = 327,680 bytes ≈ 320KB per token
```

A 4K context window sequence consumes ~1.25GB of KV cache. Multiply by 64 concurrent sequences and you've consumed the entire GPU. This is why KV cache capacity — not parameter count — is the primary bottleneck for LLM serving throughput.

## PagedAttention: Treating GPU Memory Like Virtual Memory

The naive KV cache allocates a contiguous memory block per sequence upfront, sized for the maximum sequence length. This creates two problems: fragmentation (blocks reserved but partially unused) and the inability to share memory across sequences.

PagedAttention, the core innovation in vLLM's architecture, borrows the virtual memory paging abstraction from OS design. Physical GPU memory is divided into fixed-size **blocks** (typically 16 tokens per block). Each sequence maintains a **block table** — an indirection layer mapping virtual block indices to physical block indices, exactly like a page table.

```
Sequence A block table:   [virt 0 → phys 7]  [virt 1 → phys 12]  [virt 2 → phys 3]
Sequence B block table:   [virt 0 → phys 7]  [virt 1 → phys 12]  [virt 2 → phys 9]
                                          ^^^             ^^^^
                                     same physical blocks (shared prefix)
```

The attention kernel is modified to follow these indirections. The paged attention CUDA kernel takes the block table as input and gathers KV data from non-contiguous physical addresses. This is the mechanism that makes sharing possible.

The benefits cascade: physical blocks can be allocated lazily as the sequence grows (no upfront reservation), freed immediately when a sequence finishes (no fragmentation), and shared across multiple sequences when their KV state is identical.

## Prefix Caching: Hashing Your Way to Free Compute

Given PagedAttention's block table abstraction, prefix caching becomes a natural extension. The key insight: if two sequences share the same prefix tokens, their KV blocks for those tokens are bit-for-bit identical (deterministic computation with the same weights). Those blocks can be shared in physical memory.

vLLM implements this with a **block hash → physical block** cache. When a new sequence arrives, vLLM computes a rolling hash of the token IDs in each block-sized window. If the hash matches an existing cached block, the new sequence's block table simply points to the same physical block — no KV computation required for that prefix.

The hash is content-addressed: `block_hash = hash(parent_block_hash, token_ids_in_block)`. This chains blocks together so `hash(block_N)` depends on all preceding tokens, preventing hash collisions across different prefixes that happen to share a suffix.

Reference counting tracks how many sequences point to each physical block. A block with `ref_count > 0` is considered **shared** and is never evicted. When `ref_count` drops to zero, the block becomes an eviction candidate.

**Copy-on-Write semantics** handle the case where a sequence needs to mutate a shared block (relevant for beam search or when the same prefix spawns multiple continuations with different sampling). Rather than modifying the shared physical block in-place — which would corrupt all other sequences referencing it — vLLM allocates a new physical block, copies the data, updates only the requesting sequence's block table, and decrements the original block's ref count.

## Cache Management and Eviction

vLLM maintains a **free block pool** — a list of physical blocks available for allocation. When a new prefix block is needed and no free blocks exist, the eviction policy kicks in.

The eviction strategy is LRU (Least Recently Used) applied only to blocks with `ref_count == 0`. Blocks currently referenced by active sequences are never evicted. This creates a natural priority: active sequences are protected, and cached prefixes are retained as long as there's memory to spare.

In production systems with diverse workloads, cache hit rates depend heavily on prefix diversity. A customer service chatbot with a single 200-token system prompt across all users can achieve 90%+ prefix cache hit rates. A code completion service where every request has a unique file context might see near-zero hits. vLLM exposes `enable_prefix_caching=True` as a server flag — measure your actual hit rate with the `--enable-prefix-caching` flag and check the `prefix_cache_hit_rate` metric in the Prometheus endpoint before assuming benefit.

## The Block Size Tradeoff

Block size (tokens per block) is a hyperparameter with genuine engineering tradeoffs. Default in vLLM is 16 tokens.

**Smaller blocks** (8–16 tokens): finer-grained sharing, less wasted memory at sequence tail (the last block is rarely full — average waste is `block_size / 2` tokens per sequence), but higher metadata overhead per sequence and more block table entries to manage.

**Larger blocks** (32–64 tokens): better GPU memory bandwidth utilization during the attention kernel gather (fewer CUDA kernel launches, better memory coalescing), but higher average tail waste. For use cases with highly variable sequence lengths, large blocks amplify fragmentation.

At 16 tokens with 2-byte elements for a 70B model, a single block is `16 × 320KB/token = 5.12MB`. On an 80GB A100 with the model weights consuming ~35GB (fp16), you have roughly 45GB available for KV cache — about 8,700 blocks. At typical serving batch sizes of 128–256 sequences with 1–2K average context, this is sufficient for good utilization.

## Chunked Prefill: Interleaving Prefill and Decode

A separate but related optimization worth understanding: **chunked prefill**. The standard vLLM execution model processes the entire prompt in a single prefill step (compute-bound, high throughput) then switches to per-token decode steps (memory-bandwidth-bound, lower GPU utilization).

With chunked prefill (`--enable-chunked-prefill`), long prompts are broken into fixed-size chunks processed across multiple scheduler iterations. Each iteration can interleave a chunk of prefill with decode tokens from other sequences. This keeps GPU utilization high across both phases and reduces time-to-first-token (TTFT) latency for decode-phase requests that were previously blocked behind a long prefill.

The interaction with prefix caching is important: only fully-processed prefix blocks (not partial chunks) are eligible for caching. A prefix split across two chunks becomes cacheable only after the second chunk completes.

## Measuring What Matters in Production

The metrics that actually reflect KV cache health:

**`vllm:gpu_cache_usage_perc`** — percentage of KV cache blocks currently in use. Sustained >90% means you're close to OOM under burst load. Target 70–80% at steady state.

**`vllm:prefix_cache_hit_rate`** — fraction of prefill tokens served from cache. If this is below 30% on a workload you expected to benefit from prefix caching, debug whether your clients are consistently sending the same prefix tokens (watch out for timestamp injection or session IDs in system prompts).

**`vllm:time_to_first_token_seconds`** — prefix cache hits dramatically reduce TTFT since the prefill computation is skipped. A bimodal distribution (fast for cache hits, slow for misses) is expected and healthy.

**Request queue depth** — if requests are queuing before even starting decode, you're compute-bound, not memory-bound. Adding more KV cache won't help; you need more GPU compute or smaller models.

A practical debugging pattern: if you're seeing OOM kills or high swap rates on the KV cache, first check `gpu_cache_usage_perc`. If it's pinned high with low prefix hit rates and long average context lengths, you have a genuine capacity problem — reduce `max_model_len` or `--max-num-seqs`. If hit rates are high but you're still seeing pressure, check for beam search usage (which multiplies KV memory by beam width) or tool-calling patterns that expand context aggressively.

## Concurrency and Continuous Batching

PagedAttention's memory efficiency is what makes **continuous batching** practical at scale. In the traditional static batching model, the GPU waits for an entire batch to finish before starting new requests. With continuous batching (the default in vLLM), new requests are admitted mid-flight as soon as a slot opens — possible because PagedAttention guarantees that blocks from finished sequences are immediately reclaimable without fragmentation.

At 50 QPS on a single A100 80GB serving LLaMA-3-70B, the difference between vLLM with prefix caching vs. a naive server is not incremental. In benchmarks with realistic chatbot workloads (512-token system prompt, 500-token user turns, 200-token outputs), enabling prefix caching increases sustainable throughput from ~12 req/s to ~47 req/s on identical hardware. The gain is not from a smarter model or faster kernels — it's from avoiding redundant memory-bandwidth-bound operations on shared context.

Understanding these internals matters when you're debugging latency SLO violations at 2 AM. The KV cache is not an opaque optimization — it's a memory management system with deterministic behavior you can reason about, monitor, and tune.
