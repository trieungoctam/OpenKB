"""VLM-powered image description for educational content."""
from __future__ import annotations

import base64
import logging
import re
from pathlib import Path
from urllib.parse import unquote

from litellm import completion

logger = logging.getLogger(__name__)

_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\((?!https?://|data:)(.+?\.(?:png|jpe?g|gif|webp|svg))\)")

_DESCRIBE_PROMPT = """\
Analyze this image from an educational textbook. Describe:
1. What the image shows (diagram, chart, flowchart, screenshot, etc.)
2. All visible labels, axes, and text elements
3. Relationships, connections, or processes illustrated
4. Key information conveyed that text alone cannot capture

Be concise but thorough. Focus on knowledge content, not visual style."""

MAX_IMAGE_SIZE = 20 * 1024 * 1024  # 20MB


def _encode_image(image_path: Path) -> str:
    """Read image file and return base64 data URI."""
    ext = image_path.suffix.lstrip(".") or "png"
    image_bytes = image_path.read_bytes()
    b64 = base64.b64encode(image_bytes).decode()
    return f"data:image/{ext};base64,{b64}"


def _call_vlm(base64_img: str, model: str) -> str:
    """Call VLM via litellm and return description text."""
    response = completion(
        model=model,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": _DESCRIBE_PROMPT},
                    {
                        "type": "image_url",
                        "image_url": {"url": base64_img},
                    },
                ],
            }
        ],
        max_tokens=500,
        temperature=0.3,
    )
    content = response.choices[0].message.content  # type: ignore[union-attr]
    return (content or "").strip()


def _sanitize_description(text: str) -> str:
    """Clean VLM output for safe markdown injection."""
    text = text.replace("\n", " ").strip()
    text = re.sub(r"[!\[\]]", "", text)
    return text


def describe_images(
    markdown: str,
    kb_dir: Path,
    model: str,
    max_images: int = 50,
) -> str:
    """Scan markdown for image references, describe each via VLM, inject descriptions.

    For each ``![alt](path)`` found:
    - Resolve path relative to ``kb_dir / "wiki"``
    - Validate path stays within wiki directory (no traversal)
    - Call VLM to generate a text description
    - Insert ``*[Figure: {description}]*`` below the image reference

    VLM failures are logged and skipped — original markdown preserved for that image.

    Args:
        markdown: Markdown text containing image references.
        kb_dir: Knowledge base root directory.
        model: LiteLLM model name for VLM calls.
        max_images: Maximum images to describe per call (prevents runaway costs).

    Returns:
        Markdown with image descriptions injected.
    """
    wiki_dir = kb_dir / "wiki"
    matches = list(_IMAGE_RE.finditer(markdown))
    if not matches:
        return markdown

    vlm_model = model.removeprefix("litellm/")

    described = 0
    # Process matches in reverse to preserve positions
    for match in reversed(matches):
        if described >= max_images:
            logger.warning(
                "Reached max_images limit (%d), skipping remaining images",
                max_images,
            )
            break

        rel_path = unquote(match.group(2))
        image_path = (wiki_dir / rel_path).resolve()

        # Path traversal guard
        if not image_path.is_relative_to(wiki_dir.resolve()):
            logger.warning("Image path escapes wiki dir, skipping: %s", rel_path)
            continue

        if not image_path.exists():
            logger.warning("Image not found: %s (resolved: %s)", rel_path, image_path)
            continue

        # Size guard
        if image_path.stat().st_size > MAX_IMAGE_SIZE:
            logger.warning("Image too large for VLM (%d bytes): %s", image_path.stat().st_size, rel_path)
            continue

        try:
            base64_img = _encode_image(image_path)
            description = _call_vlm(base64_img, vlm_model)
        except Exception:
            logger.warning("VLM call failed for image: %s", rel_path, exc_info=True)
            continue

        description = _sanitize_description(description)
        if not description:
            continue

        # Inject description below the image reference
        image_line = match.group(0)
        replacement = f"{image_line}\n\n*[Figure: {description}]*"
        markdown = markdown[:match.start()] + replacement + markdown[match.end():]
        described += 1

    return markdown
