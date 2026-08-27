---
layout: post
title: "Detecting DNS Query Packet Loss: Correlating Linux UDP Socket Buffer Overflows with eBPF and Otel"
date: 2026-08-27 08:00:00 +0700
tags: [ebpf, opentelemetry, linux-networking, dns, observability]
description: "Solve silent DNS packet drops due to UDP socket buffer overflows. Learn to trace drops using eBPF and correlate them with OpenTelemetry client spans."
image: "https://picsum.photos/seed/8647/1080/720"
thumbnail: "https://picsum.photos/seed/8647/400/300"
---

Picture this: a peak-traffic event triggers a sudden spike in 99th-percentile latency across your microservices. Downstream HTTP calls start failing with `context deadline exceeded` and Go DNS lookups throw sporadic `i/o timeout` errors. Your application dashboards show healthy CPU, memory, and thread counts. CoreDNS reports zero errors and sub-millisecond latencies. Yet, your application logs are littered with network timeouts. Standard TCP retransmissions don't show any anomaly because DNS runs over UDP, and here is the silent killer: the kernel is dropping incoming DNS responses inside the client application's own UDP socket receive buffer (`sk_rcvbuf`) before your application even gets a chance to read them. These buffer overflows leave no trace in application-level network metrics, and the standard command-line tools only offer global counter aggregates. To diagnose this, we must dive into the Linux network stack with eBPF and stream telemetry directly into OpenTelemetry (Otel) to correlate low-level socket overflows with high-level client spans.

## The Mechanics of Linux UDP Buffer Overflows

Unlike TCP, which implements sliding windows, active flow control, and automatic retransmissions at the L4 transport layer, UDP is a connectionless, fire-and-forget protocol. If a client application is slow to read from a TCP socket, TCP's receive window shrinks, throttling the sender. UDP has no such backpressure mechanism. When a DNS query is sent, the client opens an ephemeral UDP socket and waits. When the DNS server responds, the packet arrives at the network interface card (NIC), travels up the IP layer, and is routed to the UDP layer.

At this stage, the kernel executes `udp_queue_rcv_skb()` to enqueue the packet (represented as an `sk_buff` struct) into the socket's receive queue. Before doing so, it calls `sock_queue_rcv_skb()`, which validates whether the memory consumed by the socket's receive queue (`sk_rmem_alloc`) plus the memory size of the new packet (`skb->truesize`) exceeds the socket's configured receive buffer limit (`sk->sk_rcvbuf`).

If this check fails, the kernel increments the global SNMP counter `UDP_MIB_RCVBUFOVERFLOWS` (exposed in `/proc/net/snmp` as `RcvbufErrors`) and calls `kfree_skb()` to discard the packet. For the application, the packet simply vanished. It blocks on a `recvfrom` syscall until the client timeout is hit, resulting in a generic timeout error.

Standard tools like `netstat -su` or `ss -u -a` are too coarse. They show that overflows are happening on the host, but they cannot tell you *which* socket, *which* PID, or *which* DNS Transaction ID was dropped. This makes it impossible to debug in a multi-tenant Kubernetes cluster.

## Bridging the Gap: Tracing Socket Drops with eBPF

To trace these drops in real-time, we write an eBPF program that hooks into `udp_queue_rcv_skb`. Since we want to capture packets that were *actually* dropped, we use a kprobe/kretprobe pair:

1. `kprobe/udp_queue_rcv_skb` captures the arguments: the socket `struct sock *sk` and the packet `struct sk_buff *skb`. We store these in a BPF hash map keyed by the current thread ID (`pid_tgid`).
2. `kretprobe/udp_queue_rcv_skb` intercepts the return value. If the return value is non-zero (indicating a failure to queue, usually `-ENOMEM`), we look up the socket and packet from our map, extract the connection details, read the raw DNS payload, and emit an event to a BPF Ring Buffer.

Here is the implementation of the eBPF C program:

<script src="https://gist.github.com/mohashari/f4760550e48ff2295c63a7318272d339.js?file=snippet-1.txt"></script>

In this kernel-level C code, we leverage BPF CO-RE (Compile-Once, Run-Everywhere) to inspect internal members of `struct sock` and `struct sk_buff`. The use of `BPF_MAP_TYPE_RINGBUF` is standard here because it offers lower overhead and better memory utilization characteristics compared to the legacy Perf Event Array.

## Userspace Event Loop and DNS Parsing in Go

Once the kernel agent emits a drop event, our userspace Go daemon reads it from the ring buffer. Rather than parsing the complex DNS packet structures (with variable-length labels and pointers) inside the kernel—which is notoriously difficult due to BPF verifier stack limits and loop constraints—we pass the raw 128 bytes of the DNS payload to userspace.

The Go agent reads this event, casts it to our struct, and parses the DNS header. The first 12 bytes of a DNS payload contain:
- Transaction ID (2 bytes)
- Flags (2 bytes)
- Question Count (2 bytes)
- Answer Count (2 bytes)
- Authority Count (2 bytes)
- Additional Count (2 bytes)

Directly following the header is the Question section, starting with the QNAME. We parse the QNAME label-by-label (where each label is prefixed by its length) to rebuild the domain string.

<script src="https://gist.github.com/mohashari/f4760550e48ff2295c63a7318272d339.js?file=snippet-2.go"></script>

This Go binary acts as a daemon running on your Kubernetes nodes. It loads the eBPF code using `cilium/ebpf` and runs a high-performance event loop processing packet drops. Notice the simple label-parsing utility which reconstructs the queried domain safely.

## Structured Logging and Metrics with OpenTelemetry

Now that we have parsed the drop event, we must export it to our observability stack. We will use the OpenTelemetry Logs SDK to emit a structured log record containing all network metadata, socket utilization percentages, and the DNS transaction details. By outputting this as an Otel log, we can automatically ingest it into platforms like Grafana Loki or Elasticsearch.

<script src="https://gist.github.com/mohashari/f4760550e48ff2295c63a7318272d339.js?file=snippet-3.go"></script>

The calculated attribute `system.linux.udp.buffer_utilization` is critical here. It provides a numeric ratio indicating how saturated the socket buffer was when the drop occurred. If the ratio is close to 1.0 (or higher, because truesize can push it past the limit), it provides irrefutable proof that the packet was dropped due to kernel queue limits, rather than transient packet loss in transit.

## Instrumenting the Client Application for Correlation

We have the kernel drop event, but how do we prove that a specific timeout in a microservice was caused by this exact drop? 

DNS clients generate a random 16-bit Transaction ID (Query ID) for each outgoing request. When the server responds, it mirrors this ID. If we instrument our backend application to record the Transaction ID and the local ephemeral port used for the UDP socket, we can perfectly correlate the client's timeout span with the kernel's drop log.

Most languages hide the DNS transaction details inside standard libraries. In Go, the default resolver does not expose the raw UDP transaction. We can bypass this by using the `github.com/miekg/dns` package and wrapping the network dialer to capture both the local port and the DNS Message ID.

<script src="https://gist.github.com/mohashari/f4760550e48ff2295c63a7318272d339.js?file=snippet-4.go"></script>

By recording the `dns.query.id` and `network.local.port` attributes in your client trace spans, correlation is simplified. When an I/O timeout is recorded by the client, you can query your log and trace ingestion platform for:

`span.dns.query.id == log.dns.query.id AND span.network.local.port == log.network.local.port`

This match gives you a 100% deterministic root-cause analysis: the timeout was not due to DNS server latency, but rather the client kernel dropping the packet due to a saturated socket buffer.

## Production Alerting and Mitigation Strategies

To proactively monitor UDP drops, we can export the occurrences as a Prometheus metric (e.g., `dns_udp_buffer_drops_total`) and configure Prometheus alerting.

<script src="https://gist.github.com/mohashari/f4760550e48ff2295c63a7318272d339.js?file=snippet-5.yaml"></script>

If you alert on this metric, what are the steps to fix it?

### 1. Increase System-Wide and Socket-Level Receive Buffers

The default maximum UDP receive buffer size on Linux is often too small for high-throughput microservices (often defaulting to 208 KB). You can bump the system limits via `sysctl`:

```bash
sysctl -w net.core.rmem_max=26214400
sysctl -w net.core.rmem_default=26214400
```

However, changing the system limits is only half the battle. Go programs and JVM runtimes frequently construct UDP sockets with fixed socket options, bypassing system defaults. You must explicitly configure the socket receive buffer size inside your application's connection setups (e.g., using `conn.SetReadBuffer(8 * 1024 * 1024)` on Go's `net.UDPConn`).

### 2. Implement Node-Local DNS Caching

In a Kubernetes cluster, the most effective way to eliminate DNS UDP buffer overflows is to deploy `NodeLocal DNSCache`. This daemon runs as a DaemonSet on every cluster node. The application container queries the daemon over a loopback address or a local Unix socket. 

Because loopback packet transmission is instantaneous and bypasses network queuing bottlenecks, the application's UDP receive buffer is drained immediately. The local cache daemon then queries CoreDNS upstream, handling buffer management and failovers gracefully.

### 3. Fallback to TCP

If your microservice performs high-concurrency external DNS resolution, configure your DNS client to fallback to TCP. TCP handles buffer congestion gracefully via sliding windows and does not silently drop packets when buffers saturate.