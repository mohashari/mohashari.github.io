---
layout: post
title: "GitOps with ArgoCD: Declarative Continuous Delivery for Kubernetes"
date: 2026-03-18 07:00:00 +0700
tags: [gitops, argocd, kubernetes, devops, cicd]
description: "Adopt GitOps principles with ArgoCD to manage Kubernetes deployments declaratively, enforce drift detection, and streamline multi-environment promotions."
---

Managing Kubernetes deployments through `kubectl apply` commands and ad-hoc scripts is a path that starts simple and ends in chaos. Who applied that change to production at 2am? Why does the staging cluster have a different image tag than what's in the repository? These questions have a common answer: your deployment process lacks a single source of truth. GitOps solves this by treating Git as the authoritative definition of your desired cluster state, and ArgoCD is the engine that continuously reconciles reality against that definition. The result is an audit trail built into every merge commit, automatic drift correction, and deployment pipelines that developers can reason about without reading runbook wikis.

## What GitOps Actually Means in Practice

GitOps is not just "store your YAML in Git." It requires four properties: the entire system is described declaratively, the desired state is versioned in Git, approved changes are automatically applied, and software agents ensure correctness and alert on divergence. ArgoCD satisfies all four. It runs inside your cluster, watches one or more Git repositories, and continuously compares the live cluster state against what the repo declares. Any drift — whether caused by a manual `kubectl edit`, a node replacement that spawned a slightly different pod spec, or a botched hotfix — is detected and can be automatically or manually remediated.

## Installing ArgoCD

Start by installing ArgoCD into a dedicated namespace. The official install manifest bundles the API server, repository server, application controller, and the Dex identity provider for SSO.

<script src="https://gist.github.com/mohashari/a8f2c1079defa45e4db3a5de3416bd8c.js?file=snippet.sh"></script>

Once the pods are running, port-forward the ArgoCD API server or expose it via an ingress. For production, use an ingress controller with TLS termination and configure SSO through Dex rather than relying on the default admin account.

## Defining an Application

The core ArgoCD primitive is the `Application` custom resource. It maps a Git source — a repository path, branch or tag, and optional Helm values — to a destination cluster and namespace. ArgoCD then owns the reconciliation loop for everything described at that path.

<script src="https://gist.github.com/mohashari/a8f2c1079defa45e4db3a5de3416bd8c.js?file=snippet-2.yaml"></script>

The `selfHeal: true` flag is what makes this GitOps rather than just deployment automation. Any manual change to a managed resource will be overwritten on the next reconciliation cycle, typically within three minutes. The `prune: true` flag removes resources that exist in the cluster but have been deleted from Git.

## Structuring a Multi-Environment Repository

A common layout separates application manifests from environment-specific overrides using Kustomize. Each environment directory contains a `kustomization.yaml` that patches only what differs — image tags, replica counts, resource limits, ingress hostnames — while the base holds shared configuration.

<script src="https://gist.github.com/mohashari/a8f2c1079defa45e4db3a5de3416bd8c.js?file=snippet-3.txt"></script>

<script src="https://gist.github.com/mohashari/a8f2c1079defa45e4db3a5de3416bd8c.js?file=snippet-4.yaml"></script>

Your CI pipeline's job shrinks to one thing: updating the `newTag` value via a commit after a successful image build. ArgoCD handles the rest.

## Automating Image Tag Promotion

After a successful Docker build and push, a CI job should open a pull request (or commit directly to a promotion branch) updating the image tag in the relevant overlay. This shell snippet shows the pattern using the `yq` tool:

<script src="https://gist.github.com/mohashari/a8f2c1079defa45e4db3a5de3416bd8c.js?file=snippet-5.sh"></script>

This keeps the CI system stateless with respect to the cluster. It never calls `kubectl`. It just writes to Git, and ArgoCD takes over from there.

## Enforcing RBAC with AppProjects

ArgoCD `AppProject` resources define boundaries around which repositories, clusters, and namespaces a set of applications can target. This prevents a developer working on the payments team from accidentally deploying into the auth namespace or targeting a production cluster from a branch they control.

<script src="https://gist.github.com/mohashari/a8f2c1079defa45e4db3a5de3416bd8c.js?file=snippet-6.yaml"></script>

Developers in this project can trigger syncs and inspect application state, but cannot modify the project definition itself or deploy outside their designated namespaces.

## Drift Detection with Notifications

ArgoCD exposes a notification controller that can fire alerts when an application drifts out of sync or a sync fails. Configure it to post to Slack or PagerDuty using a `ConfigMap` and a `Secret` for credentials:

<script src="https://gist.github.com/mohashari/a8f2c1079defa45e4db3a5de3416bd8c.js?file=snippet-7.yaml"></script>

This closes the feedback loop. When an engineer makes a manual change to a production resource — intentionally during an incident or accidentally — the team is notified within minutes and can decide whether to commit the change to Git or let ArgoCD roll it back.

Adopting ArgoCD does not require rewriting your manifests or your CI pipelines in one shot. Start by registering a single non-critical service as an ArgoCD Application in manual sync mode. Build confidence by watching the drift detection surface discrepancies you didn't know existed. Once the team trusts the reconciliation loop, enable `selfHeal` and `prune` on progressively more critical services. The payoff is significant: deployments become observable, reproducible, and auditable by construction — not by convention. When something breaks in production, your first question shifts from "what changed?" to "which commit introduced this?", and the answer is always one `git log` away.