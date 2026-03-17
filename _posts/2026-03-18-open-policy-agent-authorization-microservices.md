---
layout: post
title: "Open Policy Agent: Fine-Grained Authorization for Microservices"
date: 2026-03-18 07:00:00 +0700
tags: [security, opa, authorization, microservices, policy]
description: "Decouple authorization logic from application code using OPA Rego policies, sidecar injection, and bundle servers for large-scale enforcement."
---

Authorization is one of those problems that starts simple and quietly becomes a distributed systems nightmare. Early in a microservices architecture, teams embed permission checks directly in service code — a handful of `if user.role == "admin"` guards scattered across handlers. Then the org grows. New services appear. A compliance requirement mandates attribute-based access control. Suddenly you have authorization logic duplicated across twelve services, implemented slightly differently in each one, and nobody can answer the question "who can access what?" without reading source code across four repositories. Open Policy Agent (OPA) solves this by treating authorization as a dedicated concern: a policy engine you query over a network or embed in-process, with policies written in a declarative language called Rego that can be versioned, tested, and deployed independently of your application code.

## What OPA Actually Is

OPA is a general-purpose policy engine — it takes structured input (a JSON document representing a request), evaluates it against a policy (a `.rego` file), and returns a decision (also JSON). It doesn't know anything about HTTP, Kubernetes, or your database schema by default. That generality is the point. You define what "input" means in your domain, you write rules that reason over that input, and OPA tells you whether the action is allowed.

The core data model has three pieces: **input** (the incoming request context), **data** (external facts OPA can reference, loaded separately), and **policy** (Rego rules that combine the two). A typical authorization query looks like: "given this JWT's claims and this requested resource, does the policy allow the action?"

## Writing Your First Rego Policy

Rego is a logic-based language descended from Datalog. Rules evaluate to `true` or `undefined` — there's no explicit `false`. If a rule body has any statement that doesn't hold, the entire rule is undefined, which OPA treats as a denial.

Here's a policy for an API gateway that enforces role-based access on HTTP routes. The `allow` rule succeeds only when every condition in its body holds simultaneously.

<script src="https://gist.github.com/mohashari/a4da49906f8a28dc81fd28d8b4c30ba0.js?file=snippet.txt"></script>

Notice `default allow := false` — this is critical. Without it, a denied request returns `undefined`, which your application code might misinterpret. The default makes the closed-world assumption explicit.

## Embedding OPA in a Go Service

Rather than running OPA as a separate sidecar in development, you can embed it directly using the Go SDK. This is useful for lower-latency decisions and simpler local development setups.

<script src="https://gist.github.com/mohashari/a4da49906f8a28dc81fd28d8b4c30ba0.js?file=snippet-2.go"></script>

`rego.New(...).PrepareForEval(ctx)` compiles the policy once and caches it — you want to call this at startup, not per-request, since compilation is expensive.

## Running OPA as a Sidecar

In production Kubernetes environments, the sidecar pattern is common: OPA runs as a container alongside your service, reachable at `localhost:8181`. Your service makes an HTTP call to OPA's REST API instead of embedding the engine. This decouples policy updates from application deployments — you can push new policies to OPA without restarting your app.

<script src="https://gist.github.com/mohashari/a4da49906f8a28dc81fd28d8b4c30ba0.js?file=snippet-3.yaml"></script>

The `--bundle` flag tells OPA to fetch policies from a remote bundle server and poll for updates. This is the canonical way to distribute policies at scale.

## Querying OPA from Go via HTTP

When OPA runs as a sidecar, your service queries it over HTTP. The request body contains the `input` document, and OPA returns the policy decision.

<script src="https://gist.github.com/mohashari/a4da49906f8a28dc81fd28d8b4c30ba0.js?file=snippet-4.go"></script>

The URL path maps directly to the Rego package and rule: `/v1/data/httpapi/authz/allow` evaluates `data.httpapi.authz.allow`. This is a clean convention that makes it easy to reason about which policy is being queried.

## Testing Policies with `opa test`

Rego policies are testable as first-class artifacts. OPA's built-in test runner finds any rule prefixed with `test_` and evaluates it. Ship policy tests in your CI pipeline the same way you ship unit tests.

<script src="https://gist.github.com/mohashari/a4da49906f8a28dc81fd28d8b4c30ba0.js?file=snippet-5.txt"></script>

<script src="https://gist.github.com/mohashari/a4da49906f8a28dc81fd28d8b4c30ba0.js?file=snippet-6.sh"></script>

## Distributing Policies with a Bundle Server

In large organizations, you want a single source of truth for policies — a bundle server that OPA instances across all clusters poll from. A bundle is just a `.tar.gz` containing your `.rego` files and optional data JSON. Any HTTP file server works; in practice teams use S3, GCS, or a purpose-built service.

<script src="https://gist.github.com/mohashari/a4da49906f8a28dc81fd28d8b4c30ba0.js?file=snippet-7.sh"></script>

OPA's bundle protocol supports ETags and `If-None-Match` headers, so polling is cheap — OPA only downloads a new bundle when it actually changes. Combine this with signed bundles (using `opa build --signing-key`) for tamper-evident policy distribution.

## Decision Logging for Audit Trails

A critical operational requirement for authorization systems is audit logging — who was allowed or denied, when, and why. OPA has built-in decision log support. Enable it in your sidecar configuration and ship logs to your SIEM or data warehouse.

<script src="https://gist.github.com/mohashari/a4da49906f8a28dc81fd28d8b4c30ba0.js?file=snippet-8.yaml"></script>

Each logged decision includes the input, the policy result, a timestamp, and the bundle revision that was active — giving you full reproducibility for compliance investigations.

Decoupling authorization from application code with OPA pays compounding dividends as your platform scales. Service teams stop reimplementing permission logic; security teams own a single policy repository with full test coverage and audit trails; compliance audits become queries against decision logs rather than code archaeology. The initial investment — learning Rego, wiring up the sidecar pattern, standing up a bundle server — is front-loaded, but the result is an authorization layer that evolves independently of your services and can answer "who can do what" definitively, at any point in time.