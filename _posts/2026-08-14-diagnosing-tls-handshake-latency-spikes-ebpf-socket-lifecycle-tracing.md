---
layout: post
title: "Diagnosing TLS Handshake Latency Spikes Using eBPF Socket Lifecycle Tracing in Production"
date: 2026-08-14 08:00:00 +0700
tags: [ebpf, tls, latency, networking]
description: "A deep dive into diagnosing transient TLS handshake latency spikes in production using custom eBPF socket lifecycle tracing and OpenSSL uprobes."
image: "https://picsum.photos/seed/4168/1080/720"
thumbnail: "https://picsum.photos/seed/4168/400/300"
---

When running a high-throughput edge proxy or API gateway under tight service level objectives (SLOs), the 99th percentile (p99) latency of incoming requests is the ultimate benchmark. But in production, you will periodically encounter a highly frustrating symptom: sudden, transient latency spikes in client connections, where a request that normally takes 10ms suddenly hangs for 300ms, 1s, or even 3s before completing. The application logs report no delay, application CPU usage remains stable, and downstream database operations are lightning-fast. The bottleneck lies in the network handshake. While TCP handshakes establish quickly, the TLS handshake phase frequently experiences transient delays that are invisible to application APMs. Traditional packet captures like `tcpdump` are too resource-heavy to run continuously on thousands of production cores, while socket-level stats via `ss` only show you aggregated snapshots. To dissect these anomalies without degrading performance, we must leverage eBPF (Extended Berkeley Packet Filter) to trace the lifecycle of a socket directly within the Linux kernel, correlating TCP state changes with user space cryptography library hooks.

![Diagnosing TLS Handshake Latency Spikes Using eBPF Socket Lifecycle Tracing in Production Diagram](/images/diagrams/diagnosing-tls-handshake-latency-spikes-ebpf-socket-lifecycle-tracing.svg)

## Anatomy of a TLS Latency Spike: TCP vs. TLS

To diagnose a slow TLS handshake, you must first isolate whether the delay occurs at the transport layer (TCP) or the presentation layer (TLS). A complete connection establishment consists of the TCP 3-way handshake followed by the TLS cryptographic exchange. The total time elapsed before the application receives the first byte of ciphertext spans multiple network round-trip times (RTTs) and user space processing slices:

1. **TCP Handshake (1 RTT):** Client sends `SYN`, Server responds with `SYN-ACK`, Client returns `ACK`.
2. **TLS Handshake (1 to 2 RTTs):** Client sends `Client Hello`. The server negotiates the cipher suite, sends `Server Hello`, `Certificate`, and `Key Exchange` parameters. Client and server verify the certificates, perform the key exchange, and send `Finished` messages.

In a healthy system, this entire sequence completes in under 100ms. However, three distinct production failure modes can inflate this duration by orders of magnitude:

* **TCP Listen Queue Overflow:** When your application event loop is saturated, or when the process experiences a garbage collection pause, it fails to call `accept()` on the socket. The kernel queues new connections in the `listen()` backlog. If the backlog overflows, the kernel drops incoming `SYN` packets (or delays the `SYN-ACK`). The client, receiving no response, retries the `SYN` packet. Under RFC 6298 guidelines, the initial TCP retransmission timeout (RTO) defaults to a minimum of 1.0 second. This results in a sharp, bi-modal latency distribution showing spikes of exactly 1.0, 3.0, or 7.0 seconds.
* **CPU Throttling on Cryptographic Operations:** Diffie-Hellman key exchanges (such as X25519 or ECDHE-RSA) require intensive CPU calculations. If your application or edge proxy is running inside a Docker container throttled by the Linux Completely Fair Scheduler (CFS) bandwidth control (e.g., Kubernetes CPU limits), the scheduler will pause the thread during the handshake execution. A 5ms cryptographic compute cycle can stretch to 500ms if the thread is throttled across multiple CFS periods.
* **MTU Black-Holing of Large Certificates:** The TLS `Server Hello` and `Certificate` payload can easily exceed 2-4KB. Since the standard Ethernet MTU is 1500 bytes, the IP stack must fragment this payload into multiple packets. If a hop along the path (like a VPN tunnel, transit router, or overlay network interface) has a lower MTU (e.g., 1420 bytes) and blocks ICMP "Fragmentation Needed" packets (Type 3, Code 4), the fragmented packets are dropped silently. The client's TCP stack receives the first fragment but stalls waiting for the second, eventually triggering a TCP retransmission timeout.

## Why eBPF is the Tool of Choice

Traditional debugging tools fail to resolve these issues in production. Running `tcpdump` to capture raw packets across a cluster introduces a massive memory and CPU burden. Writing packets to disk can saturate block device I/O, while pipe buffers to user space drop packets under heavy loads. Furthermore, packet captures do not log internal kernel details, such as the exact moment a socket transitioned from the SYN queue to the accept queue, or the CPU scheduling delays experienced by the user space SSL library.

eBPF solves this by placing low-overhead, sandboxed hooks directly inside kernel pathways and user space executables. 

We can target the kernel TCP stack via stable tracepoints, such as `sock:inet_sock_set_state`, which triggers every time a socket changes its state. To capture the user space TLS handshake, we can attach uprobes (user space probes) to shared libraries like OpenSSL (`libssl.so.3`) or target compiled Go binaries. By sharing state between these hooks using high-performance BPF maps, we can measure the exact latency of every socket lifecycle phase without copying packet payloads or logging redundant information.

## Writing the eBPF Kernel Probe (C)

To implement this tracer, we begin by writing the kernel-space eBPF program in C. This program tracks TCP connection establishment by intercepting state transitions. We use the tracepoint `sock:inet_sock_set_state`, which exposes socket structure pointers (`struct sock *`) and state identifiers.

The source code for our TCP tracing module is saved in [`tcp_trace.bpf.c`](file:///home/muklis/Documents/exploring/blog/src/tcp_trace.bpf.c). It uses BPF CO-RE (Compile Once - Run Everywhere) to maintain compatibility across different kernel versions.

<script src="https://gist.github.com/mohashari/8c72a6764008b838c4ded8f715bdbae7.js?file=snippet-1.txt"></script>

Next, we trace the TLS handshake duration. In OpenSSL, client and server handshakes are processed through [`SSL_do_handshake`](file:///usr/include/openssl/ssl.h). We attach a uprobe to the entry of this function to record the start time, and a uretprobe to the exit of the function to calculate the elapsed duration.

This logic is implemented in [`tls_trace.bpf.c`](file:///home/muklis/Documents/exploring/blog/src/tls_trace.bpf.c):

<script src="https://gist.github.com/mohashari/8c72a6764008b838c4ded8f715bdbae7.js?file=snippet-2.txt"></script>

## The User Space Orchestrator (Go)

With the C kernel components in place, we write a user space collector daemon in Go using the [`cilium/ebpf`](https://github.com/cilium/ebpf) framework. The orchestrator's task is to load the Compiled Object files, attach the probes to their target symbols, and pull events from the kernel ring buffers.

The entry point is in [`main.go`](file:///home/muklis/Documents/exploring/blog/src/main.go):

<script src="https://gist.github.com/mohashari/8c72a6764008b838c4ded8f715bdbae7.js?file=snippet-3.go"></script>

Now we implement the parsing and correlation logic in [`correlation.go`](file:///home/muklis/Documents/exploring/blog/src/correlation.go). The correlator calculates TCP state durations and associates TLS handshakes by tracing thread contexts.

<script src="https://gist.github.com/mohashari/8c72a6764008b838c4ded8f715bdbae7.js?file=snippet-4.go"></script>

## Compiling and Deploying the eBPF Tracer

We compile the kernel code to BPF assembly using `clang` and run our Go monitoring daemon with the root capabilities required to manipulate eBPF maps and attach tracepoints:

<script src="https://gist.github.com/mohashari/8c72a6764008b838c4ded8f715bdbae7.js?file=snippet-5.sh"></script>

To run this in a production Kubernetes environment, we deploy the monitor as a DaemonSet. The Pod must execute in the host namespace (`hostNetwork: true`, `hostPID: true`) and run with elevated security permissions. It maps key host directories: `/sys/kernel/debug` for debug logs, `/lib/modules` for kernel modules, and `/usr/lib` to locate the dynamic libraries (like `/usr/lib/x86_64-linux-gnu/libssl.so.3`) on the host filesystem.

The configuration is saved in [`daemonset.yaml`](file:///home/muklis/Documents/exploring/blog/src/daemonset.yaml):

<script src="https://gist.github.com/mohashari/8c72a6764008b838c4ded8f715bdbae7.js?file=snippet-6.yaml"></script>

## Production Case Studies: Real-World Incidents Solved

With this tooling running across our edge layer, we can inspect three real-world incident scenarios solved by tracing socket lifecycles via eBPF.

### Case Study 1: The GC-induced TCP Backlog Drop
An API gateway experienced p99 latency spikes of exactly 1000ms. Application APM traces reported the slow execution, but pointed to internal routers without detail. 

Checking our eBPF trace logs, we identified a series of connections with abnormal handshakes:
```text
[TCP CONNECTED] 192.168.10.15:43210 -> 10.0.2.100:443 | Handshake Time: 1001.24ms
[TLS HANDSHAKE] PID: 34901 | TID: 34912 | SSL*: 0x7f83a4005b60 | Duration: 1.82ms | Status: SUCCESS
```
The TCP handshake time was inflated to over a second, while the subsequent TLS handshake resolved in less than 2ms. This decoupled pattern indicates that the packet loss did not happen on the wire, nor was it caused by crypto-engine bottlenecks. Because the TCP handshake delay was exactly 1 second, it pointed directly to a TCP SYN packet drop and subsequent retransmission backoff. 

Further investigation showed that the API gateway's Go process was undergoing heavy Garbage Collection sweeps, causing momentary runtime pauses. During these pauses, the process stopped calling `accept()`, causing the TCP listen queue (configured with a small backlog of 128) to saturate. The Linux kernel dropped incoming SYNs, triggering the clients to hit the standard 1-second RTO.

### Case Study 2: Docker CPU Throttling on TLS Key Exchange
A cluster running an Nginx-based Ingress Controller was experiencing p99 latency spikes on API endpoints. These spikes occurred during peak traffic hours, but host CPU usage stayed below 50%. 

The eBPF trace logs revealed a different signature:
```text
[TCP CONNECTED] 192.168.10.22:58392 -> 10.0.2.120:443 | Handshake Time: 0.74ms
[TLS HANDSHAKE] PID: 12051 | TID: 12051 | SSL*: 0x55cda89bc100 | Duration: 354.12ms | Status: SUCCESS
```
Here, the TCP handshake succeeded instantly (0.74ms), but the TLS handshake took over 350ms. Since the network round-trip was fast, this latency was caused by slow CPU processing during the TLS key exchange. 

Looking at the cgroup limits, we found the Ingress Controller was limited to 4 CPU cores (`resources.limits.cpu = 4`). Although the average node usage was low, Nginx threads saturated the cgroup allocation limit during spikes in concurrent handshakes. The Linux CFS scheduler throttled the container, scheduling out the SSL execution threads mid-handshake. Removing the rigid CPU limits and transitioning to CPU request-based scaling resolved the issue.

### Case Study 3: The MTU/MSS Mismatch on VPN/Overlay Networks
An application communicating with an external financial payment provider experienced transient 3.0-second connection timeouts. 

Our eBPF daemon logged the following events:
```text
[TCP CONNECTED] 10.10.4.12:49822 -> 203.0.113.5:443 | Handshake Time: 34.50ms
[TLS HANDSHAKE] PID: 8904 | TID: 8905 | SSL*: 0x7fcbb000a200 | Duration: 3042.10ms | Status: FAILED (ret=-1)
```
The TCP handshake succeeded in 34.50ms, matching the physical WAN round-trip time. However, the TLS handshake failed after stalling for exactly 3 seconds (the client-side handshake timeout). 

This delay points to an MTU black-holing issue. The initial TCP handshake packets (`SYN` and `SYN-ACK`) are small (typically <120 bytes) and easily pass through restricted-MTU pathways. However, the TLS `Server Hello` containing the full certificate chain exceeded 3KB. 

The route to the payment gateway went through an IPSec VPN tunnel with an MTU limit of 1400 bytes. Because path routers had ICMP requests blocked, Path MTU Discovery failed, and the large fragmented packets were dropped silently. The connection stalled until the TCP stack timed out. Adjusting the TCP Maximum Segment Size (MSS) clamping setting on our router interface (`iptables -A FORWARD -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu`) solved the MTU bottleneck.

## Best Practices and Production Safeguards

While eBPF offers unprecedented observability, running probes in a high-load production environment requires careful precautions:

* **Mitigate Uprobe Overhead:** Unlike kernel kprobes which execute in under 50-100 nanoseconds, uprobes involve kernel-to-user-space context switches. When a thread hits a uprobe, the CPU triggers a trap, transitions to kernel space to run the BPF code, and switches back to user space. This sequence adds roughly 1.5 to 2.5 microseconds of overhead per call. For control-plane routines like `SSL_do_handshake`, which run once per connection, this overhead is negligible. However, you must never attach uprobes to hot paths such as `SSL_read` or `SSL_write`, which execute millions of times per second. Doing so will degrade the application's overall performance.
* **Map Size Configuration and Eviction:** An unresolved connection trace can leave orphan entries in BPF maps (for instance, if a client disconnects abruptly before completing the TLS handshake). Set realistic bounds on hash map sizes and implement active sweep sweeps in your user space collector daemon to delete entries older than 10 seconds.
* **Kernel Stability and CO-RE:** Always use BPF CO-RE (Compile Once - Run Everywhere) helpers (`BPF_CORE_READ()`) when reading data from internal structs like `struct sock`. Direct memory accesses can read incorrect offsets or trigger kernel panics if the underlying struct definitions change between different kernel versions in production.