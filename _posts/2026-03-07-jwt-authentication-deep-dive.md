---
layout: post
title: "JWT Authentication: A Complete Deep Dive"
tags: [security, authentication, jwt, backend]
description: "Everything you need to know about JWT — how it works, common vulnerabilities, and how to implement it securely."
---

JSON Web Tokens (JWT) are everywhere. But they're also widely misimplemented. This guide covers how JWT actually works, the mistakes that create security vulnerabilities, and how to do it right.

![JWT Authentication Flow](/images/diagrams/jwt-auth-flow.svg)

## What is a JWT?

A JWT is a compact, URL-safe string with three base64url-encoded parts separated by dots:


<script src="https://gist.github.com/mohashari/b46d87053c985fbc2d46cb6769eaf03d.js?file=snippet.txt"></script>


Decoded:

<script src="https://gist.github.com/mohashari/b46d87053c985fbc2d46cb6769eaf03d.js?file=snippet.json"></script>


The signature verifies the token hasn't been tampered with. **The payload is not encrypted — it's just base64 encoded. Anyone can read it.**

## Standard Claims

| Claim | Meaning |
|-------|---------|
| `sub` | Subject (usually user ID) |
| `iat` | Issued at (timestamp) |
| `exp` | Expiration time |
| `nbf` | Not before |
| `iss` | Issuer |
| `aud` | Audience |
| `jti` | JWT ID (for revocation) |

## Secure Implementation in Go


<script src="https://gist.github.com/mohashari/b46d87053c985fbc2d46cb6769eaf03d.js?file=snippet.go"></script>


## Refresh Token Pattern

Short-lived access tokens + long-lived refresh tokens:


<script src="https://gist.github.com/mohashari/b46d87053c985fbc2d46cb6769eaf03d.js?file=snippet-2.txt"></script>



<script src="https://gist.github.com/mohashari/b46d87053c985fbc2d46cb6769eaf03d.js?file=snippet-2.go"></script>


## Critical Security Mistakes

### 1. The `alg: none` Attack

Never accept tokens with `alg: none`. Always verify the algorithm:


<script src="https://gist.github.com/mohashari/b46d87053c985fbc2d46cb6769eaf03d.js?file=snippet-3.go"></script>


### 2. Storing JWT in localStorage

localStorage is vulnerable to XSS. Store access tokens in memory and refresh tokens in httpOnly cookies:


<script src="https://gist.github.com/mohashari/b46d87053c985fbc2d46cb6769eaf03d.js?file=snippet.js"></script>


### 3. Weak Secrets

For HS256, use at least 256 bits of entropy:


<script src="https://gist.github.com/mohashari/b46d87053c985fbc2d46cb6769eaf03d.js?file=snippet.sh"></script>


For production, prefer RS256 (RSA) or ES256 (ECDSA) — asymmetric algorithms allow public verification without sharing the private key.

### 4. Not Validating Claims

Always check `exp`, `iss`, and `aud`:


<script src="https://gist.github.com/mohashari/b46d87053c985fbc2d46cb6769eaf03d.js?file=snippet-4.go"></script>


### 5. Sensitive Data in Payload

Don't store sensitive data (passwords, PII) in JWT payload. It's base64 decoded by anyone:


<script src="https://gist.github.com/mohashari/b46d87053c985fbc2d46cb6769eaf03d.js?file=snippet-2.json"></script>


## JWT vs Sessions

| | JWT (stateless) | Sessions (stateful) |
|--|-----------------|---------------------|
| Server storage | None | Session store (Redis) |
| Revocation | Hard (need denylist) | Easy (delete session) |
| Horizontal scaling | Easy | Needs shared session store |
| Performance | Token validation | Redis lookup |
| Token size | ~500 bytes | ~32 bytes (session ID) |

**Use sessions when:** You need instant revocation (e.g., "log out all devices")
**Use JWT when:** You need stateless, horizontally scalable auth

Neither is universally better — choose based on your requirements.
