---
layout: post
title: "GitOps with ArgoCD: Declarative Continuous Delivery for Kubernetes"
date: 2026-03-15 07:00:00 +0700
tags: [gitops, argocd, kubernetes, devops, cicd]
description: "Implement GitOps workflows with ArgoCD to automate Kubernetes deployments using Git as the single source of truth."
---

Managing Kubernetes deployments manually is a recipe for configuration drift, deployment anxiety, and 3 AM incidents. When your cluster state diverges from what you *think* it is, debugging becomes an archaeological dig through `kubectl apply` history and Slack messages. GitOps solves this by making Git the single source of truth for your infrastructure: if it's not in a repository, it doesn't exist. ArgoCD implements this pattern natively for Kubernetes, continuously reconciling your cluster state with what's declared in Git. This post walks through setting up a production-grade GitOps pipeline with ArgoCD, covering application definitions, sync policies, health checks, and multi-environment promotion.

## Why GitOps Changes the Deployment Model

Traditional CI/CD pushes changes into a cluster — your pipeline runs `kubectl apply` or `helm upgrade` and hopes for the best. GitOps inverts this. The cluster *pulls* its desired state from Git, and a controller constantly watches for drift. This has three meaningful consequences for backend engineers. First, every change is auditable: your Git history *is* your deployment history. Second, rollbacks are `git revert` — no tribal knowledge required. Third, disaster recovery becomes deterministic: a new cluster pointed at the same repository will converge to identical state.

ArgoCD implements this as a Kubernetes controller that watches Git repositories and applies changes when the cluster diverges from the declared state. It supports plain Kubernetes manifests, Helm charts, Kustomize overlays, and Jsonnet. You install it into your cluster and it manages everything else from there.

## Installing ArgoCD

Start by installing ArgoCD into a dedicated namespace. This gives you the API server, repository server, application controller, and UI out of the box.

<script src="https://gist.github.com/mohashari/c2a4658e432113517176617adc42fe02.js?file=snippet.sh"></script>

Once ArgoCD is running, install the `argocd` CLI and log in. For production, you'll want to expose the API server behind an ingress or LoadBalancer and integrate with your SSO provider via Dex, but for now port-forwarding works for initial setup.

## Defining Your First Application

An ArgoCD `Application` is a Kubernetes custom resource that maps a Git source to a cluster destination. This is the fundamental unit of GitOps in ArgoCD — every workload you want managed should have one.

<script src="https://gist.github.com/mohashari/c2a4658e432113517176617adc42fe02.js?file=snippet-2.yaml"></script>

The `prune: true` flag tells ArgoCD to delete resources that exist in the cluster but are no longer in Git — this enforces Git as the authoritative source. `selfHeal: true` means if someone runs a manual `kubectl apply` that diverges from Git, ArgoCD will revert it within three minutes. The retry backoff prevents a misconfigured application from hammering the API server.

## Structuring Manifests with Kustomize Overlays

For multi-environment setups, Kustomize overlays let you share a base configuration and layer environment-specific changes on top. This avoids duplicating manifests across `dev`, `staging`, and `production` directories.

<script src="https://gist.github.com/mohashari/c2a4658e432113517176617adc42fe02.js?file=snippet-3.yaml"></script>

<script src="https://gist.github.com/mohashari/c2a4658e432113517176617adc42fe02.js?file=snippet-4.yaml"></script>

The image tag in the overlay is what your CI pipeline updates — not the base manifests. A tool like `kustomize edit set image` or a simple `sed` in your pipeline can bump this value and commit it back to Git, triggering ArgoCD to sync.

## Automating Image Tag Updates from CI

Your CI pipeline builds and pushes the image, then updates the Git repository with the new tag. This is the "write to Git" step that kicks off the GitOps loop.

<script src="https://gist.github.com/mohashari/c2a4658e432113517176617adc42fe02.js?file=snippet-5.sh"></script>

The `[skip ci]` tag in the commit message prevents your CI system from re-triggering a build on the manifest update commit — without this, you'd create an infinite loop.

## Defining AppProjects for RBAC and Policy

ArgoCD's `AppProject` resource scopes what a set of applications can deploy, to which clusters, and from which repositories. This is critical for multi-tenant clusters where you don't want one team's misconfigured application affecting another namespace.

<script src="https://gist.github.com/mohashari/c2a4658e432113517176617adc42fe02.js?file=snippet-6.yaml"></script>

The blacklist on `ResourceQuota` prevents applications from modifying their own resource constraints — a subtle but important guardrail that stops a runaway team from granting themselves unlimited CPU.

## Custom Health Checks for CRDs

ArgoCD includes health checks for standard Kubernetes resources, but for custom resources you need to define your own. These are Lua scripts that inspect the resource and return a health status. This matters because ArgoCD's sync waves and hooks depend on accurate health status to sequence deployments correctly.

<script src="https://gist.github.com/mohashari/c2a4658e432113517176617adc42fe02.js?file=snippet-7.txt"></script>

Add this to the `argocd-cm` ConfigMap under `resource.customizations.health.<group_kind>`. ArgoCD evaluates it on every sync and surfaces the result in the UI and CLI.

## Sync Waves for Ordered Deployments

Complex applications have dependencies — databases before application servers, CRDs before controllers. ArgoCD's sync waves let you declare this ordering declaratively using annotations on individual resources.

<script src="https://gist.github.com/mohashari/c2a4658e432113517176617adc42fe02.js?file=snippet-8.yaml"></script>

Resources in lower waves must reach a healthy state before ArgoCD proceeds to higher waves. The `PreSync` hook runs the migration before any other resources are applied, giving you safe schema migrations as a first-class deployment primitive.

GitOps with ArgoCD is ultimately about making the implicit explicit. Every configuration decision, every replica count, every resource limit lives in a repository with a commit message explaining why it changed. The operational benefits — auditability, rollback simplicity, disaster recovery — are significant, but the deeper shift is cultural: your cluster becomes a reflection of your Git history, and deployments become code reviews rather than shell commands run from someone's laptop. Start with a single non-critical service, validate your sync policies, and expand from there. The investment in repository structure and tooling pays back quickly the first time you need to answer "what exactly changed at 2 PM on Tuesday?"