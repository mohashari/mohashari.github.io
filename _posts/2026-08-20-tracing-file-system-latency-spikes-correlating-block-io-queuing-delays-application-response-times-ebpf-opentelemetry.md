---
layout: post
title: "Tracing File System Latency Spikes: Correlating Block I/O Queuing Delays with Application Response Times Using eBPF and OpenTelemetry"
date: 2026-08-20 08:00:00 +0700
tags: [ebpf, opentelemetry, linux-kernel, performance-tuning, storage]
description: "Correlate block I/O queuing delays with application response times using custom eBPF probes and OpenTelemetry trace propagation for tail latency debugging."
image: "https://picsum.photos/seed/3348/1080/720"
thumbnail: "https://picsum.photos/seed/3348/400/300"
---
It is a nightmare scenario: your write-heavy database or API service suddenly suffers a severe $p99.9$ tail-latency spike, jumping from 5 milliseconds to 2.5 seconds. Thread pools exhaust, upstream clients timeout, and your system enters a cascading failure state. Yet, when you check standard monitoring tools like Prometheus, average disk write rate and memory usage look nominal, and CPU utilization is comfortably under 40%. The culprit is almost always hidden within the Linux block layer—specifically, disk I/O requests waiting inside the kernel software queue ($T_{queue}$) rather than being processed by the drive controller ($T_{service}$). Standard application performance monitoring (APM) tools are blind to this queue, and diagnostic utilities like `iostat` only provide system-wide averages that wash out transient queue spikes. This guide shows how to bridge this visibility gap by leveraging eBPF probes inside the Linux kernel and correlating block-level queuing delays directly to application OpenTelemetry trace contexts.

![Tracing File System Latency Spikes: Correlating Block I/O Queuing Delays with Application Response Times Using eBPF and OpenTelemetry Diagram](/images/diagrams/tracing-file-system-latency-spikes-correlating-block-io-queuing-delays-application-response-times-ebpf-opentelemetry.svg)

## The Observability Blindspot: Virtual File System vs Block Devices

When an application thread writes to disk using a synchronous file system API (such as `write(2)` with `O_DIRECT`, or a subsequent `fsync(2)` / `fdatasync(2)` invocation), the application thread blocks. Application instrumentation, like standard OpenTelemetry libraries, intercepts these calls at the VFS (Virtual File System) layer and registers a span of, say, 500ms. 

From user space, this is a black box. You cannot tell if the delay was caused by:
1. **Virtual File System Contention**: Lock contention on inode mutexes (e.g., during database journal appending).
2. **Page Cache Dirtying Limits**: The kernel blocking the thread to write back page cache dirty pages because the dirty threshold was exceeded.
3. **Journal Commits**: The underlying file system (ext4/xfs) stalling on journal writes before acknowledging a metadata commit.
4. **Block I/O Queue Latency**: The block request waiting inside the kernel's queue (`mq-deadline`, `kyber`, or driver hardware queues) behind a burst of background writes.
5. **Physical Device Saturation**: The flash Translation Layer (FTL) of the SSD performing background garbage collection, driving up disk service time.

To resolve tail latency, you must isolate block queue latency ($T_{queue}$) from device service latency ($T_{service}$). If $T_{queue}$ is the dominant factor, tuning schedulers, increasing queue depths, or pacing application writes will solve the issue. If $T_{service}$ is high, you are hitting physical hardware limitations or drive degradation.

## Building the eBPF Block I/O Tracker

To measure queue times accurately, we hook directly into the Linux block layer tracepoints. We use `tracepoint/block/block_rq_insert` to record when a block request is queued and `tracepoint/block/block_rq_complete` to record when the block device completes the operation.

Using tracepoints is significantly more stable than attaching `kprobes` to internal functions like `submit_bio`, because tracepoint schemas are treated as stable APIs across kernel updates. 

The primary challenge is trace correlation: block I/O completion is asynchronous and decoupled from the application thread context. To solve this, we map active OpenTelemetry trace states in user space to the current Thread ID (TID) in the kernel. When a block request is queued by a thread, we copy its trace context into a pending request map. When the request completes, we compute the latency and send the telemetry packet to a user-space agent via an eBPF Ring Buffer.

Below is the production-grade C eBPF kernel program implementing this mechanism.

<script src="https://gist.github.com/mohashari/0db849b44fa6a74aa6bda536dbc42d40.js?file=snippet-1.txt"></script>

## Bridging Kernel Events to User-Space OpenTelemetry Spans

Once the eBPF kernel program calculates queue latencies and emits them to the Ring Buffer, a user-space agent must parse these events. The agent reads the trace payload, initializes a simulated child span using the matched Trace ID and Parent Span ID, and exports the latency event back to the telemetry collector.

This Go snippet shows how to consume the events and use the OpenTelemetry SDK to inject the kernel I/O queuing span back into the trace hierarchy.

<script src="https://gist.github.com/mohashari/0db849b44fa6a74aa6bda536dbc42d40.js?file=snippet-2.go"></script>

## Go Runtime Rescheduling Protection: Thread Pinning

The solution relies on thread-level correlation: the eBPF program relies on `bpf_get_current_pid_tgid()` to resolve the TID and retrieve the trace context from `active_spans`. In runtimes with dynamic thread scheduling—most notably Go’s M:N scheduler model—this creates a race condition.

If a goroutine writes its Trace ID to the BPF map, starts disk operations, and is preempted or scheduled onto a different OS thread (M) mid-execution, the kernel lookup will query the wrong thread ID. This leads to dropped event matches or incorrect context correlation.

To prevent this, you must pin the executing goroutine to its current OS thread during disk-bound sections using `runtime.LockOSThread()`.

<script src="https://gist.github.com/mohashari/0db849b44fa6a74aa6bda536dbc42d40.js?file=snippet-3.go"></script>

## Analyzing Block Layer Latencies and Queue Optimization

If the eBPF spans indicate that `block_io_queue_delay` consumes a high percentage of your overall request time, the culprit is software queue congestion. Modern high-speed drives (NVMe) behave differently under stress than legacy mechanical or SATA SSD devices.

Legacy architectures utilized schedulers like `cfq` or `deadline` to reorder disk heads. Modern solid-state drives rely on `blk-mq` (Multi-Queue Block Layer) architectures that map requests directly to parallel hardware queues. By default, the OS might apply `mq-deadline` or `kyber` to throttle and prioritize requests, but on high-throughput workloads, this layer adds CPU overhead and queuing latency.

Use the tuning script below to identify current queue limits, change block scheduling parameters, and observe the results.

```bash
# snippet-4
# tuning_block_io.sh

TARGET_DEV="nvme0n1"

echo "=== Querying Current Queue Configuration for ${TARGET_DEV} ==="
# Check current block scheduler
cat /sys/block/${TARGET_DEV}/queue/scheduler

# Check current block layer request queue limit
echo -n "Queue Limit (nr_requests): "
cat /sys/block/${TARGET_DEV}/queue/nr_requests

# 1. Switch to 'none' scheduler to bypass the software queue logic for NVMe
# This directly feeds requests into the controller, minimizing software queue latency
echo "Switching scheduler to 'none'..."
echo "none" | sudo tee /sys/block/${TARGET_DEV}/queue/scheduler

# 2. Increase software queue limit to prevent request starvation under heavy concurrency
echo "Increasing queue depth limits to 1024..."
echo "1024" | sudo tee /sys/block/${TARGET_DEV}/queue/nr_requests

# 3. Optimize Linux kernel writeback thresholds to prevent aggressive flushing spikes
# Force kernel to start background writeback earlier to avoid massive dirty page flushes
echo "Tuning page cache dirty writeback boundaries..."
sudo sysctl -w vm.dirty_background_ratio=5
sudo sysctl -w vm.dirty_ratio=15

# 4. Run kernel biolatency diagnostics tool to monitor raw performance layout
# Verify latency ranges and verify shift from high software queues
sudo biolatency -D -m 1 10
```

## Orchestrating Telemetry Aggregation with the OpenTelemetry Collector

Exporting trace child spans from user-space agents creates a high telemetry payload. A busy database writing journal transactions will trigger thousands of I/O spans per second. If all spans are sent directly to your tracing backend, network and storage cost will spiral.

To manage this, deploy the OpenTelemetry Collector with a `tail_sampling` processor. This configuration filters out normal execution paths and captures trace graphs *only* if they contain an eBPF-derived queue delay greater than 50 milliseconds or overall request latencies that exceed $p99$ tail thresholds.

```yaml
# snippet-5
# otel-collector-config.yaml
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

  tail_sampling:
    decision_wait: 10s
    num_traces: 50000
    expected_new_traces_per_sec: 2000
    policies:
      # Policy 1: Always retain traces matching kernel I/O queuing spikes >= 50ms
      - name: slow-block-io-retained
        type: numeric_attribute
        numeric_attribute:
          key: io.latency_ms
          value_condition: >=
          threshold: 50.0

      # Policy 2: Retain any root transaction trace with responses exceeding 500ms
      - name: tail-latency-spikes
        type: latency
        latency:
          threshold_ms: 500

exporters:
  otlp/tempo:
    endpoint: tempo-ingest:4317
    tls:
      insecure: true

service:
  pipelines:
    traces:
      receivers: [otlp]
      processors: [tail_sampling, batch]
      exporters: [otlp/tempo]
```

## Identifying Bottlenecks at Scale with ClickHouse Trace Analytics

With the telemetry pipelines integrated and filtering high-latency events, you can pinpoint systemic infrastructure degradation. Instead of manually inspecting individual visual timelines in Jaeger or Honeycomb, query your observability database to aggregate pattern correlations.

Storing OpenTelemetry trace data inside ClickHouse provides highly performant analytical capabilities. The query below calculates the direct mathematical correlation between application-level request latencies and underlying block-layer queuing delays.

```sql
-- snippet-6
-- clickhouse_correlation_query.sql
SELECT
    parent.service_name AS service_name,
    parent.span_name AS endpoint,
    count(*) AS occurrences,
    avg(parent.duration_ns / 1e6) AS avg_app_latency_ms,
    avg(child.duration_ns / 1e6) AS avg_kernel_io_delay_ms,
    -- Calculate the exact percentage contribution of kernel queuing to application latency
    (avg(child.duration_ns) / avg(parent.duration_ns)) * 100 AS queue_contribution_pct
FROM
    otel_traces AS parent
INNER JOIN
    otel_traces AS child
ON
    parent.trace_id = child.trace_id
WHERE
    parent.parent_span_id = '' -- Isolates root level endpoint transaction requests
    AND child.span_name = 'block_io_queue_delay'
    AND child.duration_ns > 50000000 -- Matches kernel queuing delays >= 50ms
    AND parent.duration_ns > 100000000 -- Matches total transaction delays >= 100ms
GROUP BY
    service_name, endpoint
HAVING
    queue_contribution_pct > 20.0
ORDER BY
    queue_contribution_pct DESC
LIMIT 100;
```

## Safe Execution Guidelines for Production eBPF Probes

Deploying custom kernel probes on production database machines requires careful planning. eBPF provides safety guarantees through the in-kernel verifier, but it cannot prevent performance degradation caused by unoptimized code. Keep these rules in mind when deploying:

* **Ring Buffer Overflow Sizing**: The `events` map utilizes `BPF_MAP_TYPE_RINGBUF`. If user-space reading drops behind, kernel events are discarded to prevent kernel memory allocation failures. Ensure the ring buffer size (configured in snippet-1) is large enough to handle peak page flushing cycles.
* **Bounded Map Cleaning**: Always clean up your map keys. The `active_spans` map in snippet-1 is cleaned by the user-space defer block in snippet-3, and request keys are automatically deleted in the `block_rq_complete` tracepoint. If an application exits abnormally, stale entries could accumulate. Implement a periodic user-space sweeping loop that checks if the associated PID still exists in `/proc` and deletes dead mappings.
* **Tracepoint Overhead**: Attaching tracepoints adds negligible latency (typically less than 15 nanoseconds per operation). However, writing string layouts and large arrays to the ring buffer adds copying overhead. Keep trace context variables small, and avoid extracting full file paths inside the kernel layer; query filesystem paths in user space if required.