"""Tests for deep study feature (Phases 1-3)."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from openkb.agent.compiler import (
    _format_callout,
    _format_deep_study,
    _sanitize_concept_name,
    _write_concept,
    generate_deep_study_for_concept,
)


class TestFormatCallout:
    def test_simple_content(self):
        result = _format_callout("tip", "ELI5", "Simple explanation")
        assert result.startswith("> [!tip]- ELI5\n> ")
        assert "Simple explanation" in result

    def test_multiline_content(self):
        result = _format_callout("warning", "Misconceptions", "Line 1\nLine 2")
        assert "> Line 1" in result
        assert "> Line 2" in result

    def test_question_type(self):
        result = _format_callout("question", "Check", "Q: test\nA: answer")
        assert "> [!question]- Check" in result

    def test_info_type(self):
        result = _format_callout("info", "Why It Matters", "Important stuff")
        assert "> [!info]- Why It Matters" in result


class TestFormatDeepStudy:
    def test_full_deep_study(self):
        ds = {
            "eli5": "Think of it like X",
            "analogy": "Like a thermostat",
            "misconceptions": "**Myth:** A **Reality:** B",
            "questions": "**Q:** What? **A:** This.",
            "why_it_matters": "It matters because Y",
        }
        result = _format_deep_study(ds)
        assert "> [!tip]- ELI5" in result
        assert "> [!tip]- Real-World Analogy" in result
        assert "> [!warning]- Common Misconceptions" in result
        assert "> [!question]- Check Your Understanding" in result
        assert "> [!info]- Why It Matters" in result

    def test_partial_deep_study(self):
        ds = {"eli5": "Simple", "why_it_matters": "Important"}
        result = _format_deep_study(ds)
        assert "> [!tip]- ELI5" in result
        assert "> [!info]- Why It Matters" in result
        assert "Misconceptions" not in result

    def test_empty_dict(self):
        assert _format_deep_study({}) == ""

    def test_none_values_skipped(self):
        ds = {"eli5": None, "analogy": "Good analogy"}
        result = _format_deep_study(ds)
        assert "ELI5" not in result
        assert "Real-World Analogy" in result

    def test_list_values_normalized(self):
        ds = {"eli5": ["Think of it like X", "Where X is simple"], "questions": ["Q: What?", "A: This."]}
        result = _format_deep_study(ds)
        assert "> [!tip]- ELI5" in result
        assert "> Think of it like X" in result
        assert "> Where X is simple" in result
        assert "> [!question]- Check Your Understanding" in result
        assert "openkb-deep-study-start" in result


class TestWriteConceptDeepStudy:
    def _make_wiki_dir(self, tmp_path):
        wiki_dir = tmp_path / "wiki"
        (wiki_dir / "concepts").mkdir(parents=True)
        return wiki_dir

    def test_new_concept_with_deep_study(self, tmp_path):
        wiki_dir = self._make_wiki_dir(tmp_path)
        ds = {
            "eli5": "Simple explanation",
            "analogy": "Like a thermostat",
            "questions": "**Q:** What? **A:** This.",
        }
        _write_concept(wiki_dir, "test-concept", "Body content", "summaries/doc.md",
                       False, deep_study=ds)
        path = wiki_dir / "concepts" / "test-concept.md"
        content = path.read_text()
        assert "> [!tip]- ELI5" in content
        assert "> Simple explanation" in content
        assert "> [!tip]- Real-World Analogy" in content
        assert "> [!question]- Check Your Understanding" in content
        # Body content still present
        assert "Body content" in content

    def test_new_concept_without_deep_study(self, tmp_path):
        wiki_dir = self._make_wiki_dir(tmp_path)
        _write_concept(wiki_dir, "test-concept", "Body content", "summaries/doc.md",
                       False, deep_study=None)
        path = wiki_dir / "concepts" / "test-concept.md"
        content = path.read_text()
        assert "ELI5" not in content
        assert "Body content" in content

    def test_new_concept_empty_deep_study(self, tmp_path):
        wiki_dir = self._make_wiki_dir(tmp_path)
        _write_concept(wiki_dir, "test-concept", "Body", "summaries/doc.md",
                       False, deep_study={})
        path = wiki_dir / "concepts" / "test-concept.md"
        content = path.read_text()
        assert "ELI5" not in content

    def test_tracking_fields_in_new_concept(self, tmp_path):
        wiki_dir = self._make_wiki_dir(tmp_path)
        _write_concept(wiki_dir, "test-concept", "Body", "summaries/doc.md", False)
        path = wiki_dir / "concepts" / "test-concept.md"
        content = path.read_text()
        assert "understanding_level: unreviewed" in content
        assert "last_reviewed: null" in content
        assert "review_count: 0" in content

    def test_update_preserves_tracking(self, tmp_path):
        wiki_dir = self._make_wiki_dir(tmp_path)
        # Create with tracking
        _write_concept(wiki_dir, "test-concept", "Old body", "summaries/doc.md", False)
        path = wiki_dir / "concepts" / "test-concept.md"

        # Manually update tracking to simulate user study
        content = path.read_text()
        content = content.replace("understanding_level: unreviewed", "understanding_level: understood")
        content = content.replace("review_count: 0", "review_count: 3")
        path.write_text(content)

        # Update concept
        _write_concept(wiki_dir, "test-concept", "New body", "summaries/doc2.md",
                       True, brief="Updated")
        content = path.read_text()
        assert "understanding_level: understood" in content
        assert "review_count: 3" in content
        assert "New body" in content

    def test_update_replaces_old_deep_study(self, tmp_path):
        wiki_dir = self._make_wiki_dir(tmp_path)
        ds_old = {"eli5": "Old explanation"}
        _write_concept(wiki_dir, "test-concept", "Body", "summaries/doc.md",
                       False, deep_study=ds_old)
        path = wiki_dir / "concepts" / "test-concept.md"
        content_before = path.read_text()
        assert "Old explanation" in content_before

        ds_new = {"eli5": "New explanation"}
        _write_concept(wiki_dir, "test-concept", "Body updated", "summaries/doc2.md",
                       True, deep_study=ds_new)
        content_after = path.read_text()
        assert "New explanation" in content_after
        assert "Old explanation" not in content_after


class TestGenerateDeepStudyForConcept:
    def test_skips_existing_deep_study(self, tmp_path):
        path = tmp_path / "test.md"
        path.write_text("---\ntype: concept\n---\n\nBody\n\n<!-- openkb-deep-study-start -->\n> [!tip]- ELI5\n> Already here\n<!-- openkb-deep-study-end -->")
        result = generate_deep_study_for_concept(path, "gpt-4o-mini", tmp_path)
        assert result is False

    @patch("openkb.agent.compiler._llm_call")
    def test_generates_deep_study(self, mock_llm, tmp_path):
        mock_llm.return_value = json.dumps({
            "deep_study": {
                "eli5": "Simple",
                "analogy": "Analogy",
                "misconceptions": "Myth",
                "questions": "Q&A",
                "why_it_matters": "Important",
            }
        })
        path = tmp_path / "concept.md"
        path.write_text("---\ntype: concept\n---\n\nBody content")
        result = generate_deep_study_for_concept(path, "gpt-4o-mini", tmp_path)
        assert result is True
        content = path.read_text()
        assert "> [!tip]- ELI5" in content
        assert "> Simple" in content
        assert "Body content" in content

    @patch("openkb.agent.compiler._llm_call")
    def test_handles_llm_failure(self, mock_llm, tmp_path):
        mock_llm.return_value = "not valid json at all"
        path = tmp_path / "concept.md"
        path.write_text("---\ntype: concept\n---\n\nBody")
        result = generate_deep_study_for_concept(path, "gpt-4o-mini", tmp_path)
        assert result is False
