---
layout: post
title: "Multi-Modal LLMs in Production: Vision, Text, and Data Pipelines"
date: 2026-03-22 08:00:00 +0700
tags: [ai-engineering, llm, python, machine-learning, backend]
description: "How to build production-grade multi-modal LLM pipelines that handle vision, text, and structured data without collapsing under real load."
---

Your document processing pipeline works great in the demo. You feed it a PDF invoice, the model extracts line items, totals match, product manager is thrilled. Then you hit production: scanned invoices with skewed text, handwritten annotations, mixed-language documents, images embedded inside PDFs, and throughput requirements of 500 documents per minute. The model starts hallucinating totals, latency spikes to 12 seconds per document, and your GPU bill triples. Multi-modal LLMs in production are not about calling an API with an image URL — they are about building pipelines that stay accurate under adversarial real-world inputs, scale without burning money, and fail gracefully when models do what models do.

## The Multi-Modal Input Problem

Most engineers underestimate how messy real input is. A "PDF invoice" is not a clean image — it might be a scanned TIFF wrapped in PDF, a vector PDF with embedded fonts that render incorrectly at low DPI, or a hybrid with some text layers and some rasterized regions. Before the LLM sees anything, you need a preprocessing stage that normalizes inputs aggressively.

The canonical stack for document preprocessing looks like this: PyMuPDF for PDF parsing, pdf2image for rasterization at 200+ DPI, OpenCV for deskew and contrast normalization, and Tesseract or a commercial OCR provider as a fallback signal. The key insight is that you should not trust any single modality. When you extract text via PDF text layer AND via OCR, then compare confidence scores, you catch the cases where the text layer is garbled (embedded font issues are more common than you think at 8%).

```python
# snippet-1
import fitz  # PyMuPDF
import pytesseract
from PIL import Image
from pdf2image import convert_from_bytes
import numpy as np
import cv2

def extract_document_content(pdf_bytes: bytes, dpi: int = 200) -> dict:
    """
    Dual-path extraction: PDF text layer + OCR with confidence scoring.
    Falls back to OCR-only if text layer confidence is below threshold.
    """
    result = {"text_layer": None, "ocr_text": None, "images": [], "strategy": None}

    # Path 1: PDF text layer
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    text_layer_content = []
    for page in doc:
        blocks = page.get_text("dict")["blocks"]
        for block in blocks:
            if block["type"] == 0:  # text block
                for line in block["lines"]:
                    for span in line["spans"]:
                        text_layer_content.append({
                            "text": span["text"],
                            "size": span["size"],
                            "bbox": span["bbox"],
                        })
    
    text_layer_str = " ".join(s["text"] for s in text_layer_content)
    text_layer_confidence = len(text_layer_str.strip()) / max(len(pdf_bytes) / 1000, 1)

    # Path 2: OCR on rasterized pages
    pil_images = convert_from_bytes(pdf_bytes, dpi=dpi)
    ocr_results = []
    page_images = []

    for img in pil_images:
        # Deskew
        cv_img = np.array(img.convert("L"))
        coords = np.column_stack(np.where(cv_img < 200))
        if len(coords) > 100:
            angle = cv2.minAreaRect(coords)[-1]
            if abs(angle) < 45:
                (h, w) = cv_img.shape
                M = cv2.getRotationMatrix2D((w // 2, h // 2), angle, 1.0)
                cv_img = cv2.warpAffine(cv_img, M, (w, h), flags=cv2.INTER_CUBIC)

        ocr_data = pytesseract.image_to_data(
            cv_img, output_type=pytesseract.Output.DICT, config="--psm 6"
        )
        page_text = " ".join(
            w for w, c in zip(ocr_data["text"], ocr_data["conf"])
            if int(c) > 60 and w.strip()
        )
        ocr_results.append(page_text)
        page_images.append(img)

    result["text_layer"] = text_layer_str
    result["ocr_text"] = " ".join(ocr_results)
    result["images"] = page_images
    result["strategy"] = "text_layer" if text_layer_confidence > 0.5 else "ocr"

    return result
```

## Model Selection Is Not Model Picking

There is no single best multi-modal model for production. GPT-4o, Claude 3.5 Sonnet, Gemini 1.5 Pro, and LLaVA-1.6 each have different cost/accuracy/latency profiles that matter depending on your use case.

For structured extraction (invoices, receipts, forms), Gemini 1.5 Pro with its 1M token context window wins when you need to process entire multi-page documents in a single pass — no chunking logic, no page-boundary errors. For real-time analysis (< 2 second SLA), Claude 3.5 Haiku at roughly $0.25/M input tokens is the pragmatic choice. For on-premise or air-gapped deployments, LLaVA-1.6-34B on 2x A100s gives you 80-85% of GPT-4o accuracy at zero per-call cost after infrastructure.

The decision framework should be encoded explicitly, not left to intuition:

```python
# snippet-2
from dataclasses import dataclass
from enum import Enum

class TaskType(Enum):
    STRUCTURED_EXTRACTION = "structured_extraction"
    IMAGE_CLASSIFICATION = "image_classification"
    DOCUMENT_QA = "document_qa"
    REAL_TIME_ANALYSIS = "real_time_analysis"

@dataclass
class ModelConfig:
    provider: str
    model_id: str
    max_image_size_px: int
    max_images_per_call: int
    cost_per_1k_input_tokens: float
    avg_latency_ms: int
    supports_json_mode: bool

MODEL_REGISTRY = {
    "gpt-4o": ModelConfig(
        provider="openai", model_id="gpt-4o",
        max_image_size_px=2048, max_images_per_call=10,
        cost_per_1k_input_tokens=0.005, avg_latency_ms=3200,
        supports_json_mode=True,
    ),
    "claude-3-5-haiku": ModelConfig(
        provider="anthropic", model_id="claude-haiku-4-5-20251001",
        max_image_size_px=1568, max_images_per_call=20,
        cost_per_1k_input_tokens=0.00025, avg_latency_ms=900,
        supports_json_mode=False,
    ),
    "gemini-1-5-pro": ModelConfig(
        provider="google", model_id="gemini-1.5-pro",
        max_image_size_px=3072, max_images_per_call=3000,
        cost_per_1k_input_tokens=0.00125, avg_latency_ms=4500,
        supports_json_mode=True,
    ),
}

def select_model(
    task: TaskType,
    num_pages: int,
    latency_sla_ms: int,
    budget_per_call_usd: float,
) -> str:
    if latency_sla_ms < 1500:
        return "claude-3-5-haiku"
    if task == TaskType.STRUCTURED_EXTRACTION and num_pages > 5:
        return "gemini-1-5-pro"
    if budget_per_call_usd < 0.01:
        return "claude-3-5-haiku"
    return "gpt-4o"
```

## Prompt Engineering for Vision Is Different

Prompting a vision model is not the same as prompting a text model. The model's attention is split between visual tokens and text tokens — verbose system prompts compete with image understanding. Keep system prompts under 200 tokens for vision tasks. Be explicit about spatial references ("top-left", "below the header line", "in the red box") because the model cannot know your coordinate system.

For structured extraction, JSON schema in the prompt is non-negotiable. Vague instructions like "extract the invoice fields" produce inconsistent output. Explicit schema with descriptions of edge cases produces output you can parse reliably at 99.2%+ success rate:

```python
# snippet-3
INVOICE_EXTRACTION_PROMPT = """
Extract invoice data from this document image. Return ONLY valid JSON matching this schema:

{
  "invoice_number": "string | null",
  "invoice_date": "ISO 8601 date string | null",
  "vendor": {
    "name": "string",
    "tax_id": "string | null"
  },
  "line_items": [
    {
      "description": "string",
      "quantity": "number",
      "unit_price": "number",
      "total": "number"
    }
  ],
  "subtotal": "number | null",
  "tax_amount": "number | null",
  "total_due": "number",
  "currency": "3-letter ISO currency code"
}

Rules:
- If invoice_number is not visible, use null
- All monetary values in base currency units (not cents)
- For handwritten amounts, use your best OCR reading
- If line_items total does not match total_due, include both as seen in the document
- currency defaults to "USD" if not specified
"""

async def extract_invoice(image_b64: str, model: str = "gpt-4o") -> dict:
    import openai
    import json

    client = openai.AsyncOpenAI()
    response = await client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": INVOICE_EXTRACTION_PROMPT},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{image_b64}",
                            "detail": "high",
                        },
                    },
                ],
            }
        ],
        max_tokens=1500,
        response_format={"type": "json_object"},
        temperature=0,
    )

    raw = response.choices[0].message.content
    return json.loads(raw)
```

## Pipeline Architecture Under Load

At 500 documents per minute, you cannot process synchronously. The architecture that works in production: a queue-based pipeline with three stages, each independently scalable.

Stage 1 (Preprocessor): CPU-bound. Runs on 8-core workers, 4 workers per machine. Handles PDF parsing, deskew, and image normalization. Output goes to S3 with presigned URLs valid for 15 minutes.

Stage 2 (LLM Caller): IO-bound. Python async workers using `asyncio` with controlled concurrency — 50 concurrent calls per worker instance. Each call has a 30-second timeout with one retry on 429 or 529 (rate limit / overload). Exponential backoff starting at 1 second.

Stage 3 (Validator): CPU-bound. Validates extracted JSON against Pydantic models, runs business rule checks (totals must reconcile within 0.01 currency units), writes to PostgreSQL.

```yaml
# snippet-4
# docker-compose.yml for local staging of the pipeline
version: "3.9"

services:
  redis:
    image: redis:7-alpine
    ports: ["6379:6379"]

  preprocessor:
    build:
      context: .
      dockerfile: Dockerfile.preprocessor
    environment:
      QUEUE_IN: raw_documents
      QUEUE_OUT: preprocessed_documents
      REDIS_URL: redis://redis:6379
      S3_BUCKET: doc-pipeline-staging
      WORKERS: 4
    deploy:
      replicas: 2
    depends_on: [redis]

  llm_caller:
    build:
      context: .
      dockerfile: Dockerfile.llm_caller
    environment:
      QUEUE_IN: preprocessed_documents
      QUEUE_OUT: extracted_data
      REDIS_URL: redis://redis:6379
      OPENAI_API_KEY: ${OPENAI_API_KEY}
      ANTHROPIC_API_KEY: ${ANTHROPIC_API_KEY}
      CONCURRENCY: 50
      TIMEOUT_SECONDS: 30
    deploy:
      replicas: 3
    depends_on: [redis]

  validator:
    build:
      context: .
      dockerfile: Dockerfile.validator
    environment:
      QUEUE_IN: extracted_data
      REDIS_URL: redis://redis:6379
      DATABASE_URL: ${DATABASE_URL}
      WORKERS: 2
    deploy:
      replicas: 2
    depends_on: [redis]
```

The async LLM caller is where most teams introduce subtle bugs. Rate limit handling must be per-provider, per-model, and aware of token counts — not just request counts. OpenAI's tier-based limits are per-minute and per-day. Hitting a daily token limit at 11:58pm and retrying until midnight wastes 2 minutes of throughput. Track limits explicitly:

```python
# snippet-5
import asyncio
import time
from collections import deque
from typing import Callable, Any

class TokenBucketRateLimiter:
    """
    Sliding window rate limiter tracking both request count and token count.
    Respects provider limits: requests/min and tokens/min independently.
    """

    def __init__(
        self,
        max_requests_per_min: int,
        max_tokens_per_min: int,
    ):
        self.max_requests = max_requests_per_min
        self.max_tokens = max_tokens_per_min
        self._request_timestamps: deque = deque()
        self._token_log: deque = deque()  # (timestamp, tokens)
        self._lock = asyncio.Lock()

    async def acquire(self, estimated_tokens: int) -> float:
        """Returns seconds waited."""
        async with self._lock:
            now = time.monotonic()
            window = 60.0

            # Evict old entries
            while self._request_timestamps and now - self._request_timestamps[0] > window:
                self._request_timestamps.popleft()
            while self._token_log and now - self._token_log[0][0] > window:
                self._token_log.popleft()

            current_tokens = sum(t for _, t in self._token_log)
            wait = 0.0

            if len(self._request_timestamps) >= self.max_requests:
                wait = max(wait, window - (now - self._request_timestamps[0]))
            if current_tokens + estimated_tokens > self.max_tokens:
                oldest_relevant = next(
                    (ts for ts, t in self._token_log if current_tokens - t + estimated_tokens <= self.max_tokens),
                    self._token_log[0][0] if self._token_log else now,
                )
                wait = max(wait, window - (now - oldest_relevant))

            if wait > 0:
                await asyncio.sleep(wait)
                now = time.monotonic()

            self._request_timestamps.append(now)
            self._token_log.append((now, estimated_tokens))
            return wait


# One limiter instance per model per worker process
RATE_LIMITERS = {
    "gpt-4o": TokenBucketRateLimiter(max_requests_per_min=500, max_tokens_per_min=300_000),
    "claude-haiku-4-5-20251001": TokenBucketRateLimiter(max_requests_per_min=1000, max_tokens_per_min=100_000),
}
```

## Handling the Long Tail of Failures

In production, roughly 3-5% of documents will fail or return degraded output. The failure taxonomy matters for building the right fallback logic:

**Model refusals** (~0.5%): The model declines to process content it identifies as potentially sensitive. These are almost always false positives on documents containing medical or financial PII. Fix: add a system prompt line asserting authorized processing context. Do not try to strip PII before sending — you will corrupt the document structure.

**JSON parse failures** (~1.2%): The model returns almost-valid JSON with a trailing comma or a comment. Fix: use `json-repair` library before falling back to regex extraction. `json-repair` handles 85% of malformed outputs automatically.

**Hallucinated fields** (~0.8%): The model invents a field that does not exist in the document. This is measurable when you have OCR text as a baseline — if an extracted value does not appear verbatim or as a numeric near-match in the OCR text, flag it. Do not auto-correct; route to human review queue.

**Timeout/overload** (~2%): Provider returning 529 or connection timeout. Fixed with retry + exponential backoff with jitter. Cap at 3 retries. If all retries fail, route to secondary model.

```python
# snippet-6
import asyncio
import json
import random
import logging
from json_repair import repair_json

logger = logging.getLogger(__name__)

async def call_llm_with_fallback(
    primary_fn: Callable,
    fallback_fn: Callable,
    max_retries: int = 3,
) -> dict | None:
    """
    Retry with exponential backoff, fall back to secondary model on exhaustion.
    Returns None if both primary and fallback fail — caller routes to DLQ.
    """
    last_exc = None

    for attempt in range(max_retries):
        try:
            raw = await asyncio.wait_for(primary_fn(), timeout=30)
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                repaired = repair_json(raw, return_objects=True)
                if isinstance(repaired, dict) and repaired:
                    logger.warning("Used json-repair on attempt %d", attempt)
                    return repaired
                raise ValueError(f"Unrepairable JSON: {raw[:200]}")

        except asyncio.TimeoutError as e:
            last_exc = e
            logger.warning("Timeout on attempt %d", attempt)
        except Exception as e:
            code = getattr(e, "status_code", None)
            if code in (429, 529, 503):
                last_exc = e
                wait = (2 ** attempt) + random.uniform(0, 1)
                logger.warning("Rate limit/overload (attempt %d), waiting %.1fs", attempt, wait)
                await asyncio.sleep(wait)
                continue
            raise  # Non-retriable errors propagate immediately

    # Primary exhausted — try fallback once
    logger.error("Primary model failed after %d attempts, trying fallback", max_retries)
    try:
        raw = await asyncio.wait_for(fallback_fn(), timeout=45)
        return json.loads(repair_json(raw))
    except Exception as e:
        logger.error("Fallback also failed: %s", e)
        return None
```

## Observability Is Not Optional

At scale, you need per-document tracing. Every document that enters your pipeline should have a `trace_id` that follows it through all three stages and is stored alongside the final output. When a customer calls to say "invoice #INV-2024-9987 was extracted wrong", you need to pull the original image, the OCR text, the exact prompt sent, the raw model response, and the parsed output — all correlated by that ID.

Use OpenTelemetry spans for stage timing. The metrics that matter: preprocessing latency (p50/p95/p99), LLM call latency, extraction success rate, validation pass rate, and retry rate. Alert when retry rate exceeds 5% — that is the earliest signal that a provider is degrading before they post a status page update.

Store raw model responses in S3, not in your primary database. A raw GPT-4o response for a 10-page invoice can be 8KB of JSON. At 500 docs/minute, that is 240MB/minute of raw response data — fine for S3, catastrophic for Postgres row storage.

## Image Sizing and Token Economics

Vision models charge for images based on size. GPT-4o with `detail: high` splits images into 512x512 tiles and charges 170 tokens per tile plus 85 tokens base. A 2048x2048 image costs 4x4 tiles × 170 + 85 = 2,805 tokens — about $0.014 per image at current pricing. For a 10-page document that is $0.14 per document in image tokens alone, before any text.

The optimization most teams miss: resize images to the minimum resolution that preserves the text needed for extraction. For machine-printed text, 150 DPI is sufficient. For handwritten text, 200 DPI. For documents with fine print (legal disclaimers, nutritional labels), 300 DPI. Scaling from 300 DPI to 150 DPI cuts image token cost by 75%. At 500 docs/minute, that is a meaningful number.

Build a resolution profiler that measures extraction accuracy at different DPI settings against your document corpus. Run it quarterly — your document mix changes as your customer base grows, and the optimal resolution setting drifts.

The production reality of multi-modal LLMs is that the model is the easy part. The hard parts are: normalizing chaotic real-world inputs before the model sees them, building pipelines that handle provider unreliability without data loss, making failure modes observable and debuggable, and keeping per-document costs under control as volume scales. Get those right, and the model itself becomes a reliable component rather than an unpredictable black box.
```