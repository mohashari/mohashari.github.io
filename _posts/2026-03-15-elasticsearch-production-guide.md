---
layout: post
title: "ElasticSearch in Production: Indexing, Querying, and Tuning"
date: 2026-03-15 07:00:00 +0700
tags: [elasticsearch, search, backend, performance, indexing]
description: "Build and tune Elasticsearch clusters for production — covering index design, query optimization, and cluster sizing."
---

Most backend engineers reach for Elasticsearch when a SQL `LIKE` query starts crawling and full-text search becomes a product requirement. But standing up a cluster that actually survives production — with consistent query latency under load, no runaway JVM heap, and indices that don't fragment into chaos — is a different discipline entirely. The gap between "it works in staging" and "it works at 3 AM on Black Friday" is filled with index mapping mistakes, over-allocated shards, and queries that bypass caches at exactly the wrong moment. This post walks through the decisions that matter: mapping design before you write a single document, query patterns that scale, and the operational knobs that keep clusters healthy.

## Index Mapping: Get It Right Before Data Arrives

Elasticsearch infers mappings dynamically, and that convenience is a trap. Dynamic mapping turns every new string field into a `text` field with a `keyword` sub-field, bloating your index and slowing indexing throughput. Define explicit mappings before ingesting a single document. Think carefully about what you need to search versus what you need to filter or sort — `text` fields are analyzed and tokenized; `keyword` fields are not. Using the wrong one makes queries either impossible or expensive.

<script src="https://gist.github.com/mohashari/e4c5e128b6181dec62aafd2c3705ec7a.js?file=snippet.json"></script>

Setting `"dynamic": "strict"` forces Elasticsearch to reject documents with unmapped fields rather than silently creating new ones. This is painful to enforce early and saves enormous refactoring pain later.

## Indexing Documents from Go

The official Go client (`go-elasticsearch`) handles connection pooling and retry logic, but the most impactful throughput improvement comes from bulk indexing rather than individual document writes. Every single-document index request is a network round-trip plus a flush negotiation. Bulk requests amortize that cost across hundreds of documents.

<script src="https://gist.github.com/mohashari/e4c5e128b6181dec62aafd2c3705ec7a.js?file=snippet-2.go"></script>

Never set `refresh=true` on bulk requests in production. It forces a Lucene segment flush after every call, destroying write throughput and hammering I/O.

## Writing Queries That Use Filters

Elasticsearch's query DSL has two execution paths: the query context and the filter context. Queries in the query context compute a relevance score — they're slower and not cached. Filters do not score; they're fast and automatically cached by the segment filter cache. Move every non-relevance condition — status checks, date ranges, numeric bounds — into a `filter` clause.

<script src="https://gist.github.com/mohashari/e4c5e128b6181dec62aafd2c3705ec7a.js?file=snippet-3.json"></script>

The `_source` filtering is equally important at scale. Fetching full documents when you only need three fields wastes network bandwidth and slows serialization on every node in the fetch phase.

## Paginating Safely with search_after

`from/size` pagination is convenient but catastrophic at depth. Fetching page 1000 with `size=20` requires Elasticsearch to collect and sort 20,020 documents internally across every shard, then discard 20,000 of them. Use `search_after` with a deterministic sort for any pagination beyond the first few pages.

<script src="https://gist.github.com/mohashari/e4c5e128b6181dec62aafd2c3705ec7a.js?file=snippet-4.go"></script>

The tiebreaker sort on a unique field (`order_id`) prevents documents from being skipped or duplicated when multiple records share the same `created_at` timestamp.

## JVM and Heap Configuration

Elasticsearch runs on the JVM, and the heap configuration is where most production cluster problems originate. The rules are strict: set `-Xms` and `-Xmx` to the same value to prevent heap resizing pauses, never exceed 30–31 GB (crossing the compressed ordinary object pointer boundary forces the JVM to use 64-bit pointers, effectively shrinking your usable heap), and leave half your RAM for the OS filesystem cache — Elasticsearch relies heavily on it for Lucene segment access.

<script src="https://gist.github.com/mohashari/e4c5e128b6181dec62aafd2c3705ec7a.js?file=snippet-5.sh"></script>

On a 64 GB node, 16 GB for the JVM heap and 48 GB for the OS page cache is a reasonable starting point. Monitor the `jvm.mem.heap_used_percent` metric; above 75% consistently means your heap is undersized for your query pattern.

## Index Lifecycle Management for Time-Series Data

Logs, events, and time-series indices grow without bound unless you manage them explicitly. ILM automates the rollover → warm → cold → delete pipeline, moving indices to cheaper hardware as they age and ultimately removing them when retention expires.

<script src="https://gist.github.com/mohashari/e4c5e128b6181dec62aafd2c3705ec7a.js?file=snippet-6.json"></script>

The `forcemerge` in the warm phase collapses hundreds of small Lucene segments into one, dramatically reducing memory overhead and speeding up read queries on historical data that no longer receives writes.

## Monitoring the Metrics That Matter

Instrument these cluster-level metrics in your monitoring stack. Anything else is noise until these are green.

<script src="https://gist.github.com/mohashari/e4c5e128b6181dec62aafd2c3705ec7a.js?file=snippet-7.sh"></script>

The slow query log is invaluable for finding queries that bypass filters or hit unmapped fields. Review it weekly during early production ramp-up and you will catch the majority of query anti-patterns before they become incidents.

Elasticsearch rewards up-front discipline. The clusters that run smoothly at scale share the same properties: explicit mappings with `strict` dynamic mode, bulk ingestion pipelines that avoid hot-refresh, queries that push non-relevance conditions into filter context, and ILM policies that prevent runaway index growth. Get those four foundations right before optimizing anything else. JVM tuning and shard sizing matter, but they matter far less than a query that scores ten million documents because someone forgot to add a `filter` clause. Build the index for the query pattern you know you have today, measure under realistic load, and let the data tell you where to tune next.