"""Tests for the openkb upgrade CLI command."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from openkb.cli import cli


def _setup_kb(tmp_path: Path) -> Path:
    """Create a minimal KB structure with sources."""
    kb_dir = tmp_path
    (kb_dir / "raw").mkdir()
    (kb_dir / "wiki" / "sources" / "images" / "doc").mkdir(parents=True)
    (kb_dir / "wiki" / "summaries").mkdir(parents=True)
    (kb_dir / "wiki" / "concepts").mkdir(parents=True)
    (kb_dir / "wiki" / "reports").mkdir(parents=True)
    openkb_dir = kb_dir / ".openkb"
    openkb_dir.mkdir()
    (openkb_dir / "config.yaml").write_text("model: gpt-4o-mini\n")
    (openkb_dir / "hashes.json").write_text(json.dumps({"abc": {"name": "doc.md", "type": "md"}}))
    (kb_dir / "wiki" / "index.md").write_text("# Knowledge Base Index\n")
    return kb_dir


class TestUpgradeCommand:
    def test_no_flag_prints_usage(self, tmp_path):
        kb_dir = _setup_kb(tmp_path)
        runner = CliRunner()
        with patch("openkb.cli._find_kb_dir", return_value=kb_dir):
            result = runner.invoke(cli, ["upgrade"])
        assert "No upgrade step specified" in result.output

    def test_describe_images_no_sources(self, tmp_path):
        kb_dir = _setup_kb(tmp_path)
        # Remove all source files
        for f in (kb_dir / "wiki" / "sources").glob("*.md"):
            f.unlink()
        runner = CliRunner()
        with patch("openkb.cli._find_kb_dir", return_value=kb_dir), \
             patch("openkb.cli._setup_llm_key"):
            result = runner.invoke(cli, ["upgrade", "--describe-images"])
        assert "No source files" in result.output

    def test_describe_images_all_already_described(self, tmp_path):
        kb_dir = _setup_kb(tmp_path)
        sources_dir = kb_dir / "wiki" / "sources"
        (sources_dir / "doc.md").write_text(
            "![img](sources/images/doc/x.png)\n\n*[Figure: Already described.]*\n"
        )
        runner = CliRunner()
        with patch("openkb.cli._find_kb_dir", return_value=kb_dir), \
             patch("openkb.cli._setup_llm_key"):
            result = runner.invoke(cli, ["upgrade", "--describe-images"])
        assert "already have descriptions" in result.output

    def test_describe_images_adds_descriptions(self, tmp_path):
        kb_dir = _setup_kb(tmp_path)
        sources_dir = kb_dir / "wiki" / "sources"
        # Create image file
        img_path = kb_dir / "wiki" / "sources" / "images" / "doc" / "fig.png"
        img_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 92)

        (sources_dir / "doc.md").write_text(
            "![img](sources/images/doc/fig.png)\n\nSome text.\n"
        )

        from unittest.mock import MagicMock
        msg = MagicMock()
        msg.content = "A bar chart showing growth."
        choice = MagicMock()
        choice.message = msg
        resp = MagicMock()
        resp.choices = [choice]

        runner = CliRunner()
        with patch("openkb.cli._find_kb_dir", return_value=kb_dir), \
             patch("openkb.cli._setup_llm_key"), \
             patch("openkb.image_describer.completion", return_value=resp):
            result = runner.invoke(cli, ["upgrade", "--describe-images"])

        assert result.exit_code == 0
        assert "1 images" in result.output
        content = (sources_dir / "doc.md").read_text()
        assert "*[Figure: A bar chart showing growth.]*" in content

    def test_no_kb(self, tmp_path):
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path), \
             patch("openkb.cli._find_kb_dir", return_value=None):
            result = runner.invoke(cli, ["upgrade", "--describe-images"])
        assert "No knowledge base found" in result.output
