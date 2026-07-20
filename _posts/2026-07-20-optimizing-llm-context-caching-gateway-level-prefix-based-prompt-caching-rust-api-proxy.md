---
layout: post
title: "Optimizing LLM Context Caching at the Gateway Level: Implementing Prefix-Based Prompt Caching in a Rust API Proxy"
date: 2026-07-20 08:00:00 +0700
tags: [rust, llm, caching, api-gateway, performance]
description: "Build a high-performance Rust API gateway using Axum and Redis to dynamically inject prefix-based prompt caching, slashing LLM latency and costs by 90%."
image: "https://picsum.photos/seed/7460/1080/720"
thumbnail: "https://picsum.photos/seed/7460/400/300"
---

In production LLM applications, redundant context is the single largest contributor to latency spikes and bloated API bills. When building conversational agents, multi-turn chat applications, or retrieval-augmented generation (RAG) pipelines, system prompts, database schemas, and retrieved documents are frequently re-sent on every interaction. For a 50,000-token system context, this redundant prefill step drives Time-to-First-Token (TTFT) up to 4–6 seconds and costs $0.15 per call using top-tier models. Moving prompt caching decisions into client applications creates a maintenance nightmare: polyglot microservices must coordinate token boundaries, track cache states, and modify outbound request payloads. The solution is to handle context caching transparently at the API gateway layer using a dedicated Rust proxy. By intercepting incoming payloads, normalizing and hashing the message prefixes, tracking hot pathways in Redis, and dynamically injecting provider-specific caching directives, we can drop TTFT down to 150 milliseconds and slice input token costs by up to 90% without changing a single line of client-side code.

![Optimizing LLM Context Caching at the Gateway Level: Implementing Prefix-Based Prompt Caching in a Rust API Proxy Diagram](/images/diagrams/optimizing-llm-context-caching-gateway-level-prefix-based-prompt-caching-rust-api-proxy.svg)

## The Mechanics of LLM Prompt Caching: Provider Realities and Gateway Opportunities

To optimize caching at the gateway, we must first understand the architectural quirks of upstream LLM providers. Providers like Anthropic and OpenAI implement prompt caching differently, forcing the gateway to act as an translation layer.

Anthropic (Claude 3.5 Sonnet / 3.0 Opus) implements *ephemeral prompt caching*, which requires explicit signaling inside the request payload. Developers must inject a `cache_control` block (e.g., `{"type": "ephemeral"}`) at specific breakpoints in the system prompt, tool definitions, or message arrays. Caching is only permitted at designated block boundaries—requiring a minimum prefix size of 1,024 tokens for Claude 3.5 Sonnet and Claude 3.0 Haiku (and 2,048 tokens for Claude 3.0 Opus). Upstream cache writes are billed at a ~25% premium over standard input tokens, but subsequent cache hits are billed at a 90% discount. For example, Claude 3.5 Sonnet input tokens cost $3.00/million, cache writes cost $3.75/million, and cache reads cost $0.30/million. 

OpenAI, by contrast, automatically caches prefixes behind the scenes without explicit request modifications. However, it requires absolute byte-for-byte consistency in the prefix, starting from the system prompt down through the message history. If a single character or parameter change occurs early in the payload, the entire subsequent cache is invalidated.

Implementing this selection logic directly inside application codebases violates separation of concerns. If five different backend services (written in Go, Python, and TypeScript) talk to Claude, each developer must manually count tokens, determine if the prompt prefix exceeds 1,024 tokens, format the complex nested JSON array with `cache_control` markers, and manage cache expiration timeouts. Moving this orchestration logic to a centralized Rust proxy simplifies the client-side system. The proxy normalizes incoming JSON payloads, estimates token lengths, verifies prefix popularity, and modifies the outgoing request to inject provider-specific caching flags.

## Architecture of a Low-Latency Rust API Proxy

An API gateway must introduce negligible latency overhead. In a streaming LLM setup, the gateway's processing time should be under 5 milliseconds. Rust is the ideal choice for this proxy for several reasons:

1. **Deterministic Execution**: The absence of a Garbage Collector (GC) prevents latency spikes during heavy JSON serialization and deserialization.
2. **Concurrent Connection Handling**: Tokio's multi-threaded work-stealing scheduler can process tens of thousands of concurrent TCP streams with minimal memory usage.
3. **Low-Overhead JSON Mutators**: Using `serde_json` or SIMD-accelerated parsers allows the proxy to parse, mutate, and re-serialize requests with sub-millisecond latencies.

To construct our gateway proxy, we begin with a robust set of dependencies. The `Cargo.toml` file declares our HTTP engine (`axum` and `hyper`), asynchronous runtime (`tokio`), token count estimator (`tiktoken-rs`), and state store (`redis`).

```toml
# snippet-1
[package]
name = "llm-caching-proxy"
version = "0.1.0"
edition = "2021"

[dependencies]
tokio = { version = "1.38", features = ["full"] }
axum = { version = "0.7", features = ["macros"] }
hyper = { version = "1.4", features = ["full"] }
hyper-util = { version = "0.1", features = ["client", "client-legacy", "server"] }
http-body-util = "0.1"
serde = { version = "1.0", features = ["derive"] }
serde_json = "1.0"
sha2 = "0.10"
hex = "0.4"
redis = { version = "0.25", features = ["tokio-comp"] }
tiktoken-rs = "0.5"
metrics = "0.22"
metrics-exporter-prometheus = "0.15"
tower = { version = "0.4", features = ["util"] }
tower-http = { version = "0.5", features = ["trace"] }
tracing = "0.1"
```

## Request Normalization: The Key to Prefix Cache Hits

Prefix caching relies on exact matches. In a production pipeline, slight variations in user formatting, dynamic timestamps, random metadata, or varying whitespace will invalidate the cache. The proxy must normalize payloads before computing the cryptographic hash that represents the cache key.

For example, a prompt prefix consists of the `system` message plus the first few messages of a chat transcript. To maximize cache hits, the normalizer must:
- Strip leading and trailing whitespaces from message blocks.
- Convert platform-specific line endings (`\r\n`) to standard line feeds (`\n`).
- Ensure tool definitions and system properties are sorted deterministically.
- Exclude transient fields like `temperature`, `max_tokens`, or client-side tracing IDs from the hashed prefix.

The following module implements a normalizer that takes a chat completion request, extracts the desired prefix length, normalizes the text content, and returns a SHA-256 hash representing the prefix cache signature.

<script src="https://gist.github.com/mohashari/3594af392e827bd9a9ca2fb75b1c356c.js?file=snippet-2.txt"></script>

## Token Counting and Caching Decisions

Applying prompt caching to tiny prompts is counterproductive. Anthropic rejects caching configurations if the prefix token count is below 1,024 tokens. Furthermore, serialization, hashing, and cache lookup over local networks introduce overhead. The gateway should only trigger prompt caching when the estimated token count of the prefix matches or exceeds the provider's threshold.

Running a full Byte-Pair Encoding (BPE) tokenizer like `tiktoken` on every request can introduce CPU bottlenecks at high throughput. To mitigate this, our caching decision engine uses a two-tiered validation path:
1. **Quick Length Check**: An initial estimation based on string length (e.g., characters divided by 4) to filter out requests that are obviously too small.
2. **Deterministic Tokenizer**: A robust tokenizer validation (`tiktoken-rs` using the `cl100k_base` vocabulary) for payloads that cross the rough threshold.

<script src="https://gist.github.com/mohashari/3594af392e827bd9a9ca2fb75b1c356c.js?file=snippet-3.txt"></script>

## Intercepting and Injecting Cache Control Headers

When the proxy determines that a request is a candidate for caching, it mutates the request payload before forwarding it to the upstream LLM API. 

For Anthropic's API, the proxy intercepts the body and dynamically alters the JSON structure. If the system prompt is a simple string, we transform it into an array of blocks, placing the `cache_control` parameter on the last block. If we are caching deep conversation history, we place the `cache_control` directive on the final message that fits within our caching boundaries (e.g., the 4th message of the conversation stream).

<script src="https://gist.github.com/mohashari/3594af392e827bd9a9ca2fb75b1c356c.js?file=snippet-4.txt"></script>

## Managing State: Tracking Hot Prefixes with Redis

Not all long prompts should be cached. Anthropic's write pricing premium means caching a long prompt that is only ever queried once in a 24-hour window increases total billing by 25%. We should only instruct the upstream provider to cache a prompt prefix if we know it is a "hot" prefix—meaning it will be queried at least $N$ times within a sliding window.

To track prefix popularity across distributed proxy instances, the gateway queries a Redis cluster. Using a pipeline, it increments the execution count of the hash signature and sets a TTL. If the hit counter matches or exceeds our caching frequency threshold (e.g., 3 requests in 10 minutes), the proxy marks the response as a candidate for cache-control injection.

<script src="https://gist.github.com/mohashari/3594af392e827bd9a9ca2fb75b1c356c.js?file=snippet-5.txt"></script>

## Handling Streaming Responses and Metric Instrumentation

Most client applications consume LLMs via streaming (Server-Sent Events). The proxy must forward these streams without buffering the entire upstream response, which would destroy the perceived speed advantages of streaming. While routing the response stream, the proxy parses the stream chunks to extract usage statistics (like `cache_creation_input_tokens` and `cache_read_input_tokens`) and exposes them to a Prometheus collector.

The implementation below uses Axum to route requests, parses the incoming payload, evaluates caching options, modifies the request, streams the raw response back to the client, and records the metrics.

<script src="https://gist.github.com/mohashari/3594af392e827bd9a9ca2fb75b1c356c.js?file=snippet-6.txt"></script>

## Production Failure Modes and Mitigation Strategies

Deploying a prompt caching proxy into highly traffic-heavy production environments presents several failure scenarios. Below are the primary failure modes and the design architectures implemented to mitigate them.

### 1. Redis Latency Spikes and Cascading Timeouts
If the Redis cluster experiences high CPU utilization or network division, query latency can exceed 100 milliseconds. If the proxy waits for Redis before forwarding client requests, it negates the latency savings of caching. 

**Mitigation**: Wrap the Redis verification logic in a strict timeout (e.g., 15 milliseconds). If the timeout is reached, fail open, log the warning, bypass cache-control mutation, and forward the unmodified payload directly to the upstream LLM provider.

<script src="https://gist.github.com/mohashari/3594af392e827bd9a9ca2fb75b1c356c.js?file=snippet-7.txt"></script>

### 2. Tokenizer Skew and Upstream Rejections
If your local token calculation disagrees with the upstream tokenizer, the proxy might request caching on a payload containing 1,020 tokens. If the provider rejects the entire request with a 400 Bad Request error due to falling below the 1,024-token boundary, the client's request fails.

**Mitigation**: Implement a safety buffer in the decision engine. If the provider's minimum token threshold is 1,024, configure the gateway's threshold to 1,080 tokens. This buffer protects against discrepancies caused by special character encodings or format translations.

### 3. High CPU Load from Parsing Large Base64 Payloads
If system prompts contain base64-encoded PDF files or images, parsing the JSON payload via standard deserialization routines saturates CPU cores.

**Mitigation**: Inspect the request headers first. If the `content-length` is larger than a configured value (e.g., 5 megabytes), or if the path matches an media uploads bypass rule, skip JSON parsing entirely. Instead, use a fast streaming proxy mode that routes bytes straight through to the upstream provider without inspection or caching attempts.

### 4. Cache Eviction Spikes and Increased Costs
If client requests feature minor variations (e.g., appending a unique request ID or timestamp inside the system message), the proxy will generate a unique hash for every invocation. This will result in write misses on every call, increasing input billing by 25% (Anthropic's cache write fee).

**Mitigation**: Establish strict gateway normalization policies. Ensure all dynamic parameters (such as query IDs, timestamps, and user identifiers) are placed exclusively in standard metadata headers or at the very end of the message array, rather than inside the system prompt or early prefix components. Monitor the cache hit ratio via Prometheus metrics; if the ratio falls below 40%, alerts should trigger to prompt investigations into prefix drift.