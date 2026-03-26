---
layout: post
title: "PostgreSQL Logical Replication: Slot Management and Backpressure"
date: 2026-03-26 08:00:00 +0700
tags: [postgresql, replication, database, reliability, backend]
description: "How PostgreSQL logical replication slots accumulate WAL and silently fill your disk — and what to actually do about it in production."
image: ""
thumbnail: ""
---

At 2 AM, your on-call engineer gets paged: the primary Postgres node is out of disk. The application is still writing, but now it's throwing errors. The culprit isn't a runaway query or a forgotten backup job — it's a logical replication slot that was created six weeks ago for a CDC pipeline, whose consumer died three days ago. The slot kept accumulating WAL, retained every single change since the consumer last confirmed receipt, and quietly ate 800 GB of your 1 TB disk while nobody was looking.

This is not a contrived scenario. It happens regularly in production systems that use logical replication for CDC, read replicas, or cross-database sync. Understanding slot management and backpressure mechanics is the difference between a system that degrades gracefully and one that takes down your primary at the worst possible moment.

## How Logical Replication Slots Work

A logical replication slot is a persistent bookmark in the WAL stream. When you create one, Postgres commits to retaining all WAL data from that point forward until the slot's `confirmed_flush_lsn` advances. The slot tracks two positions:

- `restart_lsn`: the oldest WAL position Postgres must keep on disk to replay the slot
- `confirmed_flush_lsn`: the position the consumer has explicitly acknowledged

The gap between these two values represents unconfirmed WAL. If your consumer falls behind — or dies entirely — this gap grows. Postgres cannot reclaim that WAL for other purposes. It cannot be cleaned up by autovacuum checkpoint rotation. It will not be deleted by `wal_keep_size`. The slot holds it open indefinitely.

This is intentional and correct behavior. The whole point of logical slots is to guarantee no data loss for consumers. The problem is that this guarantee comes with a resource obligation that operators often don't plan for.

```sql
-- snippet-1
-- Inspect all replication slots and their lag
SELECT
    slot_name,
    plugin,
    slot_type,
    active,
    active_pid,
    pg_size_pretty(
        pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)
    ) AS retained_wal,
    pg_size_pretty(
        pg_wal_lsn_diff(pg_current_wal_lsn(), confirmed_flush_lsn)
    ) AS consumer_lag,
    EXTRACT(EPOCH FROM (now() - pg_last_xact_replay_timestamp())) AS replica_delay_seconds
FROM pg_replication_slots
ORDER BY pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn) DESC;
```

The `retained_wal` column is your disk liability. Every byte there is WAL that cannot be cleaned up. In a write-heavy OLTP system, this can grow at hundreds of megabytes per minute.

## The Inactive Slot Problem

An inactive slot — `active = false` with no `active_pid` — is a ticking clock. Postgres has no automatic mechanism to expire or drop idle slots. They accumulate until either a consumer reconnects or someone manually intervenes.

The failure mode is not linear. WAL accumulation starts slow when your write load is light, then explodes during peak traffic or bulk operations. A slot that sat dormant for two days with 20 GB of retained WAL can suddenly have 200 GB if a batch job runs overnight.

```sql
-- snippet-2
-- Detect dangerous inactive slots before they cause an outage
WITH slot_metrics AS (
    SELECT
        slot_name,
        plugin,
        active,
        active_pid,
        pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn) AS retained_bytes,
        pg_wal_lsn_diff(pg_current_wal_lsn(), confirmed_flush_lsn) AS lag_bytes
    FROM pg_replication_slots
)
SELECT
    slot_name,
    plugin,
    active,
    pg_size_pretty(retained_bytes) AS retained_wal,
    pg_size_pretty(lag_bytes) AS consumer_lag,
    CASE
        WHEN NOT active AND retained_bytes > 10 * 1024^3 THEN 'CRITICAL'
        WHEN NOT active AND retained_bytes > 1 * 1024^3 THEN 'WARNING'
        WHEN active AND lag_bytes > 5 * 1024^3 THEN 'WARNING'
        ELSE 'OK'
    END AS status
FROM slot_metrics
WHERE retained_bytes > 0
ORDER BY retained_bytes DESC;
```

Set a Prometheus alert on this. The query is cheap enough to run every 60 seconds. Use the `pg_replication_slots` view from a monitoring connection that doesn't itself create slots.

## WAL Retention Math

Your disk budget for logical replication needs to account for worst-case consumer lag, not steady-state. The formula is:

```
required_disk = peak_wal_generation_rate × max_acceptable_consumer_lag × safety_factor
```

For a system generating 500 MB/s of WAL at peak, with a CDC consumer you'd tolerate being 30 minutes behind, you need at least 900 GB reserved just for that slot's potential WAL accumulation — before counting any other slots, the base data, or indexes.

Most teams don't plan for this. They provision for average WAL generation rates and get surprised the first time their Kafka consumer has a network partition during a high-write window.

```bash
# snippet-3
# Calculate actual WAL generation rate over a 5-minute window
# Run this on the primary to understand your baseline

psql -c "
WITH start_pos AS (SELECT pg_current_wal_lsn() AS lsn, now() AS ts)
SELECT pg_sleep(300)
\gexec

SELECT
    pg_size_pretty(
        pg_wal_lsn_diff(
            pg_current_wal_lsn(),
            (SELECT lsn FROM start_pos)
        )
    ) AS wal_generated_5min,
    pg_size_pretty(
        pg_wal_lsn_diff(
            pg_current_wal_lsn(),
            (SELECT lsn FROM start_pos)
        ) / 300
    ) AS wal_per_second;
"
```

In practice, use `pg_stat_bgwriter` combined with WAL file timestamps to get continuous metrics. Export these to your observability stack — Grafana, Datadog, whatever you use — and graph retained WAL per slot over time. Flat lines are safe; exponential curves are incidents in progress.

## Backpressure Mechanics

Logical replication does not implement native backpressure. The producer (Postgres WAL sender) will continue generating data regardless of whether the consumer can keep up. When replication lag grows, Postgres doesn't throttle writes on the primary — it just retains more WAL.

This asymmetry means backpressure has to be implemented at the consumer. For Debezium-based CDC pipelines, this looks like:

```properties
# snippet-4
# Debezium PostgreSQL connector configuration with backpressure handling
# connector.properties

connector.class=io.debezium.connector.postgresql.PostgresConnector
plugin.name=pgoutput
slot.name=debezium_cdc_prod
publication.autocreate.mode=filtered

# Backpressure: limit in-flight records to prevent memory pressure
max.batch.size=2048
max.queue.size=8192
max.queue.size.in.bytes=268435456

# Heartbeat: keep slot active and confirm LSN even with low traffic tables
heartbeat.interval.ms=10000
heartbeat.action.query=INSERT INTO _debezium_heartbeat (ts) VALUES (now()) ON CONFLICT (id) DO UPDATE SET ts = now()

# Avoid slot accumulation on restarts: use snapshot mode appropriately
snapshot.mode=exported

# Critical: limit how far behind before signaling unhealthy
slot.drop.on.stop=false
```

The `heartbeat.interval.ms` setting deserves attention. Without heartbeats, a connector consuming from a low-traffic table will have its `confirmed_flush_lsn` frozen even while other tables advance. The retained WAL includes everything from all tables, not just the ones the connector cares about. Heartbeats force LSN advancement on a schedule.

## Slot Lifecycle Management

In production you need tooling for the full slot lifecycle: creation, monitoring, and controlled deletion. Dropping a slot drops all retained WAL for that slot immediately — useful in emergencies but data-losing if the consumer needed those records.

```python
# snippet-5
# Slot management script with safety checks
# Usage: python slot_manager.py --action inspect|drop-safe|emergency-drop

import psycopg2
import sys
import argparse
from dataclasses import dataclass
from typing import Optional

@dataclass
class SlotStatus:
    name: str
    active: bool
    active_pid: Optional[int]
    retained_bytes: int
    lag_bytes: int
    plugin: str

def get_slot_status(conn) -> list[SlotStatus]:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT
                slot_name,
                active,
                active_pid,
                pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn),
                pg_wal_lsn_diff(pg_current_wal_lsn(), confirmed_flush_lsn),
                plugin
            FROM pg_replication_slots
        """)
        return [SlotStatus(*row) for row in cur.fetchall()]

def drop_slot_safe(conn, slot_name: str, max_lag_bytes: int = 1_073_741_824):
    """Drop slot only if it's inactive and consumer lag exceeds threshold."""
    slots = {s.name: s for s in get_slot_status(conn)}
    slot = slots.get(slot_name)

    if not slot:
        raise ValueError(f"Slot {slot_name!r} not found")
    if slot.active:
        raise RuntimeError(f"Slot {slot_name!r} is active (pid {slot.active_pid}). Refusing to drop.")
    if slot.lag_bytes < max_lag_bytes:
        raise RuntimeError(
            f"Slot lag {slot.lag_bytes / 1e9:.2f} GB below threshold "
            f"{max_lag_bytes / 1e9:.2f} GB. Is consumer actually dead?"
        )

    with conn.cursor() as cur:
        cur.execute("SELECT pg_drop_replication_slot(%s)", (slot_name,))
    conn.commit()
    print(f"Dropped slot {slot_name!r}, freed {slot.retained_bytes / 1e9:.2f} GB of WAL")
```

The safety check matters. Dropping an active slot will raise an error from Postgres itself, but it's easy to script yourself into dropping a slot whose consumer just restarted. The lag threshold check adds a second layer of confidence: if lag is below 1 GB, maybe the consumer is running and just slow.

## max_slot_wal_keep_size: Your Last Line of Defense

PostgreSQL 13 added `max_slot_wal_keep_size`. Set it.

```sql
-- snippet-6
-- Set a hard cap on WAL retained per slot
-- Add to postgresql.conf or use ALTER SYSTEM

ALTER SYSTEM SET max_slot_wal_keep_size = '50GB';
SELECT pg_reload_conf();

-- Verify the setting
SHOW max_slot_wal_keep_size;

-- When a slot exceeds this limit, it becomes "invalidated"
-- Check for invalidated slots:
SELECT slot_name, invalidation_reason, wal_status
FROM pg_replication_slots
WHERE wal_status != 'reserved';
-- wal_status values: reserved, extended, unreserved, lost
```

When a slot is invalidated, its WAL is freed and the slot is marked `wal_status = 'lost'`. The consumer will get an error on next connect: `ERROR: requested WAL segment has already been removed`. This is data loss from the slot's perspective — the consumer missed some changes and must re-snapshot.

This is the correct tradeoff. Controlled data loss for a CDC consumer is recoverable. Disk exhaustion on your primary is not.

Set `max_slot_wal_keep_size` to a value your disk can absorb across all active slots. If you have three slots and 500 GB free for WAL, set it to 150 GB to give each slot headroom. Monitor `wal_status` to catch invalidations before your consumers do.

## Monitoring Stack Integration

Wrap the diagnostics into Prometheus metrics. The `postgres_exporter` from Prometheus community supports custom queries:

```yaml
# snippet-7
# postgres_exporter custom queries (queries.yaml)
# Add to your postgres_exporter configuration

pg_replication_slots:
  query: |
    SELECT
      slot_name,
      plugin,
      CASE WHEN active THEN 1 ELSE 0 END AS active,
      pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn) AS retained_bytes,
      pg_wal_lsn_diff(pg_current_wal_lsn(), confirmed_flush_lsn) AS lag_bytes,
      CASE wal_status
        WHEN 'reserved' THEN 0
        WHEN 'extended' THEN 1
        WHEN 'unreserved' THEN 2
        WHEN 'lost' THEN 3
        ELSE -1
      END AS wal_status_code
    FROM pg_replication_slots
  metrics:
    - slot_name:
        usage: "LABEL"
        description: "Replication slot name"
    - plugin:
        usage: "LABEL"
        description: "Output plugin"
    - active:
        usage: "GAUGE"
        description: "Whether the slot has an active consumer"
    - retained_bytes:
        usage: "GAUGE"
        description: "Bytes of WAL retained by this slot"
    - lag_bytes:
        usage: "GAUGE"
        description: "Bytes of unconfirmed WAL (consumer lag)"
    - wal_status_code:
        usage: "GAUGE"
        description: "WAL status: 0=reserved, 1=extended, 2=unreserved, 3=lost"
```

Alert on `retained_bytes > threshold` for any inactive slot. Alert on `wal_status_code >= 2` (unreserved) immediately — you're close to invalidation. Alert on `wal_status_code == 3` as a critical that requires human intervention.

## Operational Runbook

When you get an alert that a slot's retained WAL is growing:

1. Check `pg_replication_slots` to confirm the slot is inactive (`active = false`).
2. Check application logs for the consumer — is it in a restart loop, behind a failed network path, or misconfigured?
3. If the consumer is recoverable within your disk budget window, let it reconnect and catch up.
4. If the consumer is down for an unknown duration, drop the slot and plan for a re-snapshot. This is the correct call. Do not let an inactive slot sit for more than 30 minutes if it's accumulating at meaningful rates.
5. After dropping, alert the consumer team with the LSN at which the slot was dropped, so they know the gap to fill via snapshot.

Establish upfront that replication slots are not durable message queues. They are streaming bookmarks. If your CDC consumer needs guaranteed delivery across arbitrary outages, that guarantee lives in Kafka, not in Postgres.

The discipline of treating slots as short-lived operational state — rather than permanent data contracts — is what keeps logical replication stable in production. Create them with purpose, monitor them aggressively, and drop them without sentimentality when consumers fall behind beyond recovery.