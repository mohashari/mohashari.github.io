---
layout: post
title: "Implementing Real-Time Tensor-Parallelism Communication Over RDMA (RoCEv2) for Distributed LLM Inference"
date: 2026-07-24 08:00:00 +0700
tags: [ai-engineering, rdma, distributed-systems, cuda]
description: "A production-grade guide to optimizing distributed LLM inference latency by bypassing CPU-staged buffers using GPUDirect RDMA over RoCEv2."
image: "/images/diagrams/implementing-real-time-tensor-parallelism-rdma-rocev2-distributed-llm-inference.svg"
thumbnail: "/images/diagrams/implementing-real-time-tensor-parallelism-rdma-rocev2-distributed-llm-inference.svg"
---

At scale, distributed LLM inference is not compute-bound—it is communication-bound. When partitioning a model like Llama 3.1 70B or Mixtral 8x22B across multiple GPU nodes using Megatron-style tensor parallelism, every single transformer layer demands a synchronized All-Reduce operation across the network. If your system relies on standard TCP/IP sockets to route these tensor activations, you will watch your GPU utilization collapse below 15% as CUDA kernels sit idle, waiting on kernel space-to-user space copies, TCP stack serialization, and CPU-staged host memory copies. To hit the latency targets required for real-time interactive generation (under 15ms per token), you must bypass the host CPU entirely and leverage GPUDirect RDMA over RoCEv2 (RDMA over Converged Ethernet) to enable direct GPU-to-GPU memory copies directly through the PCIe bus and network adapter.

![Implementing Real-Time Tensor-Parallelism Communication Over RDMA (RoCEv2) for Distributed LLM Inference Diagram](/images/diagrams/implementing-real-time-tensor-parallelism-rdma-rocev2-distributed-llm-inference.svg)

## The Anatomy of Tensor Parallelism Bottlenecks

In tensor-parallel (TP) execution, weight matrices of the Self-Attention block and Multi-Layer Perceptron (MLP) block are split across multiple GPUs. For column-parallel linear layers, no inter-GPU communication is required during the forward pass. However, row-parallel linear layers depend on accumulating intermediate activation vectors before adding biases. This accumulation is a mathematical All-Reduce operation. 

For a model partitioned across multiple nodes (e.g., TP=8 where 8 GPUs reside on different physical servers), every forward pass of a single transformer layer requires:
1. An All-Reduce barrier immediately after the Self-Attention output projection.
2. A second All-Reduce barrier immediately after the MLP output projection.

A model like Llama 3 70B has 80 layers, which translates to exactly 160 All-Reduce barriers per forward pass. During the token-generation (decoding) phase, the tensor size to reduce is small. For a batch size of 16 and a hidden dimension of 8192 at FP16 precision:

$$\text{Tensor Size} = 16 \times 1 \times 8192 \times 2 \text{ bytes} = 262,144 \text{ bytes } (256 \text{ KB})$$

If we route this 256 KB All-Reduce over a 100 GbE network using standard TCP/IP, the TCP stack traversal latency (traversing the socket layer, OS kernel network driver, and executing a double copy to host RAM) introduces 50 to 100 microseconds of overhead per transfer. Over 160 layers, the network latency component translates to:

$$\text{Communication Latency} = 160 \times 70 \mu\text{s} = 11.2\text{ ms}$$

Given that the raw compute time for generating a single token on an NVIDIA H100 Tensor Core GPU is roughly 8 to 12ms, spending an additional 11.2ms on serialization and network stack traversal cuts your system throughput in half.

By implementing GPUDirect RDMA over RoCEv2, the same 256 KB All-Reduce achieves latency of less than 3 microseconds per transfer. The data packet travels directly from GPU 0's HBM3 memory across the local PCIe bus, through the ConnectX-7 Host Channel Adapter (HCA), over the RoCEv2 fabric, and directly into the memory space of GPU 1. The host CPU and operating system kernel are completely bypassed.

## RoCEv2 vs. TCP/IP in GPU Clusters

RoCEv2 (RDMA over Converged Ethernet version 2) encapsulates InfiniBand transport packets inside standard UDP/IP packets (destination port 4791). This allows RDMA to be routed over standard IP networks. The key differentiators of RoCEv2 compared to TCP/IP in AI engineering are:

- **Kernel Bypass**: Applications post network transfer requests (Work Requests) directly to the hardware queues of the HCA. The operating system kernel is completely bypassed during transmission, eliminating context switches.
- **Zero-Copy**: The network adapter performs direct memory access (DMA) to and from application memory buffers (GPU VRAM or pinned Host RAM) without staging data in intermediate kernel-space or user-space buffers.
- **Hardware-Level Transport**: Flow control, packet acknowledgment, and retransmission are handled in the HCA hardware silicon rather than by the CPU execution stack.

However, RoCEv2 lacks the dynamic windowing and congestion-avoidance algorithms inherent in TCP. When network congestion occurs and switches drop packets, RoCEv2 relies on Go-Back-N retransmission at the hardware layer. This results in devastating tail-latency spikes if the physical network fabric is not configured to be lossless.

## Configuring the Lossless Hardware Fabric: PFC and ECN

To achieve low-latency GPU-to-GPU data paths over Ethernet, you must configure the physical network to be lossless. This requires configuring Priority Flow Control (PFC - IEEE 802.1Qbb) and Explicit Congestion Notification (ECN - RFC 3168).

PFC operates at Layer 2. When a switch port buffer reaches a specific fill threshold, the switch sends a pause frame upstream to the transmitting port, halting transmission on a specific traffic class. ECN operates at Layer 3. When switch buffers begin to build up, the switch marks the Congestion Encountered (CE) bits in the IP header of transit packets. The receiver observes this mark and sends a Congestion Notification Packet (CNP) back to the sender, prompting the sender to throttle its transmission rate according to the Data Center Quantized Congestion Control (DCQCN) algorithm.

If PFC is enabled without ECN, a congested hot-spot on a single switch port will propagate pause frames backward through the entire fabric, causing a "PFC storm" and paralyzing unrelated traffic (congestion spreading). 

The following production script [configure_roce.sh](file:///home/muklis/Documents/exploring/blog/scripts/configure_roce.sh) sets up the host kernel parameters, configures PFC on traffic class 3, and maps RoCEv2 packets with a DSCP value of 26:

<script src="https://gist.github.com/mohashari/b4a1a31ea3624b09d15c86a59fb6c303.js?file=snippet-1.sh"></script>

## GPUDirect RDMA: Memory Pinning and Registration

GPUDirect RDMA requires the network interface card to directly read and write CUDA virtual memory over the PCIe bus. Under standard operations, the virtual addresses allocated on the GPU cannot be directly accessed by the HCA. To enable this, we rely on CUDA Unified Virtual Addressing (UVA) and the `nvidia-peermem` kernel module.

The `nvidia-peermem` module acts as a bridge between the NVIDIA driver and the InfiniBand Core kernel module (`ib_core`). When an application calls `ibv_reg_mr` (Register Memory Region) with a CUDA virtual memory pointer, the InfiniBand driver queries `nvidia-peermem` to pin the underlying physical memory pages on the GPU, returning physical PCIe Bus Master addresses (BAR1 space) directly to the network card.

The C++ source file [gpudirect_mr.cpp](file:///home/muklis/Documents/exploring/blog/src/gpudirect_mr.cpp) demonstrates how to allocate GPU memory and register it with the HCA protection domain using [register_gpu_memory_with_hca](file:///home/muklis/Documents/exploring/blog/src/gpudirect_mr.cpp#L18):

<script src="https://gist.github.com/mohashari/b4a1a31ea3624b09d15c86a59fb6c303.js?file=snippet-2.txt"></script>

## Initializing RoCEv2 Queue Pairs for GPU Communication

Data transport in InfiniBand is managed via Queue Pairs (QPs). To set up communication between Rank 0 and Rank 1, we must create a Reliable Connection (RC) Queue Pair on each node and transition them through three states: `RESET` $\to$ `INIT` $\to$ `RTR` (Ready to Receive) $\to$ `RTS` (Ready to Send).

For RoCEv2, the transition to `RTR` requires configuring the Global Routing Header (GRH). Because RoCEv2 operates over Layer 3, the HCA must encapsulate the InfiniBand payload in a UDP/IP header. We pass the local source GID index (`sgid_index`) and the destination GID (which holds the remote node's IP address). Crucially, the `traffic_class` field in the GRH must be populated with the mapped priority value `(DSCP << 2)` to ensure the switches honor the PFC policy.

The C++ source file [roce_qp.cpp](file:///home/muklis/Documents/exploring/blog/src/roce_qp.cpp) details this initialization flow inside [transition_qp_to_rts](file:///home/muklis/Documents/exploring/blog/src/roce_qp.cpp#L7):

<script src="https://gist.github.com/mohashari/b4a1a31ea3624b09d15c86a59fb6c303.js?file=snippet-3.txt"></script>

## Performing Zero-Copy Direct GPU Writes (RDMA Write with Immediate)

For latency-sensitive distributed LLM inference, we use the `RDMA WRITE WITH IMMEDIATE` operation (`IBV_WR_RDMA_WRITE_WITH_IMM`). 

Unlike a raw `RDMA WRITE` (which executes silently without notifying the receiver's CPU), a write with immediate pushes a 32-bit payload directly into the receiver's Completion Queue (CQ). This triggers a local interrupt on the receiver or unblocks a polling thread. The receiver is immediately notified that the data transfer has completed and can instantly invoke the subsequent CUDA kernel. This method guarantees that we never waste GPU cycles polling memory markers or stall on slow, CPU-driven message passing.

The C++ source file [rdma_write.cpp](file:///home/muklis/Documents/exploring/blog/src/rdma_write.cpp) implements this logic in [post_gpudirect_rdma_write](file:///home/muklis/Documents/exploring/blog/src/rdma_write.cpp#L12):

<script src="https://gist.github.com/mohashari/b4a1a31ea3624b09d15c86a59fb6c303.js?file=snippet-4.txt"></script>

## PyTorch NCCL Configuration for RoCEv2 in Production

While custom C++ bindings are standard for highly optimized inference engines like TensorRT-LLM or vLLM custom paged-attention kernels, PyTorch configurations rely heavily on the NVIDIA Collective Communications Library (NCCL) backend. NCCL manages all raw ibverbs and GPUDirect connections under the hood.

To force NCCL to use GPUDirect RDMA over RoCEv2 instead of falling back to TCP/IP, you must configure key environment variables before initializing the distributed backend:

- `NCCL_IB_DISABLE=0`: Forces the use of InfiniBand/RDMA interfaces.
- `NCCL_IB_GID_INDEX=3`: Selects the specific GID entry on the network adapter corresponding to RoCEv2.
- `NCCL_IB_TC=104`: Sets the DSCP value to 26 in the outgoing UDP headers.
- `NCCL_NET_GDR_LEVEL=5`: Forces GPUDirect RDMA across the network fabric (`NET`), bypassing host system RAM entirely.

The Python script [torch_nccl_roce.py](file:///home/muklis/Documents/exploring/blog/src/torch_nccl_roce.py) configures the environment and executes a distributed All-Reduce on a mock hidden state tensor using [run_tensor_parallel_allreduce](file:///home/muklis/Documents/exploring/blog/src/torch_nccl_roce.py#L38):

```python
# snippet-5
import os
import torch
import torch.distributed as dist

def init_nccl_rocev2_environment(local_rank: int, world_size: int, master_addr: str, master_port: str):
    """
    Sets up environment variables to force PyTorch and NCCL to run
    exclusively over GPUDirect RDMA over RoCEv2.
    """
    # 1. Base distributed coordinates
    os.environ['MASTER_ADDR'] = master_addr
    os.environ['MASTER_PORT'] = master_port
    os.environ['RANK'] = str(local_rank)
    os.environ['WORLD_SIZE'] = str(world_size)
    os.environ['LOCAL_RANK'] = str(local_rank)

    # 2. Force NCCL to use InfiniBand/RDMA transport instead of socket TCP
    os.environ['NCCL_IB_DISABLE'] = '0'
    
    # 3. Restrict NCCL to use specific HCA interfaces. E.g., Mellanox ConnectX-7 cards
    # correspond to devices starting with 'mlx5_'. Modify this based on your lspci output.
    os.environ['NCCL_IB_HCA'] = 'mlx5_0:1,mlx5_1:1'
    
    # 4. GID index determines the RoCE version. 
    # Usually GID index 3 or 5 corresponds to RoCEv2 (IPv4/UDP) on Mellanox NICs.
    # To find the exact index, run: show_gids | grep RoCEv2
    os.environ['NCCL_IB_GID_INDEX'] = '3'
    
    # 5. Map RoCEv2 traffic class to match physical switch PFC settings.
    # Traffic class (tc) 104 matches DSCP 26 (CS3) for switch-level Priority Flow Control
    # Format: DSCP << 2 => 26 << 2 = 104
    os.environ['NCCL_IB_TC'] = '104'
    
    # 6. Enable GPUDirect RDMA at the highest level (direct GPU-to-GPU bypass via PCIe)
    # GDR levels: 0 (disabled), 1 (SYS - staged through system memory), 
    # 2 (PHB - cross PCIe switch host bridge), 3 (PIX - same PCIe switch), 5 (NET - remote RDMA)
    os.environ['NCCL_NET_GDR_LEVEL'] = '5'
    
    # 7. Enable debugging to trace physical mapping failures in logs
    os.environ['NCCL_DEBUG'] = 'INFO'
    os.environ['NCCL_DEBUG_SUBSYS'] = 'INIT,COLL,ENV,NET'

def run_tensor_parallel_allreduce(local_rank: int, world_size: int):
    # Initialize the device
    device = torch.device(f"cuda:{local_rank}")
    torch.cuda.set_device(device)
    
    # Initialize process group using NCCL collective library
    dist.init_process_group(
        backend="nccl", 
        init_method="env://",
        rank=local_rank,
        world_size=world_size
    )
    
    if local_rank == 0:
        print("=== NCCL process group initialized successfully ===")
        print(f"Active NCCL variables: GID_INDEX={os.environ['NCCL_IB_GID_INDEX']}, TC={os.environ['NCCL_IB_TC']}")
    
    # Emulate a Tensor Parallelism hidden state vector (e.g. Llama 3 hidden_dim = 8192)
    # Allocated in CUDA High Bandwidth Memory (HBM)
    hidden_dimension = 8192
    batch_size = 16
    
    local_tensor = torch.ones((batch_size, hidden_dimension), dtype=torch.float16, device=device)
    # Assign distinct values to differentiate ranks
    local_tensor *= (local_rank + 1)
    
    print(f"Rank {local_rank} - Local Tensor Sum (Before All-Reduce): {local_tensor.sum().item()}")
    
    # Synchronize and perform All-Reduce (Sum)
    # Behind the scenes, NCCL runs the ring or tree algorithm over GPUDirect RDMA
    dist.barrier()
    
    # Measure latency of the collective operation
    start_event = torch.cuda.Event(enable_timing=True)
    end_event = torch.cuda.Event(enable_timing=True)
    
    start_event.record()
    dist.all_reduce(local_tensor, op=dist.ReduceOp.SUM)
    end_event.record()
    
    torch.cuda.synchronize()
    latency_ms = start_event.elapsed_time(end_event)
    
    # Expected output is Rank 0 + Rank 1: (1 + 2) = 3 per element
    expected_sum = (world_size * (world_size + 1) / 2) * batch_size * hidden_dimension
    print(f"Rank {local_rank} - Reduced Tensor Sum (After All-Reduce): {local_tensor.sum().item()}")
    print(f"Rank {local_rank} - Collective All-Reduce Latency: {latency_ms:.4f} ms")
    
    dist.destroy_process_group()

if __name__ == "__main__":
    # Example execution entrypoint (typically run via torchrun)
    # torchrun --nproc_per_node=2 torch_nccl_roce.py
    import sys
    if len(sys.argv) < 3:
        # Default mock ranks for illustration
        rank = 0
        w_size = 2
    else:
        rank = int(sys.argv[1])
        w_size = int(sys.argv[2])
        
    init_nccl_rocev2_environment(
        local_rank=rank, 
        world_size=w_size, 
        master_addr="10.0.0.1", 
        master_port="29500"
    )
    # In practice, ensure CUDA is available and you run in a clustered environment
    if torch.cuda.is_available():
        run_tensor_parallel_allreduce(rank, w_size)
    else:
        print("CUDA not available. RDMA simulation requires physical GPU hardware.")
```

## Real-World Failure Modes and Diagnostics

When running multi-node tensor parallelism over RoCEv2 in production, you will encounter three common failure modes:

### 1. PFC Storms and Head-of-Line Blocking
**Symptoms**: Inference token latency spikes from 15ms to over 800ms. GPU utilization drops to near zero across the entire cluster.
**Root Cause**: A switch queue buffer overflowed, triggering PFC pause frames. Due to missing or misconfigured ECN settings on the hosts (`cc_params/ecn_enable` set to 0), the sender failed to throttle its transmission rate. The switch continued to issue pause frames upstream, cascading the congestion back to the source NICs and halting all traffic in that queue priority across unrelated nodes.
**Remedy**: Ensure ECN is enabled on all interfaces and that switch ECN marking thresholds are set to trigger before the PFC pause watermarks.

### 2. GID Index Mismatches
**Symptoms**: NCCL initialization fails with `Call to connect returned Connection refused` or silent fallback to TCP/IP.
**Root Cause**: The environment variable `NCCL_IB_GID_INDEX` is set to a GID that corresponds to RoCEv1 (which works strictly at Layer 2 and cannot route across subnets) or an inactive port GID.
**Remedy**: Run the `show_gids` utility to verify which GID index corresponds to RoCEv2 (look for the GID mapped with UDP/IPv4 or IPv6 support, usually index 3 or 5).

### 3. GPUDirect RDMA Level Downgrade
**Symptoms**: NCCL debug logs report `NCCL INFO NET/IB : Using GDRDMAs=0` or downgrade the path level to `SYS` (System memory staging).
**Root Cause**: Access Control Services (ACS) is enabled on the PCIe controller in the host BIOS. ACS blocks Peer-to-Peer DMA transactions between PCIe endpoints, forcing all transfers to route through the CPU Root Complex. Alternatively, the GPU and NIC are mounted on different PCIe root complex switches without support for Peer-to-Peer routing.
**Remedy**: Disable ACS in the BIOS settings of your GPU nodes. Check the physical topology using `nvidia-smi topo -m` and verify that the path between the target GPU and target HCA is marked as `PIX` (traverse single PCIe switch) or `PHB` (traverse host bridge), not `SYS`.

The following diagnostics script [diagnose_roce.sh](file:///home/muklis/Documents/exploring/blog/scripts/diagnose_roce.sh) can be used to troubleshoot these network and topology issues:

<script src="https://gist.github.com/mohashari/b4a1a31ea3624b09d15c86a59fb6c303.js?file=snippet-6.sh"></script>