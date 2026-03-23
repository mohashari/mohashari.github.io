# scripts/tests/test_post_generator.py
import pytest
from unittest.mock import patch, MagicMock
import logging
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import post_generator

logger = logging.getLogger("test")

SAMPLE_TOPIC = {
    "title": "PostgreSQL WAL Internals",
    "slug": "postgresql-wal-internals",
    "category": "software_engineering",
    "needs_code": True,
    "needs_diagram": True,
}

VALID_POST = """---
layout: post
title: "PostgreSQL WAL Internals"
date: 2026-03-23 08:00:00 +0700
tags: [postgresql, database, storage]
description: "A deep dive into WAL."
image: ""
thumbnail: ""
---

## Introduction

This is the post body.
"""


def test_build_prompt_includes_title():
    prompt = post_generator.build_prompt(SAMPLE_TOPIC, "2026-03-23")
    assert "PostgreSQL WAL Internals" in prompt


def test_build_prompt_includes_code_instructions_when_needed():
    prompt = post_generator.build_prompt(SAMPLE_TOPIC, "2026-03-23")
    assert "snippet-" in prompt


def test_build_prompt_includes_diagram_instructions_when_needed():
    prompt = post_generator.build_prompt(SAMPLE_TOPIC, "2026-03-23")
    assert "Excalidraw" in prompt


def test_build_prompt_no_code_when_not_needed():
    topic = {**SAMPLE_TOPIC, "needs_code": False}
    prompt = post_generator.build_prompt(topic, "2026-03-23")
    assert "snippet-" not in prompt


def test_build_prompt_no_diagram_when_not_needed():
    topic = {**SAMPLE_TOPIC, "needs_diagram": False}
    prompt = post_generator.build_prompt(topic, "2026-03-23")
    assert "Excalidraw" not in prompt


def test_build_prompt_includes_image_thumbnail_placeholders():
    prompt = post_generator.build_prompt(SAMPLE_TOPIC, "2026-03-23")
    assert 'image: ""' in prompt
    assert 'thumbnail: ""' in prompt


def test_strip_preamble_removes_text_before_frontmatter():
    raw = 'Here is your post:\n\n---\nlayout: post\n---\n\nbody'
    result = post_generator.strip_preamble(raw)
    assert result.startswith("---")


def test_strip_preamble_raises_when_no_frontmatter():
    with pytest.raises(post_generator.PostGenerationError):
        post_generator.strip_preamble("no frontmatter here")


def test_generate_returns_post_content():
    gen = post_generator.PostGenerator(logger)
    mock_result = MagicMock(returncode=0, stdout=VALID_POST, stderr="")
    with patch("subprocess.run", return_value=mock_result):
        content = gen.generate(SAMPLE_TOPIC, "2026-03-23")
    assert content.startswith("---")
    assert "PostgreSQL WAL Internals" in content


def test_generate_raises_on_claude_failure():
    gen = post_generator.PostGenerator(logger)
    mock_result = MagicMock(returncode=1, stdout="", stderr="error")
    with patch("subprocess.run", return_value=mock_result):
        with pytest.raises(post_generator.PostGenerationError):
            gen.generate(SAMPLE_TOPIC, "2026-03-23")
