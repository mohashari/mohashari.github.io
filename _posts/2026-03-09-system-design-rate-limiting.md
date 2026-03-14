---
layout: post
title: "System Design: Rate Limiting Algorithms Explained"
tags: [system-design, backend, architecture, performance]
description: "A deep dive into rate limiting algorithms — fixed window, sliding window, token bucket, and leaky bucket — with implementation examples."
---

Rate limiting is one of those topics that appears in every system design interview — and for good reason. Every production API needs it. Let's explore the algorithms and implementation strategies.

![Token Bucket Rate Limiting](/images/diagrams/rate-limiting-token-bucket.svg)

## Why Rate Limit?

- **Prevent abuse** — Stop one bad actor from hammering your API
- **Ensure fairness** — Every user gets a fair share of resources
- **Protect downstream** — Prevent cascading failures from traffic spikes
- **Cost control** — Many cloud services bill by the request

## Algorithm 1: Fixed Window Counter

The simplest approach. Count requests in fixed time windows.


<script src="https://gist.github.com/mohashari/f0c0c3b38566b065ca18da12396ca870.js?file=snippet.txt"></script>



<script src="https://gist.github.com/mohashari/f0c0c3b38566b065ca18da12396ca870.js?file=snippet.go"></script>


**Problem: Boundary burst**. A user can make 100 requests at 11:59:59 and 100 more at 12:00:00 — effectively 200 requests in 2 seconds.

## Algorithm 2: Sliding Window Log

Track exact timestamps of each request. No boundary burst problem.


<script src="https://gist.github.com/mohashari/f0c0c3b38566b065ca18da12396ca870.js?file=snippet-2.go"></script>


**Problem:** Memory intensive. Stores every request timestamp.

## Algorithm 3: Sliding Window Counter (Best for Most Cases)

Combines fixed window simplicity with sliding approximation. Low memory, no boundary burst.


<script src="https://gist.github.com/mohashari/f0c0c3b38566b065ca18da12396ca870.js?file=snippet-2.txt"></script>



<script src="https://gist.github.com/mohashari/f0c0c3b38566b065ca18da12396ca870.js?file=snippet-3.go"></script>


## Algorithm 4: Token Bucket

Tokens accumulate at a constant rate up to a maximum. Each request consumes one token. **Allows bursts up to bucket size.**


<script src="https://gist.github.com/mohashari/f0c0c3b38566b065ca18da12396ca870.js?file=snippet-3.txt"></script>



<script src="https://gist.github.com/mohashari/f0c0c3b38566b065ca18da12396ca870.js?file=snippet-4.go"></script>


Distributed implementation using Redis + Lua (atomic):


<script src="https://gist.github.com/mohashari/f0c0c3b38566b065ca18da12396ca870.js?file=snippet.lua"></script>


## Algorithm 5: Leaky Bucket

Requests queue up. They're processed at a fixed rate. The queue has a maximum size (excess requests dropped).


<script src="https://gist.github.com/mohashari/f0c0c3b38566b065ca18da12396ca870.js?file=snippet-4.txt"></script>


Good for **smoothing bursty traffic** into a steady stream. Used at the infrastructure layer.

## Choosing the Right Algorithm

| Algorithm | Burst Allowed | Memory | Accuracy | Best For |
|-----------|---------------|--------|----------|---------|
| Fixed Window | Yes (boundary) | Low | Low | Simple throttling |
| Sliding Window Log | No | High | Perfect | Low-traffic, strict limits |
| Sliding Window Counter | Minimal | Low | ~95% | Most API rate limiting |
| Token Bucket | Yes (controlled) | Low | High | APIs with burst allowance |
| Leaky Bucket | No | Medium | High | Traffic shaping |

## Implementation in HTTP Middleware


<script src="https://gist.github.com/mohashari/f0c0c3b38566b065ca18da12396ca870.js?file=snippet-5.go"></script>


## Rate Limit Tiers

Real APIs often have tiered limits:


<script src="https://gist.github.com/mohashari/f0c0c3b38566b065ca18da12396ca870.js?file=snippet-6.go"></script>


Rate limiting is one of those features that seems trivial until you're under attack. Build it in from day one.
