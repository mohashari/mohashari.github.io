---
layout: post
title: "Go pprof in Production: Continuous Profiling, Flamegraphs, and Memory Leak Detection"
date: 2026-03-30 08:00:00 +0700
tags: [go, performance, observability, profiling, backend]
description: "How to use Go's pprof toolchain for continuous profiling, flamegraph analysis, and tracking down memory leaks before they page you at 3am."
image: "https://picsum.photos/1080/720?random=1394"
thumbnail: "https://picsum.photos/400/300?random=1394"
---

Your Go service has been running for six days. RSS is at 4.2 GB and climbing. GC pause latency crossed 80ms an hour ago. You have zero profiling data from before the OOM kill. You are flying blind into a post-mortem with nothing but heap dumps you took too late and a gut feeling it's somewhere in the connection pool. This is the situation pprof exists to prevent — not just as a break-glass tool you run when things are already on fire, but as a continuous signal you mine before the incident starts.

Most engineers know `net/http/pprof` exists. Far fewer run it continuously in production with meaningful retention, and almost none have a systematic workflow for turning a flamegraph into an actionable fix within 20 minutes of seeing an anomaly. This post covers the full stack: instrumentation, continuous profiling with Pyroscope, flamegraph interpretation, and the specific patterns that betray goroutine and heap leaks.

## Wiring pprof Into Your Service

The standard import trick works, but blind-importing `net/http/pprof` into a service that exposes a public HTTP server is a serious mistake. pprof endpoints are unauthenticated by default and will hand anyone who reaches them the ability to trigger CPU-intensive profiling runs and dump your heap. Expose it on a separate, internal-only port.

```go
// snippet-1
package main

import (
	"context"
	"log/slog"
	"net"
	"net/http"
	_ "net/http/pprof" // registers handlers on DefaultServeMux
	"os"
	"os/signal"
	"syscall"
	"time"
)

func startDebugServer(addr string) *http.Server {
	srv := &http.Server{
		Addr:    addr,
		Handler: http.DefaultServeMux, // pprof registers here
		// Prevent a runaway client from keeping a 30s CPU profile open forever
		ReadTimeout:  35 * time.Second,
		WriteTimeout: 35 * time.Second,
	}

	ln, err := net.Listen("tcp", addr)
	if err != nil {
		slog.Error("debug server failed to listen", "addr", addr, "err", err)
		os.Exit(1)
	}

	go func() {
		if err := srv.Serve(ln); err != nil && err != http.ErrServerClosed {
			slog.Error("debug server error", "err", err)
		}
	}()

	slog.Info("debug server listening", "addr", addr)
	return srv
}

func main() {
	debugSrv := startDebugServer("127.0.0.1:6060") // bind to loopback only

	quit := make(chan os.Signal, 1)
	signal.Notify(quit, syscall.SIGINT, syscall.SIGTERM)
	<-quit

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	debugSrv.Shutdown(ctx)
}
```

In Kubernetes, expose port 6060 only via a ClusterIP service or a sidecar — never via an Ingress rule. If you're on a shared cluster with untrusted workloads, add a middleware layer that checks for a static bearer token against an environment variable.

## What the Endpoints Actually Give You

`/debug/pprof/` gives you a menu. The profiles that matter in practice:

- **`/debug/pprof/heap`** — in-use allocations and allocation sites. Default is `inuse_space`. Add `?gc=1` to force a GC cycle before sampling. This is your first stop for memory investigations.
- **`/debug/pprof/allocs`** — total allocation rate since process start, not just live heap. Useful when you're hunting allocations that are freed quickly but still driving GC pressure.
- **`/debug/pprof/goroutine`** — stack traces of all live goroutines. The fastest way to diagnose goroutine leaks.
- **`/debug/pprof/profile?seconds=30`** — 30-second CPU profile. Defaults to 30s, max is whatever your `WriteTimeout` allows.
- **`/debug/pprof/mutex`** — contended mutex profiles. Off by default; enable with `runtime.SetMutexProfileFraction(1)`.
- **`/debug/pprof/block`** — blocking profiles (channel ops, select, lock waits). Off by default; enable with `runtime.SetBlockProfileRate(1)`.

Mutex and block profiling carry real overhead — don't enable them at rate 1 in production without measuring the cost first. For most services, a rate of 100 (sample 1 in 100 events) is a reasonable starting point.

## Continuous Profiling With Pyroscope

On-demand profiling catches fires. Continuous profiling tells you what changed. The difference is the same as between a smoke alarm and a sprinkler system — one reacts, the other already has data from before the event.

Pyroscope (now Grafana Pyroscope after the acquisition) runs as a sidecar or remote server and pulls profiles from your pprof endpoints on a configurable interval. The Go SDK approach is cleaner for production because it pushes profiles rather than requiring an externally reachable endpoint.

```go
// snippet-2
package profiling

import (
	"os"
	"time"

	"github.com/grafana/pyroscope-go"
)

// Init starts continuous profiling. Call this in main() before serving traffic.
// PYROSCOPE_SERVER_ADDRESS should be set to your internal Pyroscope server URL.
func Init(serviceName string) (func(), error) {
	addr := os.Getenv("PYROSCOPE_SERVER_ADDRESS")
	if addr == "" {
		// No-op in environments without Pyroscope configured
		return func() {}, nil
	}

	profiler, err := pyroscope.Start(pyroscope.Config{
		ApplicationName: serviceName,
		ServerAddress:   addr,
		// Tag with deployment metadata for filtering in the UI
		Tags: map[string]string{
			"env":     os.Getenv("APP_ENV"),
			"version": os.Getenv("APP_VERSION"),
			"pod":     os.Getenv("POD_NAME"),
		},
		ProfileTypes: []pyroscope.ProfileType{
			pyroscope.ProfileCPU,
			pyroscope.ProfileAllocObjects,
			pyroscope.ProfileAllocSpace,
			pyroscope.ProfileInuseObjects,
			pyroscope.ProfileInuseSpace,
			pyroscope.ProfileGoroutines,
		},
		UploadRate: 15 * time.Second,
	})
	if err != nil {
		return nil, err
	}

	return func() { profiler.Stop() }, nil
}
```

With 15-second upload intervals, you get roughly 4 data points per minute. That's enough to see a heap growth trend within 2–3 minutes of it starting and correlate it with deployment events. The storage overhead is minimal — compressed pprof profiles for a moderate-traffic service run about 50–200 KB per upload.

## Reading Flamegraphs Without Getting Lost

A flamegraph shows call stacks as horizontally-proportional rectangles stacked vertically. Width = time spent (CPU) or bytes allocated (heap). A wide, flat bar at any layer means a lot of time or memory is attributable to that function. Narrow but tall stacks are deep call chains that don't dominate resources.

Three patterns that almost always mean something actionable:

**The plateau**: a function occupies 30%+ of width at a consistent level across multiple profiles taken hours apart. This is your hot path. It's not a leak, it's a performance opportunity.

**The growing layer**: a function's width increases between successive heap profiles without a corresponding traffic increase. This is your leak candidate.

**The unexpected width**: `encoding/json.Marshal` appearing in the top 5 of a service that's supposedly just doing database work. This tells you what's actually happening versus what you thought was happening.

```bash
# snippet-3
# Collect two heap profiles 60 seconds apart to compare allocations
curl -s "http://localhost:6060/debug/pprof/heap" -o heap1.pprof
sleep 60
curl -s "http://localhost:6060/debug/pprof/heap" -o heap2.pprof

# Open interactive pprof UI (starts HTTP server at :8080)
go tool pprof -http=:8080 heap2.pprof

# Compare two profiles — negative values in diff mean heap shrank, positive grew
go tool pprof -http=:8081 -diff_base=heap1.pprof heap2.pprof

# Text output for scripting — top allocators by cumulative bytes
go tool pprof -top -cum heap2.pprof

# Focus on a specific package to cut through noise
go tool pprof -focus=github.com/yourorg/service -top heap2.pprof
```

The `-diff_base` flag is underused. It's the fastest way to answer "what new allocations appeared in the last minute?" without staring at absolute numbers.

## Goroutine Leaks: The Silent Killer

Goroutine leaks don't trigger OOM kills — they trigger slow degradation that looks like a memory leak but isn't. Each goroutine costs at least 8 KB of stack (growing up to 1 GB by default), and leaking 10,000 of them over a few hours can exhaust memory or scheduler capacity without any single allocation being obviously wrong.

The canonical Go goroutine leak pattern:

```go
// snippet-4
// BAD: goroutine leaks if ctx is never cancelled and the channel is never closed
func processRequests(ctx context.Context, jobs <-chan Job) {
	for {
		select {
		case job := <-jobs:
			go func(j Job) {
				// If this blocks indefinitely (e.g., downstream service hung),
				// and nothing closes jobs or cancels ctx, this goroutine leaks
				result, err := callDownstream(ctx, j)
				if err != nil {
					log.Printf("job failed: %v", err)
					return
				}
				handleResult(result)
			}(job)
		}
		// Missing: case <-ctx.Done(): return
	}
}

// GOOD: goroutines are bounded and context-aware
func processRequestsFixed(ctx context.Context, jobs <-chan Job) {
	sem := make(chan struct{}, 100) // bound concurrency

	for {
		select {
		case <-ctx.Done():
			return
		case job, ok := <-jobs:
			if !ok {
				return
			}
			sem <- struct{}{}
			go func(j Job) {
				defer func() { <-sem }()
				// Use a derived context with timeout — never trust the caller's ctx alone
				callCtx, cancel := context.WithTimeout(ctx, 5*time.Second)
				defer cancel()
				result, err := callDownstream(callCtx, j)
				if err != nil {
					log.Printf("job failed: %v", err)
					return
				}
				handleResult(result)
			}(job)
		}
	}
}
```

To detect this in production, poll the goroutine count via `/debug/pprof/goroutine?debug=1` and alert when it crosses a threshold. For a service that normally holds 200–500 goroutines, an alert at 2,000 gives you a 10x buffer before things get critical.

```go
// snippet-5
package metrics

import (
	"runtime"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promauto"
)

var (
	goroutineCount = promauto.NewGauge(prometheus.GaugeOpts{
		Name: "go_goroutines_current",
		Help: "Current number of goroutines",
	})
	heapInUseBytes = promauto.NewGauge(prometheus.GaugeOpts{
		Name: "go_memstats_heap_inuse_bytes",
		Help: "Heap bytes in use",
	})
	gcPauseNs = promauto.NewHistogram(prometheus.HistogramOpts{
		Name:    "go_gc_pause_duration_ns",
		Help:    "GC stop-the-world pause duration in nanoseconds",
		Buckets: prometheus.ExponentialBuckets(1e4, 2, 20), // 10µs to ~10s
	})
)

// CollectRuntimeMetrics exports Go runtime stats to Prometheus on interval.
// Run this as a goroutine; cancel ctx to stop.
func CollectRuntimeMetrics(ctx context.Context, interval time.Duration) {
	ticker := time.NewTicker(interval)
	defer ticker.Stop()

	var lastGCPauseNs [256]uint64 // ring buffer size in runtime.MemStats
	var lastNumGC uint32

	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			var ms runtime.MemStats
			runtime.ReadMemStats(&ms)

			goroutineCount.Set(float64(runtime.NumGoroutine()))
			heapInUseBytes.Set(float64(ms.HeapInuse))

			// Record only new GC pauses since last check
			if ms.NumGC > lastNumGC {
				newPauses := ms.NumGC - lastNumGC
				for i := uint32(0); i < newPauses && i < 256; i++ {
					idx := (ms.NumGC - i - 1) % 256
					if ms.PauseNs[idx] != lastGCPauseNs[idx] {
						gcPauseNs.Observe(float64(ms.PauseNs[idx]))
						lastGCCPauseNs[idx] = ms.PauseNs[idx]
					}
				}
				lastNumGC = ms.NumGC
			}
		}
	}
}
```

When goroutine count climbs, pull the goroutine profile immediately: `curl -s "http://localhost:6060/debug/pprof/goroutine?debug=2" > goroutines.txt`. The `debug=2` parameter gives you full stack traces with exact source line numbers. `grep -A 5 "goroutine [0-9]" goroutines.txt | sort | uniq -c | sort -rn` will surface the most common blocking points instantly.

## Hunting Heap Leaks Systematically

A heap leak in Go almost always comes from one of four sources: items appended to a growing slice never freed, map entries never deleted, caches with no eviction policy, or long-lived goroutines holding references to large allocations.

The workflow:

1. Take a heap profile with `?gc=1` to force GC first — this ensures you're looking at truly live objects, not GC lag.
2. In `go tool pprof`, run `top20 -cum` to see the top 20 allocation sites by cumulative bytes.
3. Look for your own packages in the list. Standard library allocations you can't control are noise; your allocations are signal.
4. Use `list FunctionName` in the pprof REPL to see the annotated source with allocation counts per line.
5. Take a second profile 10 minutes later and diff them.

```bash
# snippet-6
# Force GC before capturing to avoid retained-but-unreachable objects inflating numbers
curl -s "http://localhost:6060/debug/pprof/heap?gc=1" -o heap_gc.pprof

# Launch interactive session
go tool pprof heap_gc.pprof

# In the pprof REPL:
# (pprof) top20 -cum                    — top allocators by cumulative bytes
# (pprof) list yourpkg.CacheGet         — annotated source for specific function
# (pprof) weblist yourpkg.CacheGet      — same but in browser with syntax highlighting
# (pprof) tree                          — full call tree
# (pprof) peek runtime.mallocgc        — who calls malloc most

# For heap that won't stop growing, look at allocs profile instead —
# it shows total allocated bytes, not just live ones
curl -s "http://localhost:6060/debug/pprof/allocs" -o allocs.pprof
go tool pprof -top -cum allocs.pprof
```

One pattern worth memorizing: if `inuse_space` is growing but `inuse_objects` is stable, you have large allocations. If both grow proportionally, you're leaking references to many small objects — think unbounded maps. If `alloc_space` far exceeds `inuse_space`, you have high allocation rate but good GC coverage — the problem is throughput, not a leak.

## Production Overhead: What's Safe

The pprof HTTP endpoints themselves are zero-overhead when idle — handlers only run when hit. The cost comes from the profiles themselves:

- **Heap profile**: triggers a STW pause of ~1ms on most services, plus a heap walk. Safe to run every 30 seconds in production.
- **CPU profile at 100Hz** (default): adds ~5–10% CPU overhead during the profile window. 30-second profiles once per minute is acceptable; continuous is not.
- **Goroutine profile**: scans all goroutines with a brief STW. For services with 10,000+ goroutines, this can be measurable. Test before enabling as a metric.
- **Pyroscope SDK push at 15s**: the SDK itself runs sampling in a background goroutine. Measured overhead is typically under 2% CPU and under 5 MB heap.

The mutex and block profilers at `SetMutexProfileFraction(1)` and `SetBlockProfileRate(1)` (sample every event) can add 10–30% overhead. Use them diagnostically, not continuously. A fraction/rate of 1000 gives you statistically meaningful data at under 1% overhead.

## Connecting Profiles to Business Metrics

A profile in isolation is an artifact. A profile correlated with a deployment event, a traffic spike, or an error rate increase is evidence. The workflow that makes pprof genuinely useful in production:

Keep at least 72 hours of continuous profiling data in Pyroscope. Tag every profile upload with `version` and `deploy_sha`. When an incident starts, your first action is to open the Pyroscope diff view comparing the profile from 30 minutes before the incident against the profile during the incident. The diff flamegraph will show you — visually, in seconds — which functions started allocating more or consuming more CPU after the regression was introduced.

This is the difference between a 20-minute RCA and a four-hour one.

The investment is minimal: a ~50-line SDK integration, a single Pyroscope instance (runs comfortably on 2 CPU / 4 GB RAM for dozens of services), and a Grafana panel that links your deployment markers to your profiling timeline. The return is that memory leaks stop being mysterious — they become specific function names on specific lines, with before-and-after allocation counts, available within minutes of noticing an anomaly.

pprof is not a tool you learn once and deploy. The engineers who get the most from it are the ones who pull profiles routinely, not just in incidents — who build a mental model of what normal looks like so that abnormal is immediately visible. Run it continuously. Tag everything. Diff aggressively.
```