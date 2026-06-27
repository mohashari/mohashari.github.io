---
layout: post
title: "Zero-Trust Database Access: Implementing HashiCorp Vault Dynamic Credentials for PostgreSQL in Kubernetes"
date: 2026-06-27 08:00:00 +0700
tags: [kubernetes, postgresql, vault, devsecops]
description: "Learn how to implement HashiCorp Vault dynamic credentials for PostgreSQL in Kubernetes to eliminate static database secrets in production."
image: "https://picsum.photos/seed/93/1080/720"
thumbnail: "https://picsum.photos/seed/93/400/300"
---

In production, static database credentials are a ticking security debt. Consider a standard microservice deployment: 30 pod replicas running in a Kubernetes cluster, all sharing a single connection string like `postgresql://app_user:SuperStaticPassword123@postgres-prod:5432/orders` injected via a Kubernetes Secret. If a single pod is compromised—whether through a remote code execution (RCE) vulnerability, an unpatched third-party dependency, or an SSRF exploit—the attacker immediately gains persistent, unrestricted access to the database. Even worse, rotating these credentials requires a coordinated redeployment of the entire service, a manual and feared operational task that teams routinely postpone, leaving the system vulnerable indefinitely. If your database credentials have an infinite lifetime, your system does not implement zero-trust.

To establish a zero-trust architecture, database credentials must be ephemeral, unique per client instance, and automatically rotated. HashiCorp Vault’s PostgreSQL Secrets Engine, integrated with the Vault Agent Sidecar Injector on Kubernetes, solves this cleanly. In this architecture, every pod replica obtains a unique, temporary database username and password that automatically expires. If a pod is compromised, the blast radius is restricted to a single, short-lived credential that Vault will automatically revoke.

## The Dynamic Secrets Lifecycle

Before configuration, you must understand how the identity token flows through the stack to generate a credential:

1. **Identity Assertion:** The application pod starts in Kubernetes under a designated `ServiceAccount`.
2. **Authentication:** The Vault Agent (running as an Init Container) takes the local ServiceAccount token and sends it to Vault's Kubernetes Auth endpoint.
3. **Verification:** Vault queries the Kubernetes API server to verify the token's signature, namespace, and service account name. If valid, Vault returns a client token scoped to a specific Vault policy.
4. **Generation:** The Vault Agent uses this token to request temporary PostgreSQL credentials from the Database Secrets Engine path.
5. **Execution:** Vault logs into the PostgreSQL instance as an administrator, executes a DDL script to create a new database role with a randomized username (e.g., `v-kubernetes-app-read-w-abc123xyz`), generates a strong password, and sets a validity timestamp.
6. **Delivery:** Vault returns these credentials to the Vault Agent, which writes them to a shared memory volume (`tmpfs`) mounted inside the pod.
7. **Consumption & Renewal:** The application container boots, reads the credentials from the shared volume, and opens its connection pool. Meanwhile, the Vault Agent sidecar container runs continuously to renew the lease and rewrite the secret file before it expires.

## Step 1: Bootstrapping the Vault PostgreSQL Secrets Engine

The first step is enabling the database secrets engine in Vault and configuring the connection details to the target PostgreSQL cluster. In production, always ensure that Vault communicates with PostgreSQL over SSL/TLS (`sslmode=verify-full` or `sslmode=require`) to prevent credential sniffing on the internal network.

<script src="https://gist.github.com/mohashari/53e8eb59441a73e1adf7094bddb08699.js?file=snippet-1.sh"></script>

The templated `{{username}}` and `{{password}}` parameters in the `connection_url` are placeholders. Vault uses them to rotate its own administrative password, ensuring that even the credentials Vault uses to manage the database are not static.

## Step 2: Defining PostgreSQL Roles with the Group Inheritance Pattern

A common failure mode when configuring Vault's PostgreSQL engine is executing direct, inline `GRANT` statements inside the creation SQL template. Configuring templates like `GRANT SELECT, INSERT ON ALL TABLES...` for every dynamically generated user causes severe catalog locking issues in PostgreSQL at scale. Every execution of a `GRANT` statement modifies system catalog tables (such as `pg_auth_members` and `pg_class`), which can lead to exclusive lock contention and query degradation under high traffic or rapid pod scaling.

To prevent this, implement the **Group Role Inheritance Pattern**:

1. Pre-create a static, login-disabled group role in PostgreSQL with the required schema privileges.
2. Configure Vault to simply create a dynamic user, grant it membership in that static group role, and set the default search path.

First, execute the following SQL migration on your PostgreSQL instance:

```sql
CREATE ROLE app_rw_group;
GRANT USAGE ON SCHEMA public TO app_rw_group;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO app_rw_group;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO app_rw_group;
```

Now, configure Vault with the dynamic credential SQL template referencing the pre-existing group role:

<script src="https://gist.github.com/mohashari/53e8eb59441a73e1adf7094bddb08699.js?file=snippet-2.sql"></script>

The dynamically generated user immediately inherits all permission sets of the `app_rw_group`. The `VALID UNTIL` clause acts as a second line of defense: even if Vault fails to delete the user (for instance, during a network partition or Vault outage), PostgreSQL itself will reject any login attempts using these credentials after the validity window expires.

## Step 3: Configuring Kubernetes-to-Vault Authentication

Vault must trust the Kubernetes cluster to authenticate workloads. Configure the Kubernetes auth method and map the application’s Kubernetes identity to a Vault policy.

<script src="https://gist.github.com/mohashari/53e8eb59441a73e1adf7094bddb08699.js?file=snippet-3.sh"></script>

## Step 4: Injecting Credentials via Vault Agent Sidecar

To avoid adding heavy SDK dependencies to your application (which complicates local development), use the Vault Agent Sidecar Injector. By annotating the Kubernetes Pod template, the injector automatically deploys an init container to fetch the initial credentials and a sidecar container to keep them renewed.

<script src="https://gist.github.com/mohashari/53e8eb59441a73e1adf7094bddb08699.js?file=snippet-4.yaml"></script>

Using `emptyDir: { medium: Memory }` ensures the dynamic credentials are saved to a `tmpfs` volume, meaning the secrets are never written to the physical node disk.

## Step 5: Implementing Dynamic Hot-Reloading in the Application

Since Vault dynamic credentials are short-lived, the credentials file will be overwritten by the Vault Agent sidecar periodically (e.g., every 45 minutes for a 1-hour TTL). 

A common anti-pattern is reading the credentials file only once during the application boot phase. Once the initial 1-hour lease expires, Vault will drop the database role, and all subsequent database queries will fail with `FATAL: password authentication failed for user ...`.

The application must watch this file for changes, construct a new connection pool, validate it, swap the active connection pool pointer thread-safely, and gracefully drain the old pool.

Here is a robust, production-ready Go implementation:

<script src="https://gist.github.com/mohashari/53e8eb59441a73e1adf7094bddb08699.js?file=snippet-5.go"></script>

## Production Failure Modes & Mitigation Strategies

Operating dynamic credentials at scale exposes architectural edge cases that standard connection setups do not encounter.

### 1. Database Connection Limit Exhaustion

In standard configurations, a microservice with 50 replicas sharing static credentials will open up to `50 * MaxOpenConns` database connections. With dynamic credentials, every single pod replica generates a *unique database user*. 

PostgreSQL allocates a process for each connection. If you have 10 microservices, each with 30 replicas, scaling up during a traffic spike could generate 300 unique database users. If these users attempt to open maximum connections, you will easily hit the PostgreSQL `max_connections` limit (which defaults to 100 and is rarely configured above 500-1000 without hitting OS-level process bottlenecks).

**Mitigation:** Keep your `MaxOpenConns` small in application code. Scale your application pods horizontally but restrict database pool sizes tightly.

### 2. Connection Pooling and pgBouncer Compatibility

Most production PostgreSQL topologies place pgBouncer in front of the database to multiplex connection pools. pgBouncer operates in transaction or statement mode and relies on a static user mapping file (typically `userlist.txt`). 

When Vault generates a random database user name (e.g., `v-kubernetes-app-read-w-abc123xyz`), pgBouncer rejects the connection because the user does not exist in `userlist.txt`.

**Mitigation:**
Configure pgBouncer to authenticate clients using PostgreSQL itself via the `auth_query` pattern. 
1. Create a helper function in a restricted schema (e.g., `auth`) inside the database that checks credentials against `pg_shadow`.
2. Configure pgBouncer with `auth_type = md5` and `auth_query = "SELECT * FROM auth.get_user_auth($1)"`.

This allows pgBouncer to query PostgreSQL directly to verify the dynamic users generated by Vault, eliminating the need to sync a static text file.

### 3. Handling Vault Outages and Resiliency

If Vault goes down or network connectivity between your Kubernetes nodes and Vault is severed:
- A pod restart will fail because the init container cannot fetch the initial database credentials, causing the pod to enter a `CrashLoopBackOff` state.
- Existing pods will fail to rotate credentials when their lease expires, and the database will revoke access.

**Mitigation:**
- **Increase Lease TTLs:** Instead of a 1-hour TTL, configure a default TTL of 12 hours and a maximum TTL of 24 hours. This gives your infrastructure team a wide window to resolve Vault outages without impacting database traffic.
- **Enable Vault Agent Caching:** Use Vault Agent's local caching mechanism by adding the annotation `vault.hashicorp.com/agent-cache-enable: "true"`. This allows the agent to serve cached leases and perform back-off retries during network issues.

### 4. PostgreSQL Catalog Bloat & Orphaned Roles

Vault is responsible for cleaning up expired database users. It issues `DROP ROLE` statements when leases expire. However, if Vault is restarted abruptly, or if the connection between Vault and PostgreSQL is severed, Vault might lose track of active leases. When this happens, orphaned database roles will accumulate in PostgreSQL. Over time, having thousands of roles degrades the performance of permission checks and system catalog queries.

**Mitigation:**
Implement an automated cron job in your database cluster to clean up expired roles. Since we appended the `VALID UNTIL` clause to the creation statement, we can query `pg_roles` to find users whose validity has expired and drop them.

<script src="https://gist.github.com/mohashari/53e8eb59441a73e1adf7094bddb08699.js?file=snippet-6.sql"></script>

## Conclusion

Implementing HashiCorp Vault dynamic credentials removes a massive security vulnerability in your Kubernetes cluster: static, long-lived database secrets. By combining Kubernetes authentication, group role inheritance, Vault agent injection, and robust application-side hot-reloading, you achieve a true zero-trust database access model. The operational overhead—such as pgBouncer authentication and database connection management—is a necessary trade-off for a significantly hardened, audit-logged, and automatically rotated credential lifecycle.