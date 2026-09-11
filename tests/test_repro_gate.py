"""The repro gate proves fails-before / passes-after — both directions, really run."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from no_human.profile import ProjectProfile
from no_human.testing import repro_gate
from no_human.testing.repro_gate import MANIFEST, read_manifest, run_repro_gate


@pytest.fixture
def repo(tmp_path):
    """base commit: buggy add(); attempt tree: fixed add() + a repro test."""
    def git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)
    git("init", "-b", "main")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a - b  # bug\n")
    git("add", "-A")
    git("commit", "-m", "base (buggy)")
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (tmp_path / "test_repro.py").write_text(
        "from calc import add\n\ndef test_add_fixed():\n    assert add(1, 2) == 3\n"
    )
    (tmp_path / ".no_human").mkdir()
    (tmp_path / MANIFEST).write_text(
        json.dumps({"tests": ["test_repro.py::test_add_fixed"]})
    )
    return tmp_path


def test_no_manifest_waives_loudly(tmp_path):
    def git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)
    git("init", "-b", "main")
    r = run_repro_gate(tmp_path, "HEAD")
    assert r.verdict == "waived"
    assert "no" in r.reasons[0] and MANIFEST in r.reasons[0]


def test_a_real_bugfix_passes_both_directions(repo):
    r = run_repro_gate(repo, "HEAD")
    assert r.verdict == "pass", r.reasons
    assert r.tests == ["test_repro.py::test_add_fixed"]


def test_a_test_that_passes_on_the_base_fails_the_gate(repo):
    """A 'repro' that reproduces on healthy code demonstrates nothing."""
    (repo / "test_repro.py").write_text(
        "def test_add_fixed():\n    assert True\n"
    )
    r = run_repro_gate(repo, "HEAD")
    assert r.verdict == "fail"
    assert "fails-before" in r.reasons[0]


def test_a_test_failing_on_the_attempt_tree_fails_the_gate(repo):
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b  # still buggy\n")
    r = run_repro_gate(repo, "HEAD")
    assert r.verdict == "fail"
    assert "passes-after" in r.reasons[0]


def test_a_deleted_declared_test_fails_the_gate(repo):
    """Delete-the-test defense: a listed repro test may not vanish."""
    (repo / "test_repro.py").unlink()
    r = run_repro_gate(repo, "HEAD")
    assert r.verdict == "fail"
    assert "may not be deleted" in r.reasons[0]


def test_a_bad_base_ref_is_an_error_never_a_fail(repo):
    r = run_repro_gate(repo, "no-such-ref")
    assert r.verdict == "error"


# --------------------------------------------------------------------------- #
# Frozen (PyInstaller) build: sys.executable is the nh binary, not python.     #
# The gate must resolve a real interpreter — never shell the frozen binary and #
# read its non-zero exit as a test verdict (a confident, false "fail").        #
# --------------------------------------------------------------------------- #

def test_pytest_python_uses_sys_executable_when_not_frozen():
    assert not getattr(repro_gate.sys, "frozen", False)
    assert repro_gate._pytest_python(Path(".")) == repro_gate.sys.executable


def test_pytest_python_avoids_the_frozen_binary(tmp_path, monkeypatch):
    """Frozen build: resolve the target repo's venv python, NOT sys.executable
    (which is the nh binary)."""
    monkeypatch.setattr(repro_gate.sys, "frozen", True, raising=False)
    monkeypatch.setattr(repro_gate.sys, "executable", "/frozen/nh-binary")
    venv_bin = tmp_path / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "python").write_text("")  # _venv_bin only checks existence
    got = repro_gate._pytest_python(tmp_path)
    assert got == str(venv_bin / "python")
    assert got != repro_gate.sys.executable


def test_pytest_python_is_none_when_frozen_and_nothing_resolvable(tmp_path, monkeypatch):
    monkeypatch.setattr(repro_gate.sys, "frozen", True, raising=False)
    monkeypatch.setattr(repro_gate.sys, "executable", "/frozen/nh-binary")
    monkeypatch.setattr(repro_gate.shutil, "which", lambda name: None)
    assert repro_gate._pytest_python(tmp_path) is None


def test_frozen_build_with_no_interpreter_fails_closed_to_error(repo, monkeypatch):
    """The crux of the bug: a real bugfix must NEVER be verdicted 'fail' just
    because the frozen build can't find an interpreter. It fails closed to
    'error' (advisory) and names what it could not verify."""
    monkeypatch.setattr(repro_gate.sys, "frozen", True, raising=False)
    monkeypatch.setattr(repro_gate.sys, "executable", "/frozen/nh-binary")
    monkeypatch.setattr(repro_gate.shutil, "which", lambda name: None)
    r = run_repro_gate(repo, "HEAD")
    assert r.verdict == "error", r.reasons
    assert "no Python interpreter" in r.reasons[0]
    assert r.tests == ["test_repro.py::test_add_fixed"]  # honest, not silent


def test_frozen_build_still_verdicts_correctly_with_a_real_interpreter(repo, monkeypatch):
    """With a real interpreter resolved (PATH fallback), a frozen build produces
    the SAME correct verdict a normal install would — proving the fix restored
    the gate rather than merely muting it."""
    real = repro_gate.sys.executable
    monkeypatch.setattr(repro_gate.sys, "frozen", True, raising=False)
    monkeypatch.setattr(repro_gate.sys, "executable", "/frozen/nh-binary")
    monkeypatch.setattr(
        repro_gate.shutil, "which",
        lambda name: real if name in ("python3", "python") else None)
    r = run_repro_gate(repo, "HEAD")
    assert r.verdict == "pass", r.reasons


def test_pytest_not_importable_is_an_error_never_a_fail():
    """A fallback interpreter without pytest must read as an environment error,
    not a test failure — otherwise every gate false-fails."""
    ran, ok, out = repro_gate._run_pytest(
        ["x.py::t"], Path("."), {}, "/does/not/exist/python")
    assert ran is False  # OSError → could not launch
    # And the explicit 'No module named pytest' signature is caught too.
    import types
    fake = types.SimpleNamespace(
        stdout="", stderr="/usr/bin/python: No module named pytest\n",
        returncode=1)
    def fake_run(*a, **k):
        return fake
    orig = repro_gate.subprocess.run
    try:
        repro_gate.subprocess.run = fake_run
        ran2, ok2, out2 = repro_gate._run_pytest(["x.py::t"], Path("."), {}, "python")
    finally:
        repro_gate.subprocess.run = orig
    assert ran2 is False and ok2 is False


def test_manifest_reader_tolerates_garbage(tmp_path):
    (tmp_path / ".no_human").mkdir()
    (tmp_path / MANIFEST).write_text("{not json")
    assert read_manifest(tmp_path) == []
    (tmp_path / MANIFEST).write_text(json.dumps({"tests": "not-a-list"}))
    assert read_manifest(tmp_path) == []
    (tmp_path / MANIFEST).write_text(json.dumps({"tests": [" a.py::t ", ""]}))
    assert read_manifest(tmp_path) == ["a.py::t"]


# --------------------------------------------------------------------------- #
# SCRUM-65: non-Python repos route the repro run through profile.test_cmd —   #
# pytest-only silently skipped the fails-before/passes-after guarantee for    #
# JS/TS/Go bugfixes. A tiny Python "runner" stands in for a real jest/go test #
# binary so these tests need no non-Python toolchain in CI; it only proves   #
# the WIRING (which command runs, with which args), not a real JS runner.     #
# --------------------------------------------------------------------------- #

@pytest.fixture
def js_repo(tmp_path):
    """base commit: buggy lib.js; attempt tree: fixed lib.js + a repro test.

    ``run_tests.py`` (committed, so it exists in both the attempt tree and the
    base worktree) stands in for the repo's real test_cmd: for each test-file
    argument it evaluates that file's content (a Python boolean expression,
    read relative to the test file's own directory) and exits 0 only if every
    expression is true — a minimal stand-in for a real jest/go test binary
    that keeps the pass/fail decision entirely inside the (copyable) test
    file, exactly like the pytest fixture above does with test_repro.py.
    """
    def git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)
    git("init", "-b", "main")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    (tmp_path / "run_tests.py").write_text(
        "import os, pathlib, sys\n"
        "def ok(test_file):\n"
        "    p = pathlib.Path(test_file).resolve()\n"
        "    expr = p.read_text().strip()\n"
        "    old = os.getcwd()\n"
        "    os.chdir(p.parent)\n"
        "    try:\n"
        "        return bool(eval(expr))\n"
        "    finally:\n"
        "        os.chdir(old)\n"
        "sys.exit(0 if all(ok(f) for f in sys.argv[1:]) else 1)\n"
    )
    (tmp_path / "lib.js").write_text("module.exports = 'buggy';\n")
    (tmp_path / "lib.test.js").write_text("'fixed' in open('lib.js').read()\n")
    git("add", "-A")
    git("commit", "-m", "base (buggy)")
    (tmp_path / "lib.js").write_text("module.exports = 'fixed';\n")
    (tmp_path / ".no_human").mkdir()
    (tmp_path / MANIFEST).write_text(
        json.dumps({"tests": ["lib.test.js"]})
    )
    return tmp_path


def _node_profile(repo: Path, test_cmd: str) -> ProjectProfile:
    return ProjectProfile(repo_path=str(repo), ecosystem="node", test_cmd=test_cmd)


def test_non_python_bugfix_routes_through_profile_test_cmd(js_repo):
    """Language routing: a non-Python profile drives the repro run via its
    test_cmd, and the fails-before/passes-after proof still holds end to end."""
    profile = _node_profile(js_repo, f"{sys.executable} run_tests.py")
    r = run_repro_gate(js_repo, "HEAD", profile)
    assert r.verdict == "pass", r.reasons
    assert r.tests == ["lib.test.js"]


def test_non_python_test_cmd_only_runs_the_touched_test_files(js_repo, monkeypatch):
    """Test file targeting: the invoked command receives exactly the manifest's
    test files as args — not a bare/full-suite invocation."""
    recorder = js_repo / "argv.json"
    (js_repo / "run_tests.py").write_text(
        "import json, pathlib, sys\n"
        "pathlib.Path(__file__).parent.joinpath('argv.json').write_text(json.dumps(sys.argv[1:]))\n"
        "sys.exit(1)\n"  # fail so we never reach the git-worktree step
    )
    profile = _node_profile(js_repo, f"{sys.executable} run_tests.py")
    r = run_repro_gate(js_repo, "HEAD", profile)
    assert r.verdict == "fail"
    assert json.loads(recorder.read_text(encoding="utf-8")) == ["lib.test.js"]


def test_non_python_bugfix_fails_before_check_still_holds(js_repo):
    """A 'repro' that already passes on the buggy base demonstrates nothing,
    same as the pytest path — the non-Python route must not weaken this."""
    (js_repo / "lib.test.js").write_text("True\n")  # always green, source-independent
    profile = _node_profile(js_repo, f"{sys.executable} run_tests.py")
    r = run_repro_gate(js_repo, "HEAD", profile)
    assert r.verdict == "fail"
    assert "fails-before" in r.reasons[0]


def test_missing_test_cmd_is_an_advisory_error_not_a_verdict(js_repo):
    """Advisory fallback: no test_cmd on the profile must never be read as a
    pass or a fail."""
    profile = _node_profile(js_repo, "")
    r = run_repro_gate(js_repo, "HEAD", profile)
    assert r.verdict == "error"
    assert "test_cmd" in r.reasons[0]
    assert r.tests == ["lib.test.js"]  # honest, not silent


def test_unparseable_test_cmd_is_an_advisory_error(js_repo):
    """Advisory fallback: a test_cmd shlex can't parse (mismatched quote)
    fails closed to 'error', never guesses a verdict."""
    profile = _node_profile(js_repo, "npm test 'unterminated")
    r = run_repro_gate(js_repo, "HEAD", profile)
    assert r.verdict == "error"
    assert "test_cmd" in r.reasons[0]


def test_missing_profile_defaults_to_python_pytest(repo):
    """Regression pin: no profile at all keeps the historical pytest-only
    behaviour byte-for-byte."""
    r = run_repro_gate(repo, "HEAD", None)
    assert r.verdict == "pass", r.reasons


def test_python_profile_still_uses_pytest_and_ignores_test_cmd(repo):
    """Regression pin: a profile that declares a python ecosystem stays on the
    pytest path even when test_cmd is garbage — proves the routing decision is
    keyed off the ecosystem, not "profile present or not"."""
    profile = ProjectProfile(
        repo_path=str(repo), ecosystem="python-pytest",
        test_cmd="this is not a real command !!",
    )
    r = run_repro_gate(repo, "HEAD", profile)
    assert r.verdict == "pass", r.reasons


def test_parse_test_cmd_rejects_missing_blank_and_unparseable():
    assert repro_gate._parse_test_cmd(None) is None
    assert repro_gate._parse_test_cmd("") is None
    assert repro_gate._parse_test_cmd("   ") is None
    assert repro_gate._parse_test_cmd("npm test 'unterminated") is None
    assert repro_gate._parse_test_cmd("npm test --silent") == ["npm", "test", "--silent"]


def test_nonexistent_runner_binary_is_advisory_error_not_pass(js_repo):
    """Mutation-test pin (SCRUM-65 review item 2): an OSError from a missing
    runner binary must classify as 'error' end to end, never a silent pass —
    the ``_run_test_cmd`` except-OSError branch is easy to accidentally
    mutate into a 'ran, failed' result without any test noticing."""
    profile = _node_profile(js_repo, "/no/such/binary-xyz --run")
    r = run_repro_gate(js_repo, "HEAD", profile)
    assert r.verdict == "error", r.reasons
    assert "could not run test_cmd" in r.reasons[0]


def test_runner_exiting_command_not_found_is_advisory_error(js_repo):
    """A runner that exits 126/127 (shell 'command not found' / 'not
    executable') is an environment failure, never a genuine fails-before
    verdict — SCRUM-65 review item 1."""
    (js_repo / "run_tests.py").write_text("import sys\nsys.exit(127)\n")
    profile = _node_profile(js_repo, f"{sys.executable} run_tests.py")
    r = run_repro_gate(js_repo, "HEAD", profile)
    assert r.verdict == "error", r.reasons
    assert "127" in r.reasons[0]


def test_non_python_missing_uncommitted_dep_is_advisory_error_not_pass(tmp_path):
    """Realistic fixture (SCRUM-65 review item 3): the committed test_cmd
    runner imports an UNCOMMITTED helper module — standing in for
    node_modules. It is present when the 'after' run executes in repo_path,
    but absent from the fresh git-worktree checkout used for the 'before'
    run, so that run fails for an ENVIRONMENT reason unrelated to the
    bugfix. Before the sanity-pre-run fix this produced a false 'pass'
    ('fails-before proven' from a runner that never really ran); it must
    instead be an advisory error."""
    def git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)
    git("init", "-b", "main")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    (tmp_path / "run_tests.py").write_text(
        "import helper\n"  # uncommitted dependency — stands in for node_modules
        "import sys\n"
        "sys.exit(0 if helper.check(sys.argv[1:]) else 1)\n"
    )
    (tmp_path / "lib.js").write_text("module.exports = 'buggy';\n")
    (tmp_path / "lib.test.js").write_text("'fixed' in open('lib.js').read()\n")
    git("add", "-A")
    git("commit", "-m", "base (buggy)")
    (tmp_path / "lib.js").write_text("module.exports = 'fixed';\n")
    (tmp_path / "helper.py").write_text(  # NEVER committed
        "import pathlib\n"
        "def check(files):\n"
        "    return all(eval(pathlib.Path(f).read_text().strip()) for f in files)\n"
    )
    (tmp_path / ".no_human").mkdir()
    (tmp_path / MANIFEST).write_text(json.dumps({"tests": ["lib.test.js"]}))
    profile = _node_profile(tmp_path, f"{sys.executable} run_tests.py")
    r = run_repro_gate(tmp_path, "HEAD", profile)
    assert r.verdict == "error", r.reasons
    assert "runner" in r.reasons[0].lower()


def test_pytest_style_node_id_stripped_before_reaching_foreign_runner(js_repo, monkeypatch):
    """SCRUM-65 review item 4: a manifest entry with a pytest-style node id
    (``path::case``) must reach a non-Python runner as a bare file path, not
    verbatim — a raw '::' means nothing to jest/mocha/go test."""
    (js_repo / MANIFEST).write_text(
        json.dumps({"tests": ["lib.test.js::whatever"]})
    )
    recorder = js_repo / "argv.json"
    (js_repo / "run_tests.py").write_text(
        "import json, pathlib, sys\n"
        "pathlib.Path(__file__).parent.joinpath('argv.json').write_text(json.dumps(sys.argv[1:]))\n"
        "sys.exit(1)\n"
    )
    profile = _node_profile(js_repo, f"{sys.executable} run_tests.py")
    r = run_repro_gate(js_repo, "HEAD", profile)
    assert r.verdict == "fail"
    assert json.loads(recorder.read_text(encoding="utf-8")) == ["lib.test.js"]


def test_unsubstituted_placeholder_in_test_cmd_is_advisory_error():
    """SCRUM-65 review item 4: an un-interpolated template token in test_cmd
    (e.g. '{test_file}') must fail closed at parse time rather than reach
    the runner literally."""
    assert repro_gate._parse_test_cmd("run-tests {test_file}") is None


def test_unsubstituted_placeholder_in_test_cmd_is_advisory_error_e2e(js_repo):
    profile = _node_profile(js_repo, "run-tests {test_file}")
    r = run_repro_gate(js_repo, "HEAD", profile)
    assert r.verdict == "error"
    assert "test_cmd" in r.reasons[0]


# --------------------------------------------------------------------------- #
# SCRUM-78: the bare-runner sanity pre-run classifies "runner unavailable" vs #
# "runner ran" (exit code alone is not enough — mirrors _run_test_cmd's own   #
# allowlist: only 126/127 are launch failures), gets its own short timeout    #
# independent of _RUN_TIMEOUT, and keeps the fail-closed contract: any        #
# ambiguity is still an advisory error, never a false pass/fail.              #
# --------------------------------------------------------------------------- #

def _py(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def test_sanity_exit_0_runner_ran(tmp_path):
    ok, _ = repro_gate._runner_sanity_check(
        _py("import sys; sys.exit(0)"), tmp_path, {})
    assert ok is True


def test_sanity_exit_127_refuses(tmp_path):
    ok, reason = repro_gate._runner_sanity_check(
        _py("import sys; sys.exit(127)"), tmp_path, {})
    assert ok is False
    assert "command not found" in reason


def test_sanity_exit_126_refuses(tmp_path):
    ok, reason = repro_gate._runner_sanity_check(
        _py("import sys; sys.exit(126)"), tmp_path, {})
    assert ok is False
    assert "not executable" in reason or "shell error" in reason


def test_sanity_oserror_refuses(tmp_path):
    ok, reason = repro_gate._runner_sanity_check(
        ["/no/such/binary-xyz"], tmp_path, {})
    assert ok is False
    assert "system error" in reason


@pytest.mark.parametrize("exit_code", [1, 2, 5])
def test_sanity_nonzero_exit_refuses(tmp_path, exit_code):
    """SCRUM-78 re-review: precision (letting a healthy-but-noisy runner
    through) proved unsafe — a slow environment failure is indistinguishable
    from a slow real failure by any signal we can read without provisioning
    deps. Any nonzero bare exit refuses, matching the pre-SCRUM-78 contract,
    at the SHIPPED default (no monkeypatching)."""
    ok, reason = repro_gate._runner_sanity_check(
        _py(f"import sys; sys.exit({exit_code})"), tmp_path, {})
    assert ok is False
    assert str(exit_code) in reason


def test_sanity_killed_by_signal_refuses(tmp_path):
    """A process killed by a signal (SIGKILL/OOM, SIGSEGV, ...) reports a
    negative returncode — it did not complete a run and must never read as
    'ran', regardless of how long it survived first."""
    ok, reason = repro_gate._runner_sanity_check(
        _py("import os, signal; os.kill(os.getpid(), signal.SIGKILL)"),
        tmp_path, {})
    assert ok is False
    assert "signal" in reason.lower()


def test_sanity_timeout_refuses(tmp_path, monkeypatch):
    monkeypatch.setattr(repro_gate, "_SANITY_TIMEOUT", 0.3)
    ok, reason = repro_gate._runner_sanity_check(
        _py("import time; time.sleep(5)"), tmp_path, {})
    assert ok is False
    assert "timed out" in reason


def test_sanity_timeout_bounded_well_under_run_timeout():
    """MINOR 5: pin the constant so it cannot silently drift back toward the
    old 600s cost."""
    assert repro_gate._SANITY_TIMEOUT < repro_gate._RUN_TIMEOUT
    assert repro_gate._SANITY_TIMEOUT <= 120


def test_sanity_reason_strings_are_distinct(tmp_path, monkeypatch):
    # The budget below bounds ONE probe (the sleeping one, which is meant to
    # time out) but is imposed on all six, and the other five are real CPython
    # spawns. At the 0.3s this used to carry, that left 6-10x headroom over a
    # bare interpreter start on an idle machine and none at all on a busy one:
    # a probe that overran returned the *timeout* reason instead of its own,
    # six distinct strings collapsed to five, and this assertion failed —
    # never on an idle machine, about half the time under CPU saturation,
    # which is exactly what `-n 4` on a 2-4 vCPU CI runner is. Distinctness
    # never needed a tight budget, so give the five honest probes room and
    # push the sleep far past the bound so the sixth still times out for the
    # right reason.
    #
    # Sized from measurement, not taste: under CPU saturation the slowest
    # honest probe took 0.379s — it blows a 0.3s budget outright — so 3.0s
    # leaves about 8x headroom over the worst observed spawn. At 3.0s this
    # test failed 0/35 under a load that fails it 5/10 at 0.3s.
    monkeypatch.setattr(repro_gate, "_SANITY_TIMEOUT", 3.0)
    _, r127 = repro_gate._runner_sanity_check(
        _py("import sys; sys.exit(127)"), tmp_path, {})
    _, r126 = repro_gate._runner_sanity_check(
        _py("import sys; sys.exit(126)"), tmp_path, {})
    _, r_os = repro_gate._runner_sanity_check(
        ["/no/such/binary-xyz"], tmp_path, {})
    _, r_nonzero = repro_gate._runner_sanity_check(
        _py("import sys; sys.exit(1)"), tmp_path, {})
    _, r_signal = repro_gate._runner_sanity_check(
        _py("import os, signal; os.kill(os.getpid(), signal.SIGKILL)"),
        tmp_path, {})
    _, r_timeout = repro_gate._runner_sanity_check(
        _py("import time; time.sleep(90)"), tmp_path, {})
    assert len({r127, r126, r_os, r_nonzero, r_signal, r_timeout}) == 6


def test_non_python_slow_missing_dep_is_advisory_error_not_pass(tmp_path):
    """HELD-NEGATIVE regression (review BLOCKER 1): the previous SCRUM-78
    attempt used elapsed wall-clock (<=1s ⇒ 'startup crash') to decide 'ran'.
    A dependency failure that takes ~1.3s to raise defeated that guard and
    produced a false 'pass'. This is the SAME fixture as
    test_non_python_missing_uncommitted_dep_is_advisory_error_not_pass, with
    a sleep added before the failing import so it is provably NOT fast. It
    must still be an advisory error, not a fabricated 'pass', regardless of
    how long the environment failure takes."""
    def git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)
    git("init", "-b", "main")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    (tmp_path / "run_tests.py").write_text(
        "import time\n"
        "time.sleep(1.3)\n"  # slow dependency resolution before it fails
        "import helper\n"  # uncommitted dependency — stands in for node_modules
        "import sys\n"
        "sys.exit(0 if helper.check(sys.argv[1:]) else 1)\n"
    )
    (tmp_path / "lib.js").write_text("module.exports = 'buggy';\n")
    (tmp_path / "lib.test.js").write_text("'fixed' in open('lib.js').read()\n")
    git("add", "-A")
    git("commit", "-m", "base (buggy)")
    (tmp_path / "lib.js").write_text("module.exports = 'fixed';\n")
    (tmp_path / "helper.py").write_text(  # NEVER committed
        "import pathlib\n"
        "def check(files):\n"
        "    return all(eval(pathlib.Path(f).read_text().strip()) for f in files)\n"
    )
    (tmp_path / ".no_human").mkdir()
    (tmp_path / MANIFEST).write_text(json.dumps({"tests": ["lib.test.js"]}))
    profile = _node_profile(tmp_path, f"{sys.executable} run_tests.py")
    r = run_repro_gate(tmp_path, "HEAD", profile)
    assert r.verdict != "pass", r.reasons
    assert r.verdict == "error", r.reasons


def test_non_python_healthy_nonzero_bare_runner_now_refuses_safely_e2e(tmp_path):
    """Documents the precision/safety trade-off (review BLOCKER 1): a runner
    that exits nonzero when invoked bare (no test-file args) but correctly
    evaluates tests when given them CANNOT be told apart, from the sanity
    pre-run alone, from an environment failure that also exits nonzero bare —
    so the gate now safely refuses (advisory error) rather than risk a
    fabricated fails-before verdict. A slower/more conservative gate that
    refuses honestly is preferred over one that fabricates evidence."""
    def git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    runner = (
        "import os, pathlib, sys\n"
        "if len(sys.argv) == 1:\n"
        "    sys.exit(1)\n"  # bare invocation: no test-file args ⇒ usage error
        "def ok(test_file):\n"
        "    p = pathlib.Path(test_file).resolve()\n"
        "    expr = p.read_text().strip()\n"
        "    old = os.getcwd()\n"
        "    os.chdir(p.parent)\n"
        "    try:\n"
        "        return bool(eval(expr))\n"
        "    finally:\n"
        "        os.chdir(old)\n"
        "sys.exit(0 if all(ok(f) for f in sys.argv[1:]) else 1)\n"
    )
    git("init", "-b", "main")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    (tmp_path / "run_tests.py").write_text(runner)
    (tmp_path / "lib.js").write_text("module.exports = 'buggy';\n")
    (tmp_path / "lib.test.js").write_text("'fixed' in open('lib.js').read()\n")
    git("add", "-A")
    git("commit", "-m", "base (buggy)")
    (tmp_path / "lib.js").write_text("module.exports = 'fixed';\n")
    (tmp_path / ".no_human").mkdir()
    (tmp_path / MANIFEST).write_text(json.dumps({"tests": ["lib.test.js"]}))

    profile = _node_profile(tmp_path, f"{sys.executable} run_tests.py")
    r = run_repro_gate(tmp_path, "HEAD", profile)
    assert r.verdict == "error", r.reasons


def test_is_python_profile_routing():
    assert repro_gate._is_python_profile(None) is True
    assert repro_gate._is_python_profile(
        ProjectProfile(repo_path=".", ecosystem="")) is True
    assert repro_gate._is_python_profile(
        ProjectProfile(repo_path=".", ecosystem="python-pytest")) is True
    assert repro_gate._is_python_profile(
        ProjectProfile(repo_path=".", ecosystem="node")) is False
    assert repro_gate._is_python_profile(
        ProjectProfile(repo_path=".", ecosystem="maven")) is False


# --------------------------------------------------------------------------- #
# resume-shape: a resumed attempt's `base_ref` is the task's TRUE pre-work    #
# base (the caller — Orchestrator._repro_base_ref — is responsible for that), #
# but the checkpoint it branched from already contains a prior attempt's fix. #
# The gate must still require BOTH directions (red-on-base, green-on-tip) —   #
# only the WORDING of the fail reasons and the recorded `resume_shape` flag   #
# change. Criteria 3/4 from the ticket.                                      #
# --------------------------------------------------------------------------- #


def test_resume_shape_fail_on_base_pass_on_tip_passes(repo):
    """Criterion 3: red-on-base + green-on-tip still passes when resumed."""
    r = run_repro_gate(repo, "HEAD", resume_shape=True)
    assert r.verdict == "pass", r.reasons
    assert r.resume_shape is True
    assert r.to_json()["resume_shape"] is True


def test_resume_shape_passing_on_both_fails_with_no_proof_reason(repo):
    """Criterion 4: a repro that passes on BOTH trees proves nothing — even
    (especially) for a resumed attempt, where a green base is the checkpoint's
    prior fix, not health. The reason must say so in resume terms."""
    (repo / "test_repro.py").write_text(
        "def test_add_fixed():\n    assert True\n"
    )
    r = run_repro_gate(repo, "HEAD", resume_shape=True)
    assert r.verdict == "fail"
    assert "resume-shape" in r.reasons[0]
    assert "do not demonstrate this change" in r.reasons[0]


def test_resume_shape_failing_on_tip_is_a_fail_not_a_pass(repo):
    """A tip-red repro is never accepted, resume-shape or not."""
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b  # still buggy\n")
    r = run_repro_gate(repo, "HEAD", resume_shape=True)
    assert r.verdict == "fail"
    assert r.verdict != "pass"
    assert "resume-shape" in r.reasons[0]
    assert "pass-on-tip" in r.reasons[0]


def test_resume_shape_unresolvable_base_is_error_never_fail(repo):
    """Mirrors test_a_bad_base_ref_is_an_error_never_a_fail: an unresolvable
    base is 'cannot verify', never a guessed fail/pass — resume-shape or not."""
    r = run_repro_gate(repo, "no-such-ref", resume_shape=True)
    assert r.verdict == "error"


def test_resume_shape_fails_before_message_does_not_blame_the_base(repo):
    """The resume-shape fails-before message must name the real cause (the
    declared repro tests don't discriminate) instead of implying the base
    ref itself is stale or already carries the fix — that reading sent a
    real task chasing base-resolution code that was already correct."""
    (repo / "test_repro.py").write_text(
        "def test_add_fixed():\n    assert True\n"
    )
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    r = run_repro_gate(repo, base, resume_shape=True)
    assert r.verdict == "fail"
    # Guard against a vacuous fixture: confirm the gate actually reached the
    # ok_before branch before trusting any of the NOT-in assertions below.
    assert r.reasons[0].startswith("resume-shape: fails-before failed")
    assert "do not demonstrate this change" in r.reasons[0]
    assert base in r.reasons[0]
    assert "test_repro.py::test_add_fixed" in r.reasons[0]
    for blame_phrase in ("resumed base", "stale", "already contains", "already carries"):
        assert blame_phrase not in r.reasons[0]


def test_normal_fails_before_message_names_base_ref_and_tests(repo):
    """Sibling of the resume-shape test above: the non-resume fails-before
    message states the same cause and also names the base ref and tests."""
    (repo / "test_repro.py").write_text(
        "def test_add_fixed():\n    assert True\n"
    )
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    r = run_repro_gate(repo, base)
    assert r.verdict == "fail"
    assert r.reasons[0].startswith("fails-before failed")
    assert "do not demonstrate this change" in r.reasons[0]
    assert base in r.reasons[0]
    assert "test_repro.py::test_add_fixed" in r.reasons[0]


def test_resume_shape_fails_before_verdict_and_flag_unchanged(repo):
    """This ticket changes only what the failure SAYS — the verdict and the
    resume_shape flag on the result object are pinned unchanged."""
    (repo / "test_repro.py").write_text(
        "def test_add_fixed():\n    assert True\n"
    )
    r_resume = run_repro_gate(repo, "HEAD", resume_shape=True)
    assert r_resume.verdict == "fail"
    assert r_resume.resume_shape is True
    assert r_resume.tests == ["test_repro.py::test_add_fixed"]
    assert r_resume.to_json()["resume_shape"] is True

    r_normal = run_repro_gate(repo, "HEAD")
    assert r_normal.verdict == "fail"
    assert r_normal.resume_shape is False
    assert r_normal.to_json()["resume_shape"] is False


def test_normal_gate_is_byte_identical_to_resume_off(repo):
    """Criterion 5: the non-resume path is untouched by this feature — same
    verdict, same three reason strings, verbatim (the fails-before reason now
    also carries the base ref and the declared test names), plus the
    (default-False) resume_shape key `to_json()` now always carries."""
    r = run_repro_gate(repo, "HEAD")
    assert r.to_json() == {
        "verdict": "pass",
        "tests": ["test_repro.py::test_add_fixed"],
        "reasons": [],
        "resume_shape": False,
    }

    (repo / "test_repro.py").write_text("def test_add_fixed():\n    assert True\n")
    r_fails_before = run_repro_gate(repo, "HEAD")
    assert r_fails_before.reasons[0] == (
        "fails-before failed — the declared repro tests already pass at "
        "base ref HEAD (test_repro.py::test_add_fixed), so they do not "
        "demonstrate this change"
    )

    (repo / "test_repro.py").write_text(
        "from calc import add\n\ndef test_add_fixed():\n    assert add(1, 2) == 3\n"
    )
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b  # still buggy\n")
    r_passes_after = run_repro_gate(repo, "HEAD")
    assert r_passes_after.reasons[0].startswith(
        "passes-after failed — the declared repro tests do not pass on "
        "the attempt's own tree:\n"
    )

    (repo / "test_repro.py").unlink()
    r_deleted = run_repro_gate(repo, "HEAD")
    assert r_deleted.reasons[0].startswith(
        "declared test file(s) missing from the attempt tree:"
    )
    assert r_deleted.reasons[0].endswith(
        "— a listed repro test may not be deleted"
    )


def test_base_run_that_executes_nothing_is_not_found_not_pass_at_base(repo):
    """Task 110655e5 att12: the base moved and the declared repro test no
    longer existed there. The old reading treated the base run's "nothing
    executed" as a genuine pass-at-base and reported the misleading
    `fails-before failed — ... already pass at base` verdict, killing a real
    attempt. Reproduced here without needing real git history to delete a
    file: a module-level ``pytest.skip(allow_module_level=True)`` guard on a
    module that exists only in the attempt tree — the fails-before worktree
    only ever gets a copy of the DECLARED test file (see `_test_files`), so
    a helper module never committed to base is genuinely absent there too."""
    (repo / "only_in_attempt.py").write_text("MARKER = True\n")
    (repo / "test_repro.py").write_text(
        "import importlib.util\n"
        "import pytest\n"
        "if importlib.util.find_spec('only_in_attempt') is None:\n"
        "    pytest.skip('only in attempt tree', allow_module_level=True)\n"
        "def test_add_fixed():\n"
        "    assert True\n"
    )
    r = run_repro_gate(repo, "HEAD")
    assert r.verdict == "error"
    assert r.verdict != "fail"
    assert "repro tests not found at base" in r.reasons[0]
    assert "the base moved under this ticket; re-anchor the repro" in r.reasons[0]
    assert "already pass at base" not in r.reasons[0]
    assert "already pass" not in r.reasons[0]


def test_zero_collection_at_base_names_the_reanchor_guidance(repo):
    """A second, differently-shaped zero-collection base run: the declared
    node id's function is only DEFINED when a marker module is importable —
    present in the attempt tree, absent at base — so pytest resolves the
    node id at base to nothing (`ERROR: not found: ...`, "no tests ran",
    exit 4) rather than skipping a found test (the sibling test above).
    Both must reach the same distinct verdict and reason, never the old
    generic `base-tree repro run could not execute`."""
    (repo / "only_in_attempt.py").write_text("MARKER = True\n")
    (repo / "test_repro.py").write_text(
        "import importlib.util\n"
        "if importlib.util.find_spec('only_in_attempt') is not None:\n"
        "    def test_add_fixed():\n"
        "        assert True\n"
    )
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    r = run_repro_gate(repo, base)
    assert r.verdict == "error"
    assert "re-anchor" in r.reasons[0]
    assert base in r.reasons[0]
    assert "base-tree repro run could not execute" not in r.reasons[0]


def test_exit0_skipif_at_base_is_not_found_not_pass_at_base(repo):
    """The one base-run shape review found genuinely undertested: pytest
    exits **0** having executed nothing — a function-level
    ``@pytest.mark.skipif`` (not the module-level ``pytest.skip(...,
    allow_module_level=True)`` used by the sibling tests above, which
    actually exits 4). Empirically distinct and verified here directly:
    ``python -m pytest test_repro.py::test_add_fixed`` on a skipif-guarded
    test prints "1 skipped in 0.00s" and exits 0 — the exact shape an
    ablation that special-cased "only classify when rc_before != 0" could
    hide behind, since every other integration test in this file exercises
    exit 4 or exit 5. Guarded on module importability exactly like the
    sibling tests: the marker module exists only in the attempt tree (never
    committed), so at base the guard is True (skip) and at tip it is False
    (runs and passes)."""
    (repo / "only_in_attempt.py").write_text("MARKER = True\n")
    (repo / "test_repro.py").write_text(
        "import importlib.util\n"
        "import pytest\n"
        "_available = importlib.util.find_spec('only_in_attempt') is not None\n"
        "@pytest.mark.skipif(not _available, reason='only in attempt tree')\n"
        "def test_add_fixed():\n"
        "    assert True\n"
    )
    r = run_repro_gate(repo, "HEAD")
    assert r.verdict == "error"
    assert r.verdict != "fail"
    assert "repro tests not found at base" in r.reasons[0]
    assert "the base moved under this ticket; re-anchor the repro" in r.reasons[0]
    assert "already pass at base" not in r.reasons[0]
    assert "already pass" not in r.reasons[0]


@pytest.mark.parametrize("returncode,out,expect_none", [
    (5, "no tests ran in 0.01s", False),
    (0, "collected 0 items\nno tests ran in 0.00s", False),
    (4, "ERROR: not found: test_x.py::test_y\nno tests ran in 0.00s", False),
    (4, "ERROR: found no collectors for test_x.py::test_y\n1 skipped in 0.00s", False),
    (0, "1 skipped in 0.01s", False),
    (0, "1 passed in 0.02s", True),
    (1, "1 failed in 0.02s", True),
    (4, "ERROR: found no collectors for test_e.py::test_y\n1 error in 0.02s", True),
])
def test_nothing_executed_classifier(returncode, out, expect_none):
    """Unit-level pin on `_nothing_executed`: every zero-collection shape
    found empirically (exit 5, "collected 0 items", a not-found node id, a
    module-level skip) returns a distinct non-None reason; a genuine pass,
    fail, or collection ERROR (something really ran/was reported) returns
    None so the existing pass/fail paths are left completely alone."""
    why = repro_gate._nothing_executed(returncode, out)
    assert (why is None) == expect_none


def test_reanchor_phrase_survives_the_event_truncation(repo):
    """The orchestrator's repro_gate event emits `reasons[0][:200]` — the
    re-anchor guidance must lead the reason string, not trail past the cut."""
    (repo / "only_in_attempt.py").write_text("MARKER = True\n")
    (repo / "test_repro.py").write_text(
        "import importlib.util\n"
        "import pytest\n"
        "if importlib.util.find_spec('only_in_attempt') is None:\n"
        "    pytest.skip('only in attempt tree', allow_module_level=True)\n"
        "def test_add_fixed():\n"
        "    assert True\n"
    )
    r = run_repro_gate(repo, "HEAD")
    assert r.verdict == "error"
    assert "repro tests not found at base" in r.reasons[0][:200]


def test_genuine_pass_at_base_still_reads_fails_before_failed(repo):
    """Negative control (AC2): a declared repro test that genuinely already
    passes at base — no zero-collection involved anywhere — must still
    produce the existing `fail` verdict and reason, completely untouched by
    the new classifier."""
    (repo / "test_repro.py").write_text(
        "def test_add_fixed():\n    assert True\n"
    )
    r = run_repro_gate(repo, "HEAD")
    assert r.verdict == "fail"
    assert r.reasons[0].startswith("fails-before failed")
    assert "already pass at base ref" in r.reasons[0]
    assert "not found at base" not in r.reasons[0]
    assert "re-anchor" not in r.reasons[0]


def test_genuine_pass_at_base_resume_shape_unchanged(repo):
    """Same negative control under resume_shape=True: still the existing
    `resume-shape: fails-before failed` prefix, never the new verdict."""
    (repo / "test_repro.py").write_text(
        "def test_add_fixed():\n    assert True\n"
    )
    r = run_repro_gate(repo, "HEAD", resume_shape=True)
    assert r.verdict == "fail"
    assert r.resume_shape is True
    assert r.reasons[0].startswith("resume-shape: fails-before failed")
    assert "not found at base" not in r.reasons[0]


def test_wrong_shaped_manifest_is_named_not_called_missing(tmp_path):
    """Task 89db42ea wrote a manifest keyed `repro_tests` (no `tests` list) and
    was told the file did not exist; the next attempt then redid 99 turns from
    base. A present-but-wrong manifest must be diagnosed as what it is."""
    from no_human.testing.repro_gate import manifest_problem, SCHEMA_HINT
    (tmp_path / ".no_human").mkdir()
    m = tmp_path / MANIFEST
    assert manifest_problem(tmp_path) is None          # absent → honest waive
    m.write_text('{"repro_tests": [{"test": "tests/t.py::t"}], "file": "x"}')
    problem = manifest_problem(tmp_path)
    assert problem and '"tests"' in problem and "repro_tests" in problem and SCHEMA_HINT in problem
    r = run_repro_gate(tmp_path, base_ref="HEAD")
    assert r.verdict == "waived" and r.reasons == [problem]
    m.write_text("{not json")
    assert "not valid JSON" in manifest_problem(tmp_path)
    m.write_text('{"tests": []}')
    assert "empty" in manifest_problem(tmp_path)
    m.write_text('{"tests": ["tests/t.py::t"]}')
    assert manifest_problem(tmp_path) is None


@pytest.mark.parametrize("content", [
    b"\xff\xfe not utf8", b"{not json", b"[]", b'{"repro_tests": []}',
    b'{"tests": []}', b'{"tests": [" "]}', b'{"tests": ["tests/t.py::t"]}',
    # ad32398b: per-test dicts under the correct key, and other odd shapes.
    b'{"tests": [{"id": "tests/t.py::t"}]}',
    b'{"tests": [{"id": "tests/t.py::t", "why": "explains it"}]}',
    b'{"tests": [{"test": "tests/t.py::t"}]}',
    b'{"tests": [{"id": 5}]}',
    b'{"tests": [7]}',
    b'{"tests": [null]}',
    b'{"tests": [["tests/t.py::t"]]}',
    b'{"tests": ["a.py::t", {"id": "b.py::t"}]}',
    # Whole-file scalars. Absence must be carried by something NO document can
    # decode to. These four are the wiring check — each is a value CPython
    # happens to return as the SAME object every time, so a loader using it as
    # its absence sentinel calls a present file missing, the same failure class
    # as 89db42ea. They are not the property, and enumerating values cannot be:
    # `test_absence_sentinel_is_not_a_decodable_type` below pins it over the
    # decoded TYPE instead, which is a closed set where the values are not.
    b"null", b'""', b"0", b"false",
])
def test_manifest_problem_and_read_manifest_agree(tmp_path, content):
    """Two readers of one file: `read_manifest` (the gate's input) and
    `manifest_problem` (the diagnosis). They must agree on every shape, or a
    present file can be called missing again — and neither may raise, since
    the hook now calls the second on every write (a non-UTF-8 byte included)."""
    from no_human.testing.repro_gate import manifest_problem
    (tmp_path / ".no_human").mkdir()
    (tmp_path / MANIFEST).write_bytes(content)
    tests = read_manifest(tmp_path)
    problem = manifest_problem(tmp_path)
    assert (tests == []) == (problem is not None)
    if problem is None:
        raw = content.decode(errors="replace")
        for test_id in tests:
            assert test_id in raw, (test_id, raw)


def test_absence_sentinel_is_not_a_decodable_type():
    """The property the SENTINEL rests on: absence is carried by something no
    document can decode to. (The change as a whole rests principally on
    `_entry_id`; this is the narrower guard the centralised read made
    necessary.)

    Enumerating VALUES cannot express that. CPython returns the same object for
    `None`, `True`, `False`, every int in [-5, 256], the empty string and
    single-character latin-1 strings, so a sentinel of `True`, `1`, `"a"` or `-5` is
    exactly as broken as `None` and a value list will always miss one — four
    were listed above and `True`, `1`, `"a"` and `-5` all still slipped
    through.

    The decoded TYPE set is closed, so assert over that instead: whatever
    `json.loads` can produce, `_ABSENT` must not be one of them. This admits a
    custom sentinel class and rejects every literal, without naming any.
    """
    import json

    from no_human.testing.repro_gate import _ABSENT

    decodable = {type(json.loads(s)) for s in (
        "null", "true", "false", "0", "-5", "1.5", '"a"', "[]", "{}",
    )}
    assert decodable == {type(None), bool, int, float, str, list, dict}, decodable
    assert not isinstance(_ABSENT, tuple(decodable)), (
        f"_ABSENT is a {type(_ABSENT).__name__}, which a manifest could decode to"
    )


def test_a_null_manifest_is_named_not_reported_as_absent(tmp_path):
    """A present file containing `null` must be described by its SHAPE, never
    as an absent manifest.

    `json.loads("null")` is `None`, so absence cannot be carried by `None`
    once the read is centralised: the loader returns a dedicated sentinel
    instead. Without that sentinel THIS refactor would make the file
    indistinguishable from no file at all and tell the coder there was no
    manifest while one sat in the tree — the same shape as 89db42ea, which
    `manifest_problem`'s docstring records (that one was a manifest keyed
    `repro_tests`, a wrong top-level KEY rather than a whole-file scalar).
    It is a guard against a hazard this change introduces, not a bug that
    shipped: on the parent commit each caller did its own `is_file()` check,
    so a whole-file scalar never reached an absence test, and this test passes
    there unchanged. The contrast with a truly absent file is asserted here in
    the same test so the two cannot drift.
    """
    from no_human.testing.repro_gate import manifest_problem, persist_manifest

    (tmp_path / ".no_human").mkdir()
    (tmp_path / MANIFEST).write_text("null")
    problem = manifest_problem(tmp_path)
    assert problem is not None, "a present `null` manifest must be named"
    assert "top level is NoneType" in problem, problem
    assert "no " + MANIFEST not in problem, problem
    assert read_manifest(tmp_path) == []
    assert persist_manifest(tmp_path, "task-x", home=tmp_path / "home") is False

    absent = tmp_path / "elsewhere"
    absent.mkdir()
    assert manifest_problem(absent) is None, "an absent file is still the honest waive"


def test_object_entries_with_id_are_read_as_node_ids(repo):
    """AC1: task ad32398b's real shape — dicts under the correct "tests" key,
    each carrying an "id" and a "why". Before the fix this mangled into a repr
    that `_test_files` split into a fake path, and the gate failed the real
    bugfix claiming a deleted test. After the fix it must pass exactly like
    the plain string-list manifest does."""
    (repo / MANIFEST).write_text(json.dumps({
        "tests": [{"id": "test_repro.py::test_add_fixed",
                   "why": "proves add() no longer subtracts"}],
    }))
    r = run_repro_gate(repo, "HEAD")
    assert r.verdict == "pass", r.reasons
    assert not any("missing from the attempt tree" in reason for reason in r.reasons)
    assert r.tests == ["test_repro.py::test_add_fixed"]


def test_read_manifest_extracts_id_from_object_entries(tmp_path):
    (tmp_path / ".no_human").mkdir()
    (tmp_path / MANIFEST).write_text(json.dumps({
        "tests": [{"id": "tests/t.py::t", "why": "because"}],
    }))
    assert read_manifest(tmp_path) == ["tests/t.py::t"]


@pytest.mark.parametrize("entry", [
    "tests/t.py::t",
    {"id": "tests/t.py::t"},
    {"id": "tests/t.py::t", "why": "x"},
    {"test": "tests/t.py::t"},   # no "id" key
    {"id": 5},                   # "id" not a string
    7,
    None,
    ["tests/t.py::t"],           # nested list
    True,
])
def test_every_entry_shape_yields_a_real_path_or_a_named_refusal(tmp_path, entry):
    """AC2: no entry shape may be coerced with `str()` into a path that never
    appeared in the manifest. Either the entry yields a real id straight out
    of the input, or the whole manifest is refused by a message naming the
    offending shape (never silently mangled into a bogus "missing" path —
    this is the exact assertion that fails on `{'id': 'tests/...` today)."""
    from no_human.testing.repro_gate import manifest_problem, SCHEMA_HINT
    (tmp_path / ".no_human").mkdir()
    raw = json.dumps({"tests": [entry]})
    (tmp_path / MANIFEST).write_text(raw)
    tests = read_manifest(tmp_path)
    problem = manifest_problem(tmp_path)
    for test_id in tests:
        assert test_id in raw, (test_id, raw)
    for f in repro_gate._test_files(tests):
        assert f in raw, (f, raw)
    if tests == []:
        assert problem, "an unusable manifest must be named, not silently empty"
        assert SCHEMA_HINT in problem
    else:
        assert problem is None


def test_mixed_string_and_object_entries_are_refused_by_name(tmp_path):
    """A single manifest must not mix the two entry forms —
    refuse it by name (which forms, at which indices) rather than silently
    accepting one and dropping/mangling the other."""
    from no_human.testing.repro_gate import manifest_problem
    (tmp_path / ".no_human").mkdir()
    (tmp_path / MANIFEST).write_text(json.dumps({
        "tests": ["a.py::t", {"id": "b.py::t"}],
    }))
    assert read_manifest(tmp_path) == []
    problem = manifest_problem(tmp_path)
    assert problem and "string" in problem and "object" in problem
