---
layout: post
title: "Implementing Tail-Based Sampling in OpenTelemetry Collectors for Cost-Effective Distributed Tracing"
date: 2026-06-24 08:00:00 +0700
tags: [opentelemetry, distributed-tracing, observability, site-reliability-engineering, platform-engineering]
description: "A production-hardened guide to implementing tail-based sampling in OpenTelemetry Collectors to reduce storage costs while preserving critical trace anomalies."
image: "https://picsum.photos/seed/oteltailsmp/1080/720"
thumbnail: "https://picsum.photos/seed/oteltailsmp/400/300"
---

A massive traffic spike or a sudden network partition does not just degrade application latency—it floods your telemetry database with millions of redundant successful HTTP 200 traces, driving storage bills to sky-high premiums while burying crucial diagnostics under a mountain of noise. Exporting 100% of traces at 50,000 requests per second with an average span payload of 5KB creates an overwhelming 250MB/s telemetry pipeline. In commercial observability platforms like Datadog or Grafana Cloud, this ingestion rate easily pushes monthly costs past $40,000, while self-hosted Grafana Tempo or ClickHouse clusters buckle under the write-heavy load. The standard fix—head-based probabilistic sampling—is a blind instrument: it drops latency anomalies and rare error traces at the exact same rate as successful database queries. To retain critical diagnostic visibility while keeping costs under control, production platforms must adopt tail-based sampling. By buffering trace spans in memory and evaluating keep/drop decisions only after a trace completes, you can guarantee the collection of 100% of system errors and P99 latency spikes, while compressing normal traffic to a modest 1% baseline.

![Implementing Tail-Based Sampling in OpenTelemetry Collectors for Cost-Effective Distributed Tracing Diagram](/images/diagrams/tail-based-sampling-opentelemetry-collector-distributed-tracing.svg)

## The Mechanics of Tail-Based Sampling: The Stateful Routing Challenge

Unlike head-based sampling, which determines a trace's fate at its inception point (for example, by having a gateway service set a trace header), tail-based sampling evaluates the entire trace structure before making a decision. This allows policies to analyze attributes like span status (e.g. `StatusCode == ERROR`), total trace duration, or specific trace attributes collected deep in the microservice call tree.

However, tail-based sampling is stateful. Spans from a single distributed trace are emitted asynchronously by multiple services and can arrive at different OpenTelemetry Collector instances in a load-balanced cluster. If Collector Pod A holds the checkout service span and Collector Pod B holds the database query span, neither pod can reconstruct the full trace history to evaluate a latency or error policy.

To solve this stateful assembly problem, you must configure a two-tier collector architecture:

1. **Local Agents (DaemonSet):** Running on each Kubernetes node, acting as a lightweight local proxy.
2. **Central Gateways (Deployment):** Running the stateful `tail_sampling` processor.

The Ingress Agents use a consistent-hashing load-balancing exporter to route all spans sharing the same `trace_id` to the same central Gateway pod. This ensures that a single Collector Gateway instance receives all spans for a given trace, allowing it to reconstruct the trace in RAM, wait for it to complete, and apply sampling policies.

```yaml
// snippet-1
# otel-agent-config.yaml - Consistent hash trace routing to Gateway pods
receivers:
  otlp:
    protocols:
      grpc:
        endpoint: 0.0.0.0:4317
      http:
        endpoint: 0.0.0.0:4318

processors:
  batch:
    send_batch_size: 8192
    timeout: 1s
    send_batch_max_size: 10240

exporters:
  loadbalancing:
    routing_key: "trace_id"
    resolver:
      dns:
        hostname: otel-gateway-headless.monitoring.svc.cluster.local
        port: 4317
        interval: 5s

service:
  pipelines:
    traces:
      receivers: [otlp]
      processors: [batch]
      exporters: [loadbalancing]
```

In this agent configuration:
- The `loadbalancing` exporter parses the `trace_id` of each span and routes it to one of the resolved backend IPs.
- The `dns` resolver queries the Kubernetes DNS headless service `otel-gateway-headless` every 5 seconds to discover active Gateway pods.
- Consistent hashing guarantees that even during a Gateway scale-out event, the vast majority of existing trace IDs continue mapping to their original target nodes, minimizing orphaned spans.

## Configuring the Gateway for Stateful Sampling Policies

The central Gateway collectors assemble spans in memory and evaluate them against configured rules. The configuration must define a `decision_wait` time—the duration the collector waits after receiving the first span of a trace before making a sampling decision. This wait time must accommodate network latency, execution time across downstream microservices, and asynchronous batching delays.

```yaml
// snippet-2
# otel-gateway-config.yaml - Stateful tail-sampling configurations with policy rules
receivers:
  otlp:
    protocols:
      grpc:
        endpoint: 0.0.0.0:4317

processors:
  tail_sampling:
    decision_wait: 10s
    num_traces: 100000
    expected_new_traces_per_sec: 5000
    policies:
      - name: drop-health-checks
        type: string_attribute
        string_attribute:
          key: http.target
          values: ["/healthz", "/metrics", "/ping"]
          enabled_regex_matching: false
          invert_match: true
      - name: keep-errors
        type: status_code
        status_code:
          status_codes: [ERROR]
      - name: keep-latency-spikes
        type: latency
        latency:
          threshold_ms: 750
      - name: keep-payment-path
        type: string_attribute
        string_attribute:
          key: http.route
          values: ["/checkout/pay", "/api/v1/billing"]
      - name: probabilistic-baseline
        type: probabilistic
        probabilistic:
          sampling_percentage: 1.0

exporters:
  otlp/tempo:
    endpoint: tempo-distributor.monitoring.svc.cluster.local:4317
    tls:
      insecure: true

service:
  pipelines:
    traces:
      receivers: [otlp]
      processors: [tail_sampling]
      exporters: [otlp/tempo]
```

Policies are evaluated sequentially. As soon as a trace matches one of the policies, the "keep" or "drop" decision is executed. Under this production policy set:
- The `drop-health-checks` rule uses `invert_match: true` to match spans that do *not* access health check routes, filtering out common noise.
- The `keep-errors` policy retains 100% of failed traces.
- The `keep-latency-spikes` policy retains all traces where duration exceeds 750ms.
- A baseline probabilistic policy keeps 1% of normal traces, providing structural telemetry to model system baselines.

The `num_traces` parameter defines the size of the collector's internal ring buffer for tracking trace state. If this buffer fills up before `decision_wait` completes, the collector is forced to drop traces prematurely, leading to lost diagnostic data.

## Calculating Buffer Sizing and Memory Requirements

Stateful trace re-assembly occurs in the heap. If your application processes 10,000 requests per second and each request generates a trace consisting of 8 spans, the collector must track 80,000 spans per second. If `decision_wait` is set to 10 seconds, the collector must buffer 800,000 spans at any point in time.

To calculate the memory required for the stateful buffering tier, use this production sizing formula:

$$\text{Memory}_{\text{buffer}} = R_{\text{trace}} \times S_{\text{span}} \times N_{\text{spans}} \times W_{\text{wait}} \times \text{Overhead}$$

Where:
- $R_{\text{trace}}$ = Peak trace arrival rate per second (e.g. `5,000` traces/sec)
- $S_{\text{span}}$ = Average serialized size of a span (typically `2KB` to `6KB` depending on custom span attributes, stack traces, and request payloads)
- $N_{\text{spans}}$ = Average number of spans per trace (e.g. `6` spans)
- $W_{\text{wait}}$ = `decision_wait` duration (e.g. `10s`)
- $\text{Overhead}$ = Go runtime memory allocation overhead multiplier (usually `2.5x` due to hash maps, string copies, and GC heap headroom)

For our example:

$$\text{Memory}_{\text{buffer}} = 5,000 \times 4\text{KB} \times 6 \times 10 \times 2.5 \approx 3\text{GB}$$

This calculation shows that memory consumption scales linearly with the `decision_wait` time and traffic spikes. If Go runs out of memory, it crashes the collector pod, disrupting the pipeline and causing tracing client buffers on your microservices to fill up. To prevent this, you must pair your configuration with the `memory_limiter` processor and the environment variable `GOMEMLIMIT`.

```yaml
// snippet-3
# memory-limiter-gateway.yaml - Memory protection configurations for stateful Gateway collectors
processors:
  memory_limiter:
    check_interval: 1s
    limit_percentage: 85
    spike_limit_percentage: 10

# Corresponding Go runtime env vars for Kubernetes pod spec:
# env:
#   - name: GOMEMLIMIT
#     value: "3600MiB" # 90% of a 4GiB container memory limit
#   - name: GOGC
#     value: "75"
```

To configure these correctly, you must align the memory limits of the container, the Go runtime (`GOMEMLIMIT`), and the `memory_limiter` processor settings. Use the following formula for production deployments:
- **Container Memory Limit:** Set to the maximum memory allowed for the pod (e.g., `4GiB`).
- **Go Memory Limit (`GOMEMLIMIT`):** Set to 90% of the container limit (e.g., `3.6GiB` or `3600MiB`). This leaves a 10% headroom for runtime overhead, stack memory, and CGo memory.
- **Processor `limit_percentage`:** Set to 85%. This ensures the collector starts shedding load (dropping incoming spans) before Go reaches `GOMEMLIMIT` and aggressively thrashes the CPU in continuous GC cycles.
- **Processor `spike_limit_percentage`:** Set to 10% to prevent sudden traffic spikes from bypassing the limiter limit before a check interval occurs.

## Managing Backpressure and Exporter Queues under Load

When downstream telemetry systems (e.g. a Grafana Tempo distributor or an in-house ClickHouse cluster) experience write lag, the collector gateway must buffer the kept traces in memory. If the exporter's sending queue overflows, the collector will propagate backpressure upstream. If the queues are not bounded or configured incorrectly, memory usage spikes, triggering the `memory_limiter` and leading to dropped traces.

```yaml
// snippet-4
# exporter-sending-queue.yaml - Bounded sending queues and exponential retry backoff
exporters:
  otlp/tempo:
    endpoint: tempo-distributor.monitoring.svc.cluster.local:4317
    tls:
      insecure: true
    sending_queue:
      enabled: true
      num_consumers: 16
      queue_size: 15000 # Max trace batches to hold in memory
    retry_on_failure:
      enabled: true
      initial_interval: 5s
      max_interval: 30s
      max_elapsed_time: 5m
```

In this configuration:
- `queue_size` defines the number of batches buffered in memory when downstream connections fail. If set to `15000`, and each batch contains `512` spans, we buffer up to `7.6 million` spans.
- `num_consumers` dictates how many concurrent gRPC streams send data to Tempo. If your backend suffers from high latency, you must increase this value (e.g., from `4` to `16` or `32`) to maintain throughput.
- `max_elapsed_time` sets a maximum threshold of `5m` for retrying. Spans older than 5 minutes are discarded to prevent heap exhaustion.

## Composite Sampling Policies for Complex Workloads

In a complex microservices mesh, you often need different sampling behavior based on service characteristics. For instance, payment transactions must be sampled at a higher baseline than catalog browsing requests. The `composite` policy allows you to combine multiple sub-policies with logical operators (AND/OR).

```yaml
// snippet-5
# composite-tail-sampling.yaml - Complex composite policy composition
processors:
  tail_sampling:
    decision_wait: 10s
    num_traces: 100000
    expected_new_traces_per_sec: 5000
    policies:
      - name: keep-all-errors
        type: status_code
        status_code:
          status_codes: [ERROR]
      - name: checkout-transaction-policy
        type: and
        and:
          and_sub_policy:
            - name: checkout-service-filter
              type: string_attribute
              string_attribute:
                key: service.name
                values: ["checkout-service"]
            - name: probabilistic-checkout
              type: probabilistic
              probabilistic:
                sampling_percentage: 10.0 # Keep 10% of successful checkouts
      - name: general-probabilistic
        type: probabilistic
        probabilistic:
          sampling_percentage: 1.0 # 1% for everything else
```

Under this configuration:
- A trace is evaluated against `keep-all-errors` first. If it contains an error, it is immediately kept.
- If not, and the trace originated in `checkout-service`, the composite policy evaluates the probabilistic rule, keeping `10%` of checkout traffic.
- Traces from other services fall through to the `general-probabilistic` policy, keeping `1%` of normal transactions.

## Application SDK Configurations to Prevent Thread Blocking

A common production failure occurs when application client buffers fill up because the local collector agent is down or rejecting spans. If the application OTel SDK is configured with a blocking span processor, HTTP worker threads will block waiting to export telemetry, causing application response times to degrade dramatically.

```go
// snippet-6
// telemetry.go - Non-blocking OTel SDK Go setup with bounded queues
package main

import (
	"context"
	"time"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracegrpc"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	semconv "go.opentelemetry.io/otel/semconv/v1.24.0"
)

func InitTracer(ctx context.Context, endpoint string) (*sdktrace.TracerProvider, error) {
	exporter, err := otlptracegrpc.New(ctx,
		otlptracegrpc.WithInsecure(),
		otlptracegrpc.WithEndpoint(endpoint),
	)
	if err != nil {
		return nil, err
	}

	res, err := resource.New(ctx,
		resource.WithAttributes(
			semconv.ServiceNameKey.String("payment-service"),
		),
	)
	if err != nil {
		return nil, err
	}

	bsp := sdktrace.NewBatchSpanProcessor(exporter,
		sdktrace.WithMaxQueueSize(2048),          // Bound maximum queue size in RAM
		sdktrace.WithMaxExportBatchSize(512),     // Size of OTLP payloads sent to agent
		sdktrace.WithBatchTimeout(500*time.Millisecond),
		sdktrace.WithExportTimeout(5*time.Second),
	)

	tp := sdktrace.NewTracerProvider(
		sdktrace.WithResource(res),
		sdktrace.WithSpanProcessor(bsp),
	)

	otel.SetTracerProvider(tp)
	return tp, nil
}
```

In this Go SDK configuration:
- `WithMaxQueueSize(2048)` ensures that the application's SDK will hold a maximum of 2,048 spans in memory. When this buffer fills (e.g. the node agent fails), new spans are dropped rather than exhausting application memory or blocking worker threads.
- `WithBatchTimeout` forces trace flush every 500ms, preventing spans from accumulating in client memory and keeping trace latency low.

## Monitoring Tail-Sampling Health (Prometheus Metrics)

Operationalizing a tail-sampling pipeline requires monitoring collector-level health metrics. If the consistent-hashing exporter on the agents starts failing, or if the gateway buffer fills up, you need alerts to flag these anomalies before traces are lost.

```bash
// snippet-7
# query_otel_metrics.sh - Checking OpenTelemetry Collector Prometheus metrics for sampling drops
COLLECTOR_METRICS_URL="http://otel-gateway-monitoring.monitoring.svc.cluster.local:8888/metrics"

# 1. Check active traces currently tracked in the tail-sampling decision wait buffer
curl -s $COLLECTOR_METRICS_URL | grep "otelcol_processor_tail_sampling_count_traces_sampled"

# 2. Check number of traces dropped by sampling decisions
curl -s $COLLECTOR_METRICS_URL | grep "otelcol_processor_tail_sampling_dropped_traces"

# 3. Check memory limiter processor dropped spans (signals processor is shedding load)
curl -s $COLLECTOR_METRICS_URL | grep "otelcol_processor_dropped_spans"
```

| Metric Name | Type | Description | Warning Threshold / Alert Condition |
| :--- | :--- | :--- | :--- |
| `otelcol_processor_tail_sampling_count_traces_sampled` | Counter | Total traces evaluated by the processor. | Alert if drops to 0 (pipeline stalled). |
| `otelcol_processor_tail_sampling_new_trace_decisions` | Counter | Number of new trace IDs registered in the buffer. | Compare with incoming trace rate to detect orphan spans. |
| `otelcol_processor_dropped_spans` | Counter | Spans dropped due to memory limiter actions. | Critical alert if value > 0 (node shedding load due to memory pressure). |
| `otelcol_receiver_refused_spans` | Counter | Spans rejected at the receiver tier due to backpressure. | Warning alert (signals upstream blocking or network congestion). |

## SRE Operational Troubleshooting Guide

### Symptom: Incomplete Traces (Orphaned Spans)
- **Root Cause:** Spans of the same trace ID are routing to different gateway instances. This occurs when the `loadbalancing` resolver DNS lookup cache is stale or when application pods are communicating directly with different gateways bypassing node agents.
- **Resolution:** Verify application containers connect *only* to their local daemonset agent on `localhost:4317`. Ensure the agent configuration's `loadbalancing` resolver references the gateway headless service and has an update interval of `5s` or less.

### Symptom: High CPU thrashing on Gateway pods
- **Root Cause:** Memory usage is near `GOMEMLIMIT` but slightly below `memory_limiter` limit, forcing Go's GC to run continuously (GC thrashing).
- **Resolution:** Reduce the `memory_limiter.limit_percentage` to `80%` or increase the pod memory limit in Kubernetes.

### Symptom: Missing trace context across Kafka or async queues
- **Root Cause:** Trace context propagation headers (`traceparent`) are not being injected/extracted across async boundary limits.
- **Resolution:** Enforce context propagation in headers.

## Conclusion and Migration Path

Implementing tail-based sampling in OpenTelemetry Collectors is one of the most effective strategies for scaling a distributed tracing infrastructure cost-effectively. By adopting a two-tier collector topology with consistent trace routing, configuring memory protection rules, and sizing your memory buffers accurately, you can reduce backend tracing costs by up to 90% while retaining critical diagnostics for error tracking and latency analysis.
