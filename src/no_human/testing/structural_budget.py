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
import re
import shlex
from pathlib import Path

#: The guard file this module knows how to read, repo-relative.
GUARD_RELPATH = "tests/test_structural_budget.py"
#: The one test inside it whose failure means "a frozen entry grew".
GROWTH_TEST = "test_no_frozen_entry_has_grown"
#: The pytest node id for that test, ready to append to any pytest command.
GROWTH_NODE_ID = f"{GUARD_RELPATH}::{GROWTH_TEST}"
#: The `cause` this preflight tags its corrective round, its events, and a
#: bound-reached attempt failure with — one constant so callers/tests assert
#: on it rather than on prose that could drift out from under them.
STRUCTURAL_BUDGET_CAUSE = "structural_budget"

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


def frozen_values(repo_path: Path) -> dict[str, int]:
    """Every `FROZEN_*` entry *repo_path*'s guard freezes, as
    `{raw_key: value}` — the raw dict key exactly as written (e.g.
    `"core/orchestrator.py"` or `"pkg/mod.py:foo"`, NOT joined to the
    scanned root the way `frozen_paths` joins it), paired with its frozen
    integer.

    A raw key that appears in more than one `FROZEN_*` dict (this repo's
    own guard freezes a handful of function keys in both
    `FROZEN_FUNCTION_LINES` and `FROZEN_FUNCTION_CC`) collapses to whichever
    dict `ast.walk` visits last — this function alone cannot tell which
    dict's number a caller means for such a key. That ambiguity is exactly
    why `reanchor_frozen` refuses to rewrite a key present in more than one
    dict rather than guessing; this reader is fine with the collapse
    because its only two callers (`_structural_budget_preflight`'s
    `before_values` snapshot and `_reconcile_structural_budget_at_commit`'s
    `after` snapshot) only ever compare the two snapshots for equality —
    and even if an ambiguous key's collapsed value happens to pass that
    comparison, `reanchor_frozen` still refuses to rewrite it once it gets
    there, for the same present-in-more-than-one-dict reason.

    This is the guard's OWN on-disk state, read the same fail-open way as
    `frozen_paths`: only a plain `ast.Constant` int value is counted — a
    computed or non-literal value is skipped, not guessed at — and any
    read/parse failure (absent file, unreadable, not valid Python) yields
    `{}`, never a raise. Used to tell whether a corrective round actually
    changed a given entry THIS round (compare a `before` and `after`
    snapshot) rather than to resolve `frozen_paths`' path-vs-key mapping,
    which is why the key here is left raw.
    """
    guard = Path(repo_path) / GUARD_RELPATH
    try:
        text = guard.read_text()
        tree = ast.parse(text, filename=str(guard))
    except (OSError, SyntaxError, ValueError):
        return {}
    try:
        out: dict[str, int] = {}
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
            for key, value in zip(stmt.value.keys, stmt.value.values):
                if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
                    continue
                if not (
                    isinstance(value, ast.Constant)
                    and isinstance(value.value, int)
                    and not isinstance(value.value, bool)
                ):
                    continue
                out[key.value] = value.value
        return out
    except Exception:  # noqa: BLE001 — fail-open, matching frozen_paths
        return {}


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


#: Matches the guard's own `offenders()`-emitted growth line, e.g.
#: `"core/orchestrator.py: frozen 23825, now 23828 (+3); this budget only
#: ratchets down"` — read from the GUARD'S OWN failure text, never a second
#: independent measurement, so this can never disagree with the verdict
#: that failed the test.
#:
#: Deliberately NOT anchored to the start of a line: pytest's own failure
#: rendering prepends the exception's class name to the FIRST line of a
#: multi-line assertion message (e.g. `"E       AssertionError: mod.py:
#: frozen 6, now 8 (+2); ..."`), so a strict `^\s*` line-start anchor would
#: silently miss that first grown entry while still matching any later
#: ones. `\S+` cannot itself cross the whitespace before "mod.py", so this
#: still can't mistake "AssertionError:" (or any other prefix token) for
#: the key.
_GROWN_LINE_RE = re.compile(
    r"(?P<key>\S+): frozen (?P<frozen>\d+), now (?P<current>\d+)\b",
)


def parse_grown(output: str) -> dict[str, tuple[int, int]]:
    """Every `{key: frozen N, now M}` line in *output* — pytest's captured
    text from a run of the guard's own growth test — as
    `{key: (frozen, current)}`.

    This reads the guard's OWN emitted diagnosis, matching `offenders()`'s
    exact message shape verbatim; it is not a second, independent
    measurement that could disagree with the one that just failed the
    test. `{}` on no match (a growth failure with different wording, or no
    failure at all) — never a raise.
    """
    out: dict[str, tuple[int, int]] = {}
    for m in _GROWN_LINE_RE.finditer(output or ""):
        out[m.group("key")] = (int(m.group("frozen")), int(m.group("current")))
    return out


def reanchor_frozen(repo_path: Path, updates: dict[str, int]) -> list[str]:
    """Rewrite ONLY the numeric value token of each `updates` key inside
    *repo_path*'s guard's `FROZEN_*` dict literals, in place, line-oriented
    (locate the `ast.Constant` value node's `lineno`/`col_offset`/
    `end_col_offset`, splice in the new digits, leave every other
    character — comments, spacing, ordering, every OTHER key — untouched).

    A key is skipped (never a partial or best-guess rewrite) when it is
    absent from every `FROZEN_*` dict, appears in more than one (ambiguous:
    which one is "the" frozen value for this key), or its current value
    node is not a plain `ast.Constant` int (a computed value has no single
    literal token to replace). Returns the keys actually rewritten — a
    caller comparing this against the keys it asked for can tell exactly
    which ones landed.

    `[]` — never a raise — on an unreadable or unparseable guard, matching
    every other function in this module.
    """
    guard = Path(repo_path) / GUARD_RELPATH
    try:
        text = guard.read_text()
        tree = ast.parse(text, filename=str(guard))
    except (OSError, SyntaxError, ValueError):
        return []
    try:
        candidates: dict[str, list[ast.Constant]] = {}
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
            for key, value in zip(stmt.value.keys, stmt.value.values):
                if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
                    continue
                if key.value not in updates:
                    continue
                if not (
                    isinstance(value, ast.Constant)
                    and isinstance(value.value, int)
                    and not isinstance(value.value, bool)
                ):
                    continue
                candidates.setdefault(key.value, []).append(value)

        targets = {
            key: nodes[0]
            for key, nodes in candidates.items()
            if len(nodes) == 1
        }
        if not targets:
            return []

        lines = text.splitlines(keepends=True)
        # Rewrite bottom-to-top so an earlier splice on the same line never
        # shifts a later node's still-pending column offsets.
        ordered = sorted(
            targets.items(), key=lambda kv: (kv[1].lineno, kv[1].col_offset),
            reverse=True,
        )
        changed: list[str] = []
        for key, node in ordered:
            row = node.lineno - 1
            line = lines[row]
            new_value = str(updates[key])
            lines[row] = (
                line[: node.col_offset] + new_value + line[node.end_col_offset :]
            )
            changed.append(key)
        guard.write_text("".join(lines))
        return sorted(changed)
    except Exception:  # noqa: BLE001 — fail-open, matching every reader above
        return []
