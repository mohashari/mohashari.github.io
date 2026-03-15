---
layout: post
title: "Graceful Shutdown in Go: Draining Connections and Handling OS Signals"
date: 2026-03-16 07:00:00 +0700
tags: [go, reliability, kubernetes, backend, devops]
description: "Implement clean shutdown sequences in Go services that honor in-flight requests, close database pools, and cooperate with Kubernetes pod lifecycle hooks."
---

Every backend engineer has seen it: a deployment rolls out, Kubernetes sends `SIGTERM`, and somewhere in the logs you find a cascade of `connection reset by peer` errors, half-written database rows, and 502s that the load balancer politely blamed on your service. The process died mid-request. Users got errors. Your on-call rotation got a ping. The root cause is almost always the same — the application treated shutdown as a hard stop rather than a coordinated wind-down. Go's standard library gives you everything you need to do this correctly, but wiring it together properly requires understanding the full lifecycle: OS signals, in-flight HTTP requests, database connection pools, background workers, and the pod lifecycle hooks that Kubernetes uses to orchestrate the whole sequence. This post walks through each layer, building toward a production-ready shutdown sequence you can drop into any Go service.

## Understanding the Signal Lifecycle

When Kubernetes wants to terminate a pod, it first removes the pod from the endpoints list (stopping new traffic), then sends `SIGTERM` to PID 1 inside the container. You have until `terminationGracePeriodSeconds` (default 30 seconds) before it escalates to `SIGKILL`. Your job is to detect `SIGTERM`, stop accepting new work, and finish existing work — all within that window.

Go's `os/signal` package lets you intercept signals and route them into a channel. The pattern below is the standard idiom: buffer the channel by at least 1 so the signal is never dropped if your goroutine isn't ready to receive immediately.

<script src="https://gist.github.com/mohashari/a980f6edb7fb1bbf2fbe778cbb342413.js?file=snippet.go"></script>

## Draining the HTTP Server

`http.Server` has a `Shutdown` method that stops accepting new connections and waits for active requests to complete. It does not interrupt long-polling or WebSocket connections, so you must pair it with a context deadline to avoid waiting forever. A 20-second deadline leaves comfortable margin inside a 30-second grace period.

<script src="https://gist.github.com/mohashari/a980f6edb7fb1bbf2fbe778cbb342413.js?file=snippet-2.go"></script>

## Closing the Database Pool

An HTTP server draining without closing its database connections leaves orphaned connections in the pool and can trigger max-connection errors in PostgreSQL or MySQL before the OS reclaims the sockets. `sql.DB.Close()` blocks until all borrowed connections are returned, which means you must close it *after* the HTTP server finishes draining — not before. Order matters.

<script src="https://gist.github.com/mohashari/a980f6edb7fb1bbf2fbe778cbb342413.js?file=snippet-3.go"></script>

## Coordinating Background Workers with WaitGroups

Most services have goroutines doing work outside the HTTP layer: queue consumers, scheduled jobs, cache warmers. A `sync.WaitGroup` lets the shutdown sequence wait for these workers to finish their current unit of work. The pattern is to pass a shared context derived from the shutdown signal; workers check `ctx.Done()` between iterations rather than after every operation.

<script src="https://gist.github.com/mohashari/a980f6edb7fb1bbf2fbe778cbb342413.js?file=snippet-4.go"></script>

At shutdown, cancel the context and then call `wg.Wait()` before exiting:

<script src="https://gist.github.com/mohashari/a980f6edb7fb1bbf2fbe778cbb342413.js?file=snippet-5.go"></script>

## Kubernetes Pod Lifecycle Configuration

Even with a perfect shutdown implementation in Go, Kubernetes can race you. There is a known window between when a pod receives `SIGTERM` and when the endpoint controller finishes propagating the removal to kube-proxy and all Envoy sidecars. During that window, new requests can still arrive at your dying pod. A `preStop` hook with a short sleep is the idiomatic mitigation — it delays `SIGTERM` just long enough for the routing tables to settle.

<script src="https://gist.github.com/mohashari/a980f6edb7fb1bbf2fbe778cbb342413.js?file=snippet-6.yaml"></script>

The `terminationGracePeriodSeconds` must be longer than `preStop sleep` + your application's internal drain timeout combined, or Kubernetes will SIGKILL you before your code finishes.

## Dockerfile: Running as PID 1

All of this falls apart if your Go binary is not PID 1 inside the container. When a shell (`/bin/sh -c`) is PID 1, signals sent to the container go to the shell, which does not forward them to child processes by default. Use the `exec` form of `ENTRYPOINT` — it replaces the shell with your binary directly.

<script src="https://gist.github.com/mohashari/a980f6edb7fb1bbf2fbe778cbb342413.js?file=snippet-7.dockerfile"></script>

The distinction between `ENTRYPOINT ["/api"]` (exec form, PID 1) and `ENTRYPOINT "/api"` (shell form, PID 1 is `/bin/sh`) is one of the most frequently misunderstood Dockerfile subtleties, and it silently breaks graceful shutdown in production more often than most teams realize.

## Putting It Together

Graceful shutdown is not a single feature — it is a layered protocol between your binary, the OS, and the orchestrator. The sequence is: receive `SIGTERM`, stop accepting new HTTP connections, drain in-flight requests, signal background workers to finish their current unit, wait for workers to exit, then close downstream resources like the database pool in dependency order. On the infrastructure side, a `preStop` sleep absorbs the endpoint propagation race, and a properly set `terminationGracePeriodSeconds` gives the whole sequence enough time to complete. The Dockerfile `ENTRYPOINT` exec form ensures signals actually reach your process. None of these pieces alone is sufficient — production reliability requires all of them, applied in the right order.