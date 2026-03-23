# scripts/config.py
from pathlib import Path

BLOG_DIR = Path("/home/muklis/Documents/exploring/blog")
POSTS_DIR = BLOG_DIR / "_posts"
IMAGES_DIR = BLOG_DIR / "images" / "diagrams"
SCRIPTS_DIR = BLOG_DIR / "scripts"
LOG_DIR = SCRIPTS_DIR / "logs"
HISTORY_PATH = SCRIPTS_DIR / "topics_history.json"

GITHUB_USER = "mohashari"
POSTS_PER_RUN = 5
CATEGORIES = ["software_engineering", "development", "devsecops", "ai_engineering"]

TIMEOUT_WITH_DIAGRAM = 600   # seconds
TIMEOUT_TEXT_ONLY = 400      # seconds

UNSPLASH_API_BASE = "https://api.unsplash.com"

ALLOWED_TOOLS = (
    "Bash,"
    "mcp__claude_ai_Excalidraw__export_to_excalidraw,"
    "mcp__claude_ai_Excalidraw__create_view,"
    "mcp__claude_ai_Excalidraw__save_checkpoint"
)
