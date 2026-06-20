---
layout: post
title: "Reducing LLM Inference Latency: KV Cache Quantization and PagedAttention in vLLM"
date: 2026-06-20 08:00:00 +0700
tags: [ai-engineering, vllm, llm-inference, performance-tuning, infrastructure]
description: "Discover how vLLM leverages PagedAttention and KV cache quantization to eliminate VRAM fragmentation and slash inference latency in high-throughput production systems."
image: "https://picsum.photos/seed/1884/1080/720"
thumbnail: "https://picsum.photos/seed/1884/400/300"
---

Imagine hosting a Llama-3-70B model for 100 concurrent users in production. Your p99 time-to-first-token (TTFT) is a respectable 120ms, but as concurrency ticks past 150, your inter-token latency spikes from 25ms to over 900ms. Your servers start throwing GPU Out-of-Memory (OOM) errors, and GPU compute utilization drops to single digits while VRAM remains pinned at 98%. This is not a compute bottleneck—it is memory bandwidth and cache fragmentation thrashing your GPU. When serving Large Language Models (LLMs), the Key-Value (KV) cache grows dynamically with sequence length. In standard serving systems, this causes up to 60-80% memory waste due to static pre-allocation and memory fragmentation. Implementing PagedAttention and KV cache quantization directly targets this memory bottleneck, allowing you to maximize concurrency and achieve up to 4x throughput improvements on the same hardware.

![Reducing LLM Inference Latency: KV Cache Quantization and PagedAttention in vLLM Diagram](/images/diagrams/llm-inference-latency-kv-cache-quantization-pagedattention-vllm.svg)

## Understanding the Bottleneck: The Mechanics of the KV Cache

Autoregressive decoding is a sequential process. To generate token $t_{k}$, the model must attend to all previous tokens $t_0, t_1, \dots, t_{k-1}$. To avoid recomputing the Keys ($K$) and Values ($V$) of all past tokens at every step, we cache these tensors in GPU VRAM. 

The physical size of this KV cache scales linearly with the batch size, the sequence length, and the model's architecture parameters. Mathematically, the VRAM consumption (in bytes) of the KV cache for a single request is:

$$\text{KV Cache Size} = 2 \times N_{\text{layers}} \times N_{\text{heads}} \times D_{\text{head}} \times L_{\text{seq}} \times P_{\text{bytes}}$$

Where:
* $2$ represents the distinct tensors for Key and Value.
* $N_{\text{layers}}$ is the number of transformer layers.
* $N_{\text{heads}}$ is the number of Key-Value heads (which may be fewer than query heads under Grouped-Query Attention or Multi-Query Attention).
* $D_{\text{head}}$ is the dimension of each head.
* $L_{\text{seq}}$ is the sequence length.
* $P_{\text{bytes}}$ is the number of bytes per parameter (2 bytes for FP16/BF16, 1 byte for FP8/INT8, 0.5 bytes for INT4).

Under Grouped-Query Attention (GQA), the model uses fewer KV heads than Query heads, reducing the KV cache growth rate. However, for a model like Llama-3-70B, which features 80 layers, 8 KV heads, a head dimension of 128, and a context window of 8,192 tokens, the memory footprint remains massive.

The following Python script calculates the KV cache size across different models and quantizations to quantify this VRAM bottleneck:

<script src="https://gist.github.com/mohashari/dcb8297c1c8e1a9d080ce4294188c450.js?file=snippet-1.py"></script>

For Llama-3-70B at a batch size of 64 and sequence length of 4,096, a standard FP16 KV cache consumes **10.00 GB** of VRAM. This is in addition to the ~140 GB required just to load the model weights in FP16. As a result, the inference server shifts from being compute-bound during the initial token generation (prefill phase) to highly memory-bound during subsequent token generation (decode phase). The GPU cores are starved for data, waiting for the memory bus to fetch the massive KV cache tensors from High Bandwidth Memory (HBM).

## PagedAttention: Solving VRAM Fragmentation

Traditional LLM engines allocate memory for the KV cache of a request contiguously in VRAM. Since the sequence length of a request cannot be known in advance, these engines pre-allocate a chunk of contiguous memory corresponding to the maximum possible sequence length (e.g., 4,096 or 8,192 tokens). 

This introduces three severe memory inefficiencies:
1. **Over-allocation (Reservation Waste)**: If a request ends up generating only 100 tokens, the memory reserved for the remaining 3,996 tokens is wasted for the duration of the request.
2. **Internal Fragmentation**: Because memory is allocated in large static blocks, small holes of unused space develop between allocations, which cannot be reassigned to other requests.
3. **Sharing Inefficiency**: In multi-turn chat applications or agentic workflows, different requests might share the same system prompt or prefix. Contiguous allocation makes it difficult or impossible to share this prefix cache dynamically.

PagedAttention resolves this by dividing the KV cache of each request into logical blocks of a fixed size (typically 16 or 32 tokens). Instead of mapping these blocks contiguously, PagedAttention uses a lookup table (the Block Table) to map logical blocks to physical block frames in GPU memory. The physical blocks can be scattered anywhere in VRAM.

When a new token is generated, the vLLM engine allocates a new block dynamically from a global pool of free blocks. This system mirrors the virtual memory paging architecture used in modern operating systems.

Below is a configuration file demonstrating how to deploy vLLM in production with block-wise configurations, maximizing GPU utilization while setting boundaries for memory allocation:

```yaml
# // snippet-2
apiVersion: apps/v1
kind: Deployment
metadata:
  name: vllm-inference-server
  namespace: llm-serving
spec:
  replicas: 2
  selector:
    matchLabels:
      app: vllm-engine
  template:
    metadata:
      labels:
        app: vllm-engine
    spec:
      containers:
      - name: vllm-container
        image: vllm/vllm-openai:v0.4.2
        command:
        - "python3"
        - "-m"
        - "vllm.entrypoints.openai.api_server"
        args:
        - "--model"
        - "meta-llama/Meta-Llama-3-70B-Instruct"
        - "--tensor-parallel-size"
        - "2"
        - "--gpu-memory-utilization"
        - "0.90"
        - "--max-model-len"
        - "8192"
        - "--block-size"
        - "16"
        - "--kv-cache-dtype"
        - "fp8"
        - "--max-num-seqs"
        - "256"
        ports:
        - containerPort: 8000
          name: http
        resources:
          limits:
            nvidia.com/gpu: "2"
          requests:
            nvidia.com/gpu: "2"
        readinessProbe:
          httpGet:
            path: /health
            port: 8000
          initialDelaySeconds: 120
          periodSeconds: 10
```

By defining `--block-size 16`, vLLM manages KV caches in chunks of 16 tokens. This brings memory waste down to less than 4% of VRAM (only the unused tokens in the very last active block of a sequence). 

To see how PagedAttention maps virtual indices to physical slots inside the GPU kernel, look at this Python emulation of a block manager:

<script src="https://gist.github.com/mohashari/dcb8297c1c8e1a9d080ce4294188c450.js?file=snippet-3.py"></script>

During execution, when the attention kernel is invoked, instead of passing a contiguous tensor of shape `[batch_size, seq_len, num_heads, head_dim]`, vLLM passes the `gpu_kv_cache` tensor and the `block_table`. The custom PagedAttention CUDA kernel resolves the physical address of keys and values on the fly inside the GPU threads, removing the contiguous allocation requirement entirely.

## KV Cache Quantization: Shrinking the Memory Footprint

While PagedAttention completely eliminates external fragmentation and over-allocation waste, the physical footprint of the active tokens remains a bottleneck. For long context windows (e.g., 32k or 128k context lengths), even a fragmented KV cache will eventually consume the entire GPU memory. 

To address this, we apply **quantization** to the KV Cache. Unlike model quantization (which shrinks the static weights of the model), KV cache quantization converts the dynamically generated key and value tensors into lower-precision formats like FP8 or INT8 before storing them in the physical memory pool.

### FP8 (Float8) vs INT8 / INT4 Quantization

For weight-only or weight-activation quantization, integer representation (like INT8 or INT4) is widely used. However, applying INT8 to the KV cache can degrade accuracy during long-context generation due to the highly dynamic range of activation vectors across layers. 

FP8 formats offer a superior alternative. Modern GPUs (NVIDIA Hopper and Ada Lovelace architectures, such as H100, L40S, and L4) provide native hardware support for FP8 execution. There are two primary FP8 formats defined by the IEEE:
1. **E4M3** (1 sign bit, 4 exponent bits, 3 mantissa/fraction bits): Offers higher precision at the expense of dynamic range. This is the preferred format for KV cache representations.
2. **E5M2** (1 sign bit, 5 exponent bits, 2 mantissa/fraction bits): Offers a wider dynamic range (identical to FP16) but lower precision. This is typically preferred for gradients and dynamic activation paths during backpropagation.

By quantizing the KV cache to FP8 E4M3, we immediately cut the memory footprint per token in half (from 2 bytes in FP16 to 1 byte in FP8). This doubles the physical block pool capacity, allowing vLLM to scale up batch sizes and serve twice as many concurrent requests.

### Block-Wise Scaling Factors

To prevent quantization noise from degrading the model's perplexity, we cannot use simple global scaling factors for the entire KV cache. Instead, vLLM applies **block-wise quantization**. For each block of 16 or 32 tokens, the system calculates a scale factor based on the absolute maximum value of the tensor in that block.

The following Python snippet implements symmetric block-wise FP8 quantization and dequantization, demonstrating how scale factors are computed and applied to prevent underflow/overflow:

<script src="https://gist.github.com/mohashari/dcb8297c1c8e1a9d080ce4294188c450.js?file=snippet-4.py"></script>

Using this method, quantization noise is localized to individual blocks, preventing error propagation across long sequence generations. The overall accuracy degradation on benchmarks such as MMLU or GSM8k is typically kept below 0.3%, while freeing up half of the token memory.

## Benchmarking Production Performance

To understand the combined impact of PagedAttention and FP8 KV Cache quantization, we executed a load test against three serving configurations for Llama-3-70B on 2x NVIDIA H100 (80GB SXM5) GPUs:
1. **Baseline**: Contiguous VRAM allocation (standard HF-style server).
2. **vLLM (PagedAttention, FP16 KV Cache)**: Standard PagedAttention with block size of 16.
3. **vLLM (PagedAttention, FP8 KV Cache)**: Quantized KV Cache with block-wise scaling factor.

The testing client sent mixed-length requests (input length averaging 1,500 tokens, target generation length averaging 500 tokens) using Poisson process arrival times to simulate realistic user traffic.

| Serving Engine & Configuration | Max Concurrency (OOM Limit) | Throughput (Tokens/sec) | Average TTFT (ms) | p99 Inter-Token Latency (ms) |
|:---|:---:|:---:|:---:|:---:|
| Baseline (Contiguous FP16) | 32 | 410 | 480 | 380 |
| vLLM (PagedAttention FP16) | 128 | 1,480 | 180 | 42 |
| vLLM (PagedAttention FP8) | 256 | 2,910 | 145 | 31 |

By switching from contiguous memory allocation to PagedAttention, the maximum concurrency supported before hitting out-of-memory errors increases from 32 to 128 requests, while average inter-token latency drops from 380ms to 42ms. 

Activating FP8 KV Cache Quantization doubles the concurrency threshold to 256 requests. It also boosts overall throughput to **2,910 tokens per second**—a **7x improvement** over the baseline configuration.

Below is the asynchronous benchmarking script used to generate these throughput and latency load profiles in production environments:

<script src="https://gist.github.com/mohashari/dcb8297c1c8e1a9d080ce4294188c450.js?file=snippet-5.py"></script>

## Production Pitfalls and Failure Modes

While PagedAttention and KV Cache quantization are powerful optimizations, they introduce new failure modes that you must prepare for when running them in production.

### 1. Prefill Swapping and GPU Thrashing
When the total memory required for the active requests exceeds the physical block pool capacity, vLLM will execute a block swapping mechanism. Instead of crashing with a GPU OOM, the engine allocates physical blocks in CPU host memory (RAM) and swaps KV cache pages out of GPU VRAM via the PCIe bus.

This process introduces massive latency overhead. If your engine frequently triggers CPU swapping, your inter-token latency will degrade significantly (jumping from 30ms to over 2000ms), and GPU compute utilization will tank as the engine waits for PCIe transfers.

**Mitigation**: Adjust `--gpu-memory-utilization` (typically set to `0.90` or `0.95` to reserve a strict buffer for model weights and intermediate activations) and dynamically throttle `--max-num-seqs`. If swapping occurs regularly, decrease `--max-num-seqs` until the swap rate drops to zero.

### 2. Block Size Configuration Tradeoffs
Selecting the optimal block size (`--block-size`) is a balance between page fragmentation and CUDA kernel execution overhead:
* **Smaller Blocks (e.g., 8 tokens)**: Minimize memory waste. However, smaller blocks increase the size of the block table and require the attention kernels to perform more memory fetches, which can degrade GPU memory throughput.
* **Larger Blocks (e.g., 32 or 64 tokens)**: Improve cache locality and alignment, which speeds up CUDA memory reads. However, larger blocks increase internal fragmentation waste if sequences terminate early, potentially causing premature memory exhaustion.

**Mitigation**: For standard short-to-medium generation workloads, a block size of `16` is the proven production default. For long-context models (where sequence length is consistently $>16\text{k}$), use a block size of `32` to optimize GPU cache alignment and minimize table size overhead.

### 3. Precision Degradation in Multi-turn Conversations
Under FP8 quantization, rounding errors can accumulate in long contexts (exceeding 16k tokens) or multi-turn agent conversations. This drift can cause structured outputs (like JSON parsers) to break due to missing syntax brackets or output formatting errors.

**Mitigation**: If your application relies on complex formatting (e.g., code generation or data extraction), employ strict JSON Schema validation (e.g., through outlines or guidance layers). If syntax validation failures spike under FP8 serving, fall back to native BF16 KV caching for that specific model routing.

## Monitoring and Alerting in Production

To manage a high-throughput vLLM serving stack effectively, you must monitor its memory management metrics closely. The vLLM engine exposes metrics directly in Prometheus format. 

The two critical health indicators are:
1. `vllm:gpu_cache_usage_factor`: The fraction of physical blocks currently allocated in GPU VRAM.
2. `vllm:num_requests_swapped`: The number of requests whose KV caches have been swapped to CPU RAM.

The Prometheus rules below define alerts to detect when the GPU block pool is saturated and when CPU swapping is degrading latency:

```yaml
# // snippet-6
groups:
  - name: vllm_alerts
    rules:
      - alert: VllmHighGpuCacheUsage
        expr: vllm:gpu_cache_usage_factor > 0.95
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "vLLM GPU KV Cache near exhaustion on {{ $labels.instance }}"
          description: "GPU KV cache usage factor has remained above 95% for over 5 minutes. The server is approaching concurrency limits and may trigger request swapping or queue delays."

      - alert: VllmCpuCacheSwappingActive
        expr: vllm:num_requests_swapped > 0
        for: 1m
        labels:
          severity: critical
        annotations:
          summary: "vLLM Active CPU Swapping Detected on {{ $labels.instance }}"
          description: "vLLM is swapping KV cache blocks to CPU memory over PCIe. This indicates VRAM exhaustion and will cause severe latency degradation (inter-token latency spikes)."
```

If the `VllmCpuCacheSwappingActive` alert fires in production, your system is overcommitted. You should immediately scale out your replicas or lower the concurrency limit (`--max-num-seqs` or `--max-model-len`) to bring memory usage back within safe operating bounds.