---
layout: post
title: "Falco Runtime Security: Custom Rules, Kubernetes Audit Logs, and SIEM Integration"
date: 2026-03-30 08:00:00 +0700
tags: [devsecops, kubernetes, security, falco, siem]
description: "How to deploy Falco with custom detection rules, wire Kubernetes audit logs, and ship alerts into your SIEM without drowning in noise."
image: "https://picsum.photos/1080/720?random=5978"
thumbnail: "https://picsum.photos/400/300?random=5978"
---

At 2:47 AM your on-call phone fires. A cryptominer has been running inside your Kubernetes cluster for six hours — not because your perimeter failed, but because a dev accidentally pushed an image with a compromised dependency that spawned a child process the moment the pod reached Running state. Your WAF saw nothing. Your network policies were fine. Your image scanner passed the image because the malicious binary was downloaded at runtime. The only layer that could have caught this is runtime behavioral detection, and if you don't have it, you're flying blind.

Falco is the de facto open-source answer to this. It hooks into the Linux kernel via eBPF or a kernel module, consumes a stream of system calls, and evaluates them against a rule engine in real time. But the default rules are a starting point, not a finish line. Production Falco means custom rules tuned to your workload, Kubernetes API audit logs feeding behavioral context, and structured output flowing into your SIEM where on-call engineers can actually act on it. This post covers all three.

## How Falco Actually Works

Falco runs as a DaemonSet (one pod per node) and reads a syscall event stream from a kernel driver — either a classic kernel module or a modern eBPF probe. Each event carries process name, executable path, container metadata, network endpoints, file paths, and the syscall arguments. The rule engine enriches events with Kubernetes metadata (pod name, namespace, labels) pulled from the local kubelet API and a shared enrichment cache.

A rule is a YAML document with a condition (a boolean expression over event fields) and a priority plus output template. Rules use macros and lists to avoid repetition. The condition language is essentially a typed predicate language — not Rego, not CEL, just Falco's own grammar with field accessors.

```yaml
# snippet-1
# Custom rule: detect execution of unexpected binaries inside application containers
# Place in /etc/falco/rules.d/app-exec.yaml

- list: allowed_app_binaries
  items:
    - /usr/bin/java
    - /usr/local/bin/python3
    - /bin/sh           # needed for health checks
    - /usr/bin/curl     # liveness probes

- macro: is_app_container
  condition: >
    k8s.ns.name in (production, staging) and
    container.name != "istio-proxy" and
    container.name != "filebeat" and
    not container.name startswith "init-"

- rule: Unexpected Binary Execution in App Container
  desc: >
    A binary ran inside an application container that is not in the
    known-good list. This can indicate supply chain compromise or
    a post-exploitation pivot.
  condition: >
    spawned_process and
    is_app_container and
    not proc.exepath in (allowed_app_binaries) and
    not proc.exepath startswith /proc/
  output: >
    Unexpected binary executed in app container
    (user=%user.name exe=%proc.exepath args=%proc.args
    container=%container.name pod=%k8s.pod.name
    ns=%k8s.ns.name image=%container.image.repository:%container.image.tag
    parent=%proc.pname ppid=%proc.ppid)
  priority: WARNING
  tags: [runtime, exec, supply-chain]
```

The `spawned_process` macro is built-in and matches `execve` syscalls where the new process is a child. The `in` operator does an O(1) hash lookup against the list. Keep lists short — anything beyond ~200 entries causes measurable latency in the hot path.

## Writing Rules That Don't Cry Wolf

The biggest operational failure with Falco in production is alert fatigue. Teams install it, get 500 alerts per hour, mute the channel, and the tool becomes shelfware. The root cause is almost always rules that fire on legitimate workload behavior.

The fix is baseline-first rule development. Run Falco in dry-run mode (`--dry-run`) or at `DEBUG` priority for two weeks across staging. Collect what would have fired, group by rule name and container image, and use that to build your exception lists before enabling `WARNING`+ alerting.

```bash
# snippet-2
# Collect dry-run baseline: run Falco and extract rule/container pairs
# that fire frequently — these need exceptions before you alert on them

falco \
  --rules-file /etc/falco/falco_rules.yaml \
  --rules-file /etc/falco/rules.d/ \
  --option "log_level=debug" \
  --option "json_output=true" \
  2>/dev/null | \
jq -r '[.rule, .output_fields."container.name", .output_fields."k8s.ns.name"] | @csv' | \
sort | uniq -c | sort -rn | head -50

# Output example:
# 847 "Read sensitive file trusted after startup","prometheus","monitoring"
# 612 "Unexpected binary executed","node-exporter","monitoring"
# 201 "Write below binary dir","cert-manager","cert-manager"
```

From this baseline you learn which rules are structurally wrong for your environment vs. which legitimately need tuning. `node-exporter` runs many binaries — exempt the entire image. `cert-manager` writes to `/usr/local/bin` at startup — add a startup window exemption using `proc.is_new_exe_file` and a time check if your Falco version supports it, or simply macro it out for that namespace.

The key mental model: **rules describe deviation from your normal, not from some abstract normal**. A rule that fires on 40% of your pods is a broken rule.

## Kubernetes Audit Log Integration

Syscall-level detection catches what happens inside a container. Kubernetes audit logs catch what the control plane did — who created a secret, who exec'd into a pod, which service account bound to a ClusterRole at 3 AM. These are orthogonal visibility planes and you need both.

Falco ingests K8s audit logs via a dedicated plugin (`k8saudit`). The plugin reads audit webhook events delivered by the API server. You configure the API server to ship audit logs to a webhook receiver, and the `k8saudit` Falco plugin listens on that webhook.

```yaml
# snippet-3
# kube-apiserver audit policy — only ship the events Falco needs.
# Full audit logging is expensive; this policy filters to security-relevant events.

apiVersion: audit.k8s.io/v1
kind: Policy
rules:
  # Log exec and port-forward — high value, low volume
  - level: RequestResponse
    resources:
      - group: ""
        resources: ["pods/exec", "pods/portforward", "pods/attach"]

  # Log secret reads — catch credential harvesting
  - level: Metadata
    resources:
      - group: ""
        resources: ["secrets"]
    verbs: ["get", "list", "watch"]

  # Log RBAC mutations — catch privilege escalation
  - level: RequestResponse
    resources:
      - group: "rbac.authorization.k8s.io"
        resources:
          - clusterroles
          - clusterrolebindings
          - roles
          - rolebindings
    verbs: ["create", "update", "patch", "delete"]

  # Log node-level operations
  - level: Metadata
    resources:
      - group: ""
        resources: ["nodes"]
    verbs: ["patch", "update"]

  # Drop everything else — health checks, watches, leader elections
  - level: None
```

Wire this to Falco's audit webhook plugin:

```yaml
# snippet-4
# Falco values for Helm chart — enable k8saudit plugin
# falco-values.yaml

falco:
  rules_file:
    - /etc/falco/falco_rules.yaml
    - /etc/falco/k8s_audit_rules.yaml
    - /etc/falco/rules.d/

plugins:
  - name: k8saudit
    library_path: libk8saudit.so
    init_config:
      sslCertificate: /etc/falco/falco.pem
    open_params: "http://0.0.0.0:9765/k8s-audit"
  - name: json
    library_path: libjson.so

load_plugins: [k8saudit, json]

falcoctl:
  artifact:
    install:
      refs:
        - falco-rules:3
        - k8saudit-rules:0.7

# Configure the API server to POST audit events here:
# --audit-webhook-config-file=/etc/kubernetes/audit-webhook.yaml
# --audit-webhook-batch-max-size=10
# --audit-webhook-batch-max-wait=5s
```

Now add an audit-specific rule that catches the thing that actually matters — a human `kubectl exec` into a production pod:

```yaml
# snippet-5
# Audit rule: alert on any kubectl exec into production namespace
# This fires on the audit event, not the syscall stream

- rule: kubectl exec into Production Pod
  desc: >
    Someone used kubectl exec to open a shell in a production pod.
    This is almost never legitimate and always worth reviewing.
  condition: >
    ka.verb = exec and
    ka.target.namespace = production and
    ka.user.name != "system:serviceaccount:monitoring:prometheus" and
    not ka.user.name startswith "system:"
  output: >
    kubectl exec in production
    (user=%ka.user.name pod=%ka.target.name ns=%ka.target.namespace
    container=%ka.target.subresource command=%ka.uri.param[command]
    src_ip=%ka.source.ip user_agent=%ka.user.agent)
  priority: CRITICAL
  source: k8saudit
  tags: [audit, exec, production]
```

The `source: k8saudit` field is mandatory — it routes the rule to the audit plugin's event stream, not the syscall stream.

## Structured Output and SIEM Integration

Raw Falco output is useful for debugging but useless for operational response. You need structured JSON flowing into your SIEM so analysts can query across time, correlate with other signals, and build alerting thresholds.

Falco supports multiple output channels. For SIEM integration the practical options are: ship JSON to stdout and let your log aggregator (Fluentd, Vector, Fluent Bit) pick it up, or use Falcosidekick to fan out to 60+ destinations directly.

Falcosidekick is the right answer for most teams. It runs as a sidecar or separate deployment, receives Falco JSON over HTTP, and forwards to Slack, PagerDuty, Elasticsearch, Splunk, Loki, Datadog, or any webhook. It also adds enrichment fields and supports priority-based routing — `CRITICAL` goes to PagerDuty, `WARNING` goes to Elasticsearch only.

```yaml
# snippet-6
# Falcosidekick config: route CRITICAL to PagerDuty, all to Elasticsearch
# Deploy as a Deployment alongside Falco DaemonSet

config.yaml: |
  listenaddress: "0.0.0.0"
  listenport: 2801
  debug: false

  customfields:
    environment: production
    cluster: eks-us-east-1-prod
    team: platform-security

  pagerduty:
    routingkey: "${PAGERDUTY_ROUTING_KEY}"
    minimumpriority: critical
    checkcert: true

  elasticsearch:
    hostport: "https://es-cluster.internal:9200"
    index: "falco-alerts"
    username: "${ES_USER}"
    password: "${ES_PASSWORD}"
    minimumpriority: warning
    mutualtls: false
    checkcert: true
    # Use ILM to roll over after 30GB or 7 days
    # PUT _ilm/policy/falco-policy { ... }

  slack:
    webhookurl: "${SLACK_WEBHOOK}"
    channel: "#security-alerts"
    minimumpriority: error
    messageformat: |
      :rotating_light: *{{ .Rule }}* | `{{ .Priority }}`
      Pod: `{{ index .OutputFields "k8s.pod.name" }}` in `{{ index .OutputFields "k8s.ns.name" }}`
      {{ .Output }}
```

The `customfields` block is critical — it adds static metadata to every alert so your SIEM queries can filter by cluster and environment without parsing the output string.

## Tuning the Hot Path: Performance Implications

Falco on a busy node processes 50,000–200,000 syscalls per second. The rule engine evaluates each one. A poorly written rule with O(n) list lookups or expensive regex patterns will push Falco's CPU from 0.5% to 8% per node — at 100 nodes that's meaningful.

Measure first:

```bash
# snippet-7
# Check Falco's internal metrics — exposed via /metrics endpoint
# when metrics_enabled: true in falco.yaml

curl -s http://localhost:8765/metrics | \
  grep -E '(falco_events_processed|falco_rules_matches|falco_cpu)' | \
  sort

# Key metrics to watch:
# falco_events_processed_total        — total syscall events seen
# falco_rules_matches_total           — how many events matched a rule
# falco_cpu_usage_ratio               — CPU burn ratio
# falco_container_memory_used_bytes   — memory for enrichment cache

# If falco_rules_matches_total / falco_events_processed_total > 0.01 (1%)
# you have too many noisy rules. Either fix the rules or you'll saturate
# the output pipeline.
```

Rules with `regex` conditions are 10x slower than equality checks. Replace `proc.name regex "^python[23]?$"` with `proc.name in (python, python2, python3)`. Avoid `contains` on long strings in the hot path. Use macros to short-circuit evaluation — put the cheap conditions first.

The enrichment cache (Kubernetes metadata) has a 5-second TTL by default. If your cluster has rapid pod churn (batch jobs, HPA scale events), increase `metadata_timeout` and `metadata_cache_max_size` in `falco.yaml` to avoid cache misses causing metadata gaps in alerts.

## What Actually Matters in Production

After running Falco in production across three clusters for over a year, the rules that generate real signal — not false positives, not noise — fall into four categories:

**Execution anomalies**: binaries spawned from unexpected parents. A Python web process spawning `bash` is almost always a web shell. A JVM spawning `wget` is almost always post-exploitation. These are low-volume, high-fidelity.

**Sensitive file reads**: `/etc/shadow`, `/proc/*/mem`, mounted service account tokens being read by a process that isn't your application binary. The list of processes that legitimately read K8s service account tokens at runtime is short.

**Network connection anomalies**: outbound connections from containers to unexpected CIDRs, especially to cloud provider metadata endpoints (`169.254.169.254`) from non-cloud-agent containers. This is the number one indicator of SSRF-to-credential-theft attacks.

**Privilege escalation indicators**: `setuid` calls, `ptrace` from user-space, `/proc/sysrq-trigger` writes, `CAP_SYS_ADMIN` usage outside of designated privileged pods.

Everything else — file modification alerts, general network alerts, container image age alerts — generates noise that erodes trust in the system. Build your rule set conservatively. Fifteen rules that always signal real problems are worth more than 200 rules that fire constantly on benign behavior.

The operational contract with your on-call team should be: **every Falco alert at WARNING or above represents something a human should look at within 24 hours, and every CRITICAL represents something a human should look at within 15 minutes**. If you can't hold to that contract, your rules need tuning, not your response process.

Start with the four categories above, run for 30 days, measure false positive rate per rule, and build from there. Falco's value compounds over time as your rule set gets tighter and your team learns what the alerts actually mean in your environment.
```