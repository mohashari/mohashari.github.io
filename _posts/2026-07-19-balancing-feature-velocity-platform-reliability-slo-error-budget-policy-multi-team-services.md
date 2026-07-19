---
layout: post
title: "Balancing Feature Velocity and Platform Reliability: Designing a Service Level Objective (SLO) Error Budget Policy for Multi-Team Services"
date: 2026-07-19 08:00:00 +0700
tags: [sre, platform-engineering, monitoring, microservices, architecture]
description: "A production-tested framework for building, calculating, and enforcing multi-team SLO error budget policies in shared microservice environments."
image: "https://picsum.photos/seed/8478/1080/720"
thumbnail: "https://picsum.photos/seed/8478/400/300"
---

Imagine a Friday afternoon when a core shared microservice, `payment-gateway`, experiences a massive latency spike. The platform handles 15,000 requests per second (RPS) at peak and is shared by three product teams: Checkout, Subscriptions, and Refund. The latency spike is traced to an unoptimized database query deployed by the Checkout team. Within two hours, the overall availability of `payment-gateway` drops from 99.95% to 99.1%, completely exhausting the shared service's monthly error budget. The automated SRE deployment gate triggers, locking out all deployments. Suddenly, the Subscriptions team, who had a critical, time-sensitive compliance patch ready to ship, finds themselves blocked from production. They did nothing wrong, yet their velocity is ground to a halt. This is the classic SRE "Tragedy of the Commons." Without a multi-team Error Budget Policy (EBP) that attributes reliability consumption to individual teams, shared platforms inevitably become organizational bottlenecks or operational liabilities.

## The Shared Service Dilemma: Why Monolithic SLOs Fail

In a modern microservices architecture, shared platform services are essential to avoid code duplication and simplify compliance (e.g., PCI-DSS, SOC2). However, they create operational coupling. When SREs define a single, service-wide Service Level Objective (SLO) for a shared component, they treat the service as a monolith. This approach fails for three reasons:

1. **Lack of Attribution:** If a shared service fails, the metrics show the service is unhealthy, but they do not automatically identify which team's upstream traffic or code deployment caused the failure.
2. **Velocity Hijacking:** As described in the opening scenario, a single high-velocity team can consume the entire error budget, penalizing low-velocity teams that share the same platform.
3. **The High-Volume Bias:** If the Checkout team generates 90% of the traffic and the Subscriptions team generates 10%, a total outage of the Subscriptions module represents only a 10% drop in overall service availability. The Checkout team could introduce massive instability and never breach the overall SLO, while a minor issue in Subscriptions could be completely masked by the high volume of checkout traffic.

To scale multi-team services, we must move away from single-service SLOs and design a multi-dimensional framework where error budgets are partitioned, attributed, and programmatically enforced.

## Anatomy of a Multi-Team Service: Tracking Attribution

To attribute reliability consumption, we must inject client and route metadata into our telemetry. Using OpenTelemetry (OTel) and Prometheus, we can capture the caller identity and the specific functional path.

Every incoming request to our Go-based `payment-gateway` must go through a middleware layer that extracts two critical dimensions:
1. `http.route`: The templated path (e.g., `/v1/charges` vs `/v1/subscriptions`).
2. `client.id`: The caller service name, extracted from the JWT token or request headers (e.g., `checkout-service`, `billing-engine`).

Here is a conceptual Go middleware snippet implementing this telemetry tagging using OpenTelemetry:

```go
func SLOMiddleware(next http.Handler) http.Handler {
    return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
        ctx := r.Context()
        clientId := r.Header.Get("X-Client-Id")
        if clientId == "" {
            clientId = "unknown"
        }
        
        // Extract route pattern
        routePattern := mux.CurrentRoute(r).GetPathTemplate()
        
        // Inject attributes into OTel span
        span := trace.SpanFromContext(ctx)
        span.SetAttributes(
            attribute.String("client.id", clientId),
            attribute.String("http.route", routePattern),
        )
        
        // Metric collection using OTel Metric SDK
        meter := otel.GetMeterProvider().Meter("payment-gateway")
        requestCounter, _ := meter.Int64Counter("http.server.requests")
        
        // Wrap writer to capture status code
        rw := newResponseWriter(w)
        
        defer func() {
            requestCounter.Add(ctx, 1, metric.WithAttributes(
                attribute.String("client.id", clientId),
                attribute.String("http.route", routePattern),
                attribute.Int("http.status_code", rw.statusCode),
            ))
        }()
        
        next.ServeHTTP(rw, r)
    })
}
```

By structuring telemetry this way, we generate multi-dimensional metrics that allow us to isolate the exact route and client during an outage.

## The Mathematics of Multi-Dimensional Error Budgets

An SLO is defined as the target success rate of a Service Level Indicator (SLI) over a compliance period (typically a rolling 30-day window).

$$\text{SLI} = \frac{\text{Good Events}}{\text{Total Events}}$$

$$\text{Error Budget} = 1 - \text{SLO Target}$$

For a shared service, we partition the error budget into specific traffic facets ($i$):

$$\text{SLI}_{i} = \frac{\sum \text{Successful Requests for Client } i \text{ or Route } i}{\sum \text{Total Requests for Client } i \text{ or Route } i}$$

$$\text{Error Budget}_{i} = \text{Total Requests}_{i} \times (1 - \text{SLO Target}_{i})$$

### Burn Rate Calculations
Relying on a simple 30-day rolling average to trigger alerts is dangerous: it reacts too slowly to sudden outages and flags false positives during transient spikes. SRE best practices dictate using multi-window, multi-burn-rate alerts (typically 1-hour and 6-hour windows for paging, and 3-day and 30-day windows for ticketing).

A burn rate of 1 means the service will consume exactly 100% of its error budget over the compliance window. A burn rate of 14.4 means 2% of the budget is consumed in 1 hour. 

To compute the multi-team burn rate dynamically in Prometheus, we use the following PromQL query for a 99.9% SLO (where the allowable error rate is $0.001$):

```promql
# Calculate the 1-hour burn rate per team/client
(
  sum(rate(http_server_requests_total{job="payment-gateway", status_code=~"5.."}[1h])) by (client_id)
  /
  sum(rate(http_server_requests_total{job="payment-gateway"}[1h])) by (client_id)
)
/
0.001
```

If the resulting value is greater than 14.4, we trigger a high-severity alert routed directly to the specific team indicated by the `client_id` label, bypass the shared platform team's pager, and isolate the source of the burn.

### Declarative SLO Definition with Sloth
Rather than writing complex PromQL alert rules by hand, we define our SLOs declaratively using Sloth, an open-source tool that generates Prometheus alert rules automatically. 

Below is a Sloth configuration partition for `payment-gateway`, separating the SLOs for the Checkout and Subscriptions domains:

```yaml
version: "prometheus/v1"
service: "payment-gateway"
slos:
  - name: "checkout-availability"
    objective: 99.9
    spec:
      alerting:
        page_alert:
          labels:
            severity: critical
            team: checkout
        ticket_alert:
          labels:
            severity: warning
            team: checkout
      sli:
        events:
          error_query: sum(rate(http_server_requests_total{job="payment-gateway", http_route=~"/v1/charges.*", status_code=~"5.."}[{{.window}}]))
          total_query: sum(rate(http_server_requests_total{job="payment-gateway", http_route=~"/v1/charges.*"}[{{.window}}]))
  - name: "subscription-availability"
    objective: 99.95
    spec:
      alerting:
        page_alert:
          labels:
            severity: critical
            team: billing
        ticket_alert:
          labels:
            severity: warning
            team: billing
      sli:
        events:
          error_query: sum(rate(http_server_requests_total{job="payment-gateway", http_route=~"/v1/subscriptions.*", status_code=~"5.."}[{{.window}}]))
          total_query: sum(rate(http_server_requests_total{job="payment-gateway", http_route=~"/v1/subscriptions.*"}[{{.window}}]))
```

## Designing the Consequences: The Tiered Escalation Policy

An SLO dashboard is useless without an agreed-upon policy that dictates consequences when error budgets are depleted. An Error Budget Policy (EBP) must be treated as a binding contract signed off by Product, Engineering, and SRE leadership.

We implement a four-tiered escalation matrix based on the remaining 30-day rolling error budget:

| Budget Status | Remaining Budget | CI/CD Pipeline Gate Behavior | Engineering Focus |
| :--- | :--- | :--- | :--- |
| **Green** | 100% - 20% | Fully automated deployments allowed | 100% Feature velocity |
| **Yellow** | 20% - 5% | Warning logged in deployment logs | Allocate 20% of next sprint to reliability backlog |
| **Orange** | 5% - 0.01% | Deployments restricted (requires SRE lead approval) | Block non-critical feature flags; focus on bug fixes |
| **Red** | $\le$ 0% | Automated deployment block enabled | 100% Engineering pivot to reliability restoration |

### Red Status Policy Execution
When a team's specific budget hits 0% (Red Status):
1. **Automated Blocking:** The CI/CD pipeline blocks all non-emergency deployments for that team's codebase/endpoints.
2. **The "Fix Only" Mandate:** The team is forbidden from shipping new business features or enabling new feature flags. They may only deploy code that directly mitigates the reliability issues.
3. **Escalation Path:** If a business-critical feature must be deployed despite a depleted budget, a formal "Policy Exception" must be requested. This exception requires written sign-off from the VP of Engineering or CTO, accompanied by a documented remediation plan with a maximum 7-day resolution window.

## Automating Enforcement: CI/CD Gates and Deployment Blockers

Manual policies fail during high-pressure releases. We must automate enforcement by integrating budget checks directly into the deployment pipeline.

We can write a bash script, `check_error_budget.sh`, which queries the Prometheus API to retrieve the remaining error budget ratio generated by Sloth (which exposes `slo:remaining_error_budget:ratio` metrics).

```bash
#!/usr/bin/env bash
# check_error_budget.sh
# Usage: ./check_error_budget.sh <team_name> <prometheus_url>

set -euo pipefail

TEAM=$1
PROM_URL=$2
SLO_NAME="${TEAM}-availability"

# Query Prometheus for the remaining error budget ratio (values range from -inf to 1.0)
QUERY="slo:remaining_error_budget:ratio{slo=\"$SLO_NAME\"}"

RESPONSE=$(curl -s -G \
  --data-urlencode "query=$QUERY" \
  "$PROM_URL/api/v1/query")

# Extract the float value from Prometheus JSON response using jq
REMAINING_BUDGET=$(echo "$RESPONSE" | jq -r '.data.result[0].value[1]')

if [[ "$REMAINING_BUDGET" == "null" ]] || [[ -z "$REMAINING_BUDGET" ]]; then
  echo "[-] Error: Metric not found for SLO: $SLO_NAME. Check telemetry pipeline."
  exit 1
fi

PERCENTAGE=$(echo "$REMAINING_BUDGET * 100" | bc -l)
printf "[+] Team '%s' has %.2f%% of their error budget remaining.\n" "$TEAM" "$PERCENTAGE"

# Check if budget is exhausted (<= 0)
if (( $(echo "$REMAINING_BUDGET <= 0" | bc -l) )); then
  echo "[!] CRITICAL: Error budget for team '$TEAM' is exhausted."
  echo "[!] Deployment blocked. Only emergency hotfixes are permitted."
  exit 1
fi

echo "[+] Error budget check passed. Continuing deployment."
exit 0
```

We integrate this script into our GitHub Actions deployment workflow:

```yaml
name: Production Deployment

on:
  push:
    branches: [main]

jobs:
  validate-reliability:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout Code
        uses: actions/checkout@v4

      - name: Query Prometheus Error Budget
        env:
          PROM_URL: "https://prometheus.internal.net"
        run: |
          chmod +x ./scripts/check_error_budget.sh
          # Assume this repository directory maps to the 'checkout' team
          ./scripts/check_error_budget.sh "checkout" "$PROM_URL"

  deploy:
    needs: validate-reliability
    runs-on: ubuntu-latest
    steps:
      - name: Trigger GitOps Deploy (ArgoCD)
        run: |
          echo "Deploying update to Kubernetes cluster..."
          # Deploy command goes here
```

If the Checkout team has exhausted their budget, the GitHub Action fails at the `validate-reliability` step, preventing the code from ever reaching the production cluster via ArgoCD.

## Edge Cases: Throttling, Connection Pools, and Low-Traffic Paths

Implementing multi-team error budgets introduces technical edge cases that can bypass or invalidate our metrics if left unaddressed.

### 1. Connection Pool Contention
If the Checkout team runs an unoptimized database query that consumes all database connections, the Subscriptions team will experience connection timeouts, and their SLO will also burn. 
* **Mitigation:** We must decouple shared resources. Configure separate PostgreSQL connection pools per module using thread/connection limits, or enforce gRPC rate-limiting and thread pools at the application layer. This ensures that resource exhaustion inside one team's module does not spill over and burn another team's budget.

### 2. The Low-Traffic Endpoint Problem
If the Refund team's path (`/v1/refunds`) only receives 100 requests per day, a single transient 500 error causes a 1% drop in availability. Under a 99.9% SLO (allowing 0.1% failures), a single error instantly breaches the budget.
* **Mitigation:** Avoid request-count SLOs for low-traffic paths. Instead, implement a **Time-Slice SLO** (or Window-Based SLO). A Time-Slice SLO measures the percentage of 5-minute intervals throughout the month that maintain a success rate above 99%. A single bad request will only fail one 5-minute slice (accounting for 0.05% of the monthly slices), keeping the budget intact for transient errors while still catching sustained outages.

### 3. Cascading Downstream Failures
If `payment-gateway` calls a downstream third-party processor (e.g., Stripe) and Stripe goes down, all teams will burn budget.
* **Mitigation:** Implement circuit breakers (e.g., using resilience4j or Envoy) and use metric filters to exclude downstream provider failures from internal team budgets, classifying them instead under a separate "External Dependencies" SLO.

## Production Case Study: Incident Response in a 15k RPS System

Let us examine how this architecture performed during an outage on a core financial platform.

### The System Setup
* **Service:** `ledger-gateway` (Go, running on AWS EKS)
* **Datastore:** PostgreSQL (Amazon Aurora)
* **Observability:** Prometheus, Grafana, OpenTelemetry SDK, Sloth.
* **Teams:** 
  * Checkout Team (Responsible for `/charges`)
  * Billing Team (Responsible for `/subscriptions`)

### The Incident
* **10:14 AM:** The Checkout team deployed a minor schema migration to optimize query speeds. They forgot to run `ANALYZE` after applying the index on a table containing 220 million records. 
* **10:16 AM:** The PostgreSQL query planner defaulted to a full table scan. The p95 response times for `/charges` spiked from 45ms to 4,200ms.
* **10:18 AM:** The database connection pool on `ledger-gateway` reached its max limit of 200 connections. Every database client thread was blocked waiting for a connection.
* **10:20 AM:** The connection exhaustion cascaded to the Billing team's `/subscriptions` path, causing connection timeouts. 

```
[10:14 AM] Deploy Index (Checkout)
     │
     ▼
[10:16 AM] DB Full Table Scan (Charges)
     │
     ▼
[10:18 AM] DB Conn Pool Exhausted (Shared Ledger Gateway)
     │
     ▼
[10:20 AM] Latency Spike on Billing Route (/subscriptions)
```

### The SRE Response and Policy Enforcement
1. **Targeted Paging:** The multi-window burn rate alert fired for `/charges` with a burn rate of 28.8. The alert bypassed the general platform team and paged the Checkout team's on-call engineer.
2. **Circuit Breaking and Throttling:** The `ledger-gateway` platform team had configured thread limits for database access per route. The gateway automatically began rejecting excessive requests on the `/charges` path with a `429 Too Many Requests` (which, under the SLI definition, does not count as a 5xx system error for the Billing team but counts as a budget burn for the Checkout team).
3. **Pipeline Lockdown:** The remaining error budget ratio for the Checkout team plummeted past 0% within 15 minutes of the incident. The CI/CD check for the Checkout team transitioned to Red, locking their pipeline.
4. **Billing Team Isolation:** Because of the active connection throttling at the application layer, the `/subscriptions` path recovered. The Billing team's remaining error budget stopped burning at 88%.
5. **Resolution:** The Billing team successfully deployed a critical regulatory billing patch at 11:00 AM without issue. Meanwhile, the Checkout team could only deploy a rollback. Their CI/CD pipeline allowed their rollback pull request to pass because the commit was tagged with an emergency override block in their Git history, bypassing the gate specifically for hotfixes.
6. **Post-Mortem:** The Checkout team was required to dedicate their entire next sprint to query optimization and query timeouts, restoring their error budget back to positive territory over the subsequent rolling window.

## Cultivating Shared Ownership: The Error Budget as Currency

The ultimate value of a multi-team Error Budget Policy is cultural. It shifts SRE from being the "gatekeeper of production" to being the provider of the safety net and telemetry tools.

By partitioning the budget:
* **Product Managers** now treat reliability as a finite resource. If they want to ship high-risk features, they must balance it by keeping their error budget positive.
* **SREs** are no longer involved in political debates about whether a deployment should happen. The metrics and the automated CI/CD script make the decision based on agreed-upon data.
* **Engineers** are empowered to take calculated risks. If their team has 90% of their error budget remaining, they can deploy complex refactors or run chaotic experiments. If their budget is at 2%, they know they must slow down and focus on telemetry, profiling, and database tuning.

Stop measuring reliability globally. Attribute your metrics, partition your budgets, automate your gates, and put the ownership of platform stability where it belongs: in the hands of the teams writing the code.