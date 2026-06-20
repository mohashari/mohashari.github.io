---
layout: post
title: "Implementing the Raft Consensus Protocol: Handling Joint Consensus and Membership Changes"
date: 2026-06-20 08:00:00 +0700
tags: [raft, distributed-systems, consensus, golang]
description: "A production-focused implementation guide to handling joint consensus transitions, membership changes, and split-brain safety in Raft."
image: "https://picsum.photos/seed/raftjoint/1080/720"
thumbnail: "https://picsum.photos/seed/raftjoint/400/300"
---

It is 3:00 AM, and your automated infrastructure is scaling out an etcd cluster from three nodes to five nodes to accommodate a peak holiday traffic surge. In a naive implementation of distributed membership changes, updating the active configuration list in a single step creates a catastrophic window of vulnerability. For a brief moment, two disjoint majorities form concurrently: the remaining two nodes of the old three-node cluster believe a quorum is two, while the newly spawned nodes, executing under the five-node configuration, believe a quorum is three. Both factions independently elect leaders, accept conflicting client writes, and commit them to their state machines. The result is a silent, unrecoverable split-brain database corruption that compromises your transactional invariants. To solve this in production, you must implement Raft’s **Joint Consensus** or strict single-server membership changes. This guide walks through a production-ready Go implementation of Joint Consensus, detailing configuration state transitions, execution safety rules, and leader step-down invariants.

![Implementing the Raft Consensus Protocol: Handling Joint Consensus and Membership Changes Diagram](/images/diagrams/raft-consensus-joint-consensus-membership-changes.svg)

## The Core Dilemma: Naive Membership Changes and Disjoint Majorities

In distributed systems, consensus is governed by the overlap of majorities. If a cluster contains a set of servers $C_{\text{old}}$, any decision (such as committing a log entry) requires the agreement of a majority of those servers. If you attempt to transition the cluster directly to a new set of servers $C_{\text{new}}$ by simply writing the new configuration to a log entry, you violate the core assumption that only a single leader can make decisions for any given term. 

To visualize this, consider a three-node cluster $C_{\text{old}} = \{A, B, C\}$. A quorum in this cluster is any subset of size 2. We want to expand this cluster to a five-node configuration $C_{\text{new}} = \{A, B, C, D, E\}$, where the quorum size increases to 3. If the leader, Node $A$, simply appends the new configuration $C_{\text{new}}$ and broadcasts it, nodes will receive and apply this configuration at different times due to network latency, packet loss, or CPU scheduling delays.

Suppose Node $C$ is temporarily partitioned. Nodes $A$ and $B$ receive the new configuration entry, transition their state to $C_{\text{new}}$, and now require a quorum of 3 nodes. Together with the newly joined Node $D$, they can form a quorum of 3 $(\{A, B, D\})$ and elect a leader or commit entries under $C_{\text{new}}$. Simultaneously, Node $C$, which has not received the configuration change entry, still operates under $C_{\text{old}}$. Node $C$ only requires a quorum of 2. If it recovers from its partition and connects to Node $B$ (which it can persuade to vote for it if Node $B$ has not advanced its term), Node $C$ and Node $B$ can form a majority of 2 under $C_{\text{old}}$ and elect Node $C$ as leader. 

You now have two leaders—Node $A$ leading a quorum of $\{A, B, D\}$ under $C_{\text{new}}$, and Node $C$ leading a quorum of $\{B, C\}$ under $C_{\text{old}}$. Both leaders will accept writes, causing the state machines to diverge permanently. The fundamental issue is that **there is no single point in time where the configuration switch occurs across all nodes simultaneously**.

## Joint Consensus: The Two-Phase Transition Protocol

To prevent disjoint majorities, Raft uses a two-phase configuration transition called **Joint Consensus**. Instead of transitioning directly from $C_{\text{old}}$ to $C_{\text{new}}$, the leader first transitions the cluster to a temporary configuration called $C_{\text{old,new}}$ (the joint configuration). 

The key safety invariant of Joint Consensus is: **any decision (e.g., committing log entries, electing leaders) during the joint consensus phase requires independent majorities from both $C_{\text{old}}$ and $C_{\text{new}}$ configurations separately**.

The transition lifecycle proceeds as follows:
1. **Initiate Transition**: The leader receives a configuration change request to migrate from $C_{\text{old}}$ to $C_{\text{new}}$.
2. **Propose Joint Configuration**: The leader appends a configuration entry $C_{\text{old,new}}$ to its log and replicates it to all nodes in both configurations.
3. **Apply Joint Configuration Locally**: As soon as *any* node (leader or follower) appends the $C_{\text{old,new}}$ entry to its local log, it must immediately begin using this configuration for all future quorum calculations. It does not wait for the entry to be committed.
4. **Commit Joint Configuration**: Once the $C_{\text{old,new}}$ entry is replicated to a majority of $C_{\text{old}}$ AND a majority of $C_{\text{new}}$, the leader advances its `commitIndex` past the $C_{\text{old,new}}$ entry. At this point, the transition to joint consensus is committed. No node can elect a leader or commit writes without satisfying the dual-majority rule.
5. **Propose New Configuration**: Once $C_{\text{old,new}}$ is committed, the leader appends a new configuration entry $C_{\text{new}}$ to its log and replicates it.
6. **Apply and Commit New Configuration**: As nodes receive the $C_{\text{new}}$ log entry, they immediately switch to using $C_{\text{new}}$ locally. Once $C_{\text{new}}$ is replicated to a majority of $C_{\text{new}}$, the leader commits it.
7. **Retirement**: Any nodes that were in $C_{\text{old}}$ but are not in $C_{\text{new}}$ can now be safely shut down.

Let us implement this configuration state representation and quorum tracking logic in Go.

<script src="https://gist.github.com/mohashari/174b240dfd98d24c79ea53adc68ed25c.js?file=snippet-1.go"></script>

## Log Replication & Immediate Configuration Application

A critical, non-obvious rule in Raft is that **a server must begin using a configuration change entry as soon as it appends it to its log**, rather than waiting for the entry to be committed. 

If followers waited for configuration entries to be committed before updating their local configuration, the cluster could deadlock. For example, if the leader attempts to transition from 3 nodes to 5, and it requires a majority of 5 nodes to commit the configuration change entry, but the followers still think the cluster has 3 nodes, the leader and followers will disagree on the quorum calculations needed to commit the very entry that establishes the new quorum. 

This immediate application rule creates a dependency loop that is resolved by treating the configuration as a stream of log-driven state updates. When a follower parses its log during an `AppendEntries` RPC, it checks for configuration entries and applies them immediately. If that entry is later overwritten (which can only happen if the entry was not committed and a new leader is elected), the follower will revert its configuration to match the new leader’s log.

<script src="https://gist.github.com/mohashari/174b240dfd98d24c79ea53adc68ed25c.js?file=snippet-2.go"></script>

## The Leader Step-Down Invariant

A complex edge case in Joint Consensus occurs when the leader executing the membership change is not part of the target configuration $C_{\text{new}}$. 

During the joint consensus phase ($C_{\text{old,new}}$), the leader must continue to manage replication and coordinate consensus because it is still a member of $C_{\text{old}}$ (and $C_{\text{old,new}}$). It coordinates replication, updates the commit index, and appends the $C_{\text{new}}$ configuration log entry. 

However, once the $C_{\text{new}}$ configuration entry is successfully **committed**, the leader's active configuration transitions entirely to $C_{\text{new}}$. Because the leader itself is not a member of $C_{\text{new}}$, it has no voting rights and cannot count toward the quorum under $C_{\text{new}}$. 

The leader must not step down immediately when it proposes or appends the $C_{\text{new}}$ entry. Doing so would prevent it from replicating the entry and managing the commit process. Instead, **the leader must step down to a follower state exactly when the $C_{\text{new}}$ log entry is committed**.

<script src="https://gist.github.com/mohashari/174b240dfd98d24c79ea53adc68ed25c.js?file=snippet-3.go"></script>

## Preventing Disruptive Candidates with Pre-Vote

One of the most persistent operational failure modes in membership changes is the **disruptive server problem**. 

When a server is removed from a cluster (either during a single-node removal or at the completion of a Joint Consensus transition to $C_{\text{new}}$), it stops receiving heartbeats from the leader. Since its election timer continues to run, the removed server will eventually time out, increment its term, transition to candidate status, and broadcast `RequestVote` RPCs to the remaining cluster. 

Under standard Raft rules, when the active leader receives a `RequestVote` carrying a term higher than its own, it must immediately step down to a follower and update its term. This forces a disruption: the remaining nodes must elect a new leader, and all client writes are blocked during the election window. The removed server, receiving no votes, times out again, increments its term further, and repeats the cycle, causing continuous leader disruption.

To prevent this, you should implement the **Pre-Vote** phase. Pre-Vote acts as a dry-run election. Before a node increments its term and enters the candidate state, it transitions to a **Pre-Candidate** state and sends a `PreVote` RPC to its peers. Peers will only vote "yes" if:
1. The sender's log is at least as up-to-date as the receiver's log.
2. The receiver has not heard from a valid leader within the minimum election timeout window (meaning the leader has likely failed).

If a Pre-Candidate fails to obtain a majority of Pre-Votes, it does not increment its term and does not disrupt the active leader.

<script src="https://gist.github.com/mohashari/174b240dfd98d24c79ea53adc68ed25c.js?file=snippet-4.go"></script>

## Operationalizing Membership Changes: Learner Nodes & Promotion Safety

Directly adding a new voter to a Raft cluster introduces a significant operational risk. 

For instance, if you add Node $D$ to a healthy three-node cluster $\{A, B, C\}$, the quorum size increases from 2 to 3. If Node $D$ has just booted up, its database is empty. It must download a full snapshot and replay the write-ahead log (WAL). 

During this catch-up phase, the cluster operates with a quorum requirement of 3, but only 3 nodes ($\{A, B, C\}$) are active and synchronized. If any node in $\{A, B, C\}$ fails or undergoes a network hiccup, the cluster loses consensus capability (only 2 active nodes remain, failing the quorum of 3). The cluster's availability is compromised until Node $D$ finishes catching up.

To solve this, modern implementations use **Learners** (or non-voting members). Learners join the replication group, receive log entries, and download snapshots from the leader, but **they do not vote, do not run for election, and do not count toward quorum calculations**. 

The leader tracks the replication progress of each learner. Once a learner's `matchIndex` is within a small, configurable threshold of the leader's `lastLogIndex`, the leader can safely propose a configuration transition to promote the learner to a full voting member.

<script src="https://gist.github.com/mohashari/174b240dfd98d24c79ea53adc68ed25c.js?file=snippet-5.go"></script>

## Production Failure Modes and Diagnostics

When running a custom Raft implementation in production under heavy write load (e.g., thousands of transactions per second writing to NVMe SSDs), membership changes expose several critical failure modes that must be monitored and handled.

### 1. The Snapshot Storm on New Voter Promotion
If you add a learner or promote a voter without ensuring disk space and network limits, the snapshot synchronization process can saturate the leader's outbound network interface. 
* **Failure Mode**: The leader attempts to stream a 50 GB snapshot to a new follower. The streaming takes 3 minutes, during which the leader's TCP stack is saturated. Heartbeats to other followers are delayed, triggering election timeouts. The cluster enters an election storm, dropping client requests.
* **Mitigation**: You must rate-limit the snapshot transmission stream (e.g., using token bucket limiters to cap output at 50 MB/s). Additionally, prioritize heartbeat messages by sending them over a dedicated TCP connection or using HTTP/2 prioritization schemes.

### 2. Cascading Failures during Single-Node Failures
In a 3-node cluster, a single failure reduces your online pool to 2 nodes. If you attempt to run a membership change to remove the failed node while the cluster is degraded:
* **Failure Mode**: You execute `member remove` on the failed node. Quorum size remains 2. If the remaining follower has a temporary network partition during the configuration append write path, the leader cannot commit the configuration entry. The configuration change blocks indefinitely.
* **Mitigation**: Never initiate configuration changes when the cluster is operating without a safe majority. If a node is permanently lost, you must force a configuration override via a recovery tool (like `etcdctl member remove` or manual WAL manipulation), which overrides the in-memory cluster layout without consensus.

### 3. Asymmetric Partitions and Log Compaction Deadlocks
During a joint consensus phase, nodes in $C_{\text{old}}$ and $C_{\text{new}}$ must both reach a quorum.
* **Failure Mode**: If an asymmetric partition occurs where the new nodes ($C_{\text{new}}$) can reach the leader, but the old nodes ($C_{\text{old}}$) cannot, the leader will successfully replicate configuration logs to the new nodes but fail to reach a quorum on the old nodes. Because $C_{\text{old}}$ cannot reach a majority, the joint configuration $C_{\text{old,new}}$ can never be committed. The leader continues to append normal write entries to its log, which cannot be committed, leading to memory pressure on the leader.
* **Mitigation**: Implement strict backpressure on client writes when configuration changes are pending. If a configuration entry is not committed within a timeout window (e.g., 5 seconds), abort the membership change, truncate the uncommitted config entry, and roll back to the previous configuration.

## Diagnostic Metrics to Alert On
To operate a Raft cluster safely, expose the following Prometheus metrics and configure alerts with tight thresholds:
* `raft_log_lag_entries`: Tracks the distance between the leader's `lastLogIndex` and each peer's `matchIndex`. Alert if a learner's lag exceeds 10,000 entries for more than 5 minutes.
* `raft_config_transition_duration_seconds`: Measures the time spent in the `IsJoint` state. Alert if this state persists for more than 10 seconds, which indicates that one of the quorums ($C_{\text{old}}$ or $C_{\text{new}}$) is degraded.
* `raft_prevote_rejections_total`: Counters the number of times a PreVote request is rejected due to active leader lease checks. A rising count indicates a disruptive, partitioned node attempting to rejoin the cluster.
* `raft_disk_wal_fsync_duration_seconds`: Monitored closely on followers. A spike in fsync latency above 50 ms directly delays the `AppendEntries` ACK path, stalling the entire replication pipeline.

By implementing Joint Consensus with immediate application, leader step-down invariants, Pre-Vote protection, and learner-based catch-up tracking, you ensure that your consensus layer remains robust, resilient, and correct during topology changes.