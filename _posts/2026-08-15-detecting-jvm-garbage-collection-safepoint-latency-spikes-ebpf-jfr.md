---
layout: post
title: "Detecting JVM Garbage Collection Safepoint Latency Spikes in Production with eBPF and JFR"
date: 2026-08-15 08:00:00 +0700
tags: [jvm, ebpf, jfr, latency, observability]
description: "Detect and diagnose JVM Garbage Collection safepoint and TTSP latency spikes in production using a combination of eBPF kernel tracing and JFR."
image: "https://picsum.photos/seed/1348/1080/720"
thumbnail: "https://picsum.photos/seed/1348/400/300"
---

Your high-throughput Java microservice is running under a steady load of 50,000 requests per second, maintaining a comfortable p99 latency of 8 milliseconds. Suddenly, without warning, the p99 latency shoots up to 850 milliseconds, causing downstream timeouts, health-check failures, and load-balancer drops. You inspect your Prometheus dashboard: CPU utilization is flat, JVM memory pools show ample headroom, and the Garbage Collection (GC) log reports a nominal pause time of just 12 milliseconds. Your APM tells you that database queries are fast and network I/O is idle. What you are witnessing is the silent killer of high-performance Java applications: Time-To-Safepoint (TTSP) latency. Standard JVM metrics lie to you because GC pause times only record the duration *inside* the safepoint operation. They completely ignore the synchronization window—the agonizing period where the VM Thread is blocked, waiting for application mutator threads to yield. Diagnosing these transient spikes in production requires moving past basic JMX counters and instrumenting the boundary where the JVM runtime meets the Linux kernel. By combining the low-overhead kernel-level visibility of eBPF with the rich thread-stack context of JDK Flight Recorder (JFR), you can systematically expose, measure, and eliminate safepoint latency.

![Detecting JVM Garbage Collection Safepoint Latency Spikes in Production with eBPF and JFR Diagram](/images/diagrams/detecting-jvm-garbage-collection-safepoint-latency-spikes-ebpf-jfr.svg)

## The Safepoint Anatomy: Why Standard Metrics Lie to You

To understand why standard GC metrics fail during latency spikes, we must dissect the execution model of JVM safepoints. A safepoint is a global state coordination mechanism. To perform operations that require a stable heap or thread-stack layout—such as garbage collection, biased lock revocation, JIT class redefinition, or generating a thread dump—the JVM must suspend all application (mutator) threads. 

The VM Thread (a dedicated JVM control thread) initiates a safepoint and coordinates the suspension of all other threads. However, the VM Thread cannot simply halt mutators mid-execution. Doing so could leave threads in inconsistent states, corrupting registers or heap references. Instead, the JVM relies on cooperative suspension. When a safepoint is requested, the VM Thread sets a global safepoint state and alters the memory permissions of a dedicated "safepoint polling page" to `PROT_NONE` via a kernel system call (typically `mprotect`). 

During execution, the Just-In-Time (JIT) compiler inserts safepoint checks (polling instructions) into the compiled machine code. These checks are strategically placed at method entries, method exits, and loop backedges. The machine code for a poll typically attempts a read operation from the address of the safepoint polling page:

```assembly
test %eax, (%rsi)  ; Where %rsi holds the address of the safepoint polling page
```

When no safepoint is active, the page is readable, and this instruction executes as a cheap `nop` (costing fractions of a nanosecond). However, when the VM Thread protects the page, any mutator thread executing this read triggers a page fault (`SIGSEGV`). The JVM's signal handler intercepts this hardware exception, changes the thread state from `_thread_in_Java` to `_thread_blocked`, and suspends the thread on a monitor until the safepoint completes.

This introduces a critical period called **Time-To-Safepoint (TTSP)**. The TTSP is the duration between the VM Thread initiating a safepoint and the final mutator thread transitioning to a suspended state. 

Standard JVM metrics (exposed via JMX or standard GC logs) report the time spent *inside* the safepoint—the execution time of the VM operation itself (e.g., GC marking/sweeping). They do not measure TTSP. If a single mutator thread is stuck in an un-safepointed state for 800 milliseconds, and the subsequent GC cycle takes 12 milliseconds, your metrics will report:
*   GC Pause: 12 ms
*   System Stopthedom (STW) Phase: 12 ms

Yet, your clients have experienced an 812-millisecond freeze. The application threads were blocked from progressing while they waited for that single tardy thread to yield. The primary root causes of TTSP latency include:
1.  **Counted Loops**: The JIT compiler optimizes loops with integer counters by removing safepoint polls to maximize execution speed. If a loop runs millions of iterations, the thread running it cannot pause until the loop terminates.
2.  **JNI Transitions and OS Calls**: Threads in native code (`_thread_in_native` state) can continue running when a safepoint is called because they cannot mutate Java heap objects. However, when the thread attempts to transition back to Java code or interact with the JNI environment, it must block. If the native call blocks on OS-level locks, page allocation, or socket I/O, it stalls the safepoint.
3.  **Kernel Page Faults and Memory Compaction**: When the VM Thread calls `mprotect` to protect the polling page, or when the system scales GC worker threads, it interacts heavily with the OS virtual memory subsystem. Under memory pressure, page allocation or kernel lock contention (such as `mmap_sem`) can block the VM Thread itself.

## eBPF to the Rescue: Tracing TTSP from the Kernel Space

When a JVM experiences a safepoint stall, the process is partially or completely unresponsive. Querying JVM diagnostics via JMX, HTTP endpoints, or even local tools like `jstack` can time out because these diagnostic tools must themselves acquire a safepoint to execute. 

Extended Berkeley Packet Filter (eBPF) provides an out-of-band diagnostic path. Because eBPF runs within the Linux kernel, it can observe process execution, thread transitions, page faults, and system calls without requiring the cooperation of the target JVM. 

Modern OpenJDK builds include Userland Statically Defined Tracing (USDT) probes embedded within `libjvm.so`. Specifically, the `hotspot:safepoint__begin` and `hotspot:safepoint__end` probes mark the lifecycle of a safepoint. Crucially:
*   `SafepointSynchronize::begin()` is called by the VM Thread to start the synchronization phase.
*   The `hotspot:safepoint__begin` USDT probe fires **after** all mutator threads have stopped (at the end of TTSP, just before the VM operation runs).
*   The `hotspot:safepoint__end` USDT probe fires when the VM operation completes and threads are resumed.

By attaching to the entry of the C++ function `SafepointSynchronize::begin()` (via a `uprobe`) and correlating it with the `hotspot:safepoint__begin` USDT probe, we can measure TTSP directly from the kernel.

If your JVM binary was compiled without DTrace/USDT support (which is common in minimal container base images), we can fallback to placing `uprobes` on the C++ symbol tables of `libjvm.so`. The mangled name for the begin function is `_ZN20SafepointSynchronize5beginEv`, and the end function is `_ZN20SafepointSynchronize3endEv`.

Here is a `bpftrace` script that attaches to these probes to measure both TTSP and the execution phase of safepoints in production.

<script src="https://gist.github.com/mohashari/9412c259c246fe77f8f64de528d0713d.js?file=snippet-1.txt"></script>

This script measures JVM latency without invasive agents. If you execute this during a latency spike and see low values in `@op_histogram` but massive spikes in `@ttsp_histogram`, you have confirmed that the latency is a TTSP sync problem, not a slow GC execution problem.

## JFR: Correlating Kernel Realities with Thread Stacks

While eBPF tells you *that* a TTSP spike is happening, it operates at the kernel level and cannot resolve the internal application state of Java threads, such as stack traces, thread names, or pool allocations. To pinpoint the exact Java code blocking the safepoint, we must correlate the kernel telemetry with JDK Flight Recorder (JFR).

JFR runs inside the JVM, utilizing a ring buffer to write diagnostic events with less than 1% runtime CPU overhead. The critical events for diagnosing safepoint latency are:
*   `jdk.SafepointBegin`: Logs the start of the safepoint and the sync phase duration.
*   `jdk.SafepointWaitBlocked`: Logs details about individual threads that are blocked or slow to reach the safepoint, including the number of running threads the JVM is waiting on.

To capture these events continuously, you must configure JFR with custom settings. The default templates (`default.jfc` and `profile.jfc`) might filter out short-lived events to conserve disk space. To capture transient TTSP latency spikes, create a custom `.jfc` configuration file that reduces event thresholds.

<script src="https://gist.github.com/mohashari/9412c259c246fe77f8f64de528d0713d.js?file=snippet-2.txt"></script>

When a TTSP anomaly is detected via kernel alerting, you can parse the active or dumped JFR file. To automate this in a diagnostic pipeline, write a Java parser utilizing the `jdk.jfr.consumer` package. This parser parses a binary `.jfr` file, matches `jdk.SafepointBegin` and `jdk.SafepointWaitBlocked` events, and prints the stack trace of the thread that held up the VM Thread.

<script src="https://gist.github.com/mohashari/9412c259c246fe77f8f64de528d0713d.js?file=snippet-3.txt"></script>

## Production Implementation: Building the Correlation Pipeline

To operationalize safepoint detection, you need an automated pipeline that monitors TTSP latency across your entire fleet, triggers alert notifications, and dumps JFR recordings for post-mortem analysis.

This pipeline consists of:
1.  An eBPF-based daemon that runs on the host (or as a DaemonSet in Kubernetes).
2.  A Prometheus metrics exporter exposing TTSP latency histograms.
3.  An alerting rule to capture p99 latency spikes.

Below is the C code for the eBPF program that hooks both the uprobe on the JVM thread initialization and the page fault exceptions of the kernel. This allows SREs to evaluate if a JVM thread's failure to reach a safepoint is correlated with kernel-level memory management delays, such as page fault latency.

<script src="https://gist.github.com/mohashari/9412c259c246fe77f8f64de528d0713d.js?file=snippet-4.txt"></script>

The Go daemon below loads the precompiled object file containing the compiled BPF programs, resolves the symbol tables of the running JVM, attaches the uprobes dynamically, and serves the metrics over an HTTP interface.

<script src="https://gist.github.com/mohashari/9412c259c246fe77f8f64de528d0713d.js?file=snippet-5.go"></script>

Deploying this binary on your production nodes allows you to extract raw safepoint timing details. To alerts on this telemetry, define a Prometheus Alerting Rule to trigger when the 99th percentile of synchronization time surpasses a strict SLA budget of 50 milliseconds.

<script src="https://gist.github.com/mohashari/9412c259c246fe77f8f64de528d0713d.js?file=snippet-6.yaml"></script>

## Root Cause Analysis of Real-World Safepoint Spikes

Once your alerting pipeline triggers, you must act on the data. The following case studies outline the two most common causes of safepoint spikes in high-throughput JVM deployments and details how to remediate them.

### Case Study 1: The Counted Loop Trap
You receive an alert. You pull the JFR log and find a `jdk.SafepointWaitBlocked` event blaming a thread from a thread-pool named `batch-processor-4`. The Java stack trace points to a utility class parsing a large JSON file or calculating a cryptographic checksum inside a loop:

```java
for (int i = 0; i < dataList.size(); i++) {
    // Math operations and data transformations
    processNode(dataList.get(i));
}
```

Because `dataList.size()` returns an integer, the JIT compiler compiles this as a "counted loop." To maximize execution speed, the compiler strips out the safepoint check instruction (`test %eax, (%rsi)`) from the compiled machine code. The CPU runs the loop at full speed, but the thread cannot check the safepoint page for updates. If the loop takes 800 milliseconds to complete, the thread continues executing, and the VM Thread is blocked from completing the safepoint. The entire JVM stalls.

**Remediation**:
To resolve this, you must force the JIT compiler to keep safepoint checks in counted loops. You do this by passing the flag `-XX:+UseCountedLoopSafepoints` to the JVM. 

In Java 10 and newer, a better alternative is Loop Strip Mining (`-XX:+UseCountedLoopSafepoints` combined with `-XX:LoopStripMiningIter`). Loop Strip Mining splits the counted loop into an outer loop and an inner loop. The inner loop executes a set number of iterations (controlled by `LoopStripMiningIter`, default is 1000) without safepoint checks to maintain execution speed. The outer loop contains the safepoint check, preventing a thread from running for more than a few microseconds without polling.

### Case Study 2: Memory Reclamation and OS Page Contention
You receive an alert showing that TTSP spiked to 1.2 seconds, but the JFR log does not show any single Java thread stalling the JVM. Instead, your Go eBPF metrics report a spike in page faults (`jvm_safepoint_page_faults_total`) occurring exactly during the synchronization phase.

This occurs due to Linux memory management. When the VM Thread attempts to initiate a safepoint, it calls:
```c
mprotect(polling_page_address, page_size, PROT_NONE);
```
This forces the CPU to invalidate the Translation Lookaside Buffer (TLB) entries for the page across all CPU cores. If the node is under heavy memory pressure, or if Transparent Huge Pages (THP) compaction (`khugepaged`) is running, the virtual memory subsystems can block on internal locks (such as the MM semaphore `mmap_lock` or zones lru lock). The `mprotect` call blocks, delaying the VM Thread from finishing the safepoint setup. 

Furthermore, if the OS swap file is active and the JVM's memory pages have been swapped to disk, a Java thread hitting the safepoint polling page will trigger a major page fault. The thread must block while the OS reads the page from disk, stalling the entire JVM.

**Remediation**:
1.  **Memory Pre-touching**: Ensure the JVM commits all memory pages to physical RAM at startup using `-XX:+AlwaysPreTouch`. This forces the OS to allocate physical memory pages immediately, preventing dynamic page fault allocations during execution.
2.  **Lock Heap Pages**: Disable swap on the OS, or use `-XX:+UseLargePages` to lock the heap into physical memory using hugepages, preventing the OS kernel from swapping out JVM pages.
3.  **Disable THP Compaction**: Configure the Linux kernel to use `madvise` for Transparent Huge Pages instead of `always` to prevent background memory compaction from blocking system calls:
    ```bash
    echo madvise > /sys/kernel/mm/transparent_hugepage/enabled
    echo madvise > /sys/kernel/mm/transparent_hugepage/defrag
    ```

To implement these changes in your environment, apply the following production JVM flags.

<script src="https://gist.github.com/mohashari/9412c259c246fe77f8f64de528d0713d.js?file=snippet-7.sh"></script>

## Conclusion: The Zero-Fluff Checklist for Production JVMs

To ensure your high-throughput JVM services do not suffer from silent safepoint freezes, apply this production checklist:

1.  **Monitor TTSP, Not Just GC**: Configure your metrics pipeline to expose total Stop-The-World (STW) duration, and correlate this with reported GC pause times. Any delta between the two represents TTSP sync latency.
2.  **Enable Safepoint Loop Safeguards**: Always run JVM services with `-XX:+UseCountedLoopSafepoints` and `-XX:LoopStripMiningIter=1000` to prevent un-safepointed loops from blocking VM operations.
3.  **Eliminate OS Swapping**: Run JVM containers with swap disabled. Ensure `-XX:+AlwaysPreTouch` is configured to commit memory pages during startup, avoiding runtime page allocation stalls.
4.  **Keep JFR Engaged**: Do not wait for an incident to enable JFR. Run JFR continuously in disk-backed ring buffers with low-overhead event settings. When an alert fires, you have the historical data ready for analysis.
5.  **Use eBPF for Unbiased Telemetry**: When deep troubleshooting is required, use eBPF uprobes to trace `SafepointSynchronize::begin` and `SafepointSynchronize::end` to measure JVM behavior from the kernel.