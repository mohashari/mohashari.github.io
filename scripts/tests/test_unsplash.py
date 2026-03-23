# scripts/tests/test_unsplash.py
import pytest
from unittest.mock import patch, MagicMock
import logging
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import unsplash

logger = logging.getLogger("test")

POST_WITH_PLACEHOLDERS = '''---
layout: post
title: "Test Post"
date: 2026-03-23 08:00:00 +0700
tags: [test]
description: "A test."
image: ""
thumbnail: ""
---

Body content.
'''

MOCK_API_RESPONSE = {
    "urls": {
        "regular": "https://images.unsplash.com/photo-123?w=1080",
        "small": "https://images.unsplash.com/photo-123?w=400",
    }
}


def test_extract_keywords_from_title():
    keywords = unsplash.extract_keywords("PostgreSQL WAL Internals Deep Dive")
    assert len(keywords) >= 2
    assert any(k in keywords for k in ["postgresql", "wal", "internals"])


def test_inject_urls_replaces_empty_image():
    result = unsplash.inject_urls(
        POST_WITH_PLACEHOLDERS,
        "https://example.com/regular.jpg",
        "https://example.com/small.jpg",
    )
    assert 'image: "https://example.com/regular.jpg"' in result
    assert 'thumbnail: "https://example.com/small.jpg"' in result


def test_inject_urls_does_not_double_inject():
    already_injected = POST_WITH_PLACEHOLDERS.replace(
        'image: ""', 'image: "https://example.com/regular.jpg"'
    ).replace(
        'thumbnail: ""', 'thumbnail: "https://example.com/small.jpg"'
    )
    result = unsplash.inject_urls(
        already_injected,
        "https://other.com/regular.jpg",
        "https://other.com/small.jpg",
    )
    # Should not replace already-filled fields
    assert 'image: "https://example.com/regular.jpg"' in result


def test_fetch_photo_returns_urls(monkeypatch):
    monkeypatch.setenv("UNSPLASH_ACCESS_KEY", "test-key")
    client = unsplash.UnsplashClient(logger)
    mock_resp = MagicMock(status_code=200)
    mock_resp.json.return_value = MOCK_API_RESPONSE
    with patch("requests.get", return_value=mock_resp):
        regular, small = client.fetch_photo("postgresql database")
    assert regular == "https://images.unsplash.com/photo-123?w=1080"
    assert small == "https://images.unsplash.com/photo-123?w=400"


def test_fetch_photo_returns_none_when_no_key(monkeypatch):
    monkeypatch.delenv("UNSPLASH_ACCESS_KEY", raising=False)
    client = unsplash.UnsplashClient(logger)
    result = client.fetch_photo("postgresql")
    assert result is None


def test_fetch_photo_returns_none_on_api_error(monkeypatch):
    monkeypatch.setenv("UNSPLASH_ACCESS_KEY", "test-key")
    client = unsplash.UnsplashClient(logger)
    mock_resp = MagicMock(status_code=403)
    mock_resp.raise_for_status.side_effect = Exception("403 Forbidden")
    with patch("requests.get", return_value=mock_resp):
        result = client.fetch_photo("postgresql")
    assert result is None


def test_enrich_post_injects_when_api_succeeds(monkeypatch):
    monkeypatch.setenv("UNSPLASH_ACCESS_KEY", "test-key")
    client = unsplash.UnsplashClient(logger)
    mock_resp = MagicMock(status_code=200)
    mock_resp.json.return_value = MOCK_API_RESPONSE
    with patch("requests.get", return_value=mock_resp):
        result = client.enrich_post(POST_WITH_PLACEHOLDERS, "PostgreSQL WAL Internals")
    assert "images.unsplash.com" in result


def test_enrich_post_returns_unchanged_on_failure(monkeypatch):
    monkeypatch.delenv("UNSPLASH_ACCESS_KEY", raising=False)
    client = unsplash.UnsplashClient(logger)
    result = client.enrich_post(POST_WITH_PLACEHOLDERS, "PostgreSQL WAL Internals")
    assert result == POST_WITH_PLACEHOLDERS
