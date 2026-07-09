---
layout: post
title: "Hardening Kubernetes Control Planes against SSRF: Configuring Cilium Egress Gateways with SPIFFE Identities"
date: 2026-07-09 08:00:00 +0700
tags: [kubernetes, security, cilium, spiffe, devsecops]
description: "Secure outbound traffic and eliminate SSRF vectors in Kubernetes by binding Cilium Egress Gateways to cryptographically attested SPIFFE identities."
image: "https://picsum.photos/seed/9375/1080/720"
thumbnail: "https://picsum.photos/seed/9375/400/300"
---

In production Kubernetes clusters, Server-Side Request Forgery (SSRF) represents one of the most high-severity vectors for privilege escalation and data exfiltration. A single compromised application workload can probe internal cluster services, query the cloud metadata service (`169.254.169.254`) to steal IAM credentials, or abuse whitelisted egress routes to exfiltrate database contents to external command-and-control servers. Traditional network firewalls and basic Kubernetes NetworkPolicies rely on ephemeral IP addresses and easily mutable pod labels, offering little protection when an attacker obtains node-level execution or patches pod metadata. This post details how to close these gaps by binding Cilium Egress Gateways to cryptographically verified SPIFFE identities issued by SPIRE, ensuring that egress paths to critical external APIs are restricted to authenticated workloads and completely immune to IP and label-spoofing attacks.

![Hardening Kubernetes Control Planes against SSRF: Configuring Cilium Egress Gateways with SPIFFE Identities Diagram](/images/diagrams/hardening-kubernetes-control-plane-ssrf-cilium-egress-spiffe.svg)

## The SSRF Threat Model in Production Kubernetes

Server-Side Request Forgery occurs when an attacker forces a server-side application to make arbitrary HTTP or TCP requests to internal or external destinations. In a standard Kubernetes environment, this threat is amplified due to the shared-network nature of the container runtime and the presence of highly sensitive internal endpoints. 

### The Cloud Metadata Server (IMDS)
The most common and devastating SSRF target in cloud-hosted Kubernetes (AWS, GCP, Azure) is the Link-Local Instance Metadata Service (IMDS) located at `169.254.169.254`. Under AWS IMDSv1, a simple HTTP `GET` request from any container on the host retrieves the IAM role credentials of the underlying EC2 node. This role often possesses broad permissions, such as reading from S3 buckets, writing to DynamoDB, or modifying Route53 records. 

While AWS IMDSv2 introduces a session-oriented token flow requiring a `PUT` request to generate a cryptographic token, it is not a silver bullet. If a workload runs a complex proxy, PDF renderer, or API integration wrapper that allows HTTP header injection or custom HTTP methods, an attacker can still fetch the token and bypass IMDSv2. Furthermore, in GCP, the metadata service requires the header `Metadata-Flavor: Google`. Although this restricts naive SSRF (such as browser-like GET redirections), any vulnerability that permits complete control over outbound headers exposes the cluster to service account token theft.

### The Kubernetes API Server and Kubelet API
If an attacker compromises a pod, the local service account token is automatically mounted at `/var/run/secrets/kubernetes.io/serviceaccount/token`. An SSRF vulnerability can be exploited to coerce the workload into querying the internal API server (`https://kubernetes.default.svc`) or the node-level Kubelet API (`https://<node-ip>:10250`) on behalf of the attacker. If the service account is over-privileged, the attacker can list resources, read Secrets, or run commands inside other containers.

### Egress Bypasses and Firewall Poisoning
Many enterprise architectures require that workloads accessing third-party APIs (such as payment gateways, credit bureaus, or ERP databases) route traffic through a NAT Gateway with a static, whitelisted IP. In Kubernetes, this is achieved using Cilium Egress Gateways, which route outbound traffic through dedicated nodes and apply Source NAT (SNAT) to masquerade the pod's IP.

However, standard egress routing rules select source pods using declarative Kubernetes labels (e.g., `app: payment-processor`). If an attacker compromises a frontend pod and has permissions to patch labels (or exploits a compromised CI/CD pipeline), they can append the `app: payment-processor` label to their malicious container. Instantly, Cilium’s CNI routes the malicious traffic through the egress gateway, allowing the attacker to masquerade as the payment processor and exfiltrate data directly through the whitelisted IP to a malicious external host.

## Deconstructing Cilium Egress Gateways

To understand how to secure egress traffic, we must look at how Cilium manages routing at the kernel level using eBPF (Extended Berkeley Packet Filter). 

When a pod attempts to send a packet to an external IP, the traffic traverses the pod's virtual ethernet (`veth`) interface. Cilium attaches eBPF programs (such as `bpf_lxc.c`) to the tc (traffic control) ingress and egress hooks of these interfaces. These eBPF programs monitor and manipulate packet headers directly in the kernel space.

In a standard egress configuration, Cilium evaluates the packet against the `CiliumEgressGatewayPolicy` (CEGP) Custom Resource. The policy consists of:
1. **Source Selectors:** Matches pods based on namespace and pod labels.
2. **Destination CIDRs:** Defines the external IP ranges destined for routing.
3. **Egress Gateway Node:** Specifies the cluster node responsible for handling the egress traffic.
4. **Masquerade IP:** The static egress IP applied to the traffic.

If the packet matches the source pod selector and the destination CIDR, the eBPF program on the local worker node intercepts it. Instead of routing the packet via the default node gateway, the eBPF program encapsulates the packet in a VXLAN or Geneve tunnel header and forwards it directly to the designated Egress Gateway Node. The gateway node decapsulates the packet, replaces the source pod's IP address with the static `egressIP` using SNAT, and routes it out to the physical network.

While eBPF enforces this routing at near-line speed, the authorization mechanism relies entirely on the Kubernetes control plane's state. If the API server's metadata is manipulated or if container network interfaces are misconfigured, the gateway becomes vulnerable to lateral movement. To establish true zero-trust security, we must decouple the source identification from volatile Kubernetes labels and transition to cryptographic identities.

## The Zero-Trust Bridge: SPIFFE/SPIRE Workload Attestation

SPIFFE (Secure Production Identity Framework for Everyone) defines a standard for cryptographically identifying workloads in dynamic heterogeneous environments. SPIRE (the SPIFFE Runtime Environment) is the implementation that handles identity issuance.

### Kernel-Level Workload Attestation
Unlike Kubernetes, which trusts the API server’s state, SPIRE verifies a workload's identity dynamically using OS-level attributes. This process is called Workload Attestation.

When a pod is scheduled, the SPIRE Agent running as a DaemonSet on the node monitors container creation. It exposes a Unix Domain Socket (UDS) at `/run/spire/sockets/agent.sock`. The Cilium Agent communicates with this socket using the SPIFFE Workload API and SPIRE Delegation API.

The attestation flow proceeds as follows:
1. The container initiates a socket connection.
2. The SPIRE Agent queries the host kernel using `getsockopt` with `SO_PEERCRED` to extract the caller's process ID (PID), User ID (UID), and Group ID (GID).
3. The SPIRE Agent inspects the `/proc/<pid>/cgroup` path to extract the container ID.
4. The SPIRE Agent queries the container runtime (e.g., `containerd` or `CRI-O`) using the container ID to fetch metadata: the Kubernetes pod name, namespace, service account, and the cryptographic hash of the container image.
5. This metadata is validated against pre-registered SPIFFE registration entries. If the attributes match, the SPIRE Agent generates a SPIFFE Verifiable Identity Document (SVID)—an X.509 certificate—representing the workload's SPIFFE ID (e.g., `spiffe://prod.muklis.dev/ns/payment-system/sa/payment-processor`).

### Integrations with Cilium eBPF
Cilium acts as a trusted SPIFFE delegate. When SPIRE attests a pod, the Cilium Agent retrieves the corresponding SPIFFE identity and maps it to a Cilium Security Identity.

Cilium translates the cryptographic SPIFFE ID into a special policy label:
`spiffe:<trust-domain>/ns/<namespace>/sa/<service-account>`

Crucially, this label is generated locally by the `cilium-agent` and injected directly into the kernel's eBPF maps (`cilium_endpoints` and `cilium_ipcache`). It is **never** pushed to the Kubernetes API server as a standard pod metadata label. An attacker attempting to modify pod labels via `kubectl patch` or by exploiting an RBAC vulnerability cannot alter this label. The eBPF datapath enforces policies based on this read-only, cryptographically verified identity, completely preventing label-spoofing and IP-spoofing.

## Step-by-Step Implementation Guide

This section outlines how to configure Cilium with SPIRE, register workloads, block SSRF to the metadata server, and secure Cilium Egress Gateways using attested SPIFFE identities.

### Step 1: Install Cilium with SPIRE Support
You must deploy Cilium with the mutual authentication and SPIFFE/SPIRE integrations enabled. Use the following Helm configuration values:

<script src="https://gist.github.com/mohashari/47eb2471a28afaf2a91ca14654cde6f8.js?file=snippet-1.sh"></script>

### Step 2: Register the Workload in SPIRE
To ensure the SPIRE Server issues identities based on strict cryptographic and kernel attributes, create a registration entry. In addition to namespace and service account checks, you should enforce container image hash validation to prevent container execution hijacking.

<script src="https://gist.github.com/mohashari/47eb2471a28afaf2a91ca14654cde6f8.js?file=snippet-2.yaml"></script>

### Step 3: Enforce SSRF Isolation
Use a `CiliumNetworkPolicy` to restrict the payment processor pod's outbound traffic. This policy blocks direct egress to the host metadata IP (`169.254.169.254`) and restricts outbound access exclusively to CoreDNS and the Stripe API CIDR.

<script src="https://gist.github.com/mohashari/47eb2471a28afaf2a91ca14654cde6f8.js?file=snippet-3.yaml"></script>

### Step 4: Configure the Cilium Egress Gateway Policy
Rather than using standard Kubernetes labels, write a `CiliumEgressGatewayPolicy` that selects the source workloads using the cryptographically verified SPIFFE label injected by Cilium.

<script src="https://gist.github.com/mohashari/47eb2471a28afaf2a91ca14654cde6f8.js?file=snippet-4.yaml"></script>

### Step 5: Restrict Gateway Access via SPIFFE-Based Network Policy
To ensure that only workloads containing the attested SPIFFE identity can access egress paths, construct an egress policy targeting the SPIFFE label directly.

<script src="https://gist.github.com/mohashari/47eb2471a28afaf2a91ca14654cde6f8.js?file=snippet-5.yaml"></script>

### Step 6: Verify and Debug in Production
To confirm that SPIFFE identity mappings are active and matched in the kernel's eBPF structures, use the following debugging commands on the worker nodes:

<script src="https://gist.github.com/mohashari/47eb2471a28afaf2a91ca14654cde6f8.js?file=snippet-6.sh"></script>

## Failure Modes and Production Considerations

Running a combined Cilium and SPIFFE/SPIRE architecture in high-traffic production environments introduces specific failure modes that system engineers must anticipate and mitigate.

### SPIRE Socket Availability and DaemonSet Startup Latency
When a pod starts up, its container runtime must mount the SPIRE Agent's Unix Domain Socket. If the SPIRE Agent DaemonSet undergoes a rolling update or is descheduled, the UDS will become unavailable. 
* **Failure Mode:** Workloads fail to boot or timeout during startup because they cannot obtain their SVID.
* **Mitigation:** Implement a fallback configuration using `spiffe-csi-driver` to mount the socket directory. Set the container's readiness probes to depend on the local SPIFFE identity acquisition. Ensure the SPIRE Agent DaemonSet runs with high priority (`priorityClassName: system-node-critical`).

### eBPF Map Limit Overflows (`map-max-entries`)
Cilium stores security identities and IP allocations in fixed-size BPF maps. In highly dynamic clusters where thousands of pods spin up and down (such as batch processing or autoscaling microservices), the BPF IPcache map can overflow.
* **Failure Mode:** Pods fail to resolve identities, causing egress packets to be dropped with `Policy Denied` errors.
* **Mitigation:** Tune the Cilium Helm values to scale up the map limits:
  ```yaml
  bpf:
    mapNameProperties:
      cilium_ipcache:
        max_entries: 524288
      cilium_auth:
        max_entries: 262144
  ```

### Egress Gateway Failover Latency
If the node acting as the Egress Gateway dies, Cilium must redirect traffic to a healthy gateway node. 
* **Failure Mode:** Traffic to external APIs drops for up to 10 seconds while the control plane updates the gateway node paths and updates the eBPF maps on worker nodes.
* **Mitigation:** Configure high availability by defining multiple gateway nodes under `egressGateways` inside your policy, or peer the gateway nodes using BGP (via Cilium BGP Control Plane) to allow the physical network to handle immediate failovers.

### Certificate Expiry and Renewal Margins
SPIFFE SVIDs are short-lived certificates, typically expiring within 1 to 12 hours. 
* **Failure Mode:** If the connection between the SPIRE Agent and SPIRE Server breaks (due to network partition or database locks), the agent cannot renew SVIDs. Once the local SVID expires, Cilium drops all associated traffic.
* **Mitigation:** Configure the SPIRE Server to alert when SVID renewal rates drop. Implement Prometheus monitoring metrics tracking the remaining lifetime of active certificates (`spire_agent_svid_ttl_seconds`).

## Conclusion

Securing outbound traffic in Kubernetes requires moving past standard IP-based perimeter models and mutable API metadata. By integrating Cilium Egress Gateways with cryptographically verified SPIFFE identities, you enforce a zero-trust model where every outbound packet is verified using kernel-level attestation. Compromised workloads cannot bypass these policies by spoofing IP addresses or patching Kubernetes labels. The combination of eBPF-driven routing and SPIFFE-based workload attestation ensures your control plane and data paths remain resilient against SSRF and unauthorized egress attacks.