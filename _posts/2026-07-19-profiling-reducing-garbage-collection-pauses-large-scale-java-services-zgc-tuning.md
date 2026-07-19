---
layout: post
title: "Profiling and Reducing Garbage Collection Pauses in Large-Scale Java Services via ZGC Tuning"
date: 2026-07-19 08:00:00 +0700
tags: [java, garbage-collection, performance-tuning, observability]
description: "A production-focused guide to diagnosing ZGC allocation stalls, analyzing JFR logs, and tuning Java 21+ Generational ZGC for high-throughput workloads."
image: "https://picsum.photos/seed/5014/1080/720"
thumbnail: "https://picsum.photos/seed/5014/400/300"
---

Imagine this: it's peak traffic hour during a promotional flash sale, and your high-throughput Java microservice—handling 150,000 requests per second with a tight p99.9 SLA of 15 milliseconds—suddenly breaches its budget, hitting latency spikes of over 2 seconds. The dashboards show CPU usage is stable, database response times are normal, and memory usage is well within the 96 GB heap allocation. You migrated to Java 21 and enabled the Z Garbage Collector (ZGC) under the assumption that GC pauses would remain strictly sub-millisecond. Yet, a closer examination reveals that under sudden, volatile allocation rates, your application threads are being forcibly blocked by the JVM itself. This is the dreaded *Allocation Stall*—the primary failure mode of ZGC in production. When application threads allocate memory faster than ZGC's concurrent threads can reclaim it, the collector has no choice but to pause the allocating threads (mutators) to prevent running out of memory entirely.

![Profiling and Reducing Garbage Collection Pauses in Large-Scale Java Services via ZGC Tuning Diagram](/images/diagrams/profiling-reducing-garbage-collection-pauses-large-scale-java-services-zgc-tuning.svg)

## Under the Hood: Generational ZGC Core Mechanics

To understand why a concurrent, low-latency garbage collector like ZGC can still pause your application, we must inspect its runtime mechanics. ZGC achieves sub-millisecond pauses by executing almost all of its heavy lifting—marking, relocation, and remapping—concurrently with application threads. 

### Colored Pointers and Load Barriers

The foundation of ZGC's concurrency lies in colored pointers and load barriers. ZGC uses metadata bits embedded directly in the reference pointers (the "color") to track the state of an object reference. Specifically, it uses bits in the upper range of the pointer to denote whether an object has been marked (marked0, marked1), is finalized, or has been relocated (remapped).

When an application thread (a mutator) attempts to read a reference from the heap, a JIT-compiled instruction hook called a **load barrier** intercepts the operation. If the color of the pointer indicates that the object has been relocated but the reference hasn't been updated, the load barrier performs a fast-path lookup in the ZGC forwarding table, updates the reference pointer to the object's new address (remap), and stores it back. This runtime self-healing ensures that mutator threads never see stale pointers, allowing ZGC to relocate objects and compact the heap concurrently.

### The Shift to Generational ZGC (Java 21+)

Prior to JDK 21, ZGC was single-generational. It treated all objects equally, regardless of their age. The garbage collector had to scan and compact the entire heap during each cycle, which was highly inefficient for workloads that adhered to the weak generational hypothesis (the empirical observation that most objects die young). High allocation rates would force single-generational ZGC into continuous cycles, consuming massive amounts of CPU and eventually leading to allocation stalls.

Generational ZGC (JEP 439) solved this by splitting the heap into two logical regions:

1.  **Young Generation**: Designed for high-frequency, low-cost sweeps. It targets short-lived objects (such as HTTP request/response DTOs, JSON deserialization nodes, and database entity wrappers).
2.  **Old Generation**: A larger region scanned less frequently, containing long-lived structures like caches, connection pools, and singleton services.

To track references from the Old to the Young generation without scanning the entire Old generation, Generational ZGC introduces **store barriers**. These barriers intercept writes and log inter-generational reference updates to a thread-local buffer, which is subsequently processed to update ZGC's card table. 

While Generational ZGC significantly reduces the CPU overhead of garbage collection and increases the allocation rate threshold the JVM can handle, it does not make the JVM immune to GC pauses. When the allocation rate surpasses the speed of the concurrent GC thread execution, the boundaries of concurrency break down.

## The Silent Killer: Allocation Stalls vs. Concurrent Mode Failures

When tuning ZGC, senior engineers must distinguish between two primary failure modes: **Allocation Stalls** and **Concurrent Mode Failures**. Both will ruin your tail-latency SLA, but their root causes and metrics are distinct.

### 1. Allocation Stalls
An allocation stall occurs when an application thread attempts to allocate an object in the Young Generation, but the JVM has run out of free pages, and ZGC's concurrent threads have not yet finished reclaiming memory. 

Because the JVM cannot allow the heap to overflow, it blocks the allocating thread. The thread is placed in a waiting state until ZGC frees up a memory page. If your service is a reactive application (using Netty or WebFlux) or uses a limited pool of thread workers, blocking even a few threads can lead to a cascading queue backup, causing a complete system freeze that manifests as a massive tail-latency spike.

```
[Application Mutator] ─── Allocate ───> [No Pages Free] ───> [Blocked/Stalled]
                                                                     │
[ZGC Worker Thread]   ─── Reclaims Page ─────────────────────────────┘
```

### 2. Concurrent Mode Failures
A Concurrent Mode Failure (CMF) occurs when the Old Generation fills up while a concurrent collection cycle is already in progress. This typically happens when objects are promoted from the Young Generation to the Old Generation faster than the Old Generation concurrent sweep can run. 

Unlike a localized allocation stall, a Concurrent Mode Failure can force ZGC to fall back to a full, synchronous, stop-the-world (STW) garbage collection phase to reclaim space. On a 128 GB heap, a synchronous fallback cycle defeats the purpose of ZGC, resulting in pauses that can span seconds.

### The Mathematical Threshold of Stalls

To put this into perspective, let's look at the numbers. Consider a JVM configured with a 64 GB heap, running at a steady-state allocation rate of 8 GB/s. 

$$\text{Time to fill young generation} = \frac{\text{Young Gen Size}}{\text{Allocation Rate}}$$

If the Young Generation is dynamically sized at 12 GB, it will be completely exhausted in:

$$\frac{12 \text{ GB}}{8 \text{ GB/s}} = 1.5 \text{ seconds}$$

If ZGC takes 1.8 seconds to complete a concurrent collection cycle (including marking, relocating, and page reclamation), the JVM will run out of space 0.3 seconds before the cycle finishes. In that 300-millisecond window, every thread attempting to allocate an object will stall.

## Step-by-Step Profiling Guide: Spotting ZGC Stalls in Production

Diagnosing ZGC pauses requires structured telemetry. You cannot rely on APM tools like Datadog or New Relic alone, as they often aggregate GC metrics over 10-second or 1-minute intervals, completely smoothing over transient 500ms stalls.

### 1. Enabling Unified JVM Logging
To get precise diagnostics, you must configure the JVM to output structured GC logs. Add the following command-line flags to your startup script:

```bash
-Xlog:gc*,gc+phases=debug:file=/var/log/jvm/gc-%t.log:time,uptime,pid,level,tags:filecount=5,filesize=100M
```

Let's break down this configuration:
*   `gc*`: Enables all logging tags starting with "gc" (e.g., `gc,heap`, `gc,metaspace`).
*   `gc+phases=debug`: Captures sub-phase timings, which are critical for identifying where ZGC is spending time (e.g., Pause Mark Start, Pause Mark End).
*   `filecount=5,filesize=100M`: Prevents disk space exhaustion via log rotation.

#### Parsing the Log Output
When an allocation stall occurs, you will find entries similar to this in your log file:

```text
[2026-07-19T19:02:11.890+0700][3412.124s][info][gc] GC(142) Garbage Collection (Allocation Stall) 91420M->12042M(98304M) 1650.124ms
```

Key fields to extract:
*   `Garbage Collection (Allocation Stall)`: The explicit trigger reason.
*   `91420M->12042M(98304M)`: Heap occupancy before the cycle, after the cycle, and the total reserved heap size. Notice that the heap was at 93% capacity when the stall was recorded.
*   `1650.124ms`: The duration of the GC cycle, which directly maps to the duration that mutator threads were blocked.

### 2. JDK Flight Recorder (JFR) Profiling
JDK Flight Recorder has a negligible overhead (<1% CPU in production) and captures deep JVM internal events. To start a recording on a running production JVM without restarting the process, use `jcmd`:

```bash
jcmd <PID> JFR.start name=zgc_debug settings=profile duration=120s filename=/tmp/zgc_profile.jfr
```

Once the recording is complete, copy the `.jfr` file to your local machine and open it with JDK Mission Control (JMC). Alternatively, if you run workloads on Kubernetes, you can automate this using Cryostat.

In JMC, navigate to the **Event Browser** and filter for the following events:

| Event Name | Description | Key Fields to Inspect |
| :--- | :--- | :--- |
| `jdk.GarbageCollection` | Records overall GC cycle statistics. | `Cause`: Look for "Allocation Stall". |
| `jdk.GCPhasePause` | Tracks the precise Stop-The-World phases. | `Duration`: Ensure handshakes are sub-ms. |
| `jdk.OldObjectSample` | Used for leak detection and tracking allocation paths. | `Allocation Time`, `In-Use Memory`. |
| `jdk.SafepointBegin` | Measures how long it takes to bring threads to a safepoint. | `Safepoint Limit Time`: Detects runaway loops. |

If you see `jdk.GarbageCollection` events with `Allocation Stall` as the cause, it indicates that the collector is under-provisioned or initiated too late.

### 3. Profiling Allocations with async-profiler
If ZGC is struggling to keep up with allocations, you must identify what code paths are generating the load. Use `async-profiler` to generate an allocation flame graph:

```bash
./asprof -e alloc -d 60 -f /tmp/allocation_profile.html --alloc 512k <PID>
```

*   `-e alloc`: Profiles heap allocations.
*   `--alloc 512k`: Filters out small allocations to focus on high-frequency paths.

Open the resulting HTML file. Look for wide towers representing libraries or frameworks that create garbage needlessly. Common culprits include:
*   **JSON Parsers**: High serialization rates using jackson-databind without object-mapper reuse.
*   **Logging Statements**: Serializing objects inside debug logging blocks that are ultimately filtered out: `log.debug("User data: " + user.toString())`.
*   **HTTP Clients**: Allocating large byte buffers or strings for HTTP payload responses instead of streaming them directly.

## Practical ZGC Tuning: Pragmatic Knobs that Move the Needle

While ZGC is designed to be self-tuning, its default heuristics are optimized for generic workloads, not tail-latency-sensitive high-throughput services. If profiling indicates allocation stalls, apply these production-hardened tunables.

### 1. Adjusting Allocation Spike Tolerance
The most direct way to resolve allocation stalls is to force ZGC to start its concurrent cycles earlier, leaving more free headroom to absorb traffic spikes.

```bash
-XX:ZAllocationSpikeTolerance=4
```

*   **Default Value**: `2`
*   **How it Works**: The JVM uses this factor to calculate the acceleration rate of memory allocation. A higher number makes ZGC more conservative, anticipating larger and sudden spikes. If the allocation rate rises quickly, ZGC will trigger a collection cycle when the heap has significantly more free space remaining.
*   **Trade-off**: The GC will run more frequently, which consumes slightly more background CPU. If your host is already CPU-constrained, this can degrade performance.

### 2. Manual Worker Thread Tuning
By default, ZGC allocates a portion of CPU cores to its concurrent threads:

$$\text{Default ZGC Workers} \approx \max\left(1, \left\lfloor \frac{\text{CPU Cores}}{8} \right\rfloor\right)$$

For instance, on a 32-core machine, ZGC will allocate 4 threads to the concurrent GC phases. If your allocation rate is high, 4 threads might not be fast enough to mark and relocate pages.

You can override this setting:

```bash
-XX:ZWorkers=6
```

#### The Container CFS Quota Pitfall (Kubernetes)
In Kubernetes environments, setting `ZWorkers` incorrectly can be disastrous due to Completely Fair Scheduler (CFS) throttling. 

If you configure a Pod with a CPU limit of `16` but it runs on a physical node with `128` cores, the JVM (if not configured with `-XX:+UseContainerSupport`) may auto-detect the host CPU count and spin up 16 ZGC worker threads. When these concurrent threads fire up alongside your 16 application threads, they will exceed the Kubernetes CFS quota. The kernel will throttle the entire container, starving the GC threads of CPU. Consequently, the GC cycle slows down, triggering a massive allocation stall.

**The Fix:** Always align the active processor count with your Kubernetes resource limits:

```bash
-XX:ActiveProcessorCount=16
-XX:ZWorkers=4
```

### 3. Sizing Heap and Preventing Page Swapping
Dynamic heap sizing is a common source of latency jitter. If the JVM has to request new memory pages from the OS kernel during runtime, those syscalls can pause the JVM.

*   **Set Min and Max Heap to Equal Values**:
    ```bash
    -Xms64g -Xmx64g
    ```
*   **Pre-Touch the Heap**:
    Use `-XX:+AlwaysPreTouch` to force the JVM to map and zero out every memory page during startup, rather than lazily doing it during runtime.
    ```bash
    -XX:+AlwaysPreTouch
    ```
*   **Transparent Huge Pages (THP)**:
    Ensure Transparent Huge Pages are disabled on your Linux hosts. THP can cause unpredictable latency spikes when the kernel tries to defragment memory pages (khugepaged).
    ```bash
    echo never > /sys/kernel/mm/transparent_hugepage/enabled
    echo never > /sys/kernel/mm/transparent_hugepage/defrag
    ```

## Real-world Case Study: Fixing a 2-Second p99.9 Spike on a 250k RPS Service

Let’s look at a concrete production incident involving a session-routing gateway service.

### The Setup
*   **JDK**: Eclipse Temurin 21.0.2
*   **JVM Flags**: `-XX:+UseZGC -XX:+ZGenerational -Xms48g -Xmx48g`
*   **Environment**: Kubernetes pod with 24 CPU cores limit, 64GB RAM limit.
*   **Load**: 250,000 requests per second at peak.

### The Symptom
Every evening at 20:00 UTC, as traffic rose to its peak, the service experienced intermittent latency spikes. The p99.9 latency jumped from 8ms to 2.2 seconds. Upstream API gateways started reporting 504 gateway timeouts, causing client retries, which exacerbated the load.

### The Diagnosis
We fetched the GC logs and isolated the timestamp of the latency spikes:

```text
[2026-07-19T20:01:45.312+0000][28412.012s][info][gc] GC(842) Garbage Collection (Allocation Stall) 45912M->8101M(49152M) 2110.45ms
```

The GC logs confirmed a 2.1-second Allocation Stall. Next, we reviewed the container's CPU metrics and found severe CPU throttling (CFS throttling metric in Prometheus reached 40% during peak).

We took a 60-second JFR capture during the start of the peak period. The JFR events showed:
1.  `jdk.GarbageCollection` showed the allocation rate spiking from **1.5 GB/s to 9.2 GB/s** within 5 seconds.
2.  `jdk.SafepointBegin` showed high safepoint bring-up times, indicating that threads were struggling to reach safe points because they were being throttled by the OS scheduler.

Because the container was configured with `ActiveProcessorCount=24` (detected dynamically), ZGC had spawned 3 concurrent workers. However, due to the high allocation spike, 3 worker threads could not keep pace with 9.2 GB/s of object creation.

### The Resolution
We applied a three-pronged remediation strategy:

1.  **Tuning ZGC Scheduling**:
    We increased the spike tolerance and explicitly tuned the worker threads to ensure reclamation finished before the heap was exhausted.
    ```bash
    -XX:ZAllocationSpikeTolerance=5
    -XX:ZWorkers=6
    ```
2.  **Addressing Container Throttling**:
    We verified that the Pod limits had sufficient headroom, increasing CPU limits to `32` while keeping the request target at `24` to prevent CFS throttling during ZGC concurrent cycles.
3.  **Code-Level Allocation Reduction**:
    We ran `async-profiler` and identified that the Jackson JSON library was recreating a new `ObjectMapper` and serializer configuration on every incoming request, allocating millions of `char[]` and `HashMap$Node` instances. We refactored the code to use a shared, thread-safe static instance of `ObjectMapper`, which reduced the allocation rate at peak from **9.2 GB/s to 3.8 GB/s**.

### The Outcome
After deploying these changes, the allocation stalls disappeared from the logs. 

During the next evening's peak, the p99.9 latency remained rock-solid at **12ms**, and CPU throttling dropped to 0%.

---

## Cheat Sheet: Production-Ready ZGC JVM Template

For high-throughput, low-latency Java services running on JDK 21+ with Generational ZGC, use the following base template for your JVM arguments:

```bash
# Core GC Engine Selection
-XX:+UseZGC
-XX:+ZGenerational

# Memory Layout and Pre-touching
-Xms64g
-Xmx64g
-XX:+AlwaysPreTouch
-XX:+UseNUMA

# Spiking Mitigation & Thread Sizing
-XX:ZAllocationSpikeTolerance=4
-XX:ZWorkers=6
-XX:ActiveProcessorCount=24

# JVM Logging Configurations
-Xlog:gc*,gc+phases=debug:file=/var/log/jvm/gc-prod.log:time,uptime,pid,level,tags:filecount=10,filesize=100M

# Safety Guards
-XX:+UnlockDiagnosticVMOptions
-XX:+GuaranteedSafepointInterval=0
```