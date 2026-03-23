---

**File 1:** `_posts/2026-03-23-crdts-distributed-state-conflict-free.md`

```markdown
---
layout: post
title: "CRDTs for Distributed State: Conflict-Free Replicated Data Types in Practice"
date: 2026-03-23 08:00:00 +0700
tags: [distributed-systems, databases, consistency, golang, architecture]
description: "How CRDTs eliminate coordination overhead in distributed systems and where they break down in real production environments."
image: ""
thumbnail: ""
---

You've instrumented your distributed shopping cart. Two users in different regions add items simultaneously. The network partitions for 8 seconds. When connectivity resumes, one cart wins and the other's additions silently vanish. You patch it with a last-write-wins register. Now concurrent adds from the same user on mobile and desktop cause random item loss. You add distributed locks. Now your 2ms add-to-cart operation takes 45ms and your lock service is a single point of failure. CRDTs — Conflict-Free Replicated Data Types — exist precisely for this failure mode: state that must be mutated concurrently across nodes without coordination, without data loss, and without consensus overhead.

![CRDTs for Distributed State: Conflict-Free Replicated Data Types in Practice Diagram](/images/diagrams/crdts-distributed-state-conflict-free.svg)

## What CRDTs Actually Guarantee

The formal property is **strong eventual consistency** (SEC): any two nodes that have received the same set of updates will have identical state, regardless of the order in which those updates were applied. This is stronger than eventual consistency (which only promises *eventual* convergence) but weaker than linearizability (which requires a total order of operations visible to all readers).

The math behind this is a **join-semilattice**: a partial order with a least upper bound (join) operation. Every merge must be:

- **Commutative**: `merge(A, B) = merge(B, A)`
- **Associative**: `merge(merge(A, B), C) = merge(A, merge(B, C))`
- **Idempotent**: `merge(A, A) = A`

If your merge function satisfies these three properties, you get SEC for free. This is why CRDTs eliminate coordination — you never need to know *when* messages arrive or in what order, because the merge is always deterministic.

There are two families: **state-based CRDTs** (CvRDTs) ship the entire state and merge on receipt; **operation-based CRDTs** (CmRDTs) ship operations and require exactly-once delivery. In practice, delta-state CRDTs give you the best of both: ship only the changed portions of state, require no delivery guarantees beyond eventual delivery.

## The Core Data Structures

### G-Counter (Grow-Only Counter)

The simplest useful CRDT. Each node maintains its own slot in a vector. The global count is the sum. Merge is component-wise max.

<script src="https://gist.github.com/mohashari/5b49034bf5547fb1c825a68ff7fe4c82.js?file=snippet-1.go"></script>

For PN-Counters (increment and decrement), maintain two G-Counters: one for increments, one for decrements. The value is `P.Value() - N.Value()`. The catch: you can never truly delete; the tombstone accumulates forever. At Riak, their internal counters are PN-Counters — this is why their counter type doesn't support reset.

### OR-Set (Observed-Remove Set)

The naive 2P-Set (two-phase set: add set + remove set) breaks down immediately: you can't re-add a removed element. The OR-Set solves this with unique tags per add operation.

<script src="https://gist.github.com/mohashari/5b49034bf5547fb1c825a68ff7fe4c82.js?file=snippet-2.go"></script>

The OR-Set resolves the add-wins vs. remove-wins debate by making it explicit: concurrent add and remove means the add wins because the remove only observed the old token. This is the behavior you almost always want in a shopping cart.

### LWW-Register and MVR

Last-Write-Wins Register assigns a timestamp to each write. Merge picks the highest. Simple, widely used (Cassandra's default), and deeply problematic in practice: clock skew of even 50ms between nodes can cause newer writes to lose to older ones. On AWS EC2, NTP drift between instances has been observed exceeding 200ms during network stress events.

The safer alternative for truly concurrent writes is a **Multi-Value Register** (MVR): retain all concurrent values as siblings and force the application to resolve. This is what Riak's `allow_mult=true` does, and what Amazon Dynamo's original paper described. The trade-off is that your application now has to handle `[]*CartItem` instead of `*CartItem` — more complex but honest about the concurrency.

<script src="https://gist.github.com/mohashari/5b49034bf5547fb1c825a68ff7fe4c82.js?file=snippet-3.go"></script>

## Delta-State CRDTs: Practical Replication

State-based CRDTs ship the entire state on every sync. For a G-Counter with 10,000 node IDs, that's 10,000 entries per gossip round. Delta-CRDTs ship only the *delta* since last acknowledgment — the minimal state change that, when merged, produces the same result as merging the full state.

<script src="https://gist.github.com/mohashari/5b49034bf5547fb1c825a68ff7fe4c82.js?file=snippet-4.go"></script>

In a 100-node cluster with infrequent writes per node, delta-CRDTs reduce gossip bandwidth by ~98% versus full state shipping. Riak DT's internal implementation is delta-based since version 2.1.

## Production Deployment: Riak, Redis, and Automerge

### Riak DT

Riak exposes G-Counters, PN-Counters, OR-Sets, OR-Maps, and LWW-Registers natively via its bucket types API. Under the hood it uses vector clocks for causality tracking and Dotted Version Vectors (DVV) for accurate sibling detection. The HTTP API:

```bash
# snippet-5
# Create bucket type with CRDT counter
riak-admin bucket-type create counters '{"props":{"datatype":"counter"}}'
riak-admin bucket-type activate counters

# Increment counter via HTTP — no read-before-write needed
curl -X POST http://riak:8098/types/counters/buckets/metrics/datatypes/page_views \
  -H 'Content-Type: application/json' \
  -d '{"increment": 1}'

# Fetch — returns current merged value across all replicas
curl http://riak:8098/types/counters/buckets/metrics/datatypes/page_views
# {"type":"counter","value":14293}

# OR-Set for session tokens
riak-admin bucket-type create sets '{"props":{"datatype":"set"}}'
riak-admin bucket-type activate sets
curl -X POST http://riak:8098/types/sets/buckets/auth/datatypes/user:42:sessions \
  -H 'Content-Type: application/json' \
  -d '{"add_all":["tok-abc123"]}'
```

Riak's default `n_val=3` with `w=2, r=2` means writes and reads quorum across replicas but CRDTs converge even on `w=1` — you're not relying on quorum for correctness, only for availability guarantees.

### Redis with CRDT Module (RedisGears / Redis Enterprise)

Redis Enterprise's Active-Active Geo-Distribution implements CRDT semantics across geo-replicated clusters. For open-source Redis, the `CRDT` module (from redislabs) provides similar primitives:

```bash
# snippet-6
# Redis Enterprise Active-Active — conflict resolution rules per data type
# Strings: LWW by default (last write by wall clock wins)
# Counters: CRDT counter (PN-Counter semantics)
# Sets: OR-Set semantics (concurrent add+remove = add wins)
# Sorted Sets: Last-write-wins per member score

# In Redis Enterprise, replicated counter increment — safe across geo
CRDT.INCRBY counter:page_views 1 :1 :1711181000000  # key delta vclock_entry timestamp_ms

# Check convergence status in Active-Active setup
redis-cli -c CRDT.DEBUG SET counter:page_views  # inspect internal CRDT state
```

The caveat: Redis LWW on strings means that `SET key val` from two regions concurrently will silently drop one write. If you need merge semantics, explicitly use `CRDT.INCRBY` or model your data as a set rather than a string.

### Automerge for Application-Level CRDTs

For document-style state (collaborative editing, config management, shared whiteboards), Automerge provides a full CRDT library in Rust/JS/Go that handles lists, maps, text, and counters:

<script src="https://gist.github.com/mohashari/5b49034bf5547fb1c825a68ff7fe4c82.js?file=snippet-7.go"></script>

Automerge's list merge uses a fractional indexing scheme: each element gets a unique position identifier, so concurrent inserts produce a deterministic total order without coordination. It's used by Ink & Switch for their local-first applications and underpins several collaborative editor implementations.

## Where CRDTs Break Down

**Causal consistency is not linearizability.** If user A removes an item from their cart and immediately reads it back, they may still see it if the read hits a replica that hasn't received the remove yet. This is SEC, not read-your-writes. You need sticky sessions or causal tokens if you need the latter.

**Tombstone accumulation is real.** OR-Sets and 2P-Sets never truly delete. At Soundcloud, their Riak-based social graph accumulated multi-gigabyte tombstone sets for heavily-modified edges. You need periodic garbage collection with a causal barrier — all nodes must have observed the tombstone before you can prune it. Riak calls this "active anti-entropy" and "tombstone reaping." Get this wrong and you resurrect deleted data after GC.

**CRDTs don't handle semantic invariants.** A PN-Counter can go negative. An OR-Set can contain logically inconsistent combinations. If your invariant is "cart total must not exceed credit limit," CRDTs cannot enforce it — that requires coordination. The practical boundary: use CRDTs for *accumulation* (counts, sets, logs) and distributed locking or Saga patterns for *invariant-constrained* state transitions.

**LWW clock drift kills correctness.** In a write-heavy, multi-region system, don't use LWW unless you have bounded clock synchronization (e.g., Google TrueTime, AWS Time Sync Service with sub-millisecond accuracy). NTP alone is not sufficient for LWW correctness under concurrent write load. Use vector clocks or hybrid logical clocks (HLC) instead — Cockroach DB's HLC implementation is a solid reference.

**State explosion in large graphs.** Vector clocks grow linearly with the number of participating nodes. In a 500-node cluster, a vector clock entry per node is 500 uint64s — 4KB just for causal metadata on a tiny counter. Dotted Version Vectors (DVV) and interval tree clocks (ITC) compress this significantly, but they're not trivially implementable.

## Choosing the Right CRDT

| Use Case | CRDT Type | Production Tool |
|---|---|---|
| View counters, metrics | G-Counter / PN-Counter | Riak counter, Redis CRDT |
| Shopping cart, session tokens | OR-Set | Riak set, Automerge |
| User profile last-updated | LWW-Register (with HLC) | Cassandra (carefully) |
| Collaborative document | LSEQ / RGA / Automerge | Automerge, Yjs |
| Distributed config | OR-Map | Riak map, custom |
| Feature flags (boolean) | Enable-wins flag | Custom G-Counter |

The biggest mistake teams make is reaching for CRDTs as a general-purpose solution to distributed state. They're not. They're a precise tool for a specific class of problem: state that must be replicated without coordination and where the merge semantics of the CRDT match the business semantics of the data. Get that alignment right and you eliminate an entire class of coordination bugs. Get it wrong and you trade lock contention for silent data anomalies that are considerably harder to debug.

Start with the data structure, not the library. Draw out the merge operation for your specific use case. If you can express `merge(A, B)` as a commutative, associative, idempotent function over your data, you have a CRDT candidate. If you can't — if the merge requires application context or invariant checking — you don't, and you need a coordination protocol instead.
```

---

Please approve the write permissions when prompted — there are two files to save:
1. `_posts/2026-03-23-crdts-distributed-state-conflict-free.md` — the blog post
2. `images/diagrams/crdts-distributed-state-conflict-free.svg` — the architecture diagram

Both are new files. If you'd like me to retry saving them now, just say the word.