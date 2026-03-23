---
layout: post
title: "Implementing Tool Use and Function Calling in Production LLM Agents"
date: 2026-03-23 08:00:00 +0700
tags: [ai-engineering, llm, backend, python, production]
description: "A production engineer's guide to tool use and function calling in LLM agents — retry logic, schema design, and failure modes that will burn you."
image: ""
thumbnail: ""
---

The first time you ship an LLM agent with tool use to production, it works great in staging. The model calls `get_order_status`, returns the right JSON, and everything chains together cleanly. Then at 2am you get paged because the model started passing `order_id` as an integer instead of a string, your validation layer threw a 422, the agent retried three times with the same bad input, and now you have 400 failed sessions sitting in the queue and a confused on-call engineer staring at logs. Tool use in LLMs is not a feature — it's a contract between your model and your backend services. Break the contract and you break your users. This post is about engineering that contract to be durable under real production conditions.

## What Tool Use Actually Is

When Anthropic, OpenAI, or Google say "function calling" or "tool use," they mean roughly the same thing: the model outputs a structured object describing a tool invocation instead of (or alongside) prose. Your application executes that invocation, returns the result, and the model continues generating with that result in context.

The critical distinction is that the model does not execute code. It outputs structured text that looks like a function call. You execute it. This means you own the entire execution layer — validation, dispatch, error handling, timeout, retry, and result serialization.

In practice this looks like a multi-turn exchange:

```python
# snippet-1
import anthropic
import json
from typing import Any

client = anthropic.Anthropic()

tools = [
    {
        "name": "get_order",
        "description": "Retrieve order details by order ID. Returns order status, items, and shipping info.",
        "input_schema": {
            "type": "object",
            "properties": {
                "order_id": {
                    "type": "string",
                    "description": "The order UUID (format: ord_xxxxxxxxxxxxxxxx)"
                },
                "include_history": {
                    "type": "boolean",
                    "description": "Whether to include status change history",
                    "default": False
                }
            },
            "required": ["order_id"]
        }
    }
]

def run_agent_turn(messages: list[dict], max_tool_rounds: int = 5) -> str:
    for _ in range(max_tool_rounds):
        response = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=1024,
            tools=tools,
            messages=messages
        )

        if response.stop_reason == "end_turn":
            return next(
                block.text for block in response.content
                if block.type == "text"
            )

        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})
            tool_results = []

            for block in response.content:
                if block.type == "tool_use":
                    result = dispatch_tool(block.name, block.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result)
                    })

            messages.append({"role": "user", "content": tool_results})

    raise RuntimeError("Agent exceeded max tool rounds without completing")
```

The loop is where most production bugs live. Notice `max_tool_rounds` — without it, a misbehaving model can spin forever. Five rounds is a reasonable ceiling for most tasks; bump it only for explicitly multi-step workflows with known chain lengths.

## Schema Design Is Your First Line of Defense

The JSON schema you provide for each tool is not documentation — it is the primary mechanism preventing malformed calls. Every hour you spend tightening schemas is an hour you avoid debugging production incidents.

Bad schemas are vague. Good schemas are prescriptive:

```python
# snippet-2
# Bad: vague types and no constraints
bad_schema = {
    "type": "object",
    "properties": {
        "date": {"type": "string"},
        "amount": {"type": "number"},
        "status": {"type": "string"}
    }
}

# Good: constrained, with examples, documented edge cases
good_schema = {
    "type": "object",
    "properties": {
        "date": {
            "type": "string",
            "format": "date",
            "description": "ISO 8601 date (YYYY-MM-DD). Use UTC. Do not include time component.",
            "example": "2026-03-23"
        },
        "amount_cents": {
            "type": "integer",
            "description": "Amount in cents (USD). Must be positive. Max 999999 ($9999.99).",
            "minimum": 1,
            "maximum": 999999
        },
        "status": {
            "type": "string",
            "enum": ["pending", "processing", "completed", "failed", "refunded"],
            "description": "Current transaction status. Use 'pending' for newly created transactions."
        }
    },
    "required": ["date", "amount_cents", "status"],
    "additionalProperties": False
}
```

Three patterns that will save you in production: use `additionalProperties: false` to reject unexpected fields the model hallucinates, prefer `amount_cents` over `amount` to eliminate float precision ambiguity, and use `enum` wherever the value set is finite. Models almost always pick from enums correctly; open strings are where they improvise.

## The Dispatch Layer

The dispatch layer is where tool calls become real side effects. Treat it like a mini service boundary — strict input validation, explicit error types, structured logging on every call.

```python
# snippet-3
import logging
import time
from dataclasses import dataclass
from typing import Any
import jsonschema

logger = logging.getLogger(__name__)

@dataclass
class ToolResult:
    success: bool
    data: Any
    error_code: str | None = None
    error_message: str | None = None

TOOL_REGISTRY: dict[str, callable] = {}
TOOL_SCHEMAS: dict[str, dict] = {}

def register_tool(name: str, schema: dict):
    def decorator(fn):
        TOOL_REGISTRY[name] = fn
        TOOL_SCHEMAS[name] = schema
        return fn
    return decorator

def dispatch_tool(tool_name: str, raw_input: dict) -> dict:
    start = time.monotonic()
    log_ctx = {"tool": tool_name, "input_keys": list(raw_input.keys())}

    if tool_name not in TOOL_REGISTRY:
        logger.warning("unknown_tool_called", extra=log_ctx)
        return ToolResult(
            success=False,
            data=None,
            error_code="UNKNOWN_TOOL",
            error_message=f"Tool '{tool_name}' is not registered"
        ).__dict__

    # Validate against schema before execution
    try:
        jsonschema.validate(raw_input, TOOL_SCHEMAS[tool_name])
    except jsonschema.ValidationError as e:
        logger.error("tool_input_validation_failed", extra={**log_ctx, "error": str(e.message)})
        return ToolResult(
            success=False,
            data=None,
            error_code="INVALID_INPUT",
            error_message=f"Validation failed: {e.message}"
        ).__dict__

    try:
        result = TOOL_REGISTRY[tool_name](**raw_input)
        elapsed_ms = (time.monotonic() - start) * 1000
        logger.info("tool_executed", extra={**log_ctx, "elapsed_ms": round(elapsed_ms, 2)})
        return ToolResult(success=True, data=result).__dict__
    except Exception as e:
        elapsed_ms = (time.monotonic() - start) * 1000
        logger.exception("tool_execution_failed", extra={**log_ctx, "elapsed_ms": round(elapsed_ms, 2)})
        return ToolResult(
            success=False,
            data=None,
            error_code="EXECUTION_ERROR",
            error_message="Internal error executing tool. Please try a different approach."
        ).__dict__
```

The error message in `EXECUTION_ERROR` is intentionally vague to the model. You do not want to leak stack traces into the LLM context. The model should get enough signal to decide whether to retry with different arguments or give up gracefully.

## Returning Rich Errors the Model Can Act On

One of the most underrated patterns: structured error responses that the model can reason about. If you return `{"success": false, "error": "not found"}`, the model might retry blindly. If you return context, it can course-correct:

```python
# snippet-4
@register_tool("search_customers", schema={
    "type": "object",
    "properties": {
        "query": {"type": "string", "minLength": 3},
        "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10}
    },
    "required": ["query"],
    "additionalProperties": False
})
def search_customers(query: str, limit: int = 10) -> dict:
    results = db.search_customers(query, limit=limit)

    if not results:
        # Rich error: give the model something to act on
        return {
            "found": 0,
            "results": [],
            "suggestions": [
                "Try a shorter search term",
                "Check for typos in the name or email",
                "Use partial matches: 'john' instead of 'john.doe@example.com'"
            ],
            "debug": {
                "query_used": query,
                "index": "customers_v3",
                "searched_fields": ["name", "email", "phone"]
            }
        }

    return {
        "found": len(results),
        "results": [
            {
                "id": r.id,
                "name": r.name,
                "email": r.email,
                "account_status": r.status
            }
            for r in results
        ]
    }
```

This pattern consistently improves agent success rates by 15-30% in our experience. Models are good at following explicit hints when they appear in context.

## Parallel Tool Calls

Claude and GPT-4 can emit multiple tool calls in a single turn. This is a significant performance win for independent lookups — instead of sequential round trips, you get parallel execution:

```python
# snippet-5
import asyncio
from anthropic import AsyncAnthropic

async_client = AsyncAnthropic()

async def execute_tool_async(name: str, input: dict) -> dict:
    # Wrap synchronous tool dispatch in thread pool for I/O-bound tools
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, dispatch_tool, name, input)

async def run_agent_async(messages: list[dict]) -> str:
    response = await async_client.messages.create(
        model="claude-opus-4-6",
        max_tokens=2048,
        tools=tools,
        messages=messages
    )

    if response.stop_reason == "tool_use":
        tool_blocks = [b for b in response.content if b.type == "tool_use"]

        # Execute all tool calls in parallel
        tasks = [
            execute_tool_async(block.name, block.input)
            for block in tool_blocks
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        tool_results = []
        for block, result in zip(tool_blocks, results):
            if isinstance(result, Exception):
                content = json.dumps({
                    "success": False,
                    "error_code": "ASYNC_EXECUTION_ERROR",
                    "error_message": str(result)
                })
            else:
                content = json.dumps(result)

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": content
            })

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

    return response
```

For an agent that calls three independent data sources, parallel execution drops median latency from ~3s to ~1.2s. That is not a micro-optimization at production scale.

## Failure Modes You Will Hit

**Schema drift**: Your database schema changes, your tool's output format changes, but you forget to update the tool schema description. The model starts making calls that used to work and now fail validation. Fix: treat tool schemas as versioned contracts and run integration tests against them in CI.

**Context explosion**: Each tool result gets appended to the message context. An agent doing 10 tool calls with 2KB results each burns 20KB of context before the model writes a single word of response. At Claude's pricing this adds up. More importantly, it degrades response quality — models get worse at long contexts. Fix: truncate tool results aggressively. Return summaries and IDs, not full payloads. A customer search should return `{id, name, email}`, not the full 50-field customer record.

**Hallucinated tool names**: Models occasionally invent tool names that do not exist in your registry. This is more common when you have many tools (10+) or when tool names are similar. Log `UNKNOWN_TOOL` calls and monitor them — a spike indicates prompt issues. Fix: use `tool_choice` to constrain which tools can be called when you know the task type.

**Infinite retry loops**: The model calls a tool, gets an error, calls it again with identical arguments, gets the same error, repeats. Fix: track tool call history within a session and detect repeated failures on the same tool with the same inputs. After two identical failures, inject a system-level message: "The `search_orders` tool has failed twice with this query. Consider a different approach or inform the user the information is unavailable."

## Observability You Cannot Skip

You need three things instrumented before going to production:

```python
# snippet-6
import structlog
from opentelemetry import trace

tracer = trace.get_tracer("llm-agent")
log = structlog.get_logger()

def dispatch_tool_instrumented(tool_name: str, raw_input: dict, session_id: str) -> dict:
    with tracer.start_as_current_span(f"tool.{tool_name}") as span:
        span.set_attribute("tool.name", tool_name)
        span.set_attribute("session.id", session_id)
        span.set_attribute("input.keys", ",".join(raw_input.keys()))

        result = dispatch_tool(tool_name, raw_input)

        span.set_attribute("result.success", result.get("success", False))
        if not result.get("success"):
            span.set_attribute("result.error_code", result.get("error_code", "UNKNOWN"))
            span.set_status(trace.StatusCode.ERROR)

        log.info(
            "tool_dispatched",
            tool=tool_name,
            session_id=session_id,
            success=result.get("success"),
            error_code=result.get("error_code")
        )

        return result
```

The three metrics that matter: **tool call success rate by tool name** (tells you which tools are unreliable), **tool calls per session** (high values indicate agents getting stuck), and **session completion rate** (did the agent actually finish its task). Track these in Datadog, Grafana, whatever you use — but track them from day one. You will not understand what is broken until you can see the pattern.

## Tool Design Principles

After shipping agents across multiple products, a few rules that hold consistently:

**One tool, one responsibility.** Do not build `get_or_create_customer` — build `get_customer` and `create_customer` separately. Models make better decisions when tools are atomic.

**Side effects last.** Order your tools so read operations are cheap and reversible actions (writes, emails, payments) require explicit model intent. Add a `confirm: boolean` parameter to destructive tools and only execute when `confirm: true`.

**Describe failure modes in the schema.** If `get_inventory` can return a 404, say so: "Returns item details or an error object with `not_found: true` if the SKU does not exist." Models handle known failure modes far better than unexpected ones.

**Keep tool count under 20.** Above 20 tools, model reliability degrades noticeably. If your agent needs 30 tools, you likely need two specialized agents, not one general agent.

Tool use is the mechanism that makes LLMs useful in production systems rather than just impressive in demos. The engineering is mostly boring — schemas, dispatch, validation, error handling, observability. That is the point. The model handles the non-deterministic reasoning; you handle the deterministic execution. Keep those responsibilities separate and your on-call rotation will thank you.
```