---
layout: post
title: "Go Netpoller Internals: How the Runtime Multiplexes Network I/O with Epoll and Kqueue"
date: 2026-06-20 08:00:00 +0700
tags: [go, networking, performance, architecture]
description: "A deep dive into Go's netpoller internals, exploring how the runtime uses epoll/kqueue, manages goroutine states, and coordinates with the scheduler."
image: "https://picsum.photos/seed/4259/1080/720"
thumbnail: "https://picsum.photos/seed/4259/400/300"
---

In production, when a Go-based microservice scales to handle hundreds of thousands of concurrent connections—such as a WebSocket gateway or a high-throughput gRPC proxy—the traditional thread-per-connection model falls off a performance cliff. If each connection required a dedicated operating system thread, the kernel would waste gigabytes of memory on thread stacks (which default to 8MB in Linux) and drown in CPU context-switch overhead. The Go runtime avoids this architectural trap using the **Netpoller**: an internal, asynchronous I/O multiplexer that bridges the gap between Go’s synchronous, blocking programming model and OS-level non-blocking, event-driven interfaces (`epoll` on Linux, `kqueue` on macOS/BSD). By mapping thousands of logical network connections onto a tiny pool of operating system threads, the netpoller keeps memory usage down to roughly 2KB to 8KB per connection (representing the initial goroutine stack) and schedules CPU cores dynamically only when packets actually arrive.

![Go Netpoller Internals: How the Runtime Multiplexes Network I/O with Epoll and Kqueue Diagram](/images/diagrams/go-netpoller-internals-epoll-kqueue-io-multiplexing.svg)

## The Design of the Netpoller and Goroutine State Transitions

When a goroutine invokes a blocking network call like `conn.Read()` or `conn.Write()`, the Go runtime does not block the underlying operating system thread (`M`). Instead, it executes a system call that operates on a file descriptor configured for non-blocking I/O (`O_NONBLOCK`). If the socket buffer is empty (for reads) or full (for writes), the kernel returns an `EAGAIN` or `EWOULDBLOCK` error.

The netpoller intercepts this error. If the file descriptor is not yet registered with the runtime's multiplexer, it registers it using `epoll_ctl` (or `kevent`). The runtime then transitions the calling goroutine (`G`) from the `_Grunning` state to the `_Gwaiting` state, with the wait reason set to `waitReasonIOPoll`. 

This parking mechanism is driven by `gopark`. The scheduler disassociates the parked goroutine from the physical thread (`M`) and logical processor (`P`), freeing the processor to execute other runnable goroutines. The block is tracked by a runtime struct named `pollDesc`, which bridges the file descriptor to the goroutine pointers.

Inside `runtime.pollDesc`, two critical fields coordinate synchronization:
- `rg`: The read goroutine pointer. It can contain a pointer to a parked goroutine (`g`), a ready state (`pdReady = 1`), or a waiting state (`pdWait = 2`).
- `wg`: The write goroutine pointer, behaving identically for write operations.

When network I/O becomes ready, the netpoller transitions the parked goroutine back to `_Grunnable` using `goready`. The scheduler then appends it to the local or global run queue.

To show what Go abstracts under the hood, Snippet 1 demonstrates a raw, non-blocking TCP socket server written in Go using direct Linux `syscall` primitives. It mimics how the netpoller configures descriptors and polls them in user space.

<script src="https://gist.github.com/mohashari/f635bbd54f2f78dda1cc5a37999d35a4.js?file=snippet-1.go"></script>

## Epoll and Kqueue: OS-level I/O Multiplexing

The core of the netpoller relies on OS-specific system calls. On Linux systems, this is `epoll`; on macOS, BSD, and iOS, it is `kqueue`. 

A critical design choice in the Go netpoller is the use of **Edge-Triggered (ET)** mode for epoll (`EPOLLET`). In contrast to **Level-Triggered (LT)** mode (which repeatedly alerts the caller as long as a socket buffer contains unread bytes), Edge-Triggered mode only alerts the caller when a state transition occurs (e.g., when new data arrives at the network interface). 

This design choice has significant implications:
1. **Fewer System Calls**: The runtime is not bombarded with event notifications when reading massive payloads in chunks.
2. **Strict Loop Handling**: User-space code must drain the network buffer completely until it receives `EAGAIN` or `EWOULDBLOCK`. Failing to do so causes the netpoller to stop receiving updates for that file descriptor, leading to connection timeouts.

To ensure production stability, network sockets must be tuned at the OS level. Standard TCP sockets in Go can be optimized using `syscall.RawConn` to bypass the high-level `net.Conn` abstractions and interact with the kernel socket buffer configuration directly. Snippet 2 illustrates how to tune TCP keep-alives and disable Nagle’s algorithm on Linux/macOS.

<script src="https://gist.github.com/mohashari/f635bbd54f2f78dda1cc5a37999d35a4.js?file=snippet-2.go"></script>

For BSD-based platforms (including macOS), Go compiles platform-specific polling code utilizing `kqueue`. While similar in spirit to epoll, kqueue uses `kevent` structures to track read, write, and signal events on a single descriptor interface. Snippet 3 demonstrates a production-grade macOS kqueue listener written in Go using raw syscalls.

<script src="https://gist.github.com/mohashari/f635bbd54f2f78dda1cc5a37999d35a4.js?file=snippet-3.go"></script>

## Deep Dive into Runtime Code: `pollDesc` and the Network Polling Loop

The lifetime of runtime-managed connections is routed through the runtime netpoll loop. The scheduler interacts with the netpoller at strategic locations inside the execution lifecycle to avoid stalling operating system threads. 

### The Network Polling Points

There are three primary entry points where the Go runtime polls the network using `runtime.netpoll`:
1. **`findrunnable()`**: When a thread (`M`) running inside a logical processor context (`P`) has exhausted its local run queue, the global run queue, and failed to steal work from other processors, it calls `netpoll(block=true)`. This call blocks the current thread inside `epoll_wait` or `kevent` until an I/O event is processed, preventing the processor from wasting CPU cycles in spin loops.
2. **`sysmon` (System Monitor Thread)**: The runtime spawns a background thread `sysmon` that runs without a logical processor `P`. It checks the state of the system every 10 microseconds to 10 milliseconds. If it detects that the runtime has not polled the network for more than 10 milliseconds, it injects a non-blocking `netpoll(block=false)` call to drain pending network events and distribute unblocked goroutines onto the global run queue.
3. **GC (Garbage Collector) STW / Scheduling Ticks**: During garbage collection phases or scheduler cycle updates (every 61 ticks), the scheduler calls `netpoll(block=false)` to ensure network worker threads are fed.

One of the most insidious issues in high-scale backends is the silent leakage of file descriptors. If a service forgets to close a socket, the netpoller keeps tracking it, exhausting the kernel resources. Snippet 4 shows a diagnostics worker that dynamically checks active file descriptors by parsing the Linux `/proc` filesystem. It provides early warning alerts before the OS hits descriptor table limits.

<script src="https://gist.github.com/mohashari/f635bbd54f2f78dda1cc5a37999d35a4.js?file=snippet-4.go"></script>

## Production Failure Modes and Diagnostics

Operating high-throughput Go backends requires an understanding of how the netpoller behaves under stress and where its failure limits reside.

### 1. File Descriptor Exhaustion (`EMFILE`)
If a database connection pool is misconfigured, or if outbound HTTP requests fail to close response bodies, the service will consume all available file descriptors. Once the process limit is reached:
- New TCP accept calls return `EMFILE` errors immediately.
- The netpoller fails to register new connections.
- The service stops accepting traffic but stays alive, failing health checks.

### 2. Idle Connection Drop and Silent Hanging
When a stateful firewall or cloud load balancer drops idle TCP connections, it often fails to send a TCP `RST` packet to the host. The netpoller, operating with blocking abstractions, waits indefinitely for data that will never arrive. The goroutine remains parked in the `_Gwaiting` state. 

To mitigate this, production dialers must enforce strict timeouts and TCP keep-alives before the socket is returned to the standard library netpoller. Snippet 5 illustrates a production-ready dialer configuration.

<script src="https://gist.github.com/mohashari/f635bbd54f2f78dda1cc5a37999d35a4.js?file=snippet-5.go"></script>

### 3. Netpoller Diagnostics via System Tools

When debugging network issues under production load, you need to verify system limits, inspect TCP buffer sizes, and monitor active socket queues. Snippet 6 outlines essential bash commands to run on Linux servers hosting Go processes.

<script src="https://gist.github.com/mohashari/f635bbd54f2f78dda1cc5a37999d35a4.js?file=snippet-6.sh"></script>

### Diagnostic Summary for High-Load Environments

| Metric | Target | Failure Symptom | Diagnostics Tool |
|---|---|---|---|
| Open File Descriptors | Less than 80% of `ulimit -n` | `socket: too many open files` | `lsof -p <PID>` |
| TCP Connection Recv-Q | 0 (or close to 0) | High application latency | `ss -lnt` |
| `runtime.NumGoroutine` | Flat line / cyclical | Exponential growth (leaking wait queues) | `pprof /debug/pprof/goroutine` |
| Thread Count | Close to CPU Core Count | Excessive threads (blocked on sys calls) | `ps -o nlwp <PID>` |

By validating socket closures, monitoring file descriptor allocations, and tuning TCP keep-alives, engineers can leverage Go's netpoller to build services capable of running on minimal infrastructure footprint without sacrificing connection density.