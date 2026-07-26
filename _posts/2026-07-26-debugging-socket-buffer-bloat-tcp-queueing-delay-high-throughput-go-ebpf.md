---
layout: post
title: "Debugging Socket Buffer Bloat and TCP Queueing Delay in High-Throughput Go Services with eBPF"
date: 2026-07-26 08:00:00 +0700
tags: [go, ebpf, networking, performance, observability]
description: "Surgically measure and eliminate hidden kernel-level TCP queueing delay and socket buffer bloat in Go services using eBPF and TCP_NOTSENT_LOWAT."
image: "https://picsum.photos/seed/3819/1080/720"
thumbnail: "https://picsum.photos/seed/3819/400/300"
---

Imagine a P99.9 latency alert firing at 3:00 AM. Your high-throughput Go-based payment ingestion gateway, serving 45,000 requests per second (RPS), is suddenly seeing tail latencies surge from 3ms to 420ms. You check your CPU utilization: it is sitting comfortably at 35%. Memory is flat, and there are no signs of Go garbage collection pauses in your runtime metrics. Your application-level APM traces show that database operations are executing in sub-millisecond times, yet the HTTP/gRPC transaction span durations are ballooning. Standard networking tools show no packet loss and zero retransmissions. What you are witnessing is socket buffer bloat and TCP queueing delay—a silent killer in high-throughput Go services where bytes are accepted and buffered by the kernel faster than the network interface card (NIC) can drain them or the peer can acknowledge them. Because these delays happen inside the Linux kernel's queueing structures after the Go runtime has handed off the payload via the `write` system call, application-level instrumentation is blind to them.

![Debugging Socket Buffer Bloat and TCP Queueing Delay in High-Throughput Go Services with eBPF Diagram](/images/diagrams/debugging-socket-buffer-bloat-tcp-queueing-delay-high-throughput-go-ebpf.svg)

## The Mechanics of TCP Socket Buffer Bloat

To understand socket buffer bloat, we must dive into how the Linux kernel manages TCP socket memory. When a Go application writes data to a `net.Conn`, the bytes are not immediately serialized onto the physical wire. Instead, the `write()` or `sendmsg()` system call copies the data from user-space memory into kernel-allocated buffers known as `sk_buff` (socket buffers), which are queued in the socket's write queue (`sk_write_queue`).

The kernel manages this queue size dynamically using TCP autotuning. The maximum amount of memory a single TCP socket can allocate for its send buffer is governed by the sysctl configuration `net.ipv4.tcp_wmem`, which consists of three values: minimum, default, and maximum bytes. On modern Linux distributions, the maximum limit is frequently set to 16MB.

While TCP autotuning is excellent for maximizing throughput across long-fat networks (LFNs) by scaling the TCP window to match the Bandwidth-Delay Product (BDP), it introduces a major vulnerability in high-throughput, low-latency microservices. If a client becomes transiently slow, or if downstream network switches experience micro-burst congestion, the sender’s congestion window (`cwnd`) shrinks. However, the Go application continues writing to the socket at its original rate.

Because the socket's send buffer capacity is large (e.g., 4MB to 16MB), the kernel will happily accept megabytes of data from the Go process, copying it to the `sk_write_queue` and returning a `200 OK` or successful system call execution status. This unsent data sits in the kernel queue, waiting to be sent as the TCP congestion control algorithm allows. If a socket has 4MB of unsent data queued on a path with an active bottleneck speed of 100 Mbps (due to cross-AZ link sharing or client-side congestion), that data will experience:

$$\text{Queueing Delay} = \frac{4 \times 1024 \times 1024 \times 8 \text{ bits}}{100,000,000 \text{ bps}} \approx 335.5 \text{ ms}$$

This 335ms delay is entirely queueing latency inside the kernel. The Go service registers the write as taking less than 50 microseconds because the copy-to-kernel operation completed immediately.

## Why Go's Netpoll Can Hide Queueing Latency

Go's runtime manages network connections using non-blocking sockets and multiplexes them using `epoll` on Linux (integrated through the runtime network poller, or `netpoll`). When you execute `conn.Write(payload)`, the Go runtime attempts to write directly to the socket file descriptor using the `write` system call:

1. If the socket's write queue has sufficient space, the system call copies the bytes, returns the number of bytes written, and Go's runtime continues execution immediately.
2. If the socket's write queue is completely full, the system call returns `EAGAIN` or `EWOULDBLOCK`. The Go runtime catches this, registers the file descriptor with `epoll` for write events (`EPOLLOUT`), parks the executing goroutine, and yields the operating system thread (M) to run other goroutines.
3. Once the kernel transmits enough data to drop the socket memory usage below the queue limits, `epoll` notifies the runtime, which unparks the goroutine to complete the write.

The critical issue is that Go's runtime only blocks and schedules out the goroutine when the kernel buffer is *completely* full. If the buffer is large (e.g., 8MB) and is filled with unsent bytes, Go will continue writing successfully without blocking, as long as it does not exceed the limit. The time spent by packets sitting in the kernel queue waiting for transmission is invisible to Go's application metrics. The goroutine thinks the write succeeded instantly, while the client waits hundreds of milliseconds for the packets to actually arrive.

## Tracing TCP Queueing Latency with eBPF

To expose this invisible latency, we must measure the time elapsed between when a block of data is written by the application to the socket, and when that data is actually transmitted over the wire. This can be accomplished surgically using eBPF by hooking two low-level kernel entry points:

1. `kprobe/tcp_sendmsg`: Invoked when the Go application writes to the socket. We record the entry timestamp and associate it with the `struct sock` pointer.
2. `kprobe/tcp_write_xmit`: Invoked when the TCP engine processes the socket's write queue to transmit packets onto the device driver. We calculate the delta between the current timestamp and the recorded `tcp_sendmsg` timestamp.

The following eBPF program, written in C, implements this measurement pipeline and streams latency events to user-space using a ring buffer.

<script src="https://gist.github.com/mohashari/45107c26c40aacc6daaf7fb81355f2ab.js?file=snippet-1.txt"></script>

In the eBPF C code above, the `__attribute__((packed))` directive on `struct event` is crucial. It forces the compiler to lay out the struct fields sequentially without any padding bytes. This guarantees that when Go deserializes the binary payload using the `encoding/binary` library, the struct alignment matches perfectly on both the C and Go sides, preventing subtle memory corruption or parsing bugs.

## Loading the eBPF Program and Polling Events in Go

To load the eBPF program, attach it to our kernel hooks, and consume the event stream, we use the `cilium/ebpf` library. It offers full support for CO-RE and integrates natively with Go’s runtime.

<script src="https://gist.github.com/mohashari/45107c26c40aacc6daaf7fb81355f2ab.js?file=snippet-2.go"></script>

To compile and link the eBPF code directly into Go binary artifacts, we use the following script to generate the Go code wrapper around the compiled ELF binary.

<script src="https://gist.github.com/mohashari/45107c26c40aacc6daaf7fb81355f2ab.js?file=snippet-6.sh"></script>

## Bubbling Backpressure Up via TCP_NOTSENT_LOWAT

Now that we have verified kernel queuing delay is occurring, how do we fix it? The most elegant solution is to force the kernel's queueing pressure back into the Go runtime where the runtime's normal scheduler can manage it.

We can achieve this using the `TCP_NOTSENT_LOWAT` socket option. Introduced in Linux 3.12, `TCP_NOTSENT_LOWAT` (Not Sent Low Watermark) controls the threshold of *unsent* bytes allowed in the socket write queue.

When this option is configured (for example, to 16KB), the kernel changes its behavior:
- When the application writes data, the kernel copies it to the socket buffer.
- As long as the amount of data in the write queue that is *unsent* (i.e. not yet sent because of the congestion window or network limits) is below 16KB, the socket remains writable.
- The moment the unsent data exceeds 16KB, the kernel marks the socket as non-writable and returns `EAGAIN` to the write system call.
- This forces the Go runtime's netpoll to park the writing goroutine.

Instead of buffering megabytes of unsent data in the kernel and creating queueing delays, `TCP_NOTSENT_LOWAT` blocks the Go goroutine at `conn.Write()`. The writing latency is now exposed inside the Go application as write duration, allowing you to trigger timeouts, apply rate limits, or return load shedding responses to clients.

The code below shows how to configure a custom Go `net.Listener` that applies `TCP_NOTSENT_LOWAT` to all accepted TCP connections.

<script src="https://gist.github.com/mohashari/45107c26c40aacc6daaf7fb81355f2ab.js?file=snippet-3.go"></script>

## Simulating and Verifying the Backpressure Behavior

To verify that setting `TCP_NOTSENT_LOWAT` successfully shifts latency from the kernel back to the Go application, we can use a benchmark test that simulates a slow consumer. In the test below, the writer writes large blocks of data as fast as possible to a reader that intentionally reads slowly.

<script src="https://gist.github.com/mohashari/45107c26c40aacc6daaf7fb81355f2ab.js?file=snippet-4.go"></script>

## Exposing Socket Latency Telemetry

To integrate our eBPF measurements into a standard observability stack, we can capture the ring buffer event loop and expose the metrics as Prometheus histograms.

<script src="https://gist.github.com/mohashari/45107c26c40aacc6daaf7fb81355f2ab.js?file=snippet-5.go"></script>

## Tuning the Production Host

Applying `TCP_NOTSENT_LOWAT` inside your Go application is most effective when paired with corresponding configuration changes in the host kernel. Traditional congestion control algorithms like CUBIC are loss-based; they aggressively fill queues until packet loss occurs, which contributes to buffer bloat.

Instead, switch to Google's BBR (Bottleneck Bandwidth and Round-trip propagation time) congestion control. BBR is a model-based algorithm that estimates the optimal bandwidth and propagation round-trip time, actively avoiding queue buildup at the bottleneck link. BBR requires the Fair Queueing (`fq`) traffic control queueing discipline (`qdisc`) to function correctly, which also enables the pacing required for `TCP_NOTSENT_LOWAT`.

Apply the configuration below to your service containers or bare-metal host hosts via sysctl:

<script src="https://gist.github.com/mohashari/45107c26c40aacc6daaf7fb81355f2ab.js?file=snippet-7.txt"></script>

## Production Verification with Linux ss

Once the sysctl tuning is applied and your Go application is running with `TCP_NOTSENT_LOWAT`, you can verify the configuration in production using the `ss` (socket statistics) tool.

Execute the command below under load to output low-level TCP statistics:

```bash
ss -ti ostate=established dst 10.0.0.0/8
```

Look for the following fields in the output:

```text
cubic wscale:7,7 rto:200 rtt:12.4/0.2 cwnd:10 ssthresh:7 notsent:128000 write_queue:256000
```

- **`write_queue`**: The total size in bytes of the data currently queued in the kernel for transmission.
- **`notsent`**: The size in bytes of the unsent data currently sitting in the queue.

Without `TCP_NOTSENT_LOWAT`, you will frequently see `notsent` values rise to several megabytes when communicating with slow clients. With `TCP_NOTSENT_LOWAT` set to `16384`, the `notsent` value will stay capped below 16KB. Any extra bytes written by Go will be held in the user-space goroutine stack, keeping the kernel queues small and latency under control.