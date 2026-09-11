"""Per-edit net-new type diagnostics (issue #114, phase 2).

No type checker is REQUIRED here: every test mocks the checker, so the suite is
green on a machine with none installed and nothing reaches a model API. The
mock is installed on `no_human.review.type_evidence`'s `shutil`/`subprocess`
because that is the module whose `_run_checker` this hook deliberately reuses
rather than reimplementing.

The fixture repos are real directories under `tmp_path` rather than a fake path
string: `_relative` resolves through `os.path.realpath`, and a non-existent root
on Windows resolves to a drive-qualified path that would make the assertions
here agree with the code for the wrong reason.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from no_human.agent.lint_hook import LintFeedbackHook, _EDIT_TOOLS
from no_human.agent.type_hook import (
    MAX_FEEDBACK_DIAGNOSTICS,
    TYPE_HOOK_SESSION_BUDGET,
    TYPE_HOOK_TIMEOUT,
    TypeFeedbackHook,
    _PER_EDIT_CHECKERS,
    per_edit_checker,
)
from no_human.agent import type_hook as th
from no_human.review.type_evidence import TypeDiagnostic

#: `_run_checker` resolves the binary through `shutil.which` before spawning, so
#: a fake run has to supply a resolution as well as a result. Outside any
#: fixture repo, because `_resolve_binary` refuses a binary that resolves inside
#: the tree under work.
_FAKE_BIN = "/opt/nh-test-tools/checker"


@pytest.fixture(autouse=True)
def _isolated_temp_root(tmp_path, monkeypatch):
    """Every test in this file gets its own temp root, and none may touch the
    real one.

    ISOLATION FIRST. Any test that drives the hook far enough to build a mypy
    argv creates a cache directory, and without a redirect that lands in the
    machine's shared temp root — `/tmp` on Linux CI. The module's cache used
    to be one derived directory per repo path that nothing ever removed (976
    had accumulated on one developer machine), and an earlier version of the
    POSIX tests went further and chmodded one to 0777, planting in `/tmp` the
    exact hazard `_dir_is_private` exists to refuse.

    THEN THE ASSERTION, because isolation that silently stops working is worth
    nothing. Checked against what each test ADDS rather than what it finds, so
    a machine carrying leftovers from the old design fails on the leak that
    produced them and not on every run afterwards.

    Caught the hard way: with the redirect missing, this file passed on a
    machine where the shared root already existed and failed on a clean one —
    which is exactly what a CI runner is.
    """
    real_root = Path(tempfile.gettempdir())
    pattern = "nh-typehook-mypy*"
    before = set(real_root.glob(pattern))

    private = tmp_path / "temp-root"
    private.mkdir()
    monkeypatch.setattr(th.tempfile, "gettempdir", lambda: str(private))

    yield

    leaked = sorted(q.name for q in set(real_root.glob(pattern)) - before)
    assert not leaked, f"test reached the real temp root: {leaked[:5]}"


class _FakeChecker:
    """Returns queued `(stdout, returncode)` pairs, one per spawn.

    Queued rather than keyed on tree content (phase 1's approach): what is under
    test here is a SEQUENCE of runs over one tree, so the run index is the only
    thing that distinguishes them. Running dry is an assertion failure, not a
    default — a test that spawns more checkers than it queued is not testing
    what it says.
    """

    def __init__(self, *results: tuple[str, int]):
        self.results = list(results)
        self.calls: list[tuple[tuple[str, ...], Path]] = []
        self.env: dict[str, str] | None = None

    def __call__(self, argv, **kwargs):
        self.calls.append((tuple(argv), Path(kwargs["cwd"])))
        self.env = kwargs.get("env")
        self._write_cache_like_mypy(list(argv), Path(kwargs["cwd"]))
        assert self.results, f"unqueued checker spawn #{len(self.calls)}"
        out, rc = self.results.pop(0)
        return subprocess.CompletedProcess(argv, rc, out, "")

    @staticmethod
    def _write_cache_like_mypy(argv: list[str], cwd: Path) -> None:
        """Emulate the ONE side effect that matters: mypy writes a cache tree,
        at `--cache-dir` when given and at `<cwd>/.mypy_cache` when not.

        Without this the fake is a pure function, and
        `test_a_run_writes_nothing_inside_the_repo` — which walks the repo
        before and after — could never fail, because no process ever ran and no
        file could ever appear. Deleting `--cache-dir` from the argv, i.e.
        reintroducing the exact phase 1 defect, left that test green. With the
        write emulated it goes red, which is the only reason to keep it.
        """
        if "--no-error-summary" not in argv:      # not the mypy invocation
            return
        if "--cache-dir" in argv:
            target = Path(argv[argv.index("--cache-dir") + 1])
        else:
            target = cwd / ".mypy_cache"
        try:
            target.mkdir(parents=True, exist_ok=True)
            (target / "cache.data.json").write_text("{}", encoding="utf-8")
        except OSError:
            pass


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "app.py").write_text("x = 1\n", encoding="utf-8")
    return repo


def _hook(repo: Path, checker: str = "mypy", **kw) -> tuple[TypeFeedbackHook, list]:
    events: list[tuple[str, str]] = []
    hook = TypeFeedbackHook(
        repo_path=repo, checker=checker,
        on_event=lambda k, t: events.append((k, t)), **kw,
    )
    return hook, events


def _edit(path: str, tool: str = "Edit") -> dict:
    return {"tool_name": tool, "tool_input": {"file_path": path}}


async def _fire(hook: TypeFeedbackHook, fake: _FakeChecker, payload: dict) -> dict:
    with patch("no_human.review.type_evidence.shutil.which", return_value=_FAKE_BIN), \
         patch("no_human.review.type_evidence.subprocess.run", side_effect=fake):
        return await hook.hook(payload, "tool-use-id", None)


#: mypy text for one error, and the same error one line lower.
_ERR = 'pkg/app.py:2:5: error: Incompatible return value type  [return-value]'
_ERR_SHIFTED = 'pkg/app.py:9:5: error: Incompatible return value type  [return-value]'
_OTHER = 'pkg/app.py:4:1: error: Name "q" is not defined  [name-defined]'


def _diag(path="pkg/app.py", line=2, column=5, code="return-value", message="bad"):
    return TypeDiagnostic(
        path=path, line=line, column=column, code=code, message=message
    )


# --------------------------------------------------------------------------- #
# Detection — nothing runs on a repo that did not ask for it                   #
# --------------------------------------------------------------------------- #

def test_a_repo_configuring_nothing_gets_no_checker(tmp_path):
    assert per_edit_checker(_repo(tmp_path)) is None


def test_a_repo_configuring_mypy_gets_mypy(tmp_path):
    repo = _repo(tmp_path)
    (repo / "mypy.ini").write_text("[mypy]\n", encoding="utf-8")
    assert per_edit_checker(repo) == "mypy"


def test_pyright_wins_over_mypy_exactly_as_phase_one_decides(tmp_path):
    """Precedence is phase 1's, not a second opinion: `detect_type_checkers`
    already returns at most one Python checker and prefers pyright, and this
    filter must not reorder that."""
    repo = _repo(tmp_path)
    (repo / "pyrightconfig.json").write_text("{}", encoding="utf-8")
    (repo / "mypy.ini").write_text("[mypy]\n", encoding="utf-8")
    assert per_edit_checker(repo) == "pyright"


def test_a_typescript_only_repo_gets_no_per_edit_checker(tmp_path):
    """The PYTHON ONLY ceiling, at the only place it is enforced. A per-file
    `tsc` run ignores tsconfig.json, so it would answer a different question
    than the repo's own configuration asks."""
    repo = _repo(tmp_path)
    (repo / "tsconfig.json").write_text("{}", encoding="utf-8")
    assert per_edit_checker(repo) is None
    assert "tsc" not in _PER_EDIT_CHECKERS


def test_a_tsc_repo_still_gets_a_python_checker_when_it_configures_one(tmp_path):
    repo = _repo(tmp_path)
    (repo / "tsconfig.json").write_text("{}", encoding="utf-8")
    (repo / "mypy.ini").write_text("[mypy]\n", encoding="utf-8")
    assert per_edit_checker(repo) == "mypy"


def test_an_unsupported_checker_name_disables_the_hook_instead_of_raising(tmp_path):
    """`tsc` cannot reach the extension gate, because `_PER_EDIT_CHECKERS[...]`
    would raise KeyError there — an exception inside a PostToolUse hook, i.e. a
    guard breaking the session it exists to guard. Normalised to off in
    `__init__` instead, so a future caller that passes it through is degraded,
    not fatal."""
    hook = TypeFeedbackHook(repo_path=_repo(tmp_path), checker="tsc")
    assert hook.checker is None


# --------------------------------------------------------------------------- #
# The gates — what never reaches a subprocess                                  #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("payload", [
    {"tool_name": "Read", "tool_input": {"file_path": "pkg/app.py"}},
    {"tool_name": "Bash", "tool_input": {"command": "ls"}},
    {"tool_name": "Grep", "tool_input": {"pattern": "x"}},
])
async def test_a_non_edit_tool_spawns_nothing(tmp_path, payload):
    hook, _ = _hook(_repo(tmp_path))
    fake = _FakeChecker()
    assert await _fire(hook, fake, payload) == {}
    assert fake.calls == []


@pytest.mark.parametrize("name", ["README.md", "app.ts", "data.json", "nb.ipynb"])
async def test_a_file_the_checker_does_not_accept_spawns_nothing(tmp_path, name):
    hook, _ = _hook(_repo(tmp_path))
    fake = _FakeChecker()
    assert await _fire(hook, fake, _edit(str(tmp_path / "repo" / name))) == {}
    assert fake.calls == []


async def test_an_uppercase_suffix_is_still_python(tmp_path):
    """The gate lowercases before matching; a Windows-cased path is not a
    reason to skip the check."""
    repo = _repo(tmp_path)
    (repo / "pkg" / "APP.PY").write_text("x = 1\n", encoding="utf-8")
    hook, _ = _hook(repo)
    fake = _FakeChecker(("", 0))
    await _fire(hook, fake, _edit(str(repo / "pkg" / "APP.PY")))
    assert len(fake.calls) == 1


async def test_a_pyi_stub_is_checked(tmp_path):
    repo = _repo(tmp_path)
    hook, _ = _hook(repo)
    fake = _FakeChecker(("", 0))
    await _fire(hook, fake, _edit(str(repo / "pkg" / "app.pyi")))
    assert len(fake.calls) == 1


async def test_an_edit_with_no_path_spawns_nothing(tmp_path):
    hook, _ = _hook(_repo(tmp_path))
    fake = _FakeChecker()
    assert await _fire(hook, fake, {"tool_name": "Edit", "tool_input": {}}) == {}
    assert fake.calls == []


async def test_a_path_outside_the_repo_spawns_nothing(tmp_path):
    """Declined, not resolved-anyway: phase 1's parsers return repo-relative
    paths, so a path the repo does not contain could never key a baseline."""
    hook, _ = _hook(_repo(tmp_path))
    fake = _FakeChecker()
    outside = tmp_path / "elsewhere" / "app.py"
    assert await _fire(hook, fake, _edit(str(outside))) == {}
    assert fake.calls == []


async def test_no_checker_configured_is_a_noop(tmp_path):
    hook, _ = _hook(_repo(tmp_path), checker=None)
    fake = _FakeChecker()
    assert await _fire(hook, fake, _edit(str(tmp_path / "repo" / "pkg" / "app.py"))) == {}
    assert fake.calls == []


async def test_every_edit_tool_the_lint_hook_watches_is_watched_here(tmp_path):
    """`_EDIT_TOOLS` is imported, not copied, and this is the assertion that the
    import is load-bearing: two PostToolUse hooks disagreeing about what counts
    as an edit is a defect neither of them would show."""
    repo = _repo(tmp_path)
    for tool in sorted(_EDIT_TOOLS):
        hook, _ = _hook(repo)
        fake = _FakeChecker(("", 0))
        await _fire(hook, fake, _edit(str(repo / "pkg" / "app.py"), tool=tool))
        assert len(fake.calls) == 1, f"{tool} did not reach the checker"


@pytest.mark.parametrize("tool_input", [
    {"file_path": "a.py"},
    {"path": "b.py"},
    {"notebook_path": "c.ipynb"},
    {"file_path": "a.py", "path": "ignored.py"},
    {},
])
def test_path_extraction_agrees_with_the_lint_hook(tool_input):
    """`_path_of` is mirrored rather than shared. This is what stops the mirror
    drifting: the two must agree on which key wins and on the absent case."""
    assert (
        TypeFeedbackHook._path_of(tool_input)
        == LintFeedbackHook._path_of(tool_input)
    )


# --------------------------------------------------------------------------- #
# The subtraction                                                              #
# --------------------------------------------------------------------------- #

async def test_the_first_edit_records_a_baseline_and_reports_nothing(tmp_path):
    """The named ceiling, asserted. The first run has nothing to subtract
    against, and inventing a baseline is the asymmetry phase 2 exists to avoid.
    """
    repo = _repo(tmp_path)
    hook, events = _hook(repo)
    fake = _FakeChecker((_ERR + "\n", 1))
    out = await _fire(hook, fake, _edit(str(repo / "pkg" / "app.py")))
    assert out == {}
    assert len(fake.calls) == 1
    assert [k for k, _ in events] == ["type_feedback_baseline"]
    assert hook._baseline["pkg/app.py"]


async def test_a_second_edit_that_introduces_an_error_reports_it_with_its_location(
    tmp_path,
):
    repo = _repo(tmp_path)
    hook, events = _hook(repo)
    fake = _FakeChecker(("", 0), (_ERR + "\n", 1))
    payload = _edit(str(repo / "pkg" / "app.py"))
    assert await _fire(hook, fake, payload) == {}
    out = await _fire(hook, fake, payload)

    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert out["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    assert "[TYPE]" in ctx
    assert "pkg/app.py:2:5" in ctx
    assert "return-value" in ctx
    assert "Incompatible return value type" in ctx
    assert "not its callers" in ctx, "the dependents ceiling is stated in the turn"
    assert [k for k, _ in events] == ["type_feedback_baseline", "type_feedback"]


async def test_an_error_present_before_the_edit_is_not_reported(tmp_path):
    """The whole point of subtracting: a pre-existing diagnostic is not this
    edit's work, and reporting it would train the coder to ignore the hook."""
    repo = _repo(tmp_path)
    hook, _ = _hook(repo)
    fake = _FakeChecker((_ERR + "\n", 1), (_ERR + "\n", 1))
    payload = _edit(str(repo / "pkg" / "app.py"))
    await _fire(hook, fake, payload)
    assert await _fire(hook, fake, payload) == {}


async def test_a_pre_existing_error_that_only_moved_lines_is_not_reported(tmp_path):
    """Phase 1's line-agnostic fingerprint, inherited. An edit that inserts an
    import shifts every diagnostic below it; a line-sensitive key would report
    the whole file as net-new on the next edit."""
    repo = _repo(tmp_path)
    hook, _ = _hook(repo)
    fake = _FakeChecker((_ERR + "\n", 1), (_ERR_SHIFTED + "\n", 1))
    payload = _edit(str(repo / "pkg" / "app.py"))
    await _fire(hook, fake, payload)
    assert await _fire(hook, fake, payload) == {}


async def test_only_the_added_error_is_reported_when_one_was_already_there(tmp_path):
    repo = _repo(tmp_path)
    hook, _ = _hook(repo)
    fake = _FakeChecker((_ERR + "\n", 1), (_ERR + "\n" + _OTHER + "\n", 1))
    payload = _edit(str(repo / "pkg" / "app.py"))
    await _fire(hook, fake, payload)
    ctx = (await _fire(hook, fake, payload))["hookSpecificOutput"]["additionalContext"]
    assert "reports 1 type diagnostic(s)" in ctx
    assert "name-defined" in ctx
    assert "return-value" not in ctx


async def test_fixing_the_only_error_reports_nothing(tmp_path):
    repo = _repo(tmp_path)
    hook, _ = _hook(repo)
    fake = _FakeChecker((_ERR + "\n", 1), ("", 0))
    payload = _edit(str(repo / "pkg" / "app.py"))
    await _fire(hook, fake, payload)
    assert await _fire(hook, fake, payload) == {}


async def test_a_reported_diagnostic_is_not_reported_twice(tmp_path):
    """REPORTED ONCE. The post-edit run becomes the baseline, so a diagnostic
    the coder chose not to fix is not re-sent every turn. The gate still has the
    last word on it."""
    repo = _repo(tmp_path)
    hook, _ = _hook(repo)
    fake = _FakeChecker(("", 0), (_ERR + "\n", 1), (_ERR + "\n", 1))
    payload = _edit(str(repo / "pkg" / "app.py"))
    await _fire(hook, fake, payload)
    assert await _fire(hook, fake, payload), "the introduction is reported"
    assert await _fire(hook, fake, payload) == {}, "and not again"


async def test_baselines_are_tracked_per_file(tmp_path):
    """Two files edited in one attempt do not share a baseline: the first edit
    of the second file must not be subtracted against the first file's run."""
    repo = _repo(tmp_path)
    (repo / "pkg" / "other.py").write_text("y = 2\n", encoding="utf-8")
    hook, _ = _hook(repo)
    other_err = 'pkg/other.py:1:1: error: Something else  [misc]'
    fake = _FakeChecker(("", 0), (other_err + "\n", 1))
    assert await _fire(hook, fake, _edit(str(repo / "pkg" / "app.py"))) == {}
    assert await _fire(hook, fake, _edit(str(repo / "pkg" / "other.py"))) == {}
    assert set(hook._baseline) == {"pkg/app.py", "pkg/other.py"}


async def test_a_diagnostic_in_an_imported_file_is_reported_too(tmp_path):
    """`mypy <file>` follows imports, so a run keyed on the edited file can
    report a diagnostic in another path. That is coverage, not a bug — it is
    reported with its own location."""
    repo = _repo(tmp_path)
    hook, _ = _hook(repo)
    dep = 'pkg/dep.py:7:3: error: Bad override  [override]'
    fake = _FakeChecker(("", 0), (dep + "\n", 1))
    payload = _edit(str(repo / "pkg" / "app.py"))
    await _fire(hook, fake, payload)
    ctx = (await _fire(hook, fake, payload))["hookSpecificOutput"]["additionalContext"]
    assert "pkg/dep.py:7:3" in ctx


# --------------------------------------------------------------------------- #
# Failure is silence, never a clean bill of health                             #
# --------------------------------------------------------------------------- #

async def test_a_crashed_checker_reports_nothing(tmp_path, caplog):
    repo = _repo(tmp_path)
    hook, events = _hook(repo)
    fake = _FakeChecker(("Traceback (most recent call last):\n", 2))
    with caplog.at_level("WARNING"):
        assert await _fire(hook, fake, _edit(str(repo / "pkg" / "app.py"))) == {}
    assert events == [], "no baseline event either"
    assert caplog.records, "a failure the operator can find in the log"


async def test_a_failed_run_does_not_become_the_baseline(tmp_path):
    """The distinction `_run_checker` exists to preserve. If `None` were stored
    as `[]`, the next successful run would read the file's every pre-existing
    diagnostic as net-new and dump the lot into the coder's turn."""
    repo = _repo(tmp_path)
    hook, _ = _hook(repo)
    fake = _FakeChecker(("boom", 2), (_ERR + "\n", 1))
    payload = _edit(str(repo / "pkg" / "app.py"))
    assert await _fire(hook, fake, payload) == {}
    assert hook._baseline == {}
    assert await _fire(hook, fake, payload) == {}, "this is the first real run"
    assert hook._baseline["pkg/app.py"]


async def test_a_missing_binary_reports_nothing(tmp_path):
    repo = _repo(tmp_path)
    hook, _ = _hook(repo)
    called = []
    with patch("no_human.review.type_evidence.shutil.which", return_value=None), \
         patch("no_human.review.type_evidence.subprocess.run",
               side_effect=lambda *a, **k: called.append(a)):
        assert await hook.hook(_edit(str(repo / "pkg" / "app.py")), "id", None) == {}
    assert called == [], "nothing is installed and nothing is spawned"


async def test_a_timeout_reports_nothing(tmp_path):
    repo = _repo(tmp_path)
    hook, _ = _hook(repo)

    def _timeout(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 20)

    with patch("no_human.review.type_evidence.shutil.which", return_value=_FAKE_BIN), \
         patch("no_human.review.type_evidence.subprocess.run", side_effect=_timeout):
        assert await hook.hook(_edit(str(repo / "pkg" / "app.py")), "id", None) == {}
    assert hook._baseline == {}


async def test_an_unexpected_exception_cannot_break_the_session(tmp_path, caplog):
    """A guard must not take down the turn it is guarding. `_run_checker` handles
    the failures it knows about; anything else lands here."""
    repo = _repo(tmp_path)
    hook, _ = _hook(repo)

    def _explode(argv, **kwargs):
        raise RuntimeError("something nobody predicted")

    with caplog.at_level("WARNING"), \
         patch("no_human.review.type_evidence.shutil.which", return_value=_FAKE_BIN), \
         patch("no_human.review.type_evidence.subprocess.run", side_effect=_explode):
        assert await hook.hook(_edit(str(repo / "pkg" / "app.py")), "id", None) == {}
    assert hook._baseline == {}


async def test_a_nonzero_exit_with_nothing_parsed_is_untrusted(tmp_path):
    """Inherited from `_run_checker`: a checker that says it has diagnostics and
    emits nothing we can read means we are not reading the format we think."""
    repo = _repo(tmp_path)
    hook, _ = _hook(repo)
    fake = _FakeChecker(("total gibberish\n", 1))
    assert await _fire(hook, fake, _edit(str(repo / "pkg" / "app.py"))) == {}
    assert hook._baseline == {}


# --------------------------------------------------------------------------- #
# Cost                                                                         #
# --------------------------------------------------------------------------- #

async def test_the_per_run_timeout_is_handed_to_the_subprocess(tmp_path):
    repo = _repo(tmp_path)
    hook, _ = _hook(repo)
    fake = _FakeChecker(("", 0))
    seen = {}

    def _capture(argv, **kwargs):
        seen.update(kwargs)
        return fake(argv, **kwargs)

    with patch("no_human.review.type_evidence.shutil.which", return_value=_FAKE_BIN), \
         patch("no_human.review.type_evidence.subprocess.run", side_effect=_capture):
        await hook.hook(_edit(str(repo / "pkg" / "app.py")), "id", None)
    assert seen["timeout"] == TYPE_HOOK_TIMEOUT


async def test_a_run_draws_from_what_is_left_of_the_attempt_budget(tmp_path):
    """Phase 1's rule: the last run may not overshoot the cumulative cap, so it
    gets the remainder rather than the full per-run timeout."""
    hook, _ = _hook(_repo(tmp_path), timeout=20, session_budget=25)
    hook._spent = 20.0
    assert hook._budget_left() == 5


async def test_the_attempt_budget_turns_the_hook_off_and_says_so_once(tmp_path):
    repo = _repo(tmp_path)
    hook, events = _hook(repo, session_budget=5)
    hook._spent = 5.0
    fake = _FakeChecker()
    payload = _edit(str(repo / "pkg" / "app.py"))

    assert await _fire(hook, fake, payload) == {}
    assert await _fire(hook, fake, payload) == {}
    assert fake.calls == [], "an exhausted budget spawns nothing"
    assert [k for k, _ in events] == ["type_feedback_budget_exhausted"], (
        "said once — repeating it every edit would bury it"
    )
    assert str(TYPE_HOOK_SESSION_BUDGET) not in events[0][1], (
        "the message quotes the configured budget, not the default"
    )


async def test_wall_clock_is_billed_even_when_the_run_raises(tmp_path):
    """A failure mode that costs time and no budget is one an attempt can repeat
    without limit."""
    repo = _repo(tmp_path)
    hook, _ = _hook(repo)

    def _slow_explosion(argv, **kwargs):
        import time as _t
        _t.sleep(0.2)
        raise RuntimeError("boom")

    with patch("no_human.review.type_evidence.shutil.which", return_value=_FAKE_BIN), \
         patch("no_human.review.type_evidence.subprocess.run",
               side_effect=_slow_explosion):
        await hook.hook(_edit(str(repo / "pkg" / "app.py")), "id", None)
    # `> 0` is the property; a threshold near the sleep would assert the clock
    # instead. `time.monotonic()` has a 15.625ms resolution on Windows, so a
    # slept 0.05s measures as little as 0.046s — the shape of a flake, not of a
    # regression. The sleep stays well clear of one tick so zero means the run
    # was skipped rather than merely fast.
    assert hook._spent > 0


# --------------------------------------------------------------------------- #
# What the checker is asked to do, and where it writes                         #
# --------------------------------------------------------------------------- #

async def test_mypy_is_scoped_to_the_edited_file_not_the_project(tmp_path):
    repo = _repo(tmp_path)
    hook, _ = _hook(repo, checker="mypy")
    fake = _FakeChecker(("", 0))
    await _fire(hook, fake, _edit(str(repo / "pkg" / "app.py")))
    argv, cwd = fake.calls[0]
    assert argv[-1] == "pkg/app.py", "the one file, repo-relative and POSIX"
    assert "." not in argv, "phase 1's whole-project argument is gone"
    assert "--no-error-summary" in argv and "--no-color-output" in argv, (
        "the flags `parse_mypy` reads the format of"
    )
    assert cwd == repo


async def test_mypys_cache_is_pointed_outside_the_repo(tmp_path):
    """Otherwise it is `.mypy_cache/` inside the tree the coder is working in
    and the tamper guard reads."""
    repo = _repo(tmp_path)
    hook, _ = _hook(repo, checker="mypy")
    fake = _FakeChecker(("", 0))
    await _fire(hook, fake, _edit(str(repo / "pkg" / "app.py")))
    argv, _ = fake.calls[0]
    cache = Path(argv[argv.index("--cache-dir") + 1])
    assert cache.is_absolute()
    assert not cache.is_relative_to(repo)


async def test_a_run_writes_nothing_inside_the_repo(tmp_path):
    repo = _repo(tmp_path)
    before = {p.relative_to(repo).as_posix() for p in repo.rglob("*")}
    hook, _ = _hook(repo, checker="mypy")
    fake = _FakeChecker(("", 0))
    await _fire(hook, fake, _edit(str(repo / "pkg" / "app.py")))
    after = {p.relative_to(repo).as_posix() for p in repo.rglob("*")}
    assert before == after


async def test_mypy_declines_to_run_without_a_relocated_cache(tmp_path):
    """`None` from `_mypy_cache` must mean "do not run", never "run with the
    default cache location" — which is `.mypy_cache/` inside the coder's tree."""
    repo = _repo(tmp_path)
    hook, _ = _hook(repo, checker="mypy")
    fake = _FakeChecker()
    with patch.object(TypeFeedbackHook, "_mypy_cache", return_value=None):
        assert await _fire(hook, fake, _edit(str(repo / "pkg" / "app.py"))) == {}
    assert fake.calls == []


@pytest.mark.skipif(os.name != "posix", reason="POSIX ownership/mode semantics")
def test_the_cache_root_is_created_private_on_disk(tmp_path, monkeypatch):
    """The real thing, against a real filesystem — `_cache_root` is the one
    PREDICTABLE path this module creates, so it is the one that has to be ours.

    Redirected to `tmp_path`: an earlier version of these tests created, and
    then chmodded to 0777, a directory at the real derived name in the SHARED
    temp root and never removed it (23 of them on one machine). Planting the
    exact hazard `_dir_is_private` exists to refuse — in `/tmp` on Linux CI —
    is not an acceptable way to test that it refuses it.
    """
    root = th._cache_root()
    assert root is not None
    assert stat.S_IMODE(root.stat().st_mode) == 0o700


@pytest.mark.skipif(os.name != "posix", reason="POSIX ownership/mode semantics")
def test_a_world_writable_cache_root_is_refused_on_disk(tmp_path, monkeypatch):
    """Refused, not adopted — and refusing means no run at all, because the
    fallback is `.mypy_cache/` inside the coder's tree. Inside `tmp_path`, so
    the hazard dies with the test."""
    root = th._cache_root()
    assert root is not None
    root.chmod(0o777)
    assert th._cache_root() is None, "a world-writable cache root was adopted"




def _st(mode_extra=0, uid=1000, isdir=True):
    mode = (stat.S_IFDIR if isdir else stat.S_IFREG) | 0o700 | mode_extra
    return os.stat_result((mode, 1, 1, 1, uid, 0, 0, 0, 0, 0))


@pytest.mark.parametrize("st,reason", [
    (_st(uid=4242), "owned by another account"),
    (_st(mode_extra=stat.S_IWOTH), "world-writable"),
    (_st(mode_extra=stat.S_IWGRP), "group-writable"),
    (_st(isdir=False), "not a directory"),
])
def test_a_cache_dir_that_is_not_ours_alone_is_refused(st, reason):
    """The POSIX ownership/mode rule, as a pure predicate so it is exercised on
    every platform the suite runs on rather than only on Linux.

    It matters because the directory name is DERIVED, so it is predictable, and
    on Linux the temp root is shared: another account can create
    `/tmp/nh-typehook-mypy-<digest>` first and then read or rewrite what mypy
    caches there. `exist_ok=True` alone adopts it silently.
    """
    from no_human.agent.type_hook import _dir_is_private

    assert _dir_is_private(st, our_uid=1000) is False, (
        f"a {reason} cache dir was accepted"
    )


def test_a_cache_dir_that_is_ours_alone_is_accepted():
    """The other half — the rule must not reject the normal case."""
    from no_human.agent.type_hook import _dir_is_private

    assert _dir_is_private(_st(), our_uid=1000) is True


def test_the_cache_root_is_created_private(tmp_path, monkeypatch):
    """`makedirs` must be asked for 0700. Without the mode the root lands
    0777&~umask at a predictable name in a shared temp root."""
    seen: dict = {}
    monkeypatch.setattr(th.os, "makedirs", lambda *a, **kw: seen.update(kw))
    monkeypatch.setattr(th, "_dir_is_private", lambda *a, **kw: True)
    th._cache_root()
    assert seen.get("mode") == 0o700, seen


async def test_a_refused_cache_dir_means_no_run_at_all(tmp_path):
    repo = _repo(tmp_path)
    hook, _ = _hook(repo, checker="mypy")
    fake = _FakeChecker()
    with patch.object(TypeFeedbackHook, "_mypy_cache", return_value=None):
        assert await _fire(hook, fake, _edit(str(repo / "pkg" / "app.py"))) == {}
    assert fake.calls == [], "mypy ran with its default in-repo cache location"


async def test_a_raising_event_sink_cannot_escape_into_posttooluse(tmp_path):
    """`_on_event` is injected and this module does not own it. The guard used
    to wrap only the `to_thread` call, so a sink that raised propagated into
    the SDK's hook machinery and took the turn down with it — a guard aborting
    the session it guards."""
    repo = _repo(tmp_path)

    def _explode(kind, text):
        raise RuntimeError("the event sink is broken")

    hook = TypeFeedbackHook(repo_path=repo, checker="mypy", on_event=_explode)
    fake = _FakeChecker(("", 0))
    # The baseline emit is the first thing that calls the sink.
    assert await _fire(hook, fake, _edit(str(repo / "pkg" / "app.py"))) == {}


def test_the_dev_group_carries_mypy_so_the_real_checker_tests_run_in_ci():
    """Two tests in this file drive a real mypy and skip when it is absent.
    Without mypy in the dev group they skipped in CI too, which made the PR's
    "including real mypy verification" true only on a machine that happened to
    have one."""
    import tomllib

    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    groups = tomllib.loads(pyproject.read_text(encoding="utf-8"))["dependency-groups"]
    assert any(d.startswith("mypy") for d in groups["dev"]), groups["dev"]


def test_the_cache_is_one_directory_per_attempt(tmp_path, monkeypatch):
    """Warm within the attempt, not across attempts — which is the whole of
    what is true now.

    The previous design keyed on `realpath(repo_path)` and its docstring
    promised a warm cache "for the second edit onward" plus no accumulation
    "per attempt". Both were false: `repo_path` is the attempt's WORKTREE,
    minted per run by `Orchestrator._worktree_path` as
    `<task_id>.<pid>.<random>`, so every run got a fresh digest, a cold cache,
    and a directory nothing reclaimed.
    """
    one = TypeFeedbackHook(repo_path=tmp_path / "repo", checker="mypy")
    two = TypeFeedbackHook(repo_path=tmp_path / "repo", checker="mypy")

    assert one._mypy_cache() == one._mypy_cache(), "stable within an attempt"
    assert one._mypy_cache() != two._mypy_cache(), (
        "two attempts must not share a cache — mypy takes no cross-process lock"
    )


def test_the_attempts_cache_is_removed_with_the_hook(tmp_path, monkeypatch):
    """The leak, closed. `weakref.finalize` runs when the hook is collected and
    at interpreter exit, and holds only the path string so it never keeps the
    hook alive."""
    import gc

    hook = TypeFeedbackHook(repo_path=tmp_path / "repo", checker="mypy")
    cache = hook._mypy_cache()
    assert cache is not None and cache.is_dir()

    del hook
    gc.collect()
    assert not cache.exists(), "the per-attempt cache outlived its hook"


def test_a_cache_left_by_a_dead_run_is_reclaimed(tmp_path, monkeypatch):
    """Reclaimed by owner-pid liveness, which is this repo's existing rule for
    per-run state (`core/worktree.py`'s sweep and salvage both skip a directory
    whose owner pid is alive). A directory whose name we cannot attribute is
    left alone — this only ever removes what it can positively blame on a dead
    process."""
    root = th._cache_root()
    assert root is not None

    dead = root / "999999.dead"
    live = root / f"{os.getpid()}.live"
    alien = root / "not-a-pid"
    for d in (dead, live, alien):
        d.mkdir()
        (d / "cache.data.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(
        "no_human.config.pid_alive", lambda pid: pid == os.getpid()
    )
    removed = th.reap_stale_caches(root)

    assert not dead.exists(), "a dead run's cache was not reclaimed"
    assert live.exists(), "a LIVE run's cache was deleted"
    assert alien.exists(), "an unattributable directory was deleted"
    assert removed == 1


def test_creating_a_cache_reaps_what_earlier_runs_left(tmp_path, monkeypatch):
    """The sweep has to run on the path that actually executes, not only when
    a test calls it: the 1840 directories accumulated precisely because nothing
    on the live path ever looked."""
    root = th._cache_root()
    dead = root / "999999.dead"
    dead.mkdir()

    monkeypatch.setattr("no_human.config.pid_alive", lambda pid: False)
    TypeFeedbackHook(repo_path=tmp_path / "repo", checker="mypy")._mypy_cache()
    assert not dead.exists()


async def test_pyright_gets_the_file_appended_to_its_json_argv(tmp_path):
    repo = _repo(tmp_path)
    hook, _ = _hook(repo, checker="pyright")
    fake = _FakeChecker(('{"generalDiagnostics": []}', 0))
    await _fire(hook, fake, _edit(str(repo / "pkg" / "app.py")))
    argv, _ = fake.calls[0]
    assert argv[1:] == ("--outputjson", "pkg/app.py")


async def test_a_relative_tool_path_resolves_against_the_repo(tmp_path):
    """Not against the process CWD: the orchestrator runs wherever it runs, and
    resolving "pkg/app.py" against that would put every relative tool input
    outside the repo and silently disable the hook."""
    repo = _repo(tmp_path)
    hook, _ = _hook(repo)
    fake = _FakeChecker(("", 0))
    await _fire(hook, fake, _edit("pkg/app.py"))
    assert fake.calls[0][0][-1] == "pkg/app.py"


# --------------------------------------------------------------------------- #
# Rendering                                                                    #
# --------------------------------------------------------------------------- #

def test_the_feedback_states_the_full_count_even_when_the_list_is_trimmed():
    hook = TypeFeedbackHook(repo_path=".", checker="mypy")
    # Lines from 1: a diagnostic with line 0 renders without a `:line` suffix
    # (its own test below), which would make the count below measure that
    # instead of the trim.
    many = [_diag(line=i, message=f"distinct message {i}")
            for i in range(1, MAX_FEEDBACK_DIAGNOSTICS + 6)]
    text = hook._feedback("pkg/app.py", many)
    assert f"reports {len(many)} type diagnostic(s)" in text
    assert "... and 5 more" in text
    assert text.count("pkg/app.py:") == MAX_FEEDBACK_DIAGNOSTICS


def test_the_feedback_renders_a_diagnostic_with_no_line_or_code():
    """mypy's text format can carry neither; a location of `:0:0` and a stray
    space would both be noise."""
    hook = TypeFeedbackHook(repo_path=".", checker="mypy")
    text = hook._feedback(
        "pkg/app.py", [_diag(line=0, column=0, code="", message="no location")]
    )
    assert "  pkg/app.py no location" in text
    assert ":0" not in text


def test_the_feedback_is_bounded_in_bytes(tmp_path):
    from no_human.agent.type_hook import MAX_FEEDBACK_BYTES

    hook = TypeFeedbackHook(repo_path=".", checker="mypy")
    huge = [_diag(line=i, message="m" * 400) for i in range(MAX_FEEDBACK_DIAGNOSTICS)]
    text = hook._feedback("pkg/app.py", huge)
    assert "more" in text, "the trim is declared, not silent"
    # The trailing ceiling line is appended after the budget is spent, so the
    # bound is on the quoted block rather than the whole string.
    assert len(text) < MAX_FEEDBACK_BYTES + 400


def test_one_pathological_message_still_leaves_a_location_in_the_block():
    """The reason the per-line cap exists. A fully-expanded generic type can be
    longer than the whole byte budget, and uncapped it consumed the budget on
    the first line — rendering a count with NOTHING underneath it, the least
    useful output this hook can produce."""
    from no_human.agent.type_hook import MAX_FEEDBACK_BYTES, MAX_FEEDBACK_LINE

    hook = TypeFeedbackHook(repo_path=".", checker="mypy")
    text = hook._feedback("pkg/app.py", [_diag(line=3, message="X" * 5000)])
    assert "pkg/app.py:3:5" in text, "the location survives the trim"
    assert "…" in text, "the trim is visible"
    # The cap applies to the QUOTED DIAGNOSTIC lines, which are the
    # unbounded input; the header and the trailing ceiling line are fixed
    # prose this module writes and are deliberately not trimmed.
    quoted = [ln for ln in text.splitlines() if ln.startswith('  pkg/')]
    assert quoted, 'no diagnostic line survived'
    assert max(len(ln) for ln in quoted) <= MAX_FEEDBACK_LINE
    assert len(text) < MAX_FEEDBACK_BYTES


async def test_the_baseline_map_is_bounded_and_evicts_least_recently_used(tmp_path):
    """Unbounded, this map grows with every distinct file an attempt edits and
    holds each one's full diagnostic list.

    Eviction is correct in a way truncating a retained list would not be: the
    evicted file re-baselines on its next edit and reports nothing that turn,
    where a shortened list would make the diagnostics dropped from it look
    net-new.
    """
    from no_human.agent.type_hook import BASELINE_MAX_FILES

    repo = _repo(tmp_path)
    hook, _ = _hook(repo)
    total = BASELINE_MAX_FILES + 3
    fake = _FakeChecker(*[("", 0)] * total)
    for i in range(total):
        name = f"m{i}.py"
        (repo / "pkg" / name).write_text("x = 1\n", encoding="utf-8")
        await _fire(hook, fake, _edit(str(repo / "pkg" / name)))

    assert len(hook._baseline) == BASELINE_MAX_FILES
    assert "pkg/m0.py" not in hook._baseline, "the oldest was evicted"
    assert f"pkg/m{total - 1}.py" in hook._baseline, "the newest is retained"


async def test_re_editing_a_file_keeps_it_from_being_evicted(tmp_path):
    """LRU, not FIFO: the file the coder keeps returning to is the one whose
    baseline is worth holding."""
    from no_human.agent.type_hook import BASELINE_MAX_FILES

    repo = _repo(tmp_path)
    hook, _ = _hook(repo)
    fake = _FakeChecker(*[("", 0)] * (BASELINE_MAX_FILES + 4))
    first = _edit(str(repo / "pkg" / "app.py"))
    await _fire(hook, fake, first)
    for i in range(BASELINE_MAX_FILES - 1):
        name = f"m{i}.py"
        (repo / "pkg" / name).write_text("x = 1\n", encoding="utf-8")
        await _fire(hook, fake, _edit(str(repo / "pkg" / name)))
    await _fire(hook, fake, first)  # touched again, so now most recent
    (repo / "pkg" / "late.py").write_text("x = 1\n", encoding="utf-8")
    await _fire(hook, fake, _edit(str(repo / "pkg" / "late.py")))

    assert "pkg/app.py" in hook._baseline
    assert "pkg/m0.py" not in hook._baseline, "the untouched oldest went instead"


# --------------------------------------------------------------------------- #
# Attribution — the hook must not blame the edit for what the edit didn't do   #
# --------------------------------------------------------------------------- #

def test_the_feedback_does_not_claim_the_edit_introduced_the_diagnostics():
    """The review finding this closes. Two ordinary sequences make "your edit
    to X introduced N" false — the checker follows imports, so a change to
    anything in the graph lands here, and `Bash` is not in `_EDIT_TOOLS`, so
    `sed -i`/`patch`/`ruff --fix` are invisible and surface at the next Edit.

    Neither is a false clean, but a wrong attribution carrying an imperative
    buys turns spent fixing something the coder did not cause — the same
    attempt cost this feature exists to remove. The lint hook can say
    "your edit introduced" honestly because ruff sees exactly one file.
    """
    hook = TypeFeedbackHook(repo_path=".", checker="mypy")
    text = hook._feedback("pkg/app.py", [_diag()])

    lowered = text.lower()
    assert "your edit to pkg/app.py introduced" not in lowered
    assert "introduced" not in lowered, (
        "the header claims causation it cannot establish"
    )
    # What it must say instead: scope, and the two ways it can be wrong.
    assert "on or reachable from pkg/app.py" in text
    assert "last checked" in text
    assert "import graph" in text
    assert "Bash" in text
    # Still actionable — the point is honesty, not a shrug.
    assert "Fix what belongs to your change" in text


async def test_a_diagnostic_from_an_imported_file_is_not_blamed_on_the_edit(tmp_path):
    """The reproduction, at unit speed: the checker reports a diagnostic in
    `pkg/dep.py` after an edit to `pkg/app.py`, and the block names `dep.py`'s
    location without asserting that the edit to `app.py` caused it."""
    repo = _repo(tmp_path)
    hook, _ = _hook(repo)
    dep = 'pkg/dep.py:7:3: error: Bad override  [override]'
    fake = _FakeChecker(("", 0), (dep + "\n", 1))
    payload = _edit(str(repo / "pkg" / "app.py"))
    await _fire(hook, fake, payload)
    ctx = (await _fire(hook, fake, payload))["hookSpecificOutput"]["additionalContext"]

    assert "pkg/dep.py:7:3" in ctx
    assert "introduced" not in ctx.lower()
    assert "on or reachable from pkg/app.py" in ctx


def test_the_module_states_the_real_invariant_not_the_overclaim():
    """The docstring said "the only thing that differs between them is the
    edit", which is what the feedback text inherited. What the run-identity
    actually buys is an identical ENVIRONMENT, and that is all the soundness
    argument needs; attribution is a separate and weaker claim."""
    src = Path(__file__).resolve().parents[1] / "src" / "no_human" / "agent"
    doc = (src / "type_hook.py").read_text(encoding="utf-8")
    assert "The only thing that differs between them is the edit" not in doc
    assert "What it does NOT buy is attribution" in doc


# --------------------------------------------------------------------------- #
# The checker subprocess carries no credential                                 #
# --------------------------------------------------------------------------- #

async def test_the_checker_runs_without_this_process_credentials(tmp_path, monkeypatch):
    """`mypy` IMPORTS the modules a `plugins =` line names, read from the config
    of the repo under review — so the reviewed repo's own code executes, and
    phase 2 fires that once per edit against a config the coder can write
    mid-attempt. With a bare `os.environ` it would hold our OAuth token."""
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "gh-secret")

    repo = _repo(tmp_path)
    hook, _ = _hook(repo)
    fake = _FakeChecker(("", 0))
    await _fire(hook, fake, _edit(str(repo / "pkg" / "app.py")))

    assert fake.env is not None, "the checker was spawned with the ambient env"
    for secret in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "GITHUB_TOKEN"):
        assert secret not in fake.env, f"{secret} reached the checker subprocess"
    assert "PATH" in fake.env, "operational vars are kept — the checker needs PATH"


def test_the_egress_allowlist_declares_the_mypy_plugin_execution():
    """The docstring claims BOTH checker behaviours are "declared against this
    module in tests/test_egress_allowlist.py". Before this change `grep -c
    plugins` on that file was 0: the pyright download was declared and the
    plugin execution was not, so the citation was half true."""
    allowlist = (
        Path(__file__).resolve().parents[1] / "tests" / "test_egress_allowlist.py"
    ).read_text(encoding="utf-8")
    assert "plugins" in allowlist
    assert "drop_foreign_secrets" in allowlist


def test_the_feedback_budget_counts_bytes_not_characters():
    """`MAX_FEEDBACK_BYTES` is a BYTE budget and `len()` counts characters.

    They coincide only for ASCII, and a type checker quotes identifiers and
    string literals out of the repo under review. Eight 120-character
    diagnostics measure 1528 either way, but 3208 bytes in CJK and 4048 with
    emoji against a 1500 cap. The old fixture was `"m" * 400` — pure ASCII, so
    it structurally could not see the gap.
    """
    from no_human.agent.type_hook import MAX_FEEDBACK_BYTES

    for label, ch in (("ascii", "m"), ("cjk", "漢"), ("emoji", "😀")):
        hook = TypeFeedbackHook(repo_path=".", checker="mypy")
        many = [_diag(line=i, message=ch * 120)
                for i in range(1, MAX_FEEDBACK_DIAGNOSTICS + 1)]
        text = hook._feedback("pkg/app.py", many)
        quoted = [ln for ln in text.splitlines() if ln.startswith("  pkg/")]
        size = sum(len(ln.encode("utf-8")) + 1 for ln in quoted)
        assert size <= MAX_FEEDBACK_BYTES, (
            f"{label}: quoted block is {size} bytes against a "
            f"{MAX_FEEDBACK_BYTES} cap"
        )


def test_a_multibyte_message_is_trimmed_without_splitting_a_character():
    """Truncating the encoded form can land mid-character; decoding with
    `errors="ignore"` drops that character instead of emitting a lone
    continuation byte. The block must always be valid UTF-8."""
    from no_human.agent.type_hook import MAX_FEEDBACK_LINE

    hook = TypeFeedbackHook(repo_path=".", checker="mypy")
    text = hook._feedback("pkg/app.py", [_diag(line=3, message="漢" * 400)])
    text.encode("utf-8").decode("utf-8")          # raises if we split one
    quoted = [ln for ln in text.splitlines() if ln.startswith("  pkg/")]
    assert quoted and "…" in quoted[0]
    assert len(quoted[0].encode("utf-8")) <= MAX_FEEDBACK_LINE


async def test_two_overlapping_edits_do_not_both_spend_the_budget(tmp_path):
    """The cumulative cap is only a bound if the read-run-write is serialised.

    Without the lock, two overlapping calls each read the full remaining
    budget before either credits `self._spent` in its `finally`, so both start
    and the attempt spends twice what the cap allows. Whether the SDK
    dispatches concurrently is the SDK's business; this hook is the first in
    the repo with mutable cross-call state and a cumulative budget, so it
    makes the property true itself rather than assuming it of the caller.
    """
    import asyncio as _aio

    repo = _repo(tmp_path)
    hook, events = _hook(repo, session_budget=1)

    overlapping = []
    depth = {"n": 0}

    def _slow(argv, **kwargs):
        depth["n"] += 1
        overlapping.append(depth["n"])
        import time as _t
        # Longer than the 1s attempt budget, so the FIRST run exhausts it and
        # the second must find nothing left. At 0.4s it did not, and the
        # assertion below passed for the wrong reason.
        _t.sleep(1.2)
        depth["n"] -= 1
        return subprocess.CompletedProcess(argv, 0, "", "")

    payload = _edit(str(repo / "pkg" / "app.py"))
    with patch("no_human.review.type_evidence.shutil.which", return_value=_FAKE_BIN),          patch("no_human.review.type_evidence.subprocess.run", side_effect=_slow):
        await _aio.gather(
            hook.hook(payload, "a", None),
            hook.hook(payload, "b", None),
        )

    assert max(overlapping) == 1, "two checkers ran at once"
    assert len(overlapping) == 1, (
        "the second call spent budget the first had already used up"
    )
    assert [k for k, _ in events][-1] == "type_feedback_budget_exhausted"


# --------------------------------------------------------------------------- #
# Wiring                                                                       #
# --------------------------------------------------------------------------- #

def test_the_type_hook_runs_after_lint_and_before_the_scope_guard():
    """🔴 ORDER IS LOAD-BEARING and this is the property, not a preference.
    `_compose_post_tool_hooks` short-circuits on the first hook that returns
    anything. Type feedback is once-only — a report advances the file's baseline
    — while `check_scope` warns on EVERY edit to an out-of-plan file, so behind
    the scope guard a coder working outside its plan loses type feedback for the
    whole attempt and loses it permanently. Ahead of it, the same collision only
    defers a scope warning that repeats anyway.

    Reviewed as a mutation: swapping the two in `_ordered_post_tool_hooks`
    leaves every other test in the suite green.
    """
    from no_human.core.orchestrator import Orchestrator

    receipts, lint, scope, types = object(), object(), object(), object()
    order = Orchestrator._ordered_post_tool_hooks(receipts, lint, scope, types)
    assert order == [receipts, lint, types, scope]


def test_the_receipt_observer_stays_first_with_a_type_hook_installed():
    """The pre-existing property, re-asserted through the widened signature:
    behind any firing hook the observer stops running, and receipts go missing
    on exactly the attempts with the most to report."""
    from no_human.core.orchestrator import Orchestrator

    receipts, types = object(), object()
    assert Orchestrator._ordered_post_tool_hooks(receipts, None, None, types)[0] is receipts


def test_the_type_hook_is_optional_in_the_composed_order():
    """Callers that predate phase 2 pass three hooks and must be unaffected."""
    from no_human.core.orchestrator import Orchestrator

    receipts, lint, scope = object(), object(), object()
    assert Orchestrator._ordered_post_tool_hooks(receipts, lint, scope) == [
        receipts, lint, scope
    ]
    assert Orchestrator._compose_post_tool_hooks(None, None, None) is None


async def test_a_composed_type_hook_reaches_the_backend_and_short_circuits():
    from no_human.core.orchestrator import Orchestrator

    class _Silent:
        async def hook(self, *a):
            return {}

    class _Firing:
        async def hook(self, *a):
            return {"hookSpecificOutput": {"additionalContext": "[TYPE] x"}}

    class _Never:
        def __init__(self):
            self.called = False

        async def hook(self, *a):
            self.called = True
            return {}

    scope = _Never()
    composed = Orchestrator._compose_post_tool_hooks(
        _Silent(), _Silent(), scope, _Firing())
    out = await composed.hook({}, None, None)
    assert "[TYPE]" in out["hookSpecificOutput"]["additionalContext"]
    assert scope.called is False, "the scope guard is behind a firing type hook"


def test_the_call_site_actually_gates_the_type_hook_on_the_config_key():
    """THE WIRING, not the helper. `_build_type_hook` would still pass every
    test above if `_run_implement` stopped calling it, or called it and dropped
    the result before `_compose_post_tool_hooks` — and the feature would then be
    dead code that no test in this file could see.

    Read over the AST rather than as source text, for the reason the precedent
    (`test_the_call_site_actually_gates_type_evidence_on_the_route`) records: a
    substring search is satisfied by the words appearing in a comment.
    """
    import ast

    src = Path(__file__).resolve().parents[1] / "src" / "no_human" / "core"
    tree = ast.parse((src / "orchestrator.py").read_text(encoding="utf-8"))

    builds = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == "_build_type_hook"
    ]
    assert builds, "nothing calls _build_type_hook, so the hook is never built"

    composes = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_compose_post_tool_hooks"
    ]
    assert composes, "no call to _compose_post_tool_hooks found"
    for call in composes:
        names = [
            a.id for a in call.args if isinstance(a, ast.Name)
        ] + [
            kw.value.id for kw in call.keywords if isinstance(kw.value, ast.Name)
        ]
        assert "type_hook" in names, (
            "orchestrator.py line %d composes the PostToolUse hooks without "
            "passing type_hook, so the built hook is dropped and per-edit type "
            "feedback never reaches the backend" % call.lineno
        )


async def test_the_builder_is_off_by_default_and_needs_a_configured_repo(tmp_path):
    """Two independent gates, both asserted: the config key, and whether the
    repo asked for type checking at all."""
    from no_human.core.orchestrator import Orchestrator

    repo = _repo(tmp_path)

    class _Repo:
        path = repo

    orch = object.__new__(Orchestrator)
    orch.emit = lambda *a, **k: None

    orch.config = {"hooks": {}}
    assert await Orchestrator._build_type_hook(orch, _Repo()) is None

    orch.config = {"hooks": {"per_edit_type": True}}
    assert await Orchestrator._build_type_hook(orch, _Repo()) is None, (
        "the repo configures no checker"
    )

    (repo / "mypy.ini").write_text("[mypy]\n", encoding="utf-8")
    built = await Orchestrator._build_type_hook(orch, _Repo())
    assert isinstance(built, TypeFeedbackHook)
    assert built.checker == "mypy"


# --------------------------------------------------------------------------- #
# Against a real checker — skipped when none is installed                      #
# --------------------------------------------------------------------------- #

@pytest.mark.skipif(shutil.which("mypy") is None, reason="mypy not installed")
async def test_end_to_end_against_a_real_mypy(tmp_path):
    """Earns its place for the reason phase 1 records about its real-`tsc`
    tests: mocked output proves the subtraction, but can only ever prove the
    parser agrees with a string this file wrote. Only a real run proves the
    argv is one the checker accepts — a rejected flag exits non-zero, which
    `_run_checker` correctly distrusts, and the hook would then be silently
    disabled forever rather than visibly broken.

    Three properties in one run, because a real mypy call is seconds, not
    milliseconds: a clean first edit reports nothing, a genuinely introduced
    error is reported with its real location and code, and the same error is
    not reported twice.
    """
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "mypy.ini").write_text("[mypy]\n", encoding="utf-8")
    app = repo / "pkg" / "app.py"
    app.write_text("def f() -> str:\n    return 'ok'\n", encoding="utf-8")

    hook, events = _hook(repo, checker="mypy")
    payload = _edit(str(app))

    assert await hook.hook(payload, "id", None) == {}, "clean baseline is silent"

    app.write_text("def f() -> str:\n    return 123\n", encoding="utf-8")
    out = await hook.hook(payload, "id", None)
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "pkg/app.py:2" in ctx
    assert "return-value" in ctx
    assert "Incompatible return value type" in ctx

    assert await hook.hook(payload, "id", None) == {}, "not reported twice"
    assert not (repo / ".mypy_cache").exists(), "the cache went outside the repo"
    assert [k for k, _ in events] == ["type_feedback_baseline", "type_feedback"]


@pytest.mark.skipif(shutil.which("mypy") is None, reason="mypy not installed")
async def test_a_real_pre_existing_error_that_shifts_lines_is_not_reported(tmp_path):
    """The line-agnostic fingerprint against real checker output rather than a
    string this file composed. An edit that inserts imports above a broken
    function moves every diagnostic below it; a line-sensitive key would report
    the file as newly broken on the next edit."""
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "mypy.ini").write_text("[mypy]\n", encoding="utf-8")
    app = repo / "pkg" / "app.py"
    app.write_text("def f() -> str:\n    return 123\n", encoding="utf-8")

    hook, _ = _hook(repo, checker="mypy")
    payload = _edit(str(app))
    assert await hook.hook(payload, "id", None) == {}

    app.write_text(
        "import os\n\n\ndef f() -> str:\n    return 123\n", encoding="utf-8")
    assert await hook.hook(payload, "id", None) == {}, (
        "the same diagnostic three lines lower is not net-new"
    )


def test_importing_the_hook_does_not_drag_in_the_review_package():
    """Why every `type_evidence` import in this module is function-local.
    `no_human.review.__init__` imports `reviewer`, which imports
    `agent.claude_backend` at module level — so a module-level import here would
    make loading one `agent` module pull the reviewer and its backend in behind
    it, and put an import cycle one edit away.

    Run in a subprocess because this file has already imported both, so an
    in-process check would assert nothing.
    """
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, "-c",
         "import sys, no_human.agent.type_hook;"
         "print('no_human.review' in sys.modules)"],
        capture_output=True, text=True,
        cwd=str(Path(__file__).resolve().parents[1]),
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "False", (
        "importing agent/type_hook.py pulled in no_human.review; move the "
        "type_evidence imports back inside the functions"
    )


def test_the_checker_call_actually_hops_off_the_event_loop():
    """THE WIRING, not the behaviour. `_run` spawns a BLOCKING subprocess, and
    the hook runs inside the SDK's event loop between the model's tool call and
    its next token — so awaiting it directly stalls every other coroutine on
    that loop for the whole run, up to `TYPE_HOOK_TIMEOUT`.

    This has its own test because phase 1 was failed on exactly this gap and the
    reviewer's words were that it "can come back silently". Reviewed as a
    mutation here too: replacing `await asyncio.to_thread(self._run, ...)` with
    a direct `self._run(...)` leaves every other test in this file green.

    Read over the AST rather than as source text, following the precedent in
    `tests/test_type_evidence.py`: a substring search is satisfied by the words
    appearing in a comment.
    """
    import ast

    src = Path(__file__).resolve().parents[1] / "src" / "no_human" / "agent"
    tree = ast.parse((src / "type_hook.py").read_text(encoding="utf-8"))

    hops = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "to_thread"
        and any(
            isinstance(a, ast.Attribute) and a.attr == "_run" for a in node.args
        )
    ]
    assert hops, (
        "type_hook.py never hands `_run` to asyncio.to_thread, so a blocking "
        "type-checker subprocess runs ON the event loop the coder's session is "
        "driven from"
    )

    # ...and nothing calls `_run` directly, which is the half the presence
    # check above cannot see: a second, un-hopped call site would satisfy it.
    direct = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_run"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "self"
    ]
    assert direct == [], (
        "type_hook.py calls self._run() directly at line(s) %s; every call must "
        "go through asyncio.to_thread" % [n.lineno for n in direct]
    )


def test_the_degraded_backend_message_names_the_type_check():
    """A backend with no PostToolUse hook loses this guard too, and an operator
    who is told about lint and scope but not this one is told something false by
    omission."""
    src = Path(__file__).resolve().parents[1] / "src" / "no_human" / "core"
    text = (src / "orchestrator.py").read_text(encoding="utf-8")
    assert "per-edit type check and the scope guard do not run" in text


