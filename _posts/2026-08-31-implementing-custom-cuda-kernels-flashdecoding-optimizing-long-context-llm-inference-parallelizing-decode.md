---
layout: post
title: "Implementing Custom CUDA Kernels for FlashDecoding: Optimizing Long-Context LLM Inference by Parallelizing the Decode Phase"
date: 2026-08-31 08:00:00 +0700
tags: [cuda, llm-inference, ai-engineering]
description: "A production guide to implementing custom CUDA kernels for FlashDecoding to parallelize the decode phase and optimize long-context LLM inference."
image: "https://picsum.photos/seed/8170/1080/720"
thumbnail: "https://picsum.photos/seed/8170/400/300"
---

In production LLM serving systems, the decode phase of inference is the silent killer of throughput and latency. While the prefill phase is highly compute-bound and easily saturates tensor cores due to massive matrix-matrix multiplications (GEMM), the decode phase is notoriously memory-bandwidth bound. At each decoding step, we generate a single new token ($Q$ length of 1) which must be multiplied against the entire history of keys and values ($KV$ cache). For a single user assistant or real-time streaming API operating at a low batch size (e.g., $B=1$), the GPU is starved for compute. A modern enterprise GPU like the NVIDIA H100 features 132 Streaming Multiprocessors (SMs). If we run a standard attention loop with 32 query heads and 8 KV heads (Grouped-Query Attention), parallelization only occurs across the batch and head dimensions. With $B=1$, we launch only 32 thread blocks, leaving 100 SMs completely idle. Meanwhile, the GPU must sweep gigabytes of KV cache memory from High-Bandwidth Memory (HBM) into SRAM for just a few FLOPs of arithmetic. FlashDecoding solves this underutilization by parallelizing the attention computation along the sequence length dimension (splitting the KV cache into blocks), enabling the system to saturate all available SMs and dramatically accelerate long-context inference.

![Implementing Custom CUDA Kernels for FlashDecoding: Optimizing Long-Context LLM Inference by Parallelizing the Decode Phase Diagram](/images/diagrams/implementing-custom-cuda-kernels-flashdecoding-optimizing-long-context-llm-inference-parallelizing-decode.svg)

## The Memory-Bandwidth Wall in Long-Context Decode

To understand why custom CUDA kernels are necessary for long-context LLM inference, we must look at the math governing arithmetic intensity during the decode phase. Arithmetic intensity is defined as the ratio of floating-point operations (FLOPs) to memory accesses (bytes). 

Consider a standard attention calculation:

$$\text{Attention}(Q, K, V) = \text{softmax}\left(\frac{Q K^T}{\sqrt{d}}\right) V$$

During prefill, the query tensor $Q$ has shape $[B, H, L, D]$ where $L$ is the prompt length. The arithmetic intensity of this matrix-matrix multiplication scales with $L$. Modern GPUs have tensor cores engineered specifically for these massive GEMM operations.

During decode, however, the query tensor $Q$ has shape $[B, H, 1, D]$. For each token generated, we must load the entire accumulated KV cache from HBM. Let us calculate the size of the KV cache for a Llama 3 70B model using Grouped-Query Attention (GQA) with a 128k context length at FP16 precision:

*   **Number of layers:** 80
*   **Key-Value heads ($H_{kv}$):** 8
*   **Head dimension ($D$):** 128
*   **Sequence length ($N$):** 128,000
*   **Data type size:** 2 bytes (float16)

$$\text{KV Cache Size} = 2 \times \text{layers} \times H_{kv} \times D \times N \times 2 \text{ bytes}$$

$$\text{KV Cache Size} = 2 \times 80 \times 8 \times 128 \times 128,000 \times 2 \approx 41.94\text{ GB}$$

At each decoding step, to generate a single token for a single user sequence ($B=1$), the GPU must read approximately $41.94\text{ GB}$ of data.

On an NVIDIA A100 (80GB SXM4 with $2.039\text{ TB/s}$ HBM bandwidth), reading $41.94\text{ GB}$ takes:

$$\text{Time}_{\text{A100}} = \frac{41.94\text{ GB}}{2039\text{ GB/s}} \approx 20.57\text{ ms}$$

On an NVIDIA H100 (80GB SXM5 with $3.35\text{ TB/s}$ HBM bandwidth), reading this takes:

$$\text{Time}_{\text{H100}} = \frac{41.94\text{ GB}}{3350\text{ GB/s}} \approx 12.52\text{ ms}$$

This $12.52\text{ ms}$ is a hard physical limit. If our attention implementation is memory-bandwidth bound, the maximum theoretical generation speed is $1 / 0.01252 \approx 80\text{ tokens/second}$. In reality, because standard attention cannot saturate the GPU's memory bus with only 8 or 32 active thread blocks (due to $B=1$), the achieved throughput is much lower. The GPU cores spend most of their time stalled, waiting for memory lines to arrive from HBM.

## The FlashDecoding Architecture: Split-K Attention

FlashAttention optimized the prefill phase by tiling query, key, and value blocks to keep them in the fast, on-chip SRAM (L1 cache) and avoid intermediate global memory writes. However, FlashAttention parallelizes along the query sequence length. In the decode phase, the query sequence length is 1. Thus, FlashAttention collapses back to a single thread block per head, yielding zero speedup over standard attention for decode.

FlashDecoding introduces a two-phase parallelization strategy across the KV sequence length (referred to as the "Split-K" dimension):

1.  **Phase 1: Parallel local attention compute.** We split the KV sequence length dimension $N$ into $S$ independent splits (or blocks) of size $B = N / S$. We launch a grid of thread blocks where the grid size along the $y$-axis is $S$, and the $x$-axis is $B \times H$. Each thread block computes attention locally over its allocated segment of the KV cache. To do this stably without two passes, we apply the online softmax algorithm, keeping track of the local maximum $m_i$ and the running denominator sum of exponentials $\ell_i$ for each block. Each block writes its local output vector $O_i$ and its local Log-Sum-Exp ($\text{LSE}_i = m_i + \log \ell_i$) to a temporary global memory buffer.
2.  **Phase 2: Global reduction.** Once all local blocks have completed, a second reduction kernel (or a fast warp reduction) is launched. It reads the local outputs $O_i$ and the corresponding $\text{LSE}_i$ values, determines the global maximum, rescales the local outputs, and produces the final global output $O$.

This design changes the parallelization factor from $B \times H$ to $B \times H \times S$. If we choose $S=32$ splits, we increase the active thread blocks on the GPU by $32\times$, saturating all 132 SMs of an H100 even at batch size 1 and utilizing the full HBM memory bandwidth.

## Designing a Custom CUDA Kernel for FlashDecoding Phase 1

To implement this on real hardware, we create a PyTorch custom CUDA extension. We start with the C++ pybind11 wrappers that handle tensor sanity checking and interface with Python.

<script src="https://gist.github.com/mohashari/e61e937f758885598bb5f1071aa8514d.js?file=snippet-1.txt"></script>

Next, we write the CUDA driver code (`flash_decode_launch.cu`). It calculates the grid sizes, sets the block dimensions, allocates dynamic shared memory, and executes the Phase 1 and Phase 2 kernels.

<script src="https://gist.github.com/mohashari/e61e937f758885598bb5f1071aa8514d.js?file=snippet-2.txt"></script>

Now, we implement the core Phase 1 kernel (`flash_decode_kernel.cu`). This kernel calculates local attention values for a slice of the sequence length. To sum the dot product components across threads, we use register-level warp shuffle operations (`__shfl_down_sync`), which are significantly faster than writing to and reading from shared memory.

<script src="https://gist.github.com/mohashari/e61e937f758885598bb5f1071aa8514d.js?file=snippet-3.txt"></script>

## Designing the Reduction Kernel

Once all sequence splits are calculated, we must merge their results. The mathematical key to this step is the Log-Sum-Exp ($\text{LSE}$) scaling mechanism. 

For each split $i$, we wrote:

$$\text{LSE}_i = m_i + \log \ell_i$$

In Phase 2, we load these values. Let $M = \max_i (\text{LSE}_i)$ be the global maximum across all splits. The global denominator $D$ is calculated as:

$$D = \sum_i e^{\text{LSE}_i - M}$$

The global output $O_{\text{final}}$ is the weighted sum:

$$O_{\text{final}} = \sum_i \frac{e^{\text{LSE}_i - M}}{D} O_i$$

Subtracting $M$ prevents floating-point overflow during execution. Here is the implementation of the reduction kernel:

<script src="https://gist.github.com/mohashari/e61e937f758885598bb5f1071aa8514d.js?file=snippet-4.txt"></script>

## Compilation and Benchmark Harness

We use PyTorch's `CUDAExtension` to compile these kernels, leveraging NVCC with fast math compiler options to optimize floating-point calculations.

```python
# snippet-5
from setuptools import setup
from torch.utils.cpp_extension import BuildExtension, CUDAExtension

setup(
    name='flash_decode_cuda',
    ext_modules=[
        CUDAExtension(
            'flash_decode_cuda',
            [
                'flash_decode_api.cpp',
                'flash_decode_launch.cu',
            ],
            extra_compile_args={
                'cxx': ['-O3'],
                'nvcc': [
                    '-O3',
                    '-gencode=arch=compute_80,code=sm_80',
                    '-gencode=arch=compute_90,code=sm_90',
                    '--use_fast_math'
                ]
            }
        ),
    ],
    cmdclass={
        'build_ext': BuildExtension
    }
)
```

We evaluate our custom FlashDecoding module against a standard PyTorch attention loop on an A100/H100 GPU using CUDA events to isolate performance.

```python
# snippet-6
import torch
import math
import flash_decode_cuda

def pytorch_naive_attention(q, k, v):
    # q: [B, H, 1, D]
    # k: [B, H_kv, N, D]
    # v: [B, H_kv, N, D]
    B, H, _, D = q.shape
    _, H_kv, N, _ = k.shape
    if H != H_kv:
        ratio = H // H_kv
        k = k.repeat_interleave(ratio, dim=1)
        v = v.repeat_interleave(ratio, dim=1)
    
    scores = torch.matmul(q, k.transpose(-2, -1)) / math.sqrt(D)
    attn_weights = torch.softmax(scores, dim=-1)
    out = torch.matmul(attn_weights, v)
    return out.squeeze(-2) # [B, H, D]

def run_benchmark():
    device = torch.device("cuda")
    torch.cuda.set_device(device)
    
    # Input parameters matching Llama-3-70B GQA configuration
    B = 1
    H = 32
    H_kv = 8
    D = 128
    N = 32768  # 32k context length
    num_splits = 32
    
    print(f"Running benchmark: B={B}, H={H}, H_kv={H_kv}, D={D}, Context Length N={N}")
    
    q = torch.randn(B, H, D, device=device, dtype=torch.float32)
    k = torch.randn(N, H_kv, D, device=device, dtype=torch.float32)
    v = torch.randn(N, H_kv, D, device=device, dtype=torch.float32)
    
    out_custom = torch.zeros(B, H, D, device=device, dtype=torch.float32)
    tmp_out = torch.zeros(B, H, num_splits, D, device=device, dtype=torch.float32)
    tmp_lse = torch.zeros(B, H, num_splits, device=device, dtype=torch.float32)
    
    # Warmup runs to avoid profiling initial CUDA allocation overhead
    for _ in range(10):
        flash_decode_cuda.forward(q, k, v, out_custom, tmp_out, tmp_lse, num_splits)
        _ = pytorch_naive_attention(q.unsqueeze(2), k.transpose(0, 1).unsqueeze(0), v.transpose(0, 1).unsqueeze(0))
        
    torch.cuda.synchronize()
    
    # Profile custom CUDA FlashDecoding
    start_evt = torch.cuda.Event(enable_timing=True)
    end_evt = torch.cuda.Event(enable_timing=True)
    
    start_evt.record()
    for _ in range(100):
        flash_decode_cuda.forward(q, k, v, out_custom, tmp_out, tmp_lse, num_splits)
    end_evt.record()
    torch.cuda.synchronize()
    custom_time = start_evt.elapsed_time(end_evt) / 100.0
    
    # Profile PyTorch Naive
    q_pt = q.unsqueeze(2)
    k_pt = k.transpose(0, 1).unsqueeze(0)
    v_pt = v.transpose(0, 1).unsqueeze(0)
    
    start_evt.record()
    for _ in range(100):
        _ = pytorch_naive_attention(q_pt, k_pt, v_pt)
    end_evt.record()
    torch.cuda.synchronize()
    pt_time = start_evt.elapsed_time(end_evt) / 100.0
    
    diff = (out_custom - _).abs().max().item()
    print(f"Correctness Verification Max Diff: {diff:.6e}")
    print(f"Standard Attention Inference Latency: {pt_time:.3f} ms")
    print(f"Custom FlashDecoding Inference Latency: {custom_time:.3f} ms")
    print(f"Speedup: {pt_time / custom_time:.2f}x")

if __name__ == "__main__":
    run_benchmark()
```

## Production Failure Modes & Optimization Bottlenecks

Implementing custom kernels is only half the battle; maintaining performance in a production serving system requires avoiding several common hardware limitations.

### Shared Memory Bank Conflicts

On modern NVIDIA architectures, shared memory is divided into 32 equal-sized memory banks that can be accessed simultaneously. If multiple threads within a warp (32 threads) access addresses that fall within the same bank, a bank conflict occurs. The GPU hardware serializes these requests, causing latency spikes.

A typical failure mode is storing temporary metrics or key arrays in shared memory with a column stride that matches a multiple of 32. For example, if you declare a shared array `__shared__ float s_data[32][128]` to store inputs, thread $i$ accessing column index $j$ will hit the same memory bank as thread $i + 1$. 

To fix this, you must pad the memory array:

```cpp
__shared__ float s_data[32][129]; // Adding 1 element breaks the 32-word alignment
```

This padding shifts the column stride in memory, distributing access across different banks and maintaining single-cycle throughput.

### Register Spilling

Each Streaming Multiprocessor contains a fast register file, but it is shared among all active threads. If your CUDA kernel uses too many registers (often caused by loop unrolling, large local array allocations, or complex matrix math), the compiler will spill the overflow registers to local memory. Local memory resides in global DRAM, which dramatically slows down memory access.

You can check for register spilling by compiling with NVCC's verbose statistics flags:

```bash
nvcc -O3 -Xptxas=-v --gpu-architecture=sm_80 -c flash_decode_launch.cu
```

Look for output indicating stack or frame memory usage:

```text
ptxas info    : Used 72 registers, 384 bytes spill stores, 384 bytes spill loads
```

To resolve register pressure, you can use the `__launch_bounds__` compiler directive to set a hard limit on thread block allocation or restructure loops to process data in smaller tiles:

```cpp
__global__ void __launch_bounds__(128, 4) flash_decode_phase1_kernel(...)
```

### Global Memory Write Overhead vs. Compute Balance (Choosing $S$)

Selecting the optimal number of splits ($S$) is a delicate balance. If $S$ is too small, you will not generate enough thread blocks to keep the GPU SMs busy. If $S$ is too large, you will launch too many blocks, and the overhead of writing the intermediate output buffers ($tmp\_O$ and $tmp\_LSE$) to global memory in Phase 1 and reading them back in Phase 2 will outweigh the benefits of parallelization.

Production systems like vLLM and TensorRT-LLM use a dynamic heuristic to choose $S$ based on sequence length:

*   For short context lengths ($N < 1024$), they skip the reduction step entirely and fall back to a single block standard FlashAttention kernel ($S=1$).
*   For medium context lengths ($1024 \le N < 8192$), they allocate $S = 4$ or $S = 8$ splits.
*   For long context lengths ($N \ge 32768$), they calculate $S$ dynamically using the formula:
    
    $$S = \min\left(128, \frac{\text{Number of SMs} \times \text{Target Waves}}{\text{Batch Size} \times \text{Heads}}\right)$$

This ensures that the GPU remains fully saturated without introducing unnecessary global memory overhead.