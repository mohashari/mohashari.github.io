---
layout: post
title: "WebSockets at Scale: Building Real-Time Backends That Handle Millions of Connections"
date: 2026-03-17 07:00:00 +0700
tags: [websockets, real-time, backend, scalability, go]
description: "Design and operate WebSocket servers that sustain millions of concurrent connections using fan-out architectures, sticky sessions, and horizontal scaling patterns."
---

Most backend engineers first encounter WebSockets in a toy chat app, where a single server handles a few dozen connections with no drama. Then production arrives: a live sports platform expects 400,000 concurrent users during a championship game, or a trading dashboard needs to push price ticks to every subscriber within 50 milliseconds. Suddenly the comfortable assumptions collapse — the stateful nature of WebSocket connections fights against stateless horizontal scaling, fan-out to millions of subscribers saturates a single node's memory, and a naive restart strategy drops every live session at once. Scaling WebSockets is not simply "add more servers." It requires rethinking connection lifetime, message routing, and infrastructure topology from the ground up.

## Understanding the Connection Model

A WebSocket connection is a persistent TCP socket. Unlike HTTP, the server must hold state for every active client — a file descriptor, read/write buffers, and whatever application metadata you attach. On Linux, a single process can hold hundreds of thousands of file descriptors once you raise the system limits, but the real bottleneck is usually memory: a naive Go connection struct with a 32 KB read buffer consumes over 64 KB per connection before you store any user data. At one million connections, that is 64 GB just in buffers.

Start by profiling your per-connection allocation and shrinking it. Go's `net/http` hijacker lets you take control of the raw connection and manage buffers yourself. The `gobwas/ws` library is purpose-built for this: it allocates nothing per-handshake by using stack-based parsing.

Before tuning buffers, make sure your OS is configured to support the load. Set these kernel parameters on every WebSocket node:

<script src="https://gist.github.com/mohashari/a92214539192f40ca40111d94b3cf750.js?file=snippet.sh"></script>

These raise the system file descriptor limit, tune the connection backlog, and expand socket buffer sizes. Without them, your Go process may be perfectly capable but the kernel will drop connections under load.

## A Minimal High-Throughput Hub

The classic hub-and-spoke pattern centralizes broadcast logic in a single goroutine that owns a registry of connected clients. This eliminates lock contention because map reads and writes never race — only the hub goroutine touches the map.

<script src="https://gist.github.com/mohashari/a92214539192f40ca40111d94b3cf750.js?file=snippet-2.go"></script>

The `default` branch in the broadcast loop is critical. A slow consumer — one whose `Send` channel is full — should be evicted immediately rather than blocking the entire fan-out. Holding the broadcast goroutine hostage for one lagging client creates cascading latency for everyone else.

## Sticky Sessions and Load Balancing

Because connection state lives in memory on a specific server, your load balancer must route each client's reconnection attempts back to the same node. Without sticky sessions, a client that reconnects after a brief network blip may land on a different server that knows nothing about them. Configure NGINX to use IP hash or a signed cookie:

<script src="https://gist.github.com/mohashari/a92214539192f40ca40111d94b3cf750.js?file=snippet-3.conf"></script>

`ip_hash` is simple but breaks down with NAT — thousands of users behind a corporate gateway all hash to the same backend. Prefer cookie-based stickiness in production using NGINX Plus or HAProxy's `balance leastconn` with a `stick-table`.

## Pub/Sub Fan-Out with Redis

When you need to broadcast a message to subscribers that may be spread across multiple backend nodes, you need a shared message bus. Redis Pub/Sub is the standard choice for moderate scale. Each WebSocket server subscribes to the channels its local clients care about and forwards incoming messages to its local hub.

<script src="https://gist.github.com/mohashari/a92214539192f40ca40111d94b3cf750.js?file=snippet-4.go"></script>

Each node runs one Redis subscription per logical channel (e.g., per room, per trading symbol). The node receives a published message once and fans it out locally. This avoids sending a message over the network once per subscriber — a crucial optimization when a single event might target 50,000 local connections.

## Graceful Drain During Deployments

Rolling deploys are painful with WebSockets because a `SIGTERM` kills every open connection. The solution is a drain period: stop accepting new connections, wait for clients to migrate, then shut down. Pair this with a client-side reconnect loop with exponential backoff and your users experience a blip rather than an error.

<script src="https://gist.github.com/mohashari/a92214539192f40ca40111d94b3cf750.js?file=snippet-5.go"></script>

Your Kubernetes `preStop` hook should send `SIGTERM` and set `terminationGracePeriodSeconds` long enough for the drain to complete — typically 30–60 seconds for most workloads.

<script src="https://gist.github.com/mohashari/a92214539192f40ca40111d94b3cf750.js?file=snippet-6.yaml"></script>

## Tracking Connection Health with Ping/Pong

TCP does not reliably detect a dead peer without sending data. A client that loses wifi without a clean close will leave a ghost connection on your server for minutes. WebSocket defines a ping/pong control frame mechanism specifically for this. Run a ticker that pings every client and closes any that fail to respond within one cycle:

<script src="https://gist.github.com/mohashari/a92214539192f40ca40111d94b3cf750.js?file=snippet-7.go"></script>

A 30-second ping interval with a 60-second read deadline means a dead connection is detected and cleaned up within 90 seconds in the worst case — acceptable for most applications, and tunable based on your memory vs. freshness trade-off.

## Putting It Together

Scaling WebSockets is fundamentally about accepting that your server is stateful and designing around that constraint rather than against it. Keep per-connection memory allocations minimal, use a single-goroutine hub to avoid lock contention, route fan-out through a shared pub/sub bus so broadcast doesn't cross node boundaries unnecessarily, and invest in sticky routing and graceful drains so deployments don't punish your users. With these patterns in place, a cluster of modestly sized Go servers can sustain millions of concurrent connections with sub-10ms delivery latency — the architecture scales horizontally, and each individual node stays simple enough to reason about under production pressure.