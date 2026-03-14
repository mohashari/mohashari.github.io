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

```
Client → L4 LB (sees: TCP port 443) → Backend Server
         ↑ Doesn't look inside the packet
```

Use cases: TCP/UDP traffic, when raw performance is critical, database proxies (e.g., HAProxy for PostgreSQL).

### Layer 7 (Application Layer)

Routes based on HTTP content — headers, cookies, URL path, body. More intelligent but more overhead.

```
Client → L7 LB → Routes /api/* to API servers
                → Routes /static/* to CDN
                → Routes /ws/* to WebSocket servers
```

Use cases: HTTP APIs, content-based routing, SSL termination, A/B testing.

## Load Balancing Algorithms

### Round Robin

Distribute requests sequentially to each server in rotation.

```
Request 1 → Server A
Request 2 → Server B
Request 3 → Server C
Request 4 → Server A (cycle repeats)
```

**Best for:** Homogeneous servers, stateless requests with similar processing time.

### Weighted Round Robin

Like round robin but respects server capacity:

```
Server A (weight 3): handles 3x more traffic
Server B (weight 1): baseline

Sequence: A, A, A, B, A, A, A, B...
```

**Best for:** Mixed-capacity servers, gradual canary deployments (route 5% to new version).

### Least Connections

Route each new request to the server with the fewest active connections.

```
Server A: 45 connections
Server B: 52 connections
Server C: 38 connections → next request goes here
```

**Best for:** Long-lived connections (WebSockets), variable-duration requests.

### Least Response Time

Routes to the server with the lowest combination of response time and active connections. The smartest basic algorithm.

```
Server A: 38ms avg, 45 connections → score: 38 × 45 = 1,710
Server B: 22ms avg, 80 connections → score: 22 × 80 = 1,760
Server C: 45ms avg, 30 connections → score: 45 × 30 = 1,350 → winner
```

### IP Hash (Sticky Sessions without Cookies)

Hash the client's IP to always route them to the same server:

```
hash(client_ip) % num_servers = server_index
```

**Problem:** Uneven distribution if many users share an IP (corporate NAT). Not ideal for large fleets.

### Consistent Hashing

Used heavily in distributed caches (Redis Cluster, Memcached). Adding/removing nodes only remaps a fraction of keys.

```
Hash ring with virtual nodes:
[ Server A ] [ Server B ] [ Server C ] [ Server A ] [ Server B ] ...
     ↑                                      ↑
  Request 1 (hash → here)             Request 2 (hash → here)
```

**Best for:** Distributed caches, content delivery, session stores.

## Session Affinity (Sticky Sessions)

Some applications store state in memory (session data, in-memory caches). Sticky sessions ensure a user always hits the same server.

### Cookie-Based Stickiness

```nginx
upstream backend {
    server 192.168.1.10:8080;
    server 192.168.1.11:8080;
    server 192.168.1.12:8080;

    sticky cookie srv_id expires=1h domain=.example.com path=/;
}
```

The load balancer inserts a cookie identifying which backend served the user.

**Warning:** Sticky sessions undermine horizontal scaling. If a server dies, those users lose session. Prefer stateless services — store sessions in Redis instead.

## Health Checks

A load balancer is useless if it routes to dead servers. Always configure health checks:

### Passive Health Checks

Mark a server unhealthy based on failed responses:

```nginx
upstream backend {
    server 192.168.1.10:8080 max_fails=3 fail_timeout=30s;
    server 192.168.1.11:8080 max_fails=3 fail_timeout=30s;
}
```

### Active Health Checks

Periodically probe servers:

```nginx
# NGINX Plus or use Lua in open-source NGINX
upstream backend {
    zone backend 64k;
    server 192.168.1.10:8080;
    server 192.168.1.11:8080;

    health_check interval=5s passes=2 fails=3 uri=/health;
}
```

Your `/health` endpoint should check:
- Database connectivity
- Cache connectivity
- Disk space
- Return 200 only if all critical dependencies are healthy

## NGINX Configuration Example

```nginx
http {
    upstream api_backend {
        least_conn;  # Algorithm

        server 10.0.0.10:8080 weight=2;  # Higher capacity
        server 10.0.0.11:8080 weight=1;
        server 10.0.0.12:8080 weight=1;

        keepalive 32;  # Reuse connections to backends
    }

    server {
        listen 443 ssl http2;
        server_name api.example.com;

        ssl_certificate /etc/ssl/cert.pem;
        ssl_certificate_key /etc/ssl/key.pem;

        location /api/ {
            proxy_pass http://api_backend;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;

            proxy_connect_timeout 5s;
            proxy_send_timeout 30s;
            proxy_read_timeout 30s;

            # Retry on errors
            proxy_next_upstream error timeout http_500;
            proxy_next_upstream_tries 3;
        }
    }
}
```

## Global Load Balancing with DNS

For multi-region deployments, use DNS-based load balancing:

```
api.example.com →
  US-East: 1.2.3.4 (primary)
  US-West: 5.6.7.8 (failover)
  EU: 9.10.11.12
```

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
