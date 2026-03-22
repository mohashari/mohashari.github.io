---
layout: post
title: "Vector Search with Metadata Filtering: Making RAG Actually Precise"
date: 2026-03-22 08:00:00 +0700
tags: [rag, vector-search, ai-engineering, databases, backend]
description: "Why vector similarity alone breaks RAG in production, and how metadata filtering at query time fixes retrieval precision without wrecking latency."
---

Your RAG pipeline returns semantically correct documents from the wrong tenant. Or from three years ago when the user asked about current pricing. Or from a knowledge base section the requesting user isn't authorized to see. The embedding similarity score was 0.94—excellent by any measure—but the answer was wrong, stale, or a security violation. This is the failure mode that bites teams after they've already celebrated "RAG is working." Vector similarity finds *related* content; metadata filtering is what makes it find the *right* content. They're not the same problem, and conflating them is why production RAG systems have precision ceilings that better embeddings will never fix.

## Why Pure ANN Retrieval Falls Apart

Approximate Nearest Neighbor search is a distance problem. Given a query vector, return the k vectors closest to it in embedding space. The algorithm doesn't know what a tenant boundary is, what a document's publication date means, or whether a user has read access to a result. It knows cosine similarity.

In a single-tenant, single-domain, permissions-free system this is fine. In every other system—multi-tenant SaaS, regulated industries, time-sensitive domains, large-scale knowledge bases—pure ANN retrieval produces results that are semantically plausible but contextually invalid.

The numbers matter here. If 30% of your document corpus belongs to tenant A and 70% belongs to tenant B, a query from tenant A's user has roughly a 70% baseline probability of pulling in tenant B's documents purely by corpus distribution. No embedding model is going to fix this. You need a hard predicate at query time.

## Pre-filtering vs. Post-filtering: The Core Trade-off

There are two architectural positions on when to apply metadata predicates relative to the ANN search:

**Post-filtering** runs ANN first, retrieves top-k results, then discards anything that doesn't match the metadata predicate. Simple to implement—it's often the first thing teams reach for. It breaks down when the filter is selective. If you're filtering to a single tenant with 2,000 documents out of a 2M document index, retrieving top-100 by similarity and then filtering might leave you with 0 results. You'd need to retrieve top-10,000 to have a reasonable chance of hitting enough tenant documents. That's not retrieval anymore, that's a full scan with extra steps.

**Pre-filtering** applies the metadata predicate first to identify a candidate document set, then runs ANN only within that set. This gives you the correct precision behavior—you're always searching within the right population—but it creates its own problems. Pre-filtering requires the vector index to support partition-level search, which most HNSW implementations don't natively do. You're often rebuilding a sub-index at query time or maintaining separate per-partition indexes.

The practical answer for most production systems is **hybrid filtering**: maintain structured metadata in a relational or columnar store alongside the vector index, use the metadata store to retrieve candidate IDs efficiently (exploiting B-tree or hash indexes), and pass those IDs as an allow-list into the ANN query. This gives you predicate performance from the right data structure and ANN performance from the right data structure, without conflating the two.

## Index Strategies Across Real Systems

### Pinecone: Namespaces and Metadata Indexes

Pinecone's namespace feature is often misused as a metadata filter. Namespaces partition the index completely—cross-namespace search isn't possible, and a query hits exactly one namespace. This is correct for tenant isolation where tenants never need cross-tenant retrieval, but it's wrong for anything more nuanced (like filtering by `document_type` or `date_range` within a shared tenant index).

For flexible filtering, Pinecone supports metadata filters at query time using a MongoDB-style predicate language. The critical operational detail: Pinecone builds inverted indexes on metadata fields that you declare as filterable. Fields you haven't indexed degrade to post-filter behavior internally. Declare your filter fields explicitly.

```python
# snippet-1
import pinecone
from pinecone import Pinecone, ServerlessSpec

pc = Pinecone(api_key="YOUR_API_KEY")

# At upsert time: include all fields you'll filter on as metadata
pc.index("knowledge-base").upsert(vectors=[
    {
        "id": "doc-8821",
        "values": embedding_vector,
        "metadata": {
            "tenant_id": "acme-corp",
            "doc_type": "contract",
            "effective_date": 1704067200,  # unix timestamp for range queries
            "access_level": "confidential",
            "department": "legal"
        }
    }
])

# At query time: compound predicate, runs as pre-filter internally
results = pc.index("knowledge-base").query(
    vector=query_embedding,
    top_k=10,
    filter={
        "tenant_id": {"$eq": "acme-corp"},
        "doc_type": {"$in": ["contract", "amendment"]},
        "effective_date": {"$gte": 1672531200},  # 2023-01-01 onwards
        "access_level": {"$in": ["public", "internal"]}  # exclude confidential
    },
    include_metadata=True
)
```

Watch the performance cliff: if your filter returns fewer than ~1,000 candidate vectors, Pinecone's HNSW index stops being useful and you're closer to brute force. For highly selective filters on large indexes, this is acceptable. For moderately selective filters on medium indexes (100k-1M vectors), benchmark explicitly—you may see p99 latency spike 5-10x compared to unfiltered queries.

### Weaviate: HNSW + Inverted Index Fusion

Weaviate's architecture is more explicit about the hybrid problem. It maintains a separate inverted index for filterable properties alongside the HNSW vector index. At query time, Weaviate decides based on filter selectivity whether to pre-filter (use inverted index to get candidate set, search within it) or use HNSW with post-filter. This decision is internal and heuristic-driven, which means you can't always predict which path executes.

The schema design implication is significant: properties you declare as `filterable: true` get maintained in the inverted index and incur write overhead. Properties you declare as `indexFilterable: true` get a roaring bitmap index, which is dramatically faster for high-cardinality categorical fields like `tenant_id`.

```yaml
# snippet-2
# Weaviate schema for a multi-tenant document store
# Deploy via REST or Go/Python client

classes:
  - class: Document
    vectorizer: none  # bring your own embeddings
    properties:
      - name: tenant_id
        dataType: [text]
        indexFilterable: true   # roaring bitmap - fast for equality/in filters
        indexSearchable: false  # no full-text, just exact match
        
      - name: content
        dataType: [text]
        indexFilterable: false
        indexSearchable: true   # BM25 index for hybrid search if needed
        
      - name: doc_type
        dataType: [text]
        indexFilterable: true
        
      - name: published_at
        dataType: [date]
        indexFilterable: true   # range queries on date need this
        
      - name: access_tier
        dataType: [text]
        indexFilterable: true
    
    vectorIndexConfig:
      ef: 256          # higher ef = more accurate, slower
      efConstruction: 512
      maxConnections: 64
```

```python
# snippet-3
from weaviate.classes.query import Filter, MetadataQuery
import weaviate

client = weaviate.connect_to_local()
collection = client.collections.get("Document")

# Compound filter with date range - Weaviate resolves pre vs post filter internally
response = collection.query.near_vector(
    near_vector=query_embedding,
    limit=10,
    filters=(
        Filter.by_property("tenant_id").equal("acme-corp") &
        Filter.by_property("doc_type").contains_any(["contract", "amendment"]) &
        Filter.by_property("published_at").greater_than("2023-01-01T00:00:00Z") &
        Filter.by_property("access_tier").not_equal("restricted")
    ),
    return_metadata=MetadataQuery(distance=True)
)

for doc in response.objects:
    print(f"{doc.properties['doc_type']} | distance: {doc.metadata.distance:.4f}")
```

### pgvector: SQL Composability as a Feature

pgvector is the option that's underrated in the filtering conversation precisely because it doesn't have this problem. In Postgres, metadata filtering is just a WHERE clause. The query planner already knows how to use B-tree indexes, partial indexes, composite indexes—all the machinery that relational databases have spent decades optimizing.

The trade-off is ANN performance. pgvector's HNSW implementation performs well up to tens of millions of vectors on appropriately spec'd hardware, but it doesn't have the managed horizontal scaling that purpose-built vector databases offer. For systems that fit within that envelope, the SQL composability is worth a lot.

```sql
-- snippet-4
-- Schema with appropriate indexing for filtered vector search
CREATE TABLE documents (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   TEXT NOT NULL,
    doc_type    TEXT NOT NULL,
    access_tier TEXT NOT NULL DEFAULT 'internal',
    published_at TIMESTAMPTZ NOT NULL,
    content     TEXT,
    embedding   vector(1536)  -- OpenAI ada-002 dimension
);

-- Standard indexes for metadata predicates
CREATE INDEX idx_documents_tenant ON documents (tenant_id);
CREATE INDEX idx_documents_tenant_type ON documents (tenant_id, doc_type);

-- Partial HNSW index: one per tenant for large tenants, reduces index size
-- and search space simultaneously
CREATE INDEX idx_documents_embedding_acme ON documents 
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 128)
    WHERE tenant_id = 'acme-corp';

-- For smaller/dynamic tenants, a single full HNSW index with post-filter
CREATE INDEX idx_documents_embedding ON documents 
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 128);
```

```sql
-- snippet-5
-- Query: tenant-scoped ANN search with compound metadata filter
-- Postgres will use partial index idx_documents_embedding_acme for 'acme-corp'
SET LOCAL hnsw.ef_search = 100;  -- higher ef_search = better recall, slower

SELECT
    id,
    doc_type,
    published_at,
    1 - (embedding <=> $1) AS similarity,
    content
FROM documents
WHERE
    tenant_id = 'acme-corp'
    AND doc_type = ANY($2)                          -- $2: '{contract,amendment}'
    AND published_at >= '2023-01-01'::timestamptz
    AND access_tier != 'restricted'
ORDER BY embedding <=> $1
LIMIT 10;

-- EXPLAIN ANALYZE this query. If you see "Index Scan using idx_documents_embedding_acme"
-- you're pre-filtering correctly. "Seq Scan" means your filter selectivity
-- killed the index and you need a different strategy.
```

The partial index approach in pgvector is powerful but has operational overhead: you need one DDL operation per tenant for large tenants, and you need to handle the transition as tenants grow. Start with the full index and migrate to partial indexes for your top-N tenants by document volume.

## The Performance Cliffs You'll Actually Hit

**Filter selectivity below 1%**: In any HNSW-based system, when your filter returns fewer than 1% of the total indexed vectors, the graph traversal becomes expensive relative to the candidate set size. You'll see this as query latency that doesn't scale with index size—it scales with the inverse of filter selectivity. At this point, maintaining a separate smaller index for that partition is almost always faster than trying to filter a large one.

**High-cardinality metadata fields**: Fields like `user_id` with millions of distinct values are expensive to index in inverted indexes. The roaring bitmap per unique value adds up. Use range partitioning or bucketing for high-cardinality numeric fields. For user-level access control, consider encoding permissions as a set membership check rather than per-document per-user metadata.

**Hot partition problem**: In multi-tenant systems, your largest tenants dominate query load. If you're using a single shared index, the ANN traversal for a large tenant query touches more graph nodes than for a small tenant. Tenant-specific indexes solve this but multiply your operational surface area. The right answer depends on your tenant size distribution—if it's power-law (a few huge tenants, many small ones), explicit indexes for the top decile and a shared index for the rest is a reasonable approach.

**Write amplification on metadata updates**: If your metadata changes (document reclassified, access tier changed, tenant reassigned), you're paying for a vector upsert even though the embedding hasn't changed. Separate your metadata store from your vector store explicitly—use Postgres for metadata, use the vector DB for vectors, join them at query time using the ID-based allow-list approach. This gives you O(1) metadata updates without touching the vector index.

```python
# snippet-6
# ID-based allow-list approach: resolve metadata in Postgres, search vectors with ID filter
# Works across any vector DB that supports filtering by ID set

import asyncpg
import pinecone

async def retrieve_with_access_control(
    query_embedding: list[float],
    tenant_id: str,
    user_permissions: list[str],
    top_k: int = 10,
    pg_pool: asyncpg.Pool = None,
    pc_index = None,
) -> list[dict]:
    
    # Phase 1: Resolve candidate IDs from Postgres (uses B-tree indexes, fast)
    candidate_ids = await pg_pool.fetch(
        """
        SELECT id::text FROM documents
        WHERE tenant_id = $1
          AND access_tier = ANY($2)
          AND published_at >= NOW() - INTERVAL '2 years'
        LIMIT 50000  -- cap the allow-list size to avoid huge Pinecone filter payloads
        """,
        tenant_id,
        user_permissions
    )
    
    if not candidate_ids:
        return []
    
    id_list = [r["id"] for r in candidate_ids]
    
    # Phase 2: ANN search within the candidate set
    # Pinecone supports $in on the reserved _id field
    results = pc_index.query(
        vector=query_embedding,
        top_k=top_k,
        filter={"_id": {"$in": id_list}},
        include_metadata=True
    )
    
    return results.matches
```

The 50,000 ID cap in that query is deliberate. Vector databases that accept ID allow-lists have payload size limits and internal set intersection costs. Benchmark what your vector DB handles efficiently—Pinecone's sweet spot is under 10,000 IDs in a filter payload before you see latency degrade. If your candidate set is larger, you need the pre-filter to happen natively in the vector DB, which brings you back to the declarative metadata filter approach.

## Getting Index Design Right Upfront

The mistake that's hard to recover from: deploying with metadata as an afterthought, then discovering you need filtering at scale when you already have 50M vectors indexed.

Questions to answer before you design your vector index:

**What are your hard partition boundaries?** Tenant ID, region, data classification level—anything where mixing results is a correctness violation, not just a relevance problem. These must be pre-filter fields, declared in your schema, indexed from day one.

**What are your soft filter dimensions?** Date ranges, document types, categories—things that improve relevance but aren't hard requirements. These can tolerate some post-filter behavior and are lower priority for dedicated indexing.

**What's your document growth rate per partition?** If a tenant will have 10M documents in 18 months, they need their own index or a partition-aware architecture. If your entire corpus is 500k documents and growing slowly, a single HNSW index with metadata filtering is fine.

**What's your latency budget?** A filter that doubles your p99 latency might be acceptable if your baseline is 20ms and unacceptable if your baseline is 100ms. Know your budget before you hit production.

Metadata filtering is infrastructure, not configuration. It determines whether your retrieval is correct, not just whether it's fast. Treat it as a first-class design concern from the start, because retrofitting it onto a deployed system is one of the more painful data engineering experiences you can have.
```