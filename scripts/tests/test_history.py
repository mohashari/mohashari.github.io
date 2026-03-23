# scripts/tests/test_history.py
import json
import pytest
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

import history


def test_load_missing_file(tmp_path):
    result = history.load(tmp_path / "missing.json")
    assert result == {"used": [], "last_updated": None}


def test_load_existing_file(tmp_path):
    p = tmp_path / "h.json"
    p.write_text(json.dumps({"used": ["slug-a"], "last_updated": "2026-01-01"}))
    result = history.load(p)
    assert result["used"] == ["slug-a"]
    assert result["last_updated"] == "2026-01-01"


def test_append_and_save_new_file(tmp_path):
    p = tmp_path / "h.json"
    history.append_and_save(p, "slug-one")
    data = json.loads(p.read_text())
    assert "slug-one" in data["used"]
    assert data["last_updated"] is not None


def test_append_and_save_no_duplicates(tmp_path):
    p = tmp_path / "h.json"
    history.append_and_save(p, "slug-one")
    history.append_and_save(p, "slug-one")
    data = json.loads(p.read_text())
    assert data["used"].count("slug-one") == 1


def test_append_and_save_preserves_existing(tmp_path):
    p = tmp_path / "h.json"
    history.append_and_save(p, "slug-one")
    history.append_and_save(p, "slug-two")
    data = json.loads(p.read_text())
    assert "slug-one" in data["used"]
    assert "slug-two" in data["used"]


def test_last_n_slugs_returns_recent(tmp_path):
    p = tmp_path / "h.json"
    for i in range(60):
        history.append_and_save(p, f"slug-{i}")
    h = history.load(p)
    recent = history.last_n_slugs(h, n=50)
    assert len(recent) == 50


def test_last_n_slugs_empty(tmp_path):
    h = history.load(tmp_path / "missing.json")
    assert history.last_n_slugs(h) == []
