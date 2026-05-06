# Brainstorm Report: Upgrade Command for Existing Data

## Problem

After adding image description feature, existing books in wiki/ don't benefit. Hash registry skips known files. No mechanism to retroactively apply new features to existing data.

## Requirements

- Retroactively apply image description to existing wiki/sources/*.md
- Re-process existing markdown only (no PDF re-conversion)
- Skip re-compilation (no LLM calls for summaries/concepts)
- Idempotent: images already with descriptions are skipped

## Agreed Design

### `openkb upgrade --describe-images`

```
Scan wiki/sources/*.md
    ↓
For each file:
    Find ![image](path) WITHOUT *[Figure: ...]* below
    ↓
    describe_images() ← existing VLM module
    ↓
    Overwrite wiki/sources/{name}.md
```

### Key Logic

- Detect images missing descriptions: regex scan for `![image](...)` NOT followed by `*[Figure:`
- Only call VLM for undescribed images (idempotent)
- Reuses `describe_images()` from image_describer.py — no new VLM code

### Files

- MODIFY: `openkb/cli.py` — add `upgrade` command (~40 lines)
- NEW: `tests/test_upgrade_cli.py` — 3-4 tests

### Effort

~1 hour. Minimal risk — purely additive CLI command.
