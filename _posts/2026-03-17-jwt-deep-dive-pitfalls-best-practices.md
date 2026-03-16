---
layout: post
title: "JWT Deep Dive: Pitfalls, Best Practices, and Secure Token Lifecycles"
date: 2026-03-17 07:00:00 +0700
tags: [jwt, security, authentication, backend, api]
description: "Examine JWT vulnerabilities like algorithm confusion and weak secrets, then implement a secure token lifecycle with rotation, revocation, and short expiry patterns."
---

JSON Web Tokens have become the default credential format for stateless APIs, yet they are routinely misconfigured in ways that hand attackers persistent, unforgeable access. The problem is not the specification itself — RFC 7519 is clear — it's that JWT libraries are designed for flexibility, and flexibility without discipline creates exploitable surface area. Algorithm confusion, weak signing secrets, missing claim validation, and absent revocation mechanisms are found in production systems at companies of every size. This post tears through the most dangerous pitfalls and shows how to build a token lifecycle that is actually secure: short-lived access tokens, rotating refresh tokens, and explicit revocation paths backed by a fast store.

## The Algorithm Confusion Attack

The most notorious JWT vulnerability is algorithm confusion — specifically, accepting `alg: none` or switching from RS256 to HS256 using the public key as the HMAC secret. Libraries that trust the `alg` header field without restriction allow an attacker who knows your public key (which is often public) to forge valid tokens by treating it as a symmetric secret.

The fix is explicit algorithm pinning. Never let the incoming token declare which algorithm was used for verification. Your verifier must enforce a specific algorithm regardless of what the header claims.

<script src="https://gist.github.com/mohashari/3e4d57eff5f0f04f61c5388b05782f48.js?file=snippet.go"></script>

## Generating Tokens with Minimal Scope and Short Expiry

Access tokens should live for minutes, not hours. Fifteen minutes is a reasonable ceiling for most APIs. Embedding a `jti` (JWT ID) enables targeted revocation if you need it, and keeping the payload small reduces the cost of parsing on every request.

<script src="https://gist.github.com/mohashari/3e4d57eff5f0f04f61c5388b05782f48.js?file=snippet-2.go"></script>

## Refresh Token Rotation with a PostgreSQL Store

A refresh token must be a high-entropy opaque value — not a JWT — stored server-side so it can be invalidated. Rotation means each use consumes the token and issues a new one atomically. Detecting reuse of an already-rotated token is a signal of theft and should trigger family invalidation: revoke all refresh tokens belonging to that user session lineage.

<script src="https://gist.github.com/mohashari/3e4d57eff5f0f04f61c5388b05782f48.js?file=snippet-3.sql"></script>

The rotation logic enforces the single-use invariant and implements reuse detection:

<script src="https://gist.github.com/mohashari/3e4d57eff5f0f04f61c5388b05782f48.js?file=snippet-4.go"></script>

## Revocation with a Redis Blocklist

Because access tokens are verified without a database round-trip, revocation requires an out-of-band signal. A Redis set keyed by `jti` with a TTL matching the token's remaining lifetime is the standard approach. The overhead is a single `GET` per request — fast enough for any access pattern.

<script src="https://gist.github.com/mohashari/3e4d57eff5f0f04f61c5388b05782f48.js?file=snippet-5.go"></script>

## Key Rotation with JWKS

RSA private keys should be rotated on a schedule. Publishing a JWKS (JSON Web Key Set) endpoint lets consumers discover keys by `kid` without redeployment. Keep both the current and previous key in the set during the rollover window so tokens issued before rotation remain valid through their natural expiry.

<script src="https://gist.github.com/mohashari/3e4d57eff5f0f04f61c5388b05782f48.js?file=snippet-6.go"></script>

The corresponding nginx configuration adds cache headers and rate-limiting to protect the endpoint from enumeration:

<script src="https://gist.github.com/mohashari/3e4d57eff5f0f04f61c5388b05782f48.js?file=snippet-7.conf"></script>

## Putting It Together: The Secure Token Lifecycle

Every endpoint that accepts a JWT must enforce the full validation chain: algorithm pinning, signature verification, issuer and audience checks, expiry, not-before, and blocklist lookup. Skipping any step creates a bypass. Wire these into middleware so the discipline is automatic:

<script src="https://gist.github.com/mohashari/3e4d57eff5f0f04f61c5388b05782f48.js?file=snippet-8.go"></script>

JWT security is not complicated, but it requires discipline on every axis simultaneously. Use RS256 or ES256, never HS256 with a weak secret and never `alg: none`. Issue access tokens with 15-minute expiry and a `jti` you can revoke. Store refresh tokens as opaque, hashed values and rotate them on every use with family invalidation on reuse. Publish keys via JWKS and rotate them quarterly. Validate every claim — issuer, audience, expiry, not-before — in middleware so no handler can opt out. Treat any validation failure as a hard deny rather than a logged warning. These are not theoretical hardening measures; each corresponds to a class of real-world token forgery or session persistence attack that has been exploited in production. Implement all of them, not just the ones that seem obviously necessary.