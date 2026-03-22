---
layout: post
title: "NGINX Configuration for High-Performance Backend Services"
date: 2026-03-22 08:00:00 +0700
tags: [nginx, backend, performance, infrastructure, devops]
description: "Stop leaving performance on the table — tune NGINX worker processes, keepalives, buffers, and rate limiting for real production traffic."
---

You deployed NGINX, pointed it at your upstream, and called it done. Traffic is fine at 500 req/s. Then a flash sale hits, upstream latency creeps to 200ms, and suddenly NGINX is refusing connections with `connect() failed (111: Connection refused)` — except your upstream is still alive. What happened? NGINX ran out of keepalive connections in the pool, started spawning new TCP connections for every request, the upstream's connection table filled up, and everything fell over. This isn't a rare edge case. It's what happens when you deploy NGINX with defaults and assume it'll scale.

The defaults are designed for correctness, not throughput. Every significant traffic event I've debugged has had at least one NGINX misconfiguration at the root — wrong worker count, no upstream keepalives, buffers too small to hold a response in memory, or rate limits that hammer retry-happy clients into a thundering herd. This post covers the configuration surface that actually matters at scale.

## Worker Processes and Connections

NGINX is single-threaded per worker. The right number of workers is the number of CPU cores available to the process — not more, not less.

<script src="https://gist.github.com/mohashari/8d8b775544bcb662803538cdc7fe1916.js?file=snippet-1.txt"></script>

`worker_processes auto` resolves to the output of `nproc`. On a 16-core host, you get 16 workers. With `worker_connections 4096`, that's 65,536 theoretical concurrent connections — before OS limits interfere.

The OS limit is what bites people. Check your actual file descriptor limits:

<script src="https://gist.github.com/mohashari/8d8b775544bcb662803538cdc7fe1916.js?file=snippet-2.sh"></script>

Each connection consumes two file descriptors on the proxy side (client + upstream). With 4096 `worker_connections`, set `LimitNOFILE` to at least `worker_connections * 2 * worker_count`. Anything less and you'll hit silent connection drops that look like upstream failures.

## Upstream Keepalives: The Single Biggest Win

Without keepalive pooling, every proxied request opens a new TCP connection to your upstream. At 1,000 req/s with 50ms latency, that's 1,000 TCP handshakes per second. Your upstream's `TIME_WAIT` table fills up, ephemeral ports exhaust, and `connect() failed (111)` starts appearing in your logs.

The fix is a connection pool per upstream block:

<script src="https://gist.github.com/mohashari/8d8b775544bcb662803538cdc7fe1916.js?file=snippet-3.txt"></script>

The `proxy_http_version 1.1` and `proxy_set_header Connection ""` pair is non-negotiable. HTTP/1.0 closes connections by default. HTTP/1.1 keeps them open. If you omit the `Connection ""` override, NGINX forwards the client's `Connection: keep-alive` header to the upstream, which is a hop-by-hop header and breaks the protocol. Set it to empty explicitly.

The `keepalive 64` value is per worker. On 8 workers with 3 upstream servers, you're maintaining up to `8 * 64 = 512` idle connections distributed across 3 hosts — roughly 170 per host. Right-size this to your upstream's connection limit. A Go HTTP server defaults to no connection limit. A PostgreSQL pool of 100 connections means you want `keepalive` set so that `workers * keepalive` doesn't exceed pool size.

## Buffer Sizing to Stay Out of Disk

NGINX buffers proxy responses in memory before forwarding to the client. When a response doesn't fit in the buffer, NGINX spills to a temp file on disk — a write followed by a read, adding 2–10ms of latency on a warm filesystem and much more under I/O pressure. For API workloads where responses are typically under 64KB, you want every response to stay in memory.

<script src="https://gist.github.com/mohashari/8d8b775544bcb662803538cdc7fe1916.js?file=snippet-4.txt"></script>

`proxy_max_temp_file_size 0` is aggressive but correct for most API services. If a response exceeds your buffer total (128KB in this config), NGINX will return a 502 rather than write to disk. That's the right tradeoff — you'll catch oversized responses in staging, not absorb them as latency spikes in production.

For services that stream large files (downloads, video), you want the opposite: set `proxy_buffering off` entirely so NGINX passes bytes through without buffering, and tune `proxy_max_temp_file_size` to something generous for intermediate cases.

## Rate Limiting That Doesn't Hammer Legit Clients

The naive rate limit configuration is:

```nginx
limit_req_zone $binary_remote_addr zone=api:10m rate=100r/s;
limit_req zone=api burst=0 nodelay;
```

This is wrong. `burst=0` means any request arriving while the previous one is being processed gets a 503. Real clients — especially mobile apps — batch requests. A page load might trigger 5 API calls simultaneously. You've just 503'd 4 of them.

The correct approach layers a burst queue with a nodelay option:

<script src="https://gist.github.com/mohashari/8d8b775544bcb662803538cdc7fe1916.js?file=snippet-5.txt"></script>

`nodelay` on the main API limit means the burst bucket absorbs spikes immediately rather than queuing them with artificial delay. Without `nodelay`, a burst of 50 requests at rate 200r/s would take 250ms to drain the queue — during which response times look pathological even though NGINX is "helping."

The `geo`/`map` block handles the X-Forwarded-For issue: if you key `limit_req_zone` on `$binary_remote_addr` when running behind an ALB or HAProxy, every client maps to the same IP and your rate limit becomes global.

## Timeouts: Matching Upstream SLAs

Wrong timeouts cause two different failure modes. Too short: NGINX returns 504 before the upstream can respond, flooding your logs with false positives and tripping your alerting. Too long: a slow upstream response holds the connection open, connection slots fill, and NGINX starts refusing new connections while technically still "working."

<script src="https://gist.github.com/mohashari/8d8b775544bcb662803538cdc7fe1916.js?file=snippet-6.txt"></script>

`proxy_read_timeout` is the one that causes production incidents. Set it to your upstream's p99.9 latency with headroom. If your upstream processes 99.9% of requests in under 5 seconds, set this to 10. If you have a batch endpoint that legitimately takes 60 seconds, give it its own `location` block with a separate timeout. Don't inflate the default for all endpoints to accommodate one slow one.

## Putting It Together: Production Config Template

<script src="https://gist.github.com/mohashari/8d8b775544bcb662803538cdc7fe1916.js?file=snippet-7.txt"></script>

The `$upstream_response_time` in the log format is how you validate that your tuning is working. Before keepalives, you'll see `urt` values consistently 1–3ms higher than your upstream's actual processing time (TCP overhead). After, they'll collapse to within microseconds of the real service latency.

## Where to Start When Things Go Wrong

When latency spikes hit, check in this order:

**1. Is NGINX the bottleneck or passing through upstream latency?** Compare `$request_time` vs `$upstream_response_time` in your logs. If they're nearly identical, NGINX is transparent — the problem is upstream. If `$request_time` is significantly larger, NGINX is the bottleneck.

**2. Check active connections.** `nginx -s status` (if `stub_status` is enabled) shows active connections. If `Active connections` is near `worker_processes * worker_connections`, you're at capacity.

**3. Check upstream connection reuse.** Run `ss -s` on the upstream host and watch TIME_WAIT counts. High TIME_WAIT means keepalives aren't working — verify `proxy_http_version 1.1` and `proxy_set_header Connection ""` are set.

**4. Check for disk I/O on temp files.** `iostat -x 1` on the NGINX host. If you see consistent writes to the disk where `/tmp` lives, your proxy buffers are too small for your response sizes. Either increase `proxy_buffers` or set `proxy_max_temp_file_size 0`.

**5. Check rate limit hit rates.** Search your logs for `status=429`. If legitimate clients are getting rate limited, either your burst is too small or you're keyed on the wrong IP (load balancer IP instead of client IP).

The configuration knobs described here aren't advanced NGINX. They're the baseline that any production deployment should have. The defaults exist to be safe and correct, not to maximize throughput. Once you understand what each setting controls and why the default exists, adjusting them for your traffic pattern is mechanical — not magic.
```