---
layout: post
title: "Implementing Dynamic Batching for Model Serving Pipelines in Go using Channels and Semaphores"
date: 2026-08-01 08:00:00 +0700
tags: [go, machine-learning, concurrency, performance]
description: "Learn how to build a high-throughput, low-latency dynamic batching pipeline for ML models in Go using channels, select blocks, and semaphores."
image: "/images/diagrams/implementing-dynamic-batching-model-serving-pipelines-go-channels-semaphores.svg"
thumbnail: "/images/diagrams/implementing-dynamic-batching-model-serving-pipelines-go-channels-semaphores.svg"
---

When serving deep learning models in production—whether they are Large Language Models (LLMs), dense vector embeddings, or computer vision classifiers—processing requests individually (batch size = 1) is a waste of capital. A single forward pass on an NVIDIA H100 or A100 GPU might consume only 5% of its tensor core capacity, yet it incurs 100% of the kernel launch overhead and GPU memory bus latency, capping throughput to a fraction of the hardware's capability. To unlock the full power of modern hardware, you must batch requests. However, forcing clients to accumulate batches upstream destroys responsiveness and breaks strict Service Level Objectives (SLOs). Server-side *dynamic batching* solves this conflict: it accepts individual incoming HTTP or gRPC requests concurrently, aggregates them into optimal batch sizes over a microsecond-scale window, executes them as a single tensor operation, and scatters the results back to the original client connections. Implementing this pattern efficiently requires robust concurrency control, strict latency limits, and backpressure mechanisms that prevent system overload.

![Implementing Dynamic Batching for Model Serving Pipelines in Go using Channels and Semaphores Diagram](/images/diagrams/implementing-dynamic-batching-model-serving-pipelines-go-channels-semaphores.svg)

## The GPU Underutilization Problem in Production

To understand why server-side dynamic batching is non-negotiable, we must look at how modern GPUs compute tensors. A GPU contains thousands of Arithmetic Logic Units (ALUs) designed for Single Instruction, Multiple Data (SIMD) execution. When you invoke a deep learning inference engine like ONNX Runtime, TensorRT, or PyTorch (often wrapped in C++ or exposed via CGO in Go), the engine compiles the neural network layers into a sequence of GPU kernels.

If your backend service receives a steady stream of requests at 500 requests per second (RPS) and serves them sequentially, each request triggers its own CUDA kernel launch. The GPU spend most of its time waiting for memory transfers (Host-to-Device and Device-to-Host copies over PCIe) and kernel launch coordination rather than actual computation. 

For instance, running a BERT-Base model with a batch size of 1 might take 4ms. Running the same model with a batch size of 16 might only take 8ms. By grouping 16 requests together:
* Aggregate latency only doubles (from 4ms to 8ms).
* Individual average latency remains within acceptable limits.
* Throughput increases by 800% (from 250 RPS to 2000 RPS).

Dynamic batching acts as a buffering layer that sits between the transport layer (HTTP/gRPC handlers) and the model runtime. It aligns asynchronous incoming requests into structured, synchronous batch windows without requiring the client to know anything about the batching mechanics.

## Architecture of a Go-Based Dynamic Batcher

Go is an exceptional language for building model-serving gateways. Its runtime scheduler (`GOMAXPROCS`) handles thousands of concurrent goroutines with minimal overhead, and channels provide a clean, thread-safe communication model. 

The pipeline we are designing consists of three main components:
1. **The HTTP/gRPC Handler**: Receives individual client requests, wraps the payload in a `Job` containing a dedicated response channel and context, enqueues the job into the batcher, and blocks waiting for a response.
2. **The Batcher Loop**: Consumes jobs from the queue. It maintains an internal slice representing the active batch. The batch is flushed to a worker under two conditions:
   * **Size-based flush**: The batch reaches the maximum configured batch size (e.g., 32).
   * **Time-based flush**: A timer expires (e.g., 5ms since the first item was added to the batch), ensuring that the first request in the batch is not delayed indefinitely.
3. **The Worker Pool & Concurrency Limit**: Spawns workers to run the model inference. Crucially, the execution phase must be protected by a semaphore to prevent VRAM saturation and thread thrashing at the CGO boundary.

## The Core Blueprint: Structuring the Job

To coordinate concurrent client goroutines with a single background processing loop, we must encapsulate each prediction task into a self-contained unit of work. This struct needs the input payload, a context for cancellation propagation, and a buffered channel to deliver the result back.

<script src="https://gist.github.com/mohashari/872223b5ceb66f558ee4b4bf6a8666b8.js?file=snippet-1.go"></script>

By passing a channel (`Result`) inside the job struct, we establish a scatter-gather pattern: the batcher loop gathers individual jobs, executes them in a batch, and then scatters the outputs back to their respective channels.

## Designing the Dynamic Batcher Loop

The batcher loop must run continuously in the background, listening on a Go channel for incoming jobs while managing a deadline timer. 

A common concurrency trap in Go is reusing the same backing slice for the batch. If you execute `batch = batch[:0]` to avoid allocations, the background worker goroutine spawned via `go db.processBatch(ctx, batch)` will suffer from severe data races as the main loop writes new jobs into the same memory. Therefore, allocating a new slice `batch = make([]Job, 0, db.maxBatchSize)` is mandatory for concurrency safety.

Here is the struct definition and its constructor:

<script src="https://gist.github.com/mohashari/872223b5ceb66f558ee4b4bf6a8666b8.js?file=snippet-2.go"></script>

Now, let's implement the core loop. To handle the latency budget properly without leaking timers or causing race conditions, we initialize the timer only when the first job of a new batch is received. 

<script src="https://gist.github.com/mohashari/872223b5ceb66f558ee4b4bf6a8666b8.js?file=snippet-3.go"></script>

## Concurrency Control and GPU Resource Management

If we spawn an unbounded number of goroutines calling `db.processBatch` simultaneously, we will overwhelm the model serving library. In ML inference, overloading a GPU's VRAM with simultaneous allocations causes fatal Out-Of-Memory (OOM) segmentation faults. Even if memory isn't fully exhausted, queuing multiple batches concurrently at the GPU driver level causes latency spikes due to CUDA stream context-switching.

To prevent this, we use Go’s `golang.org/x/sync/semaphore` to cap concurrent inference executions. We also perform a critical "double-check context" optimization: if a client's connection is closed while the job was waiting in the queue or for the semaphore, we prune it before calling the inference function.

<script src="https://gist.github.com/mohashari/872223b5ceb66f558ee4b4bf6a8666b8.js?file=snippet-4.go"></script>

## Integrating with the HTTP Layer

The HTTP handler must wrap incoming payloads, push them to the batcher, and wait for results. 

A production HTTP server must enforce strict client-side timeouts. If a client expects a response within 100ms, the HTTP handler should set a context timeout. If the server is experiencing high queue delays, the handler will time out and return a status code (like `504 Gateway Timeout` or `499 Client Closed Request`). By buffering the job's `Result` channel to `1`, the worker won't lock up if the handler has already returned and is no longer listening.

<script src="https://gist.github.com/mohashari/872223b5ceb66f558ee4b4bf6a8666b8.js?file=snippet-5.go"></script>

## Complete Pipeline Assembly & Graceful Shutdown

To demonstrate how the entire pipeline fits together, we construct a runnable script. It initializes the batcher with a mock inference function that simulates dynamic latency characteristics based on the batch size, starts the background batcher loop, spins up concurrent client traffic, and performs a clean graceful shutdown.

<script src="https://gist.github.com/mohashari/872223b5ceb66f558ee4b4bf6a8666b8.js?file=snippet-6.go"></script>

## Production Failure Modes & Mitigation Strategies

Implementing dynamic batching involves navigating several edge cases that can easily cause memory leaks, crashes, or severe latency regressions under high load.

### 1. The "Ghost Request" GPU Waste

When client connections drop or time out while their jobs are sitting in the `jobQueue` or waiting for the execution semaphore, the request is considered a "ghost request." If the worker goes ahead and runs the inference on this request anyway, it wastes expensive GPU compute cycles.

**Mitigation**:
Implement double-check validation. Check `job.Ctx.Err()` twice:
1. In the batcher loop, before placing the job into the batch slice.
2. In the worker goroutine, immediately after acquiring the semaphore and right before invoking `inferenceFn`.

### 2. High Allocation Costs and GC Thrashing

In Go, constructing dynamic slices like `inputs` and `activeJobs` inside `processBatch` allocates memory on the heap. Under a sustained load of 5000 RPS, these short-lived slices create significant pressure on Go’s garbage collector (GC), causing stop-the-world spikes that increase P99 latency.

**Mitigation**:
Utilize `sync.Pool` to reuse the slice buffers. You can define pools for `[]PredictionInput` and `[]Job` that are sized to your `maxBatchSize`. When the worker finishes processing a batch, it resets the slices to zero length (preserving capacity) and returns them to the pool.

### 3. VRAM Fragmentation via Unbounded Inputs

If your model accepts variable-length sequences (e.g., text generation or image segmentation), padding requests to the longest sequence in the batch is required for tensor compilation. If a batch contains seven short inputs and one exceptionally long input, the entire batch must be padded to the longest sequence length. This causes massive memory spikes, underutilizing processing speed and potentially triggering an OOM crash.

**Mitigation**:
Implement *bucketing* before batching. Instead of a single `jobQueue`, maintain multiple queues representing different sequence length ranges (e.g., bucket-128, bucket-512, bucket-1024). The batcher loop pulls from a bucket queue and groups items of similar sizes together.

### 4. Native Code Crash Isolation

Model inference functions often interact with shared libraries (`.so` or `.dll`) via CGO. If a segmentation fault occurs in the C++ runtime of ONNX or PyTorch, it crashes the entire Go binary, taking down your web service.

**Mitigation**:
Always run the model server behind a process supervisor like Kubernetes, systemd, or a sidecar proxy. In mission-critical environments, isolate the CGO calls by wrapping the inference engine in a separate, lightweight daemon process communicating over Unix Domain Sockets or shared memory. If the daemon process crashes, the Go gateway intercepts the termination, returns `503 Service Unavailable` to the user, and spawns a fresh worker instance without interrupting the HTTP network layer.

## Conclusion

Dynamic batching is a core design pattern for production machine learning architecture. By wrapping this logic inside a Go application using channels, select blocks, and semaphores, you can optimize GPU throughput while maintaining control over request latency.

When configuring your batcher, run load tests to identify the optimal balance between `maxBatchSize` and `maxLatency`. Start with a latency window of 3ms-8ms and a batch size of 16 or 32, adjusting based on the memory capacity and tensor dimensions of your target hardware.