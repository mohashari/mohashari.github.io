---
layout: post
title: "Implementing Zero-Trust Service-to-Service Authorization Using Envoy Proxies and Open Policy Agent (OPA)"
date: 2026-07-25 08:00:00 +0700
tags: [devsecops, kubernetes, security, envoy]
description: "Implement high-performance zero-trust service-to-service authorization using Envoy proxies and Open Policy Agent (OPA) sidecars."
image: "https://picsum.photos/seed/3109/1080/720"
thumbnail: "https://picsum.photos/seed/3109/400/300"
---
Consider this production nightmare: an attacker exploits a Server-Side Request Forgery (SSRF) or remote code execution vulnerability in your public-facing gateway container. Once inside the Kubernetes software-defined network (SDN), they find that your internal service-to-service communication is completely unauthenticated. With immediate access to the internal network, they port-scan the cluster, query your core customer database service directly on its raw HTTP port, and exfiltrate sensitive data. This catastrophic breach occurs because security was built on the outdated assumption that "the network perimeter is secure." True zero-trust architecture dictates that we treat the internal network as hostile. Every single service-to-service request must be cryptographically authenticated, authorized based on the principle of least privilege, and monitored. 

![Implementing Zero-Trust Service-to-Service Authorization Using Envoy Proxies and Open Policy Agent (OPA) Diagram](/images/diagrams/implementing-zero-trust-service-authorization-envoy-open-policy-agent-opa.svg)

The diagram above details the zero-trust sidecar pattern. When a downstream service initiates a call, the request is intercepted by the local Envoy proxy. Envoy extracts the client's identity cryptographically via mTLS and SPIFFE, then queries the local Open Policy Agent (OPA) daemon via gRPC using Envoy's `ext_authz` filter. OPA evaluates the request's HTTP path, method, and credentials against compiled Rego policy bundles. If permitted, Envoy forwards the request to the upstream application container over the local loopback interface. This decoupled architecture offloads security enforcement from application code, delivering centralized policy management with sub-millisecond local evaluation times.

## Deconstructing the Ingress Authorization Flow

Implementing this pattern in a production Kubernetes cluster requires decoupling identity verification, policy evaluation, and policy enforcement. The sidecar pattern separates these responsibilities:

1. **Identity & Transport Security (Envoy)**: Envoy manages ingress mTLS termination. Rather than relying on fragile, spoofable source IP addresses, Envoy uses SPIFFE/SPIRE to retrieve cryptographic identities embedded inside X.509 certificates.
2. **Policy Evaluation (OPA)**: Envoy delegates the decision to OPA using a standardized gRPC contract. OPA acts as the policy engine, parsing input attributes and returning an explicit policy decision.
3. **Application Layer (Upstream App)**: The application is bound only to `localhost:8080`, rendering it unreachable to raw external network traffic. It receives validated header context (such as user IDs or tenant scopes) forwarded by Envoy, trusting them implicitly because they were injected post-authorization.

## Configuring Envoy's External Authorization Filter

Envoy intercepts incoming traffic at the pod boundary and delegates authorization decisions using the HTTP external authorization filter (`envoy.filters.http.ext_authz`). 

To make this robust in production, the filter must be configured to **fail-closed** (`failure_mode_allow: false`). If the OPA sidecar crashes, runs out of memory (OOM), or fails to respond within the designated SLA (e.g., 150ms), Envoy will immediately block the request and return an HTTP `503 Service Unavailable` status. Allowing requests to fail-open defeats the security guarantees of a zero-trust network.

The following configuration defines the filter within the Envoy HTTP Connection Manager and defines the local gRPC cluster pointing to OPA on `127.0.0.1:9001`:

<script src="https://gist.github.com/mohashari/a872f63d3e3963d4fbc975cbf440bb02.js?file=snippet-1.yaml"></script>

## Running OPA as a gRPC Server

By default, OPA is widely known for exposing an HTTP JSON API. However, for high-throughput service meshes, the JSON over HTTP serialization overhead is prohibitive. To resolve this, OPA provides a dedicated Envoy plugin that exposes a high-performance gRPC server implementing the Envoy `Authorization` service protocol.

To maintain policy consistency across a fleet of microservices, OPA should fetch its policy rules and structural data asynchronously from an internal **Bundle Server**. This ensures that policies are kept in version control (GitOps) and pushed to OPA agents within seconds of merge.

The configuration below instructs the OPA sidecar daemon to listen for ext_authz gRPC requests locally and regularly poll the control plane for signed policy bundles:

<script src="https://gist.github.com/mohashari/a872f63d3e3963d4fbc975cbf440bb02.js?file=snippet-2.yaml"></script>

## Writing Production-Grade Rego Policies

When Envoy invokes the `ext_authz` gRPC filter, it sends a structured payload containing:
- Connection attributes (including peer certificates).
- Request details (HTTP method, target path, active headers).
- Optional request bodies (if body buffering is enabled).

Our OPA policy must implement a multi-layered verification strategy:
1. **Source Authenticity**: Verify that the caller presents a valid client certificate with a trusted SPIFFE identity. For example, we want to ensure only the `billing-service` workload running inside the `core-services` namespace can call our endpoint.
2. **User Identity Validation**: If the request contains a JWT, OPA decodes the token, asserts signatures, and verifies that the client has the required roles.
3. **Context Injection**: Upon successful authorization, OPA generates contextual metadata headers. Envoy intercepts this metadata and propagates it as HTTP request headers to the upstream service container, simplifying application-level auth parsing.

Here is a production-grade Rego policy implementation:

<script src="https://gist.github.com/mohashari/a872f63d3e3963d4fbc975cbf440bb02.js?file=snippet-3.txt"></script>

## Designing the Kubernetes Deployment Pod Spec

Deploying this architecture in Kubernetes requires linking the containers to share resources. We must deploy the core application, the Envoy proxy, and the OPA agent within the **same pod**. 

Because they run in the same pod:
- They share the network namespace (`localhost`). The application listens on `127.0.0.1:8080`, allowing only the Envoy proxy to proxy traffic from the outer network.
- They communicate with minimal overhead. Envoy calls OPA at `127.0.0.1:9001` via gRPC without routing packets through the network fabric.
- Workload identity certificates are distributed locally using a SPIFFE CSI driver, mounting X.509 materials directly into Envoy’s container space.

Below is the Kubernetes deployment configuration showing the three containers and volume configurations:

<script src="https://gist.github.com/mohashari/a872f63d3e3963d4fbc975cbf440bb02.js?file=snippet-4.yaml"></script>

## Testing Policies in CI/CD

Security rules must be treated like software. We cannot verify authorization policies via manual testing or during production outages. Writing unit tests for OPA Rego policies ensures syntax correctness and rules-matching accuracy before anything is deployed.

While OPA provides a built-in testing framework (`opa test`), senior backend engineers often prefer executing programmatic tests directly inside their main application test suite. The Go test below demonstrates how to mock the Envoy gRPC ext_authz payload and assert evaluation decisions programmatically:

<script src="https://gist.github.com/mohashari/a872f63d3e3963d4fbc975cbf440bb02.js?file=snippet-5.go"></script>

## Performance and Tuning at Scale

Deploying zero-trust authorization sidecars adds network hops to the execution path of every request. Minimizing latency overhead is critical. In high-throughput systems, improper configurations will degrade API performance.

Here are production-proven tuning strategies:

### 1. Latency Overhead Profiles
In a properly configured Kubernetes pod running on a standard Linux node (e.g., kernel 5.15+), average latency overhead remains minimal:
* **Envoy interception and mTLS handshake**: `0.15ms - 0.3ms`
* **gRPC ext_authz request-reply (over localhost)**: `0.08ms - 0.15ms`
* **OPA Policy Evaluation (compiled Rego)**: `0.10ms - 0.50ms` (for policies parsing a standard set of roles/scopes).
* **Total Overhead**: `< 1.0ms` at `p95` latency profiles.

### 2. Eliminating O(N) Complexity in Rego
Rego is a declarative logic language. If policies iterate over lists of permissions using standard loops or array scanning, evaluation complexity grows linearly with the number of rules. 
Instead, structure permissions as **O(1) maps or sets**.
Instead of:
```rego
# Bad: Loops through array elements sequentially
allow if {
    some permission in input.user_permissions
    permission == "read_secrets"
}
```
Use set lookups:
```rego
# Good: Performs instant set membership checking
allow if {
    input.user_permissions["read_secrets"]
}
```

### 3. Tuning the Envoy Cluster Connection Pool
Under heavy concurrent load, Envoy will exhaust HTTP/2 connection limits to the OPA sidecar. We must configure the Envoy cluster connection settings to maintain persistent warm streams. Ensure that `http2_protocol_options: {}` is present in the `ext-authz-opa` cluster definition, forcing Envoy to multiplex gRPC requests over a single connection pool.

## Real-World Failure Modes and Mitigations

Transitioning to a fail-closed, zero-trust infrastructure exposes several vulnerabilities in cluster operations.

### Failure Mode 1: OPA Out-Of-Memory (OOM) Crashes
OPA stores evaluated policy data in memory. If your OPA bundle contains large database collections (such as dynamic lists of user identities or token revoking lists), the memory footprint can spike drastically. When OPA hits its Kubernetes memory limit, the kernel kills the daemon (OOMKilled). Because Envoy is configured to fail-closed, your entire service immediately drops offline.

* **Mitigation**: Never import raw transaction database tables into OPA memory. Implement **Delta Bundles** to apply differential updates rather than complete database sets. Set clear alerting rules monitoring OPA memory usage relative to container limits.

### Failure Mode 2: Cascading Failure under Policy Load
When the control plane is updated, OPA sidecars across hundreds of pod replicas simultaneously download a new policy bundle. If a bundle contains an compilation error that was not caught in CI, OPA will reject the bundle and fallback to cached policies. However, if the bundle server itself fails or returns a corrupted bundle during a replica scale-up event, new pods will initialize without cached policies, causing immediate authorization failures.

* **Mitigation**: Implement **strict canary deployments** for policy bundles. Validate bundle compilation at the control plane level before distributing to the sidecars. Configure OPA status reporting so failures to pull or apply bundles trigger alerts within your monitoring dashboard.

### Failure Mode 3: SPIFFE Workload API Timeouts
Envoy relies on the SPIFFE workload API socket (provided by SPIRE) to receive rotated identity certificates. If the SPIRE agent becomes overloaded or fails to rotate client certificates before expiration, mTLS validation fails. Envoy will block communication between services, leading to system-wide authorization failures.

* **Mitigation**: Set the lifetime of SPIFFE certificates to a reasonable duration (e.g., 1 hour, rotating every 30 minutes) to allow time for recovery if the workload API is temporarily unavailable. Implement fallback verification strategies for non-SPIFFE internal traffic during disasters.

## Summary

Implementing zero-trust service authorization using Envoy and Open Policy Agent eliminates network-level trust assumptions. By decoupling identity, enforcement, and logic, you achieve a scalable security architecture that can withstand host compromise. Running OPA as a local sidecar ensures that these strict security checks add less than a millisecond of overhead. Failing-closed, monitoring bundle synchronization, and testing rules programmatically within your CI/CD pipelines are essential steps to successfully running this zero-trust pattern in production.