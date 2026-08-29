---
layout: post
title: "Correlating gRPC Thread Pool Starvation with Linux CFS Bandwidth Throttle Spikes in Production"
date: 2026-08-29 08:00:00 +0700
tags: [grpc, kubernetes, performance, linux, observability]
description: "Diagnose and resolve high p99 latency in containerized gRPC services caused by Linux CFS quota throttling starving multi-threaded worker pools."
image: "https://picsum.photos/seed/2536/1080/720"
thumbnail: "https://picsum.photos/seed/2536/400/300"
---

Your high-throughput gRPC microservice is running comfortably at 30% average CPU utilization, yet your p99 latency is spiking from a baseline of 4ms to an unacceptable 1500ms. Client applications are throwing `DEADLINE_EXCEEDED` errors, cascades of retries are flooding the network, and your downstream services report no slow queries or queue build-up. Standard APM metrics show the JVM or Go runtime is completely healthy, with garbage collection times well under 10ms. You pull a thread dump and find that every single thread in your gRPC worker pool is either blocked or waiting, and the queue of inbound RPC tasks is ballooning. You are witnessing thread pool starvation, but its root cause is not application-level locks or database I/O bottlenecks. It is the silent, aggressive throttling imposed by the Linux Completely Fair Scheduler (CFS) bandwidth controller suspending your container's execution because of micro-bursting, even when your average CPU usage appears completely safe.

![Correlating gRPC Thread Pool Starvation with Linux CFS Bandwidth Throttle Spikes in Production Diagram](/images/diagrams/correlating-grpc-thread-pool-starvation-linux-cfs-bandwidth-throttle-spikes-production.svg)

## The Mechanics of CFS Bandwidth Throttling

To understand why a container with low average CPU utilization gets throttled, you must understand how the Linux Kernel CFS bandwidth controller enforces CPU limits. The controller relies on two key parameters configured in the cgroup settings:
*   `cpu.cfs_period_us`: The enforcement window, typically defaulted to 100,000 microseconds (100ms).
*   `cpu.cfs_quota_us`: The total CPU runtime allowed for all threads in the cgroup within that period.

When you configure a Kubernetes deployment with a CPU limit of `2.0` cores, the container runtime translates this to the cgroup parameters shown in this manifest:

<script src="https://gist.github.com/mohashari/a6a176bff9f78930ffabd51ed9a7d83a.js?file=snippet-1.yaml"></script>

A CPU limit of `2` means the container is allocated 200,000 microseconds (200ms) of CPU runtime per 100ms period. The crucial detail is that this runtime is *cumulative across all threads* running inside that cgroup.

If you deploy a multi-threaded application (such as a gRPC server running on the JVM, Rust's Tokio, or Go) on a node with 64 physical cores, the application runtime detects the host's 64 cores and defaults its thread pool size accordingly. If 32 threads attempt to execute concurrently inside the container, they will consume the allocated 200ms quota in only 6.25ms (32 threads × 6.25ms = 200ms).

For the remaining 93.75ms of that CFS period, the kernel bandwidth controller suspends every single thread in that container. The container is effectively frozen. This phenomenon, known as **micro-bursting**, happens repeatedly every period, resulting in a system that is fully active for a tiny window and completely unresponsive for the rest.

## gRPC Execution and the Starvation Cascade

gRPC servers are highly vulnerable to CFS throttling due to their multi-threaded, asynchronous architecture. Under the hood, gRPC runtimes use non-blocking netty/epoll event loops to accept HTTP/2 connections and read incoming frames. Once a frame is parsed, the event loop dispatches the request payload to a thread pool (e.g., the default executor in gRPC Java or the Go scheduler running on `GOMAXPROCS` threads) to execute the business logic.

When CFS throttling triggers, the following cascade occurs:
1.  **Event Loop Suspension**: The kernel suspends the container. The epoll loop stops reading data off the TCP socket. The TCP receive window on the client side begins to shrink as TCP packets sit in the host queue.
2.  **Worker Thread Freeze**: Worker threads executing active RPCs are suspended mid-execution. A database query might have returned, but the thread cannot wake up to process the result.
3.  **Queue Saturation**: During the brief windows when the container is not throttled, the epoll event loops run at full speed and read all buffered packets. They dump hundreds of requests onto the worker thread pool queue. 
4.  **Starvation**: The worker threads, overwhelmed by the sudden influx of queued requests, consume the next period's CPU quota immediately. The kernel throttles the container again. Because the worker threads spend most of their time suspended, they cannot drain the queue. The queue latency (the time a request spends waiting for a thread) grows exponentially.

A client making an RPC sees a latency spike equal to the number of throttled periods multiplied by 100ms. If a request is delayed by three throttle periods, a 5ms query suddenly takes 305ms.

## Extracting Raw cgroups Telemetry

To prove that your gRPC latency spikes are caused by CFS throttling, you must collect metrics directly from the cgroup filesystems of your host nodes. The Linux kernel maintains throttling statistics in the `cpu.stat` file. 

In cgroups v1, this is located at `/sys/fs/cgroup/cpu/cpu.stat`. In cgroups v2, it is found at `/sys/fs/cgroup/cpu.stat` (or within the specific pod slice). The key metrics to monitor are:
*   `nr_periods`: Total enforcement intervals that have elapsed.
*   `nr_throttled`: Number of periods where the container exhausted its quota and was suspended.
*   `throttled_time`: Total cumulative time the threads were suspended (in nanoseconds).

This bash script runs inside the container (or on the host targeting the container cgroup) to output real-time throttling statistics:

<script src="https://gist.github.com/mohashari/a6a176bff9f78930ffabd51ed9a7d83a.js?file=snippet-2.sh"></script>

If you see `Throttled Periods` spikes above 10% during high-latency events, CFS throttling is actively destabilizing your application.

## Correlating Telemetry via Prometheus and PromQL

To correlate gRPC worker pool saturation with cgroup throttling, configure Prometheus to scrape cAdvisor and your application metrics. 

cAdvisor exposes container cgroup stats with these metrics:
*   `container_cpu_cfs_periods_total`: Total periods elapsed.
*   `container_cpu_cfs_throttled_periods_total`: Number of times the container has been throttled.
*   `container_cpu_cfs_throttled_seconds_total`: Total seconds the container threads were suspended.

To calculate the percentage of periods where your service was throttled, use this PromQL query:

<script src="https://gist.github.com/mohashari/a6a176bff9f78930ffabd51ed9a7d83a.js?file=snippet-3.txt"></script>

To correlate this with thread starvation, graph the throttle rate against the number of active/busy gRPC worker threads. In Go, you can track thread behavior by instrumenting your runtime using custom interceptors to detect scheduling delays.

This Go gRPC unary server interceptor measures the execution overhead by tracking thread runtime metrics. If the wall-clock execution time of an RPC is significantly longer than the actual CPU thread time, it indicates scheduling delays or kernel suspension:

<script src="https://gist.github.com/mohashari/a6a176bff9f78930ffabd51ed9a7d83a.js?file=snippet-4.go"></script>

## Mitigating Throttling and Starvation in Production

Resolving gRPC thread pool starvation requires a multi-layered approach across both your container configuration and application code.

### 1. Match Thread Pools to cgroup Limits
Do not let your application runtime allocate threads based on the host node’s CPU count. If your container's CPU limit is `2.0` cores, restrict your runtime execution pools to 2 threads. 

In Go, the runtime scheduler defaults to spawning threads equal to the host CPU core count. Use the `uber-go/automaxprocs` library to parse the container's cgroup limits and adjust `GOMAXPROCS` automatically.

<script src="https://gist.github.com/mohashari/a6a176bff9f78930ffabd51ed9a7d83a.js?file=snippet-5.go"></script>

For JVM applications, explicitly configure your Netty event loops and custom thread pools:

<script src="https://gist.github.com/mohashari/a6a176bff9f78930ffabd51ed9a7d83a.js?file=snippet-6.txt"></script>

### 2. Tuning the CFS Period (`cpu.cfs_period_us`)
By default, the CFS period is set to 100ms. If your container is throttled, it remains suspended for the remainder of that 100ms window. You can reduce this latency penalty by shrinking the CFS period to 10ms or 20ms.

If your quota limit is `2.0` CPUs and your period is tuned to 10ms, your quota becomes 20ms. If you experience micro-bursting, your threads will exhaust the quota much faster, but the throttling duration is scaled down to a fraction of 10ms. This prevents high queue buildup and flattens your p99 latency curve.

Configure the Kubelet on your host nodes to use a custom CFS period:

<script src="https://gist.github.com/mohashari/a6a176bff9f78930ffabd51ed9a7d83a.js?file=snippet-7.yaml"></script>

### 3. Implement Adaptive Concurrency Limiting
When a container is throttled, it cannot process its queue. To prevent this queue from growing indefinitely and consuming memory, implement adaptive concurrency limiting. Instead of using a fixed-size queue, use a gRPC interceptor that monitors response latencies and dynamically adjusts the maximum number of concurrent requests allowed.

If latency increases (due to throttling or load), the limit drops, and the server immediately sheds load by rejecting requests with `RESOURCE_EXHAUSTED`. This prevents queue starvation and preserves the health of the container.

<script src="https://gist.github.com/mohashari/a6a176bff9f78930ffabd51ed9a7d83a.js?file=snippet-8.go"></script>

### 4. Eliminating CPU Limits entirely
The most robust solution to CFS throttling in production is to disable CPU limits on your container specifications entirely while keeping CPU requests accurate. 

Kubernetes schedules pods based on CPU requests, guaranteeing that your pod has access to its requested resources. If a node has spare CPU cycles, containers without CPU limits are allowed to burst into those unused cycles without triggering CFS throttling. 

To prevent a single runaway container from starving the entire node when limits are disabled, implement Kubernetes' CPU Manager with a `static` policy. This configures exclusive CPU cores for containers in the Guaranteed QoS class (where CPU requests equal CPU limits), preventing CFS throttling while keeping execution isolated.