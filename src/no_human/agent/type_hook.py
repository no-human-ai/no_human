"""Per-edit net-new type diagnostics (issue #114, phase 2).

Phase 1 (`review/type_evidence.py`) put type diagnostics in front of the
REVIEWER: one whole-project run at the merge base, one at the attempt's tree,
subtract, attach what the diff introduced. This module puts the same signal in
front of the CODER, in the turn that caused it — the loop SWE-agent's edit-time
linter and Aider's post-edit lint occupy. It sits beside `LintFeedbackHook` and
shares its shape: a `PostToolUse` hook, deterministic, no LLM cost,
config-gated, and silent unless it has something to report.

WHAT "NET-NEW" MEANS HERE, AND WHY IT IS NOT PHASE 1's BASELINE. Phase 1 diffs
against the MERGE BASE, which is the right question at a gate: what did this
whole change introduce. Asking it per edit is not affordable — a whole-project
run, twice, against a `git worktree` — and it is not the question the coder
needs answered. The coder needs "the edit I just made broke something", so the
baseline here is THE SAME FILE, MOMENTS EARLIER, IN THE SAME TREE. The first
edit to a file records that baseline and reports nothing; every later edit is
subtracted against it.

WHAT THE SUBTRACTION ACTUALLY ESTABLISHES, stated precisely because an earlier
version of this paragraph overclaimed and the feedback text inherited the
error. Both runs are the same checker, the same argv, the same working tree and
the same interpreter, seconds apart, so the ENVIRONMENT is identical between
them. That is the whole of what the identity buys, and it is enough to make the
comparison sound: phase 1 needs `_environments_comparable` because its base run
is a dependency-less worktree whose unresolvable imports degrade to `Any` and
suppress diagnostics the after run still reports, and no such asymmetry can
arise here. There is consequently no comparability check in this module, and
its absence is a property of the design.

What it does NOT buy is attribution. A difference between the two runs means
SOMETHING the checker read changed between them — not that the triggering edit
changed it. Two ordinary sequences produce a diagnostic this hook cannot blame
correctly, and both were reproduced rather than reasoned about:

* `mypy <file>` and `pyright <file>` FOLLOW IMPORTS, so the whole transitive
  graph is inside the comparison. Editing `lib.py` and then `app.py` reports
  `lib.py`'s new error against the edit to `app.py`, whose bytes never changed.
* `_EDIT_TOOLS` does not contain `Bash`, so `sed -i`, a heredoc, `patch`,
  `ruff --fix` and `black` are invisible to this hook. Whatever they broke
  first surfaces at the next Edit/Write, attributed to that edit.

Neither is a false clean — the hook still only ever speaks about diagnostics
that are really there and really new since the last check. But a wrong
attribution carrying an imperative costs the coder turns spent "fixing"
something it did not cause, which is the same attempt cost this feature exists
to remove. So the feedback text says what is true — that these are diagnostics
on or reachable from the file which were not present at its last check, and
that a change elsewhere can be the cause — rather than borrowing the lint
hook's "your edit to X introduced", which ruff can say honestly because ruff
sees exactly one file.

NAMED CEILINGS. Silence from this hook must never read as "your edit is clean".

* THE FIRST EDIT TO A FILE REPORTS NOTHING. It has nothing to subtract against,
  and inventing a baseline (the committed content, a worktree at the base
  commit) reintroduces exactly the asymmetry above. Phase 1's gate-time pass is
  the backstop: an error introduced by a file's first edit and never touched
  again is caught there, one round later than it could have been. That is the
  price of not reporting inflated errors here.
* DEPENDENCIES, NOT DEPENDENTS. `mypy <file>` and `pyright <file>` analyse that
  file and what it IMPORTS, never its CALLERS, so the characteristic type break
  — narrow a parameter, light up every call site — is invisible here and shows
  up at the gate instead. This is the largest coverage difference from phase 1
  and the reason phase 2 does not replace it. The feedback text says so, because
  the ceiling is not visible from inside the turn.
* PYTHON ONLY. `tsc` is excluded: handed a file it ignores the repo's
  `tsconfig.json`, so a per-file TypeScript run answers a different question
  than the project's own configuration asks, and the honest alternative — a
  whole-project `tsc --noEmit` after every edit — is a phase 3 cost question.
  TypeScript repos get phase 1's gate-time evidence and nothing here.
* REPORTED ONCE. After a report the post-edit diagnostics BECOME the baseline,
  so a diagnostic the coder chose not to fix is not repeated on the next edit to
  that file. The gate still has the last word.
* OUR BINARY, NEVER THE REPO'S, and nothing is installed. Inherited whole from
  phase 1 via `_run_checker`/`_resolve_binary`; that module's ceilings apply
  unchanged, including the two things the third-party checkers do themselves
  (the PyPI `pyright` launcher's first-run download, and `mypy` importing a
  `plugins =` module the reviewed repo names). Both are declared in
  `tests/test_egress_allowlist.py` against `review/type_evidence.py`, not
  against this module: the channel is that module's because `_run_checker` is
  what spawns, and grepping the allowlist for `type_hook` is a dead end by
  design.
* NOTHING IS WRITTEN INSIDE THE REPO. `mypy` drops `.mypy_cache/` at its
  default location, which here is the tree the coder is working in and the tree
  the tamper guard reads, so it is pointed under the system temp root instead
  and a run that cannot get such a directory does not happen. `pyright` writes
  nothing. `tsc`'s `*.tsbuildinfo` would need handling of its own before
  TypeScript could be added: `vcs/git.py` excludes `.mypy_cache/**` from the
  commit path and has no exclusion for that file.

WHY THIS REUSES PHASE 1's RUNNER. `_run_checker` encodes a contract that is easy
to state and easy to lose in a second copy: `None` means "we learned nothing",
`[]` means "the checker ran and found nothing", and collapsing them turns a
crashed checker into a clean bill of health. This module imports it rather than
restating it.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import stat
import tempfile
import time
import weakref
from collections import OrderedDict
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

# Shared, not forked: two PostToolUse hooks disagreeing about what counts as an
# edit is a defect neither would show. `_path_of` is mirrored instead (it is a
# method on that class) and `tests/test_type_hook.py` pins the two together.
from .lint_hook import _EDIT_TOOLS

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..review.type_evidence import TypeDiagnostic

log = logging.getLogger(__name__)

#: Wall-clock cap for ONE per-edit run — 4.5x under phase 1's 90s, which is
#: spent once per review where this sits between the model and its next token
#: after every edit.
TYPE_HOOK_TIMEOUT = 20

#: Cumulative cap for one hook instance, i.e. one attempt. The per-run timeout
#: alone does not bound the feature — fifty edits at 20s is seventeen minutes no
#: per-run number can see. Once spent the hook is silent for the rest of the
#: attempt and says so once, because a guard that degrades without telling
#: anyone is indistinguishable from one that was never on.
TYPE_HOOK_SESSION_BUDGET = 240

#: Diagnostics quoted back into the turn, and the byte ceiling on the block.
#: Far below phase 1's 40/8192: this text is prepended to a coder's turn, and
#: forty diagnostics is not feedback, it is a wall. The count is always stated
#: in full even when the list is trimmed.
MAX_FEEDBACK_DIAGNOSTICS = 8
MAX_FEEDBACK_BYTES = 1500

#: Per-line cap. Its job is not tidiness: without it one pathological message
#: (a fully-expanded generic type) can exceed the whole byte budget on the first
#: line, and the block then renders a count with NOTHING under it — the least
#: useful thing this hook can emit. Capped, the header plus one line always
#: fits, so at least one location always reaches the coder.
MAX_FEEDBACK_LINE = 300

#: Files whose baseline is retained, LRU. Unbounded, this dict grows with every
#: distinct file an attempt edits and holds each one's full diagnostic list.
#: Eviction is safe in a way TRUNCATING a retained list would not be: an evicted
#: file re-baselines on its next edit and reports nothing that turn, whereas a
#: shortened list would make the diagnostics dropped from it look net-new.
BASELINE_MAX_FILES = 64

#: The checkers a per-file invocation is meaningful for, and the suffixes each
#: accepts. `tsc` is absent by design — see PYTHON ONLY above.
_PER_EDIT_CHECKERS: dict[str, tuple[str, ...]] = {
    "pyright": (".py", ".pyi"),
    "mypy": (".py", ".pyi"),
}


def per_edit_checker(repo_path: str | Path) -> str | None:
    """The checker this repo CONFIGURES that a per-file run can honour, or None.

    Detection is phase 1's `detect_type_checkers` verbatim — same configs, same
    precedence, same refusal to guess — filtered to what `_PER_EDIT_CHECKERS`
    covers, so a repo configuring only `tsc` gets None here and its gate-time
    evidence unchanged.
    """
    from ..review.type_evidence import detect_type_checkers

    for checker in detect_type_checkers(Path(repo_path)):
        if checker.name in _PER_EDIT_CHECKERS:
            return checker.name
    return None


def _dir_is_private(st: os.stat_result, our_uid: int) -> bool:
    """Whether a directory is ours alone: a real directory, owned by us, not
    writable by group or other.

    A pure predicate over a stat result rather than a path, so the rule is
    exercised on every platform the suite runs on. Faking it through `os.name`
    instead is not an option: `pathlib` dispatches on that name and building a
    `PosixPath` on Windows raises.
    """
    # `S_ISDIR` alone covers the symlink case BECAUSE the caller passes an
    # `os.lstat`: a symlink reports S_IFLNK, never S_IFDIR. An explicit
    # `S_ISLNK` clause here was unreachable and read as though it were the
    # thing doing the work, when the `lstat` is.
    if not stat.S_ISDIR(st.st_mode):
        return False
    if st.st_uid != our_uid:
        return False
    return not st.st_mode & (stat.S_IWGRP | stat.S_IWOTH)


#: The one PREDICTABLE path this module creates: a per-machine parent that
#: holds one private subdirectory per attempt. It is the predictability that
#: makes `_dir_is_private` load-bearing — on Linux `tempfile.gettempdir()` is
#: `/tmp`, so another account can create this name first and then read or
#: rewrite what mypy caches under it.
_CACHE_ROOT_NAME = "nh-typehook-mypy"


def _cache_root() -> Path | None:
    """The parent directory for per-attempt mypy caches, or None.

    Created 0700 and refused outright if an existing one is not ours alone —
    `exist_ok=True` on its own would adopt a hostile directory silently. The
    ownership half is POSIX-only (`st_uid` is 0 for everyone on Windows and the
    mode bits do not mean what they say there); on Windows the per-user temp
    root provides the isolation instead.
    """
    try:
        root = Path(tempfile.gettempdir()) / _CACHE_ROOT_NAME
        os.makedirs(root, mode=0o700, exist_ok=True)
        if os.name == "posix" and not _dir_is_private(os.lstat(root), os.getuid()):
            log.warning("refusing mypy cache root %s — not private to us", root)
            return None
        return root
    except OSError:
        log.warning("no writable mypy cache root; no per-edit type check",
                    exc_info=True)
        return None


def reap_stale_caches(root: Path) -> int:
    """Delete per-attempt cache directories whose owning process is gone.

    THE LEAK THIS CLOSES. The cache used to be one directory per REPO PATH,
    keyed on `realpath(repo_path)` — and `repo_path` is the attempt's
    worktree, which `Orchestrator._worktree_path` mints per RUN as
    `<task_id>.<pid>.<random>`. So the key was never stable: every run got a
    cold cache AND left a tree behind, and nothing knew the name to reclaim it
    (1840 of them on one developer machine). The docstring claimed the
    opposite on both counts.

    Reclaimed by owner-pid liveness rather than by age, which is this repo's
    existing rule for per-run state — `core/worktree.py`'s sweep and salvage
    both skip a directory whose `owner pid is alive`. An age heuristic would
    either delete a long attempt's live cache or keep a dead one for hours.
    A name we cannot parse is left alone: this function only ever removes
    directories it can positively attribute to a dead process.
    """
    from ..config import pid_alive

    removed = 0
    try:
        children = list(root.iterdir())
    except OSError:
        return 0
    for child in children:
        if not child.is_dir():
            continue
        owner, _, _ = child.name.partition(".")
        if not owner.isdigit():
            continue
        if pid_alive(int(owner)):
            continue
        shutil.rmtree(child, ignore_errors=True)
        removed += 1
    if removed:
        log.info("reclaimed %d stale mypy cache dir(s) under %s", removed, root)
    return removed


def _nbytes(text: str) -> int:
    """UTF-8 length. The budget is a BYTE budget and `len()` is characters.

    They coincide only for ASCII, and a type checker quotes identifiers and
    string literals out of the repo under review, so they routinely do not:
    eight 120-character diagnostics measured 1528 characters either way, but
    3208 bytes in CJK and 4048 with emoji against a 1500 cap. The old fixture
    (`"m" * 400`) was pure ASCII and structurally could not see the gap.
    """
    return len(text.encode("utf-8"))


def _trim_to_bytes(text: str, limit: int) -> str:
    """`text` cut to at most `limit` UTF-8 bytes, with an ellipsis when cut.

    Truncation happens on the ENCODED form and is decoded back with
    `errors="ignore"`, so a cut that lands inside a multi-byte character drops
    that character instead of emitting a lone continuation byte.
    """
    if _nbytes(text) <= limit:
        return text
    room = max(0, limit - _nbytes("…"))
    return text.encode("utf-8")[:room].decode("utf-8", "ignore").rstrip() + "…"


class TypeFeedbackHook:
    """A PostToolUse hook that type-checks the just-edited file.

    One instance per attempt: the per-file baselines and the session budget both
    live on it and neither is meaningful across attempts.
    """

    def __init__(
        self,
        *,
        repo_path: str | Path,
        checker: str | None,
        on_event: Callable[[str, str], None] | None = None,
        timeout: int = TYPE_HOOK_TIMEOUT,
        session_budget: int = TYPE_HOOK_SESSION_BUDGET,
    ):
        self.repo_path = Path(repo_path)
        # An unsupported name is normalised to "off" here rather than trusted
        # downstream: `tsc` reaching the extension gate would be a KeyError
        # raised inside a PostToolUse hook, i.e. a guard breaking the session it
        # exists to guard.
        if checker is not None and checker not in _PER_EDIT_CHECKERS:
            log.warning("per-edit type checking does not support %r; off", checker)
            checker = None
        self.checker = checker
        self._on_event = on_event or (lambda kind, text: None)
        self.timeout = timeout
        self.session_budget = session_budget
        #: repo-relative POSIX path -> the diagnostics the last run reported,
        #: most-recently-used last. Bounded by `BASELINE_MAX_FILES`.
        self._baseline: OrderedDict[str, list[TypeDiagnostic]] = OrderedDict()
        self._spent = 0.0
        self._exhausted = False
        #: Lazily created per-attempt mypy cache, and the finalizer that
        #: removes it. See `_mypy_cache`.
        #: Serialises the budget read, the checker run and the baseline
        #: write. See `_hook`.
        self._lock = asyncio.Lock()
        self._cache: Path | None = None
        self._cache_finalizer: weakref.finalize | None = None

    def _mypy_cache(self) -> Path | None:
        """This attempt's `--cache-dir`, created once and reclaimed with it.

        WARM WITHIN THE ATTEMPT, GONE WITH IT — which is the whole of the claim
        now, because it is the whole of what is true. The measured benefit
        (4.6s cold, 0.2s warm on a 468-line file) is a within-run effect, and a
        cross-run cache could not deliver it anyway: mypy keys its cache on
        source paths, and every run gets a fresh worktree path.

        A random `mkdtemp` child rather than a derived name, so two attempts
        never share one — mypy takes no cross-process lock on its cache — and
        so the per-attempt directory is not guessable even though its parent
        is. The owning pid leads the name, which is what lets
        `reap_stale_caches` attribute a leftover to a dead process.

        `weakref.finalize` rather than `__del__`: it runs when the hook is
        collected AND at interpreter exit, and it holds only the path string,
        so it never keeps the hook itself alive.
        """
        if self._cache is not None:
            return self._cache
        root = _cache_root()
        if root is None:
            return None
        reap_stale_caches(root)
        try:
            self._cache = Path(
                tempfile.mkdtemp(dir=root, prefix=f"{os.getpid()}.")
            )
        except OSError:
            log.warning("could not create a per-attempt mypy cache dir",
                        exc_info=True)
            return None
        self._cache_finalizer = weakref.finalize(
            self, shutil.rmtree, str(self._cache), True,
        )
        return self._cache

    # `LintFeedbackHook._path_of`'s twin; the shared test pins them together.
    @staticmethod
    def _path_of(tool_input: dict) -> str | None:
        p = (
            tool_input.get("file_path")
            or tool_input.get("path")
            or tool_input.get("notebook_path")
        )
        return str(p) if p else None

    def _relative(self, raw: str) -> str | None:
        """`raw` as a repo-relative POSIX path, or None when it is outside.

        A relative input is resolved against the repo rather than the process
        CWD: the hook runs wherever the orchestrator happens to live, and
        resolving "app.py" against that would put every relative tool input
        outside the repo and silently disable the hook. Outside means declined,
        not resolved-anyway — phase 1's parsers return repo-relative paths, so a
        path the repo does not contain could never key a baseline. `realpath`
        rather than `resolve` for consistency with `_resolve_binary`'s guard;
        both collapse a symlink that escapes the tree.
        """
        try:
            candidate = Path(raw)
            if not candidate.is_absolute():
                candidate = self.repo_path / candidate
            resolved = Path(os.path.realpath(str(candidate)))
            root = Path(os.path.realpath(str(self.repo_path)))
            return resolved.relative_to(root).as_posix()
        except (OSError, ValueError):
            return None

    def _budget_left(self) -> int:
        """Seconds this run may take: the per-run cap clipped to what is left.

        Phase 1's rule for phase 1's reason — a run that cannot start inside the
        remaining deadline does not start, so the cumulative cap is a bound and
        not a suggestion the last run may overshoot.
        """
        remaining = self.session_budget - self._spent
        if remaining <= 0:
            return 0
        return max(1, min(self.timeout, int(remaining)))

    def _checker_for(self, relative: str) -> Any | None:
        """A phase 1 `_Checker` whose argv names exactly `relative`, or None."""
        from ..review.type_evidence import _CHECKERS_BY_NAME, _Checker

        base = _CHECKERS_BY_NAME.get(self.checker or "")
        if base is None:
            return None
        if base.name == "mypy":
            cache = self._mypy_cache()
            if cache is None:
                return None
            # Phase 1's flags minus the `.` that made it whole-project, plus the
            # relocated cache. Dropping `--no-error-summary` or
            # `--no-color-output` silently changes the format `parse_mypy` reads.
            return _Checker(
                "mypy",
                (
                    "mypy", "--no-error-summary", "--no-color-output",
                    "--show-column-numbers", "--cache-dir", str(cache), relative,
                ),
                base.ok_codes,
            )
        return _Checker(base.name, (*base.argv, relative), base.ok_codes)

    def _run(self, checker: Any, timeout: int) -> list[TypeDiagnostic] | None:
        from ..review.type_evidence import _run_checker

        return _run_checker(
            checker, self.repo_path, timeout=timeout, repo_path=self.repo_path,
        )

    def _feedback(self, relative: str, new: list[TypeDiagnostic]) -> str:
        head = (
            f"[TYPE] {self.checker} reports {len(new)} type diagnostic(s) on or "
            f"reachable from {relative} that it did not report when this file "
            "was last checked. The edit that triggered this is the likely "
            "cause but not a proven one: a change to anything in the import "
            "graph since then produces the same result, and edits made through "
            "Bash are not observed at all. Fix what belongs to your change, "
            "and say so if a diagnostic is not yours:"
        )
        lines = [head]
        size = _nbytes(head)
        shown = 0
        for d in new:
            if shown >= MAX_FEEDBACK_DIAGNOSTICS:
                break
            loc = f"{d.path}:{d.line}" if d.line else d.path
            if d.line and d.column:
                loc = f"{loc}:{d.column}"
            code = f" {d.code}" if d.code else ""
            line = _trim_to_bytes(f"  {loc}{code} {d.message}".rstrip(),
                                  MAX_FEEDBACK_LINE)
            if size + 1 + _nbytes(line) > MAX_FEEDBACK_BYTES:
                break
            lines.append(line)
            size += 1 + _nbytes(line)
            shown += 1
        if len(new) > shown:
            lines.append(f"  ... and {len(new) - shown} more")
        lines.append(
            f"  This checked {relative} and what it imports, not its callers: a "
            "signature change can still break call sites this cannot see."
        )
        return "\n".join(lines)

    async def hook(
        self, input_data: dict, tool_use_id: str | None, context: Any
    ) -> dict:
        """The PostToolUse entry point. Never raises.

        The whole body is guarded, not just the subprocess call. An earlier
        version wrapped only `to_thread`, which left every other statement able
        to escape into the SDK's hook machinery — including `self._on_event`,
        an injected callback this module does not own and cannot vouch for. A
        guard that can abort the turn it is guarding is worse than no guard.
        """
        try:
            return await self._hook(input_data, tool_use_id, context)
        except Exception:  # noqa: BLE001 - a guard must not break the session
            log.warning("per-edit type check failed", exc_info=True)
            return {}

    async def _hook(
        self, input_data: dict, tool_use_id: str | None, context: Any
    ) -> dict:
        if not self.checker or self._exhausted:
            return {}
        if input_data.get("tool_name", "") not in _EDIT_TOOLS:
            return {}
        raw = self._path_of(input_data.get("tool_input", {}) or {})
        if not raw:
            return {}
        if not raw.lower().endswith(_PER_EDIT_CHECKERS[self.checker]):
            return {}
        relative = self._relative(raw)
        if relative is None:
            return {}

        # ONE run at a time, for the whole read-run-write. Without it the
        # cumulative budget is not the bound `_budget_left`'s docstring claims:
        # two overlapping calls each read the full remaining budget before
        # either credits `self._spent` in its `finally`, so both start. The
        # baseline has the same shape of bug — `self._baseline.get(...)` and
        # the write that follows straddle an `await`, so the run that finishes
        # LAST wins regardless of which observation is newer, and a stale
        # `after` can become the thing the next edit is subtracted against.
        #
        # Whether the SDK ever dispatches two PostToolUse callbacks
        # concurrently is a property of the SDK and is NOT established here.
        # This is the first hook in the repo carrying mutable cross-call state
        # and a cumulative budget (`LintFeedbackHook` is stateless), so the
        # property is made true here rather than assumed of the caller. The
        # lock is held across the subprocess, which is deliberate: two type
        # checkers racing on one attempt is not a thing worth allowing.
        async with self._lock:
            return await self._checked(relative)

    async def _checked(self, relative: str) -> dict:
        timeout = self._budget_left()
        if timeout == 0:
            # Once, then never again: repeating it every edit would bury it.
            self._exhausted = True
            self._on_event(
                "type_feedback_budget_exhausted",
                f"per-edit type checking spent its {self.session_budget}s "
                "attempt budget and is off for the rest of this attempt",
            )
            return {}

        checker = self._checker_for(relative)
        if checker is None:
            return {}

        started = time.monotonic()
        try:
            after = await asyncio.to_thread(self._run, checker, timeout)
        finally:
            # Billed in `finally` so a run that raised or was cancelled still
            # pays for the wall clock it burned; a failure mode that costs time
            # and no budget is one an attempt can repeat without limit. The
            # raise itself is caught by `hook`, which guards the whole body —
            # billing has already happened by the time it gets there.
            self._spent += time.monotonic() - started

        if after is None:
            # "We learned nothing", not "clean" — and nothing is recorded, so a
            # failed run cannot become what the next edit is subtracted against.
            return {}

        previous = self._baseline.get(relative)
        self._baseline[relative] = after
        self._baseline.move_to_end(relative)
        while len(self._baseline) > BASELINE_MAX_FILES:
            self._baseline.popitem(last=False)
        if previous is None:
            self._on_event(
                "type_feedback_baseline",
                f"{self.checker} baseline for {relative}: "
                f"{len(after)} diagnostic(s)",
            )
            return {}

        from ..review.type_evidence import net_new

        new = net_new(previous, after)
        if not new:
            return {}

        self._on_event(
            "type_feedback",
            f"{self.checker} reports {len(new)} net-new diagnostic(s) on {relative}",
        )
        return {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": self._feedback(relative, new),
            }
        }
