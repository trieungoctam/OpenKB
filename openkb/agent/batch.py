"""Parallel document compilation for OpenKB.

Provides ``compile_batch`` for compiling multiple short documents concurrently
with asyncio semaphore rate limiting.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import click

logger = logging.getLogger(__name__)


async def compile_batch(
    doc_tasks: list[dict],
    kb_dir: Path,
    model: str,
    max_concurrency: int = 3,
) -> list[dict]:
    """Compile multiple short documents in parallel with rate limiting.

    Each task compiles independently — one failure does not stop others.
    Progress is reported per completion.

    Args:
        doc_tasks: List of ``{"doc_name": str, "source_path": Path}``
        kb_dir: Knowledge base root directory.
        model: LLM model identifier.
        max_concurrency: Max simultaneous LLM sessions.

    Returns:
        List of ``{"doc_name": str, "status": "ok"|"error", "error": str|None}``
    """
    from openkb.agent.compiler import compile_short_doc

    semaphore = asyncio.Semaphore(max_concurrency)
    total = len(doc_tasks)
    counter = {"done": 0}

    async def _compile_one(task: dict) -> dict:
        async with semaphore:
            try:
                await compile_short_doc(
                    task["doc_name"], task["source_path"], kb_dir, model,
                )
                counter["done"] += 1
                click.echo(f"  [{counter['done']}/{total}] done: {task['doc_name']}")
                return {"doc_name": task["doc_name"], "status": "ok", "error": None}
            except Exception as e:
                counter["done"] += 1
                logger.warning("Compilation failed: %s — %s", task["doc_name"], e)
                click.echo(f"  [{counter['done']}/{total}] FAILED: {task['doc_name']}: {e}")
                return {"doc_name": task["doc_name"], "status": "error", "error": str(e)}

    results = await asyncio.gather(*[_compile_one(t) for t in doc_tasks])

    ok = sum(1 for r in results if r["status"] == "ok")
    click.echo(f"  Compiled {ok}/{total} documents")
    return list(results)
