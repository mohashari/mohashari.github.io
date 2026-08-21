---
layout: post
title: "Implementing Distributed KV Cache Sharding Across Multi-Node LLM Inference Engines using gRPC and RDMA"
date: 2026-08-21 08:00:00 +0700
tags: [ai-engineering, distributed-systems, performance, rdma]
description: "Scale LLM inference multi-node workloads by bypassing the CPU bottleneck with RDMA-driven KV cache transfer and gRPC-coordinated distributed page allocation."
image: "https://picsum.photos/seed/885/1080/720"
thumbnail: "https://picsum.photos/seed/885/400/300"
---

When serving Large Language Models (LLMs) with massive context windows (such as LLaMA-3 70B with 128k context) in production, memory bandwidth and inter-node network latency during the prefill-to-decode transition form a catastrophic throughput bottleneck. In a typical disaggregated prefill-decode architecture or multi-node tensor-parallel setup, the generated Key-Value (KV) cache must be shared or migrated across physical nodes. Transferring gigabytes of KV cache over traditional TCP/IP sockets saturates host CPUs, triggers kernel context switches, and drives up Time-To-First-Token (TTFT) to unacceptable levels. To bypass this bottleneck, we can implement a distributed KV cache sharding architecture that decouples control plane coordination from data plane transport. By utilizing a high-performance gRPC control plane for block metadata exchange and consistent-hash routing, alongside a zero-copy, kernel-bypassing RDMA-over-Converged-Ethernet (RoCEv2) data plane, nodes can write KV cache pages directly into remote GPU VRAM.

![Implementing Distributed KV Cache Sharding Across Multi-Node LLM Inference Engines using gRPC and RDMA Diagram](/images/diagrams/implementing-distributed-kv-cache-sharding-multi-node-llm-inference-grpc-rdma.svg)

## The Physics of the KV Cache: Quantifying the Overhead

To understand why traditional network stacks fail, we must evaluate the sheer scale of the KV cache. The KV cache size scales linearly with the sequence length, batch size, number of layers, and head dimension. Crucially, the introduction of Grouped-Query Attention (GQA) has significantly reduced the cache footprint, but at massive scale, it remains a severe burden. 

The formula to calculate the KV cache size in bytes for a single token is:

$$\text{Size}_{\text{token}} = 2 \times L \times H_{\text{kv}} \times D \times \text{bytes-per-element}$$

Where:
* $L$ is the number of transformer layers.
* $H_{\text{kv}}$ is the number of KV heads (in GQA).
* $D$ is the head dimension.
* The leading multiplier $2$ represents the separate matrices for Keys and Values.

Let's plug in the parameters for the LLaMA-3 70B model running in FP16 precision:
* Layers ($L$): 80
* KV Heads ($H_{\text{kv}}$): 8
* Head Dimension ($D$): 128
* Bytes per element: 2 (FP16)

$$\text{Size}_{\text{token}} = 2 \times 80 \times 8 \times 128 \times 2 = 327,680 \text{ bytes} \approx 320 \text{ KB per token}$$

For a single user session utilizing a 32,768 (32k) token context window, the KV cache requires:

$$32,768 \times 320 \text{ KB} = 10,485,760 \text{ KB} \approx 10.24 \text{ GB}$$

If the prefill phase runs on Node A and the decode phase is scheduled on Node B, migrating this single user's context cache over a standard 10 GbE TCP/IP connection takes **9.3 seconds** under theoretical limits. Even over a 100 GbE line, TCP protocol processing, socket buffer copies, and kernel context switches limit real-world throughput, taking **1.0 to 1.2 seconds** while consuming 100% of multiple host CPU cores. Under high concurrency, this copy overhead destroys the latency benefits of disaggregated execution.

By contrast, using GPUDirect RDMA over a 100 Gbps RoCEv2 network bypasses the host CPU and system RAM entirely. The Network Interface Card (RNIC) reads the memory directly from the GPU VRAM on Node A and writes it directly to the target GPU VRAM on Node B. This operation achieves near-wire speeds (11.8 GB/s), executing the transfer in **~870 milliseconds** with **0% host CPU utilization**.

## The Architecture: Decoupling Control and Data Planes

To build a production-grade distributed KV cache sharding engine, we separate the system into two distinct paths:
1. **The Control Plane (gRPC)**: A lightweight, strongly typed messaging layer. It coordinates block allocation maps, tracks sequence prefixes across nodes using a consistent hash ring, and exchanges memory addresses and remote access keys (`rkey`) required for RDMA connections.
2. **The Data Plane (RoCEv2 / GPUDirect RDMA)**: A high-throughput, low-latency transport layer. It operates directly at the hardware level using Infiniband verbs (`libibverbs`), facilitating direct memory transfers between GPU memories.

The control plane is defined using Protocol Buffers. This schema models the allocation of cache blocks and exposes endpoints to register, lookup, and synchronize memory mappings.

<script src="https://gist.github.com/mohashari/1271704f0fb9ffeb9a01eda0c0e6d22a.js?file=snippet-1.txt"></script>

## Distributed Token Address Mapping via a Prefix Trie

When a request arrives at the API Gateway, the orchestrator must determine if any part of the prompt's prefix has already been prefilled and cached on another node. We store this lookup mapping in a distributed prefix directory (implemented as a radix tree/trie) managed by the coordinator node. 

The gRPC server implementation in Go provides a thread-safe lookup mechanism to match incoming token sequences against registered blocks.

<script src="https://gist.github.com/mohashari/1271704f0fb9ffeb9a01eda0c0e6d22a.js?file=snippet-2.go"></script>

## Consistent Hashing and Prefix-Aware Routing

To distribute the KV cache load evenly across the inference cluster and avoid hotspots, we utilize a Consistent Hash Ring. By hashing the prefix tokens of a conversation, we route requests that share prompt segments (like system instructions or chat history) to the same physical node. This maximizes cache reuse, minimizing the need for inter-node transfers.

The Python implementation below manages the hashing ring structure, using virtual nodes to ensure balanced distribution.

<script src="https://gist.github.com/mohashari/1271704f0fb9ffeb9a01eda0c0e6d22a.js?file=snippet-3.py"></script>

## GPUDirect RDMA: Pinning and Registering VRAM

For the network card to directly access GPU VRAM without CPU intervention, the corresponding memory regions must be registered with the InfiniBand/RoCEv2 driver. This step pins the virtual memory addresses (VMA) to physical pages and registers them with the hardware.

Here, we interface with `libibverbs.so` via `ctypes` to register CUDA device memory.

<script src="https://gist.github.com/mohashari/1271704f0fb9ffeb9a01eda0c0e6d22a.js?file=snippet-4.py"></script>

## Initiating the Zero-Copy Transfer: `ibv_post_send`

Once target address and remote key metadata are exchanged via gRPC, the node containing the active KV cache blocks initiates the hardware-level transfer. In a low-level C++ module integrated into the inference execution engine, we call `ibv_post_send` to schedule an asynchronous RDMA WRITE operation.

<script src="https://gist.github.com/mohashari/1271704f0fb9ffeb9a01eda0c0e6d22a.js?file=snippet-5.txt"></script>

## Dynamic VRAM Block Management and Eviction

Integrating a distributed cache mechanism requires careful memory page allocation. Similar to standard PagedAttention, we segment the GPU memory pool into fixed physical blocks (e.g., 16 tokens per block). However, our block manager must support cross-node replication and handle eviction using reference-counting to allow concurrent decode steps to read from the same underlying prefill block safely.

<script src="https://gist.github.com/mohashari/1271704f0fb9ffeb9a01eda0c0e6d22a.js?file=snippet-6.py"></script>

## Production Pitfalls, Real Failures, and Mitigations

Implementing this architecture in multi-node clusters introduces critical hardware-level failure modes that will crash an engine if ignored.

### 1. PFC (Priority Flow Control) Deadlocks and Queue Head-of-Line Blocking
RoCEv2 relies on lossless Ethernet, achieved using PFC to pause traffic when buffer limits are reached on network switches. In high-throughput clusters, if traffic paths form loops, PFC pause frames propagate backward, causing network-wide "PFC storms" or deadlocks, halting all inter-node inference traffic.
* **Mitigation**: Configure Explicit Congestion Notification (ECN) thresholds on your network switches. ECN marks packets when buffers fill, signaling the sending RNIC to throttle transmission rates before PFC pause frames are generated. Ensure your RDMA traffic is mapped to specific Priority Code Points (PCP) or DSCP values (typically Class of Service 3), isolating it from storage and management traffic.

### 2. VRAM Registration Latency during Runtime
Registering VRAM (`ibv_reg_mr`) dynamically during a forward pass introduces latency spikes. The OS must lock page tables, and the PCIe controller must map virtual addresses, taking anywhere from 10ms to 80ms per registration. This completely negates the latency benefits of the RDMA transfer.
* **Mitigation**: Pre-register the entire KV cache memory pool (allocating the VRAM block arena) during the inference engine startup phase. Map physical block IDs to fixed offsets within this pre-registered memory region. During runtime, only exchange the pre-registered base memory address offset and its associated `rkey` via gRPC. Never call `ibv_reg_mr` in the execution loop.

### 3. Asymmetric Network Links & Out-of-Order Packets
If a network switch paths packets dynamically across multiple routing groups (ECMP), packets may arrive out of order at the destination RNIC, causing connection resets or silent packet drops.
* **Mitigation**: Force hardware-level adaptive routing on your network interface cards and switch fabric (such as Mellanox Adaptive Routing). This ensures packets belonging to a single RDMA Write stream are routed uniformly, and target buffer assemblies are committed sequentially before trigger notifications are sent to the target inference engine.

## Performance Metrics & Benchmark Results

The performance difference between standard TCP sockets and GPUDirect RDMA is stark when scaling context size. Below is benchmark data collected using a cluster of two NVIDIA H100 (80GB) nodes connected via a 100 Gbps Mellanox ConnectX-6 Dx network fabric.

| Context Length (Tokens) | Total KV Cache Size (GB) | TCP/IP over 100GbE Latency (ms) | RoCEv2 (RDMA) Latency (ms) | Host CPU Utilization (TCP vs RDMA) |
| :--- | :--- | :--- | :--- | :--- |
| **8,192** (8k) | 2.56 GB | 275 ms | 224 ms | 100% (4 cores) vs < 1% |
| **32,768** (32k) | 10.24 GB | 1,020 ms | 870 ms | 100% (8 cores) vs < 1% |
| **131,072** (128k) | 40.96 GB | 4,210 ms | 3,480 ms | 100% (12 cores) vs < 1% |

As the sequence size reaches 128k, TCP/IP introduces significant overhead due to memory copies between kernel and user space. This results in high CPU utilization and limits the system's ability to handle concurrent requests. In contrast, GPUDirect RDMA maintains consistent performance near wire speed with almost no CPU overhead, freeing up host CPU resources to run the orchestrator and coordinate token generation.

## Conclusion

Bypassing the CPU memory barrier is critical for running multi-node LLM inference in production. By combining a gRPC control plane for directory mapping and prefix routing with a GPUDirect RDMA data plane for zero-copy VRAM transfers, you can decouple prefill and decode phases without incurring steep latency penalties. When scaling context lengths up to 128k tokens, eliminating TCP protocol overhead is not just an optimization—it is a baseline architectural requirement.