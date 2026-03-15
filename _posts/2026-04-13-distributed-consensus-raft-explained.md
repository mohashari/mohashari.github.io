---
layout: post
title: "Distributed Consensus: Raft Explained from First Principles"
date: 2026-04-13 07:00:00 +0700
tags: [distributed-systems, raft, consensus, databases, backend]
description: "Understand how the Raft consensus algorithm achieves fault-tolerant agreement in distributed systems, from leader election to log replication."
---

Distributed systems fail in fascinating ways. A network partition splits your cluster in two; a leader crashes mid-write; a slow disk stalls a follower just long enough for it to fall behind. Without a principled approach to agreement, you end up with split-brain scenarios where two nodes both believe they are authoritative, silently diverging until your data is irrecoverably inconsistent. This is the consensus problem: how do a group of nodes agree on a sequence of values, even when some of them crash or messages are delayed? Raft is the answer that prioritizes understandability without sacrificing correctness, and understanding it from first principles will change how you reason about every distributed database, message queue, and coordination service you touch.

## The Core Insight: Elect One Leader, Then Everything Is Simple

Raft's key insight is that consensus is easier if you decompose it: first elect a single leader, then let that leader make all decisions about log ordering. This is philosophically different from Paxos, where any node can propose values and the protocol must reconcile concurrent proposals. In Raft, only the leader appends entries to the replicated log — followers are strictly passive replicators. The complexity budget spent on multi-proposer reconciliation is redirected toward making leader election safe and fast.

Every Raft node is in one of three states: **follower**, **candidate**, or **leader**. Followers accept writes only through the leader. If a follower hears nothing from a leader within an election timeout, it becomes a candidate and starts an election. The first candidate to collect a majority of votes becomes the new leader.

Here is a minimal Go representation of node state:

<script src="https://gist.github.com/mohashari/bcf0fee38e875497538130236255414f.js?file=snippet.go"></script>

## Terms: Raft's Logical Clock

Raft uses **terms** as a logical clock to detect stale information. Terms are numbered sequentially; each begins with an election. If a node receives a message with a higher term than its own, it immediately reverts to follower and updates its term. This is how Raft handles "zombie leaders" — a node that was partitioned away, believing it is still leader, is immediately demoted when it reconnects and sees the world has moved on.

<script src="https://gist.github.com/mohashari/bcf0fee38e875497538130236255414f.js?file=snippet-2.go"></script>

The comment about persistence is not cosmetic. Raft requires that `currentTerm` and `votedFor` survive crashes. Without this, a restarted node could vote for two different candidates in the same term, breaking the safety invariant that at most one leader is elected per term.

## Leader Election: Randomized Timeouts Prevent Livelock

Each follower waits for a heartbeat from the leader. If none arrives within the election timeout — typically 150–300ms, chosen randomly per node — it increments its term, transitions to candidate, votes for itself, and broadcasts `RequestVote` RPCs to all peers.

<script src="https://gist.github.com/mohashari/bcf0fee38e875497538130236255414f.js?file=snippet-3.go"></script>

A candidate grants a vote only if: the requester's term is at least as large as the voter's current term, and the requester's log is at least as up-to-date as the voter's (comparing last log term first, then last log index). This **log completeness** check ensures a node with stale entries can never win an election — the new leader is guaranteed to have all committed entries.

## Log Replication: The Heartbeat Does Double Duty

Once elected, the leader sends `AppendEntries` RPCs to all followers continuously — even if there are no new entries — as heartbeats. When a client writes arrive, the leader appends the entry to its local log and includes it in the next `AppendEntries` broadcast. An entry is **committed** once the leader has received acknowledgment from a majority of nodes.

<script src="https://gist.github.com/mohashari/bcf0fee38e875497538130236255414f.js?file=snippet-4.go"></script>

The `prevLogIndex` and `prevLogTerm` fields are the consistency check: a follower rejects the RPC if its log does not contain an entry at `prevLogIndex` with term `prevLogTerm`. This is how Raft enforces that logs are always a prefix match with the leader's log before any new entries are appended.

## Advancing the Commit Index

The leader advances its `commitIndex` only when a log entry from the **current term** has been replicated to a majority. This is a subtle but critical restriction — it prevents a specific safety violation described in the Raft paper (§5.4.2) where entries from a previous term could be incorrectly committed.

<script src="https://gist.github.com/mohashari/bcf0fee38e875497538130236255414f.js?file=snippet-5.go"></script>

## Deploying a Raft Cluster: etcd as a Reference Implementation

Most production systems don't implement Raft from scratch — they embed etcd or use a library like `etcd/raft`. A three-node etcd cluster wired via Docker Compose illustrates the configuration shape every Raft deployment shares:

<script src="https://gist.github.com/mohashari/bcf0fee38e875497538130236255414f.js?file=snippet-6.yaml"></script>

The `election-timeout` should be at least 10x the `heartbeat-interval` to give the leader enough chances to assert liveness before a follower times out and unnecessarily triggers an election.

## Testing Raft: Fault Injection Is Mandatory

A Raft implementation that only works under happy-path conditions is worthless. The only way to build confidence is chaos testing. A simple shell script that kills the current leader and verifies the cluster elects a new one within a bounded time is a good baseline:

<script src="https://gist.github.com/mohashari/bcf0fee38e875497538130236255414f.js?file=snippet-7.sh"></script>

Raft guarantees that a new leader will be elected within roughly `2 * election_timeout` as long as a majority of nodes are reachable. With a three-node cluster, you can lose one node and continue. With five nodes, you can lose two.

## What Raft Cannot Do

Understanding the limits matters as much as understanding the mechanics. Raft does not guarantee that a client's read sees the latest committed write unless the leader explicitly confirms its leadership with a quorum before responding — called a **linearizable read**. Skipping this check allows "stale reads" from a node that has been partitioned away but still believes it is leader. Additionally, Raft has no built-in mechanism for log compaction; production systems layer **snapshots** on top, periodically checkpointing the state machine and discarding old log entries to prevent unbounded disk growth.

Raft is not magic — it is a carefully constrained protocol that trades flexibility for comprehensibility. Every production system that claims "strong consistency" — etcd, CockroachDB, TiKV, Consul — has Raft (or a close variant) at its core. When you understand why the log completeness check in `RequestVote` exists, why persistence of `votedFor` is non-negotiable, and why committing only current-term entries is a safety requirement rather than an optimization, you have the mental model to operate these systems confidently, diagnose split-brain alerts correctly, and tune election timeouts without guessing. The algorithm fits in a graduate paper; the intuition fits in an afternoon. That was always the point.