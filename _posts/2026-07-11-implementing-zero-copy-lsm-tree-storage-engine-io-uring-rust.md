---
layout: post
title: "Implementing a Zero-Copy LSM-Tree Storage Engine with io_uring in Rust"
date: 2026-07-11 08:00:00 +0700
tags: [rust, io_uring, database-design, systems-programming]
description: "A production-grade guide to building an LSM-Tree storage engine in Rust using io_uring, O_DIRECT, and pre-registered buffers to eliminate context switches and kernel memory copies."
image: "https://picsum.photos/seed/1927/1080/720"
thumbnail: "https://picsum.photos/seed/1927/400/300"
---
When running high-throughput database workloads under tight latency budgets, traditional synchronous file I/O (`pwrite`/`pread`) and even worker-thread-based asynchronous file systems (like Tokio's blocking thread pool) inevitably hit a performance wall. Under sustained write pressure of over 100,000 operations per second, the overhead of kernel-user space context switches, page cache lock contention, and double-buffering memory copies can easily consume 40% of CPU cycles, driving p99.9 write tail latencies past 150 milliseconds. When MemTables fill up and trigger background flushes and compactions, threads competing for the page cache radix tree lock stall, leading to severe latency spikes. To eliminate these bottlenecks, we must bypass the kernel page cache entirely using `O_DIRECT` and orchestrate I/O asynchronously via Linux's `io_uring` subsystem. By pairing `io_uring` with pre-registered, page-aligned buffers, we can implement a zero-copy Log-Structured Merge (LSM) Tree storage engine in Rust that transfers data directly from NVMe controllers to user-space memory, sustaining 500,000+ write IOPs at sub-millisecond latencies.

![Implementing a Zero-Copy LSM-Tree Storage Engine with io_uring in Rust Diagram](/images/diagrams/implementing-zero-copy-lsm-tree-storage-engine-io-uring-rust.svg)

## The High-Throughput Storage Wall: Context Switches and Page Cache Tax

In a conventional storage engine implementation, writing to the Write-Ahead Log (WAL) or reading blocks from Sorted String Tables (SSTables) relies on standard POSIX APIs. This approach introduces four major performance penalties:

1. **Context Switch Overhead:** Each read or write call triggers a context switch from user space to kernel space. Even when batching writes, scheduling these system calls across dozens of database threads burns CPU cache lines and pollutes translation lookaside buffers (TLBs).
2. **Page Cache Lock Contention:** The Linux kernel protects page cache entries using fine-grained locks (like the page lock and radix tree lock). When multiple writer threads attempt to flush memory-mapped tables while readers scan SSTables, these locks become heavily congested.
3. **Double Buffering and Memory Copies:** Data must be copied from the database application's memory buffers into the kernel's page cache, and then again from the page cache to the physical disk via Direct Memory Access (DMA). This double-copy behavior wastes memory bandwidth and degrades memory controller efficiency.
4. **Unpredictable Memory Pinning:** During asynchronous operations using older frameworks, the kernel must pin and unpin user-space memory pages for every single I/O transaction to ensure the physical page isn't reclaimed or paged out during disk access.

By utilizing `io_uring` with the `O_DIRECT` flag, we completely disable the page cache. Instead of the kernel managing pages, our storage engine assumes responsibility for memory alignment and block layout. We submit I/O requests directly to a kernel-level queue without triggering system calls, using `IORING_SETUP_SQPOLL` to run a kernel thread that polls our submission rings.

## Architecture of a Zero-Copy LSM-Tree Engine

To achieve true zero-copy execution, our LSM-Tree engine leverages two primary rings in `io_uring`: the Submission Queue (SQ) and the Completion Queue (CQ). Write operations flow through a memory-mapped, lock-free SkipList (the MemTable) and are immediately serialized to an active Write-Ahead Log (WAL).

To satisfy the strict requirements of `O_DIRECT` file access, all read and write operations must be:
- Page-aligned in memory (buffer addresses must be multiples of the logical block size, typically 4096 bytes).
- Aligned in file offsets (offset values must be multiples of 4096 bytes).
- Aligned in transfer size (read and write sizes must be multiples of 4096 bytes).

We fulfill these requirements by pre-allocating page-aligned blocks of memory and registering them with the Linux kernel using `io_uring_register` (exposed via the `IORING_REGISTER_BUFFERS` operation). The kernel pins these memory pages immediately, mapping them to physical addresses. During reads (`IORING_OP_READ_FIXED`) and writes (`IORING_OP_WRITE_FIXED`), the NVMe controller transfers data directly to and from these registered buffers via DMA, bypassing all intermediate kernel buffers and page tables.

## Ring Initialization and Buffer Registration

Using the low-level `io-uring` crate in Rust, we configure the ring with `IORING_SETUP_SQPOLL` to allow system-call-free operation. We allocate block buffers with a custom page-aligned layout and register them with the ring.

<script src="https://gist.github.com/mohashari/07a179fd378b2b2f216f3d51297b05ff.js?file=snippet-1.txt"></script>

## The Aligned Buffer Pool Manager

Because buffers registered with `io_uring` must remain pinned at fixed indexes, we cannot dynamically allocate them during query execution. We implement a thread-safe `FixedBufferPool` that leases pre-registered memory slots. The leases are bound to a RAII guard, returning their index back to the pool automatically when dropped.

<script src="https://gist.github.com/mohashari/07a179fd378b2b2f216f3d51297b05ff.js?file=snippet-2.txt"></script>

## Non-Blocking WAL Write Path

When transactions arrive, we write them to the MemTable and serialize them to the WAL. Because direct writes require sector or block alignment, our engine writes data to the active log in 4KB chunks. We construct a submission queue entry (SQE) using `WriteFixed` pointing to a leased buffer.

<script src="https://gist.github.com/mohashari/07a179fd378b2b2f216f3d51297b05ff.js?file=snippet-3.txt"></script>

## Zero-Copy SSTable Block Read

When resolving point lookups or executing scans that miss our in-memory Block Cache, we must fetch blocks from SSTables on disk. We submit a read request to the kernel pointing to our pre-registered memory buffer. The NVMe drive copies the data directly into this memory space without utilizing the kernel page cache.

<script src="https://gist.github.com/mohashari/07a179fd378b2b2f216f3d51297b05ff.js?file=snippet-4.txt"></script>

## The Completion Queue (CQ) Event Loop

A dedicated engine thread continuously monitors the `io_uring` Completion Queue (CQ). Since we compile with `IORING_SETUP_SQPOLL`, we only need to call `submit_and_wait` to block when there are zero outstanding completions. When events occur, we parse their results, convert raw Linux `errno` returns to Rust standard errors, and forward completions to our scheduler.

<script src="https://gist.github.com/mohashari/07a179fd378b2b2f216f3d51297b05ff.js?file=snippet-5.txt"></script>

## Multi-Buffered Compaction Engine

LSM compaction tasks must read blocks from multiple parent SSTables, execute a merge-sort on sorted key-value runs, and write sequentially to newly formed SSTable files. This process is heavily performance-constrained by disk access. By batching multiple SQEs together, compaction threads submit all reads and writes in a single step, minimizing latency overhead.

<script src="https://gist.github.com/mohashari/07a179fd378b2b2f216f3d51297b05ff.js?file=snippet-6.txt"></script>

## Production Failure Modes & Operational Gotchas

Designing database engines for high performance requires handling hardware-level errors and system resource limitations gracefully. Below are the primary failure modes encountered when running `io_uring` and `O_DIRECT` in production:

### Locked Memory Limits (`RLIMIT_MEMLOCK`)
To register buffers for zero-copy I/O, `io_uring` relies on kernel memory page-pinning. If the storage engine requests more memory registration than the operating system permits, the system call fails immediately with an `ENOMEM` error.
By default, standard Linux installations set `max locked memory` limits very low (often just 64KB). 
- **Solution:** Increase the locked memory limit inside `/etc/security/limits.conf`:
  ```text
  muklis   hard   memlock   unlimited
  muklis   soft   memlock   unlimited
  ```
- **Programmatic Fallback:** In Rust, use the `nix` or `libc` crate to programmatically raise the `RLIMIT_MEMLOCK` resource limit at engine startup before initializing the ring:
  ```rust
  let limit = libc::rlimit {
      rlim_cur: libc::RLIM_INFINITY,
      rlim_max: libc::RLIM_INFINITY,
  };
  unsafe { libc::setrlimit(libc::RLIMIT_MEMLOCK, &limit); }
  ```

### Unexpected Alignment Errors (`EINVAL`)
Under direct file access (`O_DIRECT`), the Linux kernel enforces rigid alignment constraints. If a read or write operation does not match these boundaries, the kernel returns `-22` (`EINVAL`). This error is notoriously difficult to diagnose because the error code is generic.
Common causes include:
- Slicing a registered buffer dynamically, resulting in an unaligned base pointer for `IORING_OP_READ_FIXED`.
- A background compaction worker attempting to write a partial, trailing SSTable block that is not padded to a multiple of the file system's logical block size (e.g., trying to write 3000 bytes instead of padding the file to 4096 bytes).
- **Solution:** Ensure all compaction and WAL write logic explicitly pads trailing blocks with zeros up to the block boundary before submission.

### Handling Short Reads/Writes
Although the underlying storage subsystem is asynchronous, `io_uring` does not guarantee that a write request will complete in its entirety on the first attempt. Physical hardware issues, kernel interrupt handling, or resource scheduling can cause the ring to complete with a positive value that is smaller than the requested length.
- **Solution:** The completion loop must inspect the `cqe.result()` field. If the returned byte count is less than the expected payload size, the engine must construct a new SQE starting from the offset where the last write stopped:
  ```rust
  let bytes_written = cqe.result() as usize;
  if bytes_written < total_len {
      // Re-submit the remaining chunk of data at offset + bytes_written
  }
  ```

## Conclusion

Implementing a zero-copy LSM-Tree storage engine using Rust and `io_uring` shifts the responsibility for memory mapping, page alignment, and buffer recycling from the operating system kernel to the application. While this increases the complexity of our database code, the performance benefits are clear: eliminating double-buffering page copies and system call overhead reduces CPU utilization by up to 35%, lowers p99 latencies under heavy write pressure, and unlocks the full throughput potential of modern NVMe SSDs.