---
layout: post
title: "A Quantitative Framework for Tracking Codebase Entropy and API Surface Area Growth in Monorepos"
date: 2026-08-13 08:00:00 +0700
tags: [monorepo, software-architecture, software-metrics, codebase-entropy, engineering-management]
description: "A mathematically rigorous guide to tracking monorepo architectural decay, API surface drift, and package coupling using continuous static analysis."
image: "https://picsum.photos/seed/7265/1080/720"
thumbnail: "https://picsum.photos/seed/7265/400/300"
---

A single monorepo scaling from 10 to 100 backend engineers starts with high velocity, but almost always decays into a distributed monolith. As team sizes grow, package boundaries dissolve, circular import structures proliferate silently, and API surface areas inflate without oversight. Eventually, your build pipeline stretches from 4 minutes to 28 minutes, and a minor refactor in one domain cascades into broken contracts in three downstream services. This architectural degradation is code entropy in action. Instead of relying on gut feelings or arbitrary code reviews to stem this tide, we need a mathematical, reproducible framework that measures code decay in real-time, alerts on structural drift, and programmatically blocks regressive changes at the pull request level.

![A Quantitative Framework for Tracking Codebase Entropy and API Surface Area Growth in Monorepos Diagram](/images/diagrams/quantitative-framework-tracking-codebase-entropy-api-surface-area-growth-monorepos.svg)

## The Silent Decay of Scaling Monorepos

The appeal of the monorepo pattern—simplified dependency management, atomic commits across services, and unified tooling—is well understood. However, without continuous automated governance, it exhibits a natural thermodynamic progression toward disorder. In a multi-repo ecosystem, physical network boundaries and separate repositories enforce decoupling; in a monorepo, a developer is always just a relative import statement (`import ../../../billing/db`) away from bypassing architectural layers.

When these boundaries fail, three primary symptoms emerge in production:
1. **The Creeping Dependency Monolith**: Core shared utility libraries (e.g., logging, database wrappers) slowly import domain-specific code, creating cyclic import graphs that force the compiler to build the entire monorepo for every single micro-change.
2. **Silent API Surface Inflation**: Developers add parameters, duplicate routes, and nest response schemas without design reviews, creating bloated "God APIs" that are fragile and difficult to maintain.
3. **Leaky System Boundaries**: Modules that should have zero runtime dependencies on each other begin sharing memory states or direct database connections, leading to database lock contention and deployment lock-in.

To combat this, we must transition from subjective qualitative standards ("this PR looks too complex") to a quantitative engineering framework that evaluates every commit against mathematical formulas for codebase entropy, API complexity, and component instability.

## Deconstructing Monorepos: The Math of Codebase Entropy

In communication theory, Shannon entropy measures the average amount of information or uncertainty in a message. When applied to version control systems, we treat commit distributions across directories as a probability distribution. This metric, known as **Software Change Entropy**, indicates whether development effort is highly focused and modular (low entropy) or scattered and disorganized (high entropy).

If a Pull Request (PR) modifies code across 20 different packages, it suggests that these packages are tightly coupled. Conversely, if code modifications are localized to a single domain and its corresponding test suite, the change is cohesive.

Mathematically, the Shannon entropy $H(X)$ of a set of code modifications is defined as:

$$H(X) = - \sum_{i=1}^{n} P(x_i) \log_2 P(x_i)$$

Where:
* $n$ is the total number of distinct directories or packages modified in the monorepo during a given time window (or within a single PR).
* $P(x_i)$ is the probability of a change occurring in directory $i$, calculated as the ratio of lines of code changed (additions + deletions) in directory $i$ to the total lines of code changed across the entire codebase:

$$P(x_i) = \frac{\text{Lines Changed in } x_i}{\sum_{j=1}^{n} \text{Lines Changed in } x_j}$$

### Concrete Numerical Examples

Let us analyze two pull requests, each modifying exactly 100 lines of code across a monorepo containing five microservices: `billing`, `auth`, `users`, `shipping`, and a shared utility package `shared/utils`.

#### Scenario A: The Cohesive Change
A developer is implementing a new payment method. The modifications are localized to `billing` and its corresponding test files within the same directory:
* `services/billing/`: 95 lines changed
* `services/billing/tests/`: 5 lines changed

Applying the formula:
* $P(\text{billing}) = \frac{95}{100} = 0.95$
* $P(\text{billing/tests}) = \frac{5}{100} = 0.05$

$$H(X) = - \left( 0.95 \log_2(0.95) + 0.05 \log_2(0.05) \right)$$
$$H(X) \approx - \left( 0.95 \cdot (-0.074) + 0.05 \cdot (-4.322) \right)$$
$$H(X) \approx - \left( -0.0703 - 0.2161 \right) \approx 0.286 \text{ bits}$$

This low entropy value ($< 0.5$ bits) indicates that the changes are tightly scoped and highly localized.

#### Scenario B: The Leaky Change
A developer attempts to implement a cross-cutting logging feature but does so by modifying core components and domain services directly:
* `services/billing/`: 20 lines changed
* `services/auth/`: 20 lines changed
* `services/users/`: 20 lines changed
* `shared/utils/`: 20 lines changed
* `services/shipping/`: 20 lines changed

Applying the formula:
* $P(x_i) = 0.20$ for each of the 5 directories.

$$H(X) = - \sum_{i=1}^{5} 0.20 \log_2(0.20)$$
$$H(X) = - 5 \cdot \left( 0.20 \cdot (-2.3219) \right)$$
$$H(X) = 2.322 \text{ bits}$$

This high entropy value (approaching the theoretical maximum of $\log_2(5) \approx 2.32$ bits for a 5-element system) is an architectural red flag. It mathematically proves that a single logical change requires modifying almost the entire monorepo, signaling tight coupling.

We can capture these metrics programmatically by parsing git historical diffs. The function [`get_git_numstat`](file:///home/muklis/Documents/exploring/blog/scripts/monorepo_analyzer.py#L15) in the implementation script [monorepo_analyzer.py](file:///home/muklis/Documents/exploring/blog/scripts/monorepo_analyzer.py) demonstrates how to extract raw commit telemetry to calculate this value via [`calculate_shannon_entropy`](file:///home/muklis/Documents/exploring/blog/scripts/monorepo_analyzer.py#L48).

## Quantifying the API Surface Area

API surface area expansion is a major source of technical debt. When building distributed microservices within a monorepo, developers often expose HTTP endpoints, gRPC methods, or GraphQL schemas without considering the downstream impact. As the public surface area grows, the cost of testing, documentation, and contract maintenance scales superlinearly.

To monitor this, we define the **Weighted API Surface Area ($A$)** of a service:

$$A = \sum_{e \in E} \left( W_c(e) \cdot D_p(e) \right)$$

Where:
* $E$ is the set of all active public API endpoints.
* $W_c(e)$ is the schema complexity weight of endpoint $e$, calculated based on the number of query parameters, path variables, and the structural depth of the request/response payloads:

$$W_c(e) = 1 + N_{\text{params}} + D_{\text{payload}}$$

* $D_p(e)$ is the downstream dependency depth (or internal fan-out) of the endpoint handler. This measures how many distinct databases, third-party services, and internal monorepo packages the route handler imports or calls to fulfill a request.

An endpoint with a large payload schema ($W_c(e) = 15$) that queries three databases and calls two internal microservices ($D_p(e) = 5$) contributes $75$ units to the API surface area score. Conversely, a lightweight health check endpoint ($W_c(e) = 1$, $D_p(e) = 0$) contributes just $0$ units (or $1$ if using a base offset).

### Preventing Silent Schema Bloat
A common failure mode is the "God Request Payload," where a single endpoint accepts an unstructured JSON blob containing optional parameters for various sub-systems. This practice inflates $W_c(e)$ and makes client integrations fragile.

By enforcing continuous static analysis of API definitions (such as OpenAPI specifications, Protobuf files, or AST analysis of route definitions), teams can track the growth of $A$ over time. In our companion tool, the method [`analyze_python_ast`](file:///home/muklis/Documents/exploring/blog/scripts/monorepo_analyzer.py#L77) shows how static analysis can extract API decorators and calculate the basic complexity metrics of internal handlers to identify bloated endpoints.

## Component Coupling & Boundary Violations: The Instability Index

To prevent the monorepo from degenerating into a distributed monolith, we must enforce strict package boundaries. A standard method for quantifying package design is the use of the **Robert C. Martin Software Metrics**:

* **Afferent Coupling ($C_a$)**: The number of external packages within the monorepo that depend on classes or modules inside the target package. This measures how *responsible* the package is.
* **Efferent Coupling ($C_e$)**: The number of external packages inside the monorepo that the target package depends upon. This measures how *dependent* the package is.

From these metrics, we derive the **Instability Index ($I$)**:

$$I = \frac{C_e}{C_a + C_e}$$

The index ranges from $0$ to $1$:
* $I = 0$ (Maximum Stability): The package has high afferent coupling (many depend on it) and zero efferent coupling (it depends on nothing). It is extremely difficult to change because any modification propagates downstream. Examples include core primitives, configuration schemas, and domain event types.
* $I = 1$ (Maximum Instability): The package has zero afferent coupling (nothing depends on it) and high efferent coupling (it depends on other packages to function). It is easy to change because no downstream dependencies will break. Examples include orchestration handlers, API routers, and command-line entry points.

In a healthy monorepo, packages should align with the **Stable Dependencies Principle (SDP)**: *a package should only depend on packages that are more stable than itself.* This means dependency arrows must point in the direction of decreasing instability:

$$I_{\text{dependent}} \ge I_{\text{dependency}}$$

If a core package like `shared/db` ($I = 0.05$) imports a module from `services/billing` ($I = 0.90$), the instability rules are violated. This architectural regression can be caught programmatically by generating a dependency graph of the codebase at commit time and asserting that no boundary violations occur.

| Package Path | Afferent ($C_a$) | Efferent ($C_e$) | Instability ($I$) | Classification | Architectural Role |
| :--- | :---: | :---: | :---: | :--- | :--- |
| `shared/types` | 42 | 0 | 0.00 | Ultra-Stable | Core Domain Primitives |
| `shared/db_pool` | 18 | 1 | 0.05 | Stable | Infrastructure Adapter |
| `services/auth` | 5 | 4 | 0.44 | Balanced | Domain Service |
| `services/billing` | 1 | 9 | 0.90 | Instable | Consumer Application |
| `scripts/cli_tool` | 0 | 12 | 1.00 | Max Instability | Entrypoint / Script |

## Building the Quantitative Pipeline (Concrete Implementation)

To enforce this framework, we can build a lightweight static analysis pipeline that runs during CI pre-merge checks. The pipeline calculates the Shannon entropy of the incoming diff, scans the Abstract Syntax Tree (AST) of the changed modules, and generates the component coupling matrix.

Below is an architectural overview of how this pipeline executes inside a GitHub Actions environment:

```
[ Developer Push ] 
       │
       ▼
┌────────────────────────────────────────────────────────┐
│ CI Pipeline Agent                                      │
│                                                        │
│ 1. Git Diff Extraction (`git log --numstat`)          │
│ 2. Parse Code AST (Targeting API routing decorators)   │
│ 3. Compute Shannon Entropy (H) & Instability Index (I)  │
└────────────────────────────────────────────────────────┘
       │
       ├───► If H_PR > 1.5 OR I_core > 0.1 ───► [ Fail CI / Block Merge ]
       │
       ▼
┌────────────────────────────────────────────────────────┐
│ Prometheus Push Gateway                                │
│ (Stores metrics: entropy, endpoint counts, coupling)   │
└────────────────────────────────────────────────────────┘
       │
       ▼
┌────────────────────────────────────────────────────────┐
│ Grafana Dashboard                                      │
│ (Tracks historical code degradation & API growth rate) │
└────────────────────────────────────────────────────────┘
```

The script below shows how to write a custom runner to send these static analysis metrics directly to a time-series storage database, such as InfluxDB or Prometheus, allowing you to visualize code health over time:

```python
# Save to: /home/muklis/Documents/exploring/blog/scripts/ci_reporter.py
import time
from monorepo_analyzer import MonorepoAnalyzer

def send_metrics_to_tsdb(metrics: dict):
    """
    Publishes metrics to InfluxDB or Prometheus Pushgateway.
    This simulates standard TCP socket push patterns for CI metrics.
    """
    timestamp = int(time.time())
    print(f"[{timestamp}] Publishing Monorepo Metrics:")
    for metric_name, value in metrics.items():
        # Format as InfluxDB Line Protocol: measurement,tag=value field=value timestamp
        line = f"monorepo_metrics,env=ci {metric_name}={value} {timestamp}"
        print(f"  -> {line}")
        # In production, make a POST request to InfluxDB / Prometheus push-gateway:
        # requests.post("http://influxdb:8086/write", data=line)

if __name__ == "__main__":
    # Scan the local repository
    analyzer = MonorepoAnalyzer(".")
    metrics = analyzer.scan_monorepo()
    send_metrics_to_tsdb(metrics)
```

### Implementing CI/CD Gates
To enforce these boundaries, add a step in your pre-commit or CI config file (e.g., `.github/workflows/ci.yml`):

```yaml
name: Monorepo Governance
on: [pull_request]

jobs:
  analyze-structure:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0 # Full history required for git entropy calculations

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      - name: Run Static Analysis
        run: |
          python3 /home/muklis/Documents/exploring/blog/scripts/monorepo_analyzer.py . > metrics.json
          cat metrics.json

      - name: Assert Architectural Boundaries
        run: |
          ENTROPY=$(jq '.shannon_entropy_14d' metrics.json)
          AVG_COMPLEXITY=$(jq '.average_complexity' metrics.json)
          
          echo "Current PR Shannon Entropy: $ENTROPY"
          echo "Average Cyclomatic Complexity: $AVG_COMPLEXITY"
          
          # Block merge if entropy is too high without explicit architect approval
          if (( $(echo "$ENTROPY > 2.0" | bc -l) )); then
            echo "ERROR: Change distribution is too scattered (Entropy > 2.0). Refactor required."
            exit 1
          fi
          
          # Block merge if average file complexity exceeds a target threshold
          if (( $(echo "$AVG_COMPLEXITY > 15.0" | bc -l) )); then
            echo "ERROR: Code complexity exceeds maximum threshold of 15.0."
            exit 1
          fi
```

## Actionable Remediation & Architecture Governance

What should a team do when these automated gates fail a build? Simply raising exceptions in CI will frustrate developers if there are no clear paths to resolution. Here are three standard strategies for restoring order to a degraded monorepo:

### 1. Inversion of Control via Shared Interfaces
When a core package imports from a higher-level domain package, it creates circular dependencies and increases efferent coupling. To resolve this, extract the concrete domain implementation detail behind an interface and move that interface to a stable, shared package.

For example, if `shared/db` needs to publish events when a transaction completes, it should not import a concrete notifier package like `services/notifications/sns_client`. Instead, define an interface in a stable package:

```python
# In shared/interfaces/publisher.py (Stable: I = 0)
from typing import Protocol

class EventPublisher(Protocol):
    def publish(self, topic: str, payload: dict) -> None:
        ...
```

Then, inject the concrete implementation of `EventPublisher` at the application startup layer. This decouples `shared/db` from the notifier implementation, reducing its efferent coupling to zero.

### 2. Standardizing API Contracts with Linter Checks
To prevent API surface area drift, enforce strict schema validations. When changing schemas:
* Use tools like `buf` for Protobuf or `spectral` for OpenAPI linting to ensure no endpoints are added without description fields, and payload nesting depths are capped.
* Automatically flag any endpoint that expands parameter counts beyond a threshold (e.g., $N_{\text{params}} > 8$) as a target for refactoring.

### 3. Gamification and Visible Dashboards
Developers respond to feedback loops. Plotting entropy trends and instability metrics on team dashboards turns architectural health into a visible, trackable goal. When teams can see that their effort to break apart a monolithic class dropped their service's instability index from $0.85$ to $0.40$, it provides clear feedback on the value of their refactoring work.

By combining mathematical rigor with automated enforcement, engineering organizations can scale their monorepos past 100 developers without sacrificing code quality or velocity.

---

### Reference Files and Resources

* Complete implementation script: [monorepo_analyzer.py](file:///home/muklis/Documents/exploring/blog/scripts/monorepo_analyzer.py)
* CI integration and reporting helper: [ci_reporter.py](file:///home/muklis/Documents/exploring/blog/scripts/ci_reporter.py)
* Quantitative Pipeline Diagram: [quantitative-framework-tracking-codebase-entropy-api-surface-area-growth-monorepos.svg](file:///home/muklis/Documents/exploring/blog/images/diagrams/quantitative-framework-tracking-codebase-entropy-api-surface-area-growth-monorepos.svg)