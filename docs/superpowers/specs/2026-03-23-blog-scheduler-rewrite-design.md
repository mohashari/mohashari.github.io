# Blog Post Scheduler Rewrite — Design Spec

**Date:** 2026-03-23
**Author:** Moh Ashari Muklis
**Status:** Ready for Implementation

---

## Overview

Full rewrite of the automated blog post generation pipeline in `scripts/`. The new system generates 5 technical blog posts daily at 8 AM using Claude AI for dynamic topic generation and post writing, GitHub Gist for code embedding, Excalidraw MCP for architecture diagrams, and the Unsplash API for post thumbnails.

Old files removed: `generate_posts.py`, `topic_pool.py`

---

## Goals

- Replace hardcoded topic pool with Claude-generated fresh topics each run
- Add Unsplash API integration to embed thumbnail URLs (`image:` + `thumbnail:`) in frontmatter
- Restructure monolithic `generate_posts.py` into a clean multi-module package
- Change cron schedule from 1 AM to 8 AM

---

## File Structure

```
scripts/
├── orchestrator.py       # Entry point, wires all modules, runs the pipeline
├── topic_generator.py    # Calls Claude to generate fresh topics, checks history
├── post_generator.py     # Calls Claude to write the full Jekyll post content
│                         # (absorbs DescriptionGenerator — description is part of the post prompt)
├── gist_manager.py       # Extracts code blocks, uploads to GitHub Gist, embeds tags
├── unsplash.py           # Calls Unsplash API, returns photo URLs for frontmatter injection
├── publisher.py          # git add/commit/push to origin/master
├── history.py            # Load/save topics_history.json, per-post append_and_save()
├── config.py             # All constants: paths, timeouts, GITHUB_USER, categories, POSTS_PER_RUN
└── run_blog.sh           # Updated to call orchestrator.py (cron interface unchanged)
```

**Note on `excalidraw_manager.py`:** No separate module. Claude saves the SVG during post generation. `orchestrator.py` checks `images/diagrams/{slug}.svg` existence inline (two lines) and logs a warning if missing.

**Note on `DescriptionGenerator`:** Removed. The post generation prompt in `post_generator.py` includes the description angle inline — no separate Claude call needed.

---

## Configuration (`config.py`)

```python
BLOG_DIR = Path("/home/muklis/Documents/exploring/blog")
POSTS_DIR = BLOG_DIR / "_posts"
IMAGES_DIR = BLOG_DIR / "images" / "diagrams"
SCRIPTS_DIR = BLOG_DIR / "scripts"
LOG_DIR = SCRIPTS_DIR / "logs"
HISTORY_PATH = SCRIPTS_DIR / "topics_history.json"
GITHUB_USER = "mohashari"
POSTS_PER_RUN = 5
CATEGORIES = ["software_engineering", "development", "devsecops", "ai_engineering"]
TIMEOUT_WITH_DIAGRAM = 600
TIMEOUT_TEXT_ONLY = 400
UNSPLASH_API_BASE = "https://api.unsplash.com"
```

All prompt-time values (number of topics, category list) are derived from `config.py` at runtime — never hardcoded in prompt strings.

---

## Environment Variables & Pre-flight Checks

| Variable | Required | Purpose |
|---|---|---|
| `UNSPLASH_ACCESS_KEY` | Soft | Unsplash API auth. Missing = skip thumbnails gracefully. |
| `ANTHROPIC_API_KEY` | Implicit | Required by `claude` CLI. Managed by Claude CLI config. Missing = abort. |

`orchestrator.py` runs pre-flight checks on startup:
- `claude --version` — verify Claude CLI is in PATH
- `gh auth status` — verify GitHub CLI is authenticated
- `git remote -v` — verify git remote is configured
- Log warnings for any missing env vars; only abort if Claude CLI is missing (it is the core dependency)

---

## Pipeline Data Flow

### Step 1 — Topic Generation (`topic_generator.py`)

- Load `topics_history.json` via `history.py` — extract last 50 used slugs
- Build prompt using `CATEGORIES` and `POSTS_PER_RUN` from `config.py` (no hardcoded integers)
- Call `claude -p` requesting exactly `POSTS_PER_RUN` topics as JSON
- Validate JSON structure; if malformed, retry once with the same prompt
- If retry also fails: abort entire run, log error
- Return list of topic dicts: `[{title, slug, category, needs_code, needs_diagram}]`

**Claude topic generation prompt:**
```
You are selecting technical blog post topics for a backend engineering blog.

Generate exactly {POSTS_PER_RUN} topics as a JSON array. Requirements:
- Distribute across these categories: {", ".join(CATEGORIES)}
- Target: senior backend engineers with production experience
- Be specific: "PostgreSQL WAL internals" not "Introduction to Databases"
- Avoid these already-published slugs: {last_50_slugs}

Return ONLY valid JSON, no other text:
[{{"title": "...", "slug": "...", "category": "...", "needs_code": true, "needs_diagram": false}}, ...]
```

### Step 2 — Post Generation (`post_generator.py`)

- For each topic, call `claude -p` with a detailed prompt:
  - 1500–2500 words, senior backend engineer audience
  - Prompt instructs Claude to output a complete Jekyll post starting with `---` frontmatter
  - Frontmatter template includes placeholder comments for `image:` and `thumbnail:` fields:
    ```
    ---
    layout: post
    title: "..."
    date: ...
    tags: [...]
    description: "..."
    image: ""
    thumbnail: ""
    ---
    ```
  - If `needs_diagram=true`: include Excalidraw MCP instructions in prompt
  - If `needs_code=true`: include `// snippet-N` marker convention
- Timeout: 600s (diagram posts), 400s (text-only posts)
- Strip any output before first `---` frontmatter delimiter
- `image:` and `thumbnail:` start as empty strings; `unsplash.py` fills them in Step 5

### Step 3 — Code Embedding (`gist_manager.py`, if `needs_code`)

- Regex-extract fenced code blocks tagged with `// snippet-N`
- Write each snippet to a temp file
- Call `gh gist create --public` and parse the gist URL from stdout output using regex `r"https://gist\.github\.com/\S+"` (the `--json` flag is not reliably available on all `gh` versions for `gist` subcommands)
- Extract gist hash from the URL using regex `r"/([a-f0-9]{20,40})$"`
- Pre-flight check in `orchestrator.py` runs `gh gist --help` to confirm `gh` is available and authenticated
- Replace each code block in post content with `<script src="https://gist.github.com/{user}/{hash}.js?file={filename}">` embed tag
- On failure: log warning, keep raw code blocks (post still publishes)

### Step 4 — Unsplash Thumbnail (`unsplash.py`)

- Extract 2–3 keywords from topic title
- `GET https://api.unsplash.com/photos/random?query={keywords}&orientation=landscape`
- Auth: `Authorization: Client-ID {UNSPLASH_ACCESS_KEY}` from environment
- Extract `urls.regular` (for `image:`) and `urls.small` (for `thumbnail:`)
- Inject into post content by replacing the empty `image: ""` and `thumbnail: ""` lines in frontmatter using regex:
  ```python
  content = re.sub(r'^image: ""', f'image: "{regular_url}"', content, flags=re.MULTILINE)
  content = re.sub(r'^thumbnail: ""', f'thumbnail: "{small_url}"', content, flags=re.MULTILINE)
  ```
- On failure (API error, missing key, unexpected response): leave fields as empty strings, log warning, continue

### Step 5 — Write Post

- Validate content starts with `---`
- Write to `_posts/{date}-{slug}.md`
- Check if `images/diagrams/{slug}.svg` exists (if `needs_diagram=True`); log warning if missing
- Call `history.append_and_save(HISTORY_PATH, slug)` immediately after successful write (crash-resilient: partial runs save completed slugs)

### Step 6 — Publish (`publisher.py`)

- `git add` all new post files + diagram SVGs
- `git commit -m "Auto-generate N posts for {date}"`
- `git push origin master`
- On failure: log error, exit with code 1

---

## `history.py` Interface

```python
def load(path: Path) -> dict:
    """Return history dict with 'used' list and 'last_updated'."""

def append_and_save(path: Path, slug: str) -> None:
    """Load current history, append slug, save immediately. Crash-safe per-post update."""

def last_n_slugs(history: dict, n: int = 50) -> list[str]:
    """Return the most recent n slugs for deduplication prompt context."""
```

---

## Logging

`orchestrator.py` calls `setup_logging()` once at startup, returning a named logger `"blog_gen"` with:
- Rotating file handler: `logs/generation.log` (10 MB, 7 backups)
- Stdout handler (INFO level)

Each module receives the logger via constructor injection (no module-level `getLogger` calls). This keeps logging configuration centralized in `orchestrator.py`.

---

## `run_blog.sh` Update

The shell wrapper is updated to call `orchestrator.py` instead of `generate_posts.py`:

```bash
/usr/bin/python3 "${SCRIPTS_DIR}/orchestrator.py"
```

The cron interface (path, exit code contract, log redirection) is unchanged.

---

## Crontab Change

```
# Old
0 1 * * * /home/muklis/.../run_blog.sh >> .../cron.log 2>&1

# New
0 8 * * * /home/muklis/.../run_blog.sh >> .../cron.log 2>&1
```

---

## Error Handling

| Failure | Behavior |
|---|---|
| Topic generation fails (first attempt) | Retry once with same prompt |
| Topic generation fails (retry) | Abort entire run, log error, exit 1 |
| Post generation fails | Skip this post, continue with remaining |
| Gist creation fails | Keep raw code blocks, continue |
| Unsplash API fails / key missing | Leave image fields empty, continue |
| Diagram SVG missing | Log warning, continue |
| Git push fails | Log error, exit 1 (posts already written to disk) |

---

## Intra-Package Import Strategy

`scripts/` is not a Python package (no `__init__.py`). `orchestrator.py` adds `SCRIPTS_DIR` to `sys.path` at startup so all sibling modules (`topic_generator`, `history`, `config`, etc.) are importable by name without relative imports.

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
```

## Out of Scope

- Frontend/theme changes
- Email notifications
- Multiple blog support
- Topic approval workflow
- Draft mode
