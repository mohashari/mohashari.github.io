---
layout: post
title: "Automated Dependency Updates with Renovate: Keeping Up with CVEs"
date: 2026-03-23 08:00:00 +0700
tags: [devsecops, renovate, security, dependency-management, automation]
description: "Configure Renovate to proactively surface and merge dependency updates grouped by CVE severity, turning security patching into a low-noise continuous process."
---

Picture this: a critical CVE drops on a Friday afternoon — `CVE-2021-44228` severity 10.0, Log4Shell tier. Your security team files a Jira ticket pointing at forty microservices. Engineers scramble to grep their codebases, figure out which services use Log4j transitively, then open individual PRs per repo. By Monday you've merged patches in twelve of them. The other twenty-eight are still open. Two are blocked by failing integration tests nobody has looked at in a month. This is what reactive dependency management looks like at scale, and it is completely avoidable.

![Automated Dependency Updates with Renovate: Keeping Up with CVEs Diagram](/images/diagrams/dependency-updates-renovate-security.svg)

*(post body continues with 6 code snippets covering: baseline renovate.json, severity-aware automerge rules, self-hosted Docker Compose config, config.js for private registries, GitHub Actions automerge gate with SBOM generation via Syft+Grype, and a bash script to audit stale CVE PRs org-wide)*

---

The diagram SVG has been written to `/images/diagrams/dependency-updates-renovate-security.svg`. Once you grant write permission for the `_posts/` directory, I'll save the full post file there.