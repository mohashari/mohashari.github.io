---
layout: post
title: "Hybrid Search: Combining Dense and Sparse Retrieval for Better RAG"
date: 2026-03-22 08:00:00 +0700
tags: [rag, vector-search, information-retrieval, ai-engineering, python]
description: "Implement reciprocal rank fusion to combine BM25 and dense vector search for RAG systems that handle both semantic queries and exact keyword matches."
---

Your RAG system works beautifully in demos. Users ask natural language questions, the embeddings find semantically similar chunks, the LLM synthesizes a coherent answer. Then you ship to production and someone searches for "CVE-2024-3094" or "error code E_CONN_REFUSED_7842" and your retriever returns completely unrelated documents because those exact strings don't exist in embedding space in any meaningful way. Pure vector search fails on rare terms, version numbers, error codes, product SKUs, and anything where exact lexical match matters more than semantic proximity. Meanwhile, if you swap to BM25-only, you lose the semantic understanding that made the demo impressive in the first place. The answer is hybrid search, and it's less complex than you probably think.

## Why Pure Vector Search Fails in Production

Dense retrieval works by encoding both queries and documents into a shared embedding space, then finding nearest neighbors. The fundamental assumption is that semantic similarity maps to geometric proximity. This holds well for paraphrasing, concept matching, and intent understanding — but it breaks down in specific cases that production traffic hits constantly.

Consider a user searching for "LangChain 0.1.12 breaking change." Your embedding model has never seen that specific version string during training. The query embeds to a region of vector space based on its general concepts — "library," "version," "change" — and the nearest neighbors will be documents about LangChain generally, not the specific release notes. BM25 would trivially find documents containing "0.1.12" because it's doing exact token matching with TF-IDF weighting.

The failure modes are predictable:
- **Named entities**: product codes, person names, geographic locations
- **Technical identifiers**: error codes, model names, API endpoints, version strings
- **Low-frequency tokens**: domain jargon that rarely appears in general corpora
- **Abbreviations**: "OOM" might not embed close to "out of memory" for your model

In a production RAG system serving 10k+ queries per day, these edge cases aren't rare — they're a significant fraction of your actual traffic. Users searching for specific things are often the ones who most need accurate retrieval.

## BM25 as the Complementary Retriever

BM25 (Best Match 25) is the standard sparse retrieval algorithm, a refinement of TF-IDF that adds document length normalization and term saturation. For a query term `t` and document `d`:

```
// snippet-1
// BM25 scoring formula for reference
// score(d, q) = Σ IDF(t) * (tf(t,d) * (k1 + 1)) / (tf(t,d) + k1 * (1 - b + b * |d|/avgdl))
// 
// Parameters:
// k1 = 1.2 to 2.0  (term frequency saturation — higher means slower saturation)
// b  = 0.75        (length normalization — 0 disables, 1 fully normalizes)
// tf(t,d) = term frequency of t in d
// IDF(t)  = log((N - df(t) + 0.5) / (df(t) + 0.5) + 1)
// N = total number of documents, df(t) = documents containing t
```

The critical property of BM25 is that it rewards exact token matches with high scores and is completely insensitive to semantic similarity. This is the opposite failure mode from dense retrieval, which is exactly why combining them works.

Practical implementations: Elasticsearch and OpenSearch use BM25 as their default similarity algorithm. For standalone use, `rank_bm25` in Python is production-ready and fast enough for offline indexing. Weaviate, Qdrant (via sparse vectors), and pgvector all have hybrid search support with varying degrees of integration depth.

## Reciprocal Rank Fusion

The core challenge in hybrid search is score fusion: how do you combine a BM25 score of 18.4 with a cosine similarity of 0.87? Direct score combination is fragile because the scales are incompatible and distribution-dependent. A BM25 score means nothing without knowing the score distribution across your corpus.

Reciprocal Rank Fusion (RRF) sidesteps the normalization problem entirely by working with ranks rather than scores. For a document `d` across multiple ranked lists:

```
RRF_score(d) = Σ_r 1 / (k + rank_r(d))
```

Where `k` is a smoothing constant (60 is the standard default from the original paper, and it works well in practice). Documents that don't appear in a list are simply excluded from that list's contribution.

```python
# snippet-2
from typing import Any
from collections import defaultdict

def reciprocal_rank_fusion(
    result_sets: list[list[tuple[str, float]]],
    k: int = 60,
    weights: list[float] | None = None,
) -> list[tuple[str, float]]:
    """
    Merge multiple ranked result sets using RRF.
    
    Args:
        result_sets: List of ranked result lists, each containing (doc_id, score) tuples
                     ordered from most to least relevant.
        k: Smoothing constant. Default 60 works well empirically.
        weights: Optional per-retriever weights. Defaults to equal weighting.
    
    Returns:
        Merged list of (doc_id, rrf_score) sorted by descending score.
    """
    if weights is None:
        weights = [1.0] * len(result_sets)
    
    if len(weights) != len(result_sets):
        raise ValueError("weights length must match result_sets length")
    
    scores: dict[str, float] = defaultdict(float)
    
    for result_list, weight in zip(result_sets, weights):
        for rank, (doc_id, _original_score) in enumerate(result_list, start=1):
            scores[doc_id] += weight * (1.0 / (k + rank))
    
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)
```

The elegance of RRF is that it's robust to outliers, doesn't require score normalization, and naturally handles documents that appear in only one result set. A document ranked #1 by BM25 and absent from dense results still gets a strong RRF score. A document ranked #50 in both gets a weak combined score even if both individual scores are technically "good."

## Building a Production Hybrid Retriever

Here's a concrete implementation integrating OpenAI embeddings for dense retrieval, BM25 for sparse, and RRF for fusion. This is the pattern I'd use as a starting point in a real system:

```python
# snippet-3
import asyncio
from dataclasses import dataclass
from typing import Any

import numpy as np
from openai import AsyncOpenAI
from rank_bm25 import BM25Okapi


@dataclass
class Document:
    id: str
    content: str
    metadata: dict[str, Any]


@dataclass
class SearchResult:
    document: Document
    score: float
    dense_rank: int | None
    sparse_rank: int | None


class HybridRetriever:
    def __init__(
        self,
        documents: list[Document],
        embeddings: np.ndarray,
        embedding_model: str = "text-embedding-3-small",
        bm25_k1: float = 1.5,
        bm25_b: float = 0.75,
        rrf_k: int = 60,
        dense_weight: float = 1.0,
        sparse_weight: float = 1.0,
    ):
        self.documents = documents
        self.doc_index = {doc.id: doc for doc in documents}
        self.embeddings = embeddings  # shape: (n_docs, embedding_dim)
        self.embedding_model = embedding_model
        self.rrf_k = rrf_k
        self.dense_weight = dense_weight
        self.sparse_weight = sparse_weight
        self.client = AsyncOpenAI()
        
        # Build BM25 index — tokenize by whitespace, lowercase
        tokenized = [doc.content.lower().split() for doc in documents]
        self.bm25 = BM25Okapi(tokenized, k1=bm25_k1, b=bm25_b)
    
    async def _embed_query(self, query: str) -> np.ndarray:
        response = await self.client.embeddings.create(
            model=self.embedding_model,
            input=query,
        )
        return np.array(response.data[0].embedding)
    
    def _dense_search(self, query_vec: np.ndarray, top_k: int) -> list[tuple[str, float]]:
        # Cosine similarity via normalized dot product
        norms = np.linalg.norm(self.embeddings, axis=1)
        query_norm = np.linalg.norm(query_vec)
        similarities = (self.embeddings @ query_vec) / (norms * query_norm + 1e-8)
        top_indices = np.argsort(similarities)[::-1][:top_k]
        return [(self.documents[i].id, float(similarities[i])) for i in top_indices]
    
    def _sparse_search(self, query: str, top_k: int) -> list[tuple[str, float]]:
        tokens = query.lower().split()
        scores = self.bm25.get_scores(tokens)
        top_indices = np.argsort(scores)[::-1][:top_k]
        return [(self.documents[i].id, float(scores[i])) for i in top_indices]
    
    async def search(
        self,
        query: str,
        top_k: int = 10,
        prefetch_k: int = 50,
    ) -> list[SearchResult]:
        """
        Hybrid search with RRF fusion.
        
        prefetch_k controls how many candidates each retriever fetches before fusion.
        A larger prefetch_k improves recall at the cost of fusion overhead.
        50-100 is usually sufficient; diminishing returns beyond that.
        """
        query_vec = await self._embed_query(query)
        
        dense_results = self._dense_search(query_vec, prefetch_k)
        sparse_results = self._sparse_search(query, prefetch_k)
        
        # Build rank maps for result annotation
        dense_ranks = {doc_id: rank for rank, (doc_id, _) in enumerate(dense_results, 1)}
        sparse_ranks = {doc_id: rank for rank, (doc_id, _) in enumerate(sparse_results, 1)}
        
        fused = self._rrf_fuse(dense_results, sparse_results)
        
        results = []
        for doc_id, rrf_score in fused[:top_k]:
            results.append(SearchResult(
                document=self.doc_index[doc_id],
                score=rrf_score,
                dense_rank=dense_ranks.get(doc_id),
                sparse_rank=sparse_ranks.get(doc_id),
            ))
        
        return results
    
    def _rrf_fuse(
        self,
        dense: list[tuple[str, float]],
        sparse: list[tuple[str, float]],
    ) -> list[tuple[str, float]]:
        from collections import defaultdict
        scores: dict[str, float] = defaultdict(float)
        for rank, (doc_id, _) in enumerate(dense, 1):
            scores[doc_id] += self.dense_weight / (self.rrf_k + rank)
        for rank, (doc_id, _) in enumerate(sparse, 1):
            scores[doc_id] += self.sparse_weight / (self.rrf_k + rank)
        return sorted(scores.items(), key=lambda x: x[1], reverse=True)
```

## Tuning the Weight Ratio

The `dense_weight` and `sparse_weight` parameters give you control over which retriever dominates. Equal weights (1.0/1.0) is a sensible default, but you should tune this against your actual query distribution.

The practical guidance:
- **Dense-heavy (2.0/1.0)**: Better for conversational queries, paraphrase matching, conceptual questions
- **Sparse-heavy (1.0/2.0)**: Better for technical documentation, code search, product catalogs with SKUs
- **Equal (1.0/1.0)**: Good starting point for general-purpose knowledge bases

Run offline evaluation against a labeled query set before committing to weights. If you're using Ragas or a similar RAG evaluation framework, you can sweep `dense_weight` values and pick the one that maximizes your retrieval metrics (MRR@10, NDCG@10, or recall@10 depending on what you care about).

```python
# snippet-4
import itertools
from dataclasses import dataclass

import numpy as np


@dataclass
class EvalQuery:
    query: str
    relevant_doc_ids: set[str]


def evaluate_retriever(
    retriever_factory,  # callable(dense_weight, sparse_weight) -> HybridRetriever
    eval_queries: list[EvalQuery],
    top_k: int = 10,
    dense_weights: list[float] | None = None,
    sparse_weights: list[float] | None = None,
) -> dict[tuple[float, float], dict[str, float]]:
    """
    Grid search over weight combinations, returning metrics per configuration.
    Metrics: recall@k, MRR@k.
    """
    if dense_weights is None:
        dense_weights = [0.5, 1.0, 1.5, 2.0]
    if sparse_weights is None:
        sparse_weights = [0.5, 1.0, 1.5, 2.0]
    
    results = {}
    
    for dw, sw in itertools.product(dense_weights, sparse_weights):
        retriever = retriever_factory(dense_weight=dw, sparse_weight=sw)
        recalls, reciprocal_ranks = [], []
        
        import asyncio
        
        async def run_queries():
            for eq in eval_queries:
                search_results = await retriever.search(eq.query, top_k=top_k)
                retrieved_ids = [r.document.id for r in search_results]
                
                # Recall@k
                hits = len(set(retrieved_ids) & eq.relevant_doc_ids)
                recalls.append(hits / len(eq.relevant_doc_ids))
                
                # MRR@k
                rr = 0.0
                for rank, doc_id in enumerate(retrieved_ids, 1):
                    if doc_id in eq.relevant_doc_ids:
                        rr = 1.0 / rank
                        break
                reciprocal_ranks.append(rr)
        
        asyncio.run(run_queries())
        
        results[(dw, sw)] = {
            "recall@k": float(np.mean(recalls)),
            "mrr@k": float(np.mean(reciprocal_ranks)),
        }
    
    return results
```

## Index Overhead and Latency

Running two retrievers isn't free. Here's what you're actually paying:

**Storage**: A BM25 index over 1M documents is typically 200-500MB in memory (using `rank_bm25` or a custom inverted index). Your dense index is `n_docs × embedding_dim × 4 bytes` — for 1M docs with 1536-dim embeddings, that's ~6GB. These can coexist, but plan your memory budget.

**Latency**: Dense retrieval via HNSW (Qdrant, Weaviate, pgvector) runs in 10-50ms for 1M documents. BM25 with a simple inverted index is typically 5-20ms. RRF fusion over two lists of 50-100 items is microseconds. So your hybrid retrieval P95 latency is roughly `max(dense_latency, sparse_latency) + small_constant`, not the sum — they can run in parallel.

```python
# snippet-5
import asyncio
import time
from concurrent.futures import ThreadPoolExecutor


async def search_parallel(
    retriever: HybridRetriever,
    query: str,
    prefetch_k: int = 50,
) -> tuple[list, list, float]:
    """
    Run dense and sparse retrieval in parallel, return both result sets
    and wall-clock latency.
    """
    loop = asyncio.get_event_loop()
    executor = ThreadPoolExecutor(max_workers=2)
    
    start = time.perf_counter()
    
    query_vec = await retriever._embed_query(query)
    
    # BM25 is CPU-bound, run in thread pool to avoid blocking event loop
    dense_future = loop.run_in_executor(
        executor, retriever._dense_search, query_vec, prefetch_k
    )
    sparse_future = loop.run_in_executor(
        executor, retriever._sparse_search, query, prefetch_k
    )
    
    dense_results, sparse_results = await asyncio.gather(dense_future, sparse_future)
    
    elapsed_ms = (time.perf_counter() - start) * 1000
    return dense_results, sparse_results, elapsed_ms
```

For systems handling 100+ RPS, the embedding API call is almost always your bottleneck, not the retrieval itself. Cache embeddings for repeated queries using a simple LRU cache keyed on the normalized query string.

## Qdrant's Native Sparse Vector Support

If you're using Qdrant (>= 1.7), you can store sparse vectors natively alongside dense vectors and let Qdrant handle the fusion. This eliminates the need for a separate BM25 index and simplifies your architecture:

```python
# snippet-6
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    NamedSparseVector,
    NamedVector,
    PointStruct,
    Prefetch,
    Query,
    SparseIndexParams,
    SparseVector,
    SparseVectorParams,
    VectorParams,
)


def setup_hybrid_collection(client: QdrantClient, collection_name: str):
    client.recreate_collection(
        collection_name=collection_name,
        vectors_config={
            "dense": VectorParams(size=1536, distance=Distance.COSINE),
        },
        sparse_vectors_config={
            "sparse": SparseVectorParams(
                index=SparseIndexParams(on_disk=False),  # keep in RAM for speed
            ),
        },
    )


def upsert_document(
    client: QdrantClient,
    collection_name: str,
    doc_id: str,
    dense_vec: list[float],
    sparse_indices: list[int],
    sparse_values: list[float],
    payload: dict,
):
    client.upsert(
        collection_name=collection_name,
        points=[
            PointStruct(
                id=doc_id,
                vector={
                    "dense": dense_vec,
                    "sparse": SparseVector(
                        indices=sparse_indices,
                        values=sparse_values,
                    ),
                },
                payload=payload,
            )
        ],
    )


def hybrid_search_qdrant(
    client: QdrantClient,
    collection_name: str,
    query_dense: list[float],
    query_sparse_indices: list[int],
    query_sparse_values: list[float],
    top_k: int = 10,
    prefetch_k: int = 50,
) -> list:
    """
    Qdrant native hybrid search with RRF fusion.
    Qdrant handles the RRF internally when using Prefetch + Query(fusion=...).
    """
    return client.query_points(
        collection_name=collection_name,
        prefetch=[
            Prefetch(
                query=NamedVector(name="dense", vector=query_dense),
                limit=prefetch_k,
            ),
            Prefetch(
                query=NamedSparseVector(
                    name="sparse",
                    vector=SparseVector(
                        indices=query_sparse_indices,
                        values=query_sparse_values,
                    ),
                ),
                limit=prefetch_k,
            ),
        ],
        query=Query(fusion="rrf"),
        limit=top_k,
    ).points
```

The sparse vector values for BM25 can be computed using `fastembed` with a sparse model like `prithivida/Splade_PP_en_v1` or `Qdrant/bm25`, which encode text as sparse TF-IDF-like vectors with vocabulary-aligned indices. This keeps everything in one service and avoids the dual-index maintenance overhead.

## What Actually Matters in Practice

**Prefetch size matters more than you think.** Setting `prefetch_k=10` means a relevant document ranked #11 by one retriever is invisible to RRF, even if it's ranked #2 by the other. I've seen teams tune `top_k` carefully but leave `prefetch_k` too small and wonder why recall is low. A prefetch of 50-100 documents per retriever is almost always sufficient; the memory and latency cost is negligible compared to the retrieval itself.

**Monitor dense rank vs sparse rank distributions in production.** Log `dense_rank` and `sparse_rank` for every served result. If you see a bimodal distribution — results that appear only in dense or only in sparse — you know your two retrievers are seeing very different relevance signals, which means both are contributing meaningfully. If they're always co-ranked similarly, you're paying for two retrievers but only getting one signal.

**Chunking strategy affects BM25 more than dense retrieval.** Dense embeddings are somewhat robust to chunk boundaries because the model encodes semantic meaning holistically. BM25 is purely token-frequency based — a rare term that spans a chunk boundary is invisible. This doesn't mean you need different chunk sizes for each retriever, but it does mean that overlapping chunks (e.g., 50% overlap) benefit sparse retrieval disproportionately.

**Don't tune on your dev set.** The temptation is to adjust weights until your labeled queries look great. Run a proper train/validation/test split, tune on validation only, and hold out test queries until you're ready to report final numbers. Overfitting weight parameters to a small labeled set is a real failure mode — you'll ship confident numbers and see degraded production performance.

The hybrid approach isn't a research curiosity — it's the retrieval architecture I'd reach for by default in any serious RAG system. The implementation cost is one afternoon; the recall improvement on production traffic is measurable within days of deployment.
```