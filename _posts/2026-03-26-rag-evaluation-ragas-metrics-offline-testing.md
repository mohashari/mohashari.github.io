---
layout: post
title: "RAG Pipeline Evaluation: RAGAS Metrics, Faithfulness, and Offline Test Harnesses"
date: 2026-03-26 08:00:00 +0700
tags: [ai-engineering, rag, llm, evaluation, python]
description: "How to measure whether your RAG pipeline actually works using RAGAS metrics, faithfulness scoring, and reproducible offline test harnesses."
image: "https://picsum.photos/1080/720?random=3076"
thumbnail: "https://picsum.photos/400/300?random=3076"
---

You shipped a RAG pipeline. Retrieval looks reasonable in manual spot-checks, the LLM responses seem coherent, and stakeholders are happy with the demo. Then production happens: users ask questions your retrieval misses, the model starts hallucinating facts that are technically adjacent to retrieved context, and you have no way to tell whether a new embedding model or chunking strategy actually improved things — because you never established a baseline. This is the RAG evaluation trap, and most teams fall into it. The fix isn't more vibe-checking; it's a repeatable offline test harness with metrics that actually correlate with downstream quality.

## Why RAG Evaluation Is Harder Than It Looks

A RAG pipeline has at least three failure modes that can independently tank quality:

1. **Retrieval fails** — the relevant chunks aren't in the top-k results at all
2. **Context isn't used** — the LLM ignores retrieved chunks and answers from training data
3. **Context is misused** — the LLM cherry-picks or distorts retrieved information

Standard accuracy metrics catch none of these cleanly. BLEU/ROUGE compare surface-level token overlap, which tells you nothing about whether the answer is grounded in the retrieved context. Human eval doesn't scale. You need metrics that decompose the pipeline into its constituent failures.

RAGAS (Retrieval-Augmented Generation Assessment) provides exactly this decomposition. It evaluates four things independently: faithfulness, answer relevancy, context precision, and context recall. Each metric isolates a specific failure mode, which means when your score drops, you know where to look.

## The Four RAGAS Metrics

**Faithfulness** measures whether every claim in the generated answer can be inferred from the retrieved context. It's the anti-hallucination metric. RAGAS operationalizes this by extracting atomic statements from the answer, then verifying each statement against the context using an LLM judge. A faithfulness score of 0.6 means 40% of your answer's claims aren't supported by what you retrieved — that's hallucination at scale.

**Answer Relevancy** measures whether the answer actually addresses the question. A response can be perfectly faithful (everything stated is in the context) but completely unhelpful (the context was tangentially related, the answer meandered). This metric uses semantic similarity between the question and the answer, penalizing incomplete or off-topic responses.

**Context Precision** measures whether the retrieved chunks are ranked well — specifically, whether relevant chunks appear earlier in the context window. If your retriever returns 5 chunks and the only useful one is last, precision is low even though recall would be fine. This matters because most LLMs attend more strongly to content at the beginning and end of long contexts.

**Context Recall** measures whether all the information needed to answer the question was actually retrieved. This requires ground-truth reference answers. If your reference answer mentions three facts and your retrieved context only contains two of them, recall is 0.67.

## Building the Evaluation Dataset

The dataset is where most teams cut corners and then wonder why their metrics don't correlate with real quality. You need question-context-answer triples, and the sourcing matters.

The most reliable approach is synthetic generation from your own corpus, then human review of a sample. Generate questions that require multi-hop reasoning across chunks — those expose retrieval failures that single-document questions miss entirely.

```python
# snippet-1
from ragas.testset.generator import TestsetGenerator
from ragas.testset.evolutions import simple, reasoning, multi_context
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from langchain_community.document_loaders import DirectoryLoader

loader = DirectoryLoader("./corpus", glob="**/*.md")
documents = loader.load()

generator_llm = ChatOpenAI(model="gpt-4o")
critic_llm = ChatOpenAI(model="gpt-4o")
embeddings = OpenAIEmbeddings()

generator = TestsetGenerator.from_langchain(
    generator_llm,
    critic_llm,
    embeddings
)

# Weight toward multi-context questions — they expose retrieval failures
testset = generator.generate_with_langchain_docs(
    documents,
    test_size=200,
    distributions={
        simple: 0.25,
        reasoning: 0.35,
        multi_context: 0.40,
    },
)

df = testset.to_pandas()
# Save with schema: question, contexts, ground_truth, evolution_type
df.to_parquet("eval_dataset_v1.parquet", index=False)
```

200 questions is a practical minimum for stable metric estimates. Below that, variance between runs from LLM judge non-determinism will exceed the effect size of most changes you're trying to measure.

## Running the Evaluation

Once you have a dataset, evaluation is straightforward. The key is instrumenting your actual pipeline — not a simplified version of it — so that the metrics reflect real production behavior.

```python
# snippet-2
import pandas as pd
from ragas import evaluate
from ragas.metrics import (
    faithfulness,
    answer_relevancy,
    context_precision,
    context_recall,
)
from datasets import Dataset
from your_pipeline import RAGPipeline  # your actual production pipeline

eval_df = pd.read_parquet("eval_dataset_v1.parquet")
pipeline = RAGPipeline(
    vector_store="your-index",
    top_k=5,
    reranker=True,
)

results = []
for _, row in eval_df.iterrows():
    answer, contexts = pipeline.query(row["question"])
    results.append({
        "question": row["question"],
        "answer": answer,
        "contexts": contexts,
        "ground_truth": row["ground_truth"],
    })

dataset = Dataset.from_list(results)

scores = evaluate(
    dataset,
    metrics=[
        faithfulness,
        answer_relevancy,
        context_precision,
        context_recall,
    ],
)

print(scores)
# {'faithfulness': 0.82, 'answer_relevancy': 0.79,
#  'context_precision': 0.71, 'context_recall': 0.68}
```

Those numbers tell a story immediately: retrieval recall is the weakest link (0.68), meaning the pipeline is missing relevant chunks roughly a third of the time. Faithfulness at 0.82 is decent but there's room to reduce hallucination. Context precision at 0.71 suggests the reranker is doing some work but not enough.

## Offline Harness Architecture

Running evaluation ad-hoc is better than nothing, but what you want is a harness that runs on every significant change — new embedding model, different chunking strategy, modified retrieval parameters, prompt rewrites. This turns evaluation into a feedback loop rather than a one-time audit.

```yaml
# snippet-3
# .github/workflows/rag-eval.yml
name: RAG Evaluation

on:
  pull_request:
    paths:
      - 'pipeline/**'
      - 'prompts/**'
      - 'retrieval/**'

jobs:
  evaluate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Setup Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.12'

      - name: Install dependencies
        run: pip install -r requirements-eval.txt

      - name: Run RAG evaluation
        env:
          OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
          VECTOR_STORE_URL: ${{ secrets.EVAL_VECTOR_STORE_URL }}
        run: |
          python scripts/run_eval.py \
            --dataset eval_dataset_v1.parquet \
            --output eval_results/${{ github.sha }}.json \
            --baseline eval_results/baseline.json \
            --fail-on-regression 0.03

      - name: Comment PR with results
        uses: actions/github-script@v7
        with:
          script: |
            const fs = require('fs');
            const results = JSON.parse(
              fs.readFileSync(`eval_results/${{ github.sha }}.json`)
            );
            const body = `## RAG Evaluation Results\n\n` +
              `| Metric | Score | Delta |\n|---|---|---|\n` +
              Object.entries(results.scores).map(([k, v]) =>
                `| ${k} | ${v.current.toFixed(3)} | ${v.delta > 0 ? '+' : ''}${v.delta.toFixed(3)} |`
              ).join('\n');
            github.rest.issues.createComment({
              issue_number: context.issue.number,
              owner: context.repo.owner,
              repo: context.repo.repo,
              body
            });
```

The `--fail-on-regression 0.03` flag is load-bearing. A 3% drop in faithfulness is meaningful; a 0.5% drop is noise from LLM judge variance. Setting this threshold at 0 will generate endless false alarms.

## The Regression Detection Script

The CI workflow calls a script that does the actual comparison. Here's what that looks like:

```python
# snippet-4
import argparse
import json
import sys
import pandas as pd
from ragas import evaluate
from ragas.metrics import faithfulness, answer_relevancy, context_precision, context_recall
from datasets import Dataset
from your_pipeline import RAGPipeline

def run_evaluation(dataset_path: str) -> dict:
    eval_df = pd.read_parquet(dataset_path)
    pipeline = RAGPipeline.from_env()

    results = []
    for _, row in eval_df.iterrows():
        answer, contexts = pipeline.query(row["question"])
        results.append({
            "question": row["question"],
            "answer": answer,
            "contexts": contexts,
            "ground_truth": row["ground_truth"],
        })

    scores = evaluate(
        Dataset.from_list(results),
        metrics=[faithfulness, answer_relevancy, context_precision, context_recall],
    )
    return dict(scores)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--fail-on-regression", type=float, default=0.05)
    args = parser.parse_args()

    current_scores = run_evaluation(args.dataset)

    with open(args.baseline) as f:
        baseline = json.load(f)

    output = {"scores": {}}
    regressions = []

    for metric, current_val in current_scores.items():
        baseline_val = baseline.get(metric, current_val)
        delta = current_val - baseline_val
        output["scores"][metric] = {
            "current": current_val,
            "baseline": baseline_val,
            "delta": delta,
        }
        if delta < -args.fail_on_regression:
            regressions.append(f"{metric}: {baseline_val:.3f} → {current_val:.3f} (Δ{delta:.3f})")

    with open(args.output, "w") as f:
        json.dump(output, f, indent=2)

    if regressions:
        print("REGRESSION DETECTED:")
        for r in regressions:
            print(f"  {r}")
        sys.exit(1)

    print("All metrics within acceptable bounds.")

if __name__ == "__main__":
    main()
```

## Handling LLM Judge Costs

RAGAS uses an LLM (typically GPT-4) as a judge for faithfulness and answer relevancy. At 200 questions with 5 chunks each, you're looking at roughly 2,000–4,000 LLM calls per evaluation run. At GPT-4o pricing, that's around $2–5 per run — acceptable for PR checks, but you'll want to avoid running this on every commit.

Two optimizations matter in practice. First, cache judge responses — if the question, answer, and context are identical between runs, the score will be identical. Redis with a content-addressed key works well here. Second, run full evaluation only on PRs targeting main; for feature branches, run a 50-question stratified sample that covers all evolution types.

```python
# snippet-5
import hashlib
import json
import redis
from ragas.metrics.base import MetricWithLLM

r = redis.Redis(host="localhost", port=6379, decode_responses=True)

def cached_score(metric: MetricWithLLM, sample: dict) -> float:
    cache_key = hashlib.sha256(
        json.dumps(sample, sort_keys=True).encode()
    ).hexdigest()
    cache_field = f"ragas:{metric.name}:{cache_key}"

    cached = r.get(cache_field)
    if cached is not None:
        return float(cached)

    # score_single is not part of public RAGAS API, but you can
    # wrap evaluate() with a single-item dataset for the same effect
    from datasets import Dataset
    result = evaluate(Dataset.from_list([sample]), metrics=[metric])
    score = result[metric.name]

    r.setex(cache_field, 86400 * 7, str(score))  # 7-day TTL
    return score
```

## Interpreting Results in Practice

Absolute scores matter less than relative movement. A faithfulness of 0.75 on one dataset might be excellent or terrible depending on your corpus complexity and question distribution. What matters is whether changes improve or degrade each metric directionally.

When diagnosing a faithfulness drop after a prompt change, look at the per-sample scores, not just the aggregate. RAGAS returns per-question scores in the dataset. Filter to questions where faithfulness dropped below 0.5, examine the answer and retrieved contexts, and you'll usually see a clear pattern — the prompt change caused the model to answer from prior knowledge instead of context for a specific question type.

Context recall below 0.6 is almost always a chunking or embedding problem, not a generation problem. Increasing top-k is a band-aid; fix the chunking. Splitting documents at 512 tokens with no overlap across sentence boundaries will destroy multi-hop retrieval. Use semantic chunking or at minimum add 20% overlap.

Context precision below 0.65 with a reranker in place usually means your reranker is miscalibrated for your domain. Fine-tuning a cross-encoder on domain-specific query-passage pairs typically brings this to 0.80+.

## What RAGAS Doesn't Measure

RAGAS measures component quality, not end-to-end user satisfaction. A pipeline with faithfulness 0.9 can still produce answers that users find unhelpful if the question distribution in your eval set doesn't match production queries. Close the loop: log production queries, periodically add a sample to your eval dataset, and re-establish baselines quarterly.

RAGAS also doesn't measure latency, cost, or the long-tail behavior under adversarial input. Those need separate harnesses. But for the core question — "did this change make the RAG pipeline better or worse at grounding its answers in retrieved context?" — RAGAS gives you a reliable, reproducible answer that correlates strongly with what human evaluators care about. That's worth the CI overhead.

The teams that skip evaluation infrastructure inevitably end up with RAG pipelines that degrade silently, get worse with every "improvement," and can't be debugged because nobody established what "working" looked like. Build the harness before you iterate on the pipeline. It's the only way to know whether you're actually moving forward.
```