---
layout: post
title: "Hardening Kubernetes Ingress: Mitigating Slowloris Attacks using Envoy Filter Lua Scripts"
date: 2026-08-01 08:00:00 +0700
tags: [kubernetes, envoy, security]
description: "Protect your Kubernetes ingress gateway from low-and-slow Slowloris attacks using Envoy Filter Lua scripting to monitor and terminate slow connection abuses in production."
image: "https://picsum.photos/seed/9740/1080/720"
thumbnail: "https://picsum.photos/seed/9740/400/300"
---

It is 4:30 PM on a Friday. Your Kubernetes-based API gateway, handling over 15,000 requests per second, suddenly stops routing traffic. Users are greeted with `504 Gateway Timeout` errors. Yet, when you inspect your Grafana dashboards, your Horizontal Pod Autoscalers (HPAs) aren't triggering: CPU usage on the Ingress Gateway pods is hovering around 12%, memory is flat, and network egress bandwidth shows no spike. Volumetric DDoS protection from your cloud-native Web Application Firewall (WAF) or CDN remains silent because the aggregate traffic volume is negligible. Exec'ing into your Istio ingress gateway pods reveals thousands of connections in the `ESTABLISHED` state, slowly trickling in single bytes of data over several minutes. This is a classic Slowloris attack, a "low and slow" denial-of-service vector designed to exhaust gateway connection pools, thread pools, and file descriptors. Standard timeout settings fail because the attacker sends just enough data to reset connection timers without ever completing their requests.

## The Mechanics of Slowloris on Modern Edge Proxies

Slowloris operates at Layer 7 by exploiting HTTP/1.1's connection keep-alive behavior. The attacker opens a TCP connection to the gateway and starts sending an HTTP request. Instead of sending the full request headers and payload rapidly, the client transmits headers at an extremely slow pace—for instance, one custom header line every 10 to 15 seconds. Because the HTTP standard specifies that headers are separated by double CRLF sequences (`\r\n\r\n`), Envoy keeps the connection open, waiting for the final headers to arrive before routing the request to the upstream microservices.

In a thread-per-connection server architecture like Apache, this quickly exhausts the maximum worker threads. Envoy uses a non-blocking event loop model (`epoll` on Linux) with one thread pinned to each CPU core, making it resilient to thread exhaustion. However, Envoy is still bound by kernel-level and configuration-level limits:
1. **File Descriptors (`nofile`)**: Every active TCP socket consumes a file descriptor. A typical Kubernetes deployment limits open files to 65,535 per pod. An attacker can easily spin up 60,000 connections from a distributed botnet to deplete the gateway's socket space.
2. **Downstream Connection Limits**: Ingress configurations often set maximum connection limits to prevent memory exhaustion.
3. **HTTP Connection Manager Buffer Space**: Envoy buffers headers and payloads in memory. Tens of thousands of half-open requests consume significant buffer memory, eventually invoking Envoy's Out-Of-Memory (OOM) killer.

To prevent this, we must configure Envoy to aggressively monitor connection states and terminate clients that violate minimum throughput metrics.

## Why Native Timeouts Fail and How to Tune Them

Before resorting to custom scripts, you must ensure Envoy’s native timeout knobs are properly aligned. Envoy's `HttpConnectionManager` (HCM) exposes several timeout parameters: `request_headers_timeout`, `request_timeout`, and `idle_timeout`. 

By default, many Kubernetes Ingress Controllers (like Istio Ingress Gateway, Emissary-ingress, or Contour) set relaxed defaults to accommodate slow API requests or large payload uploads. An attacker can bypass `request_headers_timeout` by simply sending a header field every 5 seconds. If the header timeout is set to 10 seconds, the client stays safe by resetting the timer with each new header byte.

We can apply basic tightening via Istio's `EnvoyFilter` or Envoy's native configuration:

<script src="https://gist.github.com/mohashari/0e8bbd9c543bb789c14607a32b5a8e35.js?file=snippet-1.yaml"></script>

`request_headers_timeout` restricts the total duration allowed to send the HTTP headers. In the configuration above, the client must finish transmitting all headers within 5 seconds of the connection start. If it fails, Envoy terminates the request with a `408 Request Timeout`. However, this native mechanism is static: it doesn't calculate the data transfer rate or handle slow POST bodies (Slow Write attacks) dynamically. If your legitimate clients use slow mobile connections (e.g., 3G/2G in emerging markets), a strict 5-second static limit will trigger false positives, while a lenient limit remains exploitable.

## Hardening Ingress with Envoy Filter Lua Scripts

To dynamically identify slow clients without blocking legitimate low-bandwidth traffic, we can inject a custom Lua script into Envoy's HTTP filter chain. The Lua filter allows us to run sandboxed scripts directly inside the worker threads' event loops via LuaJIT. 

The Lua VM execution model in Envoy is synchronous but non-blocking for CPU tasks. The script receives hooks during request lifecycle states: `envoy_on_request` (when headers are fully parsed by Envoy) and `envoy_on_body` (when chunks of the body are received).

### Mitigating Slow Headers with Lua

We can evaluate how long a client took to send its headers. The stream's start time is captured by Envoy’s internal clock. By subtracting the stream start time from the epoch time when `envoy_on_request` fires, we calculate the absolute header transmission duration.

<script src="https://gist.github.com/mohashari/0e8bbd9c543bb789c14607a32b5a8e35.js?file=snippet-2.yaml"></script>

When Envoy's parser completes header parsing, it calls `envoy_on_request`. If the client trickled headers in at 10 bytes every 4 seconds, taking a total of 12 seconds to complete a 30-byte header block, the script computes the throughput rate as `30 bytes / 12 seconds = 2.5 B/s`. This falls below the 150 B/s threshold, triggering an immediate HTTP 408 response, setting the `connection: close` header, and tearing down the underlying TCP connection to release the file descriptor.

### Mitigating Slow POST (Slow Write) Attacks

A variant of Slowloris is the "Slow POST" attack. The client completes the header handoff quickly, indicating a large payload via the `Content-Length` header (e.g., 500,000 bytes). It then transmits the body at a speed of 1 byte every few seconds. 

To intercept this, we must inspect the body chunks as they stream through the gateway. We use the Lua filter’s `envoy_on_body` callback to dynamically calculate the body transfer rate.

<script src="https://gist.github.com/mohashari/0e8bbd9c543bb789c14607a32b5a8e35.js?file=snippet-3.txt"></script>

By verifying both headers and body chunks, you build a comprehensive defense boundary inside your ingress controllers. The Lua script evaluates rate changes over time, meaning high-speed legitimate transfers that dip briefly won't be killed immediately, but malicious connection pinning will.

## Simulating the Attacks

Before deploying filters to production, you must validate their behavior. The standard tool for simulating these scenarios is `slowhttptest`, but a clean custom script utilizing Python's `asyncio` engine gives us more control over the byte rate structure.

<script src="https://gist.github.com/mohashari/0e8bbd9c543bb789c14607a32b5a8e35.js?file=snippet-4.py"></script>

If your mitigation filters are working, running this script will show connection drops after the `INTERVAL` limit crosses the threshold defined in your Lua scripts. Envoy will log warnings and terminate client connections with status `408`.

## Monitoring and Alerting in Prometheus

Hardening means nothing if you can’t observe the mitigation active in production. Envoy exports descriptive telemetry out of the box. Key metrics to monitor during a suspected Slowloris campaign are:
- `envoy_http_downstream_rq_timeout`: Incrementing metric indicating downstream requests that timed out (e.g. hitting `request_headers_timeout`).
- `envoy_http_downstream_cx_destroy_remote_active_timeout`: Envoy closing the downstream connection because of idle limits.

You can configure Prometheus rules to detect anomalies when the rate of active timeout terminations climbs abnormally.

<script src="https://gist.github.com/mohashari/0e8bbd9c543bb789c14607a32b5a8e35.js?file=snippet-5.yaml"></script>

If attackers shift their strategy to pass just above the threshold, watching standard `envoy_http_downstream_rq_timeout` metrics combined with a count of raw active connections `envoy_http_downstream_cx_active` will expose the vulnerability.

## Dynamic IP Blocklisting in Lua

If an IP address generates several timeout violations, you should blocklist it to conserve Envoy resource overhead. By maintaining a thread-local blocklist dictionary in Lua, we can preemptively close subsequent connection attempts from bad actors before processing headers.

<script src="https://gist.github.com/mohashari/0e8bbd9c543bb789c14607a32b5a8e35.js?file=snippet-6.txt"></script>

Because Envoy Lua filters execute within the context of specific worker threads, `blocklisted_ips` is thread-local. While an attacker hitting worker thread 1 will not instantly be blocked on worker thread 2, the memory is safely isolated to that thread without requiring expensive cross-thread locking structures. In a multi-replica ingress model, this local mitigation is highly efficient and avoids single points of failure like global Redis lookups for latency-sensitive edge processing.

## Production Performance Considerations

Implementing Lua filters at high throughput (e.g., 20k+ RPS) requires tuning. Keep these practices in mind:
1. **Minimize Memory Allocations**: Lua garbage collection cycles can stall the Envoy worker thread event loops. Avoid concatenating strings or generating complex objects inside hot execution paths.
2. **Exclude Health Checks**: Do not run checks on system health endpoints (`/healthz`, `/readyz`). Filter them out early in the Lua script to avoid executing strings-matching logic unnecessarily.
3. **Use the Proper Filter Ordering**: Insert your Lua filter before routing filters but after authentication/TLS decryption, so the ingress proxy works with normalized headers.