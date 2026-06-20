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

```go
// snippet-1
package raft

import (
	"encoding/json"
	"fmt"
)

type ServerID string

// Config represents the membership configuration of the Raft cluster.
type Config struct {
	// Old represents the set of nodes in the active configuration (C_old).
	Old map[ServerID]struct{}
	// New represents the target set of nodes during a joint consensus transition (C_new).
	New map[ServerID]struct{}
	// IsJoint is true if the cluster is currently in the joint consensus phase (C_old,new).
	IsJoint bool
}

// Clone creates a deep copy of the configuration.
func (c *Config) Clone() Config {
	cloned := Config{
		Old:     make(map[ServerID]struct{}, len(c.Old)),
		New:     make(map[ServerID]struct{}, len(c.New)),
		IsJoint: c.IsJoint,
	}
	for k := range c.Old {
		cloned.Old[k] = struct{}{}
	}
	for k := range c.New {
		cloned.New[k] = struct{}{}
	}
	return cloned
}

// IsCommitted checks if a target log index has reached a quorum under this configuration.
// The matchIndex map contains the highest log index successfully replicated on each server.
func (c *Config) IsCommitted(target uint64, matchIndex map[ServerID]uint64, leaderID ServerID) bool {
	// Synthesize match indices including the leader's own progress.
	matches := make(map[ServerID]uint64, len(matchIndex)+1)
	for id, idx := range matchIndex {
		matches[id] = idx
	}
	// The leader always has the maximum possible match index for the purpose of quorum checks.
	matches[leaderID] = ^uint64(0)

	if !c.IsJoint {
		return c.hasMajority(c.Old, matches, target)
	}

	// Under Joint Consensus, safety dictates that we MUST satisfy majorities
	// of both C_old and C_new independently.
	return c.hasMajority(c.Old, matches, target) && c.hasMajority(c.New, matches, target)
}

func (c *Config) hasMajority(nodes map[ServerID]struct{}, matches map[ServerID]uint64, target uint64) bool {
	if len(nodes) == 0 {
		return true
	}
	count := 0
	for node := range nodes {
		if idx, ok := matches[node]; ok && idx >= target {
			count++
		}
	}
	requiredQuorum := (len(nodes) / 2) + 1
	return count >= requiredQuorum
}
```

## Log Replication & Immediate Configuration Application

A critical, non-obvious rule in Raft is that **a server must begin using a configuration change entry as soon as it appends it to its log**, rather than waiting for the entry to be committed. 

If followers waited for configuration entries to be committed before updating their local configuration, the cluster could deadlock. For example, if the leader attempts to transition from 3 nodes to 5, and it requires a majority of 5 nodes to commit the configuration change entry, but the followers still think the cluster has 3 nodes, the leader and followers will disagree on the quorum calculations needed to commit the very entry that establishes the new quorum. 

This immediate application rule creates a dependency loop that is resolved by treating the configuration as a stream of log-driven state updates. When a follower parses its log during an `AppendEntries` RPC, it checks for configuration entries and applies them immediately. If that entry is later overwritten (which can only happen if the entry was not committed and a new leader is elected), the follower will revert its configuration to match the new leader’s log.

```go
// snippet-2
package raft

import (
	"encoding/json"
	"fmt"
)

type EntryType uint8

const (
	EntryNormal EntryType = iota
	EntryConfig
)

type LogEntry struct {
	Index uint64
	Term  uint64
	Type  EntryType
	Data  []byte
}

type RaftNode struct {
	id          ServerID
	currentTerm uint64
	log         []LogEntry
	config      Config
	commitIndex uint64
	// Peer routing table, database handles, and network connections
	peers map[ServerID]struct{}
}

// AppendEntries handles log replication RPCs sent by the leader.
func (rn *RaftNode) AppendEntries(leaderTerm uint64, prevLogIndex uint64, prevLogTerm uint64, entries []LogEntry, leaderCommit uint64) (uint64, bool) {
	// 1. Term check: reject RPC if leader's term is older than our term
	if leaderTerm < rn.currentTerm {
		return rn.currentTerm, false
	}

	// 2. Log consistency check: verify log matches at prevLogIndex and prevLogTerm
	if prevLogIndex > 0 {
		if prevLogIndex >= uint64(len(rn.log)) || rn.log[prevLogIndex].Term != prevLogTerm {
			return rn.currentTerm, false
		}
	}

	// 3. Process new entries and apply configuration updates immediately
	for i, entry := range entries {
		idx := entry.Index
		
		// If there is an existing entry that conflicts with a new one (same index but different term),
		// truncate the log and all following entries.
		if idx < uint64(len(rn.log)) {
			if rn.log[idx].Term != entry.Term {
				rn.log = rn.log[:idx]
				// Re-evaluate configuration if we truncated configuration entries.
				rn.revertToLastValidConfig()
			}
		}

		// Append the new entry
		if idx == uint64(len(rn.log)) {
			rn.log = append(rn.log, entry)
		} else {
			rn.log[idx] = entry
		}

		// Immediate application rule: apply the configuration as soon as it is appended.
		if entry.Type == EntryConfig {
			var parsedCfg Config
			if err := json.Unmarshal(entry.Data, &parsedCfg); err != nil {
				panic(fmt.Sprintf("[Node %s] Corrupt config entry data at index %d: %v", rn.id, entry.Index, err))
			}
			rn.config = parsedCfg
			rn.rebuildRoutingTable()
		}
	}

	// 4. Update commit index
	if leaderCommit > rn.commitIndex {
		rn.commitIndex = min(leaderCommit, uint64(len(rn.log)-1))
	}

	return rn.currentTerm, true
}

func (rn *RaftNode) revertToLastValidConfig() {
	// Scan backward in the log to find the last configuration entry, 
	// and restore rn.config to that state.
	for i := len(rn.log) - 1; i >= 0; i-- {
		if rn.log[i].Type == EntryConfig {
			var parsedCfg Config
			_ = json.Unmarshal(rn.log[i].Data, &parsedCfg)
			rn.config = parsedCfg
			rn.rebuildRoutingTable()
			return
		}
	}
	// Fallback to bootstrap configuration if no config entry remains in the log.
}

func (rn *RaftNode) rebuildRoutingTable() {
	rn.peers = make(map[ServerID]struct{})
	for id := range rn.config.Old {
		if id != rn.id {
			rn.peers[id] = struct{}{}
		}
	}
	for id := range rn.config.New {
		if id != rn.id {
			rn.peers[id] = struct{}{}
		}
	}
}

func min(a, b uint64) uint64 {
	if a < b {
		return a
	}
	return b
}
```

## The Leader Step-Down Invariant

A complex edge case in Joint Consensus occurs when the leader executing the membership change is not part of the target configuration $C_{\text{new}}$. 

During the joint consensus phase ($C_{\text{old,new}}$), the leader must continue to manage replication and coordinate consensus because it is still a member of $C_{\text{old}}$ (and $C_{\text{old,new}}$). It coordinates replication, updates the commit index, and appends the $C_{\text{new}}$ configuration log entry. 

However, once the $C_{\text{new}}$ configuration entry is successfully **committed**, the leader's active configuration transitions entirely to $C_{\text{new}}$. Because the leader itself is not a member of $C_{\text{new}}$, it has no voting rights and cannot count toward the quorum under $C_{\text{new}}$. 

The leader must not step down immediately when it proposes or appends the $C_{\text{new}}$ entry. Doing so would prevent it from replicating the entry and managing the commit process. Instead, **the leader must step down to a follower state exactly when the $C_{\text{new}}$ log entry is committed**.

```go
// snippet-3
package raft

import (
	"log"
)

type Role int

const (
	Follower Role = iota
	Candidate
	Leader
)

type ConsensusModule struct {
	id          ServerID
	role        Role
	config      Config
	matchIndex  map[ServerID]uint64
	commitIndex uint64
	currentTerm uint64
	log         []LogEntry
}

// AdvanceCommitIndex evaluates the matchIndex of all peers and updates the commitIndex.
// It also checks for membership configuration commits and executes step-down invariants.
func (cm *ConsensusModule) AdvanceCommitIndex(lastLogIndex uint64) {
	if cm.role != Leader {
		return
	}

	highestCommitted := cm.commitIndex
	for idx := lastLogIndex; idx > cm.commitIndex; idx-- {
		// Safety rule: a leader cannot commit log entries from previous terms by counting replicas.
		// It must commit an entry from its current term to indirectly commit previous entries.
		if cm.log[idx].Term == cm.currentTerm {
			if cm.config.IsCommitted(idx, cm.matchIndex, cm.id) {
				highestCommitted = idx
				break
			}
		}
	}

	if highestCommitted > cm.commitIndex {
		cm.commitIndex = highestCommitted
		log.Printf("[Node %s] commitIndex advanced to %d", cm.id, cm.commitIndex)

		// Check if we committed a configuration entry
		cm.handleConfigCommit(highestCommitted)
	}
}

func (cm *ConsensusModule) handleConfigCommit(committedIndex uint64) {
	// Find the configuration entry in the log that matches this index.
	if committedIndex >= uint64(len(cm.log)) {
		return
	}
	
	entry := cm.log[committedIndex]
	if entry.Type != EntryConfig {
		return
	}

	var committedConfig Config
	if err := json.Unmarshal(entry.Data, &committedConfig); err != nil {
		log.Printf("Error unmarshalling config at index %d: %v", committedIndex, err)
		return
	}

	if committedConfig.IsJoint {
		// Phase 1 Transition Complete: C_old,new is committed.
		// The leader now immediately proposes the final transition entry C_new.
		log.Printf("[Node %s] C_old,new committed. Proposing C_new...", cm.id)
		
		cNew := Config{
			Old:     committedConfig.New, // Shift new configuration to primary slot
			New:     nil,
			IsJoint: false,
		}
		
		data, _ := json.Marshal(cNew)
		newEntry := LogEntry{
			Index: uint64(len(cm.log)),
			Term:  cm.currentTerm,
			Type:  EntryConfig,
			Data:  data,
		}
		
		cm.log = append(cm.log, newEntry)
		cm.config = cNew
		// Force immediate replication to peers
		cm.triggerReplication()
	} else {
		// Phase 2 Transition Complete: C_new is committed.
		// If the leader is not a member of C_new, it must step down immediately.
		_, inNewConfig := committedConfig.Old[cm.id] // committedConfig.Old represents C_new (as shifted above)
		if !inNewConfig {
			log.Printf("[Node %s] Leader is not part of the committed C_new configuration. Stepping down...", cm.id)
			cm.role = Follower
			cm.currentTerm = cm.log[committedIndex].Term // Ensure alignment
			cm.persistState()
			cm.resetElectionTimer()
		}
	}
}

func (cm *ConsensusModule) triggerReplication() {
	// Sends parallel AppendEntries RPCs to all peers in the active configuration
}

func (cm *ConsensusModule) persistState() {}
func (cm *ConsensusModule) resetElectionTimer() {}
```

## Preventing Disruptive Candidates with Pre-Vote

One of the most persistent operational failure modes in membership changes is the **disruptive server problem**. 

When a server is removed from a cluster (either during a single-node removal or at the completion of a Joint Consensus transition to $C_{\text{new}}$), it stops receiving heartbeats from the leader. Since its election timer continues to run, the removed server will eventually time out, increment its term, transition to candidate status, and broadcast `RequestVote` RPCs to the remaining cluster. 

Under standard Raft rules, when the active leader receives a `RequestVote` carrying a term higher than its own, it must immediately step down to a follower and update its term. This forces a disruption: the remaining nodes must elect a new leader, and all client writes are blocked during the election window. The removed server, receiving no votes, times out again, increments its term further, and repeats the cycle, causing continuous leader disruption.

To prevent this, you should implement the **Pre-Vote** phase. Pre-Vote acts as a dry-run election. Before a node increments its term and enters the candidate state, it transitions to a **Pre-Candidate** state and sends a `PreVote` RPC to its peers. Peers will only vote "yes" if:
1. The sender's log is at least as up-to-date as the receiver's log.
2. The receiver has not heard from a valid leader within the minimum election timeout window (meaning the leader has likely failed).

If a Pre-Candidate fails to obtain a majority of Pre-Votes, it does not increment its term and does not disrupt the active leader.

```go
// snippet-4
package raft

import (
	"context"
	"time"
)

type PreVoteRequest struct {
	CandidateID  ServerID
	Term         uint64
	LastLogIndex uint64
	LastLogTerm  uint64
}

type PreVoteResponse struct {
	Term        uint64
	VoteGranted bool
}

type Server struct {
	id                 ServerID
	currentTerm        uint64
	lastLeaderContact  time.Time
	minElectionTimeout time.Duration
	log                []LogEntry
}

// HandlePreVote evaluates an incoming Pre-Vote request.
func (s *Server) HandlePreVote(ctx context.Context, req *PreVoteRequest) (*PreVoteResponse, error) {
	// Rule 1: Ignore Pre-Vote if the candidate's term is older than ours
	if req.Term < s.currentTerm {
		return &PreVoteResponse{Term: s.currentTerm, VoteGranted: false}, nil
	}

	// Rule 2: Disruptive Server Protection
	// If we have heard from a valid leader within the minimum election timeout,
	// we reject the vote. The leader is active, so the candidate must be partitioned or removed.
	if time.Since(s.lastLeaderContact) < s.minElectionTimeout {
		return &PreVoteResponse{
			Term:        s.currentTerm,
			VoteGranted: false,
		}, nil
	}

	// Rule 3: Check log up-to-dateness
	lastIdx := uint64(len(s.log) - 1)
	lastTerm := uint64(0)
	if len(s.log) > 0 {
		lastTerm = s.log[lastIdx].Term
	}

	logOk := false
	if req.LastLogTerm > lastTerm {
		logOk = true
	} else if req.LastLogTerm == lastTerm && req.LastLogIndex >= lastIdx {
		logOk = true
	}

	if !logOk {
		return &PreVoteResponse{Term: s.currentTerm, VoteGranted: false}, nil
	}

	return &PreVoteResponse{
		Term:        s.currentTerm,
		VoteGranted: true,
	}, nil
}
```

## Operationalizing Membership Changes: Learner Nodes & Promotion Safety

Directly adding a new voter to a Raft cluster introduces a significant operational risk. 

For instance, if you add Node $D$ to a healthy three-node cluster $\{A, B, C\}$, the quorum size increases from 2 to 3. If Node $D$ has just booted up, its database is empty. It must download a full snapshot and replay the write-ahead log (WAL). 

During this catch-up phase, the cluster operates with a quorum requirement of 3, but only 3 nodes ($\{A, B, C\}$) are active and synchronized. If any node in $\{A, B, C\}$ fails or undergoes a network hiccup, the cluster loses consensus capability (only 2 active nodes remain, failing the quorum of 3). The cluster's availability is compromised until Node $D$ finishes catching up.

To solve this, modern implementations use **Learners** (or non-voting members). Learners join the replication group, receive log entries, and download snapshots from the leader, but **they do not vote, do not run for election, and do not count toward quorum calculations**. 

The leader tracks the replication progress of each learner. Once a learner's `matchIndex` is within a small, configurable threshold of the leader's `lastLogIndex`, the leader can safely propose a configuration transition to promote the learner to a full voting member.

```go
// snippet-5
package raft

import (
	"errors"
	"fmt"
	"sync"
)

type LearnerTracker struct {
	mu           sync.RWMutex
	matchIndex   map[ServerID]uint64
	leaderLast   uint64
	maxLagAllowed uint64
}

func NewLearnerTracker(maxLag uint64) *LearnerTracker {
	return &LearnerTracker{
		matchIndex:    make(map[ServerID]uint64),
		maxLagAllowed: maxLag,
	}
}

// TrackProgress updates the match index for a learner node and the leader's tail.
func (lt *LearnerTracker) TrackProgress(id ServerID, matchIdx uint64, leaderLastIdx uint64) {
	lt.mu.Lock()
	defer lt.mu.Unlock()
	lt.matchIndex[id] = matchIdx
	lt.leaderLast = leaderLastIdx
}

// CheckPromotionSafety verifies if the learner is caught up enough to be promoted to a voting member.
func (lt *LearnerTracker) CheckPromotionSafety(id ServerID) (bool, error) {
	lt.mu.RLock()
	defer lt.mu.RUnlock()

	matchIdx, exists := lt.matchIndex[id]
	if !exists {
		return false, fmt.Errorf("node %s is not registered as a learner", id)
	}

	if lt.leaderLast < matchIdx {
		return true, nil // Sanitization check
	}

	lag := lt.leaderLast - matchIdx
	if lag <= lt.maxLagAllowed {
		return true, nil
	}

	return false, fmt.Errorf("node %s lag (%d entries) exceeds maximum allowable threshold (%d)", id, lag, lt.maxLagAllowed)
}

// PromoteLearner generates a configuration change entry that shifts the learner to a voter.
func (cm *ConsensusModule) PromoteLearner(tracker *LearnerTracker, id ServerID) error {
	if cm.role != Leader {
		return errors.New("only the leader can propose configuration changes")
	}

	if cm.config.IsJoint {
		return errors.New("cannot promote nodes while a joint consensus transition is in progress")
	}

	ok, err := tracker.CheckPromotionSafety(id)
	if !ok || err != nil {
		return fmt.Errorf("promotion rejected: %w", err)
	}

	// Construct the new configuration with the learner promoted to voter
	newConfig := cm.config.Clone()
	if _, alreadyVoter := newConfig.Old[id]; alreadyVoter {
		return fmt.Errorf("node %s is already a voting member", id)
	}

	// Add voter to C_new
	newConfig.New = make(map[ServerID]struct{}, len(newConfig.Old)+1)
	for k := range newConfig.Old {
		newConfig.New[k] = struct{}{}
	}
	newConfig.New[id] = struct{}{}
	newConfig.IsJoint = true // Initiate Joint Consensus (Phase 1)

	configData, _ := json.Marshal(newConfig)
	entry := LogEntry{
		Index: uint64(len(cm.log)),
		Term:  cm.currentTerm,
		Type:  EntryConfig,
		Data:  configData,
	}

	cm.log = append(cm.log, entry)
	cm.config = newConfig
	cm.triggerReplication()

	return nil
}
```

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
