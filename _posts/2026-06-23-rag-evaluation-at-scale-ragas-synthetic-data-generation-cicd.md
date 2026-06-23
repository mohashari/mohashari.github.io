---
layout: post
title: "RAG Evaluation at Scale: Implementing Ragas with Synthetic Data Generation in CI/CD"
date: 2026-06-23 08:00:00 +0700
tags: [ai-engineering, rag, cicd]
description: "Stop vibe-checking your RAG pipelines. Learn how to implement automated evaluations using synthetic data generation, semantic pruning, and Ragas metrics in CI/CD."
image: "https://picsum.photos/seed/rageval/1080/720"
thumbnail: "https://picsum.photos/seed/rageval/400/300"
---

Vibe checking is the silent killer of production-grade Retrieval-Augmented Generation (RAG) systems. We have all been there: a RAG pipeline works flawlessly for ten mock queries during a client demo, but falls flat on its face in production when users start inputting queries that stretch the bounds of your vector store's indexing strategy or challenge your prompt constraints. Tweaking a chunk size, changing an embedding model (e.g., swapping `text-embedding-3-small` for `cohere-embed-english-v3.0`), or refining a system prompt are often done based on subjective verification of a few handpicked queries. This manual approach is not only statistically insignificant but introduces silent regressions where improving one query degrades ten others. To build a resilient RAG system, you must treat generation and retrieval quality as software features that are measured quantitatively, evaluated deterministically, and gated continuously in your CI/CD pipeline.

## Why RAG Evaluation Fails Without Automation

Before automating evaluation, you must define what to measure. Standard Natural Language Processing (NLP) metrics like BLEU or ROUGE are useless for RAG evaluation. They evaluate surface-level token overlap against a reference text. Consider a query: *"How often must a user rotate their credentials?"*
*   **Ground Truth:** *"The user must rotate their credentials every 90 days."*
*   **System A:** *"Credentials must be updated every three months."*
*   **System B:** *"The user must rotate their credentials every 9 days."*

System A is semantically perfect but would receive a low BLEU score due to vocabulary differences ("updated", "three months"). System B is factually incorrect and highly dangerous, yet it would score exceptionally high on BLEU and ROUGE due to 90% direct token overlap.

Human evaluation, while highly accurate, fails to scale. It introduces human bias, is prone to fatigue, and is prohibitively expensive to run continuously. A developer cannot review 100 queries for every single git commit. If your team has five engineers making three commits a day, that is 1,500 manual evaluations daily. This creates an immediate bottleneck, causing teams to skip evaluation entirely, leading to silent regressions.

RAGAS (Retrieval-Augmented Generation Assessment) solves this by decomposing pipeline quality into isolated, interpretable metrics that map directly to your architecture's failure modes. By isolating retrieval performance from generation quality, you can pinpoint exactly which layer of your system needs adjustment:

1.  **Faithfulness (Anti-Hallucination):** Measures whether the generated answer is strictly grounded in the retrieved contexts. It uses an LLM judge to extract statements from the generated answer and verify if each statement can be inferred from the context. If your faithfulness score drops, your generation prompt is too loose or your LLM is hallucinating.
2.  **Answer Relevancy:** Measures how directly the generated answer addresses the user query. By converting the generated answer back into potential questions and comparing them to the user's actual question via embedding cosine similarity, RAGAS penalizes answers that are technically correct but meander or contain irrelevant details.
3.  **Context Precision:** Evaluates the retriever. It measures whether the most relevant chunks are ranked at the top of the context window. This is critical because LLMs exhibit "lost in the middle" phenomena, where they attend more strongly to information presented at the absolute beginning or end of the context payload. RAGAS Context Precision rewards systems that rank relevant documents higher, ensuring that the model processes the most critical information early.
4.  **Context Recall:** Measures whether the retriever captured all key information required to answer the question, compared to a reference ground truth. A low context recall indicates that your chunking is too small, your metadata filtering is too restrictive, or your vector database search parameters (such as HNSW parameters like `ef_search`) need tuning. If the retriever missed a single crucial document, the model is forced to hallucinate or declare ignorance.

## Phase 1: Generating the Synthetic Dataset at Scale

Manually creating a high-quality test set of 200+ question-context-answer triples is a massive bottleneck. It requires domain expertise, takes weeks of manual labor, and drifts rapidly as your document corpus updates. 

Synthetic data generation solves this by using your actual document corpus to generate test cases. RAGAS provides a `TestsetGenerator` that parses your raw documents, extracts context chunks, and uses an LLM to evolve simple questions into complex reasoning patterns:
*   **Simple Evolution:** Formulates a simple question directly mapping to a specific chunk.
*   **Reasoning Evolution:** Extends a simple question by adding logical steps or constraints, requiring the LLM to deduce the answer from the chunk.
*   **Multi-Context Evolution:** Finds multiple semantically adjacent or connected documents and crafts a query that cannot be answered by either chunk in isolation, forcing the retriever to retrieve multiple chunks from different documents.
*   **Conditional Evolution:** Formulates a question containing conditional constraints to evaluate context routing logic.

Simple queries are too easy. If a user asks a question that can be answered by extracting a single sentence from one document, any baseline retriever will succeed. But real-world users ask multi-hop questions. For example, in a codebase documentation context, they might ask: *"How does the authentication middleware interact with the session store?"* Answering this requires retrieving the authentication configuration chunk *and* the session store initialization chunk. RAGAS Multi-Context Evolution simulates this by finding two semantically linked chunks, prompting an LLM to identify the intersection, and generating a question that forces the system to retrieve both documents to formulate a correct response. This exposes weaknesses in your vector database's top-k settings, which might retrieve the first document but miss the second.

However, running synthetic generation naively at scale presents significant challenges: API rate-limiting (HTTP 429), token costs, and catastrophic failures midway through a run that result in data loss. To make generation production-ready, you must implement asynchronous batching, rate-limiting handlers, and checkpoints to save intermediate progress to disk.

<script src="https://gist.github.com/mohashari/896129d486748fadc9b53b30694e192b.js?file=snippet-1.py"></script>

## Phase 2: Semantic Pruning for Cost-Effective CI Runs

While a 150-question test suite is fine for offline verification, running 150 evaluations on every pull request is too slow and expensive. With an average of 4 metrics evaluated per question (generating multiple LLM queries for statement extraction, verification, and embedding comparisons), a single CI run could require over 1,500 LLM calls. At current pricing, this costs several dollars per run and can take 20 minutes to execute, clogging your deployment queue.

To make this cost-effective, you must prune the generated test suite to a representative, high-diversity subset (e.g., 40–50 questions). A naive random sample is insufficient; it risks selecting duplicate questions or leaving out edge cases. 

Instead, use semantic clustering. To perform semantic clustering, we first convert our raw generated questions into high-dimensional vectors using an embedding model like `text-embedding-3-small`. In this 1536-dimensional vector space, semantically similar questions sit close to each other. Running K-Means partitions these vectors into $k$ distinct clusters, where $k$ represents our target test suite size (e.g., 40). If we have 200 raw questions, K-Means will group them into 40 clusters of roughly 5 questions each. For each cluster, we identify the centroid—the mathematical average of all vectors in that cluster—and select the question whose embedding is closest to it. This centroid question serves as the representative for that semantic domain. By executing only the centroid questions, we prune 80% of our test suite while maintaining maximum semantic coverage, ensuring that a regression in any single topical domain will still be caught.

<script src="https://gist.github.com/mohashari/896129d486748fadc9b53b30694e192b.js?file=snippet-2.py"></script>

Once generated and pruned, track changes to the Parquet dataset using Data Version Control (DVC) or Git LFS. This prevents bloating your main Git repository history with large binary files while keeping the test suite version-controlled alongside your code.

## Phase 3: The Evaluation Runner & Test Harness

Once your evaluation suite is pruned and versioned, you need to implement a pipeline execution script that runs the system under test, collects outputs, executes the Ragas evaluation library, and produces a structured metric file.

Decouple pipeline execution from LLM judges. Run the RAG pipeline to generate the answers and retrieved contexts *first*, save them, and then feed the complete payload to the evaluator. This architecture prevents running expensive LLM evaluations if the core pipeline throws an unhandled exception or times out.

<script src="https://gist.github.com/mohashari/896129d486748fadc9b53b30694e192b.js?file=snippet-3.py"></script>

## Phase 4: Integrating Ragas into CI/CD (GitHub Actions)

An evaluation framework is only as good as its integration into developers' everyday workflows. To enforce quality standards, the evaluation run must execute automatically during pull requests. 

However, since evaluating RAG pipelines consumes both time and API credits, you should optimize execution triggers. Only trigger the evaluation job when files that impact RAG performance are modified—such as retriever logic, prompt files, chunking scripts, or configuration files. If a developer only updates a frontend CSS style or API endpoint routing, the workflow should bypass LLM evaluations.

```yaml
# // snippet-4
name: RAG Pipeline Continuous Evaluation

on:
  pull_request:
    paths:
      - 'src/retriever/**'
      - 'src/prompts/**'
      - 'src/chunking/**'
      - 'pipeline.py'

jobs:
  evaluate-rag:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout Code
        uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'
          cache: 'pip'

      - name: Install Dependencies
        run: |
          pip install -r requirements-eval.txt

      - name: Run Evaluation on Current PR Branch
        env:
          OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
        run: |
          python scripts/run_eval.py --dataset eval_data/pruned_eval_set.parquet --output pr_results.json

      - name: Compare Against Main Branch Baseline
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: |
          # Retrieve main baseline from storage (fallback to checking out file from main)
          git checkout origin/main -- eval_data/baseline_results.json || echo "{}" > eval_data/baseline_results.json
          python scripts/compare_metrics.py \
            --baseline eval_data/baseline_results.json \
            --pr pr_results.json \
            --threshold 0.05
```

## Phase 5: Minimizing Cost and Latency with Evaluation Caching

Even with a pruned dataset of 40 questions, running evaluations repeatedly on successive PR commits generates redundant API calls. For example, if a developer adjusts only a prompt template but makes no modifications to the chunking or retriever code, the retrieved contexts remain identical. 

Because the Ragas evaluation engine is deterministic when run with a judge LLM at `temperature=0.0`, you can cache previous judge outputs. By storing a cryptographic hash of `(question, context, generated_answer)` alongside the metric score, you can skip LLM evaluations entirely for unchanged parameters. This optimization routinely reduces CI execution times by 60–80% and drastically lowers developer API costs.

In a team environment, you can upload and download this cache file as a GitHub Actions workflow artifact or store it in a shared bucket (e.g., S3 or GCS) to distribute cached evaluations across different developers' branches.

<script src="https://gist.github.com/mohashari/896129d486748fadc9b53b30694e192b.js?file=snippet-5.py"></script>

To complete the pipeline, you need the gatekeeper comparison script (`scripts/compare_metrics.py`). This script parses the baseline values against the newly run PR metrics and fails the build with exit code `1` if any regression exceeds the defined threshold.

<script src="https://gist.github.com/mohashari/896129d486748fadc9b53b30694e192b.js?file=snippet-6.py"></script>

## Production Failure Modes of Ragas in CI/CD

Integrating LLM-as-a-Judge evaluations into your CI/CD pipeline is not a silver bullet. Productionizing this workflow introduces practical issues that can break your pipeline or trigger false-alarm build failures. 

### LLM Judge Variance and Non-Determinism
Even at `temperature=0.0`, large language model outputs are not 100% deterministic due to GPU floating-point precision variances and load-balancing across different engine nodes. A metric score that evaluates to `0.85` on `main` may return as `0.82` on the PR branch, despite no code changes. 

**Mitigation:** Never set the degradation gating threshold to `0.0`. Allow a variance buffer (e.g., `0.05`). Additionally, you can run multiple evaluations for a single test point and use the mean value to smooth out random variance.

### Rate Limiting in Concurrent Builds
When multiple PR builds run concurrently, your CI runner environments will make simultaneous requests to your LLM API endpoint (e.g., OpenAI or Anthropic). If you hit your Tier rate limits, the CI job will crash mid-run.

**Mitigation:** Implement client-side rate limits inside your runner. In addition, provision a dedicated API key with elevated rate limits (e.g., Tier 3 or higher) exclusively for the CI environment, separated from development or staging.

### Corpus Drift and Schema Evolution
A static synthetic test suite will quickly become stale as you update your documentation corpus, add new knowledge base documents, or prune obsolete features. If your application codebase evolves but your test dataset references deprecated resources, context recall scores will drop false-negatively.

**Mitigation:** Run a weekly or monthly scheduled cron job in GitHub Actions that automatically runs the testset generation pipeline (Snippet 1), clusters and prunes the dataset (Snippet 2), reviews it programmatically, and updates the versioned dataset file committed to the repository or stored in an external registry like MLflow or Git LFS.

## Conclusion

Treating RAG development as an art form rather than an engineering discipline is a recipe for regression. By shifting your evaluation practices from manual vibe-checks to automated, synthetically generated evaluation sets, and gating builds directly in CI/CD with Ragas, you gain quantitative proof of your system's performance. The upfront investment in implementing robust async generation, semantic dataset pruning, and structured metric gating will repay itself instantly when it prevents a degraded retriever or hallucination-prone prompt from ever reaching your production environments.