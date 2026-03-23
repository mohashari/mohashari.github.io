# Blog Post Scheduler Rewrite — Design Spec

**Date:** 2026-03-23
**Author:** Moh Ashari Muklis
**Status:** Approved

---

## Overview

Full rewrite of the automated blog post generation pipeline in `scripts/`. The new system generates 5 technical blog posts daily at 8 AM using Claude AI for dynamic topic generation and post writing, GitHub Gist for code embedding, Excalidraw MCP for architecture diagrams, and the Unsplash API for post thumbnails.

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
├── topic_generator.py    # Calls Claude to generate 5 fresh topics, checks history
├── post_generator.py     # Calls Claude to write the full Jekyll post content
├── gist_manager.py       # Extracts code blocks, uploads to GitHub Gist, embeds tags
├── excalidraw_manager.py # Validates Excalidraw-generated SVG files exist
├── unsplash.py           # Calls Unsplash API, returns photo URLs for frontmatter
├── publisher.py          # git add/commit/push to origin/master
├── history.py            # Load/save topics_history.json, deduplication helpers
├── config.py             # All constants: paths, timeouts, GITHUB_USER, categories
└── run_blog.sh           # Shell wrapper for cron (unchanged interface)
```

Old files removed: `generate_posts.py`, `topic_pool.py`

---

## Pipeline Data Flow

Each daily run executes this sequence:

### Step 1 — Topic Generation (`topic_generator.py`)
- Load `topics_history.json` via `history.py` — extract last 50 used slugs/titles
- Call `claude -p` with a structured prompt requesting 5 topics (one per category + one free-pick)
- Claude returns JSON array: `[{title, slug, category, needs_code, needs_diagram}]`
- Validate JSON; retry once if malformed
- Categories: `software_engineering`, `development`, `devsecops`, `ai_engineering`

### Step 2 — Post Generation (`post_generator.py`)
- For each topic, call `claude -p` with a detailed prompt:
  - 1500–2500 words, senior backend engineer audience
  - Frontmatter with layout, title, date, tags, description, image, thumbnail
  - If `needs_diagram=true`: include Excalidraw MCP instructions in prompt
  - If `needs_code=true`: include `// snippet-N` marker convention
- Timeout: 600s (diagram posts), 400s (text-only posts)
- Strip any output before first `---` frontmatter delimiter

### Step 3 — Code Embedding (`gist_manager.py`, if `needs_code`)
- Regex-extract fenced code blocks tagged with `// snippet-N`
- Write each snippet to a temp file, call `gh gist create --public`
- Parse gist hash from `gh` output URL
- Replace each code block in post content with `<script src="https://gist.github.com/...">` embed tag
- On failure: log warning, keep raw code blocks (post still publishes)

### Step 4 — Diagram Validation (`excalidraw_manager.py`, if `needs_diagram`)
- Check if `images/diagrams/{slug}.svg` exists (Claude saves it during generation)
- Log warning if missing; post publishes without diagram reference

### Step 5 — Unsplash Thumbnail (`unsplash.py`)
- Extract 2–3 keywords from topic title
- `GET https://api.unsplash.com/photos/random?query={keywords}&orientation=landscape`
- Auth: `Authorization: Client-ID {UNSPLASH_ACCESS_KEY}` from environment
- Extract `urls.regular` (for `image:`) and `urls.small` (for `thumbnail:`)
- Inject both fields into post frontmatter via regex replacement
- On failure (API error, missing key): skip silently, post publishes without thumbnail

### Step 6 — Write Post
- Validate content starts with `---`
- Write to `_posts/{date}-{slug}.md`

### Step 7 — Publish (`publisher.py`)
- `git add` all new post files + diagram SVGs
- `git commit -m "Auto-generate N posts for {date}"`
- `git push origin master`
- On failure: log error, exit with code 1

### Step 8 — History Update (`history.py`)
- Append successful slugs to `topics_history.json`
- Save after each successful post (not just at end, to survive partial runs)

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

---

## Environment Variables

| Variable | Required | Purpose |
|---|---|---|
| `UNSPLASH_ACCESS_KEY` | Yes (soft) | Unsplash API auth. Skipped gracefully if missing. |

---

## Error Handling

- **Per-post isolation:** each post fails independently; remaining posts still run
- **Topic generation failure:** abort entire run, log error
- **Gist failure:** keep raw code blocks, continue
- **Unsplash failure:** omit image fields, continue
- **Diagram missing:** log warning, continue
- **Git push failure:** log error, exit 1 (posts already written to disk)

---

## Crontab Change

```
# Old
0 1 * * * /home/muklis/.../run_blog.sh >> .../cron.log 2>&1

# New
0 8 * * * /home/muklis/.../run_blog.sh >> .../cron.log 2>&1
```

---

## Claude Topic Generation Prompt Design

```
You are selecting technical blog post topics for a backend engineering blog.

Generate exactly 5 topics as a JSON array. Requirements:
- One topic per category: software_engineering, development, devsecops, ai_engineering
- Fifth topic: any category
- Target: senior backend engineers with production experience
- Be specific: "PostgreSQL WAL internals" not "Introduction to Databases"
- Avoid these already-published topics: [list of last 50 slugs]

Return ONLY valid JSON, no other text:
[{"title": "...", "slug": "...", "category": "...", "needs_code": true, "needs_diagram": false}, ...]
```

---

## Out of Scope

- Frontend/theme changes
- Email notifications
- Multiple blog support
- Topic approval workflow
- Draft mode
