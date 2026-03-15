---
layout: post
title: "Go Generics in Production: Patterns, Pitfalls, and Performance"
date: 2026-03-15 07:00:00 +0700
tags: [go, generics, backend, performance, patterns]
description: "Leverage Go generics to write reusable, type-safe code while avoiding common performance traps and over-abstraction."
---

Go 1.18 shipped generics in 2022, and the backend community responded with a mix of excitement and skepticism. Two years into production usage, the picture is clearer: generics solve real problems elegantly, but they also invite a class of mistakes that didn't exist before. If you've been writing Go for any length of time, you've felt the friction — duplicate code for `int` and `int64`, type-asserting everything out of `interface{}`, utility functions that silently accept the wrong type. Generics fix these, but they introduce new traps around instantiation cost, constraint design, and the temptation to abstract everything. This post covers the patterns that actually work in production, the pitfalls to watch for, and what the performance story really looks like.

## The Core Use Case: Type-Safe Collections and Utilities

The most immediate win from generics is eliminating the `interface{}` escape hatch in utility code. Before Go 1.18, any reusable container or algorithm required either code generation, reflection, or accepting `any` and losing type safety at the call site. A generic `Map` function over a slice is the canonical example, but the real value shows up in production code with domain types.

<script src="https://gist.github.com/mohashari/bded530176ce35b0a411bec010b82e4a.js?file=snippet.go"></script>

Notice the `make` with `len(slice)` capacity hint — this matters for performance. The generic version pre-allocates pessimistically, which is correct for most filter operations.

## Constraints Are Interfaces, and That's Powerful

Constraints in Go generics are just interfaces, which means you can compose them, share them across packages, and build a constraint library that mirrors your domain. The `comparable` built-in handles equality, but for numeric operations you need the `constraints` package or roll your own.

<script src="https://gist.github.com/mohashari/bded530176ce35b0a411bec010b82e4a.js?file=snippet-2.go"></script>

The `~int` tilde syntax is critical here — it means "any type whose underlying type is int," which covers your `type UserID int` domain types without forcing you to convert everything back to primitives.

## A Generic Repository Pattern

One of the highest-value applications in backend services is a generic repository layer. Instead of repeating CRUD boilerplate for every entity, you define it once with a constraint that captures what you need from a persistable type.

<script src="https://gist.github.com/mohashari/bded530176ce35b0a411bec010b82e4a.js?file=snippet-3.go"></script>

This pattern works well, but comes with a caveat: Go's type system cannot yet express "the concrete SQL scan target for T," which means you'll still hit friction at the `row.Scan` boundary. Pair this with a `Scanner` interface or use `sqlx`'s reflection-based scanning.

## The Pitfall: Premature Generic Abstraction

The most common mistake after generics landed was treating them as a hammer. If your function is only ever called with one concrete type, generics add complexity without benefit. The compiler has to instantiate the function for each type argument it sees, which costs compile time, and the abstraction obscures intent for readers.

<script src="https://gist.github.com/mohashari/bded530176ce35b0a411bec010b82e4a.js?file=snippet-4.go"></script>

The `RetryWithBackoff` case is genuinely useful — you want it to work with both `*http.Response` and database rows and gRPC responses without wrapping everything in `any`.

## Performance: What the Benchmarks Actually Show

Go generics use a technique called GCShape stenciling — the compiler generates one instantiation per "GC shape" rather than one per type. Pointer types share a single instantiation, while value types each get their own. This means generics over pointer types can be slightly slower than monomorphized code due to an extra indirection through a dictionary, but faster than `interface{}` dispatch for value types that avoid heap allocation.

<script src="https://gist.github.com/mohashari/bded530176ce35b0a411bec010b82e4a.js?file=snippet-5.go"></script>

The generic version is within 4% of the concrete version and 4x faster than the interface version. For hot paths, this difference is meaningful — but for most application code handling database I/O and network calls, it's noise. Profile before optimizing.

## When to Reach for Generics

The right heuristic is: reach for generics when you're about to write the same function twice with different types, when you're about to use `any` and lose type safety, or when you're building a library that others will call with types you can't anticipate. Avoid generics when the code is called from one place, when the type variation is better handled by an interface with behavior, or when you're tempted to build a generic framework for your own application internals. Go's interface system handles behavioral abstraction; generics handle structural reuse over varied types. Keep them in their lanes and both tools stay sharp. The production Go codebase that ages well is the one where generics appear in the utility layer and domain logic stays concrete — readable, greppable, and unsurprising to the next engineer who has to debug it at 2 AM.