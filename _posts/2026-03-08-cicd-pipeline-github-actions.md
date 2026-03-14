---
layout: post
title: "Building Production-Grade CI/CD Pipelines with GitHub Actions"
tags: [cicd, devops, github-actions, backend]
description: "Build a robust CI/CD pipeline with GitHub Actions — automated testing, Docker builds, and zero-downtime deployments."
---

A great CI/CD pipeline is your team's safety net and productivity multiplier. This guide shows how to build one with GitHub Actions that actually works in production.

![CI/CD Pipeline Flow](/images/diagrams/cicd-pipeline.svg)

## The Pipeline Structure

A production pipeline typically has these stages:


<script src="https://gist.github.com/mohashari/dd387d5b5b7926fb0f7b54283972bff0.js?file=snippet.txt"></script>


## Basic CI Pipeline


<script src="https://gist.github.com/mohashari/dd387d5b5b7926fb0f7b54283972bff0.js?file=snippet.yaml"></script>


## Docker Build and Push


<script src="https://gist.github.com/mohashari/dd387d5b5b7926fb0f7b54283972bff0.js?file=snippet-2.yaml"></script>


## Deploy with Zero Downtime


<script src="https://gist.github.com/mohashari/dd387d5b5b7926fb0f7b54283972bff0.js?file=snippet-3.yaml"></script>


## Dependency Updates Automation


<script src="https://gist.github.com/mohashari/dd387d5b5b7926fb0f7b54283972bff0.js?file=snippet-4.yaml"></script>


## Reusable Workflow Pattern

Extract common steps into reusable workflows:


<script src="https://gist.github.com/mohashari/dd387d5b5b7926fb0f7b54283972bff0.js?file=snippet-5.yaml"></script>


Call it from other workflows:


<script src="https://gist.github.com/mohashari/dd387d5b5b7926fb0f7b54283972bff0.js?file=snippet-6.yaml"></script>


## Best Practices Summary

1. **Cache dependencies** — `cache: true` in setup actions saves 30-60s per run
2. **Run tests in parallel** — use `strategy.matrix` for multiple Go versions
3. **Pin action versions** to SHA, not tags: `actions/checkout@abc123f`
4. **Use environments** for manual approval gates before production
5. **Fail fast** with `fail-fast: true` in matrix builds
6. **Scan everything** — code (CodeQL), dependencies (Dependabot), images (Trivy)
7. **Keep secrets in GitHub Secrets**, never hardcoded

A 10-minute CI pipeline that catches bugs before prod is worth more than a 2-minute one that misses them.
