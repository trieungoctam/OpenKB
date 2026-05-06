"""Concept merging for segments of the same document.

After splitting a large PDF into segments and compiling each independently,
duplicate concepts may appear across segments. This module detects and merges
them into unified concept pages.
"""
from __future__ import annotations

import difflib
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# Similarity threshold for name-based deduplication
_SIMILARITY_THRESHOLD = 0.8


def merge_segment_concepts(wiki_dir: Path, _doc_name: str = "") -> dict:
    """Merge concepts generated from segments of the same document.

    Detects duplicate concepts (same/similar name) across segments and:
    1. Keeps the most comprehensive version (longest content)
    2. Merges sources lists
    3. Deduplicates wikilinks
    4. Removes redundant concept files

    Args:
        wiki_dir: Path to the wiki directory.
        _doc_name: Parent document name (reserved for future scope filtering).

    Returns:
        Stats dict: {"merged": N, "kept": N, "total": N}
    """
    concepts_dir = wiki_dir / "concepts"
    if not concepts_dir.exists():
        return {"merged": 0, "kept": 0, "total": 0}

    # Load all concept pages
    concepts = _load_concepts(concepts_dir)
    if not concepts:
        return {"merged": 0, "kept": 0, "total": 0}

    # Find duplicate groups by name similarity
    groups = _find_duplicate_groups(concepts)

    merged = 0
    for group in groups:
        if len(group) <= 1:
            continue
        _merge_group(group, concepts_dir, wiki_dir)
        merged += 1

    total = len(concepts)
    kept = total - merged
    return {"merged": merged, "kept": kept, "total": total}


def _load_concepts(concepts_dir: Path) -> dict[str, dict]:
    """Load all concept pages with metadata.

    Returns: {slug: {"path": Path, "content": str, "sources": list, "body": str}}
    """
    concepts = {}
    for md in concepts_dir.glob("*.md"):
        text = md.read_text(encoding="utf-8")
        sources = _parse_frontmatter_list(text, "sources")
        body = _strip_frontmatter(text)
        concepts[md.stem] = {
            "path": md,
            "content": text,
            "sources": sources,
            "body": body,
        }
    return concepts


def _parse_frontmatter_list(text: str, key: str) -> list[str]:
    """Parse a list value from YAML frontmatter.

    Handles: key: [item1, item2] and key:\n  - item1\n  - item2
    """
    if not text.startswith("---"):
        return []
    end = text.find("---", 3)
    if end == -1:
        return []
    fm = text[3:end]

    # Try bracket format: sources: [a, b]
    bracket_match = re.search(rf'{key}:\s*\[(.*?)\]', fm)
    if bracket_match:
        items = bracket_match.group(1)
        return [i.strip().strip("'\"") for i in items.split(",") if i.strip()]

    # Try YAML list format: sources:\n  - a\n  - b
    lines = fm.split("\n")
    result = []
    in_list = False
    for line in lines:
        if line.strip().startswith(f"{key}:"):
            in_list = True
            # Check if value is on same line (non-bracket)
            after_colon = line.split(":", 1)[1].strip()
            if after_colon and after_colon != "[]":
                return [after_colon.strip("'\"")]
            continue
        if in_list:
            if line.strip().startswith("- "):
                result.append(line.strip()[2:].strip("'\""))
            elif line.startswith("  ") or line.strip() == "":
                continue
            else:
                break
    return result


def _strip_frontmatter(text: str) -> str:
    """Remove YAML frontmatter from text."""
    if text.startswith("---"):
        end = text.find("---", 3)
        if end != -1:
            return text[end + 3:].lstrip("\n")
    return text


def _find_duplicate_groups(concepts: dict[str, dict]) -> list[list[str]]:
    """Group concept slugs by name similarity.

    Uses difflib.SequenceMatcher for pairwise comparison.
    Returns list of groups (each group is a list of slugs).
    """
    slugs = list(concepts.keys())
    assigned = set()
    groups = []

    for i, slug_a in enumerate(slugs):
        if slug_a in assigned:
            continue
        group = [slug_a]
        assigned.add(slug_a)

        for j, slug_b in enumerate(slugs):
            if j <= i or slug_b in assigned:
                continue
            # Check name similarity
            ratio = difflib.SequenceMatcher(None, slug_a.lower(), slug_b.lower()).ratio()
            if ratio >= _SIMILARITY_THRESHOLD:
                group.append(slug_b)
                assigned.add(slug_b)

        groups.append(group)

    return groups


def _merge_group(group: list[str], concepts_dir: Path, wiki_dir: Path) -> None:
    """Merge a group of similar concepts into one.

    Strategy: keep the longest content, merge sources from all.
    """
    # Load all concepts in the group
    members = []
    for slug in group:
        md = concepts_dir / f"{slug}.md"
        if md.exists():
            members.append({"slug": slug, "path": md, "text": md.read_text(encoding="utf-8")})

    if len(members) <= 1:
        return

    # Sort by content length (longest first = keep)
    members.sort(key=lambda m: len(_strip_frontmatter(m["text"])), reverse=True)
    keeper = members[0]
    duplicates = members[1:]

    # Merge sources into keeper
    all_sources = set()
    for m in members:
        sources = _parse_frontmatter_list(m["text"], "sources")
        all_sources.update(sources)

    if all_sources:
        _update_frontmatter_sources(keeper["path"], keeper["text"], sorted(all_sources))

    # Update index.md to point to keeper slug
    _update_index_references(wiki_dir, group, keeper["slug"])

    # Remove duplicate files
    for dup in duplicates:
        try:
            dup["path"].unlink()
            logger.info("Merged duplicate concept: %s → %s", dup["slug"], keeper["slug"])
        except OSError:
            logger.warning("Failed to remove duplicate concept: %s", dup["slug"])


def _update_frontmatter_sources(path: Path, text: str, sources: list[str]) -> None:
    """Update sources list in frontmatter."""
    if not text.startswith("---"):
        return
    end = text.find("---", 3)
    if end == -1:
        return

    fm = text[:end + 3]
    body = text[end + 3:]

    sources_str = ", ".join(sources)
    if "sources:" in fm:
        # Replace existing sources line
        fm = re.sub(r'sources:.*', f'sources: [{sources_str}]', fm)
    else:
        fm = fm.replace("---\n", f"---\nsources: [{sources_str}]\n", 1)

    path.write_text(fm + body, encoding="utf-8")


def merge_cross_doc_concepts(wiki_dir: Path) -> dict:
    """Merge overlapping concepts from different documents.

    After parallel compilation, multiple documents may create similar concept
    pages.  This detects concepts with similar names that originated from
    different source documents and merges them into a single comprehensive page.

    Skips groups where all members share the exact same source list (already
    handled by segment merging).

    Args:
        wiki_dir: Path to the wiki directory.

    Returns:
        Stats dict: ``{"merged": N, "kept": N, "total": N}``
    """
    concepts_dir = wiki_dir / "concepts"
    if not concepts_dir.exists():
        return {"merged": 0, "kept": 0, "total": 0}

    concepts = _load_concepts(concepts_dir)
    if not concepts:
        return {"merged": 0, "kept": 0, "total": 0}

    groups = _find_duplicate_groups(concepts)

    merged = 0
    for group in groups:
        if len(group) <= 1:
            continue

        members = [concepts[s] for s in group if s in concepts]

        # Only merge if concepts come from different documents
        source_sets = [set(m["sources"]) for m in members]
        if len(source_sets) < 2:
            continue

        # All identical sources → same doc, skip (handled by segment merger)
        if all(s == source_sets[0] for s in source_sets):
            continue

        _merge_group(group, concepts_dir, wiki_dir)
        merged += 1

    total = len(concepts)
    kept = total - merged
    return {"merged": merged, "kept": kept, "total": total}


def _update_index_references(wiki_dir: Path, all_slugs: list[str], keeper_slug: str) -> None:
    """Update index.md to replace duplicate concept links with keeper."""
    index_path = wiki_dir / "index.md"
    if not index_path.exists():
        return

    text = index_path.read_text(encoding="utf-8")
    for slug in all_slugs:
        if slug == keeper_slug:
            continue
        # Replace references to duplicate slug with keeper
        text = text.replace(f"[[concepts/{slug}]]", f"[[concepts/{keeper_slug}]]")

    # Remove duplicate lines
    lines = text.split("\n")
    seen = set()
    deduped = []
    for line in lines:
        if line.startswith("- [[concepts/"):
            if line in seen:
                continue
            seen.add(line)
        deduped.append(line)

    index_path.write_text("\n".join(deduped), encoding="utf-8")
