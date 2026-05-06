---
phase: 2
title: Table and Layout Extraction
status: completed
priority: P1
effort: 2d
dependencies:
  - 1
---

# Phase 2: Table and Layout Extraction

## Overview

Replace manual PDF conversion with `pymupdf4llm` library for high-quality table extraction and multi-column layout handling. pymupdf4llm already implements table detection (`find_tables()` → markdown), column-aware text ordering, and image extraction — eliminating fragile manual code.

## Requirements

- **Functional:**
  - Tables extracted as markdown tables (not flat text)
  - Multi-column layouts produce correct reading order
  - Images extracted and saved to correct paths
  - Page chunk metadata preserved for citation tracking
  - Existing non-PDF conversion unchanged

- **Non-functional:**
  - pymupdf4llm added as dependency
  - Backward-compatible with existing wiki structure
  - Image paths follow `sources/images/{doc_name}/` convention

## Architecture

### Current (manual, fragile)
```python
# images.py: convert_pdf_with_images()
# - Iterates blocks: type 0 (text) → flat string, type 1 (image) → save PNG
# - No table detection, no column ordering
# - Tables → garbled text, 2-column → interleaved gibberish
```

### New (pymupdf4llm)
```python
# pymupdf4llm.to_markdown(pdf_path, page_chunks=True)
# Returns per-page dicts with:
#   text: markdown text (tables as markdown, columns ordered correctly)
#   tables: extracted table data
#   images: image references
#   metadata: page number, etc.
```

## Related Code Files

- **Modify:** `openkb/images.py` — Replace `convert_pdf_with_images()` with pymupdf4llm wrapper
- **Modify:** `openkb/converter.py` — Update PDF conversion path
- **Modify:** `pyproject.toml` — Add `pymupdf4llm` dependency
- **Modify:** `openkb/config.py` — Add config: `pdf_engine: pymupdf4llm|legacy`
- **Keep:** `openkb/images.py` existing functions for non-PDF formats (base64, relative images)

## Implementation Steps

### Step 1: Add pymupdf4llm dependency

```toml
# pyproject.toml
dependencies = [
    # ... existing ...
    "pymupdf4llm",  # LLM-optimized PDF conversion with tables + layout
]
```

### Step 2: Create pymupdf4llm wrapper in `images.py`

Add new function alongside existing ones (don't delete legacy yet):

```python
def convert_pdf_with_pymupdf4llm(
    pdf_path: Path,
    doc_name: str,
    images_dir: Path,
    pages: list[int] | None = None,
) -> str:
    """Convert PDF to markdown using pymupdf4llm.

    Handles tables (markdown format), multi-column layouts, and images.
    Returns full markdown string with wiki-compatible image paths.
    """
    import pymupdf4llm

    # Convert with page chunks for metadata
    chunks = pymupdf4llm.to_markdown(
        str(pdf_path),
        pages=pages,            # None = all pages, or specific 0-indexed pages
        write_images=True,
        image_path=str(images_dir),
        page_chunks=True,
        dpi=150,
    )

    parts = []
    for chunk in chunks:
        page_num = chunk["metadata"]["page"] + 1  # 0-indexed → 1-indexed

        # Get markdown text (already has tables as markdown, columns ordered)
        text = chunk.get("text", "")

        # Rewrite image paths to wiki convention
        # pymupdf4llm saves as images_dir/filename.png
        # We need sources/images/{doc_name}/filename.png
        text = _rewrite_image_paths(text, doc_name)

        if text.strip():
            parts.append(text)

    return "\n\n".join(parts)
```

### Step 3: Add image path rewriter

```python
def _rewrite_image_paths(markdown: str, doc_name: str) -> str:
    """Rewrite pymupdf4llm image paths to wiki convention.

    pymupdf4llm outputs: ![image](filename.png)
    Wiki needs: ![image](sources/images/{doc_name}/filename.png)
    """
    import re
    # Match ![alt](path) where path is not http/data/absolute
    pattern = r'!\[([^\]]*)\]\((?!https?://|data:|/)([^)]+)\)'
    def replacer(match):
        alt, path = match.group(1), match.group(2)
        filename = Path(path).name  # Strip any directory prefix
        return f'![{alt}](sources/images/{doc_name}/{filename})'
    return re.sub(pattern, replacer, markdown)
```

### Step 4: Update `converter.py` to use pymupdf4llm

```python
# In convert_document(), replace PDF conversion path:
if src.suffix.lower() == ".pdf":
    config_engine = config.get("pdf_engine", "pymupdf4llm")
    if config_engine == "pymupdf4llm":
        from openkb.images import convert_pdf_with_pymupdf4llm
        markdown = convert_pdf_with_pymupdf4llm(src, doc_name, images_dir)
    else:
        # Legacy path
        markdown = convert_pdf_with_images(src, doc_name, images_dir)
```

### Step 5: Update `convert_segment()` for Phase 1 integration

When converting a segment PDF, pass page range to pymupdf4llm:

```python
def convert_segment(segment: PDFSegment, doc_name: str, kb_dir: Path) -> ConvertResult:
    """Convert a single PDF segment."""
    from openkb.images import convert_pdf_with_pymupdf4llm

    images_dir = kb_dir / "wiki" / "sources" / "images" / doc_name
    images_dir.mkdir(parents=True, exist_ok=True)

    # Convert only the segment's pages (0-indexed)
    pages = list(range(segment.start_page - 1, segment.end_page))
    markdown = convert_pdf_with_pymupdf4llm(segment.path, doc_name, images_dir, pages=pages)

    # Prepend segment metadata header
    header = f"<!-- segment: pages {segment.start_page}-{segment.end_page} -->\n"
    header += f"## {segment.chapter_title} (pages {segment.start_page}–{segment.end_page})\n\n"
    markdown = header + markdown

    # Save to sources
    sources_dir = kb_dir / "wiki" / "sources"
    dest_md = sources_dir / f"{doc_name}.md"

    # Append to existing source file (multiple segments)
    if dest_md.exists():
        existing = dest_md.read_text(encoding="utf-8")
        dest_md.write_text(existing + "\n\n---\n\n" + markdown, encoding="utf-8")
    else:
        dest_md.write_text(markdown, encoding="utf-8")

    return ConvertResult(raw_path=segment.path, source_path=dest_md)
```

### Step 6: Add config option

```python
# config.py DEFAULT_CONFIG
"pdf_engine": "pymupdf4llm",  # "pymupdf4llm" or "legacy"
```

### Step 7: Write tests

Test cases:
- PDF with tables → verify markdown table output
- PDF with 2-column layout → verify correct reading order
- PDF with inline images → verify image paths correct
- PDF with mixed content → verify all elements preserved
- Config toggle between engines → verify both paths work

## Success Criteria

- [ ] pymupdf4llm installed and working
- [ ] Tables extracted as markdown tables with correct headers/rows
- [ ] 2-column layouts produce left→right, top→bottom text order
- [ ] Images saved to `sources/images/{doc_name}/` with correct paths
- [ ] Config toggle allows switching between pymupdf4llm and legacy
- [ ] Existing tests still pass (legacy path unchanged)
- [ ] New tests for table extraction, column handling, image paths
- [ ] Segment integration works (pages parameter)

## Risk Assessment

| Risk | Impact | Mitigation |
|------|--------|------------|
| pymupdf4llm version incompatibility | Medium | Pin version in pyproject.toml; test before merge |
| Image path format differs from convention | Low | Path rewriter handles normalization |
| Complex tables not detected | Medium | pymupdf4llm uses pymupdf find_tables() — ~90% accuracy; accept limitations |
| Multi-page tables split across segments | Low | Phase 1 splits by chapters (natural breaks) |
| Performance regression | Low | pymupdf4llm is faster than manual approach |
