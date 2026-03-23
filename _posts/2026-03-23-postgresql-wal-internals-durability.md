---

```markdown
---
layout: post
title: "Write-Ahead Log Internals: How PostgreSQL Guarantees Durability"
date: 2026-03-23 08:00:00 +0700
tags: [postgresql, databases, storage-engines, internals, backend]
description: "A deep dive into PostgreSQL WAL mechanics: LSNs, checkpoint cycles, recovery paths, and the exact knobs that prevent data loss in production."
image: ""
thumbnail: ""
---

Your database server hard-restarted at 3:47 AM — kernel OOM killer, rogue `cp -r` from a misconfigured backup script, EC2 spot reclaim, pick your favorite. The on-call engineer reopens the application, connections start flowing, and every single committed transaction is there. Nothing was lost. Most engineers treat this as magic. It isn't — it's the Write-Ahead Log, and understanding exactly how it works is the difference between a DBA who configures PostgreSQL by cargo-culting Stack Overflow answers and one who can reason about durability guarantees from first principles.

![Write-Ahead Log Internals: How PostgreSQL Guarantees Durability Diagram](/images/diagrams/postgresql-wal-internals-durability.svg)

## The Core Invariant

WAL rests on one rule, stated plainly: **a transaction's changes must be written to the WAL and flushed to disk before the COMMIT acknowledgment is sent to the client.** The data pages themselves — the heap files under `$PGDATA/base/` — can remain dirty in shared buffers for seconds or minutes. The WAL cannot.

This is why PostgreSQL can survive crashes. On restart, it doesn't need the data pages to be in a consistent state. It only needs the WAL to be intact up to the last committed LSN (Log Sequence Number). Everything else is replayed.

## WAL on Disk: Segments, LSNs, and the pg_wal Directory

WAL is stored as fixed-size segment files in `$PGDATA/pg_wal/`. Each segment defaults to 16MB, named with a 24-character hex string encoding the timeline and segment number:

<script src="https://gist.github.com/mohashari/858bd7d237ba716ea3a09cb9242f5a58.js?file=snippet-1.sh"></script>

An LSN is a 64-bit integer (displayed as `X/Y` in hex), monotonically increasing across the life of a cluster. Every WAL record has one. Every 8KB data block tracks the LSN of the last WAL record that modified it in its page header (`PageHeaderData.pd_lsn`). This field is the lynchpin of the no-write-twice optimization: if a page's LSN is ≥ the checkpoint's redo LSN, the page is already current and doesn't need to be replayed during recovery.

## The Write Path: What Actually Happens on COMMIT

Understanding the write path dispels the misconception that PostgreSQL writes to disk on every INSERT. Here's the sequence for a single-statement transaction:

1. The backend modifies the target pages in **shared buffers** (in-memory, 8KB pages).
2. Before touching those pages, it constructs a WAL record describing the change (full-page write on first modification after checkpoint, delta record otherwise) and copies it into the **WAL buffers** (`wal_buffers`, default 4–64MB depending on `shared_buffers`).
3. On COMMIT, the WAL writer flushes all WAL buffer content up to this transaction's LSN using `fsync(2)` or `fdatasync(2)` against the WAL segment file.
4. Only after that flush completes does the backend send `CommandComplete` + `ReadyForQuery` to the client.
5. The dirty data pages in shared buffers remain in memory. The checkpointer will flush them asynchronously, anywhere from seconds to minutes later.

<script src="https://gist.github.com/mohashari/858bd7d237ba716ea3a09cb9242f5a58.js?file=snippet-2.sql"></script>

The gap between `pg_current_wal_insert_lsn()` and `pg_current_wal_flush_lsn()` is the amount of WAL sitting in buffers, not yet on disk. Under normal load this should be near zero after each commit. If it's consistently non-zero, you're hitting WAL contention.

## Full-Page Writes: The Hidden Bandwidth Tax

PostgreSQL's storage layer writes 8KB pages. Most OS and storage stacks don't guarantee atomic 8KB writes. A partial write — say 4KB written before a crash — produces a torn page. To defend against this, PostgreSQL enables `full_page_writes = on` by default. After a checkpoint, the **first modification** to any data page includes a copy of the entire 8KB page in the WAL record, not just the delta.

This is expensive. A checkpoint followed by a burst of writes doubles your WAL volume for that burst. On a system doing 500MB/s of writes, `full_page_writes` can push WAL bandwidth to 800–900MB/s immediately post-checkpoint. The mitigation is to spread checkpoints out (tune `checkpoint_completion_target`) and use storage that guarantees atomic sector writes (most NVMe devices do, at 512B or 4K sector size — check with `smartctl -a /dev/nvme0`).

<script src="https://gist.github.com/mohashari/858bd7d237ba716ea3a09cb9242f5a58.js?file=snippet-3.sql"></script>

A `checkpoints_req` count that's growing fast means your writes are hitting `max_wal_size` before `checkpoint_timeout` fires. That's an early checkpoint triggered by WAL pressure — you're not getting the full benefit of your checkpoint interval, and you're generating full-page writes more frequently than necessary. Raise `max_wal_size`.

## The Checkpoint Cycle

A checkpoint is PostgreSQL's mechanism for advancing the recovery starting point. When a checkpoint completes:

1. All dirty shared buffer pages are flushed to their heap files in `base/`.
2. A `CHECKPOINT` WAL record is written with the new **redo LSN**.
3. `pg_control` is updated with the checkpoint LSN. This is the file PostgreSQL reads first on startup to locate where recovery must begin.

The critical insight: after a checkpoint, recovery only needs to replay WAL from the checkpoint's redo LSN forward. WAL segments before that point are eligible for recycling (or archiving if `archive_mode = on`).

```ini
# snippet-4
# postgresql.conf — checkpoint tuning for a write-heavy OLTP workload
# Goal: reduce full-page write storms, avoid checkpoint I/O spikes

# How long between automatic checkpoints (default: 5min)
checkpoint_timeout = 15min

# Max WAL accumulation before forcing an early checkpoint
# Size this to absorb ~15min of peak WAL without triggering early checkpoints
max_wal_size = 8GB

# Minimum WAL retained even if checkpoint fires early
min_wal_size = 2GB

# Spread checkpoint I/O over this fraction of checkpoint_timeout
# 0.9 = spread writes over 13.5 minutes, leaving 1.5min for sync phase
checkpoint_completion_target = 0.9

# WAL synchronization method — fdatasync is correct for most Linux setups
# open_sync or open_datasync can outperform on specific NVMe setups
wal_sync_method = fdatasync

# WAL buffer size — bump on systems with many concurrent writers
# Automatic sizing (default): min(1/32 of shared_buffers, 64MB)
wal_buffers = 64MB
```

Setting `checkpoint_completion_target = 0.9` is almost always correct. The default of 0.9 was finally made the default in PostgreSQL 14 — if you're on PG 13 or earlier on a production system, check this. The old default of 0.5 meant checkpoint I/O was front-loaded into half the interval, causing visible latency spikes.

## WAL Archiving and Point-in-Time Recovery

`archive_mode` is what turns WAL into a PITR backup mechanism. PostgreSQL calls `archive_command` for each completed WAL segment, and `restore_command` during recovery to retrieve archived segments.

```ini
# snippet-5
# postgresql.conf — WAL archiving with pgBackRest
archive_mode = on
archive_command = 'pgbackrest --stanza=prod archive-push %p'

# Verify archiving is keeping up — archived_count should track
# pg_current_wal_lsn() advances; last_archived_wal should be recent
```

<script src="https://gist.github.com/mohashari/858bd7d237ba716ea3a09cb9242f5a58.js?file=snippet-6.sql"></script>

A `failed_count` that keeps climbing while `archived_count` stays flat is one of the more dangerous silent failures in PostgreSQL operations. The database will start accumulating WAL segments in `pg_wal/` to avoid discarding unarchived WAL, eventually filling the disk. Your backups also stop being useful for PITR past the last successful archive.

## Streaming Replication: WAL as a Network Protocol

Physical streaming replication is WAL delivery over the network. The primary's WAL sender process streams WAL records to the standby's WAL receiver in near real-time. The standby's startup process applies those records, keeping the standby in a perpetual recovery state.

<script src="https://gist.github.com/mohashari/858bd7d237ba716ea3a09cb9242f5a58.js?file=snippet-7.sql"></script>

The distinction between `flush_lag` and `replay_lag` matters for `synchronous_commit`. With `synchronous_commit = remote_write`, the primary waits until the standby has written WAL to its OS buffer (not flushed to disk). With `remote_apply`, it waits until the standby has applied the WAL to its data pages — meaning a query on the standby will see the committed data immediately. Each level trades latency for durability/consistency:

| `synchronous_commit` value | Durability guarantee | Extra latency |
|---|---|---|
| `off` | None (WAL async) | ~0 — but 600ms window for data loss |
| `local` | Local disk only | fsync RTT (~1ms NVMe) |
| `remote_write` | Standby OS buffer | Network RTT |
| `remote_apply` | Standby applied | Network RTT + apply time |
| `on` (default) | Standby flushed to disk | Network RTT + standby fsync |

`synchronous_commit = off` is the most misunderstood setting in PostgreSQL. It does NOT risk corruption — the database will still be consistent after a crash. You risk losing the last ~600ms of commits (the `wal_writer_delay` window, default 200ms, but batching can extend the exposure). This is acceptable for session-level analytics inserts or queue-like patterns where the application can tolerate a replay. Never set it globally and forget about it.

## Recovery: What Happens at Startup After a Crash

PostgreSQL startup reads `pg_control` to find the latest checkpoint record, then replays WAL from the checkpoint's redo LSN forward to the end of the WAL. This is a pure redo-only recovery model — there is no undo pass over data pages. Aborted transactions are handled via the commit log (CLOG/pg_xact): if a transaction's XID shows `ABORTED` in pg_xact, the heap tuples it wrote are simply invisible to readers. No WAL replay needed to roll them back.

The recovery process is single-threaded and sequential. On a system with 8GB of WAL to replay (a large `max_wal_size` combined with a crash at peak write time), recovery can take 10–20 minutes. This is the real cost of aggressive checkpoint intervals. If your SLA requires sub-60-second RTO, keep `max_wal_size` ≤ 2GB and `checkpoint_timeout` ≤ 5min.

<script src="https://gist.github.com/mohashari/858bd7d237ba716ea3a09cb9242f5a58.js?file=snippet-8.sh"></script>

## What Actually Causes Data Loss (And What Doesn't)

**Does not cause data loss:**
- OOM kill of the postmaster
- EC2 instance reboot, kernel panic
- `kill -9` on any backend process
- Disk I/O errors on data files (recovery replays from WAL)

**Does cause data loss:**
- `synchronous_commit = off` + crash within the `wal_writer_delay` window (default: 200ms, max ~18 transactions)
- `fsync = off` — never do this except on a disposable test instance
- WAL segment corruption (hardware fault, bad RAID controller with write-back cache and no BBU)
- Truncating or deleting files in `pg_wal/` while the cluster is running

The storage stack matters more than most engineers admit. A RAID controller with a write-back cache that lies about fsync completion will silently corrupt your durability guarantee. Always use a controller with a battery-backed unit (BBU) or capacitor-backed write cache, or disable write-back caching entirely. On AWS, io1/io2 EBS volumes give you genuine fsync guarantees. gp3 does too — but their burst behavior means you can saturate IOPS at checkpoint time and see latency spikes you'd never see on a dedicated bare-metal host.

## The Practical Checklist

For any PostgreSQL production deployment, verify these before it matters:

- `wal_level = replica` minimum (required for streaming replication and logical decoding)
- `archive_mode = on` + a tested `restore_command` — run a PITR restore drill quarterly
- `max_wal_size` sized for your peak write rate × desired checkpoint interval
- `checkpoint_completion_target = 0.9`
- `wal_buffers = 64MB` if you have concurrent writers
- `synchronous_commit` set intentionally, not left at default without understanding the standby topology
- BBU or capacitor on any RAID controller handling WAL segments
- Monitor `pg_stat_archiver.failed_count` and alert on non-zero values

WAL is the foundation everything else in PostgreSQL builds on — replication, logical decoding, PITR, even `pg_upgrade`'s binary compatibility checks. Getting comfortable with LSNs, checkpoint mechanics, and recovery paths pays compounding dividends across every other database reliability concern.
```

---

Two files need to be written (both were blocked by permissions):
1. `_posts/2026-03-23-write-ahead-log-postgresql-durability.md` — the post above
2. `images/diagrams/postgresql-wal-internals-durability.svg` — the WAL architecture diagram

Approve both writes and they'll be saved to disk. The diagram shows the full write path (Client → Shared Buffers → WAL Buffers → WAL Segments → fsync → COMMIT ACK) plus the checkpointer, WAL archiver, streaming replication sender, and the 4-step crash recovery sequence at the bottom.