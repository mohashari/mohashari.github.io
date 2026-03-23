---
layout: post
title: "Structured Outputs and JSON Schema Enforcement in LLM Pipelines"
date: 2026-03-23 08:00:00 +0700
tags: [llm, ai-engineering, backend, python, reliability]
description: "How to enforce JSON Schema contracts in LLM pipelines so you stop parsing free-text garbage at 3 AM."
image: ""
thumbnail: ""
---

You're running an LLM extraction pipeline in production. It worked perfectly in staging. Then at 2:47 AM your alerts fire: downstream consumers are throwing `KeyError: 'confidence_score'` and `ValueError: invalid literal for int() with base 10: 'high'`. The model returned valid prose instead of valid JSON—and your pipeline swallowed it. This isn't a hypothetical. It happens to every team that ships LLM output directly to structured consumers without enforcement. The fix isn't prompt engineering harder. It's treating the LLM like any other unreliable external service: define a contract, validate at the boundary, fail fast when it's violated.

## Why Prompt-Based JSON Is Not Enough

The naive approach is "just ask nicely":

```python
# snippet-1
# DON'T DO THIS IN PRODUCTION
response = openai_client.chat.completions.create(
    model="gpt-4o",
    messages=[
        {"role": "system", "content": "Always respond with valid JSON."},
        {"role": "user", "content": f"Extract entities from: {document}"}
    ]
)
# Now pray
data = json.loads(response.choices[0].message.content)
```

This fails in at least five ways you'll encounter in production:

1. The model prefaces JSON with `"Here is the extracted data:"` — `json.loads` throws.
2. It returns a JSON code block wrapped in triple backticks — `json.loads` throws.
3. The schema is correct but a field has the wrong type (`"count": "3"` vs `"count": 3`).
4. A required field is missing because the model decided it wasn't applicable.
5. An enum field contains a value not in your enum (`"status": "partially_complete"` vs `"pending"|"done"`).

None of these are bugs you can reliably fix with better prompting. At scale, with diverse inputs, all five will happen.

## Structured Outputs: The API-Level Contract

OpenAI's structured outputs feature (GA since August 2024) and Anthropic's tool use both let you enforce a JSON Schema at the model level. The model is constrained at token generation time — it cannot produce output that violates the schema. This is fundamentally different from asking nicely.

```python
# snippet-2
from openai import OpenAI
from pydantic import BaseModel, Field
from typing import Literal
import json

client = OpenAI()

class ExtractedEntity(BaseModel):
    name: str
    entity_type: Literal["person", "organization", "location", "product"]
    confidence: float = Field(ge=0.0, le=1.0)
    span_start: int
    span_end: int

class ExtractionResult(BaseModel):
    entities: list[ExtractedEntity]
    extraction_model_version: str
    document_language: Literal["en", "id", "ms", "other"]

def extract_entities(document: str) -> ExtractionResult:
    response = client.beta.chat.completions.parse(
        model="gpt-4o-2024-08-06",
        messages=[
            {
                "role": "system",
                "content": (
                    "Extract named entities from the document. "
                    "Use span indices relative to the original text. "
                    "Set extraction_model_version to 'v2.1'."
                ),
            },
            {"role": "user", "content": document},
        ],
        response_format=ExtractionResult,
    )
    # This raises ParseError if the model refused or returned a refusal
    return response.choices[0].message.parsed
```

The key here is `response_format=ExtractionResult` with `.parse()`. OpenAI converts your Pydantic model to JSON Schema, constrains generation, then deserializes back into the typed model. You get a `ParseError` if the model refuses (safety refusals bypass structured output). You get a fully validated Python object otherwise. No `json.loads`, no manual field access, no type coercion.

For Anthropic's API, the equivalent is tool use with `tool_choice={"type": "tool", "name": "..."}` forcing the model to always call your extraction tool:

```python
# snippet-3
import anthropic
import json
from typing import Any

client = anthropic.Anthropic()

EXTRACTION_TOOL = {
    "name": "extract_entities",
    "description": "Extract named entities from the provided document.",
    "input_schema": {
        "type": "object",
        "properties": {
            "entities": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "entity_type": {
                            "type": "string",
                            "enum": ["person", "organization", "location", "product"]
                        },
                        "confidence": {
                            "type": "number",
                            "minimum": 0.0,
                            "maximum": 1.0
                        },
                        "span_start": {"type": "integer", "minimum": 0},
                        "span_end": {"type": "integer", "minimum": 0}
                    },
                    "required": ["name", "entity_type", "confidence", "span_start", "span_end"]
                }
            },
            "document_language": {
                "type": "string",
                "enum": ["en", "id", "ms", "other"]
            }
        },
        "required": ["entities", "document_language"]
    }
}

def extract_entities_anthropic(document: str) -> dict[str, Any]:
    response = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=2048,
        tools=[EXTRACTION_TOOL],
        tool_choice={"type": "tool", "name": "extract_entities"},
        messages=[{"role": "user", "content": document}]
    )
    
    tool_use_block = next(
        b for b in response.content if b.type == "tool_use"
    )
    return tool_use_block.input  # Already parsed dict, schema-enforced
```

`tool_choice` with a specific tool name is the Anthropic equivalent of forced structured output. The model must call that tool. The input will conform to your schema.

## Defense in Depth: Validate Even When You Trust the Model

Even with API-level enforcement, you want a validation layer. Schemas drift. You'll swap models. You'll add fields to your schema and forget to bump the model version in a worker. Add `jsonschema` validation as a middleware step that's independent of which LLM produced the data:

```python
# snippet-4
import jsonschema
from jsonschema import Draft202012Validator
from functools import lru_cache
from typing import Any
import logging

logger = logging.getLogger(__name__)

@lru_cache(maxsize=None)
def _get_validator(schema_json: str) -> Draft202012Validator:
    schema = json.loads(schema_json)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)

class SchemaValidationError(Exception):
    def __init__(self, errors: list[str], raw_data: Any):
        self.errors = errors
        self.raw_data = raw_data
        super().__init__(f"Schema validation failed: {errors}")

def validate_llm_output(data: Any, schema: dict) -> None:
    """
    Raises SchemaValidationError with all violations collected,
    not just the first one. Useful for debugging compound failures.
    """
    # Cache key must be hashable — serialize schema
    schema_key = json.dumps(schema, sort_keys=True)
    validator = _get_validator(schema_key)
    
    errors = [
        f"{'.'.join(str(p) for p in e.absolute_path) or 'root'}: {e.message}"
        for e in validator.iter_errors(data)
    ]
    
    if errors:
        logger.error(
            "llm_output_validation_failed",
            extra={"error_count": len(errors), "errors": errors}
        )
        raise SchemaValidationError(errors=errors, raw_data=data)
```

The `lru_cache` on validator construction matters: `Draft202012Validator` instantiation compiles the schema and is non-trivial. In a high-throughput pipeline processing thousands of documents, recompiling the same schema on every call adds up.

## Retry Logic That Includes the Validation Error

When validation fails, your retry should tell the model *what* failed. Blind retries waste tokens and quota. Send the error back:

```python
# snippet-5
import time
from dataclasses import dataclass

@dataclass
class LLMCallConfig:
    max_retries: int = 3
    base_delay: float = 1.0
    backoff_factor: float = 2.0

def extract_with_retry(
    document: str,
    schema: dict,
    config: LLMCallConfig = LLMCallConfig(),
) -> dict[str, Any]:
    messages = [{"role": "user", "content": document}]
    last_error: Exception | None = None

    for attempt in range(config.max_retries):
        try:
            raw = call_llm(messages)  # your LLM wrapper
            validate_llm_output(raw, schema)
            return raw

        except SchemaValidationError as e:
            last_error = e
            logger.warning(
                "llm_schema_retry",
                extra={"attempt": attempt + 1, "errors": e.errors}
            )
            # Inject the error context so the model can self-correct
            messages = [
                {"role": "user", "content": document},
                {"role": "assistant", "content": json.dumps(e.raw_data)},
                {
                    "role": "user",
                    "content": (
                        "Your previous response had schema violations. "
                        f"Fix these issues and respond again:\n"
                        + "\n".join(f"- {err}" for err in e.errors)
                    ),
                },
            ]
            if attempt < config.max_retries - 1:
                time.sleep(config.base_delay * (config.backoff_factor ** attempt))

        except Exception as e:
            # Rate limits, network errors — don't include previous bad output
            last_error = e
            if attempt < config.max_retries - 1:
                time.sleep(config.base_delay * (config.backoff_factor ** attempt))

    raise RuntimeError(
        f"LLM extraction failed after {config.max_retries} attempts"
    ) from last_error
```

The multi-turn correction pattern (feeding the model its own bad output plus the specific errors) reduces retry token cost by ~40% in practice compared to restarting from scratch. The model already "knows" the document; it just needs to fix specific fields.

## Schema Versioning and Migration

Your extraction schemas will evolve. A field gets added, an enum gets extended, a type changes from `string` to `object`. Treat LLM output schemas the same way you treat database schemas: version them explicitly.

```yaml
# snippet-6
# schemas/entity_extraction/v2.yaml
# Breaking changes from v1:
#   - confidence is now float (was string "high/medium/low")
#   - Added span_start, span_end (required)
#   - document_language promoted from optional to required

$schema: "https://json-schema.org/draft/2020-12"
$id: "entity_extraction/v2"
title: EntityExtractionResult
type: object
required:
  - entities
  - document_language
  - schema_version
properties:
  schema_version:
    type: string
    const: "v2"
  document_language:
    type: string
    enum: [en, id, ms, other]
  entities:
    type: array
    items:
      type: object
      required: [name, entity_type, confidence, span_start, span_end]
      additionalProperties: false
      properties:
        name:
          type: string
          minLength: 1
        entity_type:
          type: string
          enum: [person, organization, location, product]
        confidence:
          type: number
          minimum: 0.0
          maximum: 1.0
        span_start:
          type: integer
          minimum: 0
        span_end:
          type: integer
          minimum: 0
additionalProperties: false
```

Two things that bite teams repeatedly: `additionalProperties: false` and `const` for version pinning. Without `additionalProperties: false`, a model that invents extra fields (which GPT-4 does occasionally with long schemas) will pass validation silently. With `const: "v2"` on `schema_version`, you catch version skew immediately — if a worker is still running old prompt templates that produce v1 output, it fails loudly rather than silently corrupting downstream state.

## Testing LLM Schema Compliance

Schema enforcement without tests is incomplete. You need both unit tests against your schema and integration tests that confirm the model actually produces conforming output on your real input distribution:

```python
# snippet-7
import pytest
from pathlib import Path
import yaml

SCHEMA_PATH = Path("schemas/entity_extraction/v2.yaml")

@pytest.fixture(scope="session")
def extraction_schema():
    with open(SCHEMA_PATH) as f:
        return yaml.safe_load(f)

@pytest.mark.parametrize("fixture_file", list(Path("tests/fixtures/extractions").glob("*.json")))
def test_schema_compliance(fixture_file, extraction_schema):
    """
    Validate that all saved LLM outputs in fixtures comply with the current schema.
    Run this before deploying schema changes to catch regressions.
    """
    with open(fixture_file) as f:
        data = json.load(f)
    
    validator = Draft202012Validator(extraction_schema)
    errors = list(validator.iter_errors(data))
    
    assert not errors, (
        f"Fixture {fixture_file.name} violates schema:\n"
        + "\n".join(f"  {e.json_path}: {e.message}" for e in errors)
    )

@pytest.mark.integration
@pytest.mark.parametrize("document_fixture", Path("tests/documents").glob("*.txt"))
def test_live_extraction_schema(document_fixture, extraction_schema):
    """
    Slow test: actually calls the LLM. Run in CI before releases, not on every commit.
    Saves failures to tests/fixtures/failures/ for debugging.
    """
    document = document_fixture.read_text()
    result = extract_with_retry(document, extraction_schema)
    
    validator = Draft202012Validator(extraction_schema)
    violations = list(validator.iter_errors(result))
    
    if violations:
        failure_path = Path("tests/fixtures/failures") / document_fixture.name
        failure_path.with_suffix(".json").write_text(json.dumps(result, indent=2))
    
    assert not violations, f"Live extraction violated schema on {document_fixture.name}"
```

The separation between `test_schema_compliance` (fast, no LLM calls, checks saved fixtures) and `test_live_extraction_schema` (slow, gated behind `pytest.mark.integration`) is important. Your fast unit test suite should never call an external API. The integration tests run in CI on a schedule or pre-release, not on every PR.

## Production Observability

Log schema violations as structured events, not strings. You want to query "which fields fail most often" across thousands of documents in your log aggregator:

```python
# snippet-8
import structlog
from prometheus_client import Counter, Histogram

log = structlog.get_logger()

schema_violation_counter = Counter(
    "llm_schema_violations_total",
    "Total LLM output schema violations",
    labelnames=["field_path", "violation_type", "model_version"]
)

extraction_latency = Histogram(
    "llm_extraction_duration_seconds",
    "LLM extraction end-to-end latency",
    labelnames=["outcome"],  # success, validation_error, llm_error
    buckets=[0.5, 1.0, 2.0, 5.0, 10.0, 30.0]
)

def extract_with_observability(document: str, model_version: str) -> dict:
    with extraction_latency.labels(outcome="pending").time() as timer:
        try:
            result = extract_with_retry(document, SCHEMA)
            timer._labelnames = ["outcome"]  # reset to success label
            extraction_latency.labels(outcome="success")
            return result
        
        except SchemaValidationError as e:
            extraction_latency.labels(outcome="validation_error")
            for error_msg in e.errors:
                field = error_msg.split(":")[0]
                schema_violation_counter.labels(
                    field_path=field,
                    violation_type="constraint_violation",
                    model_version=model_version
                ).inc()
            log.error(
                "extraction_schema_failure",
                field_errors=e.errors,
                model_version=model_version,
                document_length=len(document)
            )
            raise
```

With this in place, you can answer "what percentage of our `confidence` field violations come from documents longer than 10k tokens?" — which is exactly the kind of question that tells you whether you need to chunk your input or adjust your schema.

## The Decision Matrix

When choosing your enforcement strategy:

| Scenario | Approach |
|---|---|
| OpenAI, well-defined schema, Python | `client.beta.chat.completions.parse()` with Pydantic |
| Anthropic, structured extraction | Tool use with `tool_choice` forced |
| Any model, schema you don't control | `jsonschema` validation + retry with error feedback |
| Schema with nested objects > 3 levels | Flatten it — deep nesting degrades model compliance |
| Streaming responses | Buffer full response before validating; don't validate partials |

The last point is often missed: streaming and structured output validation are incompatible. If you need structured output, disable streaming. If you need streaming UX, structure your schema so the most latency-sensitive fields (like a `summary` string) come first and you validate after the stream closes.

Structured output enforcement isn't optional at production scale — it's the difference between a pipeline that degrades gracefully and one that pages you at 3 AM because a model decided to add a helpful explanation before its JSON.