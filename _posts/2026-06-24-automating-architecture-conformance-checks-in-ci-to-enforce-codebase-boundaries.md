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

This erosion is called "architectural drift" or "accidental spaghettification." It manifests in several highly destructive failure modes:

1. **Leaked Infrastructure Details**: Domain objects import ORM models, leaking database-specific annotations, hooks, and transaction states into pure business logic. Suddenly, a database schema change or a migration from PostgreSQL to DynamoDB requires refactoring the core domain rules.
2. **Circular Dependencies**: In languages like Go, circular package imports cause immediate compilation failures. Developers often resolve these compiler errors by merging packages together, creating a massive, untangled "god package" that destroys cohesion. This destroys package parallelism during compilation: a clean compile step that once took 15 seconds can balloon to over 3 minutes because the compiler is forced to compile a monolithic block serially.
3. **Implicit Coupling**: Sub-domains that should be strictly separated (e.g., `billing` and `user_profiles`) start referencing each other's internal database models or internal utility functions. This turns a logical modular monolith into a distributed nightmare inside a single process, making the extraction of microservices impossible without a complete code rewrite.
4. **Testability Decay**: When the domain layer is tightly coupled to concrete databases or HTTP clients, writing unit tests becomes impossible without spinning up mock databases or complex mocking frameworks. The feedback loop slows down, and test coverage drops.

Relying on tech leads to catch these boundary violations during pull request reviews is a failed strategy. When an engineer is looking at a diff of 50 files, they are mentally evaluating business correctness, edge cases, and performance. Spotting a single illegal import statement (e.g., importing `internal/infrastructure/db` inside `internal/domain/user.go`) requires a level of cognitive tracing that humans are simply bad at maintaining consistently over long periods. Architecture must be treated as a compilation constraint: if you violate the boundaries, the build must fail.

## Defining the Boundary Policy: The Dependency Matrix

Before writing automation, you must formalize your architectural rules into a dependency matrix. If you are using Hexagonal Architecture (Ports and Adapters), clean architecture, or traditional n-tier architecture, you must explicitly state which layers are allowed to depend on which.

Let’s define a strict Hexagonal dependency policy for a standard backend service:

* **Domain Layer (`/internal/domain`)**: Contains core entities and business rules. It has **zero** dependencies on any other package in the codebase or external framework. It must not import SQL drivers, ORMs, JSON serializers, or transport layers.
* **Use Case / Application Layer (`/internal/usecase`)**: Coordinates application workflows. It can only depend on the Domain layer. It communicates with external resources via interface "ports" (e.g., `UserRepository` interface).
* **Infrastructure Layer (`/internal/infrastructure`)**: Implements ports. It contains database adapters, raw SQL queries, HTTP clients, and caching layers. It is allowed to depend on Domain and Use Case layers.
* **API / Delivery Layer (`/internal/api`)**: Exposes transport interfaces (HTTP, gRPC, CLI). It maps incoming payloads to Domain objects and calls Use Cases. It can depend on Use Cases and Domain, but **never** on Infrastructure.

This policy forms a strict unidirectional dependency graph: `API -> Use Cases -> Domain`, and `Infrastructure -> Use Cases -> Domain`. Any import in the reverse direction is an architectural violation.

## Implementing Automated Checks in Go: Unit-Testing Imports

Go packages are compiled independently, making dependency control a critical component of build times. While there are external linters, you can implement a robust, zero-dependency architectural gate directly inside your Go test suite using the standard library's `go/build` package.

Because unit tests run locally and in CI, writing a test to verify import layers ensures that boundary checks are executed every time an engineer runs `go test ./...`.

Here is a production-grade unit test that recursively walks your codebase, analyzes the imports of every package, and asserts layer boundary compliance.

```go
// snippet-1
package architecture_test

import (
	"go/build"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// dependencyRule defines target packages and packages they are prohibited from importing.
type dependencyRule struct {
	pkgPattern    string   // The subdirectory pattern to evaluate (e.g., "/internal/domain")
	forbiddenPkgs []string // Root-relative path patterns that are forbidden (e.g., "/internal/infrastructure")
}

func TestArchitectureLayerBoundaries(t *testing.T) {
	// Enforce clean architecture dependencies:
	// 1. Domain must not import Use Cases, Infrastructure, or API layers.
	// 2. Use Cases must not import Infrastructure or API layers.
	rules := []dependencyRule{
		{
			pkgPattern:    "/internal/domain",
			forbiddenPkgs: []string{"/internal/usecase", "/internal/infrastructure", "/internal/api", "/internal/delivery"},
		},
		{
			pkgPattern:    "/internal/usecase",
			forbiddenPkgs: []string{"/internal/infrastructure", "/internal/api", "/internal/delivery"},
		},
	}

	// Locate the go.mod directory to establish the repository root
	wd, err := os.Getwd()
	if err != nil {
		t.Fatalf("failed to get current working directory: %v", err)
	}

	var moduleRoot string
	for dir := wd; dir != "/" && dir != "."; dir = filepath.Dir(dir) {
		if _, err := os.Stat(filepath.Join(dir, "go.mod")); err == nil {
			moduleRoot = dir
			break
		}
	}
	if moduleRoot == "" {
		t.Fatalf("could not locate go.mod in parent directories")
	}

	// Walk through the project files and inspect Go package dependencies
	err = filepath.Walk(moduleRoot, func(path string, info os.FileInfo, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		// Skip standard directories that do not contain project code
		if info.IsDir() && (info.Name() == ".git" || info.Name() == "vendor" || info.Name() == "third_party") {
			return filepath.SkipDir
		}
		if !info.IsDir() {
			return nil
		}

		// Import the package using the go/build context
		pkg, err := build.ImportDir(path, 0)
		if err != nil {
			// Skip directories that do not contain Go source files
			return nil
		}

		for _, rule := range rules {
			// If the package matches the rule pattern, verify its imports
			if !strings.Contains(pkg.ImportPath, rule.pkgPattern) {
				continue
			}

			for _, imp := range pkg.Imports {
				for _, forbidden := range rule.forbiddenPkgs {
					if strings.Contains(imp, forbidden) {
						t.Errorf("Architecture violation: package %q is forbidden from importing %q", 
							pkg.ImportPath, imp)
					}
				}
			}
		}
		return nil
	})

	if err != nil {
		t.Fatalf("failed to execute directory walk: %v", err)
	}
}
```

This unit test is extremely fast (running in under 100 milliseconds for small-to-medium repos) because it parses only package headers, not AST trees. By running this test, you prevent any illegal package dependency from compiling successfully in production builds.

## Static Analysis Guards: `depguard` for `golangci-lint`

While custom Go tests are highly flexible, static analysis linting tools provide rich reporting and integrations. In the Go ecosystem, `golangci-lint` is the industry standard. It contains a built-in linter named `depguard` specifically designed to block package imports based on configuration.

Using `depguard` allows you to prevent both internal boundary violations and external package leaks (for example, stopping developers from pulling in generic packages like `github.com/sirupsen/logrus` if the team has standardized on Go's structured logger `slog`).

Here is a modern `.golangci.yml` configuration demonstrating how to enforce architectural boundaries via `depguard`.

```yaml
// snippet-2
linters-settings:
  depguard:
    rules:
      domain_boundaries:
        files:
          - "**/internal/domain/**/*.go"
        deny:
          - pkg: "github.com/mohashari/myapp/internal/infrastructure"
            desc: "domain layer must not import infrastructure packages"
          - pkg: "github.com/mohashari/myapp/internal/api"
            desc: "domain layer must not import api layer code"
          - pkg: "database/sql"
            desc: "domain layer must not interact directly with SQL drivers; use repository interfaces"
      usecase_boundaries:
        files:
          - "**/internal/usecase/**/*.go"
        deny:
          - pkg: "github.com/mohashari/myapp/internal/api"
            desc: "usecases must remain transport-agnostic and should not import the api layer"
      external_logging:
        files:
          - "$all"
        deny:
          - pkg: "github.com/sirupsen/logrus"
            desc: "logrus is deprecated; use log/slog for structured logging"
```

Running `golangci-lint run` in your developer workflow will instantly catch these issues in IDEs before code is committed.

## Polylithic Architectures: Python and Node.js Boundary Enforcement

If your backend architecture uses multiple languages (a common reality when managing specialized machine learning services in Python or high-concurrency microservices in Node.js), you must enforce matching boundaries.

### Python: Boundary Checks with `import-linter`

In the Python community, dependency drift is common due to Python's dynamic import resolution. A simple `import sqlalchemy` inside an entity file can quietly couple database drivers to business logic.

The tool `import-linter` enforces dependency boundaries in Python. It evaluates a configuration file (typically `.importlinter.yml`) against your project’s import graph at test time.

Here is a configuration for a payment microservice structured with strict architectural boundaries.

```yaml
// snippet-3
# .importlinter.yml
---
root_package: payment_service

contracts:
  - name: Strict Layer Architecture
    type: layers
    containers:
      - payment_service
    layers:
      - api          # Contains REST routes, controllers, and schemas
      - application  # Exposes use cases and abstract ports
      - domain       # Pure domain models and validation rules

  - name: Database Isolation contract
    type: forbidden
    source_modules:
      - payment_service.domain
      - payment_service.application
    forbidden_modules:
      - payment_service.infrastructure.db
      - sqlalchemy
      - psycopg2
```

To run this in your local shell or CI runner:

```bash
pip install import-linter
lint-imports
```

If an engineer attempts to import a helper from `payment_service.infrastructure.db` inside `payment_service.domain.models`, `lint-imports` returns an exit code of `1` and details the offending import chain.

### TypeScript / Node.js: Boundary Checks with `dependency-cruiser`

For Node.js backend systems written in TypeScript, `dependency-cruiser` is the most powerful tool for tracking and blocking illegal dependency chains. It parses the actual dependency tree (supporting ES Modules, CommonJS, and TypeScript paths) and enforces custom rules.

Here is a configuration block for `.dependency-cruiser.js` to block infrastructure leaks and prevent circular dependencies, which degrade application initialization performance in high-throughput node applications.

```javascript
// snippet-4
module.exports = {
  forbidden: [
    {
      name: 'domain-not-to-infrastructure',
      comment: 'Core business domain logic must never import concrete infrastructure implementations.',
      severity: 'error',
      from: { path: '^src/domain' },
      to: { path: '^src/infrastructure' }
    },
    {
      name: 'usecase-not-to-controllers',
      comment: 'Application workflows (use cases) must remain decoupled from HTTP/gRPC routing interfaces.',
      severity: 'error',
      from: { path: '^src/usecases' },
      to: { path: '^src/controllers' }
    },
    {
      name: 'no-circular-dependencies',
      comment: 'Circular imports create brittle module resolution paths and cause hard-to-debug runtime issues.',
      severity: 'error',
      from: {},
      to: { circular: true }
    }
  ],
  options: {
    doNotFollow: {
      path: 'node_modules'
    },
    tsPreSync: true
  }
};
```

Run this in your package script configuration:
```bash
npx depcruise --config .dependency-cruiser.js src
```

## Integrating Gates into the CI/CD Pipeline

Automated checks are only as strong as the gates enforcing them. If boundary validation tests run on the same cadence as heavy end-to-end integration tests, developers will ignore the failure logs until the end of the delivery cycle.

The rule is simple: **Fail fast**. Architecture conformance checks should execute during the initial lint stage of the pipeline, before running long, database-backed tests. If the import structure of the codebase is invalid, there is no reason to spend CI minutes building containers or running integration tests.

Here is a GitHub Actions workflow demonstrating how to execute `depguard` validation and architecture unit tests concurrently at the start of the build pipeline.

```yaml
// snippet-5
name: CI Quality Gate

on:
  push:
    branches: [ main ]
  pull_request:
    branches: [ main ]

jobs:
  conformance:
    name: Architecture Conformance
    runs-on: ubuntu-latest
    steps:
      - name: Checkout Code
        uses: actions/checkout@v4

      - name: Set up Go
        uses: actions/setup-go@v5
        with:
          go-version: '1.22'
          cache: true

      - name: Verify Go Formatting & Linter Rules
        uses: golangci/golangci-lint-action@v6
        with:
          version: v1.59
          args: --timeout=5m --enable=depguard

      - name: Run Clean Architecture Unit Tests
        run: |
          go test -v -run TestArchitectureLayerBoundaries ./...
```

By placing this job at the entry point of your pipeline, you create an unbreakable feedback loop. An engineer attempting to merge a boundary violation will get a red build indicator within 90 seconds of opening a pull request.

## Handling Legacy Debt: The Architecture Ratchet Pattern

Adding strict architecture checking to a greenfield project is trivial. Doing it in a five-year-old, million-line-of-code legacy repository is an entirely different challenge. If you enable these checks all at once, you will find hundreds of historical violations. Halting all product delivery to execute a massive refactoring is rarely an option.

The solution is to use the **Ratchet Pattern** (also known as baseline enforcement). You record all existing architectural violations in a baseline file. The CI tool accepts these existing violations but prevents any *new* violations from being introduced. As teams refactor parts of the codebase, they remove violations from the baseline. Over time, the baseline shrinks to zero, and the system moves toward its target architecture without causing a delivery block.

To implement a ratchet using the Go architecture unit test shown in `snippet-1`, you can write a helper to load a JSON-based baseline of tolerated violations and cross-reference checks.

Here is the baseline checker implementation:

```go
// snippet-6
package architecture_test

import (
	"encoding/json"
	"os"
	"testing"
)

// AllowedViolation defines an import pattern we tolerate temporarily.
type AllowedViolation struct {
	Source      string `json:"source"`      // e.g. "github.com/mohashari/myapp/internal/domain"
	Destination string `json:"destination"` // e.g. "github.com/mohashari/myapp/internal/infrastructure/db"
}

// loadArchitectureBaseline parses the baseline file.
func loadArchitectureBaseline(filePath string) (map[string]bool, error) {
	file, err := os.Open(filePath)
	if err != nil {
		if os.IsNotExist(err) {
			return make(map[string]bool), nil
		}
		return nil, err
	}
	defer file.Close()

	var list []AllowedViolation
	if err := json.NewDecoder(file).Decode(&list); err != nil {
		return nil, err
	}

	baselineMap := make(map[string]bool)
	for _, item := range list {
		key := item.Source + " -> " + item.Destination
		baselineMap[key] = true
	}
	return baselineMap, nil
}

// CheckViolation evaluates a detected violation against the legacy baseline.
func CheckViolation(t *testing.T, src, dest string, baseline map[string]bool) {
	key := src + " -> " + dest
	if baseline[key] {
		// Tolerated legacy violation: log a warning but do not fail the test
		t.Logf("WARN: Tolerating legacy architecture violation: %s", key)
		return
	}
	// Fail the test if a new violation is introduced
	t.Errorf("FAIL: New architectural violation detected: %s. You must resolve this import or refactor the boundary.", key)
}
```

This baseline file (e.g., `architecture_baseline.json`) is checked into Git:

```json
// snippet-7
[
  {
    "source": "github.com/mohashari/myapp/internal/domain/user",
    "destination": "github.com/mohashari/myapp/internal/infrastructure/db"
  }
]
```

When an engineer resolves the legacy connection inside `internal/domain/user`, they remove that block from `architecture_baseline.json`. If anyone attempts to re-introduce the import later, the CI pipeline will block the merge.

## Automate Rules, Empower Engineers

Software architecture is not defined by UML diagrams or design documents; it is defined by the actual dependency graph compiled into your binary. When you rely solely on manual code reviews to enforce architecture, you delegate engineering governance to human vigilance—which decays under deadlines.

By automating conformance checks:
* **Tech leads** save valuable code review hours, shifting focus from "why did you import this package here?" to high-level system design.
* **New developers** gain immediate, automated feedback on codebase structure, reducing the onboarding learning curve.
* **Production systems** remain modular, preventing tight coupling that turns small maintenance tasks into complex outages.

Choose a tool appropriate for your stack—whether a custom package import test in Go, `depguard` in `golangci-lint`, or `import-linter` in Python—and integrate it into your CI system today. Your database connection pools will thank you.
