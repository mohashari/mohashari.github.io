# scripts/tests/test_gist_manager.py
import pytest
from unittest.mock import patch, MagicMock
import logging
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import gist_manager

logger = logging.getLogger("test")

CONTENT_WITH_CODE = '''---
layout: post
title: "Test"
---

## Section

```go
// snippet-1
package main

func main() {}
```

Some text.

```python
// snippet-2
def hello():
    pass
```
'''

CONTENT_NO_CODE = '''---
layout: post
title: "No code"
---

Just text, no code blocks with snippet markers.
'''


def test_extract_snippets_finds_code_blocks():
    snippets = gist_manager.extract_snippets(CONTENT_WITH_CODE)
    assert len(snippets) == 2
    assert snippets[0]["lang"] == "go"
    assert snippets[0]["num"] == "1"
    assert snippets[1]["lang"] == "python"
    assert snippets[1]["num"] == "2"


def test_extract_snippets_returns_empty_when_none():
    snippets = gist_manager.extract_snippets(CONTENT_NO_CODE)
    assert snippets == []


def test_embed_tag_format():
    tag = gist_manager.embed_tag("mohashari", "abc123hash456789012345", "snippet-1.go")
    assert 'src="https://gist.github.com/mohashari/abc123hash456789012345.js?file=snippet-1.go"' in tag


def test_parse_gist_hash_from_url():
    url = "https://gist.github.com/mohashari/abc123def456789012345678901234567890abcd"
    result = gist_manager.parse_gist_hash(url)
    assert result == "abc123def456789012345678901234567890abcd"


def test_parse_gist_hash_returns_none_on_bad_url():
    result = gist_manager.parse_gist_hash("not a url")
    assert result is None


def test_replace_replaces_code_blocks_with_embeds():
    mgr = gist_manager.GistManager("mohashari", logger)
    mock_result = MagicMock(
        returncode=0,
        stdout="https://gist.github.com/mohashari/abc123def456789012345678901234567890abcd\n",
        stderr=""
    )
    with patch("subprocess.run", return_value=mock_result):
        result = mgr.process(CONTENT_WITH_CODE, "test-slug", "Test Title")
    assert "<script" in result
    assert "gist.github.com" in result


def test_process_returns_original_on_gist_failure():
    mgr = gist_manager.GistManager("mohashari", logger)
    mock_result = MagicMock(returncode=1, stdout="", stderr="auth error")
    with patch("subprocess.run", return_value=mock_result):
        result = mgr.process(CONTENT_WITH_CODE, "test-slug", "Test Title")
    # Falls back to raw code blocks
    assert "```go" in result


def test_process_returns_unchanged_when_no_snippets():
    mgr = gist_manager.GistManager("mohashari", logger)
    with patch("subprocess.run") as mock_run:
        result = mgr.process(CONTENT_NO_CODE, "no-code-slug", "No Code")
    mock_run.assert_not_called()
    assert result == CONTENT_NO_CODE
