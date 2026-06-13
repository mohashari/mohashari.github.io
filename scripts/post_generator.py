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
- Include exactly one architecture diagram
- You can generate the diagram SVG content directly using Python/Bash or any available drawing tools
- Save the resulting SVG content to: {svg_path}
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

        cmd = ["agy", "-p", prompt, "--dangerously-skip-permissions", "--print-timeout", f"{timeout}s"]
        if getattr(config, "MODEL", None):
            cmd.extend(["--model", config.MODEL])

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout + 20,
            cwd=str(config.BLOG_DIR),
        )

        if result.returncode != 0:
            raise PostGenerationError(
                f"agy -p failed (exit {result.returncode}): {result.stderr[:300]}"
            )

        cleaned_output = config.clean_agy_output(result.stdout)
        if not cleaned_output:
            raise PostGenerationError("agy -p returned empty output")

        return strip_preamble(cleaned_output)
