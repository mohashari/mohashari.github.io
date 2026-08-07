---
layout: post
title: "Optimizing KV-Cache Reuse via Prefix Caching in vLLM for Multi-Turn Conversations"
date: 2026-08-07 08:00:00 +0700
tags: [vllm, kv-cache, prefix-caching, llmops, performance]
description: "A deep dive into configuring and optimizing vLLM's Automatic Prefix Caching (APC) to slash TTFT and GPU memory usage in multi-turn LLM conversations."
image: "https://picsum.photos/seed/6384/1080/720"
thumbnail: "https://picsum.photos/seed/6384/400/300"
---

In production multi-turn conversational agents, the Time to First Token (TTFT) is the most critical user experience bottleneck. As chat histories grow to 4,000 or 8,000 tokens, sending the entire transcript back to the model on every new turn causes inference engines to perform $O(N)$ key-value tensor computations, where $N$ is the length of the history. Under heavy concurrent load—say, 100 requests per second—this computational tax spikes TTFT from sub-100 milliseconds to several seconds, saturates Tensor Core utilization, and crashes services due to GPU Out-of-Memory (OOM) errors. Solving this requires reusing the computed states of past turns. This article deep-dives into configuring, integrating, and debugging vLLM’s Automatic Prefix Caching (APC) to achieve near-instantaneous TTFT and maximize hardware efficiency in production LLM pipelines.

## Deconstructing the KV-Cache and the Power of Prefix Sharing

To generate token $t_i$, a transformer decoder needs the Key ($K$) and Value ($V$) projection matrices of all preceding tokens $t_1 \dots t_{i-1}$ to calculate the self-attention weights. In naive inference engines, the KV tensors of past tokens are computed once and stored in memory. However, in multi-turn conversations, the server processes each turn as an independent request. Without optimization, the engine must re-evaluate the system prompt and historical turns from scratch to construct the initial KV state for the new turn.

vLLM revolutionized memory management with PagedAttention, which partitions the KV-cache into fixed-size physical blocks (usually 16 tokens) mapped to virtual pages. Automatic Prefix Caching (APC) extends this concept by tracking these blocks using a Radix Tree. The keys of the tree are lists of token IDs, and the values are pointers to the physical GPU blocks where their KV tensors reside. When a new request arrives, vLLM runs a lookup on the Radix Tree. If a prefix of the request's token sequence matches an existing branch, the engine skips computation for those blocks and references the pre-calculated KV weights directly. This transforms the pre-fill phase of matched tokens from an expensive $O(N)$ GPU compute operation into an $O(1)$ block lookup.

The radix tree behaves like a cache directory. Nodes represent token blocks. If a node is currently referenced by an active sequence (running phase), its reference count is incremented. When the request finishes, the nodes are not deleted; they are kept in memory and marked as candidates for eviction. When physical GPU memory is depleted, vLLM uses a Least Recently Used (LRU) policy to evict the oldest unreferenced nodes, freeing up blocks for new sequences.

## Activating and Configuring Automatic Prefix Caching in vLLM

To leverage prefix caching in vLLM, you must enable it at engine startup. By default, APC is disabled because it incurs minor lookup overhead and memory allocation tracking. To enable it, use the `--enable-prefix-caching` flag.

However, enabling the flag is only the first step. You must configure the engine to allocate enough GPU memory to prevent the cache from thrashing. Two parameters control this behavior:

1. `--gpu-memory-utilization`: This defines the fraction of GPU memory allocated to the vLLM engine (including model weights, KV-cache, and workspace). For APC, set this as high as possible (e.g., `0.92` to `0.95`) to maximize the room for cached prefix blocks.
2. `--block-size`: The block size determines the granularity of caching. While smaller block sizes (e.g., 8 tokens) lead to higher cache hit rates on uneven prompt lengths, they increase radix tree traversal overhead. A block size of `16` is the industry standard sweet spot.
3. `--swap-space`: Specifies the CPU memory swap space (in GiB) to offload evicted blocks. If GPU memory is full, vLLM can swap cached blocks to CPU RAM, avoiding complete cache destruction.

Below is a production-grade shell script to spin up vLLM using Docker, configured for a Meta-Llama-3-70B model with APC optimized for multi-turn sessions.

<script src="https://gist.github.com/mohashari/fe441b43141f48a34b84f00b927ff93c.js?file=snippet-1.sh"></script>

## System Architecture: How Radix Trees Track Cache States

Inside the vLLM scheduler, the Radix Tree keeps track of which block IDs map to which token sequences. When a sequence is processed, vLLM matches the sequence's tokens against the Radix Tree from the root down.

To understand the underlying mechanism, consider this Python-based representation of the block allocation and matching process. This simulation mirrors the logic vLLM uses to find cache hits, assign memory blocks, and track last-access times for LRU eviction.

<script src="https://gist.github.com/mohashari/fe441b43141f48a34b84f00b927ff93c.js?file=snippet-2.py"></script>

## The API Integration Layer: Structuring Prompts for Maximum Hits

A common production failure mode is enabling `--enable-prefix-caching` and seeing a 0% cache hit rate. This is almost always caused by improper prompt layout design. The radix cache matches exact token sequences. If even a single token in the prefix changes, the entire cache tree below that token is invalidated and must be recomputed.

To design an API layer that maximizes cache hits, you must enforce the following structural rules:

1. **Absolute Prefix Integrity**: Keep the system prompt, static templates, and few-shot examples at the very beginning of the prompt. Never insert dynamic metadata—like user IDs, current timestamps, or session variables—into the system prompt or early in the conversation history.
2. **Deterministic Formatting**: Ensure that chat templates do not include arbitrary whitespaces or formatting changes. If a user asks a question, the past turns must be formatted exactly the same way they were formatted in previous turns.
3. **Volatile Metadata Appending**: If you must supply dynamic context (such as the current date or weather information), append it at the absolute end of the request payload, specifically within the final user query. This ensures that the entire history prefix remains untouched, caching all but the last few tokens.

Below is an implementation of a FastAPI middleware service that sits in front of vLLM. It strips session-specific parameters from historical messages and structures the chat completion payload to guarantee that the system prompt and historical turns remain cache-compatible.

<script src="https://gist.github.com/mohashari/fe441b43141f48a34b84f00b927ff93c.js?file=snippet-3.py"></script>

## Benchmarking & Monitoring Cache Efficiency

To justify prefix caching in production, you must monitor its actual performance. vLLM exports a robust suite of metrics to a Prometheus endpoint (typically `/metrics`). The core metric for prefix caching is `vllm:gpu_prefix_cache_hit_rate`. A healthy system prompt caching implementation should achieve a hit rate above 0.70 (70%) for multi-turn workflows. If this metric hovers near zero, your formatting rules are being violated or your GPU memory configuration is too small, triggering constant block evictions.

The following Python script scrapes the Prometheus endpoint of a running vLLM instance to monitor prefix cache hits, active sequences, and physical block allocation.

<script src="https://gist.github.com/mohashari/fe441b43141f48a34b84f00b927ff93c.js?file=snippet-4.py"></script>

To quantify the savings, we need to compare a cache miss against a cache hit using a synthetic workload. Below is a load test script using `asyncio` and `httpx` that constructs a 6,000-token system prompt. It makes a request to vLLM to warm the cache (representing a cache miss) and immediately follows it with a request sharing the identical prompt context (representing a cache hit), recording the TTFT for both.

<script src="https://gist.github.com/mohashari/fe441b43141f48a34b84f00b927ff93c.js?file=snippet-5.py"></script>

## Production Failure Modes & Mitigation Strategies

While prefix caching can optimize resources, deploying it in highly concurrent environments exposes unique failure modes that can degrade performance below naive baselines.

### 1. LRU Cache Thrashing Under Over-Subscription

If the working set size of all concurrent conversations exceeds the physical GPU memory allocated for the KV-cache, vLLM is forced to evict blocks using the LRU algorithm. If requests are interleaved, session A will evict session B's blocks, and session B will immediately recompute and evict session A's blocks. Under this state, the engine spends more time managing metadata, resolving page mappings, and swapping to CPU memory than it would running standard batch inference.

**Mitigation**: Set the `--max-num-seqs` parameter lower to cap concurrency, and match it with a horizontal scaling strategy (e.g. Kubernetes ReplicaSets) using a load balancer that routes requests with the same `session_id` to the same pod (session affinity).

### 2. The "Long-Tail" Cache Pollution

If your service supports both multi-turn chat and short, highly varied single-turn queries (e.g., ad-hoc database searches), the short queries will fill up the radix tree with single-use leaf nodes. This pollutes the cache, forcing vLLM to traverse and maintain large trees with zero hit rates.

**Mitigation**: Segregate your workloads. Deploy two distinct model-serving instances: one with prefix caching enabled for chat sessions, and another with prefix caching disabled for single-turn APIs.

### 3. Kubernetes Deployment Configuration

To ensure maximum availability and prevent memory fragmentation in a Kubernetes cluster, you must specify exact limits and configure the readiness probes to match the startup characteristics of large model weights. Below is a production Kubernetes deployment manifest demonstrating how to wrap vLLM with resource constraints and prefix caching enabled.

```yaml
# // snippet-6
apiVersion: apps/v1
kind: Deployment
metadata:
  name: vllm-llama3-apc
  namespace: ai-platform
  labels:
    app: vllm-llama3-apc
spec:
  replicas: 3
  strategy:
    type: RollingUpdate
    rollingUpdate:
      maxSurge: 1
      maxUnavailable: 0
  selector:
    matchLabels:
      app: vllm-llama3-apc
  template:
    metadata:
      labels:
        app: vllm-llama3-apc
    spec:
      containers:
      - name: inference-engine
        image: vllm/vllm-openai:v0.5.4
        args:
        - "--model"
        - "meta-llama/Meta-Llama-3-70B-Instruct"
        - "--enable-prefix-caching"
        - "--gpu-memory-utilization"
        - "0.95" # Dedicate 95% of GPU memory to model + cache
        - "--max-num-seqs"
        - "128" # Limit concurrent sequences to protect cache stability
        - "--max-model-len"
        - "8192"
        - "--block-size"
        - "16"
        - "--port"
        - "8000"
        ports:
        - containerPort: 8000
          name: http-api
        resources:
          limits:
            nvidia.com/gpu: "1"
            memory: 64Gi
            cpu: "16"
          requests:
            nvidia.com/gpu: "1"
            memory: 32Gi
            cpu: "8"
        readinessProbe:
          httpGet:
            path: /health
            port: 8000
          initialDelaySeconds: 150
          periodSeconds: 10
          timeoutSeconds: 5
        livenessProbe:
          httpGet:
            path: /health
            port: 8000
          initialDelaySeconds: 180
          periodSeconds: 20
```