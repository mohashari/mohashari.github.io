---
layout: post
title: "Correlating Linux Page Cache Eviction Spikes with Application Query Latency Using eBPF and Prometheus"
date: 2026-08-13 08:00:00 +0700
tags: [ebpf, prometheus, linux, observability, performance]
description: "Diagnose mysterious database query latency spikes by correlating page cache evictions with application latency using eBPF and Prometheus."
image: "https://picsum.photos/seed/104/1080/720"
thumbnail: "https://picsum.photos/seed/104/400/300"
---

It is the classic production nightmare: your database p99 query latency spikes from a crisp 2ms to a crippling 250ms. You check the usual suspects—CPU utilization is sitting comfortably at 45%, network throughput is nowhere near saturating the interface, and your database connection pool has plenty of head room. Even your aggregate disk I/O metrics (IOPS and throughput) look deceptively normal. Yet, application threads are stalling, waiting on queries that should be instantaneous. The culprit is often page cache eviction—Linux silently dropping clean pages containing database indexes or hot table data from memory to satisfy a sudden allocation request from a background process. In this post, we will build a low-overhead eBPF-based tracing system to track page cache evictions at the kernel level, export them to Prometheus, and correlate them directly with application-level query latency spikes.

## Why Page Cache Eviction is the Silent Killer of Query Performance

Relational databases like PostgreSQL rely heavily on the operating system page cache. This is known as "double caching." The database allocates a fixed buffer pool (`shared_buffers` in PostgreSQL, typically set to 25% of system RAM), while relying on the Linux page cache to cache the remaining hot data and indexes. 

Under the hood, Linux manages physical memory pages using two zone-based Least Recently Used (LRU) lists: the active list and the inactive list. When the kernel needs to allocate physical memory (e.g., anonymous pages for a process heap or cache pages for a new file read) and the system's free memory falls below the kernel's low watermark (`watermark[WMARK_LOW]`), the page frame reclaiming algorithm (PFRA) is triggered. This wakes up `kswapd` for background reclamation. If memory pressure is severe enough to hit the min watermark (`watermark[WMARK_MIN]`), the allocating process enters "direct reclamation," blocking its own execution context to free up pages.

During reclamation, the kernel evaluates pages on the inactive LRU list. Clean, file-backed pages (such as read-only database index pages) are target number one. Because they are clean, they do not need to be written back to disk before being reclaimed. The kernel can discard them immediately by calling `__delete_from_page_cache()`. 

While this operation is fast for the kernel, it is incredibly expensive for your database. The next query that attempts to read that index must perform a synchronous page fault, triggering a block I/O read from physical storage (SSD/NVMe). 

Consider the math:
* **Page Cache Hit (RAM):** ~100 nanoseconds
* **NVMe SSD Random 4KB Read:** ~100 microseconds (100,000 ns)
* **High-Latency Cloud Block Storage (EBS GP3 under load):** ~2–10 milliseconds (2,000,000–10,000,000 ns)

If a nested-loop join query requires 2,000 index page lookups, and those pages are cached in RAM, the query completes in under a millisecond. If those page cache lines have been evicted, the query is forced to make hundreds of synchronous block reads, blowing query execution time up to several seconds.

## The Limits of Conventional Tools

Standard Linux observability tools fail to diagnose this issue with precision:
* `free` and `/proc/meminfo` show global, aggregate memory consumption. They tell you that `buff/cache` is high, but they cannot tell you if active database index pages are being evicted.
* `vmstat` provides counters like `pgpgin` and `pgpgout` (pages paged in/out) or `pgsteal_kswapd` and `pgsteal_direct` (pages reclaimed). This proves that reclamation is happening, but it lacks file and process context.
* `iotop` or `sar -d` shows disk read rates, but they cannot distinguish between a query performing a legitimate sequence scan on a cold table versus a query stalling due to cache misses on what should be hot index pages.

To debug this effectively, we must answer three questions in real-time:
1. **What** specific files (and database relations) are losing their page cache?
2. **Who** (which process) is allocating memory and forcing the kernel to evict pages?
3. **When** do these evictions align with application-level latency anomalies?

To achieve this without degrading database performance, we turn to eBPF.

## Tracing Cache Eviction at the Kernel Level

In modern Linux kernels, the tracepoint `tracepoint/filemap/mm_filemap_delete_from_page_cache` is fired whenever a page is removed from the page cache. This tracepoint exposes the target `struct page`, the device identifier (`dev_t dev`), and the inode number (`ino_t ino`).

We will write a C-based eBPF program utilizing BPF CO-RE (Compile Once – Run Everywhere). The program hooks into this tracepoint, extracts the inode and device ID, captures the calling command (`comm`), and sends this telemetry to user space via a perf ring buffer.

<script src="https://gist.github.com/mohashari/22daa330d8bc920d733a0f2440196227.js?file=snippet-1.txt"></script>

This kernel-space program is lightweight. It runs in nanoseconds, performing simple register copies and pushing the resulting struct to the ring buffer, introducing negligible overhead even on highly active database servers.

## Building the Prometheus Exporter in Go

Next, we write a user-space Go program using the `cilium/ebpf` library to load our eBPF program, attach it to the tracepoint, read events from the perf buffer, and expose them as a Prometheus metrics endpoint.

<script src="https://gist.github.com/mohashari/22daa330d8bc920d733a0f2440196227.js?file=snippet-2.go"></script>

> [!WARNING]
> Exporting raw inode numbers as labels to Prometheus can lead to a **metrics cardinality explosion** if your application creates and deletes temporary files constantly. In production, you should filter metrics dynamically in the Go exporter, discarding events for inodes that do not belong to block devices mounting database directories.

## Resolving Inodes to Database Objects

The Prometheus metrics export device IDs and inode numbers. To resolve an inode to a physical file name and then map it to a database table or index, we run an out-of-band resolution step. Resolving pathnames inside the eBPF kernel program is extremely expensive and unstable; handling this asynchronously in user space is the standard production pattern.

First, we locate the file on disk using the major:minor device ID (e.g. `/dev/nvme0n1p1` mapped to device major `259`, minor `1`) and the target inode:

<script src="https://gist.github.com/mohashari/22daa330d8bc920d733a0f2440196227.js?file=snippet-3.sh"></script>

If the database is PostgreSQL, the file path returned will look like `/var/lib/postgresql/data/base/16384/12423`.
* `16384` is the Database OID.
* `12423` is the Relation Filenode (`relfilenode`).

Now, we connect to the database instance with OID `16384` and query the system catalog to determine the exact table or index that was evicted:

<script src="https://gist.github.com/mohashari/22daa330d8bc920d733a0f2440196227.js?file=snippet-4.sql"></script>

This query instantly identifies whether the kernel discarded a high-traffic table (like `orders`) or a critical index (like `idx_orders_user_id`), explaining why queries targeting that relation suddenly stalled.

## Instrumenting the Application for Query Latency

To prove that cache evictions are directly impacting application latency, we must collect p99 latency metrics on the client side. Server-side database execution statistics (such as PostgreSQL's `pg_stat_statements`) record *internal* runtimes, ignoring driver serialization time and network queuing. 

Here is how you wrap a Go application's database access with Prometheus histograms:

<script src="https://gist.github.com/mohashari/22daa330d8bc920d733a0f2440196227.js?file=snippet-5.go"></script>

## Correlating Telemetry in Prometheus

With both the kernel-level page cache eviction metrics and the application query latency metrics arriving at Prometheus, we can write a PromQL query for our Grafana dashboards to plot the correlation.

<script src="https://gist.github.com/mohashari/22daa330d8bc920d733a0f2440196227.js?file=snippet-6.txt"></script>

If you visualize these two queries together, a page cache eviction storm appears as a vertical spike in the first panel, followed immediately by a corresponding spike in the p99 query duration panel. If the eviction command (`comm`) is `kswapd0`, you know the host is running out of memory globally. If it is a process like `gzip`, `tar`, or `docker-backup`, you have identified the culprit that stole your database's memory.

## Setting Up High-Fidelity Alerting

Alerting on either metric in isolation leads to false positives:
* Alerting on cache evictions alone causes fatigue; the operating system evicts pages regularly, which is perfectly safe if those pages contain cold data.
* Alerting on p99 latency spikes alone does not point to the root cause, leading to tedious debugging.

Instead, we configure a Prometheus alert that only triggers when a p99 latency spike *coincides* with high page cache evictions on the database host.

<script src="https://gist.github.com/mohashari/22daa330d8bc920d733a0f2440196227.js?file=snippet-7.yaml"></script>

## Remediation Strategies for Production

Once you correlate latency spikes with page cache evictions, how do you prevent them?

### 1. Memory Isolation via cgroups v2

The most robust solution is isolating the database process using Linux control groups (cgroups v2). By configuring systemd or editing cgroup slices, you can protect the memory allocated to your database and restrict utility processes:

* Set `memory.min` or `memory.low` on your database container/service slice. This tells the kernel's memory reclaimer that this memory is off-limits unless absolutely necessary.
* Place utility processes (such as backup runners, log shippers, and monitoring daemons) inside a restricted system slice with a strict `memory.max` and lower `memory.high` throttling limits.

### 2. Tweak Virtual Memory Swappiness

By default, Linux has a swappiness setting (`vm.swappiness`) of `60`. This tells the kernel to reclaim anonymous pages (application heaps, by swapping them to disk) and page cache pages at a balanced ratio. 
* On database nodes, lower `vm.swappiness` to `10` or even `1`. This instructs the kernel to reclaim anonymous memory aggressively and preserve the file page cache. 
* Do not set it to `0` unless you want to trigger immediate Out-Of-Memory (OOM) kills when memory is exhausted.

### 3. Adjust Writeback Watermarks

If evictions are triggered by dirty page flushes, adjust the following sysctl settings:
* `vm.dirty_background_ratio`: Lower this to `5%` (default is `10%`). This starts background disk flushing earlier, preventing a large pile of dirty pages from building up and forcing aggressive eviction passes.
* `vm.dirty_ratio`: Lower this to `10%` (default is `20%`) to force active writers to block and flush pages, smoothing out memory usage spikes.

### 4. Lock Critical Index Files with `vmtouch`

If you have a small, critical table or index that must stay cached in memory at all costs, use the `vmtouch` utility to pin it to RAM using the `mlock(2)` system call:

<script src="https://gist.github.com/mohashari/22daa330d8bc920d733a0f2440196227.js?file=snippet-8.sh"></script>

This prevents the kernel from evicting those specific physical blocks, protecting your database's highest-priority pathways.