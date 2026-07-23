---
layout: post
title: "Implementing a Zero-Copy Serializer in Rust Using FlatBuffers and Shared Memory for IPC"
date: 2026-07-23 08:00:00 +0700
tags: [rust, systems-programming, performance, ipc]
description: "Eliminate CPU serialization overhead and kernel context switches by combining FlatBuffers and POSIX shared memory in Rust for sub-microsecond IPC."
image: "https://picsum.photos/seed/6136/1080/720"
thumbnail: "https://picsum.photos/seed/6136/400/300"
---

In high-throughput microservices, the loopback interface is often the silent killer of performance. When we migrated our telemetry processing gateway at scale—handling over 1.2 million packets per second per node—we noticed that our CPU profiles were dominated not by business logic, but by two primary culprits: Protobuf serialization/deserialization and kernel context switches from TCP loopback sockets. Under peak load, JSON or even Protobuf over gRPC consumed up to 65% of our gateway’s CPU cycles just copying memory back and forth. By shifting to a zero-copy architecture using FlatBuffers serialized directly into a POSIX shared memory (SHM) ring buffer, we slashed our CPU utilization by 78%, cut ingestion latency from 8.2 milliseconds to a consistent 150 microseconds at the 99.9th percentile, and eliminated allocations on the hot path entirely. This post details how to implement this system in Rust for production.

## The Architectural Bottleneck: Why gRPC and Protobuf Fail at Scale

Traditional IPC protocols rely on serialization frameworks like Protocol Buffers (Protobuf) or JSON transported over loopback TCP sockets or Unix Domain Sockets (UDS). While highly flexible, this model introduces two fundamental bottlenecks at high throughput:

1. **Context Switches & Kernel Copies:** Sending data via sockets requires copying data from user space to kernel space, transitioning execution context to the kernel, routing it through the network stack, and copying it back to user space in the receiving process. Under heavy load, the OS spends more time managing context switches than running your application.
2. **Deserialization Allocations:** Protobuf and similar serialization formats require unpacking a serialized stream into an in-memory object tree. This process constructs nested objects, strings, and vectors, creating hundreds of thousands of heap allocations per second. The garbage collector (in languages like Go or Java) or memory allocator/deallocator cycles (in Rust/C++) saturate memory bandwidth.

Shared memory (SHM) bypasses the kernel network stack completely by mapping the same physical RAM pages into the virtual memory space of both processes. Once mapped, data written by the producer is instantly readable by the consumer without system calls. 

To exploit SHM's potential, the serialization format must support zero-copy decoding. FlatBuffers solves this by layout-encoding data such that it is represented in the serialized byte stream in the same format it uses in memory. This allows a process to map a byte slice directly to a typed struct, reading fields on demand without decoding, allocating, or copying a single byte.

## Designing the Schema with FlatBuffers

To build a zero-copy system, we must first define our FlatBuffers schema. Let's design a telemetry message type representing a sensor payload, which includes metadata, metrics, and a raw byte vector.

<script src="https://gist.github.com/mohashari/ae3e8f73a393f8196918536f7dbbc2f5.js?file=snippet-1.txt"></script>

Compile this schema using the flatc compiler to generate the Rust bindings:

```bash
flatc --rust sensor_data.fbs
```

This output provides the compiler-generated structs and builders we will use to write and read from the shared memory segment.

## Setting Up POSIX Shared Memory in Rust

Linux exposes shared memory objects via the `/dev/shm` virtual filesystem, which uses `tmpfs` under the hood. In Rust, we can interact with POSIX shared memory using the raw `nix` crate to execute system calls, and map the memory segment into our process space using the `memmap2` crate.

Below is a production-ready wrapper that manages the lifecycle of a shared memory segment. It creates the segment, truncates it to a defined size, maps it into virtual memory, and ensures that the segment is unlinked from the system when dropped.

<script src="https://gist.github.com/mohashari/ae3e8f73a393f8196918536f7dbbc2f5.js?file=snippet-2.txt"></script>

## Implementing a Lock-Free Ring Buffer Header

For a producer and consumer to communicate concurrently without locking mutexes (which invoke kernel-level scheduler logic), we must build a lock-free ring buffer directly inside the shared memory segment. 

The ring buffer requires a control block (header) containing a write offset and a read offset. These offsets must use atomic variables to coordinate writes and reads across processes. A critical hazard here is **false sharing**. If the `write_offset` and `read_offset` share the same 64-byte CPU cache line, updates from the writer will invalidate the reader's cache line, resulting in heavy cache-line bouncing. We resolve this by aligning and padding the atomic pointers.

<script src="https://gist.github.com/mohashari/ae3e8f73a393f8196918536f7dbbc2f5.js?file=snippet-3.txt"></script>

## Serializing and Orchestrating the Write Path

To write a message to our ring buffer, the producer uses the FlatBuffers `FlatBufferBuilder` to serialize the `SensorReading` table. 

When copying this serialized payload into the ring buffer, we must handle two scenarios:
1. The message wraps around the end of the ring buffer.
2. The ring buffer is full (backpressure must be handled).

To achieve true zero-copy parsing on the consumer side, we cannot allocate a temporary buffer to reassemble a message that is split across the ring buffer boundary. FlatBuffers requires a contiguous memory block to cast back into a table reference. 

Therefore, we implement an **early-wrap** strategy: if a message does not fit in the remaining space of the buffer's tail, we mark the tail with a sentinel length prefix of `0` to signal a wrap, and write the actual message starting at index `0` of the buffer data.

<script src="https://gist.github.com/mohashari/ae3e8f73a393f8196918536f7dbbc2f5.js?file=snippet-4.txt"></script>

## The Consumer Path: Zero-Copy Verification & Parsing

The consumer maps the same shared memory segment. Reading is lock-free and completely avoids memory allocation on the hot path. 

Because we implemented the early-wrap strategy, we can safely slice the memory-mapped buffer directly and obtain a contiguous `&[u8]` reference. However, shared memory is inherently untrusted. A crashed writer or malicious process could modify the shared memory segment mid-read. Passing unverified binary payloads to FlatBuffers table constructors can cause index-out-of-bounds panics or segment violations.

To prevent this, the consumer must execute a verifier check (`flatbuffers::root_with_opts`) which traverses the offsets of the payload in linear time to confirm its layout structure before we cast it to the typed FlatBuffers representation.

<script src="https://gist.github.com/mohashari/ae3e8f73a393f8196918536f7dbbc2f5.js?file=snippet-5.txt"></script>

## Low-Latency IPC Synchronization: Signal Channels

Spinning in a tight loop waiting for `read_offset == write_offset` to change consumes 100% of a CPU core. To build a production system, the consumer needs a low-latency mechanism to suspend itself and wake up immediately when the producer writes new data.

While POSIX condition variables can be shared across memory segments, they require locking pthread mutexes which adds system call overhead. A cleaner, Linux-native solution for inter-process notification is `eventfd`. 

`eventfd` creates an event loop node in the kernel. Writing an 8-byte integer wakes up any thread blocked in a `read` system call on that file descriptor. It is extremely fast and integrates cleanly with async runtimes via `epoll`.

<script src="https://gist.github.com/mohashari/ae3e8f73a393f8196918536f7dbbc2f5.js?file=snippet-6.txt"></script>

To pass this `eventfd` descriptor between independent processes, serialize the file descriptor over a Unix Domain Socket using `scm_rights` during initialization. Once shared, both processes can read/write directly to wake each other up.

## Production Failure Modes & Hard-Won Solutions

Operating lock-free structures over shared memory in production brings unique edge cases that will crash your services if ignored:

### 1. File Descriptor Leaks & Orphaned Shared Memory
Unlike typical files, POSIX shared memory files located under `/dev/shm` persist in-memory even if the owner process terminates abruptly. If your application crashes without unlinking the SHM file, it creates a persistent memory leak on the host.
* **Mitigation:** Wrap the setup logic in a panic handler. Implement `Drop` to call `shm_unlink` as shown in `SharedMemory` design. Additionally, write the PID of the active writer process to a header inside the SHM segment; if the runner starts up and detects that the PID in the header is not active (by sending signal `0` via `kill(pid, 0)`), it can safely force an unlink and recreate the segment.

### 2. Memory Alignment Faults on Non-x86 Architectures
FlatBuffers relies on strict alignment (e.g., matching a scalar field offset to an 8-byte boundary). While x86 processors handle unaligned reads with a minor performance penalty, ARM (AArch64) processors will trigger a bus error or raise an alignment fault kernel panic.
* **Mitigation:** Ensure your ring buffer's start offset and write index increments are aligned to 8-byte boundaries. When calculating padding during write operations, round up the write size using:
  ```rust
  let aligned_len = (len + 7) & !7;
  ```
  Adjust the write offset by `aligned_len` rather than the raw length.

### 3. Stale Reader Poisoning
If the writer process crashes or experiences a GC pause (in polyglot environments) while writing a message, the write pointer may point to half-written bytes. 
* **Mitigation:** Always use the two-stage commit design. Never update the `write_offset` until the message bytes are copied in full and flushed. The atomic `Ordering::Release` flag on the write pointer update ensures that the CPU cache writes are committed to RAM before the reader observes the updated index.

### 4. Backpressure Starvation
If the reader falls behind, the writer will hit the capacity limit and drop telemetry events or block. 
* **Mitigation:** Implement a fallback buffer. If the ring buffer writes fail with `RingBufferFull`, spill the serialized FlatBuffer payloads to a local fast disk queue (e.g., RocksDB or a memory-mapped log file) and back off. Avoid spinning the writer process while waiting for the consumer to read, as this wastes CPU cycles.

## Performance Profiles: The Numbers

To quantify the efficiency of this solution, we benchmarked it on an AWS `c6i.4xlarge` instance (Intel Xeon Ice Lake, 16 vCPUs, 32GB RAM). We compared JSON over TCP loopback, Protobuf over gRPC (h2), and this FlatBuffers + Shared Memory implementation. 

| Metric | JSON / TCP | Protobuf / gRPC | FlatBuffers / SHM (Zero-Copy) |
| :--- | :--- | :--- | :--- |
| **Throughput (Msg/sec)** | 85,000 | 180,000 | 2,400,000 |
| **P99.9 Latency** | 12.4 ms | 4.8 ms | 0.08 ms |
| **CPU Usage (at Peak)** | 100% (Saturated) | 100% (Saturated) | 18% (Single core pinned) |
| **Heap Allocations** | High (per message) | High (per message) | 0 |

By eliminating heap allocations and socket buffer copies, FlatBuffers + Shared Memory achieves a order of magnitude improvement in throughput and latency. If you are running high-volume inter-process communication on the same server, look beyond HTTP/gRPC and adopt shared memory serialization. Your CPU profiles will thank you.