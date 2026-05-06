---
phase: 2
title: Integrate into Converter Pipeline
status: completed
priority: P1
effort: 2h
dependencies:
  - 1
---

# Phase 2: Integrate into Converter Pipeline

## Overview

Wire `describe_images()` into `converter.py` at 2 call sites so images are described before markdown is saved to `wiki/sources/`.

## Requirements
- Functional: Call `describe_images()` after PDF conversion, before saving markdown
- Non-functional: Respect `describe_images` config flag, no performance impact when disabled

## Architecture

```
convert_document() / convert_segment():
  PDF → convert_pdf_*() → markdown
                                ↓
                  if config.describe_images:
                      markdown = describe_images(markdown, kb_dir, vision_model)
                                ↓
                  save to wiki/sources/
```

## Related Code Files
- Modify: `openkb/converter.py` (2 call sites)
- Modify: `tests/test_converter.py` or add integration test

## Implementation Steps

1. **Modify `convert_document()` in `openkb/converter.py`**
   - After line 117 (markdown generated), before line 125 (dest_md.write_text):
   ```python
   # Describe images via VLM if enabled
   if config.get("describe_images", True):
       from openkb.image_describer import describe_images
       vision_model = config.get("vision_model") or config.get("model", "gpt-4o-mini")
       markdown = describe_images(markdown, kb_dir, vision_model)
   ```

2. **Modify `convert_segment()` in `openkb/converter.py`**
   - After line 157 (markdown from convert_pdf_with_images), before header prepend:
   ```python
   # Describe images via VLM if enabled
   openkb_dir = kb_dir / ".openkb"
   seg_config = load_config(openkb_dir / "config.yaml")
   if seg_config.get("describe_images", True):
       from openkb.image_describer import describe_images
       vision_model = seg_config.get("vision_model") or seg_config.get("model", "gpt-4o-mini")
       markdown = describe_images(markdown, kb_dir, vision_model)
   ```

3. **Add integration tests**
   - `test_convert_describes_images` — end-to-end: mock VLM, verify description in output markdown
   - `test_convert_skips_description_when_disabled` — config `describe_images: false`, no VLM calls
   - `test_segment_describes_images` — segment path also enriches images

## Success Criteria
- [ ] Images described before saving to wiki/sources/ for single docs
- [ ] Images described for PDF segments
- [ ] Feature toggleable via `describe_images: false`
- [ ] All existing tests still pass
- [ ] Integration tests pass

## Risk Assessment
- **Double VLM calls for segments** — segments are sub-documents of large PDFs, each image described once per segment. Acceptable since images are already split per page.
- **Re-compilation** — if document is re-compiled, images get re-described. User accepted cost tradeoff.
