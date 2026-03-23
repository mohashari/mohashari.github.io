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
