---
layout: post
title: "Securing ECS Fargate Tasks with Tetragon Runtime Security Profiling"
date: 2026-06-21 08:00:00 +0700
tags: [aws, devsecops, ebpf, security]
description: "Generate zero-trust ECS Fargate configurations by profiling application workloads using Cilium Tetragon eBPF telemetry in staging environments."
image: "/images/diagrams/securing-ecs-fargate-tetragon-runtime-security.svg"
thumbnail: "/images/diagrams/securing-ecs-fargate-tetragon-runtime-security.svg"
---

Imagine this: a Go-based REST API running inside an AWS ECS Fargate task gets compromised through a remote code execution (RCE) vulnerability in an upstream image-processing library. The attacker gains shell access, downloads a payload from a command-and-control (C2) server, and writes a backdoor script to `/tmp`. In a traditional self-managed Kubernetes node or standard EC2 instance, your runtime security agents (such as Cilium Tetragon or Falco) would instantly detect the process execution, the raw TCP connection, and the disk writes using host-level eBPF probes. But on Fargate, where the host kernel is managed by AWS and completely abstracted, you cannot run privileged containers, load eBPF programs, or attach kprobes. If a task is breached, the attacker runs silent. This post details how to resolve this critical visibility and hardening gap by using Tetragon in your staging pipeline to profile container behavior, extract runtime security baselines, and compile them into zero-trust ECS configurations that are fully enforced on AWS Fargate.

![Securing ECS Fargate Tasks with Tetragon Runtime Security Profiling Diagram](/images/diagrams/securing-ecs-fargate-tetragon-runtime-security.svg)

## The AWS Fargate Security Blind Spot: The Kernel-Abstractions Conundrum

AWS ECS Fargate is the industry standard serverless container execution platform for teams seeking to eliminate operational node management. Fargate secures workloads at the virtualization layer using Firecracker microVMs, providing strong hardware-level isolation between tasks. However, this serverless abstraction introduces a critical runtime security visibility gap: developers have zero access to the underlying host Linux kernel.

In traditional Linux or standard EC2 setups, security daemons operate in the host namespace, deploying extended Berkeley Packet Filters (eBPF) to intercept syscall entrypoints in the kernel. This allows security agents to inspect activities such as `execve` (process execution), `connect` (network connections), and `write` (filesystem modifications) across all containers transparently. 

On Fargate, however, containers cannot run with the `CAP_SYS_ADMIN` capability or in privileged mode, meaning you cannot read `/sys/kernel/debug` or load custom eBPF programs. If an application dependency is hijacked in production, an attacker can execute arbitrary binaries, open a reverse shell, or query credentials via the Instance Metadata Service (IMDS) without triggering any host-level runtime alarms. Traditional runtime defense tools are locked out.

## The Solution: Staging-Phase eBPF Profiling

To bypass the Fargate eBPF restriction, we shift runtime security profiling left. Instead of attempting to monitor workloads dynamically in production, we execute deep eBPF tracing in a controlled staging or sandbox environment where we control the host kernel.

By running our application container alongside a Cilium Tetragon daemon during integration, end-to-end (E2E), and stress test suites, we can capture a comprehensive baseline of the application's legitimate runtime behavior. The test suite drives the container to execute all expected codepaths: connecting to databases, calling external API payment gateways, reading configurations, and writing logs.

Tetragon profiles these actions, outputting a structured JSON stream containing every syscall, socket connection, and file modification. Once tests are complete, we run a custom profile compiler to parse these logs, filter out background runtime operations, and generate hard infrastructure-as-code configurations. These generated configurations include:
1. **Read-only Filesystems:** Identifying exactly which paths require write access and mounting them to ephemeral volume storage.
2. **Minimal Linux Capabilities:** Dropping unnecessary POSIX capabilities.
3. **Restrained Egress Rules:** Creating tailored VPC Security Groups that restrict network outbound connections to only the identified destination IPs and ports.

## Implementing the Profiling Environment

To set up the profiling pipeline, we deploy our application container to a staging EC2 instance running a modern kernel (5.4+ with BTF support enabled). We launch the Tetragon agent on the host, passing the Docker daemon socket so it can resolve container metadata.

<script src="https://gist.github.com/mohashari/82e67b9c13c62d95086c5d913896e0cc.js?file=snippet-1.sh"></script>

With Tetragon running on the host, we apply a custom `TracingPolicy` to target the specific container. The tracing policy hooks the `sys_execve` syscall for processes, `sys_connect` for network sockets, and file open/write actions to capture a complete footprint.

<script src="https://gist.github.com/mohashari/82e67b9c13c62d95086c5d913896e0cc.js?file=snippet-2.yaml"></script>

With this profiling policy active, run your full integration and E2E test suites. Ensure the application is exercised under production-like traffic patterns, triggering every possible database query, caching path, and external third-party API integration.

## Parsing and Compiling the Tetragon Event Stream

Tetragon logs all intercept events to `/var/log/tetragon/tetragon.log`. A single microservice test run can generate millions of raw event logs. To extract actionable profiling metrics, we run a custom Go script that parses this JSON stream, filters out noise, and aggregates the runtime metadata.

<script src="https://gist.github.com/mohashari/82e67b9c13c62d95086c5d913896e0cc.js?file=snippet-3.go"></script>

The Go compiler script processes the stream to resolve paths and network addresses:
- **Executables:** Identifies the precise binary entrypoints (e.g., `/usr/local/bin/node` or `/app/server`). If `/bin/sh` or `/usr/bin/curl` appears in this list, it indicates a shell injection or command execution occurred during testing.
- **Filesystem Write Directories:** Pinpoints which directories the application writes to. The root filesystem (`/`) should remain untouched, with writes isolated to temporary paths like `/tmp` or `/app/logs`.
- **Egress Endpoints:** Resolves the external IPs and ports. We cross-reference these IPs with cloud service endpoints (like S3, RDS, Secrets Manager) and external REST APIs.

## Compiling Hardened Task Definitions

Once we compile the profile, we apply the rules directly to our production AWS ECS Task Definition. Fargate allows us to lock down container definitions using the `readonlyRootFilesystem` flag. When set to `true`, the Linux kernel prevents any write operations to the root filesystem. 

If the application attempts to write to a path not explicitly mounted as a writable volume, the OS returns an `EROFS` (Read-only file system) error. This prevents attackers from executing payload files or modifying code dynamically. To accommodate legitimate write paths identified during staging profiling, we define ephemeral `mountPoints` backed by Fargate-managed volumes. We also drop all Linux POSIX capabilities using `linuxParameters` to enforce the principle of least privilege.

<script src="https://gist.github.com/mohashari/82e67b9c13c62d95086c5d913896e0cc.js?file=snippet-4.json"></script>

Setting `readonlyRootFilesystem: true` combined with dropping all capabilities creates a highly resilient execution sandbox. Even if an attacker achieves remote code execution, they cannot write a web shell, download malicious executables to disk, or execute system administrative commands.

## VPC Egress Security Group Compilation

Attackers who breach a container immediately attempt to establish a reverse shell or download tools from public repositories (like GitHub or malicious Command & Control IPs). Limiting outbound network connections is the most effective way to block these actions.

By using the egress endpoints extracted by our Go compiler script, we generate a strict Terraform configuration for the task's VPC Security Group. Instead of allowing default outbound traffic (`0.0.0.0/0`), we specify exact CIDRs and ports.

<script src="https://gist.github.com/mohashari/82e67b9c13c62d95086c5d913896e0cc.js?file=snippet-5.hcl"></script>

*Note on Dynamic IP Endpoints:* For SaaS services with dynamic IPs (like Stripe or SendGrid), IP addresses can rotate. To handle this without opening egress to `0.0.0.0/0`, you should route Fargate outbound traffic through a NAT Gateway equipped with an AWS Network Firewall or a forward proxy (like Squid or Envoy) that filters traffic at the DNS level (FQDN filtering).

## Auditing Anomalies in Production with AWS GuardDuty

While staging profiling minimizes the attack surface, production systems require continuous auditing. Since Tetragon cannot run directly in Fargate, we leverage AWS GuardDuty ECS Runtime Monitoring. AWS manages a lightweight agent that runs inside the Fargate infrastructure, sending runtime process telemetry to GuardDuty.

When GuardDuty detects an anomaly (e.g., a process execution that violates our baseline, or an unexpected outbound TCP socket to a known Tor exit node), it generates a finding. We configure Amazon EventBridge to capture these findings and trigger an AWS Lambda function for automated incident response. The Lambda terminates the compromised ECS task immediately, forcing the ECS Service to launch a clean instance.

<script src="https://gist.github.com/mohashari/82e67b9c13c62d95086c5d913896e0cc.js?file=snippet-6.go"></script>

This architecture closes the loop: staging profiling hardens the container boundary, while automated AWS-native auditing terminates tasks at the first sign of deviation.

## Production Reality Checks and Failure Modes

Implementing staging-phase runtime security profiling is highly effective, but senior engineers must anticipate several real-world operational challenges:

1. **The Profiling Coverage Gap:** If your E2E test suite does not exercise a specific code path (e.g., an error-handling block that writes a debug dump to disk, or a monthly cron process), that path will not be captured by Tetragon in staging. When deployed to production, the task will crash with an `EROFS` error or a blocked socket. To mitigate this, run profiling on staging servers under shadow production traffic (using tools like GoReplay) for at least 48 hours before locking down task definitions.
2. **Dynamic Runtime Libraries:** Modern runtimes like Node.js or the JVM dynamically load shared libraries (`.so` files) or write cache files under paths like `/root/.cache`. Running these runtimes on read-only filesystems requires thorough mapping of writeable volumes. When profiling, ensure you analyze the logs for hidden system writes from parent runtime processes, not just your application binary.
3. **Wolfi/Distroless Container Images:** Hardening yields the best results when paired with minimal base images. Using standard Ubuntu or Debian images introduces hundreds of binaries (`curl`, `apt`, `sh`) that can be abused. Package your application inside a Google Distroless or Wolfi image. These images contain zero package managers and shell utilities, ensuring that even if an attacker attempts an execution bypass, there are no binaries available to execute.

By shifting runtime security analysis left with Tetragon and enforcing immutable configurations on AWS Fargate, you achieve a defense-in-depth architecture that survives the loss of traditional host-level observability.