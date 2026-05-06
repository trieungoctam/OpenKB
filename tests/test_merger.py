"""Tests for concept merger module."""
from __future__ import annotations

from pathlib import Path

from openkb.merger import (
    _find_duplicate_groups,
    _parse_frontmatter_list,
    _strip_frontmatter,
    merge_cross_doc_concepts,
    merge_segment_concepts,
)


class TestParseFrontmatterList:
    def test_bracket_format(self):
        text = "---\nsources: [summaries/a.md, summaries/b.md]\n---\nBody"
        result = _parse_frontmatter_list(text, "sources")
        assert result == ["summaries/a.md", "summaries/b.md"]

    def test_yaml_list_format(self):
        text = "---\nsources:\n  - summaries/a.md\n  - summaries/b.md\n---\nBody"
        result = _parse_frontmatter_list(text, "sources")
        assert result == ["summaries/a.md", "summaries/b.md"]

    def test_no_frontmatter(self):
        result = _parse_frontmatter_list("Just body text", "sources")
        assert result == []

    def test_key_not_found(self):
        text = "---\nbrief: test\n---\nBody"
        result = _parse_frontmatter_list(text, "sources")
        assert result == []


class TestStripFrontmatter:
    def test_strips_frontmatter(self):
        text = "---\nkey: value\n---\nBody content"
        assert _strip_frontmatter(text) == "Body content"

    def test_no_frontmatter(self):
        text = "Just body"
        assert _strip_frontmatter(text) == "Just body"


class TestFindDuplicateGroups:
    def test_groups_similar_names(self):
        concepts = {
            "neural-networks": {"body": "long content " * 50},
            "neural-network": {"body": "short"},
            "deep-learning": {"body": "other"},
        }
        groups = _find_duplicate_groups(concepts)
        # neural-networks and neural-network should be grouped
        assert any(
            "neural-networks" in g and "neural-network" in g
            for g in groups
        )

    def test_no_duplicates(self):
        concepts = {
            "attention-mechanism": {"body": "a"},
            "backpropagation": {"body": "b"},
            "gradient-descent": {"body": "c"},
        }
        groups = _find_duplicate_groups(concepts)
        assert all(len(g) == 1 for g in groups)


class TestMergeSegmentConcepts:
    def test_merges_exact_duplicates(self, tmp_path):
        concepts_dir = tmp_path / "concepts"
        concepts_dir.mkdir()

        # Create two identical concept files
        (concepts_dir / "neural-networks.md").write_text(
            "---\nsources: [summaries/ch1.md]\nbrief: NN\n---\n\nNeural networks are... " * 5,
            encoding="utf-8",
        )
        (concepts_dir / "neural-network.md").write_text(
            "---\nsources: [summaries/ch2.md]\nbrief: NN short\n---\n\nShort version",
            encoding="utf-8",
        )

        # Create index
        wiki_dir = tmp_path
        (wiki_dir / "index.md").write_text(
            "# Index\n\n## Concepts\n\n- [[concepts/neural-networks]]\n- [[concepts/neural-network]]\n",
            encoding="utf-8",
        )

        result = merge_segment_concepts(wiki_dir, "test-book")
        assert result["merged"] >= 1

        # Verify keeper exists and duplicate removed
        remaining = [p.stem for p in concepts_dir.glob("*.md")]
        assert len(remaining) == 1
        assert "neural-networks" in remaining

    def test_empty_concepts_dir(self, tmp_path):
        result = merge_segment_concepts(tmp_path, "test")
        assert result == {"merged": 0, "kept": 0, "total": 0}

    def test_no_concepts_dir(self, tmp_path):
        result = merge_segment_concepts(tmp_path / "nonexistent", "test")
        assert result == {"merged": 0, "kept": 0, "total": 0}


class TestMergeCrossDocConcepts:
    def test_merges_same_concept_from_different_docs(self, tmp_path):
        """Concepts with similar names from different documents get merged."""
        concepts_dir = tmp_path / "concepts"
        concepts_dir.mkdir()

        (concepts_dir / "attention-mechanism.md").write_text(
            "---\nsources: [summaries/book1.md]\nbrief: Attention\n---\n\n"
            + "Attention is a mechanism in neural networks. " * 10,
            encoding="utf-8",
        )
        (concepts_dir / "attention-mechanisim.md").write_text(
            "---\nsources: [summaries/book2.md]\nbrief: Attention variant\n---\n\n"
            + "Short version",
            encoding="utf-8",
        )

        (tmp_path / "index.md").write_text(
            "# Index\n\n## Concepts\n\n"
            "- [[concepts/attention-mechanism]]\n- [[concepts/attention-mechanisim]]\n",
            encoding="utf-8",
        )

        result = merge_cross_doc_concepts(tmp_path)
        assert result["merged"] == 1
        remaining = [p.stem for p in concepts_dir.glob("*.md")]
        assert len(remaining) == 1

    def test_skips_same_doc_concepts(self, tmp_path):
        """Concepts from the same document are not merged (handled by segment merger)."""
        concepts_dir = tmp_path / "concepts"
        concepts_dir.mkdir()

        (concepts_dir / "neural-networks.md").write_text(
            "---\nsources: [summaries/book1.md]\n---\n\nContent A",
            encoding="utf-8",
        )
        (concepts_dir / "neural-network.md").write_text(
            "---\nsources: [summaries/book1.md]\n---\n\nContent B",
            encoding="utf-8",
        )

        (tmp_path / "index.md").write_text(
            "# Index\n\n## Concepts\n\n"
            "- [[concepts/neural-networks]]\n- [[concepts/neural-network]]\n",
            encoding="utf-8",
        )

        result = merge_cross_doc_concepts(tmp_path)
        assert result["merged"] == 0

    def test_empty_dir(self, tmp_path):
        result = merge_cross_doc_concepts(tmp_path / "nonexistent")
        assert result == {"merged": 0, "kept": 0, "total": 0}
