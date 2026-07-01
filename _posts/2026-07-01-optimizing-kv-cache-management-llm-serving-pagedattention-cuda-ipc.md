---
layout: post
title: "Optimizing Key-Value Cache Management in LLM Serving with PagedAttention and CUDA IPC"
date: 2026-07-01 08:00:00 +0700
tags: [ai-engineering, cuda, llm-serving, performance]
description: "Eliminate VRAM fragmentation and PCIe transfer overheads in decoupled LLM serving using PagedAttention and zero-copy CUDA Inter-Process Communication (IPC)."
image: "https://picsum.photos/seed/2793/1080/720"
thumbnail: "https://picsum.photos/seed/2793/400/300"
---

In high-throughput LLM serving, managing GPU memory is the difference between cost-effective scaling and persistent Out-Of-Memory (OOM) crashes. When hosting large models like Llama 3 70B, key-value (KV) cache allocation is the primary bottleneck. The autoregressive nature of LLMs means memory consumption grows linearly with sequence length. Standard PyTorch memory allocators assign contiguous blocks of GPU memory based on the *maximum* possible sequence length (e.g., 8,192 or 32,768 tokens) to prevent runtime allocation overhead. In production, this results in massive internal fragmentation: up to 60-80% of allocated GPU VRAM sits completely idle, reserved for tokens that are never generated. This waste severely restricts the maximum batch size, causing degraded throughput. PagedAttention virtualizes physical GPU memory to eliminate this fragmentation, but scaling this optimization in decoupled multi-process serving environments introduces a new bottleneck: high-latency inter-process memory copying over the PCIe bus.

## The Mechanics of KV Cache Bloat

During LLM generation, the attention mechanism requires access to all previous key-value vectors for the prompt and generated tokens. At each new generation step, a new token is converted into key and value vectors. Rather than recomputing all historical keys and values, we store them in a persistent cache—the KV Cache.

For a model with $N_{layers}$, $N_{kv\_heads}$, $D_{head}$ dimensions, and running at precision float16 or bfloat16 (2 bytes per weight):

$$\text{KV Cache Size per Token} = 2 \times N_{layers} \times N_{kv\_heads} \times D_{head} \times 2 \text{ bytes}$$

Let's calculate the exact memory footprint for a Llama 3 70B model using Grouped Query Attention (GQA):
* $N_{layers} = 80$
* $N_{kv\_heads} = 8$
* $D_{head} = 128$
* Precision = bfloat16 (2 bytes)

$$\text{KV Cache Size per Token} = 2 \times 80 \times 8 \times 128 \times 2 = 327,680 \text{ bytes} \approx 320 \text{ KB/token}$$

At a context length of 8,192 tokens:

$$320 \text{ KB} \times 8,192 = 2.56 \text{ GB per sequence}$$

For a concurrent batch size of 64:

$$2.56 \text{ GB} \times 64 = 163.84 \text{ GB}$$

This exceeds the physical VRAM of an 80GB H100 GPU by over 2x. Even if we split the model weights across two GPUs using tensor parallelism, we are left with minimal headroom for model weights. 

If we pre-allocate contiguous chunks for the maximum context length, any request that finishes early (e.g., in 150 tokens instead of 8,192) locks that 2.56 GB of memory until the request terminates. This over-reservation limits system concurrency, driving up host costs and latency because we are forced to scale up the number of active GPU instances.

## PagedAttention: Virtual Memory for GPU VRAM

Inspired by virtual memory in operating systems, PagedAttention partitions the KV cache of each sequence into fixed-size blocks (pages), typically of size 16 or 32 tokens. Instead of allocating contiguous memory buffers, we allocate non-contiguous physical blocks from a pre-allocated global memory pool.

A lookup table (the Block Table) translates logical sequences to physical addresses at runtime. The block manager dynamically assigns physical blocks to active requests, eliminating internal fragmentation and reducing waste to less than 4% per sequence.

The following block manager allocates physical pages and maintains the mapping table.

<script src="https://gist.github.com/mohashari/945a846ca87a361b957186fe7e3b1b2b.js?file=snippet-1.py"></script>

## The Bottleneck of Decoupled Serving Architectures

LLM generation consists of two phases with completely different execution profiles:
1. **Prefill phase:** Processes the prompt tokens, computes the initial attention matrices, and outputs the first token. It is compute-bound, exploiting high parallel tensor core utilization.
2. **Decode phase:** Generates subsequent tokens one by one. It is highly memory-bandwidth bound because each step needs to read the model weights and the entire historical KV cache from High Bandwidth Memory (HBM) to compute just a single new token.

When scheduled on the same GPU, prefill requests block decode steps. A prefill request for a 4,000-token prompt can monopolize the GPU for 150ms, causing active decode streams to stall. This "prefill-induced decode starvation" results in high latency spikes (tail latency p99).

To solve this, modern serving systems decouple these phases. Prefill requests are processed by a dedicated cluster of GPUs (Prefill Nodes), and the resulting KV cache is sent to Decode Nodes. The decode nodes then perform token-by-token generation.

## The High Cost of PCIe Copying

Once prefill and decode are separated, we must transfer the KV cache from the prefill engine to the decode engine. If these run as separate processes (e.g., separate microservices, or separate containers on the same node to isolate CUDA contexts and dependencies), standard network protocols or host-memory transfers introduce unacceptable latency.

For a Llama 3 70B request with a 4,000-token prompt:

$$\text{Total KV Cache} = 4,000 \times 320 \text{ KB} = 1.28 \text{ GB}$$

If we copy this 1.28 GB from the Prefill GPU to Host RAM (Device-to-Host), transfer it via socket to the Decode process, and copy it from Host to Decode GPU (Host-to-Device), we incur multiple serialization and transit delays.

PCIe Gen4 x16 has a unidirectional peak bandwidth of 32 GB/s. A round-trip copy through host memory takes:

$$T_{copy} \ge \frac{1.28 \text{ GB}}{32 \text{ GB/s}} \times 2 = 80 \text{ ms}$$

This completely destroys the latency benefits of decoupling prefill and decode, as 80ms is often longer than the time spent on the prefill computation itself.

## Zero-Copy via CUDA IPC

CUDA Inter-Process Communication (IPC) allows sharing device memory pointers directly across processes running on the same physical system. It bypasses host memory and PCIe limits entirely.

CUDA IPC permits a process to export a device pointer into a shareable 64-byte handle (`cudaIpcMemHandle_t`). Another process on the same GPU node can import this handle and map it directly into its virtual address space.

If the two processes are running on different GPUs within the same server (e.g., GPU 0 and GPU 1), and those GPUs support peer-to-peer (P2P) access (via NVLink or PCIe switch), CUDA IPC will route the memory reads/writes directly over NVLink (up to 900 GB/s on H100). The latency for this transfer drops from 80ms to sub-millisecond ranges.

The following C++ PyTorch extension exports and imports tensors using CUDA IPC handles.

<script src="https://gist.github.com/mohashari/945a846ca87a361b957186fe7e3b1b2b.js?file=snippet-2.txt"></script>

## Stream Synchronization Across Process Boundaries

Since PyTorch executes CUDA commands asynchronously on streams, calling `export_tensor_ipc` and writing to the cache occurs on the Prefill process's stream. If the Decode process begins executing the decode steps immediately upon receiving the IPC handle, it might read the cache *before* the Prefill process has completed writing the data. This will result in silent corruption of the KV cache, producing gibberish tokens.

To prevent this, we must synchronize the streams of the two processes using CUDA Events shared via IPC. The Prefill process records an event on its compute stream, exports it to a CUDA IPC Event Handle, and transmits it alongside the memory handles. The Decode process imports the event handle and instructs its stream to wait on that event.

The following C++ extension exports and imports CUDA event handles.

<script src="https://gist.github.com/mohashari/945a846ca87a361b957186fe7e3b1b2b.js?file=snippet-3.txt"></script>

## Transporting IPC Metadata via Unix Domain Sockets

To transmit the raw 64-byte `cudaIpcMemHandle_t` and `cudaIpcEventHandle_t` bytes, we use a Unix Domain Socket (UDS) connection. Since the processes are on the same machine, UDS avoids local TCP overhead.

<script src="https://gist.github.com/mohashari/945a846ca87a361b957186fe7e3b1b2b.js?file=snippet-4.py"></script>

## Decoupled Architecture Implementation: Prefill and Decode Engines

Now we assemble the complete serving pipeline. The Prefill Engine processes the prompt, allocates the physical blocks, writes the cache, records the synchronization event, and transmits the handles.

<script src="https://gist.github.com/mohashari/945a846ca87a361b957186fe7e3b1b2b.js?file=snippet-5.py"></script>

The Decode Engine listens on the Unix Domain Socket, unpacks the metadata, imports the IPC memory and event handles, and blocks its own execution stream until the prefill stream completes writing.

<script src="https://gist.github.com/mohashari/945a846ca87a361b957186fe7e3b1b2b.js?file=snippet-6.py"></script>

## Critical Production Hazards and Architecture Optimization

Implementing CUDA IPC and PagedAttention in production exposes specific engineering edge cases that, if ignored, lead to system instability.

### Hazard 1: Dangling IPC Handles and CUDA Context Crashes

If the Prefill Engine frees a request's physical blocks in its global allocator pool *before* the Decode Engine is finished generating tokens, the Decode process will access unmapped virtual memory. This triggers a CUDA memory access violation (`cudaErrorIllegalAddress` - Code 700). 

Unlike CPU exceptions, a CUDA context error is fatal. The host CUDA context is destroyed, aborting all active requests scheduled on that GPU. 

**Solution:** Implement a strict bilateral handshaking protocol over the UDS connection. The Prefill Engine must not reclaim block IDs in its `PhysicalBlockAllocator` until it receives an explicit `RELEASE_REQUEST` acknowledgment packet from the Decode Engine.

### Hazard 2: Driver Call Overhead of Dynamic IPC Mapping

While opening and closing IPC handles is fast compared to PCIe copies, calling `cudaIpcOpenMemHandle` and `cudaIpcCloseMemHandle` still incurs a driver-level syscall penalty (1-3ms per invocation). When serving at high requests-per-second, this overhead accumulates, degrading throughput.

**Solution:** Eliminate dynamic mapping at runtime. Instead of exporting handles for individual requests, the Prefill and Decode Engines should pre-allocate the entire physical page pool (e.g., 20,000 blocks) at startup. 

During initialization, the Prefill process exports the handles for the *entire global cache tensor once*, and the Decode process maps them permanently. At runtime, the Prefill Engine only transmits standard JSON metadata packets containing the integer `block_ids` (offsets within the pre-mapped global tensors). This shifts the runtime overhead of memory mapping to a O(1) integer lookup.

## Deploying Decoupled Serving on Kubernetes

To utilize CUDA IPC between distinct containerized workloads on a Kubernetes cluster, the pods must reside on the same physical host and share the IPC namespace. 

<script src="https://gist.github.com/mohashari/945a846ca87a361b957186fe7e3b1b2b.js?file=snippet-7.yaml"></script>

In the configuration above, two things are vital:
1. `hostIPC: true` enables the containers to share the host's IPC namespace. CUDA IPC uses POSIX shared memory primitives and system V IPC keys internally to map handles; without this flag, containers are isolated and handles fail to resolve.
2. An `emptyDir` volume mounted at `/dev/shm` prevents PyTorch from running out of shared memory handles during rapid inter-process serialization.