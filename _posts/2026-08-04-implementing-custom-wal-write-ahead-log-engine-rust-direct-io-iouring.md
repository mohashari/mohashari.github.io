---
layout: post
title: "Implementing a Custom WAL (Write-Ahead Log) Engine in Rust with Direct I/O and io_uring"
date: 2026-08-04 08:00:00 +0700
tags: [rust, database, storage, io-uring]
description: "Build a high-performance, crash-consistent write-ahead log (WAL) in Rust utilizing O_DIRECT and io_uring to eliminate kernel page cache bottlenecks."
image: "/images/diagrams/implementing-custom-wal-write-ahead-log-engine-rust-direct-io-iouring.svg"
thumbnail: "/images/diagrams/implementing-custom-wal-write-ahead-log-engine-rust-direct-io-iouring.svg"
---
Under heavy write-intensive workloads, database systems frequently bottleneck on the write-ahead log (WAL) synchronization path. Traditional file writes invoke the operating system page cache, causing dirty pages to accumulate until a synchronous `fsync()` system call blocks the application thread, forces a context switch, and locks the file descriptor. In production environments utilizing high-throughput NVMe SSDs, this design yields catastrophic tail latencies (99.9th percentile spikes exceeding 50ms) and limits IOPS to a fraction of the hardware's theoretical maximum. To break through this bottleneck, we can bypass the OS page cache using Direct I/O (`O_DIRECT`) and manage concurrency asynchronously using Linux's modern I/O interface, `io_uring`. This post details how to implement a custom, crash-consistent WAL engine in Rust that integrates these low-level paradigms for maximum throughput and predictable latency.

![Implementing a Custom WAL (Write-Ahead Log) Engine in Rust with Direct I/O and io_uring Diagram](/images/diagrams/implementing-custom-wal-write-ahead-log-engine-rust-direct-io-iouring.svg)

## The Bottleneck of Standard I/O and Fsync

When a database writes a WAL record via standard POSIX `write()`, it copies bytes from user-space memory to kernel-space buffers managed by the page cache. While the system call returns almost immediately, the data is not durable; it lives in volatile RAM. To guarantee crash consistency, the engine must call `fsync()` or `fdatasync()`. 

This buffered path suffers from three architectural flaws in high-performance contexts:
1. **Context Switches and Thread Blocking:** An `fsync()` call forces the executing thread to yield CPU control and block until the physical disk controller sends a hardware interrupt confirming that the sectors are written. Under heavy load, this stalls worker threads, degrading CPU utilization.
2. **Page Cache Lock Contention:** The kernel dirty-page-flushing daemon (`kswapd` or `pdflush`) and concurrent page allocations acquire heavy locks on the address space mapping. This leads to contention, generating tail-latency spikes that degrade database performance.
3. **Double Buffering and Cache Pollution:** The page cache consumes system RAM to cache blocks that are rarely read (since WAL logs are typically only read during recovery). This evicts valuable indexes and active data pages from memory, lowering overall read cache hit ratios.

On enterprise-grade NVMe SSDs capable of hundreds of thousands of IOPS, standard synchronous writes limit throughput to approximately 10,000–20,000 IOPS per thread due to POSIX blocking semantics. Decoupling write submission from completion via asynchronous interfaces is the only way to saturate the storage hardware.

## Direct I/O (O_DIRECT) and Memory Alignment in Rust

Bypassing the page cache requires opening files with the `O_DIRECT` flag (or `libc::O_DIRECT` in Rust). However, direct-to-disk DMA (Direct Memory Access) transfers place strict alignment constraints on the application:
1. **Buffer Address Alignment:** The starting memory address of the user-space buffer must be a multiple of the physical sector size (typically 4096 bytes on modern Advanced Format drives, or 512 bytes on legacy systems).
2. **File Offset Alignment:** The file offset where the write begins must align to the sector size.
3. **Write Length Alignment:** The number of bytes written must be a multiple of the sector size.

If these constraints are violated, the system call will return an `EINVAL` error. In Rust, standard allocations via `Vec<u8>` align to 1-byte or 8-byte boundaries. We must build a custom buffer wrapper utilizing Rust's `std::alloc` API to guarantee page-aligned memory layouts.

Below is the implementation of a page-aligned buffer wrapper designed for `O_DIRECT` safety:

<script src="https://gist.github.com/mohashari/0afd2e480265df9ec4c6049b923b361e.js?file=snippet-1.txt"></script>

## Architecting the io_uring Integration

Using `O_DIRECT` with standard synchronous syscalls solves lock contention but does not solve the blocking thread problem. This is where `io_uring` becomes necessary. `io_uring` replaces traditional system calls with two lock-free ring buffers shared between the kernel and user space: the Submission Queue (SQ) and the Completion Queue (CQ).

To maximize throughput, our WAL engine configures `io_uring` with two advanced features:
1. **Submission Queue Polling (SQPOLL):** Configured via the `IORING_SETUP_SQPOLL` flag, this flag instructs the kernel to spawn a dedicated background kernel thread (`io_sq_thread`). This thread continuously polls the SQ ring buffer for new entries, allowing the application to submit I/O writes without making a single system call.
2. **Buffer Registration:** Using `IORING_REGISTER_BUFFERS`, we register our `AlignedBuffer` instances with the kernel during initialization. This allows the kernel to map the virtual-to-physical memory addresses of our buffers once, bypassing the expensive step of pinning pages (`get_user_pages`) for every individual I/O operation.

The following snippet demonstrates setting up `io_uring` with SQPOLL and registered buffers in Rust using the low-level `io-uring` crate:

<script src="https://gist.github.com/mohashari/0afd2e480265df9ec4c6049b923b361e.js?file=snippet-2.txt"></script>

## WAL Record Framing and Block Packing

Because `O_DIRECT` restricts us to sector-aligned writes, we cannot write arbitrary, variable-length records directly to disk. Instead, we must pack variable-length database transaction frames into fixed-size block pages (e.g., 4096 bytes). 

To ensure crash consistency, each frame must begin with a structured header containing the transaction metadata, a length descriptor, and a CRC32 checksum of both the header and payload. The checksum is critical; if a system crash occurs mid-write, the remaining portion of the block on disk will contain corruption or stale data. The recovery manager will detect this via CRC32 mismatch and truncate the log cleanly.

Here is the implementation of the binary serialization format and checksum verification for the WAL records:

<script src="https://gist.github.com/mohashari/0afd2e480265df9ec4c6049b923b361e.js?file=snippet-3.txt"></script>

## Chaining Submissions with io_uring Linked Operations

A major design challenge in database engineering is ensuring that write operations and physical storage flushes occur in the correct sequence. Simply writing a block is insufficient; we must execute an explicit flush (`fdatasync`) to guarantee the data is non-volatile. 

In a standard model, this requires executing a write, blocking on its completion, and then executing a sync call—requiring multiple trips to the OS kernel. `io_uring` resolves this overhead using the `IOSQE_IO_LINK` flag. When this flag is set on a Submission Queue Entry (SQE), it instructs the kernel to execute the immediate next entry in the queue *only* after the linked entry completes successfully. 

We can construct a single chain containing a write SQE linked to an `fdatasync` SQE. Both are pushed to the ring simultaneously. The kernel handles the ordering entirely in kernel space, minimizing user-space coordination overhead.

The code below shows how to build and submit a linked write and sync sequence using `io_uring` SQEs:

<script src="https://gist.github.com/mohashari/0afd2e480265df9ec4c6049b923b361e.js?file=snippet-4.txt"></script>

## Reaping Completions Asynchronously

Once the kernel completes the operations, it inserts Completion Queue Entries (CQEs) into the shared Completion Queue (CQ) ring. To mark transactions as durable and notify the database worker threads, we run a background event loop that drains this queue.

Because `io_uring` handles completion ordering asynchronously, the completions can arrive out-of-order in error cases. However, under normal operations, the linked sync CQE will only arrive after its associated write CQE completes. We use the 64-bit `user_data` field to correlate completed physical flushes with pending futures in the application layer, resolving them using tokio's `oneshot` channels.

<script src="https://gist.github.com/mohashari/0afd2e480265df9ec4c6049b923b361e.js?file=snippet-5.txt"></script>

## Log Scanning and Crash Recovery

During startup, the database must scan the WAL to restore state up to the last durable transaction. The recovery scanner processes the WAL file sequentially. 

When scanning, we must distinguish between:
1. **Unallocated/Pre-allocated Space:** Since WAL files are pre-allocated to avoid allocation latency at runtime, the end of the log is marked by zeroed bytes.
2. **Torn Writes:** If the engine crashed while writing, a block may be partially written. The deserializer will catch this because the CRC32 checksum computed from the raw data will not match the checksum stored in the header.

Upon encountering the first invalid record or block of zero bytes, the scanner terminates recovery, ensuring that corrupt or incomplete transaction history is discarded.

<script src="https://gist.github.com/mohashari/0afd2e480265df9ec4c6049b923b361e.js?file=snippet-6.txt"></script>

## Production Failure Modes and Operational Realities

Running a custom WAL engine in production exposes hardware and OS edge cases that managed cloud environments and higher-level runtimes hide from developers.

### 1. Volatile Write Caches and the PLP Illusion
When using `O_DIRECT` + `fdatasync`, the execution speed depends heavily on whether the underlying storage controller features Power Loss Protection (PLP).
* **Enterprise NVMe SSDs (with PLP):** These drives contain physical capacitors that store enough charge to flush the onboard controller's DRAM cache to non-volatile flash memory in the event of sudden power loss. Consequently, when the controller receives a flush request, it can instantly acknowledge it as durable. The write latency on these drives remains flat and predictable (typically under 100 microseconds).
* **Consumer-Grade SSDs (without PLP):** These drives lack hardware capacitors. When an `fdatasync` command is received, the drive must block I/O processing entirely and flush its entire physical volatile cache to flash cells. This causes write latency to spike to tens of milliseconds, negating the throughput benefits of `io_uring`. Running custom WAL engines on storage backends without PLP is not recommended for production database workloads.

### 2. Lock Contention and io_uring SQPOLL Privileges
Using `IORING_SETUP_SQPOLL` spawns a kernel thread that polls the Submission Queue. Historically (pre-Linux kernel 5.12), this thread ran with elevated administrative privileges, requiring the process to run as root (`CAP_SYS_ADMIN`). 

In modern kernels, this restriction has been lifted, and the thread inherits the caller's privileges. However, the application must still account for the virtual memory allocation limit (`RLIMIT_MEMLOCK`). Because `io_uring` pre-registers memory buffers to lock pages in physical RAM, the kernel's default memlock limit (often 64KB) is quickly exceeded. If the system limit is not increased via systemd configuration or programmatically using `libc::setrlimit`, the `register_buffers` call will fail with an `ENOMEM` error.

### 3. File System Metadata Serialization
Even when using `O_DIRECT`, writing to a file can trigger synchronous metadata writes inside the host filesystem (such as XFS or ext4). If the file size changes, the filesystem journal block daemon must commit the new inode metadata to disk. This is a blocking, synchronous operation that bypasses `io_uring`'s async path.

To prevent filesystem journal serialization from stalling your write path:
1. **Pre-allocate Space:** Never dynamically grow WAL files. Use `fallocate()` or `libc::posix_fallocate()` to pre-allocate log segments (e.g., 64MB or 128MB chunks) up-front.
2. **Keep the File Size Constant:** Write to the pre-allocated log files using positional offsets. Keep track of the logical end-of-file (EOF) metadata within your own application headers rather than relying on the OS file-size metadata. This guarantees the kernel does not write metadata updates during hot execution loops.