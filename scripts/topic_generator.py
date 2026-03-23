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
SLUG_RE = re.compile(r'^[a-z0-9][a-z0-9\-]*$')


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
        if not SLUG_RE.match(str(item["slug"])):
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
                timeout=config.TIMEOUT_TOPIC_GENERATION,
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
