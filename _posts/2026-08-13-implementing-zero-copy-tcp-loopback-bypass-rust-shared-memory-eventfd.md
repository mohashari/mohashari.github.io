---
layout: post
title: "Implementing a Zero-Copy TCP Loopback Bypass in Rust Using Shared Memory and eventfd"
date: 2026-08-13 08:00:00 +0700
tags: [rust, systems-engineering, performance, networking]
description: "Build a lock-free, zero-copy TCP loopback bypass in Rust using shared memory and eventfd to achieve sub-microsecond inter-process communication."
image: "https://picsum.photos/seed/3223/1080/720"
thumbnail: "https://picsum.photos/seed/3223/400/300"
---

In high-frequency trading (HFT) platforms, database proxies, or microservice meshes where services reside on the same physical host, the kernel's loopback interface (`lo`) is a notorious bottleneck. When routing millions of 512-byte messages per second between processes, the kernel TCP stack imposes a crippling tax: memory allocations for `sk_buff` structs, copy-in/copy-out operations between user-space and kernel-space memory, page table traversals, and context-switching overhead. Under a synthetic load of 1.5 million requests per second, traditional TCP loopback latency spikes from a median of 15 microseconds to a p99.9 of over 450 microseconds due to socket lock contention and scheduler jitter. A zero-copy loopback bypass using POSIX shared memory (`/dev/shm`) and `eventfd` avoids the kernel network stack entirely, bringing median latencies down to sub-microsecond levels (&lt;800 nanoseconds) and stabilizing p99.9 latencies under 5 microseconds.

![Implementing a Zero-Copy TCP Loopback Bypass in Rust Using Shared Memory and eventfd Diagram](/images/diagrams/implementing-zero-copy-tcp-loopback-bypass-rust-shared-memory-eventfd.svg)

## The Bottlenecks of Traditional TCP Loopback

To understand why TCP loopback fails to deliver low latency, we have to look at the path a single byte takes through the Linux kernel. Even when communicating on the `127.0.0.1` interface, the OS does not perform a simple memory copy. Instead, data passes through the entire transport layer.

1. **System Call overhead**: A write to a TCP socket requires entering the kernel via a `send`/`write` system call. The CPU must transition from ring 3 (user-space) to ring 0 (kernel-space), incurring page table checks and hardware cache flushes.
2. **Buffer Allocation and Packetization**: The kernel allocates an `sk_buff` structure to wrap the payload. The data is copied from user space into this kernel-space buffer (`copy_from_user`). The kernel then runs the TCP state machine, processes sequence numbers, performs window size checks, and generates TCP and IP headers.
3. **Loopback Routing**: The packet is routed through the IP lookup engine, which recognizes the IP as local. The packet is then queued on the loopback device's input queue.
4. **Wakeup and Scheduler Latency**: The receiver process, blocking on a `read` or `epoll` call, is signaled. The scheduler marks the process as runnable. Depending on CPU load, the process may wait in the scheduler queue (`runqlat`) for several microseconds before being executed.
5. **Memory Retrieval**: Once the receiver runs, it invokes a `recv`/`read` system call, incurring another context switch. The kernel copies the data from the `sk_buff` to the receiver’s user-space memory (`copy_to_user`) and deallocates the kernel buffer.

This process involves at least two memory copies, two socket lock allocations, multiple context switches, and cache line invalidations. If the producer and consumer run on different CPU cores, the L1/L2 caches containing the payload are invalidated, forcing the processor to retrieve the data from the shared L3 cache or main system memory. Under heavy load, tools like `perf` and `tcptop` reveal that the CPU spends over 40% of its execution cycles inside `tcp_v4_rcv`, `__release_sock`, and `skb_clone`.

## Architecture of a Shared Memory Bypass

The zero-copy bypass architecture replaces this complex chain with a shared memory region (`/dev/shm`) mapped directly into the virtual address space of both processes using `shm_open` and `mmap`. 

Communication is managed via lock-free ring buffers (one for TX and one for RX to enable full-duplex operation). The metadata—such as head and tail pointers—is modified using atomic CPU instructions. Because both processes access the exact same physical memory pages, transferring data is as simple as writing bytes to an offset in the mapped region and updating an atomic pointer. 

However, a lock-free queue is only half the solution. If the consumer process loops indefinitely waiting for new data (busy-spinning), it will consume 100% of a CPU core. In multi-tenant or resource-constrained environments, this is unacceptable. To solve this, we introduce `eventfd`—a lightweight kernel event counter that provides a file-descriptor-based interface for low-latency notifications. When the queue is empty, the consumer registers its interest in the `eventfd` using standard event loops (`epoll` or `io_uring`) and yields the CPU. When the producer writes data to the queue, it writes an 8-byte integer to the `eventfd`, waking up the consumer with minimal latency.

## Defining the Shared Memory Ring Buffer in Rust

To implement this bypass safely in Rust, we must ensure our shared memory structure has a deterministic, predictable memory layout. Rust's default layout (`repr(Rust)`) allows the compiler to reorder fields, optimize alignment, and introduce arbitrary padding. We must enforce a C-compatible layout using `#[repr(C)]`.

Additionally, we must prevent false sharing. False sharing occurs when two threads on different cores modify variables that reside on the same cache line (typically 64 bytes on x86_64). When Process A updates the `head` pointer and Process B updates the `tail` pointer, the hardware cache coherency protocol (MESI) invalidates the entire cache line across both cores, destroying performance. We prevent this by aligning our control pointers to 64-byte boundaries using `#[align(64)]` and adding padding.

<script src="https://gist.github.com/mohashari/1b48079a62e34de2a6f1e42eab9510d7.js?file=snippet-1.txt"></script>

## Writing to and Reading from the Ring Buffer (Lock-Free Logic)

To write data without mutexes or spinlocks, we implement a single-producer, single-consumer (SPSC) queue. 

The producer tracks the write cursor using `head`. Before writing, it checks if there is space in the queue. A queue is full when `head - tail == RING_BUFFER_SIZE`. Reading the atomic `tail` pointer from shared memory on every single write is expensive, as it requires a cache invalidation. To optimize this, the producer stores a cached local copy of `tail` (`local_tail`). It only queries the atomic `tail` pointer when the queue appears to be full.

<script src="https://gist.github.com/mohashari/1b48079a62e34de2a6f1e42eab9510d7.js?file=snippet-2.txt"></script>

The consumer uses reciprocal logic. It reads `head` using `Ordering::Acquire`, ensuring that any memory writes performed by the producer prior to updating the `head` pointer are fully committed and visible to the consumer.

<script src="https://gist.github.com/mohashari/1b48079a62e34de2a6f1e42eab9510d7.js?file=snippet-3.txt"></script>

## Low-Overhead Synchronization with eventfd

When the ring buffer is empty, the consumer must yield the CPU to prevent busy-waiting. We use Linux's `eventfd` system call. The kernel manages `eventfd` as a 64-bit integer counter. 
* Writing 8 bytes containing a non-zero value increments the counter.
* Reading 8 bytes returns the counter's current value and resets it to zero.
* If configured with `EFD_NONBLOCK`, reading from an `eventfd` whose counter is zero immediately returns `EWOULDBLOCK` instead of blocking. This makes it fully compatible with asynchronous event loops.

<script src="https://gist.github.com/mohashari/1b48079a62e34de2a6f1e42eab9510d7.js?file=snippet-4.txt"></script>

## Sharing File Descriptors via Unix Domain Sockets

To make the bypass work, both processes must reference the *same* `eventfd` file description in the kernel. In Linux, file descriptors are indices into a process-specific file descriptor table. You cannot simply serialize an integer descriptor and send it over shared memory; that integer is meaningless to another process.

Instead, we must transmit the file descriptor across a Unix Domain Socket using a control message (`cmsg`) containing `SCM_RIGHTS`. The kernel intercepts this control message, duplicates the underlying file description in the kernel's global file table, and allocates a new, valid file descriptor in the recipient process's table pointing to it.

<script src="https://gist.github.com/mohashari/1b48079a62e34de2a6f1e42eab9510d7.js?file=snippet-5.txt"></script>

## Putting It All Together: The Async Runtime Integration

To deploy this in production, the loopback bypass must integrate cleanly into async Rust (Tokio). Wrapping the `EventFd` in Tokio's `AsyncFd` allows us to register the descriptor with the runtime's underlying `epoll` reactor.

An critical performance optimization here is the **opportunistic read pattern**. When receiving messages in a loop, we check the ring buffer *before* reading the `eventfd` counter. If there is data in the queue, we process it immediately and bypass the kernel entirely. We only invoke the `eventfd` read system call when the shared memory queue is fully exhausted. Under high throughput, this architecture achieves a "zero-syscall" state, processing millions of items per second without a single system call.

<script src="https://gist.github.com/mohashari/1b48079a62e34de2a6f1e42eab9510d7.js?file=snippet-6.txt"></script>

## Production Failure Modes and Mitigation Strategies

While this architecture is highly performant, systems programming in shared memory exposes edge cases that traditional TCP loopbacks abstract away. Production designs must account for these failure modes.

### 1. Cold-Start Page Faults and Latency Spikes
When a process calls `mmap`, the OS creates virtual memory mappings but does not immediately assign physical RAM pages. Instead, pages are allocated lazily upon first access. This triggers a minor page fault, stopping execution for up to 50 microseconds. 

In low-latency systems, these cold-start spikes are unacceptable. We mitigate this using two strategies:
* Use the `MAP_POPULATE` flag during `mmap` to force the kernel to pre-populate the page tables during the call.
* Call `mlock` on the mapped region to pin it in RAM, preventing the Linux kernel from swapping these pages to disk under memory pressure.

<script src="https://gist.github.com/mohashari/1b48079a62e34de2a6f1e42eab9510d7.js?file=snippet-7.txt"></script>

### 2. Peer Death and Unclean Shutdown
If the consumer process is terminated by `SIGKILL` or crashes while holding a lock-free slot, the producer might write into a saturated queue and block on the `eventfd` indefinitely. Worse, the file descriptor mapping in `/dev/shm` remains, leaking resources.
* **Cleanup on Drop**: Register signal handlers (`signal-hook`) to clean up `/dev/shm` nodes using `shm_unlink` during clean shutdowns.
* **Peer Liveness Check**: Before blocking or writing, perform a quick check on the peer process using `kill(peer_pid, 0)`. If the process is dead, handle the socket close sequence and fall back to the slow path or restart the connection.
* **Failsafe Timeout**: When using async streams, wrap writes with a timeout. If the write fails to clear after a threshold, assume the reader has hung and abort.

### 3. Memory Corruption and Bounds Checking
Because both processes have read-write access to the same shared memory, a corrupted pointer or buffer overflow in one process can crash or corrupt the other process.
* **Defensive Pointer Validation**: The receiver must validate that read offsets derived from the shared `tail` index do not point outside the mapped shared memory bounds.
* **Drop Privileges**: Set restrictive permissions on the shared memory file (e.g., `0600`) so that only the authorized peer processes can map it.
* **Data Sanitization**: Never serialize raw Rust structs containing internal pointers (like `Vec` or `String`) directly to shared memory. Only write plain old data (POD) structures, raw bytes, or serialized formats (like Protocol Buffers or FlatBuffers).

### 4. Tuning CPU Affinity and Cache Contention
IPC performance is heavily dependent on hardware topology. 
* If both processes run on the same logical CPU core (Hyper-Thread), they compete for execution units, limiting throughput.
* If they run on different NUMA nodes, cache lines must travel across the interconnect fabric (e.g., AMD's Infinity Fabric or Intel's UPI), degrading latency.
* **Optimal Affinity**: Pin the producer and consumer to different physical cores on the same CPU socket using `sched_setaffinity` or `numactl`. This ensures they share the fast L3 cache while running fully in parallel.