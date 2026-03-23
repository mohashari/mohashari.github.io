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
        try:
            result = subprocess.run(
                cmd,
                cwd=str(self.repo_dir),
                capture_output=True,
                text=True,
                timeout=120,
            )
        except subprocess.TimeoutExpired as e:
            raise GitPublishError(f"Command {cmd[0]} timed out after 120s") from e
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

        staged = self._run(["git", "diff", "--cached", "--name-only"])
        if not staged.stdout.strip():
            self.logger.info("Nothing staged — skipping commit")
            return

        n = len(post_paths)
        msg = f"Auto-generate {n} posts for {date_str}"
        self._run(["git", "commit", "-m", msg])
        self.logger.info(f"Committed: {msg}")

        self._run(["git", "push", "origin", "master"])
        self.logger.info("Pushed to origin/master")
