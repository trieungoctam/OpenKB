"""Knowledge lint agent for semantic quality checks on the wiki."""
from __future__ import annotations

from pathlib import Path

from agents import Agent, Runner, function_tool

from openkb.agent.tools import list_wiki_files, read_wiki_file

MAX_TURNS = 50
from openkb.schema import SCHEMA_MD, get_agents_md

_LINTER_INSTRUCTIONS_TEMPLATE = """\
You are OpenKB's semantic lint agent. Your job is to audit the wiki
for quality issues that structural tools cannot detect.

{schema_md}

## Output Format (MANDATORY)

Return a structured report using these severity levels:

### [CRITICAL] Issue Title
- **Category:** contradiction | gap | error
- **Location:** page path
- **Detail:** what's wrong
- **Action:** how to fix

### [WARNING] Issue Title
- **Category:** redundancy | coverage | stale
- **Location:** page path
- **Detail:** what could be improved
- **Action:** suggestion

### [INFO] Issue Title
- **Category:** suggestion | style
- **Location:** page path
- **Detail:** minor improvement opportunity
- **Action:** optional enhancement

## Checks to perform

### Critical (must fix)
1. **Contradictions** — Do any pages make conflicting claims about the same fact?
   Cite exact passages. Check concept pages citing different source documents.

### Warning (should fix)
2. **Knowledge gaps** — Topics in summaries missing concept pages?
3. **Redundancy** — Concept pages covering the same ground that could merge?
4. **Source conflicts** — Concepts citing sources with conflicting perspectives
   without acknowledging the disagreement?
5. **Coverage imbalance** — Some source documents over/under-represented?

### Info (nice to have)
6. **Concept quality** — Well-structured with clear explanations?
7. **Cross-reference richness** — Enough wikilinks to related topics?

## Process
1. Start with index.md to understand scope.
2. Read ALL summary pages.
3. Read ALL concept pages.
4. Cross-reference: for each concept, check if cited sources agree.
5. Produce the structured report using EXACTLY the format above.

Be thorough but concise. If no issues in a category, omit it.
"""


def build_lint_agent(wiki_root: str, model: str, language: str = "en") -> Agent:
    """Build the semantic knowledge-lint agent.

    Args:
        wiki_root: Absolute path to the wiki directory.
        model: LLM model name.
        language: Language code for wiki content (e.g. 'en', 'fr').

    Returns:
        Configured :class:`~agents.Agent` instance.
    """
    schema_md = get_agents_md(Path(wiki_root))
    instructions = _LINTER_INSTRUCTIONS_TEMPLATE.format(schema_md=schema_md)
    instructions += f"\n\nIMPORTANT: Write the lint report in {language} language."

    @function_tool
    def list_files(directory: str) -> str:
        """List all Markdown files in a wiki subdirectory.

        Args:
            directory: Subdirectory path relative to wiki root (e.g. 'summaries').
        """
        return list_wiki_files(directory, wiki_root)

    @function_tool
    def read_file(path: str) -> str:
        """Read a Markdown file from the wiki.

        Args:
            path: File path relative to wiki root (e.g. 'summaries/paper.md').
        """
        return read_wiki_file(path, wiki_root)

    return Agent(
        name="wiki-linter",
        instructions=instructions,
        tools=[list_files, read_file],
        model=f"litellm/{model}",
    )


async def run_knowledge_lint(kb_dir: Path, model: str) -> str:
    """Run the semantic knowledge lint agent against the wiki.

    Args:
        kb_dir: Root of the knowledge base.
        model: LLM model name.

    Returns:
        The agent's lint report as a Markdown string.
    """
    from openkb.config import load_config

    openkb_dir = kb_dir / ".openkb"
    config = load_config(openkb_dir / "config.yaml")
    language: str = config.get("language", "en")

    wiki_root = str(kb_dir / "wiki")
    agent = build_lint_agent(wiki_root, model, language=language)

    prompt = (
        "Please audit this knowledge base wiki for semantic quality issues: "
        "contradictions, gaps, staleness, redundancy, and missing concept pages. "
        "Start with index.md, then read summaries and concepts as needed. "
        "Produce a structured Markdown report."
    )

    result = await Runner.run(agent, prompt, max_turns=MAX_TURNS)
    return result.final_output or "Knowledge lint completed. No output produced."
