---
layout: post
title: "OPA Rego Policy Enforcement in Kubernetes Admission Controllers"
date: 2026-03-26 08:00:00 +0700
tags: [kubernetes, devsecops, opa, policy-as-code, admission-controllers]
description: "How to enforce OPA Rego policies in Kubernetes admission controllers to catch misconfigurations before they reach production."
image: "https://picsum.photos/1080/720?random=7652"
thumbnail: "https://picsum.photos/400/300?random=7652"
---

Three weeks before a PCI audit, a developer pushed a Deployment with `privileged: true` to production because nothing stopped them. The container ran as root, mounted the host filesystem, and sat next to payment processing workloads for 72 hours before a security engineer caught it during a manual review. The fix was one line of YAML. The exposure window was three days. The audit finding cost two weeks of remediation work. This is the problem OPA Rego solves — not after the fact, but at admission time, before the object ever lands in etcd.

Kubernetes admission controllers are the last enforcement boundary before a resource is persisted. Misconfigured RBAC, overly permissive network policies, and bloated pod specs routinely slip through CI pipelines because developers aren't security engineers and static linters don't understand cluster context. Open Policy Agent with Rego gives you a programmable policy layer that evaluates every resource mutation and creation against your organization's exact requirements, in real time, with structured violation messages that developers can actually act on.

## How Admission Controllers Work

The Kubernetes API server processes requests through two webhook phases: mutating admission and validating admission. Mutating webhooks run first and can modify the object. Validating webhooks run second and can only allow or deny. OPA Gatekeeper (the production-grade deployment of OPA for Kubernetes) operates as a validating admission webhook.

When a `kubectl apply` lands at the API server, it hits your Gatekeeper webhook endpoint. Gatekeeper evaluates the incoming object against all active `ConstraintTemplate` and `Constraint` resources. If any policy returns a violation, the API server returns a 403 with your violation message. The object never reaches etcd. Nothing gets scheduled.

This matters because it's synchronous and authoritative. Unlike audit-mode tools that report drift after the fact, admission controllers block at write time. The developer sees the failure in their terminal immediately, not in a Slack alert three hours later.

## The Gatekeeper Data Model

Gatekeeper splits policy into two CRDs: `ConstraintTemplate` defines the Rego logic and schema, `Constraint` instantiates that template with specific parameters. This separation lets platform teams write generic policies that product teams configure per-namespace or per-cluster.

```yaml
# snippet-1
apiVersion: templates.gatekeeper.sh/v1
kind: ConstraintTemplate
metadata:
  name: k8srequiredlabels
spec:
  crd:
    spec:
      names:
        kind: K8sRequiredLabels
      validation:
        openAPIV3Schema:
          type: object
          properties:
            labels:
              type: array
              items:
                type: string
  targets:
    - target: admission.k8s.gatekeeper.sh
      rego: |
        package k8srequiredlabels

        violation[{"msg": msg}] {
          provided := {label | input.review.object.metadata.labels[label]}
          required := {label | label := input.parameters.labels[_]}
          missing := required - provided
          count(missing) > 0
          msg := sprintf("Missing required labels: %v", [missing])
        }
```

The `input.review.object` is the full Kubernetes object being admitted. `input.parameters` comes from the `Constraint` resource. The `violation` rule produces a set of messages — if it's non-empty, admission is denied.

```yaml
# snippet-2
apiVersion: constraints.gatekeeper.sh/v1beta1
kind: K8sRequiredLabels
metadata:
  name: require-team-label
spec:
  match:
    kinds:
      - apiGroups: ["apps"]
        kinds: ["Deployment", "StatefulSet"]
    namespaces:
      - production
      - staging
  parameters:
    labels:
      - team
      - cost-center
      - environment
```

This constraint only applies to Deployments and StatefulSets in `production` and `staging` namespaces. You can stack multiple constraints from the same template with different scopes and parameters. Platform teams own the templates; individual teams configure constraints for their namespaces.

## Writing Real Rego Policies

The toy examples in most tutorials check if a label exists. Production policies are messier. Here's a realistic policy that enforces container security context requirements — no privileged containers, no root user, read-only root filesystem, and dropped capabilities.

```rego
# snippet-3
package k8scontainersecurity

import future.keywords.in

violation[{"msg": msg}] {
  container := input.review.object.spec.containers[_]
  container.securityContext.privileged == true
  msg := sprintf("Container '%v' must not run as privileged", [container.name])
}

violation[{"msg": msg}] {
  container := input.review.object.spec.containers[_]
  not container.securityContext.runAsNonRoot
  msg := sprintf("Container '%v' must set runAsNonRoot: true", [container.name])
}

violation[{"msg": msg}] {
  container := input.review.object.spec.containers[_]
  not container.securityContext.readOnlyRootFilesystem
  msg := sprintf("Container '%v' must set readOnlyRootFilesystem: true", [container.name])
}

violation[{"msg": msg}] {
  container := input.review.object.spec.containers[_]
  required_drops := {"ALL"}
  provided_drops := {cap | cap := container.securityContext.capabilities.drop[_]}
  missing := required_drops - provided_drops
  count(missing) > 0
  msg := sprintf("Container '%v' must drop ALL capabilities", [container.name])
}

violation[{"msg": msg}] {
  container := input.review.object.spec.initContainers[_]
  container.securityContext.privileged == true
  msg := sprintf("Init container '%v' must not run as privileged", [container.name])
}
```

Each `violation` rule is independent and evaluated separately. You get one violation message per failing condition, per container. If three containers fail for different reasons, the developer gets three specific error messages in a single rejection.

Notice the `initContainers` check at the bottom — a common gap in naive policies. Init containers run with the same privileges as regular containers unless explicitly restricted. Most off-the-shelf policies miss this.

## Resource Limits Enforcement

Unbounded resource requests cause two production failure modes: noisy neighbors starving critical workloads, and cluster autoscaler thrashing because it can't accurately estimate bin-packing. Enforce limits at admission time.

```rego
# snippet-4
package k8sresourcelimits

import future.keywords.in

# Deny if container has no resource limits set
violation[{"msg": msg}] {
  container := input.review.object.spec.containers[_]
  not container.resources.limits
  msg := sprintf("Container '%v' must define resource limits", [container.name])
}

# Deny if memory limit exceeds max allowed by namespace parameters
violation[{"msg": msg}] {
  container := input.review.object.spec.containers[_]
  limit_str := container.resources.limits.memory
  limit_bytes := parse_memory(limit_str)
  max_bytes := parse_memory(input.parameters.max_memory)
  limit_bytes > max_bytes
  msg := sprintf(
    "Container '%v' memory limit %v exceeds maximum allowed %v",
    [container.name, limit_str, input.parameters.max_memory]
  )
}

# Deny if CPU limit exceeds max allowed
violation[{"msg": msg}] {
  container := input.review.object.spec.containers[_]
  limit_str := container.resources.limits.cpu
  limit_millicores := parse_cpu(limit_str)
  max_millicores := parse_cpu(input.parameters.max_cpu)
  limit_millicores > max_millicores
  msg := sprintf(
    "Container '%v' CPU limit %v exceeds maximum allowed %v",
    [container.name, limit_str, input.parameters.max_cpu]
  )
}

parse_memory(s) = bytes {
  endswith(s, "Gi")
  val := to_number(trim_suffix(s, "Gi"))
  bytes := val * 1073741824
} else = bytes {
  endswith(s, "Mi")
  val := to_number(trim_suffix(s, "Mi"))
  bytes := val * 1048576
} else = bytes {
  endswith(s, "Ki")
  val := to_number(trim_suffix(s, "Ki"))
  bytes := val * 1024
}

parse_cpu(s) = millicores {
  endswith(s, "m")
  millicores := to_number(trim_suffix(s, "m"))
} else = millicores {
  millicores := to_number(s) * 1000
}
```

The `parse_memory` and `parse_cpu` helpers handle the Kubernetes quantity format. Rego doesn't have a built-in quantity parser, so you write one. This is the kind of boilerplate that belongs in a shared library bundle, not duplicated across policies.

## Testing Rego Policies

Untested policies are a liability. You'll block legitimate workloads, miss actual violations, or both. OPA's built-in test runner catches this before deployment.

```rego
# snippet-5
package k8scontainersecurity_test

import future.keywords.in

# Test: privileged container is rejected
test_privileged_container_denied {
  violations := violation with input as {
    "review": {
      "object": {
        "metadata": {"name": "test-pod"},
        "spec": {
          "containers": [{
            "name": "app",
            "image": "nginx:latest",
            "securityContext": {
              "privileged": true,
              "runAsNonRoot": true,
              "readOnlyRootFilesystem": true,
              "capabilities": {"drop": ["ALL"]}
            }
          }],
          "initContainers": []
        }
      }
    }
  }
  count(violations) == 1
  violations[_].msg == "Container 'app' must not run as privileged"
}

# Test: compliant container passes
test_compliant_container_allowed {
  violations := violation with input as {
    "review": {
      "object": {
        "metadata": {"name": "test-pod"},
        "spec": {
          "containers": [{
            "name": "app",
            "image": "nginx:1.25",
            "securityContext": {
              "privileged": false,
              "runAsNonRoot": true,
              "readOnlyRootFilesystem": true,
              "capabilities": {"drop": ["ALL"]}
            }
          }],
          "initContainers": []
        }
      }
    }
  }
  count(violations) == 0
}

# Test: missing readOnlyRootFilesystem produces correct violation
test_missing_readonly_fs_violation {
  violations := violation with input as {
    "review": {
      "object": {
        "spec": {
          "containers": [{
            "name": "worker",
            "securityContext": {
              "privileged": false,
              "runAsNonRoot": true
            }
          }],
          "initContainers": []
        }
      }
    }
  }
  some v in violations
  v.msg == "Container 'worker' must set readOnlyRootFilesystem: true"
}
```

Run with `opa test ./policies/ -v`. The output shows pass/fail per test case with timing. Wire this into your CI pipeline — policy tests should run on every PR that touches the `policies/` directory, with the same weight as application tests.

## Gatekeeper Audit Mode

Blocking new resources is necessary but not sufficient. You inherit existing resources that predate your policies. Gatekeeper's audit controller periodically re-evaluates all cluster resources against active constraints and writes violations back to the constraint's `status` field.

```bash
# snippet-6
# Check audit results for a specific constraint
kubectl get k8srequiredlabels require-team-label -o json | \
  jq '.status.violations[] | {namespace: .namespace, name: .name, message: .message}'

# Count total violations across all constraints
kubectl get constraints -A -o json | \
  jq '[.items[] | .status.violations // [] | length] | add'

# Get all constraints with active violations, sorted by count
kubectl get constraints -A -o json | jq -r '
  .items[]
  | select(.status.violations != null and (.status.violations | length) > 0)
  | "\(.status.violations | length)\t\(.metadata.name)"
' | sort -rn

# Watch for new violations in real time
kubectl get events -n gatekeeper-system --field-selector reason=FailedAdmission -w
```

Audit runs on a configurable interval (default 60 seconds). The `status.violations` list is capped at 20 entries per constraint by default — raise this with `--audit-chunk-size` and `--constraint-violations-limit` flags if you have dense legacy clusters. Feed audit results into your observability stack: a Prometheus exporter for Gatekeeper metrics is available at `gatekeeper-system/gatekeeper-controller-manager:8888/metrics`, exposing `gatekeeper_violations` as a gauge per constraint.

## The Exemption Problem

Every policy needs an escape valve. Blocking system namespaces (`kube-system`, `gatekeeper-system`) is mandatory — Gatekeeper itself runs privileged. You handle this with the `match` spec on constraints:

```yaml
# snippet-7
apiVersion: constraints.gatekeeper.sh/v1beta1
kind: K8sContainerSecurity
metadata:
  name: enforce-container-security
spec:
  match:
    kinds:
      - apiGroups: ["apps", ""]
        kinds: ["Deployment", "DaemonSet", "StatefulSet", "Pod"]
    excludedNamespaces:
      - kube-system
      - kube-public
      - gatekeeper-system
      - monitoring        # Prometheus node-exporter needs host access
      - logging           # Fluentd needs host log mounts
    labelSelector:
      matchExpressions:
        - key: policy.gatekeeper.sh/exempt
          operator: DoesNotExist
  enforcementAction: deny
  parameters: {}
```

The `enforcementAction: deny` blocks admission. Use `warn` during rollout to surface violations without blocking — developers see warnings in `kubectl apply` output without being stopped. Graduate to `deny` after burn-down.

The label-based exemption (`policy.gatekeeper.sh/exempt`) is a trap. If developers can self-service exempt their workloads, the policy is theater. Restrict who can apply that label with RBAC — only platform engineers should be able to label namespaces or resources as exempt. Audit exemption labels quarterly and remove stale ones.

## Operational Failure Modes

A few failure modes that will bite you in production:

**Webhook timeout cascades.** Gatekeeper defaults to a 3-second webhook timeout. If your OPA pods are under memory pressure or the policy evaluation is expensive, timeouts cause admission failures. The `failurePolicy: Fail` setting (correct for security) means a timed-out webhook denies admission. Set `failurePolicy: Ignore` only on non-security-critical webhooks. Size Gatekeeper pods appropriately — 256Mi per replica is a floor for clusters above 500 pods.

**Bundle synchronization lag.** Gatekeeper caches cluster state (pods, namespaces, services) in its OPA instance via the `Config` CRD. Policies that reference existing cluster state (`data.inventory`) can make decisions based on stale cache. The cache sync interval is configurable but not zero. Design policies to be stateless where possible; use inventory references only when truly necessary.

**CRD validation gaps.** `ConstraintTemplate` Rego is not validated at apply time — a syntax error in your Rego is silently accepted. The constraint is created, appears healthy, but evaluates nothing. Always run `opa check ./policies/` in CI before applying templates to the cluster. The `gatekeeper_constraint_template_count` metric will show the template, but violations will never fire.

**Policy ordering assumptions.** Admission webhooks run in parallel, not sequentially. You cannot assume one policy fires before another. Each policy must be self-contained and not depend on side effects from other policies.

## Maturity Path

Start with `warn` enforcement on three policies: required labels, no latest image tags, and resource limits. Measure violations in audit mode for two weeks. This gives you a baseline and surfaces the exemptions you actually need before you block anyone. Graduate to `deny` namespace by namespace, starting with new namespaces that have no legacy debt. By the time you reach old namespaces, you have operational confidence in the policies and a remediation process for violations.

The end state is a policy library under version control, tested in CI, deployed via GitOps (Flux or ArgoCD apply your constraint manifests), with audit metrics feeding into SLO dashboards. Policy violations in production become a metric with an owner and a burn-down process — not a 3 AM discovery during a security audit.

Gatekeeper is not the only option. Kyverno uses a YAML-native policy language that some teams find more approachable. But OPA Rego's expressiveness handles edge cases that Kyverno's declarative model struggles with — particularly cross-resource validation and complex set operations. For organizations that already use OPA for application authorization, the operational and tooling investment is shared. Choose based on your team's existing Rego exposure and your policy complexity requirements, not on which tutorial was easier to follow.
```