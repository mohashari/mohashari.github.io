---
layout: post
title: "Writing a Custom Envoy Filter in Rust using WebAssembly for Dynamic HTTP Request Transformation"
date: 2026-08-21 08:00:00 +0700
tags: [envoy, rust, webassembly, proxy-wasm, service-mesh]
description: "A deep-dive engineering guide to building, optimizing, and deploying high-performance in-proxy WASM filters in Envoy using Rust."
image: "/images/diagrams/writing-custom-envoy-filter-rust-webassembly-dynamic-http-request-transformation.svg"
thumbnail: "/images/diagrams/writing-custom-envoy-filter-rust-webassembly-dynamic-http-request-transformation.svg"
---

In high-traffic microservices architectures, processing cross-cutting concerns like identity propagation, custom header manipulation, or legacy authentication token translation at the edge can quickly become an operational nightmare. Traditional approaches rely on centralized gateways or external authorization sidecars (e.g., via Envoy’s `ext_authz` filter) which introduce network hops, inflate p99 tail latencies by 5–15ms, and increase egress costs under heavy load. Compiling custom C++ Envoy filters eliminates this latency overhead but introduces the risk of proxy-wide segmentation faults and complicates deployment cycles. Implementing a custom Envoy HTTP filter in Rust using WebAssembly (WASM) solves this tension: it allows you to run safe, sandboxed, near-native request mutations directly inside the Envoy event loop, decoupling filter logic from the Envoy core binary lifecycle.

![Writing a Custom Envoy Filter in Rust using WebAssembly for Dynamic HTTP Request Transformation Diagram](/images/diagrams/writing-custom-envoy-filter-rust-webassembly-dynamic-http-request-transformation.svg)

## The Cost of Middleware: Why In-Proxy Wasm Matters

At scale, every millisecond added to a request path compounds downstream. If your gateway processes 50,000 requests per second, introducing an external authorization gRPC callout (via `ext_authz`) introduces network serialization, deserialization, context switching, and network transmission delays. Even a local sidecar call over localhost adds 1 to 2 milliseconds of overhead. 

WebAssembly in Envoy executes code inside an embedded virtual machine (typically V8 or Wasmtime) directly within the Envoy host process. The overhead of entering the WASM VM is in the range of 10 to 50 microseconds.

By utilizing the `proxy-wasm` application binary interface (ABI), we can write filters that intercept request headers, payload bodies, and response sequences. Rust is the ideal choice for this task:
1. **Zero Garbage Collection**: Unlike Go or AssemblyScript, Rust does not require a garbage collector. This eliminates unpredictable GC pauses, guaranteeing stable p99 and p99.9 latency profiles.
2. **Memory Safety**: The Rust compiler guarantees memory safety at compile time. Because WASM runs inside a sandboxed linear memory space, a panic in the filter will crash the WASM VM instance (returning a 500 status to that specific request) but will *never* crash the Envoy process itself.
3. **Rich Ecosystem**: Rust provides high-quality crates for JWT validation, Base64 encoding, and JSON parsing that compile seamlessly to the `wasm32-wasi` target.

## The Wasm-Envoy ABI and Memory Boundaries

To write efficient WASM filters, you must understand the guest-host memory boundary. The WASM VM runs in a sandboxed, 32-bit linear memory space. The guest (your Rust code) cannot directly access Envoy's C++ heap memory.

When Envoy receives HTTP request headers, it does not pass pointers to the WASM VM. Instead, it serializes and copies the headers across the ABI boundary into the VM’s linear memory. When your filter mutates a header, the WASM VM executes a host call to tell Envoy's worker thread to update its internal C++ request structs.

Because of this data copy overhead, request body manipulation must be handled with extreme care. Inspecting or modifying a 10MB JSON payload requires copying the entire 10MB chunk into the VM's memory. If your requirements only involve routing or authentication transformations, operate strictly on the headers using `Action::Continue` or `Action::Pause` and avoid buffered body callbacks entirely.

## Setting Up a Production Rust proxy-wasm Project

We begin by initializing a Rust library crate. We must configure Cargo to output a dynamic library (`cdylib`) and tune the compilation profile to generate the smallest and most optimized WASM binary possible.

<script src="https://gist.github.com/mohashari/6913a33b918d308921c6b69ea2e21d5a.js?file=snippet-1.toml"></script>

The release profile parameters are critical for production:
* `opt-level = "z"`: Instructs the compiler to optimize specifically for binary size. Smaller WASM binaries reduce startup latency and fit more easily into CPU instruction caches.
* `lto = true`: Enables link-time optimization across all crate dependencies, pruning dead code aggressively.
* `panic = "abort"`: Disables stack unwinding on panic, stripping debug metadata and reducing the binary size by 150KB to 200KB.
* `strip = true`: Automatically strips symbols from the output binary.

## Implementing the Boilerplate: VM and Context Lifecycles

The `proxy-wasm` SDK structures filter execution around three hierarchical contexts:
1. **VM Context**: Created once when the WASM virtual machine is initialized. Perfect for global configuration parsing.
2. **Root Context**: Associated with a specific filter configuration. Lives across request streams and can be used to handle global metrics or share caching structures.
3. **HTTP Context**: Spawned for every single HTTP request/response transaction. This is where your transformation logic resides.

Envoy spawns one WASM VM per worker thread. Because Envoy is thread-per-core, WASM instances do not share state natively. Thread safety is guaranteed because each VM is single-threaded and isolated, preventing data races without requiring locks.

<script src="https://gist.github.com/mohashari/6913a33b918d308921c6b69ea2e21d5a.js?file=snippet-2.txt"></script>

## Modifying Headers in the HTTP Request Pipeline

Our primary goal is to inspect incoming headers. If a valid JWT is present, we extract the metadata synchronously, inject the validated payload metadata into a new internal header, and forward the request to the upstream backend. 

If the token is invalid, missing, or requires dynamic backend validation, we pause request execution to initiate an asynchronous out-of-band call to an authorization lookup service.

<script src="https://gist.github.com/mohashari/6913a33b918d308921c6b69ea2e21d5a.js?file=snippet-3.txt"></script>

By returning `Action::Pause`, we notify Envoy that this specific HTTP request state machine must halt. Envoy will suspend the downstream request connection but will continue serving other traffic on the worker thread's event loop. 

## Asynchronous External Lookups via proxy-wasm Callouts

You cannot use standard asynchronous Rust runtimes (like Tokio or async-std) within a proxy-wasm filter. The WASM environment lacks access to low-level operating system threads or epoll syscalls. Instead, network I/O is delegated entirely to the Envoy host process via host calls.

We use `self.dispatch_http_call` to initiate an out-of-band HTTP request to another Envoy cluster. When Envoy receives the response, it invokes the `on_http_call_response` hook on our HTTP context.

<script src="https://gist.github.com/mohashari/6913a33b918d308921c6b69ea2e21d5a.js?file=snippet-4.txt"></script>

This model is non-blocking. The WASM guest exits immediately after calling `dispatch_http_call`, yielding control back to Envoy. The VM instance is kept suspended until the callback arrives.

## Compiling, Optimizing, and Deploying to Envoy

To run this filter in production, you must target the `wasm32-wasi` architecture. The standard release build produces a target file size of around 1.5MB to 2MB. To minimize cold start VM initialization time and save system memory, we must post-process the binary using `wasm-opt` from the `binaryen` toolchain.

```bash
# snippet-5
# 1. Install the WASI target toolchain
rustup target add wasm32-wasi

# 2. Compile target crate in release mode
cargo build --target wasm32-wasi --release

# 3. View build size
ls -lh target/wasm32-wasi/release/envoy_dynamic_transform_filter.wasm

# 4. Run binary optimization pass
wasm-opt -O3 \
  target/wasm32-wasi/release/envoy_dynamic_transform_filter.wasm \
  -o target/wasm32-wasi/release/envoy_dynamic_transform_filter.opt.wasm

# 5. Check optimized size (typically reduces the binary down to ~250KB)
ls -lh target/wasm32-wasi/release/envoy_dynamic_transform_filter.opt.wasm
```

Now, map this optimized WASM module into Envoy's configuration using the `envoy.filters.http.wasm` typed HTTP filter.

```yaml
# snippet-6
static_resources:
  listeners:
  - name: ingress_listener
    address:
      socket_address:
        address: 0.0.0.0
        port_value: 8080
    filter_chains:
    - filters:
      - name: envoy.filters.network.http_connection_manager
        typed_config:
          "@type": type.googleapis.com/envoy.extensions.filters.network.http_connection_manager.v3.HttpConnectionManager
          stat_prefix: ingress_http
          route_config:
            name: local_route
            virtual_hosts:
            - name: local_service
              domains: ["*"]
              routes:
              - match:
                  prefix: "/"
                route:
                  cluster: upstream_backend
          http_filters:
          - name: envoy.filters.http.wasm
            typed_config:
              "@type": type.googleapis.com/envoy.extensions.filters.http.wasm.v3.Wasm
              config:
                name: "dynamic_transform"
                vm_config:
                  runtime: "envoy.wasm.runtime.v8"
                  vm_id: "transform_vm"
                  code:
                    local:
                      filename: "/etc/envoy/filters/envoy_dynamic_transform_filter.opt.wasm"
                  configuration:
                    "@type": "type.googleapis.com/google.protobuf.StringValue"
                    value: |
                      {
                        "auth_service_cluster": "outbound_auth_service",
                        "target_header": "x-transformed-user"
                      }
          - name: envoy.filters.http.router
            typed_config:
              "@type": type.googleapis.com/envoy.extensions.filters.http.router.v3.Router

  clusters:
  - name: upstream_backend
    connect_timeout: 0.25s
    type: LOGICAL_DNS
    dns_lookup_family: V4_ONLY
    lb_policy: ROUND_ROBIN
    load_assignment:
      cluster_name: upstream_backend
      endpoints:
      - lb_endpoints:
        - endpoint:
            address:
              socket_address:
                address: backend.service.consul
                port_value: 9000

  - name: outbound_auth_service
    connect_timeout: 0.1s
    type: LOGICAL_DNS
    dns_lookup_family: V4_ONLY
    lb_policy: ROUND_ROBIN
    load_assignment:
      cluster_name: outbound_auth_service
      endpoints:
      - lb_endpoints:
        - endpoint:
            address:
              socket_address:
                address: auth.service.consul
                port_value: 8081
```

## Telemetry and Observability inside Wasm

A silent gateway filter is a liability. In production, you need visibility into how many JWTs were decoded via the fast path, how many triggered slow path lookup callouts, and whether callout failures are spiking.

The `proxy-wasm` specification lets us define custom metrics inside Envoy. These metrics are exposed automatically alongside standard Envoy statistics via the proxy `/stats` endpoint.

<script src="https://gist.github.com/mohashari/6913a33b918d308921c6b69ea2e21d5a.js?file=snippet-7.txt"></script>

You can now call `MetricsManager::increment_counter("wasm_filter.jwt_fastpath_success", 1)` or `MetricsManager::increment_counter("wasm_filter.auth_lookup.timeout", 1)` directly inside request hooks to feed your Prometheus monitoring stack.

## Operational Gotchas and Production Hardening

Running WASM in production requires a pragmatic understanding of failure modes:

* **VM Isolation Crashing**: When your WASM code panics (e.g., calling `.unwrap()` on a `None` value), the hosting thread’s WASM VM immediately aborts. Envoy returns a `500 Internal Server Error` to the active request and recreates the VM instance. Although this isolates the fault, constant VM teardown and re-creation consume significant CPU cycles. Keep your Rust filter clean: avoid unwraps, utilize safe parsing, and log errors gracefully.
* **Heap Memory Limits**: WASM VMs are constrained by linear memory bounds. By default, Envoy sets limits on VM heap allocations (typically 16MB to 32MB). If your code loads huge cryptographic key lists or buffers large payloads, the VM will hit an out-of-memory (OOM) boundary, causing it to crash. Instrument your memory usage and avoid buffering requests unless strictly necessary.
* **Single-Threaded Event Loop Blocking**: Wasm filters run inline inside Envoy's worker event loops. Performing CPU-bound cryptographic signatures (like generating complex keys on every request) blocks the worker thread, causing execution queues to back up and degrading overall system throughput. Keep hot-path computation minimal.
* **Thread-Local Storage Cache Isolation**: Since Envoy allocates a WASM VM per worker thread, static caching structs are thread-isolated. A token validation cached on thread A will not be present on thread B. To share state across worker threads, you must leverage Envoy's shared data APIs (`get_shared_data`, `set_shared_data`), which utilize mutexes and cross-thread messaging.