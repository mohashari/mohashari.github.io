---
layout: post
title: "A Quantitative Framework for Deprecating Tier-1 APIs: Mapping Graph-Based API Dependencies to Customer SLIs"
date: 2026-07-22 08:00:00 +0700
tags: [microservices, api-design, site-reliability, platform-engineering]
description: "A production-tested blueprint using distributed tracing graphs and quantitative risk models to safely deprecate Tier-1 APIs without breaching customer SLIs."
image: "https://picsum.photos/seed/2277/1080/720"
thumbnail: "https://picsum.photos/seed/2277/400/300"
---

Imagine this: It is 3:00 AM on a Tuesday, and your pager is firing. P99 latency on your core checkout flow has spiked from 180 milliseconds to 4.2 seconds, and the checkout success rate has plummeted to 82%, breaching your 99.9% customer Service Level Indicator (SLI). The root cause? A seemingly innocuous API cleanup. The platform team had deprecated and shut down `/api/v1/checkout/tax-calculator` after a 30-day access log analysis showed zero direct traffic. What the log analysis missed was a legacy, un-documented batch service that called that endpoint transitively on behalf of high-value enterprise clients under a pooled connection header. Sunsetting API endpoints in a highly decoupled, distributed microservice architecture is a game of Russian roulette unless you map dependencies dynamically and calculate risk quantitatively. This post outlines an engineering framework that uses distributed tracing graphs and client-weighted risk equations to safely deprecate Tier-1 APIs without breaching customer-facing SLIs.

![A Quantitative Framework for Deprecating Tier-1 APIs: Mapping Graph-Based API Dependencies to Customer SLIs Diagram](/images/diagrams/quantitative-framework-deprecating-tier-1-apis-dependency-graphs-customer-slis.svg)

## The Fallacy of Static Log Scans and Client Surveys

Many platform engineering teams approach API deprecation with a naive checklist: query the last 30 days of Nginx/Envoy access logs, look for requests containing the target endpoint, and if the count is zero, delete the route. This strategy is fundamentally flawed and actively dangerous in a enterprise microservice environment.

Static access log analysis fails due to four primary production realities:

1. **Transitive Dependency Blindness**: If Service A calls Service B, and Service B calls the deprecated endpoint, the access logs on the deprecated endpoint will only record Service B as the client. You remain blind to the fact that Service A—and ultimately, a Tier-1 customer client using Service A—is the real upstream source. If Service B has silent retry logic or complex error handling, the disruption from shutting down the endpoint can propagate upstream in unpredictable ways, blowing past latency budgets.
2. **Dynamic Route Fallbacks**: Client-side applications, particularly native iOS and Android apps, often have hardcoded fallback logic. If their primary `/v2/` API endpoint fails or returns a specific error code, they fall back to a legacy `/v1/` path that hasn't been used in years. Static logs during normal operation will show zero requests, but the moment you decommission the legacy path, you break their self-healing mechanism.
3. **Infrequent and Seasonal Workloads**: Many critical business workloads run on weekly, monthly, or quarterly schedules. A 30-day access log window completely misses a quarterly financial reconciliation job or an annual tax-reporting process. If these jobs execute against the deprecated route, they will fail silently or crash, resulting in delayed financial closes or regulatory non-compliance.
4. **The "Developer Whisperer" Problem**: Surveying internal engineering teams about their usage of an endpoint is subjective and unreliable. Engineers change teams, legacy codebases lose their owners, and documentation drifts. Production truth is only found in the runtime dependency graph.

To safely deprecate a Tier-1 API, we must move away from static, point-in-time analysis and adopt a dynamic, quantitative approach that continuously maps the live call graph to customer-facing SLIs.

## Building the API Dependency DAG from Tracing Data

The foundation of our framework is a Directed Acyclic Graph (DAG) built from live distributed tracing data. By extracting trace context across our service mesh, we can reconstruct the exact call chain from the edge gateway down to the leaf databases and third-party APIs.

In a modern microservice architecture, every request entering the system is assigned a unique trace identifier (e.g., W3C `traceparent` headers containing `version-trace_id-parent_id-trace_flags`). As this request traverses downstream services via HTTP/gRPC, the trace context is propagated. By ingesting these spans into a central telemetry warehouse, we can model our system as a directed graph where:

* **Nodes ($V$)**: Represent unique API endpoints, defined by service name, HTTP method, and path template (e.g., `inventory-service:GET /items/{id}`).
* **Edges ($E$)**: Represent direct call relationships between endpoints. Edges carry properties such as call rate (RPS), error rates, and p99 latencies.

The following Python script demonstrates how to parse a stream of tracing span metadata and construct a dependency graph using the NetworkX library to identify all transitively dependent upstream consumers of a target deprecated endpoint:

```python
import networkx as nx

def build_dependency_graph(spans):
    """
    Constructs a directed graph from raw tracing spans.
    spans: A list of dicts containing 'parent_endpoint', 'child_endpoint', and 'rps'.
    """
    G = nx.DiGraph()
    for span in spans:
        parent = span['parent_endpoint']
        child = span['child_endpoint']
        rps = span.get('rps', 0.0)
        
        if G.has_edge(parent, child):
            G[parent][child]['weight'] += rps
        else:
            G.add_edge(parent, child, weight=rps)
    return G

def find_transitive_callers(graph, target_endpoint):
    """
    Finds all upstream nodes that transitively depend on the target endpoint.
    """
    if not graph.has_node(target_endpoint):
        return set()
    # nx.ancestors returns all nodes that can reach target_endpoint
    return nx.ancestors(graph, target_endpoint)

# Example Usage
raw_spans = [
    {"parent_endpoint": "api-gateway:GET /checkout", "child_endpoint": "checkout-service:POST /charge", "rps": 150.0},
    {"parent_endpoint": "checkout-service:POST /charge", "child_endpoint": "payment-service:POST /v1/auth", "rps": 150.0},
    {"parent_endpoint": "payment-service:POST /v1/auth", "child_endpoint": "legacy-token-service:POST /validate", "rps": 150.0},
    {"parent_endpoint": "batch-billing:cron", "child_endpoint": "payment-service:POST /v1/auth", "rps": 1.2},
    {"parent_endpoint": "api-gateway:GET /summary", "child_endpoint": "reporting-service:GET /report", "rps": 5.0},
]

dag = build_dependency_graph(raw_spans)
target = "legacy-token-service:POST /validate"
callers = find_transitive_callers(dag, target)

print(f"Transitive upstream callers for '{target}':")
for caller in callers:
    print(f"  - {caller}")
```

For large-scale production environments, querying trace stores like Jaeger, Zipkin, or OpenSearch directly for every check is highly inefficient. Instead, we can sync trace structures to a graph database like Neo4j. This allows platform teams to write Cypher queries that trace complex paths in real-time. For instance, to find all paths from an edge gateway to a target deprecated service:

```cypher
MATCH path = (g:GatewayRoute)-[*]->(s:ServiceEndpoint {name: 'legacy-token-service', path: '/validate'})
RETURN path, length(path) as path_length
ORDER BY path_length DESC
```

This graph database acts as the single source of truth for service topology, enabling us to calculate risk dynamically as production traffic shifts.

## The Quantitative Risk Score (QRS) Formula

Once the dependency graph is established, we can calculate a **Quantitative Risk Score (QRS)** for any endpoint designated for deprecation. We define this score mathematically to capture three vectors of production risk: client importance, traffic volume, latency budget safety margin, and failure propagation probability.

For a target deprecated endpoint $E$, the $QRS(E)$ is defined as:

$$QRS(E) = \sum_{c \in C(E)} W_c \cdot \log_{10}(RPS_c + 1) \cdot \left(1 + \frac{L_{dep}(E)}{L_{budget}(c) - L_{actual}(c)}\right) \cdot P_{fail}(c \to E)$$

Where:

* $C(E)$ is the set of all unique root client applications or gateway routes that transitively depend on endpoint $E$.
* $W_c$ is the **Client Tier Weight**. Clients are categorized into tiers based on business critical agreements:
  * Tier-1 (VIP / High-revenue Enterprise): $W_c = 10.0$
  * Tier-2 (Standard SMB / Public API): $W_c = 5.0$
  * Tier-3 (Internal automation / non-blocking asynchronous jobs): $W_c = 1.0$
  * Tier-4 (Sandbox / Developer Test Environments): $W_c = 0.1$
* $RPS_c$ is the average requests per second generated by client $c$ that transitively traverse through $E$ (evaluated at p95 peak).
* $L_{dep}(E)$ is the average execution latency of the deprecated endpoint $E$.
* $L_{budget}(c)$ is the maximum end-to-end latency SLA/SLO allocated to client $c$.
* $L_{actual}(c)$ is the current baseline end-to-end latency of client $c$ under normal conditions.
* $P_{fail}(c \to E)$ is the **Failure Propagation Probability**. This is the probability that a failure at endpoint $E$ results in an unhandled failure or timeout at client $c$. If $c$ has robust circuit-breakers and gracefully falls back to a cached state or alternative route, $P_{fail} \approx 0.0$. If the connection is a hard block where a failure at $E$ crashes $c$, $P_{fail} = 1.0$.

### Concrete Numerical Walkthrough

Let us evaluate the risk of deprecating the endpoint `E = /api/v1/tax-calculator`. Two distinct clients call upstream services that transitively depend on this endpoint.

#### Client 1 ($c_1$): Enterprise Mobile App ($W_c = 10.0$)
* Transitive $RPS_{c_1}$ = 120.0
* Latency Budget $L_{budget}(c_1)$ = 800ms
* Actual Baseline Latency $L_{actual}(c_1)$ = 300ms
* Deprecated Endpoint Latency $L_{dep}(E)$ = 50ms
* Failure Propagation $P_{fail}(c_1 \to E)$ = 1.0 (hard dependency, no fallback)

Calculating the risk components for Client 1:

$$Risk(c_1) = 10.0 \cdot \log_{10}(120 + 1) \cdot \left(1 + \frac{50}{800 - 300}\right) \cdot 1.0$$

$$Risk(c_1) = 10.0 \cdot 2.0827 \cdot (1 + 0.1) \cdot 1.0 = 22.91$$

#### Client 2 ($c_2$): Internal Billing Dashboard ($W_c = 1.0$)
* Transitive $RPS_{c_2}$ = 0.5
* Latency Budget $L_{budget}(c_2)$ = 5000ms
* Actual Baseline Latency $L_{actual}(c_2)$ = 1200ms
* Deprecated Endpoint Latency $L_{dep}(E)$ = 50ms
* Failure Propagation $P_{fail}(c_2 \to E)$ = 0.1 (wrapped in a background retry queue with fallback values)

Calculating the risk components for Client 2:

$$Risk(c_2) = 1.0 \cdot \log_{10}(0.5 + 1) \cdot \left(1 + \frac{50}{5000 - 1200}\right) \cdot 0.1$$

$$Risk(c_2) = 1.0 \cdot 0.1761 \cdot (1 + 0.0132) \cdot 0.1 = 0.0178$$

#### Total Risk Assessment

Summing the risk values:

$$QRS(E) = 22.91 + 0.0178 = 22.9278$$

We categorize the QRS results into operational action bands:

* **$QRS(E) \geq 10.0$ (Critical Risk)**: Deprecation must be blocked. The platform team must coordinate manual migration of high-tier clients to successor endpoints.
* **$1.0 \leq QRS(E) < 10.0$ (Medium Risk)**: Deprecation can proceed only with warning headers and direct customer alerting.
* **$QRS(E) < 1.0$ (Low Risk)**: Automated deprecation execution policies may be initiated.

Because our calculated $QRS(E)$ is **22.93**, attempting to decommission `/api/v1/tax-calculator` would immediately trigger a critical risk block, preventing an outage.

## Execution Policies: Automated Brownouts and Header Injection

Once the risk of a deprecated endpoint drops below the low-risk threshold ($QRS < 1.0$), we shift from passive observation to active enforcement. Never drop traffic instantly. A structured, multi-phase execution policy forces clients to migrate without triggering catastrophic failures.

```
       [ QRS < 1.0 ]
             │
             ▼
┌──────────────────────────┐
│   Phase 1: Header Inj.   │  <-- Inject Deprecation & Sunset Headers
└────────────┬─────────────┘
             │  (Duration: 30 Days)
             ▼
┌──────────────────────────┐
│   Phase 2: Brownouts     │  <-- Inject Latency & 429 Too Many Requests
└────────────┬─────────────┘
             │  (Duration: 14 Days)
             ▼
┌──────────────────────────┐
│   Phase 3: Final Sunset  │  <-- Hard Drop to 410 Gone / 503 Unavailable
└──────────────────────────┘
```

### Phase 1: Contextual Header Injection (RFC 8594)

The first phase involves injecting standardized metadata headers into the responses of all deprecated endpoints. This signals downstream services and developer frameworks that they are consuming a legacy resource.

As defined in RFC 8594, we inject:
* `Deprecation: true`: Indicates the resource is deprecated.
* `Sunset: Thu, 31 Dec 2026 23:59:59 GMT`: The date the endpoint will be decommissioned.
* `Link: <https://api.company.com/docs/migrations/v2-checkout>; rel="successor-version"`: Points to migration documentation.

This configuration is applied at the gateway or service mesh proxy level. Below is an Envoy routing rule configured to dynamically inject these headers for our legacy tax calculator endpoint:

```yaml
route_config:
  name: legacy_routes
  virtual_hosts:
    - name: api_v1
      domains: ["api.company.com"]
      routes:
        - match: { prefix: "/api/v1/checkout/tax-calculator" }
          route:
            cluster: tax_calculator_service
            response_headers_to_add:
              - header:
                  key: "Deprecation"
                  value: "true"
              - header:
                  key: "Sunset"
                  value: "Thu, 31 Dec 2026 23:59:59 GMT"
              - header:
                  key: "Link"
                  value: "<https://api.company.com/docs/migrations/v2-checkout>; rel='successor-version'"
```

Downstream services instrumented with modern HTTP clients can parse these headers and bubble up metrics warnings or sentry alerts to developer dashboards, automating notifications to target engineering teams.

### Phase 2: Active Brownouts (Chaos Injection)

Despite warnings, some teams will not migrate until their software actively degrades. Active brownouts are intentional, controlled windows of simulated unavailability or latency designed to flush out un-migrated systems.

We configure brownouts with a progressive degradation timeline:
1. **Week 1 (Micro-latency)**: Inject an artificial 1000ms latency to 10% of incoming requests on the deprecated endpoint. This triggers latency alerts in non-migrated upstream callers and tests their fallback and timeout resilience.
2. **Week 2 (Micro-failures)**: Return `429 Too Many Requests` on 15% of requests during specific 30-minute windows.
3. **Week 3 (Full Outage Simulation)**: Return `503 Service Unavailable` on 100% of requests for a 2-hour window during peak traffic hours.

We configure these dynamic faults using Envoy’s fault injection filter:

```yaml
http_filters:
  - name: envoy.filters.http.fault
    typed_config:
      "@type": type.googleapis.com/envoy.extensions.filters.http.fault.v3.HTTPFault
      delay:
        percentage:
          numerator: 10
          denominator: HUNDRED
        fixed_delay: 1.000s
      abort:
        percentage:
          numerator: 15
          denominator: HUNDRED
        http_status: 429
```

This active failure state breaks the complacency of downstream consumers, forcing engineering teams to prioritize the migration before the final sunset.

### Phase 3: Final Decommissioning

Once all active brownouts pass without incident and the live graph registers zero traffic, the endpoint route is updated to return a permanent `410 Gone` status code. The underlying service nodes, container deployments, and database replicas are safely scaled to zero.

## Production Failure Case Studies

Developing this framework was a response to several major production incidents that occurred under traditional deprecation practices.

### Case Study 1: The Invisible DNS Cache & Client Fallback Loop

Our engineering team once attempted to deprecate a legacy inventory query service (`v1-inventory`). Access logs had shown absolute zero traffic for three consecutive months. The platform team deleted the route configurations at the Envoy ingress.

Within ten minutes of the deploy, p99 latencies on the customer-facing mobile search API spiked from 120ms to over 8.5 seconds.

**The Post-Mortem**: A legacy Android client app had hardcoded client-side resiliency. If the primary `/v2/inventory` endpoint returned a timeout (which occurred briefly due to a routine database lock swap), the client fell back to the old `/v1/inventory` domain. Because the client app cached the DNS records indefinitely, it bypassed the CDN and went directly to the legacy backend path. When this legacy route returned a `404 Not Found` instead of inventory data, the app got stuck in an infinite retry loop, creating a self-inflicted Distributed Denial of Service (DDoS) attack that overwhelmed the search gateway.

Had the trace graph been actively mapping fallback dependencies, the existence of the client fallback path would have been discovered during trace reconstruction, and the API would not have been deleted before the legacy Android version was force-retired.

### Case Study 2: The Quarterly Ledger Reconciliation Outage

A financial engineering team deprecating `/api/v1/ledger/balance` verified that zero requests were received over a 60-day logging window. They decommissioned the database instance backing this route.

Eighty-eight days later, at the end of the fiscal quarter, the automated financial settlement pipelines kicked off. The main ledger reconciliation script, which runs strictly on a 90-day cycle, attempted to make a batch call to the deprecated balance service. The script crashed, halting credit card settlement payouts globally for 14 hours and costing the business over $340,000 in regulatory delay fees.

**The Mitigation**: Following this incident, we implemented the graph-based tracing system described above. Rather than relying on simple access logs, we configured our OpenTelemetry database to persist metadata traces of infrequently called paths for up to 365 days. The risk assessor now flags any endpoint that has been called within the last 12 months, regardless of current traffic volume.

## Step-by-Step Implementation Guide

To set up this quantitative deprecation framework, execute the following technical plan:

### Step 1: Instrument Distributed Tracing

Ensure that all services propagate the standard W3C telemetry context headers. In a Go/gRPC backend, import the OpenTelemetry instrumentation package:

```go
import (
    "go.opentelemetry.io/otel"
    "go.opentelemetry.io/otel/propagation"
)

// Configure global propagator
otel.SetTextMapPropagator(propagation.NewCompositeTextMapPropagator(
    propagation.TraceContext{},
    propagation.Baggage{},
))
```

### Step 2: Establish the Topology Graph Engine

Deploy a daily processing script (such as a Spark or ClickHouse query engine) that pulls raw HTTP/gRPC span connections and imports them into a Neo4j instance or NetworkX pipeline.

Compute edge properties by aggregating telemetry:
```sql
SELECT 
    parent_service, 
    child_service, 
    count(*) / 86400 AS avg_rps
FROM otel_spans
WHERE timestamp >= NOW() - INTERVAL 7 DAY
GROUP BY parent_service, child_service;
```

### Step 3: Configure the Risk Assessor CronJob

Create a Kubernetes CronJob that executes the QRS formula daily. The job reads the deprecated API catalog from your service discovery mechanism (e.g., Consul or a custom dashboard database), matches endpoints to the topology database, and writes the resulting risk scores to a dashboard.

If any endpoint registers a $QRS \geq 1.0$, the job sends an automated alert to the service owner and locks the corresponding deprecation PR in your GitOps pipeline.

### Step 4: Automate the Envoy Brownout Filters

Connect your GitOps pipeline to Envoy configuration generation. If the QRS for a route drops below 1.0, automatically trigger a pipeline to output the Phase 1 Header Injection configurations, followed by Phase 2 Fault Injection files.

```bash
# Example command in GitOps to render the Envoy config with brownout policies
python render_envoy_config.py --envoy-template=gateway.yaml.j2 --risk-data=qrs_report.json
```

## Conclusion

Deprecating legacy software is as critical to velocity as launching new features. Leaving dead APIs running indefinitely inflates compute costs, expands the system's attack surface, and increases cognitive overhead for product teams. However, cleanups cannot come at the expense of client stability and business SLAs.

By mapping API dependencies through live, graph-based tracing graphs and scoring deprecation risk with a strict, quantitative formula, you remove the guesswork from platform engineering. Automated brownouts and contextual header injections ensure that residual connections are highlighted and resolved gracefully. Implement these systems to keep your architecture lean, your microservices clean, and your pager silent.