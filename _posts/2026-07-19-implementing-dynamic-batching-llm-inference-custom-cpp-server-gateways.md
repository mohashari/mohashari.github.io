---
layout: post
title: "Implementing Dynamic Batching for LLM Inference in Custom C++ Server Gateways"
date: 2026-07-19 08:00:00 +0700
tags: [cpp, llm-inference, system-design, concurrency]
category: ai_engineering
description: "Build a high-performance C++ gateway implementing dynamic and continuous batching to maximize GPU utilization in LLM inference pipelines."
image: "/images/diagrams/implementing-dynamic-batching-llm-inference-custom-cpp-server-gateways.svg"
thumbnail: "/images/diagrams/implementing-dynamic-batching-llm-inference-custom-cpp-server-gateways.svg"
---

In production LLM serving, single-client request latency is dominated by memory bandwidth limits during the autoregressive decoding phase, leaving high-end Tensor Core GPUs (like NVIDIA H100s or A100s) operating at under 10% compute utilization. If you attempt to solve this by wrapping your model in a standard web framework like FastAPI or Express, the GIL or event-loop overhead will throttle ingestion, while naive FIFO queuing will cause tail latency ($p99$) to balloon to tens of seconds under moderate concurrency. To bridge the gap between high-throughput client traffic (hundreds of concurrent gRPC streams) and the strict microsecond scheduling requirements of GPU execution engines (such as TensorRT-LLM or custom vLLM setups), you must implement a custom C++ gateway. This gateway coordinates request ingestion, token tracking, backpressure propagation, and dynamic batching—allowing you to pack individual client requests into a single tensor operations matrix on the fly, transforming a memory-bound bottleneck into a compute-bound throughput engine without breaching your SLAs.

![Implementing Dynamic Batching for LLM Inference in Custom C++ Server Gateways Diagram](/images/diagrams/implementing-dynamic-batching-llm-inference-custom-cpp-server-gateways.svg)

## The Latency-Throughput Paradox in LLM Inference

Deploying Large Language Models (LLMs) in high-throughput enterprise environments presents a difficult trade-off: minimizing latency for individual users (Time to First Token - TTFT, and Inter-Token Latency - ITL) versus maximizing the overall throughput of the serving infrastructure (Queries Per Second - QPS). The source of this tension lies in the two distinct phases of LLM generation:
1. **The Prefill Phase:** The engine processes the prompt tokens. This phase is highly compute-bound because all prompt tokens are processed in parallel, allowing the GPU's Tensor Cores to perform massive General Matrix Multiply (GEMM) operations efficiently.
2. **The Decode Phase:** The engine generates subsequent tokens one by one, autoregressively. Each forward pass consumes the newly generated token to yield the next one. This phase is heavily memory-bound. The GPU must load the model weights (e.g., ~15 GB for a Llama-3 8B model in FP16) from High Bandwidth Memory (HBM) to its registers just to process a single input token vector of size 1. 

If you serve client requests sequentially (batch size = 1), the GPU spends 99% of its time waiting for weights to be copied from HBM to the processing cores, leaving the massive computing capacity of the silicon idle. 

Dynamic batching resolves this underutilization by holding incoming requests in a gateway queue for a microsecond-scale delay window. Instead of processing requests as they arrive, the gateway aggregates them into a batch and presents them to the GPU as a single, combined tensor. The GPU then loads the model weights once and performs the matrix multiplications for multiple tokens simultaneously. 

By grouping 32 or 64 requests, you amortize the HBM access cost over the entire batch, increasing GPU compute utilization and elevating system throughput by up to 10x. However, this delay window must be carefully tuned: if it is too wide, tail latencies degrade; if it is too narrow, the engine dispatches under-filled batches and fails to saturate the hardware.

## Architecture of a Custom C++ Gateway

A production-ready gateway requires a highly concurrent, asynchronous, non-blocking architecture. In our design, the C++ gateway utilizes the `grpc++` library to manage incoming network connections. 

The gateway is structured around three primary architectural components:
* **Ingress Worker Threads:** These threads are driven by gRPC CompletionQueues. When a client initiates a request, a worker thread handles the connection, extracts the prompt, runs the client-side authentication checks, tokenizes the text into a vector of integer token IDs, and packages the data into an `InferenceRequest` context. This context is then pushed to the concurrent queue. Instead of block-waiting for the GPU to finish, the worker registers a completion callback or registers a `std::promise` and returns to handle new network events.
* **The Dynamic Batcher Thread:** A dedicated thread owns the scheduling loop. It continuously monitors the queue, pops requests, evaluates their KV cache memory footprint, and builds optimal batches based on maximum batch sizes and token limits. Once a batch is sealed, the batcher dispatches the aggregated tensor structures to the underlying GPU runtime via a C++ engine API.
* **The Response Dispatcher Loop:** Once the GPU engine completes a forward pass (or a generation step), it issues a callback or resolves the promised futures. The dispatcher loop picks up these completed tokens, translates token IDs back to text using a fast concurrent tokenizer, and streams the chunks back down the active gRPC stream to the client.

By keeping the allocation of memory off the network thread path and minimizing locks, this architecture achieves sub-millisecond dispatching precision, ensuring that the gateway does not introduce overhead that could degrade the GPU's throughput.

## Implementing the Thread-Safe Request Queue

A naive thread-safe queue implemented with a single standard `std::mutex` and `std::queue` will quickly become a contention bottleneck when hundreds of concurrent connections try to enqueue requests simultaneously. To minimize lock contention, we implement a thread-safe queue that supports *batch-popping*. 

Instead of popping requests one by one—which requires acquiring and releasing the lock for every single item—the Batcher thread acquires the lock once and drains the queue up to a specified batch limit. If the queue is empty, the thread yields and waits on a condition variable, waking up only when new requests are pushed or when the batcher's delay timer expires.

The following code implements this high-performance concurrent queue structure:

<script src="https://gist.github.com/mohashari/f743571e50320ab6c647093e723a03eb.js?file=snippet-1.txt"></script>

## The Heart of the Gateway: The Dynamic Batcher Loop

The gateway must represent each incoming inference task as a rich state context. The context tracks the client’s parameters, the tokenized input IDs, the target completion limits, high-precision timing stats to measure SLA health, and the channel to stream generated tokens. 

<script src="https://gist.github.com/mohashari/f743571e50320ab6c647093e723a03eb.js?file=snippet-2.txt"></script>

Using these structures, the dynamic batcher thread executes its main loop. The scheduling decision is controlled by three constraints:
1. `max_batch_size`: The maximum number of sequences the GPU can compute in parallel without running out of register files or exceeding core occupancy limit.
2. `max_tokens_in_batch`: The combined sum of tokens across all queries in a batch, which limits memory allocations during attention calculation.
3. `max_queue_delay_us`: The maximum duration a request can sit in the queue before it must be processed.

If the batcher retrieves a batch of requests from the queue but finds that adding the next request would exceed the token capacity of the active GPU memory, it must isolate the overflow request and return it to the queue for the next execution pass.

<script src="https://gist.github.com/mohashari/f743571e50320ab6c647093e723a03eb.js?file=snippet-3.txt"></script>

## Continuous Batching vs. Static Dynamic Batching

The dynamic batcher loop described in **Snippet 3** represents *Static Dynamic Batching*. In this paradigm, once a batch is sent to the GPU, it runs until *all* requests in the batch have generated their maximum number of tokens. This is highly inefficient in production. 

For example, if Request A completes after generating 10 tokens but Request B requires 512 tokens, Request A remains locked in the active batch. The GPU continues to perform forward calculations for Request A (using padded dummy tokens) for the remaining 502 steps, wasting GPU resources and degrading system efficiency.

To solve this, modern inference engines use *Continuous Batching* (or In-Flight Batching). In continuous batching, the scheduling decision is evaluated at the iteration level—meaning after every single forward token-generation step. 

At the end of an iteration:
1. Finished sequences are evicted from the active batch immediately.
2. The scheduler polls the request queue to see if new prompts can be inserted (promoted to prefill phase) to fill the newly freed batch slots.
3. The scheduler checks if it has the required memory blocks in the Key-Value (KV) cache to sustain another step.

The following code implements an iteration-level scheduler that coordinates continuous batching:

<script src="https://gist.github.com/mohashari/f743571e50320ab6c647093e723a03eb.js?file=snippet-4.txt"></script>

## Managing Memory: KV Cache Allocation and Backpressure

The single greatest source of runtime instability in LLM gateways is GPU Out-of-Memory (OOM) errors during generation. An LLM stores the keys and values of all historical context tokens in the KV Cache to avoid recomputing them. 

The memory required for a single request context scales linearly with sequence length:
$$\text{KV Cache Size} = 2 \times \text{layers} \times \text{heads} \times \text{dim} \times \text{precision} \times \text{sequence\_length}$$

For a Llama-3 8B model utilizing FP16 precision, this equates to roughly 0.5 MB of VRAM per token. If you pre-allocate KV cache buffers using a static, worst-case sequence length (e.g., reserving space for 8,192 tokens per batch slot), a batch size of 64 would require $64 \times 8192 \times 0.5 \text{ MB} \approx 262 \text{ GB}$ of VRAM. This is far beyond the physical capacity of standard hardware (such as an 80 GB A100 or H100 GPU), which forcing you to run with small batch sizes and low throughput.

To prevent overallocation, we must implement a memory block allocation manager modeled after **PagedAttention**. Instead of allocating contiguous chunks of VRAM for each sequence, we divide the KV Cache into fixed-size physical memory pages (e.g., 16 tokens per block). The gateway maps virtual token indices to these physical blocks using a lookup table. 

If the cache manager runs low on physical blocks, it must trigger backpressure mechanisms at the gateway ingress. This is done by returning HTTP `429 Too Many Requests` or gRPC status `RESOURCE_EXHAUSTED` errors to clients, rather than letting the GPU overallocate VRAM and crash.

Here is the implementation of a thread-safe KV Cache block allocator:

<script src="https://gist.github.com/mohashari/f743571e50320ab6c647093e723a03eb.js?file=snippet-5.txt"></script>

## Production Tuning and Real-World Failure Modes

When you deploy a custom C++ batching gateway to production, you will encounter edge cases that do not manifest in synthetic benchmarks:

### 1. The Padding Tax
If your underlying tensor execution engine uses standard dense matrix multiplication kernels, it forces you to pad all inputs in a batch to the length of the longest input sequence. For example, if you batch a request with 8 tokens and a request with 2,048 tokens, the model engine must run attention operations for 2,048 tokens on *both* requests. 

This waste of computation is known as the **Padding Tax**. To prevent this, ensure your backend utilizes ragged tensor operators (such as FlashAttention-2 with variable-length sequence inputs or vLLM’s custom CUDA kernel mappings) that eliminate pad tokens completely from the GPU compute grids.

### 2. Request Starvation under Low Traffic
If your gateway experiences periods of low traffic, setting `max_queue_delay_us` to a high value (e.g., 20 milliseconds) will introduce latency. A request arriving at an empty queue will sit waiting for the full 20ms in the hope that other requests will arrive to form a batch. 

If no other requests arrive, it runs alone, having incurred a 20ms penalty for no throughput benefit. To fix this, implement an *adaptive timeout algorithm* that dynamically scales the delay window down to 0 microseconds when the current query rate is low, and scales it up to a maximum cap (e.g., 4ms) as QPS increases.

### 3. KV Cache Fragmentation
Similar to virtual memory fragmentation, physical KV cache blocks will fragment over time if users request highly variable generation lengths. Continuous allocation and deallocation of multi-page blocks can lead to a state where there are enough total free blocks, but they are scattered, degrading hardware execution alignment. 

Using a fixed-page scheme like PagedAttention mitigates external fragmentation. However, internal fragmentation still occurs within the final block of a sequence (which is rarely filled completely). Selecting an optimal block size is critical:
* **Large block sizes (e.g., 64 tokens):** Low lookup table overhead but high internal fragmentation.
* **Small block sizes (e.g., 16 tokens):** Minimal memory waste but higher overhead when walking the block tables during attention kernels.

### 4. Client Cancellation Leakage
In web environments, users frequently cancel requests (e.g., by closing a browser tab or hitting search stop). If the gRPC worker does not actively propagate connection termination down to the scheduler, the GPU will continue executing forward loops for that request until it reaches `max_new_tokens`. 

This waste of GPU execution cycles is known as **cancellation leakage**. Your C++ gateway must register cancellation callbacks with `grpc::ServerContext::AsyncNotifyWhenDone()` and immediately signal the scheduler to flag the sequence status as `FINISHED`, reclaiming its KV cache blocks on the very next scheduler loop step.

## Configuration and Production Tuning

Operating a custom C++ gateway requires configuring network thread pools, token limit budgets, queue limits, and memory thresholds. 

The configuration file below is optimized for deploying a Llama-3 8B model on a single node containing two NVIDIA A100 GPUs (configured in a Tensor Parallel arrangement):

```yaml
# // snippet-6
# Production Dynamic Batcher Gateway Configuration
gateway:
  port: 50051
  worker_threads: 16             # Matches number of CPU cores for async gRPC completion queues
  backpressure:
    max_queued_requests: 2048    # Max queue depth before returning RESOURCE_EXHAUSTED (gRPC 13)
    reject_on_oom: true          # Deny entry if GPU block capacity is depleted

engine:
  model_path: "/opt/models/llama-3-8b-instruct"
  tensor_parallel_size: 2        # Split model layers across 2 local GPUs using Megatron-LM tensor parallel style
  pipeline_parallel_size: 1      # No inter-node execution pipelines
  max_model_len: 8192            # Hard context window limit for Llama-3

batching:
  max_batch_size: 64             # Upper limit on active sequences inside a single forward pass
  max_tokens_in_batch: 8192      # Max aggregated tokens (prefill + decode) allocated in memory
  max_queue_delay_us: 4000       # 4ms maximum wait window to accumulate incoming requests
  enable_continuous_batching: true

kv_cache:
  gpu_memory_fraction: 0.85      # Dedicate 85% of GPU VRAM to KV Cache blocks (remaining for weights and activation buffers)
  block_size_tokens: 16          # 16-token page size for optimal PagedAttention scheduling
  swap_space_gb: 16              # Allocation of Host RAM to temporarily swap out preempted KV cache blocks
```

## Conclusion

Dynamic and continuous batching are essential for building high-performance, cost-effective LLM serving infrastructures. By migrating these scheduling concerns into a high-performance C++ gateway, you remove runtime latency bottlenecks and gain precise control over memory allocations, threading, and backpressure. 

When configuring your gateway, start with a conservative `max_queue_delay_us` (e.g., 2ms to 4ms) and prioritize implementing continuous iteration-level scheduling. This layout will keep your GPUs saturated, your latency SLAs healthy, and your system protected from VRAM overallocation crashes.