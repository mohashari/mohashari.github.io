---
layout: post
title: "Dynamic Secret Rotation for Kafka Clients using Vault and Kubernetes CSI"
date: 2026-07-09 08:00:00 +0700
category: devsecops
tags: [vault, kafka, kubernetes, devsecops]
description: "Implement zero-downtime dynamic mTLS cert rotation for Kafka clients in Kubernetes using HashiCorp Vault and the Secrets Store CSI driver."
image: "https://picsum.photos/seed/5248/1080/720"
thumbnail: "https://picsum.photos/seed/5248/400/300"
---

Imagine this: It is 2:00 AM on a Tuesday, and your security operations center detects a credential leak in a developer's scratch repository. The leak contains the username and password for a critical Apache Kafka client group. To mitigate the breach, your platform team generates new SASL/SCRAM credentials, updates the Kubernetes Secret objects, and triggers a rolling restart of your 100-replica payments service. What follows is a self-inflicted denial of service. The rolling restart forces consumer groups to rebalance repeatedly. Every rebalance halts message processing for several seconds, causing a massive backlog of uncommitted messages. The brokers are slammed with connection retries, CPU utilization spikes to 95%, and downstream HTTP APIs begin to time out. The entire rotation process takes 45 minutes of high-stress debugging, and a minor credential leak has turned into a major production outage.

Static credentials for Kafka clients are a systemic operational risk. Standard security frameworks like PCI-DSS and SOC2 demand regular rotation of secrets, yet manual rotation policies are rarely followed because engineers dread the associated downtime and instability. Hardcoded configurations, outdated Kubernetes Secrets stored in etcd, and the friction of service restarts make credential rotation a liability. To achieve true continuous delivery and robust security, we must decouple credential rotation from application deployment. This means implementing dynamic, zero-downtime credential rotation, where secrets have short lifespans, are rotated automatically by infrastructure controllers, and are hot-reloaded by the Kafka client without process restarts or consumer group rebalances.

## Architecture of the Vault-CSI-Kafka Loop

To construct a zero-downtime rotation loop in Kubernetes, we leverage three production-grade components: HashiCorp Vault, the Kubernetes Secrets Store CSI (Container Storage Interface) Driver, and a hot-reloading Kafka client. 

Vault serves as the cryptographic source of truth. While Vault can store static keys, its real power lies in dynamic engines. For Kafka, we avoid SASL/SCRAM because updating usernames and passwords requires mutating broker metadata or external database state in real-time, which introduces latency and state synchronization issues. Instead, we use Vault’s PKI (Public Key Infrastructure) secrets engine to generate short-lived Mutual TLS (mTLS) client certificates. mTLS is the enterprise standard for Kafka authentication; it is highly secure, cryptographically proven, and supported natively by Kafka brokers.

The Secrets Store CSI Driver acts as the delivery mechanism. Rather than injecting secrets as environment variables (which cannot be updated without restarting the container) or using Vault Sidecar Agents (which add resource overhead and complicate container lifecycles), the CSI driver mounts secrets directly into the pod’s filesystem. By configuring the CSI driver with auto-rotation enabled, the driver polls Vault at a set interval, fetches new certificates before the old ones expire, and writes them atomically to the pod's filesystem using an in-memory `tmpfs` volume.

Finally, the Kafka client application must monitor these files. When a new certificate and private key are written to the disk, the client must hot-swap them. This prevents existing connections from dropping abruptly and ensures that new TCP connections created during broker handovers or socket reconnections use the fresh credentials.

```
[Vault Engine] 
     │ (Dynamically generates short-lived credentials)
     ▼
[Vault CSI Provider] 
     │ (Polls Vault & writes to Pod's tmpfs mount)
     ▼
[Pod File System (/var/run/secrets/kafka)]
     │ (fsnotify / File Watcher)
     ▼
[Kafka Client App] 
     │ (Hot-reloads configuration)
     ▼
[Kafka Broker] (Authenticates new connections with rotated credentials)
```

## Step 1: Configuring HashiCorp Vault for Dynamic mTLS

We begin by configuring Vault to act as our internal CA for Kafka clients. We will enable the PKI secrets engine, generate a root CA, and configure a role that restricts the domains and common names that clients can request.

Here is the setup script to initialize the PKI secrets engine:

<script src="https://gist.github.com/mohashari/0be5eda5c44e045a117ddad526595c79.js?file=snippet-1.sh"></script>

Next, we must configure Kubernetes authentication in Vault. This allows the CSI driver running on our Kubernetes nodes to authenticate on behalf of client pods using their service account tokens.

<script src="https://gist.github.com/mohashari/0be5eda5c44e045a117ddad526595c79.js?file=snippet-2.sh"></script>

This configuration limits access tightly: only pods executing under the `kafka-client-sa` service account in the `production` namespace can request certificates, and the issued certificates are restricted to the configured domain and role policies.

## Step 2: Implementing the Secrets Store CSI Driver Configuration

The Secrets Store CSI Driver uses a `SecretProviderClass` CRD to map Vault paths to files mounted inside the pod. However, configuring this for PKI issue endpoints introduces a critical failure mode: the **mismatched key pair trap**.

If you specify `client.crt` and `client.key` as separate objects in the CSI configuration, the Vault CSI provider will make two distinct HTTP POST requests to Vault's `/issue` endpoint. Because each call to `/issue` generates a brand-new certificate and key pair, your application will mount the certificate from the first call and the private key from the second call. The resulting TLS handshake will fail immediately because the private key does not match the public key in the certificate.

To prevent this, we must configure the CSI driver to make a single call to Vault and extract both the certificate, private key, and issuing CA from that single JSON response. We achieve this using the `fileMap` feature of the Vault CSI provider.

<script src="https://gist.github.com/mohashari/0be5eda5c44e045a117ddad526595c79.js?file=snippet-3.yaml"></script>

With this configuration, the CSI driver calls the `/issue` endpoint once, receives the JSON response containing the matching cert and key, and writes them to separate files (`client.crt`, `client.key`, and `ca.crt`) in the mounted directory.

To ensure the CSI driver automatically rotates these files, you must configure the CSI driver daemonset with rotation enabled. Ensure the following flags are set in your CSI driver deployment:
```
--enable-secret-rotation=true
--rotation-poll-interval=2m
```
A poll interval of 2 minutes is standard. It ensures that any rotation on Vault's side is reflected on the client container's disk within 120 seconds.

## Step 3: Mounting Secrets in the Application Pod

With the `SecretProviderClass` in place, we mount the CSI volume inside our application deployment. We mount it as a read-only volume on a dedicated path.

<script src="https://gist.github.com/mohashari/0be5eda5c44e045a117ddad526595c79.js?file=snippet-4.yaml"></script>

It is critical to note how Kubernetes handles updates to mounted volumes. The CSI driver does not overwrite the mounted files directly. Instead, Kubernetes uses a symlink structure. The files `/var/run/secrets/kafka/client.crt` and `/var/run/secrets/kafka/client.key` are symlinks pointing to a hidden `..data` directory, which in turn points to a timestamped folder (e.g., `..2026_07_09_12_00_00.12345`). When the CSI driver rotates the certificates, it creates a new timestamped folder and updates the `..data` symlink atomically. 

This brings us to a major failure mode: **naive file watchers will fail**. If your application uses an `inotify` or `fsnotify` library to watch the file `/var/run/secrets/kafka/client.crt` directly, it will not receive any events when the certificate rotates. This is because the file itself is never modified; only the directory symlink target is updated. To detect changes, your code must watch the parent directory (`/var/run/secrets/kafka`) or resolve and watch the symlink target.

## Step 4: Client-Side Auto-Reloading Logic in Go

We will implement the consumer-side reloading logic in Go using the `github.com/twmb/franz-go` library, which is the most performant, production-ready Go client for Apache Kafka. 

To achieve zero-downtime rotation, we exploit Go's standard library `crypto/tls` capability. The `tls.Config` struct accepts a callback function named `GetClientCertificate`. This callback is executed by the Go runtime every time the Kafka client establishes a new TCP connection to a broker. Instead of hardcoding the certificate during client initialization, we can dynamically return the latest loaded certificate from memory.

We also write a directory watcher using `github.com/fsnotify/fsnotify` that watches `/var/run/secrets/kafka` for symlink writes and reloads the certificates into memory. We include a mutex to ensure thread-safe updates to the certificate pointers, and a rate-limiting cooldown to prevent multiple quick writes from overloading the heap.

<script src="https://gist.github.com/mohashari/0be5eda5c44e045a117ddad526595c79.js?file=snippet-5.go"></script>

Now, we initialize the `franz-go` client using this provider. Since the `tls.Config` wraps the dynamic certificate provider, the client automatically picks up the updated certificates on any reconnection attempt.

<script src="https://gist.github.com/mohashari/0be5eda5c44e045a117ddad526595c79.js?file=snippet-6.go"></script>

With this code, if a Kafka broker drops a connection (due to a rolling restart or network hiccup), the `franz-go` client reconnects. During the reconnection dial, it executes `GetClientCertificate`, which returns the newly rotated certificate written by the CSI driver. Zero client restarts, zero consumer group rebalances.

## Step 5: Client-Side Auto-Reloading Logic in Java

For backend systems running on the JVM, the Apache Kafka Java client poses a challenge. By default, configuration parameters like `ssl.keystore.location` are read once during the initialization of the `KafkaConsumer` or `KafkaProducer`. If the files on disk change, the client does not reload them, eventually leading to authentication failures when the old certificate expires.

To resolve this without restarting the application, we implement Kafka's `org.apache.kafka.common.security.auth.SslEngineFactory` interface. The Kafka client calls this factory every time it creates an `SSLEngine` for a new connection. 

Additionally, JVM clients typically require PKCS12 keystores. Rather than running a sidecar container with `openssl` and `keytool` to convert Vault's PEM output into a disk-based PKCS12 file (which creates disk write vulnerabilities and synchronization lag), we can read the raw PEM certificate and PKCS8 private key directly from the CSI mount and assemble an in-memory `KeyStore` in JVM heap memory.

Here is the in-memory PEM-to-KeyStore parser:

<script src="https://gist.github.com/mohashari/0be5eda5c44e045a117ddad526595c79.js?file=snippet-8.txt"></script>

Now, we write the custom `SslEngineFactory` that uses this reader, checks the file modification timestamps, and reloads the SSL context dynamically when a change is detected.

<script src="https://gist.github.com/mohashari/0be5eda5c44e045a117ddad526595c79.js?file=snippet-7.txt"></script>

To use this factory, pass the following configuration properties to your Java Kafka client:
```properties
security.protocol=SSL
ssl.engine.factory.class=com.production.kafka.DynamicSslEngineFactory
ssl.keystore.location=/var/run/secrets/kafka/client.crt
ssl.keystore.password=temporary-password
ssl.truststore.location=/var/run/secrets/kafka/ca.crt
ssl.truststore.password=temporary-password
```

Because we parse the PEM files directly inside the JVM heap, we map `ssl.keystore.location` to the certificate file and `ssl.truststore.location` to the CA certificate file. The password parameters are required by the Kafka configuration parser, but they are only used for the in-memory keystore initialization.

## Production Failure Modes and Resiliency

Designing for dynamic rotation means designing for failure. If your rotation loop is not resilient, a minor infrastructure issue can cause cascading outages. Ensure you guard against the following production failure modes:

### 1. Vault Outage During Rotation
If HashiCorp Vault is down when the CSI driver attempts to poll for a new certificate, the CSI driver will fail to rotate the files on disk. If your certificate TTL is too short, your clients will be locked out as soon as the current certificate expires.
* **Mitigation**: Implement a significant overlap window. Set your certificate TTL to 24 hours or 48 hours, but configure the CSI driver to rotate them every 2 hours (or set Vault's lease renewals frequently). If Vault goes down, your client applications have a buffer of 22 to 46 hours to run on the cached certificates while your platform team restores Vault.

### 2. Clock Skew Between Cluster Nodes
Kubernetes nodes, Vault servers, and Kafka brokers may have minor clock drifts (typically a few milliseconds to seconds). If Vault issues a certificate with a `NotBefore` timestamp set to the current time on the Vault server, and the Kafka broker's clock is 5 seconds behind, the broker will reject the certificate as "not yet valid" during the TLS handshake.
* **Mitigation**: Configure Vault to backdate the certificate's activation time. When defining the PKI role in Vault, use the `not_before_duration` parameter to backdate the certificate start time by 30 or 60 seconds (e.g., `vault write kafka-pki/roles/kafka-client ... not_before_duration=60s`).

### 3. Client Connection Pool Starvation
When certificates rotate, existing long-lived TCP connections between the Kafka client and the brokers remain authenticated using the old certificate. If you have extremely long-lived connections, some clients might run on expired certificates for days. While this is secure for existing sockets, it creates security discrepancies. Conversely, if you force-close all connections immediately upon rotation, you will trigger a massive reconnect storm.
* **Mitigation**: Let connections age out naturally. Configure Kafka broker connection limits and set maximum connection lifetimes (e.g., `connections.max.idle.ms` or `max.connection.creation.rate` at the broker level). Franz-go and Java clients handle socket reconnections gracefully over time, spreading out the TLS handshakes.

### 4. The Symlink Read Race Condition
Because the CSI driver updates symlinks, there is a microsecond-level window where the `client.crt` symlink has been updated to point to the new directory, but `client.key` has not yet resolved, or the application reads them mid-update.
* **Mitigation**: Implement a retry/recovery loop when parsing the key pair. In both our Go and Java code, any parsing failure (such as `tls.X509KeyPair` returning an error due to a mismatch during a read race) should not crash the application. Instead, log a warning, wait 500 milliseconds, and attempt to reload the files again. During this brief retry window, continue using the old, valid certificate cache.

By combining HashiCorp Vault's PKI engine, the Kubernetes Secrets Store CSI driver, and filesystem-aware clients, you eliminate the operational dread of secret rotation. Your credentials rotate silently in the background, your security posture is hardened, and your message pipelines keep running without a single dropped packet.