---
layout: post
title: "Linux eBPF Internals: How TC and XDP Programs Accelerate Container Networking"
date: 2026-04-05 09:00:00 +0700
tags: [linux, ebpf, networking, kubernetes, performance]
description: "A deep dive into how eBPF bypasses the standard Linux network stack using TC (Traffic Control) and XDP (eXpress Data Path) to achieve near-wire-speed container communication."
image: "https://picsum.photos/seed/7810/1080/720"
thumbnail: "https://picsum.photos/seed/7810/400/300"
---

In traditional container networking, a packet sent from one container to another on the same host traverses a labyrinth of virtual devices: veth pairs, network namespaces, IP tables, and bridge devices. Each hop involves copying memory, allocating `sk_buff` structs, and context-switching through the kernel's network stack. Under heavy loads, these overheads degrade throughput and increase latency. 

eBPF (Extended Berkeley Packet Filter) changes the game. By running sandboxed programs directly within the Linux kernel, eBPF allows network interfaces to route packets almost immediately upon arrival, bypassing the bulk of the standard network stack. Two kernel subsystems make this possible: **XDP (eXpress Data Path)** and **TC (Traffic Control)**.

## The Traditional Network Path vs. eBPF

To understand the acceleration, we must look at how a packet normally travels. 

```
[Network Hardware]
       │ (Ring Buffer / DMA)
       ▼
[NIC Driver (NAPI Poll)] 
       │ 
       ├───► [XDP Program Runs Here (Driver Level)] 
       │     (Can DROP, TX, PASS, REDIRECT)
       ▼
[Allocate sk_buff (Socket Buffer)]
       │
[IP Routing & iptables / Netfilter]
       │
       ├───► [TC Program Runs Here (Clsact qdisc)]
       │
       ▼
[Socket Buffer (Layer 4 / TCP / UDP)] ◄── [Socket Layer / Epoll]
```

In the standard path, once the physical network card receives a packet, it raises an interrupt. The NIC driver handles this via NAPI, copies the packet data into a kernel memory buffer, allocates a heavy `sk_buff` metadata structure, and passes it up to Layer 2 (Ethernet) and Layer 3 (IP). Netfilter (iptables) evaluates firewalls, routing tables determine the destination, and eventually the packet reaches the destination namespace's `veth` pair.

This entire pipeline is heavily CPU-bound. With eBPF, we can intercept and route packets at two key insertion points:
1. **XDP (eXpress Data Path):** Runs at the lowest possible level—directly inside the network driver's polling loop (before `sk_buff` allocation).
2. **TC (Traffic Control):** Runs slightly higher in the stack, after `sk_buff` is allocated, but before the standard IP routing and Netfilter filters are applied.

## XDP Internals: Zero-Copy and the Driver Layer

XDP executes eBPF bytecode in the driver context, meaning it can process millions of packets per second (Mpps) per CPU core. When an XDP program is loaded, the kernel driver invokes it for every incoming packet frame.

The program is passed an `xdp_md` context pointer:

```c
struct xdp_md {
    __u32 data;             /* User space packet start pointer */
    __u32 data_end;         /* User space packet end pointer */
    __u32 data_meta;        /* Metadata pointer */
    __u32 ingress_ifindex;  /* Ingress interface index */
    __u32 rx_queue_index;   /* Hardware RX queue index */
    __u32 egress_ifindex;   /* Egress interface index (writable) */
};
```

An XDP program must return one of five action codes:
*   `XDP_DROP`: Immediately discard the packet at the NIC level. Extremely efficient for DDoS mitigation.
*   `XDP_PASS`: Hand the packet over to the normal kernel network stack for standard processing.
*   `XDP_TX`: Bounce the packet back out of the same network interface it arrived on.
*   `XDP_REDIRECT`: Bypass the host stack entirely and send the packet directly to another network interface (e.g., a container's `veth` end) or to a user-space socket via `AF_XDP`.
*   `XDP_ABORTED`: Program error; drops the packet and triggers a kernel warning.

### Bypassing with XDP_REDIRECT and AF_XDP

For container networking, `XDP_REDIRECT` is the crown jewel. In a standard Kubernetes CNI like Cilium, a packet arriving at the host NIC destined for a local pod is redirected straight to the pod's `veth` interface, completely skipping the host's bridge and routing tables.

If user-space processing is required (e.g., high-performance proxies), `AF_XDP` (Address Family XDP) enables zero-copy packet transfers to user space. It uses a shared memory ring buffer called a **UMEM**, allowing user-space applications to read raw packets directly out of the NIC's DMA rings without memory copies.

```c
SEC("xdp")
int xdp_redirect_to_socket(struct xdp_md *ctx) {
    // Redirect incoming packets on matching port to AF_XDP socket
    return bpf_redirect_map(&xsks_map, ctx->rx_queue_index, 0);
}
```

## TC (Traffic Control) Internals: Hooking the Clsact Qdisc

While XDP is blisteringly fast, it has limitations:
1. It only works on *ingress* (incoming packets).
2. It runs before `sk_buff` allocation, so it lacks access to rich metadata (like socket state, cgroups, or connection tracking).
3. It requires NIC driver support for native mode (though it can run in "generic" software fallback mode at a performance penalty).

This is where **TC (Traffic Control)** shines. TC programs run on a pseudo-queuing discipline called `clsact`. TC operates on both **ingress** and **egress**, and receives a `__sk_buff` context pointer containing all the metadata the kernel has already parsed:

```c
struct __sk_buff {
    __u32 len;
    __u32 pkt_type;
    __u32 mark;
    __u32 queue_mapping;
    __u32 protocol;
    __u32 cb[5];
    __u32 hash;
    __u32 tc_classid;
    __u32 tc_index;
    __u32 local_ip4;
    // ... and dozens of other fields
};
```

In Cilium and other eBPF-based CNIs, TC programs are attached to the `veth` interfaces connecting pods to the host. When Pod A sends a packet to Pod B:
1. The packet enters the pod's egress `veth` interface.
2. A TC egress program intercepts it, looks up the target pod in an eBPF map, and updates the packet's destination MAC/IP.
3. The TC program redirects the packet directly to the ingress `veth` of Pod B using the `bpf_clone_redirect()` helper, bypassing the entire host IP routing table and netfilter tracking.

This cuts latency in half compared to traditional virtual bridges (`br_netfilter`) and iptables rules.

## State Management with eBPF Maps

How do XDP and TC programs know where to route packets? They query **eBPF Maps**—key-value stores residing in kernel memory that can be accessed by both the kernel (eBPF programs) and user space (management daemons like the CNI agent).

The most common map types for network acceleration:
*   `BPF_MAP_TYPE_HASH`: General-purpose hash maps for connection tracking (conntrack tables).
*   `BPF_MAP_TYPE_LPM_TRIE`: Longest Prefix Match trie, perfect for CIDR-based routing lookups.
*   `BPF_MAP_TYPE_DEVMAP` & `BPF_MAP_TYPE_CPUMAP`: Specialized maps used by `XDP_REDIRECT` to map to other network devices or offload packet processing to specific CPU cores.

```c
struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __type(key, struct endpoint_key);
    __type(value, struct endpoint_info);
    __uint(max_entries, 65536);
} endpoints_map SEC(".maps");
```

## Performance Comparison: The Numbers

To put these architectures into perspective, consider the benchmarked packet processing capabilities of a single CPU core under a synthetic load:

| Networking Path | Max Throughput (Mpps) | Latency Overhead | CPU Cost |
| :--- | :--- | :--- | :--- |
| Standard Linux Bridge + iptables | ~1.5 Mpps | High (context switches) | 100% |
| TC eBPF Bypassing | ~4.2 Mpps | Low | Moderate |
| XDP Native (Driver Mode) | ~15.0 Mpps | Near-Zero (no sk_buff) | Low |
| AF_XDP Zero-Copy (User-space) | ~12.5 Mpps | Near-Zero | Very Low |

## Conclusion

eBPF has fundamentally redefined container networking. By moving routing, security, and load-balancing decisions to the earliest possible point in the kernel—whether through driver-level XDP or clsact-level TC—we trade millions of CPU cycles spent on boilerplate network stack traversal for optimized, map-driven lookups. 

If you are building high-throughput microservices or managing large-scale Kubernetes clusters, moving away from iptables/IPVS to an eBPF-native CNI is no longer an incremental optimization; it is a architectural necessity.
