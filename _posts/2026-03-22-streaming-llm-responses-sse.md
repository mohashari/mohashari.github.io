---
layout: post
title: "Streaming LLM Responses with SSE: Real-Time UX Without Complexity"
date: 2026-03-22 08:00:00 +0700
tags: [ai-engineering, streaming, sse, go, llm]
description: "Build production-ready LLM token streaming with SSE: backpressure, error handling, and connection lifecycle without WebSockets or extra libraries."
---

Your LLM endpoint takes 8 seconds to return a response. Users are staring at a spinner. Some give up and refresh. Some open tickets. Your P95 latency graph looks like a cliff face. You know the model is streaming tokens almost immediately — both the Anthropic and OpenAI APIs start emitting within 200-400ms of receiving a request — but your architecture is buffering the entire response before sending it downstream, because that's how your HTTP handler was written. The fix is obvious in retrospect: stream tokens as they arrive. The question is how to do it without bolting WebSockets, a pub/sub layer, or a dedicated streaming library onto your stack.

## Why SSE, Not WebSockets

WebSockets are overkill for LLM streaming. You have a unidirectional data flow — the server sends tokens, the client displays them. WebSockets buy you bidirectional communication you don't need, and they cost you simplicity: a separate protocol upgrade handshake, different load balancer configuration, and stateful connection management that SSE gets for free via HTTP semantics.

SSE is just HTTP. One request, one long-lived response body, events delimited by `\n\n`. Your existing Nginx/ALB/Cloudflare setup handles it without modification. Your existing auth middleware handles it. Your existing distributed tracing handles it. The entire streaming pipeline is a regular HTTP response where you flush chunks as they arrive.

The one real WebSocket advantage — reconnection with message replay — SSE gives you via the `Last-Event-ID` header and `retry:` directive.

## The Wire Format

Internalize the format before touching code. An SSE response looks like this:

```
HTTP/1.1 200 OK
Content-Type: text/event-stream
Cache-Control: no-cache
Connection: keep-alive

data: {"token":"Hello"}\n\n
data: {"token":" world"}\n\n
data: [DONE]\n\n
```

Each event is `data: <payload>\n\n`. The double newline is the event separator. You can add `id:` for reconnection sequencing and `event:` for named event types, but for basic LLM token streaming, `data:` is sufficient. The browser's `EventSource` API handles framing automatically. Your server just needs to write and flush.

## The SSE Handler

Here's a production-honest Go handler that streams directly from the Anthropic API to the client:

<script src="https://gist.github.com/mohashari/f92b28d6eee5edbc0c2c8090e7bbeb3c.js?file=snippet-1.go"></script>

Two things worth calling out. First, `X-Accel-Buffering: no` — without this, Nginx will buffer your entire response before forwarding it, defeating streaming entirely. This is the single most common failure mode in production SSE deployments; engineers spend hours debugging why streaming works in local dev but not behind their proxy. Second, the explicit context check on error: when a user navigates away, the browser closes the TCP connection. Your stream errors. That's not a bug — don't log it as one.

## Backpressure: The Part Everyone Skips

`http.Flusher.Flush()` pushes bytes into the kernel send buffer and returns immediately — it doesn't block until the client has consumed the data. If your client is slow (mobile on 3G, overloaded JavaScript main thread) and you're calling `Flush()` on every token at 50 tokens/second, you can outrun the TCP send buffer and create head-of-line blocking.

The practical fix is a buffered channel between your producer and the HTTP writer, with non-blocking sends from the producer side. Drop events when the buffer is full rather than blocking the stream goroutine:

<script src="https://gist.github.com/mohashari/f92b28d6eee5edbc0c2c8090e7bbeb3c.js?file=snippet-2.go"></script>

At typical LLM token rates (20–80 tokens/sec), a buffer of 32–64 events absorbs most burst variance. Log `s.Dropped()` at stream completion — a non-zero drop rate on desktop connections indicates a rendering bottleneck on the client side, not a network issue.

## Client-Side: fetch Over EventSource

`EventSource` only supports GET requests. For LLM endpoints where you need to send a JSON body with messages and model parameters, you need `fetch` with `ReadableStream`. This also gives you `AbortController` for user-initiated stop:

<script src="https://gist.github.com/mohashari/f92b28d6eee5edbc0c2c8090e7bbeb3c.js?file=snippet-3.js"></script>

The `AbortController` signal cancels the fetch, which closes the TCP connection. On the Go side, `r.Context()` is cancelled immediately — the upstream Anthropic or OpenAI SDK call terminates, stopping token consumption. This matters for cost: a generation you abort at token 200 out of 2000 should cost 200 tokens, not 2000.

## Error Handling Inside an Active Stream

Once you've flushed the first byte, you've committed to a 200 response. You cannot send `HTTP 500` if something goes wrong 3 seconds into a 10-second generation. Your error signaling has to go through the event stream itself:

<script src="https://gist.github.com/mohashari/f92b28d6eee5edbc0c2c8090e7bbeb3c.js?file=snippet-4.go"></script>

Define your error vocabulary upfront and document it. The client side needs `rate_limited` to trigger exponential backoff retry. It needs `context_length_exceeded` to prompt the user to start a new conversation. `internal_error` should surface a visible error state. If your error codes are undocumented strings, every frontend engineer who integrates with your endpoint will write a different error handler — or none at all.

## Nginx and Load Balancer Configuration

Two failure modes kill SSE in production infrastructure: buffering and proxy timeouts.

```nginx
# snippet-5
location /api/chat {
    proxy_pass http://backend;
    proxy_http_version 1.1;

    # Disable buffering — without this, Nginx holds the full response
    proxy_buffering       off;
    proxy_cache           off;

    # Long generations can exceed default 60s proxy timeouts
    proxy_read_timeout    300s;
    proxy_send_timeout    300s;
    proxy_connect_timeout 10s;

    # Required for HTTP/1.1 keepalive upstream connections
    proxy_set_header Connection '';

    # Preserve client IP through proxy
    proxy_set_header X-Real-IP        $remote_addr;
    proxy_set_header X-Forwarded-For  $proxy_add_x_forwarded_for;
    proxy_set_header Host             $host;
}
```

For AWS ALB: set the idle timeout to at least 300 seconds (default is 60). The idle timeout fires when no bytes are transferred in either direction — if your model is "thinking" for 90 seconds before the first token, ALB will silently close the connection and your client sees an empty response with no error. For GCP Cloud Load Balancing, the equivalent is the backend service timeout, also defaulting to 30 seconds.

## Observability That Doesn't Mislead You

Standard request latency metrics lie about streaming. A P95 of "4.2 seconds" means nothing when 200ms of that is time-to-first-token and 4 seconds is generation time. Track these separately:

<script src="https://gist.github.com/mohashari/f92b28d6eee5edbc0c2c8090e7bbeb3c.js?file=snippet-6.go"></script>

The `client_disconnect` / `upstream_error` split is particularly important for alerting. A 30% client disconnection rate is a product signal — users aren't waiting long enough, which means your TTFT is too slow or you're not showing partial responses fast enough. A 5% upstream error rate is an infrastructure problem. They look identical in a generic error rate dashboard but require completely different responses.

## Connection Lifecycle Summary

The full lifecycle for a production SSE stream:

1. Client sends POST with JSON body
2. Server validates, sets SSE headers, writes `200 OK`, flushes
3. Server opens upstream streaming API call with request context
4. Server reads token deltas, writes `data:` events, calls `Flush()` after each
5. On `[DONE]` from upstream: write `data: [DONE]\n\n`, flush, log metrics, return
6. On upstream error: classify error, write `data: {"error":"..."}`, flush, return
7. On context cancellation (`r.Context().Err() != nil`): client disconnected, return silently
8. On user abort (client closes connection): same as context cancellation

Steps 6 and 7 are where most implementations break. Conflating them generates noisy error logs and incorrect alerts. Skipping step 8 on the server side leaves upstream API calls running and billing accumulating after the user has already navigated away.

SSE has no moving parts beyond a flushing HTTP handler. The engineers who get burned are almost always burned by Nginx buffering, a 60-second proxy timeout, or logging client disconnects as upstream errors. Instrument those three failure modes explicitly and you have a streaming architecture that requires no new dependencies, no protocol upgrades, and no infrastructure changes to deploy.
```