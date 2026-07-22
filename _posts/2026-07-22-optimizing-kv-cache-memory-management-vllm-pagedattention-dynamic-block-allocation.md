---
layout: post
title: "Optimizing KV Cache Memory Management in vLLM with PagedAttention and Dynamic Block Allocation"
date: 2026-07-22 08:00:00 +0700
tags: [vllm, memory-management, llm-inference]
category: ai_engineering
description: "Eliminate GPU memory fragmentation and OOM failures under high-concurrency LLM inference using vLLM's virtual memory block manager and PagedAttention."
image: "https://picsum.photos/seed/vllm-paged/1080/720"
thumbnail: "https://picsum.photos/seed/vllm-paged/400/300"
---

In high-throughput LLM serving systems, the most common source of page out-of-memory (OOM) crashes and throughput collapse isn't the size of the static model weights, but the wild, unpredictable expansion of the Key-Value (KV) Cache. When serving a model like LLaMA-3-70B on 8x H100 GPU nodes with standard Float16 precision, traditional inference engines allocate memory for the worst-case scenario. If a user sets the maximum sequence length to 8,192 tokens, the engine reserves memory for all 8,192 tokens immediately upon request registration—even if the sequence terminates after generating only 50 tokens. Under a moderate concurrency of 128 concurrent requests, this eager allocation scheme consumes hundreds of gigabytes of GPU VRAM that is never actually utilized, resulting in an average memory waste of 60% to 80% due to internal fragmentation and overallocation. This artificial memory barrier caps serving throughput at a fraction of the hardware's theoretical capability and causes catastrophic cascade failures under traffic spikes. vLLM solves this bottleneck by introducing PagedAttention, a virtual memory abstraction that divides the KV cache into small, non-contiguous physical block pools, dynamically mapping them through a central Block Manager. This architecture shifts LLM serving from memory-bound capacity limits to compute-bound performance frontiers.

![Optimizing KV Cache Memory Management in vLLM with PagedAttention and Dynamic Block Allocation Diagram](/images/diagrams/optimizing-kv-cache-memory-management-vllm-pagedattention-dynamic-block-allocation.svg)

## The Math of the KV Cache Bottleneck

To understand why the KV Cache dominates the operational cost of LLM serving, we must examine its runtime footprint. During the autoregressive generation (decode) phase, the model processes tokens one by one. To avoid recomputing key-value states for all previous tokens in the sequence at each step, the model caches these key-value vectors in GPU memory. 

For a single transformer layer, the memory footprint (in bytes) of the KV Cache for a single token is calculated as follows:

$$Memory_{\text{token}} = 2 \times N_{\text{layers}} \times N_{\text{kv\_heads}} \times d_{\text{head}} \times \text{BytesPerElement}$$

Where:
* The factor of **2** represents the distinct tensors stored for the Key and Value states.
* **$N_{\text{layers}}$** is the total number of transformer layers in the model.
* **$N_{\text{kv\_heads}}$** is the number of key-value heads. Under Multi-Query Attention (MQA) or Grouped-Query Attention (GQA), this value is significantly lower than the number of query heads ($N_{\text{query\_heads}}$). For Multi-Head Attention (MHA), it equals $N_{\text{query\_heads}}$.
* **$d_{\text{head}}$** is the dimension of the attention head (e.g., 128).
* **$\text{BytesPerElement}$** is the byte-width of the numerical precision format (2 bytes for FP16 or BF16, 1 byte for FP8, 4 bytes for FP32).

Let us apply this formula to LLaMA-3-70B served in BF16 precision. The architecture utilizes Grouped-Query Attention (GQA) with 8 key-value heads, 80 transformer layers, and a head dimension of 128:

$$Memory_{\text{token}} = 2 \times 80 \times 8 \times 128 \times 2 = 327,680 \text{ bytes} \approx 320 \text{ KB}$$

For a single request generating a sequence of length 8,192:

$$Memory_{\text{sequence}} = 8,192 \times 320 \text{ KB} \approx 2.56 \text{ GB}$$

If we scale this to a production batch size of $B = 128$ concurrent requests:

$$Memory_{\text{batch}} = 128 \times 2.56 \text{ GB} = 327.68 \text{ GB}$$

A single 80GB H100 GPU cannot support this volume of KV Cache memory even before considering model weights. LLaMA-3-70B weights alone occupy approximately 140 GB in FP16/BF16. In practice, engineers must use tensor parallelism (TP = 4 or TP = 8) to partition both the model weights and the KV Cache space across multiple GPUs. 

Traditional inference engines fail to manage this space dynamically, introducing three distinct types of memory waste:
1. **User Reservation Waste (Overallocation)**: The engine allocates memory for the maximum possible sequence length (`max_seq_len`) because it does not know when the model will generate the end-of-sequence (EOS) token. If the sequence terminates early, the unused memory allocated for the remaining tokens is locked and wasted.
2. **Internal Fragmentation**: Occurs when the allocation granularity exceeds the actual sequence growth. If memory is allocated in static partitions, any bytes at the tail end of the partition that are not yet filled with token vectors represent dead space.
3. **External Fragmentation**: Because sequences grow dynamically over time, the system allocates and frees contiguous chunks of memory of varying sizes. This leads to physical memory fragmentation, where the GPU contains enough total free memory to run a batch, but the memory is fragmented into disjoint, non-contiguous ranges, making it impossible to allocate a contiguous block for a new sequence.

## PagedAttention: Virtual Memory for LLMs

PagedAttention addresses these fragmentation modes by adopting the classic virtual memory mapping paradigm used in operating systems. Instead of allocating a large, contiguous memory space for each request's KV Cache, vLLM allocates space in fixed-size, non-contiguous physical blocks.

* **Logical Blocks**: The KV Cache of a single sequence is represented as a contiguous series of logical blocks. Each block has a capacity of $B$ tokens (commonly $B = 16$).
* **Physical Blocks**: The physical GPU memory is partitioned into a pool of physical blocks of the same token capacity. These blocks do not need to be contiguous.
* **Block Table**: A mapping structure (similar to an OS page table) that translates logical block indices to physical block indices for each sequence.

During the execution of the attention kernel, the engine maps logical token indices to physical locations. For a given token at sequence index $i$, the engine computes the logical block index and the token's offset within that block:

$$\text{logical\_block\_idx} = \lfloor i / B \rfloor$$
$$\text{block\_offset} = i \bmod B$$

The custom CUDA kernel queries the sequence's block table to retrieve the physical block ID, and then computes the exact memory address of the Key ($K_i$) and Value ($V_i$) vectors:

$$\text{Address}(K_i) = \text{Pool}[\text{BlockTable}[\text{logical\_block\_idx}]] + \text{block\_offset} \times d_{\text{head}} \times \text{BytesPerElement}$$

By computing these addresses dynamically during the kernel's execution, vLLM bypasses the requirement for contiguous memory allocations. The physical blocks can be scattered anywhere in the GPU's memory pool. This completely eliminates external fragmentation and reduces internal fragmentation to, at most, the last block of each sequence (a maximum of $B-1$ tokens of waste per request).

## The Block Manager and Dynamic Allocation Lifecycle

At the core of vLLM is the `BlockManager` (or `Scheduler`), which runs on the CPU host. It manages the pool of physical blocks on both the GPU (VRAM) and the CPU (system RAM, used as a swap space). 

### The Prefill Phase
When a user submits a request with a prompt of length $P$, the scheduler calculates the number of blocks required to hold the prompt's KV states:

$$\text{BlocksRequired} = \lceil P / B \rceil$$

The scheduler allocates these blocks from the free block pool and updates the request's entry in the Block Table. The model then runs the prefill phase, computing the key-value states for the entire prompt in a single parallel step. The custom kernel writes these states into the physical blocks allocated in the GPU pool.

### The Decode Phase
During autoregressive decoding, the model generates a single token at each iteration. If the current block has empty slots (i.e., $(S \bmod B) \neq 0$, where $S$ is the current sequence length), the generated key and value vectors are written to the remaining slots in the last allocated physical block. 

When the sequence reaches a block boundary (e.g., sequence length increases from 16 to 17 tokens, where $B = 16$), the scheduler allocates a new physical block from the free pool, adds it to the sequence's Block Table mapping, and the generation continues.

### Swapping (GPU to CPU)
If the GPU block pool runs out of free physical blocks during a decode iteration, vLLM cannot allocate new blocks for active requests. To prevent Out-Of-Memory (OOM) crashes, the scheduler performs preemption:
1. It selects one or more active requests to suspend based on scheduling policies (e.g., First-In-First-Out or lowest priority).
2. It evicts the physical blocks allocated to the selected sequences from GPU memory to CPU memory. This copy operation is executed via high-speed PCIe channels using pinned host memory to maximize throughput.
3. The scheduler updates the block tables for these sequences, marking their blocks as "swapped."
4. Once GPU memory pressure subsides, the scheduler swaps the blocks back from CPU memory to the GPU pool, allowing the request to resume decoding.

### Copy-on-Write (CoW) for Parallel Sampling and Prefix Caching
PagedAttention enables efficient memory sharing across multiple sequences. In scenarios like parallel sampling (generating multiple completions for a single prompt) or system prompt prefix caching, different requests share the same prompt prefix.

Because the prompt KV cache blocks are immutable, the block table maps multiple logical blocks from different sequences to the same physical blocks on the GPU. The scheduler tracks this sharing via a reference count (`ref_count`) for each physical block.

When a sequence generates a new token that requires writing to a shared block, the scheduler detects that the physical block has a `ref_count > 1`. To prevent writes from corrupting other sequences sharing the same prefix, the scheduler triggers a Copy-on-Write (CoW) operation:
1. It allocates a new, empty physical block.
2. It executes a GPU-to-GPU device copy of the existing KV states from the shared block to the new block.
3. It decrements the `ref_count` of the original shared block.
4. It updates the Block Table of the writing sequence to point to the new physical block.
5. The sequence writes its new token to the new block, now safe in its own isolated memory space.

## Simulating vLLM's Block Manager in Python

To understand how virtual block allocation, reference counting, and Copy-on-Write operate under the hood, let us walk through a Python simulation of the `BlockManager`. This simulation models physical block allocations, block table updates, and dynamic CoW during parallel sequence decoding.

```python
import math
from typing import Dict, List, Optional

class PhysicalBlock:
    def __init__(self, block_id: int, block_size: int = 16):
        self.block_id = block_id
        self.block_size = block_size
        self.ref_count = 0

    def __repr__(self) -> str:
        return f"PhysicalBlock(ID={self.block_id}, refs={self.ref_count})"


class BlockAllocator:
    def __init__(self, num_blocks: int, block_size: int = 16):
        self.block_size = block_size
        self.free_blocks: List[PhysicalBlock] = [
            PhysicalBlock(i, block_size) for i in range(num_blocks)
        ]
        self.allocated_blocks: Dict[int, PhysicalBlock] = {}

    def allocate(self) -> PhysicalBlock:
        if not self.free_blocks:
            raise MemoryError("GPU Out of Memory: No free physical blocks available in the pool.")
        block = self.free_blocks.pop(0)
        block.ref_count = 1
        self.allocated_blocks[block.block_id] = block
        return block

    def free(self, block: PhysicalBlock):
        block.ref_count -= 1
        if block.ref_count <= 0:
            if block.block_id in self.allocated_blocks:
                del self.allocated_blocks[block.block_id]
            block.ref_count = 0
            self.free_blocks.append(block)

    def increment_ref(self, block: PhysicalBlock):
        block.ref_count += 1

    @property
    def num_free_blocks(self) -> int:
        return len(self.free_blocks)


class BlockTable:
    def __init__(self):
        self.mapping: List[PhysicalBlock] = []

    def append_block(self, block: PhysicalBlock):
        self.mapping.append(block)

    def get_physical_block(self, logical_idx: int) -> PhysicalBlock:
        if logical_idx < len(self.mapping):
            return self.mapping[logical_idx]
        raise IndexError("Logical block index out of range.")

    def set_physical_block(self, logical_idx: int, block: PhysicalBlock):
        self.mapping[logical_idx] = block

    def __len__(self) -> int:
        return len(self.mapping)


class Sequence:
    def __init__(self, seq_id: int, prompt_len: int, block_size: int = 16):
        self.seq_id = seq_id
        self.num_tokens = prompt_len
        self.block_size = block_size
        self.block_table = BlockTable()

    @property
    def num_logical_blocks(self) -> int:
        return math.ceil(self.num_tokens / self.block_size)


class MockKVManager:
    def __init__(self, total_gpu_blocks: int, block_size: int = 16):
        self.block_size = block_size
        self.allocator = BlockAllocator(total_gpu_blocks, block_size)
        self.sequences: Dict[int, Sequence] = {}

    def register_prompt(self, seq_id: int, prompt_len: int) -> Sequence:
        seq = Sequence(seq_id, prompt_len, self.block_size)
        required_blocks = seq.num_logical_blocks
        
        for _ in range(required_blocks):
            block = self.allocator.allocate()
            seq.block_table.append_block(block)
            
        self.sequences[seq_id] = seq
        print(f"[Seq {seq_id}] Registered prompt of {prompt_len} tokens. Allocated {required_blocks} blocks.")
        return seq

    def fork_sequence(self, parent_seq_id: int, child_seq_id: int) -> Sequence:
        parent = self.sequences[parent_seq_id]
        child = Sequence(child_seq_id, parent.num_tokens, self.block_size)
        
        # Share physical blocks with the child sequence (increment references)
        for block in parent.block_table.mapping:
            self.allocator.increment_ref(block)
            child.block_table.append_block(block)
            
        self.sequences[child_seq_id] = child
        print(f"[Fork] Seq {child_seq_id} forked from Seq {parent_seq_id}. Shared {len(parent.block_table)} blocks.")
        return child

    def decode_token(self, seq_id: int):
        seq = self.sequences[seq_id]
        current_token_idx = seq.num_tokens
        logical_block_idx = current_token_idx // self.block_size
        
        # Check if the block is already mapped in the BlockTable
        if logical_block_idx < len(seq.block_table):
            current_physical_block = seq.block_table.get_physical_block(logical_block_idx)
            
            # Check for Copy-on-Write (CoW) condition
            if current_physical_block.ref_count > 1:
                # Resolve CoW: Allocate new block and copy contents
                new_block = self.allocator.allocate()
                print(f"[CoW] Seq {seq_id} triggered Copy-on-Write on logical block {logical_block_idx}. "
                      f"Cloned Physical #{current_physical_block.block_id} -> #{new_block.block_id}")
                
                # Decrement reference on the old block and set new mapping
                self.allocator.free(current_physical_block)
                seq.block_table.set_physical_block(logical_block_idx, new_block)
        else:
            # We reached a block boundary and need a new physical block
            new_block = self.allocator.allocate()
            seq.block_table.append_block(new_block)
            print(f"[Allocation] Seq {seq_id} hit block boundary. Allocated Physical #{new_block.block_id} for logical block {logical_block_idx}.")

        # Increment sequence token count
        seq.num_tokens += 1


# --- Execution Walkthrough ---
if __name__ == "__main__":
    # Initialize cache manager with 10 physical blocks (each storing 16 tokens)
    manager = MockKVManager(total_gpu_blocks=10, block_size=16)

    # 1. Register a request with a 24-token prompt (requires 2 blocks)
    seq1 = manager.register_prompt(seq_id=1, prompt_len=24)
    print(f"Block mappings for Seq 1: {seq1.block_table.mapping}\n")

    # 2. Fork Seq 1 to generate two parallel paths (Seq 2 and Seq 1)
    seq2 = manager.fork_sequence(parent_seq_id=1, child_seq_id=2)
    print(f"Block mappings for Seq 2: {seq2.block_table.mapping}")
    print(f"Free blocks count: {manager.allocator.num_free_blocks}\n")

    # 3. Decode token on Seq 1 (currently at 24 tokens)
    # The active logical block is index 1 (tokens 16-31). Since this block is shared (ref_count=2), CoW triggers.
    print("--- Decoding Seq 1 ---")
    manager.decode_token(seq_id=1)
    print(f"Mappings Seq 1: {seq1.block_table.mapping}")
    print(f"Mappings Seq 2: {seq2.block_table.mapping}\n")

    # 4. Decode token on Seq 2
    # Active logical block is index 1. Since Seq 1 cloned its block, the reference count of the original block 
    # mapped to Seq 2 dropped back to 1. Seq 2 can write directly to it without triggering CoW.
    print("--- Decoding Seq 2 ---")
    manager.decode_token(seq_id=2)
    print(f"Mappings Seq 2: {seq2.block_table.mapping}\n")

    # 5. Grow Seq 1 to token 32 (hitting boundary of logical block 2)
    # Grow Seq 1 through block 1 capacity (16 to 31 tokens)
    seq1.num_tokens = 31
    print("--- Decoding Seq 1 past block boundary ---")
    manager.decode_token(seq_id=1) # Token 32 -> Requires new block allocation
    print(f"Mappings Seq 1: {seq1.block_table.mapping}")
    print(f"Free blocks count: {manager.allocator.num_free_blocks}")
```

## Production Tuning: Ratios, Failure Modes, and Mitigations

When deploying vLLM in high-concurrency production environments, the default configuration settings can lead to severe performance degradation if they do not match the service workload characteristics.

### Failure Mode 1: Swap Thrashing
When the aggregate context lengths of active sessions exceed the physical VRAM block capacity, vLLM's scheduler evicts blocks to host memory. If the incoming request rate remains high, requests alternate between executing and being preempted. This results in "swap thrashing," where the PCIe bus becomes a massive bottleneck. 

Even on PCIe Gen 5 x16 lines (delivering ~63 GB/s bidirectional bandwidth), copying gigabytes of KV caches back and forth introduces severe latency. The time-to-first-token (TTFT) and inter-token latency (ITL) can spike by up to 20x.

* **Diagnostic**: Monitor the Prometheus metric `vllm:num_requests_swapped`. Any value greater than zero indicates that the system is over-saturated.
* **Mitigation**: 
  1. Reduce `--max-num-seqs` (default is 256) to limit the maximum number of concurrent requests processed in a single batch.
  2. Increase `--gpu-memory-utilization` (default is 0.90) to allocate more VRAM for the KV cache pool, but do not set it above `0.95` as this can lead to GPU execution workspace allocation failures.
  3. Set a reasonable `--max-model-len` to prevent single requests with massive context windows from exhausting the entire block pool.

### Failure Mode 2: Prefill Starvation and Preemption Cycles
Because processing prompt tokens (prefill) is computationally distinct from generating tokens (decode), a surge of long prompts can exhaust the free block pool instantly. If the engine processes a large batch of prompt tokens, it may preempt currently running decoding sequences, discarding their KV blocks to free up space. When these preempted sequences are scheduled again, they must re-run the entire prefill phase, leading to highly redundant GPU computation.

* **Diagnostic**: Monitor the `vllm:num_requests_waiting` and `vllm:gpu_cache_usage_factor` metric. High values of waiting requests accompanied by frequent oscillations in running requests indicate preemption cycles.
* **Mitigation**:
  1. Enable chunked prefill by configuring `--enable-chunked-prefill=True`. This breaks down long prompts into smaller chunks and interleaves prefill and decode tasks in the same batch, preventing long prompts from monopolizing the block allocator.
  2. Set `--max-num-batched-tokens` to a value that balances prefill throughput and decode latency (typically 4,096 or 8,192).

### Trade-offs of Block Size Tuning
The `--block-size` parameter (default is 16) determines the granularity of KV Cache paging. Tuning this parameter introduces trade-offs in CUDA performance and memory utilization:

| Block Size | Internal Fragmentation | CUDA Kernel / Metadata Overhead | Memory Access Alignment |
| :--- | :--- | :--- | :--- |
| **8** | Extremely low (~2-3 tokens wasted per request on average) | High metadata storage in the Block Table; increased thread divergent lookups in CUDA kernels. | Sub-optimal. Does not align well with GPU memory transaction coalescing. |
| **16** (Default) | Low (~7-8 tokens wasted per request) | Balanced. Low metadata overhead, good CUDA occupancy. | Optimal for most architectures (Ampere, Hopper). |
| **32** | Medium to High (~15-16 tokens wasted) | Extremely low. Minimizes page table updates and cache misses in lookup arrays. | Best memory alignment for large tensor representations. |

For workloads processing short context sequences, stick to a block size of `16`. For ultra-long context operations (e.g., 32k+ sequences using models like LLaMA-3-8B-Instruct), increasing `--block-size` to `32` can yield a 3-5% execution speedup by improving kernel memory access patterns, provided the aggregate VRAM footprint is managed.

### Key Production Configuration Tuning Reference

To deploy a high-performance vLLM instance on an 8x H100 node serving LLaMA-3-70B under high concurrency, use the following production-tested configuration as a baseline:

```bash
python3 -m vllm.entrypoints.openai.api_server \
    --model meta-llama/Meta-Llama-3-70B-Instruct \
    --tensor-parallel-size 8 \
    --gpu-memory-utilization 0.92 \
    --max-model-len 8192 \
    --max-num-seqs 128 \
    --block-size 16 \
    --enable-prefix-caching \
    --enable-chunked-prefill True \
    --max-num-batched-tokens 8192
```

## Conclusion

PagedAttention and dynamic block allocation represent a major paradigm shift in LLM serving infrastructure. By translating virtual memory operating system design patterns directly to GPU VRAM management, vLLM bypasses the physical memory fragmentation bottlenecks that historically capped inference capacity. In production, this translation enables high concurrency levels without sacrificing stability. As context lengths continue to scale toward millions of tokens, and quantization formats like FP8 and FP4 become standard, managing this dynamic allocation framework efficiently remains one of the core challenges of backend AI engineering. Optimizing these systems is no longer just about optimizing neural network kernels—it is fundamentally about memory layout and virtualization.