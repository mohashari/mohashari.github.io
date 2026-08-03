---
layout: post
title: "Automating Certificate Authority Rotation in HashiCorp Vault with Zero-Downtime mTLS"
date: 2026-08-03 08:00:00 +0700
tags: [devsecops, vault, mtls, security]
description: "A comprehensive, production-tested guide to executing zero-downtime intermediate CA rotations using HashiCorp Vault and dynamic TLS configurations in Go."
image: "https://picsum.photos/seed/5235/1080/720"
thumbnail: "https://picsum.photos/seed/5235/400/300"
---

At 3:00 AM, an unmonitored intermediate Certificate Authority (CA) expires. Within seconds, cascading connection failures rip through your Kubernetes cluster as microservices reject each other's mutually authenticated TLS (mTLS) handshakes. In modern microservice architectures, certificate authority rotation is the ultimate test of operational resilience. Hardcoding CAs or relying on manual, lock-step redeployments runs a high risk of catastrophic outages. To achieve true zero-downtime CA rotation, you must implement a multi-stage trust model where clients and servers dynamically fetch, trust, and transition between old and new CAs without restarting services. This post details how to configure HashiCorp Vault’s PKI secrets engine, manage transition states, reload TLS configurations dynamically in Go, and mitigate the infamous "HTTP/2 connection pinning" problem that plagues gRPC environments during certificate rollouts.

![Automating Certificate Authority Rotation in HashiCorp Vault with Zero-Downtime mTLS Diagram](/images/diagrams/automating-certificate-authority-rotation-hashicorp-vault-zero-downtime-mtls.svg)

## The Operational Nightmare of Manual CA Rotation

In a typical production environment with hundreds of microservices, manual CA rotation is a recipe for failure. The standard failure pattern is predictable: an operator generates a new CA, replaces the old certs, and restarts the services. However, during the rolling update, services with the new certificates attempt to talk to services still running the old certificates. Handshakes fail instantly with `x509: certificate signed by unknown authority` or `x509: certificate has expired or is not yet valid`. 

The core issue is a lack of overlapping trust. If a client presents a certificate signed by Issuer B, but the server only knows and trusts Issuer A, the connection is rejected. To prevent this, your infrastructure must support a transitional state where all nodes trust both the old and new CAs simultaneously. Furthermore, workloads must be able to reload their trust stores and leaf certificates in-memory. If a service requires a process restart to pick up a new CA certificate, you do not have automated rotation; you have a scheduled maintenance window.

## The Multi-Phase CA Rotation Lifecycle (The Dual-Trust Model)

Achieving zero-downtime CA rotation requires a strict four-phase lifecycle. This process ensures that at no point during the transition does a client or server encounter a certificate it cannot validate.

1. **Phase 1: Dual-Trust Distribution (Cross-Signing/Dual-Trust):** You generate the new CA (Issuer B). You distribute the CA certificate of Issuer B to all workloads so it is appended to their trust stores. During this phase, workloads continue to request and present leaf certificates signed by the old CA (Issuer A). However, they are now cryptographically prepared to trust certificates from Issuer B.
2. **Phase 2: Active Issuer Promotion (Dual-Issuance):** Once all workloads have updated their trust stores to include both Issuer A and Issuer B, you switch the active issuer in Vault to Issuer B. All subsequent certificate signing requests (CSRs) yield certificates signed by Issuer B. Because all workloads trust both issuers, old certificates (signed by A) and new certificates (signed by B) coexist and interoperate seamlessly.
3. **Phase 3: Certificate Natural Expiry:** You wait for all outstanding leaf certificates signed by Issuer A to expire. If your leaf certificate TTL is 24 hours, this phase must last at least 24 hours.
4. **Phase 4: Trust Store Deprecation and Cleanup:** Once all leaf certificates signed by Issuer A are gone, you safely remove Issuer A from the trust stores of all workloads, completing the rotation.

## Step 1: Provisioning the Dual-Trust State in Vault

Using HashiCorp Vault's PKI Secrets Engine, we can manage multiple keys and issuers within the same backend path (introduced in Vault 1.11+). This allows us to maintain a clean API contract for our workloads while shifting the underlying cryptographic keys behind the scenes.

First, we mount our intermediate PKI secrets engine using Terraform:

<script src="https://gist.github.com/mohashari/7e1760ec3fb25620dd86e4fcfefde454.js?file=snippet-1.hcl"></script>

Now, we perform the transition command-line actions to generate a new key and intermediate issuer. Instead of overwriting the existing issuer, we create a secondary one.

<script src="https://gist.github.com/mohashari/7e1760ec3fb25620dd86e4fcfefde454.js?file=snippet-2.sh"></script>

At this stage, Vault has two issuers. The original issuer remains the default for signing new leaf certificate requests, but the new issuer is registered and its CA certificate is appended to the CA chain returned by Vault.

## Step 2: Implementing Dynamic TLS Reloading in Go

To achieve zero downtime, applications must reload their certificate pools and active certificates dynamically. In Go, mutating fields on a running `tls.Config` after starting an HTTP or gRPC server is a race condition. The correct pattern is to leverage the `GetConfigForClient` callback for servers and the `GetClientCertificate` callback for clients.

The following Go package implements a thread-safe `DynamicTLSProvider` that monitors certificates and CA bundles on disk, updating the TLS configuration on the fly without interrupting active connections:

<script src="https://gist.github.com/mohashari/7e1760ec3fb25620dd86e4fcfefde454.js?file=snippet-3.go"></script>

This pattern ensures that every new TCP connection triggers a handshake with the latest certificates and root/intermediate pools, eliminating the need to restart long-running processes when rotating.

## Step 3: Promoting the New Issuer (Active Issuance)

Once the new CA bundle has been distributed to all workloads (which now run the dynamic reloader code), we proceed to Phase 2: promoting the new issuer to sign all subsequent leaf certificates.

<script src="https://gist.github.com/mohashari/7e1760ec3fb25620dd86e4fcfefde454.js?file=snippet-4.sh"></script>

Any workload that calls Vault's `/pki_int/issue/:role` endpoint from this moment forward will receive a certificate signed by the new intermediate CA. Because both old and new CAs are in the workloads' trust pools, existing workloads running certificates signed by the old CA will accept calls from updated workloads, and vice-versa.

## Step 4: Mitigating gRPC Stream Pinning with Keepalive Parameters

Even with dynamic TLS reloading, long-lived connections pose a critical threat to CA rotation. By default, HTTP/2 (and by extension gRPC) multiplexes multiple logical requests over a single persistent TCP connection. 

If a client service establishes a connection to a server at Hour 0 using a certificate signed by the old CA, that connection will remain open indefinitely. The client will never perform a new TLS handshake, which means it will never present its new certificate (signed by the new CA), nor will it evaluate the server's new certificate. When the old CA eventually expires, these "pinned" connections will suddenly break or, worse, continue running on stale, unvalidated credentials.

To force client connections to recycle and perform fresh handshakes, you must enforce server-side keepalive parameters. In Go, you can configure your gRPC servers with explicit connection limits:

<script src="https://gist.github.com/mohashari/7e1760ec3fb25620dd86e4fcfefde454.js?file=snippet-5.go"></script>

When the `MaxConnectionAge` is reached, the gRPC server sends an HTTP/2 `GOAWAY` frame to the client. This tells the client to stop creating new streams on the current connection and to establish a new TCP connection for subsequent requests. The new connection triggers a fresh TLS handshake, loading the latest certs and trust pools.

## Step 5: Automation and Orchestration with Consul-Template

To bridge the gap between Vault and the on-disk files read by our `DynamicTLSProvider`, we can use `consul-template`. This daemon runs side-by-side with our application container, watching Vault for changes, rewriting certificate files, and notifying the app to execute a reload.

<script src="https://gist.github.com/mohashari/7e1760ec3fb25620dd86e4fcfefde454.js?file=snippet-6.yaml"></script>

The trigger commands send a lightweight HTTP POST to the local application container's management port `/reload`, which executes the `Reload()` method on our `DynamicTLSProvider`.

## Step 6: Verification & Automated Integration Testing

To guarantee the reliability of your rotation scripts before deploying them to production, you should run integration tests simulating the transition state. The following Python script implements a test server and client that verify handshakes succeed during the dual-trust transition phase.

<script src="https://gist.github.com/mohashari/7e1760ec3fb25620dd86e4fcfefde454.js?file=snippet-7.py"></script>

By pointing the client to a client-certificate signed by the old CA and the server to a certificate signed by the new CA, and verifying that the handshake completes when both sides load the dual-trust `ca_file`, you confirm that your configuration is immune to split-brain validation errors.

## Production Checklists and Failure Modes

When orchestrating this sequence, monitor your systems for the following failure modes:

* **Clock Skew:** If nodes in your cluster have clock drift exceeding a few seconds, new certificates might have a "Not Before" validity period in the future relative to the client's system clock. This causes client handshakes to fail with invalid date errors. Ensure NTP is strictly running across all physical hypervisors.
* **Large Trust Bundles:** During rotation, the CA chain contains multiple root and intermediate certificates. Some TLS implementations limit the size of the certificate request payload during handshakes. Ensure your trust bundles do not contain stale, expired issuers; clean them up immediately after leaf certificate transition.
* **Vault Lease Overload:** When you promote the new issuer, many workloads will simultaneously detect the configuration change and attempt to renew their leaf certificates. To prevent Vault from being overwhelmed, implement randomized jitters (e.g., renewing certificates at 50% to 70% of their TTL) to spread out the request load.