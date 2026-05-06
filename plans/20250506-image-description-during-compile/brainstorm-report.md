# Brainstorm Report: Image Description During Compile

## Problem

Images extracted from PDFs sit dormant — linked in markdown but never analyzed by LLMs during compilation. For textbooks with diagrams, charts, flowcharts, this means critical visual knowledge is invisible to summaries and concepts.

**Current flow:** `PDF → extract images as PNG → ![image](path) in markdown → compile text-only → concepts miss diagram knowledge`

## User Requirements

- Auto-describe images during compile (not query-time only)
- Mixed image types: diagrams, charts, screenshots, flowcharts
- Quality at all costs — use most capable vision model
- Pre-processing approach: describe images, inject into markdown BEFORE compilation
- Keep images AND add descriptions below them in markdown

## Evaluated Approaches

### A. Pre-processing: describe then compile (CHOSEN)
- After PDF conversion, scan markdown for `![image](path)` references
- Call VLM (via litellm) for each image → generate text description
- Inject description below image: `![image](path)\n\n*[Figure: ...]*`
- Existing compilation pipeline unchanged — just sees richer text

**Pros:** Minimal code change, no pipeline restructuring, descriptions flow naturally into summaries/concepts
**Cons:** Extra VLM calls add cost and time per document

### B. In-line: vision during compilation
- Send images alongside text to VLM during compile step
- More holistic understanding but much more complex integration

**Pros:** LLM sees image in context of surrounding text
**Cons:** Major refactor of compiler, expensive, hard to test, multiplies token usage

### C. Query-time only (current behavior enhanced)
- Enhance query agent to always fetch relevant images when answering

**Pros:** No compile cost
**Cons:** Images still invisible to concepts/summaries, only helps Q&A

## Agreed Design

### Architecture

```
PDF → convert_pdf_*() → markdown with ![image](path)
                                    ↓
                    describe_images() ← VLM calls (litellm)
                                    ↓
                enriched markdown (image + *[Figure: ...]*)
                                    ↓
                    save to wiki/sources/
                                    ↓
                    compile as usual (unchanged)
```

### New Module: `openkb/image_describer.py`

- `describe_images(markdown: str, kb_dir: Path, model: str) -> str`
  - Scan for `![image](path)` patterns
  - For each: read image file, encode base64, call VLM
  - Inject `*[Figure: {description}]*` below each image reference
  - Skip images < 32x32 pixels (already filtered in extraction)

### VLM Call Pattern (via litellm)

```python
from litellm import completion
import base64

# Read image, encode base64
image_bytes = image_path.read_bytes()
b64 = base64.b64encode(image_bytes).decode()

# Call VLM
response = completion(
    model=f"litellm/{model}",
    messages=[{
        "role": "user",
        "content": [
            {"type": "text", "text": DESCRIBE_PROMPT},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}
        ]
    }],
    max_tokens=500,
    temperature=0.3,
)
description = response.choices[0].message.content
```

### Description Prompt

```
Analyze this image from an educational textbook. Describe:
1. What the image shows (diagram, chart, flowchart, screenshot, etc.)
2. All visible labels, axes, and text elements
3. Relationships, connections, or processes illustrated
4. Key information conveyed that text alone cannot capture

Be concise but thorough. Focus on knowledge content, not visual style.
```

### Config Additions

```yaml
describe_images: true       # Enable/disable image description
vision_model: null          # Defaults to main model if null
```

### Integration Points (converter.py)

1. `convert_document()` — after PDF/MD conversion, before saving to `wiki/sources/`
2. `convert_segment()` — after segment conversion, before appending to source file

### Output Format in Markdown

```markdown
![image](sources/images/book/p5_img1.png)

*[Figure: A feedforward neural network architecture with 3 layers. Input layer has 784 nodes (28×28 pixels), hidden layer has 256 nodes with ReLU activation, output layer has 10 nodes with softmax. Arrows show full connectivity between adjacent layers.]*
```

## Files

| Action | File | Purpose |
|--------|------|---------|
| CREATE | `openkb/image_describer.py` | ~80 lines, core VLM description logic |
| MODIFY | `openkb/converter.py` | 2 call sites to invoke describer |
| MODIFY | `openkb/config.py` | 2 new config keys |
| CREATE | `tests/test_image_describer.py` | Unit tests for describer |

## Effort & Risk

- **Effort:** 1-2 days
- **Risk:** Low — no pipeline changes, purely additive. VLM call failures are logged and skipped gracefully
- **Cost:** ~$0.01-0.02 per image with gpt-4o. A 400-page textbook with ~100 images ≈ $1-2 total

## Unresolved Questions

- Should we skip images that are likely decorative (page borders, publisher logos)? Current `_MIN_IMAGE_DIM = 32` filter handles most.
- Should descriptions be cached to avoid re-calling VLM on re-runs? User said quality at all costs, so YAGNI for now.
