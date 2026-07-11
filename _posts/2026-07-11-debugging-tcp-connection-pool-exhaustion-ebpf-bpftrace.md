---
layout: post
title: "Optimizing TCP Connection Pool Exhaustion Debugging with eBPF and bpftrace"
date: 2026-07-11 08:00:00 +0700
tags: [ebpf, bpftrace, networking, debugging, performance]
description: "Stop guessing why your microservices are running out of TCP connections. Learn how to use eBPF and bpftrace to diagnose connection pool leaks in real-time."
image: "https://picsum.photos/seed/6995/1080/720"
thumbnail: "https://picsum.photos/seed/6995/400/300"
---

It is 3:00 AM, and your pager is screaming. The primary Go API gateway is throwing HTTP 504 Gateway Timeouts, latency is spiking from 20 milliseconds to over 10 seconds, and downstream services are logging `dial tcp: i/o timeout` or `connection pool exhausted`. Your CPU utilization is low, memory looks stable, and the database claims it is idle. You check socket statistics via `ss -s` and find that the gateway has opened 28,000 TCP connections to a single internal service, putting it right on the edge of local ephemeral port limits. Traditional metrics show that the connection pool is empty, but they cannot tell you *which* code path is leaking sockets or *why* they aren't recycling. This is the reality of TCP connection pool exhaustion—a silent, cascading failure mode that traditional user-space observability architectures fail to capture in real time. To solve it, we must bypass user-space tooling entirely and leverage kernel-level observability via eBPF and `bpftrace`.

## The Silent Killer: TCP Connection Pool Exhaustion in Production

In microservice architectures, client-side connection pooling is a critical optimization. Establishing a TCP connection requires a three-way handshake (SYN, SYN-ACK, ACK), which introduces at least one network round-trip time (RTT) of latency. If you are using TLS, you add another one to two RTTs for the cryptographic handshake. To avoid this overhead on every HTTP request, runtimes keep a pool of idle, warm TCP connections open to downstream targets.

However, connection pools are finite resources. They are governed by constraints such as maximum open connections, maximum idle connections, and idle timeouts. When a service experiences TCP connection pool exhaustion, it generally falls into one of three patterns:

1. **Connection Leaking:** The application acquires a TCP socket from the pool, completes the transaction, but fails to release the socket back to the pool. The socket remains permanently allocated to that specific logical routine until either the remote server closes it or the client process restarts.
2. **Latency Bubbles:** A downstream dependency slows down. If the downstream response time increases from 50ms to 5s, connections remain active for 100 times longer. The pool quickly saturates its maximum connection limit trying to maintain throughput, forcing subsequent requests to queue or fail.
3. **Ephemeral Port Starvation:** The pool is misconfigured to close connections immediately after use instead of keeping them idle. This causes a massive churn of connections. When a TCP connection closes, the kernel places the socket into the `TIME_WAIT` state for two times the Maximum Segment Lifetime (MSL)—typically 60 seconds on Linux. During this time, the local IP and port tuple cannot be reused. Under high request rates, you can easily exhaust the kernel's ephemeral port range (typically ~28,231 ports), preventing any new outbound connections from being established.

Understanding which of these failure modes is occurring requires examining the exact state transitions of sockets as they interact with the kernel.

## Deconstructing the Anatomy of a Connection Leak

To understand why traditional tools fail us, we must first look at how a connection leak manifests in application code. In Go, the most common source of connection leaks is the failure to properly drain and close the response body of an HTTP request.

<script src="https://gist.github.com/mohashari/dde40f62d0a95b2de71037a0b0de348d.js?file=snippet-1.go"></script>

In the Go HTTP client lifecycle, the network connection is returned to the pool only when the response body is read to completion (EOF) and its `Close` method is executed. If either of these steps is skipped, the connection remains bound to the active goroutine. From the operating system's perspective, the socket is still in the `ESTABLISHED` state, consuming a file descriptor and system memory, but user-space connection pool metrics will simply show the connection as "active" or "in-use," giving you no indication of which specific code path leaked it.

## Why Traditional Observability Strategies Fall Short

When an outage strikes, engineers instinctively run command-line tools like `ss` or `netstat`. 

<script src="https://gist.github.com/mohashari/dde40f62d0a95b2de71037a0b0de348d.js?file=snippet-2.sh"></script>

While `ss` is highly performant because it queries kernel socket statistics directly through netlink interface, it suffers from two major limitations:

- **It is snapshot-based:** It shows the state of the sockets at the exact microsecond you run the command. If you have transient connection churn or highly dynamic leaks, you must poll `ss` continuously, which consumes significant CPU and can easily miss fast state changes.
- **It lacks user-space context:** `ss` can tell you that a socket exists, is in the `ESTABLISHED` state, and belongs to PID `12345`. However, it cannot tell you which internal goroutine, thread, or call stack initiated the connection, nor can it track how long that specific socket has been sitting idle or which code path failed to call the close system call.

Application performance monitoring (APM) tools and distributed tracing (e.g., OpenTelemetry) also fall short. They trace execution paths from HTTP entry points to exit points, but they have no visibility into the kernel socket lifecycle. An APM span might tell you that an HTTP call took 5 seconds, but it cannot differentiate between 5 seconds spent waiting for a downstream server to respond and 5 seconds spent waiting in the connection pool queue because the local kernel was starved of ephemeral ports.

To bridge this gap, we must hook directly into the kernel network stack events.

## Solving the Blind Spot: Real-time Tracing with eBPF

Extended Berkeley Packet Filter (eBPF) allows us to run sandboxed programs inside the Linux kernel without modifying the kernel source or loading external kernel modules. It provides safe, zero-overhead access to kernel state and data structures.

For debugging connection pool exhaustion, we can write eBPF scripts using `bpftrace`. `bpftrace` uses a high-level tracing language that compiles down to BPF bytecode, allowing us to perform ad-hoc tracing of kernel functions, tracepoints, and user-space probes.

To monitor TCP connection lifecycles, we hook into tracepoints in the kernel's TCP state machine. The primary tracepoint we care about is `sock:inet_sock_set_state`. This tracepoint fires whenever an internet socket changes its state (e.g., from `SYN_SENT` to `ESTABLISHED`, or from `ESTABLISHED` to `FIN_WAIT1`).

## Writing the Tracers: bpftrace in Action

Let's write a `bpftrace` tool to track TCP state transitions in real time. This script will intercept socket state changes and print the source IP, source port, destination IP, destination port, and the process name that owns the socket.

<script src="https://gist.github.com/mohashari/dde40f62d0a95b2de71037a0b0de348d.js?file=snippet-3.txt"></script>

This script extracts data directly from the kernel's `struct sock` memory layout. When a socket changes state, `bpftrace` formats and prints the transition. If you see thousands of sockets transitioning from `ESTABLISHED` to `FIN_WAIT1` or staying in `CLOSE_WAIT`, you can quickly determine if the application is failing to respond to remote close events.

However, to diagnose a *leak*, we need to know how long sockets are staying open. If a connection pool is functioning correctly, connections are either reused frequently or closed after their idle timeout. If a connection is leaked, it will stay in the `ESTABLISHED` state indefinitely. 

Let's write a second `bpftrace` script that measures the duration of TCP connections. We will use a BPF associative map to store the start timestamp when a connection enters the `ESTABLISHED` state, and calculate the delta when it transitions to a closed state.

<script src="https://gist.github.com/mohashari/dde40f62d0a95b2de71037a0b0de348d.js?file=snippet-4.txt"></script>

If you run this script and see sockets closing with active durations of hundreds of thousands of milliseconds, you are looking at connections that were held open far beyond your configured timeouts. 

Sometimes, the connection pool exhaustion occurs on the server side, where the server is dropping incoming SYN packets because its queue is saturated. We can write a script to trace server-side listen backlog overflows by hooking into the TCP incoming request handler.

<script src="https://gist.github.com/mohashari/dde40f62d0a95b2de71037a0b0de348d.js?file=snippet-5.txt"></script>

If this script triggers, it means your backend server is receiving connections faster than its user-space application loop can call `accept()`. This is a classic indicator that the application's runtime thread pool or event loop is blocked, causing incoming TCP handshakes to fail.

## Troubleshooting a Live Incident with bpftrace

Let’s walk through a concrete incident troubleshooting session using the scripts we’ve designed. 

### Step 1: Identify Socket Leak Behavior
You notice your application's file descriptor count is steadily rising. You run the `tcp_state_tracer.bt` script and filter for your specific process binary name:

<script src="https://gist.github.com/mohashari/dde40f62d0a95b2de71037a0b0de348d.js?file=snippet-6.sh"></script>

The output reveals a pattern:

```text
TIME     COMM             PID    SADDR           SPORT  DADDR           DPORT  STATE_CHANGE
0.00102  api-gateway      48102  10.0.1.15       49281  10.0.2.20       8080   CLOSE -> SYN_SENT
0.00124  api-gateway      48102  10.0.1.15       49281  10.0.2.20       8080   SYN_SENT -> ESTABLISHED
1.02102  api-gateway      48102  10.0.1.15       49302  10.0.2.20       8080   CLOSE -> SYN_SENT
1.02124  api-gateway      48102  10.0.1.15       49302  10.0.2.20       8080   SYN_SENT -> ESTABLISHED
```

Notice that new connections are transition to `ESTABLISHED` every second, but you never see any transitions to `FIN_WAIT1`, `FIN_WAIT2`, or `CLOSE`. The connections are being opened, and then they disappear from the kernel transition logs while remaining in the `ESTABLISHED` state. This confirms a user-space **connection leak**. The application is abandoning the sockets without closing them.

### Step 2: Correlate with Application Code
To find the leaky code path, we can profile the Go application using `pprof` to examine the goroutine dump, or we can use `bpftrace` user-space probes (`uprobes`) to hook into the application's socket creation calls. However, now that we know the destination IP (`10.0.2.20`) and port (`8080`), we can search our codebase for the client configuration pointing to this downstream dependency.

## Remediation: Tuning App Clients and Linux Kernels

Once you have identified the failure mode, you must apply both application-level and kernel-level mitigations.

### 1. Hardening the Application Client (Go)
Go’s default HTTP client configuration is notoriously unsafe for high-throughput production workloads. The default `http.DefaultTransport` sets `MaxIdleConnsPerHost` to `2`. 

If your application handles 1,000 concurrent requests to a downstream host, Go will open 1,000 connections. When those requests complete, the transport attempts to return them to the idle pool. However, because `MaxIdleConnsPerHost` is limited to `2`, it will keep only 2 connections open and explicitly close the other 998 connections. This causes massive connection churn and triggers ephemeral port starvation because the closed sockets flood the kernel with `TIME_WAIT` states.

Here is how you configure a production-grade client:

<script src="https://gist.github.com/mohashari/dde40f62d0a95b2de71037a0b0de348d.js?file=snippet-7.go"></script>

### 2. Tuning the Linux Network Stack
If you are running high-throughput APIs, the default kernel TCP parameters will restrict your scalability. You must tune `/etc/sysctl.conf` to handle high socket reuse rates and deep queue backlogs.

<script src="https://gist.github.com/mohashari/dde40f62d0a95b2de71037a0b0de348d.js?file=snippet-8.txt"></script>

Applying `net.ipv4.tcp_tw_reuse` allows the kernel to bypass the 60-second `TIME_WAIT` lock-out period for outbound connections if the TCP timestamp of the incoming packet is strictly greater than the last timestamp recorded for that connection. This is the single most effective sysctl change for preventing ephemeral port exhaustion.

## Summary and Production Incident Checklist

When a TCP connection pool crisis occurs, your goal is to transition from symptom detection to root-cause isolation in under 5 minutes. Use this step-by-step checklist:

1. **Verify State Distribution:** Run `ss -s` to check if the issue is connection accumulation (`ESTABLISHED`), connection closure churn (`TIME_WAIT`), or downstream rejection (`CLOSE_WAIT`).
2. **Execute eBPF Trace:** Run `tcp_state_tracer.bt` to trace the source, destination, and process names for the active connections. Identify which downstream service (DADDR:DPORT) is saturating your local sockets.
3. **Analyze Connection Durations:** Run `tcp_connection_duration.bt`. If connections are remaining open for hundreds of seconds, look for unclosed response bodies or missing client read timeouts.
4. **Inspect Server-Side Backlogs:** If the failure is incoming, use `tcp_backlog_overflow.bt` to check if your server's listen backlog is overflowing.
5. **Mitigate and Patch:** Update client configurations to enforce `MaxIdleConnsPerHost` parity with expected concurrency, set tight request timeouts, and configure `sysctl` settings (`tcp_tw_reuse`) to ensure rapid socket recycling.