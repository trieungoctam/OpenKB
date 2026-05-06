"""Frontmatter enrichment utilities for upgrading existing wiki pages."""
from __future__ import annotations

import logging
import os
from datetime import date
from pathlib import Path

logger = logging.getLogger(__name__)

_SKIP_FILES = {"index.md", "log.md", "agents.md"}


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Parse YAML frontmatter from markdown text. Returns (fields, body)."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("---", 3)
    if end == -1:
        return {}, text
    fm_text = text[3:end].strip()
    body = text[end + 3:].lstrip("\n")
    fields: dict[str, str] = {}
    for line in fm_text.split("\n"):
        line = line.strip()
        if ":" in line:
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip()
    return fields, body


def _build_frontmatter(fields: dict[str, str]) -> str:
    """Build YAML frontmatter block from fields dict."""
    lines = [f"{k}: {v}" for k, v in fields.items() if v is not None]
    return "---\n" + "\n".join(lines) + "\n---\n\n"


def enrich_frontmatter(filepath: Path, page_type: str) -> bool:
    """Add missing frontmatter fields to a wiki page. Returns True if changed."""
    if filepath.name.lower() in _SKIP_FILES:
        return False

    text = filepath.read_text(encoding="utf-8")
    fields, body = _parse_frontmatter(text)

    changed = False

    # Add type if missing
    if "type" not in fields:
        fields["type"] = page_type
        changed = True

    # Add title if missing — derive from filename
    if "title" not in fields:
        title = filepath.stem.replace("-", " ").replace("_", " ")
        title = title.replace('"', '\\"').replace("\n", " ")
        fields["title"] = f'"{title}"'
        changed = True

    # Add date if missing — use file mtime as approximation
    if "date" not in fields:
        mtime = os.path.getmtime(filepath)
        file_date = date.fromtimestamp(mtime).isoformat()
        fields["date"] = file_date
        changed = True

    # Add last_updated if missing — same as date for initial enrichment
    if "last_updated" not in fields:
        fields["last_updated"] = fields.get("date", date.today().isoformat())
        changed = True

    # Tracking fields for concept pages (Obsidian study plugin)
    if page_type == "concept":
        if "understanding_level" not in fields:
            fields["understanding_level"] = "unreviewed"
            changed = True
        if "last_reviewed" not in fields:
            fields["last_reviewed"] = "null"
            changed = True
        if "review_count" not in fields:
            fields["review_count"] = "0"
            changed = True

    if not changed:
        return False

    new_text = _build_frontmatter(fields) + body
    filepath.write_text(new_text, encoding="utf-8")
    return True


def enrich_directory(directory: Path, page_type: str) -> int:
    """Enrich all markdown files in a directory. Returns count of enriched files."""
    if not directory.exists():
        return 0
    count = 0
    for f in directory.glob("*.md"):
        if f.name.lower() in _SKIP_FILES:
            continue
        if enrich_frontmatter(f, page_type):
            count += 1
            logger.info("Enriched: %s", f.name)
    return count
