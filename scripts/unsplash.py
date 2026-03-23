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
