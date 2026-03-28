---
layout: post
title: "Secrets Rotation Zero-Downtime: Vault Dynamic Credentials with Lease Renewal Strategies"
date: 2026-03-28 08:00:00 +0700
tags: [vault, devsecops, secrets-management, golang, kubernetes]
description: "How to use HashiCorp Vault dynamic credentials with lease renewal to rotate database secrets without dropping a single connection."
image: "https://picsum.photos/1080/720?random=5941"
thumbnail: "https://picsum.photos/400/300?random=5941"
---

The 3am page is always the same: "Database authentication errors, 503s spiking, on-call is investigating." Root cause, two hours later: a static database password was rotated by the security team, the secret in Kubernetes wasn't updated fast enough, and every pod restarted simultaneously when the new secret finally propagated. That's not a security improvement — it's a self-inflicted outage. The team traded a theoretical breach risk for a guaranteed availability incident.

Dynamic credentials solve this at the architecture level. Instead of a long-lived password you rotate occasionally, Vault issues short-lived credentials on demand — each app instance gets its own username and password, valid for 1 hour, renewable up to 24 hours, then automatically revoked. The rotation problem becomes a lease renewal problem, which is tractable. Here's how to build this properly for production.

## How Vault Dynamic Credentials Actually Work

Vault's database secrets engine creates credentials at request time. When your app calls `vault read database/creds/app-role`, Vault executes a CREATE USER statement against your Postgres or MySQL instance, hands you a fresh username and password, and records a lease with a TTL. When the lease expires — or you revoke it — Vault runs DROP USER.

The critical distinction most teams miss: there are two TTLs. The `default_ttl` (how long before you must renew) and the `max_ttl` (the hard ceiling, no renewals past this point). Your application must renew leases actively before the default TTL expires, and must handle max TTL gracefully by requesting new credentials and replacing its connection pool.

```yaml
# snippet-1
# vault-database-role.yaml - Vault database role configuration
# Applied via: vault write database/roles/app-production @vault-database-role.yaml

db_name: "postgres-production"
creation_statements:
  - |
    CREATE ROLE "{{name}}" WITH
      LOGIN
      PASSWORD '{{password}}'
      VALID UNTIL '{{expiration}}'
      CONNECTION LIMIT 20;
    GRANT CONNECT ON DATABASE appdb TO "{{name}}";
    GRANT USAGE ON SCHEMA public TO "{{name}}";
    GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO "{{name}}";
    GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO "{{name}}";
    ALTER DEFAULT PRIVILEGES IN SCHEMA public
      GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO "{{name}}";
revocation_statements:
  - |
    REASSIGN OWNED BY "{{name}}" TO postgres;
    DROP OWNED BY "{{name}}";
    REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM "{{name}}";
    DROP ROLE IF EXISTS "{{name}}";
default_ttl: "1h"
max_ttl: "24h"
```

The `VALID UNTIL` clause in Postgres is belt-and-suspenders: even if Vault's revocation fails (network partition, Vault outage), the database itself rejects the credentials after expiry. Always set this.

## Building a Lease-Aware Connection Pool in Go

Most database drivers don't understand Vault leases. You need a wrapper that tracks lease expiry, renews proactively, and swaps the connection pool when max TTL forces a credential rotation. This is not optional boilerplate — it's the core of zero-downtime rotation.

<script src="https://gist.github.com/mohashari/78e5c5c4c38f38d80949ba03f504d02f.js?file=snippet-2.go"></script>

The 30-second drain window is the critical detail. When you swap the pool, you can't immediately close the old `*sql.DB` — active transactions will get `sql: database is closed`. The drain window lets in-flight work complete before the old credentials are fully abandoned. Tune this to your p99 query latency plus some headroom.

## Kubernetes Deployment with Vault Agent Injector

For most services, you don't want to manage Vault tokens and lease renewal in application code. The Vault Agent sidecar handles token renewal, template rendering, and secret rotation — your app just reads files.

```yaml
# snippet-3
# deployment.yaml - Vault Agent sidecar injection annotations
apiVersion: apps/v1
kind: Deployment
metadata:
  name: api-service
spec:
  template:
    metadata:
      annotations:
        vault.hashicorp.com/agent-inject: "true"
        vault.hashicorp.com/role: "api-service"
        vault.hashicorp.com/agent-inject-secret-db-creds: "database/creds/api-service-role"
        vault.hashicorp.com/agent-inject-template-db-creds: |
          {{- with secret "database/creds/api-service-role" -}}
          DB_USER={{ .Data.username }}
          DB_PASS={{ .Data.password }}
          DB_DSN=postgres://{{ .Data.username }}:{{ .Data.password }}@db-primary.internal:5432/appdb?sslmode=require
          {{- end }}
        # Agent watches lease expiry and re-renders the file when rotation occurs
        vault.hashicorp.com/agent-inject-command-db-creds: "/usr/local/bin/reload-db-pool"
        # Prevent the pod from starting until secrets are available
        vault.hashicorp.com/agent-init-first: "true"
        # Lease-aware: agent renews 10s before expiry
        vault.hashicorp.com/agent-revoke-on-shutdown: "true"
        vault.hashicorp.com/agent-revoke-grace: "5s"
    spec:
      serviceAccountName: api-service
      containers:
        - name: api
          image: api-service:latest
          env:
            - name: DB_CREDENTIALS_FILE
              value: /vault/secrets/db-creds
          volumeMounts:
            - name: vault-secrets
              mountPath: /vault/secrets
              readOnly: true
```

The `agent-inject-command` annotation is the hook most teams skip. When Vault Agent re-renders the template (because it fetched new credentials), it executes that command inside your container. That command should trigger a graceful pool reload — not a process restart. A process restart means downtime. A pool reload means zero-downtime.

## The Reload Signal Pattern

Your application needs to respond to file changes without restarting. The canonical pattern is a SIGHUP handler or a file-watch goroutine:

<script src="https://gist.github.com/mohashari/78e5c5c4c38f38d80949ba03f504d02f.js?file=snippet-4.go"></script>

Watch the **directory**, not the file. Vault Agent writes secrets atomically via rename (write to temp file, rename to target). On Linux, rename operations don't generate `inotify IN_MODIFY` events on the target path — they generate `IN_CREATE` on the directory. Tools like `fsnotify` abstract this, but only if you watch the directory.

## Failure Modes and How to Handle Them

**Vault is unreachable during renewal.** Your app is running fine on valid credentials. Vault goes down. What happens? If you're running Vault in HA mode with Raft, the election takes 5-10 seconds. During that window, lease renewal attempts fail. Your credentials remain valid because Vault's revocation requires Vault to be up — if Vault is down, credentials survive. Implement exponential backoff on renewal failures with a maximum retry window shorter than your `default_ttl`. If the TTL would expire before Vault returns, emit a metric and alert before attempting rotation.

**The database runs out of roles.** Vault creates a new Postgres role per credential issuance. If you have 50 pods and each gets a fresh credential every hour, you'll have up to 50 active roles at steady state — not a problem. But if your pods crash-loop (OOMKilled, bad deploy), each restart creates a new role, and the old ones don't get revoked until their TTL expires. With a 24-hour max TTL and 100 restarts, you hit Postgres's default role limit (typically limited by pg_hba.conf or `max_connections` indirectly). Set `max_ttl` conservatively and enable Vault's lease count limits per role.

**Mid-transaction credential expiry.** This cannot happen if you set `ConnMaxLifetime` to half the lease duration. A connection established at t=0 on a credential valid until t=3600 will be retired at t=1800. By t=1800, you've either renewed the lease (credential is still valid) or already rotated to new credentials. Connections from the old pool drain within the 30-second window. No transaction spans a credential boundary.

## Observability for Lease Health

You need metrics, not just logs. Three signals matter:

<script src="https://gist.github.com/mohashari/78e5c5c4c38f38d80949ba03f504d02f.js?file=snippet-5.go"></script>

Alert on `vault_lease_expiry_seconds < 300` for any role (5 minutes to expiry with no successful renewal is a bad sign). Alert on `vault_pool_rotations_total{reason="renewal_failure"}` increasing — that means your renewal loop is failing silently and relying on the fallback rotation path more than you'd like.

## The Production Checklist

Before shipping dynamic credentials in production, verify these hold:

- `revocation_statements` in your Vault role actually work. Run `vault lease revoke -prefix database/creds/your-role` in staging and confirm roles are cleaned up in `pg_roles`.
- Your connection pool's `ConnMaxLifetime` is strictly less than `default_ttl / 2`. Connections must not outlive their credentials.
- You're watching the directory in fsnotify, not the file.
- Vault Agent's `agent-revoke-on-shutdown: true` is set so pod termination revokes leases immediately rather than waiting for TTL expiry.
- You have a Prometheus alert on lease expiry seconds. Silent renewal failures cause outages 24 hours later when max TTL hits.
- Vault is in HA mode with at least 3 nodes. Single-node Vault turns your dynamic credentials into a single point of failure worse than a static secret.

Dynamic credentials don't eliminate all risk — they shift it from "will someone steal this password" to "will my renewal loop work reliably." That's a much better trade. The failure modes are predictable, monitorable, and recoverable. The old failure mode (stolen static secret) isn't.
```