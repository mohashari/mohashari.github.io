---
layout: post
title: "Implementing a Zero-Copy Message Broker in Go Using Linux splice(2) and TCP Zero-Copy"
date: 2026-07-09 08:00:00 +0700
tags: [go, linux, networking, performance]
description: "Learn how to build an ultra-high-throughput message broker in Go using Linux splice(2) and TCP Zero-Copy to eliminate user-space memory copies and GC overhead."
image: "https://picsum.photos/seed/6309/1080/720"
thumbnail: "https://picsum.photos/seed/6309/400/300"
---

At a production scale of 10 Gbps or 40 Gbps, traditional network I/O in Go quickly becomes CPU-bound. When building high-throughput message brokers or reverse proxies that route gigabytes of data per second, the naive approach of reading payloads into user-space byte slices and writing them back to target sockets introduces severe performance bottlenecks. A single 10 Gbps stream transfers approximately 1.25 GB of data per second; at a standard MTU of 1500 bytes, this translates to over 830,000 packets per second. Copying this data across the kernel-user space boundary multiple times triggers massive CPU cache invalidations, continuous TLB page-table walks, and intensive Garbage Collection (GC) write barriers. This post details how to bypass the Go runtime's allocation overhead and build a zero-copy message routing engine using Linux `splice(2)` and TCP Zero-Copy (`SO_ZEROCOPY`), enabling your broker to saturate line-rate network interfaces at minimal CPU utilization.

![Implementing a Zero-Copy Message Broker in Go Using Linux splice(2) and TCP Zero-Copy Diagram](/images/diagrams/implementing-zero-copy-message-broker-go-splice-tcp-zerocopy.svg)

## The Cost of Traditional Network I/O in Go

Before optimizing, it is critical to dissect why standard I/O patterns degrade under high-throughput networking. In a standard Go TCP broker, the routing pipeline relies on the `io.Reader` and `io.Writer` interfaces. This setup requires allocating a buffer (typically via a `sync.Pool` of `[]byte`) in user space, issuing a `Read` syscall to pull bytes from the socket, inspecting the protocol header, and issuing a `Write` syscall to push the payload to the outbound connection.

This simple workflow incurs major system-level costs:

1. **Context Switches:** Each read and write operation requires a transition from user mode to kernel mode and back. In a high-throughput broker handling millions of packets, these context switches consume significant CPU cycles.
2. **Double Copying:** The payload must be copied from the network card (NIC) to the host via DMA, then copied from the kernel socket buffer (`sk_buff`) to the user-space Go buffer via the CPU. During egress, this process is reversed, copying the data from user space back down to the target kernel socket buffer before the NIC transmits it.
3. **Go GC and Runtime Overhead:** While pooling buffers using `sync.Pool` mitigates heap allocations, the pointers passed to syscalls must still be managed. Go's runtime must ensure that these slices are not moved or collected while the kernel is performing I/O. Furthermore, the sheer volume of memory throughput pollutes the L1/L2/L3 CPU caches, forcing the CPU to constantly fetch data from system RAM, which introduces latency spikes and limits total throughput.

Under these conditions, a Go application attempting to route 10 Gbps of traffic often spends 60% of its CPU time executing kernel copies and runtime scheduling, leading to unpredictable latency tails and throughput drops.

## Kernel-Level Splicing with `splice(2)`

To eliminate the kernel-user space memory copies, Linux provides the `splice(2)` system call. Introduced in kernel 2.6.17, `splice` moves data between two file descriptors without copying it between kernel space and user space. 

```c
#define _GNU_SOURCE
#include <fcntl.h>

ssize_t splice(int fd_in, loff_t *off_in, int fd_out, loff_t *off_out,
               size_t len, unsigned int flags);
```

The fundamental restriction of `splice` is that at least one of the two file descriptors must refer to a pipe. Splicing from a socket directly to another socket is not supported by the Linux kernel. Instead, the broker must splice data from the source socket into a pipe, and then splice from that pipe to the destination socket. 

This mechanism is highly efficient because a Linux pipe is represented in the kernel as a ring buffer of page references (`struct pipe_buffer`), rather than a physical memory buffer containing the payload. When you splice from a socket to a pipe, the kernel simply populates the pipe's ring buffer with references to the physical memory pages containing the network packets (stored in the kernel's socket buffer). When you splice from the pipe to the destination socket, the kernel copies these page references into the target socket's transmission queue. The actual data payload never leaves kernel space and is never copied by the CPU. It is moved directly from the ingress network buffer to the egress network buffer.

To implement this in Go, we must manage a pool of pipes to avoid the overhead of allocating and destroying pipe file descriptors on every connection.

<script src="https://gist.github.com/mohashari/b1cef6ef3a12d23c331c16434e8b3d74.js?file=snippet-1.go"></script>

## Integrating `splice` with the Go Netpoller

Go's network implementation relies on non-blocking file descriptors managed by the internal runtime netpoller (using `epoll` on Linux). If we attempt to perform raw, blocking system calls on a socket's file descriptor, we bypass Go's cooperative scheduler. This can block OS threads and lead to thread starvation. 

To correctly integrate `splice` with Go's asynchronous runtime, we must retrieve the raw file descriptors from the `net.TCPConn` object and wrap our syscall logic in Go's `syscall.RawConn` control functions. This lets the runtime netpoller handle socket readiness events (`EAGAIN` or `EWOULDBLOCK`) and safely yield control when I/O operations would block.

First, we need to extract the raw file descriptors:

<script src="https://gist.github.com/mohashari/b1cef6ef3a12d23c331c16434e8b3d74.js?file=snippet-2.go"></script>

Using the `syscall.RawConn`, we can schedule reads and writes. The `Read` and `Write` methods of `RawConn` accept a callback function that runs in a loop. If the callback returns `false`, the runtime netpoller parks the goroutine until the file descriptor becomes ready. Once it is ready, the callback is executed again.

Below is the core implementation of the non-blocking `splice` operation, transferring bytes from a source socket to a destination socket through our pipe pool.

<script src="https://gist.github.com/mohashari/b1cef6ef3a12d23c331c16434e8b3d74.js?file=snippet-3.go"></script>

## Designing the Frame Parser and Routing Engine

A production message broker cannot blindly copy data from one socket to another. It must parse frame headers to determine routing targets, topics, and message flags before splicing the payload.

To implement zero-copy routing, we use a hybrid model:
1. **User Space Header Parsing:** Read a tiny, fixed-size header (e.g., 12 bytes containing metadata and payload size) using normal socket reads. This data is small enough that CPU copying overhead is negligible.
2. **Kernel Space Payload Splicing:** Once the broker extracts the routing target and payload size, it passes the raw sockets and payload size to `SpliceTransfer`. The payload is streamed directly to the destination socket at the kernel level.

The following snippet demonstrates this framing protocol and routing flow.

<script src="https://gist.github.com/mohashari/b1cef6ef3a12d23c331c16434e8b3d74.js?file=snippet-4.go"></script>

## Advanced: TCP Zero-Copy TX (`MSG_ZEROCOPY`)

While `splice(2)` works well for routing payloads between sockets, there are times when your broker needs to generate messages or load payloads from local memory (e.g., from an in-memory ring buffer) and send them zero-copy. 

For this scenario, Linux offers the `MSG_ZEROCOPY` flag (available in kernels 4.14+). When transmitting data via `send(2)` with `MSG_ZEROCOPY`, the kernel avoids copying the user-space buffer into the kernel's transmission buffer. Instead, it pins the user-space pages directly and uses DMA to send the data directly from user memory.

Using `MSG_ZEROCOPY` requires two steps:
1. **Enable the option:** Set `SO_ZEROCOPY` on the socket using `setsockopt`.
2. **Handle completions:** Because the kernel sends data directly from your user-space buffer, you cannot reuse or free that buffer immediately. You must wait for the kernel to write a completion notification to the socket's error queue (`MSG_ERRQUEUE`). Only after receiving this notification is it safe to recycle the buffer.

The code below shows how to configure `SO_ZEROCOPY` and parse notifications from the error queue in Go.

<script src="https://gist.github.com/mohashari/b1cef6ef3a12d23c331c16434e8b3d74.js?file=snippet-5.go"></script>

## Production Gotchas and Failure Modes

Zero-copy techniques can dramatically improve performance, but they require careful design to avoid production issues. When deploying these patterns under high load, watch out for the following critical areas:

### 1. Pipe Pool Leakage and `EMFILE`
Every pipe you create consumes two file descriptors (one for read, one for write). If your broker leaks pipes (e.g., if a connection closes unexpectedly and you fail to return the pipe to the pool), your application will quickly exhaust the system's file descriptor limit, leading to `EMFILE` errors.
* **Mitigation:** Wrap your connections in robust recovery blocks. Ensure pipes are returned to the pool using `defer` immediately after retrieving them.

### 2. Splicing Stalls and Slow Clients
`splice(2)` blocks if the target socket's send buffer is full. If a consumer connection experiences a TCP window stall (due to network congestion or a slow client), the `splice` call will pause. Because the pipeline is shared, a stalled write to the target socket can block the read from the source socket.
* **Mitigation:** Use non-blocking pipes combined with short timeouts. Monitor your writer performance. If a client stalls, abort the splice, close the connection, and free the resource to prevent head-of-line blocking for other subscribers.

### 3. Payload Thresholds for `MSG_ZEROCOPY`
Using `MSG_ZEROCOPY` involves significant overhead. Pinning memory pages, updating page tables, and polling `MSG_ERRQUEUE` consumes CPU cycles. For small payloads, this overhead can exceed the cost of a simple `copy` in memory.
* **Production Threshold:** Only use `MSG_ZEROCOPY` for payloads larger than **16 KB** to **32 KB**. For smaller messages, standard memory copies are faster and more efficient.

### 4. Adjusting Pipe Capacities
The default capacity of a Linux pipe is 64 KB. Under high latency or high-bandwidth scenarios (high Bandwidth-Delay Product), a 64 KB buffer is too small to saturate the network link. It will fill up quickly, causing the source socket to block.
* **Mitigation:** Use the `F_SETPIPE_SZ` call (as shown in snippet-1) to increase the pipe buffer capacity to 1 MB. To do this, you may need to adjust the system-wide maximum limit in `/proc/sys/fs/pipe-max-size`.

## Tuning the Linux Kernel for High-Performance Networking

To maximize the performance of a zero-copy broker, you must also tune the underlying Linux kernel. Create a custom configuration in `/etc/sysctl.d/99-broker.conf` to optimize the socket buffers:

```ini
# /etc/sysctl.d/99-broker.conf
# Maximize TCP buffer memory allocations (min, default, max in bytes)
net.ipv4.tcp_rmem = 4096 87380 16777216
net.ipv4.tcp_wmem = 4096 65536 16777216

# Increase max backlog queue size for connections
net.core.netdev_max_backlog = 100000
net.core.somaxconn = 65535

# Enable TCP BBR Congestion Control for high-throughput pipes
net.core.default_qdisc = fq
net.ipv4.tcp_congestion_control = bbr
```

Applying these adjustments ensures the Linux kernel has sufficient memory buffers to scale TCP windows, preventing network bottlenecks and allowing `splice(2)` to operate at peak efficiency. By bypassing the user-space memory boundary, your Go-based broker can run at line rate while keeping CPU usage and GC pause times to a minimum.