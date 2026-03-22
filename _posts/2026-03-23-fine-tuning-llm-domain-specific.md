---
layout: post
title: "Fine-Tuning LLMs for Domain-Specific Tasks: When and How"
date: 2026-03-23 08:00:00 +0700
tags: [ai-engineering, llm, fine-tuning, machine-learning, production]
description: "A practical decision framework for senior engineers: when fine-tuning LLMs actually wins over RAG, prompting, or function calling."
---

Your team just spent three weeks fine-tuning GPT-3.5 on 50,000 internal support tickets. The model now hallucinates your product's pricing in a slightly more confident tone. Meanwhile, a junior engineer added a 200-token system prompt with five examples and hit 89% accuracy on the same benchmark—in an afternoon. This story repeats itself constantly in engineering organizations right now, driven by the intuition that "more training = better results." Fine-tuning is real, powerful, and absolutely the right tool in specific circumstances. It is also the wrong tool in most of the circumstances where people reach for it.

## The Decision Framework Nobody Gives You

Before touching training data, answer four questions honestly:

1. **Have you exhausted prompt engineering?** Few-shot examples, chain-of-thought prompting, and structured output constraints solve an enormous category of "domain-specific" problems without any infrastructure cost.
2. **Is your problem knowledge retrieval or behavior modification?** RAG solves the former. Fine-tuning solves the latter. Confusing these two categories is the root cause of most failed fine-tuning projects.
3. **Do you have 1,000+ high-quality labeled examples?** Not scraped, not synthetic, not approximate—labeled by domain experts who understand what "correct" looks like for your use case.
4. **Can you afford to maintain this indefinitely?** Fine-tuned models are not deploy-and-forget. They drift, they need retraining as your domain evolves, and they introduce a new failure mode: the model that was right six months ago is now confidently wrong.

If you answered "no" to any of these, stop here and reach for a cheaper tool.

## When Fine-Tuning Actually Wins

There are three scenarios where fine-tuning delivers returns that no amount of prompting can match:

**Consistent output format at scale.** If you're generating thousands of structured documents per day—medical discharge summaries, legal contract clauses, financial reports—and the output format is rigid and proprietary, fine-tuning a smaller model (7B–13B) can be dramatically more reliable than wrestling a 70B model with elaborate prompts. At 10,000 documents/day, a model that requires a 1,500-token system prompt costs you roughly $45/day in tokens alone before you count the actual output. A fine-tuned 7B model running on your own infra can cut this to under $5/day and eliminate format-validation failures entirely.

**Latency-sensitive inference with specialized vocabulary.** Medical billing codes, legal citations, semiconductor chip identifiers—domains with massive proprietary vocabularies where the base model tokenizes inefficiently and hallucinates plausible-looking but wrong identifiers. A fine-tuned model learns both the vocabulary distribution and the output constraints simultaneously. You cannot achieve this with retrieval; you need weight-level memorization.

**Proprietary style and tone that cannot be described.** "Write like our lead analyst" sounds subjective until you realize that analyst has written 3,000 reports with a measurable style fingerprint. If your brand voice is genuinely distinctive and the cost of inconsistency is real (legal, regulatory, or client-facing), fine-tuning on validated examples is the only mechanism that internalizes style at the weight level.

## The Alternatives You Should Try First

**Prompt engineering with structured outputs.** Before anything else, try this:

```python
# snippet-1
import anthropic
from pydantic import BaseModel
from typing import Literal

class IncidentReport(BaseModel):
    severity: Literal["P0", "P1", "P2", "P3"]
    affected_systems: list[str]
    root_cause: str
    mitigation_steps: list[str]
    estimated_resolution_minutes: int

client = anthropic.Anthropic()

def classify_incident(raw_alert: str, runbook_context: str) -> IncidentReport:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system="""You are a senior SRE. Classify incidents using ONLY the severity levels and 
        system names defined in the provided runbook. Never invent system names.""",
        messages=[
            {
                "role": "user",
                "content": f"Runbook context:\n{runbook_context}\n\nAlert:\n{raw_alert}"
            }
        ],
        tools=[{
            "name": "report_incident",
            "description": "Structure the incident report",
            "input_schema": IncidentReport.model_json_schema()
        }],
        tool_choice={"type": "tool", "name": "report_incident"}
    )
    tool_use = next(b for b in response.content if b.type == "tool_use")
    return IncidentReport(**tool_use.input)
```

This handles the majority of "domain-specific format" requirements without fine-tuning. If the accuracy is 85%+ on your validation set, you're done.

**RAG for knowledge retrieval problems.** If the model doesn't know your internal terminology, product names, or proprietary processes, that's a retrieval problem, not a fine-tuning problem:

```python
# snippet-2
from openai import OpenAI
import chromadb
from chromadb.utils import embedding_functions

client = OpenAI()
chroma_client = chromadb.HttpClient(host="localhost", port=8000)
embed_fn = embedding_functions.OpenAIEmbeddingFunction(
    api_key=client.api_key,
    model_name="text-embedding-3-small"
)

collection = chroma_client.get_collection(
    name="internal_docs",
    embedding_function=embed_fn
)

def answer_with_context(question: str, n_results: int = 5) -> str:
    results = collection.query(
        query_texts=[question],
        n_results=n_results,
        include=["documents", "distances", "metadatas"]
    )
    
    # Filter by relevance threshold—don't inject noise
    relevant = [
        doc for doc, dist in zip(results["documents"][0], results["distances"][0])
        if dist < 0.35  # tune this per your embedding model
    ]
    
    if not relevant:
        return "I don't have specific documentation on this topic."
    
    context = "\n\n---\n\n".join(relevant)
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": "Answer using only the provided context. Quote relevant sections."},
            {"role": "user", "content": f"Context:\n{context}\n\nQuestion: {question}"}
        ]
    )
    return response.choices[0].message.content
```

The critical mistake engineers make here is fine-tuning on documentation instead of indexing it. Documents change. Weights are static. You will retrain forever to keep up with a living knowledge base.

## Data Requirements: The Part That Kills Projects

Assume you've decided fine-tuning is right. Your dataset will make or break this.

**Minimum viable dataset:** 500 examples for task adaptation, 2,000+ for meaningful style/tone transfer, 10,000+ for genuine capability extension. These are floor numbers assuming high-quality, validated data. Synthetic data from GPT-4 can augment but cannot replace human validation—models fine-tuned on model outputs develop artifacts that compound over iterations.

The format matters enormously for instruction fine-tuning:

<script src="https://gist.github.com/mohashari/c52eca5b111025af265a6a682b6ca6b1.js?file=snippet-3.txt"></script>go\nfunc (s *AccountService) DebitWithIdempotency(ctx context.Context, req DebitRequest) (*DebitResponse, error) {\n\tspan, ctx := opentracing.StartSpanFromContext(ctx, \"account.debit\")\n\tdefer span.Finish()\n\n\t// Check idempotency key first\n\texisting, err := s.idempotencyStore.Get(ctx, req.IdempotencyKey)\n\tif err != nil && !errors.Is(err, ErrNotFound) {\n\t\treturn nil, fmt.Errorf(\"idempotency check: %w\", err)\n\t}\n\tif existing != nil {\n\t\treturn existing, nil\n\t}\n\n\t// ... rest of implementation\n}\n```"}]}
{"messages": [{"role": "system", "content": "You are a backend engineer writing Go code for a high-throughput payment processing system. Always use structured errors, never panic in request handlers, and always include context propagation."}, {"role": "user", "content": "How should I handle database connection timeouts?"}, {"role": "assistant", "content": "Set explicit timeouts at the query level, not just the connection level. Use context deadlines that propagate from the HTTP request. Never use context.Background() in request handlers—always derive from the incoming context. For connection pool exhaustion, return a 503 with Retry-After, not a 500."}]}
```

Notice what's captured here: the system prompt is consistent, the responses include concrete implementation decisions, and the style is opinionated. Generic "helpful assistant" responses contaminate fine-tuning datasets and regress the model toward averaging behavior.

**Data quality checklist before you start training:**
- Every example reviewed by a domain expert, not just the person who wrote it
- No examples that you'd be embarrassed to see the model reproduce verbatim
- Consistent system prompt across all examples (or no system prompt if you'll inject at inference time)
- Validation split held out before any training—never touched during dataset construction

## Infrastructure Reality Check

Running fine-tuned models at production scale is an infrastructure commitment, not a one-time experiment.

For OpenAI fine-tuning (the simplest path):

```bash
# snippet-4
# Validate your JSONL before uploading—OpenAI will reject malformed files
openai tools fine_tunes.prepare_data -f training_data.jsonl

# Upload and start training
openai api fine_tunes.create \
  -t training_data_prepared.jsonl \
  -v validation_data_prepared.jsonl \
  -m gpt-3.5-turbo \
  --suffix "payments-v1" \
  --n_epochs 3 \
  --batch_size 4 \
  --learning_rate_multiplier 0.1

# Monitor training
openai api fine_tunes.follow -i ft-XXXXXXXXXXXXXXXX
```

OpenAI fine-tuning costs roughly $0.008/1K tokens for gpt-3.5-turbo. At 50,000 training examples averaging 500 tokens each, you're at $200 for a single training run. Budget for at least three runs to find the right hyperparameters, plus ongoing inference at 2x the base model rate. The math works if you're replacing expensive frontier model calls with cheaper fine-tuned model calls at scale.

For self-hosted fine-tuning with Axolotl (the production-grade path for Llama/Mistral variants):

```yaml
# snippet-5
# axolotl config for instruction fine-tuning on domain data
base_model: mistralai/Mistral-7B-Instruct-v0.3
model_type: MistralForCausalLM
tokenizer_type: LlamaTokenizer

load_in_8bit: false
load_in_4bit: true
strict: false

datasets:
  - path: ./data/training.jsonl
    type: chat_template
    chat_template: chatml

val_set_size: 0.05
output_dir: ./outputs/payments-v1

sequence_len: 4096
sample_packing: true
pad_to_sequence_len: true

adapter: qlora
lora_r: 32
lora_alpha: 16
lora_dropout: 0.05
lora_target_linear: true

gradient_accumulation_steps: 4
micro_batch_size: 2
num_epochs: 3
optimizer: adamw_bnb_8bit
lr_scheduler: cosine
learning_rate: 0.0002

# Evaluation during training
eval_steps: 100
save_steps: 200
logging_steps: 10
```

A single A100-40GB handles a 7B model with QLoRA. Spot instance on AWS: ~$1.50/hour. A typical fine-tuning run on 5,000 examples takes 2-4 hours. The adapter weights are 100-300MB, not 14GB—deploy the base model once, swap adapters per tenant if you're building a multi-tenant product.

## Evaluation: Where Most Projects Fail

You need three evaluation layers, and skipping any of them gives you false confidence:

```python
# snippet-6
import json
from dataclasses import dataclass
from typing import Callable

@dataclass
class EvalResult:
    exact_match: float
    format_validity: float
    semantic_similarity: float
    regression_rate: float  # % of baseline-correct examples now wrong

def run_evaluation_suite(
    model_fn: Callable[[str], str],
    baseline_model_fn: Callable[[str], str],
    test_cases: list[dict],
    format_validator: Callable[[str], bool],
    semantic_scorer: Callable[[str, str], float],
) -> EvalResult:
    exact_matches = 0
    format_valid = 0
    semantic_scores = []
    regressions = 0
    baseline_correct = 0

    for case in test_cases:
        output = model_fn(case["input"])
        baseline_output = baseline_model_fn(case["input"])
        expected = case["expected"]

        if output.strip() == expected.strip():
            exact_matches += 1

        if format_validator(output):
            format_valid += 1

        semantic_scores.append(semantic_scorer(output, expected))

        baseline_was_correct = semantic_scorer(baseline_output, expected) > 0.85
        fine_tuned_is_wrong = semantic_scorer(output, expected) < 0.70

        if baseline_was_correct:
            baseline_correct += 1
            if fine_tuned_is_wrong:
                regressions += 1

    n = len(test_cases)
    return EvalResult(
        exact_match=exact_matches / n,
        format_validity=format_valid / n,
        semantic_similarity=sum(semantic_scores) / n,
        regression_rate=regressions / max(baseline_correct, 1),
    )
```

The regression rate is the metric nobody tracks until they've been burned. Fine-tuning on domain-specific data routinely degrades general capability. If your fine-tuned model is better at payment processing but now fails 15% of previously-correct general reasoning tasks, you have a problem—especially if your system prompt evolves and hits edge cases outside the training distribution.

Set hard gates: regression rate below 5%, format validity above 95%, semantic similarity above 0.80 on your validation set. If you can't hit these, your training data has quality issues, not hyperparameter issues.

## The Hidden Maintenance Burden

Three months after deployment, your fine-tuned model starts drifting from your domain. Your product added new features, your terminology evolved, your edge cases multiplied. Now you face a choice: retrain (same cost, same timeline) or layer prompts on top of a model that was trained to ignore prompts. Both options are painful.

Budget for quarterly retraining cycles minimum. Instrument your production traffic to automatically flag low-confidence outputs as potential training candidates. Build a human review queue for edge cases. The annotation pipeline is not a one-time project—it's ongoing operational cost that belongs in your LLMOps budget.

The models most organizations should fine-tune are the ones running highest-volume, most stable tasks. "Stable" is the key word. If your task definition, output format, or success criteria change more than twice a year, the maintenance burden of fine-tuning exceeds its benefits. Prompt engineering adapts in minutes. Retraining takes weeks.

## The Actual Decision Checklist

Before starting any fine-tuning project, validate each item:

- [ ] Prompt engineering + few-shot examples tested and measured below acceptable threshold
- [ ] RAG evaluated and confirmed insufficient for this problem type
- [ ] 1,000+ expert-validated examples available (not synthetic, not approximate)
- [ ] Evaluation suite built with regression testing against baseline model
- [ ] Infrastructure budget approved: training cost + inference markup + quarterly retraining
- [ ] Maintenance ownership assigned: who owns the annotation pipeline in six months?
- [ ] Rollback plan defined: what happens when the fine-tuned model regresses in production?

If you have all seven checked, fine-tuning is probably the right tool. If you're missing more than two, you're not ready and the project will underdeliver.

The hype cycle around fine-tuning exists because it's a genuinely powerful technique that sounds simpler than it is. The engineering discipline required—data quality, evaluation rigor, operational maintenance—is identical to building any other production ML system. Treat it that way, and you'll ship something that works. Treat it as a configuration option, and you'll be explaining to your CTO why the "smarter" model is worse than the original.
```