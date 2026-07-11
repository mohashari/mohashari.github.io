---
layout: post
title: "Designing an Automated Secret Zero Bootstrapping Pipeline in Kubernetes using TPM 2.0 and SPIFFE/SPIRE"
date: 2026-07-11 08:00:00 +0700
tags: [kubernetes, security, tpm, spiffe, devsecops]
description: "Eliminate static credentials and bootstrap tokens in Kubernetes by linking physical TPM 2.0 keys to SPIFFE/SPIRE workload identities for Vault access."
image: "https://picsum.photos/seed/7658/1080/720"
thumbnail: "https://picsum.photos/seed/7658/400/300"
---

In modern cloud-native architectures, securing access to sensitive resources like database credentials, API keys, and certificates requires a secret manager such as HashiCorp Vault or AWS KMS. However, this introduces the classic "Secret Zero" paradox: how does an application securely obtain its initial identity or token to authenticate with the secret manager in the first place? Relying on Kubernetes Service Account Tokens (JWTs) wrapped in pod specs is a common pattern, but it introduces a glaring security vulnerability. Because these software-issued tokens are stored on the node’s filesystem and verified by the Kubernetes API server using standard OIDC mechanisms, any attacker who compromises a worker node can dump these tokens from memory or the container's rootfs, bypassing all identity constraints. To solve this, we must build a pipeline that anchors workload identities to the physical hardware of the host machine using Trusted Platform Modules (TPM 2.0) and translates that hardware-rooted trust into dynamic, short-lived cryptographic identities via the SPIFFE/SPIRE framework.

![Designing an Automated Secret Zero Bootstrapping Pipeline in Kubernetes using TPM 2.0 and SPIFFE/SPIRE Diagram](/images/diagrams/designing-automated-secret-zero-bootstrapping-pipeline-kubernetes-tpm2-spiffe-spire.svg)

## The Bootstrapping Paradox and the Flaws of Software-Only Identity

In a default Kubernetes cluster, workload identity is synonymous with a Service Account. When a pod is scheduled, the kubelet requests a token from the TokenRequest API and mounts it into the pod at `/var/run/secrets/kubernetes.io/serviceaccount/token`. Under the hood, this token is a JSON Web Token (JWT) signed by the Kubernetes API server's service account private key. While this is simple to use, it has zero awareness of the physical hardware executing the code. 

If an attacker achieves remote code execution (RCE) on a container or root access on a worker node, the blast radius is catastrophic. The attacker can extract the projected service account tokens of all pods running on that node. Since these tokens are bearer tokens, the attacker can use them from any environment (until they expire) to authenticate to external identity providers, secret managers, or internal microservices.

To eliminate this vulnerability, we must establish a verification chain that begins at the physical hardware layer and extends up to the ephemeral application process. This is where TPM 2.0 and SPIFFE/SPIRE come into play:

1. **Hardware Root of Trust (TPM 2.0):** Every worker node is equipped with a physical or firmware-based TPM chip. The TPM contains a unique, non-exportable Endorsement Key (EK) burned in by the manufacturer, along with an Endorsement Certificate (EK Cert) signed by the TPM vendor's Certificate Authority (CA).
2. **Identity Control Plane (SPIFFE/SPIRE):** The Secure Production Identity Framework for Everyone (SPIFFE) defines a standard for issuing cryptographically verifiable identities (SVIDs) in the form of short-lived X.509 certificates or JWTs. SPIRE is the open-source implementation of SPIFFE that automates this process.
3. **The Bridge:** SPIRE leverages the TPM to perform Node Attestation. The SPIRE Server verifies that a SPIRE Agent is running on a genuine, unmodified hardware node by validating the TPM's cryptographic quotes against a known database of Endorsement Keys. Once the node is verified, the SPIRE Agent uses kernel-level selectors to perform Workload Attestation, issuing the pod an SVID via a local Unix domain socket.

## Hardening the Host: TPM 2.0 Node Attestation in SPIRE

To execute this architecture, we must first configure SPIRE to perform node attestation using the TPM 2.0 plugin instead of weaker software attestation mechanisms like the Join Token or Kubernetes Projected Service Account (PSAT) methods. 

When a worker node boots, the SPIRE Agent running as a DaemonSet must interact with the host's TPM chip. It does this by accessing the TPM resource manager (`/dev/tpmrm0`), which multiplexes access to the physical TPM device. The SPIRE Agent generates an Attestation Key (AK) within the TPM, restricted so that the private portion cannot be exported. It then sends the AK public key, the TPM vendor-signed Endorsement Certificate (EK Cert), and a cryptographic quote (signed by the AK) to the SPIRE Server.

The SPIRE Server acts as the CA. It receives the attestation payload and verifies the EK Cert against the TPM vendor's root CA certificate. It then checks the quote to verify that the agent has access to the private key corresponding to the AK. Additionally, to prevent rogue bare-metal hardware from joining the trust domain, the SPIRE Server is configured with a whitelist of approved Endorsement Key hashes.

Here is the production-ready configuration for the SPIRE Server, showcasing the TPM 2.0 Node Attestor setup.

<script src="https://gist.github.com/mohashari/952a36072d5b874063840f95f5974a37.js?file=snippet-1.yaml"></script>

On the client side, the SPIRE Agent must run on the worker node with access to the host's TPM interface. Below is the configuration required for the SPIRE Agent to read the local TPM 2.0 chip and initiate node attestation.

<script src="https://gist.github.com/mohashari/952a36072d5b874063840f95f5974a37.js?file=snippet-2.yaml"></script>

## Bridging Hardware Identity to the Pod: The SPIFFE CSI Driver

Once the SPIRE Agent is attested by the SPIRE Server using the TPM, it is trusted to issue SVIDs to workloads running on its node. The agent exposes the SPIFFE Workload API over a Unix Domain Socket. Workloads connect to this socket to retrieve their identity documents.

In Kubernetes, mounting this socket directly from the host via a `hostPath` volume is a security anti-pattern because it exposes host-level paths to containers and restricts granular access control. Instead, we use the official SPIFFE CSI (Container Storage Interface) Driver. The CSI driver runs as a node daemon, mounts the Workload API socket directly from the SPIRE Agent, and projects it into the target pod's directory structure under a read-only filesystem.

When a pod starts, the CSI driver mounts `/run/spiffe/workload.sock` into the application container. The SPIRE Agent detects the connection, queries the local OS kernel to find the Caller UID, GID, and Process ID (PID) of the socket client, and maps the PID to a specific Kubernetes container ID. It then queries the local Kubelet API to verify the pod's namespace, service account, and labels. This process—workload attestation—runs entirely out-of-band and cannot be spoofed by the application process.

Here is the YAML manifest for a production deployment of a workload utilizing the SPIFFE CSI driver to mount the secure socket.

<script src="https://gist.github.com/mohashari/952a36072d5b874063840f95f5974a37.js?file=snippet-3.yaml"></script>

## Consuming SPIFFE Identities in the Application Layer

With the SPIFFE Workload API socket projected into the pod, the application code must connect to it to retrieve the SVID. Rather than parsing Raw PEM files and handling rotation logic manually, the application uses the official `go-spiffe` library.

The library connects to the socket, initiates a gRPC stream, and continuously receives the latest X.509 SVID (which contains the workload’s SPIFFE ID as a Subject Alternative Name (SAN) URI, e.g., `spiffe://prod.muklis.internal/ns/payment-apps/sa/billing-sa`) and the corresponding trust bundle containing the public keys of the CA. The library handles background certificate rotation transparently before expiration, ensuring the application never reads stale credentials.

The following Go code shows how to instantiate a workload API client, fetch the X.509 SVID, and construct a TLS configuration that verifies incoming and outgoing connections against the SPIFFE trust domain.

<script src="https://gist.github.com/mohashari/952a36072d5b874063840f95f5974a37.js?file=snippet-4.go"></script>

## Authenticating to Secret Management: Exchanging SVIDs for Secret Zero

Now that the application has a cryptographically verifiable SVID rooted in physical hardware, we can solve the Secret Zero problem. Instead of using a statically configured token or a Kubernetes service account token vulnerable to exfiltration, we configure the secret manager (in this case, HashiCorp Vault) to accept SPIFFE identities.

Vault natively supports SPIFFE-based authentication using either the `vault-plugin-auth-jwt` backend (with SPIRE acting as the OIDC provider issuing signed JWT SVIDs) or the `vault-plugin-auth-cert` backend (where Vault terminates mTLS connections and extracts the SPIFFE ID from the X.509 certificate). In this setup, we will configure the JWT Auth Method in Vault to parse and validate JWT SVIDs signed by the SPIRE Server.

First, the Vault administrator enables the JWT authentication engine and points it to the SPIRE OIDC discovery endpoint.

<script src="https://gist.github.com/mohashari/952a36072d5b874063840f95f5974a37.js?file=snippet-5.sh"></script>

With Vault configured, the application backend can now run its bootstrapping sequence. On startup, it queries the SPIFFE Workload API to request a signed JWT SVID specifying the `vault` audience. It then sends this JWT to Vault's `/v1/auth/spiffe/login` endpoint. Vault validates the JWT signature against the SPIRE OIDC keys, checks the subject matches the bound subject, and issues a Vault client token. The application uses this token to retrieve its database credentials (Secret Zero) and then discards the token or holds it strictly in memory.

Here is the complete Go implementation of this bootstrapping sequence.

<script src="https://gist.github.com/mohashari/952a36072d5b874063840f95f5974a37.js?file=snippet-6.go"></script>

## Production Failure Modes and Mitigation Strategies

While a TPM-backed SPIFFE/SPIRE pipeline provides a robust cryptographic foundation for Secret Zero bootstrapping, running this infrastructure in high-scale environments introduces operational complexities and unique failure modes.

### 1. TPM Key Provisioning and Motherboard Replacement Drifts

The security of the node attestation phase relies on the SPIRE Server knowing the whitelist of Endorsement Key (EK) hashes. In bare-metal environments or dedicated private clouds, hardware failures require motherboard replacements. When a motherboard is swapped, the physical TPM 2.0 chip changes, generating a new EK Cert and a different public key.

*   **Failure Mode:** A node reboot after physical maintenance results in node attestation failures (`spire-agent` logs error: `node attestation failed: endorsement key not in whitelist`).
*   **Mitigation:** Automate the hardware onboarding lifecycle. Integrate the provisioning database (e.g., NetBox, Canonical MaaS) with SPIRE Server. When a server's serial number is updated during maintenance, an event listener should extract the new EK cert out-of-band via IPMI/Redfish, compute its SHA-256 fingerprint, and update the `ek_whitelist.json` file on the SPIRE Server.

### 2. Ephemeral Pods and Workload Attestation Race Conditions

When scaling up workloads rapidly, a newly scheduled pod immediately attempts to connect to `/run/spiffe/workload.sock`. 

*   **Failure Mode:** The application container boots faster than the SPIRE Agent can register the pod with the local Kubelet API. The agent rejects the attestation request with `workload not registered` because it has not yet received the new Pod lifecycle event from the Kubernetes API server.
*   **Mitigation:** Implement exponential backoff in the application's bootstrapping phase. If connecting to the Workload API fails or returns an empty SVID list, wait 250ms, 500ms, and up to 5 seconds before giving up. Additionally, adjust the SPIRE Agent's synchronization interval (`sync_interval` configuration parameter) down to 5 seconds (from the default 30 seconds) to reduce latency when watching the local Kubelet.

### 3. SPIRE Agent Socket Starvation and Resource Constraints

The `/run/spiffe/workload.sock` Unix socket is the single point of entry for all workloads on a node. Under heavy scaling, hundreds of containers might concurrently poll the socket.

*   **Failure Mode:** The Linux kernel queue for Unix sockets fills up, causing connections to block or drop (`dial unix /run/spiffe/workload.sock: connect: connection refused` in application logs).
*   **Mitigation:** Tune the host-level sysctl parameter `net.core.somaxconn` to at least `2048`. In the SPIRE Agent daemonset manifest, set resource limits to at least `500m` CPU and `512Mi` Memory, and use the `system-node-critical` priority class to guarantee CPU cycles during periods of heavy pod churning.

### 4. Vault OIDC Discovery Endpoint Outages

If the SPIRE Server OIDC discovery endpoint is unavailable, Vault cannot verify JWT SVID signatures because it cannot pull the JSON Web Key Set (JWKS) from `/.well-known/openid-configuration`.

*   **Failure Mode:** Vault rejects login requests with `500 Internal Server Error` or `signature validation failed`.
*   **Mitigation:** Run the SPIRE OIDC Provider sidecar as a highly available deployment backed by a service mesh (like Linkerd or Istio). Implement local JWKS caching within Vault by configuring the `jwt_supported_algs` parameters and tuning the cache TTLs, ensuring that temporary network blips between Vault and SPIRE do not disrupt workload restarts.