---
layout: post
title: "Go Memory Model and Happens-Before Guarantees for Concurrent Data Structures"
date: 2026-03-26 08:00:00 +0700
tags: [go, concurrency, distributed-systems, backend, performance]
description: "How the Go memory model's happens-before guarantees determine what your concurrent code actually does in production."
image: "https://picsum.photos/1080/720?random=2013"
thumbnail: "https://picsum.photos/400/300?random=2013"
---

You've seen it: a cache that returns stale data under load, a counter that loses increments in bursts, a map that panics with `concurrent map read and map write` on a Tuesday at 3am. The Go race detector finds some of it, but not all. Production systems fail in ways your unit tests never surface because your mental model of concurrency doesn't match what the CPU and compiler actually do. The Go memory model tells you exactly what guarantees you have — and if you haven't read it recently, the 2022 revision changed things you probably assumed were safe.

## What the Memory Model Actually Specifies

The Go memory model defines when a write to a variable is *guaranteed* to be observed by a read of that variable in a different goroutine. Without a happens-before relationship between a write and a read, the read is allowed to observe any value — including an older write, a partially-written value, or something the compiler invented from a register.

This is not theoretical. Modern CPUs have per-core store buffers, write-combining hardware, and out-of-order execution units. The Go compiler performs optimizations — dead store elimination, loop hoisting, common subexpression elimination — that reorder or elide operations. When you have two goroutines running on separate cores, the memory they each observe is not necessarily consistent without explicit synchronization.

The model defines happens-before in terms of *synchronization operations*: channel sends/receives, mutex lock/unlock, `sync.Once.Do`, `sync/atomic` operations, and the `sync.WaitGroup` family. Everything else is unordered.

## The Happens-Before Rules You Need to Know

**Channel operations.** A send on a channel happens before the corresponding receive from that channel completes. For buffered channels, the kth send happens before the (k+n)th receive completes, where n is the buffer capacity. For unbuffered channels, the receive happens before the send completes — the synchronization goes both ways.

**Mutex operations.** For `sync.Mutex` and `sync.RWMutex`, the nth call to `Unlock` happens before the (n+1)th call to `Lock` returns. For `RLock`, the nth call to `Unlock` happens before the nth call to `RLock` returns.

**sync.Once.** The completion of `f()` in `once.Do(f)` happens before any `once.Do` returns.

**sync/atomic.** Since the 2022 revision, atomic operations have defined happens-before semantics: if atomic load A observes the value written by atomic store B, then B happens before A. This is sequential consistency for atomics of the same type at the same address.

What does *not* establish happens-before: global variable initialization order across packages (beyond what `init` guarantees), spawning goroutines (the goroutine start happens after the `go` statement, but that's it), and plain memory reads/writes.

## Data Races: The Silent Corruption

A data race occurs when two goroutines access the same variable concurrently, at least one access is a write, and there's no happens-before ordering between them. The behavior is undefined — not "might return stale data," but genuinely undefined. The compiler can assume data races don't exist and optimize accordingly, which means a data race can cause completely unrelated code to misbehave.

<script src="https://gist.github.com/mohashari/3183f15c8c2300f7d47e922308a01a02.js?file=snippet-1.go"></script>

The compiler optimization that turns `for !done` into `if !done { for {} }` is legal because without synchronization, the compiler can assume `done` doesn't change from the consumer's perspective. This has burned production systems running Go 1.14+ where the compiler became more aggressive.

## Building a Correct Concurrent Cache

Here's where happens-before becomes practical. A read-through cache needs `RWMutex` correctly, and most implementations get the double-checked locking pattern wrong in subtle ways.

<script src="https://gist.github.com/mohashari/3183f15c8c2300f7d47e922308a01a02.js?file=snippet-2.go"></script>

The happens-before guarantee here: the write to `c.cache[key]` under the write lock happens before the subsequent `RLock` returns. So any reader acquiring `RLock` after the write lock is released is guaranteed to see the new entry. This is what makes the pattern correct.

## sync.Map: When to Use It and When Not To

`sync.Map` is not a drop-in replacement for `map` + `sync.RWMutex`. Its internal structure uses two maps — a read-only map accessed without locks and a dirty map protected by a mutex — with a promotion mechanism. The happens-before guarantees it provides are specific and worth understanding.

<script src="https://gist.github.com/mohashari/3183f15c8c2300f7d47e922308a01a02.js?file=snippet-3.go"></script>

With 256 shards, lock contention is reduced by ~99.6% under uniformly distributed keys. This is the pattern used internally by `groupcache`, `ristretto`, and most high-throughput Go caches.

## Atomic Operations and Memory Ordering

The 2022 Go memory model clarified that `sync/atomic` operations provide sequentially consistent ordering. This is stronger than C++'s `memory_order_relaxed` but the same as `memory_order_seq_cst`. You can build correct lock-free structures on top of this, but you need to think carefully.

<script src="https://gist.github.com/mohashari/3183f15c8c2300f7d47e922308a01a02.js?file=snippet-4.go"></script>

The critical point: `atomic.Pointer.Store` establishes a happens-before relationship with subsequent `Load` calls that observe the stored pointer. So if you fully initialize a struct, then store a pointer to it atomically, any goroutine that loads that pointer is guaranteed to see the fully-initialized struct. This is the foundation of every correct lock-free config-reload pattern.

## sync.Once and Initialization Order

`sync.Once` is the correct tool for lazy initialization of shared resources. The memory model guarantee: `f()` in `once.Do(f)` completes before *any* call to `once.Do` returns. This includes all goroutines that block waiting for the first call to complete.

<script src="https://gist.github.com/mohashari/3183f15c8c2300f7d47e922308a01a02.js?file=snippet-5.go"></script>

## Channel Semantics and Pipeline Ordering

Channels are the most powerful synchronization primitive in Go, but their happens-before guarantees are precise and often misunderstood. A common mistake is assuming that sending a result on a channel implies all prior writes are visible to the receiver.

<script src="https://gist.github.com/mohashari/3183f15c8c2300f7d47e922308a01a02.js?file=snippet-6.go"></script>

The `close(results)` after `wg.Wait()` is correct here because `wg.Wait()` happens-after all `wg.Done()` calls, and each `wg.Done()` happens-after its corresponding send. So the channel close cannot happen before all sends complete.

## Running the Race Detector in Production

The Go race detector — `-race` — instruments every memory access with shadow memory and reports races at runtime. You should run it in two contexts: CI (always), and a canary deployment (sampled).

Running with `-race` has a 5-15x memory overhead and 2-20x CPU overhead. This is prohibitive for full production but acceptable at 1-5% of your fleet if you're catching races that unit tests miss. Google has published that they run race-enabled binaries in production for critical services at low sampling rates.

```bash
# snippet-7
# Build with race detector for canary deployment
# Using -race adds ~15% binary size and ~10x runtime overhead on contentious paths

go build -race -o bin/server-race ./cmd/server

# Run the race detector as part of CI with a realistic workload
go test -race -count=1 -timeout=120s ./...

# For load testing: run race-enabled binary against realistic traffic
# and pipe stderr to a collector — races are reported to stderr
./bin/server-race -addr=:8080 2>&1 | grep "DATA RACE" | \
    awk '{print $0}' >> /var/log/race-detector.log

# Check if you have any races in the current test run
go test -race ./... 2>&1 | tee /tmp/race-output.txt
grep -c "DATA RACE" /tmp/race-output.txt && echo "RACES FOUND" || echo "CLEAN"
```

## What the 2022 Revision Changed

Before the 2022 revision of the Go memory model, the behavior of programs that called `sync/atomic` was not precisely specified. The specification said atomics "behave like volatile reads and writes in Java" — which is unhelpfully vague and doesn't map cleanly to Go's runtime or compiler.

The 2022 revision aligned Go's memory model with the broader literature (Lamport, the C++ memory model) and specified that:

1. Atomic operations are sequentially consistent — all goroutines observe the same total order of atomic operations.
2. `sync.Mutex` and `sync.RWMutex` operations are sequentially consistent.
3. `sync.WaitGroup`, `sync.Once`, and `sync.Cond` all have specified happens-before relationships.

The practical impact: code that relied on `sync/atomic` to establish happens-before relationships between non-atomic variables was *technically undefined behavior* before 2022 but is now specified. The pattern in snippet-4 (storing a pointer atomically after initializing the struct) was always *intended* to work and did work in practice, but the 2022 spec made it formally correct.

## The Production Checklist

Before shipping any concurrent data structure, run through this list:

- Every shared variable is either protected by a mutex, accessed only through channels, or accessed only through `sync/atomic`.
- You're running `go test -race ./...` in CI and treating races as blocking failures.
- Double-checked locking re-checks the condition under the write lock.
- `sync.Map` is only used for stable-key or disjoint-write patterns; write-heavy workloads use sharded `RWMutex`.
- Config hot-reloading uses `atomic.Pointer[T]` to swap fully-initialized structs.
- Pipeline goroutines establish happens-before through channel sends, not shared variables.
- You have profiled lock contention with `go tool pprof` under realistic load — `sync.Mutex` at >30% CPU in the profile is a sharding candidate.

The memory model is not an academic specification. It's the contract between you and the compiler. Read it, know it, and your concurrent code will behave the way you intended.
```