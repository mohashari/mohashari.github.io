---
layout: post
title: "NATS JetStream: Lightweight Persistent Messaging for Cloud-Native Systems"
date: 2026-03-16 07:00:00 +0700
tags: [messaging, nats, distributed-systems, backend, devops]
description: "Explore NATS JetStream as a lightweight alternative to Kafka for durable pub-sub, work queues, and key-value storage in cloud-native architectures."
---

When Kafka enters the room, it brings its entire entourage: ZooKeeper (or KRaft), schema registries, complex partition strategies, and a JVM runtime that hungers for memory. For many backend teams, this is the right trade-off — Kafka's throughput and ecosystem are unmatched at scale. But for a growing class of cloud-native services that need durable messaging, ordered delivery, and consumer group semantics without operating a small data center, NATS JetStream offers a compelling alternative. It runs as a single binary under 20MB, survives restarts with full message persistence, and speaks a protocol so lean that a client handshake fits in a UDP packet. This post walks through JetStream's core primitives with working code so you can evaluate it against your own operational appetite.

## What JetStream Adds to Core NATS

Vanilla NATS is a fire-and-forget pub-sub broker. Messages are delivered to active subscribers or dropped — there is no buffering, no replay, no acknowledgment. JetStream layers a persistence engine on top of that model. Streams capture published messages to disk (or memory) according to retention policies you define. Consumers are named cursors into those streams; they track which messages have been acknowledged and re-deliver unacknowledged ones after a configurable ack-wait timeout. The mental model maps cleanly onto Kafka topics and consumer groups, but configuration is lighter and the operational surface is smaller.

## Standing Up a JetStream-Enabled Server

The simplest production-ready local setup uses Docker with a config file that enables JetStream and sets a storage directory.

<script src="https://gist.github.com/mohashari/79cce04e70aff94cfafef806ee141ea2.js?file=snippet.yaml"></script>

<script src="https://gist.github.com/mohashari/79cce04e70aff94cfafef806ee141ea2.js?file=snippet-2.dockerfile"></script>

Mount a volume at `/data/nats` and messages survive container restarts. The HTTP port exposes a `/healthz` endpoint and a `/jsz` JSON endpoint that reports stream and consumer statistics — useful for health checks and dashboards without a separate monitoring agent.

## Creating Streams Programmatically

Streams are the durable buckets that hold messages. In Go, the official `nats.go` client exposes JetStream through a context you derive from the connection. Stream creation is idempotent — calling `AddStream` with the same name and identical configuration is a no-op, which makes it safe to run on every service startup.

<script src="https://gist.github.com/mohashari/79cce04e70aff94cfafef806ee141ea2.js?file=snippet-3.go"></script>

The subject wildcard `orders.>` captures any multi-token subject starting with `orders.` — so `orders.placed`, `orders.fulfilled`, and `orders.cancelled` all land in the same stream and share its retention limits. This is a key design lever: one stream can multiplex many logical event types, and consumers can filter to a specific subject token without separate topics.

## Publishing with Acknowledgment

Unlike core NATS publish, `js.Publish` is synchronous and returns a `PubAck` confirming the broker persisted the message. Use this wherever at-least-once delivery matters.

<script src="https://gist.github.com/mohashari/79cce04e70aff94cfafef806ee141ea2.js?file=snippet-4.go"></script>

The `nats.MsgId` option enables server-side deduplication within a configurable window (default 2 minutes). If your producer retries on network error and sends the same `orderID` twice, JetStream silently discards the duplicate and returns the original `PubAck`. This gives you idempotent producers without any application-level deduplication logic.

## Pull Consumers for Work Queues

Push consumers deliver messages as fast as the server can send them, which suits high-throughput pipelines. Pull consumers let workers request messages at their own pace — a better fit for CPU-bound jobs or when worker count fluctuates with autoscaling.

<script src="https://gist.github.com/mohashari/79cce04e70aff94cfafef806ee141ea2.js?file=snippet-5.go"></script>

Each `Fetch` call is a batch request. Unacknowledged messages reappear after the stream's `AckWait` expires, so a crashed worker does not lose work. Calling `msg.Nak()` explicitly triggers immediate redelivery rather than waiting for the timeout — useful when you detect a transient error and want fast retry.

## Key-Value Storage as a Config Registry

JetStream exposes a key-value API backed by a stream under the hood. This makes it useful as a lightweight distributed configuration store — readable by any service, updated atomically, with full revision history.

<script src="https://gist.github.com/mohashari/79cce04e70aff94cfafef806ee141ea2.js?file=snippet-6.go"></script>

The watcher pattern lets services reload configuration at runtime without polling or a separate config service. Combine this with Go's `sync/atomic` to swap config values without locks.

## Monitoring Stream Health

The JetStream HTTP endpoint is machine-readable. A quick shell snippet to pull consumer lag — the gap between last published sequence and last consumer acknowledgment — is useful in CI health checks or Prometheus textfile collectors.

<script src="https://gist.github.com/mohashari/79cce04e70aff94cfafef806ee141ea2.js?file=snippet-7.sh"></script>

`num_pending` is the most actionable metric: it tells you how many messages are queued but not yet delivered to any consumer. A steadily growing lag indicates your workers cannot keep up and you need to scale horizontally or increase the `Fetch` batch size.

JetStream will not replace Kafka for teams already invested in the Kafka ecosystem, running petabyte-scale pipelines, or needing Kafka Streams' stateful processing. But for teams standing up new services — especially in Kubernetes environments where operational simplicity compounds quickly — JetStream offers durable messaging, consumer groups, key-value storage, and even object storage in a single lightweight binary. The primitives covered here (streams, pull consumers, KV watchers) handle the majority of real-world messaging patterns without a dedicated messaging team to run them. Start with a single-node deployment, add replicas when your availability requirements sharpen, and evaluate whether the added complexity of a heavier broker is actually warranted for your workload.