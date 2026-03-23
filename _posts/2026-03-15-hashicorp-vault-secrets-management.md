---
layout: post
title: "HashiCorp Vault: Secrets Management for Production Systems"
date: 2026-03-15 07:00:00 +0700
tags: [security, vault, secrets, devops, backend]
description: "Integrate HashiCorp Vault into your backend stack to dynamically manage secrets, rotate credentials, and eliminate hardcoded keys."
---

Every production system has a secrets problem. Database passwords buried in `.env` files, API keys committed to git history "just temporarily," service credentials copy-pasted into CI/CD dashboards — the attack surface grows quietly until a breach makes it impossible to ignore. HashiCorp Vault is the industry-standard answer: a unified secrets management system that centralizes credential storage, enforces access policies, and — critically — rotates secrets dynamically so that compromised credentials expire before they can be weaponized. This post walks through integrating Vault into a real backend stack, from bootstrapping to dynamic database credentials to application-side consumption in Go.

## Starting Vault in Development

Before touching production, spin up a dev server locally. Dev mode starts Vault unsealed with a root token, which is perfect for experimentation but never for production.

The following shell session initializes Vault, sets the required environment variable, and enables the KV secrets engine at the `secret/` path:

<script src="https://gist.github.com/mohashari/1b3535e93040e28aba679c2c8a00ee59.js?file=snippet.sh"></script>

`★ Insight ─────────────────────────────────────`
KV v2 (the default in modern Vault) stores versioned secrets — every `put` creates a new version, and you can roll back to any previous version with `vault kv rollback`. KV v1 lacks this, which is one reason v2 is preferred for production workloads where auditability matters.
`─────────────────────────────────────────────────`

## Defining Access Policies

Vault's authorization model is policy-based. A policy is a HCL document that grants capabilities (`read`, `write`, `list`, `delete`, `update`) on specific secret paths. The principle of least privilege means each service gets exactly the capabilities it needs — nothing more.

This policy grants a backend service read-only access to its own secrets and the ability to renew its own token lease:

<script src="https://gist.github.com/mohashari/1b3535e93040e28aba679c2c8a00ee59.js?file=snippet-2.hcl"></script>

Apply the policy with:

<script src="https://gist.github.com/mohashari/1b3535e93040e28aba679c2c8a00ee59.js?file=snippet-3.sh"></script>

## Dynamic Database Credentials

Static credentials are a liability — the same password valid today is valid six months after a breach. Vault's database secrets engine issues short-lived, dynamically generated credentials. When a service requests credentials, Vault creates a real PostgreSQL user, returns the credentials, and automatically revokes them when the lease expires.

<script src="https://gist.github.com/mohashari/1b3535e93040e28aba679c2c8a00ee59.js?file=snippet-4.sql"></script>

Now configure Vault's database engine:

<script src="https://gist.github.com/mohashari/1b3535e93040e28aba679c2c8a00ee59.js?file=snippet-5.sh"></script>

`★ Insight ─────────────────────────────────────`
The `VALID UNTIL '{% raw %}{{expiration}}{% endraw %}'` clause in the creation SQL is a defense-in-depth measure. Even if Vault's revocation job fails or is delayed, the credential has a hard expiry baked into PostgreSQL itself. This two-layer expiry is a pattern worth applying broadly — never rely on a single revocation mechanism.
`─────────────────────────────────────────────────`

## Reading Secrets from Go

Application-side Vault integration in Go uses the official `vault/api` client. The pattern is straightforward: authenticate, retrieve credentials, and build your database connection. For production, prefer AppRole auth (shown next) over token auth.

<script src="https://gist.github.com/mohashari/1b3535e93040e28aba679c2c8a00ee59.js?file=snippet-6.go"></script>

## AppRole Authentication for Services

In production, services shouldn't authenticate with static Vault tokens. AppRole authentication issues a `role_id` (semi-public, like a username) and a `secret_id` (short-lived, like a one-time password). The secret ID is injected at deploy time — typically by your orchestrator — and consumed once to obtain a renewable Vault token.

<script src="https://gist.github.com/mohashari/1b3535e93040e28aba679c2c8a00ee59.js?file=snippet-7.go"></script>

The `LifetimeWatcher` handles automatic token renewal in the background — a detail that is easy to miss but critical for long-running services whose Vault token would otherwise expire mid-flight.

## Deploying with Docker

In containerized environments, inject Vault credentials via environment variables at runtime. Never bake `VAULT_TOKEN` or secret IDs into the image.

<script src="https://gist.github.com/mohashari/1b3535e93040e28aba679c2c8a00ee59.js?file=snippet-8.dockerfile"></script>

At runtime, your orchestrator (Kubernetes, Nomad, ECS) populates `VAULT_ROLE_ID` and `VAULT_SECRET_ID` from its own secrets store — typically backed by Vault itself through a narrow bootstrap policy. This is the "secret zero" pattern: the only static credential is the one used to fetch all others, and it's rotated frequently.

---

The shift from static secrets to Vault-managed credentials is not just a security upgrade — it changes the operational posture of your entire system. Breached credentials expire on their own schedule. Audit logs record every secret access. Revoking a compromised service's access is a single `vault token revoke` command rather than a frantic credential rotation across a dozen systems. Start with the KV engine for existing static secrets, layer in dynamic database credentials for your highest-value datastores, and enforce AppRole authentication for every service that talks to Vault. The upfront integration cost is a few days of work; the alternative is a breach postmortem explaining why production passwords were valid for three years.