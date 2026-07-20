---
layout: post
title: "Debugging DNS Resolution Latency Spikes in Kubernetes: Profiling CoreDNS and UDP Packet Drops with eBPF"
date: 2026-07-20 08:00:00 +0700
tags: [kubernetes, coredns, ebpf, networking, troubleshooting]
description: "A deep dive into diagnosing transient 5-second DNS latency spikes in Kubernetes using bpftrace, Go/C eBPF tracers, and CoreDNS profiling."
image: "https://picsum.photos/seed/9719/1080/720"
thumbnail: "https://picsum.photos/seed/9719/400/300"
---

A p99 tail latency spike of exactly 5.002 seconds in a high-throughput Kubernetes microservice is a signature networking failure. It is the distinct footprint of a dropped UDP packet combined with the default glibc resolver timeout. In a containerized ecosystem, DNS resolution is the silent runtime bottleneck. Because pods are ephemeral and dynamically routed, almost every network connection relies on the CoreDNS cluster. However, the combination of glibc's default resolver configuration, Kubernetes' DNS search path resolution, and the Linux kernel's Netfilter connection tracking (conntrack) engine creates a perfect storm for transient UDP packet drops. When these drops occur, the connectionless nature of UDP means there is no kernel-level retransmission. The application-level resolver must wait for its default timeout—which in most Linux containers defaults to 5 seconds—before resending the query.

## The Anatomy of the 5-Second Latency Spike

To understand why these packets are dropped, we must first look at how Kubernetes configures DNS for containers. By default, every pod's `/etc/resolv.conf` is populated with a `search` line containing several suffixes: `<namespace>.svc.cluster.local`, `svc.cluster.local`, `cluster.local`, and so on. Additionally, the `options ndots:5` parameter is set.

The `ndots` parameter defines the threshold of dots a domain name must contain before the resolver attempts a query as an absolute name first (without appending search suffixes). If a domain has fewer than 5 dots (which almost all external domains like `api.github.com` or `payment.gateway.com` do), the resolver will sequentially append each search suffix in the list and query the DNS server. Only if all these suffix-based queries fail (returning `NXDOMAIN`) will it finally attempt to resolve the name as a fully qualified domain name (FQDN).

For a query to `api.github.com` (which has 2 dots), the glibc resolver performs the following sequential queries:
1. `api.github.com.production.svc.cluster.local` (NXDOMAIN)
2. `api.github.com.svc.cluster.local` (NXDOMAIN)
3. `api.github.com.cluster.local` (NXDOMAIN)
4. `api.github.com` (SUCCESS)

For each search suffix, the resolver queries for both `A` (IPv4) and `AAAA` (IPv6) records. Because glibc performs these queries concurrently by opening two separate UDP sockets and sending packets in parallel, a single lookup of `api.github.com` triggers 8 separate UDP packets (4 search paths × 2 record types). This design amplifies DNS traffic and triggers the kernel race condition.

## The Conntrack Confirmation Race Condition

When these parallel UDP packets traverse the Linux network stack, they go through the Netfilter conntrack (connection tracking) module. Because pods run in their own network namespaces, traffic leaving the pod must undergo Source Network Address Translation (SNAT) or Masquerading to be routed on the host network.

The conntrack module keeps track of these translations. The flow for a UDP packet traversing Netfilter involves two main steps:
1. `nf_conntrack_in`: The packet is parsed, and conntrack checks if there is an existing flow tracking this tuple (source IP/port, destination IP/port, protocol). If not, a new "unconfirmed" conntrack entry is allocated.
2. `nf_conntrack_confirm`: Just before the packet leaves the network stack, the kernel attempts to commit the unconfirmed conntrack entry to the global conntrack table.

When a client sends parallel `A` and `AAAA` queries, they are sent from the same container IP to the same DNS server IP (CoreDNS service IP) on port 53. Because they are sent concurrently, `nf_conntrack_in` processes both packets before either has been confirmed. Consequently, both packets are assigned the same SNAT tuple or collide on their connection tracking states.

When the first packet reaches the `nf_conntrack_confirm` step, it successfully inserts its entry into the global conntrack table. When the second packet reaches `nf_conntrack_confirm` immediately after, the kernel detects a collision because an identical conntrack tuple already exists. Since UDP is connectionless, the kernel has no state machine to merge these flows or buffer them. Instead, the kernel calls `nf_ct_resolve_clash` or, if that fails, simply drops the second packet.

Because UDP does not have a kernel-level retransmission mechanism (unlike TCP, which would retransmit the SYN packet after 1 second), the dropped packet is lost. The glibc resolver, expecting responses for both `A` and `AAAA` queries, waits on the socket. Since one of the responses will never arrive, the resolver blocks until its application-level timeout expires—exactly 5 seconds later—before re-opening the sockets and retrying.

## Profiling CoreDNS: Ruling out Application-level Bottlenecks

Before jumping into kernel-level tracing, you must first eliminate CoreDNS itself as the source of the latency. If CoreDNS is CPU-throttled or experiencing lock contention inside its cache or Kubernetes plugins, DNS queries will queue up, causing high latency.

You can verify CoreDNS health by checking its Prometheus metrics. CoreDNS exposes several key metrics:
- `coredns_dns_request_duration_seconds_bucket`: A histogram of the time CoreDNS takes to process and respond to queries.
- `coredns_dns_responses_total`: The total count of DNS responses, partitioned by response code (e.g., `NOERROR`, `NXDOMAIN`, `SERVFAIL`).

If your client observes 5-second latencies but `coredns_dns_request_duration_seconds_bucket` shows that 99.9% of queries are resolved in under 2 milliseconds, then the delay is not caused by CoreDNS processing speed. Instead, the packet is being dropped in transit—either on the way to CoreDNS or on the return path to the client pod.

To profile CoreDNS CPU and memory usage, you can enable the `pprof` plugin in your `Corefile` and collect a profile during a latency event. This is critical for identifying lock contention in high-throughput environments.

<script src="https://gist.github.com/mohashari/8967040dd167bd49610182a82eb8e3c0.js?file=snippet-5.txt"></script>

By enabling the `pprof` endpoint on port 8080, we can analyze the application performance profiles using standard Go tooling. If you suspect lock contention within the caching layer or Kubernetes controllers, run:
`go tool pprof http://<coredns-pod-ip>:8080/debug/pprof/profile`

## Tracing Drops with eBPF: Why tcpdump Lies

When debugging transient packet drops, the standard tool of choice is often `tcpdump` (or `wireshark`). However, in Kubernetes environments, `tcpdump` can be highly deceptive.

`tcpdump` hooks into the network stack at the level of the packet socket (`AF_PACKET`) using `libpcap`. In Linux, this hook points before Netfilter's conntrack confirmation stage for outgoing packets. This means that if you run `tcpdump` inside the client container, you will see both the `A` and `AAAA` query packets leaving the virtual interface. However, because the drop occurs during `nf_conntrack_confirm` inside the kernel *after* the packet socket tap, the packet is silently discarded by the kernel and never actually hits the physical wire or host network interface. You will see the packet in `tcpdump` but it will never arrive at the destination.

To see the truth, you must look directly inside the kernel's execution path. This is where eBPF (Extended Berkeley Packet Filter) is invaluable. Instead of capturing packets, we trace the kernel events where packets are discarded.

In the Linux kernel, whenever a socket buffer (`sk_buff`) is discarded, the kernel calls `kfree_skb`. By hooking into the `kfree_skb` tracepoint or kprobe using eBPF, we can inspect the exact packet metadata (IP headers, UDP headers) and extract the kernel call stack to see why and where the packet was dropped.

## Tracing UDP Drops with bpftrace

For rapid interactive debugging, `bpftrace` is the fastest way to write and run eBPF tracing scripts. We can write a script that attaches to the `skb:kfree_skb` tracepoint, parses the IP and UDP headers of the freed socket buffer, filters for DNS traffic (port 53), and prints the kernel call stack of the drop event.

<script src="https://gist.github.com/mohashari/8967040dd167bd49610182a82eb8e3c0.js?file=snippet-2.txt"></script>

This stack trace is the smoking gun. If you see the stack trace originating from `nf_conntrack_confirm` or `nf_conntrack_attach`, you have proof of the conntrack confirmation race.

## Writing a Custom eBPF Tracer in C & Go

For continuous monitoring in production, running `bpftrace` is not practical due to the overhead of compiling scripts on production nodes. Instead, you can write a dedicated eBPF tracer using C for the kernel-space program and Go (via the Cilium `ebpf-go` library) for the user-space daemon.

The C program hooks the `kprobe/kfree_skb` kernel function. It reads the `sk_buff` fields using safe helper functions like `bpf_probe_read_kernel`. It extracts the IP header, verifies that it is a UDP packet, and checks if either the source or destination port is 53. If a match is found, it populates a struct with the packet metadata and sends it to a BPF perf event ring buffer.

<script src="https://gist.github.com/mohashari/8967040dd167bd49610182a82eb8e3c0.js?file=snippet-3.txt"></script>

The user-space Go program loads this BPF program into the kernel, attaches it to the `kfree_skb` hook, and reads events from the perf ring buffer. When a drop event is detected, it logs the source and destination IP/port. This daemon can run as a DaemonSet on every host node, sending metrics to your observability platform whenever a DNS packet drop occurs.

<script src="https://gist.github.com/mohashari/8967040dd167bd49610182a82eb8e3c0.js?file=snippet-4.go"></script>

## Defeating the 5-Second Latency Spike

Once you have identified that the 5-second spikes are caused by conntrack races and UDP packet drops, you have several battle-tested options to mitigate and solve the issue.

### 1. Tuning `dnsConfig` Options in Kubernetes Pods

The quickest mitigation is to modify the resolver behavior inside the pod. You can configure the `dnsConfig` spec in your Kubernetes Deployment to add two options:
- `single-request-reopen`: This option tells glibc to close the network socket after sending the `A` query and open a new socket before sending the `AAAA` query. This prevents the queries from being sent concurrently from the same port, avoiding the conntrack race.
- `ndots:2` (or lower): By reducing the `ndots` threshold, you prevent the resolver from making useless search suffix queries for external domain names. If your application queries `api.github.com` with `ndots:2`, it will execute the query as an absolute name first, dropping the number of queries from 8 to 2.

<script src="https://gist.github.com/mohashari/8967040dd167bd49610182a82eb8e3c0.js?file=snippet-1.yaml"></script>

### 2. Deploying NodeLocal DNSCache

The most robust architectural solution is to deploy NodeLocal DNSCache. This runs a DNS caching agent as a DaemonSet on every Kubernetes node. NodeLocal DNSCache configures a link-local IP address (typically `169.254.20.10`) on a virtual interface on the node. Pods are configured to send their DNS queries to this link-local IP instead of the CoreDNS Service IP.

Because the caching agent runs on the same node, queries do not cross the node boundary and do not go through SNAT/Masquerading. They bypass the Netfilter conntrack table entirely. Additionally, NodeLocal DNSCache caches responses locally, reducing the load on the central CoreDNS pods and serving repeat queries in microseconds.

### 3. Kernel Sysctl Tuning

If you cannot use NodeLocal DNSCache, you can tune the kernel's conntrack and socket parameters to handle high UDP volumes. By default, the conntrack table might be too small, causing evictions and drops. You can increase `net.netfilter.nf_conntrack_max` to prevent table saturation. Additionally, increasing the maximum receive and send socket buffer sizes (`net.core.rmem_max` and `net.core.wmem_max`) prevents the kernel from dropping packets due to socket buffer overflow when CoreDNS is under heavy load.

<script src="https://gist.github.com/mohashari/8967040dd167bd49610182a82eb8e3c0.js?file=snippet-6.sh"></script>

By systematically combining eBPF tracing to verify kernel drops, optimizing client resolver configurations, and routing local UDP queries via NodeLocal DNSCache, you can permanently eliminate the transient 5-second DNS tail latency spikes from your Kubernetes infrastructure.