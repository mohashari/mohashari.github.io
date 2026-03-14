---
layout: post
title: "Database Transactions & ACID: What Every Engineer Must Know"
date: 2026-03-16 07:00:00 +0700
tags: [database, sql, transactions, backend, reliability]
description: "Deep dive into ACID properties, isolation levels, deadlocks, and how to write safe transactional code in production systems."
---

Database transactions are the backbone of data integrity. Yet many engineers use them without truly understanding what guarantees they provide — or don't provide.

## What Is a Transaction?

A transaction is a unit of work that is either fully committed or fully rolled back. It's the database's promise: "either all of this happens, or none of it."

<script src="https://gist.github.com/mohashari/70e2bfea5d2ae677533eeddec0ae0793.js?file=snippet.sql"></script>

If the server crashes after line 2 but before `COMMIT`, neither update persists.

## ACID Properties Explained

### Atomicity — All or Nothing

Every statement in a transaction succeeds together or fails together. No partial updates.

### Consistency — Rules Always Hold

After a transaction completes, all database constraints (foreign keys, NOT NULL, CHECK) must be satisfied. The database moves from one valid state to another.

### Isolation — Concurrent Transactions Don't Interfere

This is the most nuanced property. Isolation levels define how much one transaction can "see" of another in-progress transaction.

### Durability — Committed Data Survives Failures

Once `COMMIT` returns, the data is written to disk (via WAL — Write-Ahead Log). A server crash won't lose it.

## Isolation Levels — The Real Complexity

PostgreSQL's four isolation levels and the anomalies they prevent:

| Level | Dirty Read | Non-Repeatable Read | Phantom Read |
|-------|-----------|---------------------|--------------|
| Read Uncommitted | Possible | Possible | Possible |
| Read Committed (default) | Prevented | Possible | Possible |
| Repeatable Read | Prevented | Prevented | Possible |
| Serializable | Prevented | Prevented | Prevented |

### Read Committed (Default) — The Practical Choice

<script src="https://gist.github.com/mohashari/70e2bfea5d2ae677533eeddec0ae0793.js?file=snippet-2.sql"></script>

Transaction A sees different values for the same row in the same transaction. This is fine for most reads, problematic for calculations.

### Repeatable Read — Snapshot Consistency

<script src="https://gist.github.com/mohashari/70e2bfea5d2ae677533eeddec0ae0793.js?file=snippet-3.sql"></script>

PostgreSQL uses MVCC (Multi-Version Concurrency Control) to serve the snapshot without blocking writers.

### Serializable — The Safest, Slowest

<script src="https://gist.github.com/mohashari/70e2bfea5d2ae677533eeddec0ae0793.js?file=snippet-4.sql"></script>

Use serializable for financial calculations, inventory deductions, or any place where phantom reads cause incorrect results.

## Deadlocks — When Transactions Block Each Other

<script src="https://gist.github.com/mohashari/70e2bfea5d2ae677533eeddec0ae0793.js?file=snippet-5.sql"></script>

**Prevention strategy:** always acquire locks in a consistent order.

<script src="https://gist.github.com/mohashari/70e2bfea5d2ae677533eeddec0ae0793.js?file=snippet.go"></script>

## Transactional Code in Go

<script src="https://gist.github.com/mohashari/70e2bfea5d2ae677533eeddec0ae0793.js?file=snippet-2.go"></script>

Key patterns:
- `defer tx.Rollback()` — defensive cleanup even on panic
- `FOR UPDATE` — explicit row-level locking
- `BeginTx` with explicit isolation level

## Savepoints — Nested Rollbacks

<script src="https://gist.github.com/mohashari/70e2bfea5d2ae677533eeddec0ae0793.js?file=snippet-6.sql"></script>

Savepoints let you roll back part of a transaction without losing everything.

## Common Mistakes

1. **Opening transactions too wide** — long-running transactions hold locks and block other writers
2. **Ignoring `Rollback` errors** — always check and log them
3. **Using Read Committed for financial math** — use Repeatable Read or Serializable
4. **Not handling serialization failures** — Serializable transactions can fail; retry them

Transactions are your first line of defense against data corruption. Understand the isolation level you need, acquire locks in a consistent order, and keep transactions short.
