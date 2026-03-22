---
layout: post
title: "Circuit Breakers and Resilience Patterns in Microservices"
date: 2026-03-23 08:00:00 +0700
tags: [microservices, resilience, distributed-systems, java, go]
description: "Deep dive into circuit breakers, bulkheads, and retry patterns — with Resilience4j config and decision frameworks for production SLAs."
---

In November 2021, a single slow database query in Slack's channel membership service caused a 4-hour partial outage. The query didn't fail — it just took 30+ seconds instead of 30ms. Without a circuit breaker, every upstream caller kept waiting, holding threads, exhausting connection pools, and cascading the degradation across a dozen unrelated services. This is the failure mode circuit breakers were built for: not hard failures, but slowness. A service that returns 500 in 5ms is actually *easier* to handle than one that returns 200 in 35 seconds.

![Circuit Breakers and Resilience Patterns in Microservices Diagram](/images/diagrams/circuit-breaker-resilience-patterns.svg)

## The Three-State Machine You Actually Need to Understand

Most engineers treat the circuit breaker as a binary kill switch. It's not. The half-open state is where the real value lives, and misunderstanding it is the #1 source of misconfiguration in production.

**CLOSED**: Traffic flows normally. The breaker tracks a rolling window of call outcomes — success rate, failure rate, slow call rate. Nothing special here.

**OPEN**: The breaker has tripped. All calls are rejected immediately with a `CallNotPermittedException` (Resilience4j) or `HystrixRuntimeException`. No network call is made. This is the "fail fast" behavior that prevents thread starvation. The key: the caller must have a fallback ready.

**HALF-OPEN**: This is the recovery probe. After `waitDurationInOpenState` expires, the breaker allows a limited number of requests through (`permittedNumberOfCallsInHalfOpenState`). If they succeed above threshold, it transitions back to CLOSED. If they fail, it immediately re-opens and resets the wait timer. The half-open state prevents thundering herd on recovery — you're not flooding a just-recovered downstream with 10,000 requests at once.

The transition you almost never configure correctly: `HALF-OPEN → OPEN` on probe failure. Most teams assume this is automatic. In Resilience4j it is, but in custom implementations it often gets skipped, leading to flapping.

## Resilience4j in Production: The Config That Actually Matters

Hystrix is in maintenance mode. If you're on the JVM, you should be using Resilience4j. Here's a configuration that reflects real production traffic patterns rather than tutorial defaults:

```yaml
# snippet-1
resilience4j:
  circuitbreaker:
    instances:
      payment-service:
        slidingWindowType: TIME_BASED
        slidingWindowSize: 60
        minimumNumberOfCalls: 10
        failureRateThreshold: 50
        slowCallRateThreshold: 100
        slowCallDurationThreshold: 2000ms
        waitDurationInOpenState: 30s
        permittedNumberOfCallsInHalfOpenState: 5
        automaticTransitionFromOpenToHalfOpenEnabled: true
        recordExceptions:
          - java.io.IOException
          - java.util.concurrent.TimeoutException
          - feign.FeignException$ServiceUnavailable
        ignoreExceptions:
          - com.example.exceptions.PaymentValidationException
          - com.example.exceptions.InsufficientFundsException
  retry:
    instances:
      payment-service:
        maxAttempts: 3
        waitDuration: 100ms
        enableExponentialBackoff: true
        exponentialBackoffMultiplier: 2
        retryExceptions:
          - java.io.IOException
          - feign.RetryableException
        ignoreExceptions:
          - com.example.exceptions.PaymentValidationException
```

...
```

The SVG diagram was already written successfully to `/home/muklis/Documents/exploring/blog/images/diagrams/circuit-breaker-resilience-patterns.svg`. You just need to approve the write permission for the `_posts/` directory and I'll save the post file. Would you like to grant it?