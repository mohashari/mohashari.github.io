---
layout: post
title: "Python Typing in Production: Mypy, Pydantic, and Runtime Validation"
date: 2026-03-16 07:00:00 +0700
tags: [python, typing, backend, pydantic, tooling]
description: "Build safer Python services with strict mypy configurations, Pydantic v2 models, and runtime validation strategies that catch bugs before they reach production."
---

Python services have a reputation for being fast to write and slow to maintain. The dynamism that makes Python expressive in a prototype becomes a liability at scale: a mistyped dictionary key, a `None` sneaking through where a string was expected, a third-party API returning an unexpected shape — these bugs land in production logs at 2am rather than in CI. The good news is that Python's type system has matured considerably, and combining static analysis with runtime validation gives you the safety of a typed language without abandoning Python's ergonomics. This post walks through a production-grade approach using strict mypy, Pydantic v2, and thoughtful validation boundaries.

## Configuring Mypy for Real Strictness

The default mypy configuration is too permissive to catch the bugs that actually hurt you. Most teams add mypy to their project, run it once, get a handful of warnings, and then ignore it. The issue is that without strict flags, mypy silently ignores untyped functions and third-party libraries with missing stubs, giving you false confidence.

Start with a `pyproject.toml` section that enables the flags that matter. The key ones are `disallow_untyped_defs`, which requires every function to have type annotations, and `strict_optional`, which treats `Optional[X]` as genuinely distinct from `X`. Without `strict_optional`, `None` can slip through any typed variable silently.

<script src="https://gist.github.com/mohashari/90b2502c64f0ecd0ad3df020d82d6449.js?file=snippet.toml"></script>

The `overrides` block handles the reality that many popular libraries still lack complete stub packages. Rather than disabling mypy globally, you suppress errors for specific third-party modules while keeping everything you own under strict scrutiny.

## Typing Your Service Boundaries First

The highest-value place to apply types is at the edges of your system: HTTP handlers, message consumers, database layer. Internal business logic benefits from types too, but boundary code is where untrusted data enters and where the most subtle bugs live. Typed boundaries act as documentation for anyone reading the code months later.

Here is a pattern for a FastAPI endpoint where the request shape, response shape, and error cases are all expressed in types before any logic runs:

<script src="https://gist.github.com/mohashari/90b2502c64f0ecd0ad3df020d82d6449.js?file=snippet-2.py"></script>

FastAPI uses Pydantic under the hood, so validation runs automatically before your handler is called. A malformed request never reaches `user_service.create`.

## Pydantic v2 Models for Internal Data

Pydantic v2 rewrote its core in Rust, making validation significantly faster — fast enough to use inside hot paths where v1 would have been a performance concern. Beyond speed, v2's `model_validator` and `field_validator` decorators have cleaner semantics and the new `model_config` approach replaces the nested `Config` class.

<script src="https://gist.github.com/mohashari/90b2502c64f0ecd0ad3df020d82d6449.js?file=snippet-3.py"></script>

`frozen=True` makes instances immutable after construction, which eliminates an entire class of bugs where code mutates an object it should only be reading. It also makes models hashable, useful when you need to store them in sets or as dict keys.

## Parsing External Data Safely

Third-party APIs and internal microservices return data you do not control. Blindly trusting their schema means your service breaks when they add a new field, change a type, or return `null` unexpectedly. The pattern here is to parse into a model immediately and never pass raw dicts deeper into your code.

<script src="https://gist.github.com/mohashari/90b2502c64f0ecd0ad3df020d82d6449.js?file=snippet-4.py"></script>

Returning `None` on validation failure forces callers to handle the error path. If you raised the exception instead, callers could catch it incorrectly. The `exc.errors()` output is structured JSON, which makes it easy to log and query later.

## Runtime Narrowing with TypeGuard

Mypy cannot always infer that a variable has been narrowed to a specific type after a conditional check. `TypeGuard` lets you write custom narrowing functions that teach mypy what a successful check implies, removing the need for `cast()` scattered throughout the codebase.

<script src="https://gist.github.com/mohashari/90b2502c64f0ecd0ad3df020d82d6449.js?file=snippet-5.py"></script>

Without `TypeGuard`, mypy would still flag `sub.plan_id` as potentially missing because `InactiveSubscription` does not have that field, even though the `if` check makes it safe.

## Enforcing Types in CI

Types are only useful if they stay correct over time. Add mypy to your CI pipeline and fail the build on errors. A pre-commit hook catches issues locally before they reach the pipeline.

<script src="https://gist.github.com/mohashari/90b2502c64f0ecd0ad3df020d82d6449.js?file=snippet-6.yaml"></script>

<script src="https://gist.github.com/mohashari/90b2502c64f0ecd0ad3df020d82d6449.js?file=snippet-7.sh"></script>

SQLAlchemy ships a mypy plugin that understands mapped columns and relationships, so `Column(String)` resolves to `str` rather than `Any`. Adding `sqlalchemy[mypy]` to the hook dependencies and enabling the plugin in `pyproject.toml` brings your ORM layer under the same type coverage as everything else.

## Gradual Adoption in an Existing Codebase

If you are adding types to an existing service, strict mode everywhere at once is overwhelming. Mypy supports a per-module override so you can annotate one package at a time and track progress.

<script src="https://gist.github.com/mohashari/90b2502c64f0ecd0ad3df020d82d6449.js?file=snippet-8.toml"></script>

As you annotate each module, move it from `ignore_errors = true` into the strict block. The process takes weeks in a large codebase, but every module you migrate is a module that stops producing mysterious `AttributeError` crashes.

Static types and runtime validation are complementary, not competing strategies. Mypy catches type errors at development time without running your code; Pydantic catches schema violations at runtime when external data enters your system. Together they close the gap that neither approach covers alone. The practical outcome is fewer production incidents, faster onboarding for new engineers who can read the types instead of tracing execution, and refactors that surface every callsite that needs updating rather than hiding them until deployment. Start with your service boundaries, configure mypy strictly for new code, and expand coverage incrementally — the safety improvements compound as coverage grows.