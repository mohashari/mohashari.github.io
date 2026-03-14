---
layout: post
title: "API Security: Defending Against the OWASP API Top 10"
tags: [security, api, backend, owasp]
description: "Practical defenses against the OWASP API Security Top 10 with real code examples for building secure backend APIs."
---

APIs are the attack surface of modern applications. The OWASP API Security Top 10 documents the most critical risks. Let's work through each one with practical defenses.

## API1: Broken Object Level Authorization (BOLA)

The most common API vulnerability. Attackers manipulate object IDs to access other users' resources.


<script src="https://gist.github.com/mohashari/524e43fe54bb5ac5aff25ce58adf5b1d.js?file=snippet.txt"></script>


**Defense:** Always verify the authenticated user owns the requested resource.


<script src="https://gist.github.com/mohashari/524e43fe54bb5ac5aff25ce58adf5b1d.js?file=snippet.go"></script>


Never trust that "only your users will call this endpoint."

## API2: Broken Authentication

Weak tokens, predictable reset tokens, no brute-force protection.


<script src="https://gist.github.com/mohashari/524e43fe54bb5ac5aff25ce58adf5b1d.js?file=snippet-2.go"></script>


**Defenses:**
- Use cryptographically secure random tokens
- Implement brute-force protection (rate limiting + lockout)
- Use short TTLs for sensitive tokens (password reset: 15 minutes)
- Implement refresh token rotation

## API3: Broken Object Property Level Authorization

Exposing properties users shouldn't see or modify.


<script src="https://gist.github.com/mohashari/524e43fe54bb5ac5aff25ce58adf5b1d.js?file=snippet-3.go"></script>


Also prevent **mass assignment**:


<script src="https://gist.github.com/mohashari/524e43fe54bb5ac5aff25ce58adf5b1d.js?file=snippet-4.go"></script>


## API4: Unrestricted Resource Consumption

No limits on request size, rate, or computation.


<script src="https://gist.github.com/mohashari/524e43fe54bb5ac5aff25ce58adf5b1d.js?file=snippet-5.go"></script>


## API5: Broken Function Level Authorization

Endpoints that perform privileged actions but don't verify the caller has permission.


<script src="https://gist.github.com/mohashari/524e43fe54bb5ac5aff25ce58adf5b1d.js?file=snippet-6.go"></script>


## API6: Unrestricted Access to Sensitive Business Flows

No protection for sensitive flows like account creation (signup spam), content posting (spam), or checkout (inventory draining).


<script src="https://gist.github.com/mohashari/524e43fe54bb5ac5aff25ce58adf5b1d.js?file=snippet-7.go"></script>


## API7: Server Side Request Forgery (SSRF)

Attackers make your server fetch internal resources.


<script src="https://gist.github.com/mohashari/524e43fe54bb5ac5aff25ce58adf5b1d.js?file=snippet-8.go"></script>


## API8 & 9: Security Misconfiguration

- Never expose stack traces in production
- Disable debug endpoints in production
- Set security headers


<script src="https://gist.github.com/mohashari/524e43fe54bb5ac5aff25ce58adf5b1d.js?file=snippet-9.go"></script>


## API10: Unsafe Consumption of APIs

Validate and sanitize ALL data from third-party APIs — treat them like untrusted user input.


<script src="https://gist.github.com/mohashari/524e43fe54bb5ac5aff25ce58adf5b1d.js?file=snippet-10.go"></script>


## Security Checklist

- [ ] BOLA checks on every object endpoint
- [ ] Cryptographically random tokens
- [ ] Separate response types (never leak internal fields)
- [ ] Request body size limits
- [ ] Role-based access on every endpoint
- [ ] Rate limiting on all sensitive flows
- [ ] SSRF protection for any URL fetching
- [ ] Security headers on all responses
- [ ] Never expose stack traces in production
- [ ] Input validation on third-party API responses

Security is not a feature — it's a continuous discipline. Schedule regular security reviews and penetration tests.
