---
layout: post
title: "Tracking Linux Epoll Starvation in High-Throughput Event Loops with eBPF"
date: 2026-08-23 08:00:00 +0700
tags: [ebpf, linux, observability, performance, troubleshooting]
description: "Detect and debug high-throughput event loop latency anomalies by measuring kernel-level epoll starvation using eBPF tracepoints."
image: "https://picsum.photos/seed/5065/1080/720"
thumbnail: "https://picsum.photos/seed/5065/400/300"
---

Your high-throughput backend service is experiencing sudden, severe tail-latency spikes. The p99 latency climbs from 12ms to 2.4s under moderate traffic load, yet your application metrics show database queries are blazing fast, external API calls are nominal, and system-wide CPU utilization is sitting at a comfortable 12% on a 32-core virtual machine. Your APM traces show no obvious gaps, but your load balancer is reporting HTTP 504 Gateway Timeouts, and clients are experiencing dropped TCP connections. In production environments utilizing non-blocking, event-driven network architectures, this behavior is the classic signature of **event loop starvation**. When the thread hosting your network event loop is hijacked by a CPU-bound or blocking callback, it cannot execute `epoll_wait` to handle incoming TCP read/write events. The kernel queues incoming packets in the TCP receive buffer, but to the application, they do not exist. To pinpoint this silent performance killer without degrading production throughput, we can use eBPF to trace the latency between the exit and entry of the `epoll_wait` system call directly at the kernel boundary.

![Tracking Linux Epoll Starvation in High-Throughput Event Loops with eBPF Diagram](/images/diagrams/tracking-linux-epoll-starvation-high-throughput-event-loops-ebpf.svg)

## The Event Loop and Epoll Concurrency Model

Modern high-performance network runtimes (such as Envoy Proxy, Node.js, Java Netty, and Go’s netpoller) rely on the Linux `epoll` subsystem to multiplex I/O. Instead of allocating one operating system thread per connection—which incurs massive memory overhead and scheduling thrashing due to millions of context switches—these engines run a small, fixed number of event loop threads. 

Each event loop thread manages thousands of concurrent sockets by registering their file descriptors (FDs) with a single `epoll` instance. The runtime enters a continuous loop:

1. **Wait**: The thread calls the `epoll_wait` (or `epoll_pwait`) system call. The kernel puts the thread to sleep, yielding the CPU to other processes.
2. **Wake**: When one or more registered sockets have data ready to read, or space available to write, the kernel wakes the thread and returns control to user-space. `epoll_wait` returns an array of event structures.
3. **Dispatch**: The event loop thread iterates through the active FDs and executes their corresponding application-level callbacks (e.g., reading from the socket, parsing the protocol, routing the request).
4. **Repeat**: Once the batch of callbacks is executed, the thread loops back to step 1 and invokes `epoll_wait` again.

This architecture assumes that user-space callbacks are strictly non-blocking and execute in microseconds. If a callback violates this assumption by executing a CPU-intensive operation (e.g., synchronous JSON parsing of a 15MB payload, cryptographic signature verification, regex matching on long strings) or a blocking kernel system call (e.g., synchronous disk I/O, DNS resolution), the thread is pinned. 

During this starvation window, the thread cannot return to the `epoll_wait` call. Incoming TCP packets continue to arrive at the network interface card (NIC). The kernel’s TCP stack handles the TCP state machine, putting the incoming data into the socket's receive queue (`sk_rmem_alloc`). If the queue fills up, the TCP window size drops to zero, and the kernel begins dropping packets. For new connections, the SYN packets sit in the accept queue (`sk_max_ack_backlog`). If the event loop is stalled, the application cannot call `accept()`, causing the queue to overflow and connections to time out before the handshakes can complete.

## Why Traditional Observability Fails to Detect Starvation

Detecting this state is notoriously difficult using standard Linux observability tools.

First, CPU utilization metrics are misleading. If you have 32 CPU cores and one event loop thread is pinned at 100% execution capacity for 2 seconds, the global CPU utilization metric only increases by `1 / 32 = 3.1%`. If you monitor CPU utilization at the container level using Prometheus or Datadog, a 3% change is indistinguishable from background noise.

Second, application-level APM tracing is blind to queueing delays. Most APM SDKs instrument the beginning and end of a callback handler. If the event loop is stalled, the callback for request $B$ is queued inside the runtime queue or the TCP buffer while request $A$ blocks the thread. Request $B$'s APM span does not start until the thread finally picks it up. As a result, the APM trace shows that request $B$ took only 3ms of execution time, masking the fact that the client waited 2.4 seconds for a response.

Finally, user-space instrumentation of the event loop itself (e.g., measuring loop execution times in JavaScript or Go code) introduces significant overhead and is prone to inaccuracy. If the runtime is severely starved, even the instrumentation code or timer callbacks cannot run in a timely manner.

To capture this accurately, we must measure event loop latency at the syscall boundary. The exact duration of event loop starvation is the time elapsed between the thread *exiting* `epoll_wait` and its *next entry* into `epoll_wait`. 

## Measuring Starvation via eBPF Tracepoints

Using Extended Berkeley Packet Filter (eBPF), we can attach probes to the entry and exit tracepoints of the `epoll_wait` and `epoll_pwait` system calls. Because these tracepoints are built into the kernel, we can measure the latency with nanosecond precision and near-zero CPU overhead.

Specifically, we hook:
- `tracepoint/syscalls/sys_exit_epoll_wait` and `tracepoint/syscalls/sys_exit_epoll_pwait` to record the timestamp when the event loop thread returns to user-space. We store this exit timestamp in an eBPF hash map, keyed by the unique Thread ID (`TID`).
- `tracepoint/syscalls/sys_enter_epoll_wait` and `tracepoint/syscalls/sys_enter_epoll_pwait` to intercept the moment the event loop thread calls back into the kernel. We look up the thread's previous exit timestamp in the hash map, calculate the difference (`enter_ts - exit_ts`), and delete the hash map entry.

The resulting delta represents the total duration the thread spent executing user-space code. If this delta exceeds our configured starvation threshold (for example, 10 milliseconds), we write a starvation event containing the PID, TID, process command name, and duration to an eBPF Ring Buffer. The ring buffer is read asynchronously by a user-space daemon, which exports the metrics to Prometheus.

## The eBPF Kernel Tracer Code

We will write the eBPF program in C using the modern `CO-RE` (Compile Once – Run Everywhere) paradigm. The file [epoll_starve.bpf.c](file:///home/muklis/Documents/exploring/blog/code/epoll_starve.bpf.c) contains the kernel-space program.

<script src="https://gist.github.com/mohashari/23ad0f70dee897051aecc696f2a53cf3.js?file=snippet-1.txt"></script>

In the kernel code, the helper function [`bpf_get_current_pid_tgid()`](file:///home/muklis/Documents/exploring/blog/code/epoll_starve.bpf.c#L38) returns a 64-bit integer where the upper 32 bits represent the Process ID (`TGID`) and the lower 32 bits represent the Thread ID (`PID` in kernel terms, `TID` in user-space). Because event loops run on a per-thread basis, tracking starvation by Thread ID is critical. We use `BPF_MAP_TYPE_HASH` rather than an array map to ensure memory is allocated dynamically and only occupied by active event loops. The BPF Ring Buffer (`BPF_MAP_TYPE_RINGBUF`) provides memory-safe, lockless, multi-core event submission to user-space, avoiding the high overhead of older perf buffers.

## Building the Go User-Space Collector

To compile and load our eBPF program, we write a Go application using the `cilium/ebpf` library. This daemon loads the compiled BPF object file into the kernel, attaches the tracepoint handlers, reads events from the ring buffer, and prints the starvation events to standard output. In a production setup, these logs are exported directly to a Prometheus client. The code for the daemon is defined in [main.go](file:///home/muklis/Documents/exploring/blog/code/main.go).

<script src="https://gist.github.com/mohashari/23ad0f70dee897051aecc696f2a53cf3.js?file=snippet-2.go"></script>

The user-space consumer uses `rlimit.RemoveMemlock()` to remove constraints on the maximum locked memory. This step is necessary on kernels older than 5.11 because eBPF maps reside in locked memory, which defaults to very low limits (typically 64KB). The reader continuously pulls data from the ring buffer in a separate goroutine. 

## Simulating a Real-World Starvation Event

To demonstrate how the eBPF tool catches starvation, we will write a Node.js server that features a single-threaded event loop. Node.js uses `libuv`, which runs on top of `epoll`. 

The file [server.js](file:///home/muklis/Documents/exploring/blog/code/server.js) contains two endpoints: a lightweight, non-blocking health check and a CPU-intensive route that computes SHA-256 hashes synchronously.

<script src="https://gist.github.com/mohashari/23ad0f70dee897051aecc696f2a53cf3.js?file=snippet-3.js"></script>

To run this experiment:
1. Start the Node.js server:
   ```bash
   node server.js
   ```
2. In another terminal, compile and run the Go eBPF collector with root permissions (required to attach BPF probes):
   ```bash
   go generate && go build -o epoll-starve-exporter
   sudo ./epoll-starve-exporter
   ```
3. Generate high traffic on the `/health` endpoint using a load generator like `wrk`:
   ```bash
   wrk -t4 -c100 -d10s http://localhost:8080/health
   ```
   Under baseline conditions, the latency remains sub-millisecond, and the eBPF monitor logs nothing.
4. While the load test is running, execute a single request to the `/hash` route in another terminal:
   ```bash
   curl "http://localhost:8080/hash?iterations=1200000"
   ```
5. Observe the load generator output. The p99 latency spikes immediately, and the eBPF monitor outputs:
   ```text
   2026/08/23 08:15:32 [STARVATION DETECTED] Command: 'node' (PID: 23145, TID: 23145) blocked execution thread for 183.42ms
   ```

Because Node.js runs on a single main thread, computing the SHA-256 chain blocks the event loop thread for 183ms. During those 183ms, the socket handling the `/health` request is ready, but the process is trapped in the hashing loop. Our eBPF tracer registers this latency at the syscall level, reporting the process command name `'node'` along with its exact process ID and thread ID.

## Enterprise Production Deployment and Alerting

To run this system in production, we need a standard deployment setup. The eBPF exporter should run as a systemd service on virtual machines or as a DaemonSet in Kubernetes. 

We write a systemd configuration file in [epoll-tracer.service](file:///home/muklis/Documents/exploring/blog/code/epoll-tracer.service) to execute this daemon. We restrict process capabilities to follow the principle of least privilege.

<script src="https://gist.github.com/mohashari/23ad0f70dee897051aecc696f2a53cf3.js?file=snippet-4.txt"></script>

To build this exporter cleanly within our CI/CD pipelines, we can package it in a container. The Dockerfile compiles both the kernel eBPF C code and the Go collector:

<script src="https://gist.github.com/mohashari/23ad0f70dee897051aecc696f2a53cf3.js?file=snippet-5.dockerfile"></script>

Once the daemon is running, it exposes a Prometheus endpoint tracking the metric `epoll_starvation_events_total` labeled with the target binary name (`comm`), `pid`, and `tid`. We define a Prometheus Alertmanager rule to notify our on-call rotation when starvation is sustained.

<script src="https://gist.github.com/mohashari/23ad0f70dee897051aecc696f2a53cf3.js?file=snippet-6.yaml"></script>

## Mitigation and Remediation Strategies

If our eBPF monitor flags event loop starvation in production, we have three primary mitigation strategies:

### 1. Offload Blockers to Worker Pools
Never execute CPU-bound work or blocking I/O directly on the event loop threads. 
- In **Node.js**, leverage `worker_threads` or delegate CPU-heavy parsing to external microservices.
- In **Java Netty**, route heavy computation or database calls to a separate `EventExecutorGroup` rather than execution channels on the I/O event loop.
- In **Go**, while the runtime manages scheduling automatically, intensive CPU operations can block goroutines on individual threads without preemptive yielding. Call `runtime.Gosched()` inside long-running CPU loops to yield thread control back to the scheduler, or offload to a worker pool pattern.

### 2. Isolate Disk I/O
Disk I/O under Linux is fundamentally blocking unless you use asynchronous system calls like `io_uring`. Calling `read()` or `write()` on a file descriptor that points to a file on a block storage device can stall the executing thread while the OS waits for disk access. Ensure all file system operations run on dedicated threads separated from network I/O execution threads.

### 3. Tune Event Loop Allocation
Ensure your application does not create more event loop threads than available physical CPU cores. If your application attempts to run 16 event loop threads on an 8-core machine, the threads will compete with each other for CPU time, leading to context switches that increase event loop dispatch latency and trigger false-positive starvation warnings.

## Conclusion

Event loop starvation is a structural bottleneck that standard APMs and machine-level CPU monitoring tools cannot expose. By tracing the boundary between exiting and entering the `epoll_wait` system call in the Linux kernel, eBPF allows us to measure user-space event loop stalling with microsecond accuracy. Integrating an eBPF-based starvation exporter into your monitoring stack provides your platform teams with immediate visibility into performance regressions before they escalate into cascading service failures.