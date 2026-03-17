---
layout: post
title: "HashiCorp Vault: Secrets Management and Dynamic Credentials in Production"
date: 2026-03-18 07:00:00 +0700
tags: [security, vault, secrets, devops, cloud]
description: "Configure HashiCorp Vault for static and dynamic secret leasing, PKI automation, and audit logging in production Kubernetes environments."
---

# HashiCorp Vault: Secrets Management and Dynamic Credentials in Production

Every production incident involving leaked credentials follows a familiar pattern: a developer hardcoded a database password in a config file, that config ended up in version control, and months later an attacker quietly exfiltrated data for weeks before anyone noticed. Static secrets are a liability by design — they don't expire, they're easy to copy, and they accumulate across CI pipelines, Kubernetes manifests, and developer laptops like unpaid technical debt. HashiCorp Vault was built to eliminate this entire class of problem by treating secrets as ephemeral, auditable, and role-scoped. This post walks through a production-grade Vault setup: initializing and unsealing a cluster, writing secrets engine policies, configuring dynamic database credentials, automating PKI certificate issuance, and wiring it all into a Kubernetes workload using the Vault Agent sidecar.

## Initializing and Unsealing Vault

Before any secrets can be stored or issued, Vault must be initialized and unsealed. Initialization generates the root key and splits it into Shamir shares. In production you'll store these shares in separate secure locations — never in the same system. The unseal process reconstructs the root key in memory without ever writing it to disk.

<script src="https://gist.github.com/mohashari/1b27128d30818655a40f0c7747dbc165.js?file=snippet.sh"></script>

In real deployments, replace manual unseal with auto-unseal via AWS KMS, GCP Cloud KMS, or Azure Key Vault. This allows Vault pods to restart and unseal automatically without human intervention, which is essential for Kubernetes environments where pods can be rescheduled at any time.

## Enabling Secrets Engines and Writing Policies

Vault uses a policy system based on HCL (HashiCorp Configuration Language) to control which tokens can access which paths. Policies are least-privilege by default — if a path isn't explicitly permitted, access is denied. Here we enable the KV v2 secrets engine for static secrets and write a policy scoped to a specific application namespace.

<script src="https://gist.github.com/mohashari/1b27128d30818655a40f0c7747dbc165.js?file=snippet-2.hcl"></script>

<script src="https://gist.github.com/mohashari/1b27128d30818655a40f0c7747dbc165.js?file=snippet-3.sh"></script>

Keeping policies narrow and path-scoped is the most important operational habit in Vault. A policy that grants `secret/*` is nearly as dangerous as no Vault at all — when a token is compromised, the blast radius is limited only by its policy.

## Dynamic Database Credentials

Static database passwords that rotate every 90 days (when teams remember) are a common compromise vector. Vault's database secrets engine generates unique, short-lived credentials for each requesting service instance. When the lease expires, Vault revokes the credentials directly in the database. No rotation scripts, no shared passwords.

<script src="https://gist.github.com/mohashari/1b27128d30818655a40f0c7747dbc165.js?file=snippet-4.sql"></script>

<script src="https://gist.github.com/mohashari/1b27128d30818655a40f0c7747dbc165.js?file=snippet-5.sh"></script>

The resulting credentials look like `v-payments-abcd1234-xyz` — machine-generated, traceable back to the requesting token, and automatically revoked when the lease expires or when you call `vault lease revoke`.

## Reading Dynamic Credentials from Go

Your application should request credentials at startup and handle lease renewal rather than caching a password in an environment variable. Here is a minimal Go client that fetches database credentials from Vault and constructs a `*sql.DB` connection pool.

<script src="https://gist.github.com/mohashari/1b27128d30818655a40f0c7747dbc165.js?file=snippet-6.go"></script>

In production, pair this with a goroutine that calls `client.Auth().Token().RenewSelf()` before the lease TTL expires. The Vault API SDK also provides a `LifetimeWatcher` helper that handles renewal and re-issue automatically.

## PKI Automation for Internal TLS

Manually managing internal TLS certificates leads to the same problems as static passwords: certificates expire and break things, or teams disable verification to avoid the pain. Vault's PKI secrets engine acts as an internal CA, issuing short-lived certificates on demand.

<script src="https://gist.github.com/mohashari/1b27128d30818655a40f0c7747dbc165.js?file=snippet-7.sh"></script>

With 24-hour certificate TTLs, your mTLS mesh rotates certificates daily with zero operator involvement. If a certificate is compromised, it expires within hours rather than years.

## Kubernetes Auth and Vault Agent Sidecar

The final piece is authenticating Kubernetes workloads to Vault without injecting a token into the pod spec. The Kubernetes auth method validates the pod's service account JWT against the Kubernetes API, and the Vault Agent sidecar injects the resulting secrets as files into a shared volume.

<script src="https://gist.github.com/mohashari/1b27128d30818655a40f0c7747dbc165.js?file=snippet-8.yaml"></script>

The Vault Agent handles token renewal and secret re-injection when leases approach expiration. Your application reads credentials from the filesystem rather than environment variables, which means they update without a pod restart when dynamic credentials are rotated.

## Audit Logging

None of this security work matters if you can't answer "who accessed what credential, and when?" Vault's audit devices write a structured log of every API request and response.

<script src="https://gist.github.com/mohashari/1b27128d30818655a40f0c7747dbc165.js?file=snippet-9.sh"></script>

Vault hashes sensitive values in audit logs by default using HMAC-SHA256, so the raw secret never appears in plaintext. You can still verify whether a specific value was accessed by computing the HMAC with `vault audit hash` and comparing against the log.

## Wrapping Up

A mature secrets management posture built on Vault eliminates three of the most common production security failures: static credentials that never rotate, certificates that expire unexpectedly, and secrets scattered across environment variables and config files with no audit trail. The patterns here — dynamic database credentials with short TTLs, on-demand PKI, Kubernetes service account auth, and structured audit logging — compose into a system where the default behavior is secure rather than the exception. Start with the database secrets engine for your highest-risk credentials, wire up the Kubernetes auth method for your most-deployed workload, and enable audit logging before you go to production. From there, every additional secrets engine you onboard makes your infrastructure incrementally more defensible.