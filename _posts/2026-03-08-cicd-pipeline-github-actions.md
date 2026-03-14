---
layout: post
title: "Building Production-Grade CI/CD Pipelines with GitHub Actions"
tags: [cicd, devops, github-actions, backend]
description: "Build a robust CI/CD pipeline with GitHub Actions — automated testing, Docker builds, and zero-downtime deployments."
---

A great CI/CD pipeline is your team's safety net and productivity multiplier. This guide shows how to build one with GitHub Actions that actually works in production.

## The Pipeline Structure

A production pipeline typically has these stages:

```
Push/PR → Lint & Test → Build & Scan → Deploy to Staging → Deploy to Production
```

## Basic CI Pipeline

```yaml
# .github/workflows/ci.yml
name: CI

on:
  push:
    branches: [main, develop]
  pull_request:
    branches: [main]

jobs:
  test:
    name: Test
    runs-on: ubuntu-latest

    services:
      postgres:
        image: postgres:16
        env:
          POSTGRES_PASSWORD: postgres
          POSTGRES_DB: testdb
        options: >-
          --health-cmd pg_isready
          --health-interval 10s
          --health-timeout 5s
          --health-retries 5
        ports:
          - 5432:5432

      redis:
        image: redis:7
        options: >-
          --health-cmd "redis-cli ping"
          --health-interval 10s
        ports:
          - 6379:6379

    steps:
      - uses: actions/checkout@v4

      - name: Set up Go
        uses: actions/setup-go@v5
        with:
          go-version: '1.21'
          cache: true

      - name: Download dependencies
        run: go mod download

      - name: Run linter
        uses: golangci/golangci-lint-action@v3
        with:
          version: latest

      - name: Run tests
        env:
          DATABASE_URL: postgres://postgres:postgres@localhost:5432/testdb
          REDIS_URL: redis://localhost:6379
        run: |
          go test -v -race -coverprofile=coverage.out ./...
          go tool cover -func=coverage.out

      - name: Check coverage threshold
        run: |
          COVERAGE=$(go tool cover -func=coverage.out | grep total | awk '{print $3}' | tr -d '%')
          if (( $(echo "$COVERAGE < 70" | bc -l) )); then
            echo "Coverage $COVERAGE% is below 70% threshold"
            exit 1
          fi
```

## Docker Build and Push

```yaml
  build:
    name: Build & Push Docker Image
    runs-on: ubuntu-latest
    needs: test
    if: github.ref == 'refs/heads/main'

    steps:
      - uses: actions/checkout@v4

      - name: Set up Docker Buildx
        uses: docker/setup-buildx-action@v3

      - name: Login to Docker Hub
        uses: docker/login-action@v3
        with:
          username: ${{ secrets.DOCKERHUB_USERNAME }}
          password: ${{ secrets.DOCKERHUB_TOKEN }}

      - name: Docker metadata
        id: meta
        uses: docker/metadata-action@v5
        with:
          images: myorg/myapp
          tags: |
            type=sha,prefix=,format=short
            type=raw,value=latest,enable=${{ github.ref == 'refs/heads/main' }}

      - name: Build and push
        uses: docker/build-push-action@v5
        with:
          context: .
          push: true
          tags: ${{ steps.meta.outputs.tags }}
          cache-from: type=gha
          cache-to: type=gha,mode=max

      - name: Run Trivy vulnerability scanner
        uses: aquasecurity/trivy-action@master
        with:
          image-ref: myorg/myapp:latest
          format: 'sarif'
          output: 'trivy-results.sarif'
          severity: 'CRITICAL,HIGH'
          exit-code: '1'

      - name: Upload scan results
        uses: github/codeql-action/upload-sarif@v3
        if: always()
        with:
          sarif_file: 'trivy-results.sarif'
```

## Deploy with Zero Downtime

```yaml
  deploy-staging:
    name: Deploy to Staging
    runs-on: ubuntu-latest
    needs: build
    environment: staging

    steps:
      - name: Deploy to Kubernetes
        uses: azure/k8s-deploy@v4
        with:
          namespace: staging
          manifests: |
            k8s/deployment.yaml
            k8s/service.yaml
          images: myorg/myapp:${{ github.sha }}
          strategy: rolling

  deploy-production:
    name: Deploy to Production
    runs-on: ubuntu-latest
    needs: deploy-staging
    environment: production  # Requires manual approval in GitHub

    steps:
      - name: Deploy to Kubernetes
        uses: azure/k8s-deploy@v4
        with:
          namespace: production
          manifests: |
            k8s/deployment.yaml
            k8s/service.yaml
          images: myorg/myapp:${{ github.sha }}
          strategy: canary
          percentage: 20  # Deploy to 20% of pods first

      - name: Monitor canary for 5 minutes
        run: sleep 300

      - name: Promote canary
        uses: azure/k8s-deploy@v4
        with:
          namespace: production
          strategy: canary
          action: promote
```

## Dependency Updates Automation

```yaml
# .github/dependabot.yml
version: 2
updates:
  - package-ecosystem: "gomod"
    directory: "/"
    schedule:
      interval: "weekly"
    groups:
      production-dependencies:
        dependency-type: "production"

  - package-ecosystem: "docker"
    directory: "/"
    schedule:
      interval: "weekly"

  - package-ecosystem: "github-actions"
    directory: "/"
    schedule:
      interval: "weekly"
```

## Reusable Workflow Pattern

Extract common steps into reusable workflows:

```yaml
# .github/workflows/reusable-test.yml
on:
  workflow_call:
    inputs:
      go-version:
        required: false
        type: string
        default: '1.21'
    secrets:
      DATABASE_URL:
        required: true

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-go@v5
        with:
          go-version: ${{ inputs.go-version }}
      - run: go test -race ./...
        env:
          DATABASE_URL: ${{ secrets.DATABASE_URL }}
```

Call it from other workflows:

```yaml
jobs:
  test:
    uses: ./.github/workflows/reusable-test.yml
    with:
      go-version: '1.21'
    secrets:
      DATABASE_URL: ${{ secrets.DATABASE_URL }}
```

## Best Practices Summary

1. **Cache dependencies** — `cache: true` in setup actions saves 30-60s per run
2. **Run tests in parallel** — use `strategy.matrix` for multiple Go versions
3. **Pin action versions** to SHA, not tags: `actions/checkout@abc123f`
4. **Use environments** for manual approval gates before production
5. **Fail fast** with `fail-fast: true` in matrix builds
6. **Scan everything** — code (CodeQL), dependencies (Dependabot), images (Trivy)
7. **Keep secrets in GitHub Secrets**, never hardcoded

A 10-minute CI pipeline that catches bugs before prod is worth more than a 2-minute one that misses them.
