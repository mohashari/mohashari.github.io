---
layout: post
title: "Building Real-Time Agentic Workflows with LangGraph and WebSockets"
date: 2026-06-14 08:00:00 +0700
tags: [ai-engineering, langgraph, websockets, python, fastapi]
description: "Design and implement production-grade, real-time agentic workflows using LangGraph and WebSockets for streaming graph execution, tokens, and human-in-the-loop approvals."
image: "https://picsum.photos/1080/720?random=5164"
thumbnail: "https://picsum.photos/400/300?random=5164"
---

In high-throughput enterprise systems, deploying autonomous multi-agent systems via traditional REST APIs is a recipe for operational failure. When an LLM agent orchestrates complex multi-step reasoning—running search tools, generating code, writing SQL queries, and cross-checking facts—the total request duration routinely scales from 15 seconds to over 2 minutes. Forcing frontend applications to poll unary HTTP endpoints or hold open HTTP connections invites gateway timeouts (like Cloudflare's 100-second edge timeout), database connection pool exhaustion, and a sluggish, opaque user experience where users stare at generic loading spinners. To make multi-agent orchestration usable at scale, you must build stateful, bi-directional communication channels. Combining LangGraph’s cyclic graph engine with FastAPI WebSockets allows you to stream LLM generation tokens in real-time, broadcast graph state transitions (telling the user exactly which node is executing), and handle interactive human-in-the-loop (HITL) manual interventions without losing the thread's execution context.

![Building Real-Time Agentic Workflows with LangGraph and WebSockets Diagram](/images/diagrams/building-real-time-agentic-workflows-langgraph-websockets.svg)

## The Production Bottleneck: Why REST Fails for Agentic Workflows

Traditional web service architectures are stateless. An API client issues an HTTP POST request, the server executes business logic (typically hitting database indexes or calling down-stream microservices in sub-100ms), and returns an HTTP 200 response. This paradigm collapses under the computational footprint of LLM agents. 

Consider a typical customer-facing customer service agent running on LangGraph. The request flows through an initial router, performs a vector database lookup, calls an LLM to formulate a plan, executes 3 separate SQL database queries, reflects on the results, synthesizes a response, and formats the output. If the LLM generates a total of 1,000 output tokens across these steps and calls external API resources, the entire run will take 15 to 45 seconds.

If you attempt to run this over standard HTTP REST endpoints, you will hit three immediate scalability walls:
1. **Gateway and Reverse Proxy Timeouts:** Most cloud architectures rely on ingress controllers, load balancers, or CDN proxies (e.g., Nginx, AWS ALB, Cloudflare) that enforce request timeouts. Cloudflare defaults to a 100-second edge timeout. AWS Application Load Balancers default to 60 seconds. An agent graph that hits a deep reasoning cycle or a slow downstream API will trigger a 504 Gateway Timeout, disconnecting the client while the agent worker continues to waste billing cycles in the background.
2. **Database Connection Pool Exhaustion:** When a web server processes an HTTP request, it often reserves a connection from the database connection pool (e.g., PostgreSQL). If that request blocks for 45 seconds while waiting for an external LLM call to finish, that database connection remains locked and idle. Under a moderate load of 100 concurrent users, the application's connection pool will be entirely exhausted, causing incoming simple queries (like user authentication) to queue and fail, dropping the overall service throughput to near zero.
3. **Zero Visibility into Graph Execution:** Users do not tolerate blank screens. If a user has to wait 30 seconds without feedback, they will reload the page, initiating duplicate, expensive agent executions. You must show the graph's progress: *Planning* -> *Searching Database* -> *Writing Draft* -> *Refining Response*.

While Server-Sent Events (SSE) solve the one-way streaming problem by allowing the server to push tokens to the client over an HTTP stream, they fail when the agent workflow requires bidirectional communication. Modern AI workflows heavily rely on **Human-in-the-Loop (HITL)** interaction. For example, if the agent decides it needs to execute a database write command or purchase an item, safety guardrails demand that the agent halt execution, prompt the user for explicit approval, and resume only when the user submits their decision. WebSockets provide the ideal persistent, full-duplex TCP tunnel to stream events out and pipe approval payloads back in.

## Defining the LangGraph Stateful Workflow

LangGraph builds upon the concept of a stateful graph where each node represents a step in the agent workflow, and the state is carried across executions. In a production system, this graph must support compilation-level interrupts to pause execution before critical nodes.

Below is a Python implementation of a LangGraph agent that processes user requests, writes a draft execution plan, interrupts for human approval if the request is deemed sensitive (e.g., modifying database records), and executes the plan:

<script src="https://gist.github.com/mohashari/0ba6aa338afeae75da0bb5390e70abc7.js?file=snippet-1.py"></script>

To compile this workflow, we bind it to a checkpointer. The checkpointer is responsible for persisting the state database at every step of the graph, enabling recovery, step-by-step history, and human interrupts:

<script src="https://gist.github.com/mohashari/0ba6aa338afeae75da0bb5390e70abc7.js?file=snippet-2.py"></script>

## Wrapping LangGraph `astream_events` for Real-Time Streaming

LangGraph provides a granular streaming API called `astream_events` which yields events from all steps in the graph, including nested LLM calls, tool execution starts, and token emissions. For real-time user interfaces, parsing this raw output stream is mandatory; otherwise, the client receives only coarse-grained state updates.

We can construct an async generator that consumes the `astream_events` stream, extracts token increments and node execution events, and formats them into a clean JSON API contract:

<script src="https://gist.github.com/mohashari/0ba6aa338afeae75da0bb5390e70abc7.js?file=snippet-3.py"></script>

## Decoupled Event Routing with Redis Pub/Sub

In a production environment, running long-running LangGraph agents directly inside the FastAPI WebSocket process thread is an architectural anti-pattern. If the WebSocket server restarts or experiences load spikes, active agent executions are lost or blocked. 

To scale horizontally, you must decouple connection management from graph execution. The WebSocket server should act as a lightweight, state-free routing gateway. When a graph executes, it does so on a separate worker tier or in a detached background task, publishing its progress to a Redis Pub/Sub channel dedicated to that specific session ID. The WebSocket server subscribes to the Redis channel and forwards messages down the active TCP connection.

Here is the implementation of a `RedisSessionRouter` that abstracts this communication pattern:

<script src="https://gist.github.com/mohashari/0ba6aa338afeae75da0bb5390e70abc7.js?file=snippet-4.py"></script>

## Implementing the FastAPI WebSocket Gateway

With the message routing layer defined, we can implement the FastAPI WebSocket endpoint. This endpoint accepts client connections, authenticates them, initializes a local memory queue, subscribes to the Redis Pub/Sub topic, and manages bidirectional message pumps:

<script src="https://gist.github.com/mohashari/0ba6aa338afeae75da0bb5390e70abc7.js?file=snippet-5.py"></script>

## Bidirectional Human-in-the-Loop Resumption

When LangGraph halts execution at `interrupt_before=["human_approval_node"]`, the execution loop terminates, saving the exact state snapshot to Postgres. To resume this graph:
1. The client must transmit an approval decision (e.g., `{"action": "approve", "approved": true}`).
2. The server must invoke `aupdate_state` using the same `thread_id` to override state values. To avoid context issues, we run this update as if it were the `human_approval_node` itself.
3. The server restarts the execution engine by calling `astream_events` with the input parameter set to `None`. This tells LangGraph to fetch the latest checkpoint and proceed directly.

<script src="https://gist.github.com/mohashari/0ba6aa338afeae75da0bb5390e70abc7.js?file=snippet-6.py"></script>

## Durable Checkpointing: SQL Schema for Postgres Checkpointer

While memory checkpointers are convenient for local development, production requires a durable relational store. LangGraph's `AsyncPostgresSaver` uses a normalized table design to write graph configurations, checkpoint blobs, and write records. 

Below is the database schema that must be applied to your PostgreSQL instances to support concurrent checkpointer writes and trace lookups:

<script src="https://gist.github.com/mohashari/0ba6aa338afeae75da0bb5390e70abc7.js?file=snippet-7.sql"></script>

## Production Deployment: Nginx Configuration for Scaling WebSockets

When deploying your FastAPI WebSocket servers behind a reverse proxy like Nginx, default timeouts will continuously sever active agent runs. WebSockets require explicit hop-by-hop headers to prevent Nginx from dropping the HTTP handshake, along with long read/send timeouts to preserve idle connections during deep reasoning steps.

<script src="https://gist.github.com/mohashari/0ba6aa338afeae75da0bb5390e70abc7.js?file=snippet-8.txt"></script>

## Operational Pitfalls and Mitigation Strategies

Deploying real-time agents into production exposes several operational landmines that will degrade system performance if not proactively managed.

### 1. Connection Drops and Session Resumption
Mobile clients frequently experience dropouts when switching networks. If a client drops their connection midway through a 45-second execution, the WebSocket closes, but the background agent execution task continues running on your worker tier. 

If the client reconnects 2 seconds later, they receive a fresh WebSocket connection. If you simply spawn a new graph execution, you trigger double execution costs and lose the previous generation's history.
* **Resolution:** Never execute graph runs directly tied to the websocket session loop. The agent must stream all event outputs to a **Redis Stream** using `XADD` under the key `stream:session_id` with a retention limit of 100 events. When the client's WebSocket drops and reconnects, they must transmit their last received message ID. The WebSocket gateway then queries the Redis Stream from that sequence number using `XRANGE` to catch the client up without restarting the agent graph.

### 2. Checkpoint Database Growth
Every step in LangGraph writes a complete checkpoint state (including messages, planning states, and tool results) to the `checkpoints` table. An agent session with 30 message exchanges can easily generate 150 database records, with serialized state blobs totaling 5MB to 20MB. With 100,000 active sessions, your PostgreSQL database will rapidly accumulate gigabytes of historical checkpoint binaries.
* **Resolution:** Implement a data-retention routine. Since historical steps are only needed for debugging or state backtracking, compile a background cron worker that executes a purging query daily. The worker should delete rows in `checkpoints` where `created_at` is older than 7 days, or prune old checkpoints for a thread while preserving only the latest parent state checkpoint.

### 3. Client Backpressure and Buffer Overflow
If an LLM streams tokens at 120 tokens per second, and a client is connected over a throttled cellular network, the server’s output buffer begins filling up. If the FastAPI write pump blocks indefinitely, it can lead to memory exhaustion on the server as async task queues swell.
* **Resolution:** Enforce strict write timeouts on your WebSocket connections. Set an upper limit (e.g., 5 seconds) on `websocket.send_json` operations. If a write times out, close the connection immediately and rely on the client’s reconnection and session resumption logic to catch up once bandwidth stabilizes.