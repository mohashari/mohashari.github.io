---
layout: post
title: "Detecting CPU Throttling in Containerized Go Applications using eBPF and cgroupv2 Controller Metrics"
date: 2026-08-17 08:00:00 +0700
tags: [go, ebpf, kubernetes, performance, cgroups]
description: "A deep dive into detecting and tracing container CPU throttling in Go services using cgroupv2 metrics, eBPF scheduler tracing, and runtime tuning."
image: "https://picsum.photos/seed/4474/1080/720"
thumbnail: "https://picsum.photos/seed/4474/400/300"
---

Imagine this: your Go-based microservice is running under heavy load in a Kubernetes cluster. Average CPU utilization hovers around a comfortable 40%, memory usage is flat, and database connections are healthy. Yet, your p99 latency spikes from 15ms to over 800ms, triggering upstream timeouts and cascade failures. In your metrics dashboards, the service looks completely idle during these latency spikes, leading to frantic and fruitless database queries and network diagnostics. What you are witnessing is Completely Fair Scheduler (CFS) quota throttling. Under the hood, the Linux kernel has paused your container's threads because it exceeded its allocated CPU budget within a micro-window, leaving the Go runtime temporarily frozen. This post details why standard container metrics hide this behavior, how to parse cgroupv2 stats with zero memory allocations, how to trace the exact duration of these pauses using eBPF, and how to configure the Go scheduler to prevent throttling entirely.

## The Mechanics of CFS Throttling in cgroupv2

Kubernetes utilizes Linux cgroups (control groups) to enforce resource boundaries. In modern container runtimes, this is governed by cgroupv2, which establishes a unified hierarchy for resource controllers. When you define a CPU limit in your Pod spec (e.g., `limits.cpu: 2`), the container runtime translates this into CFS quota parameters in the kernel:

1. **CFS Period (`cpu.cfs_period_us`):** The enforcement window, which defaults to 100,000 microseconds (100ms).
2. **CFS Quota (`cpu.cfs_quota_us`):** The total CPU time all threads in the cgroup can collectively consume within that period. A limit of 2 CPUs translates to a quota of 200,000 microseconds (200ms).

The Completely Fair Scheduler allocates this quota across all running threads within the cgroup. For a single-threaded application, CPU usage is sequential and easily bounded. However, Go is inherently multi-threaded. By default, the Go runtime sets `GOMAXPROCS` to the number of logical CPUs available on the host machine. If your Kubernetes node has 64 cores, your Go container defaults to `GOMAXPROCS=64`, spawning at least 64 OS threads to execute goroutines.

If a burst of requests hits the container, Go will distribute work across these threads. If 32 threads run concurrently, they consume the container's CPU quota at 32 times the speed of a single core. 

To exhaust a 2-core (200ms) quota, 32 threads only need to run for:

$$\text{Time to exhaust} = \frac{200\text{ ms}}{32} = 6.25\text{ ms}$$

In just 6.25ms of the 100ms period, your application has consumed its entire CPU allowance. The kernel's CFS scheduler immediately intervenes, putting all threads in the cgroup to sleep. The application is completely frozen for the remaining 93.75ms of the period. If this cycle repeats, requests experience compounding delays, turning sub-millisecond execution times into hundreds of milliseconds of queueing latency.

Because standard APM agents and Prometheus scrapers typically sample CPU utilization every 15 to 30 seconds, they average out these micro-bursts. The resulting metrics show a flat 40% CPU utilization, completely obscuring the fact that the application was frozen for 90% of every scheduler period.

## Parsing cgroupv2 Controller Metrics in Go with Zero Allocations

To monitor this problem, we must check the kernel's cgroup controller statistics. In cgroupv2, these statistics are located at the path [cpu.stat](file:///sys/fs/cgroup/cpu.stat) (or the specific pod slice subdirectory). The file contains key-value pairs showing the total CPU usage, the number of periods, the number of throttled periods, and the total throttled duration:

```
usage_usec 48102948
user_usec 32019488
system_usec 16083460
nr_periods 10240
nr_throttled 824
throttled_usec 74920000
```

When implementing a collector to poll these metrics at high frequencies (e.g., every 1 second), we must avoid triggering Go's garbage collector. Standard parsers that use string splitting, regular expressions, or scanner allocations will generate short-lived objects on the heap, adding GC sweep latency to an already throttled application.

The following implementation is a highly optimized, zero-allocation parser that reads [cpu.stat](file:///sys/fs/cgroup/cpu.stat) using a pre-allocated byte buffer and parses integers directly from raw bytes.

<script src="https://gist.github.com/mohashari/b127979c9b2280c267fa4348a3b3abc0.js?file=snippet-1.go"></script>

## The Visibility Gap

While cgroup controller metrics tell us *that* throttling occurred over a period, they are aggregate counters. They present several blind spots for incident response:

* **Lagging Telemetry:** You only know throttling happened after the scrape interval completes. If a container is throttled for 100ms, you will only see the `throttled_usec` counter increase at the next 15-second metric collection window.
* **No Request Context:** You cannot correlate a throttling event directly with the HTTP/gRPC request that caused it. You don't know if the throttle was triggered by a heavy JSON parsing routine, a tight loop in a cryptographic function, or an expensive SQL serialization step.
* **Lack of Attribution:** On a Kubernetes node hosting multiple containers, cgroup metrics won't tell you if a container was throttled because of its own behavior, or if host-level scheduler CPU migration delays worsened the pause.

To obtain real-time, event-driven visibility, we must tap into the scheduler queues within the kernel. We can achieve this using eBPF.

## Real-Time Throttling Tracing with eBPF

To measure the exact duration of scheduler pauses, we can instrument the CFS scheduler functions in the kernel. The Linux scheduler enforces quota limits using two primary functions inside `kernel/sched/fair.c`:

* `throttle_cfs_rq()`: Called when a CFS runqueue (`cfs_rq`) consumes its entire quota, prompting the kernel to remove the tasks from the global runqueue and place them on a throttled list.
* `unthrottle_cfs_rq()`: Called at the start of a new period (or when extra runtime is redistributed) to return the throttled tasks to the runqueue.

By placing `kprobes` on these functions, we can calculate the duration of every throttling event. We save the start time of the throttling event in a BPF hash map keyed by the address of the `cfs_rq` structure, and calculate the delta when the unthrottle function triggers.

The following C program implements the eBPF tracer:

<script src="https://gist.github.com/mohashari/b127979c9b2280c267fa4348a3b3abc0.js?file=snippet-2.txt"></script>

To load and read this eBPF program, we use the `cilium/ebpf` package. The Go application loads the compiled ELF object into the kernel, hooks the kprobes, and starts an event loop to consume throttling events from the ring buffer.

<script src="https://gist.github.com/mohashari/b127979c9b2280c267fa4348a3b3abc0.js?file=snippet-3.go"></script>

## Tuning GOMAXPROCS and Monitoring Go Scheduler Latency

If you observe cgroup throttling or capture eBPF throttling events, the first step is adjusting the relationship between your application threads and the container's CPU quota.

Go's runtime maps Goroutines (`G`) to OS threads (`M`) using logical processors (`P`). If `GOMAXPROCS` (which determines the number of `P` structures) is significantly higher than the fractional CPU quota allocated to the container, the Go scheduler will overcommit resources, leading to immediate CFS throttling.

To resolve this, we can programmatically read `/sys/fs/cgroup/cpu.max`, calculate the allowed fractional quota, and adjust `GOMAXPROCS` accordingly. We also want to monitor the Go scheduler's internal run-queue latency using the Go [runtime/metrics](file:///usr/local/go/src/runtime/metrics) package to verify that threads aren't spending excessive time waiting to execute.

<script src="https://gist.github.com/mohashari/b127979c9b2280c267fa4348a3b3abc0.js?file=snippet-4.go"></script>

## Exposing Telemetry via Prometheus

To aggregate this data across your cluster, you should expose these metrics using a custom Prometheus collector. This collector exposes the values parsed from `/sys/fs/cgroup/cpu.stat` alongside real-time throttling metrics sourced from the eBPF engine.

<script src="https://gist.github.com/mohashari/b127979c9b2280c267fa4348a3b3abc0.js?file=snippet-5.go"></script>

## Designing Alerts and Mitigations

Once these metrics are flowing into Prometheus, you can build reliable alerts. The standard formula for assessing the severity of CPU throttling is to calculate the ratio of throttled time relative to the total CPU execution time:

```promql
# Alert if the cgroup is throttled for more than 5% of its execution time
rate(cgroup_cpu_throttled_seconds_total[2m]) / rate(container_cpu_usage_seconds_total[2m]) > 0.05
```

If you observe continuous throttling despite tuning `GOMAXPROCS`, consider the following mitigations:

1. **Remove CPU Limits, Use CPU Requests:** If your cluster design permits, eliminate CPU limits entirely and rely on CPU requests to allocate node priority. This allows containers to burst during idle periods on the host without encountering CFS limits, while ensuring the host scheduler can enforce fair distribution under overall contention.
2. **Increase CFS Period:** If your container runtime configures it, increase the CPU period from 100ms to 200ms or 500ms. A wider window allows Go's work-stealing scheduler to balance threads over a longer period, reducing the chance that short execution bursts trigger immediate throttling.
3. **Application Rate Limiting:** Implement token-bucket rate limiters at your gateway or within your Go middleware. Reducing concurrency during traffic spikes prevents the Go runtime from spawning excessive OS threads to handle queueing connections.

## Conclusion

CPU throttling is a quiet killer of application performance in containerized environments. Average-based monitoring tools fail to capture these microsecond-scale freezes, leaving engineers blind to p99 latency spikes. By implementing direct cgroupv2 parsing and utilizing eBPF to trace scheduler events, you can pinpoint exactly when, why, and for how long your Go runtime is suspended. Combining this visibility with proper `GOMAXPROCS` tuning will improve response times and keep your service running smoothly under load.