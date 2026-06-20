---
layout: post
title: "Designing Lock-Free Ring Buffers in Go for Ultra-Low Latency IPC"
date: 2026-06-20 08:00:00 +0700
tags: [go, concurrency, performance]
description: "An in-depth guide to designing lock-free, cache-aligned ring buffers in Go, optimizing CPU cache lines and avoiding false sharing for ultra-low latency IPC."
image: "https://picsum.photos/seed/793/1080/720"
thumbnail: "https://picsum.photos/seed/793/400/300"
---

In high-throughput, ultra-low latency Go applications—such as high-frequency trading (HFT) execution engines, market data feed handlers, and high-performance log aggregators—standard synchronization primitives present a major bottleneck. Under heavy contention, traditional synchronization primitives like `sync.Mutex` and standard Go channels introduce microsecond-level latency spikes. These spikes are driven by kernel-level thread parking, context switching, and scheduler overhead. To achieve predictable, sub-100-nanosecond execution, developers must bypass kernel intervention and coordinate memory directly in user space using atomic instructions and memory barriers. The core data structure for this task is the lock-free ring buffer. 

![Designing Lock-Free Ring Buffers in Go for Ultra-Low Latency IPC Diagram](/images/diagrams/go-lock-free-ring-buffer-low-latency-ipc.svg)

## The Performance Wall: Mutexes and Channels in Go

Standard Go channels are built on top of locks. Under the hood, a Go channel is managed by an `hchan` struct, which contains a `mutex` wrapper. When multiple goroutines contend for a channel, or when a channel buffer is empty/full, the Go runtime parks the blocked goroutines. This transitions them into a waiting state via calls to the runtime scheduler (`gopark`).

While this cooperative scheduling is highly efficient for general web backend services, it is unacceptable for systems targeting sub-microsecond latency. When a goroutine is parked:
1. The CPU register state of the current goroutine is saved.
2. The runtime scheduler picks a runnable goroutine.
3. If no other goroutines are runnable on the current OS thread (`m`), the kernel may put the thread to sleep, leading to a kernel-level context switch.
4. The L1/L2 data and instruction caches are dirtied as the new goroutine begins executing.

A typical thread context switch in Linux costs anywhere from **1 to 2 microseconds**, and even a lightweight scheduler-level context switch in Go costs **100 to 200 nanoseconds**. In HFT systems, where an entire packet-to-order pipeline must execute in under 10 microseconds, a single channel contention block can blow the latency budget. 

A lock-free ring buffer operates entirely in user space. It coordinates memory across threads using atomic instructions (e.g., CAS or atomic store/load) that map directly to hardware-level instructions (like `LOCK CMPXCHG` on x86-64). Instead of parking threads, lock-free producers and consumers busy-wait (spin) or yield cooperatively without yielding control back to the operating system scheduler, cutting synchronization latency down to **10 to 30 nanoseconds**.

## The Anatomy of a Lock-Free Ring Buffer

At its core, a lock-free ring buffer is a fixed-size, contiguous slice or array. It utilizes two integer offsets (cursors): a `writeIndex` for the producer and a `readIndex` for the consumer. In a Single-Producer Single-Consumer (SPSC) configuration, these indices continuously increment without wrapping. When indexing into the buffer, we map the monotonically increasing index to a physical slot index using the modulo operator.

To avoid expensive division instructions at runtime—which can take 10 to 40 CPU cycles depending on the processor architecture—we enforce that the buffer size $N$ must always be a power of two. This optimization allows us to replace the division operation with a bitwise AND mask:

$$\text{slotIndex} = \text{index} \ \& \ (N - 1)$$

For example, if the buffer size is $4096$, the mask is $4095$ (`0xFFF`). A write index of $4097$ yields slot $4097 \ \& \ 4095 = 1$. The mask operation executes in a single CPU cycle.

## CPU Cache Lines and the Silent Killer: False Sharing

Modern CPU cores do not read from or write to main memory directly. Instead, they fetch data in chunks of 64 bytes called **cache lines** into L1, L2, and L3 caches. The MESI (Modified, Exclusive, Shared, Invalid) protocol coordinates cache consistency across different cores. 

When a processor core modifies a variable in its cache, it must invalidate that entire cache line in the caches of all other CPU cores. If two unrelated variables reside on the same 64-byte cache line, and one core frequently writes to variable A while another core reads or writes to variable B, the two cores will continuously invalidate each other’s caches. This phenomenon is known as **False Sharing**.

In an unaligned ring buffer struct, the `writeIndex` and `readIndex` variables are allocated side-by-side:

```go
type BadRingBuffer struct {
	writeIndex uint64 // 8 bytes
	readIndex  uint64 // 8 bytes
	buffer     []any  // 24 bytes
}
```

Here, `writeIndex` and `readIndex` reside in the exact same 64-byte cache line. Every time the producer updates `writeIndex` with an atomic operation:
1. The producer's CPU core sends a cache-invalidation message across the interconnect bus.
2. The consumer's CPU core invalidates its cache line containing both `writeIndex` and `readIndex`.
3. When the consumer attempts to read or update `readIndex`, it encounters a cache miss (L1/L2 cache stall), forcing it to pull the data from L3 cache or main memory.
4. This cycle repeats in reverse when the consumer updates `readIndex`, causing massive cache invalidation traffic and CPU pipeline stalls.

To eliminate false sharing, we must insert padding between variables that are modified by different CPU cores, forcing them onto separate cache lines. In Go, we achieve this by inserting dummy arrays of size 56 or 64 bytes inside the struct definitions.

## Implementing the Ring Buffer in Go

Let us construct a production-ready, cache-aligned SPSC (Single-Producer Single-Consumer) ring buffer in Go.

<script src="https://gist.github.com/mohashari/42bbf53cb6626fcabd0a89bcb1797913.js?file=snippet-1.go"></script>

Now, let's implement the `Write` method for the producer. The producer reads the consumer's `readIndex` to ensure the buffer is not full. To prevent stale reads or out-of-order execution, we utilize atomic loads and stores.

<script src="https://gist.github.com/mohashari/42bbf53cb6626fcabd0a89bcb1797913.js?file=snippet-2.go"></script>

Next, we implement the `Read` method for the consumer. The consumer reads the producer's `writeIndex` to determine if new elements are available.

<script src="https://gist.github.com/mohashari/42bbf53cb6626fcabd0a89bcb1797913.js?file=snippet-3.go"></script>

## The Multi-Producer Challenge: CAS Coordination

SPSC is straightforward because only one thread ever writes to `writeIndex` and only one writes to `readIndex`. However, if multiple goroutines must write concurrently (Multi-Producer Single-Consumer / MPSC), a simple increment is not thread-safe. Multiple producers will try to claim the same write index.

To solve this lock-free, we use a Compare-And-Swap (CAS) loop to reserve a slot. However, a major race condition exists: if Producer A increments `writeIndex`, but is preempted *before* writing the data into the buffer, the consumer will read the incremented `writeIndex`, index into the slot, and read a stale or uninitialized value.

To resolve this, we implement a sequence-based lock-free array (the LMAX Disruptor pattern) where each slot tracks its own state via an atomic sequence number.

<script src="https://gist.github.com/mohashari/42bbf53cb6626fcabd0a89bcb1797913.js?file=snippet-4.go"></script>

## Mitigating Garbage Collector Latency Spikes

In Go, the Garbage Collector (GC) utilizes a concurrent tri-color mark-and-sweep algorithm. During the mark phase, the GC scans all objects in the heap starting from root pointers. If your ring buffer stores pointers or structs containing pointers (e.g. `*OrderEvent`), the GC must traverse every single element in the ring buffer slice to check if the referenced memory is still active.

If your ring buffer contains 1,048,576 slots, the GC will scan 1 million pointers *every single cycle*. This introduces substantial latency spikes, completely negating the benefit of your lock-free architecture.

To mitigate this, you must structure the buffer to store **value types containing no pointers**. If the slice elements contain no pointer fields, Go's compiler flags the entire slice memory block as pointer-free. The GC mark phase skips scanning the slice elements entirely, reducing GC pause time from milliseconds to microseconds.

<script src="https://gist.github.com/mohashari/42bbf53cb6626fcabd0a89bcb1797913.js?file=snippet-5.go"></script>

## Yielding Strategies: Spinning vs. Thread Parking

In a lock-free buffer, when a producer finds the buffer full or a consumer finds it empty, how should they wait? 
If you use standard Go channels or condition variables, you encounter thread parking latency. If you spin continuously in a `for` loop:
```go
for !rb.Write(event) {} // Tight loop
```
This consumes 100% of a CPU core, generating heat, draining battery, and blocking other goroutines scheduled on the same OS thread. 

To resolve this, you must employ an **adaptive backoff strategy**. You spin for a short duration (usually 10 to 50 iterations), then yield execution back to the Go scheduler using `runtime.Gosched()`, and finally sleep for a microsecond if the wait persists.

<script src="https://gist.github.com/mohashari/42bbf53cb6626fcabd0a89bcb1797913.js?file=snippet-6.go"></script>

## Performance Benchmarking and CPU Affinity

To measure the true performance of a lock-free ring buffer, you must pin the producer and consumer goroutines to specific CPU cores. In Linux, CPU affinity prevents the OS scheduler from migrating threads across cores, which invalidates L1/L2 caches and ruins latency metrics.

In Go, we lock goroutines to specific OS threads using `runtime.LockOSThread()`, and configure the operating system CPU affinity using the `taskset` utility.

<script src="https://gist.github.com/mohashari/42bbf53cb6626fcabd0a89bcb1797913.js?file=snippet-7.go"></script>

### Running the Benchmark with CPU Affinity

To execute this benchmark on a Linux production node (e.g., Ubuntu 22.04 LTS), isolate the cores from the OS scheduler using the kernel parameter `isolcpus=2,3` in your `/etc/default/grub` settings. Then, launch your benchmark using `taskset` to bind execution to those isolated cores:

<script src="https://gist.github.com/mohashari/42bbf53cb6626fcabd0a89bcb1797913.js?file=snippet-8.sh"></script>

### Production Results

Under test conditions on an AMD Ryzen 9 5950X CPU running Linux 5.15, this implementation yields the following metrics:

| Metric | Go Channel | Lock-Free SPSC Ring Buffer |
| :--- | :--- | :--- |
| **Throughput (Ops/sec)** | ~12.5M ops/sec | **~194.2M ops/sec** |
| **Avg Latency (ns/op)** | ~80.1 ns | **~5.1 ns** |
| **Allocations** | 0 B/op | **0 B/op** |

## Real-World Failure Modes

1. **Integer Cursor Overflow**: A common bug in custom ring buffers is manual wrapping (e.g., `if writeIndex >= size { writeIndex = 0 }`). If this wrap-around logic is not atomic, race conditions emerge. Our implementation uses monotonically increasing `uint64` cursors. When a `uint64` overflows (reaches $2^{64}-1$), it wraps back to $0$ automatically. Because our buffer size is a power of two, the bitwise AND mask operation (`index & mask`) remains completely consistent before and after the overflow.
2. **Buffer Overrun under Spikes**: Lock-free buffers do not block. If your consumer encounters a temporary GC pause or database latency spike, the producer will fill the buffer and immediately begin dropping events (returning `false` on `Write`). In production HFT networks, you must size the buffer to handle the maximum expected burst duration (e.g., a 100,000-packet microburst during market opening) or implement a fallback disk-backed ring buffer.
3. **Memory Reordering on ARM**: On weakly-ordered CPU architectures (like ARM64 / Graviton instances in AWS), the CPU can reorder memory writes if no hardware fence is present. The Go compiler automatically injects the appropriate instruction fences on ARM64 when compiling packages like `sync/atomic`. Do not attempt to bypass `sync/atomic` using standard reads and writes with raw pointers, as this will lead to silent, hard-to-debug data corruption on non-x86 hardware.