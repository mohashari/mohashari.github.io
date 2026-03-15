---
layout: post
title: "Serverless on AWS Lambda: Cold Starts, Concurrency, and Production Patterns"
date: 2026-04-13 07:00:00 +0700
tags: [serverless, aws, lambda, performance, cloud]
description: "Optimize AWS Lambda functions for cold start latency, manage concurrency limits, and apply production-grade patterns for event-driven workloads."
---

You've shipped your service to Lambda, the dashboards look clean, and then a Monday morning spike hits — users report 3–5 second delays on the first request, your concurrency limit surfaces as a throttling error, and a downstream DynamoDB call that worked fine in staging starts timing out under load. Serverless abstracts away servers, but it doesn't abstract away distributed systems complexity. To run Lambda reliably in production, you need to internalize how the runtime lifecycle works, where latency actually comes from, and how to design around the platform's hard edges rather than discovering them at 2 AM.

## Understanding the Cold Start Problem

When Lambda receives an invocation and no warm execution environment exists, the platform must provision a microVM, download your deployment package, start the language runtime, and run your initialization code before your handler even executes. This entire sequence — the cold start — typically runs 200ms–2s depending on runtime, package size, and VPC configuration. Go and compiled runtimes are fastest. Java and .NET with large classpaths are slowest. The key insight is that everything outside your handler function runs during init, so that's where you pay the cold start tax once, not on every invocation.

Structure your Lambda handler to move all expensive initialization — SDK clients, database connection pools, config loading — to the package-level scope so they're reused across warm invocations.

<script src="https://gist.github.com/mohashari/19eed20ab6939e7a751950b382ca2dd0.js?file=snippet.go"></script>

## Provisioned Concurrency for Latency-Sensitive Paths

For APIs where cold starts are unacceptable — auth flows, payment processing, real-time features — AWS Provisioned Concurrency pre-warms a fixed number of execution environments that stay initialized and ready. You configure it on a function alias or version, not on `$LATEST`, which means you should always deploy via versioned aliases in production.

<script src="https://gist.github.com/mohashari/19eed20ab6939e7a751950b382ca2dd0.js?file=snippet-2.yaml"></script>

Provisioned Concurrency is not free — you pay per GB-second for reserved environments regardless of invocations. A practical strategy is to combine it with Application Auto Scaling to scale provisioned concurrency up ahead of predicted traffic and down during off-peak hours, keeping costs proportional to actual demand.

## Concurrency Limits and Throttle Handling

Lambda concurrency is regional. Your account has a default limit of 1,000 concurrent executions across all functions, and a single runaway function can starve everything else. Use reserved concurrency to both guarantee capacity for critical functions and cap noisy neighbors. Setting reserved concurrency to zero is also a valid circuit-breaker pattern for non-critical async workloads.

<script src="https://gist.github.com/mohashari/19eed20ab6939e7a751950b382ca2dd0.js?file=snippet-3.sh"></script>

When Lambda throttles, it returns a 429 `TooManyRequestsException`. Synchronous callers (API Gateway, ALB) surface this as a 502 or 429 to the client. Asynchronous invocations and event source mappings handle throttles differently — SQS will back off and retry; Kinesis will block the shard. Design your event consumers to be idempotent so retries don't corrupt state.

## Idempotent Event Processing

Lambda's async invocation model guarantees at-least-once delivery. Your SQS consumer, SNS handler, or EventBridge rule may process the same event twice — during a retry after a function crash, a network hiccup, or a Lambda scaling event. The correct defense is idempotency at the operation level, using a deduplication key stored in DynamoDB with a conditional write.

<script src="https://gist.github.com/mohashari/19eed20ab6939e7a751950b382ca2dd0.js?file=snippet-4.go"></script>

## Structured Logging for Observability

Lambda's execution model makes tracing harder than traditional services — you have no persistent process to attach a profiler to, and logs from concurrent invocations interleave in CloudWatch. Structured JSON logging with the request ID and correlation ID on every line makes log queries in CloudWatch Insights or your log aggregator dramatically faster.

<script src="https://gist.github.com/mohashari/19eed20ab6939e7a751950b382ca2dd0.js?file=snippet-5.go"></script>

## Deployment Package Optimization

Binary size directly affects cold start duration because Lambda must download and extract your deployment package. For Go, compile with `CGO_ENABLED=0` and strip debug symbols. Ship only the bootstrap binary, not the full module cache.

<script src="https://gist.github.com/mohashari/19eed20ab6939e7a751950b382ca2dd0.js?file=snippet-6.dockerfile"></script>

Using Lambda container images (up to 10 GB) lets you sidestep the 250 MB unzipped package limit, but cold starts for large images are slower. For most Go services, a zip deployment under 10 MB via a two-stage Docker build hits the sweet spot of fast cold starts and straightforward CI/CD.

## Dead Letter Queues and Error Isolation

For asynchronous Lambda invocations, unhandled errors retry twice by default, then the event is silently discarded. In production, you always want a Dead Letter Queue to capture failed events for inspection and replay, configured alongside a maximum event age to prevent stale events from poisoning a recovered function.

<script src="https://gist.github.com/mohashari/19eed20ab6939e7a751950b382ca2dd0.js?file=snippet-7.sh"></script>

AWS Lambda rewards engineers who treat it as a distributed system primitive rather than a simple function host. The platform's operational model — ephemeral execution environments, at-least-once delivery, regional concurrency pools — demands deliberate design at every layer. Move initialization outside your handler, pin critical functions with provisioned concurrency or reserved concurrency, build for idempotency from day one, emit structured logs with request IDs, and always configure a DLQ before your async functions touch production traffic. These patterns won't eliminate operational surprises, but they'll ensure that when something breaks, you have the visibility and resilience to recover quickly without burning a weekend.