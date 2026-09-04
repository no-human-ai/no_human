"""`nh verifiers` — list/add/check/propose the repo's natural-language
verifiers (`review/verifiers.py`).

This module is an authoring and inspection surface only. None of its
commands make a model call, a network call, or construct an
`Orchestrator` — the only place a verifier's statement is ever put to a
judge is `Orchestrator._run_review` via `review.verifiers.run_verifiers`,
which this module never calls. `check` in particular is a config/selection
gate: it answers "which verifiers would run, and would any of them fail
closed for lack of a matching hunk" without ever building the prompt that
`run_verifiers` would send.
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from ..config import NO_HUMAN_HOME, load_config
from ..core.db import Store
from ..review.reviewer import findings_from_checklist
from ..review.verifiers import (
    filter_diff,
    load_verifiers,
    select,
    validate_entry,
)
from ..vcs.git import GitError, GitRepo

console = Console()

# `.commands` registers this module's `verifiers_group` via
# `cli.add_command`, so a top-level `from .commands import ...` here would be
# circular. `print_no_task_matching`/`mark_machine_output` are each a few
# lines with no dependency on anything else in `.commands` — duplicated here
# rather than imported, to keep every import of this file at the top, as a
# mid-file import would otherwise be needed to break the cycle.


def _print_no_task_matching(task_id: str) -> None:
    console.print(f"[red]no task matching[/] {escape(str(task_id))}")
    console.print("Fix: run 'nh task list' to see task ids (a unique id prefix is enough).")


def _mark_machine_output() -> None:
    try:
        root = click.get_current_context().find_root()
        if isinstance(root.obj, dict):
            root.obj["machine_output"] = True
    except Exception:  # noqa: BLE001 — advisory only
        pass

# Mirrors `review.verifiers._SEVERITIES`. That schema is frozen and out of
# scope for this module — this local copy is read-only, used only to decide
# whether a proposed candidate's *own* severity is worth keeping before the
# authoritative `validate_entry` gate runs. It is never a second source of
# truth for what the loader accepts.
_KNOWN_SEVERITIES = {"critical", "high", "medium", "low"}

_SLUG_JUNK_RE = re.compile(r"[^a-z0-9._-]+")
_SLUG_DASHES_RE = re.compile(r"-{2,}")


# --------------------------------------------------------------------------
# pure helpers
# --------------------------------------------------------------------------


def repo_verifiers_path(repo: Path) -> Path:
    return repo / ".no_human" / "verifiers.yaml"


def _global_file() -> Path:
    # Imported as a module attribute (not read at call time from `..config`)
    # so tests can monkeypatch `no_human.cli.verifiers_cmd.NO_HUMAN_HOME`.
    return NO_HUMAN_HOME / "verifiers.yaml"


def render_entry(v_id: str, statement: str, paths: list[str], severity: str) -> str:
    """Render one verifiers-list entry as YAML text, ready to splice into an
    existing `verifiers:` list. Uses a literal block scalar for `statement`
    and JSON-style (== valid YAML double-quoted) strings for `paths` so
    arbitrary punctuation (colons, leading `*`, quotes) never has to be
    hand-escaped — this is a textual append, not a `yaml.safe_dump` of the
    whole document, so it can't reorder or reformat anything already there.
    """
    lines = [f"  - id: {v_id}", "    statement: |-"]
    body_lines = statement.splitlines() or [""]
    for line in body_lines:
        lines.append(f"      {line}" if line else "")
    lines.append("    paths:")
    for p in paths:
        lines.append(f"      - {json.dumps(p)}")
    lines.append(f"    severity: {severity}")
    return "\n".join(lines) + "\n"


_TOP_LEVEL_VERIFIERS_RE = re.compile(r"^verifiers:\s*(#.*)?$")


def append_entry(path: Path, block: str) -> None:
    """Append one rendered entry (see `render_entry`) into `path`'s
    `verifiers:` list, preserving every other byte verbatim. If the file
    doesn't exist yet, it is created with a fresh `verifiers:` header. If it
    exists but has no top-level `verifiers:` key, one is appended at EOF. If
    it exists and already has one, the new entry is spliced in right after
    the last existing list item — not blindly at EOF — so a `verifiers:`
    section that isn't the last top-level key in the file still gets a
    syntactically valid result.
    """
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("verifiers:\n" + block, encoding="utf-8")
        return

    original = path.read_text(encoding="utf-8")
    lines = original.splitlines(keepends=True)
    header_idx = None
    for i, line in enumerate(lines):
        if _TOP_LEVEL_VERIFIERS_RE.match(line.rstrip("\n")):
            header_idx = i
            break

    if header_idx is None:
        prefix = original
        if prefix and not prefix.endswith("\n"):
            prefix += "\n"
        path.write_text(prefix + "verifiers:\n" + block, encoding="utf-8")
        return

    insert_at = len(lines)
    for j in range(header_idx + 1, len(lines)):
        line = lines[j]
        if line.strip() == "":
            continue
        if not line[0].isspace():
            insert_at = j
            break
    new_lines = lines[:insert_at] + [block] + lines[insert_at:]
    path.write_text("".join(new_lines), encoding="utf-8")


def _write_then_verify(
    path: Path, block: str, new_id: str, repo_root: Path, home: Path | None
) -> str | None:
    """Append `block` to `path`, reload the merged verifier set, and confirm
    the new id actually loads clean. On any problem, restore `path`'s
    original bytes (or delete it, if it didn't exist before) and return an
    error string; returns None on success.

    The "did it load clean" check diffs the problem list *before* vs.
    *after* the write, rather than substring-matching `str(path)` against
    every problem line: every problem the loader emits for this file is
    prefixed with `str(path)` (it is the entry's `origin`), so a file that
    already has an unrelated malformed entry would otherwise make every
    future `add`/`propose` to that file look like it broke verification.
    """
    original = path.read_bytes() if path.exists() else None
    before_problems = load_verifiers(repo_root, home=home).problems
    append_entry(path, block)
    report = load_verifiers(repo_root, home=home)
    ids = {v.id for v in report.verifiers}
    new_problems = [p for p in report.problems if p not in before_problems]
    if new_id not in ids or new_problems:
        if original is not None:
            path.write_bytes(original)
        else:
            path.unlink(missing_ok=True)
        reason = new_problems[0] if new_problems else f"verifier {new_id!r} did not load"
        return f"verification failed after write: {reason}"
    return None


def _slugify(text: str) -> str:
    s = (text or "").strip().lower()
    s = _SLUG_JUNK_RE.sub("-", s)
    s = _SLUG_DASHES_RE.sub("-", s).strip("-")
    if not s:
        s = "verifier"
    if not re.match(r"^[a-z0-9]", s):
        s = f"v-{s}"
    s = s[:64].rstrip("-")
    return s or "v-verifier"


def _one_line(text: str) -> str:
    return " ".join((text or "").split())


# --------------------------------------------------------------------------
# nh verifiers list
# --------------------------------------------------------------------------


@click.group("verifiers")
def verifiers_group() -> None:
    """Inspect, author, and check the repo's natural-language verifiers
    (`.no_human/verifiers.yaml`) — never calls a model."""


@verifiers_group.command("list")
@click.option("--repo", default=".", type=click.Path(), help="Repo root to read verifiers.yaml from.")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
def list_cmd(repo: str, as_json: bool) -> None:
    """List every configured verifier (repo + global), and any load
    problems. Always exits 0 — this is a read-only inspection command."""
    repo_root = Path(repo).resolve()
    report = load_verifiers(repo_root, home=NO_HUMAN_HOME)

    if as_json:
        _mark_machine_output()
        payload = {
            "verifiers": [
                {
                    "id": v.id,
                    "statement": v.statement,
                    "paths": list(v.paths),
                    "severity": v.severity,
                    "source": v.source,
                    "source_file": v.source_file,
                }
                for v in report.verifiers
            ],
            "problems": list(report.problems),
        }
        click.echo(json.dumps(payload, indent=2))
        return

    for p in report.problems:
        console.print(f"[red]{p}[/]")
    if not report.verifiers:
        console.print("[dim]no verifiers configured[/]")
        return
    table = Table()
    table.add_column("id")
    table.add_column("severity")
    table.add_column("paths")
    table.add_column("source")
    for v in report.verifiers:
        table.add_row(v.id, v.severity, ", ".join(v.paths), v.source)
    console.print(table)


# --------------------------------------------------------------------------
# nh verifiers add
# --------------------------------------------------------------------------


@verifiers_group.command("add")
@click.option("--id", "v_id", required=True, help="Verifier id (e.g. `no-bare-except`).")
@click.option("--statement", required=True, help="Plain-English rule statement.")
@click.option("--path", "paths", multiple=True, required=True, help="Glob a changed file must match to select this verifier. Repeatable.")
@click.option("--severity", default="high", help="critical|high|medium|low (default: high).")
@click.option("--repo", default=".", type=click.Path(), help="Repo root to write .no_human/verifiers.yaml under.")
@click.option("--global", "-g", "use_global", is_flag=True, help="Write to the global verifiers file (~/.no_human/verifiers.yaml) instead of the repo's.")
def add_cmd(v_id: str, statement: str, paths: tuple, severity: str, repo: str, use_global: bool) -> None:
    """Additively define a new verifier. Never rewrites the whole YAML file —
    only appends the new entry — so hand-authored comments and ordering
    survive untouched. Refuses (exit 1, nothing written) if the id already
    exists, or if the entry itself is invalid."""
    repo_root = Path(repo).resolve()
    existing = load_verifiers(repo_root, home=NO_HUMAN_HOME)
    existing_by_id = {v.id: v for v in existing.verifiers}
    if v_id in existing_by_id:
        console.print(
            f"[red]refused:[/] verifier {v_id!r} is already defined in "
            f"{existing_by_id[v_id].source_file}"
        )
        sys.exit(1)

    target = _global_file() if use_global else repo_verifiers_path(repo_root)
    entry = {"id": v_id, "statement": statement, "paths": list(paths), "severity": severity}
    verifier, problem = validate_entry(entry, origin=str(target))
    if problem:
        console.print(f"[red]refused:[/] {problem}")
        sys.exit(1)

    block = render_entry(v_id, statement, list(paths), severity)
    err = _write_then_verify(target, block, v_id, repo_root, NO_HUMAN_HOME)
    if err:
        console.print(f"[red]{err}[/]")
        sys.exit(1)
    console.print(f"[green]added[/] verifier {v_id} -> {target}")


# --------------------------------------------------------------------------
# nh verifiers check
# --------------------------------------------------------------------------


@verifiers_group.command("check")
@click.option("--repo", default=".", type=click.Path(), help="Repo root to read verifiers.yaml and the git history from.")
@click.option("--against", default=None, help="Ref to diff against (default: HEAD~1).")
@click.option("--path", "paths", multiple=True, help="Treat these paths as the changed set instead of reading git.")
def check_cmd(repo: str, against: str | None, paths: tuple) -> None:
    """Config/selection gate: which verifiers would be selected for the
    changed paths, and would any of them fail closed for lack of a matching
    diff hunk. Makes NO model call and NO network call — never builds a
    verifier prompt, never constructs an `Orchestrator`. Exits 1 if the
    config failed to load, if zero verifiers are configured, or if `--against`
    could not be resolved; exits 0 otherwise."""
    repo_root = Path(repo).resolve()
    report = load_verifiers(repo_root, home=NO_HUMAN_HOME)
    for p in report.problems:
        console.print(f"[red]{p}[/]")

    changed: list[str] = list(paths)
    diff_text: str | None = None
    ref_failed = False

    if not paths:
        try:
            git_repo = GitRepo(repo_root)
        except GitError as exc:
            console.print(f"[red]git error:[/] {exc}")
            ref_failed = True
        else:
            ref_name = against or "HEAD~1"
            resolved = git_repo.resolve_commitish(ref_name)
            if resolved is None:
                console.print(f"[red]cannot resolve ref:[/] {ref_name}")
                ref_failed = True
            else:
                changed = git_repo.changed_files(resolved)
                diff_text = git_repo.diff(resolved)

    selected = select(report.verifiers, changed) if changed else []
    selected_ids = {v.id for v in selected}
    for v in report.verifiers:
        if v.id not in selected_ids:
            console.print(f"  {v.id}: [dim]not selected[/]")
            continue
        if diff_text is not None:
            hunks, _files = filter_diff(diff_text, v.paths)
            if not hunks:
                console.print(
                    f"  {v.id}: [yellow]selected, no matching hunks — "
                    "would fail closed as no_verdict[/]"
                )
                continue
        console.print(f"  {v.id}: [green]selected[/]")

    console.print(
        f"[dim]{len(selected)} of {len(report.verifiers)} verifier(s) select "
        f"for {len(changed)} changed path(s)[/]"
    )

    if report.problems or not report.verifiers or ref_failed:
        sys.exit(1)
    sys.exit(0)


# --------------------------------------------------------------------------
# nh verifiers propose
# --------------------------------------------------------------------------


class _Finding:
    __slots__ = ("label", "file", "text", "severity")

    def __init__(self, label: str | None, file: str, text: str, severity: str) -> None:
        self.label = label
        self.file = file
        self.text = text
        self.severity = severity


def _findings_from_task(t, attempts: list[dict]) -> list[_Finding]:
    attempt = next((a for a in reversed(attempts) if a.get("review_checklist")), None)
    if attempt is not None:
        blocking, _advisory = findings_from_checklist(attempt["review_checklist"])
        return [
            _Finding(label=i.label, file=i.file, text=i.comment or i.evidence, severity=i.severity)
            for i in blocking
        ]
    ctx = t.context or {}
    drafts = ctx.get("draft_review_comments") or []
    return [
        _Finding(
            label=None,
            file=d.get("file") or "",
            text=d.get("comment") or "",
            severity=d.get("severity") or "",
        )
        for d in drafts
    ]


@verifiers_group.command("propose")
@click.argument("task_id")
@click.option("--repo", default=".", type=click.Path(), help="Repo root the candidate verifiers would be written under.")
@click.option("--apply", "apply_", is_flag=True, help="Write the candidates (idempotent — already-defined ids are skipped, named).")
@click.option("--severity", default="high", help="Fallback severity for findings that carry none of critical|high|medium|low.")
def propose_cmd(task_id: str, repo: str, apply_: bool, severity: str) -> None:
    """Turn a task's already-persisted review findings into candidate
    verifier YAML. Reads `attempts.review_checklist` (blocking items only),
    falling back to `task.context['draft_review_comments']`. Skips
    `rule:`-labelled findings (a verifier's own verdict) and findings that
    don't cite a file. Without `--apply`, nothing is written."""
    repo_root = Path(repo).resolve()
    config = load_config()

    async def _go() -> None:
        async with Store(config.db_path) as store:
            t = await store.find_task(task_id)
            if not t:
                _print_no_task_matching(task_id)
                sys.exit(1)
            attempts = await store.list_attempts(t.id)
            findings = _findings_from_task(t, attempts)
            if not findings:
                console.print("[dim]no review checklist yet[/]")
                return

            existing = load_verifiers(repo_root, home=NO_HUMAN_HOME)
            existing_ids = {v.id for v in existing.verifiers}
            used_ids: set[str] = set()
            candidates: list[dict] = []

            for f in findings:
                if f.label and f.label.startswith("rule:"):
                    console.print(f"[dim]skipped[/] {f.label} — a verifier's own verdict")
                    continue
                if not f.file:
                    console.print(f"[dim]skipped[/] (no file cited) {_one_line(f.text)[:60]!r}")
                    continue

                base_slug = _slugify(f.label or f.text)
                vid = base_slug
                n = 2
                while vid in used_ids:
                    vid = f"{base_slug}-{n}"
                    n += 1
                used_ids.add(vid)

                if vid in existing_ids:
                    console.print(f"[yellow]skipped[/] {vid} — already defined")
                    continue

                sev = f.severity if f.severity in _KNOWN_SEVERITIES else severity
                statement = _one_line(f.text)[:600]
                entry = {"id": vid, "statement": statement, "paths": [f.file], "severity": sev}
                _verifier, problem = validate_entry(entry, origin="propose")
                if problem:
                    console.print(f"[red]skipped[/] {vid} — {problem}")
                    continue
                candidates.append(entry)

            if not candidates:
                console.print("[dim]nothing to propose[/]")
                return

            for c in candidates:
                console.print(render_entry(c["id"], c["statement"], c["paths"], c["severity"]))

            if not apply_:
                console.print(
                    f"[dim]nothing written — rerun with --apply to add "
                    f"{len(candidates)} verifier(s)[/]"
                )
                return

            target = repo_verifiers_path(repo_root)
            any_failed = False
            for c in candidates:
                block = render_entry(c["id"], c["statement"], c["paths"], c["severity"])
                err = _write_then_verify(target, block, c["id"], repo_root, NO_HUMAN_HOME)
                if err:
                    any_failed = True
                    console.print(f"[red]failed[/] {c['id']} — {err}")
                else:
                    console.print(f"[green]added[/] verifier {c['id']} -> {target}")
            if any_failed:
                sys.exit(1)

    asyncio.run(_go())
