"""The reproduction-test gate: deterministic proof a diff does what it claims.

The coder records the tests that demonstrate its change in
``.no_human/repro_tests.json`` (``{"tests": ["tests/test_x.py::test_y", …]}``
— pytest node ids; the file is never committed, ``.no_human/**`` is excluded
from every commit). The gate then proves both directions before a single
reviewer token is spent:

  fails-before: the listed tests, copied into a worktree at the merge base,
                must FAIL there — otherwise they don't demonstrate the change
                (a bugfix that "reproduces" on healthy code reproduces nothing).
  passes-after: the same tests must PASS on the attempt's tree — otherwise the
                change doesn't do what its own tests claim.

Research basis: Agentless / SWE-Doctor / Google BRT — cogenerated
reproduction tests beat LLM review on cost and rival it on defect yield.
This gate is complementary to the adversarial reviewer, not a replacement.

Degradation is loud, never silent: no manifest → ``waived`` (the doctor
counts waiver rate), broken harness → ``error`` (never a fail), and the
whole gate sits behind ``repro_gate.mode`` (off | advisory | required). A
base-tree pytest run that COLLECTS ZERO tests (the declared file/id no
longer exists at the merge base — the base moved under the ticket) is
likewise ``error`` with a distinct "not found at base — re-anchor" reason,
never the "already passes at base" verdict: zero collection is a
selection/anchoring fact, not a test outcome (110655e5).
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..profile import ProjectProfile

MANIFEST = ".no_human/repro_tests.json"
_RUN_TIMEOUT = 600
# The sanity pre-run's own bound (SCRUM-78) — independent of _RUN_TIMEOUT so a
# broken/hung runner never costs a full fails-before/passes-after budget just
# to prove it can launch at all. A bare invocation may legitimately run a
# runner's whole default suite (jest/go test/etc. with no path given), so a
# large healthy suite can still time out here; that reads as an honest
# advisory "error" (never a false pass/fail), which is the fail-closed
# contract this gate promises — this ticket trades precision/cost, not safety.
# Kept well under _RUN_TIMEOUT (pinned by a test) so it can't silently
# drift back toward the old 600s cost.
_SANITY_TIMEOUT = 60


@dataclass
class ReproResult:
    verdict: str                    # "pass" | "fail" | "waived" | "error"
    tests: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    resume_shape: bool = False

    def to_json(self) -> dict:
        return {"verdict": self.verdict, "tests": self.tests,
                "reasons": self.reasons, "resume_shape": self.resume_shape}


SCHEMA_HINT = '{"tests": ["tests/test_x.py::test_y", ...]}'


def manifest_problem(repo_path: Path) -> str | None:
    """Why the manifest yields no tests — or None when it is absent or usable.

    Three states used to collapse into one reason, "no manifest": the file
    missing, unparseable JSON, and a well-formed JSON document of the wrong
    SHAPE. Task 89db42ea (2026-08-20) wrote a thoughtful manifest keyed
    ``repro_tests`` with per-test dicts — no ``tests`` list — and the required
    gate told it the file did not exist; the next attempt's distillate then
    named a non-existent file instead of a schema error, and the coder redid
    99 turns from base. An absent file is still ``None`` here (that is the
    honest "waived"); everything else names the real defect and the schema.
    """
    p = repo_path / MANIFEST
    if not p.is_file():
        return None
    try:
        # errors="replace": a non-UTF-8 byte must read as a JSON error, never
        # raise — this runs inside the PostToolUse hook, which had no failure
        # mode before it (the tamper guard once died on a binary test file
        # AFTER the coder was paid; same class).
        data = json.loads(p.read_text(errors="replace"))
    except OSError as exc:
        return f"{MANIFEST} is present but unreadable ({exc.__class__.__name__})"
    except json.JSONDecodeError as exc:
        return f"{MANIFEST} is present but is not valid JSON ({exc.msg} at line {exc.lineno})"
    if not isinstance(data, dict):
        return (f"{MANIFEST} is present but its top level is "
                f"{type(data).__name__}, not an object — expected {SCHEMA_HINT}")
    tests = data.get("tests")
    if not isinstance(tests, list):
        keys = ", ".join(sorted(map(str, data.keys()))) or "none"
        return (f"{MANIFEST} is present but has no \"tests\" list (keys found: "
                f"{keys}) — expected {SCHEMA_HINT}")
    if not [str(t).strip() for t in tests if str(t).strip()]:
        return f"{MANIFEST} is present but its \"tests\" list is empty — expected {SCHEMA_HINT}"
    return None


def read_manifest(repo_path: Path) -> list[str]:
    """The declared repro tests, or [] (no manifest / unreadable / empty)."""
    p = repo_path / MANIFEST
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text(errors="replace"))
    except (OSError, json.JSONDecodeError):
        return []
    tests = data.get("tests") if isinstance(data, dict) else None
    if not isinstance(tests, list):
        return []
    return [str(t).strip() for t in tests if str(t).strip()]


def task_manifest_path(task_id: str, *, home: Path | None = None) -> Path:
    """Where THIS TASK's repro manifest is persisted, outside any worktree.

    ``.no_human/**`` is gitignored and excluded from every commit (see this
    module's docstring), so a passing gate's manifest lives only as
    untracked state in the attempt's worktree. A worktree re-created for
    the same task (new pid -> new ``~/.no_human/worktrees/<task>.<pid>.<hash>``)
    starts without it — this is the per-task copy that survives that,
    living under the same ``artifacts/<task_id>/`` directory the
    verification log already writes to.

    ``NO_HUMAN_HOME`` is imported lazily (only when *home* is omitted) so
    this module keeps its "no orchestrator, no config at import time"
    posture — tests pass an explicit ``home=tmp_path`` instead.
    """
    if home is None:
        from ..config import NO_HUMAN_HOME
        home = NO_HUMAN_HOME
    return Path(home) / "artifacts" / task_id / "repro_tests.json"


def persist_manifest(repo_path: Path, task_id: str, *, home: Path | None = None) -> bool:
    """Copy the worktree's manifest to the per-task store. True on success.

    Only when the worktree copy exists AND is well-formed
    (:func:`manifest_problem` is ``None``) — a junk or missing manifest must
    never be enshrined as the task's record. Idempotent (last-write-wins on
    a single file) and best-effort: any ``OSError`` degrades to ``False``,
    never raises.
    """
    src = repo_path / MANIFEST
    if not src.is_file() or manifest_problem(repo_path) is not None:
        return False
    dst = task_manifest_path(task_id, home=home)
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(src.read_bytes())
    except OSError:
        return False
    return True


def restore_manifest(repo_path: Path, task_id: str, *, home: Path | None = None) -> bool:
    """Copy the persisted per-task manifest into a worktree lacking one.

    Returns ``True`` only when a file was actually written. Never overwrites
    a worktree copy that already exists (the live tree always wins — this
    never clobbers what the coder wrote this attempt) and never restores a
    persisted copy that does not parse clean (re-validated the same way
    :func:`persist_manifest` does, via a scratch directory so the checked
    copy is byte-identical to what gets written). Best-effort: any
    ``OSError`` degrades to ``False``, never raises.
    """
    dst = repo_path / MANIFEST
    if dst.is_file():
        return False
    src = task_manifest_path(task_id, home=home)
    if not src.is_file():
        return False
    try:
        data = src.read_bytes()
    except OSError:
        return False
    tmp = Path(tempfile.mkdtemp(prefix="nh-repro-restore-"))
    try:
        (tmp / MANIFEST).parent.mkdir(parents=True, exist_ok=True)
        (tmp / MANIFEST).write_bytes(data)
        if manifest_problem(tmp) is not None or not read_manifest(tmp):
            return False
    except OSError:
        return False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(data)
    except OSError:
        return False
    return True


def has_persisted_manifest(task_id: str, *, home: Path | None = None) -> bool:
    """True when this task's gate has passed at least once before — the
    fact a lost-manifest regenerate nudge is gated on (there is nothing to
    "regenerate" without a prior pass)."""
    return task_manifest_path(task_id, home=home).is_file()


def _test_files(tests: list[str]) -> list[str]:
    """The file part of each pytest node id, deduplicated, order kept."""
    seen: dict[str, None] = {}
    for t in tests:
        seen.setdefault(t.split("::", 1)[0], None)
    return list(seen)


def declared_test_files(repo_path: Path) -> list[str]:
    """The FILE paths the manifest declares (node ids stripped), order kept.

    The one seam callers outside the gate use to ask what was declared — the
    gate's own missing-file check (:func:`run_repro_gate`) is unchanged and
    remains the backstop."""
    return _test_files(read_manifest(repo_path))


def _pytest_python(repo_path: Path) -> str | None:
    """The interpreter to run the repro tests with, or None if none is usable.

    Normally ``sys.executable`` — the interpreter no_human runs under, which has
    pytest — because a bare ``python`` does not exist in a uv/venv project
    (the 2026-07-11 bug: every bugfix task escalated on "could not run pytest:
    No such file or directory: 'python'").

    But in a PyInstaller-frozen build ``sys.executable`` is the frozen ``nh``
    binary, NOT a Python interpreter. Invoking it with ``-m pytest`` re-runs the
    click CLI (nh_entry.main), which exits non-zero without running a single
    test — so the gate read ``ran=True, all_passed=False`` and returned a
    confident but FALSE ``fail`` for every bugfix. In a frozen build fall back to
    a real interpreter: the target repo's own venv first (it has the repo's deps
    and pytest), then ``python3``/``python`` on PATH. None → the caller fails
    closed to ``error`` (advisory), never a false pass/fail."""
    if not getattr(sys, "frozen", False):
        return sys.executable
    from .runner import _venv_bin

    from .runner import _IS_WINDOWS

    bin_dir = _venv_bin(repo_path)
    if bin_dir is not None:
        # `_venv_bin` returns `<venv>\Scripts` on Windows, where the
        # interpreter is `python.exe` — an extensionless `python` there is not
        # a file, so this returned a path that does not exist.
        return str(bin_dir / ("python.exe" if _IS_WINDOWS else "python"))
    for name in ("python3", "python"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _run_pytest_proc(
    tests: list[str], cwd: Path, env: dict, python: str,
) -> tuple[int | None, str]:
    """(returncode, tail_of_output). Never raises.

    ``returncode`` is ``None`` when pytest could not even be launched or
    could not load at all (missing interpreter, timeout, pytest not
    importable) — an ENVIRONMENT failure indistinguishable from any exit
    code, so the caller must treat it as "could not run" rather than infer
    anything from it. Any other value is the real pytest exit code (0-5),
    including 5 ("no tests collected"), which IS meaningful and is left for
    the caller to classify (see :func:`_nothing_executed`)."""
    try:
        proc = subprocess.run(
            [python, "-m", "pytest", "-x", "-q", "--no-header", *tests],
            cwd=cwd, env=env, capture_output=True, text=True,
            timeout=_RUN_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return None, f"timed out after {_RUN_TIMEOUT}s"
    except OSError as exc:
        return None, f"could not run pytest: {exc}"
    out = (proc.stdout + proc.stderr)[-2000:]
    # The interpreter can't even load pytest (a bare system python3 fallback,
    # or the frozen binary re-running the CLI). That is an environment failure,
    # NOT a test verdict — treat as not-ran so the caller returns "error", never
    # a false "fail".
    if "No module named pytest" in out or "No module named 'pytest'" in out:
        return None, out
    return proc.returncode, out


def _run_pytest(
    tests: list[str], cwd: Path, env: dict, python: str,
) -> tuple[bool, bool, str]:
    """(ran, all_passed, tail_of_output). Never raises.

    ``ran`` is False when pytest could not be launched or execute at all
    (missing interpreter, timeout, pytest not importable, collection error with
    0 tests) — an ENVIRONMENT failure, which the caller must classify as "error"
    (advisory) rather than "fail" (a real fails-before/passes-after verdict).
    ``python`` is resolved by :func:`_pytest_python` — ``sys.executable`` in a
    normal install, a real interpreter in a frozen build where ``sys.executable``
    is the nh binary.

    A thin wrapper over :func:`_run_pytest_proc` — kept so this exact
    3-tuple contract (used directly by callers in tests/test_repro_gate.py)
    never changes."""
    returncode, out = _run_pytest_proc(tests, cwd, env, python)
    if returncode is None:
        return False, False, out
    # pytest exit 5 = "no tests collected" — an environment/selection problem,
    # not a test verdict. Anything that ran at least one test gives 0-4.
    ran = returncode != 5 and "no tests ran" not in out.lower()
    return ran, returncode == 0, out


_EXECUTED_RE = re.compile(r"\b\d+\s+(passed|failed|errors?|xpassed|xfailed)\b")


def _nothing_executed(returncode: int, out: str) -> str | None:
    """Why this pytest run executed NO test — or None when at least one ran.

    Zero-collection is a SELECTION/anchoring fact, never a test verdict: an
    exit-0 run that collected nothing is not "the tests pass" (110655e5 —
    the declared repro file did not exist at the merge base, and the old
    "ran, exit 0" reading booked it as a genuine pass-at-base and killed the
    attempt with a misleading "already passes" reason). Checked in this
    order because a run can match more than one signal (e.g. exit 5 also
    lacks a "N passed" line). The final branch is a deliberately
    return-code-agnostic catch-all: a module-level ``pytest.skip(...,
    allow_module_level=True)`` guard invoked by node id exits **4** with
    "ERROR: found no collectors ... / 1 skipped" (verified empirically —
    it is not exit 0 as a naive reading of pytest's docs would suggest, and
    it names no "not found"/"file or directory not found" text either), so
    gating this on a specific returncode or error string misses it. The
    final branch's actual, narrow signal: no ``N passed/failed/error(s)/
    xpassed/xfailed`` COUNT line anywhere in the captured output. That
    reliably separates the skip/deselect/zero-collection shapes above from a
    genuine failure (exit 1, "1 failed") or a genuine collection error that
    itself prints a one-line summary (exit 4, "1 error in 0.02s" — e.g. an
    ImportError inside the declared test module) — those keep matching
    ``_EXECUTED_RE`` and are therefore left as "something ran", falling
    through unchanged to the pre-existing ok_before/ran_before handling.
    That is a real, KNOWN blind spot, not a claim this function resolves
    every zero-collection shape: it is a review-caught limit on what
    "executed" means here, deliberately left alone rather than widened,
    because a base-tree collection error is an existing, separately-scoped
    ambiguity (main already reads it as a fails-before "pass", right or
    wrong) that this ticket does not touch. Also out of reach in principle:
    this is a TEXT match over output the base tree's own pytest config
    controls — a base ``addopts = -s`` plus a conftest banner that itself
    prints a fake "N passed" line can defeat it; there is no signal here
    that is not, in the end, generated by the tree under judgment. Finally,
    this classifies one WHOLE pytest run (all declared tests together), not
    per-test: if a multi-test manifest has one test that genuinely executes
    at base, its count line masks a *different* declared test in the same
    run that collected zero — the existing "already pass at base" message
    can still surface for that case. Narrowing to per-test attribution would
    require parsing pytest's per-node result lines, which is out of scope
    here."""
    low = out.lower()
    if returncode == 5 or "no tests ran" in low:
        return "pytest collected no tests (exit 5)"
    if "collected 0 items" in out:
        return "pytest collected 0 items"
    if not _EXECUTED_RE.search(out):
        return "pytest exited without executing any test (all skipped/deselected/uncollectable)"
    return None


def _is_python_profile(profile: "ProjectProfile | None") -> bool:
    """True when the repro run should go through pytest — the default when no
    profile is given (unchanged pre-SCRUM-65 behaviour), or when the profile's
    declared ecosystem is unset or python-flavoured ("python-pytest" etc.).
    Any other declared ecosystem ("node", "maven", "go", ...) routes through
    the profile's own ``test_cmd`` instead — pytest cannot run JS/Go/Java
    tests, so keeping this pytest-only silently no-ops the repro guarantee for
    every non-Python repo (SCRUM-65)."""
    if profile is None:
        return True
    eco = (profile.ecosystem or "").strip().lower()
    return not eco or eco.startswith("python")


def _parse_test_cmd(test_cmd: str | None) -> list[str] | None:
    """The profile's ``test_cmd`` split into argv, or None if missing, blank,
    unparseable (e.g. mismatched quotes), or containing an un-interpolated
    template placeholder — the caller fails closed to 'error' rather than
    guess an invocation or forward a literal ``{token}`` to a foreign runner
    (SCRUM-65 review item 4)."""
    if not test_cmd or not test_cmd.strip():
        return None
    try:
        argv = shlex.split(test_cmd)
    except ValueError:
        return None
    if not argv:
        return None
    if any("{" in tok or "}" in tok for tok in argv):
        return None
    return argv


def _test_cmd_targets(tests: list[str]) -> list[str]:
    """Test-target arguments for a non-Python ``test_cmd``.

    pytest node ids (``path::case``) mean nothing to jest/mocha/go test — a
    raw ``::`` would reach a foreign runner verbatim (SCRUM-65 review item 4).
    Strip to the deduplicated file part, same convention as ``_test_files``."""
    return _test_files(tests)


def _runner_sanity_check(argv: list[str], cwd: Path, env: dict) -> tuple[bool, str]:
    """Prove the runner behind ``argv`` is actually AVAILABLE in ``cwd`` —
    before trusting any exit code from a test-targeted invocation there as a
    genuine fails-before verdict.

    A ``git worktree add`` checkout of ``base_ref`` contains only tracked
    files — installed deps (node_modules, a vendored module cache, ...) are
    not — so an otherwise-working test_cmd can fail to even launch in that
    isolated worktree for an ENVIRONMENT reason that has nothing to do with
    the bug under test (SCRUM-65 review: a JS repro's fails-before step
    exited 127/1 from a missing runner/dependency, and any non-zero exit was
    being read as "bug reproduced" — a vacuous, always-true repro). We never
    provision deps here (no npm install — slow and out of scope); an honest
    refusal is the correct outcome.

    SCRUM-78 tried to let a runner that RAN but found no tests / reported
    real failures ("ran, exit != 0") through, distinguishing it from "never
    started", using elapsed wall-clock as the discriminator. Review rejected
    that: a slow environment failure (e.g. a missing-dependency import that
    takes over a second to raise) is indistinguishable from a slow real
    failure by timing alone, and misclassifying it as "ran" resurrects the
    exact vacuous-repro bug SCRUM-65 fixed (proven by
    ``test_non_python_slow_missing_dep_is_advisory_error_not_pass`` in
    tests/test_repro_gate.py, which fails a pure-timing discriminator). A
    generic probe (``--version``/``--help``) doesn't generalize either —
    ``test_cmd`` is an arbitrary profile-supplied string across every
    ecosystem, and a probe a healthy runner fails is a new false-refusal
    class. There is no reliable way to tell "ran, found failures" apart from
    "never really started" without provisioning the worktree's dependencies,
    which is out of scope here — so this refuses on ANY non-zero exit,
    matching the pre-SCRUM-78 contract. What SCRUM-78 keeps: a short,
    independent timeout (``_SANITY_TIMEOUT`` vs the old ``_RUN_TIMEOUT``) and
    a distinct, honest reason string per launch-failure mode, including a
    process killed by a signal (crash/OOM) — which "ran to completion" never
    covers, timing or not."""
    if not argv:
        return False, "no test runner command configured"
    try:
        proc = subprocess.run(
            argv, cwd=cwd, env=env, capture_output=True, text=True,
            timeout=_SANITY_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return False, (
            f"sanity check timed out after {_SANITY_TIMEOUT}s (runner hung "
            "or stalled)")
    except OSError as exc:
        return False, f"test runner could not be launched (system error): {exc}"
    out = (proc.stdout + proc.stderr)[-2000:]
    if proc.returncode < 0:
        return False, (
            f"test runner was killed by signal {-proc.returncode} "
            f"(crash/OOM) — not a test verdict:\n{out}")
    if proc.returncode == 127:
        return False, f"test runner not found (command not found):\n{out}"
    if proc.returncode == 126:
        return False, f"test runner is not executable (exit 126):\n{out}"
    if proc.returncode == 0:
        return True, out
    return False, (
        f"test runner exited {proc.returncode} when launched bare with no "
        "test-file arguments in the isolated base worktree — a dependency "
        "failure and a real test failure are indistinguishable here without "
        f"provisioning deps, so this refuses rather than risk a fabricated "
        f"fails-before verdict:\n{out}")


def _run_test_cmd(
    argv: list[str], tests: list[str], cwd: Path, env: dict,
) -> tuple[bool, bool, str]:
    """(ran, all_passed, tail_of_output) for a non-Python ``profile.test_cmd``.

    Mirrors ``_run_pytest``'s contract but stays language-agnostic: the
    touched test files are appended as positional arguments (works across
    jest, mocha, go test, etc. without special syntax), and ``ran`` is False
    when the command itself could not be launched, OR when it exits 126/127
    ("command not found" / "not executable" — the shell's own signal that
    nothing ran, not a test verdict). pytest's exit-code-5 / 'no tests ran'
    conventions don't generalize across test runners, so we don't try to
    guess collection state generically beyond that — the base-not-found
    ("repro tests not found at base") classification is pytest-only."""
    try:
        proc = subprocess.run(
            [*argv, *tests], cwd=cwd, env=env, capture_output=True, text=True,
            timeout=_RUN_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return False, False, f"timed out after {_RUN_TIMEOUT}s"
    except OSError as exc:
        return False, False, f"could not run test_cmd: {exc}"
    out = (proc.stdout + proc.stderr)[-2000:]
    if proc.returncode in (126, 127):
        return False, False, (
            f"test runner exited {proc.returncode} (not found / not "
            f"executable — environment failure, not a test verdict):\n{out}")
    return True, proc.returncode == 0, out


def run_repro_gate(
    repo_path: Path, base_ref: str, profile: "ProjectProfile | None" = None,
    *, resume_shape: bool = False,
) -> ReproResult:
    """Prove fails-before / passes-after for the declared repro tests.

    ``base_ref`` is the code before this task's changes (the merge base of the
    attempt branch and the target branch). The before-run copies the test
    files into a temporary worktree at ``base_ref`` and runs them with the
    primary repo's venv on PYTHONPATH-priority, so worktree code shadows any
    editable install of the primary checkout.

    ``profile`` is the repo's confirmed :class:`ProjectProfile` (or None). A
    Python profile (or no profile — the historical default) runs the repro
    tests with pytest, unchanged. Any other declared ecosystem routes the same
    fails-before/passes-after proof through the profile's own ``test_cmd``
    instead (SCRUM-65), so the guarantee is not pytest-only.

    ``resume_shape`` (keyword-only, default ``False``) covers a RESUMED
    attempt: one that branches from a ``[WIP-BLOCKED]``/``[WIP-PARTIAL]``
    checkpoint rather than from the task's true base. The caller is
    responsible for handing this function a ``base_ref`` that resolves to
    that TRUE pre-work base (never the checkpoint itself — see
    ``Orchestrator._repro_base_ref``); this flag stamps ``resume_shape`` on
    the result and prefixes the fail reasons with ``resume-shape:`` — it does
    not change the stated CAUSE, which is identical to the non-resume path
    (the declared repro tests already pass at the named base ref, so they do
    not demonstrate this change). The red-first contract is NOT relaxed: both
    directions (fail-on-base AND pass-on-tip) are still required for
    ``pass``, exactly as the default path requires. When ``resume_shape`` is
    ``False`` (the default) this function's behaviour, reasons and return
    values are byte-identical to before this parameter existed.
    """
    tests = read_manifest(repo_path)
    if not tests:
        # Same verdict either way — the attempt is sent back under `required`
        # — but the REASON must name what is actually wrong, because it is
        # what the next attempt reads.
        problem = manifest_problem(repo_path)
        return ReproResult("waived", reasons=[problem or f"no {MANIFEST} manifest"])

    python: str | None = None
    test_argv: list[str] | None = None
    if _is_python_profile(profile):
        # In a frozen build sys.executable is the nh binary, not a Python
        # interpreter — resolve a real one, or fail closed to "error" (a
        # false verdict is worse than an honest "could not verify").
        python = _pytest_python(repo_path)
        if python is None:
            return ReproResult("error", tests=tests, reasons=[
                "no Python interpreter available to run the repro tests "
                "(frozen build with no target-repo venv and no python on "
                "PATH) — the gate fails closed rather than guess a verdict"])
    else:
        test_argv = _parse_test_cmd(profile.test_cmd if profile else None)
        if test_argv is None:
            return ReproResult("error", tests=tests, reasons=[
                "no usable profile.test_cmd to run the repro tests for "
                f"ecosystem {(profile.ecosystem if profile else '')!r} "
                "(missing or unparseable) — the gate fails closed rather "
                "than guess a verdict"])

    def _run(tests_: list[str], cwd: Path, env: dict) -> tuple[bool, bool, str]:
        if python is not None:
            return _run_pytest(tests_, cwd, env, python)
        return _run_test_cmd(test_argv, tests_, cwd, env)

    # pytest keeps its node ids (``path::case``); a foreign runner only gets
    # file paths — a raw "::" would reach jest/mocha/go test verbatim.
    target_files = tests if python is not None else _test_cmd_targets(tests)

    from .runner import _env_for  # venv-aware env (the c0df0da lesson)
    env = _env_for(repo_path)

    # passes-after first: cheapest to check, and a manifest whose tests fail
    # on the attempt's own tree is wrong regardless of the before-state.
    missing = [f for f in _test_files(tests) if not (repo_path / f).is_file()]
    if missing:
        return ReproResult("fail", tests=tests, reasons=[
            f"declared test file(s) missing from the attempt tree: {missing} "
            "— a listed repro test may not be deleted"])
    if python is not None:
        after_env = {**env, "PYTHONPATH": os.pathsep.join(
            [str(repo_path), str(repo_path / "src"), env.get("PYTHONPATH", "")])}
    else:
        after_env = env
    ran_after, ok_after, out_after = _run(target_files, repo_path, after_env)
    if not ran_after:
        # Could not RUN the tests (env/interpreter/collection) — "can't verify"
        # is not "doesn't pass". Advisory error, never blocks the task.
        return ReproResult("error", tests=tests, reasons=[
            f"repro tests could not be executed:\n{out_after}"])
    if not ok_after:
        if resume_shape:
            return ReproResult("fail", tests=tests, resume_shape=True, reasons=[
                "resume-shape: pass-on-tip failed — the declared repro "
                "tests do not pass on the attempt's own tree:\n"
                f"{out_after}"])
        return ReproResult("fail", tests=tests, reasons=[
            "passes-after failed — the declared repro tests do not pass on "
            f"the attempt's own tree:\n{out_after}"])

    # fails-before: worktree at base_ref + the test files from the after tree.
    tmp = Path(tempfile.mkdtemp(prefix="nh-repro-"))
    worktree = tmp / "base"
    try:
        added = subprocess.run(
            ["git", "worktree", "add", "--detach", str(worktree), base_ref],
            cwd=repo_path, capture_output=True, text=True,
        )
        if added.returncode != 0:
            return ReproResult("error", tests=tests, reasons=[
                f"could not build the base worktree at {base_ref}: "
                f"{added.stderr.strip()[:300]}"])
        if test_argv is not None:
            # The worktree has only tracked files — no installed deps. Prove
            # the runner itself works there BEFORE trusting any exit code
            # from the real (test-targeted) invocation as a fails-before
            # verdict; a broken/dep-less runner must never read as "the bug
            # reproduced" (SCRUM-65 review).
            sane, sane_reason = _runner_sanity_check(test_argv, worktree, env)
            if not sane:
                return ReproResult("error", tests=tests, reasons=[
                    "repro runner unavailable in the isolated base worktree "
                    "— the gate fails closed rather than read an environment "
                    f"failure as a fails-before verdict: {sane_reason}"])
        for f in _test_files(tests):
            src, dst = repo_path / f, worktree / f
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        if python is not None:
            before_env = {**env, "PYTHONPATH": os.pathsep.join(
                [str(worktree), str(worktree / "src"), env.get("PYTHONPATH", "")])}
        else:
            before_env = env
        if python is not None:
            rc_before, out_before = _run_pytest_proc(
                target_files, worktree, before_env, python)
            if rc_before is None:
                ran_before, ok_before = False, False
            else:
                why = _nothing_executed(rc_before, out_before)
                if why is not None:
                    # {why} must lead — the orchestrator's repro_gate event
                    # emits only reasons[0][:200], and base_ref (a 40-hex
                    # sha) plus the joined test-id list can by themselves
                    # exceed 200 chars, pushing the actual diagnosis (why
                    # nothing executed) past the cut (review finding).
                    return ReproResult("error", tests=tests, reasons=[
                        "repro tests not found at base — the base moved "
                        f"under this ticket; re-anchor the repro. Why: {why}. "
                        f"At base ref {base_ref} the declared test(s) "
                        f"({', '.join(tests)}) produced no pass/fail. "
                        f"Re-point {MANIFEST} at test(s) that exist and "
                        "fail at this base, or rebase the attempt onto "
                        f"the current base.\n{out_before[-1000:]}"])
                ran_before, ok_before = True, rc_before == 0
        else:
            ran_before, ok_before, out_before = _run(target_files, worktree, before_env)
        if not ran_before:
            return ReproResult("error", tests=tests, reasons=[
                f"base-tree repro run could not execute:\n{out_before}"])
        if ok_before:
            named = ", ".join(tests)
            cause = (
                "the declared repro tests already pass at base ref "
                f"{base_ref} ({named}), so they do not demonstrate this change"
            )
            if resume_shape:
                return ReproResult("fail", tests=tests, resume_shape=True, reasons=[
                    f"resume-shape: fails-before failed — {cause}"])
            return ReproResult("fail", tests=tests, reasons=[
                f"fails-before failed — {cause}"])
        return ReproResult("pass", tests=tests, resume_shape=resume_shape)
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", str(worktree)],
                       cwd=repo_path, capture_output=True)
        shutil.rmtree(tmp, ignore_errors=True)
