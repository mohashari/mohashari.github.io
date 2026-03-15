---
layout: post
title: "Docker Multi-Stage Builds: Smaller, Faster, More Secure Images"
date: 2026-03-15 07:00:00 +0700
tags: [docker, devops, backend, containers, security]
description: "Master Docker multi-stage builds to produce lean production images — reduce image size by 90%, eliminate build tools from runtime, and speed up CI pipelines."
---

The average developer Dockerfile copies source code, installs compilers, runs tests, and ships everything — including the Go compiler, npm cache, and build secrets — into the production image. Multi-stage builds fix this.

## The Problem with Single-Stage Builds

<script src="https://gist.github.com/mohashari/620be3dec333163a11ffec064c3c8412.js?file=snippet.dockerfile"></script>

Result: **~900 MB image** containing the entire Go toolchain that's only needed at build time.

## Multi-Stage: The Right Way

<script src="https://gist.github.com/mohashari/620be3dec333163a11ffec064c3c8412.js?file=snippet-2.dockerfile"></script>

Result: **~8 MB image**. The `distroless` image contains only the app binary and its runtime dependencies — no shell, no package manager, no attack surface.

## Optimizing Build Cache

Docker caches each layer. Put the things that change least at the top.

<script src="https://gist.github.com/mohashari/620be3dec333163a11ffec064c3c8412.js?file=snippet-3.dockerfile"></script>

If you only change `main.go`, Docker reuses the `go mod download` layer. Build time drops from 3 minutes to 15 seconds.

## Running Tests in the Build Pipeline

<script src="https://gist.github.com/mohashari/620be3dec333163a11ffec064c3c8412.js?file=snippet-4.dockerfile"></script>

Tests are enforced in CI without a separate test step. `docker build` fails if tests fail.

## Node.js Multi-Stage Build

<script src="https://gist.github.com/mohashari/620be3dec333163a11ffec064c3c8412.js?file=snippet-5.dockerfile"></script>

`node_modules` from the `deps` stage contains only production packages. `devDependencies` (TypeScript compiler, test runners) never reach production.

## Python Multi-Stage with Virtual Environment

<script src="https://gist.github.com/mohashari/620be3dec333163a11ffec064c3c8412.js?file=snippet-6.dockerfile"></script>

## Secrets in Builds — Don't Bake Them In

<script src="https://gist.github.com/mohashari/620be3dec333163a11ffec064c3c8412.js?file=snippet-7.dockerfile"></script>

Build with: `docker build --secret id=pypi_token,env=PYPI_TOKEN .`

## Image Size Comparison

| Base Image | Size | Use Case |
|------------|------|----------|
| `ubuntu:22.04` | 77 MB | Debugging, needs shell |
| `alpine:3.19` | 7.4 MB | Small Linux with shell |
| `distroless/static` | 2.5 MB | Go static binaries |
| `distroless/base` | 20 MB | Dynamically linked binaries |
| `scratch` | 0 B | Fully static binaries only |

## Security Best Practices

<script src="https://gist.github.com/mohashari/620be3dec333163a11ffec064c3c8412.js?file=snippet-8.dockerfile"></script>

Multi-stage builds are not just an optimization — they're a security practice. Shipping a Go binary in a distroless image means an attacker who escapes the container has no `curl`, no `bash`, no `apt-get` to work with.
