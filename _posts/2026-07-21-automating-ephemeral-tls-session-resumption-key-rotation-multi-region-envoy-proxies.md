---
layout: post
title: "Automating Ephemeral TLS Session Resumption Key Rotation Across Multi-Region Envoy Proxies"
date: 2026-07-21 08:00:00 +0700
tags: [envoy, tls, devsecops, security, kubernetes]
description: "Build an automated, zero-downtime pipeline to rotate TLS Session Ticket Encryption Keys (STEKs) across multi-region Envoy fleets using SDS."
image: "https://picsum.photos/seed/2573/1080/720"
thumbnail: "https://picsum.photos/seed/2573/400/300"
---

In a high-throughput, multi-region architecture where user requests are routed dynamically across regions (e.g., using Latency-Based Routing or Anycast DNS), the cost of full TLS handshakes is a massive operational and performance tax. A full TLS 1.2 or even a 1-RTT TLS 1.3 handshake over a high-latency WAN connection can easily add 150ms to 300ms of latency to the first byte, killing the user experience and saturating edge gateway CPU cores with costly asymmetric cryptographic operations. To combat this, senior engineers rely on TLS Session Resumption via Session Tickets (RFC 5077), which allows gateways to encrypt session state and hand it back to the client as a ticket, reducing subsequent handshakes to 0-RTT or 1-RTT. However, in a multi-region deployment, if your Envoy proxies do not share the exact same Session Ticket Encryption Keys (STEKs), or if those keys are not rotated in lockstep, any cross-region failover or regional load-balancing shift will invalidate those tickets, forcing clients into full handshakes and triggering catastrophic CPU spikes at your ingress gateways. Compounding this operational risk is a cryptographic one: if STEKs are static, a compromise of the key allows an attacker to decrypt all past recorded session traffic, violating Perfect Forward Secrecy (PFS). Therefore, automating the rotation and synchronization of ephemeral STEKs across global Envoy fleets without incurring downtime or handshake failures is a non-negotiable requirement for modern production platforms.

![Automating Ephemeral TLS Session Resumption Key Rotation Across Multi-Region Envoy Proxies Diagram](/images/diagrams/automating-ephemeral-tls-session-resumption-key-rotation-multi-region-envoy-proxies.svg)

## The Cryptographic Anatomy of Envoy STEKs

To understand the operational challenges of session ticket management, we must first examine the key structure itself. Envoy handles TLS Session Resumption using a set of symmetric keys known as Session Ticket Encryption Keys (STEKs). In the standard Envoy configuration, these keys are grouped into a keyring.

Each individual key in the Envoy STEK keyring has a fixed size of 80 bytes, structured as follows:
- **Key Name (16 bytes):** A public identifier used by the client to indicate to the server which key was used to encrypt the presented session ticket. This allows the server to select the correct key for decryption from its active keyring.
- **AES Encryption Key (32 bytes):** An AES-256 key used to encrypt and decrypt the session state payload inside the ticket.
- **HMAC Validation Key (32 bytes):** A key used to compute and verify the HMAC-SHA256 signature of the ticket, protecting against tampering.

Historically, teams configured these keys inline within the Envoy configuration YAML or referenced static files on disk. The snippet below illustrates this static approach, which acts as a major anti-pattern in cloud-native environments.

<script src="https://gist.github.com/mohashari/ae08d94f4ca9e227e930a2e4cfdd3bba.js?file=snippet-1.yaml"></script>

Relying on static inline keys or local files forces you to choose between two unacceptable failure modes. Either you keep keys static for long periods—violating perfect forward secrecy—or you trigger regular Envoy process reloads to load new keys. In high-traffic environments, hot-reloading Envoy frequently can lead to memory spikes, transient packet loss, and configuration synchronization delays across autoscaled nodes.

## The Multi-Region Architecture: Centralized Orchestration via Vault and SDS

Solving the rotation problem at scale requires separating key generation from key consumption. We achieve this by combining a centralized key orchestrator with Envoy's Secret Discovery Service (SDS) API.

Instead of writing keys to local files or pushing config files to each region, the architecture utilizes:
1. **A Centralized Coordinator:** A lightweight Go-based controller running as a CronJob (e.g., in a management Kubernetes cluster). This controller generates a cryptographically secure keyring and persists it to a highly available secret store, such as HashiCorp Vault.
2. **Regional SDS Servers:** A gRPC-based Secret Discovery Service deployed in each active region (e.g., us-east-1, eu-west-1, ap-southeast-1). These servers read the keyrings from Vault and stream them directly to regional Envoy fleets via the gRPC xDS protocol.
3. **Envoy SDS Clients:** Envoy gateways connect to their local regional SDS endpoint over a secure, mTLS-enabled gRPC stream. When the keyring in Vault updates, the SDS server pushes a `DiscoveryResponse` to all connected Envoy proxies, which immediately update their TLS context in-memory with zero downtime.

This push-based model ensures that all Envoy instances across all regions receive the new key material within seconds of generation, eliminating regional mismatches that cause session resumption failures.

## Designing the Ephemeral Key Generator in Go

To maintain strict cryptographic hygiene, session ticket encryption keys must be generated using a cryptographically secure pseudorandom number generator (CSPRNG). We must also guarantee that the keys conform precisely to Envoy’s expected 80-byte format.

The following Go implementation demonstrates how to generate a secure 80-byte STEK consisting of a 16-byte key name, a 32-byte encryption key, and a 32-byte HMAC validation key.

<script src="https://gist.github.com/mohashari/ae08d94f4ca9e227e930a2e4cfdd3bba.js?file=snippet-2.go"></script>

The output of this generator is pushed to our central secret store (Vault) using transit engine encryption or direct KV store write-paths. Once persisted, the next challenge is streaming this keyring to Envoy.

## Implementing the Envoy Secret Discovery Service (SDS) Server

The SDS API is a subset of Envoy’s xDS configuration protocol. It operates over a bidirectional gRPC stream, allowing an SDS server to dynamically push secrets (such as TLS certificates or session ticket keys) to Envoy.

The snippet below demonstrates a production-grade gRPC SDS server implementation in Go. It manages streaming connections from multiple Envoy proxies and provides thread-safe updates to push new keyrings immediately upon rotation.

<script src="https://gist.github.com/mohashari/ae08d94f4ca9e227e930a2e4cfdd3bba.js?file=snippet-3.go"></script>

## Envoy Configuration for Dynamic STEK via SDS

With the SDS server running, we must configure Envoy to consume secrets dynamically. We replace the static `session_ticket_keys` block in our listener config with `session_ticket_keys_sds_secret_config`.

Additionally, because STEK keyrings are highly sensitive cryptographic secrets, the gRPC cluster communicating with the SDS server must be secured using mutual TLS (mTLS). This prevents unauthorized workloads in your cluster from subscribing to the SDS feed and stealing the session encryption keys.

<script src="https://gist.github.com/mohashari/ae08d94f4ca9e227e930a2e4cfdd3bba.js?file=snippet-4.yaml"></script>

## Orchestrating the Key Lifecycle: Active, Accept-Only, and Deprecated Keys

When rotating keys across a global, multi-region architecture, instant propagation is a myth. DNS caching, CDN propagation delays, and network jitter mean that clients will frequently hit Region B presenting a ticket encrypted by Region A using an older key.

If Region B only knows about the absolute newest key, it will reject the ticket, causing a session resumption failure and forcing a full TLS handshake.

To prevent this, Envoy allows you to pass an array of keys instead of a single key. The position of a key in the array dictates its lifecycle state:
1. **Active Key (Index 0):** This key is used to encrypt all new session tickets issued to connecting clients. It is also used to decrypt incoming tickets.
2. **Accept-Only / Decrypt-Only Keys (Indices 1+):** These keys are *never* used to encrypt new tickets. However, if a client presents a ticket encrypted with one of these keys, Envoy successfully decrypts it, resumes the session, and seamlessly issues a *new* ticket encrypted with the current Active Key (Index 0). This is called "ticket upgrading."

We implement a sliding window of keys to ensure a smooth transition:
- **Key $T_0$ (Active):** Generated during the current rotation cycle.
- **Key $T_{-1}$ (Decrypt-Only):** The active key from the previous cycle. Handles clients with tickets issued in the last 12 hours.
- **Key $T_{-2}$ (Decrypt-Only):** The active key from two cycles ago. Handles clients with older tickets (up to 24 hours old).

The Go orchestrator below manages this lifecycle. It loads historical keys from Vault, shifts them down to demote them to decrypt-only, generates a fresh active key, and prunes keys older than 36 hours.

<script src="https://gist.github.com/mohashari/ae08d94f4ca9e227e930a2e4cfdd3bba.js?file=snippet-5.go"></script>

## Failure Modes, Edge Cases, and Observability

Operating this system at scale exposes several failure modes that you must design around.

### Failure Mode 1: Central Vault Outage or Network Split
If a regional SDS server cannot communicate with the central Vault instance, the rotation job will fail.
* **Bad Practice:** The SDS server returns an empty response or crashes, causing Envoy to lose its current keys.
* **Good Practice:** Leverage xDS’s state retention. If the SDS server cannot fetch new keys, it must continue streaming the last known good keyring. Envoy will maintain its existing in-memory TLS context, allowing session resumption to continue functioning with the old keys until Vault recovers.

### Failure Mode 2: Key Skew During Multi-Region DNS Failover
During a large-scale regional DNS failover, millions of users may suddenly be redirected from Region A to Region B. If the rotation cycles of the two regions are offset, clients will present tickets that Region B has already pruned from its keyring.
* **Mitigation:** Ensure your sliding window covers at least 3x the rotation interval. If you rotate keys every 12 hours, keep keys for 36 hours. This guarantees that even with a 12-hour synchronization delay between regions, the fallback keys will still decrypt the client tickets.

### Monitoring Resumption Rates with Prometheus
To verify that your key rotation is functioning correctly, you must monitor the ratio of resumed TLS sessions to total TLS connections. A sudden drop in this ratio indicates that Envoy is rejecting tickets due to key mismatches.

The PromQL alerting rule below detects this exact failure mode, raising an alert if the resumption rate drops below 30% for a sustained period.

<script src="https://gist.github.com/mohashari/ae08d94f4ca9e227e930a2e4cfdd3bba.js?file=snippet-6.yaml"></script>

By coupling this alerting with the metrics exposed by Envoy's DownstreamTlsContext, such as `ssl.connection_error` and `sds.secret_not_ready`, you can pinpoint whether a resumption drop is caused by network issues or key sync errors. This setup ensures that your global ingress gateways maintain sub-millisecond handshake overhead without sacrificing forward secrecy.