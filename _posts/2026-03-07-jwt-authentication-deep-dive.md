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

```
eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIiwiaWF0IjoxNTE2MjM5MDIyfQ.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c
```

Decoded:
```json
// Header
{ "alg": "HS256", "typ": "JWT" }

// Payload
{
  "sub": "1234567890",
  "name": "John Doe",
  "iat": 1516239022,
  "exp": 1516242622
}

// Signature = HMAC_SHA256(base64(header) + "." + base64(payload), secret)
```

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

```go
package auth

import (
    "time"
    "github.com/golang-jwt/jwt/v5"
)

type Claims struct {
    UserID string `json:"sub"`
    Email  string `json:"email"`
    Role   string `json:"role"`
    jwt.RegisteredClaims
}

var signingKey = []byte(os.Getenv("JWT_SECRET"))

func GenerateToken(userID, email, role string) (string, error) {
    claims := Claims{
        UserID: userID,
        Email:  email,
        Role:   role,
        RegisteredClaims: jwt.RegisteredClaims{
            ExpiresAt: jwt.NewNumericDate(time.Now().Add(15 * time.Minute)),
            IssuedAt:  jwt.NewNumericDate(time.Now()),
            Issuer:    "myapp",
        },
    }

    token := jwt.NewWithClaims(jwt.SigningMethodHS256, claims)
    return token.SignedString(signingKey)
}

func ValidateToken(tokenString string) (*Claims, error) {
    token, err := jwt.ParseWithClaims(tokenString, &Claims{}, func(t *jwt.Token) (interface{}, error) {
        // CRITICAL: verify the signing method
        if _, ok := t.Method.(*jwt.SigningMethodHMAC); !ok {
            return nil, fmt.Errorf("unexpected signing method: %v", t.Header["alg"])
        }
        return signingKey, nil
    })

    if err != nil {
        return nil, err
    }

    claims, ok := token.Claims.(*Claims)
    if !ok || !token.Valid {
        return nil, fmt.Errorf("invalid token")
    }

    return claims, nil
}
```

## Refresh Token Pattern

Short-lived access tokens + long-lived refresh tokens:

```
1. Login → server returns:
   - access_token (15 min TTL, stored in memory)
   - refresh_token (7 days TTL, stored in httpOnly cookie)

2. API requests use access_token in Authorization header

3. When access_token expires:
   - Client sends refresh_token
   - Server validates and issues new access_token + rotated refresh_token

4. Logout → invalidate refresh_token in database
```

```go
func RefreshTokens(refreshToken string) (accessToken, newRefreshToken string, err error) {
    // Validate refresh token against database
    stored, err := db.GetRefreshToken(ctx, refreshToken)
    if err != nil || stored.ExpiresAt.Before(time.Now()) {
        return "", "", errors.New("invalid or expired refresh token")
    }

    // Generate new tokens
    accessToken, _ = GenerateAccessToken(stored.UserID)
    newRefreshToken = generateSecureRandom(32)

    // Rotate: delete old, store new (refresh token rotation)
    db.DeleteRefreshToken(ctx, refreshToken)
    db.StoreRefreshToken(ctx, stored.UserID, newRefreshToken, 7*24*time.Hour)

    return accessToken, newRefreshToken, nil
}
```

## Critical Security Mistakes

### 1. The `alg: none` Attack

Never accept tokens with `alg: none`. Always verify the algorithm:

```go
// WRONG - vulnerable to alg:none attack
jwt.Parse(tokenString, func(t *jwt.Token) (interface{}, error) {
    return signingKey, nil
})

// RIGHT - verify algorithm first
jwt.ParseWithClaims(tokenString, &Claims{}, func(t *jwt.Token) (interface{}, error) {
    if _, ok := t.Method.(*jwt.SigningMethodHMAC); !ok {
        return nil, fmt.Errorf("unexpected signing method: %v", t.Header["alg"])
    }
    return signingKey, nil
})
```

### 2. Storing JWT in localStorage

localStorage is vulnerable to XSS. Store access tokens in memory and refresh tokens in httpOnly cookies:

```javascript
// WRONG
localStorage.setItem('access_token', token);

// RIGHT - store in memory (React state, etc.)
setAccessToken(token);

// Refresh token in httpOnly cookie (set by server)
// document.cookie cannot access httpOnly cookies
```

### 3. Weak Secrets

For HS256, use at least 256 bits of entropy:

```bash
# Generate a strong secret
openssl rand -hex 32
```

For production, prefer RS256 (RSA) or ES256 (ECDSA) — asymmetric algorithms allow public verification without sharing the private key.

### 4. Not Validating Claims

Always check `exp`, `iss`, and `aud`:

```go
jwt.ParseWithClaims(tokenString, &Claims{},
    keyFunc,
    jwt.WithExpirationRequired(),
    jwt.WithIssuer("myapp"),
    jwt.WithAudience("myapp-api"),
)
```

### 5. Sensitive Data in Payload

Don't store sensitive data (passwords, PII) in JWT payload. It's base64 decoded by anyone:

```json
// BAD - anyone with the token can read this
{ "sub": "42", "credit_card": "4111-1111-1111-1111" }

// GOOD - minimal claims only
{ "sub": "42", "role": "user" }
```

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
