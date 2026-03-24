---
layout: post
title: "Write-Ahead Logging Across Storage Engines: RocksDB, LevelDB, and Postgres Compared"
date: 2026-03-24 08:00:00 +0700
tags: [storage-engines, rocksdb, postgresql, databases, durability]
description: "How RocksDB, LevelDB, and PostgreSQL implement WAL differently — and why those differences matter when your node crashes at 3am."
image: ""
thumbnail: ""
---

Your node crashes mid-transaction. The OS reboots. Your storage engine replays its write-ahead log, and either your data is whole or it isn't. That moment — the first few seconds after `systemd` brings your database back up — is where WAL design differences stop being academic. I've worked on systems where a misconfigured `sync_wal` in RocksDB caused silent data loss after a kernel panic, and watched Postgres recover a 50GB database cleanly from a WAL-replayed checkpoint while a co-located LevelDB-backed service had to rebuild its state from scratch because the log got corrupted during an unclean shutdown. WAL is not a background detail — it's the contract your storage engine makes with you about what "committed" actually means.

![Write-Ahead Logging Across Storage Engines: RocksDB, LevelDB, and Postgres Compared Diagram](/images/diagrams/write-ahead-log-storage-engines-compared.svg)

## What WAL Is Actually Doing

The fundamental guarantee is simple: before any data page is modified on disk, a record describing that modification is written sequentially to the log. Sequential writes are orders of magnitude faster than random writes (on spinning disks, ~200 MB/s sequential vs ~1 MB/s random; on NVMe, the gap narrows but sequential still wins due to parallelism and queue depth). The log is your source of truth for recovery. The actual data pages are just an optimization — a cache that gets materialized from the log.

All three engines implement this core idea, but they differ in critical ways: what gets logged, how the log is structured, when it gets truncated, and what "recovery" actually involves.

## RocksDB: WAL as a Durability Supplement to the MemTable

RocksDB's WAL lives at `/data/rocksdb/*.log`. Every write that lands in the MemTable is first appended to this log. The log format is a sequence of `WriteBatch` records, each with a 32-bit CRC checksum and a sequence number. On crash, `RecoverLogFiles` replays all log records into a fresh MemTable in sequence-number order, then continues normal operation.

The key design choice: **the WAL is temporary**. Once a MemTable is flushed to an SST file in L0, the WAL records covering that MemTable are no longer needed for recovery. RocksDB tracks this via a per-column-family minimum log number stored in the MANIFEST file. When all column families have flushed past a given log number, that log file is deleted.

This creates an important failure mode: if `sync_wal` is `false` (the default in some embeddings), writes are acknowledged before the WAL is fsynced to disk. On a kernel panic — not a graceful shutdown, but a hard crash — the OS page cache holding those WAL writes is gone. You lose data. The RocksDB wiki calls this the "OS crash" vs "process crash" distinction. A process crash is fine with `sync_wal=false` because the kernel will flush the dirty pages eventually. An OS crash is not. If you're running RocksDB inside TiKV, CockroachDB, or any production system that claims durability, check `sync_wal` and `bytes_per_sync`.

RocksDB also supports **WAL recycling** via `recycle_log_file_num`. Instead of deleting old log files, they're reused for new WAL data. This avoids the overhead of allocating new inodes and avoids fragmentation on some filesystems. Facebook's production deployments reportedly keep 2–4 WAL files in the recycle pool. It matters at high write throughput: on a workload doing 500K writes/sec, WAL allocation can become a measurable bottleneck.

Group commit is supported: multiple write threads can contribute to the same WAL append via the `WriteGroup` mechanism introduced in RocksDB 5.x. The leader thread collects pending writes, performs a single `pwrite` to the WAL, then notifies all followers. This amortizes the fsync cost across concurrent writers and is essential for throughput above ~50K writes/sec.

## LevelDB: Simpler, More Honest About Its Limits

LevelDB, RocksDB's ancestor, uses a structurally similar WAL but with far fewer knobs. The log format is identical — 32-byte block headers, CRC checksums, `WriteBatch` records. The critical difference: **LevelDB has a single writer lock**. Only one thread can write to the log at a time. There is no group commit, no parallel WAL writers. This is fine for embedded single-process use cases (Chrome's IndexedDB uses LevelDB) but kills throughput in multi-threaded server workloads.

LevelDB's log handling is simpler: when the MemTable fills and a new one is created, the old log is immediately queued for deletion after the MemTable flushes to disk. No recycling, no per-column-family tracking. This means on a high-write workload, you get frequent file creation and deletion in the data directory, which is a real issue on ext4 with `dir_index` and large directories.

Recovery in LevelDB is `RecoverLogFile` — replay from the beginning of the last log file. One log file per MemTable. If the log is corrupt partway through (which happens with SSD firmware bugs or truncated writes), LevelDB's default behavior is to drop the corrupt tail and continue, which means silent data loss. You can set `paranoid_checks=true` to fail loudly instead, but most embedders don't.

LevelDB has no concept of WAL sync mode at the `DB::Open` level — each individual `Write` call takes a `WriteOptions` with a `sync` boolean. Setting `sync=false` on all writes is equivalent to RocksDB's `sync_wal=false`: fast, but only safe against process crashes. If you're using LevelDB directly in 2026, you're probably maintaining legacy code. Use RocksDB.

## PostgreSQL: ARIES-Compliant, Full Transaction Semantics

PostgreSQL's WAL implementation is in a different category. It supports full ACID transactions with arbitrary interleaving, MVCC, DDL changes, and physical replication — all driven from the same WAL stream. The design follows the ARIES (Algorithm for Recovery and Isolation Exploiting Semantics) protocol: redo-only log records for committed data, undo records for aborted transactions, and a precise notion of log sequence numbers (LSNs) that identify every byte in the WAL stream.

The write path goes through a shared in-memory `WALInsertLock`-protected buffer (`wal_buffers`, default 16MB since Postgres 11). Backends write WAL records to this buffer, then a dedicated `walwriter` background process flushes them to `pg_wal/` at configurable intervals. The files are named by LSN (e.g., `000000010000000000000001`) and are exactly 16MB by default (configurable via `wal_segment_size` at `initdb` time — you cannot change it without reinitializing).

The `wal_sync_method` parameter controls how Postgres achieves durability: `fsync` (the safest default), `fdatasync`, `open_sync`, or `open_datasync`. On Linux with ext4, `fdatasync` is typically correct and faster than `fsync` because it skips syncing file metadata unless the file size changed. On ZFS, the story is different — ZFS's write-ahead logging at the filesystem level means Postgres's `fsync` is partially redundant, and some shops run `wal_sync_method=open_sync` with a ZFS intent log (ZIL) on a dedicated SSD.

**Group commit in Postgres** is called the WAL commit queue. When multiple backends commit simultaneously, Postgres allows one backend to act as the "group leader" and flush WAL for the entire group before notifying all waiters. This is the mechanism behind the `synchronous_commit=on` vs `synchronous_commit=local` distinction — the former waits for the standby to acknowledge the WAL, the latter only waits for the local flush.

WAL retention is governed by checkpoints. A checkpoint writes all dirty shared buffer pages to disk and records a checkpoint LSN in the WAL. WAL segments older than the last checkpoint (minus `wal_keep_size`) are recycled — renamed to the next expected segment name rather than deleted. This recycling avoids filesystem overhead and is the same idea as RocksDB's `recycle_log_file_num`. After crash recovery, Postgres finds the latest checkpoint record, reads the redo LSN from it, and replays forward from there. Uncommitted transactions are rolled back via UNDO records. This is full ARIES; LevelDB and RocksDB do not implement UNDO.

## The Recovery Latency Difference

This is where the design diverges most visibly in production. When you restart a Postgres node after a crash, recovery time is bounded by the distance between the last checkpoint and the crash LSN — roughly `checkpoint_completion_target * checkpoint_timeout` worth of WAL, default ~5 minutes of data at high write rates. With `wal_compression=lz4` (available since Postgres 15), that log shrinks by 40–60% on typical workloads, cutting recovery time proportionally.

RocksDB recovery replays only the unflushed MemTable data — typically the last few hundred milliseconds of writes. On a 200MB MemTable, that's roughly 200MB of sequential reads, plus the time to re-sort into the SkipList. Fast. The MANIFEST file is also replayed to reconstruct the SST file catalog.

LevelDB has similar recovery characteristics to RocksDB but is single-threaded throughout. On an embedded device with a cold NVMe, this is fine. On a server with a 512MB MemTable limit, replaying 512MB single-threaded into a SkipList at startup is measurably slow — I've seen 8–12 seconds on a busy embedded LevelDB store.

## Operational Implications

**Disk I/O patterns**: RocksDB and LevelDB do one sequential WAL write per write batch, then eventually compact SST files in the background. The WAL write is predictable; compaction is bursty. Postgres does one WAL write per statement or transaction commit, plus background checkpoint writes. If you're sizing IOPS for a storage engine, RocksDB's write amplification during compaction (typically 10–30x in the worst case for leveled compaction) dwarfs WAL I/O. Postgres's checkpoint write amplification is lower but affects a larger surface area (heap, index, fsm, vm files).

**Replication**: Postgres's WAL is the replication protocol. Physical streaming replication (`wal_level=replica`) ships the raw WAL stream to standbys via the `walsender` process. Logical replication (`wal_level=logical`) ships decoded change events. RocksDB has no built-in replication from the WAL — replication in RocksDB-backed systems (TiKV, MyRocks) is done at the application layer, typically via Raft, with WAL used only for local durability. LevelDB has no replication at all.

**Tooling**: Postgres's `pg_waldump` lets you decode WAL records to human-readable output, which is invaluable for debugging replication lag or understanding exactly what happened before a crash. RocksDB has `ldb dump_wal`. LevelDB has nothing equivalent — you're reading binary log files with a hex editor if something goes wrong.

## Choosing the Right WAL Model

Use **LevelDB** only if you need a simple embedded key-value store in a single-threaded context and don't care about replication or advanced recovery tooling. Its WAL is correct but minimal.

Use **RocksDB** when you need high write throughput (>100K writes/sec), column families, bloom filters, or tiered storage integration. Tune `sync_wal`, `bytes_per_sync`, `recycle_log_file_num`, and `max_total_wal_size` aggressively. Monitor WAL stalls via `rocksdb.write.stall` metrics — they surface when compaction falls behind and WAL writes are throttled.

Use **PostgreSQL** when you need full ACID transactions, MVCC, arbitrary query patterns, or streaming replication with failover. Its WAL is the most operationally complete: it powers recovery, replication, PITR, and logical change capture simultaneously. Tune `wal_compression`, `checkpoint_completion_target`, and `synchronous_commit` based on your durability-vs-latency trade-off.

The WAL is not something you configure once and forget. It's a live contract — and reading it directly (`pg_waldump`, `ldb dump_wal`) when production misbehaves is one of the highest-leverage debugging skills a backend engineer can have.