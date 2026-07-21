---
layout: post
title: "Automating Real-Time Goroutine Leak Detection in Production Using eBPF and Runtime/pprof"
date: 2026-07-21 08:00:00 +0700
tags: [golang, ebpf, pprof, observability, kubernetes]
description: "A production-tested architecture for zero-overhead goroutine leak detection using eBPF uprobes combined with automated runtime/pprof differential profiling."
image: "https://picsum.photos/seed/4727/1080/720"
thumbnail: "https://picsum.photos/seed/4727/400/300"
---

It is 3:00 AM, and your high-throughput Go service has just been terminated by the Linux Out-Of-Memory (OOM) killer on Kubernetes. Standard Prometheus alerts for memory usage showed a flat line for days, followed by a sudden, steep climb and a crash. The culprit is a slow, silent goroutine leak—a background worker blocked on an unbuffered channel with no timeout, accumulating stack memory at a rate of 150MB per hour. Traditional APM profilers are either too heavy to run continuously in production, causing up to a 30% latency overhead during serialization, or too sparse to catch transient leaks. This post details how to build a production-grade, zero-overhead automated detector for goroutine leaks by combining kernel-space tracing via eBPF uprobes with the surgical precision of Go's `runtime/pprof` differential profiling.

## The Silent Threat: How Goroutine Leaks Escape Traditional APMs

A goroutine stack starts at 2KB, but can grow dynamically to 1GB. If your service handles 100,000 requests per second and leaks just 0.05% of its goroutines (50 goroutines per second), within one hour you will have 180,000 leaking goroutines. At a minimum of 2KB per stack, that is roughly 360MB of leaked RAM. However, if those goroutines have grown their stacks or reference objects on the heap, the actual memory leak rate can easily exceed 5GB per hour.

Simply monitoring the global goroutine count via `runtime.NumGoroutine()` or the Prometheus metric `go_goroutines` is reactive, not proactive. It alerts you when the system is already unstable, but tells you nothing about *which* path is leaking. Conversely, continuously polling HTTP endpoints like `/debug/pprof/goroutine?debug=1` under heavy traffic is unsafe. Generating text-based pprof dumps requires stopping the world or acquiring global runtime locks to serialize the stack traces of hundreds of thousands of goroutines, introducing latency spikes that can degrade API response times.

A robust solution requires a hybrid approach:
1. **Low-Overhead Kernel Monitoring (eBPF)**: Use eBPF uprobes to trace goroutine lifespans in real time. We aggregate counts in kernel-space maps, tracking how many goroutines were created vs. exited per parent call-site.
2. **On-Demand Diagnostic Profiling (pprof)**: Only trigger `runtime/pprof` when eBPF detects an anomalous, sustained growth rate.
3. **Differential Analysis**: Capture two short-interval pprof profiles, parse them, and compute a stack-by-stack delta to isolate the exact line of code responsible for the leak.

## Overcoming Go's Runtime Architecture Challenges with eBPF

To trace goroutines in kernel space, we must hook the Go runtime scheduler. However, Go's runtime model differs from standard C/C++ applications:
- **Custom Calling Convention**: Since Go 1.17, Go uses a register-based calling convention on amd64 (passing arguments in `RAX`, `RBX`, `RCX`, etc.), rather than the standard System V AMD64 ABI.
- **Split Stacks**: Go manages its own stack allocation.
- **Uretprobe Instability**: Linux `uretprobes` modify the return address on the stack to redirect execution to a trampoline. Because Go's runtime moves stack frames when they grow or shrink, hijacked return addresses confuse the Go scheduler and garbage collector, resulting in immediate panic or silent memory corruption.

To bypass `uretprobe` instability, we avoid them entirely. Instead, we use a per-thread correlation map to associate the creation call-site with the newly scheduled goroutine across two separate uprobes:
1. Entry of `runtime.newproc1`: Captures the caller program counter (`callerpc`), which represents the line of code that spawned the goroutine. We store this in a BPF map keyed by the current thread ID (`tgid_pid`).
2. Entry of `runtime.runqput`: Runs immediately after `newproc1` on the same thread, receiving the newly created goroutine pointer (`*g`). We retrieve the `callerpc` from the thread-local map and save the mapping of `gp -> callerpc` in an active tracking map.
3. Entry of `runtime.goexit1`: Executed when a goroutine exits. We read the exiting `g` pointer from register `R14` (which Go reserves on amd64 for the current goroutine context), look up its `callerpc` in our tracking map, decrement the active count, and delete the mapping.

## Implementing the eBPF C Tracer

The kernel-space code uses BPF hash maps to record active goroutines per call-site and update counters.

<script src="https://gist.github.com/mohashari/fbb0d11edd529724558ecb95bfd5f1f7.js?file=snippet-1.txt"></script>

## Loading eBPF and Attaching Uprobes to the Runtime

To execute this compiled object, we write a Go bootstrap loader using the Cilium `ebpf` package. The uprobes are attached to `/proc/self/exe` so that the binary monitors itself.

<script src="https://gist.github.com/mohashari/fbb0d11edd529724558ecb95bfd5f1f7.js?file=snippet-2.go"></script>

## Monitoring Maps and Resolving Virtual Addresses

Because the eBPF tracer hooks `/proc/self/exe`, the program counter (`callerpc`) stored in the BPF map is a virtual address in the process's own memory space. This allows us to resolve the program counter to the exact Go function name, source file, and line number using the Go standard library's `runtime.FuncForPC()` without needing external ELF parsing or debug symbols.

<script src="https://gist.github.com/mohashari/fbb0d11edd529724558ecb95bfd5f1f7.js?file=snippet-3.go"></script>

## Implementing Differential Profiling

Once the monitor alerts us to a suspicious creation rate, we execute the differential diagnostics. Running `runtime/pprof` dumps the call stacks of all goroutines. To identify the specific leaking code path, we collect two profiles separated by a time window, parse the binary protobuf format, and subtract the stack counts of the baseline profile from the target profile.

<script src="https://gist.github.com/mohashari/fbb0d11edd529724558ecb95bfd5f1f7.js?file=snippet-4.go"></script>

## Coordinating the Leak Detector Agent

The `LeakDetector` links the BPF tracker and the differential analyzer. It coordinates the execution lifecycle, running BPF maps validation in the background and performing on-demand stack profiling.

<script src="https://gist.github.com/mohashari/fbb0d11edd529724558ecb95bfd5f1f7.js?file=snippet-5.go"></script>

## Running the Tracer in Production Kubernetes

Deploying eBPF code requires access to kernel subsystems. In a Kubernetes cluster, we package our Go binary with the eBPF code and deploy it using a `DaemonSet` or a sidecar container. Instead of running a fully privileged container, we configure granular Linux capabilities like `CAP_SYS_ADMIN` or `CAP_BPF` to secure the execution footprint.

```yaml
# snippet-6
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: goroutine-leak-detector
  namespace: monitoring
spec:
  selector:
    matchLabels:
      app: goroutine-leak-detector
  template:
    metadata:
      labels:
        app: goroutine-leak-detector
    spec:
      hostPID: true # Allows access to the host pid namespace to read target /proc/PID/exe
      containers:
        - name: detector
          image: internal-registry.corp/observability/goroutine-detector:v1.2.0
          securityContext:
            capabilities:
              add:
                - SYS_ADMIN # Required for loading uprobes and mapping page tables
                - BPF       # Required on newer kernels (Linux 5.8+) to manage BPF maps
            privileged: false
          volumeMounts:
            - name: sys-kernel-debug
              mountPath: /sys/kernel/debug
              readOnly: false
            - name: sys-fs-bpf
              mountPath: /sys/fs/bpf
              mountPropagation: Bidirectional
      volumes:
        - name: sys-kernel-debug
          hostPath:
            path: /sys/kernel/debug
            type: Directory
        - name: sys-fs-bpf
          hostPath:
            path: /sys/fs/bpf
            type: DirectoryOrCreate
```

## Fine-Tuning and Production Considerations

Running eBPF-based self-tracing in a production environment introduces a set of technical trade-offs that you must address before deploying to high-throughput clusters.

### Compiler Inlining and Symbol Resolution

Because the eBPF program attaches directly to runtime symbols like `runtime.newproc1`, it relies on these symbols being present in the symbol table of the binary. The Go compiler can inline functions during optimization. If the runtime scheduler functions are modified or inlined by a custom Go compiler build, the uprobe attach will fail.

You can verify that the symbols exist in your compiled binary using `go tool nm`:

```bash
go tool nm /path/to/binary | grep -E 'newproc1|runqput|goexit1'
```

If your build flag optimization settings strip the symbol table (for example, building with `go build -ldflags="-s -w"`), the uprobes will fail to resolve. To trace compiled binaries in production, you must preserve the symbols. Avoid stripping symbols if you plan to use eBPF diagnostics.

### Go Runtime Layout changes

Internal runtime structures, such as the `g` struct, can change between minor and major versions of Go. The offset of fields like `goid` (goroutine ID) within the `g` struct is not a stable API. If your code accesses offsets directly, ensure that you compile your eBPF programs against headers matching the runtime version of the target Go application. Tracing through generic hooks like `newproc1` and using standard `runtime/pprof` for stack capture minimizes the reliance on compiler-internal offsets.

### BPF Map Sizing and Memory Boundings

The size of the `goroutine_parent` BPF hash map should match the target memory footprint of your daemon. If a service runs with a high number of active goroutines, the BPF map could fill up, causing newer allocations to fail to trace. The sample limit of `102,400` in the BPF configuration is configured for services handling high concurrency loads. This size allocates approximately 1.6MB of kernel memory, keeping the footprint low.

## Conclusion

Automating production diagnostics with a hybrid eBPF and `runtime/pprof` approach provides continuous visibility into goroutine lifespans without the CPU overhead of polling. The kernel tracks high-level creation and deletion patterns to detect trends, while the user-space code executes detailed stack capture on demand to isolate leaks. By isolating the exact code paths responsible for memory growth, this pattern enables you to diagnose leaks before they trigger Out-Of-Memory events.