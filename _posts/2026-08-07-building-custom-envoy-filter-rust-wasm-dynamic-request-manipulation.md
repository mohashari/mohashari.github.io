---
layout: post
title: "Building a Custom Envoy Filter in Rust using WebAssembly (Wasm) for Dynamic Request Manipulation"
date: 2026-08-07 08:00:00 +0700
tags: [envoy, rust, webassembly, service-mesh, CNCF]
description: "An in-depth guide to building, optimizing, and deploying an asynchronous custom Envoy HTTP filter in Rust using WebAssembly for production gateways."
image: "https://picsum.photos/seed/2503/1080/720"
thumbnail: "https://picsum.photos/seed/2503/400/300"
---
In high-scale microservice architectures running 100,000+ requests per second (RPS), making dynamic routing or authentication decisions on every incoming request is critical. Doing this in the application layer leads to duplicate code across language stacks, while handling it at the API gateway layer via standard C++ Envoy filters introduces severe operational risks—a single memory corruption bug can crash the proxy process. Using inline Lua scripts is slow, synchronous, hard to test, and can block Envoy’s single-threaded event loops, spiking P99 latency. WebAssembly (Wasm) solves this by enabling near-native, sandboxed filters written in Rust, which can be hot-reloaded without restarting the proxy. In this post, we will walk through building an asynchronous, production-grade Wasm request manipulation filter in Rust that inspects credentials, dispatches out-of-band authorization checks, and dynamically injects headers while maintaining sub-millisecond overhead.

![Building a Custom Envoy Filter in Rust using WebAssembly (Wasm) for Dynamic Request Manipulation Diagram](/images/diagrams/building-custom-envoy-filter-rust-wasm-dynamic-request-manipulation.svg)

## Under the Hood: Envoy Wasm Architecture & Host-Guest Boundary

To write a high-performance Wasm filter, you must understand the Proxy-Wasm Application Binary Interface (ABI). This ABI is a set of low-level C functions exported by the host (Envoy) and the guest (your compiled Wasm binary). Because Wasm executes in a secure sandbox, it cannot directly access memory outside its virtual space. Every interaction with HTTP headers, request bodies, or network calls must traverse the host-guest boundary.

When a downstream client sends a request:
1. Envoy's worker thread handles the connection and invokes the guest's exported functions (such as `proxy_on_request_headers`).
2. The guest filter parses the headers, executes its custom logic (e.g., token parsing), and triggers host calls (such as `proxy_add_header_map_value` or `proxy_dispatch_http_call`).
3. The guest returns a status code (like `Action::Pause` or `Action::Continue`) telling Envoy whether to block the request stream or resume it.

Envoy utilizes a multi-threaded event loop architecture, spawning one event loop per worker thread. To avoid lock contention, Envoy instantiates an isolated Wasm VM instance for each worker thread. This has a major architectural implication: **global static variables in Rust are NOT shared across worker threads**. State is strictly thread-local to the worker thread running the active Envoy event loop.

Furthermore, Wasm VMs run in a linear memory space. By default, runtime engines like V8 impose strict limits on this space. If your Rust filter performs uncontrolled allocations or leaks memory, the VM will panic. When a Wasm VM panics, Envoy intercepts the crash, logs the trace, and responds to the client with a `500 Internal Server Error` (unless configured otherwise). This isolation is a massive safety upgrade over C++ filters, where a null-pointer dereference or segmentation fault would crash the entire Envoy process and bring down all active traffic.

## Setting Up the Rust Toolchain and Cargo Workspace

To build the Wasm filter, we target the WebAssembly System Interface (`wasm32-wasi`). First, we define our `Cargo.toml`. We configure the crate type as `cdylib` to generate a dynamic library that Envoy's Wasm runtime can load, and pin our dependencies to stable versions of the `proxy-wasm` SDK and `serde` for runtime configuration parsing.

<script src="https://gist.github.com/mohashari/231879491ad283ada2426147b20df953.js?file=snippet-1.toml"></script>

## Establishing the Boilerplate Wasm Registry

The `proxy-wasm` SDK relies on implementing two core traits: `RootContext` and `HttpContext`.
* **RootContext**: Represents the lifetime of the plugin. It is created once when Envoy loads the configuration and handles configuration initialization (`on_configure`).
* **HttpContext**: Represents the lifecycle of a single HTTP request-response stream. It is created by the `RootContext` for each incoming connection.

We register these contexts using the `proxy_wasm::main!` macro, which sets up the global entry points required by the Proxy-Wasm ABI.

<script src="https://gist.github.com/mohashari/231879491ad283ada2426147b20df953.js?file=snippet-2.txt"></script>

## Implementing Asynchronous Request Interception & Header Injection

With the boilerplate in place, we implement the dynamic manipulation logic inside the `HttpContext` trait. 

1. **`on_http_request_headers`**: We intercept the request, extract the `Authorization` header, validate its format, and dispatch an out-of-band POST request to our centralized authentication service using `dispatch_http_call`. We then return `Action::Pause` to tell Envoy to suspend the downstream request lifecycle.
2. **`on_http_call_response`**: When the authentication service responds, Envoy invokes this callback. We verify the HTTP status code, parse the returned JSON payload, inject the verified `x-user-id` and `x-user-roles` headers into the original request, and call `resume_http_request()` to forward the request to the upstream target backend.

<script src="https://gist.github.com/mohashari/231879491ad283ada2426147b20df953.js?file=snippet-3.txt"></script>

## Configuring Envoy to Execute the Filter

To load this filter, Envoy must be configured to mount the compiled `.wasm` file using the `envoy.filters.http.wasm` extension. Below is the production-grade listener and cluster definition in `envoy.yaml`. Note the configuration block inside `vm_config` that explicitly passes arguments to our WASM plugin's `on_configure` callback.

<script src="https://gist.github.com/mohashari/231879491ad283ada2426147b20df953.js?file=snippet-4.yaml"></script>

## Compiling, Optimizing, and Shrinking the Wasm Binary

A naive compilation of Rust code targeting WebAssembly results in large files (often exceeding 2MB to 3MB) due to debug symbols, allocator metadata, and Rust's internal panicking logic. In a distributed cloud environment where service sidecars pull filters dynamically, large binaries degrade initialization time and consume critical gateway memory.

To resolve this, we compile with standard release optimization profiles and post-process the binary using `wasm-opt` (from the Binaryen toolchain). This strips dead code paths and optimizes execution blocks, shrinking the output file size to under 300KB.

<script src="https://gist.github.com/mohashari/231879491ad283ada2426147b20df953.js?file=snippet-5.sh"></script>

## Production Hardening & Operational Gotchas

Operating WebAssembly filters inside Envoy at high scale (100k+ RPS) requires addressing concrete operational constraints. Below are key failure modes and configurations you must plan for.

### 1. Memory Management & Heap Exhaustion
Wasm VMs do not run a garbage collector. Instead, they rely on Rust's allocator (`dlmalloc` for `wasm32-wasi`). In a service handling billions of requests, even a small leak will trigger a VM panic when allocations hit the Envoy-configured heap limit (often capped at 16MB or 32MB to prevent node starvation).
* **Mitigation**: Avoid storing request-specific state in global or static maps. Ensure all response structures, custom deserialization buffers, and transient strings are dropped immediately after processing. Avoid the use of `wee_alloc` in production; while it reduces binary size, it is prone to severe memory fragmentation under heavy alloc/dealloc churn.

### 2. Connection Pooling & HTTP Call Concurrency
If your filter dispatches an out-of-band network call (`dispatch_http_call`) for every incoming request, you will establish a high load on your authorization server. Without connection reuse, TCP/TLS handshakes will degrade performance, introducing up to 10-20ms of latency per client request.
* **Mitigation**: Ensure that the target cluster configuration (`auth_identity_service` in our `envoy.yaml`) has explicit connection pooling configured. You must specify a `max_requests_per_connection` and a keepalive policy (`http2_protocol_options: {}` if HTTP/2 is supported) to keep connections warm.

### 3. Fail-Open vs. Fail-Closed Strategy
If the authorization server experiences an outage or a network partition occurs, the call to `dispatch_http_call` may return an error, or the request might exceed the timeout.
* **Mitigation**: In our implementation, we fail-closed (returning a `503 Service Unavailable`). However, for non-critical features or endpoints, a fail-open policy might be preferred. To enable fail-open behavior, parse a `fail_open` parameter from your `FilterConfig`. If the authentication service is unreachable or times out, log a warning and call `self.resume_http_request()` to let the traffic bypass the filter instead of returning a hard 5xx error.

### 4. Distributed Tracing & Request Correlation
When a client request fails at the gateway, diagnosing which side of the host-guest boundary caused the failure is difficult if logs are not correlated.
* **Mitigation**: Extract the Envoy trace header (e.g., `x-request-id`) in `on_http_request_headers` and store it in your `AuthHttpContext` struct. Prepend this ID to all logs generated by the filter. This ensures that every WASM log entry corresponds cleanly to a downstream client request in your tracing system (like Jaeger or OpenTelemetry).