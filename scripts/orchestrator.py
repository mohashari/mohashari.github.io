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
