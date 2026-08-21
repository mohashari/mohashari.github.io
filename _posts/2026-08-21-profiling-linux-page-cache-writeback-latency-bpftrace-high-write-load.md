---
layout: post
title: "Profiling Linux Page Cache Writeback Latency with bpftrace under High Write Load"
date: 2026-08-21 08:00:00 +0700
tags: [linux, bpftrace, performance, writeback, storage]
description: "Diagnose and resolve page cache writeback latency spikes and dirty page throttling under high write load using bpftrace."
image: "https://picsum.photos/seed/3776/1080/720"
thumbnail: "https://picsum.photos/seed/3776/400/300"
---
In high-throughput backend systems, a sudden spike in tail latency (p99/p99.9) is often the precursor to a cascading outage. When executing buffered writes via the standard `write(2)` system call, your application expects these operations to complete almost instantaneously—typically in under 10 microseconds—because it is merely copying data to the Linux page cache in system RAM. However, under sustained high write volumes, a synchronous `write(2)` call can suddenly stall for hundreds of milliseconds or even seconds. Standard monitoring utilities like `iostat` or Prometheus node-exporter will report 100% disk utilization, but they cannot tell you *why* your application threads are blocked or whether the slowdown is caused by hardware queue saturation or kernel-level write throttling. This post dives deep into the Linux page cache writeback architecture, demonstrates how the kernel's dirty page throttling loop functions, and provides production-grade `bpftrace` scripts to isolate and diagnose these critical latency bottlenecks.

![Profiling Linux Page Cache Writeback Latency with bpftrace under High Write Load Diagram](/images/diagrams/profiling-linux-page-cache-writeback-latency-bpftrace-high-write-load.svg)

## Anatomy of the Page Cache Write Path and the Throttling Trap

To understand why a buffered write blocks, we must follow the path of data from user space to physical storage. When an application calls `write(2)` on a file descriptor, the VFS layer invokes the filesystem-specific write operation (e.g., `ext4_file_write_iter`). This function allocates physical memory pages in the page cache, copies the user-space buffer into these pages, and marks them as **dirty**. Under normal conditions, this system call returns immediately. The task of transferring these dirty pages to disk is delegated asynchronously to the kernel's background flusher threads (also known as `bdi_writeback` or flusher daemons, visible in `ps` as `kworker` threads).

However, memory is finite. If the rate of incoming writes exceeds the storage device's physical write rate, dirty pages accumulate in RAM. To prevent the system from running out of memory (OOM), the Linux kernel enforces two main thresholds controlled by sysctl parameters:

1. **`vm.dirty_background_ratio` (or `vm.dirty_background_bytes`)**: When the ratio of dirty memory to total fillable memory exceeds this threshold (defaulting to 10% on many distributions), the kernel wakes up the background flusher threads. These threads write dirty pages back to disk asynchronously without blocking the user application.
2. **`vm.dirty_ratio` (or `vm.dirty_bytes`)**: When the volume of dirty memory exceeds this hard limit (defaulting to 20%), the kernel halts asynchronous behavior. Any thread executing a buffered write is forced into a synchronous throttling loop called `balance_dirty_pages()`.

Inside `balance_dirty_pages()`, the kernel calculates the difference between the current dirty page count and the setpoint. It then forces the writing process to sleep for a computed duration (using `io_schedule_timeout`) to match the dirtying rate with the storage device's actual writeback speed. This is the **dirty page throttling trap**. On modern enterprise servers with 256 GB of RAM, a 20% `dirty_ratio` translates to 51.2 GB of dirty data. If a write burst dirty-fills this buffer, the flusher daemon must flush tens of gigabytes to disk, saturating the block I/O queue. Any subsequent write syscall will block in `balance_dirty_pages()`, causing thread starvation in your application layer.

## Setting Up the Test Bench: Simulating High Write Load

To observe and profile this behavior, we need to intentionally trigger kernel write throttling. We can achieve this by configuring extremely low dirty limits on a test system, then running a heavy buffered write workload using `fio`.

Run the following command sequence to set up the test bench and execute the workload. We will write to `/home/muklis/fio_writeback_test.bin` to ensure the writes hit a physical block device rather than a memory-backed file system like `tmpfs`.

<script src="https://gist.github.com/mohashari/550391b24edf4a36286736b9c0d3bfb6.js?file=snippet-1.sh"></script>

This configuration forces the kernel to start background writeback at 16 MB of dirty memory and block the writing processes at 32 MB. With 4 concurrent jobs pushing 256 KB blocks, the dirty threshold is exceeded instantly, forcing `fio` into the `balance_dirty_pages()` loop.

## Tracing Syscall Latency with bpftrace

The first step in diagnosing write latency is quantifying the time spent inside the `write(2)` syscall from the application's perspective. The `vfs_write` kernel function is the generic entry point for file writes. By using `bpftrace` to hook into `kprobe:vfs_write` and `kretprobe:vfs_write`, we can measure the duration of this function and output a power-of-two histogram of the latency distribution.

Save the following script as `trace_vfs_write.bt` and run it:

<script src="https://gist.github.com/mohashari/550391b24edf4a36286736b9c0d3bfb6.js?file=snippet-2.txt"></script>

When running this script under the `fio` load, you will see output structured like this:

```text
@write_latency_us[fio]:
[2, 4)                |                                       |        0
[4, 8)                |@@@@@@@@@                              |     2034
[8, 16)               |@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@|     8412
[16, 32)              |@@@@                                   |      921
...
[16384, 32768)        |@                                      |      143
[32768, 65536)        |@@@                                    |      487
[65536, 131072)       |@@@@@@                                 |      912
```

The bimodal distribution is clear: while the majority of writes finish in under 16 microseconds (hitting RAM), a significant fraction takes between 32 milliseconds and 131 milliseconds. This tail latency is what degrades system reliability.

## Unmasking Kernel Throttling via `balance_dirty_pages`

Now that we have confirmed high write latency, we must determine if this latency is caused by the block I/O layer itself or if it is kernel-level dirty throttling. To prove that processes are blocking inside `balance_dirty_pages`, we can write a target script that instruments the entry and exit of this kernel function.

<script src="https://gist.github.com/mohashari/550391b24edf4a36286736b9c0d3bfb6.js?file=snippet-3.txt"></script>

Run this script alongside your write workload. If the output contains entries for your application, it is positive proof that your application is being throttled by the OS page cache subsystem:

```text
@throttle_latency_ms[fio]:
[1, 2)                |                                       |        0
[2, 4)                |@@@                                    |      112
[4, 8)                |@@@@@@@@@@@@@@@@@                      |      582
[8, 16)               |@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@|     1204
[16, 32)              |@@@@@@@@@@@@                           |      412
[32, 64)              |@@                                     |       78

@total_throttle_ms[fio]: 24912
```

In this output, `fio` spent a total of 24.9 seconds accumulated across all threads sleeping inside the dirty balance loop, with individual sleep events lasting up to 64 milliseconds.

## Tracing Block I/O Latency to Isolate Hardware Bottlenecks

If `balance_dirty_pages` is not showing significant activity but `vfs_write` is still slow, the bottleneck likely lies lower in the stack, either within the filesystem transaction log (e.g., ext4 journaling blocks on fsync) or at the block device level. We can use block tracepoints to measure the exact time it takes for a block request to be serviced by the underlying hardware (NVMe/SSD).

We hook into `block:block_rq_insert` (when a request is queued to the block driver) and `block:block_rq_complete` (when the hardware controller signals completion via interrupt).

<script src="https://gist.github.com/mohashari/550391b24edf4a36286736b9c0d3bfb6.js?file=snippet-4.txt"></script>

Analyzing the output of this script allows us to perform differential diagnosis:
* **High VFS write latency + High block device latency**: The physical storage controller is saturated, or the hardware queue depth has been exceeded. You are hitting physical device limits.
* **High VFS write latency + Low block device latency + High balance_dirty_pages latency**: The storage hardware is performing well (e.g., block requests complete in 150 microseconds), but the application is dirtying pages faster than the flusher threads can queue them. The bottleneck is the kernel's writeback pacing.

## Analyzing Writeback Flusher Thread Activity

To complete our system-level view, we need to inspect the flusher threads. Are they executing writeback cycles frequently enough, and how much data are they pushing per run? We can trace this using the `writeback` subsystem tracepoints. Specifically, `tracepoint:writeback:writeback_start` and `tracepoint:writeback:writeback_written` expose the lifecycle of writeback jobs.

<script src="https://gist.github.com/mohashari/550391b24edf4a36286736b9c0d3bfb6.js?file=snippet-5.txt"></script>
*(Note: Ensure your kernel headers path in the `#include` directive matches your active kernel version.)*

This script tracks how long each writeback job takes to execute and calculates the sum of all pages written back to disk during the tracing period (each page is typically 4 KB). If you observe long writeback durations (e.g., exceeding 500ms), it indicates that the flusher threads are executing massive, synchronous-like sweeps which will block the request queue for normal application operations.

## Correlating the Pieces and Tuning Linux Writeback

Once your diagnostic data indicates that your application's p99 latency spikes are caused by dirty page throttling, you must adjust the kernel's virtual memory subsystem. The default settings of `vm.dirty_ratio=20` and `vm.dirty_background_ratio=10` are configured for general-purpose workloads, not high-performance backend infrastructure. On a system with large RAM capacity, these percentages allow a massive pool of dirty memory to build up. When the flusher thread eventually activates, it creates a massive I/O wave that saturates storage adapters.

To maintain low latency, you must transition from **percentage-based** limits to **byte-based** limits. This ensures that the absolute volume of dirty memory remains small, resulting in a continuous, smooth stream of writes to the disk rather than large burst flushes.

Create a configuration file at `/etc/sysctl.d/99-writeback-tuning.conf` with the following optimized parameters:

<script src="https://gist.github.com/mohashari/550391b24edf4a36286736b9c0d3bfb6.js?file=snippet-6.txt"></script>

Apply these settings immediately using:

<script src="https://gist.github.com/mohashari/550391b24edf4a36286736b9c0d3bfb6.js?file=snippet-7.sh"></script>

### Explaining the Tuning Trade-offs

When you apply these settings, you are making a deliberate architectural trade-off:

* **Latency vs. Throughput**: By forcing writeback to start at 256 MB and throttling at 512 MB, you reduce the efficiency of disk write-merging. The filesystem has less opportunity to linearize dirty blocks before executing physical writes, which can slightly lower the absolute write throughput of your storage controller.
* **Disk Lifespan (Write Amplification)**: More frequent flushes mean blocks are written to solid-state drives sooner. If your application modifies the same block multiple times within a short window, a high `dirty_expire_centisecs` allows these writes to merge in memory. Reducing this window means more physical writes hit the NAND cells, slightly increasing write amplification.
* **Lower Tail Latency**: In exchange, you eliminate large writeback bursts. The block I/O request queues remain short, leaving ample headroom for synchronous reads (e.g., database index lookups) and preventing your main application threads from blocking inside system calls.

Run your `fio` benchmark and `bpftrace` scripts again with these optimized kernel settings. You should observe that while the maximum throughput might decrease slightly, the massive write latency spikes in `vfs_write` and the occurrences of `balance_dirty_pages` invocation will be eliminated, ensuring stable and predictable latency profiles for your backend applications.