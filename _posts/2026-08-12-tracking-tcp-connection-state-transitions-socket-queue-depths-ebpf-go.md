---
layout: post
title: "Tracking TCP Connection State Transitions and Socket Queue Depths using eBPF in Go"
date: 2026-08-12 08:00:00 +0700
tags: [ebpf, go, tcp, networking, observability]
description: "Diagnose silent network drops, listen backlog overflows, and connection leaks by tracing kernel TCP state transitions and socket queue depths using eBPF and Go."
image: "/images/diagrams/tracking-tcp-connection-state-transitions-socket-queue-depths-ebpf-go.svg"
thumbnail: "/images/diagrams/tracking-tcp-connection-state-transitions-socket-queue-depths-ebpf-go.svg"
---

Your high-throughput API gateway is processing 80,000 requests per second. Suddenly, client services report brief, intermittent bursts of connection timeouts—specifically, connection refused and 504 gateway timeout errors. Yet, your application dashboard is a sea of green: CPU usage sits at a comfortable 40%, memory consumption is linear, Go routine counts are stable, and the service's p99 application latency looks entirely normal. Standard monitoring tools like `netstat` and `ss` only give you point-in-time statistics or aggregate counters that bury transient spikes in average values. Under the hood, the Linux kernel's TCP listen backlog is silently overflowing, dropping incoming `SYN` packets before the Go runtime is even aware of their existence. To diagnose these transient network drops, connection leaks, and socket-level bottlenecks in production, you must bypass the user-space runtime entirely and extract real-time connection state transitions and socket queue depths directly from the Linux kernel using eBPF and Go.

![Tracking TCP Connection State Transitions and Socket Queue Depths using eBPF in Go Diagram](/images/diagrams/tracking-tcp-connection-state-transitions-socket-queue-depths-ebpf-go.svg)

## The Invisible Queue Bottleneck: Why Application Metrics Lie

When a client initiates a TCP connection to your service, the kernel network stack handles the connection handshake before presenting it to your Go application. Understanding this pipeline is critical to understanding where observability breaks down:

1. **The SYN Queue:** Upon receiving an initial `SYN` packet, the kernel puts the connection in the SYN queue (half-open connections) and replies with a `SYN-ACK`. The size of this queue is bounded by `/proc/sys/net/ipv4/tcp_max_syn_backlog`.
2. **The Accept Queue (Listen Backlog):** Once the client replies with an `ACK`, the connection transitions to the `ESTABLISHED` state. The kernel then moves the socket to the Accept Queue. The application (in this case, your Go program running `net.Listener.Accept()`) pulls connections from this queue.
3. **Queue Bounding:** The Accept Queue's size is determined by the `backlog` argument passed to the `listen` system call, capped by the system-wide limit in `/proc/sys/net/core/somaxconn`.

If your Go runtime experiences a brief Garbage Collection (GC) pause, scheduling latency, or is temporarily CPU-starved, the speed at which it calls `Accept()` drops. Incoming connections continue to accumulate in the Accept Queue. When this queue reaches its limit (which defaults to a mere 128 on many older Linux distributions, though modern ones often set it to 4096), the kernel begins dropping incoming packets.

Depending on the value of `/proc/sys/net/ipv4/tcp_abort_on_overflow`, the kernel will either silently ignore the incoming `ACK` (forcing the client to retransmit and causing latency spikes) or send a `RST` packet (causing an immediate "connection refused" error). In both cases, the Go runtime has no record of these attempts because they were rejected before reaching user space.

A parallel issue exists for established connections: the receive and transmit socket queues.

* **Receive Queue (`sk_receive_queue`):** Holds bytes that have arrived over the wire and have been acknowledged by the kernel, but have not yet been consumed by the Go application via `conn.Read()`. A growing receive queue indicates that the application layer is slow to process data.
* **Write Queue (`sk_write_queue`):** Holds bytes that the application has sent via `conn.Write()` but are waiting to be transmitted or are waiting for an `ACK` from the client. A growing write queue indicates network congestion, packet loss, or a slow remote receiver.

Relying on user-space metrics to detect these issues is a dead end. By the time a connection leak or queue overflow affects application-level metrics, your system is already dropping traffic. We need to hook directly into the kernel's state transition function, `tcp_set_state`, and read socket descriptors in real time.

## Leveraging eBPF for TCP Observability: fentry vs kprobes

To trace connection states and queue depths, we hook into the kernel function `tcp_set_state(struct sock *sk, int state)`. Historically, eBPF developers relied on `kprobes` (kernel probes) to hook arbitrary kernel functions. While powerful, `kprobes` have two major disadvantages in high-throughput production environments:

1. **Overhead:** A `kprobe` works by replacing the target function's first instruction with a breakpoint instruction (e.g., `int3` on x86). When hit, this triggers a kernel trap, switches execution context to the probe handler, and switches back. This context switching adds measurable CPU overhead.
2. **Argument Access:** In a `kprobe`, function parameters are not typed. You must extract them manually from the registers using architecture-specific macros (like `PT_REGS_PARM1` for the first argument), which increases complexity and reduces portability.

Modern Linux kernels (version 5.5 and later) support **BPF Trampolines** via `fentry` (function entry) and `fexit` (function exit). A BPF trampoline allows the kernel to call our eBPF program directly using a compiler-inserted `nop` instruction (re-routed using `ftrace`), bypassing the expensive breakpoint trap entirely. Furthermore, `fentry` probes are fully typed, allowing us to access function arguments like `struct sock *sk` directly, as if we were writing native kernel C code.

To ensure our compiled eBPF code can run across different kernel minor versions without recompilation, we use **CO-RE (Compile Once – Run Everywhere)**. Enabled by BTF (BPF Type Format), CO-RE allows the eBPF loader to adjust member offsets within kernel structures dynamically at runtime. For instance, if the offset of `sk_receive_queue` within `struct sock` changes between kernel version 5.15 and 6.8, CO-RE rewrites the instruction offsets inside the eBPF bytecode during load time.

## Writing the eBPF Kernel Code

Let’s implement the eBPF kernel program. The code is written in C. We'll define the event payload structure, declare the ring buffer map used to pass events to user space, and write the `fentry` probe.

First, we define our headers and data structures in a file named `tcp_tracker.c`:

<script src="https://gist.github.com/mohashari/a6e2b846dec4945af248d41f1545eb01.js?file=snippet-1.txt"></script>

Next, we write the tracing program that attaches to the entry of `tcp_set_state`. Because this function is called immediately before the state is updated, `sk->sk_state` represents the *old* state, and the `state` parameter of `tcp_set_state` represents the *new* state.

<script src="https://gist.github.com/mohashari/a6e2b846dec4945af248d41f1545eb01.js?file=snippet-2.txt"></script>

## Compiling and Binding eBPF in Go

To compile this C program and interface with it from our Go application, we use Cilium’s `ebpf` package and its companion tool `bpf2go`. The `bpf2go` tool takes our C file, compiles it to BPF bytecode using `clang`, and generates Go files containing the binary data alongside loading boilerplate.

Create a file named `generate.go` in the same directory:

<script src="https://gist.github.com/mohashari/a6e2b846dec4945af248d41f1545eb01.js?file=snippet-3.go"></script>

Executing `go generate` will output several files, including `bpf_bpfel.go` (for little-endian systems) and `bpf_bpfel.o` (containing the raw ELF bytecode). These files export structures like `bpfObjects` and functions like `loadBpfObjects`, which handle loading the BPF program into the kernel.

Next, we write the Go initialization routine to unlock system resources and load the objects:

<script src="https://gist.github.com/mohashari/a6e2b846dec4945af248d41f1545eb01.js?file=snippet-4.go"></script>

## Streaming Events from Kernel to User Space

With the eBPF code loaded and hooked, we need to process the stream of events generated when TCP connections transition states. Using a standard lock-free Ring Buffer (`BPF_MAP_TYPE_RINGBUF`) is critical here. Unlike the older `BPF_MAP_TYPE_PERF_EVENT_ARRAY`, which allocates a buffer per CPU core and can suffer from out-of-order event delivery or high memory fragmentation, the modern ring buffer allocates a single memory space shared across all CPU cores. It is safer, guarantees FIFO ordering, and reduces memory consumption.

We read from the ring buffer in Go using `ringbuf.Reader`, parsing the raw byte slice directly into our Go representation of `tcp_event_t`:

<script src="https://gist.github.com/mohashari/a6e2b846dec4945af248d41f1545eb01.js?file=snippet-5.go"></script>

## Exposing TCP Queues to Prometheus

Printing logs to stdout is useful for development, but in production, we need these events exposed as metrics to scrape. We will write a Prometheus collector to export:

1. **State transition rates:** To detect routing loops, connection flapping, or high rates of ephemeral port exhaustion.
2. **Queue depths:** To track the sizes of socket queues when connections change state.

A critical state to track is `CLOSE_WAIT`. When a client closes a connection, the kernel transitions the socket to `CLOSE_WAIT` and notifies the application. If your Go code has a socket leak (e.g., forgetting to close an HTTP response body or db connection), the socket remains stuck in `CLOSE_WAIT` indefinitely. By exporting metrics of sockets entering `CLOSE_WAIT` with their respective queue depths, you can detect leaks hours before they exhaust your service's file descriptors.

<script src="https://gist.github.com/mohashari/a6e2b846dec4945af248d41f1545eb01.js?file=snippet-6.go"></script>

## Production Performance & Safety Considerations

Deploying eBPF code into high-throughput production workloads requires extreme caution. A poorly written BPF program can crash your kernel or severely degrade networking throughput. Here are the core production constraints you must design around:

### 1. The BPF Verifier
Before loading your program, the kernel runs it through the BPF Verifier to ensure safety. The verifier checks that:
* The program contains no unreachable code or infinite loops.
* All memory accesses are validated via safe bounds-checking helpers (like `BPF_CORE_READ`).
* The stack usage does not exceed the strict 512-byte limit.

If your event struct is too large or if you attempt to perform complex string parsing (e.g., formatting IP strings inside the C code), the verifier will reject your program at load time. Keep your C event structure compact, align its elements to 8-byte boundaries to avoid compiler-introduced padding issues, and delegate all human-readable formatting and IP decoding to user-space Go code.

### 2. Execution Overhead
Though `fentry` tracing is extremely fast, it still runs in the kernel context on the same CPU core executing the network path. 
* **Minimize memory operations:** Never do allocation inside the probe. Use `bpf_ringbuf_reserve` and `bpf_ringbuf_submit`.
* **Handle Ring Buffer Saturation:** If your Go service falls behind in reading from the ring buffer, `bpf_ringbuf_reserve` will return `NULL`. Do not block. Log a counter of dropped events inside user space and discard the event in kernel space immediately. A dropped monitoring event is always preferable to degrading the server's network stack.

### 3. Kernel Security and Permissions
Running eBPF code requires elevated privileges. Historically, this meant running your Go binary as `root` (or with `CAP_SYS_ADMIN`). In modern Linux systems (kernels 5.8+), you can isolate these permissions:
* Grant the executable `CAP_BPF` (to load BPF maps and programs).
* Grant `CAP_NET_ADMIN` (to attach probes to network structures).

Configure this inside your systemd service file or deployment configuration:

```yaml
# Example systemd service permissions slice
CapabilityBoundingSet=CAP_BPF CAP_NET_ADMIN
AmbientCapabilities=CAP_BPF CAP_NET_ADMIN
NoNewPrivileges=true
```

## Conclusion

By combining the speed of BPF `fentry` probes with the type safety of CO-RE and the concurrency model of Go, you gain complete visibility into the Linux TCP stack. Instead of guessing why clients are encountering intermittent timeouts, you can trace queue overflows and connection state changes back to specific local ports and remote hosts in real-time. This approach ensures you detect bottlenecks long before they impact your customer-facing latency dashboards.