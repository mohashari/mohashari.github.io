---
layout: post
title: "Building a Custom GPU Memory Allocator for PyTorch Extension C++ Kernels: Mitigating CUDA Fragmented Memory in Real-time LLM Serving"
date: 2026-08-25 08:00:00 +0700
tags: [ai-engineering, pytorch, cuda, performance]
description: "Mitigate CUDA Out-of-Memory (OOM) errors and host-side latency spikes in real-time LLM serving by building an isolated custom GPU arena allocator for C++ kernels."
image: "https://picsum.photos/seed/6246/1080/720"
thumbnail: "https://picsum.photos/seed/6246/400/300"
---
In production real-time LLM serving pipelines (such as vLLM or Hugging Face TGI), serving engines use continuous batching and dynamic padding to handle incoming user queries of wildly varying prompt and output lengths. When executing custom C++ or CUDA extensions to run optimized kernels like fused attention, dynamic layer-norms, or speculative decoding verification, these kernels must allocate intermediate scratchpad buffers (workspaces) at runtime. Under heavy concurrent load—say, serving Llama-3-70B on an 8x NVIDIA H100 node with dynamic batch sizes ranging from 1 to 128 and sequence lengths up to 8,192 tokens—the default `c10::cuda::CUDACachingAllocator` in PyTorch can suffer from severe memory fragmentation. Over 24 to 48 hours of continuous operation, this fragmentation causes the allocator to split large cached memory blocks into tiny, non-contiguous chunks. Eventually, when a kernel requests a larger contiguous block for a workspace, the server crashes with a CUDA Out of Memory (OOM) error, even if the total active GPU memory usage is well under 60% of capacity.

![Building a Custom GPU Memory Allocator for PyTorch Extension C++ Kernels: Mitigating CUDA Fragmented Memory in Real-time LLM Serving Diagram](/images/diagrams/building-custom-gpu-memory-allocator-pytorch-extension-cpp-kernels-cuda-fragmented-memory-llm-serving.svg)

## The Anatomy of PyTorch's Allocator Fragmentation

To understand why this failure occurs, we have to look at the internals of PyTorch's memory caching strategy. PyTorch does not execute raw `cudaMalloc` and `cudaFree` calls on every tensor creation and destruction. Doing so would serialize GPU host-side threads and introduce tens of microseconds of synchronization overhead per allocation. Instead, the `c10::cuda::CUDACachingAllocator` pre-allocates large VRAM pools from the CUDA driver and manages them internally using a buddy-like allocation scheme. It categorizes blocks into two primary pools: small blocks (for buffers ≤ 1 MB) and large blocks (for buffers > 1 MB).

When a request for a new tensor arrives, PyTorch searches its cached pools for a free block that fits the requested size. If an exact match is not found, PyTorch will:
1. Split a larger free block, using the required chunk and putting the remainder back into the cache pool.
2. If no large enough block exists in the pool, it will trigger an internal garbage collection sweep to coalesce free blocks.
3. If coalescing fails, it falls back to a raw `cudaMalloc` call.
4. If the device runs out of addressable memory during `cudaMalloc`, it throws a CUDA OOM runtime exception.

In standard offline training, model shapes, sequence lengths, and batch sizes are static, allowing the caching allocator to warm up and stabilize its pools within the first few forward-backward passes. However, in real-time inference serving, the request stream is stochastic. A token generation step might need a tiny workspace for a batch size of 1, followed immediately by a prefill step for a massive prompt requiring a 1.2 GB intermediate buffer. 

When PyTorch constantly splits larger blocks to satisfy small dynamic allocations, it creates a pattern resembling a Swiss cheese heap. A senior engineer looking at `torch.cuda.memory_summary()` during a crash might see:
- **Reserved Memory**: 78.2 GB (97.7% of an A100/H100)
- **Active Memory**: 42.1 GB
- **Largest Free Chunk**: 128 MB

If a fused custom kernel attempts to allocate a 512 MB workspace, PyTorch cannot find a single 512 MB contiguous block, despite having 36.1 GB of total free VRAM. The server crashes. To make matters worse, before crashing, the CPU host thread will repeatedly block on `cudaDeviceSynchronize` and allocator lock acquisitions, driving p99 latencies up from 25ms to over 2000ms.

## Design of a High-Performance Custom GPU Arena Allocator

We can resolve this performance and reliability bottleneck by completely isolating the transient memory allocations of custom C++ kernels from PyTorch's caching allocator. Instead of calling `at::empty` or `torch::zeros` within our C++ extension, we build a custom GPU arena allocator. 

This allocator has the following design principles:
1. **Pre-Allocation**: During the initialization phase of the inference server, the allocator issues a single, large `cudaMalloc` call to claim a dedicated block of GPU memory (e.g., 2 GB).
2. **Deterministic Slab Partitioning / First-Fit with Coalescing**: It manages this space using a CPU-side metadata structure, allowing it to perform sub-allocations in $O(1)$ or $O(N)$ time, where $N$ (the number of active allocations) is very small.
3. **Strict 256-Byte Alignment**: CUDA memory transactions read and write VRAM in 32, 64, or 128-byte segments. Vectorized memory loads (such as `LDG.128` or tensor core operations) require data to be aligned to at least 256-byte boundaries. Our allocator must round up every request to a multiple of 256 bytes to prevent performance degradation or misaligned memory access faults.
4. **PyTorch Tensor Integration via Custom Deleters**: When we return memory allocated from our arena to Python, we wrap the raw pointer inside a PyTorch `at::Tensor` using `torch::from_blob`. We attach a custom C++ deleter callback to the tensor. When the tensor's reference count falls to zero in Python, PyTorch's runtime automatically invokes this callback, recycling the memory block back to our arena without interacting with PyTorch's caching allocator.
5. **Thread Safety**: Real-time LLM servers often execute request schedules across multiple CPU worker threads or run asynchronous kernels on different CUDA streams. The allocator's CPU metadata changes must be guarded by lightweight locks to prevent race conditions.

## The Implementation: C++ Arena Allocator

Let's build this custom allocator. First, we define the allocator interface and block metadata structure in a header file.

<script src="https://gist.github.com/mohashari/8cf54e4334cbc093db9dafbe3b8d0729.js?file=snippet-1.txt"></script>

Next, we write the implementation. We implement a first-fit allocation algorithm with block splitting and contiguous block coalescing. We align all block start positions and sizes to 256-byte boundaries.

<script src="https://gist.github.com/mohashari/8cf54e4334cbc093db9dafbe3b8d0729.js?file=snippet-2.txt"></script>

## The C++ PyTorch Custom Extension

Now we implement a realistic CUDA kernel and wrap it inside a PyTorch extension. We write a fused bias-add and GeLU activation kernel, which is common in transformer MLP layers. The kernel takes an input tensor and a bias vector, executes a 1D grid launch, and outputs the transformed elements. 

First, we define the CUDA kernel launcher:

<script src="https://gist.github.com/mohashari/8cf54e4334cbc093db9dafbe3b8d0729.js?file=snippet-3.txt"></script>

Next, we write the C++ extension code. Instead of allocating the output tensor with PyTorch's native memory management, we allocate raw memory from our custom `GpuArenaAllocator`. We then wrap the raw pointer into a PyTorch tensor via `torch::from_blob` and pass a custom lambda function that returns the pointer to our arena upon destruction.

<script src="https://gist.github.com/mohashari/8cf54e4334cbc093db9dafbe3b8d0729.js?file=snippet-4.txt"></script>

Now, we set up the pybind11 module bindings to expose our functions and the allocator's lifecycle handlers to Python.

<script src="https://gist.github.com/mohashari/8cf54e4334cbc093db9dafbe3b8d0729.js?file=snippet-5.txt"></script>

## Compilation and Build Configuration

We write a standard `setup.py` file to compile the extension. We pass compiler flags to optimize execution, including `-O3` for host-side C++ compilation and `--use_fast_math` for the CUDA NVCC compiler.

<script src="https://gist.github.com/mohashari/8cf54e4334cbc093db9dafbe3b8d0729.js?file=snippet-6.py"></script>

## Verification and Memory Profiling

To prove that the custom allocator isolates dynamic allocations and leaves PyTorch's native caching pools clean, we can write a test script. The script initializes the custom allocator, checks the output's mathematical accuracy against PyTorch's native operations, and runs a loop with variable sequence dimensions to simulate dynamic batching.

<script src="https://gist.github.com/mohashari/8cf54e4334cbc093db9dafbe3b8d0729.js?file=snippet-7.py"></script>

## Production Integration and Operational Trade-Offs

When integrating an isolated memory allocator into a real-time LLM inference server, keep these operational considerations in mind:

### Stream Synchronization
Because the CUDA kernel runs asynchronously on PyTorch's active CUDA stream, the custom allocator deallocates memory blocks on the CPU while the GPU is still executing the kernel. This is safe because CUDA streams guarantee in-order execution of kernels. 

If block `A` is allocated, used by a kernel, freed, and subsequently reassigned to block `B` for a later step, the kernels using block `A` and block `B` are queued sequentially on the same CUDA stream. The device will not write to block `B` until it finishes reading block `A`. However, if your architecture passes tensors across multiple CUDA streams (for example, between a pipeline-parallel network transport stream and the computation stream), you must insert CUDA stream synchronizations or record stream events using `c10::cuda::CUDAStream::recordEvent()` before returning memory blocks back to the arena.

### Memory Overhead vs. Reliability
By setting aside 2 GB of memory for our custom arena, we reduce the VRAM available to PyTorch for storing KV caches by 2 GB. In a high-throughput server, this reduces the maximum batch size by a small margin. However, this is a reasonable trade-off: trading a fraction of the maximum batch capacity for a complete guarantee that the service will not experience fragmentation-induced CUDA OOM crashes during long runs.

### Tooling and Visual Verification
To verify your custom allocator's behavior in production, use NVIDIA Nsight Systems. Run your workload with the command:
```bash
nsys profile -w true -t cuda,nvtx,osrt -o my_profile python test_allocator.py
```
When you open the trace in Nsight Systems, confirm that there are no runtime calls to `cudaMalloc` or `cudaFree` during the request loop. If you see host thread synchronization blocks like `cudaStreamSynchronize` or `cudaDeviceSynchronize` outside of initialization, inspect your custom extension code for inadvertent tensor operations that fall back to PyTorch's default memory management.