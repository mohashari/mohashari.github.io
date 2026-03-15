---
layout: post
title: "mTLS in Practice: Securing Service-to-Service Communication"
date: 2026-04-13 07:00:00 +0700
tags: [mtls, tls, security, microservices, certificates]
description: "Implement mutual TLS to authenticate and encrypt traffic between backend services, from certificate management to rotation."
---

In distributed systems, trusting a service just because it knows a password is a gamble that has ended badly for too many production environments. Traditional one-way TLS verifies the server to the client, but leaves the server with no cryptographic way to prove who it's talking to on the other side. When your payment service calls your fraud-detection service, you want both parties to prove their identity before a single byte of business data is exchanged. Mutual TLS (mTLS) closes this gap by requiring both sides of a connection to present valid X.509 certificates signed by a shared Certificate Authority. It's not new technology, but getting it right in a real microservices deployment — managing certificate lifecycles, rotating certs without downtime, and threading it through your infrastructure — is where most teams stumble. This post walks through a production-grade mTLS setup end-to-end.

## Why One-Way TLS Is Not Enough

Standard TLS gives you encryption in transit and server authentication. Your browser trusts `api.example.com` because its cert was signed by a CA your OS trusts. But the server has no equivalent guarantee about you. In a service mesh, this means any process that can reach port 8443 on your payments service can attempt to send it traffic. Network-level controls like VPCs and security groups add defense-in-depth, but they are coarse and operationally fragile. mTLS moves authentication into the transport layer itself: if a client cannot present a certificate signed by your internal CA, the TLS handshake fails before the HTTP layer ever sees a byte.

## Generating a Private CA and Service Certificates

The foundation is a private Certificate Authority that you control. In production you would use a tool like HashiCorp Vault's PKI secrets engine or AWS Private CA, but understanding the raw mechanics with OpenSSL is essential for debugging.

OpenSSL can build your entire CA hierarchy from the command line. Generate a CA key and self-signed root cert, then issue leaf certificates for each service.

<script src="https://gist.github.com/mohashari/2bdcdc3df76f99a370fa920a1d382e52.js?file=snippet.sh"></script>

Note the 90-day validity on the leaf cert. Short-lived certificates are a security best practice: if a cert is compromised, it expires quickly without relying on CRL or OCSP infrastructure. Plan your automation around rotation from day one.

## Configuring an mTLS Server in Go

With certificates in hand, the server must be configured to require and verify client certificates. Go's `crypto/tls` package exposes precise control over this behavior through `tls.Config`.

The `ClientAuth` field is the key lever here. Setting it to `tls.RequireAndVerifyClientCert` tells the TLS stack to reject any handshake where the client does not present a cert signed by one of the CAs in `ClientCAs`.

<script src="https://gist.github.com/mohashari/2bdcdc3df76f99a370fa920a1d382e52.js?file=snippet-2.go"></script>

Extracting the `CommonName` from `r.TLS.PeerCertificates[0]` gives you the verified identity of the caller, which you can use for authorization decisions without any additional token or header.

## Building the mTLS Client in Go

The client mirrors the server setup: it presents its own certificate and validates the server against the same CA. Reusing the standard `http.DefaultClient` without custom TLS config is a common mistake that bypasses the entire handshake.

<script src="https://gist.github.com/mohashari/2bdcdc3df76f99a370fa920a1d382e52.js?file=snippet-3.go"></script>

## Packaging Certificates in a Container

In containerized deployments, certificates should never be baked into the image. Mount them at runtime via secrets or a volume. Here is a minimal `Dockerfile` that expects certs to be injected at `/run/secrets`.

<script src="https://gist.github.com/mohashari/2bdcdc3df76f99a370fa920a1d382e52.js?file=snippet-4.dockerfile"></script>

## Automating Certificate Rotation with a Sidecar

The operational burden of mTLS is certificate rotation. A 90-day cert expiring on a Friday night is a real incident. The standard pattern is a sidecar process that watches the cert files and sends `SIGHUP` to the main process when they are replaced by your cert manager (cert-manager on Kubernetes, Vault Agent, etc.).

Go's `tls.Config` supports this cleanly through `GetCertificate` and `GetClientCertificate` callbacks, which are invoked per-handshake rather than at startup — enabling live rotation with zero downtime.

<script src="https://gist.github.com/mohashari/2bdcdc3df76f99a370fa920a1d382e52.js?file=snippet-5.go"></script>

## Validating the Handshake End-to-End

Before rolling out to production, verify the mTLS handshake explicitly with `openssl s_client`. The `-cert` and `-key` flags supply the client certificate.

<script src="https://gist.github.com/mohashari/2bdcdc3df76f99a370fa920a1d382e52.js?file=snippet-6.sh"></script>

Running the expiry check as a daily cron job and alerting when any certificate is within 14 days of expiry is a cheap, high-value safety net.

## Enforcing Identity-Based Authorization

Encrypted and authenticated traffic is not the same as authorized traffic. Once mTLS gives you a verified `CommonName`, enforce it explicitly. A simple middleware pattern prevents service impersonation even among internal peers.

<script src="https://gist.github.com/mohashari/2bdcdc3df76f99a370fa920a1d382e52.js?file=snippet-7.go"></script>

mTLS is not a silver bullet, but it eliminates an entire class of service impersonation attacks with infrastructure you likely already have. The implementation cost is highest at the beginning — building the CA, threading certs through your deploy pipeline, wiring the reload logic — but once that scaffolding exists, every new service gets cryptographic identity for free. Start with your highest-value service boundaries, validate with `openssl s_client` before every rollout, automate expiry alerting from day one, and use the verified `CommonName` to drive authorization decisions rather than relying solely on network topology. The combination of short-lived certs, per-handshake reloading, and identity-based authorization gives you a defense-in-depth posture that holds even when other perimeter controls fail.