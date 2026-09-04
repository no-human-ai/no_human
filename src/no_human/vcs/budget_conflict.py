"""Mechanical resolution for a PR conflict confined to the structural budget
ratchet's `FROZEN_*` allow-lists in `tests/test_structural_budget.py`.

Bugfix context: two branches that each grow the SAME frozen function/file
(and re-measure/bump the same `FROZEN_*` entry to their own honestly-measured
value) conflict on that entry when one lands on top of the other — even
though neither side made a hand decision the other disagrees with. The
correct value after the merge is neither side's number: it is what the
scanner in `tests/test_structural_budget.py` measures on the MERGED tree.
Feeding that value back into the conflicting entry, and running the budget
test itself as proof, resolves the conflict exactly as truthfully as either
branch's own CI run did — no coder round needed.

This module is the leaf of the two mechanical-resolution modules: it owns
the conflict-marker parser (`parse_conflict_hunks`) that `derived_conflict.py`
also uses for `EXPORT_CLASSIFICATION.txt` hunks, so both share one
implementation without a circular import (`derived_conflict` imports these
names FROM here; this module never imports `derived_conflict`).

Fail-closed throughout, same doctrine as `derived_conflict.py`: any hunk
shape this module cannot prove is "both sides measuring the same frozen
dict, differing only in the numeric value" refuses (returns `False`/`None`),
which sends the conflict back to a coder round unchanged.
"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .approve_merge import _cap, _sh

#: Conflict-marker prefixes git writes into a merged blob. Hosted here (not
#: in `derived_conflict.py`) so both modules can share one parser without a
#: circular import: `derived_conflict` imports these names FROM this module,
#: never the reverse.
_HUNK_START = "<<<<<<<"
_HUNK_BASE = "|||||||"
_HUNK_SEP = "======="
_HUNK_END = ">>>>>>>"

BUDGET_TEST_PATH = "tests/test_structural_budget.py"

_FROZEN_DICT_RE = re.compile(r"^(?P<name>FROZEN_[A-Z_]+)\s*=\s*\{")
_FROZEN_END_RE = re.compile(r"^\}")
_ENTRY_RE = re.compile(r'^(?P<indent>\s*)"(?P<key>[^"]+)":\s*(?P<value>\d+),\s*$')

#: Maps each frozen dict's name to its positional index in the tuple
#: `scan_tree()` (defined inside `tests/test_structural_budget.py` itself)
#: returns: `(function_lines, function_cc, file_lines, total_files,
#: total_functions)`.
_DICT_TO_MEASURE = {
    "FROZEN_FUNCTION_LINES": 0,
    "FROZEN_FUNCTION_CC": 1,
    "FROZEN_FILE_LINES": 2,
}

_BUDGET_TEST_TIMEOUT_S = 300


def parse_conflict_hunks(merged_text: str) -> list[list[list[str]]] | None:
    """Split *merged_text* into a list of hunks, each a list of 2 or 3
    sections (ours/theirs, or ours/base/theirs under diff3), each section a
    list of raw lines (no trailing newline, markers excluded).

    Returns `None` (refuse) on any shape this cannot parse cleanly: a marker
    line outside a hunk, a nested/unterminated hunk, a hunk with other than
    2 or 3 sections, or an empty section. Returns `[]` (not `None`) when the
    text is well-formed but contains no conflict markers at all — callers
    must treat an empty list as "no hunks", not as a parse failure.
    """
    lines = merged_text.splitlines()
    hunks: list[list[list[str]]] = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if not line.startswith(_HUNK_START):
            if line.startswith((_HUNK_BASE, _HUNK_SEP, _HUNK_END)):
                return None  # a marker outside a hunk — unparseable
            i += 1
            continue
        sections: list[list[str]] = [[]]
        i += 1
        closed = False
        while i < n:
            cur = lines[i]
            if cur.startswith(_HUNK_START):
                return None  # nested start — unparseable
            i += 1
            if cur.startswith((_HUNK_BASE, _HUNK_SEP)):
                sections.append([])
                continue
            if cur.startswith(_HUNK_END):
                closed = True
                break
            sections[-1].append(cur)
        if not closed or len(sections) not in (2, 3):
            return None
        if any(not section for section in sections):
            return None
        hunks.append(sections)
    return hunks


@dataclass
class _Hunk:
    dict_name: str
    sections: list[list[str]]
    start: int
    end: int


def _walk_hunks(merged_text: str) -> list[_Hunk] | None:
    """Reuse `parse_conflict_hunks` for well-formedness, then a positional
    walk to find each hunk's line span and its enclosing `FROZEN_*` dict
    (the nearest preceding column-0 `FROZEN_X = {` not yet closed by a
    column-0 `}`). Refuses (returns `None`) if any hunk has no enclosing
    frozen dict, or if the two walks disagree on hunk count (defensive:
    should be impossible given both walks share the same marker rules).
    """
    hunks = parse_conflict_hunks(merged_text)
    if not hunks:
        return None
    lines = merged_text.splitlines()
    walked: list[_Hunk] = []
    current_dict: str | None = None
    i, n = 0, len(lines)
    hunk_idx = 0
    while i < n:
        line = lines[i]
        if line.startswith(_HUNK_START):
            start = i
            i += 1
            while i < n and not lines[i].startswith(_HUNK_END):
                i += 1
            if i >= n or hunk_idx >= len(hunks):
                return None
            end = i
            if current_dict is None:
                return None
            walked.append(_Hunk(dict_name=current_dict, sections=hunks[hunk_idx],
                                 start=start, end=end))
            hunk_idx += 1
            i += 1
            continue
        m = _FROZEN_DICT_RE.match(line)
        if m:
            current_dict = m.group("name")
        elif _FROZEN_END_RE.match(line):
            current_dict = None
        i += 1
    if hunk_idx != len(hunks):
        return None
    return walked


def hunk_dict_names(merged_text: str) -> list[str] | None:
    """The enclosing `FROZEN_*` dict name for every hunk, in order, or
    `None` if the text does not parse cleanly."""
    walked = _walk_hunks(merged_text)
    if walked is None:
        return None
    return [h.dict_name for h in walked]


def hunks_numeric_only(merged_text: str) -> bool:
    """True iff every conflict hunk in *merged_text* sits inside a
    `FROZEN_*` dict and every section of every hunk contains only frozen
    ``"key": <int>,`` entry lines (comments/blank lines allowed and ignored)
    whose KEY SET is identical across every section of that hunk — i.e. the
    hunk's only disagreement is the numeric value(s), never a key added,
    removed, or renamed, and never a non-entry code line."""
    walked = _walk_hunks(merged_text)
    if not walked:
        return False
    for h in walked:
        parsed_keys: list[list[str]] = []
        for section in h.sections:
            keys: list[str] = []
            for line in section:
                s = line.strip()
                if not s or s.startswith("#"):
                    continue
                m = _ENTRY_RE.match(line)
                if not m:
                    return False
                keys.append(m.group("key"))
            if not keys:
                return False
            parsed_keys.append(keys)
        first = parsed_keys[0]
        if any(other != first for other in parsed_keys[1:]):
            return False
    return True


def resolve_hunks(merged_text: str, measured: dict) -> tuple[str, list[str]] | None:
    """Rewrite every numeric-only hunk in *merged_text* to the value(s) in
    *measured* (keyed by dict name -> {entry key: measured value}), keeping
    "ours"'s comment lines plus any comment line unique to "theirs".
    Returns `(resolved_text, notes)` where each note is
    ``"<dict>:<key> -> <value>"``, or `None` if the shape is not numeric-only
    or a conflicting key is not present in *measured* (the entry no longer
    offends, or the scanner does not know it — never guess, refuse)."""
    if not hunks_numeric_only(merged_text):
        return None
    walked = _walk_hunks(merged_text)
    if not walked:
        return None
    resolved_bodies: list[list[str]] = []
    notes: list[str] = []
    for h in walked:
        ours = h.sections[0]
        theirs = h.sections[-1]
        ours_set = set(ours)
        theirs_only_comments = [ln for ln in theirs
                                 if ln.strip().startswith("#") and ln not in ours_set]
        body: list[str] = [f"{ln}\n" for ln in theirs_only_comments]
        bucket = measured.get(h.dict_name) or {}
        for ln in ours:
            s = ln.strip()
            if not s or s.startswith("#"):
                body.append(f"{ln}\n")
                continue
            m = _ENTRY_RE.match(ln)
            key = m.group("key")
            if key not in bucket:
                return None
            value = bucket[key]
            body.append(f'{m.group("indent")}"{key}": {value},\n')
            notes.append(f"{h.dict_name}:{key} -> {value}")
        resolved_bodies.append(body)
    out_lines = list(merged_text.splitlines(keepends=True))
    for h, body in reversed(list(zip(walked, resolved_bodies))):
        out_lines[h.start:h.end + 1] = body
    return "".join(out_lines), notes


def _load_module_from_path(name: str, path: Path):
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        # `tests/test_structural_budget.py` decorates `Entry` with
        # `@dataclass(frozen=True)`, and the dataclass machinery resolves
        # `sys.modules[cls.__module__]` while processing it — without
        # registering the module under its load name FIRST, that lookup
        # hits `None` and raises, silently failing this whole load closed
        # (caught below) every time there is no
        # `src/no_human/testing/structural_budget.py` yet to prefer instead.
        # Removed again once exec finishes (success or not): the caller only
        # needs the returned module object, not a lingering registration.
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        finally:
            sys.modules.pop(name, None)
        return module
    except Exception:
        return None


def load_scanner(worktree_path: str, ours_blob_text: str):
    """Load the scanner code (`scan_tree`/`scan_source`/…). Prefers the
    production module at `src/no_human/testing/structural_budget.py` if
    present (PR #1035's planned extraction); falls back to loading "ours"'s
    own copy of `tests/test_structural_budget.py` as a throwaway module —
    that file is self-contained (the scanner lives inside it today) and
    "ours" is the side whose conflict we are resolving, so it is the
    faithful copy of the scanner as of this branch."""
    real = Path(worktree_path) / "src" / "no_human" / "testing" / "structural_budget.py"
    if real.exists():
        return _load_module_from_path("nh_budget_scanner_real", real)
    tmp_path: Path | None = None
    try:
        fd, tmp_name = tempfile.mkstemp(suffix=".py")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(ours_blob_text)
        tmp_path = Path(tmp_name)
        return _load_module_from_path("nh_budget_scanner_tmp", tmp_path)
    except Exception:
        return None
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink()
            except OSError:
                pass


def measure(worktree_path: str, ours_blob_text: str) -> dict | None:
    """Run the scanner against `<worktree_path>/src/no_human` and return
    ``{"FROZEN_FUNCTION_LINES": {...}, "FROZEN_FUNCTION_CC": {...},
    "FROZEN_FILE_LINES": {...}}`` (each an offenders-only dict, exactly what
    `scan_tree` itself returns), or `None` if the scanner cannot be loaded or
    run."""
    scanner = load_scanner(worktree_path, ours_blob_text)
    if scanner is None:
        return None
    try:
        scanned = scanner.scan_tree(Path(worktree_path) / "src" / "no_human")
    except Exception:
        return None
    if not isinstance(scanned, tuple) or len(scanned) < 3:
        return None
    return {name: dict(scanned[idx]) for name, idx in _DICT_TO_MEASURE.items()}


def run_budget_test(worktree_path: str, timeout: int = _BUDGET_TEST_TIMEOUT_S) -> tuple[bool, str]:
    """Run `pytest tests/test_structural_budget.py` inside *worktree_path*
    under an isolated HOME, as proof that a mechanically re-anchored value is
    actually correct (never trust the arithmetic alone). Returns
    `(passed, captured_output)`."""
    tmp_home = tempfile.mkdtemp(prefix="nh-budget-proof-")
    env = dict(os.environ)
    env["HOME"] = tmp_home
    env["NO_HUMAN_HOME"] = tmp_home
    env["PYTHONPATH"] = str(Path(worktree_path) / "src")
    try:
        proc = _sh(
            [sys.executable, "-m", "pytest", BUDGET_TEST_PATH, "-q",
             "-p", "no:cacheprovider"],
            cwd=worktree_path, timeout=timeout, env=env,
        )
    except subprocess.TimeoutExpired:
        return False, "timed out"
    finally:
        shutil.rmtree(tmp_home, ignore_errors=True)
    return proc.returncode == 0, _cap(proc.stdout + proc.stderr)
