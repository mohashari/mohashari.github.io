---
layout: post
title: "Building a Custom KV Cache Page Swapping Engine Using Async Direct I/O and POSIX madvise in Rust"
date: 2026-08-14 08:00:00 +0700
tags: [rust, systems-programming, llm-ops, io-uring, performance]
description: "A production-focused guide to building a zero-copy, non-blocking KV cache swapping engine in Rust using O_DIRECT, POSIX madvise, and io_uring."
image: "https://picsum.photos/seed/1275/1080/720"
thumbnail: "https://picsum.photos/seed/1275/400/300"
---

In high-concurrency Large Language Model (LLM) serving environments, managing the Key-Value (KV) cache is the single greatest bottleneck to scaling throughput. As sequence lengths expand to 32k, 128k, or even millions of tokens, the VRAM required to store attention matrices quickly outgrows the capacity of modern GPUs, leading to premature Out-of-Memory (OOM) crashes or aggressive request rejection. Standard solutions offload cache pages to host DRAM via pageable memory or use standard buffered file systems to swap to NVMe drives. However, these naive approaches introduce severe latency spikes: synchronous memory copies block the active GPU execution loop, while the Linux OS kernel page cache introduces unpredictable flush pauses and lock contention. To achieve true non-blocking, zero-copy page swapping that maintains high tokens-per-second, we must bypass kernel-space buffering entirely and manage memory layout, disk access, and virtual memory page mapping ourselves.

![Building a Custom KV Cache Page Swapping Engine Using Async Direct I/O and POSIX madvise in Rust Diagram](/images/diagrams/building-kv-cache-page-swapping-engine-async-direct-io-posix-madvise-rust.svg)

## The Bottleneck: Why the OS Page Cache Kills LLM Serving

During LLM inference, requests are processed in two phases: the prefill phase (which generates the KV cache for the prompt) and the decode phase (which generates tokens one by one, appending new keys and values to the cache). When the active sequence count is high, we must swap inactive caches out of expensive GPU VRAM to cheaper host DRAM or NVMe storage.

If we rely on standard buffered filesystem APIs (`std::fs::File`), writing a page of cache to disk involves copying the data from user space to the OS kernel page cache. The kernel then decides when to write those dirty pages to the underlying NVMe controller via background writeback threads. For systems requiring microsecond-level predictability, this approach is disastrous:
1. **Double Buffering:** The KV cache page exists in both the serving process's user-space memory and the kernel's page cache, cutting host memory capacity in half.
2. **Synchronous Page Faults:** When reading cached slots back to host memory, any page cache miss triggers a hardware page fault, prompting the OS to freeze the calling thread while it fetches blocks from the disk.
3. **I/O Lock Contention:** High-throughput writebacks dirty pages rapidly, causing the kernel block layer to block user-space writers to prevent memory exhaustion, causing latency spikes in the model execution loop.

To build a high-performance swapping engine, we must bypass the kernel page cache entirely using the `O_DIRECT` flag. By executing Direct I/O, we instruct the kernel to perform direct DMA (Direct Memory Access) transfers between our user-space memory buffers and the NVMe disk controller. However, this shifts the responsibility of alignment, buffering, and page lifecycle management to our application code.

## Designing a 4KB-Aligned Page Allocator in Rust

The first constraint of `O_DIRECT` is alignment. To execute a direct DMA transfer, the starting memory address of the user-space buffer, the file offset, and the length of the read or write operation must all be integer multiples of the logical block size of the storage device (typically 4096 bytes on modern NVMe drives, or 512 bytes on legacy block storage). 

Using standard heap allocation in Rust (`Box<[u8]>` or `Vec<u8>`) does not guarantee this boundary constraint. If you attempt to pass an unaligned memory address to a file opened with `O_DIRECT`, the read or write syscall will fail immediately with `EINVAL` (Invalid Argument).

To handle this, we can build a custom `AlignedBuffer` using Rust's `std::alloc` API. We define a custom layout with a guaranteed alignment of 4096 bytes and handle the manual deallocation to avoid memory leaks:

<script src="https://gist.github.com/mohashari/35758b88f3abd816c6c491c25a0bfaea.js?file=snippet-1.txt"></script>

## Direct I/O: Bypassing the Kernel with O_DIRECT

Once we have aligned memory buffers, we must open our swap files with direct I/O enabled. On Linux, this is achieved by adding the `O_DIRECT` flag to the `open` system call flags. Additionally, we use the `O_SYNC` flag (or `O_DSYNC`) to guarantee that write operations are immediately committed to the physical flash cells before the system call returns, preventing volatile write caches on the SSD controller from lying about persistence.

Preallocating the swap file size using `set_len` (internally invoking `ftruncate`) or `fallocate` is critical. If we let the file grow dynamically during swapping, the filesystem driver must continuously update its inode metadata (block maps, extents, and directory structures) on disk. These metadata updates are synchronous and block-layer constrained, reintroducing the latency spikes we are trying to avoid.

<script src="https://gist.github.com/mohashari/35758b88f3abd816c6c491c25a0bfaea.js?file=snippet-2.txt"></script>

## Reclaiming Host DRAM with POSIX madvise

Offloading the KV cache out of VRAM solves the GPU memory bottleneck, but it introduces a host memory bottleneck. A typical serving node might have 512GB of host DRAM, but if hundreds of large context inference streams are active, even this DRAM pool can saturate. 

When pages are read from or written to our aligned DRAM buffer, physical memory frames remain pinned to the process's page table. We can instruct the OS kernel virtual memory manager on how to handle these virtual memory pages using `posix_madvise` or `madvise`.

We use two primary flags:
1. `MADV_DONTNEED`: Informs the kernel that we are finished accessing the specified page range. The kernel immediately invalidates the page table mappings and frees the associated physical DRAM frames. The virtual address space remains valid, but any subsequent access will cause a soft page fault (reallocating physical memory and initializing it with zeroes).
2. `MADV_WILLNEED`: Informs the kernel that we plan to access this memory range soon. This triggers the OS to start mapping virtual memory space to physical frames and pre-fetching data from swap files in the background, minimizing runtime faults when the KV cache page is re-activated.

<script src="https://gist.github.com/mohashari/35758b88f3abd816c6c491c25a0bfaea.js?file=snippet-3.txt"></script>

## The Swapping Registry: Managing Cache State Transitions

Swapping is a stateful process. We need a fast, thread-safe cache registry to track the physical locations of active and inactive KV cache pages. A single KV cache "Slot" (holding keys and values for a prompt sequence block) can reside in GPU VRAM, host DRAM, or on the NVMe disk. While a slot is being written to or read from disk, it enters a transitional state (e.g., `EvictingToNVMe` or `FetchingToGPU`) to prevent other execution threads from accessing stale or uninitialized memory.

We define a `CacheSlotRegistry` to manage these state transitions:

<script src="https://gist.github.com/mohashari/35758b88f3abd816c6c491c25a0bfaea.js?file=snippet-4.txt"></script>

## Bypassing System Calls: High-Performance Disk I/O with io_uring

Even with direct I/O (`O_DIRECT`), standard POSIX `read` and `write` system calls incur significant overhead. Each call triggers a context switch from user space to kernel space, requiring memory translation, permission checks, and hardware driver execution. In a high-throughput model engine executing dozens of parallel swap tasks, context switching overhead can choke the CPU, leading to thread contention.

The modern solution for Linux systems is `io_uring`. This interface maps two ring buffers (queues) shared between user space and kernel space:
* **Submission Queue (SQ):** The application pushes requests (such as write/read instructions) into this queue.
* **Completion Queue (CQ):** The kernel writes results (bytes written, status codes) here once operations complete.

By writing directly to the shared SQ, we can submit multiple direct I/O requests without executing a system call. The kernel worker threads read from the SQ, perform DMA transfers, and post completion markers to the CQ.

Using the `io-uring` crate in Rust, we construct a lightweight async I/O worker:

<script src="https://gist.github.com/mohashari/35758b88f3abd816c6c491c25a0bfaea.js?file=snippet-5.txt"></script>

## Putting It All Together: The Swapping Engine Coordinator

Now, we integrate these subsystems. The `SwappingCoordinator` acts as the coordinator. It manages the registry, handles memory offsets in the pre-allocated host DRAM pool, and submits batch tasks to the disk using the `IoUringEngine`.

During eviction, the model engine commands the coordinator to offload a specific cache slot. The coordinator determines the target physical offset in the swap file, computes the source address in host memory, changes the slot's registry status, and pushes the transaction into the `io_uring` ring.

<script src="https://gist.github.com/mohashari/35758b88f3abd816c6c491c25a0bfaea.js?file=snippet-6.txt"></script>

## Production Failure Modes & Diagnostic Metrics

When running this custom swapping engine in bare-metal production environments (such as serving nodes equipped with 8x NVIDIA H100 GPUs and 4x PCIe Gen5 NVMe drives), you will encounter real-world edge cases. Below are the common failure modes and mitigation strategies.

### 1. The Silenced OS Crash: direct I/O Alignment Errors (`EINVAL`)
If your memory buffer alignment is not exactly 4096-byte aligned, or the write offset/length is not a multiple of the sector size, the Linux block layer rejects the request with `EINVAL`. 

**Diagnostic:**
Always check the output of `io_uring` completion queues or syscall results. In the `reap_completions` loop, if `cqe.result() < 0`, map the negative value to its corresponding POSIX error string. If you see `-22`, this translates to `EINVAL`. Ensure your page allocation code handles trailing padding bytes if the model's sequence batch size doesn't naturally round up to 4096 bytes.

### 2. Cross-Socket Contention: NUMA Node Saturation
On multi-socket servers (e.g., dual-socket AMD EPYC configurations), memory transfers that cross Socket borders degrade performance. If the thread running the GPU pipeline execution is bound to Socket 0, but the aligned memory buffer resides on a memory channel attached to Socket 1, every DMA operation must travel across the AMD Infinity Fabric or Intel UPI link. This degrades transfer speeds from a potential 32 GB/s down to 8 GB/s and increases memory access latency.

**Mitigation:**
Bind the swapping worker threads to the specific NUMA node where the target GPU and NVMe controller are located. Use the `numa` crate or standard `pthread_setaffinity_np` bindings in Rust. Allocate memory using `numa_alloc_onnode` rather than standard libc `malloc`.

### 3. Submission Queue Starvation and Disk Stalls
While NVMe storage is fast, its write latency (~80 to ~120 microseconds) is still orders of magnitude slower than Host-to-Device (H2D) PCIe memory copies (~1.5 microseconds). If the model scheduler initiates evictions faster than the SSD controller can flush block updates, the `io_uring` Submission Queue will fill up, causing `submit_read` or `submit_write` to error out.

**Mitigation:**
Implement a backpressure mechanism in the model scheduler. The registry must monitor pending `EvictingToNVMe` counts. If the count exceeds the `io_uring` queue depth configuration limit (e.g., 64 or 128 SQEs), block the scheduler from starting new sequence generation phases until the coordinator reaps completions.

## Conclusion

Bypassing standard kernel file access layers in Rust requires managing memory alignment and page lifecycle flags manually. However, this gives us precise control over hardware execution. By combining custom aligned page pools, direct memory mapping using `O_DIRECT`, memory releases using POSIX `madvise`, and asynchronous block submissions using `io_uring`, we eliminate OS page cache locks and context-switching overhead. This ensures low-latency execution and prevents page faults, allowing serving nodes to handle larger context windows and maintain stable tokens-per-second performance under high concurrent loads.