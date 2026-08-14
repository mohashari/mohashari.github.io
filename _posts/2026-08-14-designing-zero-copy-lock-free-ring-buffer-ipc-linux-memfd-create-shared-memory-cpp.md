---
layout: post
title: "Designing a Zero-Copy Lock-Free Ring Buffer for IPC Using Linux memfd_create Shared Memory in C++"
date: 2026-08-14 08:00:00 +0700
tags: [cpp, systems-programming, performance, linux]
description: "Build a zero-copy, sub-microsecond latency SPSC ring buffer for Linux IPC using memfd_create, double-mapped virtual memory, and C++ atomics."
image: "https://picsum.photos/seed/9158/1080/720"
thumbnail: "https://picsum.photos/seed/9158/400/300"
---

In high-throughput, low-latency systems—such as high-frequency trading matching engines, real-time audio processors, or distributed database storage engines—the IPC channel is often the primary performance bottleneck. Traditional IPC options like TCP loopback, Unix domain sockets (`SOCK_STREAM`), or Linux pipes introduce unacceptable overhead. Every message sent through these channels requires crossing the kernel-user space boundary twice, triggering context switches and copying data at least twice (user buffer to kernel socket buffer, and kernel buffer to user buffer). In a microservice mesh handling 10 million messages per second, this double-copying and system call overhead translates to hundreds of microseconds of tail latency ($p99.9$) and saturates L3 cache lines with transient data. To break through this bottleneck, we must bypass the kernel entirely during the data path and write directly to shared memory using a zero-copy, lock-free Single Producer Single Consumer (SPSC) circular queue.

![Designing a Zero-Copy Lock-Free Ring Buffer for IPC Using Linux memfd_create Shared Memory in C++ Diagram](/images/diagrams/designing-zero-copy-lock-free-ring-buffer-ipc-linux-memfd-create-shared-memory-cpp.svg)

## The Double-Mapping Virtual Memory Trick

The most challenging design aspect of a circular ring buffer is handling the wrap-around boundary. When a producer attempts to write a message that spans the end of the buffer, it must normally split the write: copy the first segment to the end of the buffer, and copy the remaining segment to the beginning. This split write introduces conditional branching logic (which degrades CPU instruction pipelining and causes branch mispredictions) and requires a secondary `memcpy`. 

We can eliminate this complexity entirely using a virtual memory mapping trick. Instead of managing wrap-around in software, we configure the Linux kernel’s Memory Management Unit (MMU) to handle it in hardware. 

By reserving a contiguous virtual address space equal to twice the buffer's capacity ($2N$) and mapping the same physical memory pages consecutively into both halves of this virtual address space, the buffer effectively mirrors itself. A write that crosses the boundary of the primary mapping ($N-1$) seamlessly writes into the secondary mapping ($N$ to $2N-1$), which points to the exact same physical pages starting at index $0$. This allows us to write any block of data up to size $N$ contiguously using a single `memcpy` without bounds checking or branching.

To implement this double-mapping trick in C++, we use the following sequence:
1. Reserve a contiguous virtual address range of size $2N$ using `mmap` with `PROT_NONE` and `MAP_ANONYMOUS | MAP_PRIVATE`.
2. Map the physical shared memory file descriptor to the first half ($0$ to $N-1$) using `MAP_FIXED | MAP_SHARED`.
3. Map the exact same file descriptor to the second half ($N$ to $2N-1$) using `MAP_FIXED | MAP_SHARED`.

Here is the implementation of this double-mapping mechanism:

<script src="https://gist.github.com/mohashari/4ba2be7b46fe660d0ce2f2f1a4b92fcc.js?file=snippet-1.txt"></script>

## Setting Up Anonymous Shared Memory with memfd_create

Historically, POSIX shared memory (`shm_open`) or System V IPC was used for shared memory. However, these systems rely on global namespace paths (like `/dev/shm/my_shm`), which present significant security vulnerabilities and cleanup issues. If a process crashes before calling `shm_unlink`, the memory segment remains allocated in RAM indefinitely, leading to persistent memory leaks. Additionally, any process running on the system with access to the path can read or corrupt the buffer.

Linux 3.17 introduced `memfd_create`, which solves these issues by creating an anonymous file descriptor that resides entirely in RAM (using standard tmpfs). Because it has no presence in the file system namespace, other processes cannot locate it. The memory is cleaned up automatically when all file descriptors referencing the memfd are closed.

Another benefit of `memfd_create` is file sealing. Through `fcntl` and the `F_ADD_SEALS` command, we can apply seals to the memfd. Sealing prevents processes from modifying the file's size (`F_SEAL_SHRINK`, `F_SEAL_GROW`) or modifying its sealing status (`F_SEAL_SEAL`). In IPC scenarios, this ensures that a malfunctioning or compromised consumer process cannot shrink the file, which would instantly trigger a fatal `SIGBUS` signal in the producer process when it attempts to access the memory.

<script src="https://gist.github.com/mohashari/4ba2be7b46fe660d0ce2f2f1a4b92fcc.js?file=snippet-2.txt"></script>

## The Lock-Free Synchronization Model

To run at maximum speed, the ring buffer must operate without mutexes or system calls during normal operation. A Single Producer Single Consumer (SPSC) queue can be synchronized entirely using atomic operations and explicit memory barriers.

### Layout and False Sharing Prevention

When two CPU cores write to variables located on the same cache line (typically 64 bytes on x86_64), the CPU's cache coherency protocol (such as MESI) will repeatedly invalidate the cache line on both cores. This phenomenon, known as **false sharing** or cache line bouncing, can degrade performance by an order of magnitude.

To prevent false sharing, we must ensure that the producer's write-related variables and the consumer's read-related variables reside on separate cache lines. We achieve this by applying the `alignas(64)` alignment specifier to the atomic indices in our control block.

<script src="https://gist.github.com/mohashari/4ba2be7b46fe660d0ce2f2f1a4b92fcc.js?file=snippet-3.txt"></script>

### Acquire-Release Memory Orderings

We use sequentially monotonically increasing `uint64_t` indices for the `write_index` and `read_index` to track the state of the queue.

For the producer to write data safely without a mutex:
1. It reads the consumer’s `read_index` to determine how much free space remains in the buffer. This load must use `std::memory_order_acquire` to ensure that any data reads completed by the consumer are visible before the producer overwrites those regions of the buffer.
2. The producer copies the data directly into the buffer memory using `memcpy`.
3. The producer updates the `write_index` using `std::memory_order_release`. This store operation functions as a write barrier, ensuring that the actual data copy is globally visible in cache memory before the consumer observes the incremented `write_index`.

For the consumer to read data safely:
1. It reads the producer's `write_index` using `std::memory_order_acquire`. This load operation functions as a read barrier, ensuring that subsequent reads of the buffer memory do not execute before the index update is visible.
2. The consumer copies the data out of the double-mapped buffer.
3. The consumer updates the `read_index` using `std::memory_order_release`. This notifies the producer that the memory has been consumed and is available for reuse.

<script src="https://gist.github.com/mohashari/4ba2be7b46fe660d0ce2f2f1a4b92fcc.js?file=snippet-4.txt"></script>

## Bootstrapping the Connection via Unix Domain Sockets

Because our shared memory resides in an anonymous memfd, it lacks a file system path. To share it with a consumer process, we must pass the file descriptor across processes.

We can achieve this by passing the file descriptor over a local Unix domain socket (`AF_UNIX`) using auxiliary control data (`ancillary data`) containing the `SCM_RIGHTS` structure. When this message is sent, the Linux kernel duplicates the file descriptor entry in the target process's file descriptor table, granting the target process direct access to the underlying RAM pages.

### Sending the File Descriptor

<script src="https://gist.github.com/mohashari/4ba2be7b46fe660d0ce2f2f1a4b92fcc.js?file=snippet-5.txt"></script>

### Receiving the File Descriptor

<script src="https://gist.github.com/mohashari/4ba2be7b46fe660d0ce2f2f1a4b92fcc.js?file=snippet-6.txt"></script>

## End-to-End Orchestration Example

We can assemble these components into a unified flow. The code below demonstrates a producer process initializing the shared memory segment, mapping the control block, establishing the double-mapped virtual ring buffer, and passing the descriptor to a consumer thread.

<script src="https://gist.github.com/mohashari/4ba2be7b46fe660d0ce2f2f1a4b92fcc.js?file=snippet-7.txt"></script>

## Production Hazards and Hard-Won Lessons

While this system design provides significant performance improvements, deploying it to production requires addressing several real-world system hazards.

### Page Faults and Real-Time Latency Spikes
By default, the Linux kernel uses **on-demand paging**. When you allocate shared memory pages using `mmap`, the kernel updates your page tables but does not allocate physical memory immediately. When a process first attempts to write to these pages, the hardware triggers a page fault. The CPU suspends execution, enters kernel mode, allocates a physical page frame, updates the page tables, and resumes user code execution. This process can introduce latency spikes of 10 to 50 microseconds.

To prevent these latency spikes during critical processing paths, you must pre-fault the pages in advance during initialization.
* Pass the `MAP_POPULATE` flag to `mmap` to instruct the kernel to resolve all page mappings at allocation time.
* Call `mlock` or `mlockall` on the mapped address ranges. This prevents the Linux kernel's swapper daemon from paging out the memory to disk during periods of inactivity.

<script src="https://gist.github.com/mohashari/4ba2be7b46fe660d0ce2f2f1a4b92fcc.js?file=snippet-8.txt"></script>

### TLB Cache Misses and TLB Shootdowns
For high-performance applications, standard 4KB memory pages can lead to frequent translation cache misses in the processor's Translation Lookaside Buffer (TLB). 

Using Hugepages (2MB or 1GB pages) reduces the total number of translation paths required, lowering the TLB miss rate.
* Set the `MFD_HUGETLB` flag in `memfd_create` to allocate the shared memory segment using hugepages.
* Ensure the memory block size is aligned to the system's hugepage boundary (typically 2MB on x86_64 platforms).

### Handling Peer Process Crashes
A common issue in shared memory IPC designs is handling the crash of one of the participating processes. 
* If the consumer process crashes while the producer is running, the producer can continue to write to the queue until the buffer fills up, at which point the queue will block or drop data.
* If a process crashes, the other process can detect the event by monitoring the control Unix domain socket. If `read` or `recv` returns `0` (indicating the socket has closed), the remaining process should trigger clean teardown procedures.
* Avoid implementing spinloops without timeout mechanisms. Always include a retry limit or an integrated heartbeat mechanism in the control block to detect deadlocked threads.