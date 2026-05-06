"""OpenKB CLI — command-line interface for the knowledge base workflow."""
from __future__ import annotations

# Silence import-time warnings (e.g. pydub's missing-ffmpeg warning emitted
# when markitdown pulls it in). markitdown later clobbers the filters during
# its own import, so we re-apply after all imports below.
import warnings
warnings.filterwarnings("ignore")

import asyncio
import json
import logging
import time
from pathlib import Path

import os

from agents import set_tracing_disabled
set_tracing_disabled(True)
# Use local model cost map — skip fetching from GitHub on every invocation
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

import click
import litellm
litellm.suppress_debug_info = True
from dotenv import load_dotenv

from openkb.config import DEFAULT_CONFIG, load_config, save_config, load_global_config, register_kb
from openkb.converter import convert_document
from openkb.log import append_log
from openkb.schema import AGENTS_MD

# Suppress warnings after all imports — markitdown overrides filters at import time
import warnings
warnings.filterwarnings("ignore")

load_dotenv()  # load from cwd (covers running inside the KB dir)


def _setup_llm_key(kb_dir: Path | None = None) -> None:
    """Set LiteLLM API key from LLM_API_KEY env var if present.

    Load order (override=False, so first one wins):
    1. System environment variables (already set)
    2. KB-local .env  (kb_dir/.env)
    3. Global .env    (~/.config/openkb/.env)

    Also propagates to provider-specific env vars (OPENAI_API_KEY, etc.)
    so that the Agents SDK litellm provider can pick them up.
    """
    if kb_dir is not None:
        env_file = kb_dir / ".env"
        if env_file.exists():
            load_dotenv(env_file, override=False)

    from openkb.config import GLOBAL_CONFIG_DIR
    global_env = GLOBAL_CONFIG_DIR / ".env"
    if global_env.exists():
        load_dotenv(global_env, override=False)

    api_key = os.environ.get("LLM_API_KEY", "")
    if not api_key:
        # Check if any provider key is already set
        has_key = any(os.environ.get(k) for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY"))
        if not has_key:
            click.echo(
                "Warning: No LLM API key found. Set one of:\n"
                f"  1. {kb_dir / '.env' if kb_dir else '<kb_dir>/.env'} — LLM_API_KEY=sk-...\n"
                f"  2. {GLOBAL_CONFIG_DIR / '.env'} — LLM_API_KEY=sk-...\n"
                "  3. Export LLM_API_KEY in your shell profile"
            )
    else:
        litellm.api_key = api_key
        for env_var in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY"):
            if not os.environ.get(env_var):
                os.environ[env_var] = api_key

# Supported document extensions for the `add` command
SUPPORTED_EXTENSIONS = {
    ".pdf", ".md", ".markdown", ".docx", ".pptx", ".xlsx",
    ".html", ".htm", ".txt", ".csv",
}

# Map raw doc types to display types
_TYPE_DISPLAY_MAP = {
    "long_pdf": "pageindex",
}

_SHORT_DOC_TYPES = {"pdf", "docx", "md", "markdown", "html", "htm", "txt", "csv", "pptx", "xlsx"}


def _display_type(raw_type: str) -> str:
    """Map a raw stored doc type to a display type string."""
    if raw_type in _TYPE_DISPLAY_MAP:
        return _TYPE_DISPLAY_MAP[raw_type]
    if raw_type in _SHORT_DOC_TYPES:
        return "short"
    return raw_type


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_kb_dir(override: Path | None = None) -> Path | None:
    """Find the KB root: explicit override → walk up from cwd → global default_kb."""
    # 0. Explicit override (--kb-dir or OPENKB_DIR)
    if override is not None:
        if (override / ".openkb").is_dir():
            return override
        return None
    # 1. Walk up from cwd
    current = Path.cwd().resolve()
    while True:
        if (current / ".openkb").is_dir():
            return current
        parent = current.parent
        if parent == current:
            break
        current = parent
    # 2. Fall back to global config default_kb
    gc = load_global_config()
    default = gc.get("default_kb")
    if default:
        p = Path(default)
        if (p / ".openkb").is_dir():
            return p
    return None


def add_single_file(file_path: Path, kb_dir: Path, *, force: bool = False) -> None:
    """Convert, index, and compile a single document into the knowledge base."""
    from openkb.agent.compiler import compile_short_doc
    from openkb.state import HashRegistry

    logger = logging.getLogger(__name__)
    openkb_dir = kb_dir / ".openkb"
    config = load_config(openkb_dir / "config.yaml")
    _setup_llm_key(kb_dir)
    model: str = config.get("model", DEFAULT_CONFIG["model"])
    registry = HashRegistry(openkb_dir / "hashes.json")

    click.echo(f"Adding: {file_path.name}")
    try:
        result = convert_document(file_path, kb_dir, force=force)
    except Exception as exc:
        click.echo(f"  [ERROR] Conversion failed: {exc}")
        logger.debug("Conversion traceback:", exc_info=True)
        return

    if result.skipped:
        click.echo(f"  [SKIP] Already in knowledge base: {file_path.name}")
        return

    if result.is_large_pdf:
        _compile_large_pdf(file_path, result, kb_dir, model, config)
    elif result.is_long_doc:
        _compile_long_doc(file_path, result, kb_dir, model)
    else:
        click.echo(f"  Compiling short doc...")
        for attempt in range(2):
            try:
                asyncio.run(compile_short_doc(result.doc_name, result.source_path, kb_dir, model))
                break
            except Exception as exc:
                if attempt == 0:
                    click.echo(f"  Retrying compilation in 2s...")
                    time.sleep(2)
                else:
                    click.echo(f"  [ERROR] Compilation failed: {exc}")
                    logger.debug("Compilation traceback:", exc_info=True)
                    return

    if result.file_hash:
        doc_type = "long_pdf" if result.is_long_doc else file_path.suffix.lstrip(".")
        registry.add(result.file_hash, {"name": file_path.name, "type": doc_type})

    append_log(kb_dir / "wiki", "ingest", file_path.name)
    click.echo(f"  [OK] {file_path.name} added to knowledge base.")


def _compile_large_pdf(file_path: Path, result, kb_dir: Path, model: str, config: dict) -> None:
    """Process a large PDF through segment splitting and sequential compilation."""
    from openkb.agent.compiler import compile_short_doc
    from openkb.splitter import split_pdf
    from openkb.converter import convert_segment
    from openkb.merger import merge_segment_concepts

    logger = logging.getLogger(__name__)
    doc_name = result.doc_name
    segments_dir = kb_dir / ".openkb" / "segments" / doc_name
    segments_dir.mkdir(parents=True, exist_ok=True)

    try:
        segments = split_pdf(result.raw_path, segments_dir, config)
        click.echo(f"  Split into {len(segments)} segments")

        for i, seg in enumerate(segments, 1):
            click.echo(f"  [{i}/{len(segments)}] {seg.chapter_title} (pp.{seg.page_range_str})...")
            seg_source = convert_segment(seg, doc_name, kb_dir, config=config)
            seg_doc_name = f"{doc_name}-{seg.slug}"
            for attempt in range(2):
                try:
                    asyncio.run(compile_short_doc(seg_doc_name, seg_source, kb_dir, model))
                    break
                except Exception as exc:
                    if attempt == 0:
                        click.echo(f"  Retrying compilation in 2s...")
                        time.sleep(2)
                    else:
                        click.echo(f"  [ERROR] Segment compilation failed: {exc}")
                        logger.debug("Segment compilation traceback:", exc_info=True)

        merge_result = merge_segment_concepts(kb_dir / "wiki", doc_name)
        if merge_result["merged"] > 0:
            click.echo(f"  Merged {merge_result['merged']} duplicate concepts")
    except Exception as exc:
        click.echo(f"  [ERROR] Split processing failed: {exc}")
        logger.debug("Split processing traceback:", exc_info=True)
    finally:
        import shutil
        if segments_dir.exists():
            shutil.rmtree(segments_dir, ignore_errors=True)


def _compile_long_doc(file_path: Path, result, kb_dir: Path, model: str) -> None:
    """Process a long document via PageIndex."""
    from openkb.agent.compiler import compile_long_doc

    logger = logging.getLogger(__name__)
    doc_name = result.doc_name
    click.echo(f"  Long document detected — indexing with PageIndex...")
    try:
        from openkb.indexer import index_long_document
        index_result = index_long_document(result.raw_path, kb_dir)
    except Exception as exc:
        click.echo(f"  [ERROR] Indexing failed: {exc}")
        logger.debug("Indexing traceback:", exc_info=True)
        return

    summary_path = kb_dir / "wiki" / "summaries" / f"{doc_name}.md"
    click.echo(f"  Compiling long doc (doc_id={index_result.doc_id})...")
    for attempt in range(2):
        try:
            asyncio.run(
                compile_long_doc(doc_name, summary_path, index_result.doc_id, kb_dir, model,
                                 doc_description=index_result.description)
            )
            break
        except Exception as exc:
            if attempt == 0:
                click.echo(f"  Retrying compilation in 2s...")
                time.sleep(2)
            else:
                click.echo(f"  [ERROR] Compilation failed: {exc}")
                logger.debug("Compilation traceback:", exc_info=True)


def _add_files_parallel(files: list[Path], kb_dir: Path, max_concurrency: int, *, force: bool = False) -> None:
    """Add multiple files with parallel short-doc compilation."""
    from openkb.agent.batch import compile_batch
    from openkb.state import HashRegistry

    openkb_dir = kb_dir / ".openkb"
    config = load_config(openkb_dir / "config.yaml")
    _setup_llm_key(kb_dir)
    model: str = config.get("model", DEFAULT_CONFIG["model"])
    registry = HashRegistry(openkb_dir / "hashes.json")

    # Phase 1: Convert all files and route to appropriate pipeline
    short_tasks = []
    for f in files:
        click.echo(f"\nConverting: {f.name}")
        try:
            result = convert_document(f, kb_dir, force=force)
        except Exception as e:
            click.echo(f"  [ERROR] Conversion failed: {e}")
            continue
        if result.skipped:
            click.echo(f"  [SKIP] Already in knowledge base")
            continue

        if result.is_large_pdf:
            _compile_large_pdf(f, result, kb_dir, model, config)
            if result.file_hash:
                registry.add(result.file_hash, {"name": f.name, "type": f.suffix.lstrip(".")})
            append_log(kb_dir / "wiki", "ingest", f.name)
        elif result.is_long_doc:
            _compile_long_doc(f, result, kb_dir, model)
            if result.file_hash:
                registry.add(result.file_hash, {"name": f.name, "type": "long_pdf"})
            append_log(kb_dir / "wiki", "ingest", f.name)
        else:
            short_tasks.append((f, result))

    # Phase 2: Compile short docs in parallel
    if short_tasks:
        doc_tasks = [
            {"doc_name": r.doc_name, "source_path": r.source_path}
            for f, r in short_tasks
        ]
        click.echo(f"\nCompiling {len(doc_tasks)} document(s) (concurrency={max_concurrency})...")
        batch_results = asyncio.run(compile_batch(doc_tasks, kb_dir, model, max_concurrency))

        for (f, result), batch_result in zip(short_tasks, batch_results):
            if batch_result["status"] == "ok" and result.file_hash:
                registry.add(result.file_hash, {"name": f.name, "type": f.suffix.lstrip(".")})
                append_log(kb_dir / "wiki", "ingest", f.name)

    # Phase 3: Cross-doc concept merge
    from openkb.merger import merge_cross_doc_concepts
    merge_result = merge_cross_doc_concepts(kb_dir / "wiki")
    if merge_result["merged"] > 0:
        click.echo(f"  Cross-doc merge: {merge_result['merged']} concepts merged")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@click.group()
@click.option("-v", "--verbose", is_flag=True, default=False, help="Enable verbose logging.")
@click.option("--kb-dir", "kb_dir_override", default=None, type=click.Path(exists=True, file_okay=False, resolve_path=True), help="Path to a KB root directory (overrides auto-detection).")
@click.pass_context
def cli(ctx, verbose, kb_dir_override):
    """OpenKB — Karpathy's LLM Knowledge Base workflow, powered by PageIndex."""
    logging.basicConfig(
        format="%(name)s %(levelname)s: %(message)s",
        level=logging.WARNING,
    )
    if verbose:
        logging.getLogger("openkb").setLevel(logging.DEBUG)
    ctx.ensure_object(dict)
    if kb_dir_override:
        ctx.obj["kb_dir_override"] = Path(kb_dir_override)
    else:
        env_kb = os.environ.get("OPENKB_DIR")
        if env_kb:
            ctx.obj["kb_dir_override"] = Path(env_kb).resolve()
        else:
            ctx.obj["kb_dir_override"] = None


@cli.command()
@click.argument("path", default=".")
def use(path):
    """Set PATH as the default knowledge base."""
    target = Path(path).resolve()
    if not (target / ".openkb").is_dir():
        click.echo(f"Not a knowledge base: {target}")
        return
    register_kb(target)
    click.echo(f"Default KB set to: {target}")


@cli.command()
def init():
    """Initialise a new knowledge base in the current directory."""
    openkb_dir = Path(".openkb")
    if openkb_dir.exists():
        click.echo("Knowledge base already initialized.")
        return

    # Interactive prompts
    click.echo("Pick an LLM in `provider/model` LiteLLM format:")
    click.echo("  OpenAI:    gpt-5.4-mini, gpt-5.4")
    click.echo("  Anthropic: anthropic/claude-sonnet-4-6, anthropic/claude-opus-4-6")
    click.echo("  Gemini:    gemini/gemini-3.1-pro-preview, gemini/gemini-3-flash-preview")
    click.echo("  Others:    see https://docs.litellm.ai/docs/providers")
    click.echo()
    model = click.prompt(
        f"Model (enter for default {DEFAULT_CONFIG['model']})",
        default=DEFAULT_CONFIG["model"],
        show_default=False,
    )
    api_key = click.prompt(
        "LLM API Key (saved to .env, enter to skip)",
        default="",
        hide_input=True,
        show_default=False,
    ).strip()
    # Create directory structure
    Path("raw").mkdir(exist_ok=True)
    Path("wiki/sources/images").mkdir(parents=True, exist_ok=True)
    Path("wiki/summaries").mkdir(parents=True, exist_ok=True)
    Path("wiki/concepts").mkdir(parents=True, exist_ok=True)

    # Write wiki files
    Path("wiki/AGENTS.md").write_text(AGENTS_MD, encoding="utf-8")
    Path("wiki/index.md").write_text(
        "# Knowledge Base Index\n\n## Documents\n\n## Concepts\n\n## Explorations\n",
        encoding="utf-8",
    )
    Path("wiki/log.md").write_text("# Operations Log\n\n", encoding="utf-8")

    # Create .openkb/ state directory
    openkb_dir.mkdir()
    config = {
        "model": model,
        "language": DEFAULT_CONFIG["language"],
        "pageindex_threshold": DEFAULT_CONFIG["pageindex_threshold"],
    }
    save_config(openkb_dir / "config.yaml", config)
    (openkb_dir / "hashes.json").write_text(json.dumps({}), encoding="utf-8")

    # Write API key to KB-local .env (0600) if the user provided one
    if api_key:
        env_path = Path(".env")
        if env_path.exists():
            click.echo(".env already exists, skipping write. Add LLM_API_KEY manually if needed.")
        else:
            env_path.write_text(f"LLM_API_KEY={api_key}\n", encoding="utf-8")
            os.chmod(env_path, 0o600)
            click.echo("Saved LLM API key to .env.")

    # Register this KB in the global config
    register_kb(Path.cwd())

    click.echo("Knowledge base initialized.")


@cli.command()
@click.argument("path")
@click.option("--force", is_flag=True, default=False,
              help="Re-process even if file already exists in knowledge base.")
@click.pass_context
def add(ctx, path, force):
    """Add a document or directory of documents at PATH to the knowledge base."""
    kb_dir = _find_kb_dir(ctx.obj.get("kb_dir_override"))
    if kb_dir is None:
        click.echo("No knowledge base found. Run `openkb init` first.")
        return

    target = Path(path)
    if not target.exists():
        click.echo(f"Path does not exist: {path}")
        return

    if target.is_dir():
        files = [
            f for f in sorted(target.rglob("*"))
            if f.is_file() and f.suffix.lower() in SUPPORTED_EXTENSIONS
        ]
        if not files:
            click.echo(f"No supported files found in {path}.")
            return
        total = len(files)
        click.echo(f"Found {total} supported file(s) in {path}.")

        config = load_config(kb_dir / ".openkb" / "config.yaml")
        concurrency = config.get("compile_concurrency", 3)

        if total > 1 and concurrency > 1:
            _add_files_parallel(files, kb_dir, concurrency, force=force)
        else:
            for i, f in enumerate(files, 1):
                click.echo(f"\n[{i}/{total}] ", nl=False)
                add_single_file(f, kb_dir, force=force)
    else:
        if target.suffix.lower() not in SUPPORTED_EXTENSIONS:
            click.echo(
                f"Unsupported file type: {target.suffix}. "
                f"Supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))}"
            )
            return
        add_single_file(target, kb_dir, force=force)


@cli.command()
@click.argument("question")
@click.option("--save", is_flag=True, default=False, help="Save the answer to wiki/explorations/.")
@click.option(
    "--raw", "raw",
    is_flag=True, default=False,
    help="Show raw markdown source instead of rendered output (keeps tool-call colors).",
)
@click.pass_context
def query(ctx, question, save, raw):
    """Query the knowledge base with QUESTION."""
    kb_dir = _find_kb_dir(ctx.obj.get("kb_dir_override"))
    if kb_dir is None:
        click.echo("No knowledge base found. Run `openkb init` first.")
        return

    from openkb.agent.query import run_query

    openkb_dir = kb_dir / ".openkb"
    config = load_config(openkb_dir / "config.yaml")
    _setup_llm_key(kb_dir)
    model: str = config.get("model", DEFAULT_CONFIG["model"])

    try:
        answer = asyncio.run(run_query(question, kb_dir, model, stream=True, raw=raw))
    except Exception as exc:
        click.echo(f"[ERROR] Query failed: {exc}")
        return

    append_log(kb_dir / "wiki", "query", question)

    if save and answer:
        import re
        slug = re.sub(r"[^a-z0-9]+", "-", question.lower()).strip("-")[:60]
        explore_dir = kb_dir / "wiki" / "explorations"
        explore_dir.mkdir(parents=True, exist_ok=True)
        explore_path = explore_dir / f"{slug}.md"
        explore_path.write_text(
            f"---\nquery: \"{question}\"\n---\n\n{answer}\n", encoding="utf-8"
        )
        click.echo(f"\nSaved to {explore_path}")


@cli.command()
@click.option(
    "--resume", "-r", "resume",
    is_flag=False, flag_value="__latest__", default=None, metavar="[ID]",
    help="Resume the latest chat session, or a specific one by id or prefix.",
)
@click.option(
    "--list", "list_sessions_flag",
    is_flag=True, default=False,
    help="List chat sessions.",
)
@click.option(
    "--delete", "delete_id",
    default=None, metavar="ID",
    help="Delete a chat session by id or prefix.",
)
@click.option(
    "--no-color", "no_color",
    is_flag=True, default=False,
    help="Disable colored output.",
)
@click.option(
    "--raw", "raw",
    is_flag=True, default=False,
    help="Show raw markdown source instead of rendered output (keeps prompt and tool-call colors).",
)
@click.pass_context
def chat(ctx, resume, list_sessions_flag, delete_id, no_color, raw):
    """Start an interactive chat with the knowledge base."""
    kb_dir = _find_kb_dir(ctx.obj.get("kb_dir_override"))
    if kb_dir is None:
        click.echo("No knowledge base found. Run `openkb init` first.")
        return

    from openkb.agent.chat_session import (
        ChatSession,
        delete_session,
        list_sessions,
        load_session,
        relative_time,
        resolve_session_id,
    )

    if list_sessions_flag:
        sessions = list_sessions(kb_dir)
        if not sessions:
            click.echo("No chat sessions yet.")
            return
        click.echo(f"  {'ID':<22} {'TURNS':<6} {'UPDATED':<12} TITLE")
        click.echo(f"  {'-'*22} {'-'*6} {'-'*12} {'-'*30}")
        for s in sessions:
            rel = relative_time(s.get("updated_at", ""))
            title = s.get("title") or "(empty)"
            click.echo(
                f"  {s['id']:<22} {s['turn_count']:<6} {rel:<12} {title}"
            )
        click.echo(
            f"\n{len(sessions)} session(s) in {kb_dir / '.openkb' / 'chats'}"
        )
        return

    if delete_id is not None:
        try:
            resolved = resolve_session_id(kb_dir, delete_id)
        except ValueError as exc:
            click.echo(f"[ERROR] {exc}")
            return
        if not resolved:
            click.echo(f"No matching session: {delete_id}")
            return
        if delete_session(kb_dir, resolved):
            click.echo(f"Deleted session {resolved}")
        else:
            click.echo(f"Could not delete session: {resolved}")
        return

    openkb_dir = kb_dir / ".openkb"
    config = load_config(openkb_dir / "config.yaml")
    _setup_llm_key(kb_dir)

    if resume is not None:
        try:
            resolved = resolve_session_id(kb_dir, resume)
        except ValueError as exc:
            click.echo(f"[ERROR] {exc}")
            return
        if not resolved:
            if resume == "__latest__":
                click.echo("No previous chat sessions to resume.")
            else:
                click.echo(f"No matching session: {resume}")
            return
        session = load_session(kb_dir, resolved)
    else:
        model: str = config.get("model", DEFAULT_CONFIG["model"])
        language: str = config.get("language", "en")
        session = ChatSession.new(kb_dir, model, language)

    from openkb.agent.chat import run_chat

    try:
        asyncio.run(run_chat(kb_dir, session, no_color=no_color, raw=raw))
    except Exception as exc:
        click.echo(f"[ERROR] Chat failed: {exc}")


@cli.command()
@click.pass_context
def watch(ctx):
    """Watch the raw/ directory for new documents and process them automatically."""
    kb_dir = _find_kb_dir(ctx.obj.get("kb_dir_override"))
    if kb_dir is None:
        click.echo("No knowledge base found. Run `openkb init` first.")
        return

    from openkb.watcher import watch_directory

    raw_dir = kb_dir / "raw"
    raw_dir.mkdir(exist_ok=True)

    def on_new_files(paths):
        for p in paths:
            fp = Path(p)
            if fp.suffix.lower() not in SUPPORTED_EXTENSIONS:
                click.echo(
                    f"Skipping unsupported file type: {fp.suffix}. "
                    f"Supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))}"
                )
                continue
            add_single_file(fp, kb_dir)

    click.echo(f"Watching {raw_dir} for new documents. Press Ctrl+C to stop.")
    watch_directory(raw_dir, on_new_files)


async def run_lint(kb_dir: Path) -> Path | None:
    """Run structural + knowledge lint, write report, return report path.

    Returns ``None`` if the KB has no indexed documents (nothing to lint).
    Async because knowledge lint uses an LLM agent. Usable from CLI
    (via ``asyncio.run``) and directly from the chat REPL.
    """
    from openkb.lint import (
        run_structural_lint,
        check_source_diversity,
        check_concept_clusters,
        check_book_coverage,
        format_severity_report,
    )
    from openkb.agent.linter import run_knowledge_lint

    openkb_dir = kb_dir / ".openkb"

    # Skip lint entirely when the KB has no indexed documents
    hashes_file = openkb_dir / "hashes.json"
    if hashes_file.exists():
        hashes = json.loads(hashes_file.read_text(encoding="utf-8"))
    else:
        hashes = {}
    if not hashes:
        click.echo("Nothing to lint — no documents indexed yet. Run `openkb add` first.")
        return

    config = load_config(openkb_dir / "config.yaml")
    _setup_llm_key(kb_dir)
    model: str = config.get("model", DEFAULT_CONFIG["model"])

    wiki = kb_dir / "wiki"

    click.echo("Running structural lint...")
    structural_report = run_structural_lint(kb_dir)
    click.echo(structural_report)

    # Code-based severity checks
    click.echo("Running code-based checks...")
    code_issues = []
    code_issues.extend(check_source_diversity(wiki))
    code_issues.extend(check_concept_clusters(wiki))
    code_issues.extend(check_book_coverage(wiki))
    if code_issues:
        for issue in code_issues:
            click.echo(f"  [{issue.severity.value}] {issue.title}")
    else:
        click.echo("  No code-based issues found.")

    # LLM-based semantic lint (skip for tiny wikis)
    knowledge_report = ""
    if len(hashes) >= 3:
        click.echo("Running knowledge lint...")
        try:
            knowledge_report = await run_knowledge_lint(kb_dir, model)
        except Exception as exc:
            knowledge_report = f"Knowledge lint failed: {exc}"
        click.echo(knowledge_report)
    else:
        knowledge_report = "Skipped — wiki has fewer than 3 documents."
        click.echo("Wiki too small for semantic lint (< 3 documents).")

    # Combine into structured report
    reports_dir = kb_dir / "wiki" / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    import datetime
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = reports_dir / f"lint_{timestamp}.md"

    full_report = format_severity_report(code_issues, structural_report, knowledge_report)
    report_content = f"# Lint Report — {timestamp}\n\n{full_report}\n"
    report_path.write_text(report_content, encoding="utf-8")
    append_log(kb_dir / "wiki", "lint", f"report → {report_path.name}")
    click.echo(f"\nReport written to {report_path}")
    return report_path


@cli.command()
@click.option("--fix", is_flag=True, default=False, help="Automatically fix lint issues (not yet implemented).")
@click.pass_context
def lint(ctx, fix):
    """Lint the knowledge base for structural and semantic inconsistencies."""
    if fix:
        click.echo("Warning: --fix is not yet implemented. Running lint in report-only mode.")
    kb_dir = _find_kb_dir(ctx.obj.get("kb_dir_override"))
    if kb_dir is None:
        click.echo("No knowledge base found. Run `openkb init` first.")
        return
    asyncio.run(run_lint(kb_dir))


@cli.command()
@click.option("--describe-images", "describe_images_flag", is_flag=True, default=False,
              help="Retroactively add VLM descriptions to images in existing wiki/sources/.")
@click.option("--enrich-frontmatter", "enrich_frontmatter_flag", is_flag=True, default=False,
              help="Add missing YAML frontmatter fields (type, title, date, tags) to wiki pages.")
@click.option("--deep-study", "deep_study_flag", is_flag=True, default=False,
              help="Generate deep study content (ELI5, analogies, questions) for existing concept pages.")
@click.pass_context
def upgrade(ctx, describe_images_flag, enrich_frontmatter_flag, deep_study_flag):
    """Upgrade existing wiki data with new features (no re-compilation)."""
    kb_dir = _find_kb_dir(ctx.obj.get("kb_dir_override"))
    if kb_dir is None:
        click.echo("No knowledge base found. Run `openkb init` first.")
        return

    if not describe_images_flag and not enrich_frontmatter_flag and not deep_study_flag:
        click.echo("No upgrade step specified. Use --describe-images, --enrich-frontmatter, or --deep-study.")
        return

    # --enrich-frontmatter: add missing YAML fields to wiki pages
    if enrich_frontmatter_flag:
        from openkb.frontmatter import enrich_directory

        wiki_dir = kb_dir / "wiki"
        total = 0
        total += enrich_directory(wiki_dir / "sources", "source")
        total += enrich_directory(wiki_dir / "summaries", "summary")
        total += enrich_directory(wiki_dir / "concepts", "concept")
        if total == 0:
            click.echo("All pages already have complete frontmatter. Nothing to upgrade.")
        else:
            click.echo(f"Enriched {total} pages with frontmatter fields.")
        if not describe_images_flag and not deep_study_flag:
            return

    # --deep-study: generate active learning content for concept pages
    if deep_study_flag:
        _setup_llm_key(kb_dir)
        openkb_dir = kb_dir / ".openkb"
        config = load_config(openkb_dir / "config.yaml")
        model = config.get("model", "gpt-4o-mini")
        wiki_dir = kb_dir / "wiki"
        concepts_dir = wiki_dir / "concepts"

        from openkb.agent.compiler import generate_deep_study_for_concept

        concept_files = sorted(concepts_dir.glob("*.md")) if concepts_dir.exists() else []
        if not concept_files:
            click.echo("No concept pages found.")
        else:
            generated = 0
            for cf in concept_files:
                if generate_deep_study_for_concept(cf, model, wiki_dir):
                    click.echo(f"  Generated deep study: {cf.name}")
                    generated += 1
            if generated == 0:
                click.echo("All concept pages already have deep study content.")
            else:
                click.echo(f"Done. Generated deep study for {generated} concept page(s).")
        if not describe_images_flag:
            return

    _setup_llm_key(kb_dir)
    openkb_dir = kb_dir / ".openkb"
    config = load_config(openkb_dir / "config.yaml")
    model = config.get("vision_model") or config.get("model", "gpt-4o-mini")

    from openkb.image_describer import describe_images, _IMAGE_RE

    sources_dir = kb_dir / "wiki" / "sources"
    source_files = sorted(sources_dir.glob("*.md"))
    if not source_files:
        click.echo("No source files found in wiki/sources/.")
        return

    # Pattern to detect images already having descriptions
    _DESCRIBED_RE = _IMAGE_RE

    total_images = 0
    updated_files = 0

    for src_file in source_files:
        markdown = src_file.read_text(encoding="utf-8")

        # Count images without descriptions below them
        matches = list(_IMAGE_RE.finditer(markdown))
        if not matches:
            continue

        # Check which images already have descriptions
        needs_desc = []
        for m in matches:
            after = markdown[m.end():]
            # If *[Figure: appears right after (within 50 chars), skip
            if "*[Figure:" in after[:50]:
                continue
            needs_desc.append(m)

        if not needs_desc:
            continue

        click.echo(f"  {src_file.name}: {len(needs_desc)} images need descriptions")
        total_images += len(needs_desc)

        enriched = describe_images(markdown, kb_dir, model, max_images=len(needs_desc))
        if enriched != markdown:
            src_file.write_text(enriched, encoding="utf-8")
            updated_files += 1

    if total_images == 0:
        click.echo("All images already have descriptions. Nothing to upgrade.")
    else:
        click.echo(f"Done. Described {total_images} images across {updated_files} files.")


def print_list(kb_dir: Path) -> None:
    """Print all documents in the knowledge base. Usable from CLI and chat REPL."""
    openkb_dir = kb_dir / ".openkb"
    hashes_file = openkb_dir / "hashes.json"
    if not hashes_file.exists():
        click.echo("No documents indexed yet.")
        return

    hashes = json.loads(hashes_file.read_text(encoding="utf-8"))
    if not hashes:
        click.echo("No documents indexed yet.")
        return

    # Display documents table with count in header
    doc_count = len(hashes)
    click.echo(f"Documents ({doc_count}):")
    click.echo(f"  {'Name':<40} {'Type':<12} {'Pages':<8}")
    click.echo(f"  {'-'*40} {'-'*12} {'-'*8}")
    for file_hash, meta in hashes.items():
        name = meta.get("name", "unknown")
        raw_type = meta.get("type", "unknown")
        display = _display_type(raw_type)
        pages = meta.get("pages", "")
        pages_str = str(pages) if pages else ""
        click.echo(f"  {name:<40} {display:<12} {pages_str:<8}")

    # Display summaries
    summaries_dir = kb_dir / "wiki" / "summaries"
    if summaries_dir.exists():
        summaries = sorted(p.stem for p in summaries_dir.glob("*.md"))
        if summaries:
            click.echo(f"\nSummaries ({len(summaries)}):")
            for s in summaries:
                click.echo(f"  - {s}")

    # Display concepts
    concepts_dir = kb_dir / "wiki" / "concepts"
    if concepts_dir.exists():
        concepts = sorted(p.stem for p in concepts_dir.glob("*.md"))
        if concepts:
            click.echo(f"\nConcepts ({len(concepts)}):")
            for c in concepts:
                click.echo(f"  - {c}")

    # Display reports
    reports_dir = kb_dir / "wiki" / "reports"
    if reports_dir.exists():
        reports = sorted(p.name for p in reports_dir.glob("*.md"))
        if reports:
            click.echo(f"\nReports ({len(reports)}):")
            for r in reports:
                click.echo(f"  - {r}")


@cli.command(name="list")
@click.pass_context
def list_cmd(ctx):
    """List all documents in the knowledge base."""
    kb_dir = _find_kb_dir(ctx.obj.get("kb_dir_override"))
    if kb_dir is None:
        click.echo("No knowledge base found. Run `openkb init` first.")
        return
    print_list(kb_dir)


def print_status(kb_dir: Path) -> None:
    """Print knowledge base status. Usable from CLI and chat REPL."""
    wiki_dir = kb_dir / "wiki"
    subdirs = ["sources", "summaries", "concepts", "reports"]

    click.echo("Knowledge Base Status:")
    click.echo(f"  {'Directory':<20} {'Files':<10}")
    click.echo(f"  {'-'*20} {'-'*10}")

    for subdir in subdirs:
        path = wiki_dir / subdir
        if path.exists():
            count = len(list(path.glob("*.md")))
        else:
            count = 0
        click.echo(f"  {subdir:<20} {count:<10}")

    # Raw files
    raw_dir = kb_dir / "raw"
    if raw_dir.exists():
        raw_count = len([f for f in raw_dir.iterdir() if f.is_file()])
        click.echo(f"  {'raw':<20} {raw_count:<10}")

    # Hash registry summary
    openkb_dir = kb_dir / ".openkb"
    hashes_file = openkb_dir / "hashes.json"
    if hashes_file.exists():
        hashes = json.loads(hashes_file.read_text(encoding="utf-8"))
        click.echo(f"\n  Total indexed: {len(hashes)} document(s)")

    # Last compile time: newest file in wiki/summaries/
    summaries_dir = wiki_dir / "summaries"
    if summaries_dir.exists():
        summaries = list(summaries_dir.glob("*.md"))
        if summaries:
            newest_summary = max(summaries, key=lambda p: p.stat().st_mtime)
            import datetime
            mtime = datetime.datetime.fromtimestamp(newest_summary.stat().st_mtime)
            click.echo(f"  Last compile:  {mtime.strftime('%Y-%m-%d %H:%M:%S')}")

    # Last lint time: newest file in wiki/reports/
    reports_dir = wiki_dir / "reports"
    if reports_dir.exists():
        reports = list(reports_dir.glob("*.md"))
        if reports:
            newest_report = max(reports, key=lambda p: p.stat().st_mtime)
            import datetime
            mtime = datetime.datetime.fromtimestamp(newest_report.stat().st_mtime)
            click.echo(f"  Last lint:     {mtime.strftime('%Y-%m-%d %H:%M:%S')}")


@cli.command()
@click.pass_context
def status(ctx):
    """Show the current status of the knowledge base."""
    kb_dir = _find_kb_dir(ctx.obj.get("kb_dir_override"))
    if kb_dir is None:
        click.echo("No knowledge base found. Run `openkb init` first.")
        return
    print_status(kb_dir)
