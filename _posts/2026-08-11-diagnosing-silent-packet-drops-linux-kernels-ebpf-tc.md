---
layout: post
title: "Diagnosing Silent Packet Drops in Linux Kernels Using eBPF and tc"
date: 2026-08-11 08:00:00 +0700
tags: [ebpf, linux, networking, debugging, site-reliability]
description: "Learn how to track down silent kernel-level packet drops using eBPF, tc, and tracepoints to solve intermittent networking issues in production."
image: "https://picsum.photos/seed/6388/1080/720"
thumbnail: "https://picsum.photos/seed/6388/400/300"
---

Imagine running a high-throughput microservices architecture on Kubernetes where suddenly your p99 latencies spike and client requests fail with intermittent `504 Gateway Timeout` errors. You check application logs, verify HTTP metrics, and query TCP stats like `TcpRetransSegs` or `TcpOutRsts`, but everything appears perfectly clean. You run a standard `tcpdump` on your ingress proxy and observe that packets arrive at the network interface but never make it to the application socket. In high-performance environments, packets frequently vanish inside the Linux kernel network stack without generating TCP resets or ICMP destination unreachable messages. These are "silent packet drops"—scenarios where firewall rules, routing validations, queue overruns, or protocol checks discard socket buffers deep within kernel space. Standard monitoring utilities like `netstat` and `ifconfig` only increment coarse counters, leaving you completely blind. To diagnose these issues without guessing, we must write custom eBPF instrumentation to hook into kernel drop paths and reconstruct exactly where and why packets are discarded.

![Diagnosing Silent Packet Drops in Linux Kernels Using eBPF and tc Diagram](/images/diagrams/diagnosing-silent-packet-drops-linux-kernels-ebpf-tc.svg)

## The Anatomy of a Silent Packet Drop

When a network interface card (NIC) receives an incoming packet, it uses Direct Memory Access (DMA) to copy the packet data into a ring buffer. The NIC then raises a hard interrupt, triggering the kernel's New API (NAPI) subsystem to poll the driver in softirq context. The kernel wraps the raw packet data in a socket buffer structure—the `sk_buff` (or `skb`). The `sk_buff` is the single most critical data structure in the Linux networking subsystem, carrying packet payload, protocol metadata, routing states, and socket ownership pointers from the driver all the way to user space.

As the `sk_buff` traverses the kernel, it goes through a lifecycle of allocation, processing, and eventual deallocation. There are two primary functions responsible for freeing an `sk_buff`:

*   `consume_skb()`: Called when a packet has been successfully processed, such as when it is delivered to a user-space socket or successfully transmitted out of an interface.
*   `kfree_skb()`: Called when a packet must be discarded due to a processing error, a buffer overflow, a policy violation, or routing failure.

A "silent drop" occurs when the kernel calls `kfree_skb()` instead of routing the packet. The sender remains unaware because no TCP Reset (RST) or ICMP packet is sent back, and the receiver's socket never receives any data, resulting in a connection timeout.

The root causes of silent drops fall into four major categories:

1.  **Netfilter/iptables Drops:** Firewall rules configured with a `DROP` target discard packets silently. The netfilter framework returns `NF_DROP`, which ultimately results in `kfree_skb()`.
2.  **Reverse Path Filtering (rp_filter):** The kernel validates that the source IP address of an incoming packet is routable through the same interface it arrived on. If routing is asymmetric and `rp_filter` is enabled, the packet is silently discarded at `fib_validate_source()`.
3.  **Queue/Buffer Exhaustion:** If a service cannot read from a TCP socket fast enough, the socket's receive buffer fills up. Once it hits the limits defined by `net.core.rmem_max` or `net.ipv4.tcp_rmem`, incoming packets are silently discarded during TCP segment processing (`tcp_v4_rcv()`).
4.  **IP/TCP Stack Failures:** Packets with invalid checksums, malformed headers, or expired TTL values fail basic validation checks and are dropped early in the `ip_rcv()` handler.

Because `kfree_skb()` is the central graveyard for packets, it is the logical point to attach our instrumentation.

## Hooking into the Kernel: tc vs. XDP vs. Tracepoints

Before writing code, we must evaluate where to attach our eBPF probes. The Linux kernel provides three primary hooking mechanisms for network observability, each with distinct trade-offs:

1.  **XDP (eXpress Data Path):** XDP executes eBPF programs directly inside the network driver before the kernel allocates the `sk_buff`. While XDP is extremely fast and ideal for high-rate DDoS mitigation, it is blind to the rest of the kernel. Because the packet has not yet traversed the IP routing table or Netfilter, XDP cannot trace drops that happen inside the IP or TCP stack.
2.  **tc (Traffic Control) Ingress/Egress:** Placed right after the `sk_buff` is allocated, `tc` runs filters and classifiers (using `SCHED_CLS`) to shape and police traffic. Instrumenting `tc` is useful when you suspect drops caused by traffic shaping, token bucket filters, or egress queue limits.
3.  **Tracepoints (`skb:kfree_skb`):** Tracepoints are static hooks compiled directly into the kernel source. The `skb:kfree_skb` tracepoint fires every time a packet buffer is discarded. Since kernel v5.17, this tracepoint includes a `reason` code mapping directly to an enum of drop reasons (e.g., `SKB_DROP_REASON_NETFILTER`, `SKB_DROP_REASON_TCP_CSUM`). In older kernels, the tracepoint provides the raw `location` (the instruction pointer where `kfree_skb` was called), which we can resolve into a human-readable kernel symbol.

For comprehensive drop diagnostics, we combine `tc` filters (to inspect packet fields at the ingress boundary) with the `skb:kfree_skb` tracepoint (to catch the execution context of the drop).

## Building the eBPF Dropped Packet Tracker

To build our drop tracker, we will write a C-based eBPF program that hooks into the `skb:kfree_skb` tracepoint. The program will parse the network headers of the discarded `skb`, extract source and destination IP addresses, L4 protocol, and port numbers, and stream them to user space via a Perf ring buffer along with the instruction pointer of the kernel drop location.

<script src="https://gist.github.com/mohashari/80a9e0f1cbbe0faff3237d0bbb6e19b1.js?file=snippet-1.txt"></script>

Now, we write the user-space daemon in Go using the `cilium/ebpf` library. This daemon loads the compiled eBPF program, pins it to the tracepoint, reads the raw byte stream from the perf ring buffer, and prints out drops in real time.

<script src="https://gist.github.com/mohashari/80a9e0f1cbbe0faff3237d0bbb6e19b1.js?file=snippet-2.go"></script>

## Injecting and Diagnosing with tc (Traffic Control)

Network issues can also stem from active queuing and policing rules configured within the `tc` (Traffic Control) layer. If an operator configures rate limiting or ingress shaping incorrectly, packets are dropped at the interface boundary before the IP layer handles them. To trace these types of failures, we hook directly into the `tc` subsystem.

Here is a C-based eBPF program targeting the `tc` ingress classifier (`classifier` section). This filter acts as a security policy, dropping all traffic destined to port `9999` and keeping track of the drop metrics using an internal BPF hash map.

<script src="https://gist.github.com/mohashari/80a9e0f1cbbe0faff3237d0bbb6e19b1.js?file=snippet-3.txt"></script>

To deploy this program on a network interface (e.g., `eth0`), we compile the BPF code using `clang` and attach it to the `clsact` queuing discipline (qdisc) via the `iproute2` `tc` command:

<script src="https://gist.github.com/mohashari/80a9e0f1cbbe0faff3237d0bbb6e19b1.js?file=snippet-4.sh"></script>

Any packets arriving on `eth0` targetting port `9999` are immediately discarded with `TC_ACT_SHOT`. Because this drop happens within Traffic Control, standard user space sockets never register an event, but our `tc_drop_stats` map records the drop events.

## Correlating Drops with Kernel Callstacks

When `kfree_skb` intercepts a dropped packet, it returns the raw virtual memory address of the kernel instruction that executed the drop (the Program Counter). To troubleshoot the failure, you must translate this raw hex value into a readable kernel symbol.

The Linux kernel maps its internal functions to memory addresses dynamically at boot time (especially with Kernel Address Space Layout Randomization, or KASLR). You can access these mappings through `/proc/kallsyms`. Because the address of the drop point will fall somewhere inside a function rather than at its exact start, we must parse `/proc/kallsyms`, filter for executable kernel symbols, and use binary search to locate the closest preceding address to calculate the offset.

<script src="https://gist.github.com/mohashari/80a9e0f1cbbe0faff3237d0bbb6e19b1.js?file=snippet-5.py"></script>

Running this resolver maps raw memory addresses to specific kernel functions:

*   `nf_hook_slow+0x6d`: The packet was dropped by Netfilter. An `iptables` or `nftables` policy is discarding the packet.
*   `fib_validate_source+0x1b0`: The packet was dropped during source address route validation. This indicates a reverse-path routing filter mismatch (`rp_filter`).
*   `tcp_v4_rcv+0x80`: The drop happened in the TCP engine, pointing to socket queue limits, bad checksums, or protocol state mismatches (e.g., out-of-order queue overflow).

## Real-World Outage Walkthrough: The Case of the Silent rp_filter Drop

Consider a real-world production incident. An infrastructure team configured a Kubernetes database cluster. To keep database traffic secure, each node was configured with two network interfaces: `eth0` for cluster control-plane traffic, and `eth1` for a private database network.

After deploying the database clients, the pods could ping the databases, but any attempt to establish a PostgreSQL TCP connection hung and timed out. Running `tcpdump` on the database pod showed outgoing SYN packets on `eth1`, but no response. Running `tcpdump` on the database node's host interface showed the SYN packets arriving on `eth1`, but they vanished before entering the container's namespace.

To diagnose the problem, the team deployed our eBPF tracker as a privileged DaemonSet across the nodes.

<script src="https://gist.github.com/mohashari/80a9e0f1cbbe0faff3237d0bbb6e19b1.js?file=snippet-6.yaml"></script>

After deploying the DaemonSet, the logs printed this line repeatedly during connection attempts:

```text
[17923491823] Drop at fib_validate_source+0x1b0 (0xffffffff818a4d70) Reason: 0 | Proto: 6, Src: 10.200.10.45:49280 -> Dst: 10.200.20.100:5432 | Len: 60
```

The function `fib_validate_source` performs source IP address verification. If the kernel's routing tables indicate that a packet originating from `10.200.10.45` should be routed back out through interface `eth0`, but the packet actually arrived on `eth1`, the kernel detects a potential IP spoofing attempt. It marks the packet as invalid and drops it.

This behavior is governed by the sysctl configuration `net.ipv4.conf.all.rp_filter` (Reverse Path Filtering). The cluster had set this configuration to `1` (Strict Mode). Because the response routing path was asymmetric, the incoming packets on `eth1` were dropped.

To fix the issue, the team modified the configuration on the database network interface to loose mode (`2`), allowing routing tables to validate paths dynamically without discarding valid asymmetric routes:

```bash
sysctl -w net.ipv4.conf.eth1.rp_filter=2
```

The drops immediately dropped to zero, and the TCP handshake completed successfully.

## Production Considerations and Overhead

Running tracing programs in production environments requires managing performance overhead. The `skb:kfree_skb` tracepoint fires on every single packet drop. In a cluster node handling hundreds of thousands of requests per second, even a 1% packet loss rate can yield thousands of drop events per second.

Copying every drop event from the kernel to user space via a Perf or BPF ring buffer requires constant context switching, memory allocation, and CPU scheduling. If the ring buffer fills up, events are lost, and the monitoring daemon itself can become a system bottleneck.

To run this safely in production, we shift the data aggregation into the kernel using an eBPF hash map (`BPF_MAP_TYPE_HASH`). Instead of sending a message for every packet, the eBPF program updates an in-kernel counter indexed by the drop location and reason. The user-space daemon then polls the map at a fixed interval (e.g., once every 5 seconds) to collect aggregated counters and export them to Prometheus.

<script src="https://gist.github.com/mohashari/80a9e0f1cbbe0faff3237d0bbb6e19b1.js?file=snippet-7.txt"></script>

By using atomic operations (`__sync_fetch_and_add`) in kernel space, we keep the tracing overhead extremely low (under 1% CPU utilization), ensuring that monitoring does not degrade packet processing performance even under heavy network load.

## Conclusion and Best Practices

Diagnosing networking issues by guessing iptables configurations, tracing with generic TCP dumps, or rebooting virtual nodes leads to prolonged downtime. Silent packet drops are a built-in behavior of the Linux network stack, but they can be fully observed with the right tools.

To maintain network observability in production:

1.  **Expose Drop Metrics:** Run an eBPF drop daemon on all Kubernetes nodes, exporting `skb:kfree_skb` stats directly to Prometheus.
2.  **Inspect, Don't Guess:** When troubleshooting, locate the kernel symbol via `/proc/kallsyms` to identify the code path responsible for the drop (e.g., Netfilter vs. routing validation) before changing any network configuration.
3.  **Optimize Instrumentation:** Use kernel-side map aggregation to prevent Perf ring buffer exhaustion and minimize context-switching overhead under high network throughput.

By leveraging eBPF and `tc`, you eliminate the blind spots in your network stack and can track packets accurately from the wire to the socket.