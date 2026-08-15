---
layout: post
title: "Implementing a Zero-Copy S3 Multipart Upload Client in Rust Using io_uring and Direct I/O"
date: 2026-08-15 08:00:00 +0700
tags: [rust, io-uring, linux, systems-programming, cloud-infrastructure]
description: "Bypass the Linux kernel page cache and eliminate CPU copy overheads to achieve line-rate S3 multipart uploads in Rust with io_uring and O_DIRECT."
image: "https://picsum.photos/seed/2735/1080/720"
thumbnail: "https://picsum.photos/seed/2735/400/300"
---
When executing massive data migration or ingestion pipelines—such as streaming multi-terabyte database backups or raw video assets from high-performance local NVMe storage to Amazon S3—standard asynchronous file I/O operations rapidly become the primary system bottleneck. Even on storage-optimized AWS instances (e.g., `i3en.3xlarge` with local NVMe drives capable of 5 GB/s read speeds and 25 Gbps networking), standard runtime models utilizing `tokio::fs` fail to saturate the network interface. The core culprit is the CPU overhead and memory bandwidth saturation caused by the Linux kernel page cache thrashes and double-buffering. In standard asynchronous frameworks, reading a file requires copying bytes from the disk to the kernel page cache, then copying them to user-space buffers via thread-pool threads running blocking `read` syscalls, before copying them again to TLS engines and socket buffers. This architectural friction results in millions of context switches, continuous TLB invalidations, and high CPU usage that limits throughput.

![Implementing a Zero-Copy S3 Multipart Upload Client in Rust Using io_uring and Direct I/O Diagram](/images/diagrams/implementing-zero-copy-s3-multipart-upload-client-rust-iouring-direct-io.svg)

## The Physics of Memory Copies and the Page Cache

To understand why traditional approaches fail under high load, we must analyze the path of a byte from disk to network. When a file is read using standard POSIX `read(2)`, the operating system performs a synchronous lookup in the kernel page cache. If the page is not in memory, the kernel allocates physical memory, schedules a DMA (Direct Memory Access) transfer from the disk controller into this kernel space page, and blocks the calling thread. Once the disk transfer completes, the kernel copies this data into the buffer provided by the user-space application.

For data ingestion clients, this page cache lookup is entirely wasted. The files being uploaded are read sequentially exactly once and will not be accessed again soon. Filling the page cache evicts hot pages belonging to other processes, thrashes the kernel's active/inactive LRU lists, and triggers heavy reclamation overhead. Additionally, the copy from the kernel page cache to the user-space buffer uses significant CPU cycles and memory bus bandwidth. At a throughput of 20 Gbps, copying data twice (once from kernel to user space, and once from user space to the network socket) consumes approximately 5 GB/s of memory bandwidth. 

To eliminate these overheads, we must configure our file handles with the `O_DIRECT` flag. Direct I/O instructs the kernel to bypass the page cache entirely. The NVMe controller performs DMA directly into user-space buffers. However, Direct I/O enforces strict hardware constraints:
1. **Memory Alignment**: The user-space buffer address must be aligned to the block size of the physical device or filesystem (typically 4096 bytes).
2. **File Offset Alignment**: The read offset within the file must be a multiple of the block size.
3. **Length Alignment**: The number of bytes read must be a multiple of the block size.

Violating any of these constraints causes the read operation to fail immediately with an `EINVAL` error.

## True Async with io_uring

Linux historically lacked a clean, asynchronous interface for Direct I/O. The old kernel AIO (`io_submit(2)`) only works asynchronously when using `O_DIRECT` on specific filesystems, has blockages during metadata updates, and introduces system call overhead. 

The `io_uring` subsystem resolves these issues by utilizing two circular ring buffers shared between the user application and the kernel: the Submission Queue (SQ) and the Completion Queue (CQ). By mapping these queues into user space via `mmap(2)`, operations can be submitted and reaped without executing system calls.

For maximum throughput, we combine `io_uring` with two critical features:
1. **Fixed Buffers (`io_uring_register(2)`)**: Under normal circumstances, even with `O_DIRECT`, the kernel must pin the user-space memory pages in physical RAM before a DMA transfer and unpin them afterward. By registering a static pool of buffers up front via `IORING_REGISTER_BUFFERS`, the pages are pinned permanently. The kernel bypasses the page-pinning path during I/O operations, reducing CPU cycles per I/O event.
2. **Submission Queue Polling (`SQPOLL`)**: When enabled, the kernel spawns a kernel thread that actively polls the Submission Queue for new I/O submission queue entries (SQEs). This allows the user-space application to perform I/O submit operations simply by writing to the SQ ring and updating the tail pointer, completely bypassing the `io_uring_enter(2)` syscall.

## Implementing the Zero-Copy Buffer Pool in Rust

To execute Direct I/O, we must build a custom memory allocator that provides aligned memory blocks. Standard allocations via Rust's `Vec<u8>` do not guarantee the 4096-byte alignment required by `O_DIRECT`.

The following snippet implements a custom aligned buffer pool specifically designed for `io_uring` registration.

<script src="https://gist.github.com/mohashari/9ac561e2b4d053192853a2ebe4f21265.js?file=snippet-1.txt"></script>

## Initializing io_uring and Registering Buffers

Once we have allocated a pool of aligned buffers, we construct our `io_uring` instance and register these buffers with the kernel. We use the low-level `io-uring` crate, which provides raw bindings to the kernel interface without high-level runtime assumptions.

<script src="https://gist.github.com/mohashari/9ac561e2b4d053192853a2ebe4f21265.js?file=snippet-2.txt"></script>

## Submitting Asynchronous Reads with Direct I/O

When using standard asynchronous filesystems, futures are structured so that buffers are owned by user-space and can be dropped at any time. For `io_uring`, the kernel writes to the registered buffer asynchronously. If the Rust application drops the future before the kernel posts a completion event to the CQ, the kernel will write to a memory address that might have been re-allocated, causing silent memory corruption.

To prevent this, our design utilizes an ownership transfer model. When an asynchronous read is issued, ownership of the buffer index is locked until the corresponding completion event is reaped.

<script src="https://gist.github.com/mohashari/9ac561e2b4d053192853a2ebe4f21265.js?file=snippet-3.txt"></script>

## The Completion Event Loop and Pipeline Coordination

We must continuously poll the Completion Queue to process raw bytes as they arrive from the NVMe disk. Once a block is loaded into a registered buffer, it is immediately handed to the S3 network engine. The buffer is locked until the S3 HTTP client finishes sending the payload over the network.

<script src="https://gist.github.com/mohashari/9ac561e2b4d053192853a2ebe4f21265.js?file=snippet-4.txt"></script>

## Zero-Copy Network Egress with kTLS and MSG_ZEROCOPY

Once our data is loaded into a registered buffer via Direct I/O, the next bottleneck is the network transmission. S3 requires HTTPS endpoints. Encrypting this data using a user-space TLS library (like standard Rustls or OpenSSL) requires reading the plaintext data, running it through the CPU encryption routines, and writing it into a separate output buffer. This process breaks the zero-copy pipeline.

To execute a true zero-copy path to S3:
1. **kTLS (Kernel TLS)**: We hand off the TLS handshake to user space (using OpenSSL or Rustls) and then extract the symmetric keys. We program these keys directly into the Linux kernel socket using `setsockopt(2)` with `SOL_TLS`. The kernel then handles the symmetric encryption.
2. **`MSG_ZEROCOPY` or `io_uring` Send**: Once kTLS is configured, we can use `MSG_ZEROCOPY` on the socket, or issue `opcode::Send` with fixed buffers to write directly to the socket. The physical network interface controller (NIC) reads the data directly from our registered user-space memory pages using DMA, performs TLS encryption inside the kernel (or offloads it to a Crypto-capable NIC), and streams it onto the wire.

Below is the socket configuration and transmission flow using kTLS and zero-copy write operations.

<script src="https://gist.github.com/mohashari/9ac561e2b4d053192853a2ebe4f21265.js?file=snippet-5.txt"></script>

Once kTLS is configured on the socket, we transmit our registered buffer directly to the S3 socket endpoint by submitting an asynchronous send operation to `io_uring`.

<script src="https://gist.github.com/mohashari/9ac561e2b4d053192853a2ebe4f21265.js?file=snippet-6.txt"></script>

## S3 Multipart Upload Mechanics and Signature Generation

S3 Multipart Upload requires a precise API hand-shaking protocol:
1. **`CreateMultipartUpload`**: Initializes the upload and returns an `UploadId`.
2. **`UploadPart`**: Uploads a single part (typically 8MB to 128MB sizes for high-throughput lines). Every part must be signed using AWS Signature Version 4 (SigV4).
3. **`CompleteMultipartUpload`**: Submits a list of all part numbers and their corresponding `ETag` headers returned during the `UploadPart` stages.

To generate the AWS SigV4 authorization header, we must compute the SHA256 payload hash. In a zero-copy pipeline, we run a hardware-accelerated SHA256 hash directly over our registered buffer before it leaves user space. This ensures we do not perform copies for signature calculation.

The following snippet calculates the SHA256 of the payload using CPU instruction sets (AVX2 / SHA-NI) directly within the registered buffer, structures the canonical request, and yields headers for the upload.

<script src="https://gist.github.com/mohashari/9ac561e2b4d053192853a2ebe4f21265.js?file=snippet-7.txt"></script>

## Handling Non-Aligned File Tails

Because Direct I/O requires strict alignment, we encounter a problem at the end of the file. If a file is 100,000,000 bytes long, reading it in 8MB chunks (8,388,608 bytes) will leave a final chunk of 7,725,056 bytes. This tail is not a multiple of the filesystem's block size (4096 bytes). Submitting a Direct I/O read for this final chunk will immediately return `EINVAL`.

There are two production methods to handle this scenario:
1. **Direct I/O Padding**: Round the final read length *up* to the nearest 4096-byte boundary (e.g., 7,729,152 bytes) and read the extra padding bytes from disk. When performing the S3 network upload, specify the exact, non-rounded file tail size in the HTTP `Content-Length` header, sending only the valid portion of the buffer.
2. **Buffered Fallback**: Revert to standard buffered asynchronous read (`tokio::fs::File`) only for the final, non-aligned chunk of the file.

The first method is preferred as it keeps the entire pipeline inside `io_uring` and avoids falling back to blocking worker threads. Below is the implementation of the aligned padding technique.

<script src="https://gist.github.com/mohashari/9ac561e2b4d053192853a2ebe4f21265.js?file=snippet-8.txt"></script>

## Production Edge Cases and Failure Modes

Operating zero-copy pipelines with `io_uring` and Direct I/O exposes several kernel-level edge cases that must be mitigated in production:

### Memory Pinning Limits (`RLIMIT_MEMLOCK`)
Because `IORING_REGISTER_BUFFERS` pins physical memory pages, the operating system subjects these allocations to the `memlock` resource limit. If the program attempts to register buffers exceeding this value, the syscall returns `ENOMEM`.
* **Mitigation**: Adjust resource configurations under `/etc/security/limits.conf` or programmatically increase limits at process startup using `prlimit(2)` with `RLIMIT_MEMLOCK` set to a value accommodating the buffer pool size.

### Kernel Version Support and Feature Probing
The behavior of `io_uring` varies across kernels. For instance, `IORING_FEAT_FAST_POLL` was introduced in Linux 5.7, and socket send zero-copy flags became stable in 6.0.
* **Mitigation**: Probe kernel features during application startup using `io_uring_get_probe(3)`. Provide a fallback code path to standard asynchronous runtimes (e.g., standard Tokio epoll) if the kernel version is less than 6.1.

### Disk-to-Network Backpressure
If the local NVMe drive delivers data to registered buffers faster than the network card can transmit them to AWS S3, buffers fill up. Under high throughput, this results in memory starvation or pipeline blockages.
* **Mitigation**: Construct a channel-based credit system. Allocate a fixed number of buffer tokens. The disk worker must acquire a token before queuing a read operation, releasing the token only when the corresponding network write completes.

### Pathological Page Faults
Even with pre-allocated aligned buffers, memory access can trigger major page faults if the buffers were not prefaulted.
* **Mitigation**: Force physical page allocation immediately after memory mapping by executing `std::ptr::write_bytes` across the entire block (as demonstrated in Snippet 1). This ensures that page table entries are populated before I/O execution, preventing overhead during transfer loops.