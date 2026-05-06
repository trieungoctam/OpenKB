"""Document conversion pipeline for OpenKB."""
from __future__ import annotations

import logging
import re
import shutil
from dataclasses import dataclass
from datetime import date
from typing import Any
from pathlib import Path

import pymupdf
from markitdown import MarkItDown

from openkb.config import load_config
from openkb.images import copy_relative_images, extract_base64_images, convert_pdf_with_images
from openkb.state import HashRegistry

logger = logging.getLogger(__name__)

# Characters that break markdown links or filesystem paths
_UNSAFE_DOCNAME_RE = re.compile(r"[()\"\']")


def _sanitize_doc_name(name: str) -> str:
    """Remove characters that break markdown image links or cause path issues."""
    return _UNSAFE_DOCNAME_RE.sub("", name).strip()


@dataclass
class ConvertResult:
    """Result returned by :func:`convert_document`."""

    raw_path: Path | None = None
    source_path: Path | None = None
    is_long_doc: bool = False
    is_large_pdf: bool = False
    skipped: bool = False
    file_hash: str | None = None  # For deferred hash registration
    doc_name: str | None = None   # Sanitized document name


def get_pdf_page_count(path: Path) -> int:
    """Return the number of pages in the PDF at *path* using pymupdf."""
    with pymupdf.open(str(path)) as doc:
        return doc.page_count


def convert_document(src: Path, kb_dir: Path, *, force: bool = False) -> ConvertResult:
    """Convert a document and integrate it into the knowledge base.

    Steps:
    1. Hash-check — skip if already known.
    2. Copy source to ``raw/``.
    3. If PDF and page count >= threshold → return :attr:`ConvertResult.is_long_doc`.
    4. If ``.md`` — read, process relative images, save to ``wiki/sources/``.
    5. Otherwise — run MarkItDown, extract base64 images, save to ``wiki/sources/``.
    6. Register hash in the registry.
    """
    # ------------------------------------------------------------------
    # Load config & state
    # ------------------------------------------------------------------
    openkb_dir = kb_dir / ".openkb"
    config = load_config(openkb_dir / "config.yaml")
    threshold: int = config.get("pageindex_threshold", 20)
    registry = HashRegistry(openkb_dir / "hashes.json")
    doc_name = _sanitize_doc_name(src.stem)

    # ------------------------------------------------------------------
    # 1. Hash check
    # ------------------------------------------------------------------
    file_hash = HashRegistry.hash_file(src)
    if not force and registry.is_known(file_hash):
        logger.info("Skipping already-known file: %s", src.name)
        return ConvertResult(skipped=True)

    # ------------------------------------------------------------------
    # 2. Copy to raw/
    # ------------------------------------------------------------------
    raw_dir = kb_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    raw_dest = raw_dir / src.name
    if raw_dest.resolve() != src.resolve():
        shutil.copy2(src, raw_dest)

    # ------------------------------------------------------------------
    # 3. PDF long-doc / large-pdf detection
    # ------------------------------------------------------------------
    if src.suffix.lower() == ".pdf":
        page_count = get_pdf_page_count(src)
        if page_count >= threshold:
            # Check if splitting is enabled for large PDFs
            if config.get("split_large_pdfs", True):
                logger.info(
                    "Large PDF detected (%d pages): %s — will split into segments",
                    page_count,
                    src.name,
                )
                return ConvertResult(
                    raw_path=raw_dest, is_large_pdf=True, file_hash=file_hash,
                    doc_name=doc_name,
                )
            logger.info(
                "Long PDF detected (%d pages >= %d threshold): %s",
                page_count,
                threshold,
                src.name,
            )
            return ConvertResult(raw_path=raw_dest, is_long_doc=True, file_hash=file_hash, doc_name=doc_name)

    # ------------------------------------------------------------------
    # 4/5. Convert to Markdown
    # ------------------------------------------------------------------
    sources_dir = kb_dir / "wiki" / "sources"
    sources_dir.mkdir(parents=True, exist_ok=True)
    images_dir = kb_dir / "wiki" / "sources" / "images" / doc_name
    images_dir.mkdir(parents=True, exist_ok=True)

    if src.suffix.lower() == ".md":
        markdown = src.read_text(encoding="utf-8")
        markdown = copy_relative_images(markdown, src.parent, doc_name, images_dir)
    elif src.suffix.lower() == ".pdf":
        pdf_engine = config.get("pdf_engine", "pymupdf4llm")
        if pdf_engine == "pymupdf4llm":
            from openkb.images import convert_pdf_with_pymupdf4llm
            markdown = convert_pdf_with_pymupdf4llm(src, doc_name, images_dir)
        else:
            markdown = convert_pdf_with_images(src, doc_name, images_dir)
    else:
        # Non-PDF, non-MD: use markitdown (docx, pptx, html, etc.)
        mid = MarkItDown()
        result = mid.convert(str(src))
        markdown = result.text_content
        markdown = extract_base64_images(markdown, doc_name, images_dir)

    # Describe images via VLM if enabled
    if config.get("describe_images", True):
        from openkb.image_describer import describe_images

        vision_model = config.get("vision_model") or config.get("model", "gpt-4o-mini")
        max_images = config.get("max_describe_images", 50)
        markdown = describe_images(markdown, kb_dir, vision_model, max_images=max_images)

    dest_md = sources_dir / f"{doc_name}.md"
    source_fm = (
        f"---\n"
        f"type: source\n"
        f"title: \"{doc_name}\"\n"
        f"original_file: \"{src.name}\"\n"
        f"date: {date.today().isoformat()}\n"
        f"---\n\n"
    )
    dest_md.write_text(source_fm + markdown, encoding="utf-8")

    return ConvertResult(raw_path=raw_dest, source_path=dest_md, file_hash=file_hash, doc_name=doc_name)


def convert_segment(
    segment: Any,
    doc_name: str,
    kb_dir: Path,
    config: dict[str, Any] | None = None,
) -> Path:
    """Convert a single PDF segment to markdown and append to source file.

    Args:
        segment: PDFSegment dataclass instance (from splitter module).
        doc_name: Parent document name (e.g. "my-book").
        kb_dir: Knowledge base root directory.
        config: Optional pre-loaded config dict. Loaded from disk if None.

    Returns:
        Path to the written source markdown file.
    """
    from openkb.splitter import PDFSegment  # noqa: F401
    sources_dir = kb_dir / "wiki" / "sources"
    images_dir = sources_dir / "images" / doc_name
    images_dir.mkdir(parents=True, exist_ok=True)

    seg_path = segment.path
    start_page = segment.start_page
    end_page = segment.end_page
    chapter_title = segment.chapter_title

    # Convert segment PDF to markdown
    markdown = convert_pdf_with_images(seg_path, doc_name, images_dir)

    # Describe images via VLM if enabled
    seg_config = config or load_config(kb_dir / ".openkb" / "config.yaml")
    if seg_config.get("describe_images", True):
        from openkb.image_describer import describe_images

        vision_model = seg_config.get("vision_model") or seg_config.get("model", "gpt-4o-mini")
        max_images = seg_config.get("max_describe_images", 50)
        markdown = describe_images(markdown, kb_dir, vision_model, max_images=max_images)

    # Prepend segment metadata header
    header = (
        f"<!-- segment: pages {start_page}-{end_page} -->\n"
        f"## {chapter_title} (pages {start_page}–{end_page})\n\n"
    )
    markdown = header + markdown

    # Append to parent document's source file
    dest_md = sources_dir / f"{doc_name}.md"
    if dest_md.exists():
        existing = dest_md.read_text(encoding="utf-8")
        dest_md.write_text(existing + "\n\n---\n\n" + markdown, encoding="utf-8")
    else:
        dest_md.write_text(markdown, encoding="utf-8")

    return dest_md
