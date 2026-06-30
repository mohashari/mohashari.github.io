---
layout: post
title: "Zero-Trust SPIRE-to-Envoy Integration for Dynamically Rotating Service-to-Service mTLS Credentials"
date: 2026-06-30 08:00:00 +0700
tags: [devsecops, zero-trust, security, platform-engineering]
description: "Implement zero-trust service-to-service mTLS using SPIRE and Envoy SDS to dynamically rotate credentials without restarting containers or risking downtime."
image: "https://picsum.photos/seed/4493/1080/720"
thumbnail: "https://picsum.photos/seed/4493/400/300"
---

At 3:14 AM, your payment gateway suddenly drops to a 0% success rate. The culprit isn't a database deadlock, a malformed API payload, or a network route configuration error; it is an expired client certificate on an internal microservice that failed to reload its configuration. In high-velocity cloud-native environments running hundreds of microservices, relying on static filesystem mounts, periodic cron jobs, or Kubernetes Secrets for certificate rotation is a ticking time bomb. The sync latency of Kubernetes Secrets can take up to 90 seconds, and triggering hard restarts on application containers just to load rotated credentials violates high-availability SLAs. If you want to achieve true zero-trust security with short-lived X.509 certificates—where lifespans are measured in hours, not months—you must decouple certificate management from the application lifecycle. Integrating the SPIFFE Runtime Environment (SPIRE) with Envoy proxy’s Secret Discovery Service (SDS) provides a robust, zero-downtime path to dynamic certificate rotation.

## Conceptual Architecture: SPIRE and Envoy SDS

To implement a secure, automated mutual TLS (mTLS) system, we must split our concerns into identity verification (issuance) and transport-layer verification (enforcement).

- **SPIFFE (Secure Production Identity Framework for Everyone)** defines a standard for bootstrapping trust and issuing identities to workloads in dynamic environments.
- **SPIRE (SPIFFE Runtime Environment)** is the production-ready implementation of the SPIFFE specifications. It comprises a centralized SPIRE Server (which acts as a root or intermediate CA) and a SPIRE Agent running on every host or Kubernetes node.
- **Envoy Proxy** acts as the service-to-service proxy sidecar. Instead of the application handling complex TLS code directly, Envoy intercepts network traffic and enforces mTLS.

Rather than mounting certificates as files, Envoy communicates with the local SPIRE Agent over a Unix Domain Socket (UDS) using the **Secret Discovery Service (SDS)** API. SDS is a gRPC protocol designed specifically for streaming secret configurations (such as private keys and TLS certificates) to Envoy. The SPIRE Agent acts as the SDS server, and Envoy acts as the SDS client. When a certificate is nearing its expiration (e.g., 30 minutes before a 1-hour certificate expires), the SPIRE Agent automatically requests a new SPIFFE Verifiable Identity Document (SVID) from the SPIRE Server, generates a new private key in memory, and pushes the updated key and trust bundle over the gRPC stream to Envoy. Envoy dynamically updates its active listeners in memory without dropping active TCP connections or restarting the container.

## Configuring the SPIRE Agent Workload API

The SPIRE Agent must run on the same node as the Envoy proxy. In Kubernetes, this is achieved by running the SPIRE Agent as a DaemonSet. The agent exposes the SPIFFE Workload API over a Unix Domain Socket. Envoy connects to this socket to query for the trust bundle and leaf certificates.

Below is the SPIRE Agent HOCON configuration (`spire-agent.conf`), detailing the socket path, trust domain, and Kubernetes node/workload attestation plugins.

<script src="https://gist.github.com/mohashari/a0f524eaf1aa383d0ea9591816d53d4c.js?file=snippet-1.txt"></script>

In this configuration:
- The `socket_path` specifies where the Workload API socket will be created. The Envoy sidecar container must be able to read and write to this socket.
- The `NodeAttestator "k8s_psat"` verifies the node's identity using Kubernetes Project-Specific Service Account Tokens (PSATs), ensuring that the agent is running on an authorized Kubernetes node.
- The `WorkloadAttestator "k8s"` enables the agent to inspect the local container runtime to extract workload metadata, such as the namespace, service account name, and pod labels of the calling process.

## Configuring Envoy to Consume the SDS Socket

To instruct Envoy to fetch certificates dynamically from the SPIRE Agent, we configure Envoy’s bootstrap to establish an SDS cluster pointing to the SPIRE Agent's Unix Domain Socket.

<script src="https://gist.github.com/mohashari/a0f524eaf1aa383d0ea9591816d53d4c.js?file=snippet-2.yaml"></script>

With the `spire_agent` cluster configured as a static gRPC service, we can now configure Envoy's Listeners. The listener defines the DownstreamTlsContext, which requests certificates (`tls_certificate_sds_secret_configs`) and validation contexts (`validation_context_sds_secret_config`) directly from the SPIRE SDS cluster.

<script src="https://gist.github.com/mohashari/a0f524eaf1aa383d0ea9591816d53d4c.js?file=snippet-3.yaml"></script>

Key aspects of this TLS listener configuration:
- The secret name `spiffe://prod.muklis.dev/ns/prod/sa/payment-service` corresponds to the SPIFFE ID registered for the workload. The SPIRE Agent will serve the X.509 SVID matching this ID.
- The validation context name `spiffe://prod.muklis.dev` corresponds to the SPIFFE trust domain. This instructs Envoy to retrieve the Root CA bundle associated with that trust domain, allowing it to validate the client certificates presented by incoming connections.
- The `require_client_certificate: true` directive makes client certificate validation mandatory, enforcing mutual TLS (mTLS).

## Kubernetes Pod Mounting Strategy: hostPath vs. SPIRE CSI Driver

To allow Envoy to access the SPIFFE Workload API socket on the host, the socket must be shared with the Envoy container. In a basic setup, this is achieved using a Kubernetes `hostPath` volume. 

The following snippet demonstrates a Kubernetes Pod spec containing the application container and the Envoy proxy sidecar sharing the host-mounted socket directory.

<script src="https://gist.github.com/mohashari/a0f524eaf1aa383d0ea9591816d53d4c.js?file=snippet-4.yaml"></script>

While using a `hostPath` volume is straightforward, it introduces severe security risks: any container with this mount can potentially read other sockets or files on the host if permission boundaries are misconfigured. 

To mitigate this in production, you should deploy the **SPIRE Kubernetes Workload Registrar** and the **SPIRE CSI (Container Storage Interface) Driver**. The CSI driver dynamically mounts the Workload API socket into the specific pod's volume directory, preventing the pod from having direct access to the host's `/run/spire/sockets` directory. The configuration remains clean, but the pod spec's volume block changes to a CSI driver reference rather than a `hostPath` mount.

## Workload Registration and Policy Enforcement

A workload cannot retrieve credentials unless it is registered in the SPIRE Server. The registration entry maps physical selectors (like Kubernetes Service Account names and namespaces) to a logical SPIFFE ID. 

Here is the shell script to register the payment service on the SPIRE Server:

<script src="https://gist.github.com/mohashari/a0f524eaf1aa383d0ea9591816d53d4c.js?file=snippet-5.sh"></script>

This entry enforces that:
- Only workloads run on nodes attested under the `k8s_psat` mechanism can claim this ID.
- The requesting workload must reside in the `prod` namespace, use the `payment-service` service account, and possess the `app: payment-service` pod label.
- The issued certificate has a Time-To-Live (TTL) of 3600 seconds (1 hour). This forces SPIRE and Envoy to rotate the certificate at least once every 30 minutes to prevent expired credentials from causing outages.

## Enforcing Application-Level Authorization (AuthZ)

Establishing an mTLS channel guarantees that the connection is encrypted and the peer's identity is cryptographically proven. However, transport security is only half the battle. A compromised service inside the cluster could still attempt to query the `payment-service`. The application layer must perform fine-grained authorization.

Because Envoy terminates the TLS connection, the downstream application receives plain HTTP/TCP traffic. To pass the authenticated client identity to the application, Envoy parses the client certificate's Subject Alternative Name (SAN) and forwards it via the `X-Forwarded-Client-Cert` (XFCC) header.

First, we must configure Envoy to sanitize and inject the client's SPIFFE ID into the XFCC header safely.

<script src="https://gist.github.com/mohashari/a0f524eaf1aa383d0ea9591816d53d4c.js?file=snippet-7.yaml"></script>

Using `forward_client_cert_details: SANITIZE_SET` is a critical security measure. It instructs Envoy to strip any pre-existing XFCC headers from incoming network requests (preventing spoofing attacks) and write a new, verified header containing the client certificate details.

Now, the Go application can consume this header using middleware to enforce authorization policies.

<script src="https://gist.github.com/mohashari/a0f524eaf1aa383d0ea9591816d53d4c.js?file=snippet-6.go"></script>

This middleware intercepts every incoming request, extracts the `URI` component of the XFCC header (which contains the client's SPIFFE ID), and checks it against an allowed list. This ensures that only authorized services—like `checkout-service`—can invoke critical payment endpoints, blocklisting all others.

## Real-World Failure Modes & Mitigation Strategies

Integrating SPIRE and Envoy in production introduces unique failure modes that do not exist with static files. Senior engineers must design for these failure modes to maintain high availability.

### 1. Pod Startup Race Conditions (Workload Attestation Failure)
- **Symptom:** During a rolling update, new Pod replicas spin up. The Envoy sidecar container initializes and immediately attempts to connect to the SPIRE Agent SDS socket. However, the Kubernetes API server or local kubelet hasn't registered the pod's state, or the SPIRE Agent hasn't updated its pod cache. Envoy fails to retrieve its SVID, causing the SDS gRPC connection to fail, leading to connection drops and 503 errors.
- **Mitigation:** Configure Envoy's `connect_timeout` to be resilient. Set up Envoy's bootstrap configuration to allow retry attempts on the gRPC connection to the SPIRE Agent. Tune the SPIRE Agent’s Kubernetes Workload Attestator to use a local cache with a slight sync delay, or use the SPIRE CSI driver which naturally coordinates pod creation and socket mounting.

### 2. Unix Domain Socket Permission Denied
- **Symptom:** Envoy logs show permission errors when attempting to read the `/run/spire/sockets/agent.sock` socket. 
- **Mitigation:** In production, Envoy containers should run under a non-root security context (e.g., UID `10001`). The Unix Domain Socket created by the SPIRE Agent DaemonSet must be accessible to this UID. In the SPIRE Agent configuration, define `socket_gid` or `socket_mode` to align with the Envoy container's running group ID. For example, setting the socket group ownership to the Envoy group ID (e.g., `10001`) with `0660` permissions ensures the proxy can read and write to the socket while preventing other unprivileged users on the node from accessing the Workload API.

### 3. Trust Bundle Rotation Split-Brain
- **Symptom:** When rotating the SPIRE Server's Root CA trust bundle, some nodes sync the new trust bundle immediately, while others lag due to network latency. Workloads running on updated nodes present certificates signed by the new CA, but workloads on lagging nodes reject these certificates, causing intermittent SSL handshake failures (`unknown CA` errors).
- **Mitigation:** SPIRE Server supports trust bundle transitions. During CA rotation, the SPIRE Server publishes a dual-trust bundle containing both the active and the next root key. It must be configured with a transition period (e.g., 24 hours) to allow all nodes to receive the new trust bundle before workloads start using certificates signed by the new root key.

### 4. Memory Footprint and File Descriptor Exhaustion
- **Symptom:** In large-scale clusters with thousands of active Envoy proxies, the SPIRE Agent experiences high CPU usage and memory spikes. The Envoy proxies experience socket drops, resulting in file descriptor exhaustion on the node.
- **Mitigation:** Set appropriate resource requests and limits on the SPIRE Agent DaemonSet (typically a minimum of `0.5` CPU cores and `512Mi` memory). Increase the Linux `sysctl` limits for file descriptors (`fs.file-max`) and network connections on the host nodes. Implement rate limiting on node attestation requests to the SPIRE Server to prevent "thundering herd" issues during cluster restarts.

## Performance and Scale Characteristics

Implementing dynamic mTLS with SPIRE and Envoy introduces minimal performance overhead if configured correctly.

- **TLS Handshake Latency:** Using ECDSA keys (specifically P-256) over RSA keys reduces handshake overhead significantly. ECDSA handshakes typically introduce less than `1.2ms` of latency overhead compared to plain TCP.
- **Rotation Overhead:** Since Envoy rotates certificates in-memory using SDS streams, there is no disk I/O overhead. The CPU overhead during a certificate rotation event (which occurs every 30 minutes in our setup) is negligible—typically consuming less than `0.1%` of a single CPU core for a fraction of a second.
- **Memory Consumption:** An Envoy instance with SDS enabled uses approximately `15MB` of additional memory compared to a static configuration. This memory is occupied by the gRPC connection buffers and certificate cache. This is a highly acceptable trade-off for the security guarantees provided.

## Conclusion

Static certificates are an operational hazard. By pairing SPIRE's automated workload attestation with Envoy’s dynamic Secret Discovery Service, you eliminate the risk of certificate expiration outages while enforcing a strict, cryptographically validated zero-trust architecture. Implementing proper XFCC parsing at the application layer ensures that identity verification extends beyond transport security into fine-grained service-to-service authorization, keeping your platform secure, resilient, and ready for scale.