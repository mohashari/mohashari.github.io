---
layout: post
title: "Hardening gRPC Services: Mitigating HTTP/2 Frame Flooding Attacks in Go"
date: 2026-08-12 08:00:00 +0700
tags: [grpc, security, devsecops, go]
description: "Mitigate HTTP/2 frame flooding, Rapid Reset, and Continuation attacks in Go gRPC services using production-tested configurations and rate limiters."
image: "https://picsum.photos/seed/5862/1080/720"
thumbnail: "https://picsum.photos/seed/5862/400/300"
---

Imagine your production gRPC cluster handling 100,000 requests per second with stable 30% CPU utilization. Suddenly, without warning, CPU utilization across your entire node pool spikes to 100%, health checks begin failing, and your Kubernetes pods cascade into OOMKilled states. Legitimate clients receive `Unavailable` (Code 14) errors as timeouts ripple through your microservices mesh. When you inspect the ingress logs, you do not see a massive spike in payload traffic. Instead, you see a relatively small number of TCP connections producing millions of raw HTTP/2 frames. This is the reality of application-layer Denial of Service (DoS) attacks exploiting HTTP/2 multiplexing design flaws, such as the HTTP/2 Rapid Reset (CVE-2023-44487) and Continuation Flood (CVE-2024-24786). For senior backend engineers running Go-based gRPC microservices at scale, default server configurations are an open invitation to CPU and memory exhaustion under load. Hardening the HTTP/2 transport layer is an absolute prerequisite for production survival.

## Anatomy of the Threat: HTTP/2 Multiplexing and Frame Floods

HTTP/2 improves transport efficiency by introducing multiplexing: allowing multiple concurrent conversations (streams) over a single TCP connection. Each stream is split into discrete binary frames, including `HEADERS`, `DATA`, `SETTINGS`, `PING`, `RST_STREAM`, and `CONTINUATION`. While multiplexing solves the head-of-line blocking problem inherent in HTTP/1.1, it introduces significant complexity in resource allocation.

### 1. The RST_STREAM Rapid Reset Attack (CVE-2023-44487)
In standard HTTP/2, a client initiates an RPC stream by sending a `HEADERS` frame. To cancel an in-flight stream, the client issues a `RST_STREAM` frame. Under the specification, the stream transitions immediately to the closed state, freeing the client to open a new stream. 

The Rapid Reset exploit is devastatingly simple: the client opens a stream and instantly cancels it, repeating this cycle hundreds of thousands of times per second on the same TCP connection. Because the stream transitions to closed immediately, it does not count against the server's `SETTINGS_MAX_CONCURRENT_STREAMS` limit. However, the server must still allocate CPU cycles to parse the incoming headers, create internal connection contexts, and invoke the routing table before it can process the reset frame. This forces the server's CPU to spend all its cycles in thread management and frame decoding, effectively starving legitimate requests.

### 2. The CONTINUATION Flood (CVE-2024-24786)
The HTTP/2 specification dictates that a header block must be transmitted as a contiguous sequence of frames. The block begins with a `HEADERS` frame and may be followed by one or more `CONTINUATION` frames. The server is required to buffer and parse these frames until it encounters one containing the `END_HEADERS` flag. 

In a Continuation Flood, an attacker sends a `HEADERS` frame without the `END_HEADERS` flag set, followed by an endless stream of `CONTINUATION` frames. Because the header block is technically incomplete, the stream never transitions to an active state where application-level timeouts or stream limits are evaluated. The server is forced to buffer these incoming frames in memory while parsing the header fields. This results in rapid memory consumption, culminating in kernel Out-Of-Memory (OOM) termination of the process, or extreme CPU saturation from parsing infinite headers.

### 3. Control Frame Flooding (PING, SETTINGS, WINDOW_UPDATE)
Beyond stream management, attackers can exploit control frames. A constant stream of zero-payload `PING` frames or nominal `SETTINGS` adjustments forces the server to construct response frames and manage connection state tables. This consumes memory and CPU without triggering standard endpoint-level rate limiters.

## The Architecture of Go's gRPC Server under Fire

To understand why Go services are vulnerable, we must look at how `google.golang.org/grpc` allocates resources. When a new TCP connection is accepted, the gRPC server spawns a new goroutine to manage the raw connection (`Server.handleRawConn`). Under the hood, this goroutine initializes an HTTP/2 transport engine.

When a client opens a stream (representing a unary or streaming RPC call), the transport engine reads the `HEADERS` frame, parses the metadata, and spawns a new "serving goroutine" to execute your registered gRPC handler. 

If the client sends a `RST_STREAM` frame immediately after the headers, Go's runtime must schedule the termination of the serving goroutine, clean up its stack, and recycle memory. The overhead of spawning and destroying goroutines, coupled with standard memory allocations for HTTP/2 headers parsing, is highly CPU-intensive. If the incoming frame rate exceeds the Go runtime scheduler's capacity to allocate and reclaim goroutines, CPU scheduling latency spikes, garbage collection pauses increase, and the server stops responding.

## Hardening the gRPC Server Configuration

Defending your Go gRPC services starts with configuring the `grpc.ServerOption` parameters. The default values in `grpc-go` are tuned for compatibility and ease of use, not for hostile network environments. We must restrict connection lifetimes, cap stream volumes, enforce rigid header constraints, and implement aggressive keepalive settings.

<script src="https://gist.github.com/mohashari/5c316dc76f550107e6cb849b94fbd193.js?file=snippet-1.go"></script>

By setting `grpc.MaxHeaderListSize(16384)`, you enforce a strict limit on header frame sizes. When the total header list size exceeds this value, the gRPC HTTP/2 engine immediately rejects the stream with a `PROTOCOL_ERROR`, preventing the server from buffering endless `CONTINUATION` frames. 

`keepalive.EnforcementPolicy` is critical: if a malicious client sends keepalive PINGs more frequently than once every 10 seconds, the server will terminate the TCP connection instantly.

## Connection-Level Access Control and Early Filtering

Standard gRPC interceptors operate at the RPC level. They are executed *after* the connection is established, the HTTP/2 handshake is complete, and the headers are successfully decoded. To mitigate frame floods, we must intercept requests much earlier in the life cycle—before gRPC parses stream metadata.

Go’s `grpc-go` provides `tap.ServerInHeader`, which is executed as soon as the initial `HEADERS` frame is parsed, but before a serving goroutine is allocated. We can implement a connection-level rate and size limiter inside this hook.

<script src="https://gist.github.com/mohashari/5c316dc76f550107e6cb849b94fbd193.js?file=snippet-2.go"></script>

Integrating this `InTapHandle` requires registering it as a `grpc.InTapHandle` server option. This prevents any individual client IP from consuming more than your allotted share of concurrent streams, discarding excess requests before they consume handler goroutines.

## Shielding the TCP Layer with Custom Listeners

If an attacker is opening thousands of raw TCP connections and sending frame floods, the server can still run out of file descriptors and memory. To prevent TCP handshakes from saturating your system resources, you should wrap your standard `net.Listener` with a rate-limiting and connection-capping layer. 

The following snippet demonstrates a token-bucket rate limiter that restricts the frequency of accepted connections and limits the total number of open TCP sockets on the server.

<script src="https://gist.github.com/mohashari/5c316dc76f550107e6cb849b94fbd193.js?file=snippet-3.go"></script>

Applying this `ProtectedListener` directly at the server initialization guarantees that gRPC never sees more connections than it has capacity to service. Any connections beyond `maxConns` are dropped during the TCP accept phase, avoiding TLS negotiation costs.

## Application-Level Rate Limiting and Interceptors

While early filtering defends the protocol engine, you still need application-level defenses for legitimate connections that may spike request volumes. Interceptors are ideal for this. The following implementation uses a token-bucket rate limiter to inspect peer addresses and block incoming RPCs that exceed safe operational frequencies.

<script src="https://gist.github.com/mohashari/5c316dc76f550107e6cb849b94fbd193.js?file=snippet-4.go"></script>

## Deep Observability: Monitoring Frame Dynamics

To detect HTTP/2 frame floods in production before they cause service degradation, you need to expose internal connection diagnostics. Go's `google.golang.org/grpc/stats` package allows you to hook into the lifecycle of connections and RPCs. 

A high rate of rapid connection resets is the primary signature of a Rapid Reset attack. We can track this signature by implementing a custom `stats.Handler`.

<script src="https://gist.github.com/mohashari/5c316dc76f550107e6cb849b94fbd193.js?file=snippet-5.go"></script>

By connecting `StatsMonitor` to your server using `grpc.StatsHandler()`, you can expose the `RapidResetsCount` and `ActiveStreamsCount` as Prometheus metrics. If the ratio of rapid resets to active streams spikes, you can trigger immediate alerts to identify and block the attacking IPs.

## Architectural Best Practices: Edge Defense and Reverse Proxies

Relying entirely on your Go code to defend against HTTP/2 frame flooding is dangerous. Go's runtime parser can still be forced to process malicious frames. An enterprise-grade architecture delegates HTTP/2 frame termination and rate-limiting to a dedicated ingress proxy, such as Envoy or NGINX, located at your network border.

These edge proxies should be configured to terminate client TCP connections, parse HTTP/2, validate frame structures, rate-limit clients, and multiplex the sanitized traffic down to your Go gRPC backend services over internal, trusted TCP or HTTP/2 channels.

The configuration block below shows how to configure Envoy to protect downstream gRPC nodes from HTTP/2 vulnerabilities.

<script src="https://gist.github.com/mohashari/5c316dc76f550107e6cb849b94fbd193.js?file=snippet-6.yaml"></script>

By placing Envoy in front of your Go app and using configuration controls like `max_consecutive_inbound_frames_with_empty_payload`, you offload HTTP/2 protocol sanitation to optimized C++ engines. Envoy drops attacks before they can consume CPU cycles in your Go microservice processes.

## Production Hardening Checklist

When deploying high-throughput Go gRPC services to production, audit your systems against the following security checklist:

1. **Verify Keepalives:** Set `KeepaliveEnforcementPolicy` with `MinTime` $\ge$ 5 seconds and `PermitWithoutStream: false`. This isolates and terminates clients that abuse PING frames.
2. **Cap Stream Volume:** Configure `MaxConcurrentStreams` to a sensible threshold (typically 100). Do not use unbounded defaults.
3. **Limit Header Boundaries:** Set `MaxHeaderListSize` to a low value (8KB to 16KB). This protects memory allocation tables from CONTINUATION floods.
4. **Deploy Early Filters:** Implement `tap.ServerInHeader` hooks to reject malicious path names and enforce basic connection parameters before running handlers.
5. **Add Listener Protection:** Wrap TCP listeners to drop connections when maximum threshold limits are breached or connection rates spike abnormally.
6. **Implement Edge Termination:** Terminate HTTP/2 traffic at a security-hardened reverse proxy (Envoy, Cloudflare, or NGINX) to ensure that only well-formed HTTP/2 streams reach Go code.
7. **Expose Connection Metrics:** Track stream cancellation rates using custom `stats.Handler` implementations to alert when anomalous stream churn points to rapid reset attempts.