---
layout: post
title: "Load Balancing Techniques: Algorithms and Real-World Patterns"
tags: [architecture, backend, devops, system-design]
description: "A comprehensive guide to load balancing algorithms, Layer 4 vs Layer 7, session affinity, health checks, and when to use each approach."
---

Load balancing is how you turn one server into many — distributing traffic to achieve high availability and horizontal scalability. Understanding the algorithms and patterns helps you design systems that stay up when individual components fail.

![Load Balancing Architecture](/images/diagrams/load-balancing.svg)

## Layer 4 vs Layer 7 Load Balancing

### Layer 4 (Transport Layer)

Routes based on IP and TCP/UDP port. Doesn't inspect content. Ultra-fast.


<script src="https://gist.github.com/mohashari/3cf7db17b8061b26efd0dea89e179f45.js?file=snippet.txt"></script>


Use cases: TCP/UDP traffic, when raw performance is critical, database proxies (e.g., HAProxy for PostgreSQL).

### Layer 7 (Application Layer)

Routes based on HTTP content — headers, cookies, URL path, body. More intelligent but more overhead.


<script src="https://gist.github.com/mohashari/3cf7db17b8061b26efd0dea89e179f45.js?file=snippet-2.txt"></script>


Use cases: HTTP APIs, content-based routing, SSL termination, A/B testing.

## Load Balancing Algorithms

### Round Robin

Distribute requests sequentially to each server in rotation.


<script src="https://gist.github.com/mohashari/3cf7db17b8061b26efd0dea89e179f45.js?file=snippet-3.txt"></script>


**Best for:** Homogeneous servers, stateless requests with similar processing time.

### Weighted Round Robin

Like round robin but respects server capacity:


<script src="https://gist.github.com/mohashari/3cf7db17b8061b26efd0dea89e179f45.js?file=snippet-4.txt"></script>


**Best for:** Mixed-capacity servers, gradual canary deployments (route 5% to new version).

### Least Connections

Route each new request to the server with the fewest active connections.


<script src="https://gist.github.com/mohashari/3cf7db17b8061b26efd0dea89e179f45.js?file=snippet-5.txt"></script>


**Best for:** Long-lived connections (WebSockets), variable-duration requests.

### Least Response Time

Routes to the server with the lowest combination of response time and active connections. The smartest basic algorithm.


<script src="https://gist.github.com/mohashari/3cf7db17b8061b26efd0dea89e179f45.js?file=snippet-6.txt"></script>


### IP Hash (Sticky Sessions without Cookies)

Hash the client's IP to always route them to the same server:


<script src="https://gist.github.com/mohashari/3cf7db17b8061b26efd0dea89e179f45.js?file=snippet-7.txt"></script>


**Problem:** Uneven distribution if many users share an IP (corporate NAT). Not ideal for large fleets.

### Consistent Hashing

Used heavily in distributed caches (Redis Cluster, Memcached). Adding/removing nodes only remaps a fraction of keys.


<script src="https://gist.github.com/mohashari/3cf7db17b8061b26efd0dea89e179f45.js?file=snippet-8.txt"></script>


**Best for:** Distributed caches, content delivery, session stores.

## Session Affinity (Sticky Sessions)

Some applications store state in memory (session data, in-memory caches). Sticky sessions ensure a user always hits the same server.

### Cookie-Based Stickiness


<script src="https://gist.github.com/mohashari/3cf7db17b8061b26efd0dea89e179f45.js?file=snippet.conf"></script>


The load balancer inserts a cookie identifying which backend served the user.

**Warning:** Sticky sessions undermine horizontal scaling. If a server dies, those users lose session. Prefer stateless services — store sessions in Redis instead.

## Health Checks

A load balancer is useless if it routes to dead servers. Always configure health checks:

### Passive Health Checks

Mark a server unhealthy based on failed responses:


<script src="https://gist.github.com/mohashari/3cf7db17b8061b26efd0dea89e179f45.js?file=snippet-2.conf"></script>


### Active Health Checks

Periodically probe servers:


<script src="https://gist.github.com/mohashari/3cf7db17b8061b26efd0dea89e179f45.js?file=snippet-3.conf"></script>


Your `/health` endpoint should check:
- Database connectivity
- Cache connectivity
- Disk space
- Return 200 only if all critical dependencies are healthy

## NGINX Configuration Example


<script src="https://gist.github.com/mohashari/3cf7db17b8061b26efd0dea89e179f45.js?file=snippet-4.conf"></script>


## Global Load Balancing with DNS

For multi-region deployments, use DNS-based load balancing:


<script src="https://gist.github.com/mohashari/3cf7db17b8061b26efd0dea89e179f45.js?file=snippet-9.txt"></script>


AWS Route 53, Cloudflare, and GCP Cloud DNS support latency-based routing (route to closest region) and health-check-based failover.

## Load Balancing Decisions Cheatsheet

| Scenario | Algorithm |
|----------|-----------|
| Identical servers, short requests | Round Robin |
| Mixed server capacities | Weighted Round Robin |
| Long-lived connections | Least Connections |
| General HTTP API | Least Response Time |
| Distributed cache | Consistent Hashing |
| Canary deployment | Weighted (5% new, 95% old) |
| WebSocket connections | Least Connections + sticky |

The best load balancer is the one you can reason about and operate. Start simple (round robin or least connections), add complexity only when you can measure the improvement.
