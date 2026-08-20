---
layout: post
title: "Implementing a Zero-Copy B-Tree Storage Engine with Direct I/O and io_uring in C++20"
date: 2026-08-20 08:00:00 +0700
tags: [c++, databases, io_uring, systems-programming, linux]
description: "A deep dive into bypassing the Linux page cache with O_DIRECT and building an asynchronous, zero-copy B-Tree storage engine using C++20 coroutines and io_uring."
image: "https://picsum.photos/seed/7165/1080/720"
thumbnail: "https://picsum.photos/seed/7165/400/300"
---

When your database workload scales to millions of write operations per second on high-performance NVMe SSDs, the operating system's page cache ceases to be an asset and instead becomes a catastrophic bottleneck. The kernel's page cache management introduces severe write amplification, CPU cache-line bouncing, and unpredictable latency spikes during dirty page flushes, which routinely destroy p99.99 latency SLA guarantees. For database engineers building high-throughput engines, the path forward requires bypassing the OS cache entirely via Direct I/O (`O_DIRECT`). However, managing manual block alignment, zero-copy memory frames, and asynchronous thread scheduling is notoriously difficult. By leveraging Linux's `io_uring` and C++20 coroutines, we can build a storage engine that drives physical hardware to its theoretical limits while maintaining a clean, asynchronous traversal API.

## The Cost of the Kernel Page Cache in High-Throughput Databases

The standard Linux buffered I/O pipeline is designed for general-purpose applications, not high-performance databases. When an application thread issues a `read()` or `write()` system call, the kernel performs a series of costly actions:
1. **Double Buffering:** Data is copied from the storage device into kernel-space page cache pages, and then copied again from the page cache into user-space application buffers. This wastes memory bandwidth and pollutes the CPU's L1/L2 caches with redundant data.
2. **Lock Contention:** To protect the integrity of the page cache, the kernel relies on global or per-file address space locks (such as `i_mmap_rwsem`). Under highly concurrent database workloads where hundreds of worker threads read and write to the same database files, these locks quickly become bottlenecked, leading to thread serialization.
3. **Dirty Page Stalls:** When the ratio of dirty pages in system memory exceeds configured sysctl limits (e.g., `vm.dirty_background_ratio` or `vm.dirty_ratio`), background OS `flusher` threads begin flushing pages to disk. If the disk cannot keep up, user threads are blocked synchronously to regulate page dirtying. This creates catastrophic, unpredictable latency spikes.

Direct I/O (`O_DIRECT`) solves this by instructing the kernel to bypass the page cache completely. The storage controller transfers data directly to or from the user-space buffer via Direct Memory Access (DMA). However, removing the kernel page cache shifts all management responsibilities—memory alignment, cache eviction, and I/O coordination—to the application. If you make a single non-aligned access, the kernel rejects the call with a generic `EINVAL` error.

## The Blueprint: A Zero-Copy B-Tree Architecture

To achieve true zero-copy storage traversal, the data layout on disk must map 1:1 with the data layout in memory. The storage engine operates on fixed-size blocks (typically 4096 bytes or multiples thereof, matching the physical block size of the underlying NVMe drive).

In our architecture, the memory allocator, the B-Tree node layout, and the database buffer pool are completely aligned:
* **The Buffer Pool:** A contiguous chunk of page-aligned memory allocated via `mmap()` or `posix_memalign()`. This pool is divided into fixed-size slots (frames).
* **The B-Tree Nodes:** Built inside these frames. When we read a B-Tree page, the NVMe controller writes the data directly into a buffer pool slot. No parsing, deserialization, or field-by-field copying occurs. The application casts the slot memory directly to a B-Tree node pointer and starts searching.
* **Offset-Based Navigation:** Traditional memory pointers are useless on disk. All child-pointer links within our B-Tree nodes are stored as 64-bit logical Page IDs. A Page ID maps directly to a file offset (e.g., `PageID * PageSize`).

## Interfacing with the Kernel: io_uring Setup for Direct I/O

To implement Direct I/O without blocking application threads, we leverage Linux's asynchronous I/O framework, `io_uring`. To maximize performance, we configure three critical optimizations:
1. `IORING_SETUP_SQPOLL`: Spawns a kernel thread that continuously polls the submission queue (SQ). This allows our application to submit I/O operations without the overhead of making a system call.
2. `IORING_REGISTER_BUFFERS`: Pre-registers our buffer pool with the kernel. This allows the kernel to pin the physical memory pages up front, eliminating the need to perform page translation lookup and page table mapping during each read or write.
3. `IORING_SETUP_COOP_TASKRUN`: Ensures that I/O completion interrupts run on the submitting thread, reducing inter-processor interrupts (IPIs) and cache line bouncing.

The following snippet demonstrates how to initialize the `io_uring` context, open the database file with `O_DIRECT`, and register the buffer pool memory.

<script src="https://gist.github.com/mohashari/275f3a3dc7ab55da9b258c64ac42dc67.js?file=snippet-1.txt"></script>

## Zero-Copy Node Layout and Memory Alignment

When utilizing Direct I/O, the hardware controller transfers blocks directly into the buffer memory we registered in `IOContext`. Consequently, the structural nodes of our B-Tree must be strictly aligned to the hardware page boundary and lay out their member data sequentially. We can use C++20 compile-time assertions to guarantee standard layout alignment and layout rules. 

We separate leaf nodes and internal (index) nodes. Leaf nodes store the actual key-value pairs, while internal nodes store keys and child page IDs.

<script src="https://gist.github.com/mohashari/275f3a3dc7ab55da9b258c64ac42dc67.js?file=snippet-2.txt"></script>

## Asynchronous Page Cache & Buffer Pool Management

Since Direct I/O bypasses the operating system's page cache, we must manage page life cycles entirely in user space. If two threads want to read the same B-Tree page, we must fetch it from disk once, hold it in memory, and prevent eviction while it is in use.

Our buffer pool manager utilizes:
* **Frame Tracking:** An array of descriptors mapping registered buffer slots to active database page IDs.
* **Pin Counting:** A mechanism to count how many active readers are accessing a frame. A page with a non-zero pin count cannot be evicted.
* **Clock Eviction Algorithm (Second Chance):** A lock-friendly cache eviction policy. It performs a circular scan over page descriptors, examining a reference bit. If the reference bit is set, the scan clears the bit and moves to the next frame. If the bit is clear and the frame is unpinned, it is evicted.
* **Segmented Directory Locks:** A sharded latch structure over the page hash table to prevent global lock contention when concurrent threads lookup, pin, or evict pages.

<script src="https://gist.github.com/mohashari/275f3a3dc7ab55da9b258c64ac42dc67.js?file=snippet-3.txt"></script>

## The Asynchronous B-Tree Traversal Pipeline

Traversing a B-Tree involves descending from the root node to a target leaf node. If a page in the path is not cached in memory, we must fetch it from disk. In a synchronous database, this forces the executing thread to block, wasting CPU time.

Using C++20 coroutines, we write the traversal code as an asynchronous pipeline. If the page is already cached, execution proceeds synchronously. If the page is missing, the coroutine suspends (`co_await`), registers the read task via `io_uring`, and yields control of the thread. Once the drive finishes the read and returns, the event loop resumes our traversal exactly where it left off.

<script src="https://gist.github.com/mohashari/275f3a3dc7ab55da9b258c64ac42dc67.js?file=snippet-4.txt"></script>

## The Event Loop and Coroutine Resumption

To coordinate async operations, we need a reactor thread running an event loop. This loop monitors the `io_uring` completion queue (CQ). When the kernel completes a DMA transaction, it populates a Completion Queue Entry (CQE).

We retrieve the address of the suspended coroutine from `cqe->user_data`, verify that the operation completed successfully, and call `resume()`. This transfers execution back to the `async_btree_lookup` loop on the event thread.

<script src="https://gist.github.com/mohashari/275f3a3dc7ab55da9b258c64ac42dc67.js?file=snippet-5.txt"></script>

## Production Failure Modes & Mitigation Strategies

Implementing user-space storage engines exposes you to unique failure modes that standard applications never have to consider.

### 1. Handling Short Reads and Writes
Although Rare with local SSD blocks, `io_uring` read or write operations can return fewer bytes than requested (a "short read" or "short write"), which is reflected in `cqe->res`. If a 4KB block returns 2048 bytes, the engine must handle it. 

Your completion loop must catch this state:
* Detect if `cqe->res < static_cast<int>(PAGE_SIZE)` and `cqe->res > 0`.
* Issue a follow-up asynchronous operation referencing the remaining offset and remaining memory buffer slice.

### 2. Page Alignment Errors (EINVAL)
If a buffer address, file offset, or byte size is not aligned to the physical block size of the drive, the kernel immediately aborts the operation with `EINVAL`. 
* **The Cause:** Using arbitrary `malloc()` pointers or unaligned file offset math.
* **The Prevention:** Strictly allocate buffer regions via aligned allocators like `mmap` using `MAP_ANONYMOUS | MAP_POPULATE` and assert alignment in code:
```cpp
assert((reinterpret_cast<uintptr_t>(buffer) & 4095) == 0);
assert((file_offset & 4095) == 0);
```

### 3. Submission Queue Exhaustion
Under heavy traffic, your application might generate read/write tasks faster than the event loop or kernel thread can clear them. If `io_uring_get_sqe()` returns `nullptr`, the queue is full.
* **The Prevention:** Do not block. Implement a flow-control ring buffer in user-space. If the submission queue is full, yield the calling thread, or push the task to a task queue, and wait for active completions to free slots.

### 4. Torn Writes and Power Outages
Modern NVMe SSDs only guarantee write atomicity up to a certain block limit (Atomic Write Unit Normal, or `AWUN`, which is often 4KB). If your B-Tree node page size is configured to 16KB and power fails mid-write, the drive may persist only two of the four physical sectors, leaving a corrupted, half-written block on disk.
* **Mitigation A (Double-Write Buffer):** Before updating a page in place in the database file, write the page to a contiguous, circular double-write buffer file and issue a forced flush. If a crash occurs, recover the pristine page state from the double-write file.
* **Mitigation B (Checksum Verification):** Store a CRC32C checksum in the header of each page. When reading a page from disk, recalculate the checksum. If the calculated checksum does not match the header checksum, flag the page as corrupted and initiate recovery protocols from the write-ahead log (WAL) or replica pool.