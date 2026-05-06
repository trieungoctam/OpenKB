"""PDF splitting module for large document preprocessing.

Splits large PDFs into manageable segments using TOC/bookmark extraction
when available, falling back to fixed-size page chunks.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

import pymupdf

logger = logging.getLogger(__name__)

# Minimum pages per segment — avoid tiny fragments
_MIN_SEGMENT_PAGES = 5

# Maximum pages per segment — stay within LLM context window
_DEFAULT_MAX_SEGMENT_PAGES = 40

# Sanitize chapter titles for safe filenames
_SAFE_NAME_RE = re.compile(r'[^\w\-]')
_SLUG_RE = re.compile(r'-{2,}')


@dataclass
class PDFSegment:
    """A segment of a PDF document for independent processing."""

    path: Path
    chapter_title: str
    start_page: int  # 1-based, inclusive
    end_page: int    # 1-based, inclusive

    @property
    def page_count(self) -> int:
        return self.end_page - self.start_page + 1

    @property
    def page_range_str(self) -> str:
        return f"{self.start_page}-{self.end_page}"

    @property
    def slug(self) -> str:
        sanitized = _SAFE_NAME_RE.sub('-', self.chapter_title).strip('-')
        sanitized = _SLUG_RE.sub('-', sanitized)
        return sanitized or f"pages-{self.page_range_str}"


def extract_toc(pdf_path: Path) -> list[dict]:
    """Extract bookmarks/TOC from PDF.

    Returns list of dicts with keys: level (int), title (str), page (int, 1-based).
    Returns empty list if no bookmarks found.
    """
    with pymupdf.open(str(pdf_path)) as doc:
        raw_toc = doc.get_toc()  # [[level, title, page_num], ...]
        if not raw_toc:
            return []
        return [
            {"level": entry[0], "title": entry[1], "page": entry[2]}
            for entry in raw_toc
        ]


def split_by_toc(
    pdf_path: Path,
    output_dir: Path,
    max_pages: int = _DEFAULT_MAX_SEGMENT_PAGES,
) -> list[PDFSegment]:
    """Split PDF by TOC chapters into segments.

    Uses level-1 TOC entries as chapter boundaries. Subchapters are merged
    into their parent chapter. Segments exceeding max_pages are split further.
    """
    toc = extract_toc(pdf_path)
    if not toc:
        logger.info("No TOC found in %s, falling back to chunk splitting", pdf_path.name)
        return split_by_chunks(pdf_path, output_dir, max_pages)

    with pymupdf.open(str(pdf_path)) as doc:
        total_pages = doc.page_count

    # Filter to level-1 entries (main chapters)
    chapters = [entry for entry in toc if entry["level"] == 1]
    if not chapters:
        # Fall back to level-2 if no level-1 entries
        chapters = [entry for entry in toc if entry["level"] <= 2]
    if not chapters:
        return split_by_chunks(pdf_path, output_dir, max_pages)

    # Build segments from chapter boundaries
    segments: list[PDFSegment] = []
    output_dir.mkdir(parents=True, exist_ok=True)

    for i, chapter in enumerate(chapters):
        start = chapter["page"]
        # End = start of next chapter - 1, or last page
        if i + 1 < len(chapters):
            end = chapters[i + 1]["page"] - 1
        else:
            end = total_pages
        end = min(end, total_pages)

        # Clamp start to valid range
        start = max(1, min(start, total_pages))

        if end - start + 1 < _MIN_SEGMENT_PAGES:
            # Merge with previous segment if too small
            if segments:
                segments[-1].end_page = end
                continue
            # If first segment too small, expand to min size
            end = min(start + _MIN_SEGMENT_PAGES - 1, total_pages)

        # Split oversized segments
        while end - start + 1 > max_pages:
            sub_end = start + max_pages - 1
            segments.append(_extract_segment(
                pdf_path, output_dir, chapter["title"], start, sub_end, len(segments),
            ))
            start = sub_end + 1
        if start <= end:
            segments.append(_extract_segment(
                pdf_path, output_dir, chapter["title"], start, end, len(segments),
            ))

    if not segments:
        return split_by_chunks(pdf_path, output_dir, max_pages)

    return segments


def split_by_chunks(
    pdf_path: Path,
    output_dir: Path,
    chunk_size: int = 25,
) -> list[PDFSegment]:
    """Split PDF into fixed-size page chunks.

    Fallback when no TOC is available.
    """
    with pymupdf.open(str(pdf_path)) as doc:
        total_pages = doc.page_count

    output_dir.mkdir(parents=True, exist_ok=True)
    segments: list[PDFSegment] = []

    start = 1
    idx = 0
    while start <= total_pages:
        end = min(start + chunk_size - 1, total_pages)
        title = f"Part {idx + 1}"
        segments.append(_extract_segment(
            pdf_path, output_dir, title, start, end, idx,
        ))
        start = end + 1
        idx += 1

    return segments


def split_pdf(
    pdf_path: Path,
    output_dir: Path,
    config: dict,
) -> list[PDFSegment]:
    """Hybrid split: try TOC first, fallback to chunks.

    Args:
        pdf_path: Path to source PDF.
        output_dir: Directory for temporary segment files.
        config: Configuration dict with keys:
            - split_by_toc (bool): Prefer TOC-based splitting (default True)
            - chunk_size (int): Pages per chunk when no TOC (default 25)
            - max_segment_pages (int): Max pages per segment (default 40)

    Returns:
        List of PDFSegment objects.
    """
    max_pages = config.get("max_segment_pages", _DEFAULT_MAX_SEGMENT_PAGES)

    if config.get("split_by_toc", True):
        segments = split_by_toc(pdf_path, output_dir, max_pages)
        if segments:
            return segments

    chunk_size = config.get("chunk_size", 25)
    return split_by_chunks(pdf_path, output_dir, chunk_size)


def _extract_segment(
    pdf_path: Path,
    output_dir: Path,
    title: str,
    start_page: int,
    end_page: int,
    index: int,
) -> PDFSegment:
    """Extract a page range from PDF into a new temporary file.

    Args:
        pdf_path: Source PDF.
        output_dir: Where to save the segment.
        title: Chapter/part title.
        start_page: 1-based start page.
        end_page: 1-based end page (inclusive).
        index: Segment index for filename.
    """
    slug = _SAFE_NAME_RE.sub('-', title).strip('-')
    slug = _SLUG_RE.sub('-', slug) or f"seg-{index}"
    seg_path = output_dir / f"{index:02d}-{slug}.pdf"

    with pymupdf.open(str(pdf_path)) as src:
        seg_doc = pymupdf.open()
        # pymupdf uses 0-based page indices
        seg_doc.insert_pdf(src, from_page=start_page - 1, to_page=end_page - 1)
        seg_doc.save(str(seg_path))
        seg_doc.close()

    return PDFSegment(
        path=seg_path,
        chapter_title=title,
        start_page=start_page,
        end_page=end_page,
    )
