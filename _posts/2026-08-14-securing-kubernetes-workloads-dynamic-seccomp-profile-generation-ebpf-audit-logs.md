---
layout: post
title: "Securing Kubernetes Workloads with Dynamic Seccomp Profile Generation Using eBPF Audit Logs"
date: 2026-08-14 08:00:00 +0700
tags: [kubernetes, ebpf, seccomp, devsecops]
description: "Learn how to dynamically generate tight, production-ready seccomp profiles using eBPF to dramatically reduce your Kubernetes container attack surface."
image: "https://picsum.photos/seed/4530/1080/720"
thumbnail: "https://picsum.photos/seed/4530/400/300"
---

Consider this production nightmare: an attacker exploits a remote code execution (RCE) vulnerability in a public-facing containerized Go application. Because the container runs with the default Kubernetes seccomp profile, the attacker has access to over 300 system calls. Within minutes, they execute `unshare` to detach namespaces, exploit a known kernel vulnerability in `io_uring`, escape to the host node, and compromise the entire cluster. In a microservices architecture, a typical backend service requires only 40 to 70 system calls to perform its function. Allowing the remaining 230+ unused syscalls represents an unnecessary, massive security debt. By implementing dynamic seccomp profile generation powered by eBPF audit logs, you can systematically restrict your containers to the absolute minimum set of syscalls they need to survive, rendering kernel exploits and container escapes ineffective.

## The Security Debt of RuntimeDefault

Container runtimes such as `containerd` and `CRI-O` ship with a default seccomp profile, often configured via the `RuntimeDefault` setting in Kubernetes. While `RuntimeDefault` is a step up from running unconfined, it is fundamentally a compromise. It aims to prevent obvious exploits while ensuring that 99% of applications run without modification. Consequently, it allows powerful and complex syscalls like `keyctl`, `ptrace`, `unshare`, and `clone` (with arbitrary flags).

A typical backend service (for example, a REST API built in Go or Rust) does nothing but read from sockets, execute disk I/O, allocate memory, and manage threads. Allowing access to kernel keyring management, namespace manipulation, or tracing interfaces within these pods is a violation of the principle of least privilege. 

Historically, developers avoided writing custom seccomp profiles because the process was manual, error-prone, and fragile. If you missed a single syscall required during a rare code path—such as a database reconnect or log rotation—the kernel would immediately terminate the thread (`SIGSYS`), causing a hard crash in production. To fix this, we need a way to audit the application's actual syscall footprint in a staging environment under realistic traffic, compile that list into a profile, and apply it declaratively.

## The Overhead of Legacy Auditing: Why Strace and Auditd Fail

To generate a seccomp profile, you must first inspect what your application is doing. Traditional tools fail in production and staging environments for the following reasons:

1. **strace (ptrace-based interception):** `strace` relies on the `ptrace(2)` system call. Every time the tracee enters and exits a syscall, the kernel halts the process and context-switches to `strace`. For a high-throughput backend API processing 10,000 requests per second, `strace` introduces a 10x to 100x latency penalty. It is unusable in staging, let alone production.
2. **auditd (Linux Audit Framework):** `auditd` operates in the kernel, but filtering logs by specific containers is difficult. The Linux audit framework is namespace-unaware. Scraping `/var/log/audit/audit.log` and trying to map host PIDs back to ephemeral Kubernetes container IDs in real time requires complex log-parsing pipelines and introduces significant log-delivery latency.
3. **eBPF (Extended Berkeley Packet Filter):** eBPF bypasses these limitations. By attaching a program to the `raw_syscalls/sys_enter` tracepoint, we inspect syscalls directly inside the kernel context. The performance overhead is negligible—typically less than 1% CPU utilization—making it safe to run in active staging environments.

## Building the eBPF Syscall Monitor

To build an automated profiler, we need two components: a kernel-space eBPF program that captures syscall events, and a user-space daemon that associates these events with container metadata.

The kernel-space program must hook the `sys_enter` tracepoint. However, to avoid overloading user space with every syscall executed on the host, the eBPF program must filter events. We accomplish this by maintaining a BPF hash map containing the mount namespace IDs (`mnt_ns`) of the containers we want to profile.

Below is the kernel-space eBPF program written in C:

<script src="https://gist.github.com/mohashari/d552e81220384916ad64ae882a1a184f.js?file=snippet-1.txt"></script>

Next, we write the user-space daemon in Go. This program loads the compiled eBPF object, writes target mount namespace IDs into the hash map, and reads the aggregated syscall IDs from the ring buffer.

<script src="https://gist.github.com/mohashari/d552e81220384916ad64ae882a1a184f.js?file=snippet-2.go"></script>

## Mapping Namespaces to Pods

To make the user-space daemon functional, it must map host PIDs to Kubernetes Pods. When Kubelet spins up a pod, containerd creates a new process namespace. 

By reading from `/proc` on the host, the monitoring daemon can resolve the mount namespace inode of any process. The daemon can then query the runtime socket (e.g., `/run/containerd/containerd.sock`) to correlate the host PID with OCI container labels like `io.kubernetes.pod.name` and `io.kubernetes.container.name`.

Here is the helper function to resolve a host PID to its Mount Namespace ID:

<script src="https://gist.github.com/mohashari/d552e81220384916ad64ae882a1a184f.js?file=snippet-3.go"></script>

## Structuring the Seccomp Profile

Once the eBPF agent outputs the list of syscall numbers, we must translate them into names (e.g., matching syscall ID `0` to `read` and `1` to `write` on x86_64) and construct an OCI-compliant seccomp profile JSON file. 

Any syscall not explicitly defined in the `syscalls` array must fall back to the `defaultAction`. For a secure configuration, `defaultAction` must be set to `SCMP_ACT_ERRNO`. This ensures that unauthorized syscalls fail gracefully with a permission error instead of crashing the process outright with `SCMP_ACT_KILL_PROCESS`, unless you specifically require strict termination.

Below is a generated, minimal seccomp profile JSON suitable for a Go backend microservice:

<script src="https://gist.github.com/mohashari/d552e81220384916ad64ae882a1a184f.js?file=snippet-4.json"></script>

## Production Deployment via Security Profiles Operator

Distributing raw JSON seccomp profiles manually to all Kubernetes nodes at `/var/lib/kubelet/seccomp/` is impractical at scale. It creates out-of-sync configurations, node drift, and deployment failures.

The production-grade solution is the **Security Profiles Operator (SPO)**, a Kubernetes SIG subproject. SPO runs a daemon on every node, exposing custom resource definitions (CRDs) for managing AppArmor and Seccomp profiles natively.

We define a `SeccompProfile` custom resource for our application:

<script src="https://gist.github.com/mohashari/d552e81220384916ad64ae882a1a184f.js?file=snippet-5.yaml"></script>

SPO watches this resource and automatically writes the corresponding JSON profile to Kubelet’s local directory. 

We then configure our pod's deployment spec to consume this profile. The profile path points to a file within Kubelet’s directory relative to the `/var/lib/kubelet/seccomp` root. SPO dynamically creates this path matching the format: `operator/<namespace>/<profile-name>.json`.

<script src="https://gist.github.com/mohashari/d552e81220384916ad64ae882a1a184f.js?file=snippet-6.yaml"></script>

## Automated Profiling Pipelines: CI/CD Integration

To ensure seccomp profiles stay up to date as our codebase evolves, we must automate profile generation within our CI/CD pipeline. 

During the integration testing phase, we spin up the application container alongside our eBPF monitor. A testing harness executes tests designed to trigger every code branch, endpoint, error condition, and database integration. Once the test run finishes, the harness parses the log, aggregates the syscalls, and updates the `SeccompProfile` manifest stored in the Git repository.

Below is a bash pipeline script that automates this workflow:

<script src="https://gist.github.com/mohashari/d552e81220384916ad64ae882a1a184f.js?file=snippet-7.sh"></script>

## Production Pitfalls and Mitigation Strategies

Dynamic profiling is highly effective, but deploying dynamically generated seccomp profiles to production can cause issues if you don't account for runtime edge cases.

### 1. The Startup vs. Runtime Split

During the boot phase, runtime loaders (like `ld.so`) execute syscalls that your application will never run again once it is initialized. For instance, the loader reads libraries, opens files under `/etc/ld.so.cache`, and calls `uname` or `arch_prctl`. If your profiling harness only monitors the application *after* it has started, the container will crash immediately upon startup because it is missing initialization-phase syscalls.

**Mitigation:** Always start the eBPF audit recorder *before* the application container begins its entrypoint execution, and capture the entire container lifecycle from PID 1 initialization through to shutdown.

### 2. Dynamic DNS Resolution and Cgo

Go’s runtime resolves hostnames using a pure Go implementation by default. However, under certain conditions—such as if Cgo is enabled, if `/etc/nsswitch.conf` is present, or if the resolver encounters specific local domain lookups—Go falls back to the system’s C library resolver (glibc). Glibc invokes the `socket(AF_NETLINK)` system call to interact with the kernel’s network configuration, and dynamically loads shared objects using `mmap` and `openat`.

If your test suite does not trigger a DNS timeout or a resolution failure that forces a Cgo resolver fallback, these system calls will not be recorded in your profile. If this fallback happens in production, the kernel will immediately kill the thread.

**Mitigation:** Ensure your test suite mocks DNS failures to trigger fallback code paths, or append a static baseline array of common network-related syscalls to every generated profile.

### 3. Merging Dynamic Profiles with a Safe Baseline

To protect against dynamic library dependencies that load lazily, you should merge your dynamically captured syscall list with a safe baseline profile. This baseline includes basic operations for memory allocation, thread synchronization, and signal handling.

At a minimum, ensure the following syscalls are always allowed:

<script src="https://gist.github.com/mohashari/d552e81220384916ad64ae882a1a184f.js?file=snippet-8.json"></script>

By merging this baseline array with your dynamically generated profile, you protect your application from crashing during runtime memory adjustments or garbage collection cycles, while still blocking dangerous syscalls like `kexec_load`, `ptrace`, `unshare`, and `sys_chroot`.

## Conclusion

Securing your workloads with dynamic seccomp profiles does not require manual tuning. By using eBPF, you can audit your workloads at the kernel level without introducing latency or complexity. Automating this process in your CI/CD pipeline and deploying the profiles via the Security Profiles Operator ensures that your applications run with the minimum privileges they need to operate, significantly reducing your cluster's attack surface.