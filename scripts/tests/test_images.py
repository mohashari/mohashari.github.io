# scripts/tests/test_images.py
import logging
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import images

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


def test_inject_urls_replaces_empty_image():
    result = images.inject_urls(
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
    result = images.inject_urls(
        already_injected,
        "https://other.com/regular.jpg",
        "https://other.com/small.jpg",
    )
    assert 'image: "https://example.com/regular.jpg"' in result


def test_fetch_photo_url_format():
    client = images.PicsumClient(logger)
    regular, small = client.fetch_photo("Kubernetes Networking Deep Dive")
    assert regular.startswith("https://picsum.photos/1080/720?random=")
    assert small.startswith("https://picsum.photos/400/300?random=")


def test_fetch_photo_returns_deterministic_urls():
    client = images.PicsumClient(logger)
    title = "PostgreSQL WAL Internals"
    r1, s1 = client.fetch_photo(title)
    r2, s2 = client.fetch_photo(title)
    assert r1 == r2
    assert s1 == s2


def test_fetch_photo_different_titles_may_differ():
    client = images.PicsumClient(logger)
    r1, _ = client.fetch_photo("Kubernetes Networking")
    r2, _ = client.fetch_photo("PostgreSQL WAL Internals")
    # Different titles should produce different seeds (highly likely)
    assert r1 != r2


def test_enrich_post_injects_picsum_urls():
    client = images.PicsumClient(logger)
    result = client.enrich_post(POST_WITH_PLACEHOLDERS, "PostgreSQL WAL Internals")
    assert "picsum.photos" in result
    assert 'image: ""' not in result
    assert 'thumbnail: ""' not in result
