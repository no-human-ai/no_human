"""Which failing pytest node ids name a test FUNCTION this diff added or
modified — ownership by test id, not by file.

The flaky-on-rerun and pre-existing excuses (`orchestrator._flaky_on_rerun`,
`orchestrator._newly_failing_vs_base`) classify a failing test by whether the
TREE already fails elsewhere. Neither ever asks whether the CURRENT attempt
itself wrote or edited the failing test. A brand-new test that goes red then
green can only reach the flaky excuse today, which is false by construction:
it never ran before this attempt, so there is no tree for it to be "flaky on".
A modified test that also fails on the base tree is wrongly excused as
pre-existing, when the attempt's own edit is exactly what a reviewer needs to
see.

`owned_failing_ids` answers that question, pure and read-only: given a set of
failing node ids and a before/after ref range, which ids name a function the
diff added or modified. It is advisory-shaped like `lint_evidence` and
`wiring_evidence`: two `git diff` reads plus memoized `git show` per file, no
worktree, no writes, no extra pytest run, and it never raises — any failure
resolves toward "not owned" (fail closed), so the feature degrades to today's
byte-for-byte behaviour rather than accusing a test it could not read.

Fixture ownership is DIRECT ONLY (a test naming a changed fixture as its own
parameter, or via `@pytest.mark.usefixtures(...)`) — never transitive through
the fixture's own dependencies.

Ownership is intentionally ASYMMETRIC by ecosystem. `.py` ids get the
precise per-function treatment above (`parse_node_id` + `_is_owned`'s rules
1/3/4/5: added file, changed span, or direct fixture). Everything else
(node/TAP ids — `not ok N - name` has no `.py` to run an AST over) gets
`parse_file_scoped_id` + FILE-level ownership instead: owned iff the diff
touched the id's file at all, regardless of which line. That is coarser —
it over-attributes relative to the Python path — but over-attribution only
ever BILLS an attempt (an owned id is never excused as environment or
pre-existing); it can never manufacture an excuse. So the coarser direction
is still the fail-closed one, not a hole: a node id this module cannot even
resolve to a file (no ``::``, an absolute path, one that escapes the repo
root) is simply never returned as owned, same as any other unattributable
id.
"""

from __future__ import annotations

import ast
import logging
import posixpath
import subprocess
from pathlib import Path
from typing import Any

from ..review.lint_evidence import changed_line_numbers

log = logging.getLogger(__name__)

DIFF_TIMEOUT = 30


def parse_node_id(node_id: str) -> tuple[str, tuple[str, ...], str] | None:
    """Split a pytest node id into ``(rel_path, class_chain, func_name)``.

    A trailing ``[...]`` parametrize suffix is stripped from the LAST segment
    only, so every variant of a parametrized test resolves to one function.
    Returns None when the id has no ``::``, its path is not ``.py``, is
    absolute, or normalizes outside the repo root.
    """
    if "::" not in node_id:
        return None
    parts = node_id.split("::")
    rel_path = parts[0]
    if not rel_path or not rel_path.endswith(".py"):
        return None
    if posixpath.isabs(rel_path):
        return None
    norm = posixpath.normpath(rel_path)
    if norm == ".." or norm.startswith("../"):
        return None
    rest = parts[1:]
    if not rest or not rest[-1]:
        return None
    last = rest[-1]
    bracket = last.find("[")
    if bracket != -1:
        last = last[:bracket]
    if not last:
        return None
    rest = [*rest[:-1], last]
    if any(not seg for seg in rest):
        return None
    func_name = rest[-1]
    class_chain = tuple(rest[:-1])
    return rel_path, class_chain, func_name


def parse_file_scoped_id(node_id: str) -> str | None:
    """File-level companion to `parse_node_id`, for ids whose path is NOT
    ``.py`` — node/TAP ids, where there is no AST to run over ``.mjs`` so
    ownership can only ever be FILE-level (see module docstring).

    Splits on the first ``::`` and returns the path when it is relative,
    non-empty, and normalizes inside the repo root. None otherwise: no
    ``::`` at all (a bare name carries no file to check — never ownable),
    an absolute path, a path escaping the repo root, or a ``.py`` path
    (those stay on the precise per-function `parse_node_id` path above).
    """
    if "::" not in node_id:
        return None
    rel_path = node_id.split("::", 1)[0]
    if not rel_path or rel_path.endswith(".py"):
        return None
    if posixpath.isabs(rel_path):
        return None
    norm = posixpath.normpath(rel_path)
    if norm == ".." or norm.startswith("../"):
        return None
    return rel_path


def _diff_status(
    repo_path, before_ref: str, after_ref: str, *, timeout: int,
) -> dict[str, str] | None:
    """``{repo-relative path: status letter}`` from ``git diff --name-status``.

    Rename/copy lines key by the DESTINATION path. None on total resolution
    failure (bad ref, non-git path, timeout) — distinct from ``{}``, which
    means the diff ran cleanly and touched nothing.
    """
    try:
        proc = subprocess.run(
            ["git", "diff", "--no-ext-diff", "--no-textconv", "--name-status",
             "-M", f"{before_ref}..{after_ref}"],
            cwd=repo_path, capture_output=True, text=True, errors="replace",
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    out: dict[str, str] = {}
    for line in (proc.stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) < 2 or not parts[0]:
            continue
        out[parts[-1]] = parts[0][0]
    return out


def _after_text(repo_path, after_ref: str, rel_path: str, timeout: int) -> str | None:
    """Text of ``rel_path`` at ``after_ref``, or None (absent/binary/unreadable)."""
    try:
        proc = subprocess.run(
            ["git", "show", f"{after_ref}:{rel_path}"],
            cwd=repo_path, capture_output=True, timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0 or b"\x00" in proc.stdout:
        return None
    return proc.stdout.decode("utf-8", errors="replace")


def _decorator_name(dec: ast.expr) -> str:
    """Best-effort dotted decorator name, e.g. ``pytest.fixture`` or ``fixture``."""
    target = dec.func if isinstance(dec, ast.Call) else dec
    parts: list[str] = []
    while isinstance(target, ast.Attribute):
        parts.append(target.attr)
        target = target.value
    if isinstance(target, ast.Name):
        parts.append(target.id)
    return ".".join(reversed(parts))


def _is_fixture_def(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return any(
        _decorator_name(dec).rsplit(".", 1)[-1] == "fixture"
        for dec in node.decorator_list
    )


def _direct_fixture_names(node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Direct fixture names: parameters (minus ``self``) union
    ``@pytest.mark.usefixtures(...)`` string args. Never transitive."""
    names: set[str] = set()
    args = node.args
    for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs):
        if arg.arg != "self":
            names.add(arg.arg)
    for dec in node.decorator_list:
        if not isinstance(dec, ast.Call):
            continue
        if _decorator_name(dec).rsplit(".", 1)[-1] != "usefixtures":
            continue
        for call_arg in dec.args:
            if isinstance(call_arg, ast.Constant) and isinstance(call_arg.value, str):
                names.add(call_arg.value)
    return names


_FuncIndex = tuple[
    dict[tuple[tuple[str, ...], str], "ast.FunctionDef | ast.AsyncFunctionDef"],
    dict[tuple[tuple[str, ...], str], tuple[int, int]],
]


def _index_functions(text: str) -> _FuncIndex | None:
    """Index every function def (module top level and inside classes) by
    ``(class_chain, name)``. Returns ``(nodes, spans)``:

    * ``nodes`` — the LAST definition with that name (redefinition shadows).
    * ``spans`` — ``(start_line, end_line)`` unioned across ALL definitions
      with that name; start includes decorator lines, so a changed
      ``@pytest.mark.parametrize(...)`` list still owns the function.

    None on SyntaxError or any other parse failure.
    """
    try:
        tree = ast.parse(text)
    except Exception:  # noqa: BLE001 — advisory, never raise
        return None
    nodes: dict[tuple[tuple[str, ...], str], Any] = {}
    spans: dict[tuple[tuple[str, ...], str], tuple[int, int]] = {}

    def _visit(body, class_chain: tuple[str, ...]) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                start = node.lineno
                for dec in node.decorator_list:
                    start = min(start, dec.lineno)
                end = node.end_lineno if node.end_lineno is not None else node.lineno
                key = (class_chain, node.name)
                nodes[key] = node
                if key in spans:
                    s0, e0 = spans[key]
                    spans[key] = (min(s0, start), max(e0, end))
                else:
                    spans[key] = (start, end)
            elif isinstance(node, ast.ClassDef):
                _visit(node.body, (*class_chain, node.name))

    try:
        _visit(tree.body, ())
    except Exception:  # noqa: BLE001 — advisory, never raise
        return None
    return nodes, spans


def _conftest_chain(rel_path: str) -> list[str]:
    """Candidate fixture-definition sites: the test's own file, then each
    ``conftest.py`` from its directory up to the repo root."""
    parts = rel_path.split("/")[:-1]
    candidates = [rel_path]
    for i in range(len(parts), -1, -1):
        prefix = parts[:i]
        candidates.append("/".join((*prefix, "conftest.py")) if prefix else "conftest.py")
    return candidates


def _span_intersects(span: tuple[int, int] | None, lines: set[int]) -> bool:
    if not span or not lines:
        return False
    start, end = span
    return any(start <= ln <= end for ln in lines)


def _is_owned_file_scoped(node_id: str, status: dict[str, str]) -> bool:
    """Non-Python (node/TAP) ownership — the coarse, file-level half of the
    asymmetric split described in the module docstring: owned iff the diff
    touched the id's file at all (added or modified), regardless of which
    line. A failing id cannot live in a file the diff DELETED. No AST for
    `.mjs`, so there is no span/fixture check left to narrow it further —
    unlike `_is_owned` below, which is per-function for `.py`.
    """
    rel_path = parse_file_scoped_id(node_id)
    if rel_path is None:
        return False
    file_status = status.get(rel_path)
    if file_status is None or file_status == "D":
        return False
    return True


def _is_owned(
    node_id: str,
    status: dict[str, str],
    changed: dict[str, set[int]],
    index_of,
) -> bool:
    parsed = parse_node_id(node_id)
    if parsed is None:
        return _is_owned_file_scoped(node_id, status)
    rel_path, class_chain, func_name = parsed
    file_status = status.get(rel_path)

    # Rule 1: file added -> owned outright, content irrelevant.
    if file_status == "A":
        return True
    # Rule (D): a failing id cannot live in a deleted file.
    if file_status == "D":
        return False

    key = (class_chain, func_name)

    # Rule 3: function span intersects this file's changed lines.
    if file_status is not None:
        idx = index_of(rel_path)
        if idx is not None:
            _, spans = idx
            if _span_intersects(spans.get(key), changed.get(rel_path) or set()):
                return True

    # Rule 4: direct fixture ownership only (never transitive). The TEST's own
    # file need not itself be in the diff — only the fixture-definition file
    # has to be (checked below) — so this lookup is unconditional on
    # file_status, unlike rule 3 above.
    node = None
    idx = index_of(rel_path)
    if idx is not None:
        nodes, _ = idx
        node = nodes.get(key)
    if node is None:
        return False
    fixture_names = _direct_fixture_names(node)
    if not fixture_names:
        return False
    for candidate in _conftest_chain(rel_path):
        cand_status = status.get(candidate)
        if cand_status is None:
            continue  # only consider files that are themselves in the diff
        cand_idx = index_of(candidate)
        if cand_idx is None:
            continue
        cand_nodes, cand_spans = cand_idx
        cand_lines = changed.get(candidate) or set()
        for (c_chain, c_name), c_node in cand_nodes.items():
            if c_name not in fixture_names or not _is_fixture_def(c_node):
                continue
            if cand_status == "A":
                return True
            if _span_intersects(cand_spans.get((c_chain, c_name)), cand_lines):
                return True

    # Rule 5: otherwise, not owned.
    return False


def owned_failing_ids(
    repo_path, before_ref: str, after_ref: str, node_ids, *,
    cwd: str | None = None, timeout: int = DIFF_TIMEOUT,
) -> list[str]:
    """Subset of ``node_ids`` (input order preserved) naming a test function
    the diff between ``before_ref`` and ``after_ref`` added or modified.

    ``cwd`` is the directory the test runner ran in, RELATIVE to the repo
    root (``None``/``""`` = the root). pytest node ids are rootdir-relative
    while git's diff is repo-root-relative; when the runner is routed into a
    subdirectory every id would otherwise miss the diff and read as "not
    owned" — the guard silently inert, or worse, a cwd-relative id colliding
    with an identically named root path (review finding on PR #570). The
    prefix is applied for the LOOKUP only; the ids returned are the caller's.

    Read-only, idempotent, never raises. Any total resolution failure (bad
    ref, non-git path, timeout) returns ``[]`` — the feature goes inert
    rather than accusatory. Any per-id resolution error resolves toward "not
    owned" unless the file itself was added, in which case it is owned
    regardless of content (fail closed).

    Node/TAP ids (see module docstring) go through the same fail-closed
    contract as `.py` ids: an id `parse_file_scoped_id` cannot resolve to an
    in-repo relative path (no ``::``, absolute, escapes the repo root) is
    never returned here — it can only ever be BILLED to the attempt by the
    caller, never excused as environment or pre-existing.
    """
    node_ids = list(node_ids)
    if not node_ids:
        return []
    repo_path = Path(repo_path)

    status = _diff_status(repo_path, before_ref, after_ref, timeout=timeout)
    if status is None:
        log.warning(
            "ownership: diff status unavailable for %s..%s in %s; nothing owned",
            before_ref, after_ref, repo_path,
        )
        return []

    try:
        changed = changed_line_numbers(repo_path, before_ref, after_ref, timeout=timeout)
    except Exception:  # noqa: BLE001 — advisory, never raise
        log.warning("ownership: changed-line lookup failed", exc_info=True)
        changed = {}

    text_cache: dict[str, str | None] = {}
    index_cache: dict[str, _FuncIndex | None] = {}

    def _text(rel_path: str) -> str | None:
        if rel_path not in text_cache:
            text_cache[rel_path] = _after_text(repo_path, after_ref, rel_path, timeout)
        return text_cache[rel_path]

    def _index(rel_path: str) -> _FuncIndex | None:
        if rel_path not in index_cache:
            text = _text(rel_path)
            index_cache[rel_path] = _index_functions(text) if text is not None else None
        return index_cache[rel_path]

    prefix = (cwd or "").strip("/")

    def _lookup_id(node_id: str) -> str:
        if not prefix or "::" not in node_id:
            return node_id
        path, rest = node_id.split("::", 1)
        return f"{prefix}/{path}::{rest}"

    owned: list[str] = []
    for node_id in node_ids:
        lookup = _lookup_id(node_id)
        try:
            if _is_owned(lookup, status, changed, _index):
                owned.append(node_id)
        except Exception:  # noqa: BLE001 — fail closed, never raise
            log.warning("ownership: resolution failed for %r", node_id, exc_info=True)
            parsed = parse_node_id(lookup)
            if parsed is not None and status.get(parsed[0]) == "A":
                owned.append(node_id)
                continue
            file_scoped = parse_file_scoped_id(lookup)
            if file_scoped is not None and status.get(file_scoped) == "A":
                owned.append(node_id)
    return owned
