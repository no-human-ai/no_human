"""Net-new type-checker diagnostics as review evidence (issue #114, phase 1).

The review gate already reads deterministic tool output — ruff findings
(`lint_evidence`) and static reference counts (`wiring_evidence`). Type/compile
diagnostics are the signal every serious agent harness converged on and the one
this gate did not have. This module supplies it.

WHY THIS IS NOT SHAPED LIKE `lint_evidence`. Lint scopes its findings to the
lines the diff CHANGED, because a style violation on an untouched line is not
evidence about the agent's work. Type errors do not behave that way at all: the
characteristic net-new type error appears at a CALL SITE the diff never
touched — narrow a parameter, and every caller lights up. Filtering to changed
lines would drop exactly the diagnostics worth having. So the mechanism here is
a DIFF OF TWO RUNS: the same checker, the same arguments, over the whole
project, at the merge base and at the attempt's tree. What is present after and
absent before is what this change introduced.

That also disposes of the problem `lint_evidence` documents at length — whose
binary runs. Both runs use the SAME checker from no_human's own PATH, so a
version disagreement with the repo's pinned checker, or a missing third-party
stub, produces the same diagnostics on both sides and cancels in the
subtraction. What survives is attributable to the diff.

Named ceilings, stated because silence from this module must never be read as a
clean result:

* CONFIGURED REPOS ONLY. A repo that configures no type checker gets no
  section, and no subprocess is spawned. no_human installs nothing itself —
  which is not the same claim as "nothing is installed"; see WHAT THE CHECKER
  ITSELF DOES below.
* WHAT THE CHECKER ITSELF DOES is not ours to promise, because it is a
  third-party program. Two consequences are known, and both are declared
  against this module in `tests/test_egress_allowlist.py` rather than left for
  someone to discover:
  - the PyPI `pyright` distribution is a LAUNCHER. Its first run downloads a
    node runtime and the `pyright` npm package before it checks anything, so on
    a machine whose PATH resolves that wrapper a review makes a network call
    that nothing here asked for.
  - `mypy` IMPORTS the modules a `plugins =` line names, and it reads that line
    from the config of the repo under review. So this collector can execute
    repo-authored code. The harness already runs that repo's test command, so
    it is not a new trust boundary for the product — but it is the first time
    the REVIEW half executes anything the reviewed repo wrote, and that is
    worth knowing before this is ever promoted from advisory to a required
    gate.
* NO CREDENTIAL REACHES THE CHECKER, AND THAT HAS A COST. `_checker_env`
  strips every secret-shaped variable from the child environment
  (`agent/child_env.drop_foreign_secrets`, empty keep-list), because the
  bullet above means a `plugins =` line can make this collector run
  repo-authored code — which must never hold our OAuth token, API key or
  cloud credential. The cost is real and is not hypothetical: a plugin that
  READS one of those variables no longer loads. `mypy_django_plugin` is the
  canonical case, since it imports the settings module and that reads
  `SECRET_KEY`/`DATABASE_URL`; mypy then exits non-zero, `_run_checker`
  correctly distrusts the run, and the whole section would disappear. Rather
  than let it disappear silently, `_run_checker` records `_PLUGIN_ENV_NOTE`
  and the renderer prints a NOT COLLECTED line naming the cause. The trade is
  deliberate: a repo-controlled plugin is repo-controlled code, and losing one
  repo's type evidence is cheaper than handing that code a credential.
* OUR BINARY, NEVER THE REPO'S. `pyright`/`mypy`/`tsc` are resolved from
  no_human's own PATH, never a `.venv` or `node_modules` inside the repo under
  review — the same rule `lint_evidence` follows and for the same reason. A
  repo whose checker is not on our PATH gets no evidence rather than an
  execution of a binary it controls.
* NEITHER RUN TOUCHES THE TREE UNDER REVIEW. Both sides run in a throwaway
  `git worktree` at their own commit. That is not tidiness: this collector runs
  inside the window the orchestrator brackets with `reviewer_worktree.snapshot`
  / `.compare`, and a checker writing `.mypy_cache/` or `*.tsbuildinfo` into the
  attempt's tree makes that compare report an added path, which the orchestrator
  charges to the reviewer as `reviewer_wrote` — discarding a real verdict for an
  integrity failure nobody committed. `_run_at_commit` has the detail.
* TRACKED FILES ONLY, ON BOTH SIDES. A worktree at a commit carries no untracked
  files, so a scratch file the coder left in the working tree cannot contribute
  diagnostics that are net-new only because base could never have had them.
* COMPARABLE ENVIRONMENTS ONLY. A worktree carries no `.venv` and no
  `node_modules`. A checker that cannot resolve imports reports FEWER real
  errors than one that can, and an asymmetry there would read as net-new work.
  When the base run shows import-resolution failures the after run does not,
  `collect_type_evidence` reports that it did not run. See
  `_environments_comparable`.

  What that check does NOT catch, now that both sides are worktrees, is SYMMETRIC
  degradation: a project whose dependencies live in `node_modules` or a `.venv`
  resolves nothing on either side, so comparability passes and the subtraction
  runs over two equally blind analyses. It stays arithmetically sound — the same
  symbols are Any in both — but its coverage does not, and `net-new: 0` would
  read as a stronger claim than the run can support. That is why
  `TypeEvidence.unresolved_imports` is carried out to the renderer and printed
  as a COVERAGE LIMIT line: the reviewer is told how much of the tree the
  checker could not see. `mypy` is largely unaffected, resolving from OUR
  environment identically on both sides; `tsc` and `pyright` are the ones this
  bites.
* FINGERPRINTS ARE LINE-AGNOSTIC. Two runs of a checker over trees that differ
  by one inserted import put the SAME pre-existing diagnostic on different line
  numbers. Keying a diagnostic on its line would make every diagnostic below an
  edit read as net-new. The key is (path, code, normalised message) counted as
  a multiset; locations are reported from the after run only.

Advisory, exactly like its two siblings: any failure — no config, a missing
binary, a timeout, output that does not parse, a base worktree that cannot be
built — yields `ran=False`, and `ran=False` never renders a net-new count, a
diagnostic, or anything a reviewer could read as a clean result.

One failure does render a line, and only because the alternative is worse: a
plugin that could not import (see the credential ceiling above) prints a NOT
COLLECTED line naming the cause, so the reviewer can tell "the checker could
not start" from "this repo configures no checker". It is a statement about
this collector, never about the code, and `format_type_evidence` says so in
the sentence itself.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

from .lint_evidence import changed_line_numbers

log = logging.getLogger(__name__)

#: Wall-clock cap for the WHOLE collection — every checker, both sides, start to
#: finish. It was once a per-run cap, which is a different and much worse
#: number: two configured checkers times two sides made the real worst case four
#: times this, six minutes of gate time that nothing in the caller's view
#: predicted. Each run now draws from what is left of one deadline, and a run
#: that cannot start inside it does not start. Bigger than `lint_evidence`'s 30s
#: because a whole-project pyright pass is a different order of cost from a ruff
#: call, but it is still a cap: advisory evidence must never be the reason a
#: review stalls.
TYPE_TIMEOUT = 90

#: Cap on how many checkers run for one review. A repo configuring both a
#: Python checker and tsc is normal; anything past that is not worth gate time.
MAX_CHECKERS = 2

_PYRIGHT_TABLE_RE = re.compile(r"^\[tool\.pyright(\.[\w.-]+)?\]", re.MULTILINE)
_MYPY_TABLE_RE = re.compile(r"^\[tool\.mypy(\.[\w.-]+)?\]", re.MULTILINE)
_SETUP_CFG_MYPY_RE = re.compile(r"^\[mypy(-[\w.*-]+)?\]", re.MULTILINE)


@dataclass(frozen=True)
class TypeDiagnostic:
    """One diagnostic, normalised across the three checkers."""

    path: str  # repo-relative, POSIX separators
    line: int  # 1-based; 0 when the checker reported none
    column: int
    code: str  # pyright rule, mypy error code, or tsc TSxxxx
    message: str


@dataclass(frozen=True)
class TypeEvidence:
    """The outcome of one collection pass.

    `ran` is the load-bearing field. `ran=False` means no section is rendered,
    so the reviewer is told nothing about types — the correct output for "we
    could not check", because absence then carries no claim. `ran=True` with an
    empty `diagnostics` is a POSITIVE result the reviewer may use: the checker
    ran on both trees and this diff introduced nothing.
    """

    ran: bool = False
    checker: str = ""
    diagnostics: list[TypeDiagnostic] = field(default_factory=list)
    #: Total diagnostics the checker reports on the after tree, net-new or not.
    #: Rendered so that "net-new: 0" in a repo carrying 400 pre-existing errors
    #: reads as the result of the subtraction rather than as "clean repo".
    after_total: int = 0
    #: Import-resolution failures in the after run. A worktree has no installed
    #: dependencies, so a project whose imports live in `node_modules` or a
    #: `.venv` is analysed with those symbols degraded to Any — which SUPPRESSES
    #: real downstream errors on both sides equally. The subtraction stays
    #: sound, but its coverage does not, and a bare "net-new: 0" would read as a
    #: stronger claim than the run can support. Rendered as a coverage caveat
    #: whenever it is non-zero; see `format_type_evidence`.
    unresolved_imports: int = 0
    #: Why the collection produced nothing, when we can say. `ran=False` still
    #: means "no evidence", and this is never a claim about the code — it is a
    #: claim about US. Rendered so a reviewer reading a diff with no TYPE
    #: EVIDENCE section can tell "the checker could not start" apart from
    #: "this repo configures no checker"; see `format_type_evidence`.
    unavailable_reason: str = ""


@dataclass(frozen=True)
class _Checker:
    name: str
    argv: tuple[str, ...]
    #: Exit codes that mean "the checker ran and produced a verdict". Anything
    #: else (usage error, config error, crash, missing binary) is untrusted.
    ok_codes: tuple[int, ...]


# `.` is passed to mypy explicitly rather than relying on the repo's `files =`
# setting, so both runs cover the same tree even when the config names paths
# that exist on only one side of the diff.
_MYPY = _Checker(
    "mypy",
    ("mypy", "--no-error-summary", "--no-color-output", "--show-column-numbers", "."),
    (0, 1),
)
_PYRIGHT = _Checker("pyright", ("pyright", "--outputjson"), (0, 1))
_TSC = _Checker("tsc", ("tsc", "--noEmit", "--pretty", "false"), (0, 1, 2))

_CHECKERS_BY_NAME = {c.name: c for c in (_MYPY, _PYRIGHT, _TSC)}


def _setup_cfg_configures_mypy(repo_path: Path) -> bool:
    cfg = repo_path / "setup.cfg"
    if not cfg.is_file():
        return False
    try:
        text = cfg.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    return bool(_SETUP_CFG_MYPY_RE.search(text))


def detect_type_checkers(repo_path: Path) -> list[_Checker]:
    """Checkers the repo under review CONFIGURES, in run order.

    At most one Python checker: pyright wins when a repo configures both,
    because its JSON output carries a machine-readable rule name on every
    diagnostic where mypy's text format may carry none.

    Never guesses. A repo with no type-checker config returns [], and the caller
    then spawns nothing at all — no_human does not impose type checking on a
    project that has not asked for it, exactly as it does not impose ruff.
    """
    found: list[_Checker] = []
    try:
        pyproject = ""
        pyproject_file = repo_path / "pyproject.toml"
        if pyproject_file.is_file():
            pyproject = pyproject_file.read_text(encoding="utf-8", errors="ignore")

        if (repo_path / "pyrightconfig.json").is_file() or _PYRIGHT_TABLE_RE.search(
            pyproject
        ):
            found.append(_PYRIGHT)
        elif (
            (repo_path / "mypy.ini").is_file()
            or (repo_path / ".mypy.ini").is_file()
            or _MYPY_TABLE_RE.search(pyproject)
            or _setup_cfg_configures_mypy(repo_path)
        ):
            found.append(_MYPY)

        if (repo_path / "tsconfig.json").is_file():
            found.append(_TSC)
    except OSError:
        return []
    return found[:MAX_CHECKERS]


def _repo_relative(raw: str, root: Path) -> str:
    """`raw` as a path relative to `root`, POSIX-separated.

    Takes the root EXPLICITLY because the base run's root is the temporary
    worktree, not the repo. Relativising both sides against the repo would
    leave every base path absolute, no fingerprint would ever match, and every
    pre-existing diagnostic would read as net-new — the exact false positive
    this module's whole design exists to avoid.

    Returns the input (slash-normalised) when it cannot be placed inside
    `root`, so an unexpected shape degrades to a non-matching key rather than
    an exception.
    """
    if not raw:
        return raw
    try:
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = root / candidate
        resolved_root = Path(os.path.realpath(str(root)))
        resolved = Path(os.path.realpath(str(candidate)))
        return resolved.relative_to(resolved_root).as_posix()
    except (ValueError, OSError):
        return raw.replace("\\", "/")


# --------------------------------------------------------------------------- #
# Parsers — one per checker, each returning normalised diagnostics             #
# --------------------------------------------------------------------------- #

# `path:line:col: error: message  [code]`. mypy also emits `note:` lines, which
# are commentary attached to a preceding error and are not diagnostics of their
# own; only `error` is collected.
_MYPY_LINE_RE = re.compile(
    r"^(?P<path>.+?):(?P<line>\d+):(?:(?P<col>\d+):)?\s*"
    r"(?P<severity>error|warning):\s*(?P<message>.*?)"
    r"(?:\s+\[(?P<code>[\w-]+)\])?$"
)

# `path(line,col): error TS2322: message`
_TSC_LINE_RE = re.compile(
    r"^(?P<path>.+?)\((?P<line>\d+),(?P<col>\d+)\):\s*"
    r"(?P<severity>error|warning)\s+(?P<code>TS\d+):\s*(?P<message>.*)$"
)


def parse_mypy(output: str, root: Path) -> list[TypeDiagnostic]:
    out: list[TypeDiagnostic] = []
    for raw in output.splitlines():
        m = _MYPY_LINE_RE.match(raw.strip())
        if not m or m.group("severity") != "error":
            continue
        out.append(
            TypeDiagnostic(
                path=_repo_relative(m.group("path"), root),
                line=int(m.group("line")),
                column=int(m.group("col") or 0),
                code=m.group("code") or "",
                message=(m.group("message") or "").strip(),
            )
        )
    return out


def parse_tsc(output: str, root: Path) -> list[TypeDiagnostic]:
    out: list[TypeDiagnostic] = []
    for raw in output.splitlines():
        m = _TSC_LINE_RE.match(raw.strip())
        if not m or m.group("severity") != "error":
            continue
        out.append(
            TypeDiagnostic(
                path=_repo_relative(m.group("path"), root),
                line=int(m.group("line")),
                column=int(m.group("col")),
                code=m.group("code"),
                message=(m.group("message") or "").strip(),
            )
        )
    return out


def parse_pyright(output: str, root: Path) -> list[TypeDiagnostic]:
    """Parse `pyright --outputjson`.

    pyright's `range.start.line` and `.character` are ZERO-based; every other
    surface in this module (and every diagnostic a human reads) is 1-based, so
    they are converted here rather than at the point of display.
    """
    data = json.loads(output or "{}")
    if not isinstance(data, dict):
        return []
    items = data.get("generalDiagnostics")
    if not isinstance(items, list):
        return []
    out: list[TypeDiagnostic] = []
    for item in items:
        if not isinstance(item, dict) or item.get("severity") != "error":
            continue
        start = ((item.get("range") or {}).get("start")) or {}
        try:
            line = int(start.get("line", -1)) + 1
            column = int(start.get("character", -1)) + 1
        except (TypeError, ValueError):
            line, column = 0, 0
        out.append(
            TypeDiagnostic(
                path=_repo_relative(str(item.get("file") or ""), root),
                line=max(line, 0),
                column=max(column, 0),
                code=str(item.get("rule") or ""),
                message=str(item.get("message") or "").strip(),
            )
        )
    return out


_PARSERS = {"mypy": parse_mypy, "pyright": parse_pyright, "tsc": parse_tsc}


# --------------------------------------------------------------------------- #
# Fingerprinting and the subtraction                                           #
# --------------------------------------------------------------------------- #

_WS_RE = re.compile(r"\s+")
# A message that quotes a line/column of its own, or an absolute path, would
# otherwise defeat the line-agnostic key.
_DIGIT_RUN_RE = re.compile(r"\d+")


def _normalise_message(message: str) -> str:
    """Collapse whitespace and blank out digit runs.

    Digits go because a message like "Argument 2 to ..." is stable but
    "line 41" inside a checker's prose is not: a diff that shifts a file makes
    the same pre-existing diagnostic re-render with a different number, and a
    key that keeps it reads that diagnostic as net-new. Blanking digits can only
    MERGE two distinct diagnostics into one key, which under-reports; keeping
    them would SPLIT one diagnostic into two, which over-reports. Under-report
    is the correct direction for advisory evidence.
    """
    return _DIGIT_RUN_RE.sub("#", _WS_RE.sub(" ", message.strip()))


def _fingerprint(d: TypeDiagnostic) -> tuple[str, str, str]:
    """The line-agnostic identity of a diagnostic. See the module docstring."""
    return (d.path, d.code, _normalise_message(d.message))


# Import-resolution failures, per checker. Their presence at base but not after
# means the base tree was analysed without the dependencies the after tree has,
# so the two runs are not comparable — see `_environments_comparable`.
_UNRESOLVED_IMPORT_CODES = {
    "reportMissingImports",
    "reportMissingModuleSource",
    "import-not-found",
    "import-untyped",
    "TS2307",
}
_UNRESOLVED_IMPORT_TEXT_RE = re.compile(
    r"could not be resolved|cannot find module|cannot find name 'require'"
    r"|library stubs not installed|has no attribute '__file__'",
    re.IGNORECASE,
)


def _unresolved_import_count(diagnostics: list[TypeDiagnostic]) -> int:
    return sum(
        1
        for d in diagnostics
        if d.code in _UNRESOLVED_IMPORT_CODES
        or _UNRESOLVED_IMPORT_TEXT_RE.search(d.message)
    )


def _environments_comparable(
    base: list[TypeDiagnostic], after: list[TypeDiagnostic]
) -> bool:
    """False when the base tree was analysed in a poorer environment.

    A `git worktree` at the merge base holds tracked files only: no `.venv`, no
    `node_modules`, no generated stubs. A checker there fails to resolve imports
    it resolves fine on the after tree, and — this is the damaging part — an
    unresolvable import makes a checker fall back to `Any`, which SUPPRESSES the
    real downstream diagnostics that the after tree still reports. The
    subtraction then attributes a pile of pre-existing errors to the diff.

    The signature of that state is import-resolution diagnostics at base that
    the after run does not have. When it is present we decline to produce
    evidence rather than produce evidence we know is inflated.
    """
    return _unresolved_import_count(base) <= _unresolved_import_count(after)


def net_new(
    base: list[TypeDiagnostic],
    after: list[TypeDiagnostic],
    changed_lines: dict[str, set[int]] | None = None,
) -> list[TypeDiagnostic]:
    """Diagnostics present at `after` and not accounted for at `base`.

    Multiset subtraction over line-agnostic fingerprints: a fingerprint the
    after run reports 3 times and the base run reports 1 time contributes 2.

    WHICH concrete occurrences get reported is a genuine choice, because the
    fingerprint deliberately cannot tell two occurrences apart. Occurrences on
    lines this diff touched are preferred, since those are the ones most likely
    to be the new ones and are the most useful location to hand a reviewer;
    ties break on (line, column) so the output is deterministic regardless of
    the order the checker emitted.
    """
    surplus = Counter(_fingerprint(d) for d in after)
    surplus.subtract(Counter(_fingerprint(d) for d in base))

    by_fingerprint: dict[tuple[str, str, str], list[TypeDiagnostic]] = {}
    for d in after:
        by_fingerprint.setdefault(_fingerprint(d), []).append(d)

    picked: list[TypeDiagnostic] = []
    for key, count in surplus.items():
        if count <= 0:
            continue
        candidates = sorted(
            by_fingerprint.get(key, ()),
            key=lambda d: (
                0 if d.line in (changed_lines or {}).get(d.path, ()) else 1,
                d.line,
                d.column,
            ),
        )
        picked.extend(candidates[:count])
    picked.sort(key=lambda d: (d.path, d.line, d.column, d.code, d.message))
    return picked


# --------------------------------------------------------------------------- #
# Running the checkers                                                         #
# --------------------------------------------------------------------------- #

#: Bound on the worktree cleanup calls in `_run_at_commit`'s `finally`. They are
#: small git reads, and a `finally` is the worst place in the module to leave an
#: unbounded wait.
WORKTREE_CLEANUP_TIMEOUT = 20

#: Checker results, keyed by (checker, resolved SHA, realpath of the repo the
#: worktree was built from). BOTH sides go through it: a task's review rounds
#: all share one merge base, so without this every round pays again to check a
#: tree that cannot have changed, and a re-review of an unchanged head is free
#: on the after side too.
#:
#: Keyed on a RESOLVED sha, never on the symbolic ref the caller passed — a
#: branch name moves between rounds and a cache keyed on it would serve a stale
#: result for a tree that did change.
#:
#: Only SUCCESSFUL runs are cached. Memoising a failure would turn one timeout
#: into permanent silence for that commit for the rest of the process, and a
#: timeout is exactly the failure most likely to go the other way next round.
_RESULT_CACHE: OrderedDict[tuple[str, str, str], list[TypeDiagnostic]] = OrderedDict()
_RESULT_CACHE_MAX = 32


def clear_result_cache() -> None:
    """Drop every cached checker run. For tests, and for a caller that knows the
    trees on disk have been rewritten under it."""
    _RESULT_CACHE.clear()


def _cache_put(key: tuple[str, str, str], value: list[TypeDiagnostic]) -> None:
    _RESULT_CACHE[key] = value
    _RESULT_CACHE.move_to_end(key)
    while len(_RESULT_CACHE) > _RESULT_CACHE_MAX:
        _RESULT_CACHE.popitem(last=False)


def _resolve_binary(name: str, repo_path: Path) -> str | None:
    """Absolute path to `name` on no_human's OWN PATH, or None.

    Resolved with `shutil.which` rather than left to `subprocess` for a reason
    found by running a real checker, which no amount of mocked output could have
    shown: on Windows the npm-installed checkers are `tsc.cmd`/`pyright.cmd`
    shims, and `CreateProcess` does not apply `PATHEXT`, so
    `subprocess.run(["tsc", ...])` raises `FileNotFoundError` even though `tsc`
    runs fine in the same shell. That exception is indistinguishable here from
    "the checker is not installed", so the whole collector went permanently
    silent on Windows — the platform this product ships a bundle for. Handing
    subprocess the resolved absolute path executes the shim correctly.

    A resolution that lands INSIDE the repo under review is refused. `which`
    does not search the working directory on current CPython, but it did on
    Windows in older versions, and this module's contract is that nothing it
    executes is chosen by the repo being reviewed — enforced here rather than
    asserted in a docstring.
    """
    found = shutil.which(name)
    if not found:
        return None
    try:
        resolved = Path(os.path.realpath(found))
        if resolved.is_relative_to(Path(os.path.realpath(str(repo_path)))):
            log.warning(
                "refusing to run %s resolved to %s — inside the repo under review",
                name, found,
            )
            return None
    except (OSError, ValueError):
        return None
    return found


def _checker_env() -> dict[str, str]:
    """This process's environment with every secret-shaped variable removed.

    A type checker needs `PATH`, `HOME` and a proxy; it has no business with a
    credential. Inheriting one is not hypothetical here: `mypy` IMPORTS the
    modules a `plugins =` line names, and it reads that line from the config of
    the repo under review — so with a bare `os.environ` the reviewed repo's own
    code runs holding this process's `CLAUDE_CODE_OAUTH_TOKEN`. Phase 2 makes
    that sharper by firing the same channel once per edit, in the live
    worktree, against a config the coder can write mid-attempt.

    `drop_foreign_secrets` with an empty keep-list is the repo's existing rule
    (`agent/child_env.py`), reused rather than restated: it drops by name shape
    and by password-bearing URL value, and keeps the operational names — `PATH`,
    `HOME`, `AWS_REGION`, the `*_PROXY` set the pyright launcher needs to reach
    npm through a corporate network.
    """
    from ..agent.child_env import drop_foreign_secrets

    env = os.environ.copy()
    drop_foreign_secrets(env, keep=())
    return env


#: mypy's wording when `plugins =` names a module it cannot import.
_PLUGIN_IMPORT_RE = re.compile(r"error importing plugin", re.IGNORECASE)

#: What the reviewer is told when that happens. The scrub is a live cause of
#: it, and silence here would hide a collector that went dark for a reason we
#: chose. Named rather than inlined because `docs/verification.md` quotes it.
_PLUGIN_ENV_NOTE = (
    "the repo's own type-checker plugin failed to import. `_checker_env` "
    "removes every secret-shaped variable before spawning the checker, and a "
    "plugin that reads one (a settings module reaching for SECRET_KEY or "
    "DATABASE_URL is the common shape) cannot load without it, so NO type "
    "evidence was collected for this diff"
)


def _run_checker(
    checker: _Checker, cwd: Path, *, timeout: int, repo_path: Path | None = None,
    notes: list[str] | None = None,
) -> list[TypeDiagnostic] | None:
    """Run one checker in `cwd`. `None` means the run cannot be trusted.

    `None` — never `[]` — is the failure value, and the distinction is the whole
    point: `[]` says "the checker ran and found nothing", which is a claim about
    the code, while `None` says "we learned nothing", which is a claim about
    ourselves. Collapsing them is how a crashed checker becomes a clean bill of
    health.
    """
    binary = _resolve_binary(checker.argv[0], repo_path if repo_path else cwd)
    if binary is None:
        # Expected and not an error: this module never installs anything.
        log.info("%s is not available on PATH; no type evidence", checker.name)
        return None
    try:
        proc = subprocess.run(
            [binary, *checker.argv[1:]],
            cwd=cwd,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            env=_checker_env(),
        )
    except subprocess.TimeoutExpired:
        log.warning("%s type check timed out after %ds in %s", checker.name, timeout, cwd)
        return None
    except OSError:
        # `_resolve_binary` already handled "not installed"; reaching here means
        # the resolved path would not execute (permissions, a broken shim).
        log.warning("%s could not be executed; no type evidence", checker.name, exc_info=True)
        return None

    if proc.returncode not in checker.ok_codes:
        log.warning(
            "%s exited %d: %s",
            checker.name,
            proc.returncode,
            (proc.stderr or "")[:500],
        )
        if notes is not None and _PLUGIN_IMPORT_RE.search(
            (proc.stdout or "") + " " + (proc.stderr or "")
        ):
            notes.append(_PLUGIN_ENV_NOTE)
        return None

    try:
        diagnostics = _PARSERS[checker.name](proc.stdout or "", cwd)
    except (ValueError, TypeError, KeyError):
        log.warning("%s output did not parse; no type evidence", checker.name)
        return None

    # A non-zero exit says the checker HAS diagnostics. Parsing none out of that
    # run means we are not reading the format we think we are — a changed output
    # shape, a wrapper writing to stderr — and a silent zero would then be
    # indistinguishable from a genuinely clean tree.
    if proc.returncode != 0 and not diagnostics:
        log.warning(
            "%s exited %d but no diagnostic parsed; treating output as untrusted",
            checker.name,
            proc.returncode,
        )
        return None
    return diagnostics


def _resolve_sha(repo_path: Path, ref: str, *, timeout: int = 20) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--verify", f"{ref}^{{commit}}"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    sha = (proc.stdout or "").strip()
    return sha or None


def _run_at_commit(
    checker: _Checker, repo_path: Path, sha: str, *, timeout: int,
    notes: list[str] | None = None,
) -> list[TypeDiagnostic] | None:
    """Run `checker` over a throwaway worktree checked out at `sha`.

    BOTH sides of the subtraction come through here, and the after side does so
    for a reason that is not symmetry-for-its-own-sake. This collector runs
    inside `reviewer.review`, which the orchestrator brackets between
    `reviewer_worktree.snapshot` and `reviewer_worktree.compare`. A checker
    pointed at the attempt's own tree writes into it — mypy drops `.mypy_cache/`
    at its default location, `tsc` with `incremental`/`composite` writes
    `*.tsbuildinfo` — and in a repo that does not gitignore those, `compare`
    sees an added path, the orchestrator emits `reviewer_wrote`, reverts, and
    replaces a real verdict with an integrity failure at full reviewer cost.
    The reviewer would be blamed for a write it did not make. Running in a
    throwaway worktree means every byte the checker writes lands somewhere we
    are about to delete.

    It also fixes a second defect for free: the attempt's working tree can hold
    UNTRACKED files, which exist on the after side and can never exist at base,
    so their diagnostics were unconditionally net-new. A worktree at a commit
    carries tracked files only, on both sides, so the two runs finally see
    comparable file sets.

    A worktree rather than a `git stash`/`git checkout` dance because the tree
    under review belongs to the attempt: the review gate must not mutate it, and
    a failure partway through a checkout would leave it mutated. `repro_gate`
    builds its base tree the same way.
    """
    key = (checker.name, sha, os.path.realpath(str(repo_path)))
    if key in _RESULT_CACHE:
        _RESULT_CACHE.move_to_end(key)
        return _RESULT_CACHE[key]

    tmp = Path(tempfile.mkdtemp(prefix="nh-typeevid-"))
    started = time.monotonic()
    worktree = tmp / "tree"
    try:
        added = subprocess.run(
            ["git", "worktree", "add", "--detach", str(worktree), sha],
            cwd=repo_path,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
        )
        if added.returncode != 0:
            log.warning(
                "could not build the type-check worktree at %s: %s",
                sha,
                (added.stderr or "").strip()[:300],
            )
            return None
        # `repo_path`, not the worktree: the binary guard is about the repo
        # under review, and the worktree is a checkout of that same repo.
        # The checkout above already spent part of this call's budget, and on
        # a large repository that is seconds, not milliseconds. Passing the
        # WHOLE `timeout` again here let one `_run_at_commit` spend up to twice
        # the deadline the caller set, which made TYPE_TIMEOUT's "cap for the
        # WHOLE collection" untrue (found on review of PR #164).
        left = int(timeout - (time.monotonic() - started))
        if left <= 0:
            log.warning(
                "%s: the %ss budget was spent checking out %s; no run",
                checker.name, timeout, sha[:9],
            )
            return None
        result = _run_checker(
            checker, worktree, timeout=left, repo_path=repo_path, notes=notes,
        )
        if result is not None:
            _cache_put(key, result)
        return result
    except (subprocess.TimeoutExpired, OSError):
        log.warning("type-check worktree at %s failed", sha, exc_info=True)
        return None
    finally:
        # Timed, unlike before: this is the one subprocess in the module that
        # could hang without a bound, and it runs in a `finally` — a wedged
        # `git worktree remove` would hold the review open indefinitely.
        # `prune` after it clears the registration a killed run would otherwise
        # leave behind in `.git/worktrees`.
        try:
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(worktree)],
                cwd=repo_path, capture_output=True, timeout=WORKTREE_CLEANUP_TIMEOUT,
            )
            subprocess.run(
                ["git", "worktree", "prune"],
                cwd=repo_path, capture_output=True, timeout=WORKTREE_CLEANUP_TIMEOUT,
            )
        except (subprocess.TimeoutExpired, OSError):
            log.warning("could not clean up the type-check worktree at %s", worktree)
        shutil.rmtree(tmp, ignore_errors=True)


def collect_type_evidence(
    repo_path: Path,
    before_ref: str,
    after_ref: str,
    *,
    timeout: int = TYPE_TIMEOUT,
) -> TypeEvidence:
    """Net-new type diagnostics this diff introduced, or `ran=False`.

    `ran=False` on: no configured checker, a checker not on PATH, either ref
    that does not resolve, either run untrusted (bad exit code, timeout,
    unparseable output), a worktree that could not be built, or a base
    environment the after environment does not match. Never raises into the
    review gate.

    BOTH runs happen in throwaway worktrees, never in `repo_path` itself — see
    `_run_at_commit` for the integrity-guard defect that forces it.

    When more than one checker is configured, the FIRST one that produces a
    usable pair of runs wins. Reporting two checkers' diagnostics side by side
    would double-count the same defect in a repo that runs both over the same
    files, and the reviewer has no way to tell that from two distinct defects.
    """
    try:
        checkers = detect_type_checkers(repo_path)
        # A repo can CONFIGURE a checker that is not installed here, and that
        # is the common case for a fleet running many repos. Resolving the
        # binary first costs one `shutil.which`; discovering it inside
        # `_run_at_commit` instead costs a full `git worktree add` and its
        # removal FIRST, on every review round, forever — seconds on a real
        # monorepo, for a run that was always going to report nothing.
        checkers = [
            c for c in checkers if _resolve_binary(c.argv[0], repo_path)
        ]
        if not checkers:
            return TypeEvidence()
        base_sha = _resolve_sha(repo_path, before_ref)
        after_sha = _resolve_sha(repo_path, after_ref)
        if base_sha is None or after_sha is None:
            log.warning(
                "type evidence: ref does not resolve (base %r -> %s, after %r -> %s)",
                before_ref, base_sha, after_ref, after_sha,
            )
            return TypeEvidence()

        # Reported locations are preferred on lines the diff touched; an empty
        # map costs only ordering, never correctness, so its failure is ignored.
        changed = changed_line_numbers(repo_path, before_ref, after_ref)

        # ONE budget for the whole collection, not one per run. `timeout` used
        # to be a per-run cap, so the worst case was checkers x sides x cap —
        # two checkers at 90s each ran to six minutes of gate time, unbounded by
        # anything the caller could see. The deadline is shared: every run gets
        # whatever is left, and when nothing is left the collection stops with
        # what it has rather than starting a run it cannot finish.
        deadline = time.monotonic() + timeout
        #: Reasons a run produced nothing that are worth telling the reviewer
        #: rather than leaving in a `log.warning` nobody reads.
        notes: list[str] = []

        def _remaining() -> int:
            return int(deadline - time.monotonic())

        for checker in checkers:
            if _remaining() <= 0:
                log.warning(
                    "type evidence: %ds budget spent before %s could run",
                    timeout, checker.name,
                )
                break
            after = _run_at_commit(
                checker, repo_path, after_sha, timeout=_remaining(), notes=notes,
            )
            if after is None or _remaining() <= 0:
                continue
            base = _run_at_commit(
                checker, repo_path, base_sha, timeout=_remaining(), notes=notes,
            )
            if base is None:
                continue
            if not _environments_comparable(base, after):
                log.warning(
                    "%s: base tree resolved fewer imports than the after tree; "
                    "the two runs are not comparable, so no type evidence",
                    checker.name,
                )
                continue
            return TypeEvidence(
                ran=True,
                checker=checker.name,
                diagnostics=net_new(base, after, changed),
                after_total=len(after),
                unresolved_imports=_unresolved_import_count(after),
            )
        return TypeEvidence(unavailable_reason=notes[0] if notes else "")
    except Exception:  # noqa: BLE001 — advisory evidence, never blocks the review
        log.warning("type evidence failed", exc_info=True)
        return TypeEvidence()


# --------------------------------------------------------------------------- #
# Rendering                                                                    #
# --------------------------------------------------------------------------- #

# Hard caps on the rendered block, same rationale as `lint_evidence`: a diff
# that breaks a widely-imported signature can produce hundreds of net-new
# diagnostics, and this is advisory evidence, not the diff itself.
#: Opening words of the one block `format_type_evidence` renders for a run
#: that did NOT happen. Exported because `reviewer.py`'s READING SCOPE
#: enumerates what the prompt carries by testing these strings for emptiness,
#: and a non-empty NOT-COLLECTED block would otherwise make it announce "the
#: net-new type diagnostics" for a check that never ran — the exact false
#: assurance this module's ceilings forbid.
NOT_COLLECTED_PREFIX = "TYPE EVIDENCE: NOT COLLECTED"

MAX_TYPE_DIAGNOSTICS = 40
MAX_TYPE_BYTES = 8192


def format_type_evidence(evidence: TypeEvidence) -> str:
    """Render the labeled block for the reviewer prompt; "" when it did not run.

    The empty string for `ran=False` is the contract that makes this evidence
    safe: the reviewer is then told NOTHING about types, so it cannot read our
    silence as a pass. A run that happened and found nothing renders "net-new:
    0" instead, which is a fact the reviewer may use.

    A run whose imports did not resolve renders that count alongside. Both sides
    are checked out from a commit and therefore carry no installed dependencies,
    so `tsc` and `pyright` on a project with real dependencies analyse it with
    those symbols degraded to Any. The subtraction is still sound — the same
    blindness applies to both runs — but its COVERAGE is not, and "net-new: 0"
    with a third of the file unanalysable is a weaker claim than the same words
    over a clean run. Saying so is the difference between advisory evidence and
    a false clean.
    """
    if not evidence.ran:
        if not evidence.unavailable_reason:
            return ""
        # A reason, never a result. `ran=False` still renders no net-new count
        # and no diagnostics; what this adds is the difference between "this
        # repo configures no checker" and "the checker could not start", which
        # a reviewer otherwise cannot tell apart from an absent section. It is
        # worded so it cannot be read as evidence about the code.
        return (
            f"{NOT_COLLECTED_PREFIX} — "
            f"{evidence.unavailable_reason}. This says nothing about whether "
            "the diff is type-clean; it says this check did not run."
        )
    count = len(evidence.diagnostics)
    header = (
        f"TYPE EVIDENCE ({evidence.checker}, deterministic): net-new type "
        f"diagnostics introduced by this diff: {count}.\n"
        f"  Computed by running {evidence.checker} over the whole project at the "
        f"merge base and at this tree and subtracting; the after tree carries "
        f"{evidence.after_total} diagnostic(s) in total, so a pre-existing error "
        f"is not counted here."
    )
    if evidence.unresolved_imports:
        header += (
            f"\n  COVERAGE LIMIT: {evidence.unresolved_imports} of those are "
            "unresolved-import diagnostics. Both trees are checked out from a "
            "commit and carry no installed dependencies, so anything reached "
            "THROUGH an unresolved import is analysed as Any and its errors are "
            "invisible to this check — on both sides. Read the count below as "
            "'what the checker could still see', not as the whole picture."
        )
    # A STANDING limitation, stated on every run, in the same register as the
    # "Evidence, not a verdict" line below — not a warning that has to guess
    # when to appear. Three rounds of review went into this sentence and the
    # first two attempts were wrong the same way: they tried to signal the bad
    # case from data that cannot identify it. A file the checker never
    # analysed and a file it analysed and found clean BOTH produce no
    # diagnostic, so no count derived from the diagnostics can tell them apart
    # — the first attempt's caveat and the second's "reported on N of M"
    # number rendered byte-identically for the two while sounding like a
    # measurement of coverage. Establishing that a file WAS analysed needs the
    # checker's own file list, i.e. a second invocation this collector
    # deliberately does not pay for. So say the true thing, always.
    header += (
        "\n  SCOPE: a checker analyses only what the repo's own config admits "
        "(`include`/`exclude` in tsconfig.json or pyrightconfig.json, "
        "`exclude` under [tool.mypy] — `files` is defeated by the explicit "
        "path this collector passes), and a diff can land entirely "
        "outside that. Nothing in this run distinguishes a changed file that "
        "was analysed and is clean from one that was never analysed, so read "
        "the count above as a fact about the checker's configured scope, not "
        "as a statement that this diff was type-checked."
    )
    if count == 0:
        return (
            header
            + "\n  Evidence, not a verdict: a type checker cannot see a defect "
            "the types already permit."
        )

    lines = [header]
    size = len(header)
    shown = 0
    for d in evidence.diagnostics:
        if shown >= MAX_TYPE_DIAGNOSTICS:
            break
        loc = f"{d.path}:{d.line}" if d.line else d.path
        if d.line and d.column:
            loc = f"{loc}:{d.column}"
        code = f" {d.code}" if d.code else ""
        line = f"  {loc}{code} {d.message}".rstrip()
        if size + 1 + len(line) > MAX_TYPE_BYTES:
            break
        lines.append(line)
        size += 1 + len(line)
        shown += 1
    remaining = count - shown
    if remaining > 0:
        lines.append(f"  ... truncated ({remaining} more net-new diagnostics)")
    lines.append(
        "  Evidence, not a verdict: judge whether each is a defect this diff "
        "introduced or an acceptable consequence of the change."
    )
    return "\n".join(lines)
