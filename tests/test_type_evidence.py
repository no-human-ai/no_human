"""Net-new type-checker diagnostics as review evidence (issue #114, phase 1).

No type checker is REQUIRED here: every test that must run unconditionally
mocks the checker, so the suite is green on a machine with none installed and
nothing reaches a model API.

The fixture repos are real git repos built in ``tmp_path``, because the base
half of the subtraction is a real ``git worktree add`` and mocking git away
would leave the module's central mechanism untested.

Two tests DO shell out to a real ``tsc`` and skip when it is absent
(``test_end_to_end_against_a_real_tsc`` and its pre-existing-error control).
They earn their place: mocked output proves the subtraction but can only ever
prove the parser agrees with a string this file wrote. Running the real
compiler is what caught the ``argv[0]`` defect covered by
``test_the_checker_is_invoked_by_resolved_absolute_path`` — a bare ``["tsc"]``
argv raises ``FileNotFoundError`` on Windows, which the collector read as "not
installed", silently disabling itself on the platform this product ships a
bundle for.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from no_human.core.task import Task
from no_human.review.reviewer import _build_review_prompt
from no_human.review.type_evidence import (
    MAX_TYPE_DIAGNOSTICS,
    TypeDiagnostic,
    TypeEvidence,
    _CHECKERS_BY_NAME,
    _environments_comparable,
    _fingerprint,
    _resolve_binary,
    _run_checker,
    clear_result_cache,
    collect_type_evidence,
    detect_type_checkers,
    format_type_evidence,
    net_new,
    parse_mypy,
    parse_pyright,
    parse_tsc,
)


@pytest.fixture(autouse=True)
def _no_cached_base_runs():
    """The base-run cache is module-level state keyed by (checker, sha, tree).
    Two tests that build different repos at tmp_path could otherwise collide."""
    clear_result_cache()
    yield
    clear_result_cache()


def _diag(path="a.py", line=1, column=1, code="assignment", message="bad"):
    return TypeDiagnostic(
        path=path, line=line, column=column, code=code, message=message
    )


# --------------------------------------------------------------------------- #
# Config detection — nothing runs on a repo that configures no checker         #
# --------------------------------------------------------------------------- #

def test_detect_no_config_finds_nothing(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    assert detect_type_checkers(tmp_path) == []


def test_detect_pyrightconfig_json(tmp_path):
    (tmp_path / "pyrightconfig.json").write_text("{}")
    assert [c.name for c in detect_type_checkers(tmp_path)] == ["pyright"]


def test_detect_pyright_pyproject_table(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'x'\n\n[tool.pyright]\nstrict = []\n"
    )
    assert [c.name for c in detect_type_checkers(tmp_path)] == ["pyright"]


def test_detect_mypy_pyproject_table(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'x'\n\n[tool.mypy]\nstrict = true\n"
    )
    assert [c.name for c in detect_type_checkers(tmp_path)] == ["mypy"]


def test_detect_mypy_ini(tmp_path):
    (tmp_path / "mypy.ini").write_text("[mypy]\nstrict = True\n")
    assert [c.name for c in detect_type_checkers(tmp_path)] == ["mypy"]


def test_detect_mypy_setup_cfg_section(tmp_path):
    (tmp_path / "setup.cfg").write_text("[metadata]\nname = x\n\n[mypy]\nstrict = True\n")
    assert [c.name for c in detect_type_checkers(tmp_path)] == ["mypy"]


def test_detect_pyright_wins_over_mypy_when_both_configured(tmp_path):
    """Only ONE Python checker runs. Reporting both would double-count the same
    defect in a repo that runs both over the same files."""
    (tmp_path / "pyrightconfig.json").write_text("{}")
    (tmp_path / "mypy.ini").write_text("[mypy]\n")
    assert [c.name for c in detect_type_checkers(tmp_path)] == ["pyright"]


def test_detect_tsconfig_alongside_a_python_checker(tmp_path):
    (tmp_path / "mypy.ini").write_text("[mypy]\n")
    (tmp_path / "tsconfig.json").write_text("{}")
    assert [c.name for c in detect_type_checkers(tmp_path)] == ["mypy", "tsc"]


def test_detect_survives_unreadable_pyproject(tmp_path):
    bad = tmp_path / "pyproject.toml"
    bad.mkdir()  # a directory read_text() cannot read
    assert detect_type_checkers(tmp_path) == []


def test_no_config_never_spawns_a_subprocess(tmp_path):
    """The strongest form of 'no hard dependency': on an unconfigured repo this
    module does not run anything at all, not even git."""
    with patch("no_human.review.type_evidence.subprocess.run") as run:
        evidence = collect_type_evidence(tmp_path, "HEAD~1", "HEAD")
    assert evidence.ran is False
    run.assert_not_called()


# --------------------------------------------------------------------------- #
# Parsers, against real output shapes                                          #
# --------------------------------------------------------------------------- #

def test_parse_mypy_error_line(tmp_path):
    out = 'app.py:12:5: error: Incompatible return value type (got "int", expected "str")  [return-value]\n'
    (tmp_path / "app.py").write_text("x = 1\n")
    [d] = parse_mypy(out, tmp_path)
    assert (d.path, d.line, d.column, d.code) == ("app.py", 12, 5, "return-value")
    assert d.message.startswith("Incompatible return value type")


def test_parse_mypy_ignores_note_lines(tmp_path):
    """`note:` lines are commentary attached to a preceding error, not
    diagnostics of their own — counting them would inflate every count."""
    out = (
        "app.py:12:5: error: Bad thing  [misc]\n"
        'app.py:12:5: note: Possible overload variant:\n'
    )
    assert len(parse_mypy(out, tmp_path)) == 1


def test_parse_mypy_line_without_a_code(tmp_path):
    out = "app.py:3:1: error: Cannot determine type of 'x'\n"
    [d] = parse_mypy(out, tmp_path)
    assert d.code == "" and d.line == 3


def test_parse_tsc_error_line(tmp_path):
    out = "src/a.ts(4,11): error TS2322: Type 'number' is not assignable to type 'string'.\n"
    [d] = parse_tsc(out, tmp_path)
    assert (d.path, d.line, d.column, d.code) == ("src/a.ts", 4, 11, "TS2322")


def test_parse_pyright_converts_zero_based_positions(tmp_path):
    """pyright reports 0-based line/character; everything a human reads is
    1-based, so the conversion happens once, here."""
    payload = json.dumps(
        {
            "generalDiagnostics": [
                {
                    "file": str(tmp_path / "app.py"),
                    "severity": "error",
                    "message": "Argument of type int cannot be assigned",
                    "rule": "reportArgumentType",
                    "range": {"start": {"line": 9, "character": 4}},
                },
                {
                    "file": str(tmp_path / "app.py"),
                    "severity": "warning",
                    "message": "unused",
                    "range": {"start": {"line": 1, "character": 0}},
                },
            ]
        }
    )
    diags = parse_pyright(payload, tmp_path)
    assert len(diags) == 1, "only severity=error is a diagnostic here"
    assert (diags[0].path, diags[0].line, diags[0].column) == ("app.py", 10, 5)
    assert diags[0].code == "reportArgumentType"


# --------------------------------------------------------------------------- #
# The subtraction                                                              #
# --------------------------------------------------------------------------- #

def test_fingerprint_ignores_the_line_number(tmp_path):
    """The property the whole design rests on: an inserted import above a
    pre-existing error must not make that error look new."""
    assert _fingerprint(_diag(line=10)) == _fingerprint(_diag(line=99))


def test_fingerprint_separates_different_codes_and_paths():
    assert _fingerprint(_diag(code="a")) != _fingerprint(_diag(code="b"))
    assert _fingerprint(_diag(path="a.py")) != _fingerprint(_diag(path="b.py"))


def test_net_new_reports_only_what_the_after_run_added():
    base = [_diag(message="old problem")]
    after = [_diag(line=40, message="old problem"), _diag(line=7, message="new problem")]
    [got] = net_new(base, after)
    assert got.message == "new problem"


def test_net_new_is_a_multiset_not_a_set():
    """Three occurrences of one fingerprint after, one before, is two net-new —
    a set difference would report zero and hide a real regression."""
    base = [_diag(line=1)]
    after = [_diag(line=1), _diag(line=2), _diag(line=3)]
    assert len(net_new(base, after)) == 2


def test_net_new_prefers_locations_on_lines_the_diff_touched():
    """Which occurrence of an indistinguishable fingerprint to report is a
    choice; the one on a changed line is the useful one to hand a reviewer."""
    after = [_diag(line=5), _diag(line=200)]
    [got] = net_new([_diag(line=1)], after, {"a.py": {200}})
    assert got.line == 200


def test_net_new_output_is_deterministically_ordered():
    after = [
        _diag(path="b.py", line=2),
        _diag(path="a.py", line=9),
        _diag(path="a.py", line=1),
    ]
    got = net_new([], after)
    assert [(d.path, d.line) for d in got] == [("a.py", 1), ("a.py", 9), ("b.py", 2)]


def test_net_new_normalises_digits_inside_messages():
    """A message quoting its own position re-renders after an edit shifts the
    file; keeping the digits in the key would report it as net-new."""
    base = [_diag(message="defined on line 12")]
    after = [_diag(message="defined on line 31")]
    assert net_new(base, after) == []


def test_environments_not_comparable_when_base_resolved_fewer_imports():
    base = [_diag(code="import-not-found", message='Cannot find implementation for "x"')]
    assert _environments_comparable(base, []) is False
    assert _environments_comparable([], base) is True


def test_environments_comparable_detects_unresolved_by_message_too():
    """pyright words it in prose; the code is not always the giveaway."""
    base = [_diag(code="", message='Import "flask" could not be resolved')]
    assert _environments_comparable(base, []) is False


# --------------------------------------------------------------------------- #
# End to end over a real git repo with a fake checker on PATH                  #
# --------------------------------------------------------------------------- #

# Captured before any patching: `_FakeChecker` has to let git through, and the
# name it would otherwise reach is the mock that is standing in for it.
_REAL_RUN = subprocess.run


def _git(repo: Path, *args: str) -> None:
    _REAL_RUN(["git", *args], cwd=repo, check=True, capture_output=True)


def _fixture_repo(tmp_path: Path) -> Path:
    """A real git repo configuring mypy, with one commit of clean code."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    (repo / "mypy.ini").write_text("[mypy]\nstrict = True\n")
    (repo / "app.py").write_text("def f() -> str:\n    return 'ok'\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    return repo


def _commit_change(repo: Path, text: str) -> None:
    (repo / "app.py").write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "change")


class _FakeChecker:
    """Stands in for the checker binary: returns recorded stdout per tree.

    Keyed on whether ``app.py`` in the run's cwd contains a marker, so the base
    worktree and the after tree get genuinely different answers without the test
    having to know the temporary worktree's path.
    """

    def __init__(self, *, base_out="", after_out="", marker="BROKEN",
                 base_rc=None, after_rc=None):
        self.base_out, self.after_out, self.marker = base_out, after_out, marker
        self.base_rc, self.after_rc = base_rc, after_rc
        self.calls: list[Path] = []

    def __call__(self, argv, **kwargs):
        # argv[0] is the path `shutil.which` resolved, not the bare name.
        if Path(argv[0]).name != "mypy":
            return _REAL_RUN(argv, **kwargs)  # let git through untouched
        cwd = Path(kwargs["cwd"])
        self.calls.append(cwd)
        is_after = self.marker in (cwd / "app.py").read_text(encoding="utf-8")
        out = self.after_out if is_after else self.base_out
        rc = self.after_rc if is_after else self.base_rc
        if rc is None:
            rc = 1 if out.strip() else 0
        return subprocess.CompletedProcess(argv, rc, out, "")


#: A resolved checker path outside any fixture repo. `_run_checker` resolves the
#: binary through `shutil.which` before spawning (see
#: `test_the_checker_is_invoked_by_resolved_absolute_path` for why), so a fake
#: run has to supply a resolution as well as a result.
_FAKE_BIN = "/opt/nh-test-tools/mypy"


def _collect(repo: Path, fake: _FakeChecker) -> TypeEvidence:
    with patch("no_human.review.type_evidence.shutil.which", return_value=_FAKE_BIN), \
         patch("no_human.review.type_evidence.subprocess.run", side_effect=fake):
        return collect_type_evidence(repo, "HEAD~1", "HEAD")


def test_one_introduced_error_is_reported_as_net_new_one_with_its_location(tmp_path):
    """Issue #114's first required fixture: a diff introducing exactly one type
    error must yield net-new 1, at the right place."""
    repo = _fixture_repo(tmp_path)
    _commit_change(repo, "def f() -> str:\n    return 3  # BROKEN\n")
    fake = _FakeChecker(
        base_out="",
        after_out='app.py:2:12: error: Incompatible return value type (got "int", expected "str")  [return-value]\n',
    )
    evidence = _collect(repo, fake)

    assert evidence.ran is True and evidence.checker == "mypy"
    assert len(evidence.diagnostics) == 1
    (d,) = evidence.diagnostics
    assert (d.path, d.line, d.code) == ("app.py", 2, "return-value")
    assert len(fake.calls) == 2, "one run at base, one on the attempt's tree"


def test_a_preexisting_error_is_not_attributed_to_the_diff(tmp_path):
    """Issue #114's positive control for the diffing: the SAME error present on
    base and after, re-rendered on a shifted line, is net-new 0."""
    repo = _fixture_repo(tmp_path)
    _commit_change(repo, "import os  # BROKEN\n\n\ndef f() -> str:\n    return 3\n")
    err = 'app.py:{}:12: error: Incompatible return value type (got "int", expected "str")  [return-value]\n'
    evidence = _collect(
        repo, _FakeChecker(base_out=err.format(2), after_out=err.format(5))
    )

    assert evidence.ran is True, "the check ran; it simply found nothing new"
    assert evidence.diagnostics == []
    assert evidence.after_total == 1, (
        "the pre-existing error is still counted in the after total, so "
        "'net-new: 0' cannot be misread as 'this repo type-checks clean'"
    )


def test_a_crashed_checker_attaches_no_evidence(tmp_path, caplog):
    """Issue #114's third required fixture. A checker that dies must produce no
    evidence — never an empty result that reads as a clean tree."""
    repo = _fixture_repo(tmp_path)
    _commit_change(repo, "def f() -> str:\n    return 3  # BROKEN\n")
    fake = _FakeChecker(after_out="Traceback (most recent call last):\n", after_rc=2)
    with caplog.at_level("WARNING"):
        evidence = _collect(repo, fake)

    assert evidence.ran is False
    assert format_type_evidence(evidence) == ""
    assert any("mypy exited 2" in r.getMessage() for r in caplog.records), (
        "the operator has to be able to find out WHY no evidence was attached"
    )


def test_a_nonzero_exit_with_no_parsed_diagnostic_is_untrusted(tmp_path):
    """The checker says it has errors and we parsed none: we are not reading the
    format we think we are, and a silent zero would look like a clean tree."""
    repo = _fixture_repo(tmp_path)
    _commit_change(repo, "def f() -> str:\n    return 3  # BROKEN\n")
    fake = _FakeChecker(after_out="something we cannot parse\n", after_rc=1)
    assert _collect(repo, fake).ran is False


def test_a_degraded_base_environment_produces_no_evidence(tmp_path):
    """The base worktree has no installed dependencies. A checker that cannot
    resolve imports there falls back to Any and suppresses the very errors the
    after tree reports, so the subtraction would blame the diff for them."""
    repo = _fixture_repo(tmp_path)
    _commit_change(repo, "def f() -> str:\n    return 3  # BROKEN\n")
    fake = _FakeChecker(
        base_out='app.py:1:1: error: Cannot find implementation or library stub for module named "flask"  [import-not-found]\n',
        after_out='app.py:2:12: error: Incompatible return value type  [return-value]\n',
    )
    assert _collect(repo, fake).ran is False


def test_an_unresolvable_base_ref_produces_no_evidence(tmp_path):
    repo = _fixture_repo(tmp_path)
    _commit_change(repo, "def f() -> str:\n    return 3  # BROKEN\n")
    with patch("no_human.review.type_evidence.shutil.which", return_value=_FAKE_BIN), \
         patch(
            "no_human.review.type_evidence.subprocess.run",
            side_effect=_FakeChecker(after_out="app.py:2:1: error: x  [misc]\n"),
         ):
        evidence = collect_type_evidence(repo, "no-such-ref", "HEAD")
    assert evidence.ran is False


def test_neither_run_executes_inside_the_tree_under_review(tmp_path):
    """The reviewer-integrity defect, pinned at its cause.

    `collect_type_evidence` runs inside the window the orchestrator brackets
    with `reviewer_worktree.snapshot` / `.compare`. A checker invoked with
    `cwd=repo_path` drops `.mypy_cache/` (or `*.tsbuildinfo`) into the attempt's
    own tree; `compare` then reports an added path and the orchestrator charges
    the reviewer with `reviewer_wrote`, discarding a real verdict. Asserting on
    the cwd of every checker invocation is what makes that unrepeatable.
    """
    repo = _fixture_repo(tmp_path)
    _commit_change(repo, "def f() -> str:\n    return 3  # BROKEN\n")
    fake = _FakeChecker(after_out="app.py:2:1: error: x  [misc]\n")
    _collect(repo, fake)

    assert len(fake.calls) == 2, "both sides ran"
    for cwd in fake.calls:
        assert cwd != repo
        assert not cwd.is_relative_to(repo), f"checker ran inside the review tree: {cwd}"


def test_a_checker_that_writes_leaves_the_review_tree_untouched(tmp_path):
    """The same defect observed as an artifact rather than as an argument: a
    checker that drops a cache directory where it runs must leave no trace in
    the tree the integrity guard is watching."""
    repo = _fixture_repo(tmp_path)
    _commit_change(repo, "def f() -> str:\n    return 3  # BROKEN\n")

    class _WritingChecker(_FakeChecker):
        def __call__(self, argv, **kwargs):
            if Path(argv[0]).name == "mypy":
                cache = Path(kwargs["cwd"]) / ".mypy_cache"
                cache.mkdir(exist_ok=True)
                (cache / "missing_stubs").write_text("x\n")
            return super().__call__(argv, **kwargs)

    before = sorted(p.name for p in repo.iterdir())
    _collect(repo, _WritingChecker(after_out="app.py:2:1: error: x  [misc]\n"))

    assert sorted(p.name for p in repo.iterdir()) == before
    assert not (repo / ".mypy_cache").exists(), (
        "a cache in the reviewed tree is what compare() charges to the reviewer"
    )


def test_an_untracked_file_cannot_produce_a_net_new_diagnostic(tmp_path):
    """Second defect the worktree fix closes. An untracked scratch file exists
    only on the after side, so anything it reports was unconditionally net-new
    — a diagnostic about a file that is not part of the change at all."""
    repo = _fixture_repo(tmp_path)
    _commit_change(repo, "def f() -> str:\n    return 'ok'  # BROKEN\n")
    (repo / "scratch.py").write_text("this is not even valid python\n")

    seen: list[bool] = []

    class _NotesUntracked(_FakeChecker):
        def __call__(self, argv, **kwargs):
            if Path(argv[0]).name == "mypy":
                seen.append((Path(kwargs["cwd"]) / "scratch.py").exists())
            return super().__call__(argv, **kwargs)

    _collect(repo, _NotesUntracked(after_out=""))

    assert seen and not any(seen), (
        "no run may see the untracked file; a worktree carries tracked files only"
    )


def test_a_rerun_over_unchanged_commits_costs_nothing(tmp_path):
    """The cache is keyed on resolved SHAs and covers both sides, so a second
    review of the same base and the same head spawns no checker at all. A task's
    rounds always share a merge base; they share a head whenever the round was
    re-run without new commits."""
    repo = _fixture_repo(tmp_path)
    _commit_change(repo, "def f() -> str:\n    return 3  # BROKEN\n")
    fake = _FakeChecker(after_out="app.py:2:1: error: x  [misc]\n")
    _collect(repo, fake)
    assert len(fake.calls) == 2, "first round: one run per side"
    _collect(repo, fake)
    assert len(fake.calls) == 2, "second round: both sides served from cache"


def test_a_new_head_commit_is_not_served_from_the_cache(tmp_path):
    """The other half of the contract: keyed on the SHA, so a head that moved
    is re-checked. A cache that keyed on the symbolic ref would hand round two
    the previous round's diagnostics — the worst possible failure for evidence,
    because it would look like a result."""
    repo = _fixture_repo(tmp_path)
    _commit_change(repo, "def f() -> str:\n    return 3  # BROKEN\n")
    fake = _FakeChecker(after_out="app.py:2:1: error: x  [misc]\n")
    _collect(repo, fake)
    calls_after_first = len(fake.calls)

    _commit_change(repo, "def f() -> str:\n    return 4  # BROKEN again\n")
    _collect(repo, fake)

    assert len(fake.calls) == calls_after_first + 1, (
        "the new head is re-checked; the unchanged base is still cached"
    )


def test_a_failed_run_is_not_memoised(tmp_path):
    """Only successful runs are cached. Memoising a timeout would turn one
    transient failure into permanent silence for that commit."""
    repo = _fixture_repo(tmp_path)
    _commit_change(repo, "def f() -> str:\n    return 3  # BROKEN\n")
    after_out = "app.py:2:1: error: x  [misc]\n"

    broken = _FakeChecker(after_out=after_out, base_out="boom\n", base_rc=2)
    assert _collect(repo, broken).ran is False

    working = _FakeChecker(after_out=after_out, base_out="")
    assert _collect(repo, working).ran is True, "the base is re-checked, not remembered"


def test_the_review_gate_never_raises_when_collection_explodes(tmp_path):
    repo = _fixture_repo(tmp_path)
    _commit_change(repo, "def f() -> str:\n    return 3  # BROKEN\n")
    with patch(
        "no_human.review.type_evidence.subprocess.run",
        side_effect=RuntimeError("boom"),
    ):
        assert collect_type_evidence(repo, "HEAD~1", "HEAD").ran is False


# --------------------------------------------------------------------------- #
# Cost: one budget for the whole collection                                    #
# --------------------------------------------------------------------------- #

def test_the_budget_covers_the_whole_collection_not_each_run(tmp_path):
    """`timeout` is a deadline for the collection, not a per-run cap.

    As a per-run cap the real worst case was checkers x sides x cap — two
    configured checkers at 90s ran to six minutes of gate time that nothing in
    the caller's view predicted. Each run must draw from what is LEFT.
    """
    repo = _fixture_repo(tmp_path)
    (repo / "tsconfig.json").write_text("{}")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "add a second checker")
    _commit_change(repo, "def f() -> str:\n    return 3  # BROKEN\n")

    seen: list[int] = []

    def fake_run(argv, **kwargs):
        if Path(argv[0]).name in ("mypy", "tsc"):
            seen.append(kwargs.get("timeout") or 0)
            return subprocess.CompletedProcess(argv, 0, "", "")
        return _REAL_RUN(argv, **kwargs)

    with patch("no_human.review.type_evidence.shutil.which", return_value=_FAKE_BIN), \
         patch("no_human.review.type_evidence.subprocess.run", side_effect=fake_run):
        collect_type_evidence(repo, "HEAD~1", "HEAD", timeout=30)

    assert seen, "a checker ran"
    assert all(t <= 30 for t in seen), f"a run was given more than the budget: {seen}"
    assert seen == sorted(seen, reverse=True), (
        f"each run must get what is LEFT, so the budgets never rise: {seen}"
    )


def test_a_spent_budget_stops_the_collection(tmp_path):
    """When nothing is left, the next run does not start. Starting a run that
    cannot finish spends gate time to produce nothing."""
    repo = _fixture_repo(tmp_path)
    _commit_change(repo, "def f() -> str:\n    return 3  # BROKEN\n")
    calls: list[str] = []

    def fake_run(argv, **kwargs):
        if Path(argv[0]).name == "mypy":
            calls.append("check")
            return subprocess.CompletedProcess(argv, 0, "", "")
        return _REAL_RUN(argv, **kwargs)

    with patch("no_human.review.type_evidence.shutil.which", return_value=_FAKE_BIN), \
         patch("no_human.review.type_evidence.subprocess.run", side_effect=fake_run):
        evidence = collect_type_evidence(repo, "HEAD~1", "HEAD", timeout=0)

    assert evidence.ran is False
    assert calls == [], "no checker may start with no budget left"


# --------------------------------------------------------------------------- #
# Coverage: a degraded run must not read as a clean one                        #
# --------------------------------------------------------------------------- #

def test_a_configured_checker_that_is_not_installed_costs_nothing(
        tmp_path, monkeypatch):
    """A repo can CONFIGURE a checker that is not installed on this machine,
    which for a fleet running many repos is the common case rather than the
    exception. Resolving the binary before the first checkout costs one
    `shutil.which`; discovering it inside `_run_at_commit` instead means a
    full `git worktree add` and its removal FIRST, on every review round,
    forever — seconds on a real monorepo for a run that was always going to
    report nothing. Measured before this: 6 subprocesses including two
    worktree create/remove pairs; after: 0."""
    from no_human.review import type_evidence as te

    checker = te._Checker(name="tsc", argv=("tsc", "--noEmit"), ok_codes=(0, 2))
    calls = []

    def spy(argv, **kw):
        calls.append(" ".join(str(x) for x in argv))
        raise AssertionError("no subprocess should run: %s" % calls[-1])

    monkeypatch.setattr(te, "detect_type_checkers", lambda _p: [checker])
    monkeypatch.setattr(te, "_resolve_binary", lambda _n, _p: None)
    monkeypatch.setattr(te.subprocess, "run", spy)

    ev = te.collect_type_evidence(tmp_path, "HEAD~1", "HEAD")
    assert ev.ran is False
    assert not calls, "spawned %d subprocess(es) for an absent checker" % len(calls)


def test_the_checkout_spends_the_budget_it_uses(tmp_path, monkeypatch):
    """`TYPE_TIMEOUT` documents a cap for the WHOLE collection, and
    `_run_at_commit` used to pass the same value to `git worktree add` AND
    then again to the checker, so one call could spend twice it. On a large
    repository the checkout is seconds, not milliseconds.

    This is the test that fix went in without: reverting it (handing the
    checker `timeout` instead of what is left) leaves every other test in
    this file green, so nothing would notice the cap going back to double."""
    import subprocess as _sp
    import time as _time

    from no_human.review import type_evidence as te

    real_run = _sp.run
    handed = []

    def slow_checkout(argv, **kw):
        if list(argv[:3]) == ["git", "worktree", "add"]:
            _time.sleep(2.0)
            return _sp.CompletedProcess(argv, 0, "", "")
        return real_run(argv, **kw)

    def capture(checker, cwd, *, timeout, repo_path=None, notes=None):
        handed.append(timeout)
        return []

    monkeypatch.setattr(te.subprocess, "run", slow_checkout)
    monkeypatch.setattr(te, "_run_checker", capture)
    checker = te._Checker(name="tsc", argv=("tsc", "--noEmit"), ok_codes=(0, 2))
    te._run_at_commit(checker, tmp_path, "a" * 40, timeout=6)

    assert handed, "the checker was never reached"
    assert handed[0] <= 4, (
        "the checkout spent ~2s of a 6s budget, so the checker must be handed "
        "at most the ~4s left; it got %ss, which is the whole budget again "
        "and makes TYPE_TIMEOUT a cap on nothing" % handed[0]
    )


def test_the_scope_limitation_is_stated_on_every_run(tmp_path):
    """The false clean this module exists not to emit, and the sentence that
    took three review rounds to get right.

    `include` in tsconfig.json (or pyrightconfig.json, or `files` under
    [tool.mypy]) is ordinary, and in a monorepo it is the rule, so a diff can
    land entirely outside what the checker admits. Both runs then succeed over
    a scope the diff never touched: nothing is degraded so no COVERAGE LIMIT
    for unresolved imports fires, the subtraction is arithmetically correct,
    and `after_total` says the checker did see this tree.

    The two earlier attempts tried to signal that case from the diagnostics,
    and could not: a file never analysed and a file analysed-and-clean both
    produce nothing, so the caveat rendered identically for the safe and the
    dangerous shape while reading like a measurement. Establishing that a file
    WAS analysed needs the checker's own file list, a second invocation this
    collector does not pay for. So the limitation is stated ALWAYS, in the
    same register as the "Evidence, not a verdict" line."""
    shapes = {
        "no net-new at all": TypeEvidence(
            ran=True, checker="tsc", diagnostics=[], after_total=2),
        # The shape that matters most, and the one a single-shape test misses:
        # a net-new diagnostic from an IN-SCOPE file says nothing about the
        # other changed files, which may be outside the config entirely.
        # Making the sentence conditional on `count == 0` passes every other
        # test in this file, so this is what pins "on every run".
        "net-new present": TypeEvidence(
            ran=True, checker="tsc", after_total=3,
            diagnostics=[TypeDiagnostic(
                path="packages/a/x.ts", line=2, column=7, code="TS2322",
                message="Type 'number' is not assignable to type 'string'.")]),
        "degraded imports too": TypeEvidence(
            ran=True, checker="tsc", diagnostics=[], after_total=9,
            unresolved_imports=4),
    }
    for label, ev in shapes.items():
        rendered = format_type_evidence(ev)
        assert ("SCOPE: a checker analyses only what the repo's own config "
                "admits") in rendered, "no SCOPE line for %s" % label
        assert "not as a statement that this diff was type-checked" in rendered, (
            "the SCOPE line is truncated or reworded for %s" % label
        )


def test_the_scope_line_never_appears_without_a_run(tmp_path):
    """`ran=False` renders nothing at all — the contract that makes silence
    safe. A limitation sentence attached to a run that did not happen would
    imply one did."""
    assert format_type_evidence(TypeEvidence(ran=False)) == ""

def test_symmetric_unresolved_imports_are_reported_as_a_coverage_limit(tmp_path):
    """Both trees are checked out from a commit, so neither has installed
    dependencies. `tsc`/`pyright` then analyse a real project with its imports
    degraded to Any — SYMMETRICALLY, so `_environments_comparable` passes and
    the subtraction is sound, but the coverage is not. A bare `net-new: 0` there
    would be a false clean, which is the one thing this module must never emit.
    """
    repo = _fixture_repo(tmp_path)
    _commit_change(repo, "import flask\n\n\ndef f() -> str:\n    return 'ok'\n")
    unresolved = (
        'app.py:1:1: error: Cannot find implementation or library stub for '
        'module named "flask"  [import-not-found]\n'
    )
    evidence = _collect(repo, _FakeChecker(base_out=unresolved, after_out=unresolved))

    assert evidence.ran is True
    assert evidence.diagnostics == [], "it cancels — the subtraction is sound"
    assert evidence.unresolved_imports == 1

    block = format_type_evidence(evidence)
    assert "COVERAGE LIMIT: 1" in block
    assert "analysed as Any" in block
    assert "net-new type diagnostics introduced by this diff: 0" in block


def test_a_fully_resolved_run_carries_no_coverage_caveat(tmp_path):
    """The caveat has to be absent when it does not apply, or it becomes noise
    the reviewer learns to skip past."""
    repo = _fixture_repo(tmp_path)
    _commit_change(repo, "def f() -> str:\n    return 3  # BROKEN\n")
    evidence = _collect(
        repo, _FakeChecker(after_out="app.py:2:1: error: bad thing  [misc]\n")
    )
    assert evidence.unresolved_imports == 0
    assert "COVERAGE LIMIT" not in format_type_evidence(evidence)


# --------------------------------------------------------------------------- #
# Binary resolution                                                            #
# --------------------------------------------------------------------------- #

def test_the_checker_is_invoked_by_resolved_absolute_path(tmp_path):
    """Regression, found by running a REAL tsc rather than a recorded one.

    On Windows the npm-installed checkers are `tsc.cmd`/`pyright.cmd` shims and
    CreateProcess does not apply PATHEXT, so a bare `["tsc", ...]` argv raises
    FileNotFoundError and the collector reads it as 'not installed' — silent on
    every Windows machine, and no mocked-output test can see it. argv[0] must be
    an absolute path that `shutil.which` produced.
    """
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv0"] = argv[0]
        return subprocess.CompletedProcess(argv, 0, "", "")

    with patch("no_human.review.type_evidence.shutil.which",
               return_value="/opt/tools/mypy"), \
         patch("no_human.review.type_evidence.subprocess.run", side_effect=fake_run):
        _run_checker(_CHECKERS_BY_NAME["mypy"], tmp_path, timeout=5)

    assert seen["argv0"] == "/opt/tools/mypy", "argv[0] must be the resolved path"


def test_a_checker_resolved_inside_the_repo_under_review_is_refused(tmp_path):
    """Nothing this module executes may be chosen by the repo being reviewed.
    `shutil.which` does not search the working directory on current CPython, but
    it did on Windows in older versions, so the rule is enforced, not assumed."""
    planted = tmp_path / "node_modules" / ".bin" / "tsc"
    planted.parent.mkdir(parents=True)
    planted.write_text("#!/bin/sh\necho pwned\n")
    with patch("no_human.review.type_evidence.shutil.which", return_value=str(planted)):
        assert _resolve_binary("tsc", tmp_path) is None


def test_an_absent_checker_resolves_to_none(tmp_path):
    with patch("no_human.review.type_evidence.shutil.which", return_value=None):
        assert _resolve_binary("pyright", tmp_path) is None


# --------------------------------------------------------------------------- #
# End to end against a REAL type checker                                       #
# --------------------------------------------------------------------------- #

@pytest.mark.skipif(shutil.which("tsc") is None, reason="tsc not on PATH")
def test_end_to_end_against_a_real_tsc(tmp_path):
    """No mocks anywhere: a real git repo, a real tsc, the real subtraction.

    Every other test in this file feeds the parsers output that this file wrote,
    which proves the subtraction but cannot prove the parser matches what the
    tool actually emits. This one does, and it is what caught the argv[0] defect
    above.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    (repo / "tsconfig.json").write_text('{"compilerOptions":{"strict":true,"noEmit":true}}\n')
    (repo / "a.ts").write_text("export function greet(n: string): string { return n; }\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")

    (repo / "a.ts").write_text("export function greet(n: string): string { return 42; }\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "introduce one type error")

    evidence = collect_type_evidence(repo, "HEAD~1", "HEAD")

    assert evidence.ran is True and evidence.checker == "tsc"
    assert len(evidence.diagnostics) == 1
    (d,) = evidence.diagnostics
    assert d.path == "a.ts" and d.line == 1
    assert d.code == "TS2322"
    assert "not assignable" in d.message


@pytest.mark.skipif(shutil.which("tsc") is None, reason="tsc not on PATH")
def test_end_to_end_a_real_preexisting_error_shifted_down_is_not_net_new(tmp_path):
    """The positive control for the diffing, against a real compiler: the error
    is at base too, and the diff pushes it two lines down. Net-new must be 0 —
    a line-sensitive fingerprint would report 1 here."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    (repo / "tsconfig.json").write_text('{"compilerOptions":{"strict":true,"noEmit":true}}\n')
    broken = "export function greet(n: string): string { return 42; }\n"
    (repo / "a.ts").write_text(broken)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base already has the error")

    (repo / "a.ts").write_text("// a new comment\n// and another\n" + broken)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "shift it down two lines")

    evidence = collect_type_evidence(repo, "HEAD~1", "HEAD")

    assert evidence.ran is True, "the check ran; it simply found nothing new"
    assert evidence.diagnostics == []
    assert evidence.after_total == 1
    assert "net-new type diagnostics introduced by this diff: 0" in format_type_evidence(
        evidence
    )


# --------------------------------------------------------------------------- #
# Rendering                                                                    #
# --------------------------------------------------------------------------- #

def test_format_is_empty_when_the_check_did_not_run():
    assert format_type_evidence(TypeEvidence()) == ""


def test_format_states_zero_when_the_check_ran_and_found_nothing():
    block = format_type_evidence(
        TypeEvidence(ran=True, checker="mypy", diagnostics=[], after_total=400)
    )
    assert "net-new type diagnostics introduced by this diff: 0" in block
    assert "400 diagnostic(s) in total" in block


def test_format_lists_location_code_and_message():
    block = format_type_evidence(
        TypeEvidence(
            ran=True,
            checker="pyright",
            diagnostics=[_diag(path="src/a.py", line=12, column=5, code="reportX")],
            after_total=1,
        )
    )
    assert "src/a.py:12:5 reportX bad" in block


def test_format_caps_the_block_and_says_how_many_it_dropped():
    many = [_diag(line=i, message=f"e{i}") for i in range(MAX_TYPE_DIAGNOSTICS + 7)]
    block = format_type_evidence(
        TypeEvidence(ran=True, checker="mypy", diagnostics=many, after_total=len(many))
    )
    assert "... truncated (7 more net-new diagnostics)" in block


def test_format_never_asserts_a_verdict():
    block = format_type_evidence(
        TypeEvidence(ran=True, checker="mypy", diagnostics=[_diag()], after_total=1)
    )
    assert "Evidence, not a verdict" in block


# --------------------------------------------------------------------------- #
# Wiring into the reviewer prompt                                              #
# --------------------------------------------------------------------------- #

def _task():
    task = Task.new("tighten the quote signature")
    task.acceptance_criteria = ["quote() returns str"]
    return task


def _prompt(**kwargs) -> str:
    return _build_review_prompt(_task(), "diff", "tests ok", "", **kwargs)


def test_type_evidence_renders_in_the_review_prompt():
    """Asserted against LITERAL expected text, not against whatever
    `format_type_evidence` happens to return: `assert block in prompt` with a
    computed `block` passes vacuously the moment that function returns "", which
    is precisely the wiring break this test exists to catch."""
    prompt = _prompt(
        type_evidence=format_type_evidence(
            TypeEvidence(
                ran=True,
                checker="mypy",
                diagnostics=[_diag(path="src/a.py", line=12, column=5, code="assignment")],
                after_total=3,
            )
        )
    )
    assert "TYPE EVIDENCE (mypy, deterministic)" in prompt
    assert "net-new type diagnostics introduced by this diff: 1" in prompt
    assert "src/a.py:12:5 assignment bad" in prompt
    assert "3 diagnostic(s) in total" in prompt
    assert "the net-new type diagnostics" in prompt, (
        "READING SCOPE must name evidence the prompt actually carries"
    )


def test_type_evidence_adds_its_block_and_moves_nothing_else():
    """A repo that configures no type checker must get exactly the prompt it got
    before this evidence existed — otherwise every bench run shifts, unmeasured.

    Comparing `type_evidence=""` against the default argument would be
    tautological (both feed the same value through the same branch, so an
    UNCONDITIONAL section would satisfy it too). What is observable instead:
    with evidence absent the prompt names types NOWHERE, and with it present the
    block is inserted verbatim, in the evidence run between the wiring section
    and the test output — not appended somewhere the reviewer reads as its own
    judgment.
    """
    without = _prompt(type_evidence="")
    assert "TYPE EVIDENCE" not in without
    assert "net-new type diagnostics" not in without
    assert "the diff and the test run's output" in without, (
        "READING SCOPE must not claim a type check on a repo that configures none"
    )

    block = "TYPE EVIDENCE (mypy, deterministic): net-new type diagnostics: 0."
    with_block = _prompt(
        type_evidence=block, wiring_evidence="WIRING EVIDENCE (deterministic)",
    )
    assert f"\n{block}\n\n" in with_block, "inserted verbatim, not paraphrased"
    assert (
        with_block.index("WIRING EVIDENCE (deterministic)")
        < with_block.index(block)
        < with_block.index("Test results")
    ), "the block belongs in the deterministic-evidence run, before the test output"


def test_a_crashed_checker_makes_no_cleanliness_claim_in_the_prompt():
    """The third fixture's assertion at the prompt level: with no evidence, the
    reviewer is told nothing about types at all, so it cannot read our silence
    as a pass."""
    prompt = _prompt(type_evidence=format_type_evidence(TypeEvidence()))
    assert "TYPE EVIDENCE" not in prompt
    assert "net-new type diagnostics" not in prompt


def test_the_cheap_route_does_not_pay_for_type_evidence():
    """The single-turn route exists to spend less on a small, low-risk diff.
    Paying a whole-project type check there defeats the routing decision that
    was just made, so the collector is not called at all — and lint and wiring,
    which cost seconds, still are."""
    import asyncio

    from no_human.review import reviewer as rv

    with patch.object(rv, "collect_type_evidence") as collect, \
         patch.object(rv, "collect_lint_evidence", return_value=[]) as lint, \
         patch.object(rv, "collect_wiring_evidence", return_value=[]) as wiring, \
         patch.object(rv, "_changed_paths", return_value=[]):
        lint_ev, wiring_ev, type_ev = asyncio.run(
            rv._collect_gate_evidence(
                Path("."), "HEAD~1", "HEAD", with_type_evidence=False,
            )
        )

    collect.assert_not_called()
    assert type_ev == ""
    assert lint.called and wiring.called, "the cheap collectors still run"


def test_the_call_site_actually_gates_type_evidence_on_the_route():
    """The WIRING, not the helper.

    The two route tests around this one pin `_collect_gate_evidence`'s own
    behaviour, and both would still pass if `reviewer.review` stopped passing
    `with_type_evidence=not route_single_turn` and let the default `True` stand
    — which puts a whole-project type check back on the cheap route and silently
    restores the exact cost regression this change removed. Reviewed as a
    mutation: flipping that argument to `True` leaves every other test in this
    file green.

    Read over the AST rather than as source text, for the reason the precedent
    (`test_env_setup_actually_runs_through_the_isolated_environment`) records: a
    substring search is satisfied by the words appearing in a comment.
    """
    import ast

    src = Path(__file__).resolve().parents[1] / "src" / "no_human" / "review"
    tree = ast.parse((src / "reviewer.py").read_text(encoding="utf-8"))

    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_collect_gate_evidence"
    ]
    assert calls, "no call to _collect_gate_evidence found in reviewer.py"

    for call in calls:
        gate = [kw for kw in call.keywords if kw.arg == "with_type_evidence"]
        assert gate, (
            "reviewer.py line %d collects gate evidence without passing "
            "with_type_evidence, so the default True puts a whole-project type "
            "check on the single-turn route — the cheap route exists to spend "
            "less on a small diff" % call.lineno
        )
        value = gate[0].value
        assert (
            isinstance(value, ast.UnaryOp)
            and isinstance(value.op, ast.Not)
            and isinstance(value.operand, ast.Name)
            and value.operand.id == "route_single_turn"
        ), (
            "reviewer.py line %d passes with_type_evidence=<something other "
            "than `not route_single_turn`>; the type check must be gated on the "
            "route decision, not on a constant" % call.lineno
        )

    # The OTHER half of the same cost fix, and it survived mutation until this
    # was added: removing the thread hop leaves every other test in this file
    # green while a whole-project type check runs on the event loop, blocking
    # every other task the scheduler is driving, for up to the whole budget.
    hops = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "to_thread"
        and any(
            isinstance(a, ast.Name) and a.id == "collect_type_evidence"
            for a in node.args
        )
    ]
    assert hops, (
        "reviewer.py no longer calls collect_type_evidence through "
        "asyncio.to_thread, so a whole-project type check runs ON the event "
        "loop and stalls every other task, not just this review"
    )


def test_the_normal_route_does_collect_type_evidence():
    """The other half: without the opt-out the collector is called, so the flag
    cannot silently disable the feature everywhere."""
    import asyncio

    from no_human.review import reviewer as rv

    with patch.object(rv, "collect_type_evidence") as collect, \
         patch.object(rv, "collect_lint_evidence", return_value=[]), \
         patch.object(rv, "collect_wiring_evidence", return_value=[]), \
         patch.object(rv, "_changed_paths", return_value=[]):
        collect.return_value = TypeEvidence(
            ran=True, checker="mypy", diagnostics=[], after_total=0,
        )
        _, _, type_ev = asyncio.run(
            rv._collect_gate_evidence(Path("."), "HEAD~1", "HEAD")
        )

    collect.assert_called_once()
    assert "TYPE EVIDENCE (mypy" in type_ev


def test_a_failed_collector_marker_is_never_counted_as_evidence_you_have():
    """`_evidence_failure_marker` output still renders, so the reviewer learns
    the collector broke — but READING SCOPE must not list it among the evidence
    already in front of the reviewer."""
    marker = (
        "[evidence collection FAILED: type: RuntimeError] — this collector did "
        "NOT run; its silence is not evidence of a clean result."
    )
    prompt = _prompt(type_evidence=marker)
    assert marker in prompt
    assert "the net-new type diagnostics" not in prompt


# --------------------------------------------------------------------------- #
# The checker subprocess carries no credential — and says when that costs us   #
# --------------------------------------------------------------------------- #

def test_the_checker_env_drops_every_secret_shape_not_a_sample():
    """The keep-list itself, not three hard-coded names.

    Asserting a sample is what let an earlier version of this pass while
    `drop_foreign_secrets` was called with its MODULE DEFAULT keep-list
    (`CODEX_CHILD_KEEP`), which keeps `OPENAI_` — and `llm.codex_auth_mode`
    defaults to `api_key`, so `OPENAI_API_KEY` is a live credential on a real
    share of installs. It would have reached the reviewed repo's mypy plugin
    with the whole suite green.
    """
    import os as _os
    from no_human.review.type_evidence import _checker_env

    secrets = {
        "CLAUDE_CODE_OAUTH_TOKEN": "oauth",
        "ANTHROPIC_API_KEY": "sk-ant",
        "OPENAI_API_KEY": "sk-openai",
        "GITHUB_TOKEN": "ghp",
        "AWS_SECRET_ACCESS_KEY": "aws",
        "DATABASE_URL": "postgres://u:p@h/db",
        "SSH_AUTH_SOCK": "/tmp/agent.sock",
    }
    operational = {"PATH", "HOME", "SYSTEMROOT", "TEMP", "TMP", "MYPYPATH"}
    with patch.dict(_os.environ, secrets, clear=False):
        env = _checker_env()
    leaked = sorted(name for name in secrets if name in env)
    assert leaked == [], f"secrets reached the checker: {leaked}"
    kept = sorted(n for n in operational if n in _os.environ and n in env)
    missing = sorted(n for n in operational if n in _os.environ and n not in env)
    assert missing == [], f"operational vars were dropped: {missing}"
    assert kept, "nothing operational survived — the checker needs PATH"


def test_the_scrub_uses_an_empty_keep_list():
    """Read over the AST, because the behavioural test above can only sample
    the names it thought of. `keep=()` is the property: any non-empty keep-list
    silently readmits a whole namespace."""
    import ast

    src = Path(__file__).resolve().parents[1] / "src" / "no_human" / "review"
    tree = ast.parse((src / "type_evidence.py").read_text(encoding="utf-8"))
    calls = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        and n.func.id == "drop_foreign_secrets"
    ]
    assert calls, "type_evidence.py no longer scrubs the checker environment"
    for call in calls:
        keep = [kw for kw in call.keywords if kw.arg == "keep"]
        assert keep, (
            "drop_foreign_secrets called without `keep=` at line %d, so the "
            "module default (CODEX_CHILD_KEEP) applies and OPENAI_* survives"
            % call.lineno
        )
        value = keep[0].value
        assert isinstance(value, ast.Tuple) and not value.elts, (
            "keep= must be the empty tuple at line %d; a type checker needs no "
            "credential of any namespace" % call.lineno
        )


def test_a_plugin_that_cannot_import_is_reported_not_swallowed(tmp_path):
    """The cost of the scrub, made visible on the GATE path.

    A repo whose mypy plugin reads an environment variable we removed
    (`mypy_django_plugin` imports the settings module, which reads SECRET_KEY /
    DATABASE_URL) now fails to load it. mypy exits 2, `_run_checker` correctly
    distrusts the run, and the entire TYPE EVIDENCE section would vanish with
    nothing above a `log.warning` to say why.
    """
    repo = _fixture_repo(tmp_path)
    _commit_change(repo, "def f() -> str:\n    return 'ok'  # BROKEN\n")

    class _PluginFailure:
        def __call__(self, argv, **kwargs):
            if Path(argv[0]).name != "mypy":
                return _REAL_RUN(argv, **kwargs)
            return subprocess.CompletedProcess(
                argv, 2,
                'mypy.ini:2:1: error: Error importing plugin "myplug": '
                "'DATABASE_URL'\n",
                "",
            )

    with patch("no_human.review.type_evidence.shutil.which", return_value=_FAKE_BIN), \
         patch("no_human.review.type_evidence.subprocess.run",
               side_effect=_PluginFailure()):
        evidence = collect_type_evidence(repo, "HEAD~1", "HEAD")

    assert evidence.ran is False, "a failed run must never be reported as a run"
    assert evidence.diagnostics == []
    assert "_checker_env" in evidence.unavailable_reason

    rendered = format_type_evidence(evidence)
    assert "NOT COLLECTED" in rendered
    assert "plugin failed to import" in rendered
    # It must not read as a result about the code.
    assert "net-new" not in rendered
    assert "says nothing about whether the diff is type-clean" in rendered


def test_an_ordinary_failure_still_renders_absolutely_nothing(tmp_path):
    """The contract the line above must not erode: only a cause we recognise
    earns a line. A crash, a timeout or unparseable output stays silent, so
    absence keeps meaning absence."""
    repo = _fixture_repo(tmp_path)
    _commit_change(repo, "def f() -> str:\n    return 'ok'  # BROKEN\n")
    fake = _FakeChecker(after_out="Traceback (most recent call last):\n",
                        after_rc=2)
    evidence = _collect(repo, fake)
    assert evidence.ran is False
    assert evidence.unavailable_reason == ""
    assert format_type_evidence(evidence) == ""


def test_a_not_collected_block_is_not_counted_as_evidence_the_prompt_carries():
    """The READING SCOPE sentence must never announce a check that did not run.

    This is the failure mode the collector's ceilings are written against, and
    it nearly came back through the front door: `_reading_scope` decides what
    the prompt "already carries" by testing each evidence string for
    EMPTINESS, and the NOT-COLLECTED block is a non-empty string. Without the
    prefix exclusion the reviewer is told it has the net-new type diagnostics,
    and told not to re-derive them, for a diff nothing type-checked.
    """
    from no_human.review.type_evidence import NOT_COLLECTED_PREFIX

    block = format_type_evidence(
        TypeEvidence(unavailable_reason="the repo's own plugin failed to import")
    )
    assert block.startswith(NOT_COLLECTED_PREFIX)

    prompt = _prompt(type_evidence=block)
    assert block in prompt, "the reason must still reach the reviewer"
    assert "the net-new type diagnostics" not in prompt, (
        "READING SCOPE claimed type evidence for a collector that did not run"
    )


def test_real_type_evidence_is_still_counted_as_evidence_the_prompt_carries():
    """The other half — the exclusion must not silence a real result."""
    block = format_type_evidence(
        TypeEvidence(ran=True, checker="mypy", diagnostics=[], after_total=2)
    )
    prompt = _prompt(type_evidence=block)
    assert "the net-new type diagnostics" in prompt
