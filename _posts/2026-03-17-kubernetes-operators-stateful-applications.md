---
layout: post
title: "Kubernetes Operators: Extending the Control Plane for Stateful Applications"
date: 2026-03-17 07:00:00 +0700
tags: [kubernetes, operators, go, devops, cloud-native]
description: "Build custom Kubernetes Operators with the controller-runtime SDK to automate lifecycle management of stateful, complex applications."
---

Managing stateful applications on Kubernetes has always been the hard part. Deployments work beautifully for stateless services, but when you need to run a database cluster, a message broker, or any system that carries state between restarts, you quickly discover that Kubernetes primitives alone aren't enough. You need something that understands your application's domain: how to bootstrap it, how to scale it safely, how to handle failover, and how to run backups. This is exactly the problem Kubernetes Operators were designed to solve. An Operator encodes operational knowledge into software, extending the control plane with custom resources and reconciliation loops that continuously drive your application toward its desired state — the same way Kubernetes manages its own built-in resources.

## What Is an Operator?

An Operator is a Kubernetes controller that watches one or more Custom Resource Definitions (CRDs) and acts on them. The pattern follows the same control loop at the heart of every Kubernetes component: observe current state, compare to desired state, and reconcile the difference. What makes Operators powerful is that "desired state" is now expressed in terms of your application's semantics — not just replica counts and resource limits.

The `controller-runtime` SDK, maintained by the Kubernetes SIG, gives Go developers a clean framework for building Operators without reinventing the wheel. It handles leader election, caching, event queuing, and webhook serving, letting you focus on the reconciliation logic itself.

## Defining a Custom Resource

Start by defining the schema for your custom resource. Here, we're modeling a `PostgresCluster` that specifies a version, replica count, and storage size:

<script src="https://gist.github.com/mohashari/807c56538f23d7ad28f1c6c32ff1847b.js?file=snippet.go"></script>

The `+kubebuilder` marker comments are processed by `controller-gen` to generate the CRD YAML and deepcopy functions. This code-first approach keeps your schema and Go types in sync automatically.

## The CRD Manifest

Running `make generate manifests` with kubebuilder produces the CRD definition that you apply to the cluster. A trimmed version looks like this:

<script src="https://gist.github.com/mohashari/807c56538f23d7ad28f1c6c32ff1847b.js?file=snippet-2.yaml"></script>

The `subresources.status` section is critical — it ensures the `/status` subresource is handled separately from the main object, preventing controllers from accidentally overwriting spec fields when they update status.

## Writing the Reconciler

The reconciler is where your operational knowledge lives. The `Reconcile` function is called whenever a `PostgresCluster` object is created, updated, or deleted, and also periodically to self-heal against drift:

<script src="https://gist.github.com/mohashari/807c56538f23d7ad28f1c6c32ff1847b.js?file=snippet-3.go"></script>

Notice that `IgnoreNotFound` on the initial `Get` is not optional — it handles the case where a delete event fires but the object is already gone by the time you process it.

## Creating Child Resources with Owner References

When your Operator creates child resources like StatefulSets or Services, you must set owner references so that garbage collection works correctly. When the parent `PostgresCluster` is deleted, Kubernetes will cascade-delete everything it owns:

<script src="https://gist.github.com/mohashari/807c56538f23d7ad28f1c6c32ff1847b.js?file=snippet-4.go"></script>

Using `Patch` instead of `Update` for modifications is a best practice — it sends only the delta and avoids resource version conflicts under high concurrency.

## Handling Finalizers for Cleanup Logic

Some stateful applications require cleanup that Kubernetes can't do on its own — snapshotting volumes, deregistering from a service registry, or flushing write-ahead logs. Finalizers let you run this logic before the object is actually removed:

<script src="https://gist.github.com/mohashari/807c56538f23d7ad28f1c6c32ff1847b.js?file=snippet-5.go"></script>

If `runPreDeleteBackup` returns an error, the finalizer remains, the object stays in a terminating state, and your Operator will retry — giving you durable, retryable cleanup semantics without any external job scheduler.

## Running and Deploying the Operator

During development, you can run the Operator outside the cluster against your current kubeconfig context:

<script src="https://gist.github.com/mohashari/807c56538f23d7ad28f1c6c32ff1847b.js?file=snippet-6.sh"></script>

For production, build a minimal container image and deploy via a standard `Deployment` with RBAC roles scoped to exactly the resources your Operator needs to read and write — nothing more.

<script src="https://gist.github.com/mohashari/807c56538f23d7ad28f1c6c32ff1847b.js?file=snippet-7.dockerfile"></script>

The two-stage build keeps the final image under 20MB and eliminates the Go toolchain from the attack surface entirely.

Kubernetes Operators represent the maturation of cloud-native operations: instead of runbooks and on-call procedures, you encode your application's operational intelligence directly into the platform. The `controller-runtime` SDK handles the infrastructure concerns — caching, event deduplication, leader election — so your code stays focused on what your application actually needs. Start with a single CRD and a reconciler that manages one child resource. Add finalizers only when you have real cleanup work to do. Over time, as your Operator handles more of your operational surface area, you'll find that the gap between "deployed" and "managed" finally closes.