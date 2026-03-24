---
layout: post
title: "LLM Structured Extraction at Scale: Batching, Retries, and Schema Versioning"
date: 2026-03-24 08:00:00 +0700
tags: [ai-engineering, llm, python, backend, production]
description: "How to build reliable LLM-powered extraction pipelines that handle batching, schema drift, and failure modes at production scale."
image: ""
thumbnail: ""
---

You ship an LLM-powered extraction pipeline on a Monday. It works beautifully in testing — you feed it messy invoice PDFs and out comes clean, structured JSON. By Thursday it's silently returning `null` for a field that legal depends on, the model started hallucinating a new key you never defined, and your retry logic is hammering the API with identical failed requests in a tight loop. This isn't a hypothetical. It's the standard arc of putting LLM extraction into production without treating it with the same rigor you'd give a database migration or a queue consumer.

Structured extraction — using an LLM to pull typed, schema-conforming data from unstructured text — is one of the highest-value applications of LLMs in backend systems. But it has a set of failure modes that don't exist in traditional ETL pipelines: the output format is probabilistic, the schema evolves alongside your product, and the upstream API has rate limits that interact badly with retry loops. This post covers the engineering patterns that actually work at scale.

## Why Naive Implementations Break

The simplest version of LLM extraction looks like this: send the text, parse the JSON from the response, insert it. This breaks in four specific ways at scale:

**Malformed output**: Models don't always produce valid JSON, especially when the document is ambiguous or long. A missing closing brace crashes your parser.

**Schema mismatch**: The model returns a field named `invoice_total` but your schema expects `total_amount`. Or it returns a string `"1,234.56"` where you need a float.

**Rate limit cascades**: You fire 500 concurrent requests, get throttled, and your retry logic — if you have any — makes things worse by retrying immediately.

**Schema drift**: Three months after launch, a product manager adds a required field. Every extraction run before that date now has a structural incompatibility with your query layer.

None of these are solvable with "just add error handling." Each requires a deliberate pattern.

## Batching With Concurrency Control

The first rule of batching LLM calls: never trust that your concurrency is bounded by your code. Rate limits are enforced at the API level across all your processes, so you need explicit control.

Use a semaphore to cap in-flight requests. Pair it with a token bucket or leaky bucket if you're hitting token-per-minute limits (which are often tighter than request-per-minute limits for extraction workloads).

```python
# snippet-1
import asyncio
import time
from dataclasses import dataclass, field
from typing import Any
import anthropic

@dataclass
class RateLimiter:
    rps: float  # requests per second
    _tokens: float = field(init=False)
    _last_refill: float = field(init=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)

    def __post_init__(self):
        self._tokens = self.rps
        self._last_refill = time.monotonic()

    async def acquire(self):
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_refill
            self._tokens = min(self.rps, self._tokens + elapsed * self.rps)
            self._last_refill = now
            if self._tokens < 1:
                wait = (1 - self._tokens) / self.rps
                await asyncio.sleep(wait)
                self._tokens = 0
            else:
                self._tokens -= 1


async def extract_batch(
    documents: list[str],
    concurrency: int = 20,
    rps: float = 10.0,
) -> list[dict[str, Any] | None]:
    client = anthropic.AsyncAnthropic()
    semaphore = asyncio.Semaphore(concurrency)
    limiter = RateLimiter(rps=rps)
    results: list[dict[str, Any] | None] = [None] * len(documents)

    async def process(idx: int, doc: str):
        async with semaphore:
            await limiter.acquire()
            try:
                result = await extract_single(client, doc)
                results[idx] = result
            except Exception as e:
                results[idx] = {"_error": str(e), "_doc_idx": idx}

    await asyncio.gather(*[process(i, doc) for i, doc in enumerate(documents)])
    return results
```

Keep concurrency at 20–50 for most workloads. Higher than that and you'll spend more time managing rate limit backoffs than doing useful work. The `_error` sentinel in the result list is deliberate — you want partial success, not an all-or-nothing batch.

## Retry Logic That Doesn't Make Things Worse

The canonical mistake: catch an exception, sleep 1 second, retry. This is wrong for three reasons. First, it doesn't distinguish between retryable errors (rate limit, timeout) and non-retryable ones (invalid input, auth failure). Second, a fixed sleep doesn't handle thundering herd when all your workers hit the limit simultaneously. Third, it doesn't bound total retry attempts.

Use exponential backoff with jitter, and classify errors before retrying:

```python
# snippet-2
import asyncio
import random
import logging
from anthropic import RateLimitError, APITimeoutError, APIStatusError
import instructor
from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)

RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 529}

async def extract_with_retry(
    client: instructor.AsyncInstructor,
    text: str,
    schema: type[BaseModel],
    max_attempts: int = 4,
    base_delay: float = 1.0,
) -> BaseModel | None:
    last_exc = None

    for attempt in range(max_attempts):
        try:
            result = await client.chat.completions.create(
                model="claude-3-5-sonnet-20241022",
                max_tokens=1024,
                response_model=schema,
                messages=[{"role": "user", "content": text}],
            )
            return result

        except ValidationError as e:
            # Non-retryable: model returned structurally invalid output
            logger.warning("Validation failed on attempt %d: %s", attempt + 1, e)
            last_exc = e
            # Still retry — model output is probabilistic, next attempt may succeed
            # but cap at 2 retries for validation errors to avoid burning tokens
            if attempt >= 1:
                break

        except RateLimitError as e:
            last_exc = e
            retry_after = float(e.response.headers.get("retry-after", base_delay * (2 ** attempt)))
            jitter = random.uniform(0, retry_after * 0.1)
            await asyncio.sleep(retry_after + jitter)

        except APITimeoutError as e:
            last_exc = e
            delay = base_delay * (2 ** attempt) + random.uniform(0, 1)
            await asyncio.sleep(delay)

        except APIStatusError as e:
            if e.status_code not in RETRYABLE_STATUS_CODES:
                raise  # auth errors, bad request — don't retry
            last_exc = e
            delay = base_delay * (2 ** attempt) + random.uniform(0, 1)
            await asyncio.sleep(delay)

    logger.error("Extraction failed after %d attempts: %s", max_attempts, last_exc)
    return None
```

The `retry-after` header on a 429 is gospel. Anthropic and OpenAI both set it accurately — use it instead of computing your own backoff. If it's absent, fall back to exponential.

## Schema Definition With Versioning Built In

Most teams treat the extraction schema as an afterthought — a Pydantic model living in a utils file, modified in-place as requirements change. This breaks downstream consumers silently. When you add a required field, every row stored before that change is now structurally incomplete. When you rename a field, queries that worked yesterday return empty results.

The fix is to version your schemas explicitly and store the version alongside every extracted record:

```python
# snippet-3
from pydantic import BaseModel, Field, field_validator
from typing import Literal
from datetime import date
from decimal import Decimal
import re


class InvoiceExtractionV2(BaseModel):
    """
    Schema version 2: added line_items, changed total to Decimal.
    Breaking change from V1: removed 'amount_str', added 'total_amount' as Decimal.
    """
    schema_version: Literal["invoice.v2"] = "invoice.v2"

    vendor_name: str = Field(..., description="Legal name of the vendor or supplier")
    invoice_number: str = Field(..., description="Invoice ID as printed on the document")
    invoice_date: date | None = Field(None, description="Date of issue in ISO 8601 format")
    due_date: date | None = Field(None, description="Payment due date")
    total_amount: Decimal = Field(..., description="Total invoice amount, numeric only, no currency symbol")
    currency: str = Field("USD", description="ISO 4217 currency code, e.g. USD, EUR, IDR")
    line_items: list["LineItem"] = Field(default_factory=list)

    @field_validator("invoice_number")
    @classmethod
    def normalize_invoice_number(cls, v: str) -> str:
        return re.sub(r"\s+", "", v).upper()

    @field_validator("currency")
    @classmethod
    def validate_currency(cls, v: str) -> str:
        return v.upper()


class LineItem(BaseModel):
    description: str
    quantity: Decimal | None = None
    unit_price: Decimal | None = None
    subtotal: Decimal | None = None


InvoiceExtractionV2.model_rebuild()
```

The `schema_version` field as a `Literal` type is load-bearing. It forces the model to emit a fixed string you can filter on, and it makes your storage layer queryable: `WHERE extraction->>'schema_version' = 'invoice.v2'`.

## Storing Extractions With Schema-Aware Persistence

Once you have versioned schemas, your persistence layer needs to handle them. Store raw model output alongside the parsed result so you can re-parse old records when schemas evolve:

```sql
-- snippet-4
CREATE TABLE document_extractions (
    id              BIGSERIAL PRIMARY KEY,
    document_id     UUID NOT NULL REFERENCES documents(id),
    schema_version  TEXT NOT NULL,
    extracted_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    model_id        TEXT NOT NULL,
    raw_output      TEXT NOT NULL,           -- the raw LLM response before parsing
    parsed_data     JSONB,                   -- null if parsing failed
    extraction_ok   BOOLEAN GENERATED ALWAYS AS (parsed_data IS NOT NULL) STORED,
    tokens_used     INTEGER,
    latency_ms      INTEGER
);

CREATE INDEX idx_extractions_document_schema
    ON document_extractions (document_id, schema_version, extracted_at DESC);

CREATE INDEX idx_extractions_failed
    ON document_extractions (document_id)
    WHERE extraction_ok = FALSE;

-- View for latest successful extraction per document per schema version
CREATE VIEW latest_extractions AS
SELECT DISTINCT ON (document_id, schema_version)
    *
FROM document_extractions
WHERE extraction_ok = TRUE
ORDER BY document_id, schema_version, extracted_at DESC;
```

The `raw_output` column is what saves you during a schema migration. When you cut `invoice.v3`, you can run a backfill job that re-parses existing `raw_output` values against the new schema without hitting the LLM API again — which is both cheaper and deterministic.

## Validation Pipeline and Fallback Strategies

Schema validation with Pydantic catches type errors, but it doesn't catch semantic errors: a model might confidently extract `total_amount: 0.00` from an invoice that clearly shows `$12,450.00`. You need a second validation pass for business logic:

```python
# snippet-5
from pydantic import BaseModel
from decimal import Decimal
import logging

logger = logging.getLogger(__name__)


class ExtractionQualityError(Exception):
    def __init__(self, field: str, reason: str):
        self.field = field
        self.reason = reason
        super().__init__(f"{field}: {reason}")


def validate_invoice_extraction(
    extraction: InvoiceExtractionV2,
    source_text: str,
) -> list[ExtractionQualityError]:
    """
    Business-logic validation layer. Returns list of quality warnings.
    Does not raise — callers decide whether to reject or accept with warnings.
    """
    warnings: list[ExtractionQualityError] = []

    if extraction.total_amount <= Decimal("0"):
        warnings.append(ExtractionQualityError(
            "total_amount",
            f"Extracted zero/negative amount: {extraction.total_amount}"
        ))

    if extraction.total_amount > Decimal("10_000_000"):
        warnings.append(ExtractionQualityError(
            "total_amount",
            f"Suspiciously large amount: {extraction.total_amount} — verify manually"
        ))

    if extraction.invoice_date and extraction.due_date:
        if extraction.due_date < extraction.invoice_date:
            warnings.append(ExtractionQualityError(
                "due_date",
                f"Due date {extraction.due_date} precedes invoice date {extraction.invoice_date}"
            ))

    # Sanity check: invoice number should appear verbatim in source
    if extraction.invoice_number not in source_text.upper().replace(" ", ""):
        warnings.append(ExtractionQualityError(
            "invoice_number",
            f"Extracted invoice number '{extraction.invoice_number}' not found verbatim in source"
        ))

    return warnings


async def extract_and_validate(
    client: instructor.AsyncInstructor,
    document_id: str,
    text: str,
    db,
) -> dict:
    extraction = await extract_with_retry(client, text, InvoiceExtractionV2)

    if extraction is None:
        await db.execute(
            "INSERT INTO document_extractions (document_id, schema_version, model_id, raw_output) "
            "VALUES ($1, $2, $3, $4)",
            document_id, "invoice.v2", "claude-3-5-sonnet-20241022", "FAILED"
        )
        return {"ok": False, "reason": "extraction_failed"}

    warnings = validate_invoice_extraction(extraction, text)
    has_critical_warning = any(w.field == "total_amount" for w in warnings)

    await db.execute(
        "INSERT INTO document_extractions "
        "(document_id, schema_version, model_id, raw_output, parsed_data) "
        "VALUES ($1, $2, $3, $4, $5)",
        document_id,
        extraction.schema_version,
        "claude-3-5-sonnet-20241022",
        extraction.model_dump_json(),
        extraction.model_dump() if not has_critical_warning else None,
    )

    if warnings:
        logger.warning(
            "Extraction warnings for doc %s: %s",
            document_id,
            [f"{w.field}: {w.reason}" for w in warnings]
        )

    return {"ok": not has_critical_warning, "warnings": [w.reason for w in warnings]}
```

## Observability: What to Measure

Extraction quality degrades silently. You won't notice until someone files a bug report about wrong numbers in a report. Instrument these metrics from day one:

```python
# snippet-6
from dataclasses import dataclass
from collections import defaultdict
from typing import Callable
import time


@dataclass
class ExtractionMetrics:
    total: int = 0
    succeeded: int = 0
    failed_parse: int = 0
    failed_validation: int = 0
    retried: int = 0
    total_tokens: int = 0
    total_latency_ms: float = 0.0
    field_null_counts: dict = None

    def __post_init__(self):
        self.field_null_counts = defaultdict(int)

    @property
    def success_rate(self) -> float:
        return self.succeeded / self.total if self.total else 0.0

    @property
    def avg_latency_ms(self) -> float:
        return self.total_latency_ms / self.succeeded if self.succeeded else 0.0

    def record_result(self, extraction, latency_ms: float, tokens: int, retried: bool):
        self.total += 1
        self.total_latency_ms += latency_ms
        self.total_tokens += tokens

        if retried:
            self.retried += 1

        if extraction is None:
            self.failed_parse += 1
            return

        self.succeeded += 1
        # Track per-field null rates to detect schema drift early
        for field_name, value in extraction.model_dump().items():
            if value is None:
                self.field_null_counts[field_name] += 1

    def log_summary(self, logger):
        logger.info(
            "Extraction summary | total=%d success_rate=%.1f%% avg_latency=%.0fms "
            "total_tokens=%d retried=%d",
            self.total,
            self.success_rate * 100,
            self.avg_latency_ms,
            self.total_tokens,
            self.retried,
        )
        for field, count in sorted(self.field_null_counts.items(), key=lambda x: -x[1]):
            null_rate = count / self.succeeded if self.succeeded else 0
            if null_rate > 0.05:  # alert if >5% null for any field
                logger.warning("High null rate for field '%s': %.1f%%", field, null_rate * 100)
```

The per-field null rate alert is the most underrated metric here. When `invoice_date` suddenly goes from 2% null to 40% null, something changed — either the document format shifted, the model was updated, or your prompt broke. You want to know before your downstream consumers do.

## Schema Migration in Practice

When you need to cut a new schema version, the workflow is:

1. Define the new Pydantic model with `schema_version: Literal["invoice.v3"]`
2. Write a migration function that converts V2 `parsed_data` JSONB to V3 shape where possible
3. Run extraction for new documents against V3
4. Backfill existing documents by re-parsing `raw_output` against V3 (free, no API cost)
5. For records where backfill fails (truly V2-incompatible), queue for re-extraction

The backfill pattern only works if you stored `raw_output`. This is non-negotiable. Storage is cheap. Re-running 50,000 documents through an LLM API is not.

Keep old schema versions in your codebase for at least one migration cycle. Deleting `InvoiceExtractionV2` before all consumers have migrated to V3 will break your backfill tooling.

## The Operational Checklist

Before putting an extraction pipeline in production, verify:

- Concurrency is bounded by a semaphore, not just hope
- Retry logic respects `retry-after` headers and classifies non-retryable errors
- Every extraction stores `raw_output` alongside parsed data
- Schema version is persisted in the database, not just in code
- Field-level null rates are monitored and alerted on
- You have a backfill script ready for schema migrations
- Validation errors are logged with the document ID so you can reproduce them

LLM extraction is not magic. It's a probabilistic function with a contract — the schema — that you're responsible for enforcing. Build the enforcement layer with the same rigor you'd apply to any other critical data pipeline, and it becomes genuinely reliable infrastructure.