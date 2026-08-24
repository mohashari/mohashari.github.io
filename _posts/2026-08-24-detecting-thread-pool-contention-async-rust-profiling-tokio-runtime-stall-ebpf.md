---
layout: post
title: "Detecting Thread Pool Contention in Async Rust: Profiling Tokio Runtime Stall Latencies with eBPF"
date: 2026-08-24 08:00:00 +0700
tags: [rust, ebpf, tokio, performance, observability]
description: "Identify and resolve Tokio runtime stalls and worker thread pool contention in production using eBPF uprobes and sched_switch tracepoints."
image: "https://picsum.photos/seed/4147/1080/720"
thumbnail: "https://picsum.photos/seed/4147/400/300"
---

Imagine this: it is 3:00 AM on a Friday, and your service's P99 latency suddenly jumps from a clean 5 milliseconds to a crippling 2.5 seconds. The Kubernetes health checks are passing, memory utilization is flat, and CPU consumption is hovering at a modest 40%—seemingly plenty of headroom. Yet, your service is dropping TCP connections, and HTTP throughput has crashed. Traditional profiling tools show everything is normal. You log into a production container, run `top` or a basic CPU profiler, and see nothing out of the ordinary. This is the classic signature of Tokio thread pool contention. A single task, or a small group of tasks, has blocked the cooperative executor threads, starvation-locking the entire runtime. Because the CPU isn't saturated, traditional resource alarms remain silent. To debug this in a production environment without crashing the service, you need to look beneath the runtime and inspect the scheduler directly using eBPF.

## The Mechanics of Async Rust and Cooperative Scheduling

Async Rust relies on a cooperative multitasking model. Unlike Go's runtime, which can preempt long-running goroutines using OS signals (implemented in Go 1.14), Rust's futures are compiled into state machines and must yield control back to the executor at an `.await` boundary. The Tokio runtime multiplexes thousands of logical futures on top of a small, fixed-size pool of OS threads (typically matching the system's logical CPU core count).

If a task contains a synchronous database call, blocks on an OS-level mutex, or processes CPU-intensive data (like compression, serialization, or cryptography) without yielding, the entire worker thread is frozen. If you have an 8-core machine running Tokio with 8 worker threads, it only takes 8 concurrent blocking requests to completely starve the runtime. No other futures multiplexed onto those threads can run, leading to severe head-of-line blocking. 

## Under the Hood: Tokio's Work-Stealing Scheduler and the Budget System

Tokio’s multi-threaded runtime utilizes a work-stealing scheduler. Each worker thread maintains its own local run queue (a circular buffer containing up to 256 tasks) and shares a global run queue for overflow tasks. When a worker thread completes a task and its local queue is empty, it attempts to steal tasks from the local queues of other worker threads or pulls from the global queue. 

To prevent a single task from monopolizing a worker thread via cooperative loops (such as continuously reading from a high-throughput socket), Tokio implements a *cooperative budget* system. Every time a task is polled, it is allocated a budget of 128 units. Every time it performs an asynchronous operation (like writing to a `tokio::net::TcpStream` or sending a message over a `tokio::sync::mpsc` channel), the budget is decremented. When the budget hits zero, the task yields the thread back to the scheduler, even if the underlying resource has more data ready.

However, this budget system only works if the task goes through Tokio's cooperative abstractions. If a task executes a tight loop parsing a 10MB JSON string or calls a blocking synchronous filesystem operation, the budget is never decremented. The scheduler is bypassed, and the worker thread is starved.

## Snippet 1: The Anatomy of a Contended Worker Thread

The following example demonstrates a realistic Axum handler that exhibits two common patterns of runtime starvation: intensive CPU-bound computation and synchronous blocking I/O on the executor.

<script src="https://gist.github.com/mohashari/3165ff8f33e8830704ddd234681dd724.js?file=snippet-1.txt"></script>

## Why Traditional Profiling Fails

Standard observability tools are not designed to detect cooperative scheduling stalls. 

*   **CPU Profilers (`perf`, `flamegraph`)**: These tools sample CPU execution at a specific frequency (e.g., 99 Hz). If a worker thread is blocked on a `std::sync::Mutex` or a synchronous file write, it is swapped out by the kernel scheduler and becomes *off-CPU*. Because it is not running on a CPU core, `perf` does not sample it. The resulting flame graph will show a massive gap or misrepresent the actual bottleneck, making it look like the system is idle when it is actually starved.
*   **Tokio Console**: A dedicated console tool for async Rust. It operates by listening to tracing events emitted by the runtime. While it's fantastic for development, it requires compiling your app with the unstable `tokio_unstable` flag and linking the `console-subscriber` crate. In a high-throughput production environment, this instrumentation can add 15-30% overhead, which makes it unsuitable for permanent production deployment.
*   **Application Metrics (e.g., Prometheus/Grafana)**: While metrics like request latency histograms will tell you *that* your system is slow, they cannot tell you *which* task is stalling the executor, or differentiate between network latency, database latency, and scheduler starvation.

To detect both on-CPU stalls (e.g., heavy serialization) and off-CPU stalls (e.g., blocking system calls) without significant overhead, we must use eBPF.

## Tracing Task Poll Durations with eBPF Uprobes

To identify on-CPU stalls, we must measure the exact time a worker thread spends executing a single `poll` call for a future. We can do this using uprobes (User-space Probes) attached to the entry and return of Tokio’s task poll function.

Because Rust compiles down to native machine code, we can hook into the executor's task harness. Specifically, we want to target `tokio::runtime::task::harness::Harness::poll`. Since Rust uses name mangling, we must extract the mangled symbol name from the binary.

<script src="https://gist.github.com/mohashari/3165ff8f33e8830704ddd234681dd724.js?file=snippet-2.sh"></script>

Once we have the mangled symbol, we can write a `bpftrace` script to dynamically instrument the binary in production, tracking task execution times and building a histogram of poll durations.

<script src="https://gist.github.com/mohashari/3165ff8f33e8830704ddd234681dd724.js?file=snippet-3.txt"></script>

## Building an Off-CPU Monitor for Tokio Worker Threads

While uprobes on task polls are excellent for detecting overall stall durations, they do not tell us *why* a task took so long if it went off-CPU (e.g., waiting for an OS lock or file descriptor). To solve this, we can trace the Linux kernel's `sched_switch` tracepoint.

The `sched_switch` tracepoint fires every time a thread context-switches out of a CPU. We can write an eBPF program that hooks into this tracepoint, checks if the outgoing thread belongs to the Tokio worker pool, and stores a timestamp. When that thread is scheduled back in, we calculate the difference. If the duration exceeds our threshold (e.g., 5 milliseconds), we send a stall event to user-space.

Here is the kernel-space C code for the eBPF tracer.

<script src="https://gist.github.com/mohashari/3165ff8f33e8830704ddd234681dd724.js?file=snippet-4.txt"></script>

## Running the Tracer from User-Space with Aya

To load and interact with our eBPF program, we write a user-space driver in Rust using `Aya`. Aya allows us to compile, load, and consume eBPF maps in a pure Rust codebase without depending on external C libraries.

<script src="https://gist.github.com/mohashari/3165ff8f33e8830704ddd234681dd724.js?file=snippet-5.txt"></script>

## Configuring Cargo to Support eBPF Stack Traces

To make our eBPF and bpftrace scripts useful, we need the compiler to preserve debug symbols and frame pointers in release builds. By default, release builds do not generate frame pointers, which prevents eBPF from walking the user-space stack. We can configure this in our Cargo setup.

<script src="https://gist.github.com/mohashari/3165ff8f33e8830704ddd234681dd724.js?file=snippet-6.toml"></script>

Setting `symbol-mangling-version = "v0"` guarantees a predictable and standardized format (RFC 2603) for our uprobes, making it much easier to locate the mangled poll function without writing complex regex patterns to catch legacy symbols.

## Fixing the Stall: Effective Async-Safe Design Patterns

Once the eBPF tracer identifies a stall, we must refactor the offending code to keep the worker threads free. Here is the refactored, production-safe version of the Axum handler.

<script src="https://gist.github.com/mohashari/3165ff8f33e8830704ddd234681dd724.js?file=snippet-7.txt"></script>

By introducing `spawn_blocking`, Tokio shifts the heavy CPU operation to a separate pool of threads dedicated to blocking work (which can scale up to 512 threads by default). The worker thread remains free to poll other tasks, mitigating the threat of runtime starvation.

Furthermore, switching from `std::sync::Mutex` to `tokio::sync::Mutex` ensures that if lock contention occurs, the calling task yields control back to the executor rather than putting the entire OS thread to sleep.

## Production Trade-offs and Best Practices

While eBPF offers a low-overhead profiling strategy, it has operational constraints that you must consider before deploying it to production:

1.  **Privilege Requirements**: Loading eBPF programs requires `CAP_BPF` or `CAP_SYS_ADMIN` privileges. If you are running your application inside Kubernetes, your trace containers must run in privileged mode or share the host’s PID and network namespace.
2.  **Kernel Version Compatibility**: BTF tracepoints (`tp_btf`) require Linux kernel version 5.4 or higher. If you are running on older kernels, you must fall back to standard kernel tracepoints (`SEC("tracepoint/sched/sched_switch")`), which require parsing structs manually from format files in `/sys/kernel/debug/tracing/events/sched/sched_switch/format`.
3.  **USDT (User Statically Defined Tracing)**: If you control the build environment, compiling Tokio with the unstable feature flags to enable native USDT probes provides even richer profiling data, allowing you to trace task IDs and queue durations directly without relying on uprobes on raw compiler symbols.