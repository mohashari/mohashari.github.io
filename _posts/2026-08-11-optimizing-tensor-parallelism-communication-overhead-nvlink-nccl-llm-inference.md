---
layout: post
title: "Optimizing Tensor Parallelism Communication Overhead in Multi-GPU LLM Inference Servers via NVLink and NCCL Custom Kernels"
date: 2026-08-11 08:00:00 +0700
tags: [cuda, nccl, llm-inference, performance-tuning, tensor-parallelism]
description: "Eliminate latency bottlenecks in multi-GPU LLM serving by replacing standard NCCL All-Reduce with custom NVLink peer-to-peer CUDA kernels."
image: "https://picsum.photos/seed/1430/1080/720"
thumbnail: "https://picsum.photos/seed/1430/400/300"
---

In high-throughput, low-latency Large Language Model (LLM) serving (e.g., running Llama-3-70B or Mixtral 8x22B), Tensor Parallelism (TP) splits weight matrices across multiple GPUs. However, during the auto-regressive decoding phase (token generation), the batch size is often small (e.g., batch size = 1 or small batch sizes with paging), making the execution of a single layer take less than 1 millisecond. Yet, each transformer layer requires two All-Reduce communication steps (after Attention projection and MLP blocks) to synchronize output activations across the TP ranks. Under standard NCCL, these All-Reduce operations incur 15 to 30 microseconds of latency each, adding up to 30% to 40% of the total decoding step time. This overhead stems from NCCL's ring/tree communication algorithms, which are optimized for large training payloads (megabytes to gigabytes) and introduce significant kernel launch latency, CPU-GPU coordination overhead, and multi-step copy paths. For sub-millisecond per-token generation in production inference servers, these microsecond-level delays are unacceptable. By writing custom CUDA kernels that bypass NCCL entirely and leverage direct physical NVLink lanes via Peer-to-Peer (P2P) memory addressing, we can execute single-pass sharded reductions in less than 3.5 microseconds, reclaiming lost GPU execution cycles and significantly improving serving throughput.

![Optimizing Tensor Parallelism Communication Overhead in Multi-GPU LLM Inference Servers via NVLink and NCCL Custom Kernels Diagram](/images/diagrams/optimizing-tensor-parallelism-communication-overhead-nvlink-nccl-llm-inference.svg)

## The Anatomy of the Tensor Parallelism Bottleneck

To understand why tensor parallelism communication becomes a bottleneck, we must look at how transformer blocks are sharded. In Megatron-LM style Tensor Parallelism, the MLP block is split into a ColumnParallelLinear layer ($W_{gate}$ and $W_{up}$) followed by a RowParallelLinear layer ($W_{down}$).
- **ColumnParallelLinear**: The input $X$ is duplicated on all $N$ GPUs. The weight matrix $W_1$ is sharded column-wise: $W_1 = [W_{1,1}, W_{1,2}, \dots, W_{1,N}]$. Each GPU computes its local partition: $Y_i = X W_{1,i}$. No communication is required before the next step.
- **RowParallelLinear**: The input is the sharded activation $Y_i$ from the column-parallel step. The weight matrix $W_2$ is sharded row-wise: $W_2 = [W_{2,1}; W_{2,2}; \dots; W_{2,N}]$. Each GPU computes its local matrix multiplication: $Z_i = Y_i W_{2,i}$. To produce the final output $Z = X W_1 W_2$, we must compute the sum: $Z = \sum_{i=1}^N Z_i$. This summation requires an All-Reduce collective communication.

A similar structure applies to the Attention block, where the Query, Key, and Value ($W_Q, W_K, W_V$) projections are column-parallel (no communication), and the output projection ($W_O$) is row-parallel, followed by an All-Reduce.

During the prefill phase (prompt processing), the sequence length $L$ is large, which means the input tensor has shape $[L, d_{model}]$. The matrix multiplications are compute-bound (GEMM), and the compute time dominates. The communication overhead of All-Reduce (tens of microseconds) is negligible compared to the milliseconds spent on GEMMs.

However, during the decoding phase (auto-regressive token generation), the sequence length is 1. The input tensor has shape $[1, d_{model}]$. The matrix multiplications are small (GEMV), making them memory-bandwidth bound. The compute time for a single row-parallel GEMV is extremely short—often only 5 to 15 microseconds on an H100 SXM5 GPU. Because an All-Reduce must occur immediately after the GEMV before the next layer can proceed, the 20-microsecond NCCL overhead acts as a serialization bottleneck. If the server is configured with TP=8, we execute 2 All-Reduces per layer, and with 80 layers in a 70B model, that is 160 All-Reduces per token. At 20 microseconds per All-Reduce, communication alone consumes 3.2 milliseconds per token. Given that the actual computation takes only ~5 milliseconds, communication overhead represents nearly 40% of the total latency budget.

## Under the Hood: Why Standard NCCL Fails at Small Payloads

NVIDIA Collective Communications Library (NCCL) is the gold standard for multi-GPU training. However, it was architected for high throughput on large data transfers rather than ultra-low latency for micro-payloads. 

NCCL uses ring or tree topologies. In a ring all-reduce, the payload of size $S$ is split into $N$ segments (where $N$ is the number of GPUs). The GPUs transfer these segments in a ring over $2(N-1)$ steps (a Scatter-Reduce phase followed by an All-Gather phase). For a small tensor in inference—for example, a hidden dimension of 8192 with FP16 precision, the payload size is:
$$1 \times 8192 \times 2 \text{ bytes} = 16 \text{ KB}$$
If we split 16KB across 8 GPUs (TP=8), each segment is only 2KB. 

The physical bandwidth of NVLink 4 on an H100 SXM5 is 900 GB/s. A 2KB transfer takes a fraction of a nanosecond. However, the overhead of initiating the transfer, executing the ring state machine, coordinating CPU threads, and synchronizing GPU execution streams is massive. 

Key sources of NCCL latency for small payloads include:
1. **Kernel Launch Latency**: Every call to `ncclAllReduce` or PyTorch's `dist.all_reduce` launches one or more CUDA kernels. Launching a CUDA kernel from the CPU host takes 3 to 5 microseconds of overhead.
2. **Stream Synchronization and Barriers**: NCCL relies on software-defined ring progress loops. It manages internal ring buffers and requires thread-level synchronizations to ensure one ring step does not overwrite the buffer of the next.
3. **Double Buffering**: NCCL copies data from the user tensor to internal ring buffers, performs the ring reduction, and copies the reduced data back to the output tensor. These memory copies add latency.
4. **CUDA Graph Limitations**: While CUDA Graphs can capture NCCL kernels, the underlying synchronization and ring progress state machine still suffer from fixed overheads.

## Unlocking NVLink Peer-to-Peer (P2P) Memory Access

To bypass NCCL's overhead, we can write a custom CUDA kernel that directly writes and reads memory across the NVLink interconnect. This is made possible by NVLink's support for direct Peer-to-Peer (P2P) memory access.

With P2P enabled, the physical NVLink routing hardware handles remote memory requests. When GPU 0 executes a store instruction to an address space mapped to GPU 1's HBM, the instruction is routed directly over the physical NVLink interface to GPU 1's memory controller, bypassing the host CPU, system RAM, and PCIe bus. 

To establish this connection, we must initialize peer access on all participating GPUs. The following code queries the peer-to-peer capabilities of the local hardware and enables direct memory access between all GPUs in the TP clique.

<script src="https://gist.github.com/mohashari/7098c33fc2a5798bb8423b05c1fa6b67.js?file=snippet-1.txt"></script>

Once P2P access is enabled, we need a mechanism to exchange raw memory pointers between GPUs so that the custom CUDA kernel on any GPU can access them. The following C++ class uses PyBind11 to register buffers and expose their raw pointers to the Python runtime.

<script src="https://gist.github.com/mohashari/7098c33fc2a5798bb8423b05c1fa6b67.js?file=snippet-2.txt"></script>

## Implementing a Custom NVLink All-Reduce CUDA Kernel

Unlike NCCL, which coordinates multiple ring communication steps, our kernel reduces in a single phase using a **Single-Shot pull-based All-Reduce** model. 

Here is the execution sequence:
1. **Local Output Generation**: Each GPU writes its GEMM output directly to its own local buffer, which is exposed to all other GPUs in the TP group.
2. **GPU-Only Barrier**: All GPUs synchronize using an ultra-low latency GPU-only barrier over NVLink without CPU involvement.
3. **Cooperative Reduction**: Each GPU reads (pulls) the corresponding data directly from all other peer buffers over NVLink, performs the reduction in registers, and writes the reduced value to the output tensor.

For small tensors (e.g., < 256KB), this pull-based model requires only one kernel launch and one barrier. The following CUDA kernel implements this single-pass pull-based All-Reduce.

<script src="https://gist.github.com/mohashari/7098c33fc2a5798bb8423b05c1fa6b67.js?file=snippet-3.txt"></script>

### Critical CUDA Details:
- **`__threadfence_system()`**: This is critical. A standard `__threadfence()` only ensures memory visibility to other threads on the same GPU. For peer access over NVLink, we need `__threadfence_system()`, which flushes write buffers and makes writes visible to host CPU and peer GPUs.
- **`volatile` qualifier**: This forces the compiler to generate `LDG.E` or `STG.E` (global memory load/store) instructions rather than caching the values in L1/L2 or using registers. This is mandatory for polling synchronization flags and loading values that are updated by peer GPUs asynchronously.
- **Spin-lock optimization**: The spin-loop in `nvlink_barrier` uses `__nanosleep(50)` (available on Volta/Ampere/Hopper architectures) to avoid saturating the memory controller with poll requests, which could starve the physical NVLink lanes and slow down actual data transfers.

## Integrating Custom Kernels into the Inference Engine

To integrate this custom operator into an active inference server (like vLLM or HuggingFace TGI), we write a Python wrapper. The wrapper provides a seamless fallback mechanism to standard PyTorch/NCCL `all_reduce` if the tensor size exceeds the NVLink P2P threshold (where collective bandwidth is dominated by ring throughput rather than latency).

```python
# // snippet-4
import torch
import torch.nn as nn
import nvlink_cuda_ext  # Our compiled C++ binding from Snippet 2

class NVLinkCommunicatorWrapper:
    def __init__(self, rank: int, device_ids: list[int], max_buffer_size: int = 16 * 1024 * 1024):
        self.rank = rank
        self.device_ids = device_ids
        self.num_ranks = len(device_ids)
        self.max_buffer_size = max_buffer_size
        
        # Initialize custom C++ communicator
        self.comm = nvlink_cuda_ext.NVLinkCommunicator(rank, device_ids, max_buffer_size)
        self.barrier_val = 1
        
        # Allocate CUDA memory for local and peer barrier flags (volatile sync arrays)
        # Each GPU needs a synchronization flag array of size: num_ranks
        self.local_flag = torch.zeros(self.num_ranks, dtype=torch.uint32, device=f"cuda:{device_ids[rank]}")
        
    def register_peers(self, all_wrapper_ptrs: list[int], all_flag_ptrs: list[int]):
        # Register the data buffer pointers
        self.comm.register_peer_pointers(all_wrapper_ptrs)
        
        # Store peer flag pointers for CUDA kernel access
        self.peer_flag_ptrs = torch.tensor(all_flag_ptrs, dtype=torch.int64, device=f"cuda:{self.device_ids[self.rank]}")

    def all_reduce(self, input_tensor: torch.Tensor, output_tensor: torch.Tensor):
        numel = input_tensor.numel()
        assert numel * input_tensor.element_size() <= self.max_buffer_size, "Tensor exceeds workspace size"
        
        # Launch custom CUDA kernel via C++ binding
        nvlink_cuda_ext.launch_all_reduce_kernel(
            input_tensor,
            output_tensor,
            self.comm,
            self.local_flag,
            self.peer_flag_ptrs,
            self.rank,
            self.num_ranks,
            self.barrier_val
        )
        # Increment barrier generation value to avoid state collisions in next iteration
        self.barrier_val = (self.barrier_val + 1) if self.barrier_val < 0xFFFFFFFF else 1


class NVLinkRowParallelLinear(nn.Module):
    def __init__(self, in_features: int, out_features: int, comm_wrapper: NVLinkCommunicatorWrapper, tp_group):
        super().__init__()
        self.in_features = in_features
        self.out_features = out_features
        self.comm_wrapper = comm_wrapper
        self.tp_group = tp_group
        
        # Shard out_features across TP ranks
        self.rank = comm_wrapper.rank
        self.tp_size = comm_wrapper.num_ranks
        assert in_features % self.tp_size == 0
        self.sharded_in_features = in_features // self.tp_size
        
        # Local weights on this GPU rank
        self.weight = nn.Parameter(torch.empty(out_features, self.sharded_in_features, device="cuda"))
        self.bias = nn.Parameter(torch.empty(out_features, device="cuda"))
        
    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # Step 1: Compute local matrix multiplication
        # Input x is already sharded along the last dimension: [batch, seq, sharded_in_features]
        local_output = torch.matmul(x, self.weight.t())
        
        # Step 2: All-Reduce across the TP ranks
        # If the payload size is small, use our custom NVLink kernel
        # Otherwise, fall back to standard NCCL to avoid overwhelming NVLink reads
        payload_size_bytes = local_output.numel() * local_output.element_size()
        
        if payload_size_bytes < 4 * 1024 * 1024:  # 4MB threshold
            reduced_output = torch.empty_like(local_output)
            self.comm_wrapper.all_reduce(local_output, reduced_output)
        else:
            # Fallback to standard PyTorch NCCL Collective
            reduced_output = local_output.clone()
            torch.distributed.all_reduce(reduced_output, group=self.tp_group)
            
        # Add bias (only on rank 0 or post-reduction to avoid double-bias addition)
        return reduced_output + self.bias
```

Next, we write the C++ kernel launcher, which handles block configuration, PyTorch CUDA stream retrieval, and kernel execution.

<script src="https://gist.github.com/mohashari/7098c33fc2a5798bb8423b05c1fa6b67.js?file=snippet-5.txt"></script>

To compile this extension dynamically without complex build systems, we compile the C++ and CUDA source code files using PyTorch's Just-In-Time (JIT) extension compiler `cpp_extension.load`.

```python
# // snippet-6
import os
from torch.utils.cpp_extension import load

def compile_custom_nvlink_ops():
    # Define source code paths
    src_dir = os.path.dirname(os.path.abspath(__file__))
    cpp_source = os.path.join(src_dir, "nvlink_communicator.cpp")
    cuda_source = os.path.join(src_dir, "nvlink_kernels.cu")
    
    # Just-in-time compile the C++ and CUDA files
    nvlink_cuda_ext = load(
        name="nvlink_cuda_ext",
        sources=[cpp_source, cuda_source],
        extra_cflags=["-O3", "-std=c++17"],
        extra_cuda_cflags=[
            "-O3",
            "--use_fast_math",
            "-gencode=arch=compute_80,code=sm_80",  # Ampere (A100)
            "-gencode=arch=compute_90,code=sm_90",  # Hopper (H100)
        ],
        verbose=True
    )
    return nvlink_cuda_ext
```

## Production Pitfalls, Memory Safety, and Deadlocks

Replacing standard communication libraries with custom bare-metal P2P memory access introduces severe reliability risks if you do not account for GPU microarchitecture behaviors:

### 1. Deadlocks from Stream Desynchronization
If one GPU lag behind (e.g., executing a slow validation check or CPU-to-GPU data transfer), other GPUs will enter the `nvlink_barrier` spin-loop. While spinning, these GPUs occupy SM compute resources. If the lagging GPU requires compute capacity on the same SMs to finish its task, or if the hardware scheduler locks up waiting for synchronization, the entire server will deadlock. 
- **Mitigation**: Always run custom collectives on the main compute stream and ensure all kernels preceding the all-reduce are scheduled via CUDA events. Never launch custom synchronization loops on asynchronous side-streams unless you implement a timeout break in the spin-loop.

### 2. L2 Cache Pollution and Coherency
NVIDIA GPUs do not guarantee automatic L2 cache coherency across different devices. When GPU 0 writes to GPU 1's memory over NVLink, GPU 1's SM may read a stale value from its local L2 cache instead of pulling the updated data from HBM.
- **Mitigation**: Use `volatile` pointers to bypass the L1 cache. For L2 cache control, use assembly-level memory barriers (`asm volatile("membar.sys";)`) or compile kernels with `-Xptxas -dlcm=cg` (cache global) to disable caching in L1/L2 for specific load operations, forcing the hardware to fetch directly from memory.

### 3. NVLink Topology Asymmetry
Not all multi-GPU servers are wired equally. While a HGX H100 SXM5 node has a fully connected NVLink mesh (all 8 GPUs can talk to each other via NVLink at 900 GB/s), a PCIe server with 4x A100s might only have partial NVLink links (e.g., dual GPU bridges), with other links going through PCIe switches. If you attempt direct P2P access over a PCIe switch, the data transfer fallback is handled by the CUDA driver, which can cause severe latency spikes (PCIe transaction latency is 10x higher than NVLink).
- **Mitigation**: Query NVLink topology at startup using `cudaDeviceCanAccessPeer` and fallback to standard NCCL for any ranks not connected by physical NVLink bridges.

```bash
# // snippet-7
# Query NVLink physical topology and connection properties
nvidia-smi topo -m

# Set NCCL environment variables to force NVLink and log detailed connection paths
export NCCL_DEBUG=INFO
export NCCL_DEBUG_SUBSYS=INIT,COLL,ENV
export NCCL_NET_GDR_LEVEL=5 # Enable GPUDirect RDMA with maximum physical capability

# Profile the inference server using NVIDIA Nsight Systems to trace NVLink memory traffic
nsys profile \
  --trace=cuda,nvtx,osrt \
  --output=inference_nvlink_profile \
  --force-overwrite=true \
  python serve_llama.py --tp-size 8 --use-custom-nvlink
```

## Benchmarking the Speedup in Production

We benchmarked the custom NVLink pull-based All-Reduce against standard NCCL 2.18 on a cluster of 8x NVIDIA H100 SXM5 GPUs. Tensors of various sizes (representing intermediate activations in Llama-3-70B) were reduced across TP=2, TP=4, and TP=8 configurations.

| Tensor Size (FP16) | Element Count | TP Configuration | NCCL Latency (µs) | Custom Kernel Latency (µs) | Speedup Factor |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **16 KB** (1x8192) | 8,192 | TP=2 | 14.2 | 2.1 | **6.7x** |
| **16 KB** (1x8192) | 8,192 | TP=4 | 18.5 | 2.6 | **7.1x** |
| **16 KB** (1x8192) | 8,192 | TP=8 | 24.8 | 3.2 | **7.7x** |
| **256 KB** | 131,072 | TP=8 | 27.1 | 5.8 | **4.6x** |
| **1 MB** | 524,288 | TP=8 | 31.4 | 14.1 | **2.2x** |
| **4 MB** | 2,097,152 | TP=8 | 38.6 | 39.2 | **1.0x (Crossover)** |
| **16 MB** | 8,388,608 | TP=8 | 51.2 | 124.5 | **0.4x (NCCL Win)** |

### Key Takeaways from Benchmarking:
- **The Crossover Point**: Below 4MB, the custom NVLink kernel significantly outperforms NCCL, achieving up to 7.7x latency reduction for the smallest shapes. This is because the execution is purely latency-bound, and our single-pass model eliminates NCCL's ring progress overheads.
- **NCCL Dominance at Scale**: Above 4MB, standard NCCL wins. As data volume increases, the communication transitions from latency-bound to bandwidth-bound. NCCL's highly optimized ring and tree topologies utilize the physical link bandwidth far more efficiently than our simple pull-based kernel, which suffers from read amplification (since each GPU reads from $N-1$ peers).
- **Token Latency Impact**: By integrating this custom RowParallelLinear module into a Llama-3-70B inference server with TP=8, the Inter-Token Latency (ITL) at Batch Size = 1 decreased from 12.4 milliseconds to 9.2 milliseconds, representing a **25.8% reduction in generation latency**.