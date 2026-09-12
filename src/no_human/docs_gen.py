"""Repo-wiki generator — OpenWiki-inspired, no external dependency.

Spawns a bounded, read-only Agent SDK session (mirroring ``onboard.AgentDeriver``)
that inspects a repo's structure, code, and git history, then produces:

  - ``.no_human/wiki/architecture.md``
  - ``.no_human/wiki/modules.md``
  - ``.no_human/wiki/conventions.md``

A single delimited block is written into the repo's ``CLAUDE.md`` (created if
absent) pointing agents to the wiki — never the full content.  Regeneration
replaces the block atomically; user content outside the delimiters is preserved.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_KEYS = ("architecture", "modules", "conventions")

# The json_schema handed to the SDK so the model's answer arrives as a parsed
# ResultMessage.structured_output instead of prose we must scrape.
WIKI_SCHEMA = {
    "type": "object",
    "properties": {k: {"type": "string"} for k in _KEYS},
    "additionalProperties": False,
}

# Fallback scraper: fenced ```json / ```JSON / bare ``` blocks. `.*?` + DOTALL
# so a block spanning newlines is captured; the fence tag is optional.
_FENCE = re.compile(r"```(?:json|JSON)?\s*\n(.*?)```", re.DOTALL)

# Delimiters for the managed block in CLAUDE.md.
_WIKI_BLOCK_START = "<!-- no_human:wiki -->"
_WIKI_BLOCK_END = "<!-- /no_human:wiki -->"

WIKI_DIR = Path(".no_human") / "wiki"
WIKI_FILES = ("architecture.md", "modules.md", "conventions.md")

_CLAUDE_MD_BLOCK = (
    f"{_WIKI_BLOCK_START}\n"
    "## Auto-generated repo wiki\n\n"
    "See `.no_human/wiki/` for architecture, modules, and conventions docs.\n"
    "These files are regenerated automatically — edit with care.\n"
    f"{_WIKI_BLOCK_END}"
)


@dataclass
class WikiResult:
    """Output of a wiki generation run."""
    repo_path: str = ""
    files_written: list[str] = field(default_factory=list)
    commit_sha: str = ""
    error: str | None = None
    skipped: bool = False   # W3.6: HEAD unchanged since last wiki — no cost


PROMPT = (
    "You are documenting the repository at {repo}. Do NOT modify anything — "
    "only read files (grep/glob/read).\n\n"
    "Inspect the repo's structure, key source files, configuration, CI, tests, "
    "README, and recent git history (git log -20 --oneline). Produce a concise "
    "developer wiki with three sections.\n\n"
    "Respond with ONLY a fenced ```json block:\n"
    '{{"architecture": "<markdown content for architecture.md — '
    'high-level design, major components, data flow, key abstractions>", '
    '"modules": "<markdown content for modules.md — '
    'directory layout, what each top-level module/package does, entry points>", '
    '"conventions": "<markdown content for conventions.md — '
    'coding style, testing patterns, naming, CI/CD, contribution rules>"}}'
)


REFRESH_PROMPT = (
    "You maintain the developer wiki (`.no_human/wiki/*.md`) for {repo}. It was "
    "last generated at commit {since}. Do NOT modify anything — only read.\n\n"
    "These files changed since then:\n{changes}\n\n"
    "Read the existing wiki and the changed files, then UPDATE the three "
    "sections to reflect the changes — keep everything still accurate, revise "
    "only what the diff affected. Respond with ONLY a fenced ```json block "
    'with keys "architecture", "modules", "conventions" (full updated markdown '
    "for each, as before)."
)


class WikiGenerator:
    """Generate repo wiki docs via a bounded Agent SDK session.

    Mirrors ``onboard.AgentDeriver``: read-only, turn-bounded, structured JSON
    output parsed from a fenced block.
    """

    def __init__(self, backend: Any, *, max_turns: int = 12):
        self.backend = backend
        self.max_turns = max_turns

    async def generate(
        self, repo_path: str | Path, *, since_sha: str | None = None,
    ) -> WikiResult:
        """Run the wiki generation session and write files.

        W3.6 incremental refresh: when *since_sha* (the last wiki_commit) is
        given and equals HEAD, the wiki is up to date — return skipped with no
        backend call (OpenWiki's --update gate). When it differs, the agent is
        pointed at the diff since then and asked to UPDATE the existing wiki
        rather than regenerate from scratch. No *since_sha* → full generation."""
        repo = Path(repo_path).expanduser().resolve()
        if not repo.is_dir():
            return WikiResult(repo_path=str(repo), error=f"not a directory: {repo}")

        commit_sha = _git_head(repo)

        if since_sha and commit_sha and since_sha == commit_sha:
            return WikiResult(repo_path=str(repo), commit_sha=commit_sha,
                              skipped=True)

        prompt = PROMPT.format(repo=repo)
        if since_sha and commit_sha and since_sha != commit_sha:
            changed = _git_diff_stat(repo, since_sha)
            if changed:
                prompt = REFRESH_PROMPT.format(repo=repo, since=since_sha[:8],
                                               changes=changed)

        result = await self.backend.run(
            prompt,
            cwd=repo,
            max_turns=self.max_turns,
            effort="low",
            output_format={"type": "json_schema", "schema": WIKI_SCHEMA},
        )

        text = result.final_text or ""
        # Structured output first (the SDK enforced the schema); fall back to
        # scraping the prose only when the CLI produced none (older CLI, a run
        # that never emitted a final structured message).
        so = getattr(result, "structured_output", None)
        if isinstance(so, dict) and any(k in so for k in _KEYS):
            parsed = {k: str(v) for k, v in so.items() if k in _KEYS}
        else:
            parsed = _parse_wiki_json(text)
        if parsed is None:
            excerpt = text[:300].replace("\n", " ")
            return WikiResult(
                repo_path=str(repo),
                commit_sha=commit_sha,
                error="failed to parse wiki JSON from agent output: " + excerpt,
            )

        written = _write_wiki_files(repo, parsed)
        _update_claude_md(repo)

        return WikiResult(
            repo_path=str(repo),
            files_written=written,
            commit_sha=commit_sha,
        )


# --------------------------------------------------------------------------- #
# Pure helpers (testable without an Agent SDK backend)                         #
# --------------------------------------------------------------------------- #


def _parse_wiki_json(text: str) -> dict[str, str] | None:
    """Extract the wiki JSON from agent output.

    Accepts fenced ```json / ```JSON / bare ``` blocks and a bare top-level
    object, and repairs invalid backslash escapes (``C:\\x``, ``\\d``) via
    ``loads_lenient`` before giving up. The LAST valid candidate wins — the
    final answer, not an example the model quoted earlier.
    """
    from .core.jsonparse import loads_lenient

    candidates = _FENCE.findall(text) or []
    stripped = text.strip()
    if stripped.startswith("{"):
        candidates.append(stripped)
    for block in reversed(candidates):
        try:
            data = loads_lenient(block)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and any(k in data for k in _KEYS):
            return {k: str(v) for k, v in data.items() if k in _KEYS}
    return None


def _write_wiki_files(repo: Path, data: dict[str, str]) -> list[str]:
    """Write wiki markdown files to ``<repo>/.no_human/wiki/``."""
    wiki_dir = repo / WIKI_DIR
    wiki_dir.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for name in WIKI_FILES:
        key = name.removesuffix(".md")
        content = data.get(key, "")
        if not content:
            continue
        path = wiki_dir / name
        path.write_text(content if content.endswith("\n") else content + "\n", encoding="utf-8")
        written.append(str(path.relative_to(repo)))
    return written


def _update_claude_md(repo: Path) -> None:
    """Insert or replace the wiki pointer block in ``CLAUDE.md``."""
    claude_md = repo / "CLAUDE.md"
    if claude_md.exists():
        text = claude_md.read_text(encoding="utf-8")
    else:
        text = ""
    new_text = upsert_wiki_block(text)
    claude_md.write_text(new_text, encoding="utf-8")


def upsert_wiki_block(text: str) -> str:
    """Replace or append the ``<!-- no_human:wiki -->`` block.

    Preserves all user content outside the delimiters. On the first run,
    appends the block. On subsequent runs, replaces the existing block
    in place — never duplicates.
    """
    if _WIKI_BLOCK_START in text:
        # Replace existing block.
        pattern = re.compile(
            re.escape(_WIKI_BLOCK_START) + r".*?" + re.escape(_WIKI_BLOCK_END),
            re.DOTALL,
        )
        return pattern.sub(_CLAUDE_MD_BLOCK, text, count=1)
    # Append (with a blank line separator if the file has content).
    if text and not text.endswith("\n"):
        text += "\n"
    if text:
        text += "\n"
    text += _CLAUDE_MD_BLOCK + "\n"
    return text


def _git_head(repo: Path) -> str:
    """Return HEAD SHA or empty string."""
    try:
        r = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5,
            cwd=repo,
        )
        return r.stdout.strip() if r.returncode == 0 else ""
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""


def _git_diff_stat(repo: Path, since_sha: str, *, max_chars: int = 3000) -> str:
    """`git diff --stat <since>..HEAD`, capped — the changed-file summary that
    seeds the incremental wiki refresh. Empty on any git failure (caller then
    falls back to a full regeneration)."""
    try:
        r = subprocess.run(
            ["git", "diff", "--stat", f"{since_sha}..HEAD"],
            capture_output=True, text=True, timeout=10, cwd=repo,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return ""
    if r.returncode != 0:
        return ""
    return (r.stdout or "").strip()[:max_chars]
