---
layout: post
title: "API Versioning Strategies: Evolving APIs Without Breaking Clients"
date: 2026-03-15 07:00:00 +0700
tags: [api, rest, backend, architecture, versioning]
description: "Compare URI, header, and query-param versioning strategies and learn how to deprecate API versions without breaking existing consumers."
---

Every backend engineer eventually faces the same uncomfortable moment: a breaking change is needed in an API that dozens of clients depend on. Maybe a field needs to be renamed, a resource restructured, or an entire endpoint redesigned. The instinct is to just make the change and notify consumers — but in practice, clients update on their own timeline, mobile apps sit in app stores for months, and third-party integrations go unmaintained for years. API versioning is the discipline of evolving your service without pulling the rug out from under anyone. Done well, it buys you the freedom to iterate; done poorly, it becomes a graveyard of half-dead version branches nobody dares to delete.

## Why Versioning Decisions Are Architectural Decisions

The versioning strategy you choose becomes deeply embedded in your infrastructure, routing logic, documentation tooling, and developer experience. Switching strategies mid-product is painful, so it is worth understanding the trade-offs before the first endpoint ships.

The three dominant strategies are **URI path versioning** (`/v1/users`), **request header versioning** (`API-Version: 2024-01`), and **query parameter versioning** (`/users?version=2`). Each has a different impact on caching, discoverability, and client complexity.

## URI Path Versioning

URI versioning is the most widely adopted approach, used by Stripe, GitHub, and Twilio. The version is embedded in the path, making it explicit, cacheable, and easy to route at the load balancer or API gateway level.

This Go snippet shows how to register versioned route groups using the `chi` router. Each version gets its own handler set, allowing v1 and v2 to diverge completely without shared mutable state:

<script src="https://gist.github.com/mohashari/8ae8889dd9104dfc072dcc3f9a365a97.js?file=snippet.go"></script>

The downside of URI versioning is that it violates REST's principle that a URI should identify a resource, not a representation. `/v1/users/42` and `/v2/users/42` are technically the same resource. This is mostly a philosophical objection — the practical benefits usually outweigh it.

## Header-Based Versioning

Header versioning keeps URIs clean and is preferred by teams with strong REST convictions. The client signals the desired version through a custom request header. Stripe's date-based versioning (`Stripe-Version: 2023-10-16`) is a mature example of this pattern.

Here is a Go middleware that reads the version header and injects it into the request context for downstream handlers to use:

<script src="https://gist.github.com/mohashari/8ae8889dd9104dfc072dcc3f9a365a97.js?file=snippet-2.go"></script>

The challenge with header versioning is that it is invisible to browser address bars and HTTP caches by default. You must set the `Vary: API-Version` response header to prevent caches from serving the wrong version to different clients.

## Signalling Deprecation with Response Headers

Deprecation is as important as versioning itself. Silently removing a version is how you break clients. The IETF draft for HTTP deprecation headers (`Deprecation` and `Sunset`) gives clients a machine-readable signal that they need to migrate.

Every response from a deprecated version should include these headers so monitoring tools and API clients can surface warnings automatically:

<script src="https://gist.github.com/mohashari/8ae8889dd9104dfc072dcc3f9a365a97.js?file=snippet-3.go"></script>

## Routing at the Gateway Level

For teams running multiple microservices, version routing belongs at the API gateway rather than inside individual services. This NGINX configuration routes traffic by URI prefix and strips the version segment before forwarding, so upstream services never need to know which version a client requested:

<script src="https://gist.github.com/mohashari/8ae8889dd9104dfc072dcc3f9a365a97.js?file=snippet-4.conf"></script>

This setup allows you to retire v1 by simply pointing its upstream to a maintenance service that returns `410 Gone` with a migration guide in the response body.

## Schema Compatibility with Database Migrations

Versioning the API surface is only half the problem. The underlying data layer often has to support both old and new shapes simultaneously during a migration window. The expand-and-contract pattern (also called parallel change) is the safe way to do this.

This SQL migration adds a new column without removing the old one. v1 clients continue reading `full_name`; v2 clients use the split `first_name` / `last_name` fields. The old column is only dropped after v1 traffic reaches zero:

<script src="https://gist.github.com/mohashari/8ae8889dd9104dfc072dcc3f9a365a97.js?file=snippet-5.sql"></script>

## Tracking Adoption Before Sunsetting

You cannot safely sunset a version without knowing who is still calling it. Structured logs tied to the version header make it easy to run adoption queries against your log aggregator.

This shell pipeline uses `jq` to parse structured JSON logs and report the request distribution across versions over the last 24 hours, which can feed a dashboard or a pre-sunset checklist:

<script src="https://gist.github.com/mohashari/8ae8889dd9104dfc072dcc3f9a365a97.js?file=snippet-6.sh"></script>

Once v1 drops below a threshold you define (often fewer than 0.1% of total traffic, or a specific known-internal caller), you can confidently proceed with the Sunset date.

## Putting It Together

The most important versioning insight is that a strategy is not just a URL scheme — it is a contract-management system. Choose URI versioning when you want operationally simple routing and strong discoverability. Choose header versioning when clean URIs and strict REST semantics matter to your team. Whichever you pick, invest equally in the deprecation lifecycle: announce early, use `Deprecation` and `Sunset` headers so tooling can surface warnings automatically, instrument per-version traffic, and enforce the expand-and-contract pattern at the database layer. Versioning done right is not a constraint on iteration — it is the mechanism that makes fast, confident iteration possible without leaving your consumers behind.