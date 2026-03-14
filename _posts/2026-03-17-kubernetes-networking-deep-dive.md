---
layout: post
title: "Kubernetes Networking Deep Dive: Services, Ingress & Network Policies"
date: 2026-03-17 07:00:00 +0700
tags: [kubernetes, networking, devops, backend, cloud]
description: "Understand how Kubernetes networking works under the hood — ClusterIP, NodePort, LoadBalancer, Ingress controllers, and securing traffic with Network Policies."
---

Kubernetes networking trips up even experienced engineers. Pod IPs are ephemeral, Services are virtual IPs, and Ingress is yet another abstraction layer. Let's untangle all of it.

## The Kubernetes Networking Model

Every Pod gets its own IP address. Containers inside a Pod share that IP and can communicate via `localhost`. The cluster network ensures:

1. Every Pod can reach every other Pod without NAT
2. Nodes can reach every Pod without NAT
3. The IP a Pod sees for itself is the same IP others use to reach it

## Services — Stable Endpoints for Ephemeral Pods

Pods die and restart with new IPs. A Service provides a stable virtual IP (ClusterIP) that load-balances across healthy Pod replicas.

### ClusterIP — Internal Only

<script src="https://gist.github.com/mohashari/66fc6a58b1ef7cfebc2626ff92d93f47.js?file=snippet.yaml"></script>

`user-service.default.svc.cluster.local` resolves to the ClusterIP inside the cluster. kube-proxy programs iptables rules to forward traffic to one of the backing Pods.

### NodePort — Expose on Every Node

<script src="https://gist.github.com/mohashari/66fc6a58b1ef7cfebc2626ff92d93f47.js?file=snippet-2.yaml"></script>

Traffic to `<any-node-ip>:30080` reaches the service. Useful for testing, but not for production (exposes a port on every node).

### LoadBalancer — Cloud-Native Exposure

<script src="https://gist.github.com/mohashari/66fc6a58b1ef7cfebc2626ff92d93f47.js?file=snippet-3.yaml"></script>

Provisions a cloud load balancer (AWS ALB, GCP LB) automatically. Each LoadBalancer service = one cloud LB = cost. Use Ingress instead for HTTP/HTTPS traffic.

## Ingress — L7 Routing at Scale

Ingress routes HTTP/HTTPS traffic to multiple services from a single load balancer.

<script src="https://gist.github.com/mohashari/66fc6a58b1ef7cfebc2626ff92d93f47.js?file=snippet-4.yaml"></script>

The Ingress controller (nginx, Traefik, AWS ALB controller) watches Ingress resources and programs itself accordingly.

## DNS Resolution Inside the Cluster

CoreDNS handles service discovery. The full DNS name follows this pattern:

<script src="https://gist.github.com/mohashari/66fc6a58b1ef7cfebc2626ff92d93f47.js?file=snippet.txt"></script>

Within the same namespace, you can just use `<service>`. Cross-namespace requires `<service>.<namespace>`.

<script src="https://gist.github.com/mohashari/66fc6a58b1ef7cfebc2626ff92d93f47.js?file=snippet.go"></script>

## Network Policies — Kubernetes Firewall

By default, all Pods can talk to all other Pods. Network Policies restrict traffic.

### Deny All Ingress (Zero-Trust Start)

<script src="https://gist.github.com/mohashari/66fc6a58b1ef7cfebc2626ff92d93f47.js?file=snippet-5.yaml"></script>

### Allow Only Specific Traffic

<script src="https://gist.github.com/mohashari/66fc6a58b1ef7cfebc2626ff92d93f47.js?file=snippet-6.yaml"></script>

Only Pods with `app: api-server` can reach Postgres on port 5432.

### Allow Egress to External DNS and APIs

<script src="https://gist.github.com/mohashari/66fc6a58b1ef7cfebc2626ff92d93f47.js?file=snippet-7.yaml"></script>

## Headless Services — Direct Pod Access

<script src="https://gist.github.com/mohashari/66fc6a58b1ef7cfebc2626ff92d93f47.js?file=snippet-8.yaml"></script>

DNS returns individual Pod IPs instead of a ClusterIP. Used by StatefulSets (Kafka, Cassandra) where clients need to connect to specific instances.

## Debugging Networking Issues

<script src="https://gist.github.com/mohashari/66fc6a58b1ef7cfebc2626ff92d93f47.js?file=snippet.sh"></script>

## Quick Reference

| Object | Layer | Use Case |
|--------|-------|----------|
| ClusterIP | L4 | Internal service discovery |
| NodePort | L4 | Dev/testing external access |
| LoadBalancer | L4 | Single service cloud exposure |
| Ingress | L7 | Multi-service HTTP/S routing |
| NetworkPolicy | L3/L4 | Traffic firewall |
| Headless Service | L3 | StatefulSet direct pod access |

Start with ClusterIP for internal services, Ingress for external HTTP traffic, and Network Policies to enforce zero-trust networking from day one.
