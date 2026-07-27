---
layout: post
title: "Tracing TCP Connection Pool Exhaustion in Go Microservices via Linux eBPF Sockops"
date: 2026-07-27 08:00:00 +0700
tags: [go, ebpf, networking, observability, linux]
description: "Diagnose and trace Go microservice TCP connection leaks and pool exhaustion in production using Linux eBPF Sockops to capture low-level kernel socket events."
image: "https://picsum.photos/seed/1642/1080/720"
thumbnail: "https://picsum.photos/seed/1642/400/300"
---

## The Production Nightmare: Ephemeral Port Exhaustion

Imagine a Friday afternoon when your high-throughput Go payment gateway microservice suddenly triggers alerts. P99 latencies for downstream calls to your ledger service spike from a steady 12ms to over 5000ms. The application logs start flooding with errors: `dial tcp 10.0.4.12:8080: connect: connection refused` or `dial tcp 10.0.4.12:8080: i/o timeout`. You check CPU and memory utilization; they are flat. You run `ss -s` on the host and see: `TCP: 32000 (estab 1500, closed 29500, orphaned 0, timewait 28000)`. Your ephemeral ports, which typically span `32768` to `60999` on Linux, are completely exhausted. Outgoing connection attempts are forced to block or fail. Traditional APM tracing tells you that time is spent in `net/http.(*Transport).dial`, but it cannot diagnose *why* the connection pool is failing to reuse sockets. Was it a leaky response body in a newly deployed middleware? A network partition dropping TCP FIN packets? Or did downstream latency trigger a race condition in Go's idle connection cleanup? To solve this without guessing or deploying blind hotfixes, we must look below the Go runtime and instrument the Linux kernel's TCP stack using eBPF Sockops (`BPF_PROG_TYPE_SOCK_OPS`).

## The Anatomy of Go's Connection Pool Leak

Go’s `net/http` package manages TCP connections concurrently using `http.Transport`. When your service executes an HTTP request, the transport checks if an idle connection to the target host is available in its pool (`http.persistConn`). If one exists, it is reused; if not, a new TCP connection is established. 

However, Go’s default configurations are optimized for basic client applications, not high-throughput microservices. Two primary failure modes lead to connection pool exhaustion in Go: the default client configuration limit and response body leaks.

### 1. The Default Transport Configuration Trap
By default, `http.DefaultTransport` sets `MaxIdleConns` to 100 but sets `MaxIdleConnsPerHost` to **2**. If your microservice processes 200 concurrent requests and routes them to a single downstream service, only 2 connections can be returned to the idle pool when the requests complete. The remaining 198 connections are closed immediately. In a high-throughput scenario processing 2,000 requests per second, this results in the rapid creation and teardown of 1,980 connections per second. Because the Linux kernel enforces a default `TCP_TIMEWAIT_LEN` of 60 seconds before reclaiming sockets, your host will quickly accumulate over 110,000 sockets in the `TIME_WAIT` state, leading to ephemeral port exhaustion.

### 2. The Leaked Response Body
In Go, a connection is not returned to the pool until the client has read the response body to completion (`EOF`) and closed it. If your code checks the HTTP status code and returns early on an error without draining and closing `resp.Body`, the underlying connection remains allocated to that specific request context. The connection will eventually time out, but until it does, it remains open and unavailable for reuse, causing an active socket leak.

The following code illustrates this behavior, simulating both a misconfigured connection pool and a leaked response body.

<script src="https://gist.github.com/mohashari/33092c4da7f2ab417211fe7aba3f2a1c.js?file=snippet-1.go"></script>

## Why Traditional Tools Fall Short

When debugging socket leaks under load, typical troubleshooting commands introduce significant blind spots:

*   **`ss` and `netstat`**: These tools query the `/proc/net/tcp` interface or use netlink sockets to fetch snapshot-based socket tables. Under heavy loads, running `ss` repeatedly consumes high CPU and misses short-lived connections that cycle between snapshots.
*   **`tcpdump`**: Packet capture is extremely resource-intensive. Capturing raw packet data on a 10Gbps network interface can degrade CPU performance and saturate storage, making it risky to run in production environments.
*   **Go `net/http/httptrace`**: While `httptrace` provides client-side hooks like `GotFirstResponseByte` and `ConnectDone`, it is blind to the kernel's actual socket lifecycle. It cannot tell you if a connection is hanging in `CLOSE_WAIT` because of a local application bug or if the kernel is dropping TCP FIN packets due to a firewall configuration.

To trace the connection lifecycle with minimal overhead, we must monitor socket events directly inside the kernel using eBPF.

## Enter eBPF Sockops

Historically, tracing TCP state changes required attaching eBPF `kprobes` to internal kernel functions such as `tcp_set_state` or `tcp_v4_connect`. However, `kprobes` are unstable across kernel versions because internal function signatures frequently change, and parsing kernel structures like `struct sock` requires access to kernel headers.

eBPF Socket Operations (`BPF_PROG_TYPE_SOCK_OPS`) provide a stable, purpose-built interface for socket monitoring. Instead of hooking arbitrary kernel functions, Sockops attaches to a cgroupv2 path and triggers on specific socket events:

*   `BPF_SOCK_OPS_ACTIVE_ESTABLISHED_CB`: Triggers when an outbound TCP connection is established.
*   `BPF_SOCK_OPS_PASSIVE_ESTABLISHED_CB`: Triggers when an inbound TCP connection is accepted.
*   `BPF_SOCK_OPS_STATE_CB`: Triggers when the TCP state changes (e.g., transitioning from `ESTABLISHED` to `FIN_WAIT1`, `CLOSE_WAIT`, or `TIME_WAIT`).

By writing a Sockops program, we can capture every TCP state change for a specific cgroup, collect the source and destination IPs, ports, and socket cookies, and stream them to user space with minimal CPU overhead.

## Implementing the eBPF Sockops Kernel Program

Below is the kernel-space C code for our Sockops tracer. It monitors TCP state transitions, extracts the connection metadata, and pushes the records into an eBPF ring buffer.

<script src="https://gist.github.com/mohashari/33092c4da7f2ab417211fe7aba3f2a1c.js?file=snippet-2.txt"></script>

### Key Implementation Details:
1.  **Ring Buffer**: We use `BPF_MAP_TYPE_RINGBUF` (introduced in Linux 5.8) because it performs better and uses memory more efficiently than the older `BPF_MAP_TYPE_PERF_EVENT_ARRAY`. It shares memory pages across CPUs and uses memory allocation flags that avoid memory lock contention.
2.  **Socket Cookie**: `bpf_get_socket_cookie` returns a kernel-level `__u64` cookie unique to the lifecycle of that specific TCP socket. This allows us to correlate connection initialization with subsequent state changes and closures.
3.  **Selective Callbacks**: By default, state transition events (`BPF_SOCK_OPS_STATE_CB`) are disabled to save CPU cycles. When an active or passive connection is established, we call `bpf_sock_ops_cb_flags_set` to enable state tracking for only that socket.

## Loading and Reading the BPF Events in Go

To parse these kernel events, we write a user-space loader in Go using the `github.com/cilium/ebpf` library. The loader loads the compiled BPF program, attaches it to the cgroupv2 directory representing our target application, and reads records from the ring buffer.

<script src="https://gist.github.com/mohashari/33092c4da7f2ab417211fe7aba3f2a1c.js?file=snippet-3.go"></script>

## Compiling and Running the Telemetry Pipeline

To run this pipeline, we need to compile the C kernel code into BPF bytecode using `clang` and target the `bpf` architecture. We can then execute the Go binary to attach the program and stream connection events.

<script src="https://gist.github.com/mohashari/33092c4da7f2ab417211fe7aba3f2a1c.js?file=snippet-4.sh"></script>

## Diagnosing Connection Pool Exhaustion from Traces

Once the tracer is running under test load, we can identify connection pool exhaustion by looking for specific patterns in the logs:

### 1. The TIME_WAIT Storm (Misconfigured `MaxIdleConnsPerHost`)
If `MaxIdleConnsPerHost` is too low, you will see a cycle where new sockets are created, transition to `ESTABLISHED`, and then immediately transition to `FIN_WAIT1`, `FIN_WAIT2`, and `TIME_WAIT` for every request.

```text
[Socket: c2f49d8a11a2f400] State Transition: 10.0.2.15:45230 -> 10.0.4.12:8080 | ESTABLISHED -> FIN_WAIT1
[Socket: c2f49d8a11a2f400] State Transition: 10.0.2.15:45230 -> 10.0.4.12:8080 | FIN_WAIT1 -> FIN_WAIT2
[Socket: c2f49d8a11a2f400] State Transition: 10.0.2.15:45230 -> 10.0.4.12:8080 | FIN_WAIT2 -> TIME_WAIT
```

If you see these entries repeating rapidly across different socket cookies while accessing the same downstream endpoint, the client is actively closing connections instead of returning them to the pool.

### 2. The CLOSE_WAIT Hang (Leaked Response Body)
If your application leaks response bodies, the downstream server will eventually close its end of the connection due to its own idle timeouts. In the tracer, you will see the socket enter `CLOSE_WAIT`:

```text
[Socket: f2b8a071f008ad00] State Transition: 10.0.2.15:48912 -> 10.0.4.12:8080 | ESTABLISHED -> CLOSE_WAIT
```

When a socket is in `CLOSE_WAIT`, the remote host has closed its end (sending a FIN packet), but the local client has not closed its corresponding file descriptor. If these `CLOSE_WAIT` entries accumulate and do not transition to `LAST_ACK` or `CLOSE`, your Go process is leaking the response bodies.

## Mitigating and Verifying TCP Pool Exhaustion

To prevent connection pool exhaustion under heavy production loads, apply the following optimizations:

1.  **Tune `MaxIdleConns` and `MaxIdleConnsPerHost`**: Increase `MaxIdleConnsPerHost` to match your expected concurrent downstream requests. If your service handles 200 concurrent requests, set `MaxIdleConnsPerHost` to at least 200.
2.  **Enable TCP Keep-Alives**: Configure TCP keep-alive probes to detect dead sockets early and prevent firewalls from silently dropping connections.
3.  **Use a Safe Drain and Close Pattern**: Always read to the end of the response body and close it to ensure the socket is recycled.

<script src="https://gist.github.com/mohashari/33092c4da7f2ab417211fe7aba3f2a1c.js?file=snippet-5.go"></script>

By deploying the optimized client, implementing the drain pattern, and using the eBPF Sockops tracer, you can monitor socket transitions in real time to verify that connections are being reused instead of discarded.