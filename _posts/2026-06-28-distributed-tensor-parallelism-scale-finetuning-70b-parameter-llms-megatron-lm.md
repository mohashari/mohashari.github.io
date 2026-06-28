---
layout: post
title: "Distributed Tensor Parallelism at Scale: Fine-Tuning 70B Parameter LLMs Using Megatron-LM"
date: 2026-06-28 08:00:00 +0700
tags: [ai_engineering, megatron-lm, distributed-training, llm]
description: "An in-depth, production-focused guide to scaling Llama-3-70B fine-tuning using Megatron-LM 3D parallelism, network optimization, and weight conversion."
image: "https://picsum.photos/seed/9532/1080/720"
thumbnail: "https://picsum.photos/seed/9532/400/300"
---

You have provisioned a cluster of four HGX H100 nodes, each packed with 8x H100 80GB SXM5 GPUs, connected by a 3.2 Tbps InfiniBand fabrics. You spin up a naive PyTorch FSDP (Fully Sharded Data Parallel) script to fine-tune Llama-3-70B with a 4096 sequence length. Within seconds, your logs throw a fatal `torch.cuda.OutOfMemoryError` during the backward pass of the first step. Even if you aggressively shard optimizer states, gradients, and model parameters, the sheer size of activation memory for a 70B model at high sequence lengths overwhelms the 80GB GPU memory boundary. When you try to squeeze the batch size down to 1 to prevent OOMs, your Model Flops Utilization (MFU) plummets to a pathetic 12%, rendering the run financially and operationally non-viable. To train at this scale, you must move beyond basic sharding and implement structured 3D Parallelism—specifically Tensor Parallelism (TP) combined with Pipeline Parallelism (PP) and Sequence Parallelism (SP). Megatron-LM remains the definitive, low-overhead framework for orchestrating this layout, but configuring it correctly requires a deep understanding of hardware topologies, communication collectives, and memory allocation.

## Deconstructing the 3D Parallelism Grid for 70B Models

Choosing the right configuration of Tensor Parallelism ($TP$), Pipeline Parallelism ($PP$), and Data Parallelism ($DP$) is the most critical decision when configuring Megatron-LM. The total number of GPUs in your cluster ($N$) must satisfy the relation $N = TP \times PP \times DP$. The dimensions cannot be chosen arbitrarily; they must align with the physical interconnect topologies of your hardware.

Modern deep learning servers (like the HGX H100) feature an asymmetrical network topology. The 8 GPUs inside a single chassis are directly connected via NVLink, providing bidirectional bandwidth of up to 900 GB/s per GPU. Node-to-node communication, however, must cross PCIe Gen5 buses to reach host network cards (NICs) and traverse an external InfiniBand network (typically 400 Gbps per port), which is significantly slower than NVLink. 

Because Tensor Parallelism requires frequent, high-bandwidth communication (specifically `all-reduce` and `reduce-scatter` operations at every single attention and MLP layer), it must **never** cross the boundary of a physical node. If you span Tensor Parallelism across nodes, your training loop will spend up to 80% of its execution time blocked on network communication over the external switch. 

For Llama-3-70B, which has a hidden dimension ($h$) of 8192, 80 layers, and 64 query attention heads (8 key-value heads using Grouped-Query Attention), the optimal 3D grid layout on 32 GPUs across 4 nodes is:
*   **Tensor Parallelism ($TP$): 8**. This confines all intra-layer model parallel communication within the high-speed NVLink domain of each individual node.
*   **Pipeline Parallelism ($PP$): 4**. The 80 layers of Llama-3-70B are partitioned sequentially across the 4 nodes, with each node managing 20 layers.
*   **Data Parallelism ($DP$): 1** (or more if scaling up cluster size). With 32 GPUs, $DP = 32 / (8 \times 4) = 1$. If we scaled to 8 nodes (64 GPUs), $DP$ would be 2.

This layout ensures that the heavy intra-layer communications are localized, while the pipeline communication (passing activation tensors from one node to the next) occurs over the external InfiniBand network, which is well-suited for the lower-frequency sequential transfers of Pipeline Parallelism.

Below is a production-ready bash script to launch Megatron-LM on a multi-node cluster, configured for Llama-3-70B with TP=8, PP=4, sequence parallelism, and FlashAttention-3 enabled.

<script src="https://gist.github.com/mohashari/00df0ad835b3f924abc67f2a96839736.js?file=snippet-1.sh"></script>

## The Mechanics of Tensor Parallelism (TP) in Megatron-LM

To debug convergence issues and memory bottlenecks during training, you must understand exactly how Megatron-LM splits tensor computations across GPUs. Standard Tensor Parallelism applies intra-layer parallelism by partitioning the weight matrices of the Multi-Layer Perceptron (MLP) block and the Multi-Head Attention (MHA) block.

In Megatron-LM, this is accomplished via two main primitives: `ColumnParallelLinear` and `RowParallelLinear`.

### The MLP Block
The MLP in Llama-3 consists of three linear layers: gate projection ($W_{gate}$), up projection ($W_{up}$), and down projection ($W_{down}$). The forward pass is defined as:
$$\text{MLP}(x) = \text{Swish}(x W_{gate}) \cdot (x W_{up}) W_{down}$$

To parallelize this across the $TP$ group:
1.  **ColumnParallelLinear** splits the weight matrices of $W_{gate}$ and $W_{up}$ vertically (column-wise).
    $$W_{gate} = \begin{bmatrix} W_{gate, 1} & W_{gate, 2} & \cdots & W_{gate, n} \end{bmatrix}$$
    Each GPU $i$ in the $TP$ group receives a copy of the input activation $X$ (using an `identity` forward pass) and computes:
    $$Y_i = \text{Swish}(X W_{gate, i}) \cdot (X W_{up, i})$$
2.  **RowParallelLinear** splits the weight matrix of $W_{down}$ horizontally (row-wise).
    $$W_{down} = \begin{bmatrix} W_{down, 1} \\ W_{down, 2} \\ \vdots \\ W_{down, n} \end{bmatrix}$$
    Each GPU computes the partial matrix multiplication:
    $$Z_i = Y_i W_{down, i}$$
3.  To construct the final output, the partial results $Z_i$ must be summed across the group. A single **All-Reduce Sum** collective is performed at the end of the MLP block, ensuring that all GPUs in the $TP$ group obtain the complete tensor:
    $$Z = \sum_{i=1}^n Z_i$$

### The Multi-Head Attention Block
Similarly, the Query ($Q$), Key ($K$), and Value ($V$) projections are concatenated and split column-wise (ColumnParallelLinear). This means each GPU handles a subset of the attention heads. For Llama-3-70B, which uses 64 query heads and 8 KV heads, a $TP=8$ setting allocates 8 query heads and 1 KV head to each GPU. 

The attention output projection ($W_{out}$) is then parallelized row-wise (RowParallelLinear), requiring a single `all-reduce` at the end of the MHA block. Consequently, each Transformer layer requires exactly two `all-reduce` operations in the forward pass and two in the backward pass.

The Python code below demonstrates a clean, functional implementation of these parallel operators using PyTorch's distributed communication package, mirroring Megatron's internal mechanics.

```python
# // snippet-2
import torch
import torch.distributed as dist
import torch.nn.functional as F

class CopyToModelParallelRegion(torch.autograd.Function):
    """Pass the input through to the model parallel region.
    During backward, sum the gradients across the TP group.
    """
    @staticmethod
    def forward(ctx, input_):
        return input_

    @staticmethod
    def backward(ctx, grad_output):
        if dist.get_world_size() == 1:
            return grad_output
        # Sum gradients across the tensor parallel group
        dist.all_reduce(grad_output, op=dist.ReduceOp.SUM)
        return grad_output


class ReduceFromModelParallelRegion(torch.autograd.Function):
    """Sum the inputs from the model parallel region.
    During backward, copy the gradient to all partitions.
    """
    @staticmethod
    def forward(ctx, input_):
        if dist.get_world_size() == 1:
            return input_
        # Accumulate partial outputs from all GPUs in the TP group
        dist.all_reduce(input_, op=dist.ReduceOp.SUM)
        return input_

    @staticmethod
    def backward(ctx, grad_output):
        return grad_output


class ColumnParallelLinear(torch.nn.Module):
    """Linear layer parallelized by columns."""
    def __init__(self, in_features, out_features, bias=False):
        super().__init__()
        self.in_features = in_features
        self.out_features = out_features
        self.tp_size = dist.get_world_size()
        
        # Sliced output dimension
        self.out_features_per_partition = out_features // self.tp_size
        
        # Initialize only this GPU's slice of the weights
        self.weight = torch.nn.Parameter(
            torch.empty(self.out_features_per_partition, in_features)
        )
        if bias:
            self.bias = torch.nn.Parameter(
                torch.empty(self.out_features_per_partition)