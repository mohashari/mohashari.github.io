---
layout: post
title: "Profiling Go Applications: Finding and Fixing CPU, Memory, and Goroutine Bottlenecks"
date: 2026-03-15 07:00:00 +0700
tags: [go, profiling, performance, pprof, observability]
description: "Use pprof and flame graphs to identify and eliminate CPU, memory, and goroutine bottlenecks in Go services running in production."
---

Every Go service eventually hits a wall. Requests start taking longer, memory climbs without explanation, or the goroutine count balloons until the scheduler bogs down. The instinct is to look at logs, add more instances, or tune Kubernetes resource limits — but these are band-aids. The real answer is profiling: finding exactly which function, allocation pattern, or concurrency mistake is responsible. Go ships with one of the most capable profiling ecosystems in any language through `pprof`, and engineers who know how to use it can turn hours of guesswork into minutes of precise diagnosis.

## Enabling pprof in a Running Service

The fastest path to a profile is exposing the `net/http/pprof` HTTP handler. This is safe to run in production behind an internal network, and it costs near-zero overhead until you actually request a profile.

Importing the package as a side effect registers the `/debug/pprof/` routes on `http.DefaultServeMux`. If your service uses a custom mux, register the handlers explicitly.

<script src="https://gist.github.com/mohashari/598aa8c5f4f89ee92e7fc9bac344927f.js?file=snippet.go"></script>

Keeping the debug port bound only to `localhost` prevents external exposure while still allowing `kubectl port-forward` or SSH tunnels to reach it in a Kubernetes environment.

## Capturing a CPU Profile

CPU profiles sample the call stack at a fixed rate (100 Hz by default) over a time window. Run one against a live service during a load test or a production traffic spike for accurate results.

<script src="https://gist.github.com/mohashari/598aa8c5f4f89ee92e7fc9bac344927f.js?file=snippet-2.sh"></script>

The `-http` flag launches an interactive web UI where the flame graph view is the most actionable starting point. Wide, flat bars at the top of the flame are your hotspots — the functions where your program spends the most cumulative time.

## Reading a Flame Graph

Flame graphs can be disorienting at first. The key insight: the x-axis is not time — it is the total number of samples in which that function appeared anywhere in the call stack, sorted alphabetically within each level. A function that is wide at the top of the flame consumed that proportion of CPU time directly. A function that is wide in the middle is simply on the path to many leaf functions.

<script src="https://gist.github.com/mohashari/598aa8c5f4f89ee92e7fc9bac344927f.js?file=snippet-3.go"></script>

After deploying the fix, re-capturing the CPU profile and confirming the function has shrunk or disappeared from the flame graph closes the loop on the investigation.

## Heap Profiling for Memory Leaks

Memory issues in Go are often not leaks in the C sense — the garbage collector will reclaim unreachable objects. What you usually see instead is a large number of live allocations that the GC can never collect because something still holds a reference. The heap profile shows you which call sites are responsible for the most in-use bytes.

<script src="https://gist.github.com/mohashari/598aa8c5f4f89ee92e7fc9bac344927f.js?file=snippet-4.sh"></script>

In the pprof UI, switch from `inuse_space` (current live bytes) to `alloc_space` (total bytes allocated since startup) to distinguish a memory leak — growing `inuse_space` over time — from a service that simply allocates a lot but collects aggressively.

A common source of unexpected heap growth is caching without eviction. The pattern below shows a naive cache that retains entries forever:

<script src="https://gist.github.com/mohashari/598aa8c5f4f89ee92e7fc9bac344927f.js?file=snippet-5.go"></script>

The heap profile will show this map's allocation site growing steadily. The fix is a bounded cache using `golang.org/x/exp/cache`, a third-party LRU library, or a TTL-based expiry strategy.

## Goroutine Leaks

A goroutine leak is when goroutines are spawned but never terminate, usually because they are blocked on a channel receive or a context that never cancels. The goroutine profile makes these visible immediately.

<script src="https://gist.github.com/mohashari/598aa8c5f4f89ee92e7fc9bac344927f.js?file=snippet-6.sh"></script>

The `debug=2` parameter outputs full stack traces. If you see hundreds of goroutines all stuck at the same blocking call, you have found your leak. The most common cause is a goroutine waiting on a channel after the caller has already moved on:

<script src="https://gist.github.com/mohashari/598aa8c5f4f89ee92e7fc9bac344927f.js?file=snippet-7.go"></script>

Passing a context through to every goroutine and selecting on `ctx.Done()` is the idiomatic guard against this class of leak.

## Continuous Profiling in Production

Point-in-time profiling is valuable, but continuous profiling catches problems that only surface under specific traffic patterns or over long time windows. The Pyroscope agent for Go embeds profiling directly into the binary and pushes samples to a central store without manual intervention.

<script src="https://gist.github.com/mohashari/598aa8c5f4f89ee92e7fc9bac344927f.js?file=snippet-8.go"></script>

With this running, you can correlate a spike in p99 latency from your metrics dashboard directly to a flame graph recorded at that exact timestamp — a capability that is impossible with ad-hoc profiling.

---

Profiling is not a one-time activity reserved for emergencies. The engineers who ship the fastest, most reliable Go services treat profiling as a routine part of the development cycle: capture a baseline before a change, compare after, and quantify the improvement. The tools are built into the standard library, the overhead is negligible, and the signal is precise. There is no excuse to guess when you can measure. Start with the flame graph, follow the widest bar, fix one thing at a time, and let the data tell you when you are done.