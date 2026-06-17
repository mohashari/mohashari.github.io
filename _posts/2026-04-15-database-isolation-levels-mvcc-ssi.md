---
layout: post
title: "Database Isolation Levels Under the Hood: MVCC and Serializable Snapshot Isolation (SSI)"
date: 2026-04-15 09:00:00 +0700
tags: [databases, postgresql, mvcc, transactions, systems]
description: "How databases enforce transaction isolation. A deep dive into MVCC internals, transaction anomalies like write skew, and how PostgreSQL implements lock-free Serializable Snapshot Isolation (SSI)."
image: "https://picsum.photos/seed/9274/1080/720"
thumbnail: "https://picsum.photos/seed/9274/400/300"
---

In relational databases, the **I** in ACID stands for **Isolation**. Ideally, isolation means that even if thousands of transactions run concurrently, the end state of the database is exactly the same as if they had run sequentially (one after another). This ideal is called **Serializability**.

However, running transactions strictly serially is terrible for performance. To achieve high throughput, databases allow transactions to run concurrently, exposing developers to various concurrency anomalies.

In this deep dive, we will look at how modern databases manage concurrency under the hood: starting from **Multi-Version Concurrency Control (MVCC)**, moving through transaction anomalies like **Write Skew**, and finally exploring the internals of PostgreSQL's lock-free **Serializable Snapshot Isolation (SSI)**.

---

## 1. Concurrency Anomalies & SQL Standard Isolation Levels

The ANSI SQL-92 standard defined four isolation levels based on three phenomena: **Dirty Reads**, **Non-repeatable Reads**, and **Phantom Reads**.

| Isolation Level | Dirty Read | Non-Repeatable Read | Phantom Read |
| :--- | :--- | :--- | :--- |
| **Read Uncommitted** | Allowed | Allowed | Allowed |
| **Read Committed** | Prevented | Allowed | Allowed |
| **Repeatable Read** | Prevented | Prevented | Allowed |
| **Serializable** | Prevented | Prevented | Prevented |

However, this standard is famous for being both incomplete and mathematically ambiguous. It fails to define other dangerous anomalies, most notably **Write Skew**, which can easily occur in databases running under "Repeatable Read" (including PostgreSQL).

---

## 2. Multi-Version Concurrency Control (MVCC) Internals

To read and write data concurrently without blocking each other ("readers don't block writers, and writers don't block readers"), modern databases use **MVCC**.

Instead of overwriting rows in-place, the database maintains multiple physical versions of the same logical row.

### Tuple Headers in PostgreSQL

In PostgreSQL, every table row (called a **tuple**) has hidden system columns in its header:

*   `xmin`: The Transaction ID (TxID) of the transaction that inserted the row.
*   `xmax`: The TxID of the transaction that deleted or updated the row (for an active row, `xmax` is 0).
*   `t_ctid`: A physical pointer to the newest version of the tuple (linking updated versions together).

### Anatomy of an Update

When a transaction updates a row, PostgreSQL does not modify the existing tuple. It performs a **delete** followed by an **insert**:

1.  It sets the `xmax` of the existing tuple to the current transaction ID.
2.  It creates a new physical tuple elsewhere on the page with `xmin` set to the current transaction ID and `xmax` set to 0.
3.  It updates the old tuple's `t_ctid` to point to the new physical tuple.

```
Original State:
[Tuple 1]  Data: "Alice", xmin: 100, xmax: 0, t_ctid: (0, 1)

Transaction 101 updates "Alice" to "Bob":
[Tuple 1]  Data: "Alice", xmin: 100, xmax: 101, t_ctid: (0, 2)  (Marked Deleted)
[Tuple 2]  Data: "Bob",   xmin: 101, xmax: 0,   t_ctid: (0, 2)  (Active Version)
```

### Read Snapshots

When Transaction B starts, the database gives it a **Snapshot**. This snapshot contains:
1.  `active_txns`: A list of all transaction IDs that are currently running (uncommitted) at the moment B starts.
2.  `min_txid`: The oldest active transaction ID. Anything older than this is committed.
3.  `max_txid`: The highest transaction ID assigned so far. Anything newer than this has not started yet.

A tuple is **visible** to Transaction B's snapshot if:
*   Its `xmin` has committed *before* B's snapshot was taken.
*   Its `xmin` is not in the `active_txns` list.
*   Its `xmax` is either 0, uncommitted, or was created *after* B's snapshot was taken.

This simple system ensures that Transaction B sees a frozen, consistent view of the database at the exact moment its snapshot was created.

---

## 3. The Write Skew Anomaly

Under the **Repeatable Read** isolation level, a transaction guarantees that any row it reads twice will remain unchanged. However, Repeatable Read is vulnerable to **Write Skew**.

Write Skew occurs when two concurrent transactions read the same data set, see that it satisfies a certain constraint, and then perform writes that collectively violate that constraint.

### The Classic Doctor on Call Example

Suppose a hospital has a constraint: *At least one doctor must remain on call at all times.* Doctors Alice and Bob are currently on call.

```
Database State:
Doctor: Alice, OnCall: True
Doctor: Bob,   OnCall: True

Transaction 1 (Alice trying to leave)            Transaction 2 (Bob trying to leave)
1. Read count(OnCall = True) -> 2                1. Read count(OnCall = True) -> 2
   (Constraint met, Alice can leave)                (Constraint met, Bob can leave)

2. Update Doctor Alice: OnCall = False           2. Update Doctor Bob: OnCall = False
3. Commit Transaction 1                          3. Commit Transaction 2
```

Because both transactions operated on separate rows (Transaction 1 updated Alice, Transaction 2 updated Bob), there are **no write-write conflicts**. Both transactions commit successfully!

However, the final state of the database is that **no doctors are on call**, violating the hospital's integrity constraint. This is Write Skew.

---

## 4. Serializable Snapshot Isolation (SSI)

To prevent Write Skew without reverting to expensive, dead-lock prone table locks, PostgreSQL implements **Serializable Snapshot Isolation (SSI)**.

SSI is an optimistic concurrency control mechanism. It lets transactions run under Snapshot Isolation (just like Repeatable Read) but tracks dependencies between them in real-time. If it detects a cycle of dependencies that could lead to an anomaly, it aborts one of the transactions.

### Read-Write Conflicts (SIREAD Locks)

SSI tracks dependencies using virtual locks called **SIREAD locks**.
*   Unlike standard locks (which block execution), SIREAD locks **never block**. They are purely flags in memory.
*   When Transaction A reads a row, PostgreSQL places a SIREAD lock on it.
*   If Transaction B later writes to that same row, PostgreSQL detects a **rw-antidependency** (a read-write conflict, denoted as $A \rightarrow B$). This means: "A read a version of the row before B updated it. If B commits, A's snapshot is technically out-of-date."

### The Dependency Graph and Cycle Detection

An anomaly can only occur if there is a cycle of rw-antidependencies in the transaction dependency graph. Specifically, research has shown that an anomaly requires a path of the form:

$$T_1 \xrightarrow{rw} T_2 \xrightarrow{rw} T_3$$

Where $T_2$ is a active, concurrent transaction.

```
       rw-antidependency        rw-antidependency
[Tx 1] ────────────────► [Tx 2] ────────────────► [Tx 3]
                            ▲
                            │
               (Concurrent active transaction)
```

PostgreSQL maintains a lock-free, in-memory dependency graph of all active transactions.
1.  When Doctor Alice's transaction reads the doctors table, it acquires a SIREAD lock.
2.  When Bob's transaction updates Bob's status, it triggers a rw-antidependency: Alice $\rightarrow$ Bob.
3.  When Alice's transaction updates Alice's status, Bob's transaction sees it, triggering another rw-antidependency: Bob $\rightarrow$ Alice.
4.  PostgreSQL detects a cycle of two consecutive rw-antidependencies ($Alice \xrightarrow{rw} Bob \xrightarrow{rw} Alice$).
5.  To guarantee serializability, the database immediately aborts Bob's transaction with a serialization failure (`SQLSTATE 40001`), forcing the client to retry.

### Minimizing Memory Overhead

Because tracking every individual row read by every transaction would consume excessive RAM, PostgreSQL collapses SIREAD locks dynamically:
*   If a transaction reads many rows on a single page, the individual row SIREAD locks are merged into a single **Page-level SIREAD lock**.
*   If it reads many pages in a table, they are merged into a **Relation-level SIREAD lock**.

While this optimization protects kernel memory, it increases the likelihood of "false positives" (aborting a transaction due to a coarse conflict on a page even if they didn't touch the exact same row).

---

## Conclusion

Understanding transaction isolation levels is crucial for writing reliable database-backed applications.

*   **Read Committed** is fine for basic applications but vulnerable to non-repeatable reads and race conditions.
*   **Repeatable Read** prevents row mutation during a transaction but allows **Write Skew** when constraints span multiple rows.
*   If your system enforces complex multi-row logical invariants (like the doctor-on-call or balance-limit constraints), you must use the **Serializable** isolation level.

Thanks to PostgreSQL's lock-free SSI implementation, you can achieve full serializability with minimal performance overhead, letting the database engine handle the mathematics of conflict detection while you focus on writing business logic.
