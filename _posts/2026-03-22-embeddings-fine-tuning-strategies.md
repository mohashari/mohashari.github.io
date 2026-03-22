---
layout: post
title: "Embeddings Fine-Tuning: When Generic Models Are Not Enough"
date: 2026-03-22 08:00:00 +0700
tags: [ai-engineering, embeddings, machine-learning, search, nlp]
description: "When off-the-shelf embedding models fail your domain-specific search or ranking tasks, fine-tuning is the lever that actually moves the needle."
---

Your semantic search works fine in demos. Users type "cardiac arrest protocol" and get "myocardial infarction treatment guidelines" back — close enough. But in production, with 2 million medical documents and real clinicians searching under pressure, "close enough" causes misses that matter. You spent weeks integrating `text-embedding-3-large` or `bge-large-en-v1.5`, and the retrieval precision at k=5 sits at 61%. Your legal team won't let you ship it. The model doesn't know that in your domain, "protocol" means a specific clinical document type, not a generic procedure. It doesn't know that "arrest" in your corpus almost never means crime. Generic models are trained on the internet — and your data isn't the internet.

Fine-tuning embeddings is the answer, but it's not talked about as much as fine-tuning LLMs because the workflow is less obvious and the failure modes are subtler. This post covers the full production path: when to fine-tune, how to generate training data, the training loop itself, evaluation, and serving.

## When Fine-Tuning Is Actually Worth It

Don't fine-tune because it sounds sophisticated. Fine-tune when you have a measurable gap and domain-specific signal. The specific triggers:

- **Retrieval precision below your SLA** — if your RAG pipeline's hit-rate at k=10 is below ~75% and you've already tuned chunking and reranking, you've hit the model's knowledge ceiling.
- **Heavy jargon or abbreviations** — legal, medical, finance, defense, industrial. Generic models tokenize "LVEF" (left ventricular ejection fraction) poorly and have no semantic anchoring for it.
- **Cross-lingual with low-resource languages** — multilingual models are weak on Bahasa Indonesia, Thai, Vietnamese at domain-specific tasks. Fine-tuning on parallel domain pairs helps significantly.
- **Private product catalogs or internal knowledge bases** — your product SKUs, internal project codes, and team-specific terminology simply don't exist in pre-training data.

The cost threshold: if you have at least 1,000 high-quality (query, positive document) pairs, fine-tuning is viable. Under 500 pairs, you're better off with better chunking, BM25 hybrid search, or a reranker.

## Generating Training Data Without Labeled Pairs

The biggest blocker is data. You rarely have human-annotated (query, positive, negative) triplets at scale. Two production-viable approaches:

**Synthetic generation with an LLM**: Feed document chunks to GPT-4o or Claude and ask it to generate realistic queries a user might ask to find that document. This works surprisingly well — the LLM has seen enough of your domain to generate plausible queries even without fine-tuning.

```python
# snippet-1
import anthropic
import json
from pathlib import Path

client = anthropic.Anthropic()

def generate_queries_for_chunk(chunk: str, n_queries: int = 5) -> list[str]:
    prompt = f"""You are generating training data for a medical document retrieval system.

Given the following clinical document excerpt, generate {n_queries} realistic search queries
that a clinician might use to find this document. Queries should vary in specificity and phrasing.
Return a JSON array of strings only.

Document excerpt:
{chunk}

Queries:"""

    message = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}]
    )
    
    raw = message.content[0].text.strip()
    return json.loads(raw)


def build_training_pairs(chunks: list[str], output_path: str) -> None:
    pairs = []
    for i, chunk in enumerate(chunks):
        queries = generate_queries_for_chunk(chunk)
        for query in queries:
            pairs.append({"query": query, "positive": chunk, "chunk_id": i})
    
    Path(output_path).write_text(json.dumps(pairs, indent=2, ensure_ascii=False))
    print(f"Generated {len(pairs)} training pairs from {len(chunks)} chunks")
```

**Mining hard negatives from BM25**: Positive pairs alone train a model to cluster everything together. Hard negatives — documents that are lexically similar but semantically wrong — teach the model to discriminate. BM25 is excellent at finding these: run each query against BM25, take top-50 results, remove known positives, and the remainder are your hard negatives.

```python
# snippet-2
from rank_bm25 import BM25Okapi
import numpy as np

def mine_hard_negatives(
    queries: list[str],
    corpus: list[str],
    positives: list[int],  # corpus indices
    n_negatives: int = 5,
    bm25_pool: int = 50,
) -> list[dict]:
    tokenized_corpus = [doc.lower().split() for doc in corpus]
    bm25 = BM25Okapi(tokenized_corpus)
    
    triplets = []
    for query, pos_idx in zip(queries, positives):
        scores = bm25.get_scores(query.lower().split())
        top_indices = np.argsort(scores)[::-1][:bm25_pool]
        
        # Hard negatives: BM25-retrieved but not the positive
        hard_negatives = [i for i in top_indices if i != pos_idx][:n_negatives]
        
        if len(hard_negatives) < n_negatives:
            # Pad with random negatives if BM25 pool is too small
            random_pool = list(set(range(len(corpus))) - {pos_idx} - set(hard_negatives))
            hard_negatives += np.random.choice(random_pool, n_negatives - len(hard_negatives), replace=False).tolist()
        
        triplets.append({
            "query": query,
            "positive": corpus[pos_idx],
            "negatives": [corpus[i] for i in hard_negatives]
        })
    
    return triplets
```

In practice, combine both: synthetic queries give you breadth, BM25 hard negatives give you discrimination. Aim for at least 3 negatives per positive — MultipleNegativesRankingLoss (more on this below) benefits from more negatives in the batch.

## The Training Loop

Use `sentence-transformers` — it handles the embedding fine-tuning workflow cleanly and supports the loss functions you actually need.

**Loss function choice matters enormously**:
- `MultipleNegativesRankingLoss`: best for (query, positive) pairs where in-batch negatives supplement your mined ones. This is your default.
- `TripletLoss`: for (anchor, positive, negative) triplets with explicit control over margin. Use when your negatives are carefully curated.
- `CosineSimilarityLoss`: for regression tasks where you have human-rated similarity scores. Less common in retrieval.

```python
# snippet-3
from sentence_transformers import SentenceTransformer, InputExample, losses
from sentence_transformers.evaluation import InformationRetrievalEvaluator
from torch.utils.data import DataLoader
import json

def fine_tune_embeddings(
    base_model: str,
    train_path: str,
    eval_queries: dict,   # {qid: query_text}
    eval_corpus: dict,    # {cid: doc_text}
    eval_relevant: dict,  # {qid: set of relevant cids}
    output_dir: str,
    epochs: int = 3,
    batch_size: int = 32,
    warmup_ratio: float = 0.1,
) -> None:
    model = SentenceTransformer(base_model)
    
    raw = json.loads(open(train_path).read())
    train_examples = [
        InputExample(texts=[item["query"], item["positive"]])
        for item in raw
    ]
    
    train_dataloader = DataLoader(train_examples, shuffle=True, batch_size=batch_size)
    train_loss = losses.MultipleNegativesRankingLoss(model)
    
    evaluator = InformationRetrievalEvaluator(
        queries=eval_queries,
        corpus=eval_corpus,
        relevant_docs=eval_relevant,
        score_functions={"cosine": lambda a, b: (a * b).sum(dim=-1)},
        batch_size=64,
        show_progress_bar=False,
        name="medical-ir",
    )
    
    warmup_steps = int(len(train_dataloader) * epochs * warmup_ratio)
    
    model.fit(
        train_objectives=[(train_dataloader, train_loss)],
        evaluator=evaluator,
        epochs=epochs,
        warmup_steps=warmup_steps,
        output_path=output_dir,
        save_best_model=True,
        evaluation_steps=500,
        use_amp=True,  # Mixed precision — cuts training time ~40% on A100
    )
```

**Practical training tips from production**:

- Use `use_amp=True` — mixed precision training is safe for embedding fine-tuning and cuts runtime significantly.
- `batch_size=32` to `64` is typical; larger batches give MultipleNegativesRankingLoss more in-batch negatives, improving signal, but you're memory-bound quickly.
- 3 epochs is usually enough. More risks forgetting general language understanding (catastrophic forgetting).
- Start from a strong base. `BAAI/bge-large-en-v1.5` and `intfloat/e5-large-v2` are better starting points than OpenAI's embeddings for fine-tuning because you control the model weights.

## Evaluating Before You Ship

Intrinsic metrics like training loss tell you nothing useful. You need information retrieval metrics on a held-out eval set that mirrors production queries.

The metrics that matter:
- **NDCG@10** (Normalized Discounted Cumulative Gain): accounts for ranking position, not just presence. Your primary metric.
- **Recall@10**: what fraction of relevant docs appear in top-10. Important when missing a result is costly.
- **MRR** (Mean Reciprocal Rank): how high does the first relevant result appear. Good for single-answer scenarios.

```python
# snippet-4
import numpy as np
from collections import defaultdict

def evaluate_retrieval(
    model,
    eval_queries: dict[str, str],
    eval_corpus: dict[str, str],
    relevant_docs: dict[str, set[str]],
    k_values: list[int] = [1, 5, 10],
) -> dict[str, float]:
    corpus_ids = list(eval_corpus.keys())
    corpus_texts = [eval_corpus[cid] for cid in corpus_ids]
    
    corpus_embeddings = model.encode(corpus_texts, batch_size=256, show_progress_bar=True, normalize_embeddings=True)
    
    metrics = defaultdict(list)
    
    for qid, query in eval_queries.items():
        q_emb = model.encode([query], normalize_embeddings=True)
        scores = (q_emb @ corpus_embeddings.T)[0]
        ranked = np.argsort(scores)[::-1]
        relevant = relevant_docs.get(qid, set())
        
        for k in k_values:
            top_k = [corpus_ids[i] for i in ranked[:k]]
            hits = len(set(top_k) & relevant)
            metrics[f"recall@{k}"].append(hits / max(len(relevant), 1))
        
        # NDCG@10
        top10 = [corpus_ids[i] for i in ranked[:10]]
        dcg = sum(
            (1 / np.log2(rank + 2)) for rank, cid in enumerate(top10) if cid in relevant
        )
        ideal_hits = min(len(relevant), 10)
        idcg = sum(1 / np.log2(rank + 2) for rank in range(ideal_hits))
        metrics["ndcg@10"].append(dcg / idcg if idcg > 0 else 0.0)
        
        # MRR
        for rank, cid in enumerate(ranked[:10]):
            if cid in relevant:
                metrics["mrr"].append(1 / (rank + 1))
                break
        else:
            metrics["mrr"].append(0.0)
    
    return {k: float(np.mean(v)) for k, v in metrics.items()}
```

A real example of what improvement looks like: on a legal contract retrieval task, starting from `bge-large-en-v1.5` (which is already strong), fine-tuning on 8,000 synthetic query-document pairs with BM25 hard negatives moved NDCG@10 from 0.71 to 0.84. That's the difference between a mediocre and a production-worthy retrieval system.

## Serving Fine-Tuned Models

Once you have a fine-tuned model, you need to serve it efficiently. The two main paths:

**Self-hosted with Infinity or TEI**: Text Embeddings Inference (TEI) from HuggingFace is the production choice — it supports Flash Attention, continuous batching, and gRPC. Infinity is simpler to set up for lighter workloads.

```yaml
# snippet-5
# docker-compose.yml for Text Embeddings Inference
version: "3.8"
services:
  embeddings:
    image: ghcr.io/huggingface/text-embeddings-inference:latest
    command:
      - "--model-id"
      - "/models/medical-bge-finetuned"
      - "--max-batch-tokens"
      - "16384"
      - "--max-concurrent-requests"
      - "512"
      - "--port"
      - "8080"
      - "--dtype"
      - "float16"
    volumes:
      - ./models:/models
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]
    ports:
      - "8080:8080"
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8080/health"]
      interval: 10s
      timeout: 5s
      retries: 3
```

**Calling TEI from Go** (if your backend is Go):

<script src="https://gist.github.com/mohashari/2b367527400e64af842cc8f6297dd4a2.js?file=snippet-6.go"></script>

For vector storage, pgvector on Postgres handles up to ~5M vectors at 1536 dimensions with HNSW indexing before you need a dedicated vector DB. At higher scales, Qdrant or Weaviate are the production choices.

## The Catastrophic Forgetting Problem

A real failure mode that trips teams up: after fine-tuning, the model performs brilliantly on domain queries but degrades on general queries. If your search surface mixes domain-specific content with general web-like content, you'll see a regression.

Mitigation strategies:

1. **Mix general data into training**: Include a fraction (10–20%) of MSMARCO or Natural Questions pairs alongside your domain data. This preserves general language understanding.
2. **Lower learning rate**: `2e-5` is a reasonable upper bound. Higher rates accelerate forgetting.
3. **Evaluate on both domain and general benchmarks**: Run BEIR benchmarks (specifically MSMARCO, NFCorpus) alongside your domain eval. If BEIR NDCG@10 drops more than 3 points, you're over-fitting.

The `sentence-transformers` library makes it easy to mix datasets:

```python
# snippet-7
from sentence_transformers import datasets as st_datasets

# Load domain-specific pairs
domain_pairs = [InputExample(texts=[q, p]) for q, p in domain_data]

# Load general MSMARCO pairs (prevents forgetting)
msmarco_dataset = st_datasets.NoDuplicatesDataLoader(
    st_datasets.MSMARCODataset("train", corpus_chunk_size=500_000),
    batch_size=batch_size
)

# Weight: 80% domain, 20% general
from torch.utils.data import ConcatDataset, WeightedRandomSampler

domain_weight = 0.8
general_weight = 0.2
# ...implement weighted sampling across two DataLoaders
```

## When to Re-Fine-Tune

Your domain data drifts. New product lines, new regulation, new terminology. Plan for quarterly re-fine-tuning cycles if your corpus evolves. Track retrieval precision on a fixed eval set as a dashboard metric — when it drops 5 points from baseline, queue a fine-tuning run.

The good news: incremental fine-tuning from your last checkpoint (not from scratch) is significantly cheaper. You're talking 30–60 minutes on an A100 for most production workloads with a few thousand new pairs. Keep your eval set static and your data pipeline reproducible, and re-tuning becomes routine ops rather than a research project.

The pattern that works in production: treat your embedding model like any other ML model — versioned, evaluated, deployed via CI/CD, and retrained on a schedule tied to data drift metrics. The tooling is mature enough now that there's no excuse for shipping a generic model when your domain gives you the signal to do better.
```