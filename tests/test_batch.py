"""Tests for openkb.agent.batch."""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from openkb.agent.batch import compile_batch

# compile_short_doc is imported inside the function body, patch at source
_COMPILE_DOC = "openkb.agent.compiler.compile_short_doc"


class TestCompileBatch:
    def test_single_doc_success(self, tmp_path):
        """Single document compiles and returns ok status."""
        task = {"doc_name": "test-doc", "source_path": tmp_path / "source.md"}
        task["source_path"].write_text("# Test", encoding="utf-8")

        with patch(_COMPILE_DOC, new_callable=AsyncMock):
            results = asyncio.run(compile_batch([task], tmp_path, "gpt-5.4-mini"))

        assert len(results) == 1
        assert results[0]["status"] == "ok"
        assert results[0]["doc_name"] == "test-doc"

    def test_multiple_docs_parallel(self, tmp_path):
        """Multiple documents compile with semaphore limiting."""
        tasks = []
        for i in range(5):
            sp = tmp_path / f"source{i}.md"
            sp.write_text(f"# Doc {i}", encoding="utf-8")
            tasks.append({"doc_name": f"doc-{i}", "source_path": sp})

        with patch(_COMPILE_DOC, new_callable=AsyncMock):
            results = asyncio.run(compile_batch(tasks, tmp_path, "gpt-5.4-mini", max_concurrency=2))

        assert len(results) == 5
        assert all(r["status"] == "ok" for r in results)

    def test_error_isolation(self, tmp_path):
        """One failure does not stop other compilations."""
        tasks = []
        for i in range(3):
            sp = tmp_path / f"source{i}.md"
            sp.write_text(f"# Doc {i}", encoding="utf-8")
            tasks.append({"doc_name": f"doc-{i}", "source_path": sp})

        async def _mock_compile(doc_name, source_path, kb_dir, model):
            if doc_name == "doc-1":
                raise RuntimeError("API error")

        with patch(_COMPILE_DOC, side_effect=_mock_compile):
            results = asyncio.run(compile_batch(tasks, tmp_path, "gpt-5.4-mini"))

        statuses = [r["status"] for r in results]
        assert statuses.count("ok") == 2
        assert statuses.count("error") == 1
        error_result = next(r for r in results if r["status"] == "error")
        assert "API error" in error_result["error"]

    def test_semaphore_limits_concurrency(self, tmp_path):
        """Semaphore enforces max_concurrency."""
        max_seen = 0
        current = 0
        lock = asyncio.Lock()

        async def _track_concurrency(doc_name, source_path, kb_dir, model):
            nonlocal max_seen, current
            async with lock:
                current += 1
                max_seen = max(max_seen, current)
            await asyncio.sleep(0.01)
            async with lock:
                current -= 1

        tasks = [
            {"doc_name": f"doc-{i}", "source_path": tmp_path / f"s{i}.md"}
            for i in range(6)
        ]
        for t in tasks:
            t["source_path"].write_text("# x", encoding="utf-8")

        with patch(_COMPILE_DOC, side_effect=_track_concurrency):
            asyncio.run(compile_batch(tasks, tmp_path, "gpt-5.4-mini", max_concurrency=2))

        assert max_seen <= 2

    def test_empty_task_list(self, tmp_path):
        """Empty list returns empty results."""
        results = asyncio.run(compile_batch([], tmp_path, "gpt-5.4-mini"))
        assert results == []
