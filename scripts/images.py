# scripts/images.py
import hashlib
import logging
import re


def _seed(title: str) -> int:
    return int(hashlib.md5(title.encode()).hexdigest(), 16) % 10_000


def inject_urls(content: str, regular_url: str, small_url: str) -> str:
    """Replace empty image: "" and thumbnail: "" placeholders in frontmatter."""
    content = re.sub(r'^image: ""', f'image: "{regular_url}"', content, flags=re.MULTILINE)
    content = re.sub(r'^thumbnail: ""', f'thumbnail: "{small_url}"', content, flags=re.MULTILINE)
    return content


class PicsumClient:
    def __init__(self, logger: logging.Logger):
        self.logger = logger

    def fetch_photo(self, title: str) -> tuple:
        """Return (regular_url, small_url) tuple derived from title seed."""
        seed = _seed(title)
        return f"https://picsum.photos/seed/{seed}/1080/720", f"https://picsum.photos/seed/{seed}/400/300"

    def enrich_post(self, content: str, title: str) -> str:
        """Inject Picsum URLs into post content."""
        regular_url, small_url = self.fetch_photo(title)
        return inject_urls(content, regular_url, small_url)
