"""Repo-agnostic reader for the structural-budget ratchet a target repo may
ship at `tests/test_structural_budget.py` — an AST-based scanner that
freezes today's known "offenders" (functions/files over a size/complexity
threshold) by EXACT value, so the ratchet can only move down. This module
never imports that file as Python — it only parses it with `ast`, same
doctrine as `repro_gate.py`'s `manifest_problem`/`read_manifest`: a repo
without the guard, or with one this parser cannot make sense of, costs
nothing beyond one best-effort read.

WHY THIS EXISTS: a diff that grows a FROZEN entry (a function/file the
ratchet already caps) still passes review — the reviewer is not this gate
— and only fails later, in the attempt's full-suite run, on the guard's own
`test_no_frozen_entry_has_grown`. That is a whole extra attempt spent
discovering what is mechanically a one-line budget re-anchor (observed
2026-09-03 on tasks bf645f3a, c5ae50d8, c5b24230). This module is the
zero-LLM-spend half of the fix — which paths are frozen, whether THIS diff
touched any of them, and the bounded pytest invocation that re-runs just
the guard's own growth test — consumed by
`Orchestrator._structural_budget_preflight` in `core/orchestrator.py`.

FAIL-OPEN, ALWAYS: absent, unreadable, or unparseable guard ⇒ `set()`,
never a raise. Every repo without this guard — the overwhelming majority of
target repos — must pay nothing for it.
"""

from __future__ import annotations

import ast
import shlex
from pathlib import Path

#: The guard file this module knows how to read, repo-relative.
GUARD_RELPATH = "tests/test_structural_budget.py"
#: The one test inside it whose failure means "a frozen entry grew".
GROWTH_TEST = "test_no_frozen_entry_has_grown"
#: The pytest node id for that test, ready to append to any pytest command.
GROWTH_NODE_ID = f"{GUARD_RELPATH}::{GROWTH_TEST}"

# The dict names the guard freezes today's offenders under. Matched
# generically — any module-level name starting with this prefix and bound
# to a dict literal — so a renamed or added frozen list is picked up
# without an edit here.
_FROZEN_PREFIX = "FROZEN_"

# Fallback scanned-root prefix when the guard's own `SRC = ...` assignment
# is absent or not in the recognised shape — an approximate prefix beats a
# crash, and this repo's own guard nests one level below `src/`.
_DEFAULT_ROOT = "src"


def _binop_path_segments(node: ast.AST) -> list[str] | None:
    """Walk a `BASE / "a" / "b"` chain left-to-right, collecting the string
    literals in order. `None` if the chain holds anything this simple walk
    does not understand (a call, an f-string, a subscript) — it only needs
    to read the shape the guard's own `SRC = REPO_ROOT / "src" / "no_human"`
    line uses.
    """
    if isinstance(node, ast.Name):
        return []
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        left = _binop_path_segments(node.left)
        if left is None:
            return None
        right = node.right
        if isinstance(right, ast.Constant) and isinstance(right.value, str):
            return [*left, right.value]
        return None
    return None


def _scanned_root(tree: ast.Module) -> str:
    """The repo-relative, POSIX, no-trailing-slash prefix the guard's own
    scanner walks — recovered from its `SRC = ...` assignment. Falls back
    to `_DEFAULT_ROOT` when that assignment is missing or not in the
    recognised `BinOp` shape.
    """
    for stmt in ast.walk(tree):
        if not isinstance(stmt, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "SRC" for t in stmt.targets):
            continue
        segments = _binop_path_segments(stmt.value)
        if segments is not None:
            return "/".join(segments)
    return _DEFAULT_ROOT


def frozen_paths(repo_path: Path) -> set[str]:
    """Every path *repo_path*'s `tests/test_structural_budget.py` freezes a
    budget for, repo-relative and POSIX — the file part of each `FROZEN_*`
    key (`path:qualname` for a function/method entry, a bare path for a
    file entry), joined to the guard's own scanned root.

    `set()` — never a raise — when the guard file is absent, unreadable, or
    not valid Python. This repo's own layout is not the only shape a target
    repo can have, and a repo WITHOUT the guard must cost nothing.
    """
    guard = Path(repo_path) / GUARD_RELPATH
    try:
        text = guard.read_text(encoding="utf-8")
        tree = ast.parse(text, filename=str(guard))
    except (OSError, SyntaxError, ValueError):
        return set()
    try:
        root = _scanned_root(tree)
        out: set[str] = set()
        for stmt in ast.walk(tree):
            if not isinstance(stmt, ast.Assign):
                continue
            if not any(
                isinstance(t, ast.Name) and t.id.startswith(_FROZEN_PREFIX)
                for t in stmt.targets
            ):
                continue
            if not isinstance(stmt.value, ast.Dict):
                continue
            for key in stmt.value.keys:
                if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
                    continue
                path_part = key.value.split(":", 1)[0]
                out.add(f"{root}/{path_part}" if root else path_part)
        return out
    except Exception:  # noqa: BLE001 — fail-open, matching repro_gate.py
        return set()


def touched_frozen(frozen: set[str], changed_files: list[str]) -> list[str]:
    """Sorted intersection of *frozen* with *changed_files* — the frozen
    path(s) THIS diff touched, or `[]` when it touched none. The common
    case (any repo without the guard, or one the diff never came near) is
    the empty list, and costs nothing beyond this one set intersection.
    """
    return sorted(frozen.intersection(changed_files))


def bounded_growth_command(test_cmd: str | None) -> str | None:
    """*test_cmd* narrowed to just the guard's own growth test, or `None`
    when *test_cmd* is absent or is not a pytest invocation. There is no
    generic way to select one node id out of an `npm test`/`mvn test`
    command, and the guard itself is pytest-only, so a non-pytest project
    has nothing to bound this to.
    """
    if not test_cmd or "pytest" not in test_cmd.lower():
        return None
    return f"{test_cmd} {shlex.quote(GROWTH_NODE_ID)}"


def scanned_root(repo_path: Path) -> str | None:
    """Public counterpart of `frozen_paths`'s root discovery: the
    repo-relative, POSIX, no-trailing-slash prefix *repo_path*'s guard file
    scans, or `None` when the guard is absent, unreadable, or not valid
    Python.

    `touched_frozen`'s frozen-path intersection is structurally blind to two
    cases: a brand-new offender (never yet in any `FROZEN_*` dict, so it is
    in no intersection to find) and a stale frozen entry whose OWN path may
    not be among the files this diff changed at all (a sibling function's
    growth pushed a total over, or the entry was hand-edited). Both are
    still caught by re-running the guard's own tests — but only once the
    diff is known to have come near the guard's scanned root at all, which
    is what this function (with `touches_scanned_root`) exists to answer.
    `None` propagates fail-open exactly like `frozen_paths`' `set()`: a repo
    without the guard, or one this parser cannot make sense of, must cost
    nothing.
    """
    guard = Path(repo_path) / GUARD_RELPATH
    try:
        text = guard.read_text(encoding="utf-8")
        tree = ast.parse(text, filename=str(guard))
    except (OSError, SyntaxError, ValueError):
        return None
    try:
        return _scanned_root(tree)
    except Exception:  # noqa: BLE001 — fail-open, matching frozen_paths
        return None


def touches_scanned_root(root: str | None, changed_files: list[str]) -> list[str]:
    """Sorted `.py` files in *changed_files* that fall under *root* — the
    changed files a NEW offender or a STALE frozen entry could be hiding in,
    neither of which `touched_frozen`'s frozen-path intersection can see (see
    `scanned_root`). `[]` when *root* is `None` (no readable guard) — the
    same fail-open, zero-cost doctrine as the rest of this module.
    """
    if root is None:
        return []
    prefix = f"{root}/" if root else ""
    return sorted(
        f for f in changed_files
        if f.endswith(".py") and (not prefix or f.startswith(prefix))
    )


def bounded_guard_command(test_cmd: str | None) -> str | None:
    """*test_cmd* narrowed to the guard's own file — every test in it, not
    just `GROWTH_NODE_ID` — since a NEW-offender or STALE-entry failure can
    surface in a different test of the same file than the growth one does.
    `None` under the same conditions as `bounded_growth_command`: *test_cmd*
    absent or not a pytest invocation.
    """
    if not test_cmd or "pytest" not in test_cmd.lower():
        return None
    return f"{test_cmd} {shlex.quote(GUARD_RELPATH)}"


def invalidate_guard_cache(repo_path: Path) -> None:
    """Drop any cached bytecode for the guard file itself, repo-relative
    `tests/test_structural_budget.py`.

    `_structural_budget_preflight`'s corrective round rewrites this file
    with a same-length digit swap (a frozen count's value, nothing else)
    moments before re-running pytest against it — sometimes inside the same
    wall-clock second as the run that first compiled it. CPython's default
    source-cache validation stores the source mtime truncated to whole
    seconds in the `.pyc` header, so a same-second, same-size rewrite is
    indistinguishable from no change at all and the STALE compile (still
    reflecting the pre-round, still-frozen value) is served — reproduced
    directly against this gate's own re-run, not a hypothetical: roughly
    half of back-to-back runs read the guard's old content. Deleting the
    cached `.pyc`(s) removes the ambiguity outright, independent of clock
    resolution or which interpreter the target repo's test command runs
    under (matched by filename prefix, not a computed cache path). A
    missing/unremovable cache directory is not an error — compiling fresh
    is always correct, just marginally slower.
    """
    guard = Path(repo_path) / GUARD_RELPATH
    cache_dir = guard.parent / "__pycache__"
    try:
        for pyc in cache_dir.glob(f"{guard.stem}.*.pyc"):
            pyc.unlink(missing_ok=True)
    except OSError:
        pass
