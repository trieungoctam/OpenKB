---
phase: 5
title: "Semantic Linting Enhancement"
status: pending
priority: P2
effort: "2d"
dependencies: [4]
---

# Phase 5: Semantic Linting Enhancement

## Overview

Enhance the linting system with severity levels, targeted checks, and structured output. Add code-based checks for citation coverage and source diversity, plus enhanced LLM-based semantic analysis for contradictions, knowledge gaps, and cross-reference quality.

## Requirements

- **Functional:**
  - Severity levels: `critical`, `warning`, `info`
  - Contradiction detection between concept pages
  - Knowledge gap analysis (book coverage vs wiki concepts)
  - Source diversity check (per-concept source count)
  - Cross-reference quality (isolated concept clusters)
  - Structured report with sections by severity
  - Citation coverage check (from Phase 4)

- **Non-functional:**
  - Critical issues should be deterministic (code-based) where possible
  - LLM-based checks are supplementary, not primary
  - Reports formatted for both CLI and wiki storage

## Architecture

### Current lint flow
```
run_lint() →
  structural_lint() (code-based) → broken links, orphans, missing entries, index sync
  knowledge_lint() (LLM agent) → contradictions, gaps, staleness, redundancy
```

### Enhanced lint flow
```
run_lint() →
  structural_lint() (enhanced, code-based) →
    + citation_coverage()
    + source_diversity()
    + concept_cluster_analysis()
  knowledge_lint() (enhanced LLM agent) →
    + severity levels
    + targeted contradiction detection
    + book-to-wiki coverage gaps
    + structured output format
  combine_reports() → severity-ordered markdown
```

### Report format
```markdown
# Lint Report — 20260506_143022

## Summary
| Severity | Count |
|----------|-------|
| Critical | 2     |
| Warning  | 5     |
| Info     | 3     |

## Critical Issues
### ❌ CRITICAL: Contradiction detected
**Concept:** [[concepts/neural-networks]]
**Conflict:** "Attention Is All You Need" states transformer layers use residual connections (p.3),
while "Deep Learning" states they use skip connections (p.425).
**Action:** These are equivalent terms — consider adding a clarification note.

## Warnings
### ⚠️ WARNING: Low source coverage
**Concept:** [[concepts/backpropagation]]
**Sources:** 1 (only "Deep Learning")
**Action:** Add more sources for comprehensive coverage.

## Info
### ℹ️ INFO: Isolated concept cluster
**Concepts:** [[concepts/cnn]], [[concepts/rnn]], [[concepts/lstm]]
**Issue:** These concepts only link to each other, not to the broader wiki.
**Action:** Consider adding cross-links to related concepts.

## Structural Issues
(Existing checks: broken links, orphans, etc.)
```

## Related Code Files

- **Modify:** `openkb/lint.py` — Add new code-based checks + severity helpers
- **Modify:** `openkb/agent/linter.py` — Enhanced prompt with severity + structured output
- **Modify:** `openkb/cli.py` — Update `run_lint()` for new report format
- **Create:** `openkb/lint-checks/` directory with modular check files

## Implementation Steps

### Step 1: Add severity system to `lint.py`

```python
from enum import Enum
from dataclasses import dataclass, field

class Severity(Enum):
    CRITICAL = "critical"
    WARNING = "warning"
    INFO = "info"

@dataclass
class LintIssue:
    severity: Severity
    category: str          # "contradiction", "gap", "coverage", "orphan", etc.
    title: str             # Short description
    detail: str            # Full explanation
    location: str          # File path or concept name
    action: str            # Suggested fix
    sources: list[str] = field(default_factory=list)  # Affected wiki pages

def format_lint_report(issues: list[LintIssue], structural: str, semantic: str) -> str:
    """Format all lint results into severity-ordered markdown report."""
    critical = [i for i in issues if i.severity == Severity.CRITICAL]
    warnings = [i for i in issues if i.severity == Severity.WARNING]
    info = [i for i in issues if i.severity == Severity.INFO]

    lines = [
        "# Lint Report\n",
        "## Summary\n",
        f"| Severity | Count |\n|----------|-------|\n| Critical | {len(critical)} |\n| Warning | {len(warnings)} |\n| Info | {len(info)} |\n",
    ]

    if critical:
        lines.append("## Critical Issues\n")
        for issue in critical:
            lines.append(_format_issue(issue))

    if warnings:
        lines.append("## Warnings\n")
        for issue in warnings:
            lines.append(_format_issue(issue))

    if info:
        lines.append("## Info\n")
        for issue in info:
            lines.append(_format_issue(issue))

    return "\n".join(lines)


def _format_issue(issue: LintIssue) -> str:
    icon = {"critical": "❌", "warning": "⚠️", "info": "ℹ️"}[issue.severity.value]
    lines = [
        f"### {icon} {issue.severity.value.upper()}: {issue.title}",
        f"**Category:** {issue.category}",
        f"**Location:** {issue.location}",
        f"**Detail:** {issue.detail}",
        f"**Action:** {issue.action}",
    ]
    if issue.sources:
        lines.append(f"**Sources:** {', '.join(issue.sources)}")
    lines.append("")
    return "\n".join(lines)
```

### Step 2: Add source diversity check

```python
def check_source_diversity(wiki: Path, min_sources: int = 2) -> list[LintIssue]:
    """Check that concepts have multiple sources for cross-book synthesis."""
    issues = []
    concepts_dir = wiki / "concepts"
    if not concepts_dir.exists():
        return issues

    for md in sorted(concepts_dir.glob("*.md")):
        text = md.read_text(encoding="utf-8")
        sources = _parse_frontmatter_list(text, "sources")

        if len(sources) < min_sources:
            issues.append(LintIssue(
                severity=Severity.WARNING,
                category="coverage",
                title=f"Low source coverage: {md.stem}",
                detail=f"Concept has {len(sources)} source(s), recommended ≥{min_sources} for cross-book synthesis.",
                location=f"concepts/{md.name}",
                action="Add documents covering this topic for multiple perspectives.",
                sources=sources,
            ))

    return issues
```

### Step 3: Add concept cluster analysis

```python
def check_concept_clusters(wiki: Path) -> list[LintIssue]:
    """Find isolated clusters of concepts that don't link to the broader wiki."""
    concepts_dir = wiki / "concepts"
    if not concepts_dir.exists():
        return []

    # Build concept graph
    all_concepts = set()
    internal_links = {}  # concept → set of linked concepts
    external_links = {}  # concept → set of linked non-concept pages

    for md in concepts_dir.glob("*.md"):
        slug = md.stem
        all_concepts.add(slug)
        text = md.read_text(encoding="utf-8")
        links = set(_extract_wikilinks(text))
        internal_links[slug] = {l for l in links if l.startswith("concepts/")}
        external_links[slug] = {l for l in links if not l.startswith("concepts/")}

    # Find clusters via connected components
    visited = set()
    clusters = []
    for concept in all_concepts:
        if concept in visited:
            continue
        cluster = set()
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

    # Flag clusters with no external links
    issues = []
    for cluster in clusters:
        has_external = any(
            external_links.get(c, set())
            for c in cluster
        )
        if not has_external and len(cluster) > 1:
            names = [f"[[concepts/{c}]]" for c in sorted(cluster)]
            issues.append(LintIssue(
                severity=Severity.INFO,
                category="isolation",
                title=f"Isolated concept cluster ({len(cluster)} concepts)",
                detail=f"Concepts only link to each other, not to summaries or other wiki pages.",
                location=", ".join(names[:5]),
                action="Add cross-links to related summaries and concepts outside this cluster.",
            ))

    return issues
```

### Step 4: Add book coverage check

```python
def check_book_coverage(wiki: Path) -> list[LintIssue]:
    """Compare document summaries against concept coverage.

    For each document, check if its major themes are represented in wiki concepts.
    """
    issues = []
    summaries_dir = wiki / "summaries"
    concepts_dir = wiki / "concepts"
    if not summaries_dir.exists() or not concepts_dir.exists():
        return issues

    # Get all concept slugs (lowercase for matching)
    concept_slugs = {md.stem.lower() for md in concepts_dir.glob("*.md")}

    # Check each summary has related concepts
    for md in sorted(summaries_dir.glob("*.md")):
        text = md.read_text(encoding="utf-8")
        # Extract wikilinks from summary
        summary_links = _extract_wikilinks(text)
        concept_links = [l for l in summary_links if l.startswith("concepts/")]

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
```

### Step 5: Enhance LLM linter prompt

Update `_LINTER_INSTRUCTIONS_TEMPLATE` in `agent/linter.py`:

```python
_LINTER_INSTRUCTIONS_TEMPLATE = """\
You are OpenKB's semantic lint agent. Your job is to audit the wiki
for quality issues that structural tools cannot detect.

{schema_md}

## Output Format (MANDATORY)

Return a structured report in this EXACT format:

### [CRITICAL] Issue Title
- **Category:** contradiction | gap | error
- **Location:** page path
- **Detail:** what's wrong
- **Evidence:** quote from wiki showing the issue
- **Action:** how to fix

### [WARNING] Issue Title
- **Category:** redundancy | coverage | stale
- **Location:** page path
- **Detail:** what could be improved
- **Action:** suggestion

### [INFO] Issue Title
- **Category:** suggestion | style
- **Location:** page path
- **Detail:** minor improvement opportunity
- **Action:** optional enhancement

## Checks to perform

### Critical (must fix)
1. **Contradictions** — Do any pages make conflicting claims about the same fact?
   Cite exact passages that disagree. Check especially across concept pages
   that cite different source documents.

### Warning (should fix)
2. **Knowledge gaps** — Are there obvious topics mentioned in summaries but
   missing concept pages? Compare document summaries with existing concepts.
3. **Redundancy** — Are there concept pages covering the same ground that
   could be merged? Look for similar brief descriptions.
4. **Source conflicts** — Do any concepts cite sources that present conflicting
   perspectives without acknowledging the disagreement?
5. **Coverage imbalance** — Are some source documents over/under-represented
   in the wiki concepts?

### Info (nice to have)
6. **Concept quality** — Are concept pages well-structured with clear explanations?
7. **Cross-reference richness** — Do concepts have enough wikilinks to related topics?
8. **Staleness** — Are there references to outdated information?

## Process
1. Start with index.md to understand scope.
2. Read ALL summary pages to understand each document's content.
3. Read ALL concept pages to check for contradictions and gaps.
4. Cross-reference: for each concept, check if its cited sources agree.
5. Produce the structured report using EXACTLY the format above.

Be thorough but concise. If no issues in a category, omit that category entirely.
"""
```

### Step 6: Update `run_lint()` in CLI

```python
async def run_lint(kb_dir: Path) -> Path | None:
    """Run enhanced structural + semantic lint."""
    from openkb.lint import (
        run_structural_lint, check_source_diversity,
        check_concept_clusters, check_book_coverage,
        check_citation_coverage, format_lint_report, LintIssue,
    )
    from openkb.agent.linter import run_knowledge_lint

    # ... existing hash check ...

    # Code-based checks (fast, deterministic)
    structural_report = run_structural_lint(kb_dir)
    code_issues: list[LintIssue] = []
    code_issues.extend(check_source_diversity(wiki))
    code_issues.extend(check_concept_clusters(wiki))
    code_issues.extend(check_book_coverage(wiki))
    code_issues.extend(check_citation_coverage(wiki))

    # LLM-based checks (slower, supplementary)
    knowledge_report = await run_knowledge_lint(kb_dir, model)

    # Combine into structured report
    full_report = format_lint_report(code_issues, structural_report, knowledge_report)

    # Write report
    # ...
```

### Step 7: Add min-wiki-size threshold

Skip semantic lint for tiny wikis (< 3 documents):

```python
if len(hashes) < 3:
    click.echo("Wiki too small for semantic lint (< 3 documents). Running structural lint only.")
    structural_report = run_structural_lint(kb_dir)
    # ... write structural only ...
    return
```

## Success Criteria

- [ ] Lint issues have severity levels (critical/warning/info)
- [ ] Source diversity check flags concepts with < 2 sources
- [ ] Concept cluster analysis detects isolated groups
- [ ] Book coverage check finds documents without concept links
- [ ] Citation coverage check (from Phase 4) integrated
- [ ] LLM linter returns structured severity-labeled output
- [ ] Reports formatted with severity sections and icons
- [ ] Tiny wiki threshold skips semantic lint
- [ ] Existing structural checks unchanged
- [ ] New tests for all code-based checks
- [ ] Existing tests pass

## Risk Assessment

| Risk | Impact | Mitigation |
|------|--------|------------|
| LLM lint non-deterministic | Medium | Code-based checks for critical issues; LLM for supplementary |
| False positives in cluster analysis | Low | Only flag clusters with >1 concept and NO external links |
| Slow lint on large wikis | Medium | Limit pages read; LLM lint reads summaries first, concepts selectively |
| Report too verbose | Low | Summary table at top; issues grouped by severity |
