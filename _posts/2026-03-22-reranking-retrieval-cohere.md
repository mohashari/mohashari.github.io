---
layout: post
title: "Reranking in RAG: Improving Retrieval Precision with Cross-Encoders"
date: 2026-03-22 08:00:00 +0700
tags: [rag, vector-search, llm, ai-engineering, retrieval]
description: "How cross-encoder rerankers fix the precision gap in bi-encoder RAG retrieval, with benchmarks and a practical decision framework."
---

Your vector search returns 20 chunks. Your LLM gets the top 5. The user asks "what's the refund policy for international orders?" and chunk rank 1 is about domestic shipping, rank 2 is a general FAQ header, and rank 3 is the actual answer—buried. The LLM either hallucinates a policy or hedges with "I don't have enough information." This isn't a chunking problem or an embedding model problem. It's a retrieval precision problem, and it happens because bi-encoders—the cosine similarity engines powering most RAG systems—are optimized for speed at the cost of nuanced relevance judgment. A cross-encoder reranker doesn't replace your vector index. It sits in front of your context window and makes the decision your bi-encoder couldn't afford to make.

## Why Bi-Encoders Fall Short

Bi-encoders encode query and document independently, then compare them with a dot product or cosine similarity. This is what makes them fast enough to search millions of vectors in milliseconds. But independence is the constraint: at encode time, the model has no idea what the query will be. The embedding for "refund policy international orders" and the embedding for "our international shipping partners include DHL and FedEx" end up close in vector space because they share domain vocabulary—shipping, international, orders.

The failure mode compounds in three scenarios:

**Lexical-semantic mismatch**: The document says "reimbursement" and the query says "refund." Your embedding model maps these nearby, but if the document is about a completely different reimbursement context (expense reports, not product returns), bi-encoder scores won't distinguish it.

**Multi-hop reasoning chunks**: You split a long document at 512 tokens. The relevant sentence is in chunk 3. Chunk 1 scores highest because it has the introduction with all the keywords. The LLM never sees chunk 3.

**Negation blindness**: "Does this product work with iOS?" and "This product does not support iOS" often score similarly against a query about iOS compatibility. The bi-encoder encodes the concept of iOS compatibility; it doesn't model the negation.

The standard workaround—increase top-k to 50 or 100—compounds your LLM costs and latency, pushes you toward context window limits, and dilutes signal with noise. You're paying more to get worse results.

## Cross-Encoders: Full Attention at Query Time

A cross-encoder takes the query and document together as a single input and runs them through a transformer jointly. The attention mechanism can model exactly how query tokens relate to document tokens. "Refund" attends to "reimbursement." "International" attends to specific country mentions. Negation is captured because "does not" attends directly to the subsequent predicate.

The output is a scalar relevance score, not an embedding. You can't pre-compute it. The entire cost is paid at query time, which is why cross-encoders are impractical as first-stage retrievers at scale—scoring 1M documents takes 1M forward passes.

This is why the architecture is a pipeline: bi-encoder retrieves a candidate set (top-k, typically 20–100), cross-encoder reranks that set, top-n of the reranked results go to the LLM (typically 3–10).

```python
# snippet-1
import asyncio
from dataclasses import dataclass
from typing import Optional
import httpx

@dataclass
class RetrievedChunk:
    id: str
    text: str
    score: float
    metadata: dict

@dataclass 
class RankedChunk:
    chunk: RetrievedChunk
    rerank_score: float
    original_rank: int

async def retrieve_and_rerank(
    query: str,
    vector_client,
    reranker,
    top_k: int = 50,
    top_n: int = 5,
    rerank_timeout_ms: float = 800,
) -> list[RankedChunk]:
    # First stage: fast bi-encoder retrieval
    candidates: list[RetrievedChunk] = await vector_client.search(
        query=query,
        limit=top_k,
    )
    
    if not candidates:
        return []

    # Second stage: cross-encoder reranking
    try:
        async with asyncio.timeout(rerank_timeout_ms / 1000):
            ranked = await reranker.rerank(
                query=query,
                documents=[c.text for c in candidates],
                top_n=top_n,
            )
    except (asyncio.TimeoutError, Exception):
        # Fallback: return top-n from bi-encoder on reranker failure
        return [
            RankedChunk(chunk=c, rerank_score=c.score, original_rank=i)
            for i, c in enumerate(candidates[:top_n])
        ]

    return [
        RankedChunk(
            chunk=candidates[r.index],
            rerank_score=r.relevance_score,
            original_rank=r.index,
        )
        for r in ranked.results
    ]
```

## Cohere Rerank: The Hosted Path

Cohere Rerank is the fastest path to production reranking. The API takes a query, a list of documents, and a `top_n` parameter, and returns ranked results with relevance scores. As of 2025, `rerank-v3.5` is the current flagship—it handles multilingual content and longer documents better than v2.

Pricing is per 1000 searches (not per document). At roughly $2/1000 searches with a 50-document candidate set, you're looking at $2 per 1000 queries. For 100k daily queries, that's $200/day. Compare this to the LLM cost: at GPT-4o pricing, 5 chunks averaging 400 tokens each adds $0.01–0.02 per query in context tokens alone. Reranking pays for itself if it eliminates even one hallucination per 100 queries.

```python
# snippet-2
import cohere
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

co = cohere.AsyncClient(api_key="COHERE_API_KEY")

@retry(
    retry=retry_if_exception_type(cohere.errors.TooManyRequestsError),
    wait=wait_exponential(multiplier=1, min=0.5, max=10),
    stop=stop_after_attempt(3),
)
async def cohere_rerank(
    query: str,
    documents: list[str],
    top_n: int = 5,
    model: str = "rerank-v3.5",
) -> list[dict]:
    response = await co.rerank(
        model=model,
        query=query,
        documents=documents,
        top_n=top_n,
        return_documents=False,  # we already have them; avoid redundant payload
    )
    
    return [
        {
            "index": r.index,
            "relevance_score": r.relevance_score,
        }
        for r in response.results
    ]
```

Latency for Cohere Rerank on a 50-document set runs 150–400ms p50, with p99 occasionally exceeding 800ms under load. This matters: if your retrieval is 50ms and your LLM is 2s, adding 400ms is a 20% total latency increase. If your SLA is tight, this is where you evaluate self-hosted alternatives.

## BGE Reranker: The Self-Hosted Path

BAAI's BGE reranker family (`bge-reranker-v2-m3`, `bge-reranker-large`) delivers competitive quality at zero per-query cost once deployed. `bge-reranker-v2-m3` is multilingual, fits in ~2GB GPU memory, and handles 512-token inputs efficiently.

On a single A10G GPU, you can process a 50-document reranking request in 80–150ms. With batching across concurrent requests, throughput scales to 500–1000 reranks/second, making it practical for production traffic on a single instance.

```python
# snippet-3
from transformers import AutoTokenizer, AutoModelForSequenceClassification
import torch
import asyncio
from concurrent.futures import ThreadPoolExecutor

class BGEReranker:
    def __init__(self, model_name: str = "BAAI/bge-reranker-v2-m3", device: str = "cuda"):
        self.tokenizer = AutoTokenizer.from_pretrained(model_name)
        self.model = AutoModelForSequenceClassification.from_pretrained(model_name)
        self.model.eval()
        self.device = torch.device(device if torch.cuda.is_available() else "cpu")
        self.model.to(self.device)
        self._executor = ThreadPoolExecutor(max_workers=4)

    def _score_batch(self, query: str, documents: list[str]) -> list[float]:
        pairs = [[query, doc] for doc in documents]
        
        with torch.no_grad():
            inputs = self.tokenizer(
                pairs,
                padding=True,
                truncation=True,
                max_length=512,
                return_tensors="pt",
            ).to(self.device)
            
            scores = self.model(**inputs, return_dict=True).logits.view(-1).float()
            return scores.cpu().tolist()

    async def rerank(
        self, query: str, documents: list[str], top_n: int = 5
    ) -> list[dict]:
        loop = asyncio.get_event_loop()
        scores = await loop.run_in_executor(
            self._executor,
            self._score_batch,
            query,
            documents,
        )
        
        ranked = sorted(
            [{"index": i, "relevance_score": s} for i, s in enumerate(scores)],
            key=lambda x: x["relevance_score"],
            reverse=True,
        )
        return ranked[:top_n]
```

The deployment tradeoff: self-hosted eliminates per-query costs and reduces p99 latency variance from API cold paths, but you own the infrastructure. A Kubernetes deployment with a 2-replica A10G node costs roughly $1.50–2/hour on major clouds—break-even versus Cohere at ~25k queries/day. Below that threshold, Cohere is cheaper. Above it, self-hosted wins fast.

## Benchmarking the Gap: Precision Numbers

The most useful benchmark for production decisions isn't BEIR or MTEB—it's your own data. That said, published numbers give a starting point:

On MSMARCO passage ranking, a bi-encoder baseline (e5-large) achieves MRR@10 of ~0.37. Adding BGE reranker on top-50 retrieval pushes it to ~0.43. Cohere rerank-v3.5 reaches ~0.44. The gap between "no reranker" and "reranker" is 6 MRR points. In production terms with 100k queries/day, that's roughly 6,000 queries per day where the right answer moves from outside top-5 to inside top-5.

For domain-specific corpora (legal, medical, proprietary docs), the gap is larger—often 10–15 MRR points—because bi-encoder models are less calibrated on out-of-distribution vocabulary.

```python
# snippet-4
# Offline evaluation: measure rank displacement to justify reranker investment
from dataclasses import dataclass
from statistics import mean, median

@dataclass
class EvalResult:
    query_id: str
    biencoder_rank: int   # rank of ground-truth chunk in bi-encoder results
    reranker_rank: int    # rank after reranking
    reranker_score: float

def compute_mrr(results: list[EvalResult], field: str, cutoff: int = 10) -> float:
    reciprocal_ranks = []
    for r in results:
        rank = getattr(r, field)
        if rank <= cutoff:
            reciprocal_ranks.append(1.0 / rank)
        else:
            reciprocal_ranks.append(0.0)
    return mean(reciprocal_ranks)

def analyze_reranker_impact(results: list[EvalResult]) -> dict:
    improved = [r for r in results if r.reranker_rank < r.biencoder_rank]
    degraded = [r for r in results if r.reranker_rank > r.biencoder_rank]
    
    rank_deltas = [r.biencoder_rank - r.reranker_rank for r in results]
    
    return {
        "mrr@10_biencoder": compute_mrr(results, "biencoder_rank"),
        "mrr@10_reranker": compute_mrr(results, "reranker_rank"),
        "pct_improved": len(improved) / len(results),
        "pct_degraded": len(degraded) / len(results),
        "median_rank_improvement": median(rank_deltas),
        "queries_rescued_into_top5": sum(
            1 for r in results if r.biencoder_rank > 5 and r.reranker_rank <= 5
        ),
    }
```

## Tuning top-k and top-n

The relationship between top-k (how many candidates you retrieve) and top-n (how many you pass to the LLM after reranking) is the central latency-precision dial in your system.

**top-k too low**: Your reranker can't rescue answers that weren't retrieved. If the correct chunk isn't in your top-20, no reranker helps. Recall@k is the ceiling on reranker performance.

**top-k too high**: Reranking latency grows roughly linearly with candidate count. At 100 candidates, you're 2x slower than at 50. Above 100, you're also adding chunks that are genuinely unrelated—noise the reranker has to score but will correctly push down, wastefully.

**top-n**: Keep this at 3–8 for most use cases. Passing 10+ chunks to an LLM makes the prompt unwieldy, increases latency, and dilutes the answer with borderline relevance. If your LLM consistently says "based on the provided context" without answering, the problem is retrieval recall (increase top-k), not top-n.

Practical starting points:
- General-purpose Q&A: top-k=40, top-n=5  
- Long-document corpora: top-k=60, top-n=6  
- Latency-critical (<500ms total): top-k=20, top-n=3  
- High-recall requirements (compliance, legal): top-k=100, top-n=8

```python
# snippet-5
# Automated top-k sweep to find the recall ceiling before investing in reranker
import numpy as np

async def sweep_topk_recall(
    eval_queries: list[dict],  # [{"query": str, "relevant_chunk_ids": list[str]}]
    vector_client,
    k_values: list[int] = [10, 20, 40, 60, 80, 100],
) -> dict[int, float]:
    recall_at_k = {}
    
    for k in k_values:
        hits = 0
        for item in eval_queries:
            results = await vector_client.search(query=item["query"], limit=k)
            retrieved_ids = {r.id for r in results}
            if any(cid in retrieved_ids for cid in item["relevant_chunk_ids"]):
                hits += 1
        
        recall_at_k[k] = hits / len(eval_queries)
        print(f"Recall@{k}: {recall_at_k[k]:.3f}")
    
    # Find the k where recall gain diminishes below 1%
    optimal_k = k_values[0]
    for i in range(1, len(k_values)):
        gain = recall_at_k[k_values[i]] - recall_at_k[k_values[i-1]]
        if gain < 0.01:
            break
        optimal_k = k_values[i]
    
    return recall_at_k, optimal_k
```

## Async Pipeline Architecture

The most common performance mistake is calling the reranker synchronously after retrieval. If your retrieval takes 60ms and reranking takes 250ms, you have a 310ms sequential pipeline. When you have multiple retrieval sources (dense + sparse + metadata filters), async staging compounds the savings.

```python
# snippet-6
import asyncio
from typing import AsyncIterator

async def retrieval_pipeline(
    query: str,
    dense_client,
    sparse_client,
    reranker,
    top_k_per_source: int = 30,
    top_n_final: int = 5,
) -> list[RankedChunk]:
    # Retrieve from dense and sparse sources concurrently
    dense_task = asyncio.create_task(
        dense_client.search(query=query, limit=top_k_per_source)
    )
    sparse_task = asyncio.create_task(
        sparse_client.search(query=query, limit=top_k_per_source)
    )
    
    dense_results, sparse_results = await asyncio.gather(dense_task, sparse_task)
    
    # Deduplicate by chunk ID, keeping highest score
    seen: dict[str, RetrievedChunk] = {}
    for chunk in dense_results + sparse_results:
        if chunk.id not in seen or chunk.score > seen[chunk.id].score:
            seen[chunk.id] = chunk
    
    candidates = sorted(seen.values(), key=lambda c: c.score, reverse=True)[:top_k_per_source]
    
    if not candidates:
        return []
    
    ranked = await reranker.rerank(
        query=query,
        documents=[c.text for c in candidates],
        top_n=top_n_final,
    )
    
    return [
        RankedChunk(
            chunk=candidates[r["index"]],
            rerank_score=r["relevance_score"],
            original_rank=r["index"],
        )
        for r in ranked
    ]
```

## When Not to Add a Reranker

Reranking has a real cost and the right answer is sometimes "don't add it yet":

**Your top-k recall is below 0.7**: If the correct answer isn't in your retrieved set 30% of the time, the reranker can't help those queries. Fix chunking, embedding model, or retrieval strategy first.

**Your query distribution is narrow and consistent**: If users always ask the same 50 types of questions, you can tune your retrieval directly and cache or pre-compute results. A reranker adds complexity without proportional benefit.

**Sub-200ms end-to-end SLA**: With a tight SLA, 150–300ms of reranking latency is often a non-starter. Invest in better bi-encoder fine-tuning on your domain data instead.

**You haven't measured the problem**: If you're adding a reranker because it "seems like it would help," measure first. Run the eval in snippet-4 on 200–500 representative queries against human-labeled ground truth. If MRR@5 is already above 0.70 for your bi-encoder, the marginal gain likely doesn't justify the operational complexity.

The signal to add reranking is usually user feedback ("the chatbot gave the wrong info") combined with a retrieval audit that shows the correct chunk ranked 6th or lower in 15%+ of queries. At that point, the reranker pays for itself immediately.
```