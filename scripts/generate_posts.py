#!/usr/bin/env python3
"""
Automated blog post generator.
Generates 20 technical posts per day using Claude Code CLI,
uploads code to GitHub Gist, saves diagrams via Excalidraw MCP,
and commits everything to GitHub.
"""

import datetime
import json
import logging
import logging.handlers
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BLOG_DIR = Path("/home/muklis/Documents/exploring/blog")
SCRIPTS_DIR = BLOG_DIR / "scripts"
POSTS_DIR = BLOG_DIR / "_posts"
IMAGES_DIR = BLOG_DIR / "images" / "diagrams"
LOG_DIR = SCRIPTS_DIR / "logs"
HISTORY_PATH = SCRIPTS_DIR / "topics_history.json"
GITHUB_USER = "mohashari"

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class BlogGenError(Exception):
    pass


class PostGenerationError(BlogGenError):
    pass


class GistCreationError(BlogGenError):
    pass


class PostWriteError(BlogGenError):
    pass


class GitPublishError(BlogGenError):
    pass


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def setup_logging() -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("blog_gen")
    logger.setLevel(logging.DEBUG)

    fh = logging.handlers.RotatingFileHandler(
        LOG_DIR / "generation.log",
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
# PostConfig
# ---------------------------------------------------------------------------

LANG_EXT = {
    "go": "go",
    "python": "py",
    "py": "py",
    "yaml": "yaml",
    "yml": "yaml",
    "sh": "sh",
    "bash": "sh",
    "sql": "sql",
    "json": "json",
    "typescript": "ts",
    "javascript": "js",
    "js": "js",
    "ts": "ts",
    "text": "txt",
    "txt": "txt",
    "dockerfile": "dockerfile",
    "toml": "toml",
    "hcl": "hcl",
    "proto": "proto",
}


@dataclass
class PostConfig:
    category: str
    slug: str
    title: str
    date_str: str
    needs_code: bool
    needs_diagram: bool
    post_filename: str = field(init=False)
    post_path: Path = field(init=False)
    diagram_path: Path = field(init=False)

    def __post_init__(self):
        self.post_filename = f"{self.date_str}-{self.slug}.md"
        self.post_path = POSTS_DIR / self.post_filename
        self.diagram_path = IMAGES_DIR / f"{self.slug}.svg"

    description: str = ""

    @classmethod
    def from_topic(cls, topic: dict, date: datetime.date) -> "PostConfig":
        return cls(
            category=topic["category"],
            slug=topic["slug"],
            title=topic["title"],
            date_str=date.isoformat(),
            needs_code=topic["needs_code"],
            needs_diagram=topic["needs_diagram"],
        )


# ---------------------------------------------------------------------------
# DescriptionGenerator
# ---------------------------------------------------------------------------


class DescriptionGenerator:
    def __init__(self, logger: logging.Logger):
        self.logger = logger

    def generate(self, config: PostConfig) -> str:
        prompt = (
            f"You are planning a technical blog post.\n"
            f"Topic: {config.title}\n"
            f"Category: {config.category}\n\n"
            f"Write a 2-3 sentence description of this post: the specific angle, "
            f"the key problem it solves, and the main takeaway for a senior backend engineer. "
            f"Output ONLY the description, no labels or extra text."
        )
        self.logger.info(f"Getting description: {config.slug}")
        result = subprocess.run(
            ["claude", "-p", prompt],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(BLOG_DIR),
        )
        if result.returncode != 0 or not result.stdout.strip():
            self.logger.warning(f"{config.slug}: description generation failed, using empty description")
            return ""
        description = result.stdout.strip()
        self.logger.debug(f"{config.slug} description: {description}")
        return description


# ---------------------------------------------------------------------------
# GistManager
# ---------------------------------------------------------------------------


class GistManager:
    def __init__(self, github_user: str, logger: logging.Logger):
        self.github_user = github_user
        self.logger = logger

    def create_gist(self, files: dict, description: str) -> str:
        with tempfile.TemporaryDirectory() as tmpdir:
            file_paths = []
            for fname, content in files.items():
                fpath = os.path.join(tmpdir, fname)
                with open(fpath, "w") as f:
                    f.write(content)
                file_paths.append(fpath)

            cmd = ["gh", "gist", "create", "--public", "--desc", description] + file_paths
            self.logger.debug(f"Creating gist: {description}")
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if result.returncode != 0:
                raise GistCreationError(f"gh gist create failed: {result.stderr.strip()}")

            url = result.stdout.strip().split("\n")[-1]
            m = re.search(r"/([a-f0-9]{20,40})$", url)
            if not m:
                raise GistCreationError(f"Could not parse gist hash from URL: {url}")
            gist_hash = m.group(1)
            self.logger.info(f"Gist created: {gist_hash}")
            return gist_hash

    def embed_tag(self, gist_hash: str, filename: str) -> str:
        return (
            f'<script src="https://gist.github.com/{self.github_user}'
            f"/{gist_hash}.js?file={filename}\"></script>"
        )


# ---------------------------------------------------------------------------
# CodeExtractor
# ---------------------------------------------------------------------------

FENCE_RE = re.compile(
    r"```(\w+)\n(// snippet-(\d+)[^\n]*)\n(.*?)```",
    re.DOTALL,
)


class CodeExtractor:
    def __init__(self, gist_manager: GistManager, logger: logging.Logger):
        self.gist_manager = gist_manager
        self.logger = logger

    def extract_and_replace(self, content: str, config: PostConfig) -> str:
        matches = list(FENCE_RE.finditer(content))
        if not matches:
            self.logger.debug(f"{config.slug}: no code blocks found")
            return content

        gist_files = {}
        snippet_map = {}
        for m in matches:
            lang = m.group(1).lower()
            snippet_num = m.group(3) or str(len(gist_files) + 1)
            code = m.group(4)
            ext = LANG_EXT.get(lang, "txt")
            filename = f"snippet-{snippet_num}.{ext}"
            gist_files[filename] = code
            snippet_map[m.start()] = filename

        try:
            gist_hash = self.gist_manager.create_gist(
                files=gist_files,
                description=f"{config.title} — code snippets",
            )
        except GistCreationError as e:
            self.logger.warning(f"{config.slug}: gist creation failed ({e}), keeping raw code blocks")
            return content

        def replacer(m: re.Match) -> str:
            lang = m.group(1).lower()
            snippet_num = m.group(3) or "1"
            ext = LANG_EXT.get(lang, "txt")
            filename = f"snippet-{snippet_num}.{ext}"
            return self.gist_manager.embed_tag(gist_hash, filename)

        return FENCE_RE.sub(replacer, content)


# ---------------------------------------------------------------------------
# PostGenerator
# ---------------------------------------------------------------------------

ALLOWED_TOOLS = (
    "Bash,"
    "mcp__claude_ai_Excalidraw__export_to_excalidraw,"
    "mcp__claude_ai_Excalidraw__create_view,"
    "mcp__claude_ai_Excalidraw__save_checkpoint"
)

# Timeout in seconds: diagram posts require MCP tool calls on top of generation
TIMEOUT_WITH_DIAGRAM = 600
TIMEOUT_TEXT_ONLY = 400


class PostGenerator:
    def __init__(self, logger: logging.Logger):
        self.logger = logger

    def build_prompt(self, config: PostConfig) -> str:
        code_instructions = ""
        if config.needs_code:
            code_instructions = """
CODE BLOCKS:
- Include 4-8 fenced code blocks demonstrating the key concepts
- Start each block's first line with: // snippet-N (e.g. // snippet-1, // snippet-2)
- Use realistic, production-quality code — not toy examples
- Each snippet should be self-contained and immediately useful
- Use the actual language tag (go, python, yaml, bash, sql, etc.)
"""

        diagram_instructions = ""
        if config.needs_diagram:
            diagram_instructions = f"""
DIAGRAM:
- Include exactly one architecture diagram using the Excalidraw MCP tool
- Call mcp__claude_ai_Excalidraw__export_to_excalidraw to create the diagram as JSON
- Then use Bash to save the resulting SVG content to: {IMAGES_DIR}/{config.slug}.svg
- Reference the diagram in the post body as: ![{config.title} Diagram](/images/diagrams/{config.slug}.svg)
- Place the diagram reference right after the opening paragraph
- The diagram should show the high-level architecture or data flow described in the post
"""

        description_section = ""
        if config.description:
            description_section = f"\nDESCRIPTION: {config.description}\nUse this as your guiding angle for the post.\n"

        return f"""You are writing a technical blog post for Moh Ashari Muklis, a backend engineer.

TOPIC: {config.title}
CATEGORY: {config.category}
DATE: {config.date_str}{description_section}

REQUIREMENTS:
- Write a high-quality, in-depth technical post (1500-2500 words)
- Target audience: senior backend engineers with production experience
- Tone: direct, opinionated, production-focused — no fluff, no "in conclusion" paragraphs
- Open with a compelling paragraph about a real problem this topic solves in production
- Use ## for section headers (no H1 in the body)
- Be specific: give concrete numbers, name real tools, describe real failure modes
- Do NOT write generic content or surface-level explanations
{code_instructions}
{diagram_instructions}

OUTPUT FORMAT:
Output ONLY the complete Jekyll post file starting with frontmatter. No text before or after.

---
layout: post
title: "{config.title}"
date: {config.date_str} 08:00:00 +0700
tags: [pick 3-5 relevant tags]
description: "One-sentence description under 160 characters"
---

[post body here]
"""

    def generate(self, config: PostConfig) -> str:
        prompt = self.build_prompt(config)
        self.logger.info(f"Generating: {config.slug}")

        cmd = [
            "claude",
            "-p",
            prompt,
            "--output-format",
            "text",
            "--allowedTools",
            ALLOWED_TOOLS,
        ]

        timeout = TIMEOUT_WITH_DIAGRAM if config.needs_diagram else TIMEOUT_TEXT_ONLY

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(BLOG_DIR),
        )

        if result.returncode != 0:
            raise PostGenerationError(
                f"claude -p failed (exit {result.returncode}): {result.stderr[:500]}"
            )

        output = result.stdout.strip()
        if not output:
            raise PostGenerationError("claude -p returned empty output")

        # Strip any text before the frontmatter delimiter
        frontmatter_start = output.find("---")
        if frontmatter_start == -1:
            self.logger.debug(
                f"{config.slug}: output preview (no frontmatter found): {output[:500]!r}"
            )
            raise PostGenerationError("Output missing frontmatter delimiter")

        output = output[frontmatter_start:]

        return output


# ---------------------------------------------------------------------------
# PostWriter
# ---------------------------------------------------------------------------


class PostWriter:
    def __init__(self, logger: logging.Logger):
        self.logger = logger

    def write(self, config: PostConfig, content: str) -> None:
        if not content.startswith("---"):
            raise PostWriteError(f"Content does not start with frontmatter: {config.slug}")

        POSTS_DIR.mkdir(parents=True, exist_ok=True)
        config.post_path.write_text(content, encoding="utf-8")
        self.logger.info(f"Written: {config.post_path.name}")


# ---------------------------------------------------------------------------
# GitPublisher
# ---------------------------------------------------------------------------


class GitPublisher:
    def __init__(self, repo_dir: Path, logger: logging.Logger):
        self.repo_dir = repo_dir
        self.logger = logger

    def _run(self, cmd: list, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            cmd,
            cwd=str(self.repo_dir),
            capture_output=True,
            text=True,
            check=check,
        )

    def publish(self, post_paths: list, image_paths: list) -> None:
        all_files = [str(p.relative_to(self.repo_dir)) for p in post_paths + image_paths]
        if not all_files:
            self.logger.warning("No files to publish")
            return

        try:
            self._run(["git", "add"] + all_files)

            staged = self._run(["git", "diff", "--cached", "--name-only"])
            if not staged.stdout.strip():
                self.logger.info("Nothing staged — skipping commit")
                return

            date_str = datetime.date.today().isoformat()
            msg = f"Auto-generate {len(post_paths)} posts for {date_str}"
            self._run(["git", "commit", "-m", msg])
            self.logger.info(f"Committed: {msg}")

            self._run(["git", "push", "origin", "master"])
            self.logger.info("Pushed to origin/master")

        except subprocess.CalledProcessError as e:
            raise GitPublishError(f"Git operation failed: {e.stderr.strip()}")


# ---------------------------------------------------------------------------
# BlogOrchestrator
# ---------------------------------------------------------------------------


class BlogOrchestrator:
    def __init__(self):
        self.logger = setup_logging()
        self.gist_mgr = GistManager(GITHUB_USER, self.logger)
        self.desc_generator = DescriptionGenerator(self.logger)
        self.generator = PostGenerator(self.logger)
        self.extractor = CodeExtractor(self.gist_mgr, self.logger)
        self.writer = PostWriter(self.logger)
        self.git = GitPublisher(BLOG_DIR, self.logger)

    def run(self) -> int:
        from topic_pool import load_history, save_history, mark_used, select_topics

        today = datetime.date.today()
        self.logger.info(f"=== Blog generation run: {today} ===")

        history = load_history(str(HISTORY_PATH))
        topics = select_topics(history, count=5)
        self.logger.info(f"Selected {len(topics)} topics")

        successful_posts: list[Path] = []
        successful_slugs: list[str] = []
        image_paths: list[Path] = []
        failed = 0

        for i, topic in enumerate(topics, 1):
            config = PostConfig.from_topic(topic, today)
            self.logger.info(f"[{i}/5] {config.slug}")

            try:
                config.description = self.desc_generator.generate(config)
                raw = self.generator.generate(config)

                if config.needs_code:
                    content = self.extractor.extract_and_replace(raw, config)
                else:
                    content = raw

                self.writer.write(config, content)
                successful_posts.append(config.post_path)
                successful_slugs.append(config.slug)

                if config.needs_diagram and config.diagram_path.exists():
                    image_paths.append(config.diagram_path)
                elif config.needs_diagram:
                    self.logger.warning(f"Diagram SVG missing: {config.diagram_path}")

            except Exception as e:
                failed += 1
                self.logger.error(f"Failed [{config.slug}]: {e}", exc_info=True)

        history = mark_used(history, successful_slugs)
        save_history(str(HISTORY_PATH), history)

        self.logger.info(
            f"Generation complete: {len(successful_posts)}/5 succeeded, {failed} failed"
        )

        if successful_posts:
            try:
                self.git.publish(successful_posts, image_paths)
            except GitPublishError as e:
                self.logger.error(f"Git publish failed: {e}")
                return 1

        return 0 if failed == 0 else 1


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    sys.path.insert(0, str(SCRIPTS_DIR))
    orchestrator = BlogOrchestrator()
    sys.exit(orchestrator.run())
