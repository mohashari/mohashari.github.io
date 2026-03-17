---
layout: post
title: "CRDTs: Conflict-Free Data Structures for Eventually Consistent Systems"
date: 2026-03-18 07:00:00 +0700
tags: [distributed-systems, crdts, consistency, databases, backend]
description: "Explore G-Counters, LWW-Registers, and OR-Sets to build collaborative features and multi-region state that merges automatically without coordination."
---

Building collaborative features across distributed systems is one of those problems that looks deceptively simple until you hit it in production. Two users edit the same document simultaneously, two data centers accept writes during a network partition, a mobile client queues offline mutations — and suddenly you're staring at conflicting state with no obvious winner. The naive answer is locking, but locks require coordination, and coordination is the enemy of availability. A more elegant answer is to design your data structures so conflicts simply cannot occur: enter Conflict-Free Replicated Data Types (CRDTs), mathematical structures whose merge operations are commutative, associative, and idempotent, meaning any two replicas can merge in any order and always converge to the same result.

## What Makes a CRDT Work

A CRDT's merge function must satisfy three algebraic properties. **Commutativity** means `merge(A, B) == merge(B, A)` — order of arrival doesn't matter. **Associativity** means `merge(merge(A, B), C) == merge(A, merge(B, C))` — grouping doesn't matter. **Idempotency** means `merge(A, A) == A` — re-delivering a message is safe. When your state transitions obey these rules, you get "strong eventual consistency": any two nodes that have seen the same set of operations will be in identical state, without ever needing a coordinator or a lock.

There are two broad families. **State-based CRDTs (CvRDTs)** gossip their full state; merge computes a least-upper-bound in a join-semilattice. **Operation-based CRDTs (CmRDTs)** broadcast operations instead; the delivery layer must ensure exactly-once, causal delivery. State-based are simpler to implement (just ship the struct and merge it), so the examples below use that model.

## G-Counter: Distributed Increment Only

The simplest CRDT is a grow-only counter. Each node owns one slot in a vector, increments only its own slot, and the merge takes the per-slot max. The global value is the sum.

<script src="https://gist.github.com/mohashari/c46e6c4d853f08c79a6768f269d4a177.js?file=snippet.go"></script>

`★ Insight ─────────────────────────────────────`
The per-slot max is the join operation of the semilattice. Because integers ordered by `≤` form a lattice, taking the max is always safe — you can never "lose" an increment by merging. The vector structure prevents one node from inflating another's count.
`─────────────────────────────────────────────────`

## PN-Counter: Supporting Decrements

A grow-only counter is useful for things like "total events processed," but most product counters need decrement too. The trick is two G-Counters: one for increments, one for decrements. The value is `P.Value() - N.Value()`.

<script src="https://gist.github.com/mohashari/c46e6c4d853f08c79a6768f269d4a177.js?file=snippet-2.go"></script>

This pattern — decomposing a bidirectional operation into two monotonically growing structures — appears throughout CRDT design. Subtraction becomes addition in a separate lattice.

## LWW-Register: Last-Write-Wins with Timestamps

When you need a single mutable value (a user's display name, a configuration flag), a Last-Write-Wins Register assigns a timestamp to every write. Merge picks the entry with the higher timestamp. The critical engineering decision is your clock: logical timestamps (Lamport clocks) give you total order without clock skew; wall clocks are simpler but risk collisions.

<script src="https://gist.github.com/mohashari/c46e6c4d853f08c79a6768f269d4a177.js?file=snippet-3.go"></script>

The tiebreaker on `NodeID` (lexicographic comparison) gives you deterministic convergence even when two nodes write at the exact same nanosecond — a real scenario under high load.

## OR-Set: Add-Wins Concurrent Sets

Sets are where CRDTs get interesting. If node A removes element `"alice"` while node B adds `"alice"` concurrently, which wins? An **Observed-Remove Set** (OR-Set) uses unique tags: every add generates a UUID, and a remove only cancels the specific tags it has observed. Concurrent adds survive because they carry tags the remover never saw.

<script src="https://gist.github.com/mohashari/c46e6c4d853f08c79a6768f269d4a177.js?file=snippet-4.go"></script>

`★ Insight ─────────────────────────────────────`
OR-Sets solve the "add wins vs. remove wins" dilemma by making it not a choice — you get add-wins semantics by default, because a concurrent add always generates a fresh tag invisible to the remover. If you need remove-wins, you'd use an RW-Set or a different bias. The tradeoff is storage: every add leaves a UUID tombstone until you run garbage collection.
`─────────────────────────────────────────────────`

## Storing CRDT State in PostgreSQL

CRDTs don't live only in memory — you need to persist and merge them at rest. PostgreSQL's `jsonb` column type is a natural fit for the vector structures above, and a simple upsert with a merge function handles convergence.

<script src="https://gist.github.com/mohashari/c46e6c4d853f08c79a6768f269d4a177.js?file=snippet-5.sql"></script>

Because `merge_pn_counter` is idempotent and commutative, you can run this upsert from multiple application servers simultaneously without locking — Postgres serializes the row-level write, but the outcome is always correct regardless of order.

## Gossip Replication Between Nodes

In a multi-region setup, nodes periodically gossip their state to peers. The following shell snippet illustrates a simple gossip loop using `curl` — production would use gRPC streaming or a message bus, but the semantics are identical.

<script src="https://gist.github.com/mohashari/c46e6c4d853f08c79a6768f269d4a177.js?file=snippet-6.sh"></script>

The beauty here is failure handling: if a peer is unreachable, you just retry next interval. No distributed transaction, no coordinator failure mode, no split-brain. The CRDT guarantees that whenever the peer comes back online and receives the state, it will converge correctly.

## Wiring It All Together

CRDTs are not a silver bullet — LWW registers silently discard concurrent writes (only the latest survives), OR-Sets accumulate garbage until you tombstone-collect, and causal consistency requires vector clocks that grow with cluster size. But for the right problems — collaborative editing, distributed counters, shopping carts, presence indicators, feature flags — they eliminate an entire class of coordination bugs. The engineering discipline is in recognizing when your problem fits the CRDT shape: you need convergence, you can tolerate eventual consistency, and your merge semantics are well-defined. When those conditions hold, you can delete your distributed locks, remove your write serialization, and let mathematics do the coordination work for you.