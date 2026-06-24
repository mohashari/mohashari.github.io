---
layout: post
title: "Automating Architecture Conformance Checks in CI to Enforce Codebase Boundaries"
date: 2026-06-24 08:00:00 +0700
tags: [software-architecture, ci-cd, code-quality]
description: "Stop architectural erosion and import leaks in production. Learn how to automate strict layer boundaries and package separation in CI using Go unit tests, depguard, and import-linter."
image: "https://picsum.photos/seed/architecture-ci/1080/720"
thumbnail: "https://picsum.photos/seed/architecture-ci/400/300"
---

Picture this: your high-throughput payment processing service starts experiencing intermittent database connection pool exhaustion under a Friday evening traffic spike. As a senior backend engineer tasked with debugging, you trace the issue to a newly deployed feature in the domain model. You expect to find business logic, but instead, you discover a direct import of a PostgreSQL database client package inside a domain entity. A developer, seeking a quick lookup, bypassed the repository interface and query ports, instantiating a raw database connection pool directly inside the entity constructor. Because this entity was instantiated on every single API request, the service spun up thousands of orphaned connection pools, quickly choking the PostgreSQL server with over 2,000 active connections and causing a cascading system outage. The pull request was 1,500 lines long, the reviewers were suffering from review fatigue, and the boundary violation slipped through unnoticed. This is not just a junior mistake; it is an organizational failure to automate the enforcement of software boundaries. Manual code reviews are a leaky bucket when it comes to preserving architecture. To build resilient, maintainable backend systems, you must automate architectural conformance checks directly in your continuous integration (CI) pipeline.

## The High Cost of Architectural Drift

In any scaling engineering team, architectural drift is a silent killer. When a system is first designed, the team aligns on clean layers: business logic (the domain) sits at the core, surrounded by application services (use cases), which are wrapped by transport protocols, database clients, and external SDKs (infrastructure). But as pressure to deliver increases and headcount grows past 30+ engineers, the boundaries between these layers begin to erode.

This erosion is called \"architectural drift\" or \"accidental spaghettification.\" It manifests in several highly destructive failure modes:

1. **Leaked Infrastructure Details**: Domain objects import ORM models, leaking database-specific annotations, hooks, and transaction states into pure business logic. Suddenly, a database schema change or a migration from PostgreSQL to DynamoDB requires refactoring the core domain rules.
2. **Circular Dependencies**: In languages like Go, circular package imports cause immediate compilation failures. Developers often resolve these compiler errors by merging packages together, creating a massive, untangled \"god package\" that destroys cohesion. This destroys package parallelism during compilation: a clean compile step that once took 15 seconds can balloon to over 3 minutes because the compiler is forced to compile a monolithic block serially.
3. **Implicit Coupling**: Sub-domains that should be strictly separated (e.g., `billing` and `user_profiles`) start referencing each other's internal database models or internal utility functions. This turns a logical modular monolith into a distributed nightmare inside a single process, making the extraction of microservices impossible without a complete code rewrite.
4. **Testability Decay**: When the domain layer is tightly coupled to concrete databases or HTTP clients, writing unit tests becomes impossible without spinning up mock databases or complex mocking frameworks. The feedback loop slows down, and test coverage drops.

Relying on tech leads to catch these boundary violations during pull request reviews is a failed strategy. When an engineer is looking at a diff of 50 files, they are mentally evaluating business correctness, edge cases, and performance. Spotting a single illegal import statement (e.g., importing `internal/infrastructure/db` inside `internal/domain/user.go`) requires a level of cognitive tracing that humans are simply bad at maintaining consistently over long periods. Architecture must be treated as a compilation constraint: if you violate the boundaries, the build must fail.

## Defining the Boundary Policy: The Dependency Matrix

Before writing automation, you must formalize your architectural rules into a dependency matrix. If you are using Hexagonal Architecture (Ports and Adapters), clean architecture, or traditional n-tier architecture, you must explicitly state which layers are allowed to depend on which.

Let’s define a strict Hexagonal dependency policy for a standard backend service:

* **Domain Layer (`/internal/domain`)**: Contains core entities and business rules. It has **zero** dependencies on any other package in the codebase or external framework. It must not import SQL drivers, ORMs, JSON serializers, or transport layers.
* **Use Case / Application Layer (`/internal/usecase`)**: Coordinates application workflows. It can only depend on the Domain layer. It communicates with external resources via interface \"ports\" (e.g., `UserRepository` interface).
* **Infrastructure Layer (`/internal/infrastructure`)**: Implements ports. It contains database adapters, raw SQL queries, HTTP clients, and caching layers. It is allowed to depend on Domain and Use Case layers.
* **API / Delivery Layer (`/internal/api`)**: Exposes transport interfaces (HTTP, gRPC, CLI). It maps incoming payloads to Domain objects and calls Use Cases. It can depend on Use Cases and Domain, but **never** on Infrastructure.

This policy forms a strict unidirectional dependency graph: `API -> Use Cases -> Domain`, and `Infrastructure -> Use Cases -> Domain`. Any import in the reverse direction is an architectural violation.

## Implementing Automated Checks in Go: Unit-Testing Imports

Go packages are compiled independently, making dependency control a critical component of build times. While there are external linters, you can implement a robust, zero-dependency architectural gate directly inside your Go test suite using the standard library's `go/build` package.

Because unit tests run locally and in CI, writing a test to verify import layers ensures that boundary checks are executed every time an engineer runs `go test ./...`.

Here is a production-grade unit test that recursively walks your codebase, analyzes the imports of every package, and asserts layer boundary compliance.

<script src="https://gist.github.com/mohashari/581e0cdca384cefb275019ed5f54f3b0.js?file=snippet-1.go"></script>

This unit test is extremely fast (running in under 100 milliseconds for small-to-medium repos) because it parses only package headers, not AST trees. By running this test, you prevent any illegal package dependency from compiling successfully in production builds.

## Static Analysis Guards: `depguard` for `golangci-lint`

While custom Go tests are highly flexible, static analysis linting tools provide rich reporting and integrations. In the Go ecosystem, `golangci-lint` is the industry standard. It contains a built-in linter named `depguard` specifically designed to block package imports based on configuration.

Using `depguard` allows you to prevent both internal boundary violations and external package leaks (for example, stopping developers from pulling in generic packages like `github.com/sirupsen/logrus` if the team has standardized on Go's structured logger `slog`).

Here is a modern `.golangci.yml` configuration demonstrating how to enforce architectural boundaries via `depguard`.

<script src="https://gist.github.com/mohashari/581e0cdca384cefb275019ed5f54f3b0.js?file=snippet-2.yaml"></script>

Running `golangci-lint run` in your developer workflow will instantly catch these issues in IDEs before code is committed.

## Polylithic Architectures: Python and Node.js Boundary Enforcement

If your backend architecture uses multiple languages (a common reality when managing specialized machine learning services in Python or high-concurrency microservices in Node.js), you must enforce matching boundaries.

### Python: Boundary Checks with `import-linter`

In the Python community, dependency drift is common due to Python's dynamic import resolution. A simple `import sqlalchemy` inside an entity file can quietly couple database drivers to business logic.

The tool `import-linter` enforces dependency boundaries in Python. It evaluates a configuration file (typically `.importlinter.yml`) against your project’s import graph at test time.

Here is a configuration for a payment microservice structured with strict architectural boundaries.

<script src="https://gist.github.com/mohashari/581e0cdca384cefb275019ed5f54f3b0.js?file=snippet-3.yaml"></script>

To run this in your local shell or CI runner:

```bash
pip install import-linter
lint-imports
```

If an engineer attempts to import a helper from `payment_service.infrastructure.db` inside `payment_service.domain.models`, `lint-imports` returns an exit code of `1` and details the offending import chain.

### TypeScript / Node.js: Boundary Checks with `dependency-cruiser`

For Node.js backend systems written in TypeScript, `dependency-cruiser` is the most powerful tool for tracking and blocking illegal dependency chains. It parses the actual dependency tree (supporting ES Modules, CommonJS, and TypeScript paths) and enforces custom rules.

Here is a configuration block for `.dependency-cruiser.js` to block infrastructure leaks and prevent circular dependencies, which degrade application initialization performance in high-throughput node applications.

<script src="https://gist.github.com/mohashari/581e0cdca384cefb275019ed5f54f3b0.js?file=snippet-4.js"></script>

Run this in your package script configuration:
```bash
npx depcruise --config .dependency-cruiser.js src
```

## Integrating Gates into the CI/CD Pipeline

Automated checks are only as strong as the gates enforcing them. If boundary validation tests run on the same cadence as heavy end-to-end integration tests, developers will ignore the failure logs until the end of the delivery cycle.

The rule is simple: **Fail fast**. Architecture conformance checks should execute during the initial lint stage of the pipeline, before running long, database-backed tests. If the import structure of the codebase is invalid, there is no reason to spend CI minutes building containers or running integration tests.

Here is a GitHub Actions workflow demonstrating how to execute `depguard` validation and architecture unit tests concurrently at the start of the build pipeline.

<script src="https://gist.github.com/mohashari/581e0cdca384cefb275019ed5f54f3b0.js?file=snippet-5.yaml"></script>

By placing this job at the entry point of your pipeline, you create an unbreakable feedback loop. An engineer attempting to merge a boundary violation will get a red build indicator within 90 seconds of opening a pull request.

## Handling Legacy Debt: The Architecture Ratchet Pattern

Adding strict architecture checking to a greenfield project is trivial. Doing it in a five-year-old, million-line-of-code legacy repository is an entirely different challenge. If you enable these checks all at once, you will find hundreds of historical violations. Halting all product delivery to execute a massive refactoring is rarely an option.

The solution is to use the **Ratchet Pattern** (also known as baseline enforcement). You record all existing architectural violations in a baseline file. The CI tool accepts these existing violations but prevents any *new* violations from being introduced. As teams refactor parts of the codebase, they remove violations from the baseline. Over time, the baseline shrinks to zero, and the system moves toward its target architecture without causing a delivery block.

To implement a ratchet using the Go architecture unit test shown in `snippet-1`, you can write a helper to load a JSON-based baseline of tolerated violations and cross-reference checks.

Here is the baseline checker implementation:

<script src="https://gist.github.com/mohashari/581e0cdca384cefb275019ed5f54f3b0.js?file=snippet-6.go"></script>

This baseline file (e.g., `architecture_baseline.json`) is checked into Git:

<script src="https://gist.github.com/mohashari/581e0cdca384cefb275019ed5f54f3b0.js?file=snippet-7.json"></script>

When an engineer resolves the legacy connection inside `internal/domain/user`, they remove that block from `architecture_baseline.json`. If anyone attempts to re-introduce the import later, the CI pipeline will block the merge.

## Automate Rules, Empower Engineers

Software architecture is not defined by UML diagrams or design documents; it is defined by the actual dependency graph compiled into your binary. When you rely solely on manual code reviews to enforce architecture, you delegate engineering governance to human vigilance—which decays under deadlines.

By automating conformance checks:
* **Tech leads** save valuable code review hours, shifting focus from \"why did you import this package here?\" to high-level system design.
* **New developers** gain immediate, automated feedback on codebase structure, reducing the onboarding learning curve.
* **Production systems** remain modular, preventing tight coupling that turns small maintenance tasks into complex outages.

Choose a tool appropriate for your stack—whether a custom package import test in Go, `depguard` in `golangci-lint`, or `import-linter` in Python—and integrate it into your CI system today. Your database connection pools will thank you.