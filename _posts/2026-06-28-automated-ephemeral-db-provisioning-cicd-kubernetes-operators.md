---
layout: post
title: "Automated Ephemeral DB Provisioning for CI/CD Using Kubernetes Operators"
date: 2026-06-28 08:00:00 +0700
tags: [kubernetes, devsecops, cicd, postgresql]
description: "Ditch flaky shared databases and fragile Docker-in-Docker scripts. Learn how to design, write, and run a custom Kubernetes operator to provision isolated, seeded, and auto-reaped databases for your CI/CD pipelines."
image: "https://picsum.photos/seed/8985/1080/720"
thumbnail: "https://picsum.photos/seed/8985/400/300"
---

Imagine this: it's Friday afternoon, and your CI/CD pipeline is red. The culprit? An integration test failed because developer A's branch modified a row in a shared staging database, breaking developer B's concurrent pipeline execution. You hit "Re-run," wait another twelve minutes, and cross your fingers. This is the reality of shared test databases—a breeding ground for flaky tests, race conditions, and developer frustration. When you scale a backend team to 50+ engineers pushing 300+ builds per day, shared databases become an absolute bottleneck. Alternatively, mocking your database layer with tools like `go-sqlmock` or using SQLite in-memory databases hides crucial engine-specific behavior (e.g., PostgreSQL JSONB operators, transaction isolation levels, window functions). We need a solution that provides fully isolated, actual database instances on-demand, seeds them with production-like schemas, and tears them down automatically when tests finish.

## The Case for Kubernetes Operators Over Raw Shell Scripts

Standard workarounds like spinning up a Docker container on the CI runner (Docker-in-Docker) or executing Helm charts on the fly suffer from severe limitations. In a shared Kubernetes runner environment, launching multiple database instances using shell scripts leaves orphan containers whenever pipeline jobs crash or are aborted. In contrast, a Kubernetes Operator behaves as a continuous reconciliation loop. It manages the full lifecycle of custom database resources, ensures schema migrations are executed, exposes credentials securely, and automatically sweeps away expired resources via Time-to-Live (TTL) reapers, even if the CI pipeline runner completely vanishes.

By defining an `EphemeralDatabase` Custom Resource Definition (CRD), we can allow pipelines to declare their data requirements directly. The operator handles:
1. Provisioning a clean database pod (e.g., PostgreSQL).
2. Running migrations using the target schema version.
3. Exposing access credentials via a temporary Kubernetes Secret.
4. Monitoring the lifecycle and auto-deleting the database after a specified TTL.

## Designing the Custom Resource Definition (CRD)

To represent our ephemeral database, we define a Custom Resource named `EphemeralDatabase`. The spec must capture the database engine type, version, schema migration details, CPU/memory constraints, and the TTL.

<script src="https://gist.github.com/mohashari/31aeff82d03b329888f75b0a8889bd7c.js?file=snippet-1.yaml"></script>

This CRD schema ensures that all ephemeral databases are scoped to a namespace (typically the CI runner's namespace) and provides a TTL constraint to enforce hygiene.

## Implementing the Reconciler Loop in Go

Using Kubebuilder and the `controller-runtime` library in Go, we write the reconciliation logic. The controller's primary job is to ensure that for every `EphemeralDatabase` resource, a corresponding database deployment and service exist, and that a schema migration job is triggered.

<script src="https://gist.github.com/mohashari/31aeff82d03b329888f75b0a8889bd7c.js?file=snippet-2.go"></script>

The reconciler creates the Deployment, saves the creation time to `.status.createdAt`, and tells the runtime to check back when the resource's time has run out. 

## Orchestrating TTL and Garbage Collection

Relying on the client runner to clean up resources is a security and operational risk. If a pipeline is forcefully cancelled or the runner pod loses connection, teardown scripts won't run. The operator must act as the ultimate reaper.

We implement a dedicated, lightweight background manager in Go that sweeps resources whose active age exceeds their configured TTL. While the reconciler's `RequeueAfter` is the primary path, the TTL reaper acts as a backup scanner.

<script src="https://gist.github.com/mohashari/31aeff82d03b329888f75b0a8889bd7c.js?file=snippet-3.go"></script>

By separating this concern, you prevent transient reconciliation errors from locking resources on your nodes. It runs as a goroutine alongside your manager instance.

## Schema Migration and Seeding

An empty database is useless for integration tests. We must seed the schema and core static lookups (e.g., countries, roles, config matrices) before the application code connects. To do this, the controller launches a Kubernetes Job that executes toolings like `golang-migrate` or `Flyway` right after the database Pod enters the `Running` phase.

The following manifest represents the migration Job executed by the operator. It uses `golang-migrate` to execute scripts stored in a configmap or downloaded from a Git repository:

<script src="https://gist.github.com/mohashari/31aeff82d03b329888f75b0a8889bd7c.js?file=snippet-4.yaml"></script>

The init container `wait-for-db` is crucial here. In a production cluster, a database pod transition to `Running` does not mean the PostgreSQL engine has finished initializing files on disk and is ready to accept connections. The `nc` (netcat) check prevents the migration job from failing during PostgreSQL startup.

## Integrating the Operator into CI/CD Pipelines

To leverage our operator in GitHub Actions, the workflow needs to create the custom resource, poll its status until it registers as `Ready`, execute integration tests, and then explicitly delete the resource.

<script src="https://gist.github.com/mohashari/31aeff82d03b329888f75b0a8889bd7c.js?file=snippet-5.yaml"></script>

To coordinate the pipeline waiting for readiness and parsing the database connection string, we write a companion script within our codebase:

<script src="https://gist.github.com/mohashari/31aeff82d03b329888f75b0a8889bd7c.js?file=snippet-6.sh"></script>

Using `$GITHUB_OUTPUT` makes it trivial to feed the dynamically provisioned endpoint connection string directly to subsequent runner jobs.

## Production Pitfalls & Hard-Won Mitigations

Operating ephemeral databases in high-throughput clusters introduces non-obvious failure modes that can cripple your infrastructure if unmitigated.

### 1. PV Provisioning Latency and Disk Leakage
If you rely on dynamic Persistent Volume (PV) creation through cloud-based storage classes (e.g., AWS EBS gp3 or GCP pd-standard), provisioning a volume can take anywhere from 15 to 45 seconds. For a test suite that runs in under two minutes, this overhead is unacceptable. 
Furthermore, cloud providers have rate limits on volume provisioning API requests, leading to pipeline stalling when multiple builds run concurrently.

**Mitigation**: Do not write test database data to persistent cloud volumes. Configure your operator to deploy PostgreSQL pods utilizing an `emptyDir` volume backed by `Medium: Memory` (tmpfs).

<script src="https://gist.github.com/mohashari/31aeff82d03b329888f75b0a8889bd7c.js?file=snippet-7.yaml"></script>

This hosts the entire database in the node's RAM. It eliminates provisioning latency entirely, speeds up database write speeds by 10x, and ensures that when the pod dies, the memory is immediately reclaimed by the OS, leaving zero residual disk artifacts.

### 2. CPU and Memory Throttling
Concurrent pipeline executions can quickly saturate node resources if limits aren't set. If your test database pods do not have strict resource requests, Kubernetes' scheduler will overcommit the node. When tests trigger heavy write transactions, CPU spikes, leading to throttling of the CI runner containers and false test failures due to timeouts.

**Mitigation**: Set strict, optimized resource limits tailored for transient, low-load execution. For a test database, you do not need production settings.

<script src="https://gist.github.com/mohashari/31aeff82d03b329888f75b0a8889bd7c.js?file=snippet-8.yaml"></script>

Configure your PostgreSQL container config settings inside the operator to run with lightweight memory footprint parameters:

* `max_connections = 50` (tests rarely require more)
* `shared_buffers = 128MB` (conserves pod memory)
* `fsync = off` (safe to disable since data persistence is not required during test execution)

### 3. Namespace Network Isolation
Since ephemeral databases are deployed dynamically on a shared Kubernetes cluster, they present a security attack vector. A compromised application runner or testing environment pod in another namespace could potentially scan the network and connect to databases containing seed schemas and mock user records.

**Mitigation**: Enforce namespace isolation using Kubernetes `NetworkPolicies`. The operator should deploy a policy alongside each ephemeral database to restrict inbound TCP port 5432/3306 traffic only to pods residing in the exact same runner namespace.

<script src="https://gist.github.com/mohashari/31aeff82d03b329888f75b0a8889bd7c.js?file=snippet-9.yaml"></script>

This guarantees network-level isolation on your testing cluster, satisfying security audits without adding complexity to developer setups.

## Summary

Moving to automated, ephemeral database provisioning via Kubernetes Operators provides:
* **True Isolation**: No more test contamination or race conditions.
* **Deterministic Environment**: Tests run on the actual target DB engine (same version, same features), not an in-memory toy database.
* **Zero Maintenance Cleanup**: Built-in TTL reapers guarantee that nodes never leak resources, even when CI jobs crash catastrophically.
* **High Performance**: Transitioning to `emptyDir` memory mounts ensures database transactions complete in milliseconds rather than seconds.

Building your own controller takes only a few hundred lines of Go, yet it yields immense returns in development velocity, pipeline stability, and security compliance.