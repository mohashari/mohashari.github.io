---
layout: post
title: "Implementing Low-Latency Structured JSON Outputs from LLMs with Finite State Machine Guided Decoding in Go"
date: 2026-07-23 08:00:00 +0700
tags: [ai-engineering, go, llm, json, performance]
description: "Eliminate JSON schema failures in LLM outputs. Learn how to compile Go structs to GBNF grammars, enforce logit-masking, and build a streaming watchdog."
image: "https://picsum.photos/seed/705/1080/720"
thumbnail: "https://picsum.photos/seed/705/400/300"
---

If you run a Go microservice that relies on large language models (LLMs) to drive business logic—such as real-time invoice parsing, automated database query generation, or dynamic API request routing—you have faced the production nightmare of malformed JSON. You write exhaustive system prompts, inject few-shot examples, and enable "JSON Mode" in commercial APIs. Yet, at 150 requests per second (RPS) under high temperature or edge-case user inputs, the model eventually outputs a trailing comma, truncates a block, or prints a verbose conversational preamble like *"Here is your JSON:"*. In a standard JSON-dependent backend, a 3% validation failure rate translates to thousands of failed operations daily, cascading into expensive API retries (adding 1200ms–3000ms of latency per attempt) and elevated API costs.

Rather than attempting to heal malformed JSON *post-hoc* or wasting developer hours on fragile prompt engineering, production-grade AI engineering requires guiding the model's token selection at the inference layer. By framing the JSON schema as a Deterministic Finite Automaton (DFA) or a Finite State Machine (FSM), we can alter the token selection probabilities (logits) at every single decoding step. If the FSM determines that only a closing brace `}` or a comma `,` is valid at character index $x$, the logits of all other tokens in the vocabulary are set to $-\infty$, making them mathematically impossible to sample. 

This post details how to implement this end-to-end guided decoding architecture in Go, including runtime struct-to-grammar compilation, low-level logit masking, streaming FSM validation, and resiliency watchdogs.

## Under the Hood: Logit Masking and Sub-Token Boundaries

Autoregressive LLM decoding generates text token-by-token. At each step $t$, the model generates raw logit values $L_{t, i}$ for every token $v_i$ in its vocabulary $V$. A softmax function converts these logits into a probability distribution from which the next token $T_t$ is sampled:

$$P(T_t = v_i \mid T_{<t}) = \frac{e^{L_{t, i}}}{\sum_{j} e^{L_{t, j}}}$$

In an unguided generation loop, the model determines this distribution based solely on its training weights and context history. To enforce a schema, we introduce a token-level filter. By compiling a schema into a GBNF (GGML Backus-Naur Form) grammar or an FSM, the decoding engine can dynamically construct a logit mask vector $M(s)$ at state $s$:

$$M(s)_i = \begin{cases} 0 & \text{if } v_i \text{ represents a valid transition from } s \\ -\infty & \text{otherwise} \end{cases}$$

This mask is added directly to the logits prior to the softmax calculation:

$$L'_{t, i} = L_{t, i} + M(s)_i$$

This approach guarantees 100% syntactical correctness and significantly reduces latency. The model no longer wastes computation generating verbose preambles or empty whitespace indentation; it only samples the precise structure. In practice, this drops the Time-to-Last-Token (TTLT) by 15% to 30%.

However, implementing this introduces tokenizer alignment challenges. LLM tokenizers do not split text on clean character boundaries. For instance, the JSON key `"provider"` might be tokenized as `["prov", "ider"]` or as a single token `["provider"]`. The guiding FSM must track state transitions on a sub-token byte level, ensuring that partial tokens are evaluated dynamically against prefix rules before being allowed in the vocabulary.

## Compiling Go Structs into GBNF Grammars

To guide the inference engine, we must define the grammar rules. In local inference runtimes (like `llama.cpp` and engines using GGML), context-free grammars are written in GBNF. GBNF specifies rules for strings, numbers, lists, and objects.

To keep our codebase clean and type-safe, we should avoid writing GBNF files by hand. Instead, we can write a Go compiler that inspects a Go struct using reflection and automatically generates the GBNF syntax. The following snippet implements this runtime compiler.

<script src="https://gist.github.com/mohashari/b285436f23a1622d9ea090739425a203.js?file=snippet-1.go"></script>

## Interfacing with the Inference Engine

Once we generate the GBNF grammar, we submit it to our inference server. For local environments, engines like `llama.cpp` expose an HTTP endpoint `/completion` that natively processes a `grammar` parameter. Below is a high-performance HTTP client implemented in Go that leverages connection pooling and optimized transport properties to handle high-concurrency requests.

<script src="https://gist.github.com/mohashari/b285436f23a1622d9ea090739425a203.js?file=snippet-2.go"></script>

## Zero-Allocation Streaming FSM JSON Parser

In production, waiting for a full JSON payload to generate is a major bottleneck. To reduce user-perceived latency (Time-to-First-Token) or process pipelines asynchronously, we stream tokens. 

Standard JSON parsers (such as `encoding/json` or `fastjson`) require a complete JSON buffer before they can decode. If you feed them partial JSON, they fail immediately with parse errors. To process structured outputs as they stream from the LLM, we write a streaming state machine parser. It parses tokens incrementally, emitting key-value pairs the moment they are closed and validated.

<script src="https://gist.github.com/mohashari/b285436f23a1622d9ea090739425a203.js?file=snippet-3.go"></script>

## Low-Level Logit Masking Sampler

To understand how the inference engine filters tokens under the hood, we can build a simulation of the sampling step in Go.

When logits are returned from the raw neural network forward pass, we apply a mask that forces all invalid token IDs (those not matching the allowed FSM state transitions) to $-\infty$ (`-math.MaxFloat32`). After masking, we apply temperature scaling and select the highest probability token using greedy sampling.

<script src="https://gist.github.com/mohashari/b285436f23a1622d9ea090739425a203.js?file=snippet-4.go"></script>

## Resilient Watchdog Middleware

Although guided decoding guarantees syntactical validity, it introduces a new failure mode: **The State Loop Trap**. 

Because invalid tokens are forced to zero probability, if the model loses its semantic direction but is forced by the FSM to continue generating tokens (for instance, if the grammar allows infinite array elements or nested objects and the model fails to output the termination character), it can fall into a repetitive generation loop. The engine gets stuck repeating token patterns indefinitely, generating meaningless data until it reaches the model's hard context limit.

To prevent this, production backends must implement a watchdog wrapper. The wrapper inspects the stream, tracks token generation speed, monitors for repetitive sequences, and falls back to a lower temperature or secondary model if loop behavior is detected.

<script src="https://gist.github.com/mohashari/b285436f23a1622d9ea090739425a203.js?file=snippet-5.go"></script>

## Complete Integration Pipeline

Below is a complete, executable integration file that compiles a schema, sets up the state machine, generates simulated token streams, and parses fields dynamically.

<script src="https://gist.github.com/mohashari/b285436f23a1622d9ea090739425a203.js?file=snippet-6.go"></script>

## Production Operational Trade-offs

Before refactoring your entire backend to enforce GBNF constraints, evaluate these architectural trade-offs:

1. **Tokenizer Dependency**: Grammars are highly sensitive to tokenizer choices. If you swap your underlying model from Llama-3 (which uses a 128k vocabulary) to Mistral (which uses a 32k vocabulary), token boundary matching parameters might shift. Always build integration tests checking edge-case character boundary splits.
2. **Cold Starts and Compilation Overhead**: Converting complex, deeply nested JSON schemas with hundreds of fields to regex-based GBNF rules adds initial parsing overhead. Pre-compile these rules at application startup rather than dynamically evaluating them on each runtime request context.
3. **Hardware & Engine Limits**: Enforcing FSM constraints requires access to logits during generation. This limits you to self-hosted engines (`llama.cpp`, `vLLM`, or Outlines) or commercial endpoints that explicitly expose grammar integration. You cannot use native logit-level masking on APIs that only accept text prompts and return fully formed text responses.

By moving validation logic from post-generation parsing to token-level generation constraints, you can achieve a zero-failure rate for structured output pipelines while lowering latency, reducing network costs, and ensuring reliable integrations in Go-based architectures.