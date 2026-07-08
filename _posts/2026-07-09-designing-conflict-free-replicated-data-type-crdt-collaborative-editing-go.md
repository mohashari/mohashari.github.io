---
layout: post
title: "Designing a Conflict-Free Replicated Data Type (CRDT) for Collaborative Editing in Go"
date: 2026-07-09 08:00:00 +0700
tags: [go, distributed-systems, crdt, collaborative-editing, systems-architecture]
description: "An in-depth, production-focused guide to building a high-performance sequence CRDT in Go, addressing tombstone garbage collection, run-length encoding, and network sync."
image: "https://picsum.photos/seed/8897/1080/720"
thumbnail: "https://picsum.photos/seed/8897/400/300"
---

In a production collaborative editor, network hiccups, high-latency mobile connections, and rapid concurrent edits are constant realities. If you rely on Operational Transformation (OT)—the tech behind Google Docs—you must route every single keystroke through a centralized, stateful server that maintains an append-only log of operations, resolves conflicts, and broadcasts transformed edits. When a client reconnects after a 10-second tunnel passage, the server must reconcile a massive buffer of divergent operations, often resulting in complex state resolution loops, CPU spikes, and disrupted user cursors. Transitioning to a Conflict-Free Replicated Data Type (CRDT) allows you to bypass the central coordinator entirely, enabling decentralized, peer-to-peer sync where updates are mathematically guaranteed to converge. However, moving CRDTs from academic papers to a production Go codebase introduces severe engineering challenges: pointer-heavy data structures trigger massive garbage collection (GC) pauses, tombstones leak memory, and lock contention on shared documents chokes throughput.

This post walks through the design and optimization of a production-grade Sequence CRDT in Go. We will look at memory layout optimization, deterministic conflict resolution, run-length encoding (RLE), state synchronization, and thread-safe actor-based execution.

## The Failure of Operational Transformation in Decentralized Production

Before diving into the CRDT implementation, it is crucial to understand why Operational Transformation (OT) fails in decentralized or edge-heavy architectures. OT requires a single, global authority to determine the "definitive" history of the document. If Client A and Client B concurrently edit a document, their operations must be sent to the server, transformed against any concurrent operations that arrived earlier, and then sent back to the clients. 

This model collapses when you introduce:
- **Offline Mode:** If a client goes offline for hours, it accumulates thousands of local operations. Merging these operations back into the main history using OT requires traversing a complex transformation matrix, which scales quadratically ($O(N^2)$) with the number of concurrent operations.
- **Peer-to-Peer Sync:** In a decentralized mesh network, there is no central server to act as the single source of truth. Without a global authority to sequence operations, OT cannot guarantee consistency.
- **Server-Side CPU Bottlenecks:** The server must hold the operational history of every active document in memory to transform incoming edits. For thousands of active documents, this creates substantial memory and CPU overhead.

A sequence CRDT resolves this by shifting the complexity from the transport layer to the data model. Instead of treating the document as an array of characters indexed by absolute positions, a sequence CRDT treats it as a directed acyclic graph (DAG) or a double-linked list of uniquely identified items. Operations are associative, commutative, and idempotent, meaning they can be applied in any order, any number of times, and still yield the exact same document state.

## Designing the Core: The Sequence CRDT Data Model

A naive sequence CRDT implementation represents every character as an individual object on the heap. If you represent a 100,000-character document with one struct per character, you end up with 100,000 heap allocations. In Go, where each pointer takes 8 bytes and struct padding applies, this naive design leads to massive memory overhead and forces the tri-color mark-and-sweep garbage collector to scan millions of objects, causing latency spikes (GC pauses) on every keypress.

To solve this, we must:
1. Avoid string identifiers. Use compact numeric types for IDs.
2. Group contiguous character insertions from the same client into a single `Item` block using Run-Length Encoding (RLE).
3. Use a double-linked list for fast in-memory document traversal and insertion.

Here is the production-grade data model for our Go CRDT:

<script src="https://gist.github.com/mohashari/ab7a974e27f259eac08b5599ff601609.js?file=snippet-1.go"></script>

By defining the `ID` struct with a `uint64` client ID and a `uint32` counter, we keep it down to 12 bytes. Compare this to using a UUID string, which would require a 16-byte string header pointing to a heap-allocated byte slice, immediately doubling the allocation count.

## The YATA Integration Loop: Deterministic Convergence

When multiple clients concurrently insert text at the same position, their new items share the same `Origin`. The core challenge of a sequence CRDT is deciding the relative order of these concurrent inserts deterministically across all nodes. We will use a variant of the YATA (Yet Another Text Ack) algorithm, which is also utilized by frameworks like Yjs.

When integrating a remote item, we scan right from its `Origin`. If we encounter other items that share the same origin, or items that were inserted concurrently by other clients, we must compare their client IDs to enforce a total order.

<script src="https://gist.github.com/mohashari/ab7a974e27f259eac08b5599ff601609.js?file=snippet-2.go"></script>

This integration loop guarantees that even if Client A's insert arrives at Client B before B's own edits are processed, both clients will resolve the conflict identically and arrive at the exact same character sequence.

## The Tombstone Tax and Run-Length Encoding

In a collaborative text editor, users delete characters almost as often as they insert them. Because sequence CRDTs rely on the historical graph to resolve concurrent insertions, we cannot simply delete an `Item` from the linked list when a user presses backspace. Doing so would break the origin chain for any concurrent inserts still in flight. 

Instead, we mark the item as deleted (`Deleted = true`). This is called a **tombstone**. 

In long-running documents with thousands of edits, tombstones create a massive memory leak. If a document has 5,000 characters of active text but has undergone 500,000 edits, the in-memory graph still contains all 505,000 nodes. 

To mitigate this heap bloat in production, we use **Run-Length Encoding (RLE)**. When a client types "hello", we allocate a single `Item` block containing `Value = []byte("hello")` and a counter of `0`. 

If a concurrent edit is inserted in the middle of this block (e.g., between "e" and "l"), we split the block at the offset, reslicing the byte slice without allocating new backing arrays, and insert the new character in between.

<script src="https://gist.github.com/mohashari/ab7a974e27f259eac08b5599ff601609.js?file=snippet-3.go"></script>

### Memory Savings Analysis

By reslicing `target.Value = target.Value[:offset]`, Go simply adjusts the slice header length without copying or allocating new memory on the heap. Only the new `rightItem` struct is allocated. In production, this design reduces memory usage by up to 90% during typical typing sessions compared to character-by-character allocation schemes.

## Network Sync: State Vectors and Delta Updates

When two clients connect or reconnect, they must reconcile their states. Sending the entire document history over the wire is highly inefficient. Instead, we use **State Vectors** (also known as Version Vectors). 

A State Vector maps each `ClientID` to the highest `Counter` that the local node has integrated. By exchanging state vectors, two nodes can determine exactly which edits they are missing.

<script src="https://gist.github.com/mohashari/ab7a974e27f259eac08b5599ff601609.js?file=snippet-4.go"></script>

This synchronization exchange is a two-step handshake:
1. **Client A** connects to **Server B** and sends its local `StateVector`.
2. **Server B** calls `ComputeDelta(clientStateVector)` and streams the missing `Item` objects back to **Client A**. **Client A** then calls `Integrate` on each received item.

## Production Hardening: Actor Loops and Allocation Tuning

In a real-time collaborative system, you might have hundreds of WebSocket connections updating the same document simultaneously. If you protect the document using a single `sync.RWMutex` directly inside the WebSocket read handlers, you will quickly run into lock contention, queueing delays, and high CPU usage.

To solve this lock contention, we use the **Actor Pattern**. The document state is owned by a single coordinator goroutine. All WebSocket handlers write edits to an inbox channel, and the coordinator processes them sequentially. 

Additionally, we use a `sync.Pool` to recycle incoming update envelopes, preventing heap allocation churn under high write concurrency.

<script src="https://gist.github.com/mohashari/ab7a974e27f259eac08b5599ff601609.js?file=snippet-5.go"></script>

### Tuning Garbage Collection (GC) in Production

In high-concurrency Go services, heap allocation rates dictate latency profiles. By pooling the update wrapper structs via `sync.Pool`, we keep the garbage collector out of the critical path. 

In addition, because we represent the CRDT list natively in memory, you should consider using a memory threshold (like setting `GOGC=100` or configuring `GOMEMLIMIT` in Go 1.19+) to control GC frequency relative to the container's physical memory footprint.

## Key Design Takeaways for Production Deployments

When building collaborative sync services in Go, keep these architectural decisions in mind:

1. **Avoid Pointer Churn:** Never allocate one struct per character. Group contiguous text runs into block-based items and split them dynamically on edit.
2. **Compact Identifiers:** Use numeric types (e.g., `uint64` and `uint32`) for transaction clocks instead of strings to reduce struct size and avoid heap pointer scanning.
3. **Actor over Mutex:** Do not allow WebSocket goroutines to directly lock and modify the document. Serialize writes through an actor loop to remove lock contention and avoid data races.
4. **Pool the Envelopes:** Use `sync.Pool` to recycle incoming message envelopes. Minimizing allocations per keystroke is the most effective way to eliminate GC pauses.