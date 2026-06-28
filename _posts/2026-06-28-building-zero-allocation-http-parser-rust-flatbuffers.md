---
layout: post
title: "Building a Zero-Allocation HTTP Parser in Rust Using FlatBuffers"
date: 2026-06-28 08:00:00 +0700
tags: [rust, performance, serialization]
description: "Eliminate heap allocator lock contention and tail latency spikes at 200k+ RPS by combining httparse and FlatBuffers into a zero-allocation parsing pipeline."
image: "https://picsum.photos/seed/7637/1080/720"
thumbnail: "https://picsum.photos/seed/7637/400/300"
---

At 200,000 requests per second (RPS) per node, the primary bottleneck in your edge proxy or API gateway is rarely the CPU's compute limits or raw network bandwidth. Instead, it is the memory allocator. Standard HTTP parsers and serialization libraries (like `serde_json` or Protobuf) allocate hundreds of tiny heap buffers per request for paths, headers, and intermediate string representation. This behavior results in severe allocator lock contention across threads, cache line bouncing, and frequent heap fragmentation. Even under highly optimized allocators like `jemalloc` or `mimalloc`, these allocations trigger unpredictable tail-latency spikes (p99 and p99.9) that ripple downstream. To achieve flat, microsecond-level latency profiles at scale, you must eliminate heap allocations from the request-processing hot path. 

This post details how to design and implement a strictly zero-allocation HTTP/1.1 parsing and serialization pipeline in Rust. By combining the zero-copy parser `httparse` with a thread-local pool of FlatBuffers, we can parse raw incoming socket bytes, validate the protocol, and serialize the structured payload for downstream gRPC or RPC microservices without allocating a single byte of memory on the heap.

## The Memory Bottleneck of Traditional Deserialization

To understand why a new pattern is required, analyze the execution trace of a conventional Rust service handling HTTP/1.1 requests. Typically, the pipeline consists of:
1. Reading bytes from a socket into a dynamic buffer.
2. Parsing the request line and headers using a library that instantiates owned `String` objects for header names and values.
3. Mapping the parsed request onto an internal structure.
4. Serializing the structured type into a binary format (like Protobuf) to push down the wire.

Even if step 1 uses a reusable buffer, steps 2 and 4 create garbage. Every header parsed creates a new `Vec` or `String` allocation. A request containing 15 headers triggers at least 30 allocation calls. Multiplied by 200,000 connections, the system executes 6 million allocations per second. At this volume, the kernel's memory management subsystems and the thread-local cache of your allocator are saturated. 

FlatBuffers solves the serialization side of this bottleneck. Unlike Protobuf or JSON, FlatBuffers encodes data in a binary format that represents the internal memory layout of the final data structure. Accessing fields inside a serialized FlatBuffer is a zero-copy operation; you simply cast the byte slice to the schema's root type and read fields via precomputed offsets. No parsing or unpacking occurs on the reader side.

On the writer side, however, we must construct the FlatBuffer binary layout. The standard `FlatBufferBuilder` internally manages a growable byte array (`Vec<u8>`). While it performs some heap allocations as it resizes, we can eliminate these allocations entirely by reusing the same builder instance across requests using thread-local pooling. The builder's internal vector grows to the peak request size (typically 4KB to 8KB) within the first few requests and stabilizes, executing zero allocations for all subsequent requests.

## The FlatBuffers Schema Layout

To build our pipeline, we first define the schema that maps the HTTP request. We must design it to support zero-copy string and byte representation.

<script src="https://gist.github.com/mohashari/df6dc435bdba0884c514eef648d967c9.js?file=snippet-1.txt"></script>

We configure `value` and `body` as raw byte arrays (`[ubyte]`) rather than strings. HTTP header values and request bodies are not guaranteed to be valid UTF-8, and validating them as strings at the parser level incurs unnecessary CPU overhead. Conversely, `method` and `path` are strictly validated as strings since they dictate routing logic.

## Reusable Parser and Builder Architecture

To implement the zero-allocation pipeline, we must construct a wrapper around the generated FlatBuffers code and the `httparse` library. This wrapper manages the lifetime constraints of the raw request byte slice and keeps the `FlatBufferBuilder` around for reuse.

First, let's look at the parser's structural definition and its initialization code.

<script src="https://gist.github.com/mohashari/df6dc435bdba0884c514eef648d967c9.js?file=snippet-2.txt"></script>

By specifying the `'static` lifetime parameter on `FlatBufferBuilder`, we ensure that the builder has no reference dependencies to transient network buffers. This allows the parser to be held in long-lived state managers, thread pools, or thread-local storage.

## Writing the Zero-Allocation Parsing Loop

The core parsing and serialization logic must execute without invoking the allocator. We use a stack-allocated header array to store the parsed slices from `httparse`. 

A critical detail when working with FlatBuffers is that the builder constructs tables from back to front. Consequently, when serializing vectors, elements must be pushed in reverse order if we are to write them directly to the builder without allocating a temporary intermediate heap vector.

<script src="https://gist.github.com/mohashari/df6dc435bdba0884c514eef648d967c9.js?file=snippet-3.txt"></script>

### Deconstructing the Memory Overhead
In the implementation above:
- `headers` is an array of slices stored on the stack (`[EMPTY_HEADER; 64]`).
- `header_offsets` is an array of `u32` wrappers (`WIPOffset`) stored on the stack.
- The `builder.create_string` and `builder.create_vector` operations write directly into the existing capacity of the builder's internal `Vec<u8>`. No new buffers are allocated unless the request exceeds the builder's current capacity, which only happens on the first few large requests during the warm-up phase.

## Concurrency: The Thread-Local Storage Pattern

If the parser is shared across thread pools using dynamic synchronization primitives like `Mutex` or `RwLock`, you replace the memory allocation bottleneck with lock contention. At high concurrency levels, threads block waiting for access to the shared builder.

To resolve this, we pin the `ZeroAllocHttpParser` to threads using Thread-Local Storage (TLS). This ensures that each CPU core has its own dedicated parser and buffer, eliminating synchronization overhead entirely.

<script src="https://gist.github.com/mohashari/df6dc435bdba0884c514eef648d967c9.js?file=snippet-4.txt"></script>

## Security and Robustness in Production

Implementing custom zero-allocation engines exposes you to critical security vulnerabilities if input data is not validated. In production, you must protect your services against:
1. **Slowloris Attacks**: Attackers sending HTTP requests incrementally to keep connection handles open.
2. **Buffer Overruns / DoS**: Attackers sending infinitely large HTTP headers or request lines to exhaust the pre-allocated buffers.
3. **HTTP Request Smuggling**: Differences in parser behaviour between front-end proxies and back-end services.

The following code demonstrates a robust async network reader using `tokio` that safely wraps our parser with strict read limits and timeout policies.

<script src="https://gist.github.com/mohashari/df6dc435bdba0884c514eef648d967c9.js?file=snippet-5.txt"></script>

This async wrapper prevents partial-write attacks by timing out idle clients and guarantees that malformed requests are dropped before they can consume CPU resources in the FlatBuffer serialization loop.

## Unit Testing and Verification

To ensure that the parser correctly translates incoming raw bytes into valid FlatBuffer binaries, we write a unit test suite verifying the structured offsets and data integrity.

<script src="https://gist.github.com/mohashari/df6dc435bdba0884c514eef648d967c9.js?file=snippet-6.txt"></script>

## Mock Implementations for Self-Contained Compilation

For completeness and to ensure immediate usefulness, the code below provides the minimal mock interfaces representing the structure generated by the FlatBuffers schema compiler (`flatc`).

<script src="https://gist.github.com/mohashari/df6dc435bdba0884c514eef648d967c9.js?file=snippet-7.txt"></script>

## Production Performance Trade-Offs

Eliminating allocations on the hot path yields significant benefits but introduces key architectural trade-offs:

### 1. Memory Footprint
By caching a `FlatBufferBuilder` in thread-local storage, you trade static memory footprint for lower latency. If a thread processes a request with an abnormally large body (e.g., a 50MB file upload), that thread's builder memory will grow to 50MB and remain allocated. 

*Mitigation*: Implement a capacity check in the cleanup phase. If `builder.capacity()` exceeds a specific safety limit (e.g., 64KB), reset the builder and force release its backing memory back to the system.

### 2. Thread-Pool Contention
If you run thousands of dynamic green threads (like Tokio tasks) across a small pool of OS threads, thread-local storage is highly efficient because OS thread counts are typically bound to the CPU core count (e.g., 16 or 32 threads). The total static memory overhead remains minimal. 

However, if you spawn thousands of raw OS threads, the TLS memory overhead will balloon. Maintain a strict thread-to-core mapping (e.g., setting `worker_threads` explicitly on the Tokio runtime configuration) when employing TLS pools.

### 3. Complexity of Downstream Decoding
Because FlatBuffers does not fully deserialize data, downstream services must read the buffers in their raw format. This requires copying the FlatBuffer schemas (`.fbs` files) to all consuming microservices and compiling them into their respective languages (Go, Java, Python). While this introduces dependency coordination overhead, it ensures that the zero-copy pipeline spans from edge proxy parsing all the way down to internal RPC processing.