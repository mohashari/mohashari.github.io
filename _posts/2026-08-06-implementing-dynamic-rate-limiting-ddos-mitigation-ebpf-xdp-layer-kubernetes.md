---
layout: post
title: "Implementing Dynamic Rate-Limiting and DDOS Mitigation at the eBPF XDP Layer in Kubernetes"
date: 2026-08-06 08:00:00 +0700
tags: [ebpf, kubernetes, devsecops, rate-limiting, networking]
description: "Protect Kubernetes ingress from high-volume DDoS attacks by implementing dynamic, low-overhead rate-limiting directly at the network card level using eBPF XDP."
image: "https://picsum.photos/seed/3655/1080/720"
thumbnail: "https://picsum.photos/seed/3655/400/300"
---

It is 3:00 AM on a Friday, and your Kubernetes cluster is collapsing under a 10 million packet-per-second (pps) HTTP flood. Your ingress controller pods (Envoy, Nginx, or Traefik) are pegged at 100% CPU, struggling to negotiate TLS handshakes and parse HTTP headers for requests that should be dropped immediately. The Horizontal Pod Autoscaler (HPA) starts spawning more replicas, but the control plane bottlenecks as database connections saturate and node resource starvation triggers cascading failures. Standard ingress-level rate limiting is useless here because the CPU cost of accepting the TCP connection, reading the L7 payload, and sending a `429 Too Many Requests` response is enough to crash your nodes. To survive a volumetric DDoS attack at scale, you must drop the traffic before the kernel allocates network buffers (`sk_buff`) or manages connection state. This means moving rate limiting down to the Express Data Path (XDP) layer using eBPF, dropping hostile packets directly at the network interface card (NIC) level.

## The Limits of L7 and Netfilter Rate Limiting

Standard rate limiting fails in volumetric DDoS scenarios because of where it runs in the Linux network stack. 

When a packet arrives at a physical network interface, the NIC driver allocates a socket buffer struct (`sk_buff`) to represent the packet in kernel memory. The kernel then processes the packet through the IP stack, executing Netfilter rules (iptables/nftables) and tracking connection state via `nf_conntrack`. Finally, the packet is handed to a user-space socket buffer where an ingress controller (like Envoy or Nginx) parses HTTP headers to apply rate limits.

At 100,000 pps, this pipeline begins to struggle:
1. **conntrack Exhaustion**: Netfilter tracks every connection state. Under a spoofed IP flood, the conntrack table fills rapidly, throwing `nf_conntrack: table full, dropping packet` errors. Legitimate connections are dropped randomly.
2. **sk_buff Allocation Overhead**: Allocating and freeing `sk_buff` structs for millions of hostile packets consumes substantial kernel memory bandwidth and CPU cycles.
3. **Context Switching and TLS Handshakes**: L7 rate-limiters require the TCP handshake to complete and, in most cases, the TLS handshake to finish before they can inspect headers (like `X-Forwarded-For` or `Authorization`) to determine if a request should be dropped. This consumes massive cryptographic processing power.

By contrast, an XDP program runs directly inside the network driver's polling loop (`napi_poll`) before the kernel allocates the `sk_buff` or processes Netfilter rules. If the program returns `XDP_DROP`, the packet is immediately discarded, freeing the CPU core to process the next packet. A single modern CPU core running XDP can drop up to 15 million packets per second, compared to iptables which starts choking around 200,000 pps.

## Architecture: Integrating XDP with Kubernetes Pod Networking

Deploying XDP in a Kubernetes environment requires mapping host network interfaces to pod traffic. In standard CNI configurations (like Calico or Cilium in non-eBPF mode), traffic enters the host network namespace via a physical interface (e.g., `eth0`), gets routed by the host's IP routing tables, and passes through virtual ethernet interfaces (`veth` pairs) into the network namespaces of individual pods.

To protect the pods, we must attach our XDP rate-limiter program to the host's physical network interface (`eth0`). This intercepts all inbound packets before they are routed to the pod network namespaces.

The system consists of two primary components:
1. **Kernel-space Program (eBPF)**: Written in C, this code inspects raw packets, parses IPv4/IPv6 headers, queries eBPF maps containing rate-limit counters and blocklists, and returns `XDP_DROP` or `XDP_PASS`.
2. **User-space Daemon (Go)**: Written in Go using the `cilium/ebpf` library, this daemon runs as a Kubernetes DaemonSet. It loads the eBPF program, pins BPF maps to `/sys/fs/bpf`, monitors traffic patterns or reads blocklist instructions from a central controller (like a Prometheus alert manager or dynamic security coordinator), and updates the BPF maps in real time.

## The Kernel-Space eBPF Program

Let's examine the kernel-space code. The eBPF program uses a token bucket algorithm to enforce rate limits. We use a `BPF_MAP_TYPE_LRU_HASH` map to track the IP addresses and token counts of incoming traffic. We choose an Least Recently Used (LRU) hash map because during a DDoS attack, a regular hash map will fill up, causing updates to fail. The LRU map automatically evicts older entries when the map saturates, ensuring we can always rate-limit the most active attackers without running out of BPF memory.

<script src="https://gist.github.com/mohashari/0dfa23d6fecb88d75b383eca7ab68d3d.js?file=snippet-1.txt"></script>

## Parsing Headers Safely: Satisfying the BPF Verifier

The eBPF verifier is notoriously strict. It statically analyzes BPF bytecode before loading it to guarantee it cannot crash the kernel or access out-of-bounds memory. Every memory read must be explicitly bounds-checked against the boundaries of the packet buffer.

In `snippet-1`, we perform two vital checks:
1. `(void *)(eth + 1) > data_end`: Checks that the ethernet header does not extend past the end of the packet payload.
2. `(void *)(iph + 1) > data_end`: Checks that the IP header fits completely within the parsed packet space.

If either check fails, we return `XDP_PASS`, letting the Linux network stack handle the packet (where it will be dropped naturally as corrupt). If you omit these checks, the verifier will reject the program with an `invalid access to packet` error message, preventing the load phase.

## Building the User-Space Daemon in Go

The user-space daemon is responsible for compiling the BPF C program (typically into an ELF object via clang/llvm), loading it into the kernel, attaching it to host network interfaces, and updating the rate-limiting configuration parameters.

We use the `github.com/cilium/ebpf` package. To ensure the rate-limiter maps persist if the Go daemon restarts or crashes, we use BPF pinning. Pinning maps to a virtual filesystem (`/sys/fs/bpf`) allows the kernel-space application to continue processing packets without interruption even if the user-space process is absent.

<script src="https://gist.github.com/mohashari/0dfa23d6fecb88d75b383eca7ab68d3d.js?file=snippet-2.go"></script>

## Kubernetes Deployment Strategy

To deploy this in production, the user-space daemon must run as a DaemonSet to secure every worker node in the Kubernetes cluster. The container needs high-level privileges:
1. `hostNetwork: true` to access host interfaces like `eth0`.
2. `securityContext.privileged: true` to grant access to BPF-related syscalls and map pinning.
3. Bidirectional volume mounts to interact with `/sys/fs/bpf` on the host.

<script src="https://gist.github.com/mohashari/0dfa23d6fecb88d75b383eca7ab68d3d.js?file=snippet-3.yaml"></script>

## Observability: Tracking Blocked Traffic with Atomic Stats

An XDP rate-limiter is incomplete without observability. You must know what traffic is being allowed and what is being dropped. In eBPF, writing stats can lead to race conditions if multiple CPU cores try to update a metric concurrently. We solve this using standard atomic primitives (`__sync_fetch_and_add`) to count packet actions per IP.

We define a second BPF map `stats_map` to store stats data.

<script src="https://gist.github.com/mohashari/0dfa23d6fecb88d75b383eca7ab68d3d.js?file=snippet-4.txt"></script>

To expose these metrics to Prometheus, the Go daemon runs a custom collector that queries the pinned `stats_map` and maps network byte order keys into string format IPs.

<script src="https://gist.github.com/mohashari/0dfa23d6fecb88d75b383eca7ab68d3d.js?file=snippet-5.go"></script>

## Garbage Collection of BPF Maps and Time Synchronization

If we choose a regular hash map (`BPF_MAP_TYPE_HASH`) for tracking packet statistics or dynamic blocklists, we must clean up stale entries in user-space to prevent map saturation. 

A common pitfall when cleaning up BPF maps from Go is comparing times. The eBPF kernel function `bpf_ktime_get_ns()` returns the monotonic time in nanoseconds *since boot*, not standard wall-clock epoch time. If you compare `last_seen` with Go's `time.Now().UnixNano()`, you will calculate incorrect durations.

To solve this, the Go collector should fetch the actual kernel monotonic time using `unix.CLOCK_MONOTONIC` via syscalls.

<script src="https://gist.github.com/mohashari/0dfa23d6fecb88d75b383eca7ab68d3d.js?file=snippet-6.go"></script>

## Real-World Failure Modes & Mitigation Strategies

Implementing BPF XDP at scale exposes several critical infrastructure failure modes that you must prepare for:

### 1. Generic vs Native XDP

XDP runs in three modes:
* **Native XDP**: The BPF code is executed directly by the NIC driver inside its polling loop, before any allocations occur. This is the optimal path but requires driver support (supported by `ixgbe`, `i40e`, `mlx5`, `virtio_net`, etc.).
* **Offloaded XDP**: The BPF program is loaded directly onto a SmartNIC. This offers the highest performance but requires specialized hardware.
* **Generic XDP**: Used as a fallback when the NIC driver does not support native mode. The kernel executes the BPF program *after* allocating the `sk_buff` buffer inside the driver framework.

In production, running Generic XDP during a volumetric attack will still saturate the CPU with kernel buffer allocations. You must verify native support on your nodes' NICs before enabling enforcement:

```bash
# Check if XDP is supported natively by checking driver capacity
ethtool -i eth0
```

Ensure your Go code loads BPF with native flags (`link.XDPLinkMode` or `link.XDPDriverMode`) rather than `link.XDPGenericMode` in production.

### 2. Multi-Queue NIC Core Saturation

Modern NICs distribute incoming packets across multiple hardware queues, routing interrupts to different CPU cores via RSS (Receive Side Scaling). If an attacker targets a single connection profile and the RSS hashing configuration pins all those packets to a single CPU queue, that specific CPU core will hit 100% capacity processing XDP loops while other cores remain idle.

Ensure your worker node interrupt configuration (`irqbalance`) is optimized and check core saturation under load:

```bash
watch -n 1 cat /proc/interrupts
```

If you observe uneven queue loading, configure RSS hashing on the host using `ethtool` to distribute traffic based on L3 and L4 properties:

```bash
ethtool -N eth0 rx-flow-hash tcp4 sdfn
```

### 3. CNI Chain Incompatibilities

If your Kubernetes cluster uses a modern CNI that is itself built on eBPF (like Cilium), the CNI might already attach its own XDP or TC programs to the host interfaces. Loading your custom XDP rate-limiter program can overwrite the CNI's hooks, breaking all cluster ingress routing.

If you are using Cilium, standard practice is to utilize Cilium's native XDP integration or chain your logic into Cilium's pipeline. Cilium provides a mechanism to run custom XDP filters inside its control loop or chain load multiple BPF programs using `libbpf` multi-program loading features.

## Conclusion

Protecting ingress resources at the edge of the Kubernetes cluster is no longer optional. When standard ingress routing layers choke under heavy volumetric attacks, adopting eBPF XDP allows security engineers to drop threat actors before they hit kernel networking resources. By writing verifier-safe C code, managing limits in a user-space Go controller, and running observability pipelines, you can build a resilient rate-limiting layer capable of handling millions of packets per second.