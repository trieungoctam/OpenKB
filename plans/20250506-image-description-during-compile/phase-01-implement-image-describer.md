---
phase: 1
title: Implement Image Describer
status: completed
priority: P1
effort: 4h
dependencies: []
---

# Phase 1: Implement Image Describer

## Overview

Create `openkb/image_describer.py` — a module that scans markdown for image references, calls VLM to describe each image, and injects descriptions below them.

## Requirements
- Functional: Scan markdown for `![image](path)`, call VLM per image, inject `*[Figure: ...]*` below each
- Non-functional: Graceful failure on VLM errors, <100 lines, no external deps beyond litellm

## Architecture

```
Input: markdown string with ![image](sources/images/doc/img.png) references
  ↓
Scan with regex to find all image references
  ↓
For each image:
  - Resolve path relative to kb_dir / "wiki"
  - Read bytes, encode base64
  - Call litellm completion with image_url content block
  - Extract description text
  ↓
Replace each ![image](path) with ![image](path)\n\n*[Figure: {desc}]*
  ↓
Return enriched markdown
```

## Related Code Files
- Create: `openkb/image_describer.py`
- Create: `tests/test_image_describer.py`
- Modify: `openkb/config.py` (add 2 config keys)

## Implementation Steps

1. **Add config keys to `openkb/config.py`**
   ```python
   # Add to DEFAULT_CONFIG:
   "describe_images": True,
   "vision_model": None,  # Falls back to main model
   ```

2. **Create `openkb/image_describer.py`**
   - `_IMAGE_RE = re.compile(r'!\[([^\]]*)\]\((?!https?://|data:)([^)]+)\)')` — reuse pattern from images.py
   - `_DESCRIBE_PROMPT` constant — educational image analysis prompt
   - `_encode_image(path: Path) -> str` — read file, return `data:image/png;base64,...`
   - `_call_vlm(base64_img: str, model: str) -> str` — litellm completion call
   - `describe_images(markdown: str, kb_dir: Path, model: str) -> str` — main entry point
     - Find all image refs with `_IMAGE_RE`
     - For each: resolve path, encode, call VLM, inject description
     - Skip if file doesn't exist or VLM fails (log warning)
     - Return modified markdown

3. **Write tests `tests/test_image_describer.py`**
   - `test_no_images_returns_unchanged` — markdown without images passes through
   - `test_describe_single_image` — mock litellm, verify description injected
   - `test_describe_multiple_images` — multiple refs, multiple VLM calls
   - `test_missing_image_file_skipped` — image path doesn't exist, graceful skip
   - `test_vlm_failure_graceful` — mock VLM to raise, verify no crash
   - `test_image_retained_with_description` — original `![image](...)` preserved

## Success Criteria
- [ ] `describe_images()` correctly enriches markdown with image descriptions
- [ ] VLM failures logged but don't crash
- [ ] Missing image files skipped gracefully
- [ ] Original image references preserved
- [ ] All tests pass

## Risk Assessment
- **VLM API format** — litellm multimodal API well-documented, low risk
- **Token costs** — configurable via `vision_model`, user accepts costs
- **Large images** — pymupdf4llm extracts at 150 DPI, reasonable token usage
