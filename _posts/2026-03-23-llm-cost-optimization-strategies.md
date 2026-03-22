---
layout: post
title: "LLM Cost Optimization: Caching, Batching, and Model Routing"
date: 2026-03-23 08:00:00 +0700
tags: [llm, ai-engineering, cost-optimization, backend, architecture]
description: "Three concrete strategies — semantic caching, request batching, and model routing — that eliminate 60-80% of production LLM spend without sacrificing quality."
---

Your LLM bill is a mirror of your architecture decisions, and most teams are staring at an ugly reflection. The typical trajectory looks like this: you prototype with GPT-4, ship to production, get real traffic, then watch your monthly invoice climb from hundreds to tens of thousands of dollars before anyone asks whether every single request actually needed your most expensive model. The answer is almost always no. A customer support classification task that routes tickets to departments does not need the same model as a nuanced legal document summarizer. A product description fetch that a thousand users requested in the last hour does not need a fresh inference call. Treating every LLM request as identical is the root cause — and fixing it requires three distinct architectural patterns that compound on each other.

## Semantic Caching: Stop Paying for Questions You've Already Answered

Exact-match caching of LLM responses is table stakes. The real leverage comes from semantic caching — recognizing that "how do I reset my password?" and "password reset steps?" should return the same cached response. The distance between these strings in embedding space is tiny; the distance in compute cost is one full inference call.

The architecture is straightforward: embed incoming requests with a cheap, fast embedding model (text-embedding-3-small at $0.02/1M tokens, or a self-hosted `bge-small-en-v1.5`), query a vector store for nearest neighbors within a similarity threshold, and return cached responses when you hit. Cache misses proceed to inference, then write back.

```python
# snippet-1
import hashlib
import numpy as np
from redis import Redis
from openai import OpenAI

client = OpenAI()
redis = Redis(host="localhost", port=6379, decode_responses=False)

SIMILARITY_THRESHOLD = 0.92
EMBEDDING_MODEL = "text-embedding-3-small"
CACHE_TTL = 86400 * 7  # 7 days


def get_embedding(text: str) -> list[float]:
    response = client.embeddings.create(input=text, model=EMBEDDING_MODEL)
    return response.data[0].embedding


def cosine_similarity(a: list[float], b: list[float]) -> float:
    a, b = np.array(a), np.array(b)
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))


def semantic_cache_get(query: str) -> str | None:
    query_embedding = get_embedding(query)
    query_key = f"emb:{hashlib.sha256(query.encode()).hexdigest()}"

    # Scan recent cache keys — in production, use a vector DB like Qdrant or pgvector
    cursor = 0
    while True:
        cursor, keys = redis.scan(cursor, match="llm_cache:*", count=100)
        for key in keys:
            cached = redis.hgetall(key)
            if not cached:
                continue
            cached_embedding = list(map(float, cached[b"embedding"].decode().split(",")))
            similarity = cosine_similarity(query_embedding, cached_embedding)
            if similarity >= SIMILARITY_THRESHOLD:
                redis.expire(key, CACHE_TTL)  # refresh TTL on hit
                return cached[b"response"].decode()
        if cursor == 0:
            break
    return None


def semantic_cache_set(query: str, response: str) -> None:
    query_embedding = get_embedding(query)
    cache_key = f"llm_cache:{hashlib.sha256(query.encode()).hexdigest()}"
    redis.hset(cache_key, mapping={
        "query": query,
        "response": response,
        "embedding": ",".join(map(str, query_embedding)),
    })
    redis.expire(cache_key, CACHE_TTL)
```

The scanning approach above is for illustration — at production scale you need a proper vector index. pgvector with an HNSW index handles millions of cached embeddings with sub-10ms lookup times. Qdrant is a better choice if you're running a dedicated vector store already.

The threshold is the critical tuning parameter. At 0.92 you'll get high cache hit rates but occasional false positives — semantically similar but contextually different queries returning wrong cached answers. For customer-facing chat, 0.95 is safer. For internal classification pipelines where queries are templated, 0.90 is fine. Measure your false positive rate during the first week by logging cache hits alongside user feedback signals.

Real numbers from a mid-sized SaaS product: semantic caching on a customer support chat reduced inference calls by 67% within 30 days of launch. The first week was only 30% hit rate — the cache was cold. By week four, common question patterns were saturated and hit rates stabilized above 65%. At $0.03/1K tokens on GPT-4o with average 800-token responses, that's roughly $2,400/month saved on 100K daily requests.

## Request Batching: Amortize Fixed Costs Across More Work

Every LLM API call has overhead independent of token count — network round trips, scheduling, load balancer overhead. When you're firing single-item inference requests one-at-a-time from a high-throughput pipeline, you're paying that overhead thousands of times. Batching collapses it.

The Anthropic Batch API and OpenAI Batch API both offer 50% cost reduction on batched workloads. The tradeoff is latency — batch jobs complete within 24 hours, not milliseconds. This is fine for a wide class of backend jobs: nightly content classification, bulk embedding generation, offline document processing, scheduled report generation.

```python
# snippet-2
import asyncio
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable, Any
from openai import AsyncOpenAI

client = AsyncOpenAI()


@dataclass
class BatchRequest:
    messages: list[dict]
    model: str
    future: asyncio.Future = field(default_factory=asyncio.get_event_loop().create_future)


class LLMBatcher:
    """Collects requests for up to `window_ms` milliseconds, then flushes as a batch."""

    def __init__(self, window_ms: int = 50, max_batch_size: int = 20):
        self.window_ms = window_ms
        self.max_batch_size = max_batch_size
        self._queue: list[BatchRequest] = []
        self._lock = asyncio.Lock()
        self._flush_task: asyncio.Task | None = None

    async def infer(self, messages: list[dict], model: str = "gpt-4o-mini") -> str:
        loop = asyncio.get_event_loop()
        req = BatchRequest(messages=messages, model=model, future=loop.create_future())

        async with self._lock:
            self._queue.append(req)
            if len(self._queue) >= self.max_batch_size:
                await self._flush()
            elif self._flush_task is None or self._flush_task.done():
                self._flush_task = asyncio.create_task(self._delayed_flush())

        return await req.future

    async def _delayed_flush(self):
        await asyncio.sleep(self.window_ms / 1000)
        async with self._lock:
            await self._flush()

    async def _flush(self):
        if not self._queue:
            return
        batch, self._queue = self._queue[:], []

        # Group by model to send correctly typed requests
        by_model: dict[str, list[BatchRequest]] = defaultdict(list)
        for req in batch:
            by_model[req.model].append(req)

        for model, requests in by_model.items():
            tasks = [
                client.chat.completions.create(
                    model=model,
                    messages=req.messages,
                )
                for req in requests
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for req, result in zip(requests, results):
                if isinstance(result, Exception):
                    req.future.set_exception(result)
                else:
                    req.future.set_result(result.choices[0].message.content)
```

This windowed batcher is useful when you have many concurrent async callers hitting the same service — think a FastAPI endpoint processing webhook events that each trigger an LLM call. Instead of 50 separate API round trips in a burst, you get one batch call with 50 completions. The throughput improvement is 3-5x at high concurrency; the latency cost is one window duration (50ms here).

For offline batch jobs, use the native batch endpoints directly. Anthropic's Message Batches API accepts up to 10,000 requests per batch and guarantees 24-hour completion — the 50% discount makes this a clear win for any non-realtime pipeline:

```python
# snippet-3
import anthropic
import json

client = anthropic.Anthropic()


def submit_classification_batch(items: list[dict]) -> str:
    """Submit a batch of classification requests, return batch ID for polling."""
    requests = [
        {
            "custom_id": f"item-{item['id']}",
            "params": {
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 50,
                "messages": [{
                    "role": "user",
                    "content": f"Classify this support ticket into one of [billing, technical, account, other]. Reply with just the category.\n\nTicket: {item['text']}"
                }]
            }
        }
        for item in items
    ]

    batch = client.messages.batches.create(requests=requests)
    return batch.id


def poll_batch_results(batch_id: str) -> dict[str, str]:
    """Poll until complete, return mapping of custom_id -> classification."""
    import time
    while True:
        batch = client.messages.batches.retrieve(batch_id)
        if batch.processing_status == "ended":
            break
        time.sleep(60)

    results = {}
    for result in client.messages.batches.results(batch_id):
        if result.result.type == "succeeded":
            results[result.custom_id] = result.result.message.content[0].text.strip()
    return results
```

## Model Routing: Match Complexity to Capability

The highest-leverage cost reduction is model routing — dynamically selecting the cheapest model capable of handling each request. The price differential is stark: GPT-4o costs roughly 15x more than GPT-4o-mini. Claude Opus 4.6 costs roughly 20x more than Claude Haiku 4.5. If you can route even 70% of your traffic to cheaper models, the math is brutal in your favor.

The routing decision lives on two axes: task complexity and quality tolerance. Simple classification, extraction, and templated generation tasks run fine on small models. Complex reasoning, code generation requiring correctness, and nuanced synthesis need the frontier models.

There are two practical approaches. The first is rule-based routing — fast, predictable, zero cost:

```python
# snippet-4
from enum import Enum
from dataclasses import dataclass


class ModelTier(Enum):
    CHEAP = "claude-haiku-4-5-20251001"      # $0.25/1M input, $1.25/1M output
    MID = "claude-sonnet-4-6"                 # $3/1M input, $15/1M output
    FRONTIER = "claude-opus-4-6"              # $15/1M input, $75/1M output


@dataclass
class RoutingContext:
    task_type: str
    estimated_input_tokens: int
    requires_code: bool
    requires_reasoning: bool
    quality_critical: bool


def route_model(ctx: RoutingContext) -> ModelTier:
    # Always use frontier for quality-critical or complex reasoning
    if ctx.quality_critical or ctx.requires_reasoning:
        return ModelTier.FRONTIER

    # Code generation needs mid-tier minimum
    if ctx.requires_code:
        return ModelTier.MID

    # Simple extraction and classification on cheap tier
    if ctx.task_type in {"classification", "extraction", "summarization", "translation"}:
        return ModelTier.CHEAP

    # Long context tasks — cheaper models degrade faster, use mid
    if ctx.estimated_input_tokens > 8000:
        return ModelTier.MID

    return ModelTier.CHEAP


# Usage: classify task type before invoking LLM
def classify_request(user_query: str, context: dict) -> RoutingContext:
    return RoutingContext(
        task_type=context.get("task_type", "general"),
        estimated_input_tokens=len(user_query.split()) * 1.3,  # rough tokenization
        requires_code="code" in user_query.lower() or "implement" in user_query.lower(),
        requires_reasoning=context.get("chain_of_thought", False),
        quality_critical=context.get("quality_critical", False),
    )
```

The second approach is classifier-based routing — train a small model (or use a cheap LLM call) to predict required model tier. This costs one cheap inference call but can route more accurately on ambiguous inputs:

```python
# snippet-5
import anthropic

client = anthropic.Anthropic()

ROUTER_SYSTEM = """You are a routing classifier. Given a user request, output ONLY one of:
SIMPLE - for classification, extraction, translation, templated generation
MODERATE - for code generation, structured analysis, multi-step tasks  
COMPLEX - for open-ended reasoning, creative tasks, legal/medical analysis, or anything requiring nuanced judgment

Output exactly one word."""


def classify_complexity(user_message: str) -> str:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",  # Use cheap model to route to expensive models
        max_tokens=10,
        system=ROUTER_SYSTEM,
        messages=[{"role": "user", "content": user_message}]
    )
    return response.content[0].text.strip()


MODEL_MAP = {
    "SIMPLE": "claude-haiku-4-5-20251001",
    "MODERATE": "claude-sonnet-4-6",
    "COMPLEX": "claude-opus-4-6",
}


def routed_completion(user_message: str, system: str = "") -> tuple[str, str]:
    complexity = classify_complexity(user_message)
    model = MODEL_MAP.get(complexity, "claude-sonnet-4-6")

    response = client.messages.create(
        model=model,
        max_tokens=1024,
        system=system,
        messages=[{"role": "user", "content": user_message}]
    )
    return response.content[0].text, model  # return model used for cost tracking
```

The classification call costs ~50 tokens on Haiku — essentially free. If it saves you from routing even 3% of traffic to Opus when Haiku would suffice, it pays for itself immediately.

## The Tiered Cost Architecture

These three patterns compose. A request hits semantic cache first — if it's a hit, you pay for one embedding call (fractions of a cent). If it's a cache miss, the router classifies complexity. If it's SIMPLE, it goes to Haiku directly. If it's MODERATE or COMPLEX, it proceeds to the appropriate tier. Async batch requests skip the latency-sensitive path entirely and go straight to the batch endpoint at half price.

The cost reduction compounds:
- Semantic cache: eliminates 50-70% of total inference calls
- Model routing: reduces per-call cost by 60-80% on cache misses
- Batching: reduces remaining async workload cost by 50%

On a baseline of $10,000/month in LLM spend, this architecture routinely gets teams to $2,000-3,500/month — a 65-80% reduction. The exact numbers depend on your request distribution, but the directional improvement is consistent across workloads I've seen in production.

## Instrumentation You Need

None of this works without measurement. Track cache hit rate, model distribution per day, and cost-per-request broken down by model tier. The worst outcome is routing too aggressively to cheap models and degrading output quality in ways your users notice before you do.

```python
# snippet-6
import time
from dataclasses import dataclass
from prometheus_client import Counter, Histogram, Gauge

llm_requests_total = Counter(
    "llm_requests_total", "Total LLM requests", ["model", "cache_status", "task_type"]
)
llm_cost_dollars = Counter(
    "llm_cost_dollars_total", "Estimated LLM cost in dollars", ["model"]
)
llm_latency = Histogram(
    "llm_request_duration_seconds", "LLM request latency", ["model", "cache_status"]
)

# Pricing per 1M tokens (input/output average approximation)
MODEL_COST_PER_TOKEN = {
    "claude-haiku-4-5-20251001": 0.000_000_8,
    "claude-sonnet-4-6": 0.000_009,
    "claude-opus-4-6": 0.000_045,
    "gpt-4o-mini": 0.000_000_6,
    "gpt-4o": 0.000_005,
}


def record_llm_call(model: str, tokens_used: int, cache_hit: bool, task_type: str, latency: float):
    cache_status = "hit" if cache_hit else "miss"
    llm_requests_total.labels(model=model, cache_status=cache_status, task_type=task_type).inc()
    llm_latency.labels(model=model, cache_status=cache_status).observe(latency)

    if not cache_hit:
        cost = MODEL_COST_PER_TOKEN.get(model, 0) * tokens_used
        llm_cost_dollars.labels(model=model).inc(cost)
```

Set up a Grafana dashboard with cache hit rate by task type, model tier distribution over time, and daily cost trend. When cache hit rate drops suddenly, you've introduced a new request type or your semantic threshold is too strict. When the model distribution shifts toward expensive tiers, check whether your routing logic is regressing or whether the traffic mix genuinely changed.

The engineering investment here is two to three weeks for a team that hasn't done this before. The payback period, even at modest LLM spend, is measured in days. Ship the cache first — it's the highest leverage with the least operational risk. Add routing second. Add batching last, since it requires changes to how you structure async workloads. All three together make LLM-powered features economically sustainable at scale instead of a cost center that finance keeps questioning every quarter.
```