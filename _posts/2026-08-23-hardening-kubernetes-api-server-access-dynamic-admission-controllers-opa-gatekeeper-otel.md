---
layout: post
title: "Hardening Kubernetes API Server Access: Dynamic Admission Controllers with OPA Gatekeeper and OTel Tracing"
date: 2026-08-23 08:00:00 +0700
tags: [kubernetes, devsecops, gatekeeper, opentelemetry, tracing]
description: "Hardening the Kubernetes API server using OPA Gatekeeper constraints and tracing admission webhook latencies with OpenTelemetry to prevent production outages."
image: "https://picsum.photos/seed/8142/1080/720"
thumbnail: "https://picsum.photos/seed/8142/400/300"
---

In a high-throughput Kubernetes production cluster, a single unchecked resource deployment can lead to complete cluster-wide instability or massive security exposure. Consider this common production disaster: an automated CI/CD pipeline deploys a misconfigured service manifest that omits resource limits, mounts the host node's Docker socket, and pulls an unvetted image from a public registry. Within seconds, the container runs as root, executes a cryptominer, and consumes all CPU cycles on the host node, starving the kubelet and system daemons. While static linting in CI/CD catches some errors, it cannot validate dynamic runtime context, cluster state, or user identity. Dynamic admission controllers—such as Open Policy Agent (OPA) Gatekeeper—serve as the final, immutable gatekeeper at the Kubernetes API server entry point. However, inserting webhooks into the critical path of the Kubernetes API server introduces a dangerous dependency: if your admission controller becomes slow, the entire Kubernetes control plane grinds to a halt. To prevent security rules from causing self-inflicted denial-of-service outages, you must wrap policy enforcement in a robust observability wrapper using OpenTelemetry (OTel) tracing.

## The Admission Control Lifecycle and the Latency Budget

When a client (like `kubectl` or a GitOps controller) makes a request to the Kubernetes API server, the request traverses a strict, multi-stage lifecycle before the resource is persisted in etcd. The phases of interest are **Mutating Admission**, **Object Schema Validation**, and **Validating Admission**.

Dynamic admission webhooks are invoked over HTTP/2 during the Mutating and Validating phases. The API server sends an `AdmissionReview` request to the webhook service, which must respond with an `AdmissionReview` containing a boolean `allowed` flag and an optional status message.

The defining operational constraint here is the **Latency Budget**. By default, Kubernetes allows webhooks up to 10 seconds to respond. In a busy, production-grade cluster, a 10-second timeout is a ticking time bomb. If your API server experiences a spike in write requests (e.g., during a deployment rollout or auto-scaling event) and your admission webhook takes even 500ms to respond, the API server's request queue will rapidly saturate. This leads to API server thread exhaustion, cascading timeouts, and client disconnects. To defend the control plane, production clusters must dial this timeout down to **2 or 3 seconds**.

Setting a tight timeout requires you to make a stark architectural decision: **Fail-Open or Fail-Closed**.
*   **`failurePolicy: Fail` (Fail-Closed):** If the webhook is unreachable, times out, or returns a 5xx error, the API request is rejected. This is the only acceptable setting for security and compliance policies. However, it means a webhook outage will block all deployments, namespace creations, and scaling events.
*   **`failurePolicy: Ignore` (Fail-Open):** If the webhook fails, the API server logs the error but allows the request. This preserves availability but bypasses security controls.

To confidently run a fail-closed webhook, you must have absolute visibility into its performance. This is where OpenTelemetry tracing becomes indispensable, allowing you to trace admission requests from the API server through your network, into the policy engine, and back.

## Designing the Observability Architecture: Gatekeeper + OTel

OPA Gatekeeper utilizes a controller-manager that runs inside the cluster to evaluate admission requests against policies defined as Custom Resource Definitions (CRDs). The policies themselves are authored in **Rego**, a declarative query language. 

To monitor this setup, we deploy an OpenTelemetry Collector as a sidecar or a central agent. Gatekeeper emits prometheus metrics out-of-the-box, but monitoring metrics only shows you aggregated latencies—it doesn't tell you *which* specific deployment, container, or namespace caused a latency spike, nor does it trace the network path. 

By instrumenting the webhook infrastructure with OTel tracing, every admission request generates a trace with a distinct parent span from the API server (if W3C trace context propagation is enabled) or starts a root span at the webhook level. The spans capture critical attributes such as the resource Group-Version-Kind (GVK), name, namespace, user agent, request UID, and the execution time of individual Rego queries. This telemetry is forwarded to the OTel Collector, which batches and exports it to a distributed tracing backend like Jaeger, Grafana Tempo, or AWS X-Ray.

## Implementing OPA Gatekeeper Constraints

Let's look at how to construct a robust, production-quality policy configuration. We will define a policy that ensures images are pulled only from verified internal registries, containers do not run as privileged, and resources limits are declared.

First, we create a `ConstraintTemplate` which defines the Rego validation logic and the schema for its parameters.

<script src="https://gist.github.com/mohashari/1c9d0a4c74d8d88d4a20d3d60db31ea5.js?file=snippet-1.yaml"></script>

Next, we define the corresponding `Constraint` resource to enforce this template in specific namespaces.

<script src="https://gist.github.com/mohashari/1c9d0a4c74d8d88d4a20d3d60db31ea5.js?file=snippet-2.yaml"></script>

## Writing a Custom Instrumented Admission Webhook in Go

While Gatekeeper is excellent for declarative OPA policy enforcement, many backend engineering teams find themselves writing custom admission webhooks. This is typical when the validation logic requires dynamic state checking, database queries (e.g., verifying if a department ID label exists in an external database), or complex mutating logic that is highly imperative.

To ensure a custom webhook does not compromise API server reliability, it must be instrumented using the official OpenTelemetry Go SDK. Below is a production-ready implementation of a validating webhook server that parses an `AdmissionReview` request, wraps the validation phase in an OTel span, adds metadata as span attributes, and records errors.

<script src="https://gist.github.com/mohashari/1c9d0a4c74d8d88d4a20d3d60db31ea5.js?file=snippet-3.go"></script>

## Configuring the Webhook and the OpenTelemetry Pipeline

To wire this custom webhook server into the Kubernetes lifecycle, we must apply a `ValidatingWebhookConfiguration`. This configuration enforces the tight 3-second timeout we discussed earlier and ensures system namespaces are skipped to avoid cluster initialization deadlocks.

<script src="https://gist.github.com/mohashari/1c9d0a4c74d8d88d4a20d3d60db31ea5.js?file=snippet-4.yaml"></script>

The telemetry generated by both OPA Gatekeeper and our custom webhook needs to be processed. We deploy an OpenTelemetry Collector to accept spans via OTLP (gRPC/HTTP), batch them, and route them to Grafana Tempo. The collector also runs a memory limiter to prevent the telemetry agent itself from OOMing under heavy cluster load.

<script src="https://gist.github.com/mohashari/1c9d0a4c74d8d88d4a20d3d60db31ea5.js?file=snippet-5.yaml"></script>

## Operational Troubleshooting and Metrics Alerting

In production, your first line of defense is monitoring aggregated admission controller metrics. When latency shifts or the API server begins throwing 500 errors during deployments, you need immediate alerts. You can implement Prometheus alert rules to detect when the 99th percentile evaluation latency of OPA Gatekeeper constraints exceeds critical levels.

<script src="https://gist.github.com/mohashari/1c9d0a4c74d8d88d4a20d3d60db31ea5.js?file=snippet-6.yaml"></script>

If the alert triggers, your troubleshooting workflow should transition from metrics to traces:

1.  **Locate the Outlier Spans:** Query your tracing system (Tempo/Jaeger) for spans where `otel.library.name == "admission-webhook-tracer"` or Gatekeeper spans where the duration exceeds 200ms.
2.  **Examine Span Metadata:** Filter by `k8s.admission.namespace` and `k8s.admission.kind` to see if a specific tenant namespace is generating exceptionally large objects (e.g., massive ConfigMaps or custom resources with thousands of lines).
3.  **Inspect Rego Evaluation Traces:** If using Gatekeeper, examine if specific complex rules containing multiple nested iterations are blowing up CPU cycles.

## Performance Hardening & Mitigating Control Plane Deadlocks

Hardening Kubernetes API server access is a double-edged sword. To keep your admission control layer reliable and performant in high-scale environments, follow these operational rules:

### Avoid Namespace Bootstrapping Deadlocks
If your webhook is configured as `failurePolicy: Fail` and intercepts all namespaces, what happens when the entire cluster restarts? The webhook pods themselves (in the `security-system` or `gatekeeper-system` namespace) cannot be scheduled or started because the API server cannot validate their Pod creation requests since the webhook isn't running yet. 

Always exclude system namespaces (`kube-system`, `gatekeeper-system`, and the namespace containing the webhook itself) from the `ValidatingWebhookConfiguration` rules using `namespaceSelector.matchExpressions` as demonstrated in snippet-4.

### The Threat of Namespace Label Mutation
Relying entirely on `namespaceSelector` introduces a severe vulnerability. If a malicious actor (or a compromised service account) has permissions to edit namespaces, they can label their namespace with `security.enterprise.io/bypass: true` and completely bypass your security policies. 

To mitigate this, implement a strict validating webhook rule that prevents any namespace modification adding the bypass label, or switch to using `objectSelector` configurations targeting resource-specific metadata that normal users cannot edit.

### Webhook Mutation Order and Idempotency
If you utilize mutating webhooks alongside validating ones, be aware that Kubernetes processes mutating webhooks first, sequentially. Once mutated, the final object is sent to validating webhooks. Since mutating webhooks can be called multiple times (if one mutation triggers another pass), keep your custom webhooks idempotent. Any latency introduced in the mutation phase directly eats into the overall request budget before the validation phase even begins.

### Optimizing Rego Code
Rego is incredibly powerful, but O(N^2) algorithms are easy to write accidentally. Avoid checking every pod in the cluster inside a single rule. Instead, rely on Gatekeeper’s replication mechanism (syncing cluster state to OPA cache) with extreme caution, and monitor the size of the OPA memory footprint. Ensure that the rules fail fast.

## Conclusion

By coupling OPA Gatekeeper with a well-configured OpenTelemetry tracing pipeline, you enforce robust, declarative security policies without operating in the dark. Your security posture changes from reactive vulnerability management to proactive admission prevention, backed by the deep performance profiling needed to keep your Kubernetes control plane fast, reliable, and secure under load.