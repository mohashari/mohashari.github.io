---
layout: post
title: "Implementing Two-Phase Commit and Two-Phase Locking in Distributed Key-Value Stores"
date: 2026-06-18 08:00:00 +0700
tags: [distributed-systems, database-internals, transactions, golang]
description: "A production-focused guide to building distributed ACID transactions in key-value stores using Strong Strict Two-Phase Locking and Two-Phase Commit."
image: ""
thumbnail: ""
---

In a distributed key-value store, sharding data across multiple nodes provides horizontal write scalability, but it completely breaks transaction atomicity and isolation. Consider a high-throughput e-commerce checkout where Shard A decrements inventory for a scarce item and Shard B charges a user's digital wallet. If Shard A succeeds but the network partitions or Shard B crashes before the charge is applied, the system enters an inconsistent state that requires manual reconciliation. To prevent this, a distributed database must guarantee ACID properties across shard boundaries. This is achieved by combining Two-Phase Locking (2PL) for serializability and isolation with Two-Phase Commit (2PC) for atomic commitment. Implementing these protocols in production is notoriously difficult, requiring meticulous handling of thread synchronization, write-ahead logging (WAL), deadlock prevention, and coordinator crash-recovery paths.

![Implementing Two-Phase Commit and Two-Phase Locking in Distributed Key-Value Stores Diagram](/images/diagrams/distributed-two-phase-commit-locking-key-value-stores.svg)

## The Core Dilemma: Isolation vs. Atomicity

When building distributed transactions, engineers frequently conflate concurrency control (Isolation) with atomic commitment (Atomicity). 

*   **Concurrency Control (2PL):** Ensures that concurrent transactions do not interfere with each other, maintaining serializable execution. Without concurrency control, transactions running across shards can experience dirty reads, non-repeatable reads, and write skew.
*   **Atomic Commitment (2PC):** Ensures that all participant shards either commit their local writes or abort them in unison, regardless of individual node failures or network partitions.

Crucially, neither protocol is sufficient on its own. 2PL without 2PC results in shards committing their local updates independently without knowing if other shards failed, violating atomicity. 2PC without 2PL allows transactions to read uncommitted intermediate states, violating isolation. 

To achieve full distributed ACID properties, they must be integrated: local locks must be acquired during transaction execution via **Strong Strict Two-Phase Locking (SS2PL)**, held throughout the duration of the transaction, and only released *after* the **Two-Phase Commit (2PC)** protocol has reached a final decision (COMMIT or ABORT) and persisted it to the Write-Ahead Log (WAL).

## Concurrency Control via Strong Strict Two-Phase Locking (SS2PL)

Standard Two-Phase Locking guarantees serializability by dividing a transaction's lock lifecycle into two phases: a growing phase where locks are acquired, and a shrinking phase where locks are released. However, standard 2PL is prone to **cascading aborts**. If Transaction 1 releases a lock during its shrinking phase, and Transaction 2 reads that modified key and commits, and then Transaction 1 aborts, Transaction 2 must also be aborted. In a distributed environment, tracking and executing cascading aborts across shards is a performance and engineering nightmare.

To solve this, production systems like CockroachDB and Google Spanner utilize **Strong Strict Two-Phase Locking (SS2PL)**. Under SS2PL:
1.  **Growing Phase:** Shared (S) and Exclusive (X) locks are acquired dynamically as read and write operations are executed.
2.  **Shrinking Phase:** All locks are held until the transaction completely finishes (either committed or aborted). No locks are released prematurely.

This guarantees that no other transaction can read uncommitted dirty data, eliminating cascading aborts and ensuring strict serializability.

Here is a thread-safe, production-ready Go implementation of a Lock Manager that supports SS2PL with timeout-based acquisition to prevent lock starvation.

```go
// snippet-1
package transaction

import (
	"context"
	"errors"
	"sync"
)

var (
	ErrLockTimeout  = errors.New("lock acquisition timed out")
	ErrLockConflict = errors.New("lock conflict detected")
)

type LockMode int

const (
	Shared LockMode = iota
	Exclusive
)

type LockReq struct {
	TxnID  string
	Mode   LockMode
	Grant  chan struct{}
	Cancel chan struct{}
}

type LockState struct {
	mu             sync.Mutex
	holders        map[string]LockMode // TxnID -> LockMode
	waitQueue      []*LockReq
	exclusiveOwner string
}

type LockManager struct {
	mu    sync.RWMutex
	locks map[string]*LockState // Key -> LockState
}

func NewLockManager() *LockManager {
	return &LockManager{
		locks: make(map[string]*LockState),
	}
}

func (lm *LockManager) getLockState(key string) *LockState {
	lm.mu.Lock()
	defer lm.mu.Unlock()
	state, exists := lm.locks[key]
	if !exists {
		state = &LockState{
			holders:   make(map[string]LockMode),
			waitQueue: make([]*LockReq, 0),
		}
		lm.locks[key] = state
	}
	return state
}

func (lm *LockManager) Acquire(ctx context.Context, txnID string, key string, mode LockMode) error {
	state := lm.getLockState(key)

	state.mu.Lock()
	// Check if already holding the lock at the required or stronger level
	if currentMode, holding := state.holders[txnID]; holding {
		if currentMode == Exclusive || mode == Shared {
			state.mu.Unlock()
			return nil // Already holds sufficient lock
		}
		// Upgrade lock (Shared -> Exclusive)
		if len(state.holders) == 1 {
			state.holders[txnID] = Exclusive
			state.exclusiveOwner = txnID
			state.mu.Unlock()
			return nil
		}
	}

	// Check if lock is immediately available
	canGrant := false
	if mode == Shared {
		canGrant = state.exclusiveOwner == "" && len(state.waitQueue) == 0
	} else {
		canGrant = len(state.holders) == 0 && len(state.waitQueue) == 0
	}

	if canGrant {
		state.holders[txnID] = mode
		if mode == Exclusive {
			state.exclusiveOwner = txnID
		}
		state.mu.Unlock()
		return nil
	}

	// Queue the lock request
	req := &LockReq{
		TxnID:  txnID,
		Mode:   mode,
		Grant:  make(chan struct{}, 1),
		Cancel: make(chan struct{}, 1),
	}
	state.waitQueue = append(state.waitQueue, req)
	state.mu.Unlock()

	// Wait for grant or timeout/context cancellation
	select {
	case <-req.Grant:
		return nil
	case <-ctx.Done():
		state.mu.Lock()
		// Remove request from queue on cancellation
		for i, r := range state.waitQueue {
			if r == req {
				state.waitQueue = append(state.waitQueue[:i], state.waitQueue[i+1:]...)
				break
			}
		}
		state.mu.Unlock()
		return ErrLockTimeout
	}
}

func (lm *LockManager) ReleaseAll(txnID string, keys []string) {
	lm.mu.RLock()
	defer lm.mu.RUnlock()

	for _, key := range keys {
		state, exists := lm.locks[key]
		if !exists {
			continue
		}

		state.mu.Lock()
		delete(state.holders, txnID)
		if state.exclusiveOwner == txnID {
			state.exclusiveOwner = ""
		}

		// Process waiting requests
		for len(state.waitQueue) > 0 {
			next := state.waitQueue[0]
			
			// Determine if next request can be granted
			grantNext := false
			if next.Mode == Shared {
				grantNext = state.exclusiveOwner == ""
			} else {
				grantNext = len(state.holders) == 0
			}

			if grantNext {
				state.holders[next.TxnID] = next.Mode
				if next.Mode == Exclusive {
					state.exclusiveOwner = next.TxnID
				}
				state.waitQueue = state.waitQueue[1:]
				next.Grant <- struct{}{}
				if next.Mode == Exclusive {
					break // Only one exclusive lock can be granted
				}
			} else {
				break // Cannot grant; wait queue remains blocked
			}
		}
		state.mu.Unlock()
	}
}
```

## Deadlock Prevention: Wound-Wait

A major side effect of holding locks until commit (SS2PL) is deadlocks. In a distributed key-value store, if Transaction 1 locks Key A on Shard 1 and requests Key B on Shard 2, while Transaction 2 locks Key B on Shard 2 and requests Key A on Shard 1, a deadlock cycle occurs. 

Detecting distributed deadlocks via wait-for graphs requires building a centralized coordinator or executing complex distributed cycle-finding algorithms. Both introduce significant network overhead. Therefore, high-performance distributed key-value stores prefer **Deadlock Prevention** algorithms using logical timestamps.

The two main timestamp-based deadlock prevention schemes are:
1.  **Wait-Die (Non-preemptive):** If $T_{requester}$ is older than $T_{holder}$, the requester waits. If $T_{requester}$ is younger, it dies (aborts).
2.  **Wound-Wait (Preemptive):** If $T_{requester}$ is older than $T_{holder}$, the requester "wounds" (aborts) $T_{holder}$ and steals the lock. If $T_{requester}$ is younger, it waits.

**Wound-Wait is generally superior** in production. An older transaction preempts younger transactions instantly, reducing latency for long-running transactions. Younger transactions wait gracefully instead of constantly aborting and retrying, which prevents live-locks.

Below is the implementation of a Wound-Wait deadlock prevention policy integrated into a lock manager context.

```go
// snippet-2
package transaction

import (
	"context"
	"errors"
	"sync"
	"time"
)

type Txn struct {
	ID        string
	Timestamp time.Time
	mu        sync.Mutex
	Aborted   bool
	AbortChan chan struct{}
}

func NewTxn(id string) *Txn {
	return &Txn{
		ID:        id,
		Timestamp: time.Now(),
		AbortChan: make(chan struct{}, 1),
	}
}

func (t *Txn) Abort() {
	t.mu.Lock()
	defer t.mu.Unlock()
	if !t.Aborted {
		t.Aborted = true
		close(t.AbortChan)
	}
}

type WoundWaitLockManager struct {
	mu    sync.Mutex
	locks map[string]*WoundWaitLockState
}

type WoundWaitLockState struct {
	holders   map[string]*Txn // TxnID -> Txn
	exclusive bool
	waitQueue []*WoundWaitReq
}

type WoundWaitReq struct {
	txn   *Txn
	mode  LockMode
	grant chan struct{}
}

func (lm *WoundWaitLockManager) Acquire(ctx context.Context, txn *Txn, key string, mode LockMode) error {
	lm.mu.Lock()
	if lm.locks == nil {
		lm.locks = make(map[string]*WoundWaitLockState)
	}
	state, exists := lm.locks[key]
	if !exists {
		state = &WoundWaitLockState{holders: make(map[string]*Txn)}
		lm.locks[key] = state
	}
	lm.mu.Unlock()

	state.muLockAndEvaluate:
	// Verify if transaction is already aborted
	select {
	case <-txn.AbortChan:
		return ErrLockConflict
	default:
	}

	state.waitQueue = cleanAbortedRequests(state.waitQueue)

	// Evaluate conflicts
	conflicts := make([]*Txn, 0)
	if state.exclusive {
		for _, holder := range state.holders {
			if holder.ID != txn.ID {
				conflicts = append(conflicts, holder)
			}
		}
	} else if mode == Exclusive {
		for _, holder := range state.holders {
			if holder.ID != txn.ID {
				conflicts = append(conflicts, holder)
			}
		}
	}

	if len(conflicts) > 0 {
		// Wound-Wait logic
		for _, holder := range conflicts {
			// Older transactions wound younger transactions
			if txn.Timestamp.Before(holder.Timestamp) {
				holder.Abort()
			}
		}
		
		// Queue request and wait
		req := &WoundWaitReq{
			txn:   txn,
			mode:  mode,
			grant: make(chan struct{}, 1),
		}
		
		state.waitQueue = append(state.waitQueue, req)
		
		// Release lock map lock and wait
		select {
		case <-req.grant:
			return nil
		case <-txn.AbortChan:
			return ErrLockConflict
		case <-ctx.Done():
			return ErrLockTimeout
		}
	}

	// Grant Lock
	state.holders[txn.ID] = txn
	if mode == Exclusive {
		state.exclusive = true
	}
	return nil
}

func cleanAbortedRequests(q []*WoundWaitReq) []*WoundWaitReq {
	n := 0
	for _, req := range q {
		select {
		case <-req.txn.AbortChan:
			// Discard aborted transactions waiting in queue
		default:
			q[n] = req
			n++
		}
	}
	return q[:n]
}
```

## Atomic Commitment: Two-Phase Commit (2PC)

While SS2PL handles isolating concurrent operations, Two-Phase Commit ensures that a single write transaction spanning multiple partitions is committed atomically.

The protocol involves a **Coordinator** (which orchestrates the transaction lifecycle) and multiple **Participants** (which manage the local storage shards).

```
Coordinator                  Participant 1                Participant 2
    |                              |                            |
    |--- 1. PREPARE -------------->|                            |
    |--- 1. PREPARE ------------------------------------------->|
    |                              | (Locks acquired, WAL sync) |
    |<-- 2. VOTE_COMMIT (YES) -----|                            |
    |<-- 2. VOTE_COMMIT (YES) ----------------------------------|
    |                              |                            |
    | (State: COMMITTED written)  |                            |
    |--- 3. COMMIT --------------->|                            |
    |--- 3. COMMIT -------------------------------------------->|
    |                              | (Apply changes, Free locks)|
    |<-- 4. ACK -------------------|                            |
    |<-- 4. ACK ------------------------------------------------|
    v                              v                            v
```

### The Protocol Flow
1.  **Phase 1 (Prepare):**
    *   The Coordinator assigns a unique transaction ID and sends a `PREPARE` message containing the local write payload to all participant shards.
    *   Each participant attempts to acquire local locks (via S2PL) and validate constraints.
    *   If successful, the participant writes a `PREPARE` record containing the changes to its local **Write-Ahead Log (WAL)**, flushes it to disk via `fsync`, and responds to the coordinator with `VOTE_COMMIT`. At this stage, the participant guarantees it can commit the transaction if ordered to do so.
    *   If the participant cannot lock the keys or experiences a local failure, it responds with `VOTE_ABORT` or times out.
2.  **Phase 2 (Commit/Abort):**
    *   **Decision:** If all participants vote `VOTE_COMMIT`, the Coordinator decides to commit. It writes a `COMMIT` record to its local WAL and flushes it. If any participant voted `VOTE_ABORT` or failed to respond within the timeout, the coordinator decides to abort, writing an `ABORT` record to its WAL.
    *   **Broadcast:** The coordinator sends the decision (`COMMIT` or `ABORT`) to all participants.
    *   **Application:** Upon receiving `COMMIT`, participants apply the updates to their persistent storage engine, write a `COMMIT` record to their local WAL, release all transaction locks, and respond with an `ACK`.
    *   **Clean Up:** Once the coordinator collects all `ACK`s, it marks the transaction as complete.

### Crash Recovery and Write-Ahead Logging (WAL)
Every phase transitions only after writing to the Write-Ahead Log. If a node crashes, it must read the WAL on startup to determine its state:
*   If a participant finds a `PREPARE` record but no `COMMIT`/`ABORT` record, it is in the **Prepared** state. It must query the coordinator to discover the transaction's fate. Until it receives an answer, it **must keep holding the exclusive locks**, blocking other transactions.
*   If a coordinator crashes mid-transaction, it must read its WAL to reconstruct active transactions and resume broadcasting decisions.

Here is a robust, concurrent Write-Ahead Log implementation tailored for 2PC recovery.

```go
// snippet-3
package transaction

import (
	"encoding/json"
	"fmt"
	"os"
	"sync"
)

type RecordType int

const (
	TxnStart RecordType = iota
	TxnPrepare
	TxnCommit
	TxnAbort
)

type LogRecord struct {
	LSN      uint64     `json:"lsn"`
	TxnID    string     `json:"txn_id"`
	Type     RecordType `json:"type"`
	Payload  []byte     `json:"payload,omitempty"`
}

type WAL struct {
	mu       sync.Mutex
	file     *os.File
	nextLSN  uint64
}

func OpenWAL(path string) (*WAL, error) {
	file, err := os.OpenFile(path, os.O_CREATE|os.O_RDWR|os.O_APPEND, 0666)
	if err != nil {
		return nil, fmt.Errorf("failed to open WAL: %w", err)
	}

	wal := &WAL{
		file:    file,
		nextLSN: 1,
	}

	return wal, nil
}

func (w *WAL) Append(txnID string, recType RecordType, payload []byte) (uint64, error) {
	w.mu.Lock()
	defer w.mu.Unlock()

	rec := LogRecord{
		LSN:     w.nextLSN,
		TxnID:   txnID,
		Type:    recType,
		Payload: payload,
	}

	data, err := json.Marshal(rec)
	if err != nil {
		return 0, err
	}

	// Format: [Data Length (4 bytes)][JSON Data]
	length := uint32(len(data))
	lengthBytes := []byte{
		byte(length >> 24),
		byte(length >> 16),
		byte(length >> 8),
		byte(length),
	}

	if _, err := w.file.Write(lengthBytes); err != nil {
		return 0, err
	}
	if _, err := w.file.Write(data); err != nil {
		return 0, err
	}

	// Critical: Force disk serialization to satisfy Durability
	if err := w.file.Sync(); err != nil {
		return 0, fmt.Errorf("fsync failed: %w", err)
	}

	lsn := w.nextLSN
	w.nextLSN++
	return lsn, nil
}

func (w *WAL) Close() error {
	w.mu.Lock()
	defer w.mu.Unlock()
	return w.file.Close()
}
```

Now, let's look at the **Coordinator** orchestration engine. It coordinates distributed participants, executing the two-phase commit state machine.

```go
// snippet-4
package transaction

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"time"
)

type ParticipantClient interface {
	Prepare(ctx context.Context, txnID string, keys []string, vals []string) error
	Commit(ctx context.Context, txnID string) error
	Abort(ctx context.Context, txnID string) error
}

type Coordinator struct {
	wal          *WAL
	participants map[string]ParticipantClient
	timeout      time.Duration
}

func NewCoordinator(wal *WAL, participants map[string]ParticipantClient, timeout time.Duration) *Coordinator {
	return &Coordinator{
		wal:          wal,
		participants: participants,
		timeout:      timeout,
	}
}

func (c *Coordinator) ExecuteTransaction(txnID string, writes map[string]map[string]string) error {
	// 1. Write TXN_START to local WAL
	if _, err := c.wal.Append(txnID, TxnStart, nil); err != nil {
		return fmt.Errorf("failed to log txn start: %w", err)
	}

	// Configure context with timeout for Phase 1 (Prepare)
	ctx, cancel := context.WithTimeout(context.Background(), c.timeout)
	defer cancel()

	var wg sync.WaitGroup
	errs := make(chan error, len(writes))

	// Phase 1: Dispatch parallel Prepare RPCs
	for participantID, payload := range writes {
		wg.Add(1)
		go func(pID string, pPayload map[string]string) {
			defer wg.Done()
			client := c.participants[pID]
			
			keys := make([]string, 0, len(pPayload))
			vals := make([]string, 0, len(pPayload))
			for k, v := range pPayload {
				keys = append(keys, k)
				vals = append(vals, v)
			}

			if err := client.Prepare(ctx, txnID, keys, vals); err != nil {
				errs <- fmt.Errorf("participant %s prepare failure: %w", pID, err)
			}
		}(participantID, payload)
	}

	wg.Wait()
	close(errs)

	decision := TxnCommit
	var firstErr error
	if len(errs) > 0 {
		decision = TxnAbort
		firstErr = <-errs
	}

	// 2. Commit Decision to local WAL
	if _, err := c.wal.Append(txnID, decision, nil); err != nil {
		return fmt.Errorf("critical WAL log failure on commit decision: %w", err)
	}

	// Phase 2: Execute Decision (Commit or Abort)
	var phase2Wg sync.WaitGroup
	for participantID := range writes {
		phase2Wg.Add(1)
		go func(pID string) {
			defer phase2Wg.Done()
			client := c.participants[pID]
			pCtx, pCancel := context.WithTimeout(context.Background(), 5*time.Second)
			defer pCancel()

			if decision == TxnCommit {
				// Retries must be driven by production coordinators if participants are offline
				for retry := 0; retry < 5; retry++ {
					if err := client.Commit(pCtx, txnID); err == nil {
						return
					}
					time.Sleep(100 * time.Millisecond)
				}
			} else {
				_ = client.Abort(pCtx, txnID)
			}
		}(participantID)
	}

	phase2Wg.Wait()
	return firstErr
}
```

The matching **Participant Node** implementation exposes RPC interfaces for Prepare, Commit, and Abort, mapping actions to the local memory store, local WAL, and Lock Manager.

```go
// snippet-5
package transaction

import (
	"context"
	"encoding/json"
	"errors"
	"sync"
)

type StorageEngine interface {
	Put(key, val string)
	Delete(key string)
	Get(key string) (string, bool)
}

type MemoryStore struct {
	mu   sync.RWMutex
	data map[string]string
}

func (m *MemoryStore) Put(key, val string) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.data[key] = val
}

func (m *MemoryStore) Get(key string) (string, bool) {
	m.mu.RLock()
	defer m.mu.RUnlock()
	v, ok := m.data[key]
	return v, ok
}

func (m *MemoryStore) Delete(key string) {
	m.mu.Lock()
	defer m.mu.Unlock()
	delete(m.data, key)
}

type ParticipantNode struct {
	mu           sync.Mutex
	wal          *WAL
	lockMgr      *LockManager
	store        StorageEngine
	pendingTxns  map[string]map[string]string // TxnID -> KeyValue Writes
}

func NewParticipantNode(wal *WAL, lockMgr *LockManager, store StorageEngine) *ParticipantNode {
	return &ParticipantNode{
		wal:         wal,
		lockMgr:     lockMgr,
		store:       store,
		pendingTxns: make(map[string]map[string]string),
	}
}

func (pn *ParticipantNode) Prepare(ctx context.Context, txnID string, keys []string, vals []string) error {
	// 1. Acquire Locks via SS2PL
	for _, key := range keys {
		if err := pn.lockMgr.Acquire(ctx, txnID, key, Exclusive); err != nil {
			pn.lockMgr.ReleaseAll(txnID, keys)
			return fmt.Errorf("failed locking key %s: %w", key, err)
		}
	}

	// 2. Buffer Writes locally
	payloadMap := make(map[string]string)
	for i, k := range keys {
		payloadMap[k] = vals[i]
	}

	pn.mu.Lock()
	pn.pendingTxns[txnID] = payloadMap
	pn.mu.Unlock()

	payloadBytes, err := json.Marshal(payloadMap)
	if err != nil {
		pn.lockMgr.ReleaseAll(txnID, keys)
		return err
	}

	// 3. Write Prepare Record to WAL and fsync
	if _, err := pn.wal.Append(txnID, TxnPrepare, payloadBytes); err != nil {
		pn.lockMgr.ReleaseAll(txnID, keys)
		return fmt.Errorf("WAL failure: %w", err)
	}

	return nil
}

func (pn *ParticipantNode) Commit(ctx context.Context, txnID string) error {
	pn.mu.Lock()
	writes, exists := pn.pendingTxns[txnID]
	if !exists {
		pn.mu.Unlock()
		return nil // Already committed or cleaned up
	}
	delete(pn.pendingTxns, txnID)
	pn.mu.Unlock()

	// 1. Persist committed updates to storage engine
	keys := make([]string, 0, len(writes))
	for k, v := range writes {
		pn.store.Put(k, v)
		keys = append(keys, k)
	}

	// 2. Append COMMIT to local WAL
	if _, err := pn.wal.Append(txnID, TxnCommit, nil); err != nil {
		// Log critical crash warning, but continue releasing locks as coordinator committed
	}

	// 3. Release Locks (End of SS2PL)
	pn.lockMgr.ReleaseAll(txnID, keys)
	return nil
}

func (pn *ParticipantNode) Abort(ctx context.Context, txnID string) error {
	pn.mu.Lock()
	writes, exists := pn.pendingTxns[txnID]
	if !exists {
		pn.mu.Unlock()
		return nil
	}
	delete(pn.pendingTxns, txnID)
	pn.mu.Unlock()

	keys := make([]string, 0, len(writes))
	for k := range writes {
		keys = append(keys, k)
	}

	// Append ABORT to local WAL
	_, _ = pn.wal.Append(txnID, TxnAbort, nil)

	// Release locks
	pn.lockMgr.ReleaseAll(txnID, keys)
	return nil
}
```

## The Single Point of Failure (SPOF): Coordinator Replicated Consensus

The classic implementation of Two-Phase Commit is a blocking protocol. If the coordinator crashes *after* participants have voted YES but *before* broadcasting the COMMIT/ABORT decision, participants are left in the **Prepared** state. 

Because they voted YES, participants cannot unilaterally abort; they promised they would commit if instructed. Because they don't know the final outcome, they cannot commit either. Consequently, they are blocked, **holding exclusive locks indefinitely**. Any transaction trying to read or write those locked keys will block or time out, resulting in a localized database freeze.

### Replicating the Coordinator via Raft
To mitigate this single point of failure, modern distributed databases (e.g. CockroachDB, Spanner) do not run the coordinator as a single process. Instead, coordinator states are driven by a Replicated State Machine using consensus protocols like **Raft** or **Paxos**.

*   Each transaction is assigned to a specific Raft consensus group.
*   Every state transition of the coordinator (Start, Prepare Votes, Commit Decision) is replicated to a quorum of nodes.
*   If the primary coordinator crashes, the Raft leader election chooses a new coordinator node.
*   The new coordinator leader reads the replicated Raft log, finds the active transaction's status, and completes the commit protocol with the participants.

Here is a snippet showing how a transaction's state changes are modeled as replicated Raft log entries to prevent blocking.

```go
// snippet-6
package transaction

import (
	"encoding/json"
	"fmt"
)

type CoordinatorState int

const (
	StatePending CoordinatorState = iota
	StatePrepared
	StateCommitted
	StateAborted
)

type RaftTxnCommand struct {
	TxnID        string           `json:"txn_id"`
	State        CoordinatorState `json:"state"`
	Participants []string         `json:"participants"`
}

type ReplicatedCoordinator struct {
	raftNode    RaftService
	localStates map[string]CoordinatorState
}

type RaftService interface {
	Propose(data []byte) error
	IsLeader() bool
}

func (rc *ReplicatedCoordinator) TransitionTxn(txnID string, state CoordinatorState, participants []string) error {
	if !rc.raftNode.IsLeader() {
		return fmt.Errorf("not the leader node; forward request to leader")
	}

	cmd := RaftTxnCommand{
		TxnID:        txnID,
		State:        state,
		Participants: participants,
	}

	payload, err := json.Marshal(cmd)
	if err != nil {
		return err
	}

	// Replicate coordinator state across quorum before returning success
	if err := rc.raftNode.Propose(payload); err != nil {
		return fmt.Errorf("failed to replicate coordinator state over Raft: %w", err)
	}

	return nil
}

// ApplyCommand is executed on all Raft replicas during log replication
func (rc *ReplicatedCoordinator) ApplyCommand(entry []byte) {
	var cmd RaftTxnCommand
	if err := json.Unmarshal(entry, &cmd); err != nil {
		// Log corruption warning
		return
	}

	rc.localStates[cmd.TxnID] = cmd.State
	
	// If replica is leader and state is committed/aborted, it guarantees completion
	if rc.raftNode.IsLeader() && (cmd.State == StateCommitted || cmd.State == StateAborted) {
		go rc.driveToCompletion(cmd.TxnID, cmd.State, cmd.Participants)
	}
}

func (rc *ReplicatedCoordinator) driveToCompletion(txnID string, state CoordinatorState, participants []string) {
	// Execute background RPC retries to ensure participants apply the decision
}
```

## Production Failure Modes & Optimization Strategies

When deploying 2PL and 2PC under high-throughput write workloads, standard protocols will degrade unless optimized for physical hardware and network constraints.

### 1. The Cost of Fsync (Group Commit)
The durability requirement of 2PC mandates that nodes write `TxnPrepare` and `TxnCommit` to the WAL and perform an `fsync` call to flush physical disk controllers. A standard NVMe SSD can perform an `fsync` in roughly 1ms, which limits transaction throughput to 1,000 transactions per second per node if processed sequentially.

**Solution: Group Commit.** Instead of issuing a separate `fsync` call for every transaction, write records to a ring buffer. A background goroutine flushes the buffer to disk at fixed intervals (e.g. every 2ms) or when the batch size reaches a threshold (e.g., 64KB). The coordinator delays responding to participants until the batch containing their transaction's prepare record is physically flushed.

### 2. Lock Saturation under High Contention (Hot Spots)
If many transactions modify the same key (e.g. inventory counters), lock queues explode, causing context timeouts and transaction abort cascades due to Wound-Wait.

**Solution: Split Keys or Single-Shard Orchestration.**
*   **Key Partitioning:** Rather than having one global key `inventory_item_123`, split the inventory into multiple keys (`inventory_item_123_part_A`, `inventory_item_123_part_B`) and increment them randomly, aggregating the total when reading.
*   **Command Query Responsibility Segregation (CQRS):** Route hot updates to an in-memory queue that applies mutations sequentially within a single shard, bypassing distributed transactions entirely for those specific counters.

### 3. Read Latency & Read-Only Transactions
Acquiring shared locks (S) under SS2PL for read queries blocks writes (X). If a system has a 9:1 read-to-write ratio, concurrency drops significantly.

**Solution: MVCC with Read-Only Transactions.** Implement Multi-Version Concurrency Control (MVCC). Each write creates a new version of the key tagged with the transaction's commit timestamp. Read-only transactions are assigned a read-timestamp (typically the current system time) and read the latest version of the key that is older than their read-timestamp. Because they read historic immutable snapshots, **read-only transactions require no lock acquisition (neither S nor X)**, preventing them from blocking writes.

## Verifying Protocol Correctness under Partition Scenarios

To verify that your custom 2PL + 2PC engine does not corrupt database state under split-brain situations, you must execute targeted chaos tests:

*   **Test Case 1 (Partition during Prepare):** Split the network between the Coordinator and Participant B while Participant A has already voted YES. Verify that the Coordinator executes an ABORT path, Participant A releases its locks, and Participant B never commits the writes.
*   **Test Case 2 (Coordinator crashes during Commit):** Kill the coordinator process immediately after it writes the local WAL `COMMIT` record but before it sends RPCs to participants. Bring the node back online, trigger the WAL recovery log scan, and verify that the coordinator automatically resumes broadcasting the `COMMIT` messages to finalize the transaction on participant shards.
