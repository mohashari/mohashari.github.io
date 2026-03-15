---
layout: post
title: "Dependency Injection in Go: Patterns, Wire, and Testing Without Mocks"
date: 2026-03-16 07:00:00 +0700
tags: [go, architecture, testing, backend, patterns]
description: "Structure Go applications with explicit dependency injection—using manual wiring and Google Wire—to improve testability, modularity, and compile-time safety."
---

Most Go applications start simple: a main function, a database connection, a handler. Then the codebase grows. Handlers need services, services need repositories, repositories need database pools, and suddenly `main.go` is a 300-line initialization maze where everything is wired together with global variables and `init()` functions. Tests become nightmares because you can't swap a real database for a test double without reaching into package-level state. This is the problem dependency injection solves—not by adding magic or a heavyweight framework, but by making dependencies explicit, visible, and replaceable at every layer of your application.

## What Dependency Injection Actually Means in Go

Dependency injection (DI) in Go is simply the practice of passing dependencies into a struct rather than constructing them inside it. There's no interface annotation, no container registration, no reflection—just constructor functions that accept interfaces and return concrete types.

The pattern hinges on one Go feature: interfaces are satisfied implicitly. Any type that implements the required methods satisfies an interface, without explicitly declaring it. This makes it trivial to pass a real PostgreSQL repository in production and a fast, in-memory test double in tests—without changing a single line of business logic.

Here's the foundational shape. A `UserService` depends on a `UserRepository`. Instead of creating the repository inside the service, we accept it as a parameter:

<script src="https://gist.github.com/mohashari/bdb26d9c8f30381cd6471dc4b4762e6e.js?file=snippet.go"></script>

## Wiring the Service Layer

With the repository defined as an interface, the service layer becomes clean and focused on business logic. It has no knowledge of PostgreSQL, connection strings, or SQL syntax:

<script src="https://gist.github.com/mohashari/bdb26d9c8f30381cd6471dc4b4762e6e.js?file=snippet-2.go"></script>

Every dependency is visible in the constructor signature. Reading `NewUserService` tells you everything the service needs to function. There's no hidden global state, no `sync.Once` initialization, and no surprises.

## Manual Wiring in main.go

For smaller applications, manual wiring is entirely sufficient and has zero runtime overhead. The composition root—the single place where the entire object graph is assembled—lives in `main.go`:

<script src="https://gist.github.com/mohashari/bdb26d9c8f30381cd6471dc4b4762e6e.js?file=snippet-3.go"></script>

The dependency graph flows top-to-bottom. If you add a new dependency to `UserService`, the compiler immediately tells you `main.go` needs updating. This is the compile-time safety that makes explicit DI so valuable—misconfigured dependency graphs become build errors, not runtime panics at 3am.

## Testing Without Mocks Using Fakes

The real payoff of explicit injection is in testing. Instead of mocking frameworks that rely on reflection and `interface{}` gymnastics, you write simple fake implementations. A fake is a lightweight, in-memory struct that satisfies the same interface:

<script src="https://gist.github.com/mohashari/bdb26d9c8f30381cd6471dc4b4762e6e.js?file=snippet-4.go"></script>

Tests now read as straightforward business-logic verification, with no mock setup ceremony:

<script src="https://gist.github.com/mohashari/bdb26d9c8f30381cd6471dc4b4762e6e.js?file=snippet-5.go"></script>

No `mock.On(...)`, no `AssertExpectations`, no implicit call verification. Just real function calls through a real (albeit fake) implementation.

## Scaling Up with Google Wire

When applications grow to dozens of services, manual wiring becomes tedious and error-prone. Google Wire is a compile-time code generator that reads your constructor signatures and generates the wiring code for you. You define providers—your constructors—and Wire figures out the graph.

A Wire provider set describes which constructors are available:

<script src="https://gist.github.com/mohashari/bdb26d9c8f30381cd6471dc4b4762e6e.js?file=snippet-6.go"></script>

Running `wire gen ./...` produces a `wire_gen.go` file that contains the exact same manual wiring you'd write yourself. The generated output is readable, compilable Go—not magic at runtime. If you add a dependency to `NewUserService` and forget to add its provider, Wire fails during code generation with a clear error message before the code ever compiles.

## Structuring for Testability at Scale

For larger codebases, organizing packages around dependency direction pays dividends. The `internal/domain` package defines interfaces. Infrastructure packages like `internal/postgres` and `internal/smtp` implement them. The application package wires everything together:

<script src="https://gist.github.com/mohashari/bdb26d9c8f30381cd6471dc4b4762e6e.js?file=snippet-7.txt"></script>

This layout makes the dependency direction a physical property of the filesystem. Nothing in `service/` can accidentally import `postgres/` because the import would violate the architectural direction—and `go vet` or a linter like `depguard` can enforce it automatically.

Dependency injection in Go doesn't require a framework to be effective. The pattern is just explicit constructors, interface-typed parameters, and a single composition root that assembles the graph. Manual wiring is appropriate for most services; Wire earns its place when the graph becomes large enough that human maintenance of `main.go` becomes a source of bugs. Either way, the result is the same: every component in your system is independently testable, every dependency is visible at a glance, and refactoring a single layer never silently breaks another. That's not magic—it's just good engineering made explicit.