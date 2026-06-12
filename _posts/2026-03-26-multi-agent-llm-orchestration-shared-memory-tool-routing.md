---
layout: post
title: "Multi-Agent LLM Orchestration with Shared Memory and Tool Routing"
date: 2026-03-26 08:00:00 +0700
tags: [ai-engineering, llm, distributed-systems, python, architecture]
description: "How to build production-grade multi-agent LLM systems with shared memory, deterministic tool routing, and failure isolation."
image: ""
thumbnail: ""
---

Single-agent LLM pipelines hit a wall somewhere around 15-20 tool calls per request. The context window bloats, the model starts hallucinating tool arguments, and latency climbs past anything a user will tolerate. The real problem isn't the model—it's the architecture. Cramming search, code execution, data retrieval, and synthesis into one context means every capability competes for attention, and the model loses track of what it was supposed to do three steps ago. Multi-agent systems with shared memory and explicit tool routing are the production answer to this, and building them correctly requires a different mental model than chaining LLM calls together.

![Multi-Agent LLM Orchestration with Shared Memory and Tool Routing Diagram](/images/diagrams/multi-agent-llm-orchestration-shared-memory-tool-routing.svg)

## The Core Problem With Monolithic Agents

A single agent handling everything is appealing because it's simple. One prompt, one context window, one model call. It works fine in demos. In production, it degrades in three predictable ways.

First, **context pollution**. Tool results accumulate in the context. A web search returns 4,000 tokens of HTML fragments. A SQL result dumps 200 rows. By the time the model needs to synthesize an answer, 70% of its context is intermediate noise it should have discarded 5 steps back. You end up manually truncating results and hoping nothing important got cut.

Second, **no parallelism**. If answering a question requires both a database lookup and a web search, a monolithic agent does them sequentially. Each LLM call decides the next tool, waits for the result, then decides again. For workflows that genuinely have independent branches, you're paying 2-4x the latency you need to.

Third, **failure blast radius**. One tool failing—a flaky external API, a timeout—can corrupt the entire agent state. There's no clean boundary between what succeeded and what failed, so recovery means restarting from scratch.

Multi-agent systems address all three by assigning responsibilities: a small orchestrator decides what needs doing and in what order, specialized agents each maintain a focused context, and shared memory provides the communication layer that lets them coordinate without coupling.

## Orchestrator Design: Stateless and Narrow

The orchestrator's job is task decomposition and delegation—nothing else. It should not hold tool results, should not attempt synthesis, and should not have direct access to external APIs. Keep its system prompt under 500 tokens. Its only inputs are the user request and the current state of shared memory. Its only output is a structured dispatch plan.

```python
# snippet-1
from pydantic import BaseModel
from enum import Enum
from typing import Optional

class AgentType(str, Enum):
    SEARCH = "search"
    CODE = "code"
    DATA = "data"
    SUMMARIZER = "summarizer"

class TaskDispatch(BaseModel):
    task_id: str
    agent: AgentType
    instruction: str
    depends_on: list[str] = []
    priority: int = 0
    timeout_seconds: int = 30

class OrchestratorPlan(BaseModel):
    plan_id: str
    tasks: list[TaskDispatch]
    final_agent: AgentType = AgentType.SUMMARIZER

ORCHESTRATOR_SYSTEM = """
You are a task decomposition engine. Given a user request, output a JSON plan
mapping each subtask to a specialist agent. Rules:
- Search agent: external information retrieval only
- Code agent: computation, transformation, execution
- Data agent: SQL queries, structured data analysis
- Summarizer agent: synthesis of completed task outputs
Output only valid JSON matching OrchestratorPlan schema. No prose.
"""

async def decompose(request: str, memory_snapshot: dict) -> OrchestratorPlan:
    response = await llm_call(
        system=ORCHESTRATOR_SYSTEM,
        user=f"Request: {request}\nMemory context: {memory_snapshot}",
        response_format=OrchestratorPlan,
    )
    return response
```

The `depends_on` field is critical. It lets the orchestrator express that the summarizer can't run until search and data tasks complete, without hardcoding a sequential loop. Your execution engine reads this DAG and schedules accordingly.

## Tool Routing: Schema-First, Not Prompt-First

The worst pattern I've seen in production agent systems is embedding tool selection logic in the LLM prompt. "If the user asks about code, call the code tool. If they ask about data, call the SQL tool." This fails the moment the request is ambiguous or spans categories—which is constantly.

Tool routing should be deterministic where possible. Map agent types to tool registries at system initialization. Each agent gets a manifest of exactly which tools it can call, with JSON Schema definitions for every argument. The model doesn't decide which tools exist; it only decides how to call the tools its agent type owns.

```python
# snippet-2
from dataclasses import dataclass, field
from typing import Callable, Any
import json

@dataclass
class ToolDefinition:
    name: str
    description: str
    parameters: dict  # JSON Schema
    handler: Callable
    timeout_seconds: int = 10
    max_retries: int = 2

class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, ToolDefinition] = {}
        self._agent_tools: dict[str, list[str]] = {}

    def register(self, agent_type: str, tool: ToolDefinition):
        self._tools[tool.name] = tool
        self._agent_tools.setdefault(agent_type, []).append(tool.name)

    def get_manifest(self, agent_type: str) -> list[dict]:
        """Returns OpenAI-compatible tool manifest for a given agent type."""
        return [
            {
                "type": "function",
                "function": {
                    "name": t,
                    "description": self._tools[t].description,
                    "parameters": self._tools[t].parameters,
                }
            }
            for t in self._agent_tools.get(agent_type, [])
        ]

    async def dispatch(self, tool_name: str, args: dict) -> Any:
        tool = self._tools.get(tool_name)
        if not tool:
            raise ValueError(f"Unknown tool: {tool_name}")
        # Validate args against schema before calling
        validate_json_schema(args, tool.parameters)
        return await tool.handler(**args)


# Wire up at startup — agents never see tools outside their registry
registry = ToolRegistry()
registry.register("search", ToolDefinition(
    name="web_search",
    description="Search the web for current information",
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "maxLength": 200},
            "num_results": {"type": "integer", "default": 5, "maximum": 10}
        },
        "required": ["query"]
    },
    handler=brave_search_handler,
))
registry.register("data", ToolDefinition(
    name="run_sql",
    description="Execute a read-only SQL query against the analytics replica",
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "timeout_ms": {"type": "integer", "default": 5000}
        },
        "required": ["query"]
    },
    handler=postgres_readonly_handler,
    timeout_seconds=15,
))
```

Validating against JSON Schema before calling the handler catches a class of hallucination bugs where the model invents argument names that don't exist. You want this to fail loudly at the boundary, not silently inside your business logic.

## Shared Memory: Two Tiers, One Interface

Agent communication is the hardest part to get right. Shared mutable state in a distributed system is always a footgun, but you actually need two distinct things that get conflated under "memory":

1. **Working context**: task state, intermediate results, which agents have completed. Short-lived. High-write, high-read. Redis is correct here—set a TTL equal to your maximum task timeout (e.g., 5 minutes) and treat it as ephemeral.

2. **Semantic memory**: retrieved facts, document embeddings, reusable knowledge. Longer-lived. pgvector or Qdrant for similarity search. This layer persists across sessions for the same user or project.

```python
# snippet-3
import redis.asyncio as redis
from pgvector.asyncpg import register_vector
import asyncpg
import numpy as np
from datetime import timedelta

class SharedMemory:
    def __init__(self, redis_url: str, pg_dsn: str):
        self._redis = redis.from_url(redis_url, decode_responses=True)
        self._pg_dsn = pg_dsn
        self._pg: asyncpg.Pool | None = None

    async def connect(self):
        self._pg = await asyncpg.create_pool(self._pg_dsn, init=register_vector)

    # --- Working context (Redis) ---

    async def set_task_result(self, plan_id: str, task_id: str, result: dict, ttl: int = 300):
        key = f"plan:{plan_id}:task:{task_id}"
        await self._redis.setex(key, ttl, json.dumps(result))

    async def get_completed_tasks(self, plan_id: str) -> dict[str, dict]:
        pattern = f"plan:{plan_id}:task:*"
        keys = await self._redis.keys(pattern)
        results = {}
        for key in keys:
            task_id = key.split(":")[-1]
            raw = await self._redis.get(key)
            if raw:
                results[task_id] = json.loads(raw)
        return results

    async def mark_plan_complete(self, plan_id: str):
        await self._redis.setex(f"plan:{plan_id}:done", 600, "1")

    # --- Semantic memory (pgvector) ---

    async def upsert_fact(self, session_id: str, content: str, embedding: list[float], metadata: dict):
        async with self._pg.acquire() as conn:
            await conn.execute("""
                INSERT INTO agent_memory (session_id, content, embedding, metadata, created_at)
                VALUES ($1, $2, $3::vector, $4, NOW())
                ON CONFLICT (session_id, content_hash)
                DO UPDATE SET metadata = EXCLUDED.metadata, updated_at = NOW()
            """, session_id, content, embedding, json.dumps(metadata))

    async def retrieve_relevant(self, session_id: str, query_embedding: list[float], top_k: int = 5) -> list[dict]:
        async with self._pg.acquire() as conn:
            rows = await conn.fetch("""
                SELECT content, metadata, 1 - (embedding <=> $2::vector) AS similarity
                FROM agent_memory
                WHERE session_id = $1
                ORDER BY embedding <=> $2::vector
                LIMIT $3
            """, session_id, query_embedding, top_k)
            return [{"content": r["content"], "metadata": json.loads(r["metadata"]), "score": r["similarity"]} for r in rows]
```

One interface, two backends. Agents call `set_task_result` to publish their output and `retrieve_relevant` to pull context before forming their LLM prompt. The orchestrator calls `get_completed_tasks` to know when its DAG is satisfied.

## Parallel Execution With Dependency Tracking

Once you have the DAG from the orchestrator, you need an executor that respects `depends_on` without blocking on tasks that could run concurrently.

```python
# snippet-4
import asyncio
from collections import defaultdict

async def execute_plan(plan: OrchestratorPlan, memory: SharedMemory, registry: ToolRegistry) -> dict:
    completed: set[str] = set()
    results: dict[str, dict] = {}
    pending = {t.task_id: t for t in plan.tasks}
    in_flight: dict[str, asyncio.Task] = {}

    async def run_agent_task(dispatch: TaskDispatch) -> dict:
        agent = get_agent(dispatch.agent, registry)
        # Pull relevant context from semantic memory
        ctx_embedding = await embed(dispatch.instruction)
        context = await memory.retrieve_relevant(plan.plan_id, ctx_embedding)
        result = await agent.run(
            instruction=dispatch.instruction,
            context=context,
            timeout=dispatch.timeout_seconds,
        )
        await memory.set_task_result(plan.plan_id, dispatch.task_id, result)
        return result

    while pending or in_flight:
        # Schedule any task whose deps are satisfied
        for task_id, task in list(pending.items()):
            deps_met = all(d in completed for d in task.depends_on)
            if deps_met and task_id not in in_flight:
                in_flight[task_id] = asyncio.create_task(run_agent_task(task))
                del pending[task_id]

        if not in_flight:
            break  # deadlock guard — remaining tasks have unsatisfiable deps

        # Wait for at least one to finish
        done, _ = await asyncio.wait(in_flight.values(), return_when=asyncio.FIRST_COMPLETED)
        for coro in done:
            task_id = next(k for k, v in in_flight.items() if v is coro)
            try:
                results[task_id] = coro.result()
                completed.add(task_id)
            except Exception as e:
                # Fail the plan if a non-optional task errors
                raise RuntimeError(f"Task {task_id} failed: {e}") from e
            finally:
                del in_flight[task_id]

    return results
```

This gives you maximum parallelism within the DAG constraints. A plan with four independent search tasks runs them concurrently; a summarizer that depends on all four waits exactly as long as the slowest one. In practice, this cuts latency by 40-60% on plans with more than two independent branches.

## Failure Modes You Will Hit in Production

**Context accumulation in summarizer**: The summarizer agent receives outputs from all prior agents. If search returns 3,000 tokens and the data agent returns 2,000 tokens, you've already consumed 5,000 tokens before the summarizer writes a word. Cap task result size at the boundary—truncate to 800 tokens per result before storing in shared memory, and store the full result separately if you need an audit trail.

**Tool timeout propagation**: A flaky external API that hangs for 29 seconds will block your entire plan if you're not careful. Set hard timeouts at the tool level (see `timeout_seconds` in `ToolDefinition`) and at the task level in `asyncio.wait_for`. Return a structured error result instead of raising—let the orchestrator decide whether the failure is fatal.

**Memory key collisions across concurrent plans**: If two requests from the same user overlap in time, `plan:{plan_id}:task:*` keys must be globally unique. Use UUIDs for `plan_id`, not sequential integers. I've seen systems use user IDs as plan IDs and spend two days debugging cross-contaminated results.

**Embedding drift**: If you store embeddings in pgvector and later upgrade your embedding model (e.g., from `text-embedding-3-small` to `text-embedding-3-large`), cosine similarity scores become meaningless across model versions. Tag every row with the model name and dimension count. When you upgrade, backfill or partition.

```python
# snippet-5
# Structured error result — agents return this instead of raising
from pydantic import BaseModel
from typing import Any, Optional
from datetime import datetime

class AgentResult(BaseModel):
    task_id: str
    agent_type: str
    success: bool
    output: Optional[Any] = None
    error: Optional[str] = None
    tokens_used: int = 0
    latency_ms: int = 0
    tool_calls: list[str] = []
    timestamp: datetime = datetime.utcnow()

    @classmethod
    def failure(cls, task_id: str, agent_type: str, error: str) -> "AgentResult":
        return cls(task_id=task_id, agent_type=agent_type, success=False, error=error)


# In your agent run loop:
async def safe_run_agent(dispatch: TaskDispatch, **kwargs) -> AgentResult:
    start = time.monotonic()
    try:
        result = await asyncio.wait_for(
            run_agent_task(dispatch, **kwargs),
            timeout=dispatch.timeout_seconds,
        )
        return AgentResult(
            task_id=dispatch.task_id,
            agent_type=dispatch.agent,
            success=True,
            output=result,
            latency_ms=int((time.monotonic() - start) * 1000),
        )
    except asyncio.TimeoutError:
        return AgentResult.failure(dispatch.task_id, dispatch.agent, "timeout")
    except Exception as e:
        return AgentResult.failure(dispatch.task_id, dispatch.agent, str(e))
```

## Observability: What to Actually Instrument

You cannot debug a multi-agent system from logs alone. The useful signal is structured traces per plan: which agents ran, in what order, what tools each called, how many tokens each consumed, and what the intermediate results looked like.

Emit a span per task dispatch, with `plan_id` and `task_id` as trace attributes. Log the full tool input/output at DEBUG level—this is verbose but essential during incidents. At INFO level, log: agent type, task completion status, token count, latency. Set an alert on `agent_task_failure_rate > 5%` over a 5-minute window; that's your leading indicator for model degradation or tool service problems.

The two metrics that matter most operationally: **plan completion rate** (what fraction of plans finish all required tasks successfully) and **p95 plan latency**. Everything else is diagnostic. Build dashboards for these two first.

```yaml
# snippet-6
# OpenTelemetry semantic conventions for agent traces
# Emit these attributes on every agent task span

span_attributes:
  # Plan-level
  agent.plan_id: "{uuid}"
  agent.plan_task_count: 4

  # Task-level
  agent.task_id: "{uuid}"
  agent.task_type: "search|code|data|summarizer"
  agent.task_depends_on: ["task-id-1", "task-id-2"]

  # LLM call
  llm.model: "claude-sonnet-4-6"
  llm.input_tokens: 1240
  llm.output_tokens: 380
  llm.latency_ms: 1820

  # Tool calls within task
  agent.tool_calls: ["web_search", "web_search"]
  agent.tool_errors: []

  # Result
  agent.success: true
  agent.output_tokens_stored: 320  # truncated before storing to memory
```

## When Not to Use Multi-Agent Systems

Multi-agent adds overhead—more LLM calls, more coordination, more failure surfaces. A request that genuinely needs one tool and produces one output is better served by a single agent. The threshold where multi-agent pays off is roughly: more than three distinct tool categories, or any workflow where two or more branches are genuinely independent. Below that, you're paying the overhead without getting the parallelism or isolation benefits.

The orchestrator design also adds a call that's entirely overhead if the plan is trivial. Consider building a classifier that routes simple requests directly to a single-agent path and only invokes the orchestrator for complex plans. At scale, 60-70% of requests are simple enough to skip orchestration entirely.

The pattern described here is worth the complexity for long-running, multi-step research and analysis workflows where failure isolation, parallelism, and context hygiene are necessary properties. For CRUD-heavy applications with occasional LLM augmentation, simpler is better.

## Putting It Together

The pieces that matter most in production, in order of impact: structured task results instead of free-form text between agents, hard timeouts at every async boundary, TTL on all working memory, and schema validation at every tool invocation boundary. The model is often the least-broken component—the infrastructure around it is where systems fail.

If you're starting from scratch, build the shared memory layer first. Without a clean write/read interface for intermediate results, every other part of the system becomes harder to reason about. Get that right, and the agent and routing logic becomes straightforward composition on top of a reliable foundation.
