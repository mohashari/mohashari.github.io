---
layout: post
title: "The Actor Model with Erlang/Elixir: Fault-Tolerant Concurrency by Design"
date: 2026-03-15 07:00:00 +0700
tags: [elixir, actor-model, concurrency, fault-tolerance, distributed-systems]
description: "Explore how the actor model and OTP supervision trees in Elixir enable highly concurrent, self-healing backend systems."
---

# The Actor Model with Erlang/Elixir: Fault-Tolerant Concurrency by Design

Every backend engineer eventually hits the same wall: you've carefully locked your shared state, tuned your thread pool, and added retry logic everywhere — and your system still falls over under load or, worse, silently corrupts data when two requests race each other to the database. The traditional imperative model treats concurrency as an afterthought, bolted on top of sequential code with mutexes and semaphores. Erlang, and its modern sibling Elixir, take a fundamentally different approach. They were designed from the ground up around the actor model, where concurrency is not a feature you add — it's the shape of the runtime itself.

## What Is the Actor Model?

The actor model treats computation as a collection of independent entities called **actors**. Each actor has private state that nothing outside it can read or write directly. Actors communicate exclusively by passing immutable messages. When an actor receives a message, it can: update its own internal state, send messages to other actors, or spawn new actors. There are no shared memory locations, no locks, and no condition variables. The only synchronization mechanism is the mailbox — a queue attached to each actor where incoming messages wait their turn.

Elixir's concurrency primitive is the **process** (not an OS thread — an Elixir process is a green thread managed by the BEAM virtual machine). You can spawn hundreds of thousands of them on a single machine, each isolated, each with its own heap. When a process crashes, it crashes alone. Nothing else is affected unless you explicitly link processes together.

## Spawning Your First Actor

The simplest actor in Elixir is just a recursive function that calls `receive` in a loop:

<script src="https://gist.github.com/mohashari/c74127b232d419a8ae6d6dcbdf9ef7b9.js?file=snippet.txt"></script>

Notice what's absent: no mutex, no atomic integer, no synchronized block. The counter's state lives exclusively inside `loop/1`'s stack frame. Concurrent callers never touch the same memory — they queue up in the process mailbox and are handled one at a time.

## GenServer: The Production-Grade Actor

Raw `spawn` is instructive but impractical. OTP's `GenServer` behaviour wraps the receive loop with error handling, timeouts, introspection, and a clean callback API. Here's the same counter as a proper GenServer:

<script src="https://gist.github.com/mohashari/c74127b232d419a8ae6d6dcbdf9ef7b9.js?file=snippet-2.txt"></script>

`GenServer.call/2` is synchronous — it sends a message and blocks the caller until the server replies. `GenServer.cast/2` is fire-and-forget. This distinction matters for backpressure: if you always use casts and the server falls behind, the mailbox grows unbounded. Calls give you natural pushback because callers block until the server catches up.

## Supervision Trees: The "Let It Crash" Philosophy

The real superpower of OTP is **supervisors** — actors whose only job is to watch other actors and restart them when they crash. Instead of writing defensive code that tries to recover from every possible error, you let the process die cleanly and rely on the supervisor to bring it back with a fresh state. This is the "let it crash" philosophy.

<script src="https://gist.github.com/mohashari/c74127b232d419a8ae6d6dcbdf9ef7b9.js?file=snippet-3.txt"></script>

The `:one_for_one` strategy restarts only the crashed child. `:one_for_all` restarts every sibling when any one dies — useful when processes are tightly coupled. `:rest_for_one` restarts the crashed process and every process started after it, which models pipeline dependencies elegantly. Supervision trees are nested: a crashing subtree can be restarted without touching unrelated parts of the system.

## Fault Isolation with Linked Processes

When you need two processes to live or die together, you link them. If either crashes, the exit signal propagates to the other. Monitors are one-directional: process A watches B, but B doesn't watch A. This is how GenServer implements supervised workers under the hood.

<script src="https://gist.github.com/mohashari/c74127b232d419a8ae6d6dcbdf9ef7b9.js?file=snippet-4.txt"></script>

## Building a Rate-Limited Job Queue

Here's a more realistic pattern — a bounded job queue that limits concurrency using a pool of worker processes. This uses `Task.async_stream/3`, which spawns at most `max_concurrency` tasks simultaneously:

<script src="https://gist.github.com/mohashari/c74127b232d419a8ae6d6dcbdf9ef7b9.js?file=snippet-5.txt"></script>

Each job runs in its own isolated process. A job that hangs past `timeout` is killed without affecting its siblings — and because processes share nothing, there's no corrupted state left behind.

## Distributing Across Nodes

BEAM clusters are first-class citizens. Once you connect nodes, you can send messages to remote processes using the same `send/2` API — the location of the process is transparent:

<script src="https://gist.github.com/mohashari/c74127b232d419a8ae6d6dcbdf9ef7b9.js?file=snippet-6.txt"></script>

This is how tools like Phoenix PubSub, Horde (distributed supervisors), and Mnesia (built-in distributed database) work under the hood — they're all just message passing over the BEAM distribution protocol.

## Running in Production with Releases

A production Elixir system is packaged as a self-contained **release** — a directory with the BEAM runtime, your compiled bytecode, and a boot script. No Elixir installation required on the target machine.

<script src="https://gist.github.com/mohashari/c74127b232d419a8ae6d6dcbdf9ef7b9.js?file=snippet-7.dockerfile"></script>

The release includes the entire BEAM VM, so your image is fully self-contained. You get hot code reloading in production too — BEAM can swap out a module's bytecode while the system is running, without restarting processes or dropping connections.

---

The actor model is not merely a different syntax for threads — it is a different mental model for what a running program is. Instead of a single execution path that occasionally branches, you have a city of small, independent agents that collaborate through messages. When one agent fails, the others continue; supervisors notice and respawn the dead agent with clean state. This design makes failure a first-class concept rather than an exceptional case, and it scales horizontally across cores and machines with the same primitives you use locally. If you're building systems where uptime and concurrency genuinely matter — real-time APIs, message brokers, game servers, financial pipelines — spending a week with OTP will permanently change how you think about fault tolerance.