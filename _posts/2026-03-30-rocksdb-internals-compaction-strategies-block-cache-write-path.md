---
layout: post
title: "RocksDB Internals: Compaction Strategies, Block Cache Tuning, and Write Path Optimization"
date: 2026-03-30 08:00:00 +0700
tags: [rocksdb, storage-engines, performance, databases, systems]
description: "A production-focused deep dive into RocksDB compaction strategies, block cache configuration, and write path tuning for high-throughput workloads."
image: "https://picsum.photos/1080/720?random=5313"
thumbnail: "https://picsum.photos/400/300?random=5313"
---

You're three weeks into production with your new metadata store. Write throughput looks fine in staging. Then your on-call fires at 2 AM: p99 read latency is 800ms, disk I/O is pegged at 100%, and your RocksDB-backed service is thrashing. The culprit is almost always the same: compaction is either too aggressive, consuming disk bandwidth you need for writes, or too lazy, letting SST file counts balloon until read amplification kills you. Understanding RocksDB's internals isn't academic—it's what separates a storage layer that holds up at 200K writes/sec from one that melts under sustained load.

## The LSM-Tree Write Path: What Actually Happens

Every write to RocksDB goes through a well-defined sequence: WAL → active memtable → immutable memtable → L0 SST files → compaction into deeper levels. Understanding each handoff is where tuning starts.

When you call `Put()`, RocksDB serializes the key-value pair into a write batch, appends it to the Write-Ahead Log (WAL), then inserts it into the active memtable (a skip list or hash-linked-list structure in memory). When the memtable hits `write_buffer_size` (default 64MB), it becomes immutable and a new active memtable takes writes. A background thread flushes immutable memtables to L0 SST files. This is where write stalls begin.

<script src="https://gist.github.com/mohashari/26fcfd11cc73fb3dfe67372c2f2ae0ca.js?file=snippet-1.txt"></script>

The L0 file count is your canary. When it climbs toward `level0_slowdown_writes_trigger`, RocksDB artificially delays writes. When it hits `level0_stop_writes_trigger`, all writes block. This is the write stall most engineers hit first.

## Compaction Strategies: Leveled vs Universal vs FIFO

RocksDB ships three compaction styles. Picking the wrong one for your workload is a source of chronic production pain.

**Leveled Compaction** (default) minimizes read amplification at the cost of write amplification. Files in each level are non-overlapping and sorted. A read touches at most one SST per level. The trade-off: each byte you write may be rewritten 10–30x across compaction levels before it reaches Lmax. For workloads where read latency matters more than disk write bandwidth, leveled is correct.

**Universal Compaction** reduces write amplification by merging runs instead of levels. All SST files are sorted runs; compaction merges the smallest ones together when size ratio exceeds a threshold. Write amplification drops to roughly 10x in steady state versus 30x+ for leveled. The cost: read amplification increases because a read may scan multiple overlapping runs. For write-heavy workloads—event ingestion, audit logs, metric writes—where reads are rare or time-bounded, universal wins.

**FIFO Compaction** is for time-series data with TTL. It keeps the most recently written SST files and deletes the oldest when total size exceeds a limit. No merging, minimal CPU overhead. Not suitable for anything requiring full key lookups.

<script src="https://gist.github.com/mohashari/26fcfd11cc73fb3dfe67372c2f2ae0ca.js?file=snippet-2.txt"></script>

The `max_size_amplification_percent = 200` setting means RocksDB will trigger a full compaction when live data is less than half the total SST size—i.e., more than 50% of space is dead data. In practice, keep this between 100 and 300 depending on your space budget.

## Block Cache: The Knob You're Probably Under-Tuning

The block cache sits in front of SST file reads. A cache miss means a synchronous disk read, which on even fast NVMe is 100–300µs versus sub-microsecond for a cache hit. Most engineers set this once and forget it.

RocksDB's default block cache (LRU) is a single shard. At high concurrency, this becomes a mutex contention bottleneck. The fix is `NewLRUCache` with enough shards to eliminate contention.

<script src="https://gist.github.com/mohashari/26fcfd11cc73fb3dfe67372c2f2ae0ca.js?file=snippet-3.txt"></script>

The `high_pri_pool_ratio` parameter reserves a fraction of the cache for high-priority blocks (index and filter blocks). Without this, a full table scan can evict your entire bloom filter set, turning the next 30 seconds of point lookups into disaster. Set it to at least 0.1 in any mixed workload.

Monitor `rocksdb.block.cache.hit` and `rocksdb.block.cache.miss` via the statistics interface. A hit rate below 85% on a read-heavy workload means you either need more cache or your working set is too large to cache—at which point you need to reconsider your data access patterns.

## Write Path Optimization: WAL, Sync Modes, and Rate Limiting

The WAL is a sequential append-only file. Every write is durable only after it syncs to disk. The default `sync` mode calls `fsync()` per write batch, which caps you at roughly the IOPS of your storage device for small writes.

<script src="https://gist.github.com/mohashari/26fcfd11cc73fb3dfe67372c2f2ae0ca.js?file=snippet-4.txt"></script>

One often-missed optimization: `enable_pipelined_write = true`. With pipelining, the WAL write and memtable insert happen concurrently for successive batches. On high-concurrency workloads (50+ writer threads), this yields 20–40% throughput improvement with no durability trade-off.

## Rate Limiting Compaction I/O

If you're sharing a disk with other services—or even just have RocksDB as one of several column families on the same device—uncontrolled compaction will steal I/O from foreground reads. The rate limiter is the correct tool.

<script src="https://gist.github.com/mohashari/26fcfd11cc73fb3dfe67372c2f2ae0ca.js?file=snippet-5.txt"></script>

The `refill_period_us` controls burst size. With 200MB/s and 100ms refill, the burst budget is 20MB. A single large compaction won't immediately eat all bandwidth; it gets tokens refilled at the configured rate.

## Monitoring: What to Watch in Production

You can't tune what you can't see. RocksDB exposes a rich statistics interface, but most deployments only log it to a file and never alert on it.

```bash
# snippet-6
# Key metrics to pull from RocksDB statistics for a Prometheus-style setup
# Expose via your application's metrics endpoint or parse LOG files

# Write path health
rocksdb.write.stall                   # cumulative microseconds spent stalled
rocksdb.memtable.hit / miss           # memtable lookup efficiency
rocksdb.number.keys.written           # total keys written

# Compaction health  
rocksdb.compact.read.bytes            # bytes read during compaction
rocksdb.compact.write.bytes           # bytes written during compaction
rocksdb.estimate-pending-compaction-bytes  # via GetIntProperty()

# Read path health
rocksdb.block.cache.hit               # block cache hits
rocksdb.block.cache.miss              # block cache misses
rocksdb.bloom.filter.useful           # bloom filter saved a disk read
rocksdb.bloom.filter.full.positive    # bloom filter false positive (expensive)

# Space amplification
rocksdb.live-sst-files-size           # actual data size
rocksdb.total-sst-files-size          # total SST size including dead data
# Space amp = total / live; alert if > 2.5 for leveled, > 3.0 for universal
```

The metric I watch most closely in production: `rocksdb.write.stall`. Any nonzero value means you have a compaction debt problem. If it's nonzero for more than 10 seconds in a rolling minute, you need to either reduce write rate, increase compaction parallelism, or switch compaction styles.

## Putting It Together: Column Families as Isolation Units

One final architectural point: use column families to isolate workloads with different access patterns within a single RocksDB instance. A column family is not just a logical namespace—it has its own memtable, compaction options, and block cache priority.

<script src="https://gist.github.com/mohashari/26fcfd11cc73fb3dfe67372c2f2ae0ca.js?file=snippet-7.txt"></script>

The interaction between column families and the shared block cache is worth understanding: all CFs share the cache by default. If your cold CF is running a compaction that's doing large sequential reads, it can evict hot CF's bloom filters from cache. The `pin_l0_filter_and_index_blocks_in_cache` option mitigates this for L0 files, but for high-contention scenarios, consider giving column families separate cache instances via `BlockBasedTableOptions`.

RocksDB rewards the engineer who reads the source and measures. The defaults are sane for general workloads, but every production database I've seen that actually handles serious write throughput has at least `write_buffer_size`, `max_background_jobs`, `level0_slowdown_writes_trigger`, and block cache size explicitly set. Start there, instrument everything, and let your actual write amplification numbers drive compaction style selection. The theory is useful; the disk I/O graphs tell the truth.
```