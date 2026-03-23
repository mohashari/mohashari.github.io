# scripts/tests/test_topic_generator.py
import json
import pytest
from unittest.mock import patch, MagicMock
import logging
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import topic_generator

logger = logging.getLogger("test")


VALID_TOPICS = [
    {"title": "PostgreSQL WAL Internals", "slug": "postgresql-wal-internals",
     "category": "software_engineering", "needs_code": True, "needs_diagram": True},
    {"title": "Go Error Handling Patterns", "slug": "go-error-patterns",
     "category": "development", "needs_code": True, "needs_diagram": False},
    {"title": "SBOM Supply Chain Security", "slug": "sbom-supply-chain",
     "category": "devsecops", "needs_code": True, "needs_diagram": True},
    {"title": "RAG Chunking Strategies", "slug": "rag-chunking",
     "category": "ai_engineering", "needs_code": True, "needs_diagram": False},
    {"title": "Consistent Hashing", "slug": "consistent-hashing",
     "category": "software_engineering", "needs_code": True, "needs_diagram": True},
]


def test_parse_valid_json():
    raw = json.dumps(VALID_TOPICS)
    result = topic_generator.parse_topics(raw)
    assert len(result) == 5
    assert result[0]["slug"] == "postgresql-wal-internals"


def test_parse_strips_markdown_fences():
    raw = f"```json\n{json.dumps(VALID_TOPICS)}\n```"
    result = topic_generator.parse_topics(raw)
    assert len(result) == 5


def test_parse_invalid_json_returns_none():
    result = topic_generator.parse_topics("not json at all")
    assert result is None


def test_parse_missing_required_fields_returns_none():
    bad = [{"title": "Missing slug"}]
    result = topic_generator.parse_topics(json.dumps(bad))
    assert result is None


def test_build_prompt_uses_config_values():
    prompt = topic_generator.build_prompt(["slug-a", "slug-b"])
    assert "5" in prompt
    assert "software_engineering" in prompt
    assert "slug-a" in prompt
    assert "slug-b" in prompt


def test_generate_calls_claude_once_on_success():
    gen = topic_generator.TopicGenerator(logger)
    mock_result = MagicMock(returncode=0, stdout=json.dumps(VALID_TOPICS), stderr="")
    with patch("subprocess.run", return_value=mock_result) as mock_run:
        topics = gen.generate(past_slugs=[])
    assert len(topics) == 5
    assert mock_run.call_count == 1


def test_generate_retries_on_bad_json():
    gen = topic_generator.TopicGenerator(logger)
    bad_result = MagicMock(returncode=0, stdout="not json", stderr="")
    good_result = MagicMock(returncode=0, stdout=json.dumps(VALID_TOPICS), stderr="")
    with patch("subprocess.run", side_effect=[bad_result, good_result]):
        topics = gen.generate(past_slugs=[])
    assert len(topics) == 5


def test_generate_raises_after_two_failures():
    gen = topic_generator.TopicGenerator(logger)
    bad_result = MagicMock(returncode=0, stdout="bad json", stderr="")
    with patch("subprocess.run", return_value=bad_result):
        with pytest.raises(topic_generator.TopicGenerationError):
            gen.generate(past_slugs=[])
