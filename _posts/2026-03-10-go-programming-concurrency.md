---
layout: post
title: "Go Concurrency Patterns: Goroutines, Channels, and Beyond"
tags: [golang, concurrency, backend]
description: "Master Go's concurrency model — goroutines, channels, sync primitives, and production-ready patterns for building concurrent systems."
---

Go's concurrency model is one of its greatest strengths. Goroutines are cheap, channels make communication clean, and the standard library gives you everything you need. But getting it right requires understanding the patterns. Let's dig in.

## Goroutines: Lightweight Threads

A goroutine is a function running concurrently with other goroutines in the same address space. They start with ~8KB of stack and grow as needed.


<script src="https://gist.github.com/mohashari/a45669096e9bcc577a7cccdf9fc49615.js?file=snippet.go"></script>


In other languages, 10,000 threads would exhaust memory. In Go, this is fine.

## Channels: Communication Between Goroutines

> "Do not communicate by sharing memory; instead, share memory by communicating." — Go Proverb


<script src="https://gist.github.com/mohashari/a45669096e9bcc577a7cccdf9fc49615.js?file=snippet-2.go"></script>


## Pattern 1: Worker Pool

Limit concurrency to avoid overwhelming downstream systems:


<script src="https://gist.github.com/mohashari/a45669096e9bcc577a7cccdf9fc49615.js?file=snippet-3.go"></script>


## Pattern 2: Fan-Out, Fan-In

Distribute work across multiple goroutines, then merge results:


<script src="https://gist.github.com/mohashari/a45669096e9bcc577a7cccdf9fc49615.js?file=snippet-4.go"></script>


## Pattern 3: Context for Cancellation

Always propagate context for cancellation and deadlines:


<script src="https://gist.github.com/mohashari/a45669096e9bcc577a7cccdf9fc49615.js?file=snippet-5.go"></script>


## Pattern 4: errgroup for Parallel Operations

Run multiple operations concurrently and collect errors:


<script src="https://gist.github.com/mohashari/a45669096e9bcc577a7cccdf9fc49615.js?file=snippet-6.go"></script>


This runs all three queries in parallel, cutting 3 sequential queries (e.g., 3 × 50ms = 150ms) down to max(50ms, 50ms, 50ms) = ~50ms.

## sync.Mutex vs sync.RWMutex


<script src="https://gist.github.com/mohashari/a45669096e9bcc577a7cccdf9fc49615.js?file=snippet-7.go"></script>


Use `sync.RWMutex` when reads are much more frequent than writes.

## sync.Once for Initialization


<script src="https://gist.github.com/mohashari/a45669096e9bcc577a7cccdf9fc49615.js?file=snippet-8.go"></script>


## Common Mistakes

### Goroutine Leak

Always ensure goroutines can exit:


<script src="https://gist.github.com/mohashari/a45669096e9bcc577a7cccdf9fc49615.js?file=snippet-9.go"></script>


### Data Race

Use `go test -race` to detect data races:


<script src="https://gist.github.com/mohashari/a45669096e9bcc577a7cccdf9fc49615.js?file=snippet.sh"></script>


### Closing a Channel Twice


<script src="https://gist.github.com/mohashari/a45669096e9bcc577a7cccdf9fc49615.js?file=snippet-10.go"></script>


Go's concurrency model is powerful but requires discipline. Use `-race` in tests, always propagate context, and design goroutine lifecycles explicitly.
