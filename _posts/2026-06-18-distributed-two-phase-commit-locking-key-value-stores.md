---
layout: post
title: "Implementing Two-Phase Commit and Two-Phase Locking in Distributed Key-Value Stores"
date: 2026-06-18 08:00:00 +0700
tags: [distributed-systems, database-internals, golang, transactions]
description: "A production-focused guide to building distributed ACID transactions in key-value stores using Strong Strict Two-Phase Locking and Two-Phase Commit."
image: "https://picsum.photos/seed/distributed-2pc-2pl/1080/720"
thumbnail: "https://picsum.photos/seed/distributed-2pc-2pl/400/300"
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

<script src="https://gist.github.com/mohashari/ceba5f1329e08892005b7add868f50ba.js?file=snippet-1.go"></script>

## Deadlock Prevention: Wound-Wait

A major side effect of holding locks until commit (SS2PL) is deadlocks. In a distributed key-value store, if Transaction 1 locks Key A on Shard 1 and requests Key B on Shard 2, while Transaction 2 locks Key B on Shard 2 and requests Key A on Shard 1, a deadlock cycle occurs. 

Detecting distributed deadlocks via wait-for graphs requires building a centralized coordinator or executing complex distributed cycle-finding algorithms. Both introduce significant network overhead. Therefore, high-performance distributed key-value stores prefer **Deadlock Prevention** algorithms using logical timestamps.

The two main timestamp-based deadlock prevention schemes are:
1.  **Wait-Die (Non-preemptive):** If $T_{requester}$ is older than $T_{holder}$, the requester waits. If $T_{requester}$ is younger, it dies (aborts).
2.  **Wound-Wait (Preemptive):** If $T_{requester}$ is older than $T_{holder}$, the requester \"wounds\" (aborts) $T_{holder}$ and steals the lock. If $T_{requester}$ is younger, it waits.

**Wound-Wait is generally superior** in production. An older transaction preempts younger transactions instantly, reducing latency for long-running transactions. Younger transactions wait gracefully instead of constantly aborting and retrying, which prevents live-locks.

Below is the implementation of a Wound-Wait deadlock prevention policy integrated into a lock manager context.

<script src="https://gist.github.com/mohashari/ceba5f1329e08892005b7add868f50ba.js?file=snippet-2.go"></script>

## Atomic Commitment: Two-Phase Commit (2PC)

While SS2PL handles isolating concurrent operations, Two-Phase Commit ensures that a single write transaction spanning multiple partitions is committed atomically.

The protocol involves a **Coordinator** (which orchestrates the transaction lifecycle) and multiple **Participants** (which manage the local storage shards).

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

<script src="https://gist.github.com/mohashari/ceba5f1329e08892005b7add868f50ba.js?file=snippet-3.go"></script>

Now, let's look at the **Coordinator** orchestration engine. It coordinates distributed participants, executing the two-phase commit state machine.

<script src="https://gist.github.com/mohashari/ceba5f1329e08892005b7add868f50ba.js?file=snippet-4.go"></script>

The matching **Participant Node** implementation exposes RPC interfaces for Prepare, Commit, and Abort, mapping actions to the local memory store, local WAL, and Lock Manager.

<script src="https://gist.github.com/mohashari/ceba5f1329e08892005b7add868f50ba.js?file=snippet-5.go"></script>

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

<script src="https://gist.github.com/mohashari/ceba5f1329e08892005b7add868f50ba.js?file=snippet-6.go"></script>

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