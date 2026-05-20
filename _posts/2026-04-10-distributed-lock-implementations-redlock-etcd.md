---
layout: post
title: "Distributed Lock Internals: Redlock vs. Etcd/Consul Raft-based Locks"
date: 2026-04-10 09:00:00 +0700
tags: [distributed-systems, redis, etcd, raft, concurrency]
description: "An in-depth analysis of distributed lock guarantees, comparing the Redlock algorithm's clock-dependent heuristic against Raft-backed consensus leases in Etcd and Consul."
image: ""
thumbnail: ""
---

In distributed systems, managing concurrent access to shared resources is notoriously difficult. If two nodes attempt to modify the same database record or file simultaneously, it can lead to state corruption. Distributed locks solve this by ensuring mutual exclusion across multiple independent processes.

However, not all distributed locks are created equal. Broadly, they fall into two categories: **highly available but clock-dependent heuristics** (like Redis Redlock) and **strongly consistent, consensus-backed leases** (like Etcd or Consul). 

In this article, we'll dive deep into the internals of both approaches, explore the famous Martin Kleppmann critique of Redlock, and explain why consensus-backed locks are mathematically superior for safety-critical operations.

---

## 1. Redlock: Redis-based Optimistic Locking

The **Redlock** algorithm, designed by Salvatore Sanfilippo (creator of Redis), is designed for cases where high availability and performance are favored, and absolute mathematical safety is secondary.

### The Algorithm

To acquire a lock, Redlock requires a cluster of $N$ independent Redis masters (usually 5). The process works as follows:

1.  **Generate Metadata:** The client gets the current time in milliseconds and generates a unique random value (the verifier token, typically a UUID).
2.  **Acquire Sequentially:** The client attempts to acquire the lock in all $N$ instances sequentially, using the same key name and random value. It uses a small timeout (e.g., 5-10ms) compared to the lock auto-release time (e.g., 10 seconds) to avoid getting blocked by a dead Redis node.
3.  **Calculate Elapsed Time:** The client subtracts the start time from the current time to see how long it took to acquire the locks.
4.  **Evaluate Quorum:** If the client successfully acquired the lock on a majority of nodes (at least $\lfloor N/2 \rfloor + 1$, or 3 out of 5) *and* the total elapsed time is less than the lock validity time, the lock is acquired.
5.  **Compute Validity Window:** The lock's remaining active duration is the initial validity time minus the time spent acquiring it.
6.  **Release on Failure:** If the client fails to acquire the majority or the validity time is negative, it sends an unlock script (`Lua` script to check matching UUID token and delete key) to all instances.

```lua
-- Lua script for safe release to prevent deleting another client's lock
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
else
    return 0
end
```

### The Vulnerability: Martin Kleppmann's Critique

In 2016, distributed systems researcher Martin Kleppmann published a detailed critique of Redlock. He proved that Redlock is unsafe because it relies on an implicit assumption: **the synchrony model of physical clocks**.

In a real distributed system, physical clocks drift. They can also jump backward or forward due to NTP adjustments. Redlock fails under three common scenarios:

#### Scenario A: The GC Pause / Stop-the-World (STW)
Suppose Client 1 acquires the lock on Redis nodes A, B, and C. Immediately after acquiring the lock, Client 1 is hit by a long garbage collection (GC) pause.
*   While Client 1 is paused, its lock validity window expires.
*   Redis nodes A, B, and C expire the keys automatically.
*   Client 2 requests the lock, successfully acquires it on A, B, and C, and begins writing to the database.
*   Client 1 wakes up from its GC pause, believing it still holds a valid lock, and also writes to the database.
*   **Result:** Mutual exclusion is broken. Data corruption occurs.

```
Client 1        [Acquire Lock]───( GC Pause / STW )──────────────────────────► [Write to DB (Unsafe!)]
Redis Cluster   [Lock Valid]───────────────────────►[Auto-Expired]
Client 2                                            [Acquire Lock]───[Write]
```

#### Scenario B: Clock Drift
If the physical clock on Redis Node C jumps forward rapidly, Node C will expire its key prematurely. Client 2 can then acquire a lock on C and D (plus E), while Client 1 still holds the lock on A and B.

#### Scenario C: Unsynced Write to Disk on Crash
Redis uses asynchronous snapshotting (RDB) or write-ahead logging (AOF) flushed every second. If Redis Node C crashes immediately after receiving a lock write, restarts before the key is synced to disk, it will boot up without the key. Client 2 can now lock C, D, and E, breaking mutual exclusion.

---

## 2. Consensus-Backed Leases: Etcd & Consul

Unlike Redis, which uses ad-hoc replication, **Etcd** (using Raft) and **Consul** (using Raft) are CP systems (consistent and partition-tolerant under the CAP theorem). They do not rely on physical clocks for safety; instead, they use logical timers, monotonically increasing counters, and Raft consensus.

### How Etcd Leases Work

Etcd implements locks using three core abstractions: **Leases**, **KeepAlives**, and **Revisions**.

```
Client A  ──(Create Lease: 10s)──►  Etcd Cluster (Raft)
Client A  ──(KeepAlive Loop)─────►  Maintains Lease Active

Client A  ──(TXN: Create Key if Revision == 0) ──► Acquired!
Client B  ──(TXN: Create Key fails) ──► Watches key for changes
```

1.  **Lease Creation:** The client creates a Lease with a Time-To-Live (TTL) (e.g., 10 seconds). The consensus cluster replicates this lease.
2.  **Transactional Key Creation:** The client attempts to write a key (e.g., `/locks/my-resource`) associated with that Lease inside an atomic transaction (Txn).
    *   The transaction checks if the key already exists.
    *   If it does not exist, the key is created, and its `CreateRevision` is recorded. The client holds the lock.
    *   If the key exists, the transaction fails.
3.  **Active Keep-Alive:** The client runs a background loop to send heartbeat "KeepAlive" requests to the lease. As long as the client is alive and connected, the lease is extended.
4.  **Consensus-driven Expiry:** If the client dies or is partitioned, it stops sending KeepAlives. Once the lease TTL expires, the Etcd cluster automatically deletes the key.
5.  **Revisions and Waiters:** Other clients waiting for the lock do not poll. They set a **Watch** on the prefix `/locks/my-resource`. When the key is deleted, Etcd notifies the next client immediately.

### The Fencing Token: The Ultimate Savior

Even with Etcd's strong consistency, the GC Pause (Scenario A) can still cause a client to write stale data after its lease expires. How do we solve this?

With **Fencing Tokens**.

Every time a consensus lock is acquired, the database returns a monotonically increasing sequence number (in Etcd, this is the `ModRevision` or global raft index).

```
Client A (Token: 101)  ───────►  Shared Storage (S3 / DB)  [Accepts 101]
Client A goes into GC STW...
Lease expires. Client B acquires lock.
Client B (Token: 102)  ───────►  Shared Storage (S3 / DB)  [Accepts 102]
Client A wakes up from GC.
Client A (Token: 101)  ───────►  Shared Storage (S3 / DB)  [REJECTS 101 < 102]
```

When writing to a shared database or storage service, the write operation must include this token. The storage engine enforces a simple rule: **reject any write with a token less than the highest token already processed**.

Because Client A's token (101) is smaller than the token written by Client B (102), the storage service immediately aborts Client A's late, stale write.

---

## Comparison Summary

| Guarantee / Feature | Redlock (Redis) | Etcd / Consul (Raft) |
| :--- | :--- | :--- |
| **Consistency Class** | AP (Highly Available, Eventually Consistent) | CP (Strongly Consistent) |
| **Safety Dependency** | Physical Clocks (System Time, NTP) | Logical consensus timers, Raft index |
| **Performance** | Extremely Fast (sub-millisecond) | Moderate (requires Raft consensus sync) |
| **Failover Safety** | Vulnerable to clock jumps, GC pauses, crash restarts | Safe from clock drift; self-healing consensus |
| **Fencing Support** | No native monotonic sequence numbers | Yes (Raft transaction revisions) |

## Conclusion

If your system uses a distributed lock merely to optimize performance—such as preventing duplicate expensive calculations or scraping the same website twice—**Redlock is perfectly acceptable**. It is fast, lightweight, and easy to set up.

However, if your distributed lock is used for **correctness**—such as ensuring only one node writes to an accounting ledger, or preventing two storage drivers from modifying a virtual hard disk—**Redlock is unsafe**. You must use a consensus-backed engine like **Etcd** or **Consul**, and you *must* implement **fencing tokens** in your storage engine to protect against unavoidable network partitions and runtime execution pauses.
