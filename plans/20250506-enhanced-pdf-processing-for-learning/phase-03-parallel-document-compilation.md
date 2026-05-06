---
phase: 3
title: "Parallel Document Compilation"
status: pending
priority: P2
effort: "2d"
dependencies: []
---

# Phase 3: Parallel Document Compilation

## Overview

Enable document-level parallel compilation so multiple books can compile simultaneously. Currently `add_single_file()` processes files sequentially in a for-loop. Add `compile_batch()` with asyncio rate-limiting semaphore and post-compilation concept merge.

## Requirements

- **Functional:**
  - Multiple documents compile concurrently
  - API rate limiting via configurable semaphore
  - Post-compilation merge for overlapping concepts
  - Progress reporting: `[{completed}/{total}] document-name`
  - Error isolation: one doc failure doesn't stop others

- **Non-functional:**
  - Configurable concurrency (default: 3 parallel)
  - Compatible with both short-doc and segment compilation
  - No file write conflicts between concurrent compilations

## Architecture

### Current (sequential)
```python
# cli.py add command
for i, f in enumerate(files, 1):
    click.echo(f"\n[{i}/{total}] ", nl=False)
    add_single_file(f, kb_dir)
```

### New (parallel)
```python
# cli.py add command (directory mode)
await compile_batch(files, kb_dir, model, max_concurrency=3)

# compile_batch:
async def compile_batch(files, kb_dir, model, max_concurrency=3):
    semaphore = asyncio.Semaphore(max_concurrency)
    results = await asyncio.gather(
        *[_compile_with_semaphore(f, kb_dir, model, semaphore) for f in files],
        return_exceptions=True
    )
    # Merge concepts from all documents
    await merge_cross_doc_concepts(kb_dir)
```

### Concurrency Strategy

```
                    ┌─ compile_short_doc(book1) → concepts + summary
Semaphore(3) ──────┼─ compile_short_doc(book2) → concepts + summary
                    └─ compile_short_doc(book3) → concepts + summary
                                         ↓
                    merge_cross_doc_concepts() → deduplicate, merge
```

Key: concept files are independent per document. Merge happens after ALL docs complete.

## Related Code Files

- **Modify:** `openkb/cli.py` — Add parallel `add` mode
- **Modify:** `openkb/agent/compiler.py` — Add `compile_batch()` function
- **Modify:** `openkb/merger.py` (from Phase 1) — Extend for cross-doc merging
- **Modify:** `openkb/config.py` — Add `compile_concurrency: 3`
- **Modify:** `openkb/cli.py` — Update `add_single_file()` to async variant

## Implementation Steps

### Step 1: Add `compile_batch()` to `compiler.py`

```python
async def compile_batch(
    doc_tasks: list[dict],
    kb_dir: Path,
    model: str,
    max_concurrency: int = 3,
) -> list[dict]:
    """Compile multiple documents in parallel with rate limiting.

    Args:
        doc_tasks: [{"doc_name": str, "source_path": Path, "doc_type": "short"|"pageindex"}]
        max_concurrency: Max simultaneous LLM sessions

    Returns:
        List of {"doc_name": str, "status": "ok"|"error", "error": str|None}
    """
    semaphore = asyncio.Semaphore(max_concurrency)

    async def _compile_one(task: dict) -> dict:
        async with semaphore:
            try:
                if task["doc_type"] == "short":
                    await compile_short_doc(task["doc_name"], task["source_path"], kb_dir, model)
                else:
                    await compile_long_doc(task["doc_name"], task["summary_path"], task["doc_id"], kb_dir, model)
                return {"doc_name": task["doc_name"], "status": "ok", "error": None}
            except Exception as e:
                return {"doc_name": task["doc_name"], "status": "error", "error": str(e)}

    results = await asyncio.gather(*[_compile_one(t) for t in doc_tasks])

    # Progress callback per completion
    completed = sum(1 for r in results if r["status"] == "ok")
    failed = sum(1 for r in results if r["status"] == "error")
    click.echo(f"  Compiled {completed}/{len(doc_tasks)} documents ({failed} failed)")

    return list(results)
```

### Step 2: Add progress callback

```python
import tqdm

class BatchProgress:
    """Track parallel compilation progress."""

    def __init__(self, total: int):
        self.bar = tqdm.tqdm(total=total, desc="Compiling")
        self.lock = asyncio.Lock()

    async def update(self, doc_name: str, status: str):
        async with self.lock:
            self.bar.set_postfix_str(f"Last: {doc_name[:30]}")
            self.bar.update(1)

    def close(self):
        self.bar.close()
```

### Step 3: Extend `merger.py` for cross-doc merging

```python
async def merge_cross_doc_concepts(wiki_dir: Path, model: str) -> dict:
    """Merge overlapping concepts from different documents.

    After parallel compilation, multiple docs may create similar concepts.
    This function:
    1. Reads all concept pages
    2. Groups by similarity (name-based first, then content-based)
    3. For each group: either merge (LLM-assisted) or keep separate

    Returns stats: {"groups_found": N, "merged": N, "kept_separate": N}
    """
    concepts_dir = wiki_dir / "concepts"
    concepts = []
    for md in concepts_dir.glob("*.md"):
        text = md.read_text(encoding="utf-8")
        # Parse frontmatter for sources
        sources = _parse_frontmatter_sources(text)
        concepts.append({"slug": md.stem, "path": md, "sources": sources})

    # Group by name similarity
    groups = _group_similar_concepts(concepts)

    merged = 0
    for group in groups:
        if len(group) <= 1:
            continue
        if _should_merge(group):
            _merge_concept_group(group, wiki_dir, model)
            merged += 1

    return {"groups_found": len(groups), "merged": merged, "kept_separate": len(groups) - merged}


def _should_merge(group: list[dict]) -> bool:
    """Determine if similar concepts should be merged."""
    # If they share >50% content topics → merge
    # If from same document → never merge (same doc won't duplicate)
    sources_sets = [set(c["sources"]) for c in group]
    # All from same source doc → likely false positive
    if all(s == sources_sets[0] for s in sources_sets):
        return False
    return True
```

### Step 4: Update CLI for parallel mode

```python
# In cli.py add command, directory branch:
if target.is_dir():
    files = [f for f in sorted(target.rglob("*"))
             if f.is_file() and f.suffix.lower() in SUPPORTED_EXTENSIONS]
    total = len(files)
    click.echo(f"Found {total} supported file(s) in {path}.")

    config_concurrency = config.get("compile_concurrency", 3)

    if total > 1 and config_concurrency > 1:
        # Parallel compilation
        click.echo(f"Compiling in parallel (concurrency={config_concurrency})...")
        asyncio.run(_add_files_parallel(files, kb_dir, config_concurrency))
    else:
        # Sequential (original behavior)
        for i, f in enumerate(files, 1):
            click.echo(f"\n[{i}/{total}] ", nl=False)
            add_single_file(f, kb_dir)
```

### Step 5: Handle file write conflicts

Concept files are per-document during compilation, so no conflicts during write.
Only the merge phase modifies shared files (index.md, cross-linked concepts).

Strategy:
- Each compilation writes to its own concept files (slug-based, unique per doc)
- `index.md` updates are atomic (read → modify → write with file lock)
- Cross-link phase happens after all compilations complete (in merge step)

```python
import fcntl

def atomic_update_file(path: Path, update_fn):
    """Update a file atomically using file lock."""
    with open(path, 'r+') as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        content = f.read()
        updated = update_fn(content)
        f.seek(0)
        f.write(updated)
        f.truncate()
        fcntl.flock(f, fcntl.LOCK_UN)
```

### Step 6: Add config option

```python
# config.py
"compile_concurrency": 3,  # Max parallel document compilations
```

## Success Criteria

- [ ] Multiple documents compile concurrently (visible via progress bar)
- [ ] API rate limiting via semaphore prevents throttling
- [ ] One document failure doesn't affect others
- [ ] Post-compilation merge detects overlapping concepts
- [ ] index.md updated correctly without conflicts
- [ ] Configurable concurrency via config.yaml
- [ ] Sequential mode still works (concurrency=1 or single file)
- [ ] Existing tests pass
- [ ] New tests for batch compilation, merge logic

## Risk Assessment

| Risk | Impact | Mitigation |
|------|--------|------------|
| API rate limiting from provider | High | Semaphore + configurable concurrency |
| index.md write conflicts | Medium | File locking (fcntl) |
| Concept merge quality | Medium | Conservative merge (only high-similarity); LLM-assisted when ambiguous |
| Memory pressure with many docs | Low | Process in chunks if >10 docs |
| Progress reporting accuracy | Low | tqdm + async lock for updates |
