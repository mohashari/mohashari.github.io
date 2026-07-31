---
layout: post
title: "Preventing SSRF in Kubernetes: Hardening the Instance Metadata Service (IMDSv2) using Cilium Network Policies"
date: 2026-07-31 08:00:00 +0700
tags: [kubernetes, security, cilium, devsecops, aws]
description: "Mitigate SSRF vulnerabilities in Kubernetes by blocking cloud metadata (IMDSv2) access using eBPF-powered Cilium network policies."
image: "https://picsum.photos/seed/4073/1080/720"
thumbnail: "https://picsum.photos/seed/4073/400/300"
---

In production Kubernetes clusters, egress security is frequently treated as an afterthought. Engineers spend weeks configuring ingress controllers, Web Application Firewalls (WAFs), and API gateways, yet they leave the egress path wide open. Consider a common microservice design pattern: a report generator, PDF renderer, or webhook dispatcher that accepts a user-provided URL and fetches its contents. A single Server-Side Request Forgery (SSRF) vulnerability in this service can lead to total infrastructure compromise. If a pod can initiate outbound TCP connections to arbitrary destinations, it can query the cloud provider’s Instance Metadata Service (IMDS) at the well-known link-local IP `169.254.169.254`. Under IMDSv1, a single, unauthenticated `GET` request retrieves the temporary IAM credentials assigned to the worker node's EC2 instance profile. With these credentials, an attacker can move laterally, query Amazon S3 buckets, pull images from private Elastic Container Registries (ECR), or even modify DNS records via Route53, escalating a simple application bug into a full-scale cloud account takeover.

## Anatomy of the Exploit: From SSRF to Credential Exfiltration

To understand how this vulnerability manifests in Go services, let us examine a typical vulnerable backend endpoint. The service below uses the popular Echo framework to accept a URL from a user, download the target resource, and return a preview.

<script src="https://gist.github.com/mohashari/5d815688da7f0de5bbcb73ae83beab19.js?file=snippet-1.go"></script>

An attacker exploits this endpoint by supplying a payload targeting the cloud provider's metadata endpoint. AWS designed IMDSv2 to prevent simple SSRF by requiring a session-oriented flow. A caller must first execute an HTTP `PUT` request containing a TTL header to obtain a session token, and then pass that token in an `X-aws-ec2-metadata-token` header on subsequent requests. 

However, if the SSRF vulnerability is flexible enough to permit the attacker to specify arbitrary HTTP methods and headers—such as via a misconfigured proxy, a webhook builder, or a custom script runner inside the container—obtaining EC2 IAM credentials remains trivial. The following shell script simulates how an attacker extracts the node's temporary security credentials via IMDSv2 from inside a compromised pod:

<script src="https://gist.github.com/mohashari/5d815688da7f0de5bbcb73ae83beab19.js?file=snippet-2.sh"></script>

The output of the final command contains a JSON block containing standard AWS session credentials. An attacker can load these credentials into their local terminal and immediately interact with the AWS API as if they were the Kubernetes worker node itself.

## The IMDSv2 Hop Limit Catch-22 in Kubernetes

AWS built a clever defense mechanism into IMDSv2: the IP Time-to-Live (TTL) hop limit. When the EC2 virtualization layer responds to a token metadata request, it sets the IP packet's TTL value to a configured limit. By default, when launching a new EC2 instance, the hop limit is set to 1.

When a packet travels from the host network namespace into a container running in a separate network namespace (which is how standard Kubernetes pods run under CNIs like AWS VPC CNI), the packet must cross a virtual ethernet (`veth`) interface. This transition counts as an IP routing hop, which decrements the packet's TTL. 
- If the response packet's TTL is set to 1, the hop to the host's namespace consumes the entire TTL.
- When the packet tries to traverse the `veth` pair into the pod's namespace, the TTL falls to 0, and the Linux network stack discards the packet.
- Consequently, the pod never receives the IMDSv2 token, effectively neutralizing the SSRF vector.

However, this security control introduces a production engineering problem. Certain legitimate cluster-wide operators and controllers (such as the AWS Load Balancer Controller, ExternalDNS, or Cluster Autoscaler) must query IMDS to discover metadata about the subnet, VPC, or auto-scaling groups they run on. If the hop limit is locked at 1, these controllers fail to initialize.

To resolve this, engineers often modify their infrastructure configuration to raise the instance metadata hop limit to 2 or more. The following Terraform configuration block shows how to define this on an EC2 Launch Template used by EKS Managed Node Groups:

<script src="https://gist.github.com/mohashari/5d815688da7f0de5bbcb73ae83beab19.js?file=snippet-6.hcl"></script>

Setting `http_put_response_hop_limit = 1` protects the cluster from SSRF, but breaks key controllers. Conversely, raising this parameter to 2 fixes the controllers but immediately opens every single pod in the cluster to SSRF credential exfiltration. 

Relying solely on the AWS hop limit creates a system-wide security compromise. We must decouple metadata access from IP packet TTL configurations and handle enforcement inside the Kubernetes networking layer.

## The Solution: eBPF-Powered Egress Control with Cilium

Standard Kubernetes `NetworkPolicy` resources compile down to Linux `iptables` rules. While functional, iptables-based firewalls have drawbacks in high-scale environments:
1. **Performance Degradation**: iptables evaluates rules sequentially. As the cluster grows and policies multiply, search time degrades to $O(N)$ lookup complexity.
2. **Default-Allow Proliferation**: Standard policies are default-allow. To block access to a single IP like `169.254.169.254`, you must write a catch-all policy that puts the pod in a default-deny state, requiring you to explicitly allow every other legitimate DNS, CIDR, and service target.
3. **No Global Policy Concept**: You must deploy a copy of the policy to every single namespace. If a developer creates a new namespace and forgets to apply the egress template, that namespace is exposed.

Cilium solves these issues using eBPF (Extended Berkeley Packet Filter). Instead of traversing linear iptables chains, Cilium injects sandboxed programs directly into kernel hooks, evaluating policy rules in $O(1)$ time using BPF maps.

Cilium introduces the `CiliumClusterwideNetworkPolicy` (CCNP) custom resource. Unlike namespace-scoped policies, a CCNP applies across the entire cluster. Cilium also supports `egressDeny` rules. This allows us to write a single policy that blocks egress to `169.254.169.254/32` for all workloads while leaving all other egress paths completely untouched.

<script src="https://gist.github.com/mohashari/5d815688da7f0de5bbcb73ae83beab19.js?file=snippet-3.yaml"></script>

Let us dissect the logic of the selector in `snippet-3`:
- We target all endpoints *except* those explicitly labeled with `security.corp.internal/allow-imds: "true"`.
- By using `egressDeny`, we do not force matching pods into a default-deny egress state. They can still talk to external public domains and resolve DNS via standard mechanisms. 
- Only TCP traffic destined for `169.254.169.254:80` is dropped.

## Granular Exceptions: Excluded System Pods and Namespace Policies

When a critical platform application (such as `aws-load-balancer-controller` or `external-dns`) legitimately requires metadata access and cannot use OIDC-based IAM Roles for Service Accounts (IRSA), we exclude it from the global block policy by applying the bypass label to its deployment template.

However, leaving access completely open to these sensitive pods is a security risk. If an attacker compromises a labeled pod, they can query the metadata service freely. To prevent this, we pair our cluster-wide deny policy with a target-scoped `CiliumNetworkPolicy` (CNP) that restricts the allowed pods to *only* communicate with the metadata IP and DNS, preventing general internet egress.

The manifest below demonstrates how to configure this for a deployment in the `kube-system` namespace:

<script src="https://gist.github.com/mohashari/5d815688da7f0de5bbcb73ae83beab19.js?file=snippet-4.yaml"></script>

By explicitly selecting the `aws-load-balancer-controller` via `endpointSelector` and applying the rules in `snippet-4`, we constrain its outgoing connections. Even if an attacker gains shell access to this controller, they cannot initiate outgoing TCP handshakes to an external command-and-control server because all egress paths outside of DNS and the local metadata service are blocked.

## Observability in Production: Monitoring Drops with Hubble

Deploying network policies in production without observability can lead to unexpected outages. When a pod fails to fetch a cloud resource, you need to know immediately whether the application is misconfigured or if Cilium is dropping the packets.

Cilium integrates with Hubble, an eBPF-powered network observability platform. Hubble runs as a daemon on each node, logging flows from kernel space without modifying the application code.

To verify that our global policies are operating correctly, we use the Hubble CLI to monitor dropped traffic targeting the metadata service IP:

<script src="https://gist.github.com/mohashari/5d815688da7f0de5bbcb73ae83beab19.js?file=snippet-5.sh"></script>

When an unauthorized pod attempts to query IMDS, the command above outputs a structured JSON log. The key fields of a drop log show exactly why the connection failed:

```json
{
  "flow": {
    "time": "2026-07-31T08:05:12.482Z",
    "verdict": "DROPPED",
    "drop_reason": 130,
    "source": {
      "namespace": "staging",
      "pod_name": "vulnerable-preview-service-7f89d9-abcde",
      "identity": 4091
    },
    "destination": {
      "ip": "169.254.169.254",
      "port": 80
    },
    "traffic_direction": "EGRESS",
    "policy_match_type": "L3-L4",
    "drop_reason_desc": "Policy denied by CiliumClusterwideNetworkPolicy 'deny-imds-access-global'"
  }
}
```

The log entry details that the request from `vulnerable-preview-service` in the `staging` namespace was dropped, citing the exact policy responsible (`deny-imds-access-global`). This level of visibility allows platform teams to quickly identify and fix misconfigured services without guessing.

## Application-Level Defense: Hardening the Go HTTP Client

Relying solely on network-layer protection leaves a gap during local development or when deploying services to environments where Cilium is not active. A robust defense-in-depth strategy requires application-level protection.

Many developers attempt to sanitize URLs by parsing the hostname, resolving it, and checking if the IP matches `169.254.169.254` or RFC 1918 private ranges. This approach is vulnerable to two common bypass techniques:
1. **DNS Rebinding**: The attacker points a custom domain (e.g., `malicious.domain.com`) to a public IP address with a DNS TTL of 0. During the application's initial validation phase, the domain resolves to the public IP. Immediately after, when the HTTP client initiates the connection, the domain resolves to `169.254.169.254`.
2. **HTTP Redirects**: The application validates the initial domain, but the server responds with an HTTP redirect (e.g., `302 Found`) pointing to `http://169.254.169.254/`.

To solve this, we must control the resolution and socket connection phases directly by overriding the `DialContext` function of our HTTP Transport. We resolve the hostname once, validate all returned IPs, and establish the connection to the validated IP, bypassing the default resolver during the dial step.

<script src="https://gist.github.com/mohashari/5d815688da7f0de5bbcb73ae83beab19.js?file=snippet-7.go"></script>

This Go code implementation blocks SSRF at the application tier. By resolving the host address manually and verifying all returned IPs against our blacklist in `IsPrivateIP`, we ensure the client never opens a connection to `169.254.169.254` or internal network endpoints.

## The Ultimate Goal: Bypassing IMDS with EKS Pod Identities

The most effective security posture is one where your application pods do not need access to the Instance Metadata Service at all.

Historically, AWS EKS clusters relied on IAM Roles for Service Accounts (IRSA) to assign permissions to pods. IRSA functions by using an OIDC identity provider to generate a token, which is then swapped for credentials. While secure, configuring IRSA requires managing trust relationships, mutating webhooks, and occasionally querying IMDS for region and account information.

With the introduction of **EKS Pod Identities** (available in EKS 1.28+), AWS redesigned pod credential management. Pod Identities run a daemonset (`aws-pod-identity-agent`) on the host network of each node.
- EKS mounts a token onto the pod's filesystem.
- The environment variable `AWS_CONTAINER_CREDENTIALS_FULL_URI` is injected into the pod, pointing to the local agent endpoint: `http://169.254.170.23/` (not `169.254.169.254`).
- Unlike the node’s global IMDS profile, this endpoint only provides permissions linked to the pod's specific Kubernetes `ServiceAccount`.

By implementing EKS Pod Identities, you restrict the damage an attacker can do via an SSRF vulnerability to the scope of that single pod's service account, rather than exposing the node's credentials.

Combining these three layers creates a robust security architecture:
1. **EC2 Layer**: Enforce IMDSv2 and configure `http_put_response_hop_limit = 1` to block standard pods from querying node metadata.
2. **Network Layer**: Deploy Cilium eBPF network policies with `egressDeny` to drop all metadata requests from pods that do not have explicit exemption labels.
3. **Application Layer**: Use custom dialers to block loopback and RFC 1918 IP resolutions.