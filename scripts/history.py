# scripts/history.py
import datetime
import json
from pathlib import Path


def load(path: Path) -> dict:
    """Return history dict with 'used' list and 'last_updated'."""
    if not path.exists():
        return {"used": [], "last_updated": None}
    try:
        with open(path) as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        raise ValueError(f"History file is corrupt ({path}): {e}") from e


def append_and_save(path: Path, slug: str) -> None:
    """Load current history, append slug if new, save immediately. Crash-safe per-post update."""
    data = load(path)
    used = list(data.get("used", []))
    if slug not in used:
        used.append(slug)
    data["used"] = used
    data["last_updated"] = datetime.date.today().isoformat()
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def last_n_slugs(history: dict, n: int = 50) -> list:
    """Return the most recent n slugs for deduplication prompt context."""
    used = history.get("used", [])
    return used[-n:]
