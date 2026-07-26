---
layout: post
title: "Implementing Custom KV Cache Eviction Policies in vLLM for Multi-Tenant Chatbots"
date: 2026-07-26 08:00:00 +0700
tags: [vllm, kv-cache, multi-tenancy, llmops]
description: "Learn how to override vLLM's block allocator and scheduler to implement a custom, tenant-aware KV cache eviction policy to protect premium SLAs."
image: "https://picsum.photos/seed/3727/1080/720"
thumbnail: "https://picsum.photos/seed/3727/400/300"
---

In a multi-tenant SaaS chatbot architecture, hosting dedicated LLM instances for each customer is a financial non-starter. Instead, senior engineers pack multiple tenants onto shared GPU nodes running high-throughput inference engines like vLLM. However, when free-tier users orchestrate a sudden burst of parallel, long-context conversations, they saturate the GPU's Key-Value (KV) cache. Under vLLM's default First-Come-First-Served (FCFS) and Least-Recently-Used (LRU) block swapping policy, the active KV cache pages of high-value, paying customers are aggressively swapped out to CPU host memory. The resulting swap-in operations introduce massive Time-To-First-Token (TTFT) latency spikes—often jumping from a clean 150ms to upwards of 4,500ms over saturated PCIe Gen 4 lanes. This post walks through the process of hacking into vLLM's memory manager and scheduler to implement a tenant-aware, lease-based KV cache eviction policy that guarantees latency isolation for premium tenants.

![Implementing Custom KV Cache Eviction Policies in vLLM for Multi-Tenant Chatbots Diagram](/images/diagrams/implementing-custom-kv-cache-eviction-policies-vllm-multi-tenant-chatbots.svg)

## How vLLM Manages Memory Under the Hood (and Why it Fails You)

To understand why multi-tenant performance degrades, we have to look at vLLM's core memory allocator, PagedAttention. PagedAttention solves VRAM fragmentation by partitioning the KV cache into fixed-size physical blocks (typically 16 tokens per block). The `BlockSpaceManager` tracks these allocations using logical-to-physical block tables.

When the scheduler runs its execution loop (`Scheduler._schedule()`), it evaluates whether the GPU has enough free physical blocks to accommodate the next token generation for all currently running sequences. If the GPU runs out of physical blocks, the scheduler must free up memory. It does this by executing one of two preemption mechanisms:
1. **Recomputation**: The scheduler discards the KV cache blocks of a sequence entirely. When that sequence is eventually rescheduled, the engine must re-evaluate the prompt from scratch, causing a massive TTFT hit.
2. **Swapping**: The scheduler copies the KV cache blocks from GPU VRAM to CPU RAM over PCIe. When the sequence is ready to run again, these blocks are swapped back to the GPU, causing a latency spike proportional to the context length and PCIe bandwidth.

The fundamental flaw in a multi-tenant environment is that vLLM's default `Scheduler` treats all sequence groups equally. It chooses preemption candidates based strictly on the sequence's arrival time or its active state. If a premium tenant's session is temporarily idle (e.g., the user is reading a response) while a free-tier tenant sends a flurry of requests, the premium tenant's KV cache is instantly flagged as the least recently used and swapped out. When the premium user replies, their session hits the "latency cliff."

## The Solution: Tenant-Aware Lease-Based Eviction (LALB)

To protect premium tenants, we must replace the default scheduling heuristics with a Tenant-Aware Lease-Based Eviction policy. This policy enforces three rules:
- **Priority Tiering**: Every incoming request must be tagged with a tenant identifier and a priority score (0 for Premium SLA, 1 for Standard, 2 for Free).
- **Minimum Cache Leases**: High-priority tenants are guaranteed a minimum "lease duration" (e.g., 300 seconds) during which their KV cache blocks cannot be preempted or swapped, even if the connection is idle.
- **SLA-Prioritized Preemption**: When GPU memory saturation triggers preemption, the engine must exhaustively evict and swap blocks from the lowest-priority active tenants before touching higher-priority blocks.

To implement this, we must extend the request metadata pipeline, build a tracker to maintain state on active tenant leases, and override the scheduler's candidate evaluation logic.

## Hacking vLLM: Intercepting and Injecting Tenant Metadata

First, we must capture tenant and SLA metadata at the API boundary and pass it down to the engine. vLLM uses an internal class called `SequenceGroup` to represent a request. We need to subclass this or use vLLM's custom request parameters to inject our tenant payload. 

The following snippet implements a FastAPI middleware layer that extracts tenant metadata from JWT claims and injects it into the engine's internal request options.

<script src="https://gist.github.com/mohashari/0a3cfd724251024fc76be5c51b58ec1e.js?file=snippet-1.py"></script>

Next, when we submit the request to the vLLM engine via the AsyncLLMEngine, we attach this metadata to the `RequestOutput` and `SequenceGroup` objects. Below is the custom entry point that overrides the engine request submission payload.

<script src="https://gist.github.com/mohashari/0a3cfd724251024fc76be5c51b58ec1e.js?file=snippet-2.py"></script>

## The Tenant-Aware Memory Tracker

To evaluate eviction candidates, the scheduler must have access to global allocation statistics grouped by tenant. We maintain a centralized `TenantCacheTracker` inside our scheduler state. This component keeps track of physical blocks occupied by each tenant and monitors active leases.

<script src="https://gist.github.com/mohashari/0a3cfd724251024fc76be5c51b58ec1e.js?file=snippet-3.py"></script>

## Overriding the Scheduler's Swap and Preempt Logic

This is where we execute the core intervention. We must inject our custom eviction logic into vLLM's scheduling decisions. In `vllm/core/scheduler.py`, the engine schedules running sequences. When resources are constrained, the method `_schedule_default` selects sequences to preempt.

We will write a custom scheduling filter that intercepts the list of running sequence groups and re-orders the preemption selection process. Instead of preempting the newest request (which could belong to a premium tenant that just initiated a high-value generation), we select candidates using the following heuristic:

1. Scan for active sequence groups belonging to tenants whose lease has expired.
2. Order those candidates by priority tier descending (Free -> Standard -> Premium).
3. If all active sequences have unexpired leases, we fallback to preempting the lowest priority tier, regardless of lease status, but we raise a telemetry event indicating SLA degradation.

<script src="https://gist.github.com/mohashari/0a3cfd724251024fc76be5c51b58ec1e.js?file=snippet-4.py"></script>

Now we integrate this helper by monkey-patching or subclassing the vLLM `Scheduler`. In production, subclassing is cleaner. We override `_preempt` decisions in the scheduling loop to ensure our prioritization controls block reclamation.

<script src="https://gist.github.com/mohashari/0a3cfd724251024fc76be5c51b58ec1e.js?file=snippet-5.py"></script>

## Monitoring Isolation: Prometheus Metrics & Alerting

An eviction policy is only as good as its production telemetry. To verify that premium tenants are insulated from noisy neighbors, we export custom Prometheus metrics. We track the `kv_cache_evictions_total` and `kv_cache_swaps_total` labeled by `tenant_id` and `priority`.

<script src="https://gist.github.com/mohashari/0a3cfd724251024fc76be5c51b58ec1e.js?file=snippet-6.py"></script>

With these metrics exposed, you can write alert rules in Prometheus to detect SLA breaches immediately. For example, if a premium tenant (priority 0) incurs any eviction or swap events, the systems engineering team must be alerted to scale out the cluster.

```yaml
# // snippet-7
groups:
  - name: vllm-multi-tenant-sla
    rules:
      - alert: PremiumTenantKVMemoryEvicted
        expr: sum by (tenant_id) (rate(vllm_tenant_kv_cache_evictions_total{priority="0"}[1m])) > 0
        for: 10s
        labels:
          severity: critical
          tier: ai-platform
        annotations:
          summary: "Premium tenant {{ $labels.tenant_id }} is experiencing KV cache evictions"
          description: "GPU VRAM is saturated and leases are being broken for premium traffic. Scale inference nodes immediately."
```

## Production Considerations & Fallback Mechanisms

When putting this system into production at scale, several edge cases can break your isolation guarantees:

### 1. The Premium Lease Saturation Failure Mode
What happens when all active sequences on the GPU belong to premium tenants with active leases, and memory is fully exhausted?
If the scheduler strictly respects the lease, it will hang or raise an Out-Of-Memory (OOM) error. To prevent catastrophic failure, you must implement a "graceful fallback." When no low-priority candidates exist, the lease tracking system must enter a degraded execution state. It will dynamically shorten leases for the oldest premium sessions to allow the node to continue executing tokens.

### 2. High-Frequency Swapping Overhead
Swapping blocks to CPU RAM creates a major bottleneck on the PCIe bus. In multi-tenant environments with highly active workloads, high-frequency swapping can degrade performance for *all* tenants on the GPU, because the PCIe bus transfer latency blocks the execution threads of the Python runtime.
- **Tip**: Keep `--max-num-seqs` tightly bound to prevent excessive concurrent swap queues.
- **Rule of Thumb**: Allocate at least 15% of GPU memory as a safety margin for active PagedAttention blocks, and set `gpu_memory_utilization` to no more than `0.85`.

### 3. Eviction vs. Recomputation Configuration
For long-context conversations (e.g., sessions with >16k context window tokens), swapping to CPU is almost always faster than recomputing. However, for short conversations (<2k context tokens), recomputation is often faster than waiting for PCIe bus serialization. Your controller should dynamically choose the preemption mode based on the sequence's allocated block count:

<script src="https://gist.github.com/mohashari/0a3cfd724251024fc76be5c51b58ec1e.js?file=snippet-8.py"></script>

## Conclusion

Standard inference schedulers are built with single-tenant workloads in mind. When you operate a multi-tenant LLM gateway, neglecting KV cache isolation is a recipe for sporadic latency spikes and broken customer trust. By writing context-aware middleware, tracking allocation state in a custom scheduler tracker, and overriding the preemption decision loop, you ensure that premium requests are insulated from noisy neighbors. Integrate these modifications into your vLLM deployment to run cost-effective, high-density, and latency-stable multi-tenant chatbot clusters.