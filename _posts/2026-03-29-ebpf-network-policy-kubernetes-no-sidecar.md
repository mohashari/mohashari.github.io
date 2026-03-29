---
layout: post
title: "eBPF-Based Network Policy Enforcement in Kubernetes Without Sidecar Proxies"
date: 2026-03-29 08:00:00 +0700
tags: [kubernetes, ebpf, cilium, networking, devsecops]
description: "How eBPF lets Kubernetes enforce L3/L4/L7 network policy directly in the kernel, eliminating the latency and memory cost of sidecar proxies."

Files created:
- `_posts/2026-03-29-ebpf-network-policy-kubernetes-no-sidecar.md` — ~2,400-word post
- `images/diagrams/ebpf-network-policy-kubernetes-no-sidecar.svg` — architecture diagram

The post covers:
1. Why sidecar proxies are a structural performance problem (3–5ms latency, 256MB/pod)
2. How eBPF hooks work (XDP, TC, cgroup) and where Cilium attaches them
3. `CiliumNetworkPolicy` with a realistic L7 HTTP policy example
4. The compilation pipeline from CRD → eBPF bytecode → kernel
5. eBPF map inspection for debugging policy state
6. Full Cilium Helm values replacing kube-proxy
7. Hubble for sidecar-free L7 observability
8. A concrete migration path from Istio
9. Honest tradeoffs (JWT validation, WASM filters, SPIFFE identity)
10. Kernel version requirements per feature