---
layout: post
title: "Linux cgroups v2 and Container Memory Pressure: OOM Scoring, Memory Limits, and Swap Accounting"
date: 2026-03-31 08:00:00 +0700
tags: [linux, containers, kubernetes, performance, systems]
description: "How cgroups v2 memory accounting actually works, why your containers OOM at the wrong time, and how to fix it."
image: "https://picsum.photos/1080/720?random=6810"
thumbnail: "https://picsum.photos/400/300?random=6810"
---

Your container gets OOM-killed at 400MB when the limit is 512MB. You've seen this. The app is healthy, GC hasn't run yet, the heap is nowhere near full — but the kernel disagrees. You crank the limit up, redeploy, and it happens again at 600MB. You're fighting a system you don't fully understand, and the stakes are a 3am page when the wrong pod dies in production. Linux cgroups v2 changed memory accounting significantly from v1, and most of the tribal knowledge engineers carry around — memory limits, OOM scoring, swap behavior — is either wrong or outdated. This post covers how the memory subsystem actually works in cgroups v2, where containers get it wrong, and how to instrument and tune it properly.

## The cgroups v2 Memory Model

In cgroups v1, memory controllers were mounted separately under `/sys/fs/cgroup/memory/`. In v2, everything lives under a unified hierarchy at `/sys/fs/cgroup/`. This isn't cosmetic — it changes how limits are inherited, how pressure propagates, and critically, what counts toward a limit.

The key files you need to know:

<script src="https://gist.github.com/mohashari/86dff7c818142cfa471fcf4c1a057182.js?file=snippet-1.sh"></script>

The critical difference from v1: `memory.current` includes the page cache. If your app reads a 200MB config file at startup, that 200MB counts against the limit even though it's just cached disk data and will be reclaimed under pressure. In v1, you could set `memory.memsw.limit_in_bytes` separately; in v2, swap is accounted through `memory.swap.max` as a delta on top of memory — not a combined limit.

## OOM Score: What Actually Gets Killed

When the kernel's OOM killer fires, it doesn't kill the process consuming the most memory. It scores every process using `oom_score`, then kills the highest score. The score is roughly: (memory usage as percentage of total RAM) × 10, then adjusted by `oom_score_adj` (-1000 to +1000).

Container runtimes set `oom_score_adj` on the container's init process. Kubernetes sets it based on QoS class:

- **Guaranteed** (requests == limits): -997
- **Burstable** (requests < limits): 2 to 999, proportional to requests
- **BestEffort** (no requests/limits): 1000

This matters in production: a Guaranteed pod running at 90% of its 2GB limit will have a lower OOM score than a BestEffort pod using 10MB. The BestEffort pod dies first. This is correct behavior, but it surprises engineers who assume "highest memory user gets killed."

<script src="https://gist.github.com/mohashari/86dff7c818142cfa471fcf4c1a057182.js?file=snippet-2.sh"></script>

One subtle failure mode: if your application forks child processes (common in Python multiprocessing, or any `fork()`-based worker model), each child inherits the parent's `oom_score_adj`. If the parent has `oom_score_adj = -997` (Guaranteed), every worker is also -997. The kernel will prefer to kill other pods entirely before touching your workers, even if your workers are the ones leaking memory and causing system-wide pressure. You need to be deliberate about this.

## memory.high: The Throttle You're Not Using

Most Kubernetes engineers know `memory.limit_bytes` maps to `memory.max` in cgroups v2. Fewer know about `memory.high`, and it's the more useful knob for production workloads.

When `memory.current` exceeds `memory.high`, the kernel starts aggressively reclaiming pages from that cgroup before allocating more. The process isn't killed — it's throttled. Allocation latency increases, swap pressure builds, and if the process can shed page cache it will. Only when `memory.current` exceeds `memory.max` does the OOM killer fire.

Kubernetes 1.27+ exposes this via the `MemoryQoS` feature gate (beta in 1.28, enabled by default in 1.29):

```yaml
# snippet-3
# Pod spec with explicit memory QoS tuning
# memory.high is set to requests * throttlingFactor (default 0.9 of limits when requests are set)
apiVersion: v1
kind: Pod
metadata:
  name: memory-sensitive-app
  annotations:
    # Force specific memory.high via alpha annotation (Kubernetes 1.27+)
    # Otherwise calculated as: requests / limits ratio applied to memory.high
spec:
  containers:
  - name: app
    image: your-app:latest
    resources:
      requests:
        memory: "384Mi"   # memory.high ~ 384Mi * throttlingFactor
      limits:
        memory: "512Mi"   # memory.max = 512Mi
    # Without MemoryQoS: only memory.max is set
    # With MemoryQoS: memory.high = requests = 384Mi
    # This gives you 128Mi of "burst" before OOM, with throttling as a signal
```

The practical effect: set `requests` to your steady-state usage and `limits` to your burst ceiling. With MemoryQoS enabled, your application gets throttled at the request level, which typically manifests as increased GC pauses in JVM apps or slower allocation in Go — detectable, not fatal.

## Page Cache Accounting and Why Your Limits Feel Wrong

Here's the failure mode that bites Java services hardest: at startup, the JVM reads hundreds of JAR files, the kernel pages them in, and `memory.current` spikes well above the actual heap usage. The OOM killer sees 400MB used against a 512MB limit and fires, even though the JVM's `-Xmx` is set to 256MB.

<script src="https://gist.github.com/mohashari/86dff7c818142cfa471fcf4c1a057182.js?file=snippet-4.sh"></script>

This is exactly what Kubernetes uses for the "Working Set" metric in `kubectl top pod`. It's `memory.current - inactive_file`. The inactive file cache is page cache that hasn't been accessed recently — the kernel will reclaim it before OOM-killing. Your limit should be sized against the working set, not the raw RSS.

## Swap Accounting in cgroups v2

In cgroups v1, `memory.memsw.limit_in_bytes` set a combined memory+swap limit. In v2, swap is a separate controller: `memory.swap.max` sets the maximum swap usage *in addition to* the memory limit. If `memory.max = 512Mi` and `memory.swap.max = 256Mi`, the process can use up to 768Mi of total virtual memory before dying.

Kubernetes disables swap by default and the kubelet refuses to start on nodes with swap enabled — unless you use the `NodeSwap` feature gate (stable in 1.30 for cgroups v2 nodes). When you enable it, you configure swap per-pod:

```yaml
# snippet-5
# Kubernetes swap configuration (requires NodeSwap feature gate + cgroups v2)
# kubelet config: --fail-swap-on=false, --feature-gates=NodeSwap=true
apiVersion: v1
kind: Pod
metadata:
  name: swap-enabled-app
spec:
  os:
    name: linux
  containers:
  - name: app
    image: your-app:latest
    resources:
      requests:
        memory: "256Mi"
      limits:
        memory: "512Mi"
    # swapBehavior options:
    # - NoSwap (default): memory.swap.max = 0
    # - LimitedSwap: swap proportional to (limits - requests) / limits
    # - UnlimitedSwap (not yet supported for individual containers)
  # Pod-level swap behavior
  # LimitedSwap calculates: swap = limits * (1 - requests/limits)
  # For this pod: swap = 512Mi * (1 - 256/512) = 256Mi
```

For most stateless services, you want `NoSwap`. Swap is useful for: Java services with large heap that can tolerate latency spikes (swapped heap pages hit on GC, not on hot path), worker processes that are idle most of the time, and memory-mapped file workloads where the kernel's swap-out heuristics align with access patterns. It's actively harmful for: any latency-sensitive service, anything with frequent random heap access, Redis or any in-memory data store.

## PSI: Pressure Stall Information as an Early Warning

cgroups v2 exposes PSI metrics via `memory.pressure`. This is the most underused signal in the ecosystem. PSI tells you the percentage of time tasks were stalled waiting for memory, broken into `some` (at least one task stalled) and `full` (all tasks stalled, i.e., no progress):

<script src="https://gist.github.com/mohashari/86dff7c818142cfa471fcf4c1a057182.js?file=snippet-6.sh"></script>

Facebook's (Meta's) `senpai` daemon uses PSI to proactively reclaim memory from cgroups before they hit limits, rather than waiting for the OOM killer. Systemd's `systemd-oomd` does the same. The pattern: watch `memory.pressure` full avg10/avg60, and when it crosses a threshold, either kill the lowest-priority task yourself or trigger a scale-out before the kernel does something worse.

## Practical Tuning Checklist

Given all of this, here's what actually matters in production:

**Set limits based on working set, not RSS.** Use `memory.stat inactive_file` to understand how much of your container's footprint is reclaimable page cache. If you're running a JVM service, `kubectl top pod` shows working set — that's your baseline.

**Enable MemoryQoS and set accurate requests.** `memory.high` is a far better signal than OOM — it throttles rather than kills, and it's observable. Set requests to your P95 steady-state memory usage so `memory.high` fires before you're in danger.

**Audit OOM score adjustments for forking applications.** If your app forks workers and you're running as Guaranteed QoS, your workers have `oom_score_adj = -997`. Ensure that's intentional. Worker pools that leak memory should have a higher adj so they die before the parent.

**Instrument PSI at the node level.** Add `memory.pressure` scraping to your node exporter or write a small daemonset that watches cgroup pressure files. An `avg10 full > 2%` is a more actionable alert than "pod was OOM-killed" — you can act before the kill.

**Be explicit about swap.** On cgroups v2 nodes with NodeSwap enabled, explicitly set `swapBehavior: NoSwap` for latency-sensitive services. Don't rely on the default. Know which pods benefit from swap and which will be degraded by it.

The OOM killer is a last resort, not a control mechanism. cgroups v2 gives you the tools — `memory.high` for throttling, PSI for pressure visibility, explicit swap accounting — to stay well clear of it. Use them.