---
layout: post
title: "Go Profiling in Production: CPU, Memory, and Goroutine Leak Detection"
date: 2026-03-17 07:00:00 +0700
tags: [go, profiling, performance, observability, debugging]
description: "Use pprof, trace, and continuous profiling tools to identify CPU hot paths, memory leaks, and goroutine starvation in live Go services."
---

Production Go services have a way of misbehaving at the worst possible moments. A service that hummed along during load testing suddenly pegs a CPU core at 100% under real traffic, or memory climbs steadily until the OOM killer arrives. The instinct is to add more logging, stare at dashboards, and guess. But Go ships with one of the most powerful profiling ecosystems in any language, and most engineers barely scratch its surface. This post walks through using `pprof`, the execution tracer, and continuous profiling to diagnose CPU hot paths, memory leaks, and goroutine starvation in services that are already running in production — without restarting them.

## Enabling the pprof HTTP Endpoint

The simplest way to expose profiling data from a running service is to import the `net/http/pprof` package. It registers handlers on the default `http.ServeMux` automatically, so you only need a side-effect import and a dedicated HTTP server. Never expose this on your public-facing port — bind it to a loopback address or an internal network interface only.

<script src="https://gist.github.com/mohashari/16e9e176259988f7ce42dbaa3c3906c3.js?file=snippet.go"></script>

Once the service is running, you can fetch a 30-second CPU profile directly with `go tool pprof`:

<script src="https://gist.github.com/mohashari/16e9e176259988f7ce42dbaa3c3906c3.js?file=snippet-2.sh"></script>

The `-http` flag opens a browser-based flame graph. The width of each frame represents cumulative time spent in that function. Look for unexpectedly wide frames that shouldn't be on the hot path — string formatting inside tight loops, JSON marshaling every request, or reflection-heavy code hiding behind an abstraction.

## Reading a Heap Profile

CPU time is only half the story. A heap profile captures allocations: both what is live at the moment of the snapshot and the cumulative allocation rate since the process started. The `alloc_space` view is particularly useful because it shows you where your code is creating garbage for the GC to collect, even if those allocations don't show up as a memory leak.

<script src="https://gist.github.com/mohashari/16e9e176259988f7ce42dbaa3c3906c3.js?file=snippet-3.go"></script>

When you open the heap profile, switch between `inuse_space` (currently live bytes) and `alloc_space` (total bytes allocated since start). A service with a flat `inuse_space` but explosively growing `alloc_space` is creating enormous GC pressure — even if it isn't leaking, you're paying CPU time for collection. Common culprits are `fmt.Sprintf` in hot paths, `[]byte` to `string` conversions, and building slices without pre-allocation.

## Detecting Goroutine Leaks

Goroutine leaks are insidious. Each leaked goroutine holds a stack (starting at 2KB, growing as needed), and hundreds of thousands of them will exhaust your heap. The goroutine profile endpoint exposes the full stack trace of every live goroutine, which makes the source of a leak obvious once you know where to look.

<script src="https://gist.github.com/mohashari/16e9e176259988f7ce42dbaa3c3906c3.js?file=snippet-4.go"></script>

To see exactly where leaked goroutines are blocked, fetch the goroutine dump:

<script src="https://gist.github.com/mohashari/16e9e176259988f7ce42dbaa3c3906c3.js?file=snippet-5.sh"></script>

You will frequently find goroutines blocked on a channel receive with no corresponding sender — a classic leak pattern when a context cancellation isn't propagated correctly, or when a worker pool is abandoned after an error. The stack trace points directly at the blocking line.

## Using the Execution Tracer for Latency Spikes

The CPU profiler samples at 100Hz and works well for finding compute-heavy hot spots, but it completely misses latency caused by scheduling delays, GC stop-the-world pauses, system call blocking, and goroutine preemption. That's where the execution tracer shines. It records a timeline of every scheduler event, giving you nanosecond-resolution visibility into what your goroutines were actually doing.

<script src="https://gist.github.com/mohashari/16e9e176259988f7ce42dbaa3c3906c3.js?file=snippet-6.go"></script>

Open the resulting trace file with `go tool trace trace-*.out`. The "Goroutine analysis" view shows minimum, maximum, and percentile scheduling latency. A goroutine with a P99 scheduling delay over 10ms when your target latency is 5ms tells you the scheduler is under pressure — usually from too many runnable goroutines competing for GOMAXPROCS threads.

## Continuous Profiling with Pyroscope

Ad-hoc profiling catches incidents but misses regressions that creep in over days. Continuous profiling runs the profiler at low sample rates perpetually and stores the data in a time-series database, so you can compare a slow Tuesday afternoon against a fast Monday morning. Pyroscope is a popular open-source option with native Go support.

<script src="https://gist.github.com/mohashari/16e9e176259988f7ce42dbaa3c3906c3.js?file=snippet-7.go"></script>

With version tags in place, you can pull up a diff flame graph between your last two deploys and immediately see which functions got hotter or colder. This turns performance regression detection from a manual investigation into a routine part of your deploy review.

## Benchmarking Allocations Before They Reach Production

The best time to catch allocation-heavy code is before it ships. Go's benchmark tooling can report per-operation allocations with `-benchmem`, and `benchstat` compares two runs with statistical significance to catch regressions in CI.

<script src="https://gist.github.com/mohashari/16e9e176259988f7ce42dbaa3c3906c3.js?file=snippet-8.sh"></script>

A typical `benchstat` output will tell you if your refactor went from 3 allocs/op to 12 allocs/op with 95% confidence. Catching that in CI costs nothing; catching it after a memory-related outage costs a lot more.

Profiling is not a debugging activity you reach for when things break — it is an observability practice you build into your services from the start. Expose the pprof endpoints behind authentication on every service, wire up continuous profiling in production, and add `benchmem` to your CI pipeline. When something does go wrong, you will have historical data to compare against rather than a blank slate. The difference between a one-hour incident and a three-day investigation often comes down to whether the profiler was already running before anyone noticed the problem.