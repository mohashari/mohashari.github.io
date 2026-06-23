---
layout: post
title: "Establishing RFC Templates and Architectural Decision Record (ADR) Workflows in Multi-Team Organizations"
date: 2026-06-23 08:00:00 +0700
tags: [engineering-leadership, system-design, documentation]
description: "Learn how to build a low-overhead, high-impact RFC and ADR workflow that aligns engineering teams and prevents technical archaeology in production."
image: "https://picsum.photos/seed/4021/1080/720"
thumbnail: "https://picsum.photos/seed/4021/400/300"
---

Picture this: your high-throughput payment processing service starts experiencing intermittent database connection pool exhaustion under a 3x traffic spike. As a senior backend engineer tasked with fixing it, you dive into the code and find a custom connection pool manager written in-house, coupled with a complex retry mechanism. There is no documentation, and the developer who wrote it left the company eighteen months ago. You search slack histories, git logs, and wiki pages, only to find a trail of fragmented conversations. Why did the team implement a custom manager instead of using `pgxpool` or `sql.DB` native pooling? Why did they choose transaction-level pooling over session pooling at the proxy level? You are forced into "architecture archaeology," spending days reverse-engineering historical contexts before writing a single line of code. This friction is a direct consequence of a systemic organizational failure: the lack of structured technical governance to capture and communicate design decisions.

![Establishing RFC Templates and Architectural Decision Record (ADR) Workflows in Multi-Team Organizations Diagram](/images/diagrams/establishing-rfc-templates-architectural-decision-records-adr-workflows.svg)

In a scaling multi-team engineering organization, tribal knowledge is the first thing that breaks. As the engineering department grows past 30-50 developers, split across isolated product lines or domain-driven platforms, alignment degrades into fragmentation. Teams start solving the same infrastructure problems in silos, choosing differing database solutions (e.g., PostgreSQL vs. DynamoDB for time-series data) and diverging API patterns (e.g., REST vs. gRPC for internal communications). To combat this decay, tech leads must establish a lightweight, highly structured, and automated governance workflow using two distinct documentation patterns: Request for Comments (RFCs) for macro-level strategic planning, and Architectural Decision Records (ADRs) for micro-level codebase changes.

## The Architectural Governance Hierarchy: RFCs vs. ADRs

A major failure mode in governance implementation is conflating RFCs and ADRs. When teams treat them as the same document type, they end up with a single workflow that is either too heavy for quick technical decisions or too weak for organizational alignment. A clear distinction in scope, lifecycle, and residency is required to keep documentation friction low and adoption high.

| Dimension | Request for Comments (RFC) | Architectural Decision Record (ADR) |
| :--- | :--- | :--- |
| **Scope** | Global, cross-team, high-impact (e.g., moving from a monolith to distributed services, adopting a central schema registry). | Local, repository-specific, implementation-focused (e.g., choosing `uuidv7` as primary keys, disabling prepared statements). |
| **Lifecycle** | Collaborative, transient, and high-churn during review. Becomes an archive reference once approved. | Immutable, version-controlled, and living alongside the source code. |
| **Primary Audience** | Tech leads, engineering directors, security engineers, product managers, and infrastructure teams. | Developers actively modifying that specific codebase. |
| **Residency** | Centralized repository (e.g., an `rfc` GitHub repo, Notion database, or developer portal like Backstage). | Inside the target code repository under a `docs/adr/` directory. |
| **Process Overhead** | High. Requires asynchronous feedback windows, formal meetings, and a defined review committee. | Low. Written as markdown, reviewed as part of a pull request, and merged alongside the code. |

An RFC is a proposal for a change that does not yet exist. It is a communication tool designed to build consensus, surface hidden risks (e.g., database lock thrashing or cross-team database dependencies), and ensure security and compliance checks are satisfied before investing engineering time. Conversely, an ADR is a record of a decision that has been made and implemented. It serves as the definitive source of truth for the codebase’s history, documenting the context, decisions, and consequences.

## Designing a Pragmatic RFC Template

An RFC template must strike a balance between thoroughness and developer velocity. If the template is too long, engineers will avoid drafting them, preferring to build in silence and present a fait accompli. If the template is too short, critical technical considerations will be missed. 

A production-grade RFC template should contain the following core sections:

```markdown
# RFC [Number]: [Descriptive Title]

* **Author(s):** [Name, Team]
* **Status:** Draft | Under Review | Approved | Rejected | Deprecated
* **Target Date:** [Expected implementation start date]
* **Reviewers:** [Lead Engineers, Security, Infrastructure Reps]

## 1. Executive Summary
A concise, three-to-five sentence summary of the problem, the proposed solution, and the anticipated impact on the business. This section should be readable by product managers and engineering leaders.

## 2. Context & Problem Statement
Detail the background of the issue. Use concrete metrics to justify the change:
- What are the current system constraints? (e.g., \"Write throughput on the `invoices` table is capped at 1,200 write ops/sec due to disk IOPS limits on r6g.xlarge PostgreSQL instances.\")
- What is the business impact? (e.g., \"Transactions are failing with 504 timeouts during promotional hours, costing $15,000 per hour in abandoned checkouts.\")
- Provide links to log traces, monitoring dashboards, or bug reports.

## 3. Proposed Solution & Architecture
A detailed technical deep-dive of the proposed changes. 
- **High-level Architecture:** Include component diagrams showing how systems interact. Use standard ASCII art or SVG diagrams.
- **Data Model Changes:** Include schema migrations, indexing strategies, or serialization changes (e.g., JSON payload examples for REST or Proto files for gRPC).
- **Failure Modes:** Explicitly list what happens when dependent systems fail. (e.g., \"If Kafka is unavailable, the Outbox table will accumulate jobs up to 10M rows before backing up checkout transactions.\")

## 4. Alternatives Considered
List at least two alternative approaches and explain why they were rejected.
- **Alternative A:** Detail the implementation, cost, and complexity.
- **Alternative B (Status Quo):** What happens if we do nothing? (e.g., \"If we do not shard the table, the database B-Tree indexes will no longer fit in memory by Q4 2026, leading to full table scans.\")

## 5. Security, Compliance, & Observability
- **Data Privacy:** Does this system touch PII or payment data? How is data encrypted at rest and in transit?
- **IAM Policies:** What roles are required? (Enforce the principle of least privilege).
- **Monitoring:** Define the core Golden Signals (Latency, Traffic, Errors, Saturation) and what custom Prometheus metrics will be exposed.

## 6. Migration, Rollout, & Rollback Strategy
How do we deploy this change without downtime?
- **Migration Plan:** Outline the steps for data backfills or schema updates. (e.g., \"Phase 1: Dual-write to Postgres and DynamoDB. Phase 2: Backfill historical data via Spark. Phase 3: Switch reads to DynamoDB. Phase 4: Deprecate Postgres writes.\")
- **Feature Flags:** How will the rollout be throttled? (e.g., \"Canary deployment starting at 1% of user traffic, incrementing by 10% daily.\")
- **Rollback Criteria:** What metrics trigger an automatic rollback? (e.g., \"If HTTP 5xx errors exceed 0.05% of total requests on the payment gateway, disable the feature flag.\")
```

One of the most common failure modes in RFC drafting is ignoring the **Migration & Rollout Strategy**. Teams frequently design elegant greenfield architectures but completely miss the operational realities of migrating hot data structures under production load. Including these sections as mandatory fields forces developers to think about zero-downtime execution from day one.

## Establishing the RFC Lifecycle and Review Workflow

To prevent RFCs from stalling in endless review loops, you must establish a clear lifecycle with strict time boundaries. A structured RFC process follows four key phases.

### Phase 1: Drafting (The Author)
The author creates a markdown file based on the template. At this stage, the document is in the `Draft` state. The author should socialize the draft informally with immediate team members to validate basic design assumptions.

### Phase 2: Asynchronous Review (The Quiet Window)
Once the author is confident in the design, they update the status to `Under Review` and submit a Pull Request to the centralized `rfc` repository. A notification is automatically dispatched to the engineering organization's messaging channel (e.g., Slack or Teams).
- **The 7-Day Timebox:** The review window is strictly limited to 7 business days. 
- **Silence is Consent:** If a reviewer does not leave feedback within the 7-day window, they forfeit their veto power. This forces reviewers to prioritize critical RFCs and prevents designs from stalling.

### Phase 3: The Architecture Review Board (ARB)
For high-impact, cross-team proposals, an asynchronous PR review is not enough to resolve architectural trade-offs. Organizations should set up an Architecture Review Board (ARB) — a weekly or bi-weekly 60-minute meeting attended by tech leads, principal engineers, and domain architects.
- **No Presentation Rule:** Reviewers must read the RFC *before* the meeting. The meeting starts immediately with discussion, Q&A, and debate. If a reviewer has not read the document, they are excused from the session. This keeps the meeting focused and prevents it from devolving into a passive slide deck presentation.
- **Resolving Deadlocks (DACI):** To prevent gridlock, every RFC must identify a single **Decider** (typically the tech lead of the owning team). The Decider’s job is not to build 100% consensus — which is impossible — but to listen to feedback, make a call, and move the organization forward.

### Phase 4: Resolution & Archive
An RFC terminates in one of two states:
- **Approved:** The PR is merged into the `rfc` repository, and the document is updated with an `Approved` header, along with a summary of the decision. The engineering teams can now begin implementation.
- **Rejected:** The RFC is merged with a `Rejected` status. *Do not delete rejected RFCs.* They contain valuable historical context regarding paths that were explored and why they were deemed unviable. Saving them prevents future engineers from repeating the same failed experiments.

## Transitioning from Strategy to Execution: The ADR Workflow

Once an RFC is approved, the high-level design must be translated into codebase-level implementations. This is where Architectural Decision Records (ADRs) come into play. While the RFC lives in a central repository, the ADR is stored inside the code repository itself under `docs/adr/`.

Storing ADRs alongside the code guarantees that documentation is version-controlled and subject to the exact same peer-review standards as the code itself. If a developer submits a PR that changes the database connection pool configuration, they must submit a matching ADR in the same PR.

### The ADR Structure
A standard ADR uses the Markdown Architectural Decision Records (MADR) format. It must be highly structured, concise, and fit on a single page.

```markdown
# ADR-0024: Use PgBouncer in Transaction Pooling Mode for Payment Service

* **Status:** Proposed | Accepted | Rejected | Superceded by [ADR-XXXX]
* **Date:** 2026-06-23
* **Context:** [GitHub PR #1042](https://github.com/my-org/payment-service/pull/1042)

## Context and Problem Statement
Our payment microservice is deployed as a stateless API in Kubernetes. Under peak workloads (e.g., seasonal promotions), the service scales from 10 to 60 replicas. Each replica initiates an internal connection pool with a maximum limit of 20 connections. 

At 60 replicas, this results in up to 1,200 active connections to our PostgreSQL instance (`db.r6g.xlarge`, 4 vCPUs, 32GB RAM). PostgreSQL allocates roughly 10MB of memory per backend process, resulting in 12GB of RAM consumed solely by idle connections, degrading query execution performance and triggering memory alerts.

## Decision Drivers
1. **Memory Overhead:** Reduce the memory footprint of idle database connections.
2. **Horizontal Scalability:** Support scaling to 100+ replicas without crashing the database.
3. **Transaction Safety:** Ensure compatibility with our current Go transaction structure.

## Considered Options
1. **Option 1: Scale Database Instance:** Increase instance size to `db.r6g.2xlarge` to support more connections.
2. **Option 2: Session Pooling (PgBouncer):** Pool connections but bind them to the lifecycle of the client session.
3. **Option 3: Transaction Pooling (PgBouncer):** Pool connections and release them back to the pool as soon as a database transaction finishes.

## Pros and Cons of Options

### Option 1: Scale Database Instance
* **Pro:** No additional network hop or infrastructure dependencies.
* **Con:** Costs double from $720/mo to $1,440/mo.
* **Con:** Does not solve the underlying connection inefficiency; memory is still wasted.

### Option 2: Session Pooling (PgBouncer)
* **Pro:** Fully transparent to the client application.
* **Con:** Does not reduce connection counts when replicas remain idle but keep connections open.

### Option 3: Transaction Pooling (PgBouncer) (Chosen)
* **Pro:** High efficiency. Allows 100+ application replicas to share a small pool of 50 physical Postgres connections.
* **Pro:** Reduces PostgreSQL memory footprint by 80% (from 12GB to under 2GB).
* **Con:** Breaks PostgreSQL features that rely on session state: Prepared Statements, Temporary Tables, and `LISTEN/NOTIFY` commands.

## Consequences
By adopting Transaction Pooling, we must disable prepared statements in our database driver configuration:
- In Go (`pgx`), we must configure the connection config string with `prefer_simple_protocol=true` or set `ConnConfig.BuildStatementCache` to `nil` to prevent driver crashes due to missing statement descriptors on reused pool sessions.
- Developers must not use temporary tables (`CREATE TEMP TABLE`) inside transactions.
```

This ADR format is direct, highly technical, and immediately outlines the consequences of the decision. Any engineer opening this repository six months later will instantly understand the architectural constraints and avoid trying to enable prepared statements to optimize queries.

## Automating and Scaling the Workflows

To ensure compliance across multi-team organizations, you cannot rely solely on developers remembering to follow the rules. You must automate the enforcement of the RFC and ADR processes through CI/CD pipelines and developer portals.

### 1. Automating ADR Validation in CI
Use a GitHub Actions workflow (or GitLab CI equivalent) to enforce basic ADR health on every pull request. Below is a production-grade CI script that:
1. Enforces the MADR naming convention: `docs/adr/NNNN-[kebab-case-title].md`.
2. Validates that the ADR contains required headers (Status, Context, Decision, Consequences).
3. Ensures that new ADR files are committed when key system configuration files (e.g., Terraform scripts, database migration files, or driver configurations) are modified.

```yaml
name: "ADR Guardrail and Linter"

on:
  pull_request:
    paths:
      - 'docs/adr/**'
      - 'db/migrations/**'
      - 'terraform/**'
      - 'go.mod'
      - 'Cargo.toml'

jobs:
  validate-adr:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout Code
        uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - name: Check ADR Naming Convention
        run: |
          # Loop through modified/added files in docs/adr
          for file in $(git diff --name-only --diff-filter=AM origin/main...HEAD | grep '^docs/adr/'); do
            filename=$(basename "$file")
            # Enforce 4-digit index followed by lowercase kebab-case
            if [[ ! "$filename" =~ ^[0-9]{4}-[a-z0-9\-]+\.md$ ]]; then
              echo "Error: File '$file' does not match naming convention: docs/adr/YYYY-kebab-case-title.md"
              exit 1
            fi
            
            # Enforce presence of critical headers
            for header in "Status" "Context" "Decision" "Consequences"; do
              if ! grep -q "^## $header" "$file" && ! grep -q "^\* \*\*$header" "$file"; then
                echo "Error: File '$file' is missing required section: '$header'"
                exit 1
              fi
            done
          done

      - name: Verify ADR Submission for Infrastructure/Schema Changes
        run: |
          # If migration or terraform files modified, check if an ADR was added/modified
          MIGRATION_CHANGES=$(git diff --name-only origin/main...HEAD | grep -E '(db/migrations/|terraform/|go.mod)')
          ADR_CHANGES=$(git diff --name-only origin/main...HEAD | grep '^docs/adr/')
          
          if [ -n "$MIGRATION_CHANGES" ] && [ -z "$ADR_CHANGES" ]; then
            echo "Warning: Database schemas, infrastructure, or dependency locks have changed, but no ADR was added under docs/adr/."
            echo "Please document this architectural shift in a new ADR."
            exit 1
          fi
```

### 2. Centralized Discoverability (Backstage Integration)
When you have 50+ microservices scattered across separate repositories, developers will not manually navigate to every repository's `docs/adr/` folder to check architectural history. You must centralize discoverability.

A highly effective approach is using **Spotify Backstage**. Backstage uses the TechDocs plugin to scrape markdown documentation directly from Git repositories.
By configuring Backstage to read directories matching `docs/adr/*.md`, you can render a unified engineering decision registry. Developers can search across all repositories for keywords like "PgBouncer", "Kafka", or "DynamoDB" to see every architectural decision made across the entire company.

```yaml
# catalog-info.yaml (placed in every service repository)
apiVersion: backstage.io/v1alpha1
kind: Component
metadata:
  name: payment-service
  description: Handles processing of chargebacks and transactions
  annotations:
    backstage.io/techdocs-ref: dir:.
spec:
  type: service
  owner: payment-team
  lifecycle: production
```

## Production Failure Modes and Strategic Mitigations

No matter how well-designed your templates are, human systems will fail. When rolling out RFC and ADR processes, expect to encounter these three specific operational failure modes.

### 1. The "Bikeshedding" Trap in RFC Reviews
**Failure Mode:** An RFC is published, and within 48 hours, it has 147 comments. The discussion devolves into arguments over naming conventions, formatting choices, or deep theoretical edge cases that have a 0.01% chance of occurring in production. The RFC stalls, and the author becomes demotivated.
- **Mitigation:** Enforce a strict **separation of concerns** in comments. Reviewers must categorize comments using prefixes:
  - `[BLOCKER]`: A critical architectural flaw that will break scalability, security, or reliability. Must be resolved before approval.
  - `[DISCUSSION]`: A general design question or trade-off to explore.
  - `[NIT]`: Superficial comments (naming, typos). The author is free to ignore these.
- In addition, the Tech Lead must actively manage the comment threads and declare "decision checkpoints" to move discussion forward.

### 2. The ADR Drift Antipattern
**Failure Mode:** An ADR is approved and merged. Six months later, the system architecture changes, but the original ADR remains untouched. The documentation now active in `docs/adr/` directly contradicts the actual state of the codebase.
- **Mitigation:** Never modify an existing ADR’s decision contents directly. ADRs must be treated as an immutable ledger. 
- If you decide to deprecate PgBouncer in favor of PgCat, you must write a *new* ADR: `docs/adr/0045-migrate-from-pgbouncer-to-pgcat.md`. 
- In this new ADR, update the status header to `Accepted (Supercedes ADR-0024)`. 
- In the original ADR (`0024`), update the status to `Superceded by ADR-0045` and provide a link to the new file. This preserves the historical audit trail while preventing developers from being misled by outdated documents.

### 3. Documentation Fatigue and Over-Documentation
**Failure Mode:** Engineers are forced to write ADRs for minor decisions (e.g., choosing a library for JSON parsing or formatting log messages). The process becomes a bureaucratic check-the-box exercise, causing velocity to grind to a halt.
- **Mitigation:** Define clear, objective thresholds for when an ADR is required. An ADR is mandatory *only* if the decision meets at least one of the following criteria:
  - Introduces a new database, messaging middleware, or third-party service dependency.
  - Modifies external API contracts or breaks backwards compatibility.
  - Introduces a change that would take more than two engineering-weeks of effort to reverse.
  - Affects regulatory compliance (e.g., PCI-DSS, GDPR data flows).
- Anything else should be managed via standard code reviews and inline comments.

## Measuring Governance Health

Documentation is not a vanity metric. You should measure the health and impact of your RFC and ADR workflows using three key signals:

* **Decay Rate:** The average time an RFC remains in the `Under Review` state. Healthy organizations keep this under 10 days. If this metric creeps up to 20+ days, it indicates that your review committees are bottlenecked or that async reviews are not being prioritized.
* **Search Recency:** How often developers search for and read ADRs/RFCs in your developer portal. If this number is low, your documentation is either hard to find, poorly formatted, or out of date.
* **Onboarding Lead Time:** Track how long it takes for a new engineer to submit their first pull request to a complex backend codebase. A robust, ADR-rich codebase should reduce this lead time by 30-40%, as new hires can read the historical decision ledger to understand the system’s design rationale without scheduling hours of tribal-knowledge walkthroughs.

By treating architectural decisions as code, version-controlling them, automating their format verification, and centralizing their discoverability, engineering leads can transition their organizations from chaotic tribal networks to structured, self-documenting execution engines. You stop wasting engineering hours on technical archeology and start focusing on shipping reliable systems.