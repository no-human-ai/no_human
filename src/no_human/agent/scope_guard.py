"""Deterministic scope & dependency guards (EVOLUTION_PLAN Phase 5e).

Three non-LLM guards that fire every time — no "every 5 calls" gap:

1. **Scope guard** (PostToolUse): after an edit, check if the target file was
   declared in PLAN.md's "FILES TO CHANGE/CREATE" section.  Warn (don't block)
   when the edit is out-of-scope — the agent gets a nudge to justify or revert.

2. **Dependency-set diff** (commit-time): compare ``pyproject.toml``
   ``[project.dependencies]`` before vs after — flag any *added* packages so the
   reviewer can confirm they're necessary (lean-stack constraint).

3. **Forbidden-import regex** (commit-time): scan changed ``.py`` files for
   imports that violate project constraints (e.g. heavyweight frameworks).

All functions are pure — no LLM calls, no side effects — so they're trivially
testable and cost zero latency beyond the file reads.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any, Callable


# ── 0. Agent-owned paths ────────────────────────────────────────────────── #

#: Directories owned by agent tooling.  Every one of them is excluded from every
#: git diff (``vcs/git.py::_EPHEMERAL``), so nothing written inside them can ever
#: reach a commit or a PR.  Two consequences the guards below rely on: an edit
#: there cannot *be* a scope violation, and rewriting one repeatedly is not an
#: edit-loop.  no_human itself writes ``.no_human/PLAN.md`` for the agent to read.
AGENT_OWNED_DIRS = frozenset({".no_human", ".claude", ".devin", ".windsurf"})  # term-ok: real on-disk IDE config dirs

#: Agent-authored files, likewise excluded from every git diff wherever they sit.
#: The orchestrator's own comment (core/orchestrator.py, plan materialization)
#: records why: a root ``PLAN.md`` outlives the run and the next planner reads it
#: back as repo content. Same class as the dirs above, not a build artifact.
AGENT_OWNED_FILES = frozenset({"PLAN.md", "HANDOVER.md"})

#: Repo-relative scratch directory the coder is told to use for drafts and notes.
SCRATCH_DIR = ".no_human/scratch"
#: Agent-owned paths the HARNESS reads back from the working tree — the implement
#: prompt tells the coder to write them, so the scratch nudge must stay silent
#: there. Spelled here (not imported from testing.repro_gate) to keep this
#: module import-light; `tests/test_scope_guard.py` pins the two in sync.
HARNESS_READ_FILES = frozenset({".no_human/repro_tests.json"})
_SCRATCH_PARTS = Path(SCRATCH_DIR).parts


def _relative(path: str | Path, repo_root: str | Path = "") -> Path:
    """Best-effort repo-relative view of *path* (unchanged if not under root)."""
    p = Path(str(path).removeprefix("./"))
    if repo_root:
        try:
            return p.relative_to(repo_root)
        except ValueError:
            pass
    return p


def is_agent_owned(path: str | Path, repo_root: str | Path = "") -> bool:
    """True when *path* lives inside a directory owned by agent tooling.

    Pass *repo_root* whenever it is known.  Without it the check runs against the
    absolute path, and a checkout that lives *inside* an agent-owned directory —
    a per-task worktree under ``~/.no_human/worktrees/`` is exactly that, and
    since ``isolation.enabled`` defaults on it is the normal case, not an
    opt-in one — reads as wholly agent-owned, switching both guards off.
    """
    rel = _relative(path, repo_root)
    if any(part in AGENT_OWNED_DIRS for part in rel.parts):
        return True
    return rel.name in AGENT_OWNED_FILES


def is_outside_repo(path: str | Path, repo_root: str | Path = "") -> bool:
    """True when *path* is not part of this checkout at all — not agent-owned,
    just outside the repo (task 0ab78498 attempt 1: 15 edits of
    ``/tmp/dcrace/harness.mjs``, a harness script the coder wrote next to the
    repo, killed the attempt for an "edit loop" on a path nothing in the repo
    could ever see).

    Without a known *repo_root* nothing can be classified as outside, so this
    returns False (count it) — same fallback as `is_agent_owned`, needed for
    the pre-`_active_repo_root` callers and existing sink tests that pass bare
    relative paths like ``"calc.py"``. A relative *path* is likewise always
    False: it is relative to the worktree cwd, i.e. already inside.

    An absolute *path* is inside if EITHER the raw path is under the raw root
    OR ``os.path.realpath(path)`` is under ``os.path.realpath(repo_root)``;
    otherwise it is outside. That union is what makes a symlink unusable as an
    escape hatch both ways: a symlink planted outside the repo that points at
    a real repo file resolves under the root and still counts as inside, while
    platform root symlinks (macOS's ``/tmp`` -> ``/private/tmp``) don't wrongly
    evict a real repo file just because its raw form doesn't textually match a
    resolved root. A linked git worktree is passed in as *repo_root* itself,
    so any file inside it is trivially inside on the raw comparison alone.
    """
    if not repo_root:
        return False
    p = Path(str(path).removeprefix("./"))
    if not p.is_absolute():
        return False
    root = Path(repo_root)
    if p.is_relative_to(root):
        return False
    try:
        resolved_p = Path(os.path.realpath(p))
        resolved_root = Path(os.path.realpath(root))
    except OSError:
        return True  # raw comparison above already said "not under root"
    return not resolved_p.is_relative_to(resolved_root)


def scratch_redirect(
    edited_path: str | Path, repo_root: str | Path = ""
) -> str | None:
    """Nudge for edits to agent-owned dirs outside the blessed scratch path.

    Deliberately never says "revert": the old scope warning did, and an agent
    dutifully rewriting the same ignored file tripped the per-file edit-loop
    detector and killed the task (task 61406d02).
    """
    if not is_agent_owned(edited_path, repo_root):
        return None
    rel = _relative(edited_path, repo_root)
    if rel.parts[: len(_SCRATCH_PARTS)] == _SCRATCH_PARTS:
        return None  # already in the blessed scratch dir — nothing to say
    if rel.as_posix() in HARNESS_READ_FILES:
        # The harness READS these from the working tree; "nothing written
        # there can reach the PR" is true and beside the point. Task 89db42ea
        # (2026-08-20): the coder wrote the repro manifest where the implement
        # prompt told it to, this nudge told it to move it to "a real path",
        # and the REQUIRED repro gate then failed the attempt for having no
        # manifest — 99 turns lost to two guards contradicting each other.
        return None
    return (
        f"[SCRATCH] '{rel}' is an agent-owned path, excluded from every git diff, "
        f"so nothing written there can reach the PR. If this file is part of the "
        f"change, write it to a real path in the repo. If it is a draft, note, or "
        f"throwaway, put it under `{SCRATCH_DIR}/` and move on — do not rewrite it "
        f"in place."
    )


def harness_file_hint(edited_path: str | Path, repo_root: str | Path = "") -> str | None:
    """Immediate feedback on a write to a file the harness reads back.

    The scratch nudge is silent on these paths (see `scratch_redirect`), and
    silence is not enough: task 89db42ea wrote a well-meant manifest in the
    wrong SHAPE and learned of it only when the required gate failed the
    attempt two minutes later, with a reason that named a missing file. Run
    the gate's own reader on the write and hand the schema back now.
    """
    rel = _relative(edited_path, repo_root).as_posix()
    if rel not in HARNESS_READ_FILES or not repo_root:
        return None
    from ..testing.repro_gate import manifest_problem  # function-local: keeps this module import-light
    try:
        problem = manifest_problem(Path(repo_root))
    except Exception as exc:  # noqa: BLE001 — a hook must never raise into the SDK
        problem = f"{rel} could not be read ({exc.__class__.__name__})"
    if problem is None:
        return None
    return f"[REPRO] {problem}. Fix the file in place — the harness reads it from here."


# ── 1. Scope guard ──────────────────────────────────────────────────────── #

def parse_plan_files(plan_text: str) -> set[str]:
    """Extract declared file paths from PLAN.md's FILES TO CHANGE/CREATE section.

    Delegates section extraction to ``TaskSpec.from_plan`` (single source of
    truth for plan-markdown parsing), then applies path-token extraction to
    handle items that include descriptions (e.g. ``src/foo.py — add bar``).

    Extension-less filenames (``Jenkinsfile``, ``Makefile``) are preserved.
    Returns a set of normalised paths (stripped, no leading ``./``).
    """
    from ..core.task import TaskSpec

    spec = TaskSpec.from_plan(plan_text)
    paths: set[str] = set()
    for item in spec.files_to_change:
        tok = _extract_path_token(item)
        if tok:
            paths.add(tok.removeprefix("./"))
    return paths


def _extract_path_token(text: str) -> str | None:
    """Extract the first path-like token from a file-list item.

    Handles items that include descriptions after the path
    (e.g. ``src/foo.py — add validation``) and bare filenames
    with no extension or slash (e.g. ``Jenkinsfile``).
    """
    tokens = text.split()
    for tok in tokens:
        tok = tok.strip("`'\"(),:")
        if not tok:
            continue
        if "/" in tok or "." in tok:
            return tok
        if len(tokens) == 1:
            return tok
    return None


def check_scope(
    edited_path: str,
    plan_files: set[str],
    repo_root: str | Path = "",
) -> str | None:
    """Return a warning string if *edited_path* is not in *plan_files*, else None.

    Normalises both sides to repo-relative paths before comparing.
    """
    if is_agent_owned(edited_path, repo_root):
        return None  # excluded from every git diff — cannot be out of scope
    if not plan_files:
        return None  # no plan → nothing to enforce
    norm = edited_path.removeprefix("./")
    if repo_root:
        try:
            # `as_posix()`, not `str()`: plan files are `/`-separated (they come
            # from the plan text and from git), and `str(WindowsPath)` produces
            # backslashes, so the membership test below never matched on
            # Windows and EVERY edit was reported as out of scope.
            norm = Path(norm).relative_to(repo_root).as_posix()
        except ValueError:
            pass
    if norm in plan_files:
        return None
    # Fuzzy: check if any plan file ends with the edited path's basename
    base = Path(norm).name
    if any(Path(pf).name == base for pf in plan_files):
        return None
    return (
        f"[SCOPE] Edit to '{norm}' is outside the declared plan. "
        "If this is intentional (e.g. a necessary refactor), continue — "
        "otherwise revert and stay within the planned file list."
    )


# ── 2. Dependency-set diff ──────────────────────────────────────────────── #

_DEP_LINE = re.compile(r"""^\s*['"]?([a-zA-Z0-9_-]+)""")


def _parse_deps(text: str) -> set[str]:
    """Extract package names from a ``[project.dependencies]`` block."""
    deps: set[str] = set()
    in_deps = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[project.dependencies]") or stripped == "dependencies = [":
            in_deps = True
            continue
        if in_deps:
            if stripped.startswith("[") or (stripped == "]"):
                break
            m = _DEP_LINE.match(stripped)
            if m:
                deps.add(m.group(1).lower())
    return deps


def check_dependency_diff(repo_path: Path) -> list[str]:
    """Return list of *newly added* dependencies in ``pyproject.toml``.

    Compares HEAD vs the working tree.  Returns an empty list if the file is
    unchanged or the repo has no ``pyproject.toml``.
    """
    pyproject = repo_path / "pyproject.toml"
    if not pyproject.exists():
        return []
    try:
        before = subprocess.run(
            ["git", "show", "HEAD:pyproject.toml"],
            cwd=repo_path, capture_output=True, text=True,
        )
        before_deps = _parse_deps(before.stdout or "")
    except Exception:
        before_deps = set()
    after_deps = _parse_deps(pyproject.read_text(encoding="utf-8"))
    added = sorted(after_deps - before_deps)
    return added


# ── 3. Forbidden-import regex ───────────────────────────────────────────── #

# Default forbidden imports — heavyweight frameworks that violate lean-stack.
DEFAULT_FORBIDDEN_IMPORTS: list[str] = [
    r"^import\s+tensorflow\b",
    r"^from\s+tensorflow\b",
    r"^import\s+torch\b",
    r"^from\s+torch\b",
    r"^import\s+django\b",
    r"^from\s+django\b",
]


def check_forbidden_imports(
    changed_files: list[Path],
    forbidden_patterns: list[str] | None = None,
) -> list[tuple[str, int, str]]:
    """Scan *changed_files* for forbidden import patterns.

    Returns a list of ``(filepath, line_number, matched_line)`` tuples.
    Only checks ``.py`` files.
    """
    patterns = [re.compile(p) for p in (forbidden_patterns or DEFAULT_FORBIDDEN_IMPORTS)]
    findings: list[tuple[str, int, str]] = []
    for fp in changed_files:
        if not str(fp).endswith(".py"):
            continue
        if not fp.exists():
            continue
        try:
            for i, line in enumerate(fp.read_text(encoding="utf-8").splitlines(), 1):
                for pat in patterns:
                    if pat.search(line):
                        findings.append((str(fp), i, line.strip()))
                        break
        except (OSError, UnicodeDecodeError):
            continue
    return findings


# ── 4. PostToolUse hook for scope guard ─────────────────────────────────── #

_EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}


class ScopeGuardHook:
    """PostToolUse hook that warns when edits target out-of-plan files."""

    def __init__(
        self,
        plan_text: str,
        repo_path: str | Path,
        on_event: Callable[[str, str], None] | None = None,
    ):
        self.plan_files = parse_plan_files(plan_text)
        self.repo_root = str(repo_path)
        self._on_event = on_event or (lambda kind, text: None)
        self._nudged: set[str] = set()

    @staticmethod
    def _context(message: str) -> dict:
        return {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": message,
            }
        }

    async def hook(
        self, input_data: dict, tool_use_id: str | None, context: Any
    ) -> dict:
        if input_data.get("tool_name", "") not in _EDIT_TOOLS:
            return {}
        raw = (
            (input_data.get("tool_input") or {}).get("file_path")
            or (input_data.get("tool_input") or {}).get("path")
            or (input_data.get("tool_input") or {}).get("notebook_path")
        )
        if not raw:
            return {}
        # Checked before the plan gate: an edit to an agent-owned dir is never a
        # scope violation, with or without a declared plan.
        nudge = scratch_redirect(str(raw), self.repo_root)
        if nudge is not None:
            if str(raw) not in self._nudged:
                self._nudged.add(str(raw))
                self._on_event("scratch_redirect", nudge)
            return self._context(nudge)
        hint = harness_file_hint(str(raw), self.repo_root)
        if hint is not None:
            # Every time, like the nudge: it is the tool's feedback on THIS
            # write. Logged every time too — each write may carry a new shape.
            self._on_event("repro_manifest_hint", hint)
            return self._context(hint)
        if not self.plan_files:
            return {}
        warning = check_scope(str(raw), self.plan_files, self.repo_root)
        if warning is None:
            return {}
        self._on_event("scope_warning", warning)
        return self._context(warning)


# ── 5. Commit-time aggregate guard ──────────────────────────────────────── #

def commit_time_checks(
    repo_path: Path,
    changed_files: list[Path] | None = None,
    forbidden_import_patterns: list[str] | None = None,
) -> list[str]:
    """Run deterministic guards at commit time. Returns a list of warnings."""
    warnings: list[str] = []
    # Dependency diff
    added = check_dependency_diff(repo_path)
    if added:
        warnings.append(
            f"[DEP] New dependencies added to pyproject.toml: {', '.join(added)}. "
            "Confirm each is necessary (lean-stack constraint)."
        )
    # Forbidden imports
    if changed_files:
        hits = check_forbidden_imports(changed_files, forbidden_import_patterns)
        for fpath, lineno, line in hits:
            warnings.append(
                f"[IMPORT] Forbidden import at {fpath}:{lineno}: {line}"
            )
    return warnings
