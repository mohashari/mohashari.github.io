---
layout: post
title: "API Versioning Strategies: Breaking Changes Without Breaking Clients"
date: 2026-03-17 07:00:00 +0700
tags: [api, versioning, rest, backend, architecture]
description: "Evaluate URI, header, and media-type versioning approaches and design a deprecation lifecycle that lets you evolve APIs without disrupting existing consumers."
---

Every engineering team eventually faces the same uncomfortable moment: you need to change a response field, rename a resource, or restructure a payload — and somewhere out there, a client is depending on the exact shape you're about to break. API versioning isn't a bureaucratic formality; it's the discipline that lets your API evolve at the speed your product demands without turning every release into a negotiation with every consumer. The strategies you choose early will determine whether deprecations are painless migrations or emergency incidents at 2 AM.

## The Three Versioning Approaches

Before writing a single line of version-routing code, you need to decide *where* the version lives. The three dominant strategies each carry different tradeoffs.

**URI versioning** (`/v1/users`, `/v2/users`) is the most visible and the most cacheable. Reverse proxies and CDNs treat `/v1` and `/v2` as entirely different resources, which means you get aggressive caching for free. The downside is that URIs are supposed to identify resources, not API contracts — `/v2/users/42` and `/v1/users/42` are the same user, just described differently. It also pollutes your route table quickly.

**Header versioning** (`API-Version: 2`) keeps URLs clean and is semantically more honest. The cost is developer experience: version negotiation happens out-of-band and is invisible in browser address bars, log aggregators, and HAR files unless you make it explicit. It also defeats naive HTTP caching unless you add `Vary: API-Version` to every response.

**Media-type versioning** (Accept: `application/vnd.myapi.v2+json`) is the purest REST approach — content negotiation is precisely what `Accept` was designed for. In practice, it's the least common because most HTTP clients make it awkward to set custom media types, and many developers find it confusing to debug.

For most teams, URI versioning wins on pragmatism. Header versioning is a strong choice for internal APIs where all clients are controlled. Media-type versioning works best when your API is consumed by sophisticated HTTP clients and you care deeply about REST semantics.

## Routing by Version in Go

Here's a minimal version-routing setup using `chi` that registers both versions under the same server without duplicating middleware:

<script src="https://gist.github.com/mohashari/db38d535f5219d5e150b8f9be6561526.js?file=snippet.go"></script>

The key insight here: shared middleware (auth, logging, rate limiting) lives on the root router. Version-specific handlers are mounted as sub-routers. This avoids the common mistake of copy-pasting middleware into each version branch.

## Modeling Version Divergence in Handlers

The cleanest pattern for managing divergence is to keep a single domain model and transform at the boundary. Don't duplicate business logic — duplicate only the serialization layer:

<script src="https://gist.github.com/mohashari/db38d535f5219d5e150b8f9be6561526.js?file=snippet-2.go"></script>

This "transform at the boundary" pattern is borrowed from hexagonal architecture. Your service layer never knows which version asked the question — only the HTTP handler does, and only long enough to serialize the response.

## Deprecation Headers

Signaling deprecation in-band is far more reliable than relying on documentation or email. The IETF draft for deprecation headers gives you a standard way to embed this signal in every response from an old version:

<script src="https://gist.github.com/mohashari/db38d535f5219d5e150b8f9be6561526.js?file=snippet-3.go"></script>

Mount this middleware on your v1 router the moment you release v2. Clients using automated HTTP libraries like `got` or `axios` can be configured to log warnings on `Deprecation: true` headers — giving teams passive notice without any email coordination.

## Tracking Version Usage in SQL

Before you can sunset a version, you need to know who's still using it. A lightweight approach: log version usage to a database table and query it before any deprecation decision.

<script src="https://gist.github.com/mohashari/db38d535f5219d5e150b8f9be6561526.js?file=snippet-4.sql"></script>

Run the last query weekly as you approach a sunset date. A client appearing in results two weeks before the deadline gets a direct outreach — not a broadcast email that lands in someone's promotions folder.

## Nginx Routing for a Gradual Migration

When you're ready to start shifting traffic, a reverse proxy lets you do it at the infrastructure layer without touching application code. This NGINX config routes clients that send no version header to v2 by default while still honoring explicit v1 requests:

<script src="https://gist.github.com/mohashari/db38d535f5219d5e150b8f9be6561526.js?file=snippet-5.conf"></script>

The `always` flag on `add_header` ensures the deprecation headers appear even on error responses — important because a `4xx` might be the only response a misconfigured client ever inspects.

## The Deprecation Lifecycle as a Shell Script

Formalizing your sunset process into a runnable checklist prevents ad-hoc decisions and makes the process auditable. A simple shell script can serve as both documentation and automation:

<script src="https://gist.github.com/mohashari/db38d535f5219d5e150b8f9be6561526.js?file=snippet-6.sh"></script>

This script gates the sunset on human review of active client data, creates a git tag for post-mortem traceability, and updates the live NGINX header in one operation.

## Closing Thoughts

Versioning strategy is less about picking the "correct" scheme and more about committing to a deprecation lifecycle your team will actually follow. URI versioning buys you visibility and cache friendliness; deprecation headers turn that visibility into in-band client communication; SQL usage tracking gives you the data to make confident sunset decisions rather than hopeful ones. The pattern that fails most often isn't a bad versioning scheme — it's an absent sunset process. Clients don't migrate because they want to; they migrate because you've made it easy and inevitable. Build the deprecation machinery before you need it, and you'll never have to choose between shipping and breaking someone's production system.