"""Tests for PDF splitter module."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from openkb.splitter import PDFSegment, extract_toc, split_by_chunks, split_by_toc


class TestPDFSegment:
    def test_page_count(self):
        seg = PDFSegment(path=Path("/tmp/test.pdf"), chapter_title="Ch1", start_page=1, end_page=25)
        assert seg.page_count == 25

    def test_page_range_str(self):
        seg = PDFSegment(path=Path("/tmp/test.pdf"), chapter_title="Ch1", start_page=1, end_page=25)
        assert seg.page_range_str == "1-25"

    def test_slug(self):
        seg = PDFSegment(path=Path("/tmp/test.pdf"), chapter_title="Introduction to ML", start_page=1, end_page=25)
        assert seg.slug == "Introduction-to-ML"

    def test_slug_fallback_for_empty(self):
        seg = PDFSegment(path=Path("/tmp/test.pdf"), chapter_title="", start_page=10, end_page=20)
        assert seg.slug == "pages-10-20"


class TestExtractToc:
    @patch("openkb.splitter.pymupdf.open")
    def test_returns_toc_entries(self, mock_open):
        fake_doc = MagicMock()
        fake_doc.get_toc.return_value = [
            [1, "Chapter 1", 1],
            [2, "Section 1.1", 5],
            [1, "Chapter 2", 20],
        ]
        fake_doc.__enter__ = MagicMock(return_value=fake_doc)
        fake_doc.__exit__ = MagicMock(return_value=False)
        mock_open.return_value = fake_doc

        toc = extract_toc(Path("test.pdf"))
        assert len(toc) == 3
        assert toc[0]["level"] == 1
        assert toc[0]["title"] == "Chapter 1"
        assert toc[0]["page"] == 1

    @patch("openkb.splitter.pymupdf.open")
    def test_returns_empty_when_no_toc(self, mock_open):
        fake_doc = MagicMock()
        fake_doc.get_toc.return_value = []
        fake_doc.__enter__ = MagicMock(return_value=fake_doc)
        fake_doc.__exit__ = MagicMock(return_value=False)
        mock_open.return_value = fake_doc

        toc = extract_toc(Path("test.pdf"))
        assert toc == []


class TestSplitByChunks:
    @patch("openkb.splitter.pymupdf.open")
    def test_splits_evenly(self, mock_open, tmp_path):
        fake_doc = MagicMock()
        fake_doc.page_count = 100
        fake_doc.__enter__ = MagicMock(return_value=fake_doc)
        fake_doc.__exit__ = MagicMock(return_value=False)
        mock_open.return_value = fake_doc

        with patch("openkb.splitter._extract_segment") as mock_extract:
            def make_seg(pdf_path, output_dir, title, start_page, end_page, index):
                return PDFSegment(
                    path=tmp_path / f"seg{index}.pdf",
                    chapter_title=title,
                    start_page=start_page,
                    end_page=end_page,
                )
            mock_extract.side_effect = make_seg
            segments = split_by_chunks(Path("test.pdf"), tmp_path, chunk_size=25)

        assert len(segments) == 4
        assert segments[0].start_page == 1
        assert segments[0].end_page == 25
        assert segments[3].start_page == 76
        assert segments[3].end_page == 100

    @patch("openkb.splitter.pymupdf.open")
    def test_handles_remainder(self, mock_open, tmp_path):
        fake_doc = MagicMock()
        fake_doc.page_count = 30
        fake_doc.__enter__ = MagicMock(return_value=fake_doc)
        fake_doc.__exit__ = MagicMock(return_value=False)
        mock_open.return_value = fake_doc

        with patch("openkb.splitter._extract_segment") as mock_extract:
            def make_seg(pdf_path, output_dir, title, start_page, end_page, index):
                return PDFSegment(
                    path=tmp_path / f"seg{index}.pdf",
                    chapter_title=title,
                    start_page=start_page,
                    end_page=end_page,
                )
            mock_extract.side_effect = make_seg
            segments = split_by_chunks(Path("test.pdf"), tmp_path, chunk_size=25)

        assert len(segments) == 2
        assert segments[0].page_count == 25
        assert segments[1].page_count == 5


class TestSplitByToc:
    @patch("openkb.splitter._extract_segment")
    @patch("openkb.splitter.pymupdf.open")
    def test_splits_by_toc_chapters(self, mock_open, mock_extract, tmp_path):
        fake_doc = MagicMock()
        fake_doc.page_count = 100
        fake_doc.__enter__ = MagicMock(return_value=fake_doc)
        fake_doc.__exit__ = MagicMock(return_value=False)
        mock_open.return_value = fake_doc

        def make_seg(pdf_path, output_dir, title, start_page, end_page, index):
            return PDFSegment(
                path=tmp_path / f"seg{index}.pdf",
                chapter_title=title,
                start_page=start_page,
                end_page=end_page,
            )
        mock_extract.side_effect = make_seg

        with patch("openkb.splitter.extract_toc") as mock_toc:
            mock_toc.return_value = [
                {"level": 1, "title": "Chapter 1", "page": 1},
                {"level": 1, "title": "Chapter 2", "page": 30},
                {"level": 1, "title": "Chapter 3", "page": 60},
            ]
            # Use max_pages=50 so Chapter 3 (41 pages) fits in one segment
            segments = split_by_toc(Path("test.pdf"), tmp_path, max_pages=50)

        assert len(segments) == 3
        assert segments[0].chapter_title == "Chapter 1"
        assert segments[0].start_page == 1
        assert segments[0].end_page == 29
