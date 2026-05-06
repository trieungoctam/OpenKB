"""Tests for semantic linting enhancements (Phase 5)."""
from __future__ import annotations

from pathlib import Path

from openkb.lint import (
    LintIssue,
    Severity,
    check_book_coverage,
    check_concept_clusters,
    check_source_diversity,
    format_severity_report,
)


class TestCheckSourceDiversity:
    def test_flags_low_source_concepts(self, tmp_path):
        """Concepts with fewer than min_sources are flagged."""
        concepts_dir = tmp_path / "concepts"
        concepts_dir.mkdir()
        (concepts_dir / "sparse.md").write_text(
            "---\nsources: [summaries/book1.md]\nbrief: sparse\n---\n\nContent",
            encoding="utf-8",
        )

        issues = check_source_diversity(tmp_path, min_sources=2)
        assert len(issues) == 1
        assert issues[0].severity == Severity.WARNING
        assert "sparse" in issues[0].title

    def test_passes_rich_concepts(self, tmp_path):
        """Concepts with enough sources pass."""
        concepts_dir = tmp_path / "concepts"
        concepts_dir.mkdir()
        (concepts_dir / "rich.md").write_text(
            "---\nsources: [summaries/a.md, summaries/b.md]\nbrief: rich\n---\n\nContent",
            encoding="utf-8",
        )

        issues = check_source_diversity(tmp_path, min_sources=2)
        assert len(issues) == 0

    def test_empty_dir(self, tmp_path):
        issues = check_source_diversity(tmp_path / "nonexistent")
        assert issues == []


class TestCheckConceptClusters:
    def test_flags_isolated_cluster(self, tmp_path):
        """Cluster of concepts linking only to each other is flagged."""
        concepts_dir = tmp_path / "concepts"
        concepts_dir.mkdir()
        (concepts_dir / "cnn.md").write_text(
            "---\n---\n\nSee [[concepts/rnn]]",
            encoding="utf-8",
        )
        (concepts_dir / "rnn.md").write_text(
            "---\n---\n\nSee [[concepts/cnn]]",
            encoding="utf-8",
        )

        issues = check_concept_clusters(tmp_path)
        assert len(issues) == 1
        assert issues[0].severity == Severity.INFO
        assert "Isolated" in issues[0].title

    def test_no_flag_with_external_links(self, tmp_path):
        """Cluster with external links is not flagged."""
        concepts_dir = tmp_path / "concepts"
        concepts_dir.mkdir()
        (concepts_dir / "cnn.md").write_text(
            "---\n---\n\nSee [[summaries/book1]]",
            encoding="utf-8",
        )

        issues = check_concept_clusters(tmp_path)
        assert len(issues) == 0

    def test_single_concept_no_flag(self, tmp_path):
        """Single isolated concept is not flagged (>1 required)."""
        concepts_dir = tmp_path / "concepts"
        concepts_dir.mkdir()
        (concepts_dir / "loner.md").write_text("---\n---\n\nContent", encoding="utf-8")

        issues = check_concept_clusters(tmp_path)
        assert len(issues) == 0


class TestCheckBookCoverage:
    def test_flags_summary_without_concepts(self, tmp_path):
        """Summary with no concept links is flagged."""
        summaries_dir = tmp_path / "summaries"
        summaries_dir.mkdir()
        (summaries_dir / "doc1.md").write_text(
            "---\n---\n\nNo concept links here.",
            encoding="utf-8",
        )

        issues = check_book_coverage(tmp_path)
        assert len(issues) == 1
        assert issues[0].severity == Severity.WARNING
        assert "doc1" in issues[0].title

    def test_passes_summary_with_concepts(self, tmp_path):
        """Summary with concept links passes."""
        summaries_dir = tmp_path / "summaries"
        summaries_dir.mkdir()
        (summaries_dir / "doc1.md").write_text(
            "---\n---\n\nSee [[concepts/attention]]",
            encoding="utf-8",
        )

        issues = check_book_coverage(tmp_path)
        assert len(issues) == 0


class TestFormatSeverityReport:
    def test_formats_severity_sections(self):
        issues = [
            LintIssue(Severity.CRITICAL, "error", "Bad", "Broken", "file.md", "Fix it"),
            LintIssue(Severity.WARNING, "coverage", "Low", "1 source", "f.md", "Add more"),
            LintIssue(Severity.INFO, "style", "Nit", "Formatting", "f.md", "Optional"),
        ]
        report = format_severity_report(issues, "Structural OK", "Semantic OK")
        assert "Critical | 1" in report
        assert "Warning | 1" in report
        assert "Info | 1" in report
        assert "Bad" in report
        assert "Low" in report
        assert "Nit" in report

    def test_empty_issues(self):
        report = format_severity_report([], "Structural", "Semantic")
        assert "Critical | 0" in report
        assert "Structural" in report
