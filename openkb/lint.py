"""Structural lint checks for the OpenKB wiki.

Checks for:
- Broken [[wikilinks]] — link targets that don't exist
- Orphaned pages — pages with no incoming or outgoing links
- Missing wiki entries — raw files without corresponding sources/summaries
- Index sync — index.md links vs actual files on disk
- Citation coverage — concepts missing structured citations
- Source diversity — concepts with few sources
- Concept cluster isolation — groups not linked to broader wiki
- Book coverage — documents without concept links
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

# Matches [[wikilink]] or [[subdir/link]]
_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")

# Files to exclude from lint scanning (schema, logs, etc.)
_EXCLUDED_FILES = {"AGENTS.md", "SCHEMA.md", "log.md"}


class Severity(Enum):
    CRITICAL = "critical"
    WARNING = "warning"
    INFO = "info"


@dataclass
class LintIssue:
    severity: Severity
    category: str
    title: str
    detail: str
    location: str
    action: str
    sources: list[str] = field(default_factory=list)


def _parse_frontmatter_list(text: str, key: str) -> list[str]:
    """Parse a list value from YAML frontmatter (bracket or YAML list format)."""
    if not text.startswith("---"):
        return []
    end = text.find("---", 3)
    if end == -1:
        return []
    fm = text[3:end]
    bracket_match = re.search(rf'{key}:\s*\[(.*?)\]', fm)
    if bracket_match:
        items = bracket_match.group(1)
        return [i.strip().strip("'\"") for i in items.split(",") if i.strip()]
    lines = fm.split("\n")
    result = []
    in_list = False
    for line in lines:
        if line.strip().startswith(f"{key}:"):
            in_list = True
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


def _read_md(path: Path) -> str:
    """Read a Markdown file safely, returning empty string on error."""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _all_wiki_pages(wiki: Path) -> dict[str, Path]:
    """Return a mapping of stem/relative-path → absolute Path for all .md files.

    Keys are normalized: 'concepts/attention', 'summaries/paper', 'index', etc.
    """
    pages: dict[str, Path] = {}
    for md in wiki.rglob("*.md"):
        rel = md.relative_to(wiki)
        # Store both the full relative path without extension and the stem
        key = str(rel.with_suffix("")).replace("\\", "/")
        pages[key] = md
        # Also index by stem alone for convenience
        pages[md.stem] = md
    return pages


def _extract_wikilinks(text: str) -> list[str]:
    """Return all wikilink targets found in *text*.

    Handles ``[[target|display text]]`` alias syntax — only the target is returned.
    """
    raw = _WIKILINK_RE.findall(text)
    return [link.split("|")[0].strip() for link in raw]


def find_broken_links(wiki: Path) -> list[str]:
    """Scan all wiki pages for [[wikilinks]] pointing to non-existent targets.

    Args:
        wiki: Path to the wiki root directory.

    Returns:
        List of error strings describing each broken link.
    """
    pages = _all_wiki_pages(wiki)
    errors: list[str] = []

    for md in wiki.rglob("*.md"):
        if md.name in _EXCLUDED_FILES:
            continue
        # Skip reports/ and sources/ — auto-generated, not wiki content
        rel_parts = md.relative_to(wiki).parts
        if rel_parts and rel_parts[0] in ("reports", "sources"):
            continue
        text = _read_md(md)
        for target in _extract_wikilinks(text):
            # Normalise target: strip leading/trailing whitespace and slashes
            target_norm = target.strip().strip("/")
            # Check if target resolves as a key in our page map
            if target_norm not in pages:
                rel = md.relative_to(wiki)
                errors.append(f"Broken link [[{target}]] in {rel}")

    return sorted(errors)


def find_orphans(wiki: Path) -> list[str]:
    """Find pages that have no links to or from other pages.

    A page is orphaned if:
    - No other page links to it (no incoming links), AND
    - It has no outgoing wikilinks itself.

    index.md is excluded from orphan detection.

    Args:
        wiki: Path to the wiki root directory.

    Returns:
        List of relative page paths that are orphaned.
    """
    # Exclude index, schema, log, and sources/ (sources are auto-generated, not expected to be linked)
    all_mds = [
        p for p in wiki.rglob("*.md")
        if p.name not in {"index.md", *_EXCLUDED_FILES}
        and "sources" not in p.relative_to(wiki).parts
    ]
    if not all_mds:
        return []

    # Build outgoing links per page
    outgoing: dict[str, set[str]] = {}
    for md in all_mds:
        rel = str(md.relative_to(wiki).with_suffix("")).replace("\\", "/")
        text = _read_md(md)
        outgoing[rel] = set(_extract_wikilinks(text))

    # Build incoming link set (which pages are linked to)
    incoming: set[str] = set()
    for links in outgoing.values():
        for lnk in links:
            incoming.add(lnk.strip().strip("/"))
        # Also add stems
        for lnk in links:
            incoming.add(Path(lnk.strip()).stem)

    orphans: list[str] = []
    for rel, links in outgoing.items():
        stem = Path(rel).stem
        has_incoming = rel in incoming or stem in incoming
        has_outgoing = bool(links)
        if not has_incoming and not has_outgoing:
            orphans.append(rel)

    return sorted(orphans)


def find_missing_entries(raw: Path, wiki: Path) -> list[str]:
    """Find files in raw/ that have no corresponding wiki entries.

    A file is considered "present" if it has either a sources/ or summaries/
    page with the same stem.

    Args:
        raw: Path to the raw documents directory.
        wiki: Path to the wiki root directory.

    Returns:
        List of filenames in raw/ with no wiki entry.
    """
    sources_dir = wiki / "sources"
    summaries_dir = wiki / "summaries"

    sources_stems = {p.stem for p in sources_dir.glob("*.md")} if sources_dir.exists() else set()
    summary_stems = {p.stem for p in summaries_dir.glob("*.md")} if summaries_dir.exists() else set()
    known_stems = sources_stems | summary_stems

    missing: list[str] = []
    if raw.exists():
        for f in raw.iterdir():
            if f.is_file() and f.stem not in known_stems:
                missing.append(f.name)

    return sorted(missing)


def check_index_sync(wiki: Path) -> list[str]:
    """Compare index.md wikilinks against actual files on disk.

    Returns issues for:
    - Links in index.md pointing to non-existent pages
    - Pages in summaries/ or concepts/ not mentioned in index.md

    Args:
        wiki: Path to the wiki root directory.

    Returns:
        List of sync issue strings.
    """
    index_path = wiki / "index.md"
    issues: list[str] = []

    if not index_path.exists():
        return ["index.md does not exist"]

    index_text = _read_md(index_path)
    index_links = set(_extract_wikilinks(index_text))
    pages = _all_wiki_pages(wiki)

    # Check that all index links resolve
    for lnk in index_links:
        lnk_norm = lnk.strip().strip("/")
        if lnk_norm not in pages:
            issues.append(f"index.md links to missing page: [[{lnk}]]")

    # Check that summaries and concepts pages are mentioned in index
    index_stems = {Path(lnk.strip()).stem for lnk in index_links}
    index_text_lower = index_text.lower()

    for subdir in ("summaries", "concepts"):
        subdir_path = wiki / subdir
        if not subdir_path.exists():
            continue
        for md in sorted(subdir_path.glob("*.md")):
            stem = md.stem
            if stem not in index_stems and stem.lower() not in index_text_lower:
                issues.append(f"{subdir}/{stem}.md not mentioned in index.md")

    return sorted(issues)


def check_citation_coverage(wiki: Path) -> list[str]:
    """Check that concept pages with sources have proper citations.

    Args:
        wiki: Path to the wiki root directory.

    Returns:
        List of citation coverage issues.
    """
    concepts_dir = wiki / "concepts"
    if not concepts_dir.exists():
        return []

    issues: list[str] = []
    for md in concepts_dir.glob("*.md"):
        text = _read_md(md)
        has_sources = "sources:" in text
        has_citations = "citations:" in text
        has_section = "## Sources & Perspectives" in text

        if has_sources and not has_citations:
            issues.append(f"{md.stem}: has sources but no structured citations")
        if has_sources and not has_section:
            issues.append(f"{md.stem}: missing 'Sources & Perspectives' section")

    return sorted(issues)


def check_source_diversity(wiki: Path, min_sources: int = 2) -> list[LintIssue]:
    """Check that concepts have multiple sources for cross-book synthesis."""
    concepts_dir = wiki / "concepts"
    if not concepts_dir.exists():
        return []

    issues: list[LintIssue] = []
    for md in sorted(concepts_dir.glob("*.md")):
        text = _read_md(md)
        sources = _parse_frontmatter_list(text, "sources")
        if len(sources) < min_sources:
            issues.append(LintIssue(
                severity=Severity.WARNING,
                category="coverage",
                title=f"Low source coverage: {md.stem}",
                detail=f"Concept has {len(sources)} source(s), recommended ≥{min_sources}.",
                location=f"concepts/{md.name}",
                action="Add documents covering this topic for multiple perspectives.",
                sources=sources,
            ))
    return issues


def check_concept_clusters(wiki: Path) -> list[LintIssue]:
    """Find isolated clusters of concepts not linked to the broader wiki."""
    concepts_dir = wiki / "concepts"
    if not concepts_dir.exists():
        return []

    all_concepts: set[str] = set()
    internal_links: dict[str, set[str]] = {}
    external_links: dict[str, set[str]] = {}

    for md in concepts_dir.glob("*.md"):
        slug = md.stem
        all_concepts.add(slug)
        text = _read_md(md)
        links = set(_extract_wikilinks(text))
        internal_links[slug] = {l for l in links if l.startswith("concepts/")}
        external_links[slug] = {l for l in links if not l.startswith("concepts/")}

    visited: set[str] = set()
    clusters: list[set[str]] = []
    for concept in all_concepts:
        if concept in visited:
            continue
        cluster: set[str] = set()
        queue = [concept]
        while queue:
            node = queue.pop(0)
            if node in visited:
                continue
            visited.add(node)
            cluster.add(node)
            for linked in internal_links.get(node, set()):
                linked_slug = linked.replace("concepts/", "")
                if linked_slug in all_concepts:
                    queue.append(linked_slug)
        clusters.append(cluster)

    issues: list[LintIssue] = []
    for cluster in clusters:
        has_external = any(external_links.get(c, set()) for c in cluster)
        if not has_external and len(cluster) > 1:
            names = [f"[[concepts/{c}]]" for c in sorted(cluster)]
            issues.append(LintIssue(
                severity=Severity.INFO,
                category="isolation",
                title=f"Isolated concept cluster ({len(cluster)} concepts)",
                detail="Concepts only link to each other, not to summaries or other pages.",
                location=", ".join(names[:5]),
                action="Add cross-links to related summaries and concepts outside this cluster.",
            ))
    return issues


def check_book_coverage(wiki: Path) -> list[LintIssue]:
    """Check that document summaries link out to concept pages."""
    summaries_dir = wiki / "summaries"
    if not summaries_dir.exists():
        return []

    issues: list[LintIssue] = []
    for md in sorted(summaries_dir.glob("*.md")):
        text = _read_md(md)
        links = _extract_wikilinks(text)
        concept_links = [l for l in links if l.startswith("concepts/")]
        if not concept_links:
            issues.append(LintIssue(
                severity=Severity.WARNING,
                category="coverage",
                title=f"No concept links from summary: {md.stem}",
                detail="Document summary has no outgoing concept wikilinks.",
                location=f"summaries/{md.name}",
                action="Compile this document to generate concept pages.",
            ))
    return issues


def format_severity_report(issues: list[LintIssue], structural: str, semantic: str) -> str:
    """Format all lint results into severity-ordered markdown report."""
    critical = [i for i in issues if i.severity == Severity.CRITICAL]
    warnings = [i for i in issues if i.severity == Severity.WARNING]
    info = [i for i in issues if i.severity == Severity.INFO]

    lines = [
        "## Summary\n",
        f"| Severity | Count |",
        f"|----------|-------|",
        f"| Critical | {len(critical)} |",
        f"| Warning | {len(warnings)} |",
        f"| Info | {len(info)} |\n",
    ]

    if critical:
        lines.append("## Critical Issues\n")
        for issue in critical:
            lines.append(f"- **{issue.title}** ({issue.location}): {issue.detail}")

    if warnings:
        lines.append("\n## Warnings\n")
        for issue in warnings:
            lines.append(f"- **{issue.title}** ({issue.location}): {issue.detail}")

    if info:
        lines.append("\n## Info\n")
        for issue in info:
            lines.append(f"- **{issue.title}** ({issue.location}): {issue.detail}")

    lines.append(f"\n{structural}")
    lines.append(f"\n{semantic}")
    return "\n".join(lines)


def run_structural_lint(kb_dir: Path) -> str:
    """Run all structural lint checks and return a formatted Markdown report.

    Args:
        kb_dir: Root of the knowledge base (contains wiki/ and raw/).

    Returns:
        Formatted Markdown string with lint results.
    """
    wiki = kb_dir / "wiki"
    raw = kb_dir / "raw"

    broken = find_broken_links(wiki)
    orphans = find_orphans(wiki)
    missing = find_missing_entries(raw, wiki)
    sync_issues = check_index_sync(wiki)
    citation_issues = check_citation_coverage(wiki)

    lines = ["## Structural Lint Report\n"]

    # Broken links
    lines.append(f"### Broken Links ({len(broken)})")
    if broken:
        for issue in broken:
            lines.append(f"- {issue}")
    else:
        lines.append("No broken links found.")
    lines.append("")

    # Orphans
    lines.append(f"### Orphaned Pages ({len(orphans)})")
    if orphans:
        for page in orphans:
            lines.append(f"- {page}")
    else:
        lines.append("No orphaned pages found.")
    lines.append("")

    # Missing entries
    lines.append(f"### Raw Files Without Wiki Entry ({len(missing)})")
    if missing:
        for name in missing:
            lines.append(f"- {name}")
    else:
        lines.append("All raw files have wiki entries.")
    lines.append("")

    # Index sync
    lines.append(f"### Index Sync Issues ({len(sync_issues)})")
    if sync_issues:
        for issue in sync_issues:
            lines.append(f"- {issue}")
    else:
        lines.append("Index is in sync.")
    lines.append("")

    # Citation coverage
    lines.append(f"### Citation Coverage ({len(citation_issues)})")
    if citation_issues:
        for issue in citation_issues:
            lines.append(f"- {issue}")
    else:
        lines.append("All concepts have proper citations.")

    return "\n".join(lines)
