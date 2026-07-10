---
layout: post
title: "Profiling Heap Allocations in Production Rust Services with Jemalloc and Grafana Pyroscope"
date: 2026-07-10 08:00:00 +0700
tags: [rust, profiling, observability, performance]
description: "Diagnose and resolve memory leaks in production Rust services using Jemalloc heap profiling and continuous profiling with Grafana Pyroscope."
image: "https://picsum.photos/seed/3781/1080/720"
thumbnail: "https://picsum.photos/seed/3781/400/300"
---

It is 3:00 AM, and your pager goes off. A core Rust microservice handling 45,000 requests per second has just been terminated by the Linux kernel's Out-Of-Memory (OOM) killer. You look at your Prometheus dashboard: the resident set size (RSS) has been climbing linearly at a rate of 120 MB per hour for the last two days. The CPU usage is normal, response latencies are steady, and standard `tokio` metrics show no unusual task accumulation. Standard APM metrics tell you *that* you are leaking memory, but they are blind to *what* is allocating it. To diagnose this, you need stack-trace-aware heap profiling. In high-throughput production environments, running instrumented tools like Valgrind or DHAT is out of the question due to their 10x–50x performance degradation. You need a production-grade solution that operates with negligible overhead.

This post will guide you through configuring Jemalloc heap profiling, integrating it with Grafana Pyroscope for continuous profiling, and analyzing production memory leaks without sacrificing service performance.

## The Anatomy of Rust Memory Issues

Rust is celebrated for its memory safety, but safety against undefined behavior does not mean immunity to memory leaks. In Rust, production memory leaks typically manifest in a few distinct ways:
1. **Unbounded Collections and Caches**: Using thread-locals, global `DashMap` instances, or static state caches that grow without eviction policies.
2. **Task Leaks**: Spawning long-lived Tokio tasks that await on channels that are never written to, preventing the task's stack and associated allocations from being freed.
3. **Mismatched Deallocations in FFI**: Calling C libraries via foreign function interfaces where Rust code fails to call the appropriate destructor, leaving C-allocated buffers on the heap.
4. **Memory Fragmentation**: In high-throughput systems, allocator churn can lead to fragmentation where the allocator holds onto pages (virtual memory) that are mostly empty, leading to a high RSS even if the active heap allocation is low.

To trace these issues, we swap the default allocator (usually `system` or `mimalloc`) for `jemalloc`. Jemalloc provides built-in, low-overhead heap profiling by intercepting allocation pathways and statistically sampling them. Instead of recording every single allocation, it records one allocation every $2^N$ bytes (typically 512 KB). This sample-based approach keeps CPU overhead under 2% and memory overhead to a minimum.

## Setting Up Jemalloc for Profiling

To implement heap profiling, we must link the Jemalloc allocator to our binary and enable its profiling features during the build process. We will use the `tikv-jemallocator` crate, which is the standard choice in the Rust ecosystem.

First, configure your `Cargo.toml` and initialize the global allocator in your application entry point.

<script src="https://gist.github.com/mohashari/f52202ad43cc3e512186c123b99efe6f.js?file=snippet-1.txt"></script>

By default, Jemalloc will not compile with profiling code unless the feature flag `profiling` is explicitly enabled. Furthermore, compiled binaries in release mode typically strip debug symbols to reduce binary size. However, without debug symbols, your heap profiles will show raw hex addresses instead of human-readable Rust function names. You must configure Cargo to retain debug symbols in release builds.

Update your `Cargo.toml` profiles:

```toml
[profile.release]
debug = true # Retain line tables and debug symbols
split-debuginfo = "packed" # Recommended for Linux platforms
```

## Simulating a Production Memory Leak

To demonstrate the power of heap profiling, let's write a realistic memory leak pattern. Consider an HTTP service that parses metadata and caches it in a global thread-safe registry. Due to a logical error under specific error paths, expired keys are never pruned from the cache.

<script src="https://gist.github.com/mohashari/f52202ad43cc3e512186c123b99efe6f.js?file=snippet-2.txt"></script>

## Injecting Profiling Controls Programmatically

While you can control Jemalloc via the `MALLOC_CONF` environment variable (e.g., `MALLOC_CONF=prof:true,lg_prof_interval:30`), it is highly beneficial to trigger heap dumps dynamically via API endpoints. This allows you to dump heap states on demand during an incident without restarting the process.

We will use the `tikv-jemalloc-ctl` crate to write a handler that writes the heap profile directly to disk.

<script src="https://gist.github.com/mohashari/f52202ad43cc3e512186c123b99efe6f.js?file=snippet-3.txt"></script>

To run this application with heap profiling active, you must start it with the `MALLOC_CONF` environment variable specifying that profiling should be initialized:

<script src="https://gist.github.com/mohashari/f52202ad43cc3e512186c123b99efe6f.js?file=snippet-4.sh"></script>

The parameter `lg_prof_sample:19` means that Jemalloc will sample one allocation every $2^{19}$ bytes (512 KB). Setting this value too low (e.g. `lg_prof_sample:10`, which is 1 KB) will degrade performance, while setting it too high will miss small but steady memory leaks.

## Continuous Profiling with Grafana Pyroscope

Ad-hoc heap dumping is useful but has limitations: you must know *when* to dump, have SSH or shell access to the production container, and copy the profiles out. Continuous profiling solves this by continuously sampling the heap and shipping stack traces to a centralized dashboard like Grafana Pyroscope.

To integrate continuous profiling, use the `pyroscope` and `pyroscope-jemalloc` crates. The Pyroscope agent spawns a background thread that periodically pulls profile data from Jemalloc and pushes it to your Pyroscope server.

<script src="https://gist.github.com/mohashari/f52202ad43cc3e512186c123b99efe6f.js?file=snippet-5.txt"></script>

To ingest these profiles, you need a running Grafana Pyroscope instance. Below is a minimal production-ready Docker Compose configuration that orchestrates your Rust service and a Pyroscope server.

<script src="https://gist.github.com/mohashari/f52202ad43cc3e512186c123b99efe6f.js?file=snippet-6.yaml"></script>

## Analyzing Ad-Hoc Heap Profiles Offline

If you are not using continuous profiling or need to do fine-grained differential analysis between two points in time, you must analyze raw dump files using `jeprof` (a tool derived from Google's `pprof` specifically tailored for Jemalloc).

Install `jeprof` via your system package manager (e.g., `apt-get install libgoogle-perftools-dev` or through `jemalloc` source build utilities).

To locate memory leaks, use differential profiling. This involves taking a base heap dump shortly after the application starts (and warms up) and a second heap dump after the memory usage has noticeably grown.

<script src="https://gist.github.com/mohashari/f52202ad43cc3e512186c123b99efe6f.js?file=snippet-7.sh"></script>

The output PDF will render a call graph showing the function execution paths. The edges and nodes will be annotated with allocation metrics. The nodes with the thickest borders and largest numbers represent the exact stack frames responsible for the memory growth.

### Understanding `jeprof` Output Modes

When viewing the flamegraph or call graph, pay close attention to the following parameters:
- **`inuse_space`**: The amount of memory currently allocated and active on the heap. This is the primary metric for tracking down leaks.
- **`alloc_space`**: The total cumulative memory allocated since the application started, regardless of whether it has been deallocated. High `alloc_space` but low `inuse_space` indicates high memory churn, which can lead to CPU overhead and fragmentation, but is not a leak.
- **`inuse_objects`**: The count of active objects on the heap. Useful when tracking leaks involving small metadata structures.

## Production Performance Trade-offs

Deploying Jemalloc heap profiling to high-traffic systems requires careful tuning. Let’s evaluate the performance trade-offs:

1. **CPU Overhead**: At a sampling rate of 512 KB (`lg_prof_sample:19`), the CPU overhead is negligible (typically under 0.5%). If your service is allocation-heavy (e.g., parsing massive JSON payloads repeatedly), the cost of grabbing stack traces during samples can rise. If you observe CPU degradation in profiling, increase `lg_prof_sample` to `20` (1 MB) or `21` (2 MB).
2. **Memory Overhead**: Jemalloc must store stack trace metadata in internal structures. This increases your service’s memory footprint slightly. In practice, expect a 1% to 3% memory overhead.
3. **Lock Contention**: The sampling code path must occasionally access global maps to record stack frames. In highly concurrent scenarios where many threads allocate simultaneously, this can cause minor lock contention. Using `tikv-jemallocator` with thread-local caching mitigates this effectively.

## Conclusion

When memory leaks threaten your production Rust services, standard APM metrics will only tell you that your application is dying. By compiling your service with Jemalloc profiling enabled, leaving debug symbols intact, and routing trace data to Grafana Pyroscope, you establish continuous stack-trace visibility into your heap. 

The next time your service experiences a memory spike at 3 AM, you will not have to guess which tokio task or internal cache is responsible. You can open your Pyroscope dashboard, look at the active flamegraph, and pinpoint the exact file and line number causing the leak within seconds.