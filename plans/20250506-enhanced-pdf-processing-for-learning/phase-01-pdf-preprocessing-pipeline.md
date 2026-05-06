---
phase: 1
title: PDF Preprocessing Pipeline
status: completed
priority: P1
effort: 3d
dependencies: []
---

# Phase 1: PDF Preprocessing Pipeline

## Overview

Split large PDF textbooks (400+ pages) into manageable segments before LLM processing. Uses pymupdf TOC extraction for chapter-aware splitting with fixed-size chunk fallback. This is the **critical blocker** — without it, LLM API calls fail on large books.

## Requirements

- **Functional:**
  - Detect large PDFs (>threshold pages) and split before compilation
  - Extract PDF bookmarks/TOC via `pymupdf doc.get_toc()`
  - Split by chapters when TOC available
  - Fallback to fixed-size chunks (25-30 pages) when no TOC
  - Process each segment through short-doc pipeline
  - Merge wiki results from all segments into unified KB

- **Non-functional:**
  - Each segment must fit within LLM context window (~50k tokens max per segment)
  - Preserve page number metadata for citation tracking
  - Handle edge cases: single-chapter books, books with broken TOC

## Architecture

### Current Flow (broken for large books)
```
PDF (400 pages) → PageIndex (full doc) → compile_long_doc → LLM FAILS
```

### New Flow
```
PDF (400 pages)
  → get_toc() → has chapters?
    → Yes: Split by TOC chapters → [ch1.pdf, ch2.pdf, ...]
    → No: Split by 25-page chunks → [chunk1.pdf, chunk2.pdf, ...]
  → For each segment:
    → convert_segment() → markdown with page metadata
    → compile_short_doc() → wiki pages with segment context
  → merge_concepts() → deduplicate cross-chapter concepts
```

### Data Flow

```
Input: raw/book.pdf (400 pages)
  ↓
split_pdf() → List[Segment{path, pages, chapter_name}]
  ↓
For each segment:
  convert_segment() → wiki/sources/book-ch1.md (with "Original pages: 1-45")
  compile_short_doc() → wiki/summaries/book-ch1.md + wiki/concepts/*.md
  ↓
merge_concepts() → deduplicate, merge overlapping concepts
  ↓
Output: Unified wiki with book segments + merged concepts
```

## Related Code Files

- **Modify:** `openkb/converter.py` — Add segment conversion, routing logic
- **Modify:** `openkb/images.py` — Add segment-aware image extraction
- **Modify:** `openkb/cli.py` — Update `add_single_file()` to use new pipeline
- **Modify:** `openkb/agent/compiler.py` — Add segment compilation context
- **Create:** `openkb/splitter.py` — PDF splitting module (TOC + chunk logic)
- **Create:** `openkb/merger.py` — Concept merging for segments
- **Modify:** `openkb/config.py` — Add config: `chunk_size: 25`, `split_by_toc: true`

## Implementation Steps

### Step 1: Create `openkb/splitter.py`

```python
@dataclass
class PDFSegment:
    path: Path           # Temporary file path
    page_range: str      # "1-25" or "Chapter 1: Introduction (pp.1-45)"
    chapter_title: str   # From TOC or "Part 1", "Part 2"
    start_page: int      # 1-based
    end_page: int        # 1-based inclusive

def extract_toc(pdf_path: Path) -> list[dict]:
    """Extract bookmarks from PDF. Returns [{level, title, page}, ...]"""
    # pymupdf: doc.get_toc() returns [[level, title, page_number], ...]

def split_by_toc(pdf_path: Path, output_dir: Path) -> list[PDFSegment]:
    """Split PDF by TOC chapters. Each segment = one chapter."""

def split_by_chunks(pdf_path: Path, output_dir: Path, chunk_size: int = 25) -> list[PDFSegment]:
    """Split PDF into fixed-size page chunks. Fallback when no TOC."""

def split_pdf(pdf_path: Path, output_dir: Path, config: dict) -> list[PDFSegment]:
    """Hybrid: try TOC first, fallback to chunks."""
    # if config.get("split_by_toc", True) and extract_toc(pdf_path):
    #     return split_by_toc(pdf_path, output_dir)
    # return split_by_chunks(pdf_path, output_dir, config.get("chunk_size", 25))
```

Key implementation details:
- Use `pymupdf.Document.insert_pdf()` to create segment PDFs
- `doc.get_toc()` returns `[[level, title, page_num], ...]` — page_num is 1-based
- Group TOC entries: chapter = level-1 entry, subchapters merged into parent
- Minimum segment size: 5 pages (avoid tiny fragments)
- Maximum segment size: 40 pages (stay within context window)
- Save segments to `.openkb/segments/{doc_name}/` temp directory

### Step 2: Update `openkb/converter.py`

Add `convert_segment()` function:

```python
def convert_segment(segment: PDFSegment, doc_name: str, kb_dir: Path) -> ConvertResult:
    """Convert a single PDF segment to markdown.

    Like convert_document() but:
    - No hash check (segments are temporary)
    - Adds page range metadata to output markdown
    - Images saved to doc_name images dir (not segment-specific)
    """
    # Header: "## Chapter: {segment.chapter_title} (pages {segment.start_page}-{segment.end_page})\n\n"
    # Then convert via pymupdf → markdown
    # Append page metadata as HTML comment for citation tracking
```

Add routing in `convert_document()`:

```python
# In convert_document(), after PDF detection:
if page_count >= threshold:
    # Instead of returning is_long_doc=True immediately,
    # check if we should split
    config_split = config.get("split_large_pdfs", True)
    if config_split and page_count > threshold:
        return ConvertResult(
            raw_path=raw_dest,
            is_long_doc=False,  # Will be handled as segments
            is_large_pdf=True,  # New flag
            file_hash=file_hash
        )
```

### Step 3: Create `openkb/merger.py`

```python
def merge_segment_concepts(wiki_dir: Path, doc_name: str) -> dict:
    """Merge concepts generated from segments of the same document.

    Detects duplicate concepts (same/similar name) across segments and:
    1. Keeps the most comprehensive version
    2. Merges sources lists
    3. Combines content if complementary
    4. Deduplicates wikilinks

    Returns stats: {merged: int, kept: int, total: int}
    """
```

Strategy for merging:
- **Exact match**: Same sanitized slug → merge sources, keep longest content
- **Similar name**: Use simple string similarity (Levenshtein) → ask if overlap > 0.8
- **Different**: Keep both as separate concepts
- After merge: update index.md to remove duplicates

### Step 4: Update `openkb/cli.py`

Modify `add_single_file()`:

```python
# Replace current long_doc handling with:
if result.is_large_pdf:
    from openkb.splitter import split_pdf
    from openkb.merger import merge_segment_concepts

    segments = split_pdf(result.raw_path, segments_dir, config)
    click.echo(f"  Split into {len(segments)} segments")

    for i, seg in enumerate(segments, 1):
        click.echo(f"  [{i}/{len(segments)}] Processing {seg.chapter_title}...")
        seg_result = convert_segment(seg, doc_name, kb_dir)
        # Compile with segment context (chapter name, page range)
        asyncio.run(compile_short_doc(
            f"{doc_name}-{seg.chapter_title}", seg_result.source_path,
            kb_dir, model, segment_context=seg
        ))

    # Merge concepts from all segments
    merge_result = merge_segment_concepts(kb_dir / "wiki", doc_name)
    click.echo(f"  Merged {merge_result['merged']} duplicate concepts")
```

### Step 5: Update compiler for segment context

Add optional `segment_context` parameter to `compile_short_doc()`:

```python
async def compile_short_doc(
    doc_name: str,
    source_path: Path,
    kb_dir: Path,
    model: str,
    max_concurrency: int = DEFAULT_COMPILE_CONCURRENCY,
    segment_context: PDFSegment | None = None,  # NEW
) -> None:
```

When `segment_context` is provided:
- Prefix document content with `"[Chapter: {title}, pages {start}-{end} of {doc_name}]"`
- Adjust concept prompts to note this is a segment of a larger document
- Concepts created should reference parent document name, not segment name

### Step 6: Update config

Add to `DEFAULT_CONFIG`:
```python
DEFAULT_CONFIG = {
    "model": "gpt-5.4-mini",
    "language": "en",
    "pageindex_threshold": 20,
    "split_large_pdfs": True,    # NEW
    "chunk_size": 25,            # NEW: pages per chunk when no TOC
    "split_by_toc": True,        # NEW: prefer TOC-based splitting
    "max_segment_pages": 40,     # NEW: max pages per segment
}
```

### Step 7: Cleanup temp segments

Add cleanup in `add_single_file()` after successful merge:
```python
# Clean up temporary segment PDFs
import shutil
segments_dir = kb_dir / ".openkb" / "segments" / doc_name
if segments_dir.exists():
    shutil.rmtree(segments_dir)
```

## Success Criteria

- [ ] PDF with 400+ pages splits into segments without errors
- [ ] Each segment < 40 pages, compiles without LLM context overflow
- [ ] TOC-based splitting produces chapter-level segments
- [ ] Chunk-based splitting produces fixed-size segments when no TOC
- [ ] All segments' wiki pages merged under parent document name
- [ ] Duplicate concepts across segments detected and merged
- [ ] Page number metadata preserved in source markdown
- [ ] Temporary segment files cleaned up after compilation
- [ ] Config options for chunk_size, split_by_toc work correctly
- [ ] Existing tests still pass
- [ ] New tests for splitter, merger, segment conversion

## Risk Assessment

| Risk | Impact | Mitigation |
|------|--------|------------|
| TOC missing/broken in some PDFs | Medium | Fallback to chunk splitting |
| Segment boundaries cut through concepts | Medium | Use chapter boundaries (natural concept breaks); accept for chunks |
| Concept merge creates duplicates | Low | Levenshtein similarity + source list merge |
| pymupdf segment extraction quality | Low | pymupdf 1.27+ stable; widely used |
| Temp file cleanup failure | Low | Add `.openkb/segments/` to .gitignore; manual cleanup |
| PageIndex still needed for some cases | Medium | Keep PageIndex path as fallback (config: `use_pageindex: false`) |
