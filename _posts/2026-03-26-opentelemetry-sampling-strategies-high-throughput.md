---

layout: post
title: "OpenTelemetry Sampling Strategies for High-Throughput Services"
date: 2026-03-26 08:00:00 +0700
tags: [opentelemetry, observability, distributed-tracing, backend, performance]
description: "A production-focused breakdown of OTel sampling strategies — head, tail, and adaptive — for services processing millions of requests per day."
image: "https://images.unsplash.com/photo-1717501217912-933d2792d493?crop=entropy&cs=tinysrgb&fit=max&fm=jpg&ixid=M3wzMTE1NTV8MHwxfHJhbmRvbXx8fHx8fHx8fDE3NzQ1MDE2Mjd8&ixlib=rb-4.1.0&q=80&w=1080"
thumbnail: "https://images.unsplash.com/photo-1717501217912-933d2792d493?crop=entropy&cs=tinysrgb&fit=max&fm=jpg&ixid=M3wzMTE1NTV8MHwxfHJhbmRvbXx8fHx8fHx8fDE3NzQ1MDE2Mjd8&ixlib=rb-4.1.0&q=80&w=400"
---

At 50,000 requests per second, exporting every trace to your backend will either bankrupt you or melt your Jaeger cluster. Most teams discover this the hard way: they instrument everything correctly, deploy to production, watch their observability bill spike by 10x, then gut their tracing entirely and go back to logs. That's the wrong lesson. The problem isn't tracing — it's sampling, and specifically the absence of a deliberate sampling strategy. OpenTelemetry gives you the machinery to sample intelligently, but the defaults will hurt you, and the documentation won't save you. This post covers what actually works at scale.

## Why the Default 100% Rate Is a Trap

The OTel SDK default is `ParentBased(root=AlwaysOn)`, which samples everything. At low traffic this is fine. At 10k RPS with spans averaging 5KB each, you're looking at ~50MB/s of trace data flowing to your collector — before fan-out to multiple exporters. Multiply by microservices and the math becomes absurd quickly.

The naive fix is head-based sampling: drop a fixed percentage at the point where a trace originates. This works until you realize you've dropped the one 30-second database query that was causing your P99 latency spike. Random sampling is indifferent to trace value, and high-cardinality failure signals are exactly the data you cannot afford to lose.

## Head-Based Sampling: Fast but Blind

Head-based sampling makes the keep/drop decision at trace creation time, before you know anything about how the trace will turn out. It's computationally cheap and requires no coordination across services.

```yaml
# snippet-1
# otel-collector-config.yaml — head-based probabilistic sampling
receivers:
  otlp:
    protocols:
      grpc:
        endpoint: 0.0.0.0:4317

processors:
  probabilistic_sampler:
    sampling_percentage: 10  # keep 10% of all traces
  batch:
    timeout: 5s
    send_batch_size: 1024

exporters:
  otlp/jaeger:
    endpoint: jaeger-collector:4317
    tls:
      insecure: true

service:
  pipelines:
    traces:
      receivers: [otlp]
      processors: [probabilistic_sampler, batch]
      exporters: [otlp/jaeger]
```

This reduces volume by 90%, but the failures you care about — errors, slow traces, anomalies — aren't 10% of your traffic. They might be 0.1%. Head-based sampling treats them the same as successful fast requests, so they vanish from your observability data proportionally. You will miss incidents.

The one scenario where head-based sampling is the right default: uniform traffic where every request has equal diagnostic value. A data pipeline processing identical ETL jobs. A batch processor with homogeneous workloads. Everywhere else, it's a blunt instrument.

## Tail-Based Sampling: Expensive but Correct

Tail-based sampling buffers spans for a complete trace and makes the keep/drop decision after the trace finishes. You can now say: keep all traces with errors, keep all traces slower than 500ms, drop everything else at 1%. This is the correct approach for most production systems.

The tradeoff is infrastructure: spans from a distributed trace may arrive at different collector instances. You need consistent hashing or routing to ensure all spans for a trace land on the same collector node.

```yaml
# snippet-2
# otel-collector-config.yaml — tail-based sampling with policy composition
receivers:
  otlp:
    protocols:
      grpc:
        endpoint: 0.0.0.0:4317

processors:
  tail_sampling:
    decision_wait: 10s          # how long to wait for all spans
    num_traces: 50000           # in-memory trace buffer size
    expected_new_traces_per_sec: 1000
    policies:
      - name: keep-errors
        type: status_code
        status_code:
          status_codes: [ERROR]
      - name: keep-slow-traces
        type: latency
        latency:
          threshold_ms: 500
      - name: keep-sampling-flag
        type: trace_state
        trace_state:
          key: sampling
          values: ["1"]
      - name: probabilistic-baseline
        type: probabilistic
        probabilistic:
          sampling_percentage: 2   # 2% of normal traces
      - name: composite-policy
        type: and
        and:
          and_sub_policy:
            - name: high-value-service
              type: string_attribute
              string_attribute:
                key: service.name
                values: ["payment-service", "checkout-service"]
            - name: not-health-check
              type: string_attribute
              string_attribute:
                key: http.route
                values: ["/health", "/ready", "/metrics"]
                invert_match: true

exporters:
  otlp/tempo:
    endpoint: tempo:4317
    tls:
      insecure: true

service:
  pipelines:
    traces:
      receivers: [otlp]
      processors: [tail_sampling]
      exporters: [otlp/tempo]
```

`decision_wait: 10s` is the hidden landmine here. You're holding all spans for a trace in memory for up to 10 seconds. At 1000 traces/sec with 50 spans each, that's potentially 500,000 spans in memory at steady state. Size your collector nodes accordingly — 8GB+ is not unusual for high-throughput deployments. If a collector restarts mid-trace, those spans are gone.

## Sampling at the SDK Level: Controlling What Leaves the Service

Collector-side sampling still means every span crosses the network. For truly high-throughput services, you want to drop spans before they leave the process. The OTel SDK's sampler interface gives you this.

<script src="https://gist.github.com/mohashari/7e8a0bdebec7e2b1f4e636a3c2a117ec.js?file=snippet-3.go"></script>

One limitation: at SDK sampling time, you haven't executed the span body yet. You don't know if it will be slow or error. This is exactly the same problem as head-based sampling at the collector. The workaround for latency-based SDK sampling is to use parent-based sampling in combination with a collector-side tail sampler — the SDK samples at a low base rate and keeps all propagated sampling decisions, the collector handles the latency and error policies downstream.

## Adaptive Sampling: Rate-Limiting What Matters

A subtler failure mode: high error rates. During an incident, every request fails. If your sampling policy keeps all error spans, you've just turned off sampling entirely at the worst possible moment, flooding your backend when it's already under stress. You need a rate limit, not just a predicate.

<script src="https://gist.github.com/mohashari/7e8a0bdebec7e2b1f4e636a3c2a117ec.js?file=snippet-4.go"></script>

At 500 errors/sec with `maxErrorsPerSec: 100`, you're keeping 20% of error spans — enough for diagnostic signal without overwhelming your backend. Combine with a `sampling.reason` attribute and you can distinguish "sampled because error" from "sampled probabilistically" in your trace UI, which matters when you're doing incident analysis.

## The Collector Pipeline for Multi-Tier Deployments

Real deployments have multiple collector tiers. SDK → agent collector (per host or sidecar) → gateway collector → backend. Sampling policy placement matters.

```yaml
# snippet-5
# agent-collector-config.yaml — lightweight agent, no tail sampling
# Runs as DaemonSet or sidecar; minimal memory footprint
processors:
  batch:
    timeout: 1s
    send_batch_size: 512
  filter/drop-health:
    traces:
      span:
        - 'attributes["http.route"] == "/health"'
        - 'attributes["http.route"] == "/ready"'
        - 'attributes["http.route"] == "/metrics"'
  memory_limiter:
    limit_mib: 512
    spike_limit_mib: 128
    check_interval: 5s

exporters:
  otlp/gateway:
    endpoint: otel-gateway:4317
    sending_queue:
      enabled: true
      num_consumers: 10
      queue_size: 1000

service:
  pipelines:
    traces:
      receivers: [otlp]
      processors: [memory_limiter, filter/drop-health, batch]
      exporters: [otlp/gateway]
```

The agent tier does cheap filtering only — drop health check spans, apply memory limits, batch aggressively. No tail sampling here; the agent can't see full traces. The gateway tier runs tail_sampling with the policies above. This split keeps agent resource usage predictable and centralizes sampling logic where it belongs.

The `memory_limiter` processor is non-negotiable on the agent. Without it, a burst of traffic will OOM your agent and you'll lose all telemetry data. Set `limit_mib` to 75% of your allocated container memory.

## Measuring Sampling Effectiveness

You can't tune what you don't measure. The OTel Collector exposes Prometheus metrics at `localhost:8888/metrics` by default. The ones that matter:

```bash
# snippet-6
# Useful collector metrics for sampling observability
# otelcol_processor_tail_sampling_sampling_decision_latency
# otelcol_processor_tail_sampling_sampling_policy_indicator
# otelcol_processor_tail_sampling_count_traces_sampled
# otelcol_processor_tail_sampling_count_traces_not_sampled

# Check current sampling rate
curl -s localhost:8888/metrics | grep tail_sampling | grep -E "(sampled|not_sampled)"

# Expected output format:
# otelcol_processor_tail_sampling_count_traces_sampled{policy="keep-errors"} 1247
# otelcol_processor_tail_sampling_count_traces_sampled{policy="probabilistic-baseline"} 8934
# otelcol_processor_tail_sampling_count_traces_not_sampled 891023

# Derive effective sampling rate per policy:
# sampled / (sampled + not_sampled) per policy type
```

Watch `sampling_decision_latency` histograms. If your p99 decision latency creeps above `decision_wait`, you have a memory pressure problem — the collector is making premature decisions before all spans arrive. This shows up as incomplete traces in Jaeger: you'll see a root span with no children, then wonder why your instrumentation is broken. It isn't.

## What to Actually Deploy

For a service processing 10k-100k RPS, here's the configuration that minimizes regret:

**SDK level**: `TraceIDRatioBased(0.01)` wrapped with parent-based sampling. 1% base rate, inherit parent decisions. No error logic at SDK level — you don't have enough information yet.

**Agent collector**: Drop health checks and static noise. Batch with 1-second timeout. 512MB memory limit. Nothing else.

**Gateway collector**: Tail sampling with `decision_wait: 10s` and a policy hierarchy: (1) keep all error spans up to 200/sec per service, (2) keep all spans over 500ms up to 100/sec per service, (3) keep 2% of everything else. These numbers are starting points — instrument and adjust based on your `count_traces_sampled` metrics after a week in production.

The temptation is to add more policies over time as you find gaps. Resist it. Each policy increases memory pressure and decision latency. The right response to a gap is to improve your structured logging and events, not to add a sampling policy that keeps 100% of a specific route forever. Sampling policies are not a substitute for good instrumentation; they're a budget management tool.

One final point: propagate sampling decisions across service boundaries correctly. The `traceparent` header carries the sampling flag. If service A samples a request, service B should honor that decision and also sample — this is `ParentBased` sampler behavior. If you override this and re-sample independently in each service, you will get partial traces where some services are present and others aren't, and you'll spend hours debugging your instrumentation when the bug is in your sampler configuration.