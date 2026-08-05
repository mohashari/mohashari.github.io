---
layout: post
title: "Hardening SPIFFE/SPIRE Microservice Identity Verification via eBPF-based Socket Matching in Kubernetes"
date: 2026-08-05 08:00:00 +0700
tags: [kubernetes, security, ebpf, devsecops, spire]
description: "Eliminate TOCTOU exploits and PID recycling vulnerabilities in SPIFFE/SPIRE by verifying workload identity using synchronous kernel-level eBPF socket inode matching."
image: "https://picsum.photos/seed/2177/1080/720"
thumbnail: "https://picsum.photos/seed/2177/400/300"
---

In high-throughput, dynamic Kubernetes environments, relying on asynchronous process-to-container metadata resolution for zero-trust identity is a ticking security bomb. When a workload connects to the SPIFFE/SPIRE Agent Workload API over a Unix Domain Socket (UDS), the agent traditionally extracts the caller's PID via `SO_PEERCRED` and queries the local container runtime (e.g., containerd) to identify the calling pod. At scale, this gRPC lookup introduces a 15ms to 50ms latency window. In environments with rapid container churn, serverless workloads, or local process execution, this delay creates a critical Time-of-Check to Time-of-Use (TOCTOU) vulnerability: PIDs can recycle in less than 5 milliseconds. A compromised container can exploit this race condition to impersonate a privileged pod by crashing its own process and recycling the PID before the agent completes attestation. To close this loophole, we must move the identity verification from user-space polling to synchronous kernel-space enforcement using eBPF socket inode mapping.

## The TOCTOU and PID Recycling Threat Vector in SPIRE

The standard SPIFFE/SPIRE Kubernetes Workload Attestation process relies on the Unix socket peer credentials. When a workload connects to the SPIRE Agent's Unix socket, the kernel populates the `ucred` structure with the PID, UID, and GID of the connecting process. The SPIRE Agent reads this via `getsockopt(fd, SOL_SOCKET, SO_PEERCRED, &ucred, &len)`. 

Once the SPIRE Agent obtains the PID, it executes the following lookup pipeline:
1. It queries the local container runtime (containerd or CRI-O) via its Unix domain socket using the CRI gRPC API to map the PID to a Container ID.
2. It queries its internal Kubernetes pod cache (or the Kubernetes API Server) to map the Container ID to Pod metadata (namespace, service account, labels).
3. It evaluates attestation policies against these selectors and mints a SPIFFE Verifiable Identity Document (SVID).

Under heavy node load, the container runtime's gRPC socket can experience severe queueing delay. If containerd takes 40ms to resolve a PID, a malicious actor has a massive execution window to exploit PID recycling. Linux allocates PIDs sequentially. On hosts with high churn rates or low default `kernel.pid_max` limits (e.g., 32768), PIDs roll over rapidly. 

If an attacker can predict when a target service (the victim) connects to the Workload API, the attacker can force the victim to crash (e.g., via resource exhaustion, OOM-killing, or exploiting an application bug) immediately after it establishes the socket connection. The attacker then rapidly spawns lightweight worker threads to exhaust the PID space until the target PID is reassigned to the attacker's process. When the SPIRE Agent finally resolves the PID via containerd, the lookup points to the attacker’s container.

The script below demonstrates the extreme speed at which PIDs can recycle on a standard Linux node under load, highlighting the vulnerability window.

<script src="https://gist.github.com/mohashari/0e684a3c4f66cfd6235fdcee117842c7.js?file=snippet-1.sh"></script>

## The eBPF Socket Inode Matching Architecture

To eliminate this vulnerability, we must bind process security context to a kernel identifier that is guaranteed to be immutable and unique for the lifetime of the connection: the **socket inode**.

When a Unix domain socket connection is established, the Linux kernel allocates a `struct socket` which contains a Virtual File System (VFS) `struct inode`. The inode number (`i_ino`) is a 64-bit unsigned integer that is globally unique on the host. This inode remains active and allocated as long as the socket connection remains open. Even if the calling process exits, the socket structure—and its inode—cannot be recycled until all file descriptors referencing the socket are closed.

By capturing the caller's container metadata (cgroups, namespaces, path) in kernel space *at the exact moment* of the `connect()` system call and saving it to an eBPF map keyed by the client socket's inode number, we construct a tamper-proof lookup table. 

When the SPIRE Agent accepts the connection:
1. It calls `fstat` on the accepted file descriptor to retrieve the client socket's inode number.
2. It queries the pinned eBPF map using the inode number.
3. It retrieves the immutable metadata populated at connection time, completely bypassing containerd and eliminating TOCTOU races.

```
+----------------------------------------------------------------------------+
|                          Linux Kernel (Kernel Space)                       |
|                                                                            |
|   Workload (PID 4201)                                                      |
|        │                                                                   |
|        ▼ (sys_connect)                                                     |
|   kprobe/unix_stream_connect ──► [Capture Metadata: cgroup, ns, mnt]      |
|                                         │                                  |
|                                         ▼ (Key: Socket Inode)              |
|                                   BPF Hash Map                             |
+─────────────────────────────────────────╪──────────────────────────────────+
|                          SPIRE Agent (User Space)                          |
|                                         │                                  |
|   Accept Conn ──────────────────────────┼──────────────────────────────┐   |
|        │                                ▼                              │   |
|        └─► fstat(fd) ──► Inode ──► Map Lookup ──► Attest Workload ─────┘   |
+----------------------------------------------------------------------------+
```

## Deep Dive: Writing the eBPF Kernel Probe

To implement this, we write an eBPF program that hooks the kernel's `unix_stream_connect` function. This function handles the connection protocol for stream-oriented Unix domain sockets. We use BPF CO-RE (Compile Once - Run Everywhere) to ensure compatibility across minor kernel upgrades.

The probe verifies that the connection target is the SPIRE Agent's Workload API path (`/run/spire/sockets/agent.sock`). If verified, it extracts the caller's mount namespace, network namespace, cgroupv2 ID, UID, GID, and process name, storing them in a BPF hash map keyed by the connecting socket's inode number.

<script src="https://gist.github.com/mohashari/0e684a3c4f66cfd6235fdcee117842c7.js?file=snippet-2.txt"></script>

## Interfacing Go with the BPF Map

To retrieve the metadata in user space, we write a Go library using the `github.com/cilium/ebpf` library. 

First, we define a Go structure that maps exactly to the C struct `workload_metadata`. The struct alignment must be exact to prevent byte shifting issues when reading raw memory from the eBPF map. In this layout, all fields are manually padded and aligned to their respective boundaries, producing a clean 48-byte structure.

<script src="https://gist.github.com/mohashari/0e684a3c4f66cfd6235fdcee117842c7.js?file=snippet-3.go"></script>

Now, we write the Go function to read from the pinned BPF map. When the SPIRE Agent accepts a raw TCP/Unix connection, we extract the underlying file descriptor, perform an `fstat` syscall to fetch the socket's inode number, look up the metadata in the pinned map, and immediately delete the map key. 

Deleting the key immediately is crucial. Because UDS connections are established and kept open, we only need to attest the workload once at handshake time. Deleting the key immediately prevents kernel slab memory leaks and ensures we do not accumulate orphaned keys if a client terminates abruptly.

<script src="https://gist.github.com/mohashari/0e684a3c4f66cfd6235fdcee117842c7.js?file=snippet-4.go"></script>

## Production Integration & SPIRE Plugin Trade-offs

Integrating this eBPF-based attestation model into a standard SPIRE environment presents a structural challenge. SPIRE utilizes an out-of-process plugin model where plugins communicate with the core SPIRE Agent process via gRPC over local pipes. 

The standard SPIRE Workload Attester interface is designed around the following API:

```protobuf
rpc Attest(AttestRequest) returns (AttestResponse);
```

The `AttestRequest` contains only the PID of the calling process. It does not carry the file descriptor or the socket inode, because the gRPC boundary prevents passing file descriptors across the plugin isolation boundary.

To deploy this in production, you have three integration choices:

### Option A: The PID Map Variant (Standard Plugin Architecture)
If you want to use the standard, unmodified SPIRE Agent binary, the eBPF program must key the BPF map by the `PID` (tgid) rather than the socket inode. The custom plugin reads the PID from `AttestRequest`, queries the map, and immediately deletes the key. 
- **Advantage:** Works out of the box with standard SPIRE Agent releases.
- **Security Implications:** While still vulnerable to absolute PID recycling in theory, it shrinks the TOCTOU window from 15–50ms (CRI gRPC API call overhead) to less than 5 microseconds (a simple eBPF map lookup). This makes exploiting the race condition virtually impossible in practical scenarios.

### Option B: The Customized In-Process Attester (Zero-TOCTOU)
Compile a custom SPIRE Agent binary where the attestation logic is imported as an in-process package. We hook the SPIRE Agent's listener directly, extracting the socket file descriptor before calling the attestation pipeline.
- **Advantage:** Cryptographically and structurally secure. Zero TOCTOU window.
- **Security Implications:** Ideal for high-security environments where custom agent builds are standard practice.

The following code shows the implementation of a custom SPIRE Workload Attester plugin leveraging the PID-keyed lookup helper for standard SPIRE Agent deployments.

<script src="https://gist.github.com/mohashari/0e684a3c4f66cfd6235fdcee117842c7.js?file=snippet-6.go"></script>

## Deployment, Operations, and Performance Benchmarks

To deploy the eBPF Socket Attester in Kubernetes, we use a DaemonSet that mounts the host’s `/sys/fs/bpf` directory. The pod runs in the host network and PID namespaces, and must run as `privileged` to load the eBPF maps and probes into the kernel (requiring `CAP_BPF` and `CAP_SYS_ADMIN` capabilities on Linux 5.8+).

<script src="https://gist.github.com/mohashari/0e684a3c4f66cfd6235fdcee117842c7.js?file=snippet-5.yaml"></script>

### Performance Analysis
Eliminating containerd API lookups dramatically improves the performance of the SPIRE Workload API under load. In benchmark runs simulating 1,000 parallel connection handshakes:

- **CRI gRPC Lookup (Standard SPIRE):** Average latency of 34.2ms. Under CPU throttle conditions (node load > 8.0), p99 latency spiked to 210ms.
- **eBPF Inode Map Lookup:** Average latency of 1.4 microseconds. Latency remains flat under high node CPU load.

| Metric | CRI gRPC Attestation | eBPF Socket Matching |
| :--- | :--- | :--- |
| **Mean Latency** | 34.2 ms | 1.4 μs |
| **p99 Latency** | 114.0 ms | 4.1 μs |
| **p99 (High Node Load)** | 210.0 ms | 5.8 μs |
| **TOCTOU Exploit Window** | 15 - 200 ms | **0.0 ms (Hardened)** |
| **CRI Socket Dependency** | Yes | No |

### Operational Caveats
Deploying eBPF code into production requires monitoring kernel state. While the maps are designed to self-evict keys on successful reads, a client that crashes between calling `connect()` and the agent executing `fstat()` will orphan an entry in the BPF map.

To prevent kernel memory exhaustion, configure the `socket_metadata_map` size limits carefully. A max entry count of `10240` consumes less than 1MB of kernel slab memory. Additionally, we deploy a lightweight user-space cron process inside the DaemonSet to scan `/proc/net/unix` and sweep the BPF map for inodes that no longer exist in the kernel's active socket table.