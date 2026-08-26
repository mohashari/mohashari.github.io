---
layout: post
title: "Hardening Kubernetes Pod Security: Implementing Custom Seccomp Filters and Monitoring Violations with eBPF and Falco"
date: 2026-08-26 08:00:00 +0700
tags: [kubernetes, security, ebpf, falco, devsecops]
description: "Build, enforce, and audit minimal custom Seccomp profiles for Kubernetes pods, and set up zero-overhead monitoring of blocked syscalls with eBPF and Falco."
image: "https://picsum.photos/seed/3852/1080/720"
thumbnail: "https://picsum.photos/seed/3852/400/300"
---

A single remote code execution (RCE) vulnerability in a Go or Rust Web API should not lead to a full container escape or host compromise. Yet, in most production Kubernetes clusters, it does. By default, even when running under the standard container runtime configuration (`RuntimeDefault`), a container has access to over 300 Linux system calls. These include highly sensitive operations like `unshare`, `ptrace`, `keyctl`, and `clone`. If your application only reads from a network socket, queries a database, and writes structured logs to stdout, it has no business invoking 85% of these syscalls. This post details how to implement custom Seccomp (Secure Computing Mode) profiles to restrict the kernel attack surface to the bare minimum and use eBPF-powered Falco rules to monitor and alert on policy violations in real time without degrading application performance.

## The Attack Surface: Why RuntimeDefault Isn't Enough

The Linux kernel exposes roughly 450 system calls. Container runtimes like containerd and Docker apply a default Seccomp profile that blocks dangerous operations such as `mount`, `reboot`, and `kexec_load`. While this cuts off obvious vectors, it still permits around 300+ syscalls to maintain maximum compatibility with arbitrary application stacks.

For a specialized backend service, this default list is dangerously permissive. Consider `unshare`. This syscall allows a process to disassociate parts of its execution context, such as namespaces. An attacker exploiting an RCE can use `unshare` to create a new user namespace, map their unprivileged container UID to root inside that namespace, and exploit kernel vulnerabilities (like CVE-2022-0185 in the filesystem context subsystem) to mount raw file systems and escape to the host node.

By enforcing custom Seccomp profiles, you restrict the process at the kernel level. If your Go binary attempts to call a blocked system call, the kernel immediately intercepts the request and denies execution or terminates the calling thread. The vulnerability is rendered useless because the system call required to execute the payload is physically unavailable to the process.

## Step 1: Profiling and Generating Custom Seccomp Profiles

Before enforcing a custom Seccomp profile, you must determine exactly which syscalls your application requires. Generating this profile manually is prone to errors, while dynamic tracing with standard utilities like `strace` introduces substantial performance overhead because `ptrace` context-switches for every single system call.

Instead, you can record system calls in a staging environment under simulated production loads. The resulting list of syscalls is then used to construct a custom JSON profile. 

Below is an example of a hardened Seccomp profile designed for a production-ready Go gRPC or HTTP microservice. It uses `SCMP_ACT_ERRNO` with `EPERM` (Operation Not Permitted) as the default action. This ensures that any unlisted system call is blocked and returns an error code to the caller, rather than immediately terminating the process, which is ideal during initial testing.

<script src="https://gist.github.com/mohashari/7d4d463ad2d2003230dcb030ba0556e6.js?file=snippet-1.json"></script>

This profile permits memory allocation (`mmap`, `mprotect`), threading operations (`clone`, `futex`), I/O multiplexing (`epoll`), network operations (`accept4`), and basic system telemetry (`getpid`). All other system calls, including execution of shell binaries via `execve`, are blocked.

## Step 2: Deploying Seccomp Profiles in Kubernetes

To make a custom Seccomp profile available to Kubernetes pods, the JSON file must be located on the worker node's local filesystem inside the Kubelet seccomp directory, which defaults to `/var/lib/kubelet/seccomp/`. 

To apply the profile, you configure the pod's `securityContext` using the `seccompProfile` field set to `Localhost`, referencing the relative path of the file starting from `/var/lib/kubelet/seccomp/`.

<script src="https://gist.github.com/mohashari/7d4d463ad2d2003230dcb030ba0556e6.js?file=snippet-2.yaml"></script>

When Kubelet provisions this pod, it reads the JSON profile from `/var/lib/kubelet/seccomp/profiles/payment-gateway-v1.json` and configures the container runtime (e.g., containerd) to apply the filter when executing the container's entrypoint.

## Step 3: Automating Profile Generation with the Security Profiles Operator

Distributing JSON files manually to `/var/lib/kubelet/seccomp/` on every worker node is fragile and doesn't scale in dynamic environments where nodes are autoscaled. The Kubernetes-native solution is the **Security Profiles Operator (SPO)**.

SPO runs as a DaemonSet and synchronizes Seccomp profiles across all nodes using Custom Resources. It also includes an eBPF-based recorder that can trace a running pod's system calls in staging and automatically output a fully structured `SeccompProfile` object.

The following manifest defines a `ProfileRecording` resource. It targets pods matching the label `app: payment-gateway` in the `production` namespace and records all system calls directly from the container runtime interface.

<script src="https://gist.github.com/mohashari/7d4d463ad2d2003230dcb030ba0556e6.js?file=snippet-4.yaml"></script>

Once the recording session is complete, SPO outputs a new custom resource of kind `SeccompProfile`. This resource can then be exported, saved in Git, and deployed via your CI/CD pipeline to production namespaces.

## Step 4: The Monitoring Gap — Catching Blocked Syscalls with eBPF and Falco

Enforcing Seccomp profiles is only half the battle. If a blocked system call occurs in production, it generally indicates one of two things:
1. **Application drift**: An updated library or dependency is using a new system call (e.g., Go upgrading its internal poll mechanism from `epoll_wait` to `epoll_pwait2`), causing the application to crash or throw errors.
2. **An active exploit attempt**: An attacker has executed code in your container and is attempting to probe the OS, escalate privileges, or execute binaries.

When Seccomp blocks a syscall with `SCMP_ACT_ERRNO`, the application receives an error code (such as `EPERM`). Traditional user-space logging is insufficient because the application may not log the raw system error, or the process may crash before writing to stdout. Furthermore, host-level audit logging (`auditd`) lacks Kubernetes context (namespaces, pod names, and container IDs).

Using Falco, a CNCF runtime security tool, you can capture these events using eBPF probes. Falco hooks system call tracepoints at the kernel level. It detects when a process returns `EPERM` for system calls that are typically blocked or highly sensitive, extracting container and Kubernetes metadata in the process.

Below is a custom Falco rule that triggers an alert whenever a containerized process attempts to execute a blocked syscall within production namespaces.

<script src="https://gist.github.com/mohashari/7d4d463ad2d2003230dcb030ba0556e6.js?file=snippet-3.yaml"></script>

Because Falco monitors system calls using eBPF ring buffers, the overhead is minimal, adding less than 1-2% CPU utilization under high-throughput network workloads.

## Step 5: Self-Hardening: Embedding Seccomp in the Application Binary

For highly sensitive services (like cryptographic microservices or tokenization engines), relying solely on Kubernetes infrastructure configuration leaves a security gap during local development or if the cluster configuration is modified. You can implement defense-in-depth by compiling Seccomp filters directly into your backend binaries.

Go applications can load Seccomp filters programmatically using the standard `libseccomp` library bindings. The following implementation configures the system call filter. It must be called early in the initialization sequence (`init()` or at the start of `main()`), but after the Go runtime has finished spawning its initial scheduler threads.

<script src="https://gist.github.com/mohashari/7d4d463ad2d2003230dcb030ba0556e6.js?file=snippet-5.go"></script>

*Note: This approach requires CGO compilation and links against `libseccomp` on the target host. Ensure your Docker multi-stage build uses a matching distribution (e.g., Debian or Alpine with `musl-dev` and `libseccomp-dev`).*

## Step 6: Verifying Seccomp Enforcement on the Host

When debugging deployment issues, you should verify if Seccomp filters are actually active for your running containers. You can inspect the Linux status of any running process on the worker node.

The kernel exposes Seccomp status inside the `/proc` filesystem under the `Seccomp` field of a process status file.
- `0`: Seccomp is disabled.
- `1`: Strict mode (allows only `read`, `write`, `exit`, and `sigreturn`).
- `2`: Filtering mode (custom Seccomp filters applied via BPF).

The following bash script runs on a Kubernetes worker node. It queries the local container shims (containerd) to identify target processes and outputs their active Seccomp mode status.

<script src="https://gist.github.com/mohashari/7d4d463ad2d2003230dcb030ba0556e6.js?file=snippet-6.sh"></script>

If a production container returns `0 (Disabled!)`, the pod configuration has bypassed your Seccomp rules, and it should be remediated immediately.

## Production Failure Modes & Runbooks

Implementing custom Seccomp filters in production introduces operational risks that must be managed to prevent unexpected service disruptions.

### Failure Mode 1: Syscall Drift due to Go/Rust Runtime Upgrades
When the Go or Rust compiler version is updated, the underlying runtime may change how standard operations are mapped to system calls. For example, a minor version update could replace `select` with `pselect6`, or begin using newer kernel interfaces like `epoll_pwait2`. If the Seccomp profile does not allow this new syscall, the application will crash loop instantly on startup with `EPERM` or `ENOSYS`.
* **Mitigation**: Never promote a Seccomp profile directly to production without running it in staging under load test conditions. Use `SCMP_ACT_ERRNO` with `EPERM` in staging, search the logs for `Permission Denied` errors, and append missing system calls to your JSON profiles before promoting them to production.

### Failure Mode 2: Multi-Architecture CPU Discrepancies
If your Kubernetes cluster runs a mix of x86_64 and ARM64 worker nodes (e.g., AWS Graviton), system call structures will differ. ARM64 kernels do not support older system calls (such as `open` or `dup2`), relying instead on modern alternatives like `openat` and `dup3`. A profile recorded on x86_64 might lack these calls, causing the pod to crash if scheduled onto an ARM64 node.
* **Mitigation**: Ensure your Seccomp configuration contains the `architectures` block for both `SCMP_ARCH_X86_64` and `SCMP_ARCH_AARCH64`. Record profiles on both architectures during the CI pipeline validation stages.

### Failure Mode 3: Falco eBPF Event Drops under High Network Load
On high-throughput services running on large nodes, Falco may drop system call events if the kernel-to-user-space ring buffers become saturated. Dropped events mean security violations could pass unnoticed.
* **Mitigation**: Optimize Falco configuration by allocating larger buffer sizes using the `modern-ebpf` engine. Tune the kernel ring buffer size:
  ```yaml
  engine:
    modern_ebpf:
      cpus_share_buffer: 2
      buffer_size_in_bytes: 16777216 # 16MB buffer per CPU core
  ```
  Additionally, discard non-security-related system calls at the eBPF filter layer before they are copied to user-space.