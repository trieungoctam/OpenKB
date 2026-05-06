"""Wiki compilation pipeline for OpenKB.

Pipeline leveraging LLM prompt caching:
  Step 1: Build base context A (schema + document content).
  Step 2: A → generate summary.
  Step 3: A + summary → concepts plan (create/update/related).
  Step 4: Concurrent LLM calls (A cached) → generate new + rewrite updated concepts.
  Step 5: Code adds cross-ref links to related concepts, updates index.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
import threading
import time
import unicodedata
from datetime import date
from pathlib import Path

import litellm

from openkb.schema import get_agents_md

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------

_SYSTEM_TEMPLATE = """\
You are OpenKB's wiki compilation agent for a personal knowledge base.

{schema_md}

Write all content in {language} language.
Use [[wikilinks]] to connect related pages (e.g. [[concepts/attention]]).
"""

_SUMMARY_USER = """\
New document: {doc_name}

Full text:
{content}

Write a summary page for this document in Markdown.

Return a JSON object with two keys:
- "brief": A single sentence (under 100 chars) describing the document's main contribution
- "content": The full summary in Markdown. Include key concepts, findings, ideas, \
and [[wikilinks]] to concepts that could become cross-document concept pages

Return ONLY valid JSON, no fences.
"""


_CONCEPTS_PLAN_USER = """\
Based on the summary above, decide how to update the wiki's concept pages.

Existing concept pages:
{concept_briefs}

Return a JSON object with three keys:

1. "create" — new concepts not covered by any existing page. Array of objects:
   {{"name": "short-slug", "title": "Human-Readable Title"}}

2. "update" — existing concepts that have significant new information from \
this document worth integrating. Array of objects:
   {{"name": "existing-slug", "title": "Existing Title"}}

3. "related" — existing concepts tangentially related to this document but \
not needing content changes, just a cross-reference link. Array of slug strings.

Rules:
- For the first few documents, create 2-3 foundational concepts at most.
- Do NOT create a concept that overlaps with an existing one — use "update".
- Do NOT create concepts that are just the document topic itself.
- "related" is for lightweight cross-linking only, no content rewrite needed.

Slug naming rules (CRITICAL):
- Use 1-3 words, lowercase, hyphenated. Examples: "lean-validation", \
"problem-interview", "innovation-accounting"
- NEVER use long phrases, sentences, or document titles as slugs
- The "title" field carries the full human-readable name; "name" is only a \
short identifier for the filename

Return ONLY valid JSON, no fences, no explanation.
"""

_CONCEPT_PAGE_USER = """\
Write the concept page for: {title}

This concept relates to the document "{doc_name}" summarized above.
{update_instruction}

Include a "## Sources & Perspectives" section at the end listing each source \
with page/chapter references and a 1-2 sentence summary of that source's view.
{citation_context}

Return a JSON object with five keys:
- "brief": A single sentence (under 100 chars) defining this concept
- "tags": Array of 3-5 lowercase topic tags (e.g., ["methodology", "startups"])
- "citations": Array of objects with keys: book, pages (string), chapter, perspective
- "content": The full concept page in Markdown. Include clear explanation, \
key details from the source document, and [[wikilinks]] to related concepts \
and [[summaries/{doc_name}]]
- "deep_study": Object for active learning, with keys:
  - "eli5": One-paragraph simple explanation (avoid jargon)
  - "analogy": A concrete everyday analogy
  - "misconceptions": 2-3 common misunderstandings as **Myth:** X **Reality:** Y pairs
  - "questions": 3-5 non-trivial understanding-check questions with hidden answers \
as **Q:** question **A:** answer pairs
  - "why_it_matters": Why this concept matters in practice (1-2 sentences)

Return ONLY valid JSON, no fences.
"""

_CONCEPT_UPDATE_USER = """\
Update the concept page for: {title}

Current content of this page:
{existing_content}

New information from document "{doc_name}" (summarized above) should be \
integrated into this page. Rewrite the full page incorporating the new \
information naturally — do not just append.

Citation rules:
1. Preserve ALL existing citations in "## Sources & Perspectives"
2. ADD a new citation for "{doc_name}" with page/chapter references
3. If the new source conflicts with existing ones, add a note callout
4. Maintain existing [[wikilinks]] and add new ones where appropriate

Return a JSON object with five keys:
- "brief": A single sentence (under 100 chars) defining this concept (may differ from before)
- "tags": Array of 3-5 lowercase topic tags, updated based on new information
- "citations": Complete array of citation objects (existing + new), each with: book, pages, chapter, perspective
- "content": The rewritten full concept page in Markdown
- "deep_study": Object for active learning, with keys: "eli5" (simple explanation), \
"analogy" (everyday analogy), "misconceptions" (myths vs reality), "questions" \
(3-5 Q&A pairs), "why_it_matters" (practical significance). Refresh based on ALL known sources.

Return ONLY valid JSON, no fences.
"""

_LONG_DOC_SUMMARY_USER = """\
This is a PageIndex summary for long document "{doc_name}" (doc_id: {doc_id}):

{content}

Based on this structured summary, write a concise overview that captures \
the key themes and findings. This will be used to generate concept pages.

Return ONLY the Markdown content (no frontmatter, no code fences).
"""

_DEEP_STUDY_UPGRADE_PROMPT = """\
Based on this concept page, generate active learning content for deep study.

Return a JSON object with one key:
- "deep_study": Object with keys:
  - "eli5": One-paragraph simple explanation (avoid jargon)
  - "analogy": A concrete everyday analogy
  - "misconceptions": 2-3 common misunderstandings as **Myth:** X **Reality:** Y pairs
  - "questions": 3-5 non-trivial understanding-check questions with hidden answers \
as **Q:** question **A:** answer pairs
  - "why_it_matters": Why this concept matters in practice (1-2 sentences)

Return ONLY valid JSON, no fences.
"""


# ---------------------------------------------------------------------------
# LLM helpers
# ---------------------------------------------------------------------------

class _Spinner:
    """Animated dots spinner that runs in a background thread."""

    def __init__(self, label: str):
        self._label = label
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        sys.stdout.write(f"    {self._label}")
        sys.stdout.flush()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(timeout=1.0):
            sys.stdout.write(".")
            sys.stdout.flush()

    def stop(self, suffix: str = "") -> None:
        self._stop.set()
        if self._thread:
            self._thread.join()
        sys.stdout.write(f" {suffix}\n")
        sys.stdout.flush()


def _format_usage(elapsed: float, usage) -> str:
    """Format timing and token usage into a short summary string."""
    cached = getattr(usage, "prompt_tokens_details", None)
    cache_info = ""
    if cached and hasattr(cached, "cached_tokens") and cached.cached_tokens:
        cache_info = f", cached={cached.cached_tokens}"
    return f"{elapsed:.1f}s (in={usage.prompt_tokens}, out={usage.completion_tokens}{cache_info})"


def _fmt_messages(messages: list[dict], max_content: int = 200) -> str:
    """Format messages for debug output, truncating long content."""
    parts = []
    for msg in messages:
        role = msg["role"]
        content = msg["content"]
        if len(content) > max_content:
            preview = content[:max_content] + f"... ({len(content)} chars)"
        else:
            preview = content
        parts.append(f"      [{role}] {preview}")
    return "\n".join(parts)


def _llm_call(model: str, messages: list[dict], step_name: str, **kwargs) -> str:
    """Single LLM call with animated progress and debug logging."""
    logger.debug("LLM request [%s]:\n%s", step_name, _fmt_messages(messages))
    if kwargs:
        logger.debug("LLM kwargs [%s]: %s", step_name, kwargs)

    spinner = _Spinner(step_name)
    spinner.start()
    t0 = time.time()

    response = litellm.completion(model=model, messages=messages, **kwargs)
    content = response.choices[0].message.content or ""

    spinner.stop(_format_usage(time.time() - t0, response.usage))
    logger.debug("LLM response [%s]:\n%s", step_name, content[:500] + ("..." if len(content) > 500 else ""))
    return content.strip()


async def _llm_call_async(model: str, messages: list[dict], step_name: str) -> str:
    """Async LLM call with timing output and debug logging."""
    logger.debug("LLM request [%s]:\n%s", step_name, _fmt_messages(messages))

    t0 = time.time()

    response = await litellm.acompletion(model=model, messages=messages)
    content = response.choices[0].message.content or ""

    elapsed = time.time() - t0
    sys.stdout.write(f"    {step_name}... {_format_usage(elapsed, response.usage)}\n")
    sys.stdout.flush()
    logger.debug("LLM response [%s]:\n%s", step_name, content[:500] + ("..." if len(content) > 500 else ""))
    return content.strip()


def _parse_json(text: str) -> list | dict:
    """Parse JSON from LLM response, handling fences, prose, and malformed JSON."""
    from json_repair import repair_json
    cleaned = text.strip()
    if cleaned.startswith("```"):
        first_nl = cleaned.find("\n")
        cleaned = cleaned[first_nl + 1:] if first_nl != -1 else cleaned[3:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
    result = json.loads(repair_json(cleaned.strip()))
    if not isinstance(result, (dict, list)):
        raise ValueError(f"Expected JSON object or array, got {type(result).__name__}")
    return result


# ---------------------------------------------------------------------------
# File I/O helpers
# ---------------------------------------------------------------------------

def _read_wiki_context(wiki_dir: Path) -> tuple[str, list[str]]:
    """Read current index.md content and list of existing concept slugs."""
    index_path = wiki_dir / "index.md"
    index_content = index_path.read_text(encoding="utf-8") if index_path.exists() else ""

    concepts_dir = wiki_dir / "concepts"
    existing = sorted(p.stem for p in concepts_dir.glob("*.md")) if concepts_dir.exists() else []

    return index_content, existing


def _read_concept_briefs(wiki_dir: Path) -> str:
    """Read existing concept pages and return compact one-line summaries.

    For each concept, reads the ``brief:`` field from YAML frontmatter if
    present; otherwise falls back to truncating the first 150 chars of the body
    (newlines collapsed to spaces).  Formats each as ``- {slug}: {brief}``.

    Returns "(none yet)" if the concepts directory is missing or empty.
    """
    concepts_dir = wiki_dir / "concepts"
    if not concepts_dir.exists():
        return "(none yet)"

    md_files = sorted(concepts_dir.glob("*.md"))
    if not md_files:
        return "(none yet)"

    lines: list[str] = []
    for path in md_files:
        text = path.read_text(encoding="utf-8")
        brief = ""
        body = text
        if text.startswith("---"):
            end = text.find("---", 3)
            if end != -1:
                fm = text[:end + 3]
                body = text[end + 3:]
                for line in fm.split("\n"):
                    if line.startswith("brief:"):
                        brief = line[len("brief:"):].strip()
                        break
        if not brief:
            brief = body.strip().replace("\n", " ")[:150]
        if brief:
            lines.append(f"- {path.stem}: {brief}")

    return "\n".join(lines) or "(none yet)"


def _get_section_bounds(lines: list[str], heading: str) -> tuple[int, int] | None:
    """Return the [start, end) bounds for a Markdown H2 section."""
    for i, line in enumerate(lines):
        if line == heading:
            start = i + 1
            end = len(lines)
            for j in range(start, len(lines)):
                if lines[j].startswith("## "):
                    end = j
                    break
            return start, end
    return None


def _section_contains_link(lines: list[str], heading: str, link: str) -> bool:
    """Check whether an index entry already exists inside the named section."""
    bounds = _get_section_bounds(lines, heading)
    if bounds is None:
        return False

    start, end = bounds
    entry_prefix = f"- {link}"
    return any(line.startswith(entry_prefix) for line in lines[start:end])


def _replace_section_entry(lines: list[str], heading: str, link: str, entry: str) -> bool:
    """Replace the first matching entry within a specific section."""
    bounds = _get_section_bounds(lines, heading)
    if bounds is None:
        return False

    start, end = bounds
    entry_prefix = f"- {link}"
    for i in range(start, end):
        if lines[i].startswith(entry_prefix):
            lines[i] = entry
            return True
    return False


def _insert_section_entry(lines: list[str], heading: str, entry: str) -> bool:
    """Insert a new entry at the top of a specific section."""
    bounds = _get_section_bounds(lines, heading)
    if bounds is None:
        return False

    start, _ = bounds
    lines.insert(start, entry)
    return True



def _write_summary(wiki_dir: Path, doc_name: str, summary: str,
                    doc_type: str = "short") -> None:
    """Write summary page with frontmatter."""
    if summary.startswith("---"):
        end = summary.find("---", 3)
        if end != -1:
            summary = summary[end + 3:].lstrip("\n")
    summaries_dir = wiki_dir / "summaries"
    summaries_dir.mkdir(parents=True, exist_ok=True)
    ext = "md" if doc_type == "short" else "json"
    today = date.today().isoformat()
    fm_lines = [
        f"type: summary",
        f"title: \"{doc_name}\"",
        f"date: {today}",
        f"last_updated: {today}",
        f"doc_type: {doc_type}",
        f"full_text: sources/{doc_name}.{ext}",
    ]
    frontmatter = "---\n" + "\n".join(fm_lines) + "\n---\n\n"
    (summaries_dir / f"{doc_name}.md").write_text(frontmatter + summary, encoding="utf-8")


_SAFE_NAME_RE = re.compile(r'[^\w\-]')


def _sanitize_concept_name(name: str) -> str:
    """Sanitize a concept name for safe use as a filename.

    Replaces non-word chars with hyphens, collapses consecutive hyphens,
    strips leading/trailing hyphens, and truncates to 60 chars.
    """
    name = unicodedata.normalize("NFKC", name)
    sanitized = _SAFE_NAME_RE.sub("-", name).strip("-")
    sanitized = re.sub(r"-{2,}", "-", sanitized)
    if len(sanitized) > 60:
        sanitized = sanitized[:60].rsplit("-", 1)[0]
    return sanitized or "unnamed-concept"


def _normalize_content(value) -> str:
    """Normalize LLM output that may be a list instead of string."""
    if value is None:
        return ""
    if isinstance(value, list):
        return "\n".join(str(v) for v in value)
    return str(value)


def _format_callout(callout_type: str, title: str, content: str) -> str:
    """Format content as an Obsidian collapsible callout block."""
    if isinstance(content, list):
        content = _normalize_content(content)
    lines = content.strip().split("\n")
    quoted = "\n".join(f"> {line}" for line in lines)
    return f"> [!{callout_type}]- {title}\n{quoted}"


def _format_deep_study(ds: dict) -> str:
    """Format a deep_study dict into Obsidian callout blocks."""
    if not ds:
        return ""
    sections = []
    eli5 = _normalize_content(ds.get("eli5", ""))
    if eli5.strip():
        sections.append(_format_callout("tip", "ELI5", eli5))
    analogy = _normalize_content(ds.get("analogy", ""))
    if analogy.strip():
        sections.append(_format_callout("tip", "Real-World Analogy", analogy))
    misconceptions = _normalize_content(ds.get("misconceptions", ""))
    if misconceptions.strip():
        sections.append(_format_callout("warning", "Common Misconceptions", misconceptions))
    questions = _normalize_content(ds.get("questions", ""))
    if questions.strip():
        sections.append(_format_callout("question", "Check Your Understanding", questions))
    why = _normalize_content(ds.get("why_it_matters", ""))
    if why.strip():
        sections.append(_format_callout("info", "Why It Matters", why))
    if not sections:
        return ""
    return "<!-- openkb-deep-study-start -->\n" + "\n\n".join(sections) + "\n<!-- openkb-deep-study-end -->"


def _write_concept(wiki_dir: Path, name: str, content: str, source_file: str, is_update: bool, brief: str = "", citations: list | None = None, tags: list | None = None, deep_study: dict | None = None) -> None:
    """Write or update a concept page, managing the sources frontmatter."""
    concepts_dir = wiki_dir / "concepts"
    concepts_dir.mkdir(parents=True, exist_ok=True)
    safe_name = _sanitize_concept_name(name)
    path = (concepts_dir / f"{safe_name}.md").resolve()
    if not path.is_relative_to(concepts_dir.resolve()):
        logger.warning("Concept name escapes concepts dir: %s", name)
        return

    if is_update and path.exists():
        existing = path.read_text(encoding="utf-8")
        if source_file not in existing:
            if existing.startswith("---"):
                end = existing.find("---", 3)
                if end != -1:
                    fm = existing[:end + 3]
                    body = existing[end + 3:]
                    if "sources:" in fm:
                        fm = fm.replace("sources: [", f"sources: [{source_file}, ")
                    else:
                        fm = fm.replace("---\n", f"---\nsources: [{source_file}]\n", 1)
                    existing = fm + body
            else:
                existing = f"---\nsources: [{source_file}]\n---\n\n" + existing
        # Strip frontmatter from LLM content to avoid duplicate blocks
        clean = content
        if clean.startswith("---"):
            end = clean.find("---", 3)
            if end != -1:
                clean = clean[end + 3:].lstrip("\n")
        # Replace body with LLM rewrite (prompt asks for full rewrite, not delta)
        if existing.startswith("---"):
            end = existing.find("---", 3)
            if end != -1:
                existing = existing[:end + 3] + "\n\n" + clean
            else:
                existing = clean
        else:
            existing = clean
        if brief and existing.startswith("---"):
            end = existing.find("---", 3)
            if end != -1:
                fm = existing[:end + 3]
                body = existing[end + 3:]
                if "brief:" in fm:
                    fm = re.sub(r"brief:.*", f"brief: {brief}", fm)
                else:
                    fm = fm.replace("---\n", f"---\nbrief: {brief}\n", 1)
                existing = fm + body
        # Update citations in frontmatter
        if citations and existing.startswith("---"):
            existing = _update_frontmatter_field(existing, path, "citations", json.dumps(citations, ensure_ascii=False))
        # Update tags in frontmatter
        if tags and existing.startswith("---"):
            existing = _update_frontmatter_field(existing, path, "tags", json.dumps(tags, ensure_ascii=False))
        # Refresh last_updated
        if existing.startswith("---"):
            existing = _update_frontmatter_field(existing, path, "last_updated", date.today().isoformat())
        # Append deep study callouts
        if deep_study:
            callouts = _format_deep_study(deep_study)
            if callouts:
                start_marker = "<!-- openkb-deep-study-start -->"
                end_marker = "<!-- openkb-deep-study-end -->"
                if start_marker in existing:
                    start_pos = existing.find(start_marker)
                    end_pos = existing.find(end_marker)
                    if end_pos != -1:
                        existing = existing[:start_pos].rstrip() + "\n\n" + callouts
                    else:
                        existing = existing[:start_pos].rstrip() + "\n\n" + callouts
                else:
                    existing = existing.rstrip() + "\n\n" + callouts
        path.write_text(existing, encoding="utf-8")
    else:
        if content.startswith("---"):
            end = content.find("---", 3)
            if end != -1:
                content = content[end + 3:].lstrip("\n")
        today = date.today().isoformat()
        fm_lines = [
            f"type: concept",
            f"title: \"{name}\"",
            f"date: {today}",
            f"last_updated: {today}",
            f"sources: [{source_file}]",
        ]
        if brief:
            fm_lines.append(f"brief: {brief}")
        if citations:
            fm_lines.append(f"citations: {json.dumps(citations, ensure_ascii=False)}")
        if tags:
            fm_lines.append(f"tags: {json.dumps(tags, ensure_ascii=False)}")
        fm_lines.extend([
            "understanding_level: unreviewed",
            "last_reviewed: null",
            "review_count: 0",
        ])
        frontmatter = "---\n" + "\n".join(fm_lines) + "\n---\n\n"
        page_text = frontmatter + content
        if deep_study:
            callouts = _format_deep_study(deep_study)
            if callouts:
                page_text += "\n\n" + callouts
        path.write_text(page_text, encoding="utf-8")


def _update_frontmatter_field(text: str, path: Path, field: str, value: str) -> str:
    """Insert or replace a field in YAML frontmatter. Returns updated text."""
    if not text.startswith("---"):
        return text
    end = text.find("---", 3)
    if end == -1:
        return text
    fm = text[:end + 3]
    body = text[end + 3:]
    if f"{field}:" in fm:
        fm = re.sub(rf'^{field}:.*', f'{field}: {value}', fm, flags=re.MULTILINE)
    else:
        fm = fm.replace("---\n", f"---\n{field}: {value}\n", 1)
    result = fm + body
    path.write_text(result, encoding="utf-8")
    return result


def _add_related_link(wiki_dir: Path, concept_slug: str, doc_name: str, source_file: str) -> None:
    """Add a cross-reference link to an existing concept page (no LLM call)."""
    concepts_dir = wiki_dir / "concepts"
    path = concepts_dir / f"{concept_slug}.md"
    if not path.exists():
        return

    text = path.read_text(encoding="utf-8")
    link = f"[[summaries/{doc_name}]]"
    if link in text:
        return

    # Update sources in frontmatter
    if source_file not in text:
        if text.startswith("---"):
            end = text.find("---", 3)
            if end != -1:
                fm = text[:end + 3]
                body = text[end + 3:]
                if "sources:" in fm:
                    fm = fm.replace("sources: [", f"sources: [{source_file}, ")
                else:
                    fm = fm.replace("---\n", f"---\nsources: [{source_file}]\n", 1)
                text = fm + body
        else:
            text = f"---\nsources: [{source_file}]\n---\n\n" + text

    text += f"\n\nSee also: {link}"
    path.write_text(text, encoding="utf-8")


def _backlink_summary(wiki_dir: Path, doc_name: str, concept_slugs: list[str]) -> None:
    """Append missing concept wikilinks to the summary page (no LLM call).

    After all concepts are generated, this ensures the summary page links
    back to every related concept — closing the bidirectional link that
    concept pages already have toward the summary.

    If a ``## Related Concepts`` section already exists, new links are
    appended into it rather than creating a duplicate section.
    """
    summary_path = wiki_dir / "summaries" / f"{doc_name}.md"
    if not summary_path.exists():
        return

    text = summary_path.read_text(encoding="utf-8")
    missing = [slug for slug in concept_slugs if f"[[concepts/{slug}]]" not in text]
    if not missing:
        return

    new_links = "\n".join(f"- [[concepts/{s}]]" for s in missing)
    if "## Related Concepts" in text:
        # Append into existing section
        text = text.replace("## Related Concepts\n", f"## Related Concepts\n{new_links}\n", 1)
    else:
        text += f"\n\n## Related Concepts\n{new_links}\n"
    summary_path.write_text(text, encoding="utf-8")


def _backlink_concepts(wiki_dir: Path, doc_name: str, concept_slugs: list[str]) -> None:
    """Append missing summary wikilink to each concept page (no LLM call).

    Ensures every concept page links back to the source document's summary,
    regardless of whether the LLM included the link in its output.

    If a ``## Related Documents`` section already exists, the link is
    appended into it rather than creating a duplicate section.
    """
    link = f"[[summaries/{doc_name}]]"
    concepts_dir = wiki_dir / "concepts"

    for slug in concept_slugs:
        path = concepts_dir / f"{slug}.md"
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        if link in text:
            continue
        if "## Related Documents" in text:
            text = text.replace("## Related Documents\n", f"## Related Documents\n- {link}\n", 1)
        else:
            text += f"\n\n## Related Documents\n- {link}\n"
        path.write_text(text, encoding="utf-8")

def _update_index(
    wiki_dir: Path, doc_name: str, concept_names: list[str],
    doc_brief: str = "", concept_briefs: dict[str, str] | None = None,
    doc_type: str = "short",
) -> None:
    """Append document and concept entries to index.md.

    When ``doc_brief`` or entries in ``concept_briefs`` are provided, entries
    are written as ``- [[link]] (type) — brief text``. Existing entries are
    detected within their own section by exact entry prefix and skipped to
    avoid duplicates.
    ``doc_type`` is ``"short"`` or ``"pageindex"`` — shown in the entry so the
    query agent knows how to access detailed content.
    """
    if concept_briefs is None:
        concept_briefs = {}

    index_path = wiki_dir / "index.md"
    if not index_path.exists():
        index_path.write_text(
            "# Knowledge Base Index\n\n## Documents\n\n## Concepts\n\n## Explorations\n",
            encoding="utf-8",
        )

    lines = index_path.read_text(encoding="utf-8").split("\n")

    doc_link = f"[[summaries/{doc_name}]]"
    if not _section_contains_link(lines, "## Documents", doc_link):
        doc_entry = f"- {doc_link} ({doc_type})"
        if doc_brief:
            doc_entry += f" — {doc_brief}"
        _insert_section_entry(lines, "## Documents", doc_entry)

    for name in concept_names:
        concept_link = f"[[concepts/{name}]]"
        concept_entry = f"- {concept_link}"
        if name in concept_briefs:
            concept_entry += f" — {concept_briefs[name]}"
        if _section_contains_link(lines, "## Concepts", concept_link):
            if name in concept_briefs:
                _replace_section_entry(lines, "## Concepts", concept_link, concept_entry)
        else:
            _insert_section_entry(lines, "## Concepts", concept_entry)

    index_path.write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

DEFAULT_COMPILE_CONCURRENCY = 5


def generate_deep_study_for_concept(
    concept_path: Path,
    model: str,
    wiki_dir: Path,
) -> bool:
    """Generate deep study content for a single concept page (upgrade use case).

    Returns True if deep study was generated, False if skipped or failed.
    """
    text = concept_path.read_text(encoding="utf-8")

    if "<!-- openkb-deep-study-start -->" in text:
        return False

    body = text
    if text.startswith("---"):
        end = text.find("---", 3)
        if end != -1:
            body = text[end + 3:].strip()

    schema_md = get_agents_md(wiki_dir)
    system_msg = {"role": "system", "content": f"Generate active learning content.\n\n{schema_md}"}
    user_msg = {"role": "user", "content": f"Concept page content:\n\n{body}\n\n{_DEEP_STUDY_UPGRADE_PROMPT}"}

    raw = _llm_call(model, [system_msg, user_msg], f"deep-study: {concept_path.stem}")
    try:
        parsed = _parse_json(raw)
        deep_study = parsed.get("deep_study", {})
    except (json.JSONDecodeError, ValueError):
        logger.warning("Failed to parse deep study for %s", concept_path.name)
        return False

    callouts = _format_deep_study(deep_study)
    if not callouts:
        return False

    updated = text.rstrip() + "\n\n" + callouts + "\n"
    concept_path.write_text(updated, encoding="utf-8")
    return True


async def _compile_concepts(
    wiki_dir: Path,
    kb_dir: Path,
    model: str,
    system_msg: dict,
    doc_msg: dict,
    summary: str,
    doc_name: str,
    max_concurrency: int,
    doc_brief: str = "",
    doc_type: str = "short",
    citation_context: str = "",
) -> None:
    """Shared Steps 2-4: concepts plan → generate/update → index.

    Uses ``_CONCEPTS_PLAN_USER`` to get a plan with create/update/related
    actions, then executes each action type accordingly.
    """
    source_file = f"summaries/{doc_name}.md"

    # --- Step 2: Get concepts plan (A cached) ---
    concept_briefs = _read_concept_briefs(wiki_dir)

    plan_raw = _llm_call(model, [
        system_msg,
        doc_msg,
        {"role": "assistant", "content": summary},
        {"role": "user", "content": _CONCEPTS_PLAN_USER.format(
            concept_briefs=concept_briefs,
        )},
    ], "concepts-plan", max_tokens=1024)

    try:
        parsed = _parse_json(plan_raw)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("Failed to parse concepts plan: %s", exc)
        logger.debug("Raw: %s", plan_raw)
        _update_index(wiki_dir, doc_name, [], doc_brief=doc_brief, doc_type=doc_type)
        return

    # Fallback: if LLM returns a flat list, treat all items as "create"
    if isinstance(parsed, list):
        plan = {"create": parsed, "update": [], "related": []}
    else:
        plan = {
            "create": parsed.get("create", []),
            "update": parsed.get("update", []),
            "related": parsed.get("related", []),
        }

    create_items = plan["create"]
    update_items = plan["update"]
    related_items = plan["related"]

    if not create_items and not update_items and not related_items:
        _update_index(wiki_dir, doc_name, [], doc_brief=doc_brief, doc_type=doc_type)
        return

    # --- Step 3: Generate/update concept pages concurrently (A cached) ---
    semaphore = asyncio.Semaphore(max_concurrency)

    async def _gen_create(concept: dict) -> tuple[str, str, bool, str, list, dict]:
        name = concept["name"]
        title = concept.get("title", name)
        async with semaphore:
            raw = await _llm_call_async(model, [
                system_msg,
                doc_msg,
                {"role": "assistant", "content": summary},
                {"role": "user", "content": _CONCEPT_PAGE_USER.format(
                    title=title, doc_name=doc_name,
                    update_instruction="",
                    citation_context=citation_context,
                )},
            ], f"concept: {name}")
        try:
            parsed = _parse_json(raw)
            brief = parsed.get("brief", "")
            content = parsed.get("content", raw)
            citations = parsed.get("citations", []) if isinstance(parsed, dict) else []
            tags = parsed.get("tags", []) if isinstance(parsed, dict) else []
            ds = parsed.get("deep_study", {}) if isinstance(parsed, dict) else {}
        except (json.JSONDecodeError, ValueError):
            brief, content, citations, tags, ds = "", raw, [], [], {}
        return name, content, False, brief, citations, tags, ds

    async def _gen_update(concept: dict) -> tuple[str, str, bool, str, list, dict]:
        name = concept["name"]
        title = concept.get("title", name)
        concept_path = wiki_dir / "concepts" / f"{_sanitize_concept_name(name)}.md"
        if concept_path.exists():
            raw_text = concept_path.read_text(encoding="utf-8")
            if raw_text.startswith("---"):
                parts = raw_text.split("---", 2)
                existing_content = parts[2].strip() if len(parts) >= 3 else raw_text
            else:
                existing_content = raw_text
        else:
            existing_content = "(page not found — create from scratch)"
        async with semaphore:
            raw = await _llm_call_async(model, [
                system_msg,
                doc_msg,
                {"role": "assistant", "content": summary},
                {"role": "user", "content": _CONCEPT_UPDATE_USER.format(
                    title=title, doc_name=doc_name,
                    existing_content=existing_content,
                )},
            ], f"update: {name}")
        try:
            parsed = _parse_json(raw)
            brief = parsed.get("brief", "")
            content = parsed.get("content", raw)
            citations = parsed.get("citations", []) if isinstance(parsed, dict) else []
            tags = parsed.get("tags", []) if isinstance(parsed, dict) else []
            ds = parsed.get("deep_study", {}) if isinstance(parsed, dict) else {}
        except (json.JSONDecodeError, ValueError):
            brief, content, citations, tags, ds = "", raw, [], [], {}
        return name, content, True, brief, citations, tags, ds

    tasks = []
    tasks.extend(_gen_create(c) for c in create_items)
    tasks.extend(_gen_update(c) for c in update_items)

    concept_names: list[str] = []
    concept_briefs_map: dict[str, str] = {}

    if tasks:
        total = len(tasks)
        sys.stdout.write(f"    Generating {total} concept(s) (concurrency={max_concurrency})...\n")
        sys.stdout.flush()

        results = await asyncio.gather(*tasks, return_exceptions=True)

        for r in results:
            if isinstance(r, Exception):
                logger.warning("Concept generation failed: %s", r)
                continue
            name, page_content, is_update, brief, citations, tags, deep_study = r
            _write_concept(wiki_dir, name, page_content, source_file, is_update, brief=brief, citations=citations, tags=tags, deep_study=deep_study)
            safe_name = _sanitize_concept_name(name)
            concept_names.append(safe_name)
            if brief:
                concept_briefs_map[safe_name] = brief

    # --- Step 3b: Process related items (code only, no LLM) ---
    sanitized_related = [_sanitize_concept_name(s) for s in related_items]
    for slug in sanitized_related:
        _add_related_link(wiki_dir, slug, doc_name, source_file)

    # --- Step 3c: Backlink — summary ↔ concepts (code only) ---
    all_concept_slugs = concept_names + sanitized_related
    if all_concept_slugs:
        _backlink_summary(wiki_dir, doc_name, all_concept_slugs)
        _backlink_concepts(wiki_dir, doc_name, all_concept_slugs)

    # --- Step 4: Update index (code only) ---
    _update_index(wiki_dir, doc_name, concept_names,
                  doc_brief=doc_brief, concept_briefs=concept_briefs_map,
                  doc_type=doc_type)


async def compile_short_doc(
    doc_name: str,
    source_path: Path,
    kb_dir: Path,
    model: str,
    max_concurrency: int = DEFAULT_COMPILE_CONCURRENCY,
) -> None:
    """Compile a short document using a multi-step LLM pipeline with caching.

    Step 1: Build base context A (schema + doc content), generate summary.
    Steps 2-4: Delegated to ``_compile_concepts``.
    """
    from openkb.config import load_config

    openkb_dir = kb_dir / ".openkb"
    config = load_config(openkb_dir / "config.yaml")
    language: str = config.get("language", "en")

    wiki_dir = kb_dir / "wiki"
    schema_md = get_agents_md(wiki_dir)
    content = source_path.read_text(encoding="utf-8")

    # Base context A: system + document
    system_msg = {"role": "system", "content": _SYSTEM_TEMPLATE.format(
        schema_md=schema_md, language=language,
    )}
    doc_msg = {"role": "user", "content": _SUMMARY_USER.format(
        doc_name=doc_name, content=content,
    )}

    # --- Step 1: Generate summary ---
    summary_raw = _llm_call(model, [system_msg, doc_msg], "summary")
    try:
        summary_parsed = _parse_json(summary_raw)
        doc_brief = summary_parsed.get("brief", "")
        summary = summary_parsed.get("content", summary_raw)
    except (json.JSONDecodeError, ValueError):
        doc_brief = ""
        summary = summary_raw
    _write_summary(wiki_dir, doc_name, summary)

    # --- Steps 2-4: Concept plan → generate/update → index ---
    await _compile_concepts(
        wiki_dir, kb_dir, model, system_msg, doc_msg,
        summary, doc_name, max_concurrency, doc_brief=doc_brief,
        doc_type="short",
    )


async def compile_long_doc(
    doc_name: str,
    summary_path: Path,
    doc_id: str,
    kb_dir: Path,
    model: str,
    doc_description: str = "",
    max_concurrency: int = DEFAULT_COMPILE_CONCURRENCY,
) -> None:
    """Compile a long (PageIndex) document's concepts and index.

    The summary page is already written by the indexer. This function
    generates concept pages and updates the index.
    """
    from openkb.config import load_config

    openkb_dir = kb_dir / ".openkb"
    config = load_config(openkb_dir / "config.yaml")
    language: str = config.get("language", "en")

    wiki_dir = kb_dir / "wiki"
    schema_md = get_agents_md(wiki_dir)
    summary_content = summary_path.read_text(encoding="utf-8")

    # Base context A
    system_msg = {"role": "system", "content": _SYSTEM_TEMPLATE.format(
        schema_md=schema_md, language=language,
    )}
    doc_msg = {"role": "user", "content": _LONG_DOC_SUMMARY_USER.format(
        doc_name=doc_name, doc_id=doc_id, content=summary_content,
    )}

    # --- Step 1: Generate overview ---
    overview = _llm_call(model, [system_msg, doc_msg], "overview")

    # --- Steps 2-4: Concept plan → generate/update → index ---
    await _compile_concepts(
        wiki_dir, kb_dir, model, system_msg, doc_msg,
        overview, doc_name, max_concurrency, doc_brief=doc_description,
        doc_type="pageindex",
    )
