---
layout: post
title: "Contract Testing with Pact: Preventing API Breakage Between Microservices"
date: 2026-04-13 07:00:00 +0700
tags: [testing, pact, microservices, apis, consumer-driven]
description: "Implement consumer-driven contract testing with Pact to catch breaking API changes before they reach production."
---

In a microservices architecture, the silent killer isn't the bug you can see — it's the API change you didn't know broke your downstream consumer. A team ships a refactored `GET /orders/{id}` response that renames `customer_id` to `customerId`, all tests pass, the PR merges, and three hours later an on-call engineer is paged because the payment service is throwing null pointer exceptions in production. Integration test suites that mock everything give false confidence. End-to-end test environments are expensive, flaky, and slow. Contract testing with Pact offers a third path: each service independently verifies its assumptions about the other, without needing them both running at the same time.

## What Contract Testing Actually Is

Contract testing flips the ownership model. Instead of a provider team writing integration tests to verify their API works, the *consumer* team writes tests that document exactly what they expect from the provider. These expectations get serialized into a "pact" file — a JSON document describing the interactions the consumer relies on. The provider then runs its own test suite against that pact file to verify it can honor every interaction. If it can't, the build fails before anything reaches staging.

Pact is the most widely adopted framework for this pattern. It supports Go, Java, Node.js, Python, Ruby, and more, with a centralized Pact Broker to share contracts between teams. The workflow is: consumer generates pact → pact published to broker → provider verifies pact → results published back to broker → deployment gates check both sides are compatible.

## Setting Up the Consumer Side in Go

Start with the consumer service. Here we have an `OrderService` that calls a `ProductService` to fetch product details. We want to define and test what we expect that API to look like.

<script src="https://gist.github.com/mohashari/b5f5b8f5519a82beeb578943b3067344.js?file=snippet.go"></script>

Notice the use of `matchers.Like` and `matchers.Decimal` instead of exact values. This is deliberate — the contract says "I expect a field called `price` that is a decimal number," not "I expect exactly 49.99." Exact value matching produces brittle contracts. Pact matchers let you express structural and type expectations while leaving the provider free to return realistic data.

## The Product Client Implementation

The client under test should be a thin HTTP wrapper. The contract test exercises this real code against Pact's mock server, so any serialization bugs get caught here, not in production.

<script src="https://gist.github.com/mohashari/b5f5b8f5519a82beeb578943b3067344.js?file=snippet-2.go"></script>

Running `go test ./consumer/...` will execute the pact test, spin up a mock provider on a random port, verify the client makes the right request and handles the response correctly, then write a pact file to `./pacts/order-service-product-service.json`.

## Publishing Contracts to the Pact Broker

Rather than passing JSON files around by hand, teams use a Pact Broker as the source of truth. Pactflow is the hosted SaaS option; for self-hosting, the open-source broker runs as a Docker container.

<script src="https://gist.github.com/mohashari/b5f5b8f5519a82beeb578943b3067344.js?file=snippet-3.yaml"></script>

With the broker running, publish the generated pact file using the Pact CLI:

<script src="https://gist.github.com/mohashari/b5f5b8f5519a82beeb578943b3067344.js?file=snippet-4.sh"></script>

The `--consumer-app-version` tied to a git SHA means you can always trace which version of the consumer produced a given contract. The broker's network graph UI then shows you a live map of which consumer versions are compatible with which provider versions — invaluable during a migration.

## Provider Verification

On the provider side, the `product-service` team runs verification tests that pull the latest contracts from the broker and replay each interaction against their real running service. No mocking, no test doubles — actual handler code.

<script src="https://gist.github.com/mohashari/b5f5b8f5519a82beeb578943b3067344.js?file=snippet-5.go"></script>

The `StateHandlers` map is where provider-side setup lives. Each "Given" clause in the consumer test corresponds to a state handler that seeds the database or configures mocks so the real handler can respond correctly. This is the mechanism that makes Pact tests deterministic despite being run against a live service.

## Gating Deployments with can-i-deploy

The final piece is making the Pact Broker a hard gate in your CI pipeline. The `can-i-deploy` command queries the broker to determine whether it's safe to deploy a specific version of a service given the verification results for all its consumers and providers.

<script src="https://gist.github.com/mohashari/b5f5b8f5519a82beeb578943b3067344.js?file=snippet-6.sh"></script>

This command checks not just whether the current provider version was verified, but whether it was verified against the *currently deployed* consumer versions in the target environment. You can be confident that shipping the new `product-service` won't break the `order-service` running in production today.

Contract testing isn't a replacement for all other testing — you still need unit tests and a minimal set of end-to-end smoke tests. What Pact eliminates is the category of production incident caused by uncoordinated API changes in fast-moving teams. By making contracts explicit, versioned, and automatically verified on every commit, you shift the discovery of integration failures from 2 AM pages to pre-merge CI failures. Start with your highest-traffic, most volatile service boundary, get one consumer and one provider running the full cycle through the broker, and the pattern will quickly prove its value before you roll it out across the organization.