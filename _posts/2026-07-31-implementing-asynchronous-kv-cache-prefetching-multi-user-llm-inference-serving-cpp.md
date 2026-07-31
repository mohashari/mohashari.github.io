---
layout: post
title: "Implementing Asynchronous KV Cache Prefetching for Multi-User LLM Inference Serving in C++"
date: 2026-07-31 08:00:00 +0700
tags: [ai-engineering, llm-inference, cuda, performance, cplusplus]
description: "Eliminate GPU compute starvation in multi-user LLM serving by implementing a paged KV cache prefetcher with Radix attention, custom CUDA streams, and asynchronous device-side synchronization."
image: "/images/diagrams/implementing-asynchronous-kv-cache-prefetching-multi-user-llm-inference-serving-cpp.svg"
thumbnail: "/images/diagrams/implementing-asynchronous-kv-cache-prefetching-multi-user-llm-inference-serving-cpp.svg"
---

In multi-user LLM inference engines serving long-context, multi-turn conversations, the Key-Value (KV) cache is both a lifesaver and a massive bottleneck. While caching historical token states prevents redundant attention calculations, transferring large, paged KV caches from host CPU memory to GPU High Bandwidth Memory (HBM) during the prefill phase introduces severe latency penalties. For instance, serving Llama-3-70B in FP16 over Grouped Query Attention (GQA) requires approximately 327.68 KB of KV cache per token. A 2048-token prompt prefix demands a 671 MB memory transfer; over a PCIe Gen4 x16 interface operating at an effective 26 GB/s, this transfer stalls the execution engine for ~25.8 milliseconds. When performed synchronously, this memory-copy overhead directly blocks the GPU compute kernels, leading to compute starvation, decreased GPU utilization, and spikes in time-to-first-token (TTFT). To achieve high-throughput multi-user serving, we must overlap host-to-device (H2D) KV cache transfers with active token decoding phases of other active sessions. This guide details how to build an asynchronous KV cache prefetching pipeline in C++ using Paged KV blocks, a Radix tree directory, concurrent CUDA streams, and low-overhead device-side synchronization.

![Implementing Asynchronous KV Cache Prefetching for Multi-User LLM Inference Serving in C++ Diagram](/images/diagrams/implementing-asynchronous-kv-cache-prefetching-multi-user-llm-inference-serving-cpp.svg)

## The High-Concurrency KV Cache Bottleneck

LLM serving is split into two distinct execution phases: the **prefill** phase (which processes prompt tokens and is compute-bound) and the **decoding** phase (which generates tokens autoregressively and is memory-bandwidth bound). In a multi-user environment, users send queries that often share prefix contexts, such as system prompts, system instructions, or previous turns in a chat history. 

By caching the KV tensors of these prefixes, we skip the prefill computation for those tokens. However, because HBM space on modern accelerators (e.g., NVIDIA H100, A100) is highly constrained, we cannot store the KV caches of all inactive users on the device. Instead, we must offload inactive cached pages to CPU host RAM and stream them back to GPU HBM when a user resumes their session.

If the serving engine fetches these pages synchronously when a request is scheduled, the compute stream stalls. The GPU remains idle during the transfer because the CPU thread blocks on the memory-copy operation before launching the prefill kernel. To eliminate this stall, we must run the H2D copies asynchronously on a dedicated CUDA prefetch stream, executing the transfer while the GPU is executing decoding kernels for other active sessions on the main compute stream.

## Paged KV Cache and the Radix Tree Directory

To support variable-length sequences without memory fragmentation, we structure the KV cache into fixed-size blocks (e.g., 16 or 32 tokens) mapping to non-contiguous virtual pages, similar to vLLM's PagedAttention. We maintain separate block allocators for the host (CPU pinned memory) and the device (GPU HBM).

<script src="https://gist.github.com/mohashari/7a3ffc7c3b72d30a50f953f271779b9a.js?file=snippet-1.txt"></script>

## Prefetch Scheduling and Queue Orchestration

To implement asynchronous prefetching, the scheduler must inspect incoming requests while they are in the queue. For each request, the scheduler determines if its prefix is cached in host memory but missing from device memory. It then generates a prefetch job and pushes it to a background worker thread.

<script src="https://gist.github.com/mohashari/7a3ffc7c3b72d30a50f953f271779b9a.js?file=snippet-2.txt"></script>

## Async Memory Copy Pipeline with CUDA Streams

The prefetch worker thread pulls jobs from the scheduler and initiates the transfers. We configure the prefetch stream with a lower priority using `cudaStreamCreateWithPriority`. This guarantees that the background transfers do not starve the main compute kernels of execution hardware or PCIe arbitration priority.

<script src="https://gist.github.com/mohashari/7a3ffc7c3b72d30a50f953f271779b9a.js?file=snippet-3.txt"></script>

## Dual-Stream Synchronization: Computing while Transferring

To overlap computation and communication, the CPU host thread must launch kernels and memory transfers without waiting for completion on the host. Instead, we use device-side events. The prefetch engine records a CUDA event after issuing the memory transfers. The compute stream is then ordered to wait on this event. The GPU hardware manages this dependency internally: the compute stream will pause execution of the prefill kernel until the memory transfer is complete, while other independent kernels (like active decode steps) continue executing.

<script src="https://gist.github.com/mohashari/7a3ffc7c3b72d30a50f953f271779b9a.js?file=snippet-4.txt"></script>

## The Radix Tree Prefix Cache Directory

We manage prefix sharing using a Radix Tree directory. Each node represents a token sequence segment and tracks its corresponding physical blocks on host and device. During request scheduling, we perform a prefix match to resolve block hits and update access timestamps for LRU eviction policies.

<script src="https://gist.github.com/mohashari/7a3ffc7c3b72d30a50f953f271779b9a.js?file=snippet-5.txt"></script>

## End-to-End Orchestrator Integration

The orchestrator puts all elements together. When a request arrives, it checks the Radix cache directory, allocates physical GPU destination blocks, dispatches the async prefetch job, triggers device synchronization, and launches the prefill attention kernels.

<script src="https://gist.github.com/mohashari/7a3ffc7c3b72d30a50f953f271779b9a.js?file=snippet-6.txt"></script>

## Critical Failure Modes and Production Hardening

In production serving pipelines, raw CUDA implementations expose severe bottleneck edge cases. If not handled, your system will suffer from performance regression or crashes.

### 1. Page Faults and Unpinned Memory
If you call `cudaMemcpyAsync` on memory allocated via standard `std::vector` or `malloc`, the CUDA driver cannot perform direct DMA transfers. Under the hood, the driver locks the memory pageable range, copies it to an internal staging pinned buffer on the host, and then transfers it to the device. This process forces the host-side call to run synchronously, blocking the CPU scheduler and destroying all concurrent overlapping pipelines.
*   **Mitigation:** Always allocate host cache memory using `cudaHostAlloc` (as shown in snippet-1) or register existing user-allocated memory blocks using `cudaHostRegister`.

### 2. Stream Priority Inversion
Without setting explicit stream priorities, default CUDA streams operate at the same priority level. When a large prefetch task issues hundreds of concurrent `cudaMemcpyAsync` calls, it can saturate the PCIe bus DMA engines. Consequently, urgent GPU-to-host execution logs or model parameter updates stall, increasing token generation step latency.
*   **Mitigation:** Define clear stream priority ranges. Assign the prefetch stream the lowest priority (`lowest_priority` via `cudaDeviceGetStreamPriorityRange`), and assign the compute stream high priority.

### 3. Cache Thrashing under High Concurrency
If the request queue rate exceeds the available GPU HBM cache capacity, the block allocator will continuously evict blocks that are currently queued for prefetching. This causes cache thrashing: blocks are copied from CPU to GPU, evicted before the compute kernel can execute, and subsequently scheduled for prefetching again, causing PCIe bus saturation and stalling the inference engine.
*   **Mitigation:** Implement strict scheduling watermarks. A request should only trigger a prefetch job if the destination GPU blocks are locked in HBM and guaranteed not to be evicted before the prefill kernel launches.

### 4. CUDA Event Leakage and Host-Side Allocation Overhead
Calling `cudaEventCreate` inside the request-handling path introduces high host-side driver overhead. Under heavy serving workloads (e.g., hundreds of requests per second), the allocation and destruction of events degrade CPU scheduler latency, causing latency spikes.
*   **Mitigation:** Implement a thread-safe CUDA Event Pool to recycle events rather than allocating them dynamically.

<script src="https://gist.github.com/mohashari/7a3ffc7c3b72d30a50f953f271779b9a.js?file=snippet-7.txt"></script>

## Production Tuning Guidelines

To optimize the throughput of your asynchronous prefetching engine:
*   **Block Size Selection:** Set block size to 16 tokens for models with high head counts or large head dimensions (e.g., Llama-3-70B) to minimize internal fragmentation. For smaller models (e.g., Llama-3-8B), use a block size of 32 tokens to reduce the overhead of managing a large number of blocks.
*   **Prefetch Margin:** Begin prefetching blocks when a request transitions to the "next-to-schedule" pool. Do not prefetch too early (e.g., for requests placed deep in the scheduler queue) to avoid pinning memory blocks that might sit idle.
*   **GPUDirect Storage (GDS):** For distributed setups where the CPU host cache acts as an intermediate ring buffer, evaluate GPUDirect Storage to stream KV pages directly from NVMe drives to GPU HBM over the PCIe bus, bypassing host CPU bounce buffers.