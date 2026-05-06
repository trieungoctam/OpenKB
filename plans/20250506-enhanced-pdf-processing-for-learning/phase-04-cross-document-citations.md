---
phase: 4
title: "Cross-Document Citations"
status: pending
priority: P1
effort: "3d"
dependencies: [2]
---

# Phase 4: Cross-Document Citations

## Overview

Add structured source citations to concept pages and inline source attribution in query answers. Enables cross-book synthesis: users see which book a concept comes from, compare perspectives, and detect conflicting information across sources.

## Requirements

- **Functional:**
  - Concept pages show structured citations (book name, pages, chapter)
  - Multiple sources listed when concept spans multiple books
  - Query answers include inline source references
  - Conflicting perspectives between sources highlighted
  - Citation data stored in YAML frontmatter for programmatic access

- **Non-functional:**
  - Citations use existing wiki page metadata (no new storage needed)
  - Frontmatter format: `citations: [{book, pages, chapter, perspective}]`
  - Query agent prompt enforces citation style

## Architecture

### Current concept frontmatter
```yaml
---
sources: [summaries/book-a.md, summaries/book-b.md]
brief: "Attention mechanism enables..."
---
```

### New concept frontmatter
```yaml
---
sources: [summaries/book-a.md, summaries/book-b.md]
brief: "Attention mechanism enables..."
citations:
  - book: "Attention Is All You Need"
    pages: "3-8"
    chapter: "3 Model Architecture"
    perspective: "Introduces multi-head attention as core mechanism"
  - book: "Deep Learning"
    pages: "420-435"
    chapter: "12.3 Attention Mechanisms"
    perspective: "Explains attention as general-purpose sequence alignment"
---
```

### Concept page body (new section)
```markdown
## Sources & Perspectives

### Attention Is All You Need (pp.3-8)
Introduces multi-head attention as the core mechanism replacing recurrent layers.

### Deep Learning (pp.420-435)
Explains attention as a general-purpose sequence alignment tool with broader applications.

> **Note:** These sources present different perspectives — the first focuses on architectural innovation, the second on general mathematical framework.
```

### Query answer with citations
```
The attention mechanism works by computing weighted sums of value vectors,
where weights are determined by query-key compatibility. [Source: [[summaries/attention-is-all-you-need]], Ch.3]

Different implementations exist: multi-head attention splits into parallel
attention heads [Source: [[summaries/attention-is-all-you-need]], pp.3-5],
while additive attention uses a feed-forward network [Source: [[summaries/deep-learning]], pp.425-427].
```

## Related Code Files

- **Modify:** `openkb/agent/compiler.py` — Update prompt templates for citation generation
- **Modify:** `openkb/agent/compiler.py` — Update `_write_concept()` for citations frontmatter
- **Modify:** `openkb/agent/query.py` — Update query instructions for inline citations
- **Modify:** `openkb/agent/linter.py` — Add citation coverage check
- **Modify:** `openkb/schema.py` — Update AGENTS.md with citation conventions

## Implementation Steps

### Step 1: Update concept generation prompt

Modify `_CONCEPT_PAGE_USER` in `compiler.py`:

```python
_CONCEPT_PAGE_USER = """\
Write the concept page for: {title}

This concept relates to the document "{doc_name}" summarized above.
{update_instruction}

IMPORTANT: Include a "## Sources & Perspectives" section at the end that:
1. Lists each source document with specific page/chapter references
2. Summarizes that source's perspective on this concept
3. Notes any differences in emphasis or approach between sources
4. Uses format: ### Source Title (pp.X-Y) followed by perspective summary

{citation_context}

Return a JSON object with three keys:
- "brief": A single sentence (under 100 chars) defining this concept
- "citations": An array of objects, each with keys:
  - "book": source document name
  - "pages": page range as string (e.g. "42-55") or empty string
  - "chapter": chapter/section name or empty string
  - "perspective": 1-2 sentence summary of this source's view
- "content": The full concept page in Markdown including the Sources & Perspectives section

Return ONLY valid JSON, no fences.
"""
```

### Step 2: Update concept update prompt

Modify `_CONCEPT_UPDATE_USER`:

```python
_CONCEPT_UPDATE_USER = """\
Update the concept page for: {title}

Current content of this page:
{existing_content}

New information from document "{doc_name}" (summarized above) should be
integrated into this page. Rewrite the full page incorporating the new
information naturally — do not just append.

IMPORTANT CITATION RULES:
1. Preserve ALL existing citations in the "## Sources & Perspectives" section
2. ADD a new citation entry for "{doc_name}" with page/chapter references
3. If the new source conflicts with existing sources, add a "> **Note:**" callout
4. Maintain existing [[wikilinks]] and add new ones where appropriate

Return a JSON object with three keys:
- "brief": A single sentence (under 100 chars) defining this concept (may differ from before)
- "citations": Complete array of citation objects (existing + new)
- "content": The rewritten full concept page in Markdown

Return ONLY valid JSON, no fences.
"""
```

### Step 3: Update `_write_concept()` for citations

```python
def _write_concept(wiki_dir, name, content, source_file, is_update, brief="", citations=None):
    """Write or update a concept page, managing citations frontmatter."""
    # ... existing logic for sources ...

    # Add citations to frontmatter
    if citations:
        if existing.startswith("---"):
            # ... existing frontmatter logic ...
            citations_yaml = yaml.dump({"citations": citations}, allow_unicode=True)
            # Insert citations after sources line
            # ...
        else:
            fm_lines.append(f"citations: {json.dumps(citations)}")
```

### Step 4: Parse citations from LLM response

Update concept parsing in `_compile_concepts()`:

```python
async def _gen_create(concept: dict) -> tuple[str, str, bool, str, list]:
    # ... existing ...
    try:
        parsed = _parse_json(raw)
        brief = parsed.get("brief", "")
        content = parsed.get("content", raw)
        citations = parsed.get("citations", [])  # NEW
    except (json.JSONDecodeError, ValueError):
        brief, content, citations = "", raw, []
    return name, content, False, brief, citations  # NEW: include citations
```

Update caller to pass citations to `_write_concept()`:

```python
for r in results:
    name, page_content, is_update, brief, citations = r  # NEW
    _write_concept(wiki_dir, name, page_content, source_file, is_update,
                   brief=brief, citations=citations)  # NEW
```

### Step 5: Add citation context from segment info

When compiling segments (Phase 1), include segment metadata in prompt:

```python
# In compile_short_doc, build citation context:
citation_context = ""
if segment_context:
    citation_context = f"""\
Citation context for this document:
- Book: {doc_name}
- Chapter: {segment_context.chapter_title}
- Pages: {segment_context.start_page}-{segment_context.end_page}
Reference these in your citations.
"""
```

### Step 6: Update query agent instructions

Modify `_QUERY_INSTRUCTIONS_TEMPLATE` in `query.py`:

```python
_QUERY_INSTRUCTIONS_TEMPLATE = """\
You are OpenKB, a knowledge-base Q&A agent. You answer questions by searching the wiki.

{schema_md}

## Search strategy
1. Read index.md to see all documents and concepts with brief summaries.
2. Read relevant summary pages (summaries/) for document overviews.
3. Read concept pages (concepts/) for cross-document synthesis.
4. When you need detailed source document content, use read_file or get_page_content.
5. Source content may reference images — use get_image tool to view them.
6. Synthesize a clear, concise answer grounded in wiki content.

## Citation requirements (IMPORTANT)
- Every factual claim must include a source citation
- Format: [Source: [[summaries/doc-name]], pp.X-Y] or [Source: [[summaries/doc-name]], Ch.Name]
- When multiple sources agree, cite all: [Source: [[summaries/book-a]], [[summaries/book-b]]]
- When sources disagree, note the conflict: "Book A states X [Source: ...], while Book B argues Y [Source: ...]"
- If a concept page has a "## Sources & Perspectives" section, use it for citation info
- If no specific page numbers available, use chapter/section references

Answer based only on wiki content. Be concise.
Before each tool call, output one short sentence explaining the reason.

If you cannot find relevant information, say so clearly.
"""
```

### Step 7: Update AGENTS.md schema

Add citation conventions to `openkb/schema.py`:

```python
AGENTS_MD = """\
# Wiki Schema

## Directory Structure
...

## Citation Conventions
- Concept pages include structured citations in frontmatter
- Each citation has: book, pages, chapter, perspective
- Concept pages have "## Sources & Perspectives" section
- Multiple perspectives are compared and conflicts noted
- Query answers include inline [Source: ...] citations
...
"""
```

### Step 8: Add citation coverage to lint

Add to `openkb/lint.py`:

```python
def check_citation_coverage(wiki: Path) -> list[str]:
    """Check that concept pages have proper citations."""
    issues = []
    concepts_dir = wiki / "concepts"
    if not concepts_dir.exists():
        return issues

    for md in concepts_dir.glob("*.md"):
        text = md.read_text(encoding="utf-8")
        # Check for citations frontmatter
        if "citations:" not in text and "sources:" in text:
            issues.append(f"{md.stem}: has sources but no structured citations")
        # Check for Sources & Perspectives section
        if "## Sources & Perspectives" not in text and "sources:" in text:
            issues.append(f"{md.stem}: missing 'Sources & Perspectives' section")
        # Check for inline citations in content
        if text.count("[[summaries/") < 2:
            issues.append(f"{md.stem}: few or no source wikilinks in content")

    return sorted(issues)
```

## Success Criteria

- [ ] Concept pages include `citations` in YAML frontmatter
- [ ] Each citation has book, pages, chapter, perspective fields
- [ ] Concept pages include "## Sources & Perspectives" section
- [ ] Multiple sources listed when concept spans multiple books
- [ ] Conflicting perspectives flagged with `> **Note:**` callout
- [ ] Query answers include inline [Source: ...] citations
- [ ] Lint check reports citation coverage issues
- [ ] AGENTS.md updated with citation conventions
- [ ] Existing tests pass
- [ ] New tests for citation parsing, frontmatter, query citations

## Risk Assessment

| Risk | Impact | Mitigation |
|------|--------|------------|
| LLM hallucinates page numbers | High | Validate against segment page ranges; use chapter refs when pages unreliable |
| Citations bloat concept pages | Low | Keep citations concise; perspective = 1-2 sentences max |
| Query citations inconsistent | Medium | Strict prompt instructions + few-shot examples |
| JSON parsing failures for citations | Medium | json-repair + fallback to empty citations array |
| Cross-doc merge loses citations | Low | Merger preserves all citations from both sources |
