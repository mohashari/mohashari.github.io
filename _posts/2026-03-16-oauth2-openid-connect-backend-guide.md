---
layout: post
title: "OAuth 2.0 and OpenID Connect: A Backend Engineer's Complete Guide"
date: 2026-03-16 07:00:00 +0700
tags: [security, oauth, authentication, apis, backend]
description: "Understand the flows, token types, and implementation pitfalls of OAuth 2.0 and OIDC so you can build secure, standards-compliant authorization into your services."
---

`★ Insight ─────────────────────────────────────`
OAuth 2.0 and OIDC are layered protocols — OAuth 2.0 handles *authorization* (what can you do?), while OIDC adds an *identity layer* on top (who are you?). Understanding this separation is the key to avoiding the most common implementation mistakes.
`─────────────────────────────────────────────────`

Every week, a team somewhere ships a broken authentication system — not because the engineers weren't smart, but because OAuth 2.0 is deceptively approachable on the surface and brutally punishing when misunderstood beneath it. You read the "sign in with Google" tutorial, copied the flow, got tokens back, and called it done. But what *kind* of token? Valid for how long? Scoped to what? Verifiable by whom? OpenID Connect (OIDC) and OAuth 2.0 together form the backbone of modern identity infrastructure, and backend engineers who understand them at the protocol level — not just the library level — ship dramatically more secure systems. This post is a working guide: real flows, real pitfalls, and real Go code you can adapt today.

## The Two Things OAuth 2.0 Is Not

OAuth 2.0 is **not** an authentication protocol. It is an *authorization delegation* framework. When a user clicks "Log in with GitHub," GitHub doesn't tell your app who the user is — it tells your app that a user has *authorized* your app to access certain GitHub resources on their behalf. The identity layer is OIDC's job, built on top of OAuth 2.0 by adding the ID token and a standardized `/userinfo` endpoint.

OAuth 2.0 is also **not** a single flow. It defines four grant types: Authorization Code, Implicit (now deprecated), Client Credentials, and Resource Owner Password Credentials (also effectively deprecated). For user-facing apps, you want Authorization Code with PKCE. For machine-to-machine, you want Client Credentials. Choosing wrong here is the most common architectural mistake.

## Authorization Code Flow with PKCE

The Authorization Code flow keeps your client secret off the wire. The user's browser redirects to the authorization server, which returns a short-lived `code`. Your backend exchanges that code for tokens. PKCE (Proof Key for Code Exchange) extends this by having the client generate a random `code_verifier`, hash it to a `code_challenge`, and send the challenge upfront — so even if the code is intercepted, it's useless without the verifier.

Here's how to generate a PKCE pair and build the authorization URL in Go:

<script src="https://gist.github.com/mohashari/8cb01e3c08a884021db2c867c867e7f5.js?file=snippet.go"></script>

The `state` parameter doubles as CSRF protection — generate it randomly, store it in the user's session, and reject any callback where it doesn't match what you stored.

## Exchanging the Code for Tokens

Once the authorization server redirects back to your callback with a `code`, you exchange it for an access token and, if you requested the `openid` scope, an ID token. This exchange happens server-to-server, never in the browser.

<script src="https://gist.github.com/mohashari/8cb01e3c08a884021db2c867c867e7f5.js?file=snippet-2.go"></script>

Notice that `client_secret` never touches the browser. If you're building a single-page app with no backend, use PKCE alone and treat the access token as opaque — never embed a client secret in frontend code.

## Validating the ID Token (JWT)

The ID token is a JWT signed by the authorization server. You must validate it — don't just decode and trust the claims. Critically, you must verify the signature against the server's public keys (fetched from its JWKS endpoint), check that `iss` matches your authorization server, `aud` matches your client ID, and `exp` is in the future.

<script src="https://gist.github.com/mohashari/8cb01e3c08a884021db2c867c867e7f5.js?file=snippet-3.go"></script>

The JWKS endpoint returns the authorization server's public keys. Cache these with a short TTL — fetching them on every request will tank your latency and anger your auth provider.

## Client Credentials for Service-to-Service Auth

When Service A needs to call Service B with no user involved, Client Credentials is the right grant. There's no user redirect — just a direct POST from your service to the token endpoint.

<script src="https://gist.github.com/mohashari/8cb01e3c08a884021db2c867c867e7f5.js?file=snippet-4.go"></script>

Cache the returned token and reuse it until it's close to expiry — typically you'd subtract 30 seconds from `expires_in` as a safety buffer. Fetching a new token per request under high load will cause token endpoint rate limiting.

## Protecting Your Resource Server

Your API (the resource server) must validate the access token on every request. If you issued JWTs, validate the signature and claims locally. If you issued opaque tokens, call the authorization server's introspection endpoint.

<script src="https://gist.github.com/mohashari/8cb01e3c08a884021db2c867c867e7f5.js?file=snippet-5.go"></script>

The scope check is often forgotten. An access token that's otherwise valid but scoped to `profile` should be rejected by an endpoint that requires `api:write`. A 401 means "you aren't authenticated"; a 403 means "you're authenticated but not authorized for this resource" — use them correctly.

## Token Storage and Refresh

Refresh tokens are long-lived credentials and must be treated like passwords. Store them encrypted at rest. When an access token expires, exchange the refresh token silently.

<script src="https://gist.github.com/mohashari/8cb01e3c08a884021db2c867c867e7f5.js?file=snippet-6.go"></script>

Many authorization servers implement refresh token rotation: each use of a refresh token invalidates it and issues a new one. If your storage update fails after receiving the new token, you'll lose the user's session — wrap the token exchange and storage in an atomic operation, or accept that as a rare but acceptable logout scenario.

---

OAuth 2.0 and OIDC reward engineers who read past the "getting started" guide. The protocol's flexibility is intentional — it covers a staggering range of deployment contexts — but that flexibility means the security properties of your system are the direct result of your implementation choices. Use Authorization Code + PKCE for anything involving a user. Use Client Credentials for machine-to-machine. Always validate tokens fully — signature, issuer, audience, expiry, and scope. Cache JWKS and access tokens intelligently to avoid hammering the authorization server. And never store sensitive tokens — refresh tokens especially — without encryption. The authorization server is only as trustworthy as the implementation sitting in front of it.

`★ Insight ─────────────────────────────────────`
- **Token confusion attacks** are a real threat: a JWT signed for one audience (your ID token) should never be accepted by a resource server expecting an access token. Always validate `aud` strictly.
- **JWKS caching with forced refresh** is a nuanced pattern — cache aggressively, but support fetching fresh keys when you encounter a `kid` (key ID) you don't recognize, to handle key rotations gracefully without downtime.
`─────────────────────────────────────────────────`