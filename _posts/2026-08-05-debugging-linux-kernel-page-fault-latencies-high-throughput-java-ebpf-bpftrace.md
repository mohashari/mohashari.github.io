---
layout: post
title: "Debugging Linux Kernel Page Fault Latencies in High-Throughput Java Applications using eBPF and bpftrace"
date: 2026-08-05 08:00:00 +0700
tags: [java, ebpf, linux-kernel, performance-tuning, latency]
description: "Learn how to isolate and eliminate silent page fault latency spikes in high-throughput JVM applications using eBPF tracing and kernel optimization."
image: "https://picsum.photos/seed/930/1080/720"
thumbnail: "https://picsum.photos/seed/930/400/300"
---

You are running a critical Java microservice handling 50,000 requests per second. The application-level metrics show stable p95 latency at 3ms, but your p99.9 tail latency periodically explodes to 120ms. You check your garbage collection logs—ZGC is running, and maximum GC pause times are under 2ms. Your APM tools show no slow database queries, thread dumps reveal no lock contention, and overall CPU utilization remains at a comfortable 45%. You are dealing with a silent latency killer: kernel-level page fault stalls. When a Java thread accesses a virtual memory address that is not yet mapped to a physical frame, the CPU triggers a page fault exception. The operating system intercepts the thread, transitions to kernel mode, performs memory management tasks—such as page allocation, zeroing, or dynamic compaction—and only then returns control to the JVM. For high-throughput services, these micro-stalls aggregate into unpredictable tail-latency spikes that escape traditional user-space Java observability tools.

## The Virtual Memory Illusion and JVM Allocation Mechanics

To understand why page faults occur in a running JVM, we must look at how the Linux kernel manages virtual memory and how HotSpot interacts with it. The operating system utilizes demand paging to present an illusion of abundant contiguous memory. When the JVM starts and requests a 16GB heap via `-Xms16g -Xmx16g`, the kernel does not immediately allocate 16GB of physical RAM. Instead, it creates virtual memory mappings via the `mmap` system call. The kernel updates the process's page table entries (PTEs) to point to a read-only, shared zero page. The resident set size (RSS) of your Java process remains negligible.

Only when a Java thread attempts to write a new object to a Thread Local Allocation Buffer (TLAB) does the CPU execute a write instruction to a virtual address that lacks a valid physical mapping. The hardware raises a MMU (Memory Management Unit) exception: a page fault. 

The kernel intercepts this exception. Depending on the state of the target memory, the fault falls into one of three categories:

1. **Minor Page Faults:** The page is present in physical memory but is not yet mapped into the process's page tables. This occurs during initial heap usage, dynamic off-heap allocation (e.g., direct byte buffers used by Netty), thread stack creation, or class loading. The kernel retrieves a free physical frame from its buddy allocator, updates the process PTE, zeroes out the page to prevent data leaks (a process called anonymous page allocation), and resumes execution.
2. **Major Page Faults:** The required page is not in physical memory. The kernel must fetch it from disk. In Java applications, this happens when class files (JARs) or memory-mapped files (e.g., Lucene indexes in Elasticsearch) are evicted from the kernel's page cache due to memory pressure, or when JVM heap pages have been swapped to disk. The thread blocks synchronously on disk I/O, introducing latencies ranging from 5ms to over 100ms.
3. **Huge Page Allocation and Compaction Faults:** If Transparent Huge Pages (THP) are enabled, the kernel attempts to allocate 2MB pages instead of the default 4KB pages to reduce PTE overhead. If the kernel's memory zone is fragmented and a contiguous 2MB block is unavailable, the page fault handler triggers *direct compaction* synchronously. The faulting Java thread is paused while the kernel scans memory, relocates active 4KB pages, and defragments the zone to form a huge page. This process is highly CPU-intensive and can block the calling thread for hundreds of milliseconds.

## The Observability Gap: Why Traditional Tooling Fails

Standard Linux diagnostic utilities are designed for aggregate reporting. They provide macroscopic metrics that are useless for isolating low-frequency tail events in high-throughput environments:

* **`/proc/PID/stat` and `/proc/PID/smaps`:** Files like [/proc/self/stat](file:///proc/self/stat) provide cumulative counters for minor faults (`minflt`) and major faults (`majflt`). While you can observe these counts increase, you cannot correlate which specific HTTP request thread triggered the fault, nor can you measure the precise latency duration of individual faults.
* **`vmstat` and `sar`:** These tools report system-wide statistics at coarse intervals (usually 1 to 5 seconds). A burst of 20,000 minor page faults in a 1-second window looks normal, yet if 10 of those faults stalled critical request threads for 50ms each, your p99.9 latency is compromised without visible trace.
* **User-Space Java Profilers:** Profilers that rely on JVM APIs (like JVM TI or JMX) or signal-based sampling (like standard configuration of `async-profiler`) can only capture state transitions within the JVM execution context. When a thread is suspended inside the kernel's `handle_mm_fault` function, the JVM profiler often reports the thread state as `RUNNABLE`. The time spent in the kernel is misattributed to the Java method execution itself, leading engineers to optimize Java code that is already efficient.

To gain absolute visibility, we must trace page faults dynamically from within the kernel using eBPF (Extended Berkeley Packet Filter).

## Tracing Page Fault Latency with bpftrace

The Linux kernel handles virtual memory faults in the `handle_mm_fault` function defined in the kernel's memory management subsystem. By attaching kprobes (kernel dynamic probes) to the entry and return points of `handle_mm_fault`, we can measure the exact execution duration of every page fault.

Below is a production-grade `bpftrace` script that hooks into the kernel memory fault handler. It measures page fault count and aggregates the execution latency in a logarithmic histogram, broken down by the executing thread name.

<script src="https://gist.github.com/mohashari/7cf95703fc54abc1e54236ba3fde0e48.js?file=snippet-1.txt"></script>

When you execute this script on a host running a Java application, the output shows the exact thread names causing faults. In a reactive Netty-based application, you will frequently see Netty event-loop threads (e.g., `epollEventLoopGroup`) experiencing page faults. If these threads experience faults taking longer than 100 microseconds, your pipeline stalls.

## Correlating Kernel Faults to Java Code

Knowing that a page fault occurred is only half the battle. To fix the issue, you must identify the exact Java class and method that triggered the allocation or access. However, mapping kernel stack traces back to Java code presents two major hurdles:

1. **Frame Pointer Omission:** The HotSpot JVM traditionally uses the frame pointer register (`RBP` on x86_64 architectures) as a general-purpose register to increase register availability for JIT-compiled code. Without frame pointers, the kernel's stack walker cannot traverse the userspace call stack, resulting in broken stack traces.
2. **Dynamic JIT Compilation:** The JVM compiles bytecode to machine code in memory at runtime. The kernel has no static symbol table to map these dynamic memory addresses back to Java method names like `com.example.OrderService.create()`.

To resolve these issues, we must configure the JVM to preserve frame pointers and generate an external symbol map file that `bpftrace` can consume at runtime.

First, apply the following flags to your JVM startup configuration file (such as [/etc/default/app.vmoptions](file:///etc/default/app.vmoptions)):

<script src="https://gist.github.com/mohashari/7cf95703fc54abc1e54236ba3fde0e48.js?file=snippet-2.txt"></script>

Second, we must generate a translation map file located at [/tmp/perf-PID.map](file:///tmp/perf-PID.map). The Linux kernel and tools like `perf` or `bpftrace` search for this file when translating address pointers to human-readable symbols for a specific PID. We use `async-profiler` in symbol-dumping mode to create this file without placing overhead on the execution loop:

<script src="https://gist.github.com/mohashari/7cf95703fc54abc1e54236ba3fde0e48.js?file=snippet-3.sh"></script>

With frame pointers enabled and the `/tmp/perf-PID.map` file populated, `bpftrace` can resolve both the kernel stack and the JVM JIT-compiled stack.

## Deep-Dive Diagnostic: Capturing High-Latency Fault Stacks

Now we can write an advanced `bpftrace` script that targets high-latency page faults. In a low-latency service, we want to ignore fast page faults (which complete in under 10-20 microseconds) and capture call stacks only for faults exceeding a strict threshold, such as 500 microseconds.

<script src="https://gist.github.com/mohashari/7cf95703fc54abc1e54236ba3fde0e48.js?file=snippet-4.txt"></script>

Run this script alongside your traffic generator. When a latency spike occurs, the script outputs a comprehensive traceback. Let's analyze a typical output payload:

```text
[ALERT] High-Latency Page Fault detected!
Timestamp: 18446744073709551615
Process:   epollEventLoopG (PID: 10452, TID: 10478)
Latency:   12450 microseconds

--- Kernel Call Stack ---
        handle_mm_fault+0x120
        do_user_addr_fault+0x21a
        exc_page_fault+0x78
        asm_exc_page_fault+0x26
        
--- JVM / Userspace Call Stack ---
        java.io.RandomAccessFile.readBytes+0x3d
        java.io.RandomAccessFile.read+0x28
        sun.nio.ch.FileChannelImpl.read+0x4c
        com.example.search.IndexReader.loadBlock+0x8f
        com.example.search.QueryEngine.search+0x1c4
        com.example.api.SearchController.handleRequest+0xa2
```

The trace tells a clear story: The Netty event loop thread `epollEventLoopG` was blocked for 12.4ms. The userspace stack shows that this was triggered by a file read call in `IndexReader.loadBlock`. The kernel stack trace indicates that the CPU executed `exc_page_fault` followed by `handle_mm_fault` because the mapped file pages were not present in physical RAM. This is a major page fault acting as a blocking synchronous disk read on the main event loop thread.

## The Threat of Transparent Huge Pages (THP)

Transparent Huge Pages (THP) can make page fault latencies significantly worse. In an attempt to reduce TLB (Translation Lookaside Buffer) misses, the Linux kernel groups physical allocations into 2MB pages. 

When a page fault occurs, if `/sys/kernel/mm/transparent_hugepage/enabled` is set to `always`, the kernel's `khugepaged` daemon or the synchronous fault handler tries to locate 2MB of contiguous physical space. If memory is fragmented, the thread undergoes direct compaction. The execution thread calls `compact_zone`, which blocks to rearrange memory.

To determine if compaction is ruining your tail latency, run this script targeting the kernel's memory defragmentation loops:

<script src="https://gist.github.com/mohashari/7cf95703fc54abc1e54236ba3fde0e48.js?file=snippet-5.txt"></script>

If this script triggers output, you are paying a heavy penalty for huge pages. For latency-sensitive Java applications, synchronous compaction must be disabled.

## Mitigating Page Fault Latency in Production

Once you have identified page faults as the root cause of your micro-stalls, you can implement OS and JVM-level modifications to eliminate them.

### 1. Mandatory Heap Pre-touching
By default, the JVM allocates memory lazily. Using the `-XX:+AlwaysPreTouch` flag (shown in **snippet-2**) forces the JVM to iterate over the entire heap during boot, writing a zero byte to every single page. This forces the OS to handle all minor page faults upfront, populating the page tables and locking physical memory frames (RSS) before the application begins accepting traffic. Note that this increases startup time, but completely eliminates heap-allocation page faults during runtime.

### 2. Tuning Transparent Huge Pages (THP)
For databases and low-latency runtimes like Java, THP-induced compaction is a known hazard. You should configure the kernel to either disable THP completely or limit it to memory segments explicitly requested via `madvise`.

### 3. Locking Memory with mlock
To prevent the OS kernel from paging out JVM memory, you can configure the JVM to lock its heap using `mlockall`. This is achieved by adjusting system-level constraints and configuring the JVM to use large pages or explicit pinning.

### 4. Linux Kernel VM Tuning
Create a custom system configuration file at [/etc/sysctl.d/99-latency-tuning.conf](file:///etc/sysctl.d/99-latency-tuning.conf) to optimize memory reclamation and swappiness behaviors.

Here is a comprehensive script to apply these optimizations to your production hosts:

<script src="https://gist.github.com/mohashari/7cf95703fc54abc1e54236ba3fde0e48.js?file=snippet-6.sh"></script>

### 5. Managing Mapped Files (Lucene/Index Files)
If your Java application processes large amounts of read-only mapped files (e.g., Lucene indexes, embedded databases), page eviction from cache will cause major faults. To prevent this:
* Ensure your system has sufficient free physical memory to fit the active working set of files in the kernel's page cache.
* Use tools like `vmtouch` to lock critical data files or class JARs directly into memory on system boot, preventing the OS from evicting them.
* Re-architect file reader threads: ensure blocking page cache reads are handled by dedicated thread pools, rather than your non-blocking netty event loops.

## Summary

When standard metrics report that your application is healthy yet your tail latencies tell a different story, look deeper. By using eBPF and `bpftrace`, you can peer through the abstraction layers of the JVM and examine raw kernel page-allocation behavior. 

1. Write-trace page faults using `kprobes` to pinpoint exact timing.
2. Generate user-space mappings using `async-profiler` and preserve frame pointers.
3. Eliminate heap page faults by configuring `-XX:+AlwaysPreTouch`.
4. Prevent compaction stalls by setting THP to `never`.
5. Optimize kernel parameters to secure your low-latency execution environment.