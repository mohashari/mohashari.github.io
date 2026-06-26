---
layout: post
title: "Implementing Fine-Grained Kubernetes Network Policies using Cilium and eBPF"
date: 2026-06-26 08:00:00 +0700
tags: [kubernetes, ebpf, devsecops]
category: devsecops
description: "Enforce fine-grained L3/L4 and L7 Kubernetes network policies at scale using eBPF and Cilium to bypass legacy iptables latency and CPU starvation."
image: "https://picsum.photos/seed/3528/1080/720"
thumbnail: "https://picsum.photos/seed/3528/400/300"
---

When you scale a Kubernetes cluster past 150 nodes and 10,000 active pods, the networking foundation begins to rot. In standard Kubernetes configurations utilizing `kube-proxy` in `iptables` mode, every service creation triggers an $O(N)$ rule synchronization across every single node. Under high churn, this translates to severe CPU spikes, kernel lockups, and traffic latency spikes up to 40ms as the system runs `iptables-restore` to process a ruleset that has bloated to over 20,000 rules. Worse, IP-based policies are inherently race-prone in dynamic environments: if a pod terminates and its IP is immediately recycled for a new pod in a different namespace before the security rules synchronize, your firewalls are breached. Cilium bypasses this architectural limitation entirely by swapping `iptables` for eBPF (Extended Berkeley Packet Filter). By attaching sandboxed programs directly to network sockets and traffic control (`tc`) hooks, Cilium translates Kubernetes labels into numeric security identities, enforcing network policies at the kernel level in $O(1)$ time.

![Implementing Fine-Grained Kubernetes Network Policies using Cilium and eBPF Diagram](/images/diagrams/implementing-fine-grained-kubernetes-network-policies-cilium-ebpf.svg)

## The Architecture of eBPF-Powered Networking

Traditional Linux container networking routes traffic through virtual ethernet (`veth`) pairs, forcing packets to traverse the entire kernel TCP/IP stack twice—once inside the container's network namespace and once on the host node. When implementing network policies, the kernel matches packets against linear tables in `iptables`.

Cilium replaces this model using eBPF, a kernel technology that executes sandboxed programs in response to system events. Cilium attaches these programs to the Traffic Control (`tc`) subsystem at the ingress and egress points of the container's `veth` interfaces. As soon as a packet leaves a pod's network namespace, the eBPF program `cil_to_container` or `cil_from_container` intercepts it, reads its headers, and determines its fate before the packet enters the host's networking stack.

For pods residing on the same physical host, Cilium bypasses the TCP/IP stack entirely using socket redirection (`sockmap` and `sockops`). By mapping the local sockets of the communicating pods, Cilium's eBPF programs copy payloads directly from one socket's write queue to the other socket's read queue. This side-channel transfer avoids packet serialization, checksum computation, and virtual device traversal, reducing local pod-to-pod latency to near-native UNIX socket speeds.

To install Cilium and verify that the host kernel supports the necessary eBPF features (such as socket operations and Traffic Control hooks), you must check the kernel configuration and mount the BPF filesystem (`bpffs`) so BPF maps can persist across daemon restarts.

<script src="https://gist.github.com/mohashari/cbfa162b82b576857dbacd780f5665a4.js?file=snippet-1.sh"></script>

## Identity-Based Security vs. IP Address Ephemerality

Standard Kubernetes network policies select pods based on labels, but legacy CNI implementations translate those selectors into IP addresses. Under the hood, this translates to a daemonset updating an `ipset` or appending `iptables` chains whenever pods scale up or down. If a rolling update replaces 500 replicas, the CNI control plane must compute the new IPs and push those updates to every node in the cluster. This synchronization latency creates a vulnerability window: for up to 10 seconds, traffic can be misrouted to an unrelated pod that happened to claim a recycled IP address.

Cilium eliminates this race condition by using **Security Identities**. Instead of managing IP addresses, the Cilium control plane assigns a unique 16-bit integer identity to every distinct set of labels (e.g., pods with the label `app=payment,env=production` receive security identity `9912`). 

When a pod initiates a network connection, Cilium's eBPF programs handle policy evaluation using the following mechanism:

1. **IP Cache Lookup**: The local node maintains a BPF map (`cilium_ipcache`) that maps every endpoint IP in the cluster to its corresponding security identity.
2. **Encapsulation & Metadata Injection**: If the destination is on a remote node, the packet is encapsulated (via VXLAN or Geneve), and the source security identity (`4105`) is embedded directly in the tunnel metadata header.
3. **Kernel Enforcement**: When the packet arrives at the destination host, the receiving node's eBPF program reads the source identity from the header and compares it against the local pod's destination identity (`9912`). It looks up the tuple `(SourceIdentity=4105, DestIdentity=9912, Port=8080, Protocol=TCP)` in the `cilium_policy` map. If allowed, the packet passes; otherwise, it is dropped instantly at the kernel level without wasting CPU cycles traversing user-space firewalls.

A production-grade `CiliumNetworkPolicy` (CNP) implements these identity-based rules directly. The following policy isolates a backend payment service, allowing ingress traffic exclusively from the web frontend identity on port 8080.

<script src="https://gist.github.com/mohashari/cbfa162b82b576857dbacd780f5665a4.js?file=snippet-2.yaml"></script>

## Deep Dive into Layer 7 Policy Enforcement

While L3 and L4 policies (IPs, identities, ports, and protocols) are processed efficiently inside the kernel, enforcing security rules based on HTTP paths, headers, REST methods, or gRPC endpoints requires parsing the application layer payload. The Linux kernel's BPF runtime cannot natively parse application-level protocols like HTTP/2 or Kafka safely due to strict limits on instructions, complexity, and memory access.

To bypass this limitation, Cilium implements transparent redirection to a node-local Envoy proxy instance. When a `CiliumNetworkPolicy` contains Layer 7 rules, the eBPF program intercepts the connection handshake. Instead of routing the TCP packet out of the host, the eBPF program modifies the target socket address, redirecting the connection to a local Envoy loopback listener running in user space.

The redirect occurs transparently without any changes to application configuration or sidecar injection. Envoy parses the protocol stream, validates the HTTP path or gRPC method against the policy, and decides whether to permit or reject the request:

- If the request violates the policy, Envoy terminates the request, returning a `403 Forbidden` response to the sender.
- If the request matches the rules, Envoy establishes a secondary backend connection to the destination pod.

This hybrid approach minimizes context switches: 95% of traffic (L3/L4) is handled inside the kernel, and only connections requiring deep packet inspection pay the CPU penalty of traversing user space.

<script src="https://gist.github.com/mohashari/cbfa162b82b576857dbacd780f5665a4.js?file=snippet-3.yaml"></script>

## Egress Control and DNS-Based Policies

Securing egress traffic is a fundamental security requirement, but microservices regularly need to connect to external SaaS products (e.g., Stripe, Datadog, Twilio). Hardcoding IP addresses or subnet ranges for these endpoints is highly fragile; external providers utilize dynamic IP routing and CDNs with short TTLs.

Cilium resolves this problem by implementing DNS-based policies using FQDN (Fully Qualified Domain Name) selectors. When an egress-locked pod initiates a DNS lookup (e.g., querying `api.stripe.com`), Cilium's eBPF engine intercepts the UDP/TCP port 53 query and redirects it to an internal Cilium DNS proxy. The proxy forwards the lookup to the cluster's upstream DNS (e.g., CoreDNS) and inspects the response.

Upon parsing the resolved IP addresses from the DNS response, the DNS proxy dynamically updates the node's local eBPF mapping tables. It associates the returned IPs (e.g., `18.235.124.214`) with the requesting pod's security identity. These IPs are cached in the kernel for the duration of the DNS record's TTL. As soon as the TTL expires, the eBPF program garbage-collects the entries, closing the egress path.

<script src="https://gist.github.com/mohashari/cbfa162b82b576857dbacd780f5665a4.js?file=snippet-4.yaml"></script>

## Real-World Failure Modes, Troubleshooting, and Debugging

Running Cilium in high-throughput production environments exposes specific failure modes that differ significantly from standard `iptables` debugging.

### BPF Map Exhaustion
Because eBPF maps are pre-allocated in kernel memory with fixed dimensions, they can be exhausted under extreme scale. The key map to watch is `cilium_ipcache` (which stores the cluster-wide IP-to-Identity mapping) and `cilium_policy` (which tracks security rules). If your cluster scales rapidly, you may see errors in the `cilium-agent` pod logs:
```
level=warning msg="Failed to update BPF map" error="hash table full" mapName=cilium_ipcache
```
When this occurs, newly scheduled pods cannot register their identities, causing immediate connection drops. You can prevent this failure mode by scaling up the map limits in your Helm values:
```yaml
bpf:
  mapDynamicSizeRatio: 0.0075
  mapMaxEntries: 65536
```

### Connection Tracking (Conntrack) Table Saturation
Cilium maintains its own BPF connection tracking table (`cilium_ct_any4`). If you run applications that generate massive amounts of concurrent, short-lived TCP connections (such as misconfigured HTTP benchmark tools or applications lacking connection pools), you can saturate this map. The default limit is typically 262,144 entries. If saturated, the kernel drops incoming SYN packets. You must increase the limit:
```yaml
bpf:
  conntrackMax: 524288
```

### MTU Misconfiguration
If you run Cilium in tunnel mode (VXLAN or Geneve), the encapsulation headers add 50 bytes of overhead. If the host's physical interface MTU is 1500, you must configure Cilium's MTU to 1450. A mismatch causes packets exceeding 1450 bytes to be dropped silently by network hardware when the `DF` (Don't Fragment) flag is set. The symptoms are connections that hang indefinitely during TLS handshakes or during large API responses.

To inspect, debug, and monitor packet drops in real-time, you must use Cilium's diagnostic tools directly from the node or via the Hubble CLI.

<script src="https://gist.github.com/mohashari/cbfa162b82b576857dbacd780f5665a4.js?file=snippet-5.sh"></script>

If you are developing custom monitoring components or need to audit kernel state programmatically, you can read these pinned maps directly from the BPF filesystem using Go and the `ebpf` package. The following Go program opens the pinned `cilium_ipcache` map and parses its binary entries.

<script src="https://gist.github.com/mohashari/cbfa162b82b576857dbacd780f5665a4.js?file=snippet-6.go"></script>