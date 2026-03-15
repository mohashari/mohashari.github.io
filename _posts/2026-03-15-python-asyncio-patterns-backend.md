---
layout: post
title: "Python Async Programming: asyncio Patterns for Backend Engineers"
date: 2026-03-15 07:00:00 +0700
tags: [python, asyncio, concurrency, backend, performance]
description: "Master Python's asyncio event loop with practical patterns for building high-throughput, non-blocking backend services."
---

Every backend engineer eventually hits the wall: your service handles 50 requests per second just fine, then at 500 it starts dropping connections, latency spikes, and the on-call pager lights up. You throw more threads at it, but threads are expensive — each one consumes ~8MB of stack space on Linux, and context-switching overhead grows faster than your request rate. The real bottleneck isn't CPU; it's I/O. Your threads are sleeping, waiting on database queries, HTTP calls, and filesystem reads. Python's `asyncio` was built precisely for this scenario — a single-threaded, cooperative concurrency model that keeps your CPU busy while I/O is in flight. But `asyncio` is famously easy to use wrong. This post walks through the patterns that actually matter in production backend systems.

## Understanding the Event Loop

Before writing a single `await`, you need a mental model of what's happening. The event loop is a scheduler. When a coroutine hits an `await`, it suspends and hands control back to the loop. The loop checks which suspended coroutines have their I/O ready (via the OS `epoll`/`kqueue` syscall), resumes them, and repeats. No threads, no locks — just cooperative yielding.

The most common mistake is treating coroutines like threads. If you block the event loop — with `time.sleep()`, a synchronous database driver, or heavy CPU computation — every other coroutine stalls. The entire system becomes single-threaded in the worst possible way.

<script src="https://gist.github.com/mohashari/36de1600ab15c5de232d0d66eac2979a.js?file=snippet.py"></script>

## Concurrent Fan-Out with `asyncio.gather`

The most immediate win from asyncio is firing multiple I/O operations concurrently. If your endpoint fetches user data, permissions, and recent activity — three separate database calls — running them sequentially means total latency is the sum of all three. `asyncio.gather` runs them concurrently, so total latency approaches the slowest single call.

<script src="https://gist.github.com/mohashari/36de1600ab15c5de232d0d66eac2979a.js?file=snippet-2.py"></script>

Note `return_exceptions=True` — without it, a single failure cancels all sibling tasks and raises immediately, which is usually not what you want for non-critical data enrichment.

## Controlled Concurrency with Semaphores

Unbounded concurrency is its own problem. Firing 10,000 simultaneous database connections will exhaust your connection pool and crash downstream services. A `Semaphore` acts as a concurrency throttle — it limits how many coroutines can execute a critical section simultaneously.

<script src="https://gist.github.com/mohashari/36de1600ab15c5de232d0d66eac2979a.js?file=snippet-3.py"></script>

## Task Groups and Structured Concurrency

Python 3.11 introduced `asyncio.TaskGroup`, which brings structured concurrency: all tasks in a group are cancelled and cleaned up if any one of them raises. This eliminates a whole class of leaked-task bugs that plague manual `gather` usage.

<script src="https://gist.github.com/mohashari/36de1600ab15c5de232d0d66eac2979a.js?file=snippet-4.py"></script>

## Async Context Managers for Resource Pools

Connection pools are the lifeblood of async backends. Libraries like `asyncpg` expose async context managers that acquire a connection from the pool, yield it, and release it back — even on exceptions. Always use pools, never open a new connection per request.

<script src="https://gist.github.com/mohashari/36de1600ab15c5de232d0d66eac2979a.js?file=snippet-5.py"></script>

## Offloading CPU-Bound Work

Asyncio does not help with CPU-bound work — compute-heavy operations will still block the event loop. The escape hatch is `loop.run_in_executor`, which runs synchronous code in a thread pool (or process pool for true parallelism) without blocking other coroutines.

<script src="https://gist.github.com/mohashari/36de1600ab15c5de232d0d66eac2979a.js?file=snippet-6.py"></script>

## Timeouts and Cancellation

Never let an async operation hang indefinitely. `asyncio.wait_for` wraps any coroutine with a deadline, raising `TimeoutError` on expiry and cleanly cancelling the underlying task.

<script src="https://gist.github.com/mohashari/36de1600ab15c5de232d0d66eac2979a.js?file=snippet-7.py"></script>

For `httpx` specifically you'd use its built-in timeout parameter, but `wait_for` works universally — useful for wrapping legacy async code or third-party coroutines that don't expose timeout arguments.

---

Asyncio's performance gains are real, but they require discipline. Use `asyncio.gather` with `return_exceptions=True` for fan-out, semaphores to protect downstream services from concurrency spikes, `TaskGroup` for any group of tasks where failure should propagate, and always use async-native libraries — a synchronous database driver or blocking HTTP client will silently undo every optimization. The rule is simple: if it does I/O, it must be async; if it's CPU-bound, push it to an executor. Get those two things right, and a single Python process can comfortably handle thousands of concurrent connections.