"""Tests for image description integration in converter pipeline."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from openkb.converter import convert_document, convert_segment


def _mock_pdf(page_count: int = 5) -> MagicMock:
    fake_doc = MagicMock()
    fake_doc.page_count = page_count
    fake_doc.__enter__ = MagicMock(return_value=fake_doc)
    fake_doc.__exit__ = MagicMock(return_value=False)
    return fake_doc


class TestConvertDocumentDescribesImages:
    def test_describes_images_when_enabled(self, kb_dir, tmp_path):
        """Image description is called when describe_images config is True."""
        src = tmp_path / "test.pdf"
        src.write_bytes(b"%PDF-1.4 fake")

        enriched = "![img](sources/images/test/x.png)\n\n*[Figure: A diagram.]*"

        with (
            patch("openkb.converter.pymupdf.open", return_value=_mock_pdf()),
            patch(
                "openkb.images.convert_pdf_with_pymupdf4llm",
                return_value="![img](sources/images/test/x.png)",
            ),
            patch("openkb.image_describer.describe_images", return_value=enriched) as mock_desc,
        ):
            result = convert_document(src, kb_dir)

        mock_desc.assert_called_once()
        assert result.source_path is not None
        content = result.source_path.read_text()
        assert "*[Figure: A diagram.]*" in content

    def test_skips_description_when_disabled(self, kb_dir, tmp_path):
        """No VLM calls when describe_images=False."""
        src = tmp_path / "test.pdf"
        src.write_bytes(b"%PDF-1.4 fake")

        config_dir = kb_dir / ".openkb"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "config.yaml").write_text("describe_images: false\n")

        md = "![img](sources/images/test/x.png)"

        with (
            patch("openkb.converter.pymupdf.open", return_value=_mock_pdf()),
            patch(
                "openkb.images.convert_pdf_with_pymupdf4llm",
                return_value=md,
            ),
            patch("openkb.image_describer.describe_images") as mock_desc,
        ):
            convert_document(src, kb_dir)

        mock_desc.assert_not_called()


class TestConvertSegmentDescribesImages:
    def test_segment_describes_images(self, kb_dir):
        """Segments also get image descriptions."""
        seg_file = kb_dir / "raw" / "seg.pdf"
        seg_file.parent.mkdir(parents=True, exist_ok=True)
        seg_file.write_bytes(b"%PDF-1.4")

        from openkb.splitter import PDFSegment

        segment = PDFSegment(
            path=seg_file,
            start_page=1,
            end_page=10,
            chapter_title="Chapter 1",
        )

        enriched = "![img](sources/images/book/fig.png)\n\n*[Figure: Chart.]*"
        config = {"describe_images": True, "model": "gpt-4o-mini", "max_describe_images": 50}

        with (
            patch(
                "openkb.converter.convert_pdf_with_images",
                return_value="![img](sources/images/book/fig.png)",
            ),
            patch("openkb.image_describer.describe_images", return_value=enriched) as mock_desc,
        ):
            dest = convert_segment(segment, "book", kb_dir, config=config)

        mock_desc.assert_called_once()
        content = dest.read_text()
        assert "*[Figure: Chart.]*" in content

    def test_segment_skips_when_disabled(self, kb_dir):
        """Segments skip description when describe_images=False."""
        seg_file = kb_dir / "raw" / "seg2.pdf"
        seg_file.parent.mkdir(parents=True, exist_ok=True)
        seg_file.write_bytes(b"%PDF-1.4")

        from openkb.splitter import PDFSegment

        segment = PDFSegment(
            path=seg_file, start_page=1, end_page=5, chapter_title="Ch2",
        )

        config = {"describe_images": False}

        with (
            patch(
                "openkb.converter.convert_pdf_with_images",
                return_value="![img](x.png)",
            ),
            patch("openkb.image_describer.describe_images") as mock_desc,
        ):
            convert_segment(segment, "book2", kb_dir, config=config)

        mock_desc.assert_not_called()
