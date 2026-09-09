"""Repo-agnostic reader for the doc-citation drift check a target repo may
ship at `scripts/reanchor_citations.py` — a script that verifies every line
number/symbol a doc (`docs/security.md`, `docs/eval.md`,
`docs/KNOWN_ISSUES.md`, …) cites, per `tests/test_readme_claims.py`'s own
`CITATION_TABLE`, still points at what it claims to. This module never
imports either file as Python — `cited_source_files` only parses
`tests/test_readme_claims.py` with `ast`, same doctrine as
`structural_budget.py`'s `frozen_paths`/`repro_gate.py`'s
`manifest_problem`/`read_manifest`: a repo without this convention, or with
one this parser cannot make sense of, costs nothing beyond one best-effort
read. `run_check` DOES shell out — but only to the script's own read-only
`--check` mode, and only once a diff is already known to have touched a
cited file (see `Orchestrator._citation_drift_preflight`, which is the sole
caller).

WHY THIS EXISTS: a diff that shifts a cited line still passes review — the
reviewer is not this gate — and only fails later, in the attempt's
full-suite run, on `tests/test_readme_claims.py`'s own citation tests. That
is a whole extra attempt spent discovering what is mechanically a one-shot
`--apply` re-anchor (2026-09-09: task 7a9e7998 attempt 1, task 302012e3
round 2). This module is the zero-LLM-spend half of the fix — which source
files carry a citation, whether THIS diff touched one, and the classified
outcome of the script's own `--check` — consumed by
`Orchestrator._citation_drift_preflight` in `core/orchestrator.py`.

FAIL-OPEN, ALWAYS: absent, unreadable, or unparseable table ⇒ `set()`,
never a raise. Every repo without this convention — the overwhelming
majority of target repos — must pay nothing for it.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

#: The script this module knows how to drive, repo-relative.
SCRIPT_RELPATH = "scripts/reanchor_citations.py"
#: The file whose `CITATION_TABLE` names which source files carry citations.
TABLE_RELPATH = "tests/test_readme_claims.py"

_CITATION_TABLE_NAME = "CITATION_TABLE"


def script_path(repo_path: Path) -> Path | None:
    """*repo_path*'s citation-reanchor script, or `None` when the repo does
    not ship one — the preflight this module backs must cost nothing for
    the overwhelming majority of target repos that carry no such tooling.
    """
    candidate = Path(repo_path) / SCRIPT_RELPATH
    return candidate if candidate.is_file() else None


def _resolve_one(repo_path: Path, spec: str) -> str | None:
    """*spec* (one `CITATION_TABLE` resolve-path column value) resolved to a
    repo-relative POSIX path that actually exists under *repo_path* —
    mirroring `tests/test_readme_claims.py`'s own `_resolve_source` order
    (a slashed path tried against the repo root, `src/<pkg>/`, and `src/`;
    a bare basename searched for uniquely under `src/`) but against the
    TARGET repo rather than this one. `None` when nothing resolves, or more
    than one candidate matches a bare basename — an unresolvable row is
    simply skipped, never raised.
    """
    repo_path = Path(repo_path)
    src = repo_path / "src"
    if "/" in spec:
        candidates = [repo_path / spec]
        if src.is_dir():
            for pkg in sorted(p for p in src.iterdir() if p.is_dir()):
                candidates.append(pkg / spec)
            candidates.append(src / spec)
        for candidate in candidates:
            if candidate.is_file():
                try:
                    return candidate.resolve().relative_to(
                        repo_path.resolve()).as_posix()
                except ValueError:
                    continue
        return None
    if not src.is_dir():
        return None
    hits = sorted(src.rglob(spec))
    if len(hits) != 1:
        return None
    try:
        return hits[0].resolve().relative_to(repo_path.resolve()).as_posix()
    except ValueError:
        return None


def cited_source_files(repo_path: Path) -> set[str]:
    """Every repo-relative source path *repo_path*'s `TABLE_RELPATH` cites —
    read by a cheap `ast.parse` of its `CITATION_TABLE` literal's third
    column (the resolve-path spec), never an import. `set()` — never a
    raise — when the table file is absent, unreadable, not valid Python, or
    carries no recognisable `CITATION_TABLE = (...)` assignment; a repo
    without this convention must pay nothing for it.
    """
    table = Path(repo_path) / TABLE_RELPATH
    try:
        text = table.read_text(encoding="utf-8")
        tree = ast.parse(text, filename=str(table))
    except (OSError, SyntaxError, ValueError, UnicodeDecodeError):
        return set()
    try:
        specs: set[str] = set()
        for stmt in ast.walk(tree):
            if not isinstance(stmt, ast.Assign):
                continue
            if not any(
                isinstance(t, ast.Name) and t.id == _CITATION_TABLE_NAME
                for t in stmt.targets
            ):
                continue
            if not isinstance(stmt.value, (ast.Tuple, ast.List)):
                continue
            for row in stmt.value.elts:
                if not isinstance(row, (ast.Tuple, ast.List)):
                    continue
                if len(row.elts) < 3:
                    continue
                col = row.elts[2]
                if isinstance(col, ast.Constant) and isinstance(col.value, str):
                    specs.add(col.value)
        resolved: set[str] = set()
        for spec in specs:
            hit = _resolve_one(Path(repo_path), spec)
            if hit is not None:
                resolved.add(hit)
        return resolved
    except Exception:  # noqa: BLE001 — fail-open, matching structural_budget.py
        return set()


def touched_cited(cited: set[str], changed: Iterable[str]) -> list[str]:
    """Sorted intersection of *cited* with *changed* — the cited path(s)
    THIS diff touched, or `[]` when it touched none. The common case (any
    repo without this convention, or a diff that never comes near a cited
    file) is the empty list, and costs nothing beyond this one set
    intersection.
    """
    return sorted(cited.intersection(changed))


@dataclass(frozen=True)
class CitationCheck:
    """The classified outcome of one `--check` run of *SCRIPT_RELPATH*.

    Exactly one of the following holds, in this precedence — error, then
    unfixable, then drifts, then clean:

    * ``error`` set: the check could not run at all (script missing/not
      executable, timed out, or crashed/raised — including its own
      `tests/test_readme_claims.py` failing to import) or exited with a
      code this reader does not recognise as either "clean" or a
      `DRIFT:`/`FAIL:`-marked verdict. Not auto-fixable — spending a round
      would just fail again.
    * ``unfixable`` non-empty: the script itself reports one or more
      citations no re-anchor can repair (the `FAIL: ` line(s) from its own
      `--check` output). Not auto-fixable either.
    * ``drifts`` non-empty (and `unfixable` empty): every reported citation
      is the ordinary, mechanically-repairable kind (the `DRIFT: ` line(s)).
      This is the one case worth spending a round on.
    * All three empty/`None`: clean — nothing to do.
    """

    returncode: int | None
    stdout: str
    drifts: list[str]
    unfixable: list[str]
    error: str | None


def _combined_output(proc: "subprocess.CompletedProcess[str]") -> str:
    parts = [p.strip() for p in (proc.stdout, proc.stderr) if p and p.strip()]
    return "\n".join(parts)


def run_check(repo_path: Path, *, timeout: float = 120.0) -> CitationCheck:
    """Run *repo_path*'s `SCRIPT_RELPATH` in `--check` mode — read-only,
    never `--apply` — and classify the result per `CitationCheck`'s own
    precedence.

    Classification is driven by the script's stdout line prefixes
    (`"DRIFT: "` for a fixable row, `"FAIL: "` for an unfixable one — the
    real script's own grammar), corroborated by exit code: `0` is always
    clean; `2` is reserved for the script's own checker-load failure (its
    `tests/test_readme_claims.py` failed to import) and is always `error`,
    even though that failure line also happens to start with `"FAIL: "`;
    every other exit code is classified purely by which line prefixes are
    present, so a `1` with no recognisable marker line at all (an
    unanticipated crash) still falls to `error` rather than being silently
    read as "nothing to do".
    """
    script = Path(repo_path) / SCRIPT_RELPATH
    try:
        proc = subprocess.run(
            [sys.executable, str(script), "--check"],
            cwd=repo_path, capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return CitationCheck(None, "", [], [], str(exc))

    stdout = proc.stdout or ""
    lines = stdout.splitlines()
    if proc.returncode == 0:
        return CitationCheck(0, stdout, [], [], None)

    fail_lines = [line for line in lines if line.startswith("FAIL: ")]
    if proc.returncode == 2:
        detail = "\n".join(fail_lines) or _combined_output(proc) or (
            f"{SCRIPT_RELPATH} --check exited {proc.returncode}")
        return CitationCheck(proc.returncode, stdout, [], [], detail)

    drift_lines = [line for line in lines if line.startswith("DRIFT: ")]
    if fail_lines:
        return CitationCheck(proc.returncode, stdout, drift_lines, fail_lines, None)
    if drift_lines:
        return CitationCheck(proc.returncode, stdout, drift_lines, [], None)
    detail = _combined_output(proc) or (
        f"{SCRIPT_RELPATH} --check exited {proc.returncode} with no "
        "recognised output")
    return CitationCheck(proc.returncode, stdout, [], [], detail)
