---
layout: post
title: "Preventing Secret Exfiltration in Kubernetes: Restricting DNS Resolution inside Pods via CoreDNS Policies and Cilium"
date: 2026-07-19 08:00:00 +0700
tags: [kubernetes, devsecops, networking, security]
description: "Secure your Kubernetes cluster against DNS tunneling and secret exfiltration using CoreDNS policies, Cilium network policies, and egress controls."
image: "https://picsum.photos/seed/9630/1080/720"
thumbnail: "https://picsum.photos/seed/9630/400/300"
---

In production Kubernetes environments, network security policies often focus heavily on blocking outbound TCP/UDP traffic to unauthorized IP ranges or ports. However, there is a gaping security hole in almost every default cluster configuration: unrestricted DNS resolution (UDP/TCP port 53). Consider a common scenario: a backend pod is compromised via a Remote Code Execution (RCE) vulnerability. Direct external ingress and egress are tightly locked down using standard Kubernetes NetworkPolicies; the pod cannot establish a single direct TCP handshake to the internet. Yet, because it must resolve internal services, it is allowed to send DNS queries to CoreDNS (`kube-dns`). An attacker who has read a sensitive token—such as a cloud provider metadata token or database credential—can easily chunk this secret, encode it into a series of base32 subdomains, and resolve them against a custom domain they control (e.g., `<chunk>.attacker.com`). CoreDNS receives the query, recursively forwards it to the upstream DNS resolver, and eventually the query reaches the attacker's authoritative name server. Without making a single direct connection to the outside world, the attacker has successfully exfiltrated your secrets.

## The Mechanics of DNS Tunneling Exfiltration

To understand how to prevent this vector, we must understand how DNS tunneling protocols (such as Iodine or Dnscat2) operate at the protocol level. DNS is inherently a hierarchical, distributed database. When a recursive resolver receives a query for a subdomain of a zone it does not host, it forwards that query up the tree. 

Because DNS lookup queries are allowed to pass through firewalls to reach local resolvers, they serve as an open, bi-directional gateway. Attackers exploit this behavior by chunking sensitive payload data into labels of a subdomain query. Under RFC 1035, each label in a DNS name can be up to 63 characters long, and the entire domain name can be up to 253 characters. While TXT, CNAME, or A records are typically used to receive command-and-control (C2) instructions back, the outbound exfiltration occurs purely via the lookup request itself. 

The Go snippet below demonstrates how easily an attacker-controlled script running inside a compromised container can automate this exfiltration process using standard DNS query paths.

<script src="https://gist.github.com/mohashari/c7774511bbdd4209e857098ca5eb2709.js?file=snippet-1.go"></script>

## Hardening the Egress Path: Blocking Rogue DNS Resolvers

A naive approach to fixing this vulnerability is implementing L7 DNS filtering rules at the CNI level while leaving the UDP/TCP port 53 egress wide open. However, if a compromised container can communicate directly with external public DNS servers (like `8.8.8.8` or `1.1.1.1`), it can bypass your local filtering controls entirely. The container can simply write its own resolver rules to `/etc/resolv.conf` and query its own name servers.

Therefore, the first rule of Kubernetes egress security is: **Drop all direct outbound traffic to UDP/TCP port 53 on the internet, allowing DNS resolution only through the cluster's internal DNS service.**

The following CiliumNetworkPolicy locks down a namespace so that pods can only establish port 53 connections directly to the CoreDNS pods managed by Kubernetes.

<script src="https://gist.github.com/mohashari/c7774511bbdd4209e857098ca5eb2709.js?file=snippet-2.yaml"></script>

## Cilium DNS-Based L7 Egress Control

With port 53 locked down to internal paths, Cilium can parse and enforce domain-level egress constraints. Traditional IP-based firewalls fail in modern cloud-native architectures where SaaS endpoints (like Stripe, SendGrid, or AWS APIs) utilize dynamic IP mappings, CDNs, and short Time-To-Live (TTL) records.

Cilium solves this with its `toFQDNs` policy engine. When a pod performs a DNS query to CoreDNS, the Cilium agent intercepts the response. If the query matches an allowed pattern, Cilium maps the returned IP addresses to the allowed domain name and dynamically installs eBPF rules allowing outbound traffic to those specific IPs.

Here is a production-grade Cilium network policy restricting a service to query and connect only to authorized external APIs:

<script src="https://gist.github.com/mohashari/c7774511bbdd4209e857098ca5eb2709.js?file=snippet-3.yaml"></script>

## Hardening CoreDNS Filtering at the Resolver Layer

While Cilium operates at the network interface layer, defense-in-depth dictates that we also apply security controls directly at the resolver layer using CoreDNS plugins. If Cilium is not used as the CNI, or if we want to block resolution early to save node processing cycles, CoreDNS configurations can be hardened.

We can leverage CoreDNS plugins like `rewrite` to intercept requests for forbidden TLDs, block dynamic DNS domains commonly used by command-and-control frameworks, or map queries directly to local sinkholes using `hosts`.

The ConfigMap configuration below implements a hardened CoreDNS configuration for production clusters:

<script src="https://gist.github.com/mohashari/c7774511bbdd4209e857098ca5eb2709.js?file=snippet-4.yaml"></script>

## Production Failure Modes & Operational Gotchas

Deploying L7 DNS controls in production environments introduces operational challenges that can lead to intermittent downtime if not managed correctly. Below are the most critical failure modes experienced at scale.

### 1. DNS TTL Mismatches and Application-Level Caching
When a client application queries a host, it receives an IP address and a TTL. The application caches the query result, and Cilium caches it to maintain the eBPF data path rules. However, if your application framework is misconfigured, it might cache DNS resolutions indefinitely. 

For instance, the Java Virtual Machine (JVM) historically default-cached successful DNS resolutions forever (`networkaddress.cache.ttl=-1`). If the target endpoint (e.g., Stripe API) updates its IP mappings, the JVM will continue sending TCP packets to the old IP. Since Cilium's FQDN cache has cleared the old IP (following the standard DNS TTL expiration), Cilium's eBPF map will immediately drop the TCP packets, resulting in connection timeouts.

* **Mitigation**: Configure your application runtime to enforce a strict DNS caching policy that aligns with or is lower than the DNS provider's TTL:
  ```properties
  # Set JVM DNS Cache TTL to 10 seconds
  networkaddress.cache.ttl=10
  ```

### 2. Wildcard FQDN Map Overflows in eBPF
Using overly broad wildcard domains (such as `*.amazonaws.com` or `*.google.com`) in your `CiliumNetworkPolicy` forces Cilium to track every single subdomain resolved by any pod in the namespace. 

Cilium stores these dynamically mapped IPs inside fixed-size eBPF maps on each node (`cilium_fqdn_cache`). If a pod makes millions of random subdomain queries matching the wildcard, the eBPF map can overflow. Once the map limit is reached, Cilium fails to register new resolved IPs, causing legitimate requests to drop silently.

* **Mitigation**: Scope policies tightly. Avoid broad wildcards. Instead of `*.amazonaws.com`, define the specific AWS resources:
  ```yaml
  toFQDNs:
    - matchName: "my-bucket.s3.us-west-2.amazonaws.com"
  ```
  Additionally, monitor the FQDN cache size on nodes using the Cilium CLI:
  ```bash
  cilium map show cilium_fqdn_cache
  ```

### 3. CNAME Flattening and Resolution Failures
Many SaaS providers use aliases (CNAMEs) that resolve to intermediate CDN hosts before pointing to the final A/AAAA records. If the DNS response returns a chain of CNAMEs, Cilium must follow and cache the entire chain. If a pod queries using a specific CNAME but the CNAME target resolves dynamically to changing IPs, the data path rule updates may lag behind the connection attempt, resulting in 502/504 errors on the first request.

* **Mitigation**: Adjust Cilium's agent configuration to optimize DNS proxy delays:
  ```yaml
  # Increase response delay buffer to allow eBPF maps to update before client gets DNS response
  fqdn-proxy-response-max-delay: "100ms"
  ```

## Telemetry and Detecting Exfiltration in DNS Flow Logs

Prevention is only half of the solution; you also need detection. DNS exfiltration traffic has distinct patterns: it involves long, random subdomains, high query rates, and a high frequency of NXDOMAIN responses. 

We can collect L7 flow data from Cilium Hubble and evaluate the Shannon Entropy of query subdomains. High entropy indicates the use of encrypted or encoded base32/base64 strings representing exfiltrated data rather than standard english domain names.

The Python script below reads parsed Cilium Hubble logs and raises alerts when potential DNS tunneling activities are detected.

<script src="https://gist.github.com/mohashari/c7774511bbdd4209e857098ca5eb2709.js?file=snippet-5.py"></script>

## Conclusion

Securing the DNS boundary is critical when designing threat models in Kubernetes. Leaving port 53 egress wide open gives attackers an invisible highway to leak sensitive tokens, service accounts, and proprietary code. By implementing a layered defense strategy—combining network policies that block direct public DNS routing, Cilium FQDN rules to restrict resolution domains, and CoreDNS rewrites—you successfully eliminate this exfiltration path. Ensure your application-level caching, eBPF map sizing, and FQDN configuration settings are aligned to maintain stability in high-traffic production environments.