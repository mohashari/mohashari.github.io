---
layout: post
title: "CDN Strategy for Backend Engineers: Cache-Control, Purging, and Edge Logic"
date: 2026-03-17 07:00:00 +0700
tags: [cdn, caching, performance, backend, cloud]
description: "Design an effective CDN strategy using precise Cache-Control headers, surrogate keys for instant purging, and edge compute for latency-sensitive backend logic."
---

Most backend engineers treat the CDN as someone else's problem — a layer ops teams configure once and forget. That works until a cache stampede takes down your origin at 2 AM, or a bad deploy serves stale HTML to users for forty minutes because nobody thought about purging. CDNs are not passive caches sitting in front of your app. They are a distributed execution layer you can program, and getting them wrong is expensive in both performance and correctness. This post covers the three levers that matter most: precise `Cache-Control` headers, surrogate-key-based purging, and edge compute for latency-sensitive logic.

## Cache-Control Is an API Contract

Every HTTP response your origin sends is a caching instruction. The default behavior when you omit `Cache-Control` varies by CDN vendor, but it is rarely what you want — Cloudflare will cache based on file extension heuristics; Fastly will not cache at all. Be explicit.

The key distinction most engineers miss is the difference between `public` and `private`, and between `s-maxage` and `max-age`. The `s-maxage` directive is specifically for shared caches (CDN edges); `max-age` applies to browser caches. This lets you serve a browser a 60-second TTL while keeping the CDN warm for ten minutes.

<script src="https://gist.github.com/mohashari/768ee5e75d8a2036e2e6cce3c6bdf2af.js?file=snippet.go"></script>

`stale-while-revalidate` is underused. When a cached object expires, the first request that arrives triggers a background revalidation while the CDN continues serving the old copy. This eliminates the thundering herd problem where hundreds of requests simultaneously reach your origin the moment a popular object expires.

## Surrogate Keys: Surgical Cache Purging

URL-based purging is too coarse. If your product page at `/products/42` is cached in 15 CDN PoPs and you update the product, you can purge that URL — but what about the category listing at `/categories/electronics` that embeds the same product? You would need to maintain a graph of URL dependencies, which is unmaintainable.

Surrogate keys (also called cache tags) solve this. You tag each response with one or more semantic identifiers. When the underlying data changes, you purge by tag, and every cached response bearing that tag is invalidated across all edges simultaneously.

<script src="https://gist.github.com/mohashari/768ee5e75d8a2036e2e6cce3c6bdf2af.js?file=snippet-2.go"></script>

Now when a product is updated, your application triggers a targeted purge. Here is a thin wrapper around the Fastly API that you can call from a database hook or event handler:

<script src="https://gist.github.com/mohashari/768ee5e75d8a2036e2e6cce3c6bdf2af.js?file=snippet-3.go"></script>

Trigger this after writes to your database, not before — you want the new data committed before the CDN fetches a fresh copy from origin.

## Vary Headers and Cache Fragmentation

`Vary` tells the CDN to store separate cache entries for different request variants. This is powerful but dangerous. `Vary: Accept-Encoding` is safe and ubiquitous — gzip and brotli responses are stored separately. `Vary: Cookie` is catastrophic — it defeats caching entirely because every unique cookie value creates a separate cache bucket.

If you need to vary on user state, the correct pattern is to cache the public shell and populate user-specific content client-side, or use edge compute to stitch them together.

<script src="https://gist.github.com/mohashari/768ee5e75d8a2036e2e6cce3c6bdf2af.js?file=snippet-4.go"></script>

## Edge Compute: Move Logic to the Request Path

Modern CDNs expose a JavaScript runtime at the edge — Cloudflare Workers, Fastly Compute, Lambda@Edge. This is not for replacing your origin; it is for eliminating round trips on latency-sensitive operations that do not need your database.

A/B test assignment, feature flag evaluation, auth token validation, and request routing are all good candidates. Here is a Cloudflare Worker that handles A/B routing without touching origin:

<script src="https://gist.github.com/mohashari/768ee5e75d8a2036e2e6cce3c6bdf2af.js?file=snippet-5.js"></script>

## Monitoring Cache Efficiency

None of this matters if you cannot measure it. Your CDN's cache hit ratio is a first-class metric. A ratio below 80% on cacheable content usually indicates a `Vary` misconfiguration, too-short TTLs, or query string parameters fragmenting your cache namespace.

<script src="https://gist.github.com/mohashari/768ee5e75d8a2036e2e6cce3c6bdf2af.js?file=snippet-6.sh"></script>

Track this over time and alert when it drops more than 10 percentage points from baseline — it is usually a symptom of a code change that accidentally introduced a new `Vary` dimension or started sending `Cache-Control: private` on responses that should be public.

## Putting It Together

A well-designed CDN strategy is built on four habits: always set explicit `Cache-Control` headers rather than relying on vendor defaults; tag every cacheable response with surrogate keys that map to your domain model; trigger purges from your write path, not on a timer; and push stateless, latency-sensitive logic to edge compute rather than letting it hit origin. The CDN is not a fire-and-forget cache — it is the outermost tier of your architecture, and it deserves the same intentionality you bring to database schema design. Get the headers right, build a purging pipeline you can trust, and your origin sees a fraction of the traffic it would otherwise handle.