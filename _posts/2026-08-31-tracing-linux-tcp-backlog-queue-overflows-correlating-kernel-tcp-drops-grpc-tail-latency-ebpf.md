---
layout: post
title: "Tracing Linux TCP Backlog Queue Overflows: Correlating Kernel TCP Drops with gRPC Tail Latency Spikes using eBPF"
date: 2026-08-31 08:00:00 +0700
tags: [ebpf, linux-kernel, grpc, networking, latency]
description: "Diagnose and fix silent TCP accept queue overflows causing gRPC tail latency spikes by tracing Linux kernel drops using eBPF and Go instrumentation."
image: "https://picsum.photos/seed/7708/1080/720"
thumbnail: "https://picsum.photos/seed/7708/400/300"
---

It’s 3:00 AM, and your p99.9 latency for a critical gRPC-based microservice suddenly spikes from 4ms to over 1000ms. Your CPU usage is hovering at a modest 40%, memory is stable, and there is zero database lock contention. Standard application metrics show nothing but slow response times, and the server-side gRPC request logs report that the handler execution took only 2ms. This is the classic "phantom latency" anomaly. Under transient burst conditions, your application’s listen backlog queue overflows, causing the Linux kernel to silently drop incoming TCP ACK packets during the final phase of the three-way handshake. Because the client believes the connection is established, it immediately sends the HTTP/2 payload and waits, while the server's TCP stack ignores the data. The client eventually times out and retransmits the TCP ACK after the initial Retransmission Timeout (RTO) expires—which in the Linux kernel is hardcoded to 1 second. This post breaks down how the Linux kernel's TCP backlog queues operate under the hood, how overflows manifest in gRPC tail latency, and how to write and deploy an eBPF-based tracer using BCC and Go to capture these drops in real-time.

![Tracing Linux TCP Backlog Queue Overflows: Correlating Kernel TCP Drops with gRPC Tail Latency Spikes using eBPF Diagram](/images/diagrams/tracing-linux-tcp-backlog-queue-overflows-correlating-kernel-tcp-drops-grpc-tail-latency-ebpf.svg)

## The Silent Killer of gRPC Tail Latency: Listen Backlog Overflows

In high-throughput, low-latency microservice architectures, gRPC is often chosen for its HTTP/2-based multiplexing and binary serialization. However, HTTP/2 multiplexing relies on maintaining a persistent TCP connection. During auto-scaling events, network partitions, or client-side connection pooling resets, a "connection storm" occurs. When hundreds of clients attempt to establish new TCP connections simultaneously, the server's kernel must process these handshakes rapidly.

The major issue is that when the server's listen queue fills up, the server-side gRPC application does not even see the incoming connections. It is blocked in the `accept()` system call, waiting to dequeue established connections. Because the connection has not been accepted yet, the gRPC handler is never invoked, meaning server-side instrumentation (like OpenTelemetry interceptors) registers absolutely zero latency anomalies. The client, however, experiences a delay of exactly 1.00 seconds (or more), which corrupts your p99 and p99.9 latency metrics. 

Understanding why this happens requires a deep dive into the two distinct queues the Linux kernel maintains for every listening TCP socket.

## Anatomy of the Linux TCP Handshake Queues: SYN vs. Accept

When a server socket is bound and put into a listening state via the `listen(fd, backlog)` system call, the kernel allocates two separate queues for that socket:

1. **The SYN Queue (Incomplete Connection Queue):**
   When a client sends a TCP SYN packet, the kernel puts the connection into the `SYN_RECV` state and adds it to the SYN Queue. The kernel then replies with a SYN-ACK packet. The size of this queue is governed globally by the `/proc/sys/net/ipv4/tcp_max_syn_backlog` parameter.
   
2. **The Accept Queue (Complete Connection Queue):**
   When the client replies with the final ACK packet of the handshake, the kernel removes the connection from the SYN Queue and moves it into the Accept Queue, changing its state to `ESTABLISHED`. The connection remains in this queue until the user-space application calls `accept()`.

The maximum capacity of the Accept Queue is determined by the minimum of two values: the `backlog` argument passed to the `listen()` system call by the application, and the system-wide kernel parameter `/proc/sys/net/core/somaxconn`.

```
Accept Queue Limit = min(backlog, somaxconn)
```

When the Accept Queue is completely full and a new ACK arrives from a client to complete the handshake, the kernel cannot move the connection into the Accept Queue. What happens next depends on the system-wide sysctl setting `/proc/sys/net/ipv4/tcp_abort_on_overflow`:

- **`tcp_abort_on_overflow = 0` (Default):** The kernel ignores the incoming ACK packet. Since the client has already transitioned its end of the connection to `ESTABLISHED` (upon sending the ACK), it immediately begins transmitting the HTTP/2 headers (gRPC request payload). However, because the server has ignored the ACK, the server does not consider the connection established. The server silently discards the client's data packets. After a timeout, the client's TCP stack realizes no ACKs have returned for its data, and it triggers a retransmission.
- **`tcp_abort_on_overflow = 1`:** The kernel replies to the client's ACK with a TCP RST (Reset) packet, actively tearing down the connection. The client application sees a "Connection reset by peer" error immediately.

While setting `tcp_abort_on_overflow = 1` makes the failure explicit, the default value of `0` is preferred in most production environments because it allows the system to absorb transient spikes. If the server application drains the Accept Queue via `accept()` shortly after the spike, the connection can recover without throwing user-facing errors. The cost, however, is a massive tail latency spike caused by the client waiting for the TCP Retransmission Timeout (RTO). 

On Linux, the initial TCP RTO is defined by the kernel macro `TCP_TIMEOUT_INIT`, which is hardcoded to 1 second:

```c
#define TCP_TIMEOUT_INIT ((unsigned long)(HZ)) /* HZ = 1 second */
```

If the second handshake attempt fails, the exponential backoff doubles the timeout to 2 seconds, then 4 seconds, and so on. This is why backlog overflows result in latency spikes clustered around discrete values: 1 second, 3 seconds, 7 seconds, etc.

## Why Traditional Tools Fail to Diagnose Overflows

If you suspect Accept Queue overflows, your first instinct might be to poll network metrics using standard CLI tools like `netstat` or `ss`. 

For example, you can inspect global listen drops and overflows via `netstat -s`:

// snippet-1
```bash
# Check total ListenOverflows and ListenDrops since system boot
netstat -s | grep -E "SYNs to LISTEN|listen queue"
```

The output of `netstat -s` will show cumulative statistics:

```text
    84234 times the listen queue of a socket overflowed
    84234 SYNs to LISTEN sockets dropped
```

While this confirms that overflows are occurring, it has major limitations in production debugging:
1. **Lack of Granularity:** The counters are global and cumulative. You cannot determine *which* socket or port overflowed, or *when* the overflow occurred.
2. **Polling Blind Spots:** You can run `ss -lnt` to look at the instantaneous queue depths:

// snippet-2
```bash
# Recv-Q shows current backlog size (ack_backlog), Send-Q shows max backlog (max_ack_backlog)
ss -lntp 'sport = :50051'
```

Under normal operation, `ss` might output:

```text
State      Recv-Q Send-Q  Local Address:Port  Peer Address:Port Process
LISTEN     0      128     *:50051             *:*               users:(("grpc-server",pid=12345,fd=6))
```

The `Recv-Q` shows the current number of connections waiting to be accepted, and `Send-Q` shows the backlog limit (128). Because microbursts of connection requests can fill and drain a queue of 128 slots within a few milliseconds, a polling tool running every 10 seconds will almost always miss the event. 

To catch these microbursts without introducing CPU overhead, we must use eBPF to hook directly into the kernel functions responsible for managing queue limits.

## Tracing TCP Accept Queue Overflows with eBPF

To trace Accept Queue overflows, we can write an eBPF program that hooks into the Linux kernel's TCP stack. The primary entry point where the kernel decides what to do with a completing handshake is `tcp_v4_syn_recv_sock` (for IPv4) or the common helper function `tcp_acceptq_is_full`. 

Inside `tcp_v4_syn_recv_sock`, the kernel checks if the accept queue is full by calling `sk_acceptq_is_full(sk)`. If it is full, the kernel increments the `LINUX_MIB_LISTENOVERFLOWS` SNMP counter and drops the connection.

We can write a C program using BCC (BPF Compiler Collection) that hooks into `tcp_v4_syn_recv_sock`, extracts the current queue stats, and sends them to user space via a perf ring buffer.

// snippet-3
```c
// listen_overflow.c
#include <uapi/linux/ptrace.h>
#include <net/sock.h>
#include <bcc/proto.h>

struct listen_overflow_event_t {
    u32 pid;
    u32 saddr;
    u32 daddr;
    u16 sport;
    u16 dport;
    u32 backlog;
    u32 max_backlog;
    u64 ts;
};

// Define the perf ring buffer
BPF_PERF_OUTPUT(events);

int kprobe__tcp_v4_syn_recv_sock(struct pt_regs *ctx, struct sock *sk) {
    u32 backlog = sk->sk_ack_backlog;
    u32 max_backlog = sk->sk_max_ack_backlog;

    // Check if the accept queue is full. 
    // We trace when the current backlog exceeds the maximum configured backlog.
    if (backlog > max_backlog) {
        struct listen_overflow_event_t event = {};
        event.pid = bpf_get_current_pid_tgid() >> 32;
        event.backlog = backlog;
        event.max_backlog = max_backlog;
        event.ts = bpf_ktime_get_ns();

        // Read source and destination IPs from the socket struct.
        // sk_daddr is the client IP (destination of server's packets, source of incoming)
        // sk_rcv_saddr is the server IP (source of server's packets, destination of incoming)
        bpf_probe_read_kernel(&event.saddr, sizeof(event.saddr), &sk->__sk_common.skc_daddr);
        bpf_probe_read_kernel(&event.daddr, sizeof(event.daddr), &sk->__sk_common.skc_rcv_saddr);
        
        // Read ports (stored in network byte order)
        u16 sport = 0;
        u16 dport = 0;
        bpf_probe_read_kernel(&sport, sizeof(sport), &sk->__sk_common.skc_dport);
        bpf_probe_read_kernel(&dport, sizeof(dport), &sk->__sk_common.skc_num);
        
        // Convert network byte order to host byte order
        event.sport = ntohs(sport);
        event.dport = dport; // skc_num is already in host byte order inside the kernel struct

        events.perf_submit(ctx, &event, sizeof(event));
    }
    return 0;
}
```

Now, we write a Python user-space script to load the eBPF program into the kernel, read from the perf ring buffer, and format the output.

// snippet-4
```python
# tracer.py
from bcc import BPF
import socket
import struct
import datetime

# Load BPF program from C source file
b = BPF(src_file="listen_overflow.c")

def print_event(cpu, data, size):
    event = b["events"].event(data)
    
    # Format IPv4 addresses
    saddr = socket.inet_ntoa(struct.pack("<L", event.saddr))
    daddr = socket.inet_ntoa(struct.pack("<L", event.daddr))
    
    # Calculate real timestamp
    time_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')
    
    print(f"[{time_str}] OVERFLOW DETECTED: {saddr}:{event.sport} -> {daddr}:{event.dport} | "
          f"Backlog: {event.backlog} / Max Backlog: {event.max_backlog} (PID: {event.pid})")

# Bind python function to the perf buffer events
b["events"].open_perf_buffer(print_event)
print("Tracing TCP accept queue overflows... Press Ctrl+C to exit.")

while True:
    try:
        b.perf_buffer_poll()
    except KeyboardInterrupt:
        break
```

When run as `root` on a server undergoing connection spikes, this tool will produce direct, actionable logs:

```text
Tracing TCP accept queue overflows... Press Ctrl+C to exit.
[2026-08-31 08:05:12.124562] OVERFLOW DETECTED: 10.0.1.45:49221 -> 10.0.2.10:50051 | Backlog: 129 / Max Backlog: 128 (PID: 12345)
[2026-08-31 08:05:12.124890] OVERFLOW DETECTED: 10.0.1.46:58212 -> 10.0.2.10:50051 | Backlog: 130 / Max Backlog: 128 (PID: 12345)
```

## Quick-Win Triage with bpftrace

If you need to diagnose a production system immediately and cannot deploy a custom compilation toolchain or python scripts, you can utilize `bpftrace` to execute a one-liner. `bpftrace` compiles code on-the-fly and is perfect for fast incident response.

// snippet-5
```bash
# Execute this on-liner on the target host to trace accept queue overflows in real-time
bpftrace -e '
#include <net/sock.h>
kprobe:tcp_v4_syn_recv_sock {
  $sk = (struct sock *)arg0;
  $backlog = $sk->sk_ack_backlog;
  $max_backlog = $sk->sk_max_ack_backlog;
  if ($backlog > $max_backlog) {
    time("%H:%M:%S ");
    printf("TCP Overflow: client %s:%d -> listener port %d | backlog %d/%d\n",
      ntop($sk->__sk_common.skc_daddr),
      ntohs($sk->__sk_common.skc_dport),
      $sk->__sk_common.skc_num,
      $backlog,
      $max_backlog
    );
  }
}'
```

This one-liner will immediately log any TCP accept queue overflows to your terminal, providing instant visibility during live incident management.

## Remediating Backlog Overflows: Server and Client Strategies

Once you have confirmed that TCP backlog queue overflows are causing your gRPC latency spikes, you need to apply remediation strategies on both the server and the client side.

### 1. Server-Side Remediation: Tuning the OS and the Application

To handle larger connection bursts, you must increase the capacity of the Accept Queue on the server. This requires adjusting both the system-wide limits and the application's configuration.

First, update the system-wide limits via sysctl:

```bash
# Temporarily increase limits to 4096
sysctl -w net.core.somaxconn=4096
sysctl -w net.ipv4.tcp_max_syn_backlog=4096

# Persist changes across reboots
echo "net.core.somaxconn=4096" >> /etc/sysctl.d/99-networking.conf
echo "net.ipv4.tcp_max_syn_backlog=4096" >> /etc/sysctl.d/99-networking.conf
sysctl --system
```

Next, you must ensure that your application passes a matching backlog value when calling `listen()`. In Go, the `net.Listen` package automatically reads the `/proc/sys/net/core/somaxconn` file on startup and uses it as the backlog argument. However, if your application runs inside a container, Go may read the container namespace's default `somaxconn` (which is often set to `128` by Docker/Kubernetes) instead of the host machine's tuned configuration.

If you cannot change the container or host-level sysctl values easily, you can bypass Go's standard listener creation and bind to the socket manually using syscalls to force a specific backlog size:

// snippet-6
```go
// listener.go
package main

import (
	"fmt"
	"net"
	"syscall"
	"golang.org/x/sys/unix"
)

// CreateListenerWithBacklog builds a net.Listener with an explicit listen backlog.
// This bypasses the default Go behavior of reading /proc/sys/net/core/somaxconn.
func CreateListenerWithBacklog(addr string, backlog int) (net.Listener, error) {
	tcpAddr, err := net.ResolveTCPAddr("tcp", addr)
	if err != nil {
		return nil, fmt.Errorf("resolve tcp addr failed: %w", err)
	}

	family := unix.AF_INET
	if tcpAddr.IP.To4() == nil {
		family = unix.AF_INET6
	}

	// Create raw system socket
	fd, err := unix.Socket(family, unix.SOCK_STREAM, unix.IPPROTO_TCP)
	if err != nil {
		return nil, fmt.Errorf("socket creation failed: %w", err)
	}

	// Set SO_REUSEADDR to avoid address-already-in-use errors on restarts
	if err := unix.SetsockoptInt(fd, unix.SOL_SOCKET, unix.SO_REUSEADDR, 1); err != nil {
		unix.Close(fd)
		return nil, fmt.Errorf("setsockopt SO_REUSEADDR failed: %w", err)
	}

	// Resolve system socket address struct
	var sa unix.Sockaddr
	if family == unix.AF_INET {
		sa4 := &unix.SockaddrInet4{Port: tcpAddr.Port}
		copy(sa4.Addr[:], tcpAddr.IP.To4())
		sa = sa4
	} else {
		sa6 := &unix.SockaddrInet6{Port: tcpAddr.Port}
		copy(sa6.Addr[:], tcpAddr.IP.To16())
		sa = sa6
	}

	// Bind address to socket
	if err := unix.Bind(fd, sa); err != nil {
		unix.Close(fd)
		return nil, fmt.Errorf("bind failed: %w", err)
	}

	// Execute listen system call with explicit backlog
	if err := unix.Listen(fd, backlog); err != nil {
		unix.Close(fd)
		return nil, fmt.Errorf("listen failed: %w", err)
	}

	// Convert file descriptor back to a standard net.Listener
	file := net.NewFile(uintptr(fd), "grpc-listener")
	defer file.Close()

	listener, err := net.FileListener(file)
	if err != nil {
		return nil, fmt.Errorf("file listener conversion failed: %w", err)
	}

	return listener, nil
}
```

You can then pass this custom listener directly to your gRPC server startup sequence:

```go
listener, err := CreateListenerWithBacklog(":50051", 4096)
if err != nil {
    log.Fatalf("Failed to listen: %v", err)
}
grpcServer := grpc.NewServer()
// Register services...
grpcServer.Serve(listener)
```

### 2. Client-Side Remediation: Correlating Latency and Tuning Pools

On the client side, you must correlate connection times with overall request latency. Since the server does not record the time spent by the connection waiting in the backlog queue, the client must capture the TCP dial time explicitly. 

Using OpenTelemetry in Go, you can write a custom `grpc.WithContextDialer` that measures the exact duration of the TCP connection establishment and injects it into trace spans. If the dial time exceeds 500ms, it adds a warning event to the span, signaling a possible server-side queue overflow.

// snippet-7
```go
// dialer.go
package main

import (
	"context"
	"net"
	"time"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/trace"
	"google.golang.org/grpc"
)

// DialTimeTracker returns a custom gRPC DialOption that measures TCP connection latency
// and records it as a span event if it exceeds a threshold (e.g., 500ms).
func DialTimeTracker(tracer trace.Tracer) grpc.DialOption {
	return grpc.WithContextDialer(func(ctx context.Context, addr string) (net.Conn, error) {
		span := trace.SpanFromContext(ctx)
		start := time.Now()
		
		dialer := &net.Dialer{
			Timeout: 3 * time.Second,
		}
		
		conn, err := dialer.DialContext(ctx, "tcp", addr)
		duration := time.Since(start)
		
		if span.IsRecording() {
			span.SetAttributes(
				attribute.String("tcp.dial.duration", duration.String()),
				attribute.Int64("tcp.dial.duration_ms", duration.Milliseconds()),
			)
			// A connection time over 500ms indicates a TCP retransmission delay (RTO default is 1s)
			if duration > 500*time.Millisecond {
				span.AddEvent("tcp_dial_slow", trace.WithAttributes(
					attribute.String("warning", "Possible listen backlog queue overflow on server"),
					attribute.Int64("duration_ms", duration.Milliseconds()),
					attribute.String("destination", addr),
				))
			}
		}
		return conn, err
	})
}
```

To use this dialer when initializing your client connection:

```go
conn, err := grpc.Dial(
    "server-address:50051",
    grpc.WithTransportCredentials(insecure.NewCredentials()),
    DialTimeTracker(myOTelTracer),
)
```

Additionally, apply these client configurations to prevent connection storms:
- **Jittered Backoff:** When reconnecting, ensure that your client uses a truncated exponential backoff algorithm with full jitter. If a cluster of 1000 instances reconnects at exactly the same time, the server backlog will immediately overflow. Adding random jitter spreads the connection requests across a wider window.
- **Connection Pooling:** Limit the maximum number of concurrent new connection attempts. Instead of spinning up hundreds of transient connections, reuse connections and use HTTP/2 streams effectively.
- **Keep-Alives:** Configure gRPC keep-alive parameters (`grpc.KeepaliveParams` in Go) to maintain active connections during low-traffic periods. This prevents the client from tearing down and reconstructing connections, minimizing the frequency of TCP handshakes.

## Summary

When diagnosing tail latency in high-performance gRPC systems, remember that application-level instrumentation has a blind spot at the kernel socket layer. When the Accept Queue overflows, the server silently ignores connection completion, leaving the client stuck waiting for a 1-second TCP Retransmission Timeout. By writing surgical eBPF programs using BCC or deploying ad-hoc `bpftrace` commands, you can inspect socket-level backlog depth in real-time, trace dropped packets, and correlate them with client-side dial telemetry. Tuning `/proc/sys/net/core/somaxconn` and bypassing standard library listener limitations with raw socket system calls ensures that your backend infrastructure can seamlessly absorb client-side connection storms.