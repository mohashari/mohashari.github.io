---
layout: post
title: "CRDTs: Conflict-Free Replicated Data Types for Distributed State"
date: 2026-03-16 07:00:00 +0700
tags: [distributed-systems, databases, consistency, algorithms, backend]
description: "Learn how CRDTs enable eventually consistent, merge-friendly shared state across nodes without coordination or conflict resolution logic."
---

Distributed systems lie to you. Two nodes write to the same counter at the same time, and when they sync, one of those writes disappears. A user adds an item to a shared shopping cart on their phone while offline, then merges with the server — and the item vanishes because the server's version "won." These aren't edge cases; they're the default behavior when you build distributed state naively. The usual fixes — locking, consensus protocols, last-write-wins — either kill availability or destroy correctness. CRDTs (Conflict-Free Replicated Data Types) offer a third path: data structures mathematically designed so that concurrent updates from any number of nodes always merge into a deterministic, consistent result, with zero coordination required.

## What Makes a CRDT Work

The core idea comes from lattice theory. A CRDT is a data structure equipped with a merge operation that is commutative (`merge(a,b) == merge(b,a)`), associative (`merge(merge(a,b),c) == merge(a,merge(b,c))`), and idempotent (`merge(a,a) == a`). These three properties mean you can apply updates in any order, any number of times, and always converge to the same state. You don't need a coordinator. You don't need to know which update "happened first." You just merge.

There are two broad families. **State-based CRDTs** (CvRDTs) ship the entire state to peers; the merge function reconciles two full states. **Operation-based CRDTs** (CmRDTs) ship individual operations; the network must guarantee exactly-once delivery. State-based CRDTs are simpler to reason about and more resilient to network failures, so they're what most production systems use.

## The G-Counter: Grow-Only Counter

The simplest CRDT is the grow-only counter. Each node maintains its own slot in a vector, increments only its own slot, and the "value" is the sum of all slots. Merging two G-Counters takes the element-wise maximum.

<script src="https://gist.github.com/mohashari/7f9da7a80decd678a7451d0b32a03d71.js?file=snippet.go"></script>

Notice `Merge` never decreases any slot. That's the invariant that guarantees convergence: the structure can only grow. Two nodes that independently incremented will, after merging, reflect both increments.

## PN-Counter: Supporting Decrements

A grow-only counter is limited. Real systems need decrements — tracking available inventory, reference counts, vote tallies. The PN-Counter composes two G-Counters: one for increments (P), one for decrements (N). The value is `P.Value() - N.Value()`.

<script src="https://gist.github.com/mohashari/7f9da7a80decd678a7451d0b32a03d71.js?file=snippet-2.go"></script>

Decrementing below zero is allowed at the data structure level — your application logic enforces business rules separately. The CRDT guarantees convergence regardless.

## LWW-Register: Last-Write-Wins with Timestamps

For scalar values (a user's display name, a configuration flag), you often want a single-value register where the most recent write wins. The LWW-Register attaches a timestamp to each write; merge picks the higher timestamp.

<script src="https://gist.github.com/mohashari/7f9da7a80decd678a7451d0b32a03d71.js?file=snippet-3.go"></script>

The caveat: LWW depends on clock synchronization. If clocks skew badly, an older write with a higher timestamp wins. In practice, use hybrid logical clocks (HLC) rather than wall clocks. Systems like CockroachDB and Cassandra use LWW-Registers extensively, with HLC to mitigate clock skew.

## OR-Set: Add and Remove From Sets

Sets are where CRDTs get interesting. A naive approach — track additions and removals as sets, subtract — breaks under concurrency: if node A removes an element while node B adds it simultaneously, the result is undefined. The OR-Set (Observed-Remove Set) solves this by tagging each addition with a unique token. Removal only removes specific tagged instances.

<script src="https://gist.github.com/mohashari/7f9da7a80decd678a7451d0b32a03d71.js?file=snippet-4.go"></script>

The OR-Set's semantic rule: add wins over concurrent remove. If two nodes concurrently add and remove the same element, after merge the element is present. This is the right default for shopping carts and collaborative documents.

## Storing CRDT State in PostgreSQL

CRDT state is just serializable data. PostgreSQL's `jsonb` type makes a natural home for it, especially with its support for partial updates.

<script src="https://gist.github.com/mohashari/7f9da7a80decd678a7451d0b32a03d71.js?file=snippet-5.sql"></script>

This approach works well when PostgreSQL is your state store and you're doing infrequent merges. For high-frequency CRDT operations, you'd push merge logic into application code and batch-write to the database.

## Syncing State Between Services

Here's a minimal HTTP sync endpoint that accepts a G-Counter state payload from a peer and merges it into local state:

<script src="https://gist.github.com/mohashari/7f9da7a80decd678a7451d0b32a03d71.js?file=snippet-6.go"></script>

Nodes gossip on a schedule — every few seconds, each node picks a random peer, POSTs its current state, and receives the peer's state in the response body. Two rounds of gossip are sufficient to propagate a change across `n` nodes with high probability, making this O(log n) in convergence time.

## When to Reach for CRDTs

CRDTs shine in four situations: offline-first clients that sync later (mobile apps, local-first software), multi-region active-active databases where you can't afford cross-region write coordination, real-time collaborative features (shared cursors, presence, document editing), and high-throughput counters where locking creates contention. They are not a replacement for strong consistency — financial transactions, inventory reservations, and anything requiring linearizability still need coordination. The discipline is knowing which parts of your system can tolerate eventual consistency (most of them) and which genuinely cannot (fewer than you think). For those that can, CRDTs eliminate an entire class of coordination complexity by encoding the merge semantics directly into the data structure, letting the network be unreliable and the nodes be independent without ever producing a conflict you have to resolve.