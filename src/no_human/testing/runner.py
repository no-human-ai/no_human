"""Local test runner + git-backed snapshots for the tamper guard.

Phase 0 runs the existing suite locally and reports pass/fail with the raw
output as evidence (no assertions without the command + output). CI triggering
arrives in Phase 3.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from ..proc import hidden_console_kwargs
from . import tamper_guard

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable


log = logging.getLogger(__name__)

# Read the platform through a constant, never an inline `os.name` test, so the
# Windows branches below are reachable from a test on any host.
_IS_WINDOWS = os.name == "nt"

# POSIX: `start_new_session` puts the child in its own group so a timeout can
# kill the whole tree. Windows: CREATE_NEW_PROCESS_GROUP is the nearest
# equivalent (detaches from our console so a Ctrl-C to `nh` does not also hit
# it) AND CREATE_NO_WINDOW so the pytest child spawns no visible console when
# the desktop app launched nh without one. See no_human.proc.
_NEW_GROUP_KWARGS: dict[str, object] = hidden_console_kwargs(new_group=True)


def _kill_process_tree(proc: "subprocess.Popen") -> bool:
    """Kill *proc* AND its descendants. True only if the TREE kill succeeded.

    POSIX: ``killpg`` over the session ``start_new_session=True`` created.

    Windows: ``os.killpg`` and ``os.getpgid`` DO NOT EXIST, so this path raised
    AttributeError and a wedged test run could never be reaped — the exact
    orphaned-xdist-worker failure the POSIX branch was written to prevent.
    ``taskkill /F /T`` walks the tree instead; CREATE_NEW_PROCESS_GROUP alone
    would not, because a Windows process group is not a kill target.
    UNTESTED ON WINDOWS.
    """
    if _IS_WINDOWS:
        try:
            done = subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True, timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return done.returncode == 0
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        return False
    return True


@dataclass
class TestRunResult:
    ran: bool
    ok: bool
    passed: int
    failed: int
    errors: int
    command: str
    output: str
    invocation_error: bool = False
    failing_tests: list[str] = field(default_factory=list)
    traceback_excerpts: dict[str, str] = field(default_factory=dict)
    # WHICH tests passed, not how many. `passed` is a COUNT parsed from a
    # summary line that may belong to another framework entirely (a compound
    # `node --test … && … pytest …` command reports node's TAP tally), so it
    # cannot answer "did THIS id run?". Only populated when the output actually
    # carries pytest's `PASSED <nodeid>` short-summary lines, i.e. when the
    # command asked for them (`-rA`/`-rP`) — an empty list means "not reported",
    # never "nothing passed", so every consumer must treat it as evidence that
    # is present or absent, not as a count.
    passed_tests: list[str] = field(default_factory=list)
    # Node TAP `not ok` blocks, parsed off the FULL captured output for the
    # same reason as `passed_tests` above: the `[-8000:]` tail every branch
    # below carries drops the failing blocks of any suite with a few hundred
    # tests after them (the incident this exists for: 417 tests, failures at
    # ordinals 182/343, 35,642 and 11,284 bytes from the end respectively —
    # see tests/test_missing_prereq_env_classification.py's `_incident_tap`
    # / `test_the_incident_signatures_are_outside_the_eight_kilobyte_tail`,
    # both measured, round-4 review MINOR-3). `failing_tests` is populated
    # for node too (`_node_tap_failing_tests`, off the same full output), but
    # each id is only the `path::name` pair a `not ok` line and its
    # `location:` diagnostic yield — not the surrounding assertion text, and
    # never truncation-safe on its own. This field stays the ONLY place a
    # node failure's own diagnostic text survives truncation —
    # `prerequisite_reason_for` reads this, never `.output` or `failing_tests`.
    failure_blocks: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        if not self.ran:
            return "no tests run"
        return f"{'PASS' if self.ok else 'FAIL'}: {self.passed} passed, {self.failed} failed, {self.errors} errors"

    @property
    def traceback_block(self) -> str:
        """Human-readable, untruncated rendering of ``traceback_excerpts``."""
        return render_traceback_excerpts(self.traceback_excerpts)


def _dir_has_python(d: Path) -> bool:
    """True if *d* contains at least one .py file (two levels deep)."""
    return bool(list(d.glob("*.py")) or list(d.glob("*/*.py")))


def _looks_like_pytest(repo_path: Path) -> bool:
    """Detect a pytest project, including polyglot repos that also carry a
    pom.xml/package.json but whose tests are really Python (e.g. tests live
    under src/tests with a root pytest.ini). Checks shallow, well-known
    locations only — never a deep recursive walk on a large repo."""
    # Strong markers — these files only exist in Python projects.
    for marker in ("pytest.ini", "tox.ini", "conftest.py"):
        if (repo_path / marker).exists():
            return True
    # pyproject.toml / setup.cfg / setup.py are Python-specific only when they
    # actually contain Python packaging metadata, but as a heuristic they are
    # strong enough when there is NO competing ecosystem marker.
    if not (repo_path / "package.json").exists():
        for marker in ("pyproject.toml", "setup.cfg", "setup.py"):
            if (repo_path / marker).exists():
                return True
    # Test directories — only count if they contain at least one .py file.
    for tdir in ("tests", "test", "src/tests", "src/test"):
        d = repo_path / tdir
        if d.is_dir() and _dir_has_python(d):
            return True
    if list(repo_path.glob("src/conftest.py")) \
            or list(repo_path.glob("src/*/conftest.py")):
        return True
    for req in repo_path.glob("requirements*.txt"):
        try:
            if "pytest" in req.read_text(errors="ignore").lower():
                return True
        except OSError:
            pass
    return False


def _declares_xdist(repo_path: Path) -> bool:
    """True when the repo declares pytest-xdist (pyproject or uv.lock).
    File reads only, like every other check in this module."""
    for name in ("pyproject.toml", "uv.lock"):
        try:
            if "pytest-xdist" in (repo_path / name).read_text(errors="ignore"):
                return True
        except OSError:
            pass
    return False


def detect_command(repo_path: Path) -> str | None:
    """Best-effort test command detection.

    Pytest is checked first via explicit markers so a polyglot repo that also
    ships a pom.xml (a Java build alongside a Python test suite — an observed
    shape, not a hypothetical) isn't misrouted to ``mvn``.
    """
    repo_path = Path(repo_path)
    if _looks_like_pytest(repo_path):
        if (repo_path / "uv.lock").exists():
            # Parallelize only when the repo itself declares pytest-xdist:
            # `uv run` syncs the default groups from the lock, so the plugin
            # is present in every ordinary layout (a non-default-group or
            # platform-marked declaration can still miss — that lands in
            # _is_invocation_error and the -n-less retry, an honest failure,
            # never a false pass). A bare `pytest` environment carries no
            # guarantee at all, so the non-uv path below stays serial. Fixed
            # worker count: `-n auto` has wedged on large suites.
            if _declares_xdist(repo_path):
                return "uv run pytest -q -n 4"
            return "uv run pytest -q"
        return "pytest -q"
    if (repo_path / "package.json").exists():
        return "npm test --silent"
    if (repo_path / "pom.xml").exists():
        return "mvn -q test"
    return None


_PYTEST_SUMMARY = re.compile(r"(\d+) passed|(\d+) failed|(\d+) error")

# The line pytest prints its final tally on (plain "5 passed, 2 failed in
# 1.2s" or "=" bar-padded "===== 2 failed, 40 passed in 3.45s ====="). Text
# printed AFTER this line — e.g. an atexit/session-finish traceback from a
# pytest-xdist teardown race — never contains a bare "N passed/failed/error"
# fragment today, so anchoring to the LAST matching line is a no-op for every
# known-good output and a hard backstop against a future stray digit+keyword
# match (a traceback mentioning a count) being misread as the real tally.
_PYTEST_SUMMARY_LINE = re.compile(r"^.*\b\d+\s+(?:passed|failed|error(?:s)?)\b.*$", re.M)


def _parse_pytest(output: str) -> tuple[int, int, int]:
    lines = _PYTEST_SUMMARY_LINE.findall(output)
    text = lines[-1] if lines else output
    passed = failed = errors = 0
    for m in re.finditer(r"(\d+)\s+(passed|failed|error[s]?)", text):
        n, kind = int(m.group(1)), m.group(2)
        if kind == "passed":
            passed = n
        elif kind == "failed":
            failed = n
        else:
            errors = n
    return passed, failed, errors


# The captured id must contain "::" — a real pytest node id always does.
# Review 2026-07-25: without it, any log line shaped "ERROR <logger> - msg"
# (log_cli repos) minted a phantom "failing test", which ALSO defeated the
# teardown-race INFRA classifier via its `not failing_tests` gate.
_PYTEST_FAILED_ID = re.compile(
    r"^(?:FAILED|ERROR)\s+(\S+::\S+?)(?:\s+-.*)?$", re.M)


_PYTEST_PASSED_ID = re.compile(r"^PASSED\s+(\S+::\S+?)\s*$", re.M)

# 🔴 THE ONLY PART OF THE OUTPUT THE TESTS THEMSELVES CANNOT WRITE.
# Everything above this bar — the FAILURES blocks, and under `-rA` the PASSES
# blocks with each passing test's `Captured stdout call` echoed verbatim — is
# text the code under test can author. A test containing
# `print("PASSED tests/test_x.py::test_x")` puts a line matching
# `_PYTEST_PASSED_ID` at column 0 in that echo (verified against real output),
# so an unanchored scan hands the change under test a way to MINT identities
# for tests that never ran, and `_flaky_on_rerun`'s identity accounting — the
# whole point of which is that only pytest may say what ran — would believe it.
# Anchoring to the LAST bar also defeats a forged bar line, because captured
# stdout is always printed before the real summary section — the same ordering
# assumption, with the same atexit-output caveat, that `_PYTEST_SUMMARY_LINE`
# already rests on.
_SHORT_SUMMARY_BAR = re.compile(r"^=+ short test summary info =+\s*$", re.M)


def _short_summary_section(output: str) -> str | None:
    """The text after the LAST ``short test summary info`` bar, or None if the
    run printed no such section (``-rN``, ``--no-summary``, or a green run with
    no ``-r`` flags — pytest prints the bar for failures by default, including
    under ``-q``, ``--tb=no`` and xdist; verified per-flag)."""
    last = None
    for m in _SHORT_SUMMARY_BAR.finditer(output):
        last = m
    return None if last is None else output[last.end():]


def _pytest_passed_tests(output: str) -> list[str]:
    """Extract PASSING test node ids from pytest's short test summary info
    (``PASSED path::test``), which pytest only prints when asked (``-rA`` /
    ``-rP``). Ordered, de-duplicated; empty when the run did not report them.

    The twin of `_pytest_failing_tests`, and it exists for the same reason
    that one does: a count cannot name anything. ``passed`` is parsed from a
    summary line whose framework is guessed (`_parse_test_output`), so on a
    compound command it can be some other runner's tally — a caller asking
    "did this specific id run and pass?" must have the ids, and only these
    lines carry them. SKIPPED/XFAIL lines are deliberately not matched: they
    do not carry a node id in this section, and neither is a pass.

    Read STRICTLY from the summary section: no section, no names. This one
    grants passes, so an unanchored read would be fail-OPEN — see the bar
    comment above.
    """
    section = _short_summary_section(output)
    if section is None:
        return []
    seen: list[str] = []
    for m in _PYTEST_PASSED_ID.finditer(section):
        name = m.group(1)
        if name not in seen:
            seen.append(name)
    return seen


def _pytest_failing_tests(output: str) -> list[str]:
    """Extract failing/erroring test node ids from pytest's short test summary
    info section (``FAILED path::test - reason`` / ``ERROR path::test -
    reason``). Ordered, de-duplicated. Without this the gate's failure event
    carried only a bare count — the failing test(s) were never named.

    Read from the summary section when there is one, for the same
    anti-forgery reason as `_pytest_passed_tests` — this direction is
    fail-CLOSED (a phantom failure only ever bills more), so it is hardening
    rather than a fix, and it retires the printed-`FAILED` half of the
    phantom class the 2026-07-25 `::` guard above only narrowed.

    The unanchored FALLBACK stays for runs with no section at all (`-rN`,
    `--no-summary`): there the whole-output scan is the only source of names,
    and losing them would strip the failing tests out of the PR body and out
    of the base-tree attribution — a real regression in exchange for closing
    a direction that cannot manufacture a pass.
    """
    section = _short_summary_section(output)
    scan = output if section is None else section
    seen: list[str] = []
    for m in _PYTEST_FAILED_ID.finditer(scan):
        name = m.group(1)
        if name not in seen:
            seen.append(name)
    return seen


# pytest's FAILURES section prints one block per failing test, headed by a
# rule of underscores around the (short) test name:
#   _________________________ test_y _________________________
# and terminated either by the next such header or by a "=" bar line (e.g.
# the trailing "===== short test summary info =====" / "===== 2 failed in
# 1.2s ====="). SCRUM-40: the gate previously named only the failing test,
# with no assertion detail — an escalation could not be triaged without a
# live reproduction.
_TRACEBACK_HEADER = re.compile(r"^_{3,}\s+(.+?)\s+_{3,}\s*$", re.M)
_BAR_LINE = re.compile(r"^={3,}.*$", re.M)

_EXCERPT_MAX_LINES = 40
_EXCERPT_MAX_BYTES = 2048
_EXCERPT_MAX_TESTS = 3


def _short_test_name(node_id: str) -> str:
    """``tests/test_x.py::TestC::test_y`` -> ``TestC.test_y``; a node id with
    no ``::`` (shouldn't happen for a FAILED/ERROR line) is returned as-is."""
    parts = node_id.split("::")
    if len(parts) <= 1:
        return node_id
    return ".".join(parts[1:])


def _cap_excerpt(text: str) -> str:
    lines = text.strip("\n").splitlines()
    truncated = len(lines) > _EXCERPT_MAX_LINES
    capped = "\n".join(lines[:_EXCERPT_MAX_LINES])
    if len(capped) > _EXCERPT_MAX_BYTES:
        capped = capped[:_EXCERPT_MAX_BYTES]
        truncated = True
    if truncated:
        capped += "\n… [truncated]"
    return capped


def _pytest_traceback_excerpts(
    output: str, failing_tests: list[str],
) -> dict[str, str]:
    """Map each name in *failing_tests* to a capped excerpt of its FAILURES
    block in *output*. Unmatched node ids (malformed/absent output) are
    silently skipped — never raises. When more than ``_EXCERPT_MAX_TESTS``
    tests match, keeps the ones with the largest (most diagnostic, by
    uncapped line count) blocks, tiebroken by original failure order."""
    if not failing_tests:
        return {}
    headers = list(_TRACEBACK_HEADER.finditer(output))
    if not headers:
        return {}
    # Short header names are not unique across files (tests/a.py::test_y and
    # tests/b.py::test_y both header as "test_y"), so keep every same-name
    # block in output order and pair them positionally with the node ids —
    # pytest emits the FAILURES section and the summary in the same order.
    blocks: dict[str, list[str]] = {}
    for i, m in enumerate(headers):
        name = m.group(1).strip()
        start = m.end()
        next_header_start = headers[i + 1].start() if i + 1 < len(headers) else len(output)
        bar_match = _BAR_LINE.search(output, start, next_header_start)
        end = bar_match.start() if bar_match else next_header_start
        blocks.setdefault(name, []).append(output[start:end])

    consumed: dict[str, int] = {}
    matched: list[tuple[str, str]] = []
    for node_id in failing_tests:
        name = _short_test_name(node_id)
        bodies = blocks.get(name)
        nth = consumed.get(name, 0)
        if not bodies or nth >= len(bodies):
            continue  # unmatched node ids are silently skipped (docstring)
        consumed[name] = nth + 1
        matched.append((node_id, bodies[nth]))
    if not matched:
        return {}

    indexed = [(node_id, body, idx) for idx, (node_id, body) in enumerate(matched)]
    if len(indexed) > _EXCERPT_MAX_TESTS:
        indexed.sort(key=lambda t: (-len(t[1].strip("\n").splitlines()), t[2]))
        indexed = indexed[:_EXCERPT_MAX_TESTS]
        indexed.sort(key=lambda t: t[2])  # restore chronological order

    return {node_id: _cap_excerpt(body) for node_id, body, _idx in indexed}


_TAP_NOT_OK_RE = re.compile(r"^\s*not ok \d+\b")
_TAP_BLOCK_END_RE = re.compile(r"^\s*(?:not )?ok \d+\b|^\s*1\.\.\d+|^#|^\s*\.\.\.\s*$")


def _tap_failure_blocks(output: str) -> list[str]:
    """Node TAP `not ok N ...` blocks, each captured through its own YAML
    terminator (`  ...`, the next `ok`/`not ok` line, the next `#` comment,
    or the trailing `1..N` plan line) — off the FULL output, before any
    `[-8000:]` tail truncates it. `[]` when *output* carries no `not ok`
    line (not TAP, or a clean pytest run); never raises. Capped like
    `_pytest_traceback_excerpts` (`_EXCERPT_MAX_TESTS` blocks, each through
    `_cap_excerpt`) so a runaway TAP dump can't blow up state.
    """
    if not output:
        return []
    lines = output.splitlines()
    blocks: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        if _TAP_NOT_OK_RE.match(lines[i]):
            start = i
            j = i + 1
            while j < n and not _TAP_BLOCK_END_RE.match(lines[j]):
                j += 1
            blocks.append(_cap_excerpt("\n".join(lines[start:j])))
            i = j
        else:
            i += 1
    return blocks[:_EXCERPT_MAX_TESTS]


_TAP_NOT_OK_LINE_RE = re.compile(r"^\s*not ok \d+\s*-\s*(.+?)\s*$")
_TAP_SKIP_DIRECTIVE_RE = re.compile(r"#\s*(?:SKIP|TODO)\b", re.IGNORECASE)
_TAP_LOCATION_RE = re.compile(r"^\s*location:\s*'(.+?)'\s*$", re.M)
_TAP_LOCATION_LINE_COL_RE = re.compile(r":\d+:\d+$")


def _relativize_tap_location(
    loc: str, repo_path: "Path | None", work_dir: "Path | None",
) -> str:
    """Best-effort: make an absolute TAP `location:` path relative to
    *work_dir* first, then *repo_path* — the same preference order
    `_newly_failing_vs_base`/`_owned_failing_tests` already use for node
    cwd-prefixing. Falls back to *loc* unchanged (relative already, or
    under neither base) rather than raising: a node id with an
    unrelativized absolute path is still a usable (if uglier) id, never a
    reason to drop the failure.
    """
    path = Path(loc)
    if not path.is_absolute():
        return loc
    for base in (work_dir, repo_path):
        if base is None:
            continue
        try:
            return path.resolve().relative_to(Path(base).resolve()).as_posix()
        except (ValueError, OSError):
            continue
    return loc


def _node_tap_failing_tests(
    output: str, *, repo_path: "Path | None" = None, work_dir: "Path | None" = None,
) -> list[str]:
    """Node TAP `not ok N - <name>` ids, ordered and de-duplicated — the node
    counterpart to `_pytest_failing_tests`. Without this, `failing_tests`
    stayed `[]` for every node run, which made per-test ownership
    attribution (`ownership.owned_failing_ids`, `_owned_failing_tests`) a
    silent no-op for the whole ecosystem: an owned node id could never be
    excused OR billed by name, because there was never an id to look up
    (round-3 review MAJOR).

    Each `not ok` block's own YAML diagnostic is checked for a `location:
    'path:line:col'` line (node's `--test-reporter=tap` emits one per
    failure when the test has a resolvable source location); when present,
    the id is ``f"{relative_path}::{name}"`` — a shape `ownership.py`'s new
    file-scoped id parsing understands. With no `location:` line the id is
    the bare name, which `ownership.py` deliberately treats as unownable
    (fail-closed: an id nothing can be attributed to is only ever billed,
    never excused).

    A block whose `not ok` line carries a `# SKIP`/`# TODO` directive is not
    a failure and is dropped. `[]` on no `not ok` line; never raises.
    """
    if not output:
        return []
    lines = output.splitlines()
    ids: list[str] = []
    seen: set[str] = set()
    i, n = 0, len(lines)
    while i < n:
        m = _TAP_NOT_OK_LINE_RE.match(lines[i])
        if not m:
            i += 1
            continue
        raw_name = m.group(1)
        start = i
        j = i + 1
        while j < n and not _TAP_BLOCK_END_RE.match(lines[j]):
            j += 1
        block = "\n".join(lines[start:j])
        i = j
        if _TAP_SKIP_DIRECTIVE_RE.search(raw_name):
            continue  # a skip/todo is not a failure
        name = raw_name.strip()
        node_id = name
        loc_m = _TAP_LOCATION_RE.search(block)
        if loc_m:
            loc = _TAP_LOCATION_LINE_COL_RE.sub("", loc_m.group(1))
            rel = _relativize_tap_location(loc, repo_path, work_dir)
            node_id = f"{rel}::{name}"
        if node_id not in seen:
            seen.add(node_id)
            ids.append(node_id)
    return ids


def _failing_ids(
    output: str, *, repo_path: "Path | None" = None, work_dir: "Path | None" = None,
) -> list[str]:
    """`_pytest_failing_tests`, falling back to `_node_tap_failing_tests` when
    pytest's parser found nothing — a single run's output is one ecosystem
    or the other, never both, and the node parser (a full line-by-line TAP
    walk) is not worth running when the cheaper pytest regex already
    matched. Kept as one helper so all of `run_tests`'s `TestRunResult`
    construction sites stay identical (round-3 review MAJOR).
    """
    return _pytest_failing_tests(output) or _node_tap_failing_tests(
        output, repo_path=repo_path, work_dir=work_dir,
    )


def render_traceback_excerpts(excerpts: dict[str, str]) -> str:
    """Human-readable, non-truncated rendering of *excerpts* for the
    ``attempt_failed`` event text. Returns "" when empty so callers can
    append conditionally, exactly like ``failing_tests``."""
    if not excerpts:
        return ""
    parts = ["Traceback excerpts:"]
    for name, excerpt in excerpts.items():
        parts.append(f"\n——— {name} ———\n{excerpt}")
    return "\n".join(parts)


# pytest-xdist's tmp-dir cleanup (TempPathFactory's session-scoped finalizer)
# runs at pytest_unconfigure time, AFTER the real summary line already
# printed. A worker (`popen-gw*`) racing another worker's rmdir of the shared
# `garbage-*` staging dir raises `OSError: [Errno 66] Directory not empty`
# from that late finalizer — the tests already passed, but the process exits
# non-zero, so the gate previously read it as a bare test failure with no
# name to show for it (SCRUM-37).
# Reviewer finding (SCRUM-37 round 1): the race's OSError line often carries
# only the garbage-*/pytest-of-* staging path WITHOUT a popen-gw token (the
# per-worker dir appears on a separate "removing" line, or not at all when the
# session-scoped finalizer loses the race). Any of the three staging markers
# adjacent to the error is the signature; all are pytest-tmpdir-specific
# strings that never appear in real test-failure output.
_TEARDOWN_RACE = re.compile(
    r"Directory not empty.{0,400}?(?:popen-gw|garbage-|pytest-of-)"
    r"|(?:popen-gw|garbage-|pytest-of-).{0,400}?Directory not empty",
    re.DOTALL,
)


def _is_teardown_race(output: str) -> bool:
    return bool(_TEARDOWN_RACE.search(output))


def _parse_vitest(output: str) -> tuple[int, int, int]:
    """Parse vitest summary lines like:
      Tests  413 passed (413)
      Tests  3 failed | 5 passed (8)
    """
    passed = failed = errors = 0
    m = re.search(r"Tests\s+(.+)", output)
    if m:
        line = m.group(1)
        pm = re.search(r"(\d+)\s+passed", line)
        if pm:
            passed = int(pm.group(1))
        fm = re.search(r"(\d+)\s+failed", line)
        if fm:
            failed = int(fm.group(1))
    return passed, failed, errors


def _parse_jest(output: str) -> tuple[int, int, int]:
    """Parse Jest summary like:
      Tests:       3 failed, 5 passed, 8 total
    """
    passed = failed = errors = 0
    m = re.search(r"Tests:\s+(.+)", output)
    if m:
        line = m.group(1)
        pm = re.search(r"(\d+)\s+passed", line)
        if pm:
            passed = int(pm.group(1))
        fm = re.search(r"(\d+)\s+failed", line)
        if fm:
            failed = int(fm.group(1))
    return passed, failed, errors


def _parse_unittest_summary(output: str) -> tuple[int, int, int] | None:
    """Parse a ``Passed:/Failed:/Errors:`` summary block (unittest-style runners).

    A hand-rolled `run_tests.py` runner prints exactly this. Without it the
    pytest fallback scanned the whole LOG rather than the summary block, matched
    a "N failed" printed somewhere in the body, and reported failures for a run
    whose own ``Failed:`` line said zero and which exited 0. The counts are
    deliberately not quoted here: a real suite's size is a fingerprint, and the
    bug is in reading the wrong line, not in any particular number.
    """
    passed = re.search(r"^Passed:\s*(\d+)", output, re.M)
    failed = re.search(r"^Failed:\s*(\d+)", output, re.M)
    errors = re.search(r"^Errors:\s*(\d+)", output, re.M)
    if passed and failed and errors:
        return int(passed.group(1)), int(failed.group(1)), int(errors.group(1))
    return None


def _parse_node_test(output: str) -> tuple[int, int, int]:
    """node --test TAP summary: '# pass 40' / '# fail 0' / '# tests 40'."""
    p = re.search(r"^#\s*pass\s+(\d+)", output, re.M)
    f = re.search(r"^#\s*fail\s+(\d+)", output, re.M)
    return (int(p.group(1)) if p else 0, int(f.group(1)) if f else 0, 0)


def _parse_test_output(command: str, output: str) -> tuple[int, int, int]:
    """Dispatch to the right parser based on the command and output."""
    cmd_lower = command.lower()
    # node --test emits a distinctive TAP summary (`# pass N`); detect it
    # before the pytest fallback, which would read "# pass 40" as 0 passed.
    if "node --test" in cmd_lower or re.search(r"^#\s*(pass|fail|tests)\s+\d+", output, re.M):
        return _parse_node_test(output)
    # Try vitest/jest detection from output first (more reliable than command).
    if re.search(r"Test Files\s+\d+", output) or "vitest" in cmd_lower:
        return _parse_vitest(output)
    if re.search(r"Tests:\s+\d+", output) or "jest" in cmd_lower:
        return _parse_jest(output)
    # A line-anchored Passed:/Failed:/Errors: block is unambiguous — check it
    # before the pytest fallback, which greps the whole log for "N failed".
    unittest_counts = _parse_unittest_summary(output)
    if unittest_counts is not None:
        return unittest_counts
    if "pytest" in cmd_lower or re.search(r"\d+\s+passed", output):
        return _parse_pytest(output)
    # npm test / npx — inspect output to guess framework.
    if "npm" in cmd_lower or "npx" in cmd_lower:
        # vitest puts "Test Files" in output; jest puts "Tests:"
        if "Test Files" in output:
            return _parse_vitest(output)
        if "Tests:" in output:
            return _parse_jest(output)
    # Fallback to pytest parser (handles generic "N passed" lines).
    return _parse_pytest(output)


#: A test run whose failing tests are explained by a missing BUILD
#: PREREQUISITE in this checkout is an ENVIRONMENT error, not failed code:
#: the suite ran (and mostly passed) but nothing about the diff was judged
#: by the tests it DID fail, so retrying the coder just burns attempts
#: (observed: a desktop `npm test` where `app-builder-lib` was not installed
#: and `web/dist` had never been built — 2 of 410 tests red, judged "tests
#: failed", retried 3x).
#: Deliberately a SHORT, EXPLICIT list of node/build-artefact signatures.
#: Python's ModuleNotFoundError/ImportError are NOT here — a coder-introduced
#: import breakage is owned by the base-tree gate
#: (`_invocation_error_reproduces_on_base`) and must keep failing the attempt.
_MISSING_PREREQUISITE_RULES: tuple[tuple[re.Pattern, str], ...] = (
    # bare specifier only: "./foo.mjs" is a file THIS change may have deleted.
    (re.compile(r"Cannot find (?:module|package) ['\"]([^'\"./][^'\"]*)['\"]"),
     "a node package is not installed: {0}"),
    # The real artefact/test text uses an em dash (`web/src/cancelFlow.test.mjs:64`,
    # bytes e2 80 94), not a hyphen — match hyphen, en dash and em dash so the
    # actual incident bytes are not silently missed.
    (re.compile(r"([^\s'\"]+) is missing [-–—] run `npm run build`"),
     "a build artefact was never built: {0}"),
    # `dist` gets its own word boundary (`(?<![\w-])...(?![\w-])`) so it
    # matches the build-output directory (`web/dist`, `dist/assets`) and not
    # a substring of an unrelated name (`redistribute.json`, `dist_tools/`) —
    # `node_modules` is specific enough on its own to need none.
    (re.compile(r"ENOENT[^\n]*?((?:[\w.@/-]*/)?(?:node_modules|(?<![\w-])dist(?![\w-]))(?:/[\w.@-]+)*)"),
     "a build/install path is missing: {0}"),
    # Same signature, opposite order: `spawnSync …/node_modules/.bin/foo ENOENT`
    # names the path BEFORE the errno, not after.
    (re.compile(r"((?:[\w.@/-]*/)?(?:node_modules|(?<![\w-])dist(?![\w-]))(?:/[\w.@-]+)*)[^\n]*?ENOENT"),
     "a build/install path is missing: {0}"),
)


def missing_prerequisite_reason(output: str) -> str | None:
    """Why (some of) this run's failing tests are explained by a missing
    build prerequisite — the suite itself ran and may be mostly green — or
    None when *output* shows a real test failure instead.

    One function, one list: every consumer classifies identically, and the
    tests observe it through the verdict string the caller builds from it.
    """
    for pattern, template in _MISSING_PREREQUISITE_RULES:
        match = pattern.search(output or "")
        if match:
            return template.format(match.group(1))
    return None


def prerequisite_reason_for(result: "TestRunResult") -> str | None:
    """Same rule list as `missing_prerequisite_reason`, scoped to the FAILING
    content only: `result.failure_blocks` (node TAP blocks parsed off the
    untruncated output, see `_tap_failure_blocks`), then — for pytest —
    `result.traceback_excerpts`. Deliberately never `result.output`: that is
    an [-8000:] tail that may not contain the failure at all, and even when
    it does, a signature sitting in unrelated PASSING output must not excuse
    a real failure elsewhere in the same run (the product-review defect this
    replaces). None when neither carries a signature — the failing tests
    here were not judgements of the diff is a claim only the failing
    evidence itself may make.
    """
    for block in result.failure_blocks:
        reason = missing_prerequisite_reason(block)
        if reason is not None:
            return reason
    for excerpt in (result.traceback_excerpts or {}).values():
        reason = missing_prerequisite_reason(excerpt)
        if reason is not None:
            return reason
    return None


_INVOCATION_ERROR_PATTERNS = re.compile(
    r"error: unrecognized arguments"
    r"|no tests ran"
    r"|no tests collected"
    r"|ModuleNotFoundError"
    r"|ImportError"
    r"|command not found"
    # node module-resolution failures — a worktree without `node_modules`
    # (gitignored, so a fresh `git worktree add` never has it) reads as a
    # partial test failure ("2335 passed, 1 failed") instead of the
    # infrastructure gap it actually is (SCRUM-33/SCRUM-35).
    r"|Cannot find package"
    r"|Cannot find module"
    r"|ERR_MODULE_NOT_FOUND",
    re.IGNORECASE,
)


def _is_invocation_error(
    returncode: int, output: str,
    passed: int, failed: int, errors: int,
) -> bool:
    """True when the test runner itself failed to execute (bad flags, missing
    plugins, command not found) rather than tests actually failing."""
    if returncode == 0:
        return False
    if passed + failed + errors == 0:
        return True
    if _INVOCATION_ERROR_PATTERNS.search(output):
        return True
    return False


def _fix_invocation(cmd: str, output: str, repo_path: Path) -> str | None:
    """Try to produce a corrected command for a known invocation failure."""
    # python not found → try python3
    if cmd.startswith("python ") and "command not found" in output:
        return "python3" + cmd[6:]
    # pytest bad flags → strip addopts
    if "pytest" in cmd and "unrecognized arguments" in output:
        return f'pytest -q --override-ini="addopts=" --override-ini="testpaths=" {repo_path}'
    # bare `pytest` can't import pytest ITSELF → re-run via no_human's own
    # interpreter, which always has pytest (it's a dependency of ours). This is
    # deterministic (no PATH guessing) and CANNOT manufacture a false pass: if
    # the tested project needs deps our interpreter lacks, pytest re-errors with
    # a DIFFERENT module name, `_is_invocation_error` re-flags it, and the loop
    # stays at honest "no test evidence". Scoped to a BARE `pytest` — `uv run
    # pytest ...` manages its own env (a different failure mode) and is left
    # alone. The needle matches pytest ITSELF or a `pytest_*` plugin being
    # unimportable — both are safe: a still-missing plugin just re-errors on the
    # retry → honest "no evidence", never a false pass. A ModuleNotFound naming
    # some OTHER project dep does not match, so a real project-dep gap stays honest.
    stripped = cmd.strip()
    if stripped == "pytest" or stripped.startswith("pytest "):
        out_lower = output.lower()
        if (
            "no module named pytest" in out_lower
            or "no module named 'pytest'" in out_lower
            or ("pytest" in out_lower and "command not found" in out_lower)
        ):
            rest = stripped[len("pytest"):]
            return f"{sys.executable} -m pytest{rest}"
    return None


def _venv_bin(repo_path: Path) -> Path | None:
    """The repo's virtualenv ``bin/`` directory, if it ships one.

    These commands are shelled out from the long-running server, whose PATH is
    not a developer's shell: on this machine it has ``python3`` but no ``python``
    at all. A repo that ships a venv expects ``source .venv/bin/activate`` first,
    and its test command is written on that assumption (a confirmed profile in
    use says ``python run_tests.py``, whose imports resolve only inside the
    venv). This is that activation, without the shell.
    """
    names = [".venv", "venv"]
    names += sorted(
        p.name for p in repo_path.glob(".venv*")
        if p.is_dir() and p.name not in names
    )
    # Windows venvs are `<venv>\Scripts\python.exe`, not `<venv>/bin/python`.
    # Probing only the POSIX shape there found NO venv, ever — so the PATH and
    # VIRTUAL_ENV injection in `_env_for` silently did nothing and every test
    # command ran against whatever interpreter happened to be first on PATH,
    # which is the "TESTING had never actually run" failure mode.
    sub, exe = ("Scripts", "python.exe") if _IS_WINDOWS else ("bin", "python")
    for name in names:
        bin_dir = repo_path / name / sub
        if (bin_dir / exe).exists():
            return bin_dir
    return None


def _is_node_cmd(cmd: str) -> bool:
    """True for a node/npm-family test command — the ones that need
    `node_modules` present to even start."""
    c = cmd.lower()
    return (
        "node --test" in c or "npm" in c or "npx" in c
        or "vitest" in c or "jest" in c
    )


def _ensure_node_deps(repo_path: Path, work_dir: Path, source_repo: Path | None) -> None:
    """Make `node_modules` present in *work_dir* before a node test command runs.

    `node_modules` is gitignored, so a freshly created `git worktree add`
    checkout never has it — unlike a Python venv (physically present, so the
    runner's ``_venv_bin``/``_env_for`` reuse it automatically), leaving web
    tests non-hermetic in a task worktree (SCRUM-33/SCRUM-35). Mirrors that
    venv-reuse pattern for node: symlink the already-installed
    `node_modules` from the source checkout rather than reinstalling
    (`npm ci` in a fresh worktree is slow and network-dependent — itself an
    infra risk).

    *work_dir* maps to *source_repo* via the same path relative to
    *repo_path* (e.g. `<worktree>/web` -> `<source_repo>/web`). Best-effort:
    never raises — a residual missing-deps run is still caught by
    ``_INVOCATION_ERROR_PATTERNS`` below.
    """
    if source_repo is None:
        return
    work_dir = Path(work_dir)
    target = work_dir / "node_modules"
    if target.exists() or target.is_symlink():
        return  # already present (real dir or a prior symlink) — no-op
    try:
        rel = work_dir.resolve().relative_to(Path(repo_path).resolve())
    except (OSError, ValueError):
        rel = Path(".")
    src = Path(source_repo) / rel / "node_modules"
    try:
        if src.is_dir():
            work_dir.mkdir(parents=True, exist_ok=True)
            target.symlink_to(src, target_is_directory=True)
    except OSError as exc:
        log.warning(
            "node deps provisioning failed (work_dir=%s, source=%s): %s",
            work_dir, src, exc,
        )


def _ensure_forced_build_artifacts(repo_path: Path, source_repo: Path | None) -> None:
    """Make `pyproject.toml`'s force-included paths present in a task worktree.

    Same trap as `_ensure_node_deps`, reached from the Python side. `web/dist` is
    a gitignored build artifact that `git worktree add` never creates, and the
    wheel and sdist targets both force-include it. Absence is deliberately loud,
    so a release cut without `npm run build` fails instead of shipping a
    boardless wheel. In a task worktree that same loudness used to mean ANY
    `uv run` / `uv build` / editable install died before collection, so the
    coder could not run the suite at all.

    Since `hatch_build.py`, the board's entry is injected per build VERSION and
    an editable install only WARNS, so a boardless worktree can now at least
    install and collect. This still runs: without it the worktree's board tests
    skip or run against nothing, and any real wheel build there still fails.

    Observed 2026-08-01: a task burned its entire lifetime budget (2.58M
    cost-weighted tokens, one attempt) without reaching a green run, and its
    reviewer traced the red suite to exactly this — `uv run pytest` could not
    reach collection because `web/dist` was missing from the worktree.

    Taken from the source checkout rather than rebuilt: `npm run build` in a
    fresh worktree is slow and network-dependent, which is the same reasoning
    `_ensure_node_deps` gives. If the source checkout has not built it either,
    nothing is provisioned and the loud failure stands — which is correct,
    because then there genuinely is no board to package.

    COPIED, where `_ensure_node_deps` symlinks, and the difference is
    deliberate. `web/package.json`'s build script is `vite build`, which WRITES
    into `web/dist`; through a symlink a task that touches the UI would rebuild
    straight into the developer's checkout. `node_modules` is hundreds of
    megabytes, so linking it is the only practical option and the write-through
    risk is accepted; this is 1 MB across 27 files, so isolation is nearly free
    and there is no reason to accept it here.

    Best-effort: never raises.
    """
    if source_repo is None:
        return
    repo_path, source_repo = Path(repo_path), Path(source_repo)
    try:
        cfg = tomllib.loads((repo_path / "pyproject.toml").read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return
    targets = (cfg.get("tool", {}).get("hatch", {})
                  .get("build", {}).get("targets", {}))
    forced: set[str] = set()
    for target in targets.values():
        forced.update((target.get("force-include") or {}).keys())
        # `web/dist` is no longer a STATIC force-include: it is injected by
        # `hatch_build.py`, which only fails a distributable build (a clean
        # clone must stay installable — see that file). The path still has to be
        # provisioned here, or a task worktree runs the board's tests against a
        # board that is not there, so the hook's declared `source` counts as
        # forced too. Read from config rather than hardcoded, so the two cannot
        # drift.
        for hook in (target.get("hooks") or {}).values():
            source = (hook or {}).get("source")
            if isinstance(source, str) and source:
                forced.add(source)
    for rel in sorted(forced):
        dest = repo_path / rel
        if dest.exists() or dest.is_symlink():
            if rel == "web/dist":
                stamp = source_repo / "web" / ".board-stamp.json"
                destination_stamp = repo_path / "web" / stamp.name
                if stamp.is_file() and not destination_stamp.exists():
                    try:
                        shutil.copy2(stamp, destination_stamp)
                    except OSError as exc:
                        log.warning("board stamp provisioning failed (%s -> %s): %s",
                                    stamp, destination_stamp, exc)
            continue
        # Only provision what git genuinely does not carry. A TRACKED forced
        # path is absent from a worktree for exactly one reason — the branch
        # deleted it — and restoring it would hide that from the branch's own
        # test run: a task that deleted the whole schema directory would watch
        # its suite go green. The gitignored case is the one this exists for.
        # (Caught in review of the commit that added this, against a real
        # deletion of `migrations/`; the export gate refuses that downstream on
        # both a count pin and nine content pins, but a task must not be able to
        # mislead itself in the meantime.)
        try:
            tracked = subprocess.run(
                ["git", "ls-files", "--error-unmatch", rel],
                cwd=repo_path, capture_output=True, timeout=30,
            ).returncode == 0
        except (OSError, subprocess.SubprocessError):
            tracked = False
        if tracked:
            continue
        src = source_repo / rel
        try:
            if src.is_dir():
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(src, dest, symlinks=True)
                if rel == "web/dist":
                    stamp = source_repo / "web" / ".board-stamp.json"
                    if stamp.is_file():
                        shutil.copy2(stamp, repo_path / "web" / stamp.name)
        except OSError as exc:
            log.warning("forced-include provisioning failed (%s -> %s): %s",
                        dest, src, exc)


def _env_for(repo_path: Path, env: dict[str, str] | None = None) -> dict[str, str]:
    """Process env for a repo command: the server's, plus the repo's venv."""
    run_env = {**os.environ, **(env or {})}
    bin_dir = _venv_bin(repo_path)
    if bin_dir is not None:
        run_env["PATH"] = f"{bin_dir}{os.pathsep}{run_env.get('PATH', '')}"
        run_env["VIRTUAL_ENV"] = str(bin_dir.parent)
        run_env.pop("PYTHONHOME", None)  # activate() clears this too
    return run_env


# Live test subprocesses, so the attempt teardown can kill any left running by
# a CANCELLATION before it removes the worktree. `nh pause` / stuck-reset unwind
# the awaiting coroutine, but asyncio.to_thread's thread (and its pytest
# subprocess) keep running; the teardown then rmtree's the worktree .venv out
# from under them (the xdist INTERNALERROR). terminate_running() kills them
# first. Keyed by id(proc); each entry holds the resolved cwd for prefix match.
_RUNNING_LOCK = threading.Lock()
_RUNNING: dict[int, tuple[str, "subprocess.Popen"]] = {}


def _register(work_dir: Path, proc: "subprocess.Popen") -> None:
    with _RUNNING_LOCK:
        _RUNNING[id(proc)] = (str(Path(work_dir).resolve()), proc)


def _deregister(proc: "subprocess.Popen") -> None:
    with _RUNNING_LOCK:
        _RUNNING.pop(id(proc), None)


def terminate_running(under: Path | str) -> int:
    """Kill (process-group) any live test subprocess whose cwd is inside *under*.
    Called by the attempt teardown BEFORE it removes the worktree, so a test
    orphaned by a cancellation is dead before rmtree deletes its .venv. Safe to
    call always: on a normal finish the process already deregistered, so this is
    a no-op — it can only improve the race, never worsen it. Returns the count."""
    root = str(Path(under).resolve())
    with _RUNNING_LOCK:
        procs = [p for (wd, p) in _RUNNING.values()
                 if wd == root or wd.startswith(root + os.sep)]
    killed = 0
    for p in procs:
        if _kill_process_tree(p):
            killed += 1
        else:
            try:
                p.kill()
            except Exception:  # noqa: BLE001
                pass
    return killed


def _run_shell_streaming(
    cmd: str, work_dir: Path, timeout: int, run_env: dict[str, str],
    on_line: "Callable[[str], None]",
) -> tuple[int | None, str, bool]:
    """The streaming half of ``_run_shell``: identical process setup, but the
    output is read line-by-line and handed to *on_line* as it arrives instead
    of being collected in one ``communicate()``.

    Everything that makes a run a PROOF is deliberately unchanged — same command
    string, same cwd, same env, same ``shell=True``, same ``_NEW_GROUP_KWARGS``
    process group/creation flags, same kill-the-tree-on-timeout. Only the
    *reading* differs,
    so a run watched live by the web wizard proves exactly what an unwatched run
    would (see onboard.py's module docstring: proving must never drift from what
    the orchestrator later executes).

    ``stderr`` is folded into ``stdout`` here so the caller sees the two
    interleaved in real time, which is how a human reads a failing test run.
    """
    proc = subprocess.Popen(
        cmd, cwd=work_dir, shell=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        bufsize=1, env=run_env, **_NEW_GROUP_KWARGS,
    )
    _register(work_dir, proc)
    chunks: list[str] = []

    def _pump() -> None:
        try:
            for line in proc.stdout:  # type: ignore[union-attr]
                chunks.append(line)
                try:
                    on_line(line.rstrip("\n"))
                except Exception:  # noqa: BLE001
                    # A broken/disconnected consumer (the user closed the tab)
                    # must never fail the run or corrupt its exit status.
                    pass
        except (ValueError, OSError):  # pipe closed under us on kill
            pass

    reader = threading.Thread(target=_pump, daemon=True)
    reader.start()
    try:
        proc.wait(timeout=timeout)
        reader.join(timeout=15)
        return proc.returncode, "".join(chunks), False
    except subprocess.TimeoutExpired:
        if not _kill_process_tree(proc):
            proc.kill()  # fallback: at least the direct child
        reader.join(timeout=15)
        return None, "".join(chunks), True
    finally:
        _deregister(proc)


def _run_shell(
    cmd: str, work_dir: Path, timeout: int, run_env: dict[str, str],
) -> tuple[int | None, str, bool]:
    """Run a shell test command, capturing stdout+stderr. Returns
    ``(returncode, output, timed_out)``.

    On timeout the WHOLE process tree is killed (``_NEW_GROUP_KWARGS`` puts
    the shell in its own group on POSIX, ``_kill_process_tree`` reaps it —
    ``killpg`` there, ``taskkill /T`` on Windows), so the shell's
    grandchildren — pytest and its ``-n auto`` xdist workers — die too instead
    of being orphaned. Orphaned workers keep the worktree's ``.venv`` open, and
    the attempt's teardown then rmtree's it out from under them (the xdist
    ``INTERNALERROR: .venv/bin/python`` seen in the 3-parallel run). Plain
    ``subprocess.run(timeout=…)`` only kills the direct child (the shell), so
    the workers survived."""
    proc = subprocess.Popen(
        cmd, cwd=work_dir, shell=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env=run_env, **_NEW_GROUP_KWARGS,
    )
    _register(work_dir, proc)
    try:
        out, err = proc.communicate(timeout=timeout)
        return proc.returncode, (out or "") + (err or ""), False
    except subprocess.TimeoutExpired:
        if not _kill_process_tree(proc):
            proc.kill()  # fallback: at least the direct child
        try:
            out, err = proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            out, err = "", ""
        return None, (out or "") + (err or ""), True
    finally:
        _deregister(proc)


def run_tests(
    repo_path: Path, command: str | None = None, *,
    cwd: Path | None = None, timeout: int = 1800,
    env: dict[str, str] | None = None,
    source_repo: Path | None = None,
    on_line: "Callable[[str], None] | None" = None,
) -> TestRunResult:
    # ``on_line``, when given, receives each output line as it is produced so a
    # caller can show live progress (the web wizard's prove step). It changes
    # nothing about WHAT runs — see _run_shell_streaming.
    # timeout was 600s — shorter than a real suite in a fresh worktree venv
    # under parallel load. Every dogfood task of the first parallel run
    # (2026-07-11) failed as "0 passed, 0 failed, 1 errors": the TIMEOUT,
    # parsed as an error, invisible until failures carried their output.
    # 1800s gives honest room; a timeout still reads as failure (never a
    # false pass), and now says so in the record.
    repo_path = Path(repo_path)
    work_dir = Path(cwd) if cwd else repo_path  # Phase 6b: cross-repo cwd
    cmd = command or detect_command(repo_path)
    if not cmd:
        return TestRunResult(False, True, 0, 0, 0, "", "no test command detected")
    if source_repo is not None and _is_node_cmd(cmd):
        _ensure_node_deps(repo_path, work_dir, Path(source_repo))
    # Unconditional, unlike the node case: a forced-include miss kills `uv run` /
    # `uv build` / any editable install before collection, so it breaks a python
    # command whatever that command turns out to be.
    _ensure_forced_build_artifacts(repo_path, source_repo)
    run_env = _env_for(repo_path, env)
    # The shared venv's editable install resolves the package to the MAIN
    # checkout, so a worktree's tests silently import main's code — the python
    # twin of the node-deps trap above (SCRUM-18 burned its whole budget on 5
    # "failures" that were main's code, not the branch's). sys.path (which
    # PYTHONPATH feeds) is consulted before appended editable finders, so the
    # tested tree's src/ must lead. Review 2026-07-25: the original gate
    # (work_dir != repo_path only) NEVER fired in the production shape — the
    # orchestrator passes repo_path=<worktree> with cwd=None, so work_dir ==
    # repo_path. `source_repo is not None` is true exactly when repo_path is a
    # task worktree with a primary checkout elsewhere — that is the case that
    # needs repo_path/src injected. The work_dir branch stays for explicit
    # cross-dir cwds.
    inject: list[Path] = []
    if source_repo is not None and (repo_path / "src").is_dir():
        inject.append(repo_path / "src")
    wd_src = work_dir / "src"
    if work_dir != repo_path and wd_src.is_dir() and wd_src not in inject:
        inject.append(wd_src)
    if inject:
        prior = run_env.get("PYTHONPATH", "")
        joined = os.pathsep.join(str(p) for p in inject)
        run_env["PYTHONPATH"] = (
            f"{joined}{os.pathsep}{prior}" if prior else joined
        )
    # Streaming is opt-in and additive: with no watcher this is literally
    # `_run_shell`, resolved at call time so existing monkeypatches of it
    # still apply. With a watcher it is the same process setup, read a line
    # at a time (_run_shell_streaming) — the command, cwd, env and shell are
    # identical either way, which is what keeps a watched run a real proof.
    if on_line is None:
        _shell = _run_shell
    else:
        def _shell(c, w, t, e):
            return _run_shell_streaming(c, w, t, e, on_line)
    rc, output, timed_out = _shell(cmd, work_dir, timeout, run_env)
    if timed_out:
        return TestRunResult(True, False, 0, 0, 1, cmd, f"timed out after {timeout}s")
    passed, failed, errors = _parse_test_output(cmd, output)
    failing_tests = _failing_ids(output, repo_path=repo_path, work_dir=work_dir)
    # Parsed off the FULL captured output, like the failing ids above — the
    # [-8000:] tail each result carries below would drop most of an -rA summary.
    passed_tests = _pytest_passed_tests(output)
    failure_blocks = _tap_failure_blocks(output)
    ok = rc == 0
    if not ok and failed == 0 and errors == 0 and not failing_tests and _is_teardown_race(output):
        # INFRA, not a coder-bug: the tests already passed (the anchored
        # summary line shows zero failed/errors and nothing was named), but
        # the process exited non-zero from the late xdist teardown race.
        # Bounded to exactly ONE retry — same doctrine as the node-deps
        # invocation-error fix (SCRUM-35), applied to the in-runner retry
        # half rather than the base-tree-confirmation half (the race is
        # non-deterministic, so re-confirming against the base tree would
        # just be a second coin flip).
        log.warning("teardown-race signature in test output (cmd=%s, rc=%s); retrying once",
                    cmd, rc)
        rc_r, output_r, timed_out_r = _shell(cmd, work_dir, timeout, run_env)
        if timed_out_r:
            # Same classification as the first-run timeout above: a hanging
            # suite must never earn the advisory invocation_error path.
            return TestRunResult(True, False, 0, 0, 1, cmd,
                                 f"timed out after {timeout}s")
        passed_r, failed_r, errors_r = _parse_test_output(cmd, output_r)
        failure_blocks_r = _tap_failure_blocks(output_r)
        if rc_r != 0 and _is_invocation_error(rc_r, output_r, passed_r, failed_r, errors_r):
            # KEEP THE NAMES. `_is_invocation_error` returns True even with real
            # counts (the "2335 passed, 1 failed" node case just above), so this
            # result can describe a suite that RAN and named its failures. The PR
            # body renders those names; dropping them here left it promising a
            # list nothing could fill.
            return TestRunResult(True, False, passed_r, failed_r, errors_r,
                                 cmd, output_r[-8000:], invocation_error=True,
                                 failing_tests=_failing_ids(
                                     output_r, repo_path=repo_path, work_dir=work_dir),
                                 passed_tests=_pytest_passed_tests(output_r),
                                 failure_blocks=failure_blocks_r)
        failing_tests_r = _failing_ids(output_r, repo_path=repo_path, work_dir=work_dir)
        return TestRunResult(True, rc_r == 0, passed_r, failed_r, errors_r,
                             cmd, output_r[-8000:], failing_tests=failing_tests_r,
                             passed_tests=_pytest_passed_tests(output_r),
                             traceback_excerpts=_pytest_traceback_excerpts(output_r, failing_tests_r),
                             failure_blocks=failure_blocks_r)
    if not ok and _is_invocation_error(rc, output, passed, failed, errors):
        retry_cmd = _fix_invocation(cmd, output, repo_path)
        if retry_cmd and retry_cmd != cmd:
            log.warning("test invocation error (cmd=%s, rc=%s), retrying with: %s",
                        cmd, rc, retry_cmd)
            rc2, output2, timed_out2 = _shell(retry_cmd, work_dir, timeout, run_env)
            if timed_out2:
                # Timeout doctrine (see the first-run and teardown-retry
                # returns): a hanging suite never earns the advisory
                # invocation_error path, no matter which retry it hangs in.
                return TestRunResult(True, False, 0, 0, 1, retry_cmd,
                                     f"timed out after {timeout}s")
            passed2, failed2, errors2 = _parse_test_output(retry_cmd, output2)
            failure_blocks2 = _tap_failure_blocks(output2)
            ok2 = rc2 == 0
            if _is_invocation_error(rc2, output2, passed2, failed2, errors2):
                return TestRunResult(True, False, passed2, failed2, errors2,
                                     retry_cmd, output2[-8000:],
                                     invocation_error=True,
                                     failing_tests=_failing_ids(
                                         output2, repo_path=repo_path, work_dir=work_dir),
                                     passed_tests=_pytest_passed_tests(output2),
                                     failure_blocks=failure_blocks2)
            failing_tests2 = _failing_ids(output2, repo_path=repo_path, work_dir=work_dir)
            return TestRunResult(True, ok2, passed2, failed2, errors2,
                                 retry_cmd, output2[-8000:],
                                 failing_tests=failing_tests2,
                                 passed_tests=_pytest_passed_tests(output2),
                                 traceback_excerpts=_pytest_traceback_excerpts(output2, failing_tests2),
                                 failure_blocks=failure_blocks2)
        # No fixable retry — mark as invocation error
        return TestRunResult(True, False, passed, failed, errors,
                             cmd, output[-8000:], invocation_error=True,
                             failing_tests=failing_tests,
                             passed_tests=passed_tests,
                             failure_blocks=failure_blocks)
    return TestRunResult(True, ok, passed, failed, errors, cmd, output[-8000:],
                         failing_tests=failing_tests, passed_tests=passed_tests,
                         traceback_excerpts=_pytest_traceback_excerpts(output, failing_tests),
                         failure_blocks=failure_blocks)


@dataclass
class LintResult:
    ran: bool
    ok: bool
    command: str
    output: str

    @property
    def summary(self) -> str:
        if not self.ran:
            return "no lint run"
        return f"lint {'PASS' if self.ok else 'FAIL'}"


def run_lint_on_changed(
    repo_path: Path,
    lint_cmd: str | None = None,
    changed_files: list[str] | None = None,
    *,
    timeout: int = 120,
) -> LintResult:
    """Run the repo's lint command scoped to changed files when possible.

    If *changed_files* is provided and the lint command supports appending file
    args (ruff, flake8, eslint, mypy), only those files are checked. This
    avoids noise from pre-existing violations in untouched code.

    Returns ``LintResult(ran=False)`` if no lint command is available.
    """
    repo_path = Path(repo_path)
    if not lint_cmd:
        return LintResult(False, True, "", "no lint command configured")

    cmd = lint_cmd
    if changed_files:
        # Scope to changed files for linters that accept file arguments.
        ext_files = [f for f in changed_files if not f.endswith("/")]
        if ext_files and _lint_supports_file_args(lint_cmd):
            cmd = f"{lint_cmd} {' '.join(ext_files)}"

    try:
        proc = subprocess.run(
            cmd, cwd=repo_path, shell=True, capture_output=True, text=True,
            timeout=timeout, env=_env_for(Path(repo_path)),
        )
    except subprocess.TimeoutExpired:
        return LintResult(True, False, cmd, f"lint timed out after {timeout}s")
    output = ((proc.stdout or "") + (proc.stderr or ""))[-4000:]
    return LintResult(True, proc.returncode == 0, cmd, output)


def _lint_supports_file_args(cmd: str) -> bool:
    """Heuristic: common linters that accept file/path arguments."""
    for tool in ("ruff", "flake8", "pylint", "mypy", "eslint", "prettier", "black"):
        if tool in cmd.lower():
            return True
    return False


def run_command(
    repo_path: Path, command: str, *, timeout: int = 600,
    on_line: "Callable[[str], None] | None" = None,
) -> tuple[bool, int, str]:
    """Run an arbitrary shell command in ``repo_path``; return (ok, exit_code,
    output_tail). Used by `nh onboard` to PROVE a derived install/lint command by
    actually running it — the literal command, no agent in the loop.

    With *on_line* the identical command is streamed line-by-line instead of
    captured in one go (``_run_shell_streaming``) — same command, cwd, env and
    shell, so the proof is worth exactly the same."""
    if on_line is not None:
        rc, output, timed_out = _run_shell_streaming(
            command, Path(repo_path), timeout, dict(os.environ), on_line)
        if timed_out:
            return False, -1, f"timed out after {timeout}s"
        return rc == 0, (rc if rc is not None else -1), output[-4000:]
    try:
        proc = subprocess.run(
            command, cwd=repo_path, shell=True, capture_output=True, text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, -1, f"timed out after {timeout}s"
    output = ((proc.stdout or "") + (proc.stderr or ""))[-4000:]
    return proc.returncode == 0, proc.returncode, output


def _git_show(repo_path: Path, ref: str, path: str) -> str:
    # No `text=True`: that decodes stdout with the locale codec and RAISES
    # UnicodeDecodeError on a binary blob (e.g. a `.tgz` fixture under
    # `tests/`), which propagated out of `tamper_check_between` and killed a
    # whole run at scoring (first hit 2026-08-09, SWE-bench). Decoding bytes
    # ourselves with `errors="replace"` never raises; a binary blob decodes to
    # replacement-char noise that `tamper_guard.is_binary_content` then skips.
    proc = subprocess.run(
        ["git", "show", f"{ref}:{path}"],
        cwd=repo_path, capture_output=True,
    )
    if proc.returncode != 0:
        return ""
    return proc.stdout.decode("utf-8", errors="replace")


def _git_files(repo_path: Path, ref: str) -> list[str]:
    proc = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", ref],
        cwd=repo_path, capture_output=True, text=True,
    )
    return [f for f in proc.stdout.splitlines() if f] if proc.returncode == 0 else []


def run_held_out_tests(repo_path: Path, *, timeout: int = 120) -> TestRunResult | None:
    """Run held-out tests if tests/held_out/ exists. Returns None if absent.

    The held-out directory is intentionally not mentioned to the implementing
    agent, so it cannot be gamed. These results are passed to the reviewer as
    additional evidence the implementer never saw.
    """
    held_path = Path(repo_path) / "tests" / "held_out"
    if not held_path.exists():
        return None
    cmd = f"pytest -q {held_path}"
    try:
        proc = subprocess.run(
            cmd, cwd=repo_path, shell=True, capture_output=True, text=True,
            timeout=timeout, env=_env_for(Path(repo_path)),
        )
    except subprocess.TimeoutExpired:
        return TestRunResult(True, False, 0, 0, 1, cmd, f"timed out after {timeout}s")
    output = (proc.stdout or "") + (proc.stderr or "")
    passed, failed, errors = _parse_pytest(output)
    ok = proc.returncode == 0
    return TestRunResult(True, ok, passed, failed, errors, cmd, output[-4000:])


def test_file_diff(
    repo_path: Path, before_ref: str = "HEAD~1", after_ref: str = "HEAD",
) -> str:
    """The diff of the TEST FILES ONLY between two refs, "" when there is none.

    The adjudicator's whole evidence about what changed. Test files only, by the
    SAME `tamper_guard.is_test_file` predicate the guard itself counts with, so
    the diff it reads and the counts it is explaining are about the same set of
    files. Shipping the product diff too would hand the adjudicator the coder's
    comments and docstrings — a channel for exactly the self-advocacy the
    adjudicator must not receive.

    Best-effort by contract: a git failure returns "" and the adjudicator says
    it could not see the diff, which routes to CANNOT_DECIDE (park). It never
    raises, because a missing diff must not crash the pipeline the guard just
    stopped.
    """
    repo_path = Path(repo_path)
    try:
        proc = subprocess.run(
            ["git", "diff", "--name-only", f"{before_ref}..{after_ref}"],
            cwd=repo_path, capture_output=True, text=True,
        )
        if proc.returncode != 0:
            return ""
        paths = [p for p in proc.stdout.splitlines()
                 if p and tamper_guard.is_test_file(p)]
        if not paths:
            return ""
        proc = subprocess.run(
            ["git", "diff", "--no-color", f"{before_ref}..{after_ref}", "--", *paths],
            cwd=repo_path, capture_output=True, text=True,
        )
        return proc.stdout if proc.returncode == 0 else ""
    except Exception:  # noqa: BLE001 — no diff is a CANNOT_DECIDE, not a crash
        return ""


class TamperCheckUnavailable(RuntimeError):
    """The tamper guard could not be run — the checkout was not inspectable.

    Distinct from "the guard ran and found nothing" on purpose; see
    `tamper_check_between`.
    """


def _rev_parse(repo_path: Path, ref: str) -> str | None:
    proc = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
        cwd=repo_path, capture_output=True, text=True,
    )
    sha = proc.stdout.strip()
    return sha if proc.returncode == 0 and sha else None


def _merge_base(repo_path: Path, a: str, b: str) -> str | None:
    proc = subprocess.run(
        ["git", "merge-base", a, b], cwd=repo_path, capture_output=True, text=True,
    )
    sha = proc.stdout.strip()
    return sha if proc.returncode == 0 and sha else None


def _is_ancestor(repo_path: Path, ancestor: str, descendant: str) -> bool:
    proc = subprocess.run(
        ["git", "merge-base", "--is-ancestor", ancestor, descendant],
        cwd=repo_path, capture_output=True, text=True,
    )
    return proc.returncode == 0


def attempt_own_base(
    repo_path: Path, recorded_base: str, default_branch: str | None = None,
) -> str:
    """The tamper guard's comparison base — the attempt's OWN contribution.

    Equivalent to three-dot ``origin/<default_branch>...HEAD`` semantics: the
    window used for the before/after snapshot is
    ``merge-base(HEAD, current default branch)`` when that merge-base is a
    descendant of (strictly forward of, or equal to) ``recorded_base`` —
    otherwise ``recorded_base`` verbatim. Candidates are tried in preference
    order (``origin/<default_branch>`` first, then the local branch) and only
    ever move the base FORWARD from `recorded_base`, never sideways or back.

    Incident (2026-08-23, task b3463d74 attempt 7): the coder was instructed
    by an escalation reply to merge `origin/main` and resolve conflicts — the
    standard recovery for a branch that diverged. The guard at the time
    compared the merged tree against the task's ORIGINAL recorded base (never
    updated for the merge), so main's own landed commit b31b53f76 —
    `tests/test_readme_claims.py` gaining two skip markers — showed up inside
    the attempt's window and the guard fired: "tests/test_readme_claims.py
    gained skip markers (from 0 to 2), so some tests no longer run", escalating
    a false AMBIGUITY to a human. `git diff origin/main...<branch> --
    tests/test_readme_claims.py` showed ZERO skip lines added by the attempt
    on every branch of that task — the change was entirely main's, carried in
    by the sanctioned merge.

    Why this does not weaken the guard: a candidate base is only ever widened
    to commits that are ALSO reachable from the current default branch, i.e.
    already landed independently of this attempt. Anything the attempt itself
    adds, removes, or reverts — including reverting a test the default branch
    added mid-flight — is not reachable from the default branch alone and
    still falls inside the window, so it still fires. On a branch that never
    merges the default branch, the widened candidate never advances past
    `recorded_base` and behaviour is bit-identical to today.

    Fails closed: `default_branch` falsy, a ref that does not resolve
    (unmerged branch, single-branch clone, detached worktree, no remote), a
    non-git directory, or any git/OSError — all return `recorded_base`
    unchanged. Never raises. Local git reads only (`rev-parse`,
    `merge-base`), never a `git fetch`.
    """
    repo_path = Path(repo_path)
    if not default_branch:
        return recorded_base
    try:
        if not (repo_path / ".git").exists():
            return recorded_base
        if _rev_parse(repo_path, recorded_base) is None:
            return recorded_base

        best = recorded_base
        for candidate_ref in (f"origin/{default_branch}", default_branch):
            resolved = _rev_parse(repo_path, candidate_ref)
            if resolved is None:
                continue
            merge_base = _merge_base(repo_path, "HEAD", resolved)
            if merge_base is None:
                continue
            if _is_ancestor(repo_path, best, merge_base):
                best = merge_base
        return best
    except Exception:  # noqa: BLE001 — fail closed to the recorded base
        return recorded_base


def tamper_check_between(
    repo_path: Path, before_ref: str = "HEAD~1", after_ref: str = "HEAD",
    *, default_branch: str | None = None,
) -> tamper_guard.TamperReport:
    """Snapshot test files at two refs and run the tamper guard between them.

    Comparison base: `before_ref` verbatim, UNLESS `default_branch` is given,
    in which case it is first widened by `attempt_own_base` to
    `merge-base(HEAD, current default branch)` — three-dot
    `origin/<default_branch>...HEAD` semantics — so that changes the default
    branch landed mid-flight (e.g. via a sanctioned `merge origin/main`) are
    excluded from the attempt's window instead of being charged to it. See
    `attempt_own_base` for the full rationale and the 2026-08-23 incident
    (task b3463d74 attempt 7) that this parameter exists to fix. Callers that
    omit `default_branch` get exactly today's unwidened behaviour.

    Raises `TamperCheckUnavailable` when the checkout is not there to inspect.
    That is deliberately an ERROR and never a clean report: a missing worktree
    means the guard could not run, and a guard that answers "no tampering"
    when it did not look is worse than no guard — it launders the absence of
    evidence into evidence of absence. A resumed attempt whose worktree had
    been removed used to reach this function and die on an opaque
    FileNotFoundError raised by a subprocess several frames down, which is how
    it crashed the worker pool with no diagnosable cause (observed twice on
    2026-07-31).

    NOTE — the other half of that fix is NOT here: the orchestrator's call site
    still lets this propagate into the pool's crash handler. Turning it into a
    structured, task-visible attempt failure belongs there.
    """
    repo_path = Path(repo_path)
    if not repo_path.is_dir():
        raise TamperCheckUnavailable(
            f"cannot run the tamper guard: {repo_path} does not exist")
    if not (repo_path / ".git").exists():
        raise TamperCheckUnavailable(
            f"cannot run the tamper guard: {repo_path} is not a git checkout")
    if default_branch:
        before_ref = attempt_own_base(repo_path, before_ref, default_branch)
    before, after = {}, {}
    for path in _git_files(repo_path, before_ref):
        if tamper_guard.is_test_file(path):
            src = _git_show(repo_path, before_ref, path)
            if tamper_guard.is_binary_content(src):
                log.debug("tamper guard: skipping binary test-path file %s", path)
                continue
            before[path] = src
    for path in _git_files(repo_path, after_ref):
        if tamper_guard.is_test_file(path):
            src = _git_show(repo_path, after_ref, path)
            if tamper_guard.is_binary_content(src):
                log.debug("tamper guard: skipping binary test-path file %s", path)
                continue
            after[path] = src
    return tamper_guard.check(before, after)
