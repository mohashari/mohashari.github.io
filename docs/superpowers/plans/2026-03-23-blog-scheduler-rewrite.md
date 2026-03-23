# Blog Scheduler Rewrite Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rewrite the blog post automation pipeline as a clean multi-module Python package that generates 5 daily posts at 8 AM using dynamic Claude-generated topics, Unsplash thumbnails, GitHub Gist code embedding, and Excalidraw diagrams.

**Architecture:** 8 focused modules under `scripts/` — `config`, `history`, `topic_generator`, `post_generator`, `gist_manager`, `unsplash`, `publisher`, `orchestrator` — each with one clear responsibility. `orchestrator.py` is the entry point called by `run_blog.sh` via cron.

**Tech Stack:** Python 3.13, Claude CLI (`claude -p`), GitHub CLI (`gh gist create`), Unsplash REST API (`requests`), pytest for unit tests.

---

## File Map

| File | Action | Purpose |
|---|---|---|
| `scripts/config.py` | Create | All constants and paths |
| `scripts/history.py` | Create | Load/save/update topics_history.json |
| `scripts/topic_generator.py` | Create | Claude-generated topics with deduplication |
| `scripts/post_generator.py` | Create | Claude-generated Jekyll post content |
| `scripts/gist_manager.py` | Create | Extract code blocks, upload to GitHub Gist |
| `scripts/unsplash.py` | Create | Fetch Unsplash photo, inject into frontmatter |
| `scripts/publisher.py` | Create | git add/commit/push |
| `scripts/orchestrator.py` | Create | Pre-flight checks, pipeline runner, logging setup |
| `scripts/run_blog.sh` | Modify | Call `orchestrator.py` instead of `generate_posts.py` |
| `scripts/tests/test_history.py` | Create | Unit tests for history module |
| `scripts/tests/test_topic_generator.py` | Create | Unit tests for topic generator |
| `scripts/tests/test_post_generator.py` | Create | Unit tests for post generator |
| `scripts/tests/test_gist_manager.py` | Create | Unit tests for gist manager |
| `scripts/tests/test_unsplash.py` | Create | Unit tests for unsplash module |
| `scripts/tests/test_publisher.py` | Create | Unit tests for publisher |
| `scripts/generate_posts.py` | Delete | Replaced by orchestrator.py |
| `scripts/topic_pool.py` | Delete | Replaced by topic_generator.py |

---

## Task 1: Install pytest and create test directory

**Files:**
- Create: `scripts/tests/__init__.py`

- [ ] **Step 1: Install pytest**

```bash
pip3 install pytest requests --user
```

Expected: Successfully installed pytest and requests.

- [ ] **Step 2: Create tests directory**

```bash
mkdir -p /home/muklis/Documents/exploring/blog/scripts/tests
touch /home/muklis/Documents/exploring/blog/scripts/tests/__init__.py
```

- [ ] **Step 3: Verify pytest works**

```bash
cd /home/muklis/Documents/exploring/blog/scripts
python3 -m pytest tests/ -v
```

Expected: `no tests ran` (0 items, no errors).

- [ ] **Step 4: Commit**

```bash
git add scripts/tests/__init__.py
git commit -m "test: scaffold test directory for scripts package"
```

---

## Task 2: Create `config.py`

**Files:**
- Create: `scripts/config.py`

No tests needed — it's constants only.

- [ ] **Step 1: Create config.py**

```python
# scripts/config.py
from pathlib import Path

BLOG_DIR = Path("/home/muklis/Documents/exploring/blog")
POSTS_DIR = BLOG_DIR / "_posts"
IMAGES_DIR = BLOG_DIR / "images" / "diagrams"
SCRIPTS_DIR = BLOG_DIR / "scripts"
LOG_DIR = SCRIPTS_DIR / "logs"
HISTORY_PATH = SCRIPTS_DIR / "topics_history.json"

GITHUB_USER = "mohashari"
POSTS_PER_RUN = 5
CATEGORIES = ["software_engineering", "development", "devsecops", "ai_engineering"]

TIMEOUT_WITH_DIAGRAM = 600   # seconds
TIMEOUT_TEXT_ONLY = 400      # seconds

UNSPLASH_API_BASE = "https://api.unsplash.com"

ALLOWED_TOOLS = (
    "Bash,"
    "mcp__claude_ai_Excalidraw__export_to_excalidraw,"
    "mcp__claude_ai_Excalidraw__create_view,"
    "mcp__claude_ai_Excalidraw__save_checkpoint"
)
```

- [ ] **Step 2: Verify import works**

```bash
cd /home/muklis/Documents/exploring/blog/scripts
python3 -c "import config; print(config.POSTS_PER_RUN, config.CATEGORIES)"
```

Expected: `5 ['software_engineering', 'development', 'devsecops', 'ai_engineering']`

- [ ] **Step 3: Commit**

```bash
git add scripts/config.py
git commit -m "feat: add config module with all constants"
```

---

## Task 3: Create `history.py` with TDD

**Files:**
- Create: `scripts/history.py`
- Create: `scripts/tests/test_history.py`

- [ ] **Step 1: Write failing tests**

```python
# scripts/tests/test_history.py
import json
import pytest
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

import history


def test_load_missing_file(tmp_path):
    result = history.load(tmp_path / "missing.json")
    assert result == {"used": [], "last_updated": None}


def test_load_existing_file(tmp_path):
    p = tmp_path / "h.json"
    p.write_text(json.dumps({"used": ["slug-a"], "last_updated": "2026-01-01"}))
    result = history.load(p)
    assert result["used"] == ["slug-a"]
    assert result["last_updated"] == "2026-01-01"


def test_append_and_save_new_file(tmp_path):
    p = tmp_path / "h.json"
    history.append_and_save(p, "slug-one")
    data = json.loads(p.read_text())
    assert "slug-one" in data["used"]
    assert data["last_updated"] is not None


def test_append_and_save_no_duplicates(tmp_path):
    p = tmp_path / "h.json"
    history.append_and_save(p, "slug-one")
    history.append_and_save(p, "slug-one")
    data = json.loads(p.read_text())
    assert data["used"].count("slug-one") == 1


def test_append_and_save_preserves_existing(tmp_path):
    p = tmp_path / "h.json"
    history.append_and_save(p, "slug-one")
    history.append_and_save(p, "slug-two")
    data = json.loads(p.read_text())
    assert "slug-one" in data["used"]
    assert "slug-two" in data["used"]


def test_last_n_slugs_returns_recent(tmp_path):
    p = tmp_path / "h.json"
    for i in range(60):
        history.append_and_save(p, f"slug-{i}")
    h = history.load(p)
    recent = history.last_n_slugs(h, n=50)
    assert len(recent) == 50


def test_last_n_slugs_empty(tmp_path):
    h = history.load(tmp_path / "missing.json")
    assert history.last_n_slugs(h) == []
```

- [ ] **Step 2: Run tests — verify they fail**

```bash
cd /home/muklis/Documents/exploring/blog/scripts
python3 -m pytest tests/test_history.py -v
```

Expected: `ModuleNotFoundError: No module named 'history'`

- [ ] **Step 3: Write `history.py`**

```python
# scripts/history.py
import datetime
import json
from pathlib import Path


def load(path: Path) -> dict:
    """Return history dict with 'used' list and 'last_updated'."""
    if not path.exists():
        return {"used": [], "last_updated": None}
    with open(path) as f:
        return json.load(f)


def append_and_save(path: Path, slug: str) -> None:
    """Load current history, append slug if new, save immediately. Crash-safe per-post update."""
    data = load(path)
    used = list(data.get("used", []))
    if slug not in used:
        used.append(slug)
    data["used"] = used
    data["last_updated"] = datetime.date.today().isoformat()
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def last_n_slugs(history: dict, n: int = 50) -> list:
    """Return the most recent n slugs for deduplication prompt context."""
    used = history.get("used", [])
    return used[-n:]
```

- [ ] **Step 4: Run tests — verify they pass**

```bash
cd /home/muklis/Documents/exploring/blog/scripts
python3 -m pytest tests/test_history.py -v
```

Expected: `7 passed`

- [ ] **Step 5: Commit**

```bash
git add scripts/history.py scripts/tests/test_history.py
git commit -m "feat: add history module with per-post crash-safe save"
```

---

## Task 4: Create `topic_generator.py` with TDD

**Files:**
- Create: `scripts/topic_generator.py`
- Create: `scripts/tests/test_topic_generator.py`

- [ ] **Step 1: Write failing tests**

```python
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
```

- [ ] **Step 2: Run tests — verify they fail**

```bash
cd /home/muklis/Documents/exploring/blog/scripts
python3 -m pytest tests/test_topic_generator.py -v
```

Expected: `ModuleNotFoundError: No module named 'topic_generator'`

- [ ] **Step 3: Write `topic_generator.py`**

```python
# scripts/topic_generator.py
import json
import logging
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import config


class TopicGenerationError(Exception):
    pass


REQUIRED_FIELDS = {"title", "slug", "category", "needs_code", "needs_diagram"}


def build_prompt(past_slugs: list) -> str:
    slugs_str = ", ".join(past_slugs) if past_slugs else "(none yet)"
    categories_str = ", ".join(config.CATEGORIES)
    return (
        f"You are selecting technical blog post topics for a backend engineering blog.\n\n"
        f"Generate exactly {config.POSTS_PER_RUN} topics as a JSON array. Requirements:\n"
        f"- Distribute across these categories: {categories_str}\n"
        f"- Target: senior backend engineers with production experience\n"
        f"- Be specific: 'PostgreSQL WAL internals' not 'Introduction to Databases'\n"
        f"- Avoid these already-published slugs: {slugs_str}\n\n"
        f"Return ONLY valid JSON, no other text:\n"
        f'[{{"title": "...", "slug": "...", "category": "...", "needs_code": true, "needs_diagram": false}}, ...]'
    )


def parse_topics(raw: str) -> list | None:
    """Parse and validate JSON topic list from Claude output. Returns None on failure."""
    # Strip markdown code fences if present
    raw = re.sub(r"^```[a-z]*\n?", "", raw.strip(), flags=re.MULTILINE)
    raw = re.sub(r"```$", "", raw.strip(), flags=re.MULTILINE)
    raw = raw.strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list) or not data:
        return None
    for item in data:
        if not REQUIRED_FIELDS.issubset(item.keys()):
            return None
    return data


class TopicGenerator:
    def __init__(self, logger: logging.Logger):
        self.logger = logger

    def generate(self, past_slugs: list) -> list:
        prompt = build_prompt(past_slugs)
        for attempt in range(2):
            self.logger.info(f"Generating topics (attempt {attempt + 1})")
            result = subprocess.run(
                ["claude", "-p", prompt, "--output-format", "text"],
                capture_output=True,
                text=True,
                timeout=120,
                cwd=str(config.BLOG_DIR),
            )
            if result.returncode != 0:
                self.logger.warning(f"claude -p failed: {result.stderr[:200]}")
                continue
            topics = parse_topics(result.stdout)
            if topics is not None:
                self.logger.info(f"Got {len(topics)} topics")
                return topics
            self.logger.warning(f"Attempt {attempt + 1}: could not parse topics JSON")
        raise TopicGenerationError("Topic generation failed after 2 attempts")
```

- [ ] **Step 4: Run tests — verify they pass**

```bash
cd /home/muklis/Documents/exploring/blog/scripts
python3 -m pytest tests/test_topic_generator.py -v
```

Expected: `8 passed`

- [ ] **Step 5: Commit**

```bash
git add scripts/topic_generator.py scripts/tests/test_topic_generator.py
git commit -m "feat: add topic_generator with Claude-powered dynamic topic selection"
```

---

## Task 5: Create `post_generator.py` with TDD

**Files:**
- Create: `scripts/post_generator.py`
- Create: `scripts/tests/test_post_generator.py`

- [ ] **Step 1: Write failing tests**

```python
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
```

- [ ] **Step 2: Run tests — verify they fail**

```bash
cd /home/muklis/Documents/exploring/blog/scripts
python3 -m pytest tests/test_post_generator.py -v
```

Expected: `ModuleNotFoundError: No module named 'post_generator'`

- [ ] **Step 3: Write `post_generator.py`**

```python
# scripts/post_generator.py
import logging
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import config


class PostGenerationError(Exception):
    pass


def build_prompt(topic: dict, date_str: str) -> str:
    code_block = ""
    if topic.get("needs_code"):
        code_block = """
CODE BLOCKS:
- Include 4-8 fenced code blocks demonstrating key concepts
- Start each block's first line with: // snippet-N (e.g. // snippet-1)
- Use realistic, production-quality code — not toy examples
- Each snippet should be self-contained and immediately useful
- Use the actual language tag (go, python, yaml, bash, sql, etc.)
"""

    diagram_block = ""
    if topic.get("needs_diagram"):
        svg_path = config.IMAGES_DIR / f"{topic['slug']}.svg"
        diagram_block = f"""
DIAGRAM:
- Include exactly one architecture diagram using the Excalidraw MCP tool
- Call mcp__claude_ai_Excalidraw__export_to_excalidraw to create the diagram as JSON
- Then use Bash to save the resulting SVG content to: {svg_path}
- Reference the diagram in the post body as: ![{topic['title']} Diagram](/images/diagrams/{topic['slug']}.svg)
- Place the diagram reference right after the opening paragraph
"""

    return f"""You are writing a technical blog post for Moh Ashari Muklis, a backend engineer.

TOPIC: {topic['title']}
CATEGORY: {topic['category']}
DATE: {date_str}

REQUIREMENTS:
- Write a high-quality, in-depth technical post (1500-2500 words)
- Target audience: senior backend engineers with production experience
- Tone: direct, opinionated, production-focused — no fluff
- Open with a compelling paragraph about a real problem this topic solves in production
- Use ## for section headers (no H1 in the body)
- Be specific: give concrete numbers, name real tools, describe real failure modes
{code_block}{diagram_block}
OUTPUT FORMAT:
Output ONLY the complete Jekyll post starting with frontmatter. No text before or after.

---
layout: post
title: "{topic['title']}"
date: {date_str} 08:00:00 +0700
tags: [pick 3-5 relevant tags]
description: "One-sentence description under 160 characters"
image: ""
thumbnail: ""
---

[post body here]
"""


def strip_preamble(raw: str) -> str:
    """Strip any text before the first '---' frontmatter delimiter."""
    idx = raw.find("---")
    if idx == -1:
        raise PostGenerationError("Output missing frontmatter delimiter")
    return raw[idx:]


class PostGenerator:
    def __init__(self, logger: logging.Logger):
        self.logger = logger

    def generate(self, topic: dict, date_str: str) -> str:
        prompt = build_prompt(topic, date_str)
        self.logger.info(f"Generating post: {topic['slug']}")

        timeout = (
            config.TIMEOUT_WITH_DIAGRAM
            if topic.get("needs_diagram")
            else config.TIMEOUT_TEXT_ONLY
        )

        result = subprocess.run(
            [
                "claude", "-p", prompt,
                "--output-format", "text",
                "--allowedTools", config.ALLOWED_TOOLS,
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(config.BLOG_DIR),
        )

        if result.returncode != 0:
            raise PostGenerationError(
                f"claude -p failed (exit {result.returncode}): {result.stderr[:300]}"
            )

        output = result.stdout.strip()
        if not output:
            raise PostGenerationError("claude -p returned empty output")

        return strip_preamble(output)
```

- [ ] **Step 4: Run tests — verify they pass**

```bash
cd /home/muklis/Documents/exploring/blog/scripts
python3 -m pytest tests/test_post_generator.py -v
```

Expected: `11 passed`

- [ ] **Step 5: Commit**

```bash
git add scripts/post_generator.py scripts/tests/test_post_generator.py
git commit -m "feat: add post_generator with Claude-powered Jekyll post generation"
```

---

## Task 6: Create `gist_manager.py` with TDD

**Files:**
- Create: `scripts/gist_manager.py`
- Create: `scripts/tests/test_gist_manager.py`

- [ ] **Step 1: Write failing tests**

```python
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
```

- [ ] **Step 2: Run tests — verify they fail**

```bash
cd /home/muklis/Documents/exploring/blog/scripts
python3 -m pytest tests/test_gist_manager.py -v
```

Expected: `ModuleNotFoundError: No module named 'gist_manager'`

- [ ] **Step 3: Write `gist_manager.py`**

```python
# scripts/gist_manager.py
import logging
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import config

FENCE_RE = re.compile(
    r"```(\w+)\n(// snippet-(\d+)[^\n]*)\n(.*?)```",
    re.DOTALL,
)

LANG_EXT = {
    "go": "go", "python": "py", "py": "py", "yaml": "yaml", "yml": "yaml",
    "sh": "sh", "bash": "sh", "sql": "sql", "json": "json",
    "typescript": "ts", "javascript": "js", "js": "js", "ts": "ts",
    "text": "txt", "txt": "txt", "dockerfile": "dockerfile",
    "toml": "toml", "hcl": "hcl", "proto": "proto",
}

GIST_URL_RE = re.compile(r"https://gist\.github\.com/\S+")
GIST_HASH_RE = re.compile(r"/([a-f0-9]{20,40})$")


def extract_snippets(content: str) -> list:
    """Return list of dicts with lang, num, code for each snippet block."""
    return [
        {"lang": m.group(1).lower(), "num": m.group(3), "code": m.group(4), "match": m}
        for m in FENCE_RE.finditer(content)
    ]


def parse_gist_hash(url: str) -> str | None:
    m = GIST_HASH_RE.search(url.strip())
    return m.group(1) if m else None


def embed_tag(github_user: str, gist_hash: str, filename: str) -> str:
    return (
        f'<script src="https://gist.github.com/{github_user}'
        f'/{gist_hash}.js?file={filename}"></script>'
    )


class GistManager:
    def __init__(self, github_user: str, logger: logging.Logger):
        self.github_user = github_user
        self.logger = logger

    def _create_gist(self, files: dict, description: str) -> str | None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = []
            for fname, code in files.items():
                p = os.path.join(tmpdir, fname)
                with open(p, "w") as f:
                    f.write(code)
                paths.append(p)
            result = subprocess.run(
                ["gh", "gist", "create", "--public", "--desc", description] + paths,
                capture_output=True, text=True, timeout=60,
            )
            if result.returncode != 0:
                self.logger.warning(f"gh gist create failed: {result.stderr[:200]}")
                return None
            # Find gist URL in output
            m = GIST_URL_RE.search(result.stdout)
            if not m:
                self.logger.warning(f"Could not find gist URL in output: {result.stdout[:200]}")
                return None
            return parse_gist_hash(m.group(0))

    def process(self, content: str, slug: str, title: str) -> str:
        snippets = extract_snippets(content)
        if not snippets:
            return content

        gist_files = {}
        for s in snippets:
            ext = LANG_EXT.get(s["lang"], "txt")
            fname = f"snippet-{s['num']}.{ext}"
            gist_files[fname] = s["code"]

        gist_hash = self._create_gist(gist_files, f"{title} — code snippets")
        if gist_hash is None:
            self.logger.warning(f"{slug}: gist failed, keeping raw code blocks")
            return content

        def replacer(m: re.Match) -> str:
            lang = m.group(1).lower()
            num = m.group(3)
            ext = LANG_EXT.get(lang, "txt")
            fname = f"snippet-{num}.{ext}"
            return embed_tag(self.github_user, gist_hash, fname)

        return FENCE_RE.sub(replacer, content)
```

- [ ] **Step 4: Run tests — verify they pass**

```bash
cd /home/muklis/Documents/exploring/blog/scripts
python3 -m pytest tests/test_gist_manager.py -v
```

Expected: `8 passed`

- [ ] **Step 5: Commit**

```bash
git add scripts/gist_manager.py scripts/tests/test_gist_manager.py
git commit -m "feat: add gist_manager for code block extraction and GitHub Gist embedding"
```

---

## Task 7: Create `unsplash.py` with TDD

**Files:**
- Create: `scripts/unsplash.py`
- Create: `scripts/tests/test_unsplash.py`

- [ ] **Step 1: Write failing tests**

```python
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
```

- [ ] **Step 2: Run tests — verify they fail**

```bash
cd /home/muklis/Documents/exploring/blog/scripts
python3 -m pytest tests/test_unsplash.py -v
```

Expected: `ModuleNotFoundError: No module named 'unsplash'`

- [ ] **Step 3: Write `unsplash.py`**

```python
# scripts/unsplash.py
import logging
import os
import re
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
import config

STOP_WORDS = {
    "a", "an", "the", "and", "or", "in", "on", "at", "to", "for",
    "of", "with", "from", "by", "vs", "via", "into", "using",
}


def extract_keywords(title: str) -> list:
    """Extract 2-3 meaningful keywords from a topic title."""
    words = re.findall(r"[a-zA-Z]+", title.lower())
    return [w for w in words if w not in STOP_WORDS and len(w) > 2][:3]


def inject_urls(content: str, regular_url: str, small_url: str) -> str:
    """Replace empty image: "" and thumbnail: "" placeholders in frontmatter."""
    content = re.sub(r'^image: ""', f'image: "{regular_url}"', content, flags=re.MULTILINE)
    content = re.sub(r'^thumbnail: ""', f'thumbnail: "{small_url}"', content, flags=re.MULTILINE)
    return content


class UnsplashClient:
    def __init__(self, logger: logging.Logger):
        self.logger = logger

    def fetch_photo(self, query: str):
        """Return (regular_url, small_url) tuple or None on failure."""
        key = os.environ.get("UNSPLASH_ACCESS_KEY")
        if not key:
            self.logger.warning("UNSPLASH_ACCESS_KEY not set — skipping thumbnails")
            return None
        try:
            resp = requests.get(
                f"{config.UNSPLASH_API_BASE}/photos/random",
                params={"query": query, "orientation": "landscape"},
                headers={"Authorization": f"Client-ID {key}"},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            return data["urls"]["regular"], data["urls"]["small"]
        except Exception as e:
            self.logger.warning(f"Unsplash API error: {e}")
            return None

    def enrich_post(self, content: str, title: str) -> str:
        """Inject Unsplash URLs into post content. Returns content unchanged on failure."""
        keywords = extract_keywords(title)
        query = " ".join(keywords)
        result = self.fetch_photo(query)
        if result is None:
            return content
        regular_url, small_url = result
        return inject_urls(content, regular_url, small_url)
```

- [ ] **Step 4: Run tests — verify they pass**

```bash
cd /home/muklis/Documents/exploring/blog/scripts
python3 -m pytest tests/test_unsplash.py -v
```

Expected: `8 passed`

- [ ] **Step 5: Commit**

```bash
git add scripts/unsplash.py scripts/tests/test_unsplash.py
git commit -m "feat: add unsplash module for thumbnail injection into post frontmatter"
```

---

## Task 8: Create `publisher.py` with TDD

**Files:**
- Create: `scripts/publisher.py`
- Create: `scripts/tests/test_publisher.py`

- [ ] **Step 1: Write failing tests**

```python
# scripts/tests/test_publisher.py
import pytest
from unittest.mock import patch, MagicMock, call
import logging
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import publisher

logger = logging.getLogger("test")
REPO = Path("/home/muklis/Documents/exploring/blog")


def make_pub():
    return publisher.GitPublisher(REPO, logger)


def test_publish_nothing_when_no_files():
    pub = make_pub()
    with patch("subprocess.run") as mock_run:
        pub.publish([], [], "2026-03-23")
    mock_run.assert_not_called()


def test_publish_calls_git_add_commit_push():
    pub = make_pub()
    post = REPO / "_posts/2026-03-23-test.md"
    mock_ok = MagicMock(returncode=0, stdout="file.md\n", stderr="")
    with patch("subprocess.run", return_value=mock_ok) as mock_run:
        pub.publish([post], [], "2026-03-23")
    commands = [c.args[0] for c in mock_run.call_args_list]
    assert any("add" in cmd for cmd in commands)
    assert any("commit" in cmd for cmd in commands)
    assert any("push" in cmd for cmd in commands)


def test_publish_raises_on_git_failure():
    pub = make_pub()
    post = REPO / "_posts/2026-03-23-test.md"
    mock_fail = MagicMock(returncode=1, stdout="", stderr="push failed")
    mock_ok = MagicMock(returncode=0, stdout="file.md\n", stderr="")

    def side_effect(cmd, **kwargs):
        if "push" in cmd:
            return mock_fail
        return mock_ok

    with patch("subprocess.run", side_effect=side_effect):
        with pytest.raises(publisher.GitPublishError):
            pub.publish([post], [], "2026-03-23")


def test_publish_skips_commit_when_nothing_staged():
    pub = make_pub()
    post = REPO / "_posts/2026-03-23-test.md"
    empty_staged = MagicMock(returncode=0, stdout="", stderr="")
    mock_ok = MagicMock(returncode=0, stdout="", stderr="")

    def side_effect(cmd, **kwargs):
        if "diff" in cmd:
            return empty_staged
        return mock_ok

    with patch("subprocess.run", side_effect=side_effect) as mock_run:
        pub.publish([post], [], "2026-03-23")

    commands = [c.args[0] for c in mock_run.call_args_list]
    assert not any("push" in cmd for cmd in commands)
```

- [ ] **Step 2: Run tests — verify they fail**

```bash
cd /home/muklis/Documents/exploring/blog/scripts
python3 -m pytest tests/test_publisher.py -v
```

Expected: `ModuleNotFoundError: No module named 'publisher'`

- [ ] **Step 3: Write `publisher.py`**

```python
# scripts/publisher.py
import logging
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import config


class GitPublishError(Exception):
    pass


class GitPublisher:
    def __init__(self, repo_dir: Path, logger: logging.Logger):
        self.repo_dir = repo_dir
        self.logger = logger

    def _run(self, cmd: list) -> subprocess.CompletedProcess:
        result = subprocess.run(
            cmd,
            cwd=str(self.repo_dir),
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise GitPublishError(
                f"Command {cmd[0]} {cmd[1] if len(cmd) > 1 else ''} failed: {result.stderr.strip()}"
            )
        return result

    def publish(self, post_paths: list, image_paths: list, date_str: str) -> None:
        all_files = [
            str(p.relative_to(self.repo_dir))
            for p in post_paths + image_paths
            if p.exists()
        ]
        if not all_files:
            self.logger.warning("No files to publish")
            return

        self._run(["git", "add"] + all_files)

        staged = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            cwd=str(self.repo_dir),
            capture_output=True,
            text=True,
        )
        if not staged.stdout.strip():
            self.logger.info("Nothing staged — skipping commit")
            return

        n = len(post_paths)
        msg = f"Auto-generate {n} posts for {date_str}"
        self._run(["git", "commit", "-m", msg])
        self.logger.info(f"Committed: {msg}")

        self._run(["git", "push", "origin", "master"])
        self.logger.info("Pushed to origin/master")
```

- [ ] **Step 4: Run tests — verify they pass**

```bash
cd /home/muklis/Documents/exploring/blog/scripts
python3 -m pytest tests/test_publisher.py -v
```

Expected: `4 passed`

- [ ] **Step 5: Commit**

```bash
git add scripts/publisher.py scripts/tests/test_publisher.py
git commit -m "feat: add publisher module for git add/commit/push pipeline"
```

---

## Task 9: Create `orchestrator.py`

**Files:**
- Create: `scripts/orchestrator.py`

No unit tests — `orchestrator.py` is integration glue. Test it by running a dry-run smoke test.

- [ ] **Step 1: Write `orchestrator.py`**

```python
#!/usr/bin/env python3
"""
Blog post generation orchestrator.
Generates POSTS_PER_RUN technical posts daily using Claude, Gist, Unsplash, and Excalidraw.
"""

import datetime
import logging
import logging.handlers
import subprocess
import sys
from pathlib import Path

# Ensure sibling modules are importable
sys.path.insert(0, str(Path(__file__).parent))

import config
import history
import topic_generator
import post_generator
import gist_manager
import unsplash
import publisher


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging() -> logging.Logger:
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("blog_gen")
    logger.setLevel(logging.DEBUG)

    fh = logging.handlers.RotatingFileHandler(
        config.LOG_DIR / "generation.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=7,
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))

    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger


# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------

def preflight(logger: logging.Logger) -> bool:
    """Check that required CLI tools are available. Returns False if Claude is missing."""
    checks = [
        (["claude", "--version"], "Claude CLI", True),
        (["gh", "auth", "status"], "GitHub CLI auth", False),
        (["git", "remote", "-v"], "git remote", False),
    ]
    ok = True
    for cmd, name, required in checks:
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                logger.info(f"[preflight] {name}: OK")
            else:
                msg = f"[preflight] {name}: WARNING — {result.stderr.strip()[:100]}"
                if required:
                    logger.error(msg)
                    ok = False
                else:
                    logger.warning(msg)
        except FileNotFoundError:
            msg = f"[preflight] {name}: NOT FOUND"
            if required:
                logger.error(msg)
                ok = False
            else:
                logger.warning(msg)
    return ok


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

class BlogOrchestrator:
    def __init__(self, logger: logging.Logger):
        self.logger = logger
        self.topic_gen = topic_generator.TopicGenerator(logger)
        self.post_gen = post_generator.PostGenerator(logger)
        self.gist_mgr = gist_manager.GistManager(config.GITHUB_USER, logger)
        self.unsplash_client = unsplash.UnsplashClient(logger)
        self.git_pub = publisher.GitPublisher(config.BLOG_DIR, logger)

    def run(self) -> int:
        today = datetime.date.today()
        date_str = today.isoformat()
        self.logger.info(f"=== Blog generation run: {date_str} ===")

        # Load history
        hist = history.load(config.HISTORY_PATH)
        past_slugs = history.last_n_slugs(hist, n=50)

        # Generate topics
        try:
            topics = self.topic_gen.generate(past_slugs)
        except topic_generator.TopicGenerationError as e:
            self.logger.error(f"Topic generation failed: {e}")
            return 1

        self.logger.info(f"Topics selected: {len(topics)}")

        successful_posts: list[Path] = []
        image_paths: list[Path] = []
        failed = 0

        for i, topic in enumerate(topics, 1):
            slug = topic["slug"]
            self.logger.info(f"[{i}/{len(topics)}] {slug}")

            try:
                # Generate post
                content = self.post_gen.generate(topic, date_str)

                # Embed code into Gist
                if topic.get("needs_code"):
                    content = self.gist_mgr.process(content, slug, topic["title"])

                # Inject Unsplash thumbnail
                content = self.unsplash_client.enrich_post(content, topic["title"])

                # Validate
                if not content.startswith("---"):
                    raise post_generator.PostGenerationError(
                        f"{slug}: content does not start with frontmatter"
                    )

                # Write post
                config.POSTS_DIR.mkdir(parents=True, exist_ok=True)
                post_path = config.POSTS_DIR / f"{date_str}-{slug}.md"
                post_path.write_text(content, encoding="utf-8")
                self.logger.info(f"Written: {post_path.name}")

                # Check diagram
                if topic.get("needs_diagram"):
                    diagram_path = config.IMAGES_DIR / f"{slug}.svg"
                    if diagram_path.exists():
                        image_paths.append(diagram_path)
                    else:
                        self.logger.warning(f"Diagram SVG missing: {diagram_path}")

                # Save history immediately (crash-safe)
                history.append_and_save(config.HISTORY_PATH, slug)
                successful_posts.append(post_path)

            except Exception as e:
                failed += 1
                self.logger.error(f"Failed [{slug}]: {e}", exc_info=True)

        self.logger.info(
            f"Generation complete: {len(successful_posts)}/{len(topics)} succeeded, {failed} failed"
        )

        if not successful_posts:
            self.logger.error("No posts generated — skipping git publish")
            return 1

        try:
            self.git_pub.publish(successful_posts, image_paths, date_str)
        except publisher.GitPublishError as e:
            self.logger.error(f"Git publish failed: {e}")
            return 1

        return 0 if failed == 0 else 1


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logger = setup_logging()
    if not preflight(logger):
        logger.error("Pre-flight failed — aborting")
        sys.exit(1)
    orchestrator = BlogOrchestrator(logger)
    sys.exit(orchestrator.run())
```

- [ ] **Step 2: Verify syntax**

```bash
cd /home/muklis/Documents/exploring/blog/scripts
python3 -c "import orchestrator; print('OK')"
```

Expected: `OK`

- [ ] **Step 3: Run full test suite**

```bash
cd /home/muklis/Documents/exploring/blog/scripts
python3 -m pytest tests/ -v
```

Expected: All previous tests still pass.

- [ ] **Step 4: Commit**

```bash
git add scripts/orchestrator.py
git commit -m "feat: add orchestrator — main entry point wiring all pipeline modules"
```

---

## Task 10: Update `run_blog.sh` and crontab

**Files:**
- Modify: `scripts/run_blog.sh`

- [ ] **Step 1: Update `run_blog.sh` to call `orchestrator.py`**

Edit `scripts/run_blog.sh` line 16. Change:
```bash
/usr/bin/python3 "${SCRIPTS_DIR}/generate_posts.py"
```
To:
```bash
/usr/bin/python3 "${SCRIPTS_DIR}/orchestrator.py"
```

- [ ] **Step 2: Verify the script is executable and parses correctly**

```bash
bash -n /home/muklis/Documents/exploring/blog/scripts/run_blog.sh && echo "OK"
```

Expected: `OK`

- [ ] **Step 3: Update crontab from 1 AM to 8 AM**

```bash
crontab -l | sed 's|^0 1 \* \* \*|0 8 * * *|' | crontab -
```

- [ ] **Step 4: Verify new crontab**

```bash
crontab -l
```

Expected: `0 8 * * * /home/muklis/.../run_blog.sh ...`

- [ ] **Step 5: Commit**

```bash
git add scripts/run_blog.sh
git commit -m "fix: update run_blog.sh to call orchestrator.py, cron to 8 AM"
```

---

## Task 11: Remove old files

**Files:**
- Delete: `scripts/generate_posts.py`
- Delete: `scripts/topic_pool.py`

- [ ] **Step 1: Confirm all tests pass before deleting**

```bash
cd /home/muklis/Documents/exploring/blog/scripts
python3 -m pytest tests/ -v
```

Expected: All tests pass.

- [ ] **Step 2: Delete old files**

```bash
rm /home/muklis/Documents/exploring/blog/scripts/generate_posts.py
rm /home/muklis/Documents/exploring/blog/scripts/topic_pool.py
```

- [ ] **Step 3: Run tests again to confirm nothing broke**

```bash
cd /home/muklis/Documents/exploring/blog/scripts
python3 -m pytest tests/ -v
```

Expected: All tests still pass.

- [ ] **Step 4: Commit**

```bash
git add -u scripts/generate_posts.py scripts/topic_pool.py
git commit -m "chore: remove old monolithic generate_posts.py and topic_pool.py"
```

---

## Task 12: Smoke test end-to-end

- [ ] **Step 1: Run `orchestrator.py` with preflight only (no real Claude call)**

```bash
cd /home/muklis/Documents/exploring/blog/scripts
python3 -c "
import sys
sys.path.insert(0, '.')
from orchestrator import setup_logging, preflight
logger = setup_logging()
ok = preflight(logger)
print('Preflight:', 'PASS' if ok else 'FAIL')
"
```

Expected: preflight logs for `claude`, `gh`, `git` and prints `Preflight: PASS`

- [ ] **Step 2: Verify logs directory exists and log file is writable**

```bash
ls -la /home/muklis/Documents/exploring/blog/scripts/logs/
```

Expected: `generation.log` is present and writable.

- [ ] **Step 3: Final commit**

```bash
git add scripts/
git status
git commit -m "chore: verify end-to-end smoke test passed" --allow-empty
```

---

## Summary

| Task | Module | Tests |
|---|---|---|
| 1 | Test scaffold | — |
| 2 | `config.py` | — |
| 3 | `history.py` | 7 tests |
| 4 | `topic_generator.py` | 8 tests |
| 5 | `post_generator.py` | 11 tests |
| 6 | `gist_manager.py` | 8 tests |
| 7 | `unsplash.py` | 8 tests |
| 8 | `publisher.py` | 4 tests |
| 9 | `orchestrator.py` | smoke |
| 10 | `run_blog.sh` + crontab | — |
| 11 | Delete old files | — |
| 12 | Smoke test | — |

**Total: 46 unit tests across 6 modules.**
