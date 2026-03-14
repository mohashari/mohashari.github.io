---
layout: post
title: "OAuth 2.0 and OIDC Explained: The Complete Backend Guide"
date: 2026-03-22 07:00:00 +0700
tags: [security, oauth, authentication, authorization, backend]
description: "Demystify OAuth 2.0 and OpenID Connect — authorization flows, token types, PKCE, and how to implement secure auth in your backend services."
---

OAuth 2.0 confuses even experienced engineers because it's an authorization framework, not a protocol — it defines roles and token exchange, but leaves implementation details open. Let's fix the mental model.

## The Core Vocabulary

- **Resource Owner**: The user who owns the data
- **Client**: Your application requesting access
- **Authorization Server**: Issues tokens (Auth0, Keycloak, Google)
- **Resource Server**: Your API that validates tokens
- **Access Token**: Short-lived credential for API access
- **Refresh Token**: Long-lived credential to get new access tokens
- **ID Token**: (OIDC only) JWT containing user identity claims

## The Four OAuth 2.0 Flows

### 1. Authorization Code Flow — For Web Apps

The most secure flow. The authorization code is exchanged server-side, so access tokens never touch the browser.

<script src="https://gist.github.com/mohashari/2cb9cd1234099cdd510eb882eb7515f8.js?file=snippet.txt"></script>

<script src="https://gist.github.com/mohashari/2cb9cd1234099cdd510eb882eb7515f8.js?file=snippet.go"></script>

### 2. Authorization Code + PKCE — For SPAs and Mobile

PKCE (Proof Key for Code Exchange) prevents authorization code interception attacks. Mandatory for public clients (no client secret).

<script src="https://gist.github.com/mohashari/2cb9cd1234099cdd510eb882eb7515f8.js?file=snippet-2.go"></script>

### 3. Client Credentials — Service-to-Service

No user involved. Machine-to-machine authentication.

<script src="https://gist.github.com/mohashari/2cb9cd1234099cdd510eb882eb7515f8.js?file=snippet-3.go"></script>

### 4. Device Authorization — Smart TVs, CLIs

<script src="https://gist.github.com/mohashari/2cb9cd1234099cdd510eb882eb7515f8.js?file=snippet-2.txt"></script>

Used by: GitHub CLI, Google TV, AWS CLI.

## OpenID Connect (OIDC) — Authentication on Top of OAuth

OIDC adds the `openid` scope and returns an **ID Token** (JWT) with user identity:

<script src="https://gist.github.com/mohashari/2cb9cd1234099cdd510eb882eb7515f8.js?file=snippet.json"></script>

**Key validation steps for ID Tokens:**

<script src="https://gist.github.com/mohashari/2cb9cd1234099cdd510eb882eb7515f8.js?file=snippet-4.go"></script>

Never manually decode a JWT without verifying the signature first.

## Token Validation in Resource Servers

<script src="https://gist.github.com/mohashari/2cb9cd1234099cdd510eb882eb7515f8.js?file=snippet-5.go"></script>

## Common Security Mistakes

| Mistake | Fix |
|---------|-----|
| Storing access tokens in `localStorage` | Use `HttpOnly` cookies |
| Not validating `state` parameter | Always validate to prevent CSRF |
| Accepting `alg: none` JWTs | Explicitly whitelist allowed algorithms |
| Long-lived access tokens | Keep them short (15 min), use refresh tokens |
| Skipping PKCE for SPAs | PKCE is mandatory for public clients |
| Trusting `sub` without `iss` | Always validate both to prevent token confusion |

OAuth 2.0 and OIDC have a steep learning curve, but getting auth right is non-negotiable. Use a battle-tested library, validate every claim, and treat tokens like passwords.
