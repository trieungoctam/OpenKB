# Brainstorm Report: Enhanced PDF Processing for Learning

**Date:** 2026-05-06
**Status:** Approved → Proceeding to Planning

## Problem Statement

User needs OpenKB to process large PDF textbooks (~200k tokens, 400-500 pages) with images, tables, and diagrams for learning purposes. Primary use case: cross-book synthesis — compiling multiple books on same topic and querying across them.

## User Requirements

- **Book type:** Digital PDFs (text layer + images), no OCR needed
- **Content:** Tables, diagrams, charts, images (no LaTeX)
- **Usage:** Cross-book synthesis, compare perspectives across sources
- **Priority:** Wiki content quality over speed or cost

## Evaluated Approaches

### Approach A: Tier 1 Only (Selected)
Focus on 5 core features that directly impact extraction quality and cross-book synthesis.
- Pros: Focused scope, delivers value quickly
- Cons: Lacks semantic quality assurance

### Approach B: Tier 1 + Tier 2
10 features including follow-up suggestions, confidence scoring, caching.
- Pros: Complete learning experience
- Cons: Too large for initial implementation

### Approach C: Cherry-pick 3-4
Minimal subset for fastest delivery.
- Pros: Fastest to implement
- Cons: Misses important features

**Decision:** Approach A + Preprocessing foundation (6 features total)

## Final 6 Features (Implementation Order)

### Feature 0: PDF Preprocessing (Foundation)
**Problem:** Large books (400+ pages) overwhelm single-pass processing.
**Solution:** Hybrid TOC-based chapter splitting + fixed-chunk fallback.
- Extract bookmarks via `pymupdf doc.get_toc()`
- Split PDF into chapter-level segments
- Fallback: fixed 30-page chunks for books without TOC
- Each segment processed independently through short-doc pipeline
- Merge wiki results after all segments compiled

**Files:** `images.py` (new `split_pdf_by_toc()`, `split_pdf_by_chunks()`), `converter.py` (routing logic)
**Effort:** 2-3 days

### Feature 1: Table Extraction & Preservation
**Problem:** Tables lose structure — converted to flat text blocks.
**Solution:** Use pymupdf `page.find_tables()` API for table detection.
- Detect tables before text processing in block loop
- Convert to markdown tables via `table.to_markdown()`
- Handle: merged cells, multi-page tables, tables inside figures
- Config: `table_extraction: true`

**Files:** `images.py` (modify `convert_pdf_with_images`)
**Effort:** 2-3 days
**Risk:** ~85-90% accuracy on complex layouts. Fallback to camelot for edge cases.

### Feature 2: Multi-Column Layout Handling
**Problem:** 2-column textbook layouts produce interleaved, incorrect text.
**Solution:** X-coordinate clustering to detect columns, reorder blocks.
- Cluster blocks by X-position (left vs right column)
- Process: left column top→bottom, then right column top→bottom
- Handle: full-width headings, mixed layouts, headers/footers
- Config: `column_detection: auto | single | double`

**Files:** `images.py` (new `detect_columns()`, modify `convert_pdf_with_images`)
**Effort:** 1-2 days
**Risk:** Complex layouts (callout boxes) may mis-detect. Simple 2-column = high accuracy.

### Feature 3: Parallel Document Compilation
**Problem:** Multiple books compiled sequentially = slow.
**Solution:** Document-level parallel compilation with concept merge.
- `compile_batch()` wraps multiple `compile_short_doc` calls
- Global semaphore for API rate limiting
- Post-compilation merge phase for duplicate concepts
- Progress reporting: N/M documents

**Files:** `compiler.py`, new batch module, CLI integration
**Effort:** 2-3 days
**Risk:** Concept merge quality when concurrent docs create same concept.

### Feature 4: Cross-Document Citation Highlighting
**Problem:** No way to know which book a concept comes from.
**Solution:** Structured citations in concept pages + inline citations in query answers.
- Concept pages add "Sources & Perspectives" section per source book
- Frontmatter `citations` array: `[{"book": "name", "pages": "42-55"}]`
- Query agent shows inline `[Source: Book A, p.42]`
- Highlight conflicting perspectives between books

**Files:** `compiler.py` (prompt templates, `_write_concept`), `query.py` (instructions)
**Effort:** 3-4 days
**Risk:** LLM may hallucinate page numbers. Need validation against document structure.

### Feature 5: Semantic Linting Enhancement
**Problem:** Current semantic lint is basic, no severity, no structured output.
**Solution:** Enhanced targeted checks with severity levels.
- Contradiction detection with severity (critical/warning/info)
- Knowledge gap analysis (book TOC vs wiki coverage)
- Source coverage check (per-concept source diversity)
- Cross-reference quality (isolated concept clusters)
- Structured report format by severity

**Files:** `lint.py` (new checks), `agent/linter.py` (enhanced prompt, structured output)
**Effort:** 2-3 days
**Risk:** LLM-based lint = non-deterministic. Critical issues should be code-based where possible.

## Sprint Plan

| Sprint | Features | Duration | Focus |
|--------|----------|----------|-------|
| Sprint 1 | Feature 0 + 1 + 2 | 5-8 days | Preprocessing + extraction quality |
| Sprint 2 | Feature 3 | 2-3 days | Parallel compilation |
| Sprint 3 | Feature 4 | 3-4 days | Cross-document citations |
| Sprint 4 | Feature 5 | 2-3 days | Semantic linting |

**Total estimate:** 12-18 days

## Dependency Chain

```
Feature 0 (preprocessing) ──→ must complete first
Feature 1 + 2 (table/column) ──→ depend on Feature 0's output format
Feature 3 (parallel) ──→ independent, can overlap with 1+2
Feature 4 (citations) ──→ needs extraction quality from 1+2
Feature 5 (linting) ──→ needs complete wiki content from all above
```

## Success Criteria

1. Books 400+ pages process without context overflow
2. Tables rendered as markdown tables (not flat text)
3. 2-column layouts produce correct reading order
4. Multiple books compile in parallel with <10% time increase vs single
5. Concept pages show source attribution with book name + page/chapter
6. Lint reports include severity levels and specific actionable items

## Unresolved Questions

1. pymupdf version requirement for `find_tables()` — need to check minimum version
2. Concept merge strategy for parallel compilation — last-write-wins vs LLM merge?
3. Page number validation — how to verify LLM-generated citations?
4. Minimum wiki size threshold for semantic lint usefulness
