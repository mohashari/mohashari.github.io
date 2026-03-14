---
layout: post
title: "API Security: Defending Against the OWASP API Top 10"
tags: [security, api, backend, owasp]
description: "Practical defenses against the OWASP API Security Top 10 with real code examples for building secure backend APIs."
---

APIs are the attack surface of modern applications. The OWASP API Security Top 10 documents the most critical risks. Let's work through each one with practical defenses.

## API1: Broken Object Level Authorization (BOLA)

The most common API vulnerability. Attackers manipulate object IDs to access other users' resources.

```http
# Attacker changes user ID from their ID to victim's ID
GET /api/orders/99999  ← victim's order ID
Authorization: Bearer attacker_token
```

**Defense:** Always verify the authenticated user owns the requested resource.

```go
func GetOrder(w http.ResponseWriter, r *http.Request) {
    orderID := chi.URLParam(r, "id")
    currentUser := r.Context().Value("user").(*User)

    order, err := db.GetOrder(ctx, orderID)
    if err != nil {
        http.Error(w, "Not Found", 404)
        return
    }

    // CRITICAL: Verify ownership
    if order.UserID != currentUser.ID {
        http.Error(w, "Forbidden", 403)
        return
    }

    json.NewEncoder(w).Encode(order)
}
```

Never trust that "only your users will call this endpoint."

## API2: Broken Authentication

Weak tokens, predictable reset tokens, no brute-force protection.

```go
// BAD: Sequential, predictable IDs as tokens
token := fmt.Sprintf("token_%d", userID)

// BAD: Weak reset token
token := fmt.Sprintf("%d", time.Now().Unix())

// GOOD: Cryptographically secure random token
func generateToken() string {
    bytes := make([]byte, 32)
    rand.Read(bytes)
    return hex.EncodeToString(bytes)
}
```

**Defenses:**
- Use cryptographically secure random tokens
- Implement brute-force protection (rate limiting + lockout)
- Use short TTLs for sensitive tokens (password reset: 15 minutes)
- Implement refresh token rotation

## API3: Broken Object Property Level Authorization

Exposing properties users shouldn't see or modify.

```go
// BAD: Returns entire user object including internal fields
type User struct {
    ID           int    `json:"id"`
    Email        string `json:"email"`
    PasswordHash string `json:"password_hash"` // ← Never expose!
    IsAdmin      bool   `json:"is_admin"`       // ← Sensitive
    InternalNotes string `json:"internal_notes"` // ← Internal only
}

// GOOD: Separate response types
type UserResponse struct {
    ID    int    `json:"id"`
    Email string `json:"email"`
    Name  string `json:"name"`
}

func toUserResponse(user *User) UserResponse {
    return UserResponse{ID: user.ID, Email: user.Email, Name: user.Name}
}
```

Also prevent **mass assignment**:

```go
// BAD: Blindly bind all JSON fields (attacker can set is_admin=true)
json.NewDecoder(r.Body).Decode(&user)
db.Save(user)

// GOOD: Only accept specific fields
type UpdateProfileRequest struct {
    Name  string `json:"name"`
    Email string `json:"email"`
    // No is_admin field — attackers can't set it
}

var req UpdateProfileRequest
json.NewDecoder(r.Body).Decode(&req)
user.Name = req.Name
user.Email = req.Email
db.Save(user)
```

## API4: Unrestricted Resource Consumption

No limits on request size, rate, or computation.

```go
// Limits middleware
func LimitsMiddleware(next http.Handler) http.Handler {
    return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
        // Limit request body size (default is unlimited!)
        r.Body = http.MaxBytesReader(w, r.Body, 1*1024*1024) // 1MB max

        next.ServeHTTP(w, r)
    })
}

// Pagination limits
func ListOrders(w http.ResponseWriter, r *http.Request) {
    limit := parseIntParam(r, "limit", 20)
    if limit > 100 {
        limit = 100  // Cap at 100 regardless of what client requests
    }
    // ...
}
```

## API5: Broken Function Level Authorization

Endpoints that perform privileged actions but don't verify the caller has permission.

```go
// BAD: Any authenticated user can access admin endpoints
router.DELETE("/api/users/{id}", deleteUserHandler)

// GOOD: Role-based access control middleware
router.With(RequireRole("admin")).DELETE("/api/users/{id}", deleteUserHandler)

func RequireRole(role string) func(http.Handler) http.Handler {
    return func(next http.Handler) http.Handler {
        return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
            user := r.Context().Value("user").(*User)
            if !user.HasRole(role) {
                http.Error(w, "Forbidden", 403)
                return
            }
            next.ServeHTTP(w, r)
        })
    }
}
```

## API6: Unrestricted Access to Sensitive Business Flows

No protection for sensitive flows like account creation (signup spam), content posting (spam), or checkout (inventory draining).

```go
// Add CAPTCHA verification for sensitive flows
func Register(w http.ResponseWriter, r *http.Request) {
    var req RegisterRequest
    json.NewDecoder(r.Body).Decode(&req)

    // Verify CAPTCHA
    if !captcha.Verify(req.CaptchaToken) {
        http.Error(w, "Invalid CAPTCHA", 400)
        return
    }

    // Rate limit by IP
    if !ipRateLimiter.Allow(r.RemoteAddr) {
        http.Error(w, "Too Many Requests", 429)
        return
    }

    createUser(req)
}
```

## API7: Server Side Request Forgery (SSRF)

Attackers make your server fetch internal resources.

```go
// BAD: Fetches any URL the user provides
func FetchPreview(url string) (string, error) {
    resp, err := http.Get(url)
    // Attacker sends: http://169.254.169.254/latest/meta-data/ (AWS metadata!)
    // ...
}

// GOOD: Validate and allowlist URLs
func FetchPreview(rawURL string) (string, error) {
    parsed, err := url.Parse(rawURL)
    if err != nil {
        return "", errors.New("invalid URL")
    }

    // Only allow HTTPS
    if parsed.Scheme != "https" {
        return "", errors.New("only HTTPS URLs allowed")
    }

    // Block private IP ranges
    ips, _ := net.LookupHost(parsed.Hostname())
    for _, ip := range ips {
        if isPrivateIP(net.ParseIP(ip)) {
            return "", errors.New("private IPs not allowed")
        }
    }

    resp, err := http.Get(rawURL)
    // ...
}
```

## API8 & 9: Security Misconfiguration

- Never expose stack traces in production
- Disable debug endpoints in production
- Set security headers

```go
func SecurityHeadersMiddleware(next http.Handler) http.Handler {
    return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
        w.Header().Set("X-Content-Type-Options", "nosniff")
        w.Header().Set("X-Frame-Options", "DENY")
        w.Header().Set("X-XSS-Protection", "1; mode=block")
        w.Header().Set("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        w.Header().Set("Content-Security-Policy", "default-src 'self'")
        next.ServeHTTP(w, r)
    })
}

// Never expose internal errors
func errorResponse(w http.ResponseWriter, err error, statusCode int) {
    log.Printf("Internal error: %v", err)  // Log full error internally

    // Return generic message to client
    http.Error(w, "An internal error occurred", statusCode)
}
```

## API10: Unsafe Consumption of APIs

Validate and sanitize ALL data from third-party APIs — treat them like untrusted user input.

```go
// BAD: Trust third-party data blindly
resp := stripe.GetPayment(paymentID)
db.Save(Payment{Amount: resp.Amount, Currency: resp.Currency})

// GOOD: Validate before using
resp := stripe.GetPayment(paymentID)
if resp.Amount <= 0 || resp.Amount > MaxPaymentAmount {
    return errors.New("invalid amount from payment provider")
}
if !validCurrencies[resp.Currency] {
    return errors.New("invalid currency from payment provider")
}
```

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
