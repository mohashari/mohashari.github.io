---
layout: post
title: "Designing a Lock-Free Ring Buffer for IPC with Shared Memory and POSIX Semaphores in C"
date: 2026-07-15 08:00:00 +0700
tags: [c, systems-programming, lock-free, ipc, performance]
description: "Build a zero-copy, cache-aligned SPSC ring buffer in C using shared memory, atomic operations, and POSIX semaphores for ultra-low latency IPC."
image: "https://picsum.photos/seed/5288/1080/720"
thumbnail: "https://picsum.photos/seed/5288/400/300"
---

If your IPC layer is costing you 20% of your CPU budget in context switches, you have built a bottleneck. Under high-throughput workloads—such as routing telemetry packets to a local daemon or pumping tick data to an execution engine—traditional Unix domain sockets (UDS) become an expensive luxury. At 2,000,000 messages per second, UDS forces millions of context switches per second, consuming massive CPU resources in scheduler overhead and memory copies. By bypassing the Linux kernel entirely and mapping a shared memory region (`shm_open` and `mmap`) directly into the address spaces of your processes, you can reduce transport latencies from microseconds to double-digit nanoseconds.

However, sharing memory introduces the problem of synchronization. Traditional POSIX mutexes (`pthread_mutex_t`) configured with `PTHREAD_PROCESS_SHARED` still fall back on kernel-level futex calls when contested. To achieve peak efficiency, we must build a lock-free Single-Producer Single-Consumer (SPSC) ring buffer that utilizes C11 atomic operations. But pure lock-free structures suffer from a critical flaw: they spin-wait, burning 100% CPU when the buffer is empty or full. 

This post details the design of a production-grade, zero-copy, cache-aligned SPSC ring buffer in C. We will pair C11 lock-free memory semantics with POSIX semaphores to create an adaptive synchronization mechanism that spins during low-latency bursts but parks threads cleanly during idle periods, all while protecting against process crashes and false sharing.

## Designing for the CPU Cache: Eliminating False Sharing

In modern symmetric multiprocessing (SMP) architectures, CPUs do not read and write to main memory directly. Instead, they fetch data in 64-byte blocks called cache lines into L1, L2, and L3 caches. If the producer process writes to a write pointer, and the consumer process writes to a read pointer, and both pointers reside within the same 64-byte block, the CPU cores will constantly invalidate each other's cache lines. This phenomenon, known as **false sharing** or cache line bouncing, can degrade IPC performance by an order of magnitude, turning nanosecond operations into memory-bus bottlenecked stalls.

To prevent this, we must lay out our shared memory structure such that variables modified by the producer and variables modified by the consumer reside on completely separate cache lines. We achieve this using explicit alignment attributes (`__attribute__((aligned(64)))` or C11's `alignas(64)`). 

Furthermore, we must design our ring buffer to minimize reads across the interconnect bus. Instead of querying the peer's atomic index on every transaction, each process maintains a local, non-atomic cached copy of the peer's cursor. The producer only reads the consumer's atomic read index when its local copy indicates that the buffer is full; the consumer only reads the atomic write index when its local copy indicates the buffer is empty.

Below is the header layout for our robust shared memory ring buffer:

<script src="https://gist.github.com/mohashari/7d3f27783955aba8c8b43b5cfb2e806a.js?file=snippet-1.txt"></script>

## Race-Free Shared Memory Initialization

Creating a shared memory segment requires coordinating which process allocates the memory and runs the initial setup. If both the producer and consumer start up simultaneously, they might race to initialize the segment. 

To handle this cleanly, we use `shm_open` with the `O_EXCL` flag. The process that successfully creates the file is designated the creator, sets the file size using `ftruncate`, maps the memory, and initializes the semaphores. The secondary process will fail to open the file with `EEXIST`, fallback to opening the existing segment, and wait for an initialization flag (`is_active`) to be raised.

To configure the POSIX semaphores for process-shared synchronization, we must pass `1` as the second argument (`pshared`) to `sem_init`. Passing `0` limits the semaphore's scope to threads within the calling process, which will cause undefined behavior or segmentation faults when accessed across process boundaries.

<script src="https://gist.github.com/mohashari/7d3f27783955aba8c8b43b5cfb2e806a.js?file=snippet-2.txt"></script>

## Memory Ordering & The SPSC Lock-Free Logic

To make our SPSC queue thread-safe without locks, we rely on C11's memory model. Specifically, we use **acquire-release** semantics rather than sequentially consistent ordering (`memory_order_seq_cst`). On standard x86_64 architectures, acquire-release operations are virtually free; they resolve to standard compiler-level barriers and omit expensive bus-locking assembly instructions (like `mfence`).

The mechanics of SPSC enqueue and dequeue require strict sequence guarantees:
1. **Producer Enqueue**: The producer must write the packet data into the target slot *before* updating `write_idx`. We use a `memory_order_release` store to update `write_idx`. This store acts as a memory barrier, ensuring the payload write is visible to any thread that reads the updated index.
2. **Consumer Dequeue**: The consumer must read the updated `write_idx` using a `memory_order_acquire` load. This load guarantees that the reader will observe the memory writes that occurred prior to the index update. After reading the slot payload, the consumer updates `read_idx` using a `memory_order_release` store. This informs the producer that the memory has been consumed and is safe to overwrite.

### The Sleep-Wake Race: Dekker-style Double-Checked Signaling

To prevent the producer and consumer from constantly making expensive semaphore system calls, we employ a hybrid synchronization pattern. The producer only signals `sem_fill` if it detects that the consumer is sleeping. However, this introduces a race condition:
- The consumer checks the queue, finds it empty, and prepares to sleep.
- The producer writes a message, notices the consumer is not sleeping *yet*, and skips signaling `sem_post`.
- The consumer sets its state to sleeping and blocks on `sem_wait`, waiting for a message that has already been sent. This results in a permanent deadlock.

To prevent this lost-wakeup race, we use a Dekker-style double-check algorithm utilizing sequentially consistent (`memory_order_seq_cst`) atomic variables for the sleeping flags:

<script src="https://gist.github.com/mohashari/7d3f27783955aba8c8b43b5cfb2e806a.js?file=snippet-3.txt"></script>

The consumer implementation operates symmetrically:

<script src="https://gist.github.com/mohashari/7d3f27783955aba8c8b43b5cfb2e806a.js?file=snippet-4.txt"></script>

## Handling Production Failure Modes: Crash Recovery

A major challenge with shared-memory-based IPC is robustness. If a process holding a lock or waiting on a semaphore crashes (`SIGKILL`, `SIGSEGV`), the shared memory remains mapped, but the system state is corrupted. Unlike robust mutexes (`pthread_mutexattr_setrobust`), POSIX semaphores do not clean up automatically when a process terminates. 

To make this architecture production-ready, we track the PIDs of both processes directly inside the control metadata block. When a process starts, it writes its PID to the segment. When trying to attach to an existing shared memory segment, we perform a health check on the registered peer using `kill(pid, 0)`. If this call fails with `ESRCH`, the process is dead, indicating the segment is stale and should be re-initialized.

<script src="https://gist.github.com/mohashari/7d3f27783955aba8c8b43b5cfb2e806a.js?file=snippet-5.txt"></script>

## System-Level Optimization and Tuning

To get the absolute lowest latency out of your SPSC ring buffer, compile your C code with optimizations enabled (`-O3`) and inspect the generated assembly code to verify that atomic instructions are translated optimally. 

Using gcc, check the compiler output:
```bash
gcc -O3 -std=c11 -S ipc_ring_buffer.c -o ipc_ring_buffer.s
```

Verify that the acquire loads and release stores compile to standard CPU instruction sets without memory fence calls. On x86_64, a relaxed load, acquire load, and release store all translate to basic `mov` instructions. The sequentially consistent writes to `producer_sleeping` and `consumer_sleeping` will compile to an explicit `xchg` or a `lock` prefixed instruction, which is necessary to preserve store-load ordering across CPU cores.

For production deployments, the following OS-level configurations are highly recommended:

### 1. CPU Core Pinning (Thread Affinity)
By default, the Linux scheduler can migrate your producer and consumer threads across CPU cores, destroying L1/L2 cache locality. You should bind both processes to dedicated cores. For the best performance, keep them on the same physical CPU socket (to leverage a shared L3 cache) but on different physical cores.
<script src="https://gist.github.com/mohashari/7d3f27783955aba8c8b43b5cfb2e806a.js?file=snippet-6.txt"></script>

### 2. Core Isolation
To prevent the Linux kernel from scheduling user processes or handling interrupts on your hot paths, isolate your cores. Add the `isolcpus` option to the kernel command line via GRUB (e.g., `isolcpus=2,3`). You can then run your producer on core 2 and consumer on core 3 without scheduling interference.

### 3. CPU Governor Configuration
Set the scaling governor to performance. This prevents CPU cores from entering low-power states (C-states), which can add up to 20 microseconds of latency when waking up a parked thread:
```bash
sudo cpupower frequency-set -g performance
```

## Conclusion

Lock-free programming in C requires a deep understanding of memory ordering, processor design, and compiler behavior. By designing memory layouts with alignment in mind, utilizing Dekker-style double-checks to manage semaphores, and configuring OS-level affinity, you can build an IPC system capable of supporting millions of operations per second with nanosecond latency. Under low traffic, your threads will park efficiently on POSIX semaphores, protecting your CPU budget. Under high load, the system operates entirely in user space, achieving zero-copy IPC at the hardware limit.