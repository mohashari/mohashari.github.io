---
layout: post
title: "API Versioning Strategies: Breaking Changes Without Breaking Clients"
date: 2026-03-23 08:00:00 +0700
tags: [api-design, backend, versioning, distributed-systems, platform-engineering]
description: "A production playbook for URL versioning, header negotiation, and consumer-driven contracts that lets you retire old API versions without incident."
---

The moment you deprecate `/api/v1/users` is not the moment you shut it down. The gap between those two events—measured in months, sometimes years—is where most platform teams fail. They announce a sunset, publish a migration guide, and then watch helplessly as 40% of their consumers still call the old endpoint on the day they flip the switch, because no one on those teams saw the announcement, the headers weren't being logged, and the internal SDK everyone uses still hardcodes `v1`. A versioning strategy that lacks a compulsory deprecation lifecycle isn't a strategy; it's a wish.

![API Versioning Strategies: Breaking Changes Without Breaking Clients Diagram](/images/diagrams/api-versioning-strategies.svg)

## URL Versioning Is Still the Right Default

Accept it upfront: URL versioning (`/api/v2/orders`) is blunt, leaks abstraction concerns into your URI space, and REST purists hate it. It's also the only scheme that works reliably at every layer of your stack—load balancers, CDN cache keys, access logs, curl one-liners, browser dev tools. Header-based versioning (`Accept: application/vnd.myapi.v2+json`) is cleaner semantically, but it makes caching hard, breaks default browser behavior, and routinely disappears when traffic passes through API gateways that strip custom `Accept` headers.

The practical rule: use URL versioning as your canonical routing mechanism. If you also need content negotiation (say, for clients that want partial field sets or different serialization formats), layer a `Accept` header on top, but never let it be the *only* signal that routes between incompatible resource shapes. Every router, every log aggregator, every rate-limit rule you ever write will thank you.

One underused pattern: keep your *internal* service boundaries on a separate versioning axis. Your public `/api/v3/payments` endpoint can fan out to `payment-service:v7` internally. Decoupling public version from internal version means you can refactor service internals without touching the contract clients depend on.

## Routing Versions at the Gateway Layer

Every request touching a deprecated version should pass through a gateway that can inject deprecation headers *without* requiring the upstream service to be modified. This separation matters: if your deprecation logic lives inside the service itself, you can't retrofit it onto services you don't own, and you can't enforce uniform header formats across 30 microservices.

```nginx
# snippet-1
# nginx — inject Deprecation/Sunset on all /api/v1/ traffic
server {
    listen 443 ssl;

    location ~ ^/api/v1/ {
        # Upstream still handles v1 requests normally
        proxy_pass http://api-service-v1;

        # RFC 8594 / draft-ietf-httpapi-deprecation-header
        add_header Deprecation            "true"                             always;
        add_header Sunset                 "Mon, 01 Sep 2026 00:00:00 GMT"   always;
        add_header Link                   '</api/v3/docs>; rel="successor-version"' always;
        add_header X-API-Warn             "v1 sunset 2026-09-01; migrate to /api/v3/" always;
    }

    location ~ ^/api/v2/ {
        proxy_pass http://api-service-v2;
        add_header Deprecation            "2026-03-01T00:00:00Z"            always;
        add_header Sunset                 "Mon, 01 Mar 2027 00:00:00 GMT"   always;
        add_header Link                   '</api/v3/docs>; rel="successor-version"' always;
    }

    location ~ ^/api/v3/ {
        proxy_pass http://api-service-v3;
        # No deprecation headers — this is current
    }
}
```

The `Deprecation` and `Sunset` headers follow [RFC 8594](https://www.rfc-editor.org/rfc/rfc8594) and the IETF `draft-ietf-httpapi-deprecation-header` draft. Use them. Don't invent `X-Deprecated: yes`—there are already SDKs that parse the standardized headers and surface warnings in their HTTP client middleware.

For teams using Kong or AWS API Gateway, the equivalent is a response transformer plugin that injects these headers based on route tags. Kong's `response-transformer` plugin handles this in under 10 lines of YAML.

## The Deprecation Header Isn't Enough on Its Own

Clients that don't log response headers will never see `Sunset`. The header is a machine-readable signal intended for SDK middleware and monitoring dashboards, not for the developer who wrote the consumer in 2023 and hasn't touched it since. Your deprecation workflow needs to force the signal into *human-visible* channels.

The minimum viable escalation ladder looks like this:

**T-180 days**: Announce sunset publicly (changelog, developer portal, email to all registered API key holders). Start injecting `Deprecation` and `Sunset` headers.

**T-30 days**: Query your access logs for any consumer still hitting v1. Email the team contact on record for every API key that made a request in the last 7 days. If you have no contact on record, that is a process failure to fix now, not at T-0.

**T-7 days**: Start returning `429 Too Many Requests` with a `Retry-After` of 0 and a body like `{"error": "api_version_sunset_imminent", "migrate_by": "2026-09-01", "docs": "/api/v3/migration"}` for a percentage of v1 traffic. Start at 5%, ramp to 25%. This is the fire alarm. Clients that ignored headers will not ignore production errors.

**T-0**: Return `410 Gone` with a body that includes the migration URL. Do not return `404`. `410` is semantically permanent; `404` implies the resource might come back.

```go
// snippet-2
// Go — gateway middleware that enforces the sunset escalation ladder
package middleware

import (
    "net/http"
    "time"
)

var v1SunsetDate = time.Date(2026, 9, 1, 0, 0, 0, 0, time.UTC)

func V1SunsetMiddleware(next http.Handler) http.Handler {
    return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
        now := time.Now().UTC()
        remaining := v1SunsetDate.Sub(now)

        switch {
        case now.After(v1SunsetDate):
            // Permanent — 410 Gone
            w.Header().Set("Content-Type", "application/json")
            w.WriteHeader(http.StatusGone)
            w.Write([]byte(`{"error":"version_sunset","message":"v1 was retired 2026-09-01","migrate_to":"/api/v3/"}`))
            return

        case remaining <= 7*24*time.Hour:
            // Final week — throttle 25% of requests to force visibility
            if pseudoRandPct(r) < 25 {
                w.Header().Set("Content-Type", "application/json")
                w.Header().Set("Retry-After", "0")
                w.WriteHeader(http.StatusTooManyRequests)
                w.Write([]byte(`{"error":"sunset_imminent","sunset":"2026-09-01","docs":"/api/v3/migration"}`))
                return
            }
        }

        // Normal path — inject deprecation headers
        w.Header().Set("Deprecation", "true")
        w.Header().Set("Sunset", "Mon, 01 Sep 2026 00:00:00 GMT")
        w.Header().Set("Link", `</api/v3/docs>; rel="successor-version"`)
        next.ServeHTTP(w, r)
    })
}

// pseudoRandPct returns a stable 0-99 value for a given request,
// using a hash of the API key so the same client always gets the same answer.
func pseudoRandPct(r *http.Request) int {
    key := r.Header.Get("Authorization")
    if key == "" {
        key = r.RemoteAddr
    }
    h := fnv32(key)
    return int(h % 100)
}
```

The `pseudoRandPct` approach (hash of API key mod 100) gives you *sticky* throttling: once a consumer hits the wall, they keep hitting it. Random sampling would let the same client succeed 75% of the time and make the problem look intermittent, which causes incident tickets instead of migration tickets.

## Consumer-Driven Contract Testing as a Migration Gate

URL versioning and deprecation headers tell clients *that* things are changing. Consumer-driven contract testing (CDCT) tells you *which* clients will break—before you ship. If you're not running Pact or a compatible broker, you're flying blind on breaking changes.

The workflow: each consumer publishes a "pact"—a recorded set of interactions (requests + expected responses) against the provider API. On every provider deploy, the provider verifies it can still satisfy every outstanding pact for every consumer, across every version. A consumer on v1 that hasn't migrated will have a v1 pact. The moment you make a breaking change to v1's response shape, that verification fails.

```yaml
# snippet-3
# pact-broker — docker-compose fragment for a self-hosted broker
version: "3.9"
services:
  pact-broker:
    image: pactfoundation/pact-broker:2.107.1
    ports:
      - "9292:9292"
    environment:
      PACT_BROKER_BASE_URL: "https://pact.internal.example.com"
      PACT_BROKER_DATABASE_URL: "postgres://pact:secret@postgres/pact"
      PACT_BROKER_ALLOW_PUBLIC_READ: "false"
      PACT_BROKER_WEBHOOK_RETRY_LIMIT: "3"
    depends_on: [postgres]

  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_DB: pact
      POSTGRES_USER: pact
      POSTGRES_PASSWORD: secret
    volumes:
      - pact_pgdata:/var/lib/postgresql/data

volumes:
  pact_pgdata:
```

The `can-i-deploy` CLI command is the key gate. Wire it into your CI pipeline on the *provider* side:

```bash
# snippet-4
#!/usr/bin/env bash
# CI script — block provider deploy if any consumer contract is broken
set -euo pipefail

PROVIDER="orders-service"
BROKER_URL="https://pact.internal.example.com"
BROKER_TOKEN="${PACT_BROKER_TOKEN}"

# Check against all consumer versions that are deployed to production
pact-broker can-i-deploy \
  --pacticipant "${PROVIDER}" \
  --version "${GIT_SHA}" \
  --to-environment production \
  --broker-base-url "${BROKER_URL}" \
  --broker-token "${BROKER_TOKEN}" \
  --retry-while-unknown 30 \
  --retry-interval 10

# Also verify against any consumer tagged as "v1-migration-pending"
# These are consumers that haven't finished migrating off v1 yet
pact-broker can-i-deploy \
  --pacticipant "${PROVIDER}" \
  --version "${GIT_SHA}" \
  --to-environment production \
  --consumer-version-selectors '[{"tag":"v1-migration-pending","latest":true}]' \
  --broker-base-url "${BROKER_URL}" \
  --broker-token "${BROKER_TOKEN}"

echo "All consumer contracts satisfied — safe to deploy"
```

The `v1-migration-pending` tag pattern is the operational trick that makes CDCT useful for version management specifically. When a consumer team confirms they've completed migration off v1, they remove the tag. When the tag list is empty and the sunset window has passed, you have cryptographic proof that no registered consumer still depends on v1 behavior.

## Designing the Version Shape: What Actually Breaks

Not all changes require a new major version. The breakage taxonomy matters because over-versioning is its own failure mode—if you bump the major version for every field addition, you'll have v12 by the end of year two and consumers will stop tracking migrations entirely.

Non-breaking (safe without versioning):
- Adding optional fields to a response
- Adding optional query parameters
- Expanding an enum (with caveats—clients that switch on exhaustive enums will break)
- Adding new endpoints
- Relaxing validation rules

Breaking (requires a version bump):
- Removing or renaming a field
- Changing a field's type (`string` → `integer`, `object` → `array`)
- Changing authentication schemes
- Tightening validation (e.g., a field that accepted empty string now rejects it)
- Changing pagination semantics (cursor-based replacing offset)
- Changing error response shape

The hardest category is behavioral changes that don't touch the schema—changing the semantics of an existing field without changing its name or type. `status: "pending"` that used to mean "not yet processed" now means "processing started." The schema is identical. The behavior is completely different. No contract test catches this unless you write explicit interaction tests with real state assertions.

```python
# snippet-5
# Python — automated changelog diff that flags breaking changes in OpenAPI specs
# Uses openapi-diff (https://github.com/OpenAPITools/openapi-diff) output parsed in CI

import json
import subprocess
import sys
from pathlib import Path


BREAKING_CHANGE_TYPES = {
    "REQUEST_BODY_REQUIRED_PROPERTY_ADDED",
    "RESPONSE_PROPERTY_MISSING",
    "RESPONSE_PROPERTY_TYPE_CHANGED",
    "REQUEST_PROPERTY_TYPE_CHANGED",
    "ENDPOINT_REMOVED",
    "RESPONSE_STATUS_MISSING",
    "SECURITY_REQUIREMENT_ADDED",
}


def check_breaking_changes(old_spec: Path, new_spec: Path) -> list[dict]:
    result = subprocess.run(
        [
            "openapi-diff",
            str(old_spec),
            str(new_spec),
            "--json",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode not in (0, 1):
        print(f"openapi-diff error: {result.stderr}", file=sys.stderr)
        sys.exit(2)

    diff = json.loads(result.stdout)
    breaking = [
        change
        for change in diff.get("incompatibleChanges", [])
        if change.get("type") in BREAKING_CHANGE_TYPES
    ]
    return breaking


def main():
    old_spec = Path("specs/v2.yaml")
    new_spec = Path("specs/v2-candidate.yaml")

    breaking = check_breaking_changes(old_spec, new_spec)
    if breaking:
        print(f"BLOCKED: {len(breaking)} breaking change(s) detected in v2 spec:")
        for change in breaking:
            print(f"  [{change['type']}] {change.get('description', '')}")
        print("Either bump to v3 or revert these changes.")
        sys.exit(1)

    print("No breaking changes detected in v2 spec — safe to ship.")


if __name__ == "__main__":
    main()
```

Run this in CI on every PR that touches the OpenAPI spec. If breaking changes are detected, the PR is blocked until the author either reverts the change or explicitly creates a v3 spec alongside it. This turns version bumping from a manual judgment call into an enforced workflow.

## The Version Registry: Knowing Who Calls What

You cannot retire a version if you don't know who's using it. This sounds obvious, but most teams don't have a registry—they have access logs and a vague sense that "some team uses v1." Access logs aren't enough. You need:

1. **API key → team mapping**: Every key issued must have a registered owner (team name, Slack channel, oncall rotation). This is enforced at issuance, not audited later.
2. **Per-key version usage metrics**: Prometheus counters tagged with `{version="v1", api_key_id="k_abc123", path="/api/v1/orders"}`. Exposed via a Grafana dashboard that shows week-over-week v1 traffic trends.
3. **Automated outreach**: A weekly cron that queries Prometheus for any key with >100 v1 calls in the last 7 days and opens a Jira ticket (or sends a Slack DM) to the registered owner.

```go
// snippet-6
// Go — Prometheus counter for per-version, per-key API usage
package metrics

import (
    "net/http"
    "strings"

    "github.com/prometheus/client_golang/prometheus"
    "github.com/prometheus/client_golang/prometheus/promauto"
)

var apiVersionRequests = promauto.NewCounterVec(
    prometheus.CounterOpts{
        Namespace: "api",
        Name:      "version_requests_total",
        Help:      "Total API requests broken down by version, method, and API key ID.",
    },
    []string{"version", "method", "api_key_id", "status_class"},
)

// VersionMetricsMiddleware wraps an http.Handler and records per-version metrics.
// It expects the URL to start with /api/vN/ and an X-API-Key-ID header set by
// the authentication middleware upstream.
func VersionMetricsMiddleware(next http.Handler) http.Handler {
    return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
        rw := &statusRecorder{ResponseWriter: w, status: 200}
        next.ServeHTTP(rw, r)

        version := extractVersion(r.URL.Path) // "v1", "v2", "v3", or "unknown"
        keyID := r.Header.Get("X-API-Key-ID")
        if keyID == "" {
            keyID = "anonymous"
        }

        statusClass := statusBucket(rw.status) // "2xx", "4xx", "5xx"
        apiVersionRequests.WithLabelValues(version, r.Method, keyID, statusClass).Inc()
    })
}

func extractVersion(path string) string {
    parts := strings.SplitN(strings.TrimPrefix(path, "/api/"), "/", 2)
    if len(parts) > 0 && strings.HasPrefix(parts[0], "v") {
        return parts[0]
    }
    return "unknown"
}

func statusBucket(code int) string {
    switch {
    case code < 300:
        return "2xx"
    case code < 400:
        return "3xx"
    case code < 500:
        return "4xx"
    default:
        return "5xx"
    }
}

type statusRecorder struct {
    http.ResponseWriter
    status int
}

func (r *statusRecorder) WriteHeader(code int) {
    r.status = code
    r.ResponseWriter.WriteHeader(code)
}
```

With this in place, you can run a Prometheus query like `sum by (api_key_id) (increase(api_version_requests_total{version="v1"}[7d]))` and immediately see which keys are responsible for 80% of your v1 traffic. Target those teams first.

## The Policy Document Is the Artifact

Every API versioning strategy eventually becomes a social contract as much as a technical one. Document it explicitly:

- **Version support lifecycle**: Current (no sunset), Stable/LTS (12-month sunset notice minimum), Deprecated (6-month sunset notice minimum).
- **Breaking change definition**: Enumerate exactly what constitutes a breaking change. Attach the openapi-diff check as enforcement.
- **Sunset notification requirements**: Who gets notified, at what intervals, through what channels. Make the oncall team responsible for closing out the consumer list before T-0.
- **Migration tooling commitment**: When you introduce v3, you publish an automated migration guide. Not a blog post—a script or a codemods that mechanically transforms v2 request/response shapes to v3 wherever the mapping is 1:1.

The teams that execute version sunsets cleanly are the ones who treated the policy document as a first-class artifact and enforced it through automation, not good intentions. The `Sunset` header gets ignored. The 410 response at T-0 does not.
