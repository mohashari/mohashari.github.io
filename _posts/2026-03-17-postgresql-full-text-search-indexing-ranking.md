---
layout: post
title: "PostgreSQL Full-Text Search: Indexing, Ranking, and Going Beyond LIKE Queries"
date: 2026-03-17 07:00:00 +0700
tags: [postgresql, search, databases, performance, indexing]
description: "Replace slow LIKE queries with PostgreSQL's native full-text search engine, leveraging tsvector, tsquery, GIN indexes, and ranking functions for fast, relevant results."
---

# PostgreSQL Full-Text Search: Indexing, Ranking, and Going Beyond LIKE Queries

Every backend engineer has written it: `WHERE title LIKE '%search term%'`. It works in development, it passes code review, and then it silently destroys production performance the moment your table grows past a few hundred thousand rows. A leading wildcard forces a sequential scan — no index can help you — and you're left watching query times balloon from milliseconds to seconds. PostgreSQL ships with a mature, powerful full-text search engine built right into the database. It supports linguistic processing, relevance ranking, phrase search, and GIN indexes that keep queries fast at scale. This post walks through replacing your LIKE queries with `tsvector`, `tsquery`, and the surrounding machinery that makes it production-worthy.

## Understanding tsvector and tsquery

Full-text search in PostgreSQL operates on two types. A `tsvector` is a sorted list of lexemes — normalized word stems — derived from a document. A `tsquery` is a parsed search expression. The `@@` operator checks whether a query matches a vector. PostgreSQL handles stemming, stop-word removal, and normalization through text search configurations (dictionaries). The default `english` configuration knows that "running", "runs", and "ran" all stem to "run".

<script src="https://gist.github.com/mohashari/ae9c33254012b6f199979d3f40fe51a3.js?file=snippet.sql"></script>

## Setting Up a Searchable Table

A common pattern is to maintain a dedicated `search_vector` column of type `tsvector`, populated from multiple source columns with different weights. PostgreSQL's `setweight` function assigns labels A through D (A being most significant), which the ranking functions later use. This lets you boost matches in a title over matches in body text.

<script src="https://gist.github.com/mohashari/ae9c33254012b6f199979d3f40fe51a3.js?file=snippet-2.sql"></script>

## Keeping the Vector Fresh with a Trigger

Manually updating `search_vector` on every write is error-prone. A trigger ensures the column stays current automatically. PostgreSQL provides `tsvector_update_trigger` as a built-in convenience, but a custom trigger gives you the weighted multi-column control shown above.

<script src="https://gist.github.com/mohashari/ae9c33254012b6f199979d3f40fe51a3.js?file=snippet-3.sql"></script>

## Creating the GIN Index

A GIN (Generalized Inverted Index) is the right index type for `tsvector` columns. It stores a mapping from each lexeme to the rows that contain it, making lookups O(log n) rather than sequential scans. Creating it is a single statement, though on large tables you may want `CREATE INDEX CONCURRENTLY` to avoid locking.

<script src="https://gist.github.com/mohashari/ae9c33254012b6f199979d3f40fe51a3.js?file=snippet-4.sql"></script>

## Querying and Ranking Results

With the index in place, you can run ranked searches. `ts_rank` scores each match based on term frequency and weight class. `ts_rank_cd` considers cover density — how close matching terms appear to each other — which often produces better results for phrase-like queries. Normalizing the rank by document length (normalization option `32`) prevents longer articles from dominating purely by word count.

<script src="https://gist.github.com/mohashari/ae9c33254012b6f199979d3f40fe51a3.js?file=snippet-5.sql"></script>

`ts_headline` generates a contextual snippet with matching terms highlighted — ready to render directly in search results. The `StartSel`/`StopSel` options let you inject any HTML or markup around matched terms.

## Querying from Go

In application code you want to pass user input safely without string interpolation. The `websearch_to_tsquery` function (available since PostgreSQL 11) accepts natural language input — spaces become AND, quoted phrases become phrase queries, a leading minus negates a term — making it suitable for a search box without requiring users to learn tsquery syntax.

<script src="https://gist.github.com/mohashari/ae9c33254012b6f199979d3f40fe51a3.js?file=snippet-6.go"></script>

`websearch_to_tsquery` safely handles empty strings and malformed input without panicking or causing SQL errors, which makes it a practical default for user-facing search.

## Handling Multi-Language Content

When your content spans multiple languages, the `english` configuration no longer cuts it. PostgreSQL ships with configurations for dozens of languages. A practical approach is to store the detected language alongside each document and use it during both indexing and querying.

<script src="https://gist.github.com/mohashari/ae9c33254012b6f199979d3f40fe51a3.js?file=snippet-7.sql"></script>

PostgreSQL's full-text search is not Elasticsearch — it won't match typos, it has no neural ranking, and it requires your text to be in the database rather than a separate service. But for the vast majority of search features that backend teams ship — article search, product catalogs, documentation lookup, user-generated content — it is fast enough, operationally free, and dramatically better than anything built on LIKE. The combination of a trigger-maintained `tsvector` column, a GIN index, `ts_rank_cd` for relevance, and `ts_headline` for snippets gives you a search experience indistinguishable from a dedicated engine, with zero additional infrastructure to operate, monitor, or pay for. Start there, measure your query times, and only reach for external tooling when you have a concrete need that PostgreSQL cannot meet.