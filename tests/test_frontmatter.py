"""Tests for rich YAML frontmatter generation and enrichment."""
from __future__ import annotations

import os
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

from openkb.frontmatter import enrich_frontmatter, enrich_directory, _parse_frontmatter
from openkb.converter import convert_document


def _mock_pdf(page_count: int = 5) -> MagicMock:
    fake_doc = MagicMock()
    fake_doc.page_count = page_count
    fake_doc.__enter__ = MagicMock(return_value=fake_doc)
    fake_doc.__exit__ = MagicMock(return_value=False)
    return fake_doc


class TestParseFrontmatter:
    def test_no_frontmatter(self):
        fields, body = _parse_frontmatter("Hello world")
        assert fields == {}
        assert body == "Hello world"

    def test_with_frontmatter(self):
        text = "---\ntype: summary\ntitle: test\n---\n\nBody here"
        fields, body = _parse_frontmatter(text)
        assert fields["type"] == "summary"
        assert fields["title"] == "test"
        assert body == "Body here"

    def test_unclosed_frontmatter(self):
        text = "---\ntype: summary\nBody here"
        fields, body = _parse_frontmatter(text)
        assert fields == {}
        assert "Body here" in body


class TestSourceFrontmatter:
    def test_source_file_has_frontmatter(self, tmp_path):
        kb_dir = tmp_path
        src = kb_dir / "raw" / "test.md"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text("# Test\n\nContent here.", encoding="utf-8")

        with patch("openkb.converter.pymupdf"):
            result = convert_document(src, kb_dir)

        content = result.source_path.read_text(encoding="utf-8")
        assert content.startswith("---")
        assert "type: source" in content
        assert "title:" in content
        assert "original_file:" in content
        assert "date:" in content
        assert "# Test" in content


class TestSummaryFrontmatter:
    def test_summary_has_rich_frontmatter(self, tmp_path):
        from openkb.agent.compiler import _write_summary

        wiki_dir = tmp_path / "wiki"
        _write_summary(wiki_dir, "my-book", "# Summary\n\nKey points.", "short")

        content = (wiki_dir / "summaries" / "my-book.md").read_text()
        assert "type: summary" in content
        assert 'title: "my-book"' in content
        assert "date:" in content
        assert "last_updated:" in content
        assert "doc_type: short" in content
        assert "full_text: sources/my-book.md" in content
        assert "# Summary" in content


class TestConceptFrontmatter:
    def test_new_concept_has_rich_frontmatter(self, tmp_path):
        from openkb.agent.compiler import _write_concept

        wiki_dir = tmp_path / "wiki"
        _write_concept(
            wiki_dir, "Lean Startup", "## Explanation\n\nDetails.",
            "summaries/book.md", False,
            brief="A methodology for building startups.",
            citations=[{"book": "Book", "pages": "1-10", "chapter": "Ch1", "perspective": "Author view"}],
            tags=["methodology", "startups"],
        )

        content = (wiki_dir / "concepts" / "Lean-Startup.md").read_text()
        assert "type: concept" in content
        assert 'title: "Lean Startup"' in content
        assert "date:" in content
        assert "last_updated:" in content
        assert "sources: [summaries/book.md]" in content
        assert "brief: A methodology for building startups." in content
        assert "tags:" in content
        assert "methodology" in content
        assert "## Explanation" in content

    def test_concept_update_refreshes_last_updated(self, tmp_path):
        from openkb.agent.compiler import _write_concept

        wiki_dir = tmp_path / "wiki"

        # Create concept first
        _write_concept(
            wiki_dir, "MVP", "## MVP\n\nMinimum Viable Product.",
            "summaries/book1.md", False, brief="Minimum Viable Product.", tags=["product"],
        )
        original = (wiki_dir / "concepts" / "MVP.md").read_text()

        # Update it
        _write_concept(
            wiki_dir, "MVP", "## MVP\n\nUpdated explanation.",
            "summaries/book2.md", True, brief="MVP updated.", tags=["product", "agile"],
        )
        updated = (wiki_dir / "concepts" / "MVP.md").read_text()

        # Should have both sources
        assert "book1.md" in updated
        assert "book2.md" in updated
        # last_updated should be today
        today = date.today().isoformat()
        assert f"last_updated: {today}" in updated


class TestEnrichFrontmatter:
    def test_enriches_file_with_no_frontmatter(self, tmp_path):
        f = tmp_path / "test.md"
        f.write_text("# Some page\n\nContent here.")

        result = enrich_frontmatter(f, "concept")
        assert result is True

        content = f.read_text()
        assert "type: concept" in content
        assert "title:" in content
        assert "date:" in content
        assert "last_updated:" in content
        assert "# Some page" in content

    def test_skips_already_enriched(self, tmp_path):
        today = date.today().isoformat()
        f = tmp_path / "test.md"
        f.write_text(f"---\ntype: concept\ntitle: \"test\"\ndate: {today}\nlast_updated: {today}\nunderstanding_level: unreviewed\nlast_reviewed: null\nreview_count: 0\n---\n\nBody")

        result = enrich_frontmatter(f, "concept")
        assert result is False

    def test_adds_only_missing_fields(self, tmp_path):
        f = tmp_path / "test.md"
        f.write_text("---\ntype: summary\ntitle: \"my doc\"\n---\n\nBody")

        result = enrich_frontmatter(f, "summary")
        assert result is True

        content = f.read_text()
        assert "type: summary" in content
        assert "date:" in content
        assert "last_updated:" in content

    def test_skips_special_files(self, tmp_path):
        for name in ["index.md", "log.md", "AGENTS.md"]:
            f = tmp_path / name
            f.write_text("# Content")
            assert enrich_frontmatter(f, "source") is False

    def test_enrich_directory(self, tmp_path):
        concepts_dir = tmp_path / "concepts"
        concepts_dir.mkdir()
        (concepts_dir / "concept-a.md").write_text("# A")
        (concepts_dir / "concept-b.md").write_text("# B")

        count = enrich_directory(concepts_dir, "concept")
        assert count == 2

    def test_enrich_directory_skips_nonexistent(self, tmp_path):
        count = enrich_directory(tmp_path / "nonexistent", "concept")
        assert count == 0


class TestUpgradeEnrichFrontmatter:
    def test_enrich_flag(self, tmp_path):
        from click.testing import CliRunner
        from openkb.cli import cli

        kb_dir = tmp_path
        (kb_dir / "raw").mkdir()
        (kb_dir / "wiki" / "sources").mkdir(parents=True)
        (kb_dir / "wiki" / "summaries").mkdir(parents=True)
        (kb_dir / "wiki" / "concepts").mkdir(parents=True)
        (kb_dir / ".openkb").mkdir()
        (kb_dir / ".openkb" / "config.yaml").write_text("model: gpt-4o-mini\n")

        # Create a file without frontmatter
        (kb_dir / "wiki" / "sources" / "doc.md").write_text("# Doc\n\nContent.")

        runner = CliRunner()
        with patch("openkb.cli._find_kb_dir", return_value=kb_dir):
            result = runner.invoke(cli, ["upgrade", "--enrich-frontmatter"])

        assert result.exit_code == 0
        assert "Enriched" in result.output

        content = (kb_dir / "wiki" / "sources" / "doc.md").read_text()
        assert "type: source" in content
