---
layout: post
title: "Automating Dynamic Secret Rotation for Multi-Region PostgreSQL Clusters Using HashiCorp Vault and cert-manager"
date: 2026-08-09 08:00:00 +0700
tags: [devsecops, postgresql, vault, kubernetes, security]
description: "Implement zero-trust database credentials and automated mTLS rotation across multi-region PostgreSQL clusters without downtime."
image: "https://picsum.photos/seed/7435/1080/720"
thumbnail: "https://picsum.photos/seed/7435/400/300"
---

Hardcoded, long-lived database credentials represent a critical failure point in modern cloud-native architectures. If a single application container is compromised, or an environment file is exposed, attackers gain unrestricted, permanent access to your core data store. Manual rotation schedules are rarely executed, and custom cron-driven rotation scripts are fragile, frequently leading to downtime due to synchronization gaps. In a multi-region deployment, this risk is compounded: standby database nodes require write access to dynamic user metadata (which replicates asynchronously), and TLS certificates securing replication links must be continuously rotated across regional boundaries without breaking the cluster. Transitioning to a zero-trust model requires automating both database credential generation via HashiCorp Vault and cryptographic identity provisioning via cert-manager.

![Automating Dynamic Secret Rotation for Multi-Region PostgreSQL Clusters Using HashiCorp Vault and cert-manager Diagram](/images/diagrams/automating-dynamic-secret-rotation-multi-region-postgresql-clusters-vault-cert-manager.svg)

## The Multi-Region Topology and TLS Foundation

To establish a resilient, zero-trust infrastructure, we deploy a multi-region architecture spanning two main regions: Region A (the primary active site, `us-east-1`) and Region B (the hot-standby disaster recovery site, `us-west-2`). 

Each region runs an independent Kubernetes cluster. HashiCorp Vault is deployed in both regions; Region A hosts the active Vault Primary cluster, while Region B runs a Vault Performance Replica. This replica handles local read requests and token lookups, minimizing cross-region latency, but forwards write operations (such as secret generation) back to the Primary Vault.

Securing the communication channels between all nodes is critical. PostgreSQL streaming replication and client-to-database traffic must run over Mutual TLS (mTLS). To achieve this without manual intervention, we utilize `cert-manager` in each Kubernetes cluster, integrated directly with Vault's PKI (Public Key Infrastructure) secrets engine. 

Vault acts as the root or intermediate Certificate Authority (CA). By configuring a `ClusterIssuer` in cert-manager that authenticates against Vault's PKI engine, Kubernetes pods can dynamically request short-lived certificates.

The following manifest defines a `ClusterIssuer` pointing to a Vault intermediate CA path:

<script src="https://gist.github.com/mohashari/bf952805784ad5212e4231d16af84e61.js?file=snippet-1.yaml"></script>

With the issuer established, we define the `Certificate` resources for the PostgreSQL servers. Because PostgreSQL uses these certificates for both incoming client traffic and outgoing replication links (where standby nodes act as clients connecting to the primary node), the certificate must be issued with both `server auth` and `client auth` key usages. 

Additionally, the Subject Alternative Names (SANs) must include the headless service DNS records for both regions to ensure host verification passes during a failover.

<script src="https://gist.github.com/mohashari/bf952805784ad5212e4231d16af84e61.js?file=snippet-2.yaml"></script>

By setting `renewBefore` to 15 days, cert-manager automatically contacts Vault to rotate the private key and issue a new certificate before the old one expires. The PostgreSQL pods run a sidecar utility that detects updates to the mounted TLS secret and signals the database engine (`pg_ctl reload`) to reload certificates without terminating active client connections.

## Configuring Vault's Database Secrets Engine

The Vault Database Secrets Engine generates PostgreSQL users dynamically. Rather than sharing a single database role across dozens of application instances, Vault interfaces directly with the PostgreSQL primary instance using administrative credentials. When an application requests database access, Vault runs a configured SQL script to generate a unique, short-lived database user with a random password, inheriting specific permissions.

When designing the SQL templates for dynamic user creation, security engineering best practices dictate that we never grant dynamic users direct table ownership or administrative privileges. Instead, we pre-create static group roles in the database (e.g., `db_readonly`, `db_readwrite`) and configure Vault to grant these group roles to the dynamically generated users.

<script src="https://gist.github.com/mohashari/bf952805784ad5212e4231d16af84e61.js?file=snippet-3.sql"></script>

Configuring timeouts on the dynamic user level is a critical defense-in-depth measure. Setting `statement_timeout` to 30 seconds and `lock_timeout` to 10 seconds prevents a compromised or poorly written application query from locking tables indefinitely and starving the rest of the microservices.

To manage this configuration declaratively, we utilize Terraform. We define the database mount, specify the connection string (with `sslmode=verify-full` referencing the CAs managed by cert-manager), and register the database role.

<script src="https://gist.github.com/mohashari/bf952805784ad5212e4231d16af84e61.js?file=snippet-4.hcl"></script>

## Solving the Replication Lag Race Condition

In a multi-region PostgreSQL cluster, writes are executed exclusively on the Primary node in Region A. Secondary standby nodes in Region B operate as read-only replicas, streaming transactions asynchronously via Write-Ahead Log (WAL) replication.

This replication delay introduces a significant race condition when using dynamic secrets. When an application pod in Region B requests database credentials from the local Vault Performance Replica:

1. The Vault Performance Replica in Region B proxies the write request to the Vault Primary in Region A.
2. The Vault Primary connects to the PostgreSQL Primary in Region A and executes the `CREATE ROLE` statement.
3. Vault returns the generated credentials to the application pod in Region B.
4. The application pod in Region B immediately attempts to establish a connection to its local PostgreSQL Standby.

If this sequence completes faster than the physical network transport and replay of the WAL record to Region B's standby node (typically 20ms to 2s, depending on network load and transaction volume), the standby node will reject the connection with a `Password authentication failed` error. The dynamic user does not exist on the replica yet.

To prevent application crashes and bootstrap errors during rotation, we must implement a resilient connection manager. The client-side database driver wrapper must detect authentication failures, apply an exponential backoff retry loop to allow the replication lag to clear, and execute a zero-downtime connection pool swap when credentials rotate.

The following Go implementation manages this lifecycle safely:

<script src="https://gist.github.com/mohashari/bf952805784ad5212e4231d16af84e61.js?file=snippet-5.go"></script>

## Vault Agent Auto-Auth and Client-Side Credential Lifecycle

Applications running in Kubernetes do not query Vault directly for database users. Instead, we use the Vault Agent Sidecar Injector. The Vault Agent sidecar runs alongside the application container, authenticates to Vault using the local pod's Kubernetes Service Account, and fetches the database credentials.

Vault Agent writes these credentials to a shared memory volume (`/vault/secrets/`) in JSON format. The Go application reads from this volume and watches for updates. 

Crucially, the Vault Agent is configured to auto-renew the dynamic leases. If the lease cannot be renewed (e.g., it hits its maximum TTL), the Vault Agent requests a new dynamic credential from Vault, writes it to the shared volume, and the application's credential watcher triggers the `rotate` procedure defined in snippet 5.

<script src="https://gist.github.com/mohashari/bf952805784ad5212e4231d16af84e61.js?file=snippet-6.yaml"></script>

Setting `vault.hashicorp.com/agent-cache-enable: "true"` is critical. Without local Vault Agent caching, if a pod crashes or rescales rapidly, the incoming surge of login queries can saturate Vault's token generation limits, cascading failure throughout the infrastructure.

## Production Hardening and Edge Cases

Running dynamic secrets at scale requires mitigating three major failure modes: session exhaustion, WAN partitions, and role deletion blockages.

### 1. Database Connection Limit Exhaustion

Each dynamic credential created by Vault is a unique database role. In standard PostgreSQL configurations, `max_connections` is a finite resource (often set between 500 and 2000). If you scale your deployment to 50 application pods, and each pod spawns a connection pool with a max size of 40, you can quickly exhaust the connections.

Worse, when Vault rotates credentials, the old user continues to exist for a grace period (e.g., 30 seconds to 1 hour). If the application pools do not shut down old connections immediately upon rotation, the active connections will double.

**Hardening strategy:**
*   Enforce a connection limit inside the Vault role creation template using PostgreSQL `CONNECTION LIMIT`:
    ```sql
    ALTER ROLE "{{name}}" CONNECTION LIMIT 45;
    ```
*   Scale up PostgreSQL cluster capacity and deploy an intermediate connection pooler like PgBouncer *between* the application and the database. 
*   Keep the Vault lease TTLs reasonable (e.g., 1 to 4 hours). Setting TTLs to 5 minutes causes excessive database catalog bloat and puts unnecessary CPU overhead on PostgreSQL from constantly executing `CREATE ROLE` and `DROP ROLE`.

### 2. WAN Partition and Vault Split-Brain

In the event of a total network partition between Region A and Region B, the Vault Performance Replica in Region B becomes isolated. Because it cannot forward user creation requests to the Vault Primary in Region A, all requests for new database credentials in Region B will fail.

If a pod in Region B restarts during this partition, it will fail to start up because Vault Agent cannot retrieve credentials.

**Hardening strategy:**
*   Enable Vault Agent Caching with client-side persistent storage or long token leases.
*   Define a fallback static emergency user in the standby database. This emergency user should only be accessible if Vault is unreachable.
*   Configure the connection logic to fallback to this restricted, static, read-only emergency role after a prolonged timeout, triggering high-severity alerts in your monitoring system (e.g., Datadog or Prometheus/PagerDuty).

### 3. PostgreSQL Role Revocation Failures

When a Vault lease expires, Vault attempts to clean up the database catalog by dropping the dynamically created role:
```sql
DROP ROLE "v-token-billing-service-xxxx";
```
However, if the application created any temporary tables, materialized views, or if the role owns any schemas/objects inside the database, this statement will fail with:
```
ERROR: role "v-token-billing-service-xxxx" cannot be dropped because some objects depend on it
```
If this occurs, Vault enters a failed state, continually retrying the revocation, and leaving orphaned roles in the system, which causes database catalog degradation.

**Hardening strategy:**
Ensure that your database creation template forces the dynamic user to only write to tables owned by the static group role. Define default privileges on your database schemas so that any object created by a dynamic user automatically has its ownership transferred or shared with the parent group role:

<script src="https://gist.github.com/mohashari/bf952805784ad5212e4231d16af84e61.js?file=snippet-7.sql"></script>

Additionally, customize Vault's revocation statements to reassign any rogue objects owned by the dynamic user to the static administrative user before executing the drop:

<script src="https://gist.github.com/mohashari/bf952805784ad5212e4231d16af84e61.js?file=snippet-8.sql"></script>

By ensuring schema designs isolate object ownership and that application pools handle credentials as ephemeral configuration inputs rather than static constants, you construct a database environment that rotates automatically, remains highly available across regions, and maintains a zero-trust posture.