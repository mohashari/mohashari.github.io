---
layout: post
title: "Supply Chain Security for Backend Engineers: SBOMs, Sigstore, and Dependency Auditing"
date: 2026-03-15 07:00:00 +0700
tags: [security, supply-chain, sbom, sigstore, devops]
description: "Harden your software supply chain by generating SBOMs, signing artifacts with Sigstore, and automating dependency vulnerability scanning."
---

The SolarWinds breach did not compromise a single application vulnerability — it compromised the build pipeline. Attackers inserted malicious code into a legitimate software update, and thousands of organizations installed it trustingly because the artifact was signed by a trusted vendor. This is the core threat model of supply chain attacks: your code can be perfect and you can still ship malware. For backend engineers, this is no longer a theoretical concern. The 2021 Log4Shell incident, the 2022 colors.js sabotage, and the 2024 XZ Utils backdoor all demonstrate that the libraries, build tools, and CI systems you depend on are as much a part of your attack surface as the code you write. Hardening the supply chain means treating every dependency, every build artifact, and every deployment pipeline as a potential vector — and building systematic defenses around them.

## What Is a Software Bill of Materials?

A Software Bill of Materials (SBOM) is a machine-readable inventory of every component in your software: direct dependencies, transitive dependencies, their versions, licenses, and known vulnerabilities at the time of build. Think of it as a nutritional label for software. Standards like CycloneDX and SPDX define the schema; tools like Syft, cdxgen, and Trivy generate them. Without an SBOM, you cannot answer "are we affected by CVE-XXXX-YYYY?" in less than a day. With one, you can answer it in seconds.

Syft can generate a CycloneDX SBOM from a container image or directory in a single command:

<script src="https://gist.github.com/mohashari/02c34e2a367d25674a4b6ff8d68211fb.js?file=snippet.sh"></script>

This SBOM should be published as a build artifact alongside your container image, not as an afterthought.

## Attesting and Signing with Sigstore

Generating an SBOM is only useful if consumers can trust it was produced by your legitimate build system and not tampered with in transit. Sigstore solves this through keyless signing using ephemeral keys and OIDC identity from your CI provider (GitHub Actions, GitLab CI, etc.). The resulting signature is recorded in a public, append-only transparency log called Rekor, making it auditable without requiring you to manage long-lived signing keys.

The `cosign` tool handles signing and verification. In a GitHub Actions workflow:

<script src="https://gist.github.com/mohashari/02c34e2a367d25674a4b6ff8d68211fb.js?file=snippet-2.yaml"></script>

The `id-token: write` permission is critical — it allows the runner to obtain a short-lived OIDC token from GitHub, which Sigstore uses to prove the signing identity without any stored secrets.

## Verifying Signatures at Deploy Time

Signing is useless without enforced verification. In Kubernetes environments, you can enforce signature verification using a policy admission controller. Kyverno makes this straightforward:

<script src="https://gist.github.com/mohashari/02c34e2a367d25674a4b6ff8d68211fb.js?file=snippet-3.yaml"></script>

This policy rejects any Pod that tries to run an image from your org without a valid Sigstore signature from your specific CI workflow identity. Lateral movement via a pushed-but-unsigned image is blocked at the cluster boundary.

## Automating Dependency Vulnerability Scanning

Signing confirms provenance; scanning confirms safety. The Go toolchain ships with `govulncheck`, which performs static analysis to identify which vulnerabilities in your dependency tree are actually reachable from your code — not just present in a library you import:

<script src="https://gist.github.com/mohashari/02c34e2a367d25674a4b6ff8d68211fb.js?file=snippet-4.sh"></script>

The distinction between "dependency has a CVE" and "your code calls the vulnerable code path" is enormous for reducing alert fatigue. `govulncheck` makes this distinction automatically.

For polyglot repositories, Grype provides unified scanning across ecosystems:

<script src="https://gist.github.com/mohashari/02c34e2a367d25674a4b6ff8d68211fb.js?file=snippet-5.sh"></script>

The `--fail-on high` flag makes the CI job exit non-zero on any high or critical finding, blocking deployment automatically.

## Pinning Dependencies and Detecting Drift

Dependency confusion and typosquatting attacks succeed partly because engineers use unpinned, mutable version references. In Go, the `go.sum` file provides cryptographic pinning by design. But for container base images, engineers routinely write `FROM ubuntu:22.04` — a mutable tag that can change under you. Pin by digest:

<script src="https://gist.github.com/mohashari/02c34e2a367d25674a4b6ff8d68211fb.js?file=snippet-6.dockerfile"></script>

The `-trimpath` flag removes local filesystem paths from the binary, reducing information leakage. `go mod verify` re-checks that downloaded modules match the `go.sum` hashes — catching any tampering with your module cache.

## Tracking SBOM History in a Database

For compliance and incident response, you need SBOM history over time — not just the current state. A simple PostgreSQL schema gives you the ability to query "which of our services shipped with log4j between these dates?":

<script src="https://gist.github.com/mohashari/02c34e2a367d25674a4b6ff8d68211fb.js?file=snippet-7.sql"></script>

This schema supports blast radius queries across your entire fleet during an incident, reducing the "are we affected?" analysis from hours of manual grep work to a single SQL query.

---

Supply chain security is not a product you buy — it is a set of engineering habits you build into every stage of your pipeline. Start by generating SBOMs at build time and treating them as first-class artifacts. Sign everything with Sigstore so provenance is verifiable without managing secrets. Enforce signature verification at deploy time so unsigned images cannot run in production. Automate vulnerability scanning against your SBOMs in CI and fail loudly on critical findings. Pin your base images by digest and verify your module checksums. Store SBOM history so you can answer incident questions in seconds. Each of these steps is independently valuable, but together they transform your pipeline from a trusted assumption into a verified guarantee — which is the only kind of trust that survives contact with an adversary.