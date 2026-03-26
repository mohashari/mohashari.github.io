---
layout: post
title: "Kafka Consumer Group Rebalancing: Cooperative Sticky Assignor Internals"
date: 2026-03-26 08:00:00 +0700
tags: [kafka, distributed-systems, streaming, java, backend]
description: "How the Cooperative Sticky Assignor eliminates stop-the-world rebalances by incrementally revoking only the partitions that need to move."
image: ""
thumbnail: ""
---

You deploy a new version of your consumer service. A rolling restart begins — pods spin down one at a time. What you expect is a brief lag spike per pod. What you get is a cascade: every restart triggers a full rebalance, every rebalance pauses all consumers for 10–30 seconds while partitions are redistributed, and your consumer lag graph looks like a heartbeat monitor on a bad day...

The post covers:

- **The eager protocol's stop-the-world mechanics** — why revoking all 100 partitions to move 10 is the default behavior and why it hurts
- **Cooperative sticky internals** — the two-phase JoinGroup/SyncGroup protocol, how members keep consuming while revocation is computed
- **The sticky assignment algorithm** — how it minimizes partition movement using current assignment metadata
- **Safe migration path** — listing both assignors during rolling upgrade to avoid mid-migration fallback
- **Failure modes** — stuck rebalances, generation ID fencing, and how cooperative mode limits blast radius
- **Static membership combo** — `group.instance.id` + cooperative sticky for zero-rebalance rolling deploys
- **Monitoring** — JMX metrics and the lag spike pattern that distinguishes cooperative from eager rebalances
- **When NOT to use it** — high consumer churn bursts, heavy exactly-once revocation callbacks

7 code snippets (Java, Python, YAML, Bash) across all the practical configuration and callback patterns. File saved to `_posts/2026-03-26-kafka-consumer-group-rebalancing-cooperative-sticky.md`.