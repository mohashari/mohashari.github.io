---
layout: post
title: "mTLS in Production: Mutual Authentication Between Microservices"
date: 2026-03-18 07:00:00 +0700
tags: [security, mtls, tls, microservices, networking]
description: "Implement mutual TLS for service-to-service authentication using SPIFFE/SPIRE, certificate rotation, and zero-downtime rollout strategies in Kubernetes."
---

Every microservice mesh starts with good intentions: internal traffic is "trusted," the network perimeter is the boundary, and service-to-service calls are just HTTP. Then comes the first lateral movement incident, the compliance audit that asks how Service A proves it's actually talking to Service B and not a rogue process on a compromised pod, or the penetration test that trivially spoofs an internal service identity. The answer the industry has converged on is mutual TLS — where both the client and server present certificates and cryptographically prove who they are before a byte of application data flows. mTLS eliminates the assumption of trust based on IP address or network segment and replaces it with identity grounded in PKI. This post walks through the full production story: bootstrapping a SPIFFE/SPIRE-based certificate authority, writing the Go plumbing to consume SVID certificates, automating rotation without dropping connections, and rolling the change out to a live Kubernetes cluster.

## Why SPIFFE Instead of Rolling Your Own CA

A hand-rolled internal CA — a `ca.crt`, a handful of `openssl req` commands, and a shared secret baked into a ConfigMap — works in demos and breaks in production. Certificates expire, rotation is manual, and when a service scales to fifty pods you have fifty certificates to track. SPIFFE (Secure Production Identity Framework For Everyone) standardises the identity document format (the SVID, or SPIFFE Verifiable Identity Document) and SPIRE is the reference implementation that issues and rotates them automatically. Each workload gets a short-lived X.509 SVID tied to its Kubernetes service account, and SPIRE rotates it before expiry with no operator involvement.

Install SPIRE server and agent using the upstream Helm chart, but override the trust domain and node attestor to match your cluster:

<script src="https://gist.github.com/mohashari/84a4f786e60be5d8799aef57d7f7deb1.js?file=snippet.yaml"></script>

<script src="https://gist.github.com/mohashari/84a4f786e60be5d8799aef57d7f7deb1.js?file=snippet-2.sh"></script>

## Registering Workload Identities

SPIRE needs to know which Kubernetes workloads map to which SPIFFE IDs. A `ClusterSPIFFEID` custom resource (provided by the SPIRE controller manager) handles this declaratively. Each service gets a SPIFFE URI in the form `spiffe://<trust-domain>/ns/<namespace>/sa/<service-account>`.

<script src="https://gist.github.com/mohashari/84a4f786e60be5d8799aef57d7f7deb1.js?file=snippet-3.yaml"></script>

The `ttl` of one hour is intentionally short. Short-lived certificates are the whole point — if a certificate leaks, the blast radius is bounded by the TTL, not by whether your security team notices and manually revokes it.

## Consuming SVIDs in Go with the Workload API

SPIRE exposes a Unix socket inside each pod that serves the SPIFFE Workload API. The `go-spiffe` library wraps this into idiomatic Go, giving you a `tls.Config` that automatically refreshes as SPIRE rotates certificates. This is the critical detail most mTLS tutorials miss: you must not load the certificate once at startup. You must use a callback or a live `X509Source` so the TLS stack picks up the new certificate before the old one expires.

<script src="https://gist.github.com/mohashari/84a4f786e60be5d8799aef57d7f7deb1.js?file=snippet-4.go"></script>

## Wiring the Server into Your Service

With the `NewMTLSServer` helper above, the application entry point becomes a thin wrapper. The critical operational detail is mounting the SPIRE agent socket — the sidecar injection annotation handles this automatically if you're using SPIRE's Kubernetes integration.

<script src="https://gist.github.com/mohashari/84a4f786e60be5d8799aef57d7f7deb1.js?file=snippet-5.go"></script>

## Pod Spec: Mounting the SPIRE Socket

The SPIRE agent daemonset creates a `hostPath` volume for the socket. Pods must opt in by mounting it. If you use the SPIRE controller manager's webhook for injection, this is automatic. For manual configuration:

<script src="https://gist.github.com/mohashari/84a4f786e60be5d8799aef57d7f7deb1.js?file=snippet-6.yaml"></script>

## Zero-Downtime Rollout: The Permissive Phase

You cannot flip a live cluster from plaintext to mTLS in one deployment without causing a cascade of connection failures. The safe path is a three-phase rollout. Phase one is permissive mode: services accept both TLS and plaintext, but always initiate TLS. Phase two enables strict server-side verification. Phase three removes the plaintext listener entirely. Track which phase each service is in via a label on the deployment and use Prometheus metrics on TLS handshake errors to gate promotion between phases.

<script src="https://gist.github.com/mohashari/84a4f786e60be5d8799aef57d7f7deb1.js?file=snippet-7.sh"></script>

## Verifying the Handshake End-to-End

Before declaring a service production-ready, verify the full handshake from outside the cluster using `openssl s_client`. This lets you inspect the presented certificate, confirm the trust chain, and validate the server is requiring client authentication — not silently accepting anonymous connections.

<script src="https://gist.github.com/mohashari/84a4f786e60be5d8799aef57d7f7deb1.js?file=snippet-8.sh"></script>

---

The operational payoff of this stack is not just security theatre — it is a concrete, auditable answer to "how does Service A know it's talking to Service B?" grounded in short-lived cryptographic identities rather than network ACLs that drift. SPIRE handles the certificate lifecycle so your engineers do not have to, the `go-spiffe` `X509Source` ensures the TLS stack always presents a valid certificate even through rotation, and the three-phase rollout lets you migrate a live system without a maintenance window. Once all services are in phase three, your service mesh has a hardened baseline: no service can impersonate another, no lateral movement is possible without a valid SVID, and your compliance team has the PKI audit trail they've been asking for.