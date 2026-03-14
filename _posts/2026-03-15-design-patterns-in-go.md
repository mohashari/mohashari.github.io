---
layout: post
title: "Design Patterns Every Backend Engineer Should Know (in Go)"
date: 2026-03-15 07:00:00 +0700
tags: [go, design-patterns, backend, software-engineering]
description: "Practical implementation of the most impactful design patterns — Singleton, Factory, Observer, Strategy, and more — with real Go code."
---

Design patterns are reusable solutions to common software problems. They're not copy-paste templates — they're blueprints for structuring your thinking. Here are the most useful ones for backend engineers, implemented in Go.

## 1. Singleton — One Instance to Rule Them All

Use when you need exactly one shared resource: database pool, config loader, logger.

<script src="https://gist.github.com/mohashari/09ab6d6ad0a1a67062b5efe1c34f0082.js?file=snippet.go"></script>

`sync.Once` guarantees thread-safe initialization — far cleaner than a double-checked lock.

## 2. Factory — Decouple Creation from Use

<script src="https://gist.github.com/mohashari/09ab6d6ad0a1a67062b5efe1c34f0082.js?file=snippet-2.go"></script>

Callers only know about `Notifier` — you can add new channels without touching existing code.

## 3. Strategy — Swap Algorithms at Runtime

<script src="https://gist.github.com/mohashari/09ab6d6ad0a1a67062b5efe1c34f0082.js?file=snippet-3.go"></script>

Use this when the algorithm selection depends on runtime conditions (e.g., small dataset → bubble sort, large dataset → quicksort).

## 4. Observer — Event-Driven Decoupling

<script src="https://gist.github.com/mohashari/09ab6d6ad0a1a67062b5efe1c34f0082.js?file=snippet-4.go"></script>

This is the foundation for event-driven microservices — each service subscribes to what it cares about.

## 5. Repository — Abstract Your Data Layer

<script src="https://gist.github.com/mohashari/09ab6d6ad0a1a67062b5efe1c34f0082.js?file=snippet-5.go"></script>

The interface lets you swap PostgreSQL for SQLite in tests — no mock frameworks needed.

## 6. Middleware Chain — Composable HTTP Handlers

<script src="https://gist.github.com/mohashari/09ab6d6ad0a1a67062b5efe1c34f0082.js?file=snippet-6.go"></script>

## When to Use Which Pattern

| Pattern | Use When |
|---------|----------|
| Singleton | Shared stateful resource (DB pool, config) |
| Factory | Multiple implementations of one interface |
| Strategy | Algorithm varies by condition/config |
| Observer | Decoupled event propagation |
| Repository | Abstract data access from business logic |
| Middleware | Cross-cutting concerns (auth, logging, tracing) |

Patterns are tools, not rules. Apply them when they reduce complexity — avoid them when they add unnecessary indirection to simple problems.
