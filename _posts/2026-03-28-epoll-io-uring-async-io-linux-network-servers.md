---
layout: post
title: "Epoll and io_uring: Async I/O Models Powering Modern Linux Network Servers"
date: 2026-03-28 08:00:00 +0700
tags: [linux, performance, networking, systems, rust]
description: "A deep technical comparison of epoll and io_uring for backend engineers building high-throughput Linux network servers."
image: ""
thumbnail: ""
---

At 50,000 concurrent connections your nginx holds steady; at 500,000 it buckles—not because the CPU is saturated, but because each `epoll_wait` cycle is flushing TLB entries, churning through a ready-list, and then issuing a separate `read()` syscall per socket. That's the syscall-per-event tax, and it compounds at scale. The move from `select` to `epoll` in the early 2000s bought a decade of headroom. The move from `epoll` to `io_uring`—available since Linux 5.1 (2019), production-hardened by 5.10 LTS—is the next leap. Understanding *why* each model is designed the way it is will help you pick the right one, tune it properly, and avoid the landmines that production teaches you the hard way.

![Epoll and io_uring: Async I/O Models Powering Modern Linux Network Servers Diagram](/images/diagrams/epoll-io-uring-async-io-linux-network-servers.svg)

## How epoll Actually Works

`epoll` is a readiness-notification interface. The kernel maintains a red-black tree of watched file descriptors and an internal linked list of ready events. When a socket becomes readable—say, a TCP segment arrives and the receive buffer fills—the kernel appends the fd to the ready list. Your `epoll_wait()` call drains that list into a user-space array and returns.

```c
// snippet-1
// Minimal epoll server loop — production skeleton in C
int epfd = epoll_create1(EPOLL_CLOEXEC);

struct epoll_event ev = {
    .events = EPOLLIN | EPOLLET,  // edge-triggered
    .data.fd = listen_fd,
};
epoll_ctl(epfd, EPOLL_CTL_ADD, listen_fd, &ev);

struct epoll_event events[MAX_EVENTS];
for (;;) {
    int n = epoll_wait(epfd, events, MAX_EVENTS, -1);
    for (int i = 0; i < n; i++) {
        int fd = events[i].data.fd;
        if (fd == listen_fd) {
            // accept loop until EAGAIN (edge-triggered requires draining)
            while (1) {
                int conn = accept4(listen_fd, NULL, NULL, SOCK_NONBLOCK | SOCK_CLOEXEC);
                if (conn == -1 && (errno == EAGAIN || errno == EWOULDBLOCK)) break;
                register_connection(epfd, conn);
            }
        } else {
            handle_client(fd);  // issues its own read()/write() calls
        }
    }
}
```

The critical detail is **edge-triggered (ETELLIN) vs. level-triggered (EPOLLIN)**. Level-triggered (default) re-notifies you on every `epoll_wait` as long as data is available—which means if you do a short read, you'll hear about it again. Edge-triggered fires once when the state transitions from unreadable to readable. ET forces you to drain the socket completely on each notification (loop until `EAGAIN`), but it eliminates spurious wakeups and scales better under high connection churn.

The cost: every notification still requires a separate `read()` or `write()` syscall, crossing the user-kernel boundary each time. At 100K events/second, that's 100K mode switches. Each context switch on a modern x86 processor costs 1–3 μs plus cache/TLB disruption. The math hurts.

## Edge Cases That Bite in Production

**The thundering herd on `accept()`**: Before Linux 4.5's `EPOLLEXCLUSIVE`, multiple threads calling `epoll_wait` on the same epfd would all wake on a new connection, then race to `accept()`, with all but one returning `EAGAIN`. Fix: use `EPOLLEXCLUSIVE` or a per-thread epfd with SO_REUSEPORT.

**EPOLLRDHUP**: Without this flag, you won't detect half-closed connections (peer sent FIN) until you attempt a read. Add `EPOLLRDHUP` to your event mask and handle it explicitly—otherwise you'll accumulate zombie connections that appear open but deliver EOF on every read attempt.

**Stale fds after `dup2()`**: If you `dup()` a file descriptor and close the original, the epoll interest list still holds the duplicated fd. The kernel tracks fds by open-file-description, not fd number, so you'll keep getting events from a fd you thought you closed. Always `EPOLL_CTL_DEL` before closing.

```python
# snippet-2
# Python asyncio internals — epoll under the hood (CPython's DefaultEventLoopPolicy on Linux)
import asyncio
import socket

async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    while True:
        data = await reader.read(65536)   # translates to epoll_wait + read()
        if not data:
            break
        writer.write(data)
        await writer.drain()              # translates to epoll_wait for writability
    writer.close()

async def main():
    server = await asyncio.start_server(
        handle_client,
        host="0.0.0.0",
        port=8080,
        reuse_port=True,          # SO_REUSEPORT: one epfd per worker thread
        backlog=4096,
    )
    async with server:
        await server.serve_forever()

asyncio.run(main())
# Under the hood: SelectorEventLoop → EpollSelector → epoll_ctl/epoll_wait
# Each awaited read/write is a registered EPOLLIN/EPOLLOUT event + syscall round-trip
```

## Enter io_uring: Submission/Completion Rings

`io_uring` flips the model. Instead of asking the kernel "which fds are ready?"—then issuing separate I/O calls—you submit *operations* to the kernel and poll for *completions*. The interface is a pair of lock-free ring buffers shared between user space and kernel via `mmap`: the Submission Queue (SQ) and the Completion Queue (CQ).

You populate a Submission Queue Entry (SQE) describing an operation—`IORING_OP_RECV`, `IORING_OP_SEND`, `IORING_OP_ACCEPT`, even `IORING_OP_OPENAT` or `IORING_OP_STATX`—then tell the kernel (via `io_uring_enter()`) or let it poll. The kernel executes the operation asynchronously, possibly on a kernel thread pool, and posts a Completion Queue Entry (CQE) with the result code.

```c
// snippet-3
// io_uring echo server using liburing — production-quality skeleton
#include <liburing.h>
#include <netinet/in.h>
#include <stdio.h>
#include <string.h>

#define QUEUE_DEPTH 256
#define BUFFER_SIZE 4096

enum op_type { OP_ACCEPT, OP_RECV, OP_SEND };

struct conn_info {
    int fd;
    enum op_type type;
    char buf[BUFFER_SIZE];
};

static void add_accept(struct io_uring *ring, int listen_fd, struct sockaddr_in *addr, socklen_t *addrlen) {
    struct io_uring_sqe *sqe = io_uring_get_sqe(ring);
    io_uring_prep_accept(sqe, listen_fd, (struct sockaddr *)addr, addrlen, SOCK_NONBLOCK);
    struct conn_info *ci = calloc(1, sizeof(*ci));
    ci->fd = listen_fd;
    ci->type = OP_ACCEPT;
    io_uring_sqe_set_data(sqe, ci);
}

static void add_recv(struct io_uring *ring, int fd) {
    struct io_uring_sqe *sqe = io_uring_get_sqe(ring);
    struct conn_info *ci = calloc(1, sizeof(*ci));
    ci->fd = fd;
    ci->type = OP_RECV;
    io_uring_prep_recv(sqe, fd, ci->buf, BUFFER_SIZE, 0);
    io_uring_sqe_set_data(sqe, ci);
}

int main(void) {
    struct io_uring ring;
    // IORING_SETUP_SQPOLL: kernel thread polls SQ — zero syscalls at steady state
    // Requires CAP_SYS_NICE or unprivileged io_uring (Linux 5.11+)
    struct io_uring_params params = { .flags = IORING_SETUP_SQPOLL, .sq_thread_idle = 2000 };
    io_uring_queue_init_params(QUEUE_DEPTH, &ring, &params);

    int listen_fd = setup_listen_socket(8080);  // standard socket/bind/listen
    struct sockaddr_in client_addr;
    socklen_t addrlen = sizeof(client_addr);
    add_accept(&ring, listen_fd, &client_addr, &addrlen);
    io_uring_submit(&ring);

    struct io_uring_cqe *cqe;
    while (1) {
        io_uring_wait_cqe(&ring, &cqe);  // blocks until at least one completion
        struct conn_info *ci = io_uring_cqe_get_data(cqe);

        if (ci->type == OP_ACCEPT && cqe->res > 0) {
            add_recv(&ring, cqe->res);          // new connection: queue RECV
            add_accept(&ring, listen_fd, &client_addr, &addrlen);  // re-arm accept
        } else if (ci->type == OP_RECV && cqe->res > 0) {
            // echo back — queue SEND (not shown for brevity)
            add_recv(&ring, ci->fd);            // re-arm for next message
        }

        io_uring_cqe_seen(&ring, cqe);          // advance CQ tail
        io_uring_submit(&ring);
    }
}
```

## SQPOLL: The Zero-Syscall Mode

The deepest `io_uring` optimization is `IORING_SETUP_SQPOLL`. With this flag, the kernel spawns a dedicated kernel thread that busy-polls the SQ ring. Your application writes SQEs directly—without ever calling `io_uring_enter()`—and the kernel thread picks them up. At true steady state (the poller thread stays awake), you make **zero syscalls** for I/O operations. The kernel poller goes to sleep after `sq_thread_idle` milliseconds of inactivity and wakes on your next `io_uring_enter()`.

The trade-off: the poller thread burns a CPU core. This only makes sense when your server is continuously saturated. For bursty workloads, stick to non-SQPOLL mode—batch submissions instead by calling `io_uring_submit()` once after populating multiple SQEs.

Benchmarks from the Tokio io-uring adapter show ~40% fewer context switches versus epoll at 200K RPS on a single core, translating to 8–12% lower CPU at equivalent throughput. For file I/O—which was never truly async before io_uring even with `O_NONBLOCK` (Linux just buffered it and pretended)—the gains are far larger: `io_uring` is the first genuinely async file I/O interface on Linux.

## Rust and io_uring: glommio and monoio

The Rust ecosystem has moved fastest on io_uring. Two runtimes worth knowing:

**glommio** (DataDog, thread-per-core): Uses io_uring exclusively, pins each thread to a core, no work-stealing, no `Arc<Mutex<>>`. It achieves sub-100μs P99 latency for storage-intensive workloads because there's no cross-thread locking anywhere in the hot path.

**monoio** (ByteDance): Also thread-per-core, io_uring native, with a compatibility layer for tokio types. Production-tested inside ByteDance at millions of RPS.

```rust
// snippet-4
// glommio HTTP-style server skeleton — io_uring, thread-per-core
use glommio::{net::TcpListener, LocalExecutorBuilder, Placement};
use futures_lite::io::{AsyncReadExt, AsyncWriteExt};

fn main() {
    // Spawn one executor per logical core, pinned (Placement::Fixed(core_id))
    let handles: Vec<_> = (0..num_cpus::get())
        .map(|core| {
            LocalExecutorBuilder::new(Placement::Fixed(core))
                .spawn(|| async move {
                    let listener = TcpListener::bind("0.0.0.0:8080").unwrap();
                    loop {
                        let (mut stream, _) = listener.accept().await.unwrap();
                        // glommio's task model: cooperative, single-threaded per executor
                        glommio::spawn_local(async move {
                            let mut buf = vec![0u8; 8192];
                            loop {
                                let n = stream.read(&mut buf).await.unwrap_or(0);
                                if n == 0 { break; }
                                // All I/O submits SQEs; completions via CQE polling
                                stream.write_all(&buf[..n]).await.unwrap();
                            }
                        }).detach();
                    }
                })
                .unwrap()
        })
        .collect();

    for h in handles { h.join().unwrap(); }
}
```

## Choosing the Right Model for Production

**Use epoll when:**
- You're deploying on kernels older than 5.10 (RHEL 8, Ubuntu 20.04 with stock kernel).
- You're maintaining a codebase built on libuv, asyncio, or any mature event loop—rewriting the I/O layer for marginal gains is rarely worth it.
- Your bottleneck is application logic, not syscall overhead. Profile first.
- Connection counts are under ~100K. epoll's overhead at this scale is negligible.

**Use io_uring when:**
- You're building a new service and can target Linux 5.15+ (Ubuntu 22.04, RHEL 9).
- You're doing significant file I/O—log ingestion, local storage engines, WAL writers.
- You need predictable low latency: io_uring's fixed-buffer mode (`IORING_OP_READ_FIXED`) eliminates per-operation buffer registration overhead.
- You're writing Rust and can adopt glommio or monoio from the start.

**Hybrid is viable**: Tokio (Rust's dominant async runtime) has an `io-uring` feature flag that transparently replaces its epoll backend with io_uring while keeping the same API surface. You can run the same application code and just swap the reactor.

```bash
# snippet-5
# Verify io_uring availability and limits on your kernel
$ uname -r
5.15.0-100-generic

# Check if SQPOLL is available (requires CONFIG_IO_URING=y)
$ cat /boot/config-$(uname -r) | grep IO_URING
CONFIG_IO_URING=y

# Check the per-process locked memory limit (io_uring requires locked pages for ring buffers)
$ ulimit -l
unlimited   # must not be 64 (default) — increase via /etc/security/limits.conf

# Monitor io_uring operations in production
$ sudo bpftrace -e '
  tracepoint:io_uring:io_uring_submit_sqe {
    @ops[args->opcode] = count();
  }
  interval:s:5 { print(@ops); clear(@ops); }
'
```

## Fixed Buffers and Registered Files

Two often-overlooked io_uring features that matter at high throughput:

**Registered buffers** (`io_uring_register_buffers`): Pre-pins user-space memory with the kernel so it doesn't need to pin/unpin pages on every I/O operation. At 500K small reads/second, the page-pinning overhead is measurable. With fixed buffers, use `IORING_OP_READ_FIXED` / `IORING_OP_WRITE_FIXED` and pass the buffer index.

**Registered files** (`io_uring_register_files`): Submitting operations by fd requires the kernel to look up the open-file-description in the fd table on every SQE. Registering your fd table upfront lets the kernel cache these lookups. On servers with thousands of persistent connections, this is a real win.

```c
// snippet-6
// io_uring registered buffers and files — eliminating per-op overhead
struct io_uring ring;
io_uring_queue_init(QUEUE_DEPTH, &ring, 0);

// Register a fixed buffer pool (e.g., 1024 x 4KB buffers)
#define NUM_BUFS 1024
#define BUF_SIZE 4096
char *bufs = mmap(NULL, NUM_BUFS * BUF_SIZE, PROT_READ|PROT_WRITE,
                  MAP_PRIVATE|MAP_ANONYMOUS, -1, 0);
struct iovec iovs[NUM_BUFS];
for (int i = 0; i < NUM_BUFS; i++) {
    iovs[i].iov_base = bufs + i * BUF_SIZE;
    iovs[i].iov_len  = BUF_SIZE;
}
io_uring_register_buffers(&ring, iovs, NUM_BUFS);  // pins pages once

// Register file descriptors
int fds[MAX_CONNS];  // fill with your connection fds
io_uring_register_files(&ring, fds, MAX_CONNS);

// Use IORING_OP_READ_FIXED + buf_index instead of IORING_OP_READ
struct io_uring_sqe *sqe = io_uring_get_sqe(&ring);
io_uring_prep_read_fixed(sqe,
    file_index,    // index into registered file table
    iovs[buf_idx].iov_base,
    BUF_SIZE,
    0,             // offset
    buf_idx        // index into registered buffer table
);
sqe->flags |= IOSQE_FIXED_FILE;  // use registered fd
```

## What Doesn't Change

Neither model removes the need to think carefully about backpressure. An overloaded server that can't `accept()` fast enough will exhaust the listen backlog regardless of whether your I/O model is epoll or io_uring—the kernel will start dropping SYN packets at `tcp_max_syn_backlog`. Set your `backlog` parameter properly (`SO_RCVBUF`, `net.core.somaxconn`, `net.ipv4.tcp_max_syn_backlog`) and measure your accept latency under load before attributing tail latency to your I/O model.

Profiling remains the arbiter. Before migrating from epoll to io_uring, run `perf stat -e syscalls:sys_enter_epoll_wait,syscalls:sys_enter_read,syscalls:sys_enter_write` on your production workload. If syscall overhead doesn't appear in your top-10 CPU consumers, the migration will not move your latency numbers. io_uring is a powerful tool—but like any optimization, it only matters where the actual bottleneck is.
