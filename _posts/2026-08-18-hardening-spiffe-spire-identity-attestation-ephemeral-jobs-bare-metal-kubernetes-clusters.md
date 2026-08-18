---
layout: post
title: "Hardening SPIFFE/SPIRE Identity Attestation for Ephemeral Batch Jobs in Kubernetes"
date: 2026-08-18 08:00:00 +0700
tags: [spiffe, spire, kubernetes, security, devsecops]
description: "A deep dive into securing SPIFFE/SPIRE workload attestation for short-lived, ephemeral Kubernetes Jobs against token reuse and PID recycling."
image: "https://picsum.photos/seed/4878/1080/720"
thumbnail: "https://picsum.photos/seed/4878/400/300"
---

In modern microservices architectures, transition to zero-trust networking requires cryptographic identity verification for every executing process. While long-running services (like gRPC APIs or web applications) can comfortably manage SPIFFE/SPIRE SVID rotations over hours, short-lived ephemeral workloads—such as Kubernetes Jobs running ETL pipelines, ML training steps, or database migrations—introduce severe security and operational challenges. If your batch jobs run for only 15 seconds, a standard SPIRE configuration exposes you to race conditions where compromised co-located workloads can siphon off credentials, or rate-limit the Kubernetes API server due to high-churn metadata queries. In high-churn environments where hundreds of batch pods are spawned and destroyed every minute, standard out-of-the-box SPIRE configurations fail. This post details how to harden SPIFFE/SPIRE node and workload attestation specifically for ephemeral Kubernetes batch jobs, addressing race conditions, pod identity recycling, and secure cloud integration.

## The Ephemeral Workload Attestation Vector: The Race Against the Cache

To understand the vulnerabilities, we must examine how the SPIRE Agent attests a workload. When a container starts, it queries the local SPIRE Agent over a Unix domain socket. The agent identifies the calling process's PID by inspecting the socket connection via `SO_PEERCRED` syscall options, reads the namespace layout and the `/proc/<pid>/cgroup` structure of the calling process, and matches that metadata against the Kubernetes API or Kubelet cache to resolve the container ID, pod name, and namespace.

This process contains a critical security vulnerability: **PID and Container ID Recycling**.
In Linux, the system variable `/proc/sys/kernel/pid_max` is frequently set to a default value of 32768. In high-throughput clusters running thousands of short-lived batch jobs, PIDs are recycled rapidly. If a privileged batch job pod (e.g., `db-migration-482a`) starts, fetches its credential, and exits within 500 milliseconds, its PID is immediately freed. If the API server event stream to the SPIRE Agent is delayed, the agent's internal cache may still associate that recycled PID with the privileged pod. A malicious, public-facing container on the same node that spawns a process with the recycled PID can immediately call the agent socket and receive the database migration's SVID.

Furthermore, if you rely solely on weak selectors like `k8s:ns` or `k8s:sa` without enforcing cryptographic ties to the specific container instance, any process running under that service account in the namespace can retrieve the SVID. To secure ephemeral jobs, we must lock down the attestation process at the node level, the workload level, and the client application level.

## Hardening SPIRE Agent Attestation on Shared Nodes

Workload attestation is only as secure as the agent performing it. On shared Kubernetes nodes, the SPIRE Agent itself must be attested using cryptographically strong credentials. In production environments, rely on **Kubernetes Projected Service Account Tokens (PSAT)** combined with Cloud Provider Node Attestation (like AWS Instance Identity Documents). 

PSAT avoids the security issues of static Service Account tokens by issuing time-limited, audience-restricted tokens directly to the SPIRE Agent pod. 

Snippet-1 demonstrates the hardened SPIRE Agent configuration using `k8s_psat` as the node attestation mechanism.

<script src="https://gist.github.com/mohashari/1d7f46dbc8015991187488ff6039a2ba.js?file=snippet-1.yaml"></script>

On the server side, we must strictly define which service accounts are allowed to attest as SPIRE Agents. Allowing any agent token to register could let a compromised pod impersonate an agent.

Snippet-2 shows the SPIRE Server configuration defining the allowed service accounts for node attestation.

<script src="https://gist.github.com/mohashari/1d7f46dbc8015991187488ff6039a2ba.js?file=snippet-2.hcl"></script>

## Securing Pod Attestation: Moving Beyond Namespaces

A common anti-pattern in SPIRE registration is using broad wildcard selectors for batch jobs, such as registering an entry with `k8s:ns:prod` and `k8s:sa:default`. This allows *any* pod running with the default service account in the `prod` namespace to claim the identity.

Instead, ephemeral batch jobs must use the `k8s_psat` workload attester. This attester validates the pod's identity by requiring the workload container to mount a projected token. SPIRE validates this token against the Kubernetes TokenReview API to verify the caller's namespace, service account name, and—crucially—the unique **Pod UID**.

Snippet-3 shows how to register an entry on the SPIRE Server that strictly binds the identity to the `db-migration-sa` service account, requiring a specific pod label and utilizing the `k8s_psat` attester.

<script src="https://gist.github.com/mohashari/1d7f46dbc8015991187488ff6039a2ba.js?file=snippet-3.sh"></script>

To support this attestation path, the Kubernetes `Job` resource must project the token volume into the container and mount the SPIFFE Workload API socket via the SPIFFE CSI Driver. Mounting the socket via CSI is significantly more secure than using a standard hostPath mount, as the CSI driver dynamically verifies pod properties and prevents container escape paths.

Snippet-4 outlines a production-ready Kubernetes `Job` specification configured with the SPIFFE CSI driver and a projected service account token volume.

<script src="https://gist.github.com/mohashari/1d7f46dbc8015991187488ff6039a2ba.js?file=snippet-4.yaml"></script>

## Mitigating the Pod Lifecycle Race Condition

When a short-lived container initializes, it may immediately call the SPIFFE Workload API to request an SVID. If the SPIRE Agent has not received the container creation event from the Kubernetes API server, it will return an `Unavailable` or `WorkloadNotRegistered` error. In high-churn environments, this synchronization delay can range from 100 milliseconds to over 2 seconds.

To address this, your application bootstrap code must implement an active connection retry loop with exponential backoff. Do not allow your batch jobs to fail immediately upon starting.

Snippet-5 provides a robust Go implementation utilizing the `go-spiffe/v2` SDK to establish connection retries and fetch the SVID before initiating downstream database connections.

<script src="https://gist.github.com/mohashari/1d7f46dbc8015991187488ff6039a2ba.js?file=snippet-5.go"></script>

To fully secure against PID recycling, SPIRE Agents can be configured to check the start time of the requesting process via `/proc/<pid>/stat`. By asserting that the process start time matches the container start time registered in the container runtime, the agent guarantees that a recycled PID cannot acquire SVIDs registered for the dead container.

## Fine-Grained OIDC Federation for Ephemeral Jobs

One of the most powerful use cases for SPIFFE identities in ephemeral jobs is avoiding long-lived cloud credentials (like AWS IAM Access Keys) inside Kubernetes. By establishing OIDC federation between the SPIRE Server and your cloud provider, your ephemeral job can exchange its SPIFFE JWT SVID for temporary cloud credentials (such as an AWS IAM Role) via the Security Token Service (STS).

To restrict access, the cloud provider's IAM role must trust only the SPIRE OIDC Provider and validate the exact SPIFFE ID of the database migration job in the OIDC `sub` claim. 

Snippet-6 shows the AWS IAM Role Trust Policy configured to restrict access strictly to the OIDC subject representing our ephemeral database migration job.

<script src="https://gist.github.com/mohashari/1d7f46dbc8015991187488ff6039a2ba.js?file=snippet-6.json"></script>

By constraining the `sub` claim, a compromised pod running in a different namespace (e.g. `dev`) cannot assume this role, even if it tries to exploit the same OIDC provider.

## Operationalizing SPIRE for High-Throughput Batch Clusters

Scaling SPIFFE/SPIRE to clusters that spawn thousands of ephemeral pods per hour introduces scaling limits on the SPIRE Server database and the Kubernetes API server. By default, SPIRE Server writes registration entry audits and token states directly to its datastore. High churn rates can lead to database connection exhaustion and API server rate-limiting.

To scale effectively:
1. **Implement Connection Pooling:** Configure robust database parameters for connection pooling.
2. **Kubelet-only Metadata resolution:** Offload the Kubernetes API Server by configuring SPIRE Agent to read pod metadata directly from the local Kubelet read-only port (port 10255 or secure 10250) rather than querying the API Server.

Snippet-7 shows the SPIRE Server configuration overrides for high-performance PostgreSQL connection pooling and workload attestation rate-limiting.

<script src="https://gist.github.com/mohashari/1d7f46dbc8015991187488ff6039a2ba.js?file=snippet-7.yaml"></script>

Snippet-8 demonstrates how to configure the SPIRE Agent's Kubernetes workload attester to resolve metadata directly from the Kubelet interface.

<script src="https://gist.github.com/mohashari/1d7f46dbc8015991187488ff6039a2ba.js?file=snippet-8.hcl"></script>

## Conclusion: The Zero-Trust Ephemeral Checklist

Hardening SPIFFE/SPIRE for ephemeral Kubernetes jobs is not a nice-to-have; it is a necessity to prevent identity theft in high-churn container clusters. Before deploying your next batch workload, verify the following configuration points:

*   Ensure the SPIRE Agent uses PSAT node attestation rather than static tokens.
*   Restrict registration entries to use the `k8s_psat` workload attester to bind identity to the unique Pod UID.
*   Implement socket mounts via the SPIFFE CSI Driver to avoid insecure hostpath binds.
*   Program client applications to handle agent connection delays using exponential backoff and retries.
*   Enforce strict `sub` claim validation in your cloud OIDC trust policies.
*   Offload the Kubernetes control plane by routing agent metadata queries to the local Kubelet.

By implementing these patterns, you eliminate credential exposure windows and build a highly resilient, zero-trust infrastructure suitable for high-scale enterprise environments.