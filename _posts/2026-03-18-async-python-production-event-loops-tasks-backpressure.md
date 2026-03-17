---
layout: post
title: "Async Python in Production: Event Loops, Tasks, and Backpressure"
date: 2026-03-18 07:00:00 +0700
tags: [python, asyncio, concurrency, backend, performance]
description: "Build high-throughput async Python services using asyncio primitives, task management, structured concurrency, and backpressure control in real-world systems."
---

Most Python services don't fail because of slow algorithms or bad database queries — they fail because async code is misused at the architectural level. Engineers reach for `asyncio` to handle thousands of concurrent connections, then discover their event loop is stalling on blocking calls, their task queues have no depth limits, and a single slow downstream service cascades into a full process hang. Understanding how CPython's event loop actually schedules work, how tasks are created and cancelled, and how to implement backpressure separates services that survive production traffic from ones that fall apart under load.

## The Event Loop Is Single-Threaded — Act Like It

The event loop runs on one thread. Every `await` is a yield point where the loop can schedule another coroutine. The moment you call a blocking function without an executor, you block the entire loop — all pending I/O, all scheduled callbacks, everything. This is the most common source of latency spikes in async Python services.

The pattern below shows the wrong and right way to handle CPU-bound or blocking I/O work. Using `run_in_executor` offloads the blocking call to a thread pool, keeping the event loop free.

<script src="https://gist.github.com/mohashari/73c00bd53f748fbc28306fdc2f92a3ce.js?file=snippet.py"></script>

## Task Creation and Lifecycle Management

`asyncio.create_task()` schedules a coroutine to run concurrently. The critical mistake is fire-and-forget — creating tasks without holding a reference, which lets the garbage collector cancel them mid-execution. Always store task references and handle their results or exceptions explicitly.

<script src="https://gist.github.com/mohashari/73c00bd53f748fbc28306fdc2f92a3ce.js?file=snippet-2.py"></script>

## Structured Concurrency with TaskGroup

Python 3.11 introduced `asyncio.TaskGroup`, which enforces structured concurrency: all tasks in the group must complete before the block exits, and if any task raises, remaining tasks are cancelled automatically. This eliminates an entire class of resource leak bugs.

<script src="https://gist.github.com/mohashari/73c00bd53f748fbc28306fdc2f92a3ce.js?file=snippet-3.py"></script>

## Semaphores for Concurrency Limits

Spinning up one task per item in a list of thousands will overwhelm downstream services and exhaust file descriptors. `asyncio.Semaphore` is the right primitive to bound concurrency — it's an async-aware counter that blocks tasks when the limit is reached.

<script src="https://gist.github.com/mohashari/73c00bd53f748fbc28306fdc2f92a3ce.js?file=snippet-4.py"></script>

## Implementing Backpressure with asyncio.Queue

Without backpressure, producers outrun consumers and memory grows unbounded until the process is killed by the OOM killer. `asyncio.Queue` with a `maxsize` argument blocks producers when the queue is full — this is explicit backpressure. The producer awaits `queue.put()`, which suspends until a consumer calls `queue.get()`.

<script src="https://gist.github.com/mohashari/73c00bd53f748fbc28306fdc2f92a3ce.js?file=snippet-5.py"></script>

## Timeout Handling That Actually Works

A common mistake is wrapping an entire batch operation in a single `asyncio.wait_for()`. If it times out, you lose all results, including work that already completed. The better pattern is per-task timeouts combined with `asyncio.wait()` using `FIRST_EXCEPTION` or `ALL_COMPLETED` modes with explicit deadline tracking.

<script src="https://gist.github.com/mohashari/73c00bd53f748fbc28306fdc2f92a3ce.js?file=snippet-6.py"></script>

## Profiling the Event Loop

When a service shows mysterious latency, the first question is whether the event loop is being blocked. The snippet below installs a slow-callback debug hook and uses `loop.slow_callback_duration` to surface calls that held the loop for too long.

<script src="https://gist.github.com/mohashari/73c00bd53f748fbc28306fdc2f92a3ce.js?file=snippet-7.py"></script>

Building robust async Python services comes down to three disciplines: never block the event loop without an executor, always track task ownership so cancellation and cleanup are predictable, and enforce backpressure at every producer-consumer boundary. `asyncio.TaskGroup` and `asyncio.Semaphore` give you structured concurrency and concurrency limits without manual bookkeeping. Instrument your loop in staging with slow-callback detection before surprises find you in production. Async Python is genuinely powerful for I/O-heavy workloads — but only when you work with the event loop's cooperative scheduling model rather than against it.