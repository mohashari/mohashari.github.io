---
layout: post
title: "LLM Prompt Caching: Anthropic and OpenAI Cache Semantics for High-Throughput Inference"
date: 2026-03-28 08:00:00 +0700
tags: [ai-engineering, llm, performance, inference, cost-optimization]
description: "A production-focused breakdown of prompt caching on Anthropic and OpenAI APIs — semantics, gotchas, and patterns for high-throughput systems."
image: "https://picsum.photos/1080/720?random=6745"
thumbnail: "https://picsum.photos/400/300?random=6745"
---

At $15 per million input tokens, a RAG pipeline that prepends 50k tokens of context to every user query burns through budget in hours. That's the problem prompt caching solves — not elegantly, not transparently, but with enough nuance that getting it wrong leaves you paying full price while thinking you're hitting cache. I've seen systems where 80% of API spend was on tokens that should have been cached but weren't, purely because of prefix ordering mistakes and TTL misunderstandings. This post covers how caching actually works across Anthropic and OpenAI, where the semantics diverge, and the production patterns that make the difference.

## What Prompt Caching Actually Is

Both Anthropic and OpenAI cache prompt prefixes on their inference infrastructure. When you send a request, the provider checks if the leading portion of your prompt — the *prefix* — matches a previously computed KV cache entry. If it does, you skip the prefill computation for those tokens. You still pay for generation, but prefill is where the cost lives for long-context workloads.

The practical upshot: a 50k-token system prompt costs ~$0.75 on Claude Sonnet at standard pricing. With cache hits, you pay $0.075 — 90% off. On OpenAI GPT-4o, cached input tokens are billed at 50% of base price. The math is compelling. The challenge is making your requests actually hit cache.

## Anthropic's Explicit Cache Control

Anthropic's caching is opt-in and explicit. You mark which parts of your prompt to cache using `cache_control` blocks. This gives you precise control, but it also means the cache is only as good as your annotation discipline.

```python
# snippet-1
import anthropic

client = anthropic.Anthropic()

SYSTEM_CONTEXT = """You are a senior code reviewer specializing in Go backends.
[...50,000 tokens of coding standards, past review examples, API docs...]
"""

def review_code(diff: str, file_path: str) -> str:
    response = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=2048,
        system=[
            {
                "type": "text",
                "text": SYSTEM_CONTEXT,
                "cache_control": {"type": "ephemeral"},  # Cache this block
            }
        ],
        messages=[
            {
                "role": "user",
                "content": f"Review this diff for {file_path}:\n\n{diff}"
            }
        ]
    )

    usage = response.usage
    print(f"Input tokens: {usage.input_tokens}")
    print(f"Cache creation: {usage.cache_creation_input_tokens}")
    print(f"Cache read: {usage.cache_read_input_tokens}")

    return response.content[0].text
```

The `usage` object tells you exactly what happened. `cache_creation_input_tokens` means you wrote to cache (you pay for prefill at 25% markup). `cache_read_input_tokens` means you read from cache (you pay 10% of base price). If both are zero, nothing was cached.

**The 5-minute TTL is the trap.** Anthropic's cache entries expire after 5 minutes of inactivity. Each cache hit resets the timer. In a low-traffic system, you can find yourself constantly paying cache creation costs because traffic gaps let entries expire. The solution is to warm the cache proactively, not just reactively.

**Minimum token threshold is 1024 for Claude Sonnet/Haiku and 2048 for Claude Opus.** Shorter prompts aren't eligible for caching regardless of `cache_control` annotations. If your system prompt is 800 tokens, you're not getting cache hits — you're just paying for the annotation overhead.

## OpenAI's Automatic Prefix Caching

OpenAI flips the model: caching is automatic, invisible, and based purely on prefix matching. You don't annotate anything. If the first N tokens of your prompt match a previous request's prefix, you get a cache hit. The cached portion is billed at 50% of input token price.

```python
# snippet-2
from openai import OpenAI

client = OpenAI()

SYSTEM_PROMPT = """You are a senior code reviewer specializing in Go backends.
[...50,000 tokens of coding standards, past review examples, API docs...]
"""

def review_code(diff: str, file_path: str) -> dict:
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Review this diff for {file_path}:\n\n{diff}"}
        ],
        max_tokens=2048
    )

    usage = response.usage
    cached_tokens = usage.prompt_tokens_details.cached_tokens if usage.prompt_tokens_details else 0
    print(f"Prompt tokens: {usage.prompt_tokens}")
    print(f"Cached tokens: {cached_tokens}")

    return {
        "review": response.choices[0].message.content,
        "cache_hit_ratio": cached_tokens / usage.prompt_tokens if usage.prompt_tokens > 0 else 0
    }
```

OpenAI's cache TTL is not publicly documented but behaves more like several hours in practice. The minimum cacheable chunk is 1024 tokens. Unlike Anthropic, you can't inspect which specific portion was cached — just the token count.

**The ordering constraint is strict.** OpenAI caches based on prefix. If your system prompt is 50k tokens and the user message is dynamic, you'll hit cache on the system prompt prefix reliably. But if you're prepending dynamic content (user name, timestamp, session ID) *before* your long static context, you break the prefix match entirely. Every request looks like a cache miss.

## Prefix Ordering: The Failure Mode Nobody Talks About

This is where most production systems hemorrhage money. The rule is simple: **static content first, dynamic content last**. But it's easy to violate in practice.

```python
# snippet-3
# WRONG: dynamic content breaks prefix matching
def build_prompt_wrong(user_name: str, docs: str, question: str) -> list[dict]:
    return [
        {
            "role": "system",
            "content": f"User: {user_name}\nSession started: {datetime.now()}\n\n{docs}"
            # Dynamic prefix destroys cache hits
        },
        {"role": "user", "content": question}
    ]

# RIGHT: static corpus first, dynamic user context in messages
def build_prompt_correct(user_name: str, docs: str, question: str) -> list[dict]:
    return [
        {
            "role": "system",
            "content": docs  # Static, long, cacheable
        },
        {
            "role": "user",
            "content": f"[Context: speaking with {user_name}]\n\n{question}"
            # Dynamic content at the end — doesn't affect prefix
        }
    ]
```

With multi-turn conversations, the same principle applies to message history. Anthropic lets you put `cache_control` on individual message blocks, which means you can cache the conversation history up to the last assistant turn and only pay for the new user message. OpenAI will cache whatever prefix matches — so keeping message history stable (not re-ordering or modifying previous turns) is critical.

## Cache Warming in Production

Relying on organic traffic to warm the cache is a bad idea for systems with bursty traffic patterns. A deployment at midnight, a cron job kicking off at 9am, a marketing email blast — these create cold cache conditions right when load spikes. Proactive warming is the answer.

```python
# snippet-4
import asyncio
import anthropic

async def warm_cache(system_prompt: str, model: str = "claude-sonnet-4-5") -> bool:
    """
    Warm the Anthropic prompt cache by making a minimal request.
    Returns True if cache was created, False if already warm.
    """
    client = anthropic.AsyncAnthropic()

    # Use a minimal user message to minimize generation cost
    response = await client.messages.create(
        model=model,
        max_tokens=1,  # Minimize generation cost
        system=[
            {
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": "ping"}]
    )

    usage = response.usage
    cache_created = usage.cache_creation_input_tokens > 0
    cache_hit = usage.cache_read_input_tokens > 0

    if cache_created:
        print(f"Cache warmed: {usage.cache_creation_input_tokens} tokens written")
    elif cache_hit:
        print(f"Cache already warm: {usage.cache_read_input_tokens} tokens in cache")

    return cache_created


async def schedule_cache_warmup(system_prompt: str, interval_seconds: int = 240):
    """
    Re-warm cache every 4 minutes to stay within the 5-minute TTL.
    Run this as a background task alongside your application.
    """
    while True:
        try:
            await warm_cache(system_prompt)
        except Exception as e:
            print(f"Cache warmup failed: {e}")
        await asyncio.sleep(interval_seconds)
```

For Anthropic, you need to keep cache warm every 4 minutes if you want guaranteed hits. In practice, this means running a background task that makes a minimal request on the interval. The cost is one `max_tokens=1` generation every 4 minutes — negligible.

## Monitoring Cache Performance

Flying blind on cache hit rates means you can't catch regressions. A code change that reorders prompt construction, a feature that adds dynamic content to the prefix — these silently tank your cache hit rate and inflate costs.

```python
# snippet-5
from dataclasses import dataclass
from prometheus_client import Counter, Histogram, Gauge
import time

cache_hits_total = Counter("llm_cache_hits_total", "Cache hits", ["provider", "model"])
cache_misses_total = Counter("llm_cache_misses_total", "Cache misses", ["provider", "model"])
cache_creation_tokens = Counter("llm_cache_creation_tokens_total", "Tokens written to cache", ["provider", "model"])
cache_read_tokens = Counter("llm_cache_read_tokens_total", "Tokens read from cache", ["provider", "model"])
request_latency = Histogram("llm_request_duration_seconds", "Request latency", ["provider", "model", "cache_status"])


@dataclass
class CacheMetrics:
    cache_hit: bool
    cache_read_tokens: int
    cache_creation_tokens: int
    total_input_tokens: int
    latency_seconds: float


def instrument_anthropic_call(response, latency: float, model: str) -> CacheMetrics:
    usage = response.usage
    hit = usage.cache_read_input_tokens > 0

    labels = {"provider": "anthropic", "model": model}
    if hit:
        cache_hits_total.labels(**labels).inc()
        cache_read_tokens.labels(**labels).inc(usage.cache_read_input_tokens)
    else:
        cache_misses_total.labels(**labels).inc()

    if usage.cache_creation_input_tokens > 0:
        cache_creation_tokens.labels(**labels).inc(usage.cache_creation_input_tokens)

    cache_status = "hit" if hit else ("creation" if usage.cache_creation_input_tokens > 0 else "miss")
    request_latency.labels(provider="anthropic", model=model, cache_status=cache_status).observe(latency)

    return CacheMetrics(
        cache_hit=hit,
        cache_read_tokens=usage.cache_read_input_tokens,
        cache_creation_tokens=usage.cache_creation_input_tokens,
        total_input_tokens=usage.input_tokens,
        latency_seconds=latency,
    )
```

Alert on cache hit rate dropping below your baseline. In my experience, a healthy RAG pipeline with a stable system prompt should hit 85%+ cache read rates. Below 70% and something is broken — usually prefix ordering or a TTL miss.

## Multi-Turn Conversations and Incremental Caching

Multi-turn conversation patterns are where Anthropic's explicit cache control really shines. You can cache the entire conversation history up to the last exchange, then only pay for the new turn.

```python
# snippet-6
import anthropic
from typing import TypedDict

class Message(TypedDict):
    role: str
    content: str | list

def build_cached_conversation(
    history: list[Message],
    new_user_message: str,
    system_prompt: str,
) -> tuple[list, list[dict]]:
    """
    Build a message list where history is cached and only the new message is fresh.
    Cache the last assistant turn to maximize cache coverage.
    """
    cached_messages = []

    # Cache all but the last user message
    for i, msg in enumerate(history):
        if i == len(history) - 1 and msg["role"] == "assistant":
            # Mark the last assistant turn for caching
            cached_messages.append({
                "role": "assistant",
                "content": [
                    {
                        "type": "text",
                        "text": msg["content"],
                        "cache_control": {"type": "ephemeral"},
                    }
                ]
            })
        else:
            cached_messages.append(msg)

    # Add the new uncached user message
    cached_messages.append({"role": "user", "content": new_user_message})

    system = [
        {
            "type": "text",
            "text": system_prompt,
            "cache_control": {"type": "ephemeral"},
        }
    ]

    return system, cached_messages


client = anthropic.Anthropic()

# Usage in a chat loop
conversation_history = []
system_prompt = "You are a helpful assistant. [Long context...]\n" * 1000  # ~50k tokens

def chat(user_input: str) -> str:
    system, messages = build_cached_conversation(
        conversation_history, user_input, system_prompt
    )

    response = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=1024,
        system=system,
        messages=messages,
    )

    assistant_message = response.content[0].text
    conversation_history.append({"role": "user", "content": user_input})
    conversation_history.append({"role": "assistant", "content": assistant_message})

    return assistant_message
```

## Key Differences at a Glance

| Dimension | Anthropic | OpenAI |
|---|---|---|
| Opt-in | Explicit `cache_control` | Automatic |
| TTL | 5 minutes (refreshed on hit) | Not documented (~hours) |
| Minimum tokens | 1024–2048 | 1024 |
| Cache discount | 90% off (10% of base price) | 50% off |
| Cache creation cost | 25% markup on write | None |
| Visibility | Full per-block metrics | Aggregate cached token count |
| Multi-block caching | Up to 4 breakpoints | Single prefix |

Anthropic's model is more operationally complex but gives you finer control and better discounts. OpenAI's model is zero-configuration but less predictable — you can't force a specific segment to cache independently of the prefix.

## What This Means for System Design

If you're building a high-throughput RAG or agent system, caching changes your architecture decisions:

**System prompt design**: Treat your system prompt as a compiled artifact. Version it, hash it, and never modify it at request time. Put configuration, user context, and dynamic data in the user turn.

**Context window allocation**: With caching, there's less pressure to aggressively trim context. A 30k-token context that caches well costs less than a 10k-token context that doesn't. Run the numbers for your traffic pattern.

**Latency impact**: Cache hits reduce time-to-first-token noticeably — the prefill phase is skipped. For streaming interfaces, this matters. Measure TTFT separately from generation throughput.

**Cost modeling**: Cache creation is not free on Anthropic. If your TTL strategy is poor, you can end up paying 125% of base price (creation markup) most of the time. Model your expected hit rate before committing to caching strategy.

The cache semantics are straightforward once you internalize the prefix constraint. The operational discipline — keeping static content first, monitoring hit rates, warming before traffic — is where most teams slip up. Get that right and you can cut LLM infrastructure costs by 60-80% on context-heavy workloads without touching a single model parameter.
```