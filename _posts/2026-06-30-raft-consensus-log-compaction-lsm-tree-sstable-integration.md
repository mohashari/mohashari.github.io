---
layout: post
title: "Implementing Raft Consensus Log Compaction via LSM-Tree SSTable Integration"
date: 2026-06-30 08:00:00 +0700
tags: [raft, distributed-systems, lsm-tree, sstable, database-design]
description: "Eliminate write amplification and latency spikes in distributed databases by unifying Raft log compaction with LSM-Tree SSTable lifecycles."
image: "https://picsum.photos/seed/5645/1080/720"
thumbnail: "https://picsum.photos/seed/5645/400/300"
---

In high-throughput distributed databases, naive Raft log compaction is a notorious driver of production latency spikes and out-of-memory (OOM) failures. Standard implementations of the Raft consensus protocol periodically serialize the entire in-memory state machine into a monolithic snapshot file to allow log truncation, creating massive disk I/O, CPU bottlenecks, and a double-write tax on data already persisted by the underlying database engine. In production systems under heavy write loads, this stop-the-world serialization routinely triggers write stalls, heartbeat timeouts, and subsequent cluster instability. To solve this, we can eliminate the snapshot overhead entirely by integrating the state machine's Log-Structured Merge (LSM) Tree directly into the Raft log compaction boundary. By leveraging immutable Sorted String Tables (SSTables) as logical snapshots, we can achieve zero-copy, non-blocking compaction that reduces write amplification by up to 60% and maintains sub-millisecond p99.9 write latencies.

![Implementing Raft Consensus Log Compaction via LSM-Tree SSTable Integration Diagram](/images/diagrams/raft-consensus-log-compaction-lsm-tree-sstable-integration.svg)

## The Double-Write Tax of Naive Raft Snapshotting

In a classic decoupled architecture, the consensus module (e.g., `etcd/raft` or `hashicorp/raft`) maintains its own Write-Ahead Log (WAL) to record incoming mutations, while the storage engine (e.g., RocksDB, Pebble, or BadgerDB) operates as a separate state machine. When the Raft WAL grows beyond a configured threshold (such as 1 GB or 1,000,000 entries), the consensus module triggers a snapshot. 

This decoupling introduces severe structural inefficiencies:
1. **Write Amplification:** A single mutation is written first to the Raft WAL, then applied to the storage engine's memory table (MemTable), which eventually flushes it to an LSM L0 SSTable. During a naive snapshot, the storage engine serializes its state back out to a separate snapshot file. Finally, during LSM compaction, the same key is rewritten multiple times across levels (L1 to Ln).
2. **I/O Starvation:** Dumping a 100 GB database state to a monolithic snapshot file saturates disk write bandwidth. Even on enterprise NVMe drives with 3 GB/s write capacity, a snapshot operation can steal critical I/O cycles for dozens of seconds, causing incoming client requests to pile up in memory.
3. **Memory Swings:** Building snapshot structures in memory before writing them to disk can cause memory usage to spike. In Kubernetes environments, this frequently results in container termination via the Out-Of-Memory (OOM) killer.

To bypass these failure modes, we must link the lifecycle of the Raft log directly to the LSM-Tree's flushing mechanism. Instead of treating the storage engine as a black-box state machine, we must treat the immutable SSTables produced by the storage engine as the actual, versioned snapshot of the database state.

## Unified Lifecycle: Mapping Raft Index to SSTable Metadata

An LSM-tree stores data in memory (MemTables) and on disk (SSTables). To unify the lifecycle, we must tag every key-value mutation with the Raft index at which it was committed. We extend the key format at the engine level to store the Raft index as a suffix of the user key. 

By encoding the Raft index directly into the key, we achieve two goals:
1. **Multi-Version Concurrency Control (MVCC):** The database can serve read queries at a specific Raft index (Snapshot Isolation) simply by filtering out keys with a Raft index greater than the read index.
2. **Deterministic Truncation:** We can identify exactly which SSTables contain data corresponding to a specific range of Raft indices.

To ensure that the most recent version of a key is read first during physical scans, keys are sorted by user key ascending, and then by Raft index descending. We achieve descending sort order of the Raft index by bitwise inverting the `uint64` index value during serialization.

The following Go code demonstrates the key encoding and decoding structure used to associate Raft indexes with LSM-Tree mutations:

<script src="https://gist.github.com/mohashari/a18919024b63c84950b5b2d44021802e.js?file=snippet-1.go"></script>

## Non-Blocking Compaction: The Flush Trigger and Manifest Sync

In our integrated architecture, log compaction does not copy data. Instead, it is a coordination sequence between the Raft log manager and the LSM storage engine. 

When Raft decides to compact the log up to index $K$, the following sequence occurs:
1. The Raft log manager requests the storage engine to ensure all mutations up to index $K$ are flushed to disk.
2. The storage engine checks if the active MemTable contains any mutations with an index less than or equal to $K$. If it does, the storage engine freezes the active MemTable and rotates it to an immutable MemTable.
3. A background flush worker writes the immutable MemTable to disk as an L0 SSTable. The metadata footer of this SSTable is tagged with the minimum and maximum Raft indices represented within its key-value pairs.
4. The LSM engine updates its `MANIFEST` file, recording the addition of the new SSTable and its index boundary. This update is committed to disk using `fsync`.
5. Once the `MANIFEST` update is confirmed on disk, the storage engine returns the maximum safely persisted Raft index to the Raft log manager.
6. The Raft log manager truncates its WAL up to this index, reclaiming disk space without writing a single bytes of state machine snapshot.

The coordination code below demonstrates how to orchestrate the MemTable flush and the Raft log truncation safely:

<script src="https://gist.github.com/mohashari/a18919024b63c84950b5b2d44021802e.js?file=snippet-2.go"></script>

## Recovering State from SSTables on Node Startup

When a node crashes and restarts, it must reconstruct its state machine and determine where to resume replaying log entries from the Raft WAL. 

In a traditional setup, the engine loads the latest snapshot file, initializes the database state, and then replays the WAL. This step can take minutes for large databases.

With LSM integration, the recovery process is instantaneous:
1. The node opens the `MANIFEST` file to read the active database version.
2. It parses the metadata of all active SSTables to find the maximum Raft index that was successfully committed and flushed (`MaxPersistedRaftIndex`).
3. The node configures the Raft engine to set its `lastAppliedIndex` to this `MaxPersistedRaftIndex`.
4. The node opens the Raft WAL and replays entries starting from `MaxPersistedRaftIndex + 1`.

Because the active SSTables already represent the exact state up to `MaxPersistedRaftIndex`, there is no snapshot file to read, verify, or load. 

The Go snippet below shows how to parse the manifest on startup and resume WAL replay from the last consistent disk boundary:

<script src="https://gist.github.com/mohashari/a18919024b63c84950b5b2d44021802e.js?file=snippet-3.go"></script>

## Handling Concurrency: Compaction vs. Write-Ahead Logging

Integrating the Raft log with the LSM lifecycle creates concurrency challenges. We must handle two critical scenarios:
1. **Reads During Flushes:** A read transaction must see a consistent snapshot of the data, even if a flush is actively writing a new L0 SSTable or a Raft proposal is being appended to the WAL.
2. **LSM Compactions (L0 -> L1 -> L2):** When background compactions merge multiple SSTables, they discard older versions of keys (overwrites) and delete tombstones. If a background compaction discards a tombstone, it must ensure that a lagging follower that has not yet applied the tombstone does not miss the deletion.

To solve the read isolation issue, we execute reads under Snapshot Isolation. Every read query specifies a `readIndex` (typically the leader's current commit index). When scanning the MemTable and SSTables, the engine filters out any key version with a Raft index greater than the `readIndex`.

The following code illustrates how the engine searches the active MemTable, immutable MemTables, and persisted SSTables while applying the `readIndex` filter:

<script src="https://gist.github.com/mohashari/a18919024b63c84950b5b2d44021802e.js?file=snippet-4.go"></script>

To prevent background compactions from prematurely discarding tombstones, we pass the cluster's oldest active Raft index (the minimum `lastIncludedIndex` among all active cluster members) into the LSM compaction loop. A tombstone can only be discarded if its associated Raft index is older than this value. Otherwise, it must be preserved to prevent data resurrecting when a lagging node catches up.

## Streaming SSTables for Joint Consensus and Catch-Up

When a new node joins the cluster, or when an existing follower falls too far behind, the leader must transmit its current state. In naive systems, this means serializing a new snapshot on the fly, streaming it, and deserializing it on the follower.

With our integrated architecture, the leader uses its active SSTables to stream the snapshot. 

The process is as follows:
1. The leader calls `AcquireSnapshot` on the LSM engine. The LSM engine returns a consistent view of the database by incrementing the reference count of the current set of SSTables, preventing background compactions from deleting them.
2. The leader sends a manifest listing these SSTables to the follower.
3. The leader streams the raw SSTable files over a TCP connection using zero-copy system calls like `sendfile(2)`.
4. The follower writes the incoming files to a temporary staging directory, verifies their checksums, and moves them to its database directory.
5. The follower updates its `MANIFEST` file atomically to point to these new SSTables and resets its WAL to start at the snapshot's maximum Raft index.
6. The leader releases the reference count on the SSTables, allowing normal LSM compactions to resume.

This design reduces CPU usage during catch-up operations from 100% (due to serialization) to near-idle, as data is transferred directly from page cache to the network socket.

The Go code below demonstrates how the leader streams these SSTable files directly using zero-copy mechanisms:

<script src="https://gist.github.com/mohashari/a18919024b63c84950b5b2d44021802e.js?file=snippet-5.go"></script>

## Real-world Failure Modes and Production Mitigation

While integrating the LSM-tree with the Raft log solves the double-write tax, it introduces new failure modes that must be handled in production.

### 1. LSM Write Stalls Block Raft Compaction
Under intense write pressure, the LSM-tree may produce L0 files faster than they can be compacted to L1. To prevent disk read performance from collapsing, the LSM engine triggers a write stall, delaying MemTable flushes. 

If MemTable flushes are delayed, the storage engine cannot notify the Raft log manager that it has persisted index $K$. Consequently, the Raft WAL cannot be truncated. If writes continue to append to the WAL while flushes are stalled, the WAL will grow until the disk runs out of space.

**Mitigation:** We must implement a backpressure mechanism in the Raft proposal loop. The proposal loop must monitor LSM state (such as the number of L0 files and MemTable memory usage). If L0 file count exceeds a critical threshold, the engine must slow down or reject incoming client requests.

The Go middleware below demonstrates how to throttle Raft proposals based on L0 file count and MemTable size to prevent WAL disk runaway:

<script src="https://gist.github.com/mohashari/a18919024b63c84950b5b2d44021802e.js?file=snippet-6.go"></script>

### 2. Orphaned SSTables from Interrupted Catch-Up
If a follower crashes or loses its network connection while receiving SSTable streams from the leader, it may leave partial SSTable files on disk. If these files are loaded on startup, they can corrupt the database or conflict with future writes.

**Mitigation:** The follower must download incoming SSTables into a dedicated staging directory. Only when the full set of files has been received and verified against the leader's manifest checksums should the follower swap the staging files into the active database directory. This directory swap must be followed by a metadata manifest update and a directory `fsync` to ensure atomic state updates.