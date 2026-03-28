---
layout: post
title: "Kubernetes Admission Webhooks: Building Mutating and Validating Controllers in Go"
date: 2026-03-28 08:00:00 +0700
tags: [kubernetes, go, devsecops, platform-engineering, admission-webhooks]
description: "Build production-grade mutating and validating admission webhooks in Go to enforce security policies and inject sidecars at scale."
image: "https://picsum.photos/1080/720?random=4906"
thumbnail: "https://picsum.photos/400/300?random=4906"
---

Three weeks before a SOC 2 audit, you discover that 40% of your production pods are running as root. No resource limits. No readiness probes. Some with `hostNetwork: true` that nobody remembers adding. The team has grown to 15 engineers deploying across 6 namespaces, and no amount of documentation or Slack reminders has kept the manifests clean. The real fix isn't a process — it's making the API server reject bad configs before they land.

That's what admission webhooks are for. They're the enforcement layer between `kubectl apply` and etcd, and once you've built one, you'll wonder how you shipped anything without them.

## What Actually Happens During Admission

Before a resource is persisted, the Kubernetes API server runs it through two webhook phases in order:

1. **Mutating admission** — webhooks can modify the object (inject sidecars, set defaults, add labels)
2. **Validating admission** — webhooks can reject the object based on policy

Both phases fan out to all registered webhooks in parallel within a phase, then collect results. If any validating webhook returns a non-2xx response or explicitly denies the request, the entire operation fails with the webhook's message surfaced to the user.

The critical implication: mutations happen *before* validation. Your mutating webhook can normalize a spec, then your validating webhook enforces the normalized form. This ordering is intentional and powerful.

Each webhook receives an `AdmissionReview` request and must return an `AdmissionReview` response. The API is simple. The operational concerns are not.

## TLS Is Not Optional

Webhooks must be served over HTTPS. The API server validates the webhook server's certificate against the `caBundle` in your `MutatingWebhookConfiguration`. No exceptions, no `insecureSkipVerify`.

In practice, there are three approaches:

- **cert-manager** — the production standard; use `Certificate` resources with `spec.dnsNames` matching your service
- **Manual self-signed** — works, but you're now rotating certificates manually
- **In-cluster CA** — use the `CertificateSigningRequest` API to get your cert signed by the cluster CA

For anything beyond a dev cluster, use cert-manager with the `inject-ca-from` annotation on your webhook configuration. It auto-rotates the `caBundle` when the certificate renews.

```yaml
# snippet-1
apiVersion: admissionregistration.k8s.io/v1
kind: MutatingWebhookConfiguration
metadata:
  name: pod-defaults
  annotations:
    cert-manager.io/inject-ca-from: webhook-system/pod-defaults-tls
webhooks:
  - name: pod-defaults.webhook-system.svc
    admissionReviewVersions: ["v1"]
    clientConfig:
      service:
        name: pod-defaults
        namespace: webhook-system
        path: /mutate
        port: 443
    rules:
      - operations: ["CREATE", "UPDATE"]
        apiGroups: [""]
        apiVersions: ["v1"]
        resources: ["pods"]
        scope: "Namespaced"
    namespaceSelector:
      matchExpressions:
        - key: webhook.io/inject
          operator: In
          values: ["true"]
    failurePolicy: Fail
    sideEffects: None
    timeoutSeconds: 5
```

Note `failurePolicy: Fail`. In production, you want this. If your webhook is unreachable, reject the request — don't silently let it through. The alternative, `Ignore`, means your policy enforcement has a silent bypass whenever your webhook restarts.

## The Webhook Server Skeleton

A webhook server is just an HTTPS server with two endpoints. Here's the foundation you'll build everything on:

<script src="https://gist.github.com/mohashari/80a1ee1fb430fba8d1119a3be55bd07d.js?file=snippet-2.go"></script>

The 30-second graceful shutdown window matters. During a rolling deploy of your webhook itself, in-flight admission requests need time to complete. Without it, you'll see sporadic failures as old pods die mid-request.

## Mutating: Injecting Defaults

The most common mutation is setting resource defaults. Teams will ship pods without limits; your webhook enforces them. The response uses a JSON Patch array:

<script src="https://gist.github.com/mohashari/80a1ee1fb430fba8d1119a3be55bd07d.js?file=snippet-3.go"></script>

One gotcha: JSON Patch path segments containing `/` must be escaped as `~1`. The label key `app.kubernetes.io/managed-by` becomes `app.kubernetes.io~1managed-by` in a patch path. I've seen this break in production when someone adds a label with a slash and wonders why the webhook panics.

## Validating: Enforcing Security Policy

Validation is where you catch what mutation didn't fix and what should be outright rejected. A production security policy might enforce:

<script src="https://gist.github.com/mohashari/80a1ee1fb430fba8d1119a3be55bd07d.js?file=snippet-4.go"></script>

## Sidecar Injection Pattern

Injecting a sidecar — the Istio/Linkerd pattern — is a specific mutation that deserves its own treatment. The pod spec you receive may already have containers, so you append rather than replace:

<script src="https://gist.github.com/mohashari/80a1ee1fb430fba8d1119a3be55bd07d.js?file=snippet-5.go"></script>

The `/spec/containers/-` path is the JSON Patch append syntax. Using a specific index like `/spec/containers/2` is fragile — if your mutation runs after another webhook that also added a container, the index is off.

## Testing Without a Cluster

Unit testing webhook logic is straightforward — serialize a pod to JSON, call `handleMutate` or `handleValidate`, inspect the response. For integration tests, use `envtest` from `sigs.k8s.io/controller-runtime`:

<script src="https://gist.github.com/mohashari/80a1ee1fb430fba8d1119a3be55bd07d.js?file=snippet-6.go"></script>

`envtest` spins up a real API server and etcd locally, wires your webhook server in via the installed configuration, and gives you a real `client.Client`. Tests run in under 10 seconds and catch the JSON Patch path bugs that unit tests miss.

## Operational Reality

**Latency budget**: You have `timeoutSeconds` (max 30, default 10). Your webhook P99 should be under 200ms, or you'll start seeing cascading failures during bursts. Profile your handler. If you're making external calls (Vault, OPA), cache aggressively.

**Scope your rules tightly**: Avoid `operations: ["*"]` and `resources: ["*"]`. A webhook that fires on every resource type in the cluster will receive ConfigMaps, Secrets, Events — volume you didn't plan for. Start with `pods` in specific namespaces and expand deliberately.

**Dry-run support**: The `req.DryRun` field is true when `kubectl apply --dry-run=server` is used. Your webhook should handle this gracefully — don't mutate external state in response to dry-run requests.

**Namespace exclusions**: Always exclude `kube-system` and your webhook's own namespace from the `namespaceSelector`. If your webhook crashes and tries to restart, it shouldn't block its own pod from starting. Use a label-based selector and explicitly opt namespaces in.

**Metrics**: Expose a `/metrics` endpoint with Prometheus. Track `webhook_request_duration_seconds` as a histogram (buckets: 10ms, 50ms, 100ms, 500ms, 1s), `webhook_requests_total` by operation and result, and `webhook_violations_total` by policy name. The violations metric is particularly useful — it shows you which policies are actually firing in production before you make them hard-rejections.

## The Graduation Path

Start permissive: deploy as `failurePolicy: Ignore` with mutations only. Watch the logs to see what you're catching. After two weeks of signal, promote violations to warnings (log, but allow). After another two weeks, flip to `failurePolicy: Fail` for the validating webhook.

This mirrors feature flagging in application code — you get production signal without production blast radius. The teams shipping bad manifests will also see the warning messages in their CI logs, which gives them time to fix before you enforce.

Admission webhooks aren't bureaucracy. They're the difference between "we have a policy document" and "that configuration cannot reach production." Build the webhook, tune the policy against real workloads, then enforce it. The audit finding about root containers stops being a finding the next time someone tries to deploy one.
```