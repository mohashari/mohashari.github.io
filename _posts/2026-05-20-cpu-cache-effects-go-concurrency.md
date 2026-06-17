---
layout: post
title: "CPU Cache Effects in Go: Writing Cache-Friendly and High-Performance Concurrent Code"
date: 2026-05-20 08:00:00 +0700
tags: [go, performance, hardware-concurrency, memory-management]
description: "An exploration of CPU cache line effects in Go, focusing on false sharing, cache-locality, and how to optimize concurrent structures."
image: "https://picsum.photos/seed/9296/1080/720"
thumbnail: "https://picsum.photos/seed/9296/400/300"
---

In modern systems programming, the CPU is no longer the primary bottleneck for many highly concurrent backend applications. Instead, the latency bottleneck has shifted to the **memory subsystem**. While a modern CPU core can execute instructions in a fraction of a nanosecond, fetching data from main memory (DRAM) takes approximately 50 to 100 nanoseconds—hundreds of times slower. 

To bridge this massive performance gap, CPU manufacturers design multi-tiered cache hierarchies (L1, L2, L3). Understanding how these hardware caches operate is critical to writing highly efficient concurrent code in Go.

In this deep dive, we'll explore **cache lines**, **spatial/temporal locality**, and the performance-killing phenomenon known as **false sharing**. Finally, we'll demonstrate how to detect and eliminate false sharing in Go structs to achieve massive performance gains.

---

## 1. The Hardware Cache Hierarchy & Cache Lines

CPUs do not read and write to main memory in individual bytes. Instead, data is transferred between DRAM and the cache hierarchy in fixed-size blocks called **cache lines**. On almost all modern x86-64 and ARM64 architectures, a cache line is exactly **64 bytes** in size.

```
+-------------------------------------------------------------+
|                      Main Memory (DRAM)                     |
+------------------------------┬------------------------------+
                               │ (Transferred in 64-byte chunks)
                               ▼
+-------------------------------------------------------------+
|                        L3 Cache (Shared)                     |
+------------------------------┬------------------------------+
                               │
                               ▼
+------------------------------┴------------------------------+
|                     L2 Cache (Per Core)                      |
+------------------------------┬------------------------------+
                               │
                               ▼
+------------------------------┴------------------------------+
|                     L1 Cache (Per Core)                      |
+-------------------------------------------------------------+
```

When your Go program accesses a field in a struct, the CPU loads the entire 64-byte cache line containing that field into the L1 cache. This hardware design relies on two key principles:
*   **Temporal Locality:** If a memory location is referenced, it is highly likely to be referenced again soon.
*   **Spatial Locality:** If a memory location is referenced, it is highly likely that nearby memory locations will be referenced soon.

In Go, arrays and slices are contiguous blocks of memory, making them extremely cache-friendly. When you iterate sequentially through an array of integers, the CPU loads 64 bytes at a time, resulting in a cache hit for almost every element after the first one in the cache line.

---

## 2. The Nemesis of Concurrency: False Sharing

While caches work beautifully for single-threaded or read-only workloads, multi-threaded write workloads introduce the complex problem of **cache coherency**. 

CPUs use protocols like **MESI** (Modified, Exclusive, Shared, Invalid) to ensure that if one core modifies a value in its local cache, all other cores' caches are updated or invalidated before they can read the old value.

**False sharing** occurs when two distinct concurrent threads (or Go goroutines running on separate OS threads/CPU cores) modify completely independent variables that happen to reside on the same 64-byte cache line.

```
       Core 1 (Modifies Struct.A)            Core 2 (Modifies Struct.B)
             │                                     │
             ▼                                     ▼
+─────────────────────────────────────────────────────────────────+
|               Active Cache Line (64 Bytes)                      |
|  [ Field A (8 bytes) ]              [ Field B (8 bytes) ]       |
+─────────────────────────────────────────────────────────────────+
```

Even though Core 1 only cares about `Field A` and Core 2 only cares about `Field B`, the cache coherency hardware operates at the granularity of the *entire cache line*. 
1. Core 1 updates `Field A`, marking the entire cache line as **Modified** (M) and invalidating the corresponding cache line in Core 2's cache.
2. Core 2 wants to update `Field B`, but its cache line is now marked as **Invalid** (I). Core 2 must pause, wait for Core 1's cache to flush back to the L2/L3 cache, and reload the entire cache line.
3. Core 2 updates `Field B`, which in turn invalidates Core 1's cache line.
4. This cycle repeats continuously, causing the cache line to bounce back and forth between the cores' caches. This is known as **cache line bouncing**, and it destroys parallel performance.

---

## 3. Real-world Demonstration in Go

Let's look at a concrete Go example to demonstrate the massive impact of false sharing. We will design a simple counter struct where multiple goroutines concurrently increment their own dedicated counters.

### The Naive Struct (Vulnerable to False Sharing)

```go
// NaiveCounter is highly susceptible to false sharing.
// The fields CounterA and CounterB reside immediately next to each other in memory
// and will sit on the same 64-byte cache line.
type NaiveCounter struct {
	CounterA uint64
	CounterB uint64
}
```

Because a `uint64` is 8 bytes, `CounterA` and `CounterB` occupy a total of 16 bytes. They are guaranteed to be placed inside the same 64-byte cache line.

### The Optimized Struct (Using Padding)

To eliminate false sharing, we must ensure that `CounterA` and `CounterB` reside on separate cache lines. We do this by inserting **padding** between the fields. In Go, we can use the `golang.org/x/sys/cpu` package's `CacheLinePad` struct, or manually pad the struct with blank byte arrays.

```go
import "runtime"

// PaddedCounter prevents false sharing by placing padding between fields.
type PaddedCounter struct {
	CounterA uint64
	// 56 bytes of padding to fill the rest of the 64-byte cache line
	_        [56]byte 
	CounterB uint64
	_        [56]byte
}
```

Alternatively, Go provides an internal way to enforce alignment using `cpu.CacheLinePad` which automatically adapts to the system's cache line size:

```go
import "golang.org/x/sys/cpu"

type ModernPaddedCounter struct {
	_        cpu.CacheLinePad
	CounterA uint64
	_        cpu.CacheLinePad
	CounterB uint64
}
```

---

## 4. Benchmark and Performance Comparison

Let's write a benchmark to compare the two implementations. We will spin up two goroutines, each incrementing one of the counters 100,000,000 times concurrently.

```go
// snippet-1
package main

import (
	"sync"
	"testing"
)

type NaiveCounter struct {
	CounterA uint64
	CounterB uint64
}

type PaddedCounter struct {
	CounterA uint64
	_        [56]byte // 56 bytes padding
	CounterB uint64
	_        [56]byte
}

func BenchmarkNaiveCounter(b *testing.B) {
	counter := NaiveCounter{}
	var wg sync.WaitGroup

	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		wg.Add(2)
		go func() {
			defer wg.Done()
			for j := 0; j < 10000000; j++ {
				counter.CounterA++
			}
		}()
		go func() {
			defer wg.Done()
			for j := 0; j < 10000000; j++ {
				counter.CounterB++
			}
		}()
		wg.Wait()
	}
}

func BenchmarkPaddedCounter(b *testing.B) {
	counter := PaddedCounter{}
	var wg sync.WaitGroup

	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		wg.Add(2)
		go func() {
			defer wg.Done()
			for j := 0; j < 10000000; j++ {
				counter.CounterA++
			}
		}()
		go func() {
			defer wg.Done()
			for j := 0; j < 10000000; j++ {
				counter.CounterB++
			}
		}()
		wg.Wait()
	}
}
```

### Typical Results

When running these benchmarks on a standard multi-core developer machine (e.g., Apple M-series or Intel Core i7), the padded counter is usually **3x to 5x faster** than the naive one:

```bash
go test -bench=. -benchmem
BenchmarkNaiveCounter-12          24   49382100 ns/op          0 B/op          0 allocs/op
BenchmarkPaddedCounter-12         96   12248500 ns/op          0 B/op          0 allocs/op
PASS
```

By simply introducing 56 bytes of structure padding, we avoided cache coherency conflicts, allowing both CPU cores to run at maximum hardware efficiency.

---

## 5. Summary & Key Takeaways

When architecting high-throughput Go applications (like custom connection pools, lock-free queues, or real-time telemetry systems), keep these principles in mind:

1.  **Map Out Struct Memory Layouts:** Always align critical concurrent fields to prevent them from landing on the same 64-byte boundaries.
2.  **Use CPU Padding Wisely:** Padding is not free; it increases memory footprint and reduces spatial locality for read-heavy linear iterations. Only use padding on hot fields that are written to concurrently from separate goroutines.
3.  **Benchmark Under Real Concurrency:** CPU cache effects cannot be evaluated on single-threaded runs. Always run your benchmarks with realistic concurrency counts (`-cpu=2,4,8,12`) to verify the presence of cache line conflicts.
