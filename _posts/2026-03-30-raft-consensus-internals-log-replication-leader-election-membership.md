---
layout: post
title: "Raft Consensus Internals: Log Replication, Leader Election, and Membership Changes"
date: 2026-03-30 08:00:00 +0700
tags: [distributed-systems, consensus, raft, etcd, backend]
description: "A deep dive into Raft's log replication, election mechanics, and membership change protocols — with production failure modes and real numbers."
image: "https://picsum.photos/seed/7437/1080/720"
thumbnail: "https://picsum.photos/seed/7437/400/300"
---

Your etcd cluster loses its leader at 3 AM. Within 300 milliseconds, one of the remaining two nodes has called an election, collected votes, and is already sending heartbeats. Your Kubernetes API server briefly pauses, then resumes without a single request lost. That recovery didn't happen by accident — it is the result of a carefully engineered consensus protocol whose internals most engineers who depend on it have never read. Raft is the algorithm inside etcd, CockroachDB, TiKV, Consul, and dozens of other systems you probably run in production. Understanding its internals is not academic exercise; it is the difference between diagnosing a split-brain incident in ten minutes and spending three hours staring at metrics.

![Raft Consensus Internals: Log Replication, Leader Election, and Membership Changes Diagram](/images/diagrams/raft-consensus-internals-log-replication-leader-election-membership.svg)

## The Replicated Log as the Single Source of Truth

Raft's core abstraction is a **replicated log**: an ordered, append-only sequence of commands that every node in the cluster will eventually apply in the same order. The leader owns that log. It appends entries, replicates them to followers via `AppendEntries` RPCs, and advances the `commitIndex` once a majority has persisted each entry.

The critical invariant: **a log entry is committed only when it has been durably written by a majority of nodes**. Not acknowledged in memory — written. etcd fsync()s the WAL before acknowledging. That single fact is why etcd's default write latency is 1–2 ms on NVMe but 10–15 ms on spinning disk: the disk flush is on the critical path of every write quorum.

Each log entry carries three pieces of metadata: the entry's index, the term in which the leader that created it was elected, and the payload (your key-value write, your CockroachDB row, your Consul KV update). The `(index, term)` pair uniquely identifies the entry across the lifetime of the cluster. This is how Raft's log matching property works: if two logs have an entry with the same index and term, all preceding entries are identical. The leader exploits this during replication by sending its `prevLogIndex` and `prevLogTerm` with each `AppendEntries` — followers reject the RPC if their log doesn't match at that position, forcing a binary search-style rollback until the leader finds the divergence point.

The `commitIndex` and `lastApplied` pointers are intentionally separate. A leader can commit an entry and advance `commitIndex` before `lastApplied` catches up on the state machine. This allows pipelining: the leader sends the next batch of entries while the state machine applies the previous batch. etcd's implementation keeps this pipeline small (default `maxInflightMsgs = 256`) to bound memory and avoid thundering-herd replay during crash recovery.

## Leader Election: Randomized Timeouts and Why They Work

Raft avoids a coordinator for elections by using randomized election timeouts. Every follower resets a timer when it receives a heartbeat. If the timer fires — meaning no heartbeat arrived within the window — the follower transitions to **Candidate**, increments its term, votes for itself, and broadcasts `RequestVote` RPCs.

The default etcd election timeout is 1000 ms (heartbeat at 100 ms), but the cluster-wide effective timeout is randomized per-node in the range `[ElectionTimeout, 2×ElectionTimeout]`. The randomization ensures that under a simultaneous leader failure, nodes fire their timers at slightly different times — the first to fire usually collects a quorum before others have even transitioned to Candidate, which is why Raft typically completes elections in a single round-trip rather than multiple rounds of voting conflict.

A Candidate wins an election when it collects votes from a majority — in a five-node cluster that means three votes including its own. The voting rules enforce two properties simultaneously:

1. **Safety**: A voter grants a vote only if the Candidate's log is *at least as up-to-date* as the voter's own log. "More up-to-date" means: higher term in the last log entry, or equal term with equal-or-higher index. This prevents a node with a stale log from becoming leader and overwriting committed entries.

2. **Liveness**: Each node grants at most one vote per term, on a first-come-first-served basis. This prevents vote-splitting from becoming a livelock — combined with randomized timeouts, the probability of repeated failed elections drops exponentially per round.

A newly elected leader does not immediately know the full committed state of the cluster. It knows its own log, and it knows it was elected because its log was at least as up-to-date as a majority. But it does not commit entries from previous terms by counting replicas — it only advances `commitIndex` by committing a new entry from the **current term**. This is the subtlest rule in Raft and the one most engineers get wrong when reading the paper. The practical consequence: a new leader appends a no-op entry at election time (etcd does this), which immediately establishes `commitIndex` in the new term and unblocks any reads that require linearizability.

## Log Replication: Pipeline, Back-Pressure, and the Two-Phase Commit Analogy

Once elected, the leader sends `AppendEntries` RPCs to all followers in parallel. It maintains a `nextIndex` and `matchIndex` for each follower: `nextIndex` is the optimistic next entry to send, `matchIndex` is the confirmed highest replicated index. On success, the leader advances `matchIndex` and recalculates `commitIndex` as the highest index that a majority of `matchIndex` values cover.

Raft's replication looks like a two-phase commit at the level of an individual entry:
- **Phase 1**: Leader appends to its local log, sends `AppendEntries` to followers.
- **Phase 2**: Once majority acknowledges, leader advances `commitIndex`, sends next `AppendEntries` (which carries the updated `leaderCommit`), followers advance their own `commitIndex` and apply to state machine.

The key difference from 2PC: **there is no blocking coordinator waiting for all nodes**. A majority is sufficient. The minority that is slow or partitioned will catch up when they reconnect — the leader detects their lag via `nextIndex` and will retransmit from the divergence point. Under network partition, a minority partition that elects its own leader will find all its writes rolled back when the partition heals, because the majority partition's leader will have a higher term. Those rolled-back writes were never committed, so no state machine ever applied them. Safety holds.

One subtle production failure: **follower disk saturation**. If a follower's disk is slow, `AppendEntries` ACKs arrive late, the follower falls behind, and the in-memory log on the leader grows waiting for retransmits. In etcd, a follower that falls more than `--snapshot-count` (default 100,000) entries behind triggers a snapshot transfer rather than log replay, because the WAL segment for those 100k entries may have been compacted. The snapshot transfer is expensive (full state serialization over gRPC). If you're seeing frequent `sending snapshot to` log lines in etcd, your follower is repeatedly disk-thrashing. Fix the disk, or tune `--snapshot-count`.

## Read Linearizability Without Stale Reads

Reads are deceptively tricky in Raft. A naive read from the leader's state machine can return stale data: the leader may have been partitioned, and a new leader may have committed writes that the old leader hasn't seen. Raft provides two mechanisms:

**ReadIndex**: The leader records the current `commitIndex`, then sends a round of heartbeats. Once it confirms it still holds a quorum (all acks arrive), it serves the read against the state at that `commitIndex`. This adds one heartbeat RTT — roughly 100–200 µs within a datacenter — but requires no disk writes.

**Lease-based reads**: The leader notes the time it received a quorum of heartbeat responses. Since followers won't start an election until `ElectionTimeout` elapses after their last heartbeat, the leader can serve reads for the next `ElectionTimeout - ClockDrift` duration without a second quorum round. This is faster but requires bounded clock skew. etcd enables lease reads with `--experimental-enable-lease-checkpoint` in newer versions.

CockroachDB calls this "follower reads" — by serving reads from a consistent timestamp that lags behind the leader by `kv.closed_timestamp.target_duration` (default 4.8 seconds), follower nodes can serve reads without any leader involvement. The tradeoff is staleness, which is acceptable for analytical workloads but not transactional ones.

## Membership Changes: The Joint Consensus Problem

Adding or removing nodes from a running Raft cluster without downtime is harder than it looks. The naive approach — just update the config on the leader and replicate it — creates a window where two disjoint majorities are possible. Consider removing one node from a three-node cluster: during the transition, the old config requires 2 of 3 nodes for a quorum, and the new config requires 2 of 2. If the leader commits under the new config while a partitioned node still thinks it's running under the old config, you have two leaders.

Raft's original solution is **joint consensus**, a two-phase approach:
- **Phase 1 (C_old+new)**: The leader appends a configuration entry that contains both the old and new member sets. During this phase, decisions require a majority from *both* the old and new configurations simultaneously. This ensures there's no point where two disjoint majorities can exist.
- **Phase 2 (C_new)**: Once C_old+new is committed, the leader appends C_new. Nodes not in C_new shut down; nodes in C_new now use only the new config for quorum.

Joint consensus is correct but complex to implement. The real-world alternative, championed by Diego Ongaro in his later work and used by etcd, is **single-server changes**: add or remove one node at a time. If you go from 3 nodes to 4, then 4 to 5, each individual step is safe without a joint phase — a single-node delta can't create two disjoint majorities. etcd's `member add` and `member remove` commands use this approach. The catch: you must add learners (non-voting members) first, wait for them to catch up via log replication, then promote them to voters — done via `member promote`. If you promote too early, you've just added a stale node to your quorum and extended your write latency.

The learner state is itself a Raft optimization: learners receive log entries but don't count toward quorum. This lets you spin up a new replica, stream a snapshot to it, and replay the WAL without ever blocking your write path. Only once it has caught up — confirmed by the leader's `matchIndex` tracking — is it safe to promote. etcd exposes this as `etcdctl member add --learner`.

## Production Failure Modes You Will Actually Encounter

**Split-brain during asymmetric partition**: Node A can reach B but not C; C can reach B but not A. B receives `RequestVote` from both A and C when both time out. Only one gets the vote — the other keeps retrying with incrementing terms. Meanwhile, clients hitting the partitioned minority see request timeouts, not incorrect data. Raft's safety guarantee holds; your SLO doesn't.

**Leader churn from CPU starvation**: A Go GC pause on the leader node of 400 ms is enough to trigger an election with etcd's default 1000 ms timeout if the pause delays heartbeats. The new leader starts serving, the old leader recovers from GC, discovers it has a stale term, steps down. During those 300–500 ms of election, your writes are blocked. Fix: reduce heap pressure on etcd nodes (dedicated machines, `GOGC=25` or lower, or use etcd's built-in memory accounting).

**Snapshot storm on restart**: A node that has been down for hours comes back with a log 500k entries behind. The leader immediately starts sending a snapshot. Snapshot generation is CPU and memory intensive — etcd serializes the entire bbolt database. On a 10 GB etcd dataset, this can take 30+ seconds and spike memory by 2x. If you restart multiple followers simultaneously, you serialize snapshot sends, compounding the recovery time. Restart followers one at a time.

**Disk full on a follower**: The follower stops fsync()ing WAL entries, stops ACKing `AppendEntries`. The leader's in-flight log buffer grows. After `--max-recv-msg-size` bytes, the gRPC stream is reset. etcd emits `failed to send message to` warnings, the follower is marked inactive, and eventually snapshot transfer is attempted. You have 15–30 minutes before the problem cascades depending on write rate. Alert on disk utilization, not just on follower health.

Understanding Raft at this depth means you can look at an etcd metrics dashboard — `etcd_server_leader_changes_seen_total`, `etcd_disk_wal_fsync_duration_seconds`, `etcd_network_peer_round_trip_time_seconds` — and map each anomaly to a specific protocol behavior. The algorithm is not magic; it is a precise sequence of RPCs, term comparisons, and quorum calculations. Read the source in `go.etcd.io/raft`, specifically `raft.go` and `log.go`. Every function name maps directly to a concept from the paper. That is by design.
