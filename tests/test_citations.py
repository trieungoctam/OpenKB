"""Tests for cross-document citation features."""
from __future__ import annotations

import json
from pathlib import Path

from openkb.lint import check_citation_coverage
from openkb.agent.compiler import _write_concept, _update_frontmatter_field


class TestWriteConceptWithCitations:
    def test_create_with_citations(self, tmp_path):
        """New concept page includes citations in frontmatter."""
        wiki_dir = tmp_path / "wiki"
        concepts_dir = wiki_dir / "concepts"
        concepts_dir.mkdir(parents=True)

        citations = [
            {"book": "Deep Learning", "pages": "420-435", "chapter": "12.3", "perspective": "Explains attention"},
        ]
        _write_concept(
            wiki_dir, "attention", "Attention is...", "summaries/book.md",
            is_update=False, brief="A mechanism", citations=citations,
        )

        text = (concepts_dir / "attention.md").read_text(encoding="utf-8")
        assert "sources: [summaries/book.md]" in text
        assert "brief: A mechanism" in text
        assert "citations:" in text
        # Verify citation content is valid JSON
        for line in text.split("\n"):
            if line.startswith("citations:"):
                citation_json = line[len("citations: "):]
                parsed = json.loads(citation_json)
                assert parsed[0]["book"] == "Deep Learning"

    def test_create_without_citations(self, tmp_path):
        """Concept without citations omits the field."""
        wiki_dir = tmp_path / "wiki"
        concepts_dir = wiki_dir / "concepts"
        concepts_dir.mkdir(parents=True)

        _write_concept(
            wiki_dir, "test", "Content", "summaries/doc.md",
            is_update=False, brief="Test",
        )

        text = (concepts_dir / "test.md").read_text(encoding="utf-8")
        assert "citations:" not in text

    def test_update_preserves_citations(self, tmp_path):
        """Update adds new citations alongside existing ones."""
        wiki_dir = tmp_path / "wiki"
        concepts_dir = wiki_dir / "concepts"
        concepts_dir.mkdir(parents=True)

        # Create initial concept
        (concepts_dir / "attention.md").write_text(
            "---\nsources: [summaries/book1.md]\nbrief: old\n---\n\nOld content",
            encoding="utf-8",
        )

        new_citations = [
            {"book": "Book 2", "pages": "10-20", "chapter": "Ch 2", "perspective": "New view"},
        ]
        _write_concept(
            wiki_dir, "attention", "Updated content", "summaries/book2.md",
            is_update=True, brief="updated", citations=new_citations,
        )

        text = (concepts_dir / "attention.md").read_text(encoding="utf-8")
        assert "citations:" in text
        assert "Updated content" in text


class TestUpdateFrontmatterField:
    def test_adds_new_field(self, tmp_path):
        """Adds a field to existing frontmatter."""
        path = tmp_path / "test.md"
        text = "---\nsources: [a.md]\n---\n\nBody"
        path.write_text(text, encoding="utf-8")

        result = _update_frontmatter_field(text, path, "citations", "[]")
        assert "citations: []" in result

    def test_replaces_existing_field(self, tmp_path):
        """Replaces an existing field value."""
        path = tmp_path / "test.md"
        text = "---\nsources: [a.md]\ncitations: []\n---\n\nBody"
        path.write_text(text, encoding="utf-8")

        result = _update_frontmatter_field(text, path, "citations", '[{"book":"X"}]')
        assert 'citations: [{"book":"X"}]' in result
        assert result.count("citations:") == 1


class TestCheckCitationCoverage:
    def test_reports_missing_citations(self, tmp_path):
        """Concept with sources but no citations is flagged."""
        concepts_dir = tmp_path / "concepts"
        concepts_dir.mkdir()
        (concepts_dir / "test.md").write_text(
            "---\nsources: [summaries/book.md]\nbrief: test\n---\n\nContent",
            encoding="utf-8",
        )

        issues = check_citation_coverage(tmp_path)
        assert len(issues) == 2
        assert any("no structured citations" in i for i in issues)
        assert any("Sources & Perspectives" in i for i in issues)

    def test_passes_complete_concept(self, tmp_path):
        """Concept with citations and section passes."""
        concepts_dir = tmp_path / "concepts"
        concepts_dir.mkdir()
        (concepts_dir / "test.md").write_text(
            "---\nsources: [summaries/book.md]\ncitations: []\n---\n\n"
            "Content\n\n## Sources & Perspectives\n\nInfo",
            encoding="utf-8",
        )

        issues = check_citation_coverage(tmp_path)
        assert len(issues) == 0

    def test_empty_dir(self, tmp_path):
        issues = check_citation_coverage(tmp_path / "nonexistent")
        assert issues == []
