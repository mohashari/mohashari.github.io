---
layout: post
title: "Building an Adaptive LLM Routing Gateway for Multi-Model Cost-Latency Tradeoffs"
date: 2026-07-09 08:00:00 +0700
tags: [ai_engineering, golang, sysdesign, redis, ops]
description: "A deep dive into engineering a production-grade LLM routing gateway in Go to dynamically optimize costs, upstream latency, and quality SLAs."
image: "/images/diagrams/building-adaptive-llm-routing-gateway-multi-model-cost-latency-tradeoffs.svg"
thumbnail: "/images/diagrams/building-adaptive-llm-routing-gateway-multi-model-cost-latency-tradeoffs.svg"
---

In production backend engineering, deploying LLM-backed features is a constant battle against the laws of economics and physics. Routing every single user query to a frontier model like Claude 3.5 Sonnet is a financial hazard, easily driving API bills to tens of thousands of dollars per month for moderate-scale systems. Conversely, defaulting entirely to a lightweight model like Gemini 1.5 Flash or a local Llama 3.1 8B instance yields unacceptable regression in output quality for complex reasoning, multi-step orchestration, or code generation. The solution is not to settle for a single compromised model, but to build an intelligent, low-overhead adaptive routing gateway. This gateway operates as a high-performance reverse proxy that inspects incoming queries in real time, evaluates cost-latency utility functions, enforces SLAs, and handles failovers with microsecond-level decision latency.

![Building an Adaptive LLM Routing Gateway for Multi-Model Cost-Latency Tradeoffs Diagram](/images/diagrams/building-adaptive-llm-routing-gateway-multi-model-cost-latency-tradeoffs.svg)

## The Core Architecture: Classification, Decisions, and Resiliency

A production-grade routing gateway sits directly in the request path, positioned between the downstream client applications and the upstream model providers. Traditional API gateways, such as Envoy or Kong, are built for static routing based on URI paths, headers, or simple round-robin weightings. They are fundamentally unequipped to handle the state-dependent, content-driven, and highly variable nature of Large Language Model requests. 

An LLM-native routing gateway must split its operations into three distinct phases: Request Analysis, Policy Evaluation, and Resiliency Execution. When an application client dispatches a prompt, it defines its constraints: a maximum tolerable latency budget (SLA) and a baseline quality target. The gateway ingests this request, estimates the token complexity, evaluates the likelihood of a semantic cache match, and calculates a utility score across all available upstream models. 

To map this process programmatically, we define the foundational structures that govern this metadata exchange. In Go, these types form the contract between the ingestion layer and the internal routing engine:

<script src="https://gist.github.com/mohashari/5d905b6a2de626df87fd020b41d6ddc1.js?file=snippet-1.go"></script>

The config structures allow the engine to weigh a model's capabilities dynamically. A quality score (`QualityScore`) is established via internal eval suites (e.g., MMLU or custom domain-specific evaluations), ranging from `0.0` (unusable) to `1.0` (frontier capability). The latency fields (`P50Latency` and `P99Latency`) are updated continuously in memory via an asynchronous metrics pipeline to prevent upstream performance degradation from poisoning client SLAs.

## The Request Analyzer: Heuristic Token Estimation and Vector Caching

The Request Analyzer must execute in less than 5 milliseconds to avoid becoming a latency bottleneck itself. Calling a second LLM to classify the incoming query is out of the question; it would double the network overhead and run counter to the entire cost-optimization objective. 

Instead, the analyzer uses a combination of two low-overhead techniques:
1. **Fast Token Approximation**: Character-based token heuristics or a locally loaded BPE tokenizer (e.g., a compiled tiktoken wrapper) to determine the approximate size of the prompt. For English text, a simple heuristic of `len(prompt) / 4` provides a rough boundary, but using a Go-based port of the OpenAI cl100k_base tokenizer ensures token boundary precision within 2%.
2. **Vector Semantic Cache Lookup**: Evaluating whether a mathematically similar request has been processed recently. The gateway computes an embedding of the incoming query. Rather than invoking OpenAI's embedding API, which incurs 50–150ms of network latency, the gateway executes a local ONNX runtime running a highly optimized, small embedding model (e.g., `all-MiniLM-L6-v2`, yielding a 384-dimensional vector in ~2ms on CPU).

This embedding vector is queried against a Redis database configured with the RediSearch module. The index is built using a Hierarchical Navigable Small World (HNSW) graph for high-speed k-nearest neighbor (k-NN) search:

<script src="https://gist.github.com/mohashari/5d905b6a2de626df87fd020b41d6ddc1.js?file=snippet-2.go"></script>

If the cosine similarity is above the threshold (e.g., `0.92`), the gateway bypasses the LLM network request entirely, returning the cached response in less than 8ms. If the cache misses, the analyzer hands the query metadata over to the Policy Engine.

## The Policy Engine: Epsilon-Greedy Routing and Utility Functions

The routing choice is formulated as a multi-objective optimization problem. The variables are:
* $Q_m$: The quality score of model $m$.
* $C_m(t_{est})$: The estimated cost of model $m$ for the estimated tokens $t_{est}$.
* $P99_m$: The historical p99 latency of model $m$.
* $SLA$: The client's requested latency SLA.

We define a utility function $U(m)$ to evaluate each candidate model:

$$U(m) = w_{Q} Q_m - w_{C} C_m(t_{est}) - w_{L} \max(0, P99_m - SLA)$$

Where $w_{Q}$, $w_{C}$, and $w_{L}$ are weighting coefficients configured at the gateway level. If a model's p99 latency exceeds the requested SLA, the utility function applies a heavy, non-linear penalty.

To prevent the gateway from making decisions on stale metric data—such as failing to notice when a provider resolves an outage or upgrades their infrastructure—we implement an **Epsilon-Greedy ($\epsilon$-greedy) strategy**. The gateway routes the majority of the traffic $(1 - \epsilon)$ to the optimal model (exploitation), but allocates a small portion ($\epsilon \approx 0.05$) to randomly probe alternative models that meet the request's minimum requirements (exploration).

The Go implementation of this decision matrix is structured as follows:

<script src="https://gist.github.com/mohashari/5d905b6a2de626df87fd020b41d6ddc1.js?file=snippet-3.go"></script>

## Resiliency and Failover: Custom Circuit Breakers and Upstream Overload

Model providers fail in distinct patterns. Rate limits (HTTP 429), token-per-minute (TPM) exhaustions, context-window overloads, and transient internal server errors (HTTP 500/503) are common occurrences at scale. If the gateway simply passes these errors to the client, application stability suffers.

To prevent upstream failures from degrading client systems, the gateway wraps each provider endpoint in a dedicated **Circuit Breaker**. The circuit breaker monitors upstream requests within a sliding window. If the failure rate crosses a 15% threshold, the circuit trips (transitions to `StateOpen`). Subsequent requests immediately skip the degraded provider and route to the next candidate in the `FallbackChain`, saving the overhead of a failed HTTP connection.

Here is the implementation of a thread-safe circuit breaker and fallback client proxy in Go:

<script src="https://gist.github.com/mohashari/5d905b6a2de626df87fd020b41d6ddc1.js?file=snippet-4.go"></script>

## Token Tracking and Real-Time Billing Telemetry Loops

Tracking the exact token usage and financial impact of requests is critical for maintaining an accurate routing matrix. Executing these calculations synchronously in the request-response lifecycle adds latency. To prevent this, the gateway delegates logging and telemetry calculations to an asynchronous queue using buffered channels.

The telemetry worker consumes request logs, updates Prometheus counters for monitoring, and pushes cost metrics to a Redis sliding window. This updates the p50/p99 latency and cost metadata used by the Policy Engine:

<script src="https://gist.github.com/mohashari/5d905b6a2de626df87fd020b41d6ddc1.js?file=snippet-5.go"></script>

## Context Window and Prompt Drift: Schema Adaptation Middleware

Upstream providers use different API request formats. For example, OpenAI's Chat Completions endpoint expects a standard list of message objects, with system prompts included in the array:

```json
{"role": "system", "content": "You are a database optimizer..."}
```

Anthropic's Messages API, however, requires the system prompt to be a top-level property, separate from the `messages` array:

```json
{
  "model": "claude-3-5-sonnet",
  "system": "You are a database optimizer...",
  "messages": [{"role": "user", "content": "..."}]
}
```

To support dynamic routing, the gateway abstracts these details behind a unified internal request schema. The schema adapter maps the unified format to the correct provider representation before dispatching the request:

<script src="https://gist.github.com/mohashari/5d905b6a2de626df87fd020b41d6ddc1.js?file=snippet-6.go"></script>

## Putting It All Together: Serving as a High-Performance Reverse Proxy

The gateway handler acts as the main HTTP entrypoint. It coordinates the request flow: parsing the incoming payload, evaluating the routing policy, executing the HTTP call with fallback logic, and logging telemetry metrics.

<script src="https://gist.github.com/mohashari/5d905b6a2de626df87fd020b41d6ddc1.js?file=snippet-7.go"></script>

## Production Pitfalls: What Will Break

Deploying a multi-model routing gateway introduces operational trade-offs. Below are three key failure modes encountered in high-throughput environments:

### 1. Cascading Timeouts and Remaining-Budget Exhaustion
If the primary route (e.g., Claude 3.5 Sonnet) consumes 1.4 seconds of a 1.5-second SLA budget before failing or timing out, the gateway cannot safely fallback to a secondary model like GPT-4o. Attempting a fallback with only 100 milliseconds remaining will lead to context deadline errors, multiplying upstream costs without returning a successful response to the user.

**Mitigation**: Implement **deadline propagation**. The fallback client must check the remaining time in the context (`ctx.Deadline()`) before initiating a new request. If the remaining budget is less than the p50 latency of the next fallback candidate, the gateway should abort the request chain early and return a lightweight response from the semantic cache or fallback immediately to the fastest local model (e.g., an internal vLLM-hosted model).

### 2. Semantic Cache Drift and Poisoned Inputs
If a cheap fallback model returns a low-quality or partially incorrect answer under high load, caching that response based on semantic similarity can poison the cache. Subsequent requests will serve this incorrect cached response to other users, spreading the error across the system.

**Mitigation**: Tag cache entries with a quality metadata metric. Do not cache answers generated by lower-tier models unless the output passes validation checks. Alternatively, apply a short TTL (e.g., 5 minutes) to cache entries from fallback models, while retaining responses from high-quality primary models for longer periods (e.g., 24 hours).

### 3. State Inconsistency in Structured Output Modes
Modern APIs rely on structured outputs (JSON schema mode) to parse data into backend types. Secondary models often use different JSON formatting strategies than primary models, which can cause JSON parsing errors during fallback execution.

**Mitigation**: Standardize structured outputs at the gateway level. Define JSON schemas using standard schemas (e.g., JSON Schema draft-07) and configure the gateway middleware to validate and normalize response formats before returning them to downstream services.