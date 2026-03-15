---
layout: post
title: "WebSockets at Scale: Real-Time Backend Architecture"
date: 2026-03-15 07:00:00 +0700
tags: [websockets, real-time, backend, architecture, scalability]
description: "Design and scale WebSocket servers that handle millions of concurrent connections without dropping a message."
---

Real-time applications have fundamentally changed user expectations. Chat systems, collaborative editors, live dashboards, and multiplayer games all demand persistent, bidirectional communication that HTTP's request-response model was never designed to handle. When you first wire up a WebSocket server, it feels almost magical — the connection stays open, messages flow freely, and everything works. Then your user base grows. At ten thousand concurrent connections, you notice memory pressure. At one hundred thousand, your single server becomes a bottleneck. At one million, you realize that naive WebSocket architecture is a liability, not an asset. This post walks through the architecture decisions, connection management patterns, and infrastructure choices that let you scale WebSocket systems to millions of concurrent connections without losing messages or sleep.

## Connection Lifecycle and the C10M Problem

The fundamental challenge with WebSockets at scale is that each open connection consumes a file descriptor, memory for read/write buffers, and goroutine or thread resources depending on your runtime. Linux defaults to 1024 open file descriptors per process — a limit you'll hit embarrassingly fast in production.

Before writing a single line of application code, tune your OS. A misconfigured kernel will silently cap your concurrency regardless of how well-engineered your application is.

<script src="https://gist.github.com/mohashari/4c6ec8a44c4c886177e380eba4881a01.js?file=snippet.sh"></script>

<script src="https://gist.github.com/mohashari/4c6ec8a44c4c886177e380eba4881a01.js?file=snippet-2.sh"></script>

## The Hub-and-Spoke Connection Model

A naive implementation registers each connection in a global map and broadcasts by iterating that map. This works until write latency on slow clients blocks your broadcast loop, or a single slow map operation causes head-of-line blocking for everyone else. The solution is a dedicated hub goroutine that owns the connection registry and communicates exclusively through channels.

Go's goroutine-per-connection model is excellent here — goroutines are cheap (2KB initial stack vs. 1MB for OS threads), and channel-based communication gives you natural backpressure.

<script src="https://gist.github.com/mohashari/4c6ec8a44c4c886177e380eba4881a01.js?file=snippet-3.go"></script>

The `default` branch in the broadcast select is critical. Without it, one slow client can block your entire broadcast loop. Dropping the connection is harsh but correct — a client that cannot keep up with message delivery is a broken client.

## Sharding Hubs for Horizontal Write Throughput

A single hub becomes a bottleneck at high connection counts because all broadcast operations serialize through one goroutine. Shard your hub registry using consistent hashing on connection ID. This distributes both memory and write CPU across N independent hubs.

<script src="https://gist.github.com/mohashari/4c6ec8a44c4c886177e380eba4881a01.js?file=snippet-4.go"></script>

## Cross-Node Pub/Sub with Redis

Horizontal scaling means clients connected to different server nodes cannot communicate through in-process channels. You need a message bus. Redis Pub/Sub is the standard choice — it's fast, operationally simple, and its fan-out semantics map cleanly onto WebSocket broadcast patterns. Each server node subscribes to relevant channels and forwards messages to its local connections.

<script src="https://gist.github.com/mohashari/4c6ec8a44c4c886177e380eba4881a01.js?file=snippet-5.go"></script>

Store connection-to-topic membership in Redis as well so any node can route targeted messages without knowing which server a client is connected to.

<script src="https://gist.github.com/mohashari/4c6ec8a44c4c886177e380eba4881a01.js?file=snippet-6.sql"></script>

## Load Balancing and Sticky Sessions

Standard round-robin load balancing breaks WebSockets because the HTTP upgrade handshake and subsequent frames must hit the same backend node. Configure your load balancer to use IP hash or cookie-based sticky sessions. With NGINX, this looks straightforward but requires careful timeout tuning — the default proxy read timeout of 60 seconds will terminate idle WebSocket connections aggressively.

<script src="https://gist.github.com/mohashari/4c6ec8a44c4c886177e380eba4881a01.js?file=snippet-7.conf"></script>

## Containerizing the WebSocket Server

WebSocket servers are stateful in a way that most HTTP services are not — graceful shutdown matters enormously because you need to drain connections before terminating a pod. Kubernetes will SIGTERM your pod, giving you a window (configured via `terminationGracePeriodSeconds`) to close connections cleanly.

<script src="https://gist.github.com/mohashari/4c6ec8a44c4c886177e380eba4881a01.js?file=snippet-8.dockerfile"></script>

<script src="https://gist.github.com/mohashari/4c6ec8a44c4c886177e380eba4881a01.js?file=snippet-9.yaml"></script>

## Heartbeats and Dead Connection Detection

TCP's keepalive mechanism is too slow for application-level dead connection detection — it can take minutes to notice a silently dropped connection. Implement application-level ping/pong at 30-second intervals. Any connection that misses two consecutive pongs is considered dead and must be forcibly closed to reclaim its file descriptor.

<script src="https://gist.github.com/mohashari/4c6ec8a44c4c886177e380eba4881a01.js?file=snippet-10.go"></script>

## Scaling WebSockets Is an Operational Discipline

The engineering challenges of WebSockets at scale are real but tractable. The kernel tuning is a one-time configuration step. The sharded hub pattern scales write throughput linearly with cores. Redis Pub/Sub decouples your nodes cleanly without introducing complex dependencies. Sticky sessions at the load balancer keep connections stable during normal operation, while graceful shutdown handling ensures deployments don't disconnect thousands of users simultaneously. The most important discipline is treating every connection as a resource that must be actively managed: heartbeats to detect dead connections, bounded send buffers to protect against slow clients, and a clean shutdown path that closes gracefully rather than dropping frames. Start with these primitives in place and you'll have architecture that scales from thousands to millions of connections by adding nodes, not by rewriting your core logic.