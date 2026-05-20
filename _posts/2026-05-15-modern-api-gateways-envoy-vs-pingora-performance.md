---
layout: post
title: "Modern API Gateways: Envoy vs. Pingora — Architecture and Performance Tradeoffs"
date: 2026-05-15 09:00:00 +0700
tags: [networking, infrastructure, rust, envoy, pingora]
description: "A deep architectural analysis of the modern API gateway landscape, comparing Envoy Proxy's C++ event-loop model against Cloudflare's new Rust-based Pingora framework."
image: ""
thumbnail: ""
---

At the edge of every microservices architecture sits an **API Gateway**. The gateway is responsible for routing, rate limiting, SSL termination, and security. Historically, companies relied on Nginx or HAProxy. However, as dynamic configurations and service meshes became standard, **Envoy Proxy** became the industry-wide default.

But the status quo is changing. In 2022, Cloudflare announced they had replaced Nginx with **Pingora**, a new in-house proxy built entirely in **Rust**. In 2024, Cloudflare open-sourced Pingora, triggering a fresh architectural debate on edge proxy designs.

In this deep dive, we will analyze the internal architectures of Envoy and Pingora, compare their thread execution models, explore their memory/CPU efficiency, and discuss the trade-offs between Envoy’s configuration-first vs. Pingora’s library-first approaches.

---

## 1. Envoy Proxy: The Event-Driven Heavyweight

Developed at Lyft and graduated under the CNCF, **Envoy** is a high-performance C++ proxy. It is designed as a universal data plane, forming the backbone of popular service meshes like Istio.

### The Threading Model (Thread-Per-Worker)

Envoy uses a multi-threaded, non-blocking **Thread-Per-Worker** model.

```
                  ┌────────────────────────┐
                  │      Main Thread       │ (Manages config & binds sockets)
                  └──────────┬─────────────┘
                             │ (Accepts & delegates sockets)
            ┌────────────────┼────────────────┐
            ▼                ▼                ▼
     ┌─────────────┐  ┌─────────────┐  ┌─────────────┐
     │ Worker 1    │  │ Worker 2    │  │ Worker 3    │ (Each runs libevent loop)
     └─────────────┘  └─────────────┘  └─────────────┘
```

1.  **Main Thread:** Manages configuration updates, stats coordination, and handles administrative endpoints. It binds to listening ports and accepts incoming connections.
2.  **Worker Threads:** When a connection is accepted, the Main Thread assigns it to a Worker Thread. Once assigned, **the connection is owned by that worker for its entire lifetime**.
3.  **Non-Blocking Event Loops:** Each worker thread runs a dedicated event loop (using `libevent`). It reads, filters, and writes bytes for its owned connections asynchronously.

### The Dynamic Configuration Engine (xDS APIs)
Envoy’s killer feature is **xDS** (XML/JSON/Protobuf Discovery Services). Envoy can dynamically update its entire routing table, upstream clusters, and SSL certs on-the-fly *without restarting*. A control plane pushes updates via gRPC, and Envoy processes them lock-free using atomic pointer swaps in memory.

---

## 2. Pingora: Rust-Based Work-Stealing Gateway

Developed by Cloudflare, **Pingora** is a library framework for building high-performance proxies in Rust.

### The Threading Model (Work-Stealing & Epoll Shared State)

Unlike Envoy, which locks a connection to a specific worker thread, Pingora leverages **Tokio's multi-threaded work-stealing runtime**.

```
Incoming Connection ──► Shared Epoll Queue (All threads listen)
                           │
             ┌─────────────┼─────────────┐
             ▼             ▼             ▼
      ┌───────────┐ ┌───────────┐ ┌───────────┐
      │ Thread A  │ │ Thread B  │ │ Thread C  │ (Threads steal tasks from queue)
      └───────────┘ └───────────┘ └───────────┘
```

1.  **Shared Epoll:** Pingora threads share a single `epoll` file descriptor for incoming connection events.
2.  **Tokio Work-Stealing Scheduler:** When a network socket becomes readable, any idle thread in the pool can grab (steal) the event and process it. 
3.  **Thread Migration:** If Thread A gets blocked waiting for an upstream backend, Tokio's scheduler migrates other active connections off Thread A onto Thread B.

This model is extremely resilient against **Head-of-Line blocking**. If one client sends an extremely heavy request that burns CPU cycles (e.g., decrypting a complex payload), it won't freeze other lightweight requests that happen to be queued on the same thread (which frequently happens in Envoy's strict thread-per-worker model).

---

## 3. Performance and Memory Footprint Tradeoffs

### Memory Overhead (Rust Wins)
*   **Envoy:** C++ has no garbage collector, but Envoy's massive feature set and extensive internal state tracking (hundreds of Prometheus metrics tracked per upstream pod) mean it has a substantial memory footprint. A baseline idle Envoy process frequently consumes **50MB - 100MB** of RAM.
*   **Pingora:** Rust enables precise memory management. Because Pingora is a modular library framework, you only compile the features you actually use. An idle Pingora edge node can run on **under 5MB** of RAM, making it ideal for resource-constrained environments or sidecar containers.

### CPU Efficiency and Latency
In Cloudflare's production benchmarks, Pingora achieved:
*   **70% reduction in CPU consumption** compared to their old Nginx/Lua architecture under identical loads.
*   **67% reduction in median response latency** due to superior upstream connection reuse (pooling).

---

## 4. Configuration-First vs. Library-First (Code-First)

The biggest difference between Envoy and Pingora is not performance—it is **Developer Experience (DX)**.

### Envoy: Declarative YAML (Configuration-First)
Envoy is a pre-compiled binary. You customize its behavior entirely through massive declarative YAML configuration files or a gRPC control plane.

```yaml
# Envoy Route Config Example
static_resources:
  listeners:
  - name: listener_0
    address:
      socket_address: { address: 0.0.0.0, port_value: 10000 }
    filter_chains:
    - filters:
      - name: envoy.filters.network.http_connection_manager
        typed_config:
          "@type": type.googleapis.com/envoy.extensions.filters.network.http_connection_manager.v3.HttpConnectionManager
          route_config:
            name: local_route
            virtual_hosts:
            - name: local_service
              domains: ["*"]
              routes:
              - match: { prefix: "/" }
                route: { cluster: service_backend }
```
*   **Pros:** Standardized, language-agnostic. Integrates easily with Kubernetes operators.
*   **Cons:** "YAML Programming." Debugging routing logic or writing custom filter policies involves navigating thousands of lines of verbose schema definitions. Custom extensions require writing C++ filters or running sandboxed **WebAssembly (WASM)** modules, which introduce latency penalties.

### Pingora: Rust Code (Library-First)
Pingora is *not* a pre-compiled server. It is a library (crate) that you import into your own Rust codebase. You implement its behavior by overriding Rust traits:

```rust
use async_trait::async_trait;
use pingora_core::prelude::*;
use pingora_proxy::{ProxyHttp, Session};

pub struct MyGateway;

#[async_trait]
impl ProxyHttp for MyGateway {
    type CTX = ();
    fn new_ctx(&self) -> Self::CTX {}

    // Custom upstream routing logic written directly in Rust!
    async fn upstream_peer(&self, _session: &mut Session, _ctx: &mut Self::CTX) -> Result<Box<HttpPeer>> {
        let addr = ("127.0.0.1", 8080);
        let peer = Box::new(HttpPeer::new(addr, false, "backend.local".to_string()));
        Ok(peer)
    }
}

fn main() {
    let mut server = Server::new(None).unwrap();
    let mut proxy = pingora_proxy::http_proxy_service(&server.configuration, MyGateway);
    server.add_service(proxy);
    server.run_forever();
}
```
*   **Pros:** Unlimited flexibility. You can call databases directly inside filters, build custom authentication mechanisms, log events to Kafka, and handle request modifications using type-safe Rust code with zero performance penalties.
*   **Cons:** Requires Rust engineering expertise. You must compile and deploy your own proxy binaries. There is no standard declarative configuration out-of-the-box.

---

## Architectural Comparison Matrix

| Feature | Envoy Proxy | Pingora |
| :--- | :--- | :--- |
| **Language** | C++ | Rust |
| **Threading Model** | Thread-per-worker (libevent) | Work-stealing (Tokio async thread pool) |
| **Head-of-Line Blocking**| Vulnerable within worker thread | Resilient (automatic task migration) |
| **Primary Interface** | Declarative Configuration (YAML/JSON) | Programmatic Library API (Rust Crates) |
| **Dynamic Updates** | Native xDS APIs (Excellent) | Programmatic (Requires custom control code) |
| **Custom Filters** | C++ / WASM (High overhead) | Native Rust Traits (Zero overhead) |
| **Memory Footprint** | Moderate (~50MB+ idle) | **Extremely Low (~5MB idle)** |

## Conclusion

The choice between Envoy and Pingora depends on your organization's infrastructure strategy.

*   **Choose Envoy** if you run standard Kubernetes service meshes, need declarative YAML definitions, and want an out-of-the-box, battle-tested gateway that requires no software development to deploy.
*   **Choose Pingora** if you are building custom CDNs, edge firewalls, or ultra-high-throughput API gateways where performance, memory footprint (e.g. sidecars), and custom, low-latency filter logic (written directly in Rust) are key business drivers.
