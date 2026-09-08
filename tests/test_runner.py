"""Tests for local test-command detection (testing/runner.py)."""

from pathlib import Path

from no_human.testing.runner import (
    detect_command,
    _parse_vitest,
    _parse_jest,
    _parse_test_output,
    _looks_like_pytest,
    run_lint_on_changed,
    _lint_supports_file_args,
    _parse_pytest,
    _pytest_failing_tests,
    _pytest_traceback_excerpts,
    render_traceback_excerpts,
    _is_teardown_race,
    run_tests,
)


def test_uv_pyproject_uses_uv_pytest(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    (tmp_path / "uv.lock").write_text("")
    assert detect_command(tmp_path) == "uv run pytest -q"


def test_uv_project_with_xdist_parallelizes(tmp_path):
    """2026-08-10 zero-throughput incident: four workers each ran a serial
    `pytest -q` over a 7,700-test repo (~40-60 min each under load) while the
    rest of the board starved in the queue. When the repo itself declares
    pytest-xdist, `uv run` guarantees the plugin is installed, so the gate may
    parallelize. Fixed worker count — `-n auto` has wedged this repo."""
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname='x'\n"
        "[dependency-groups]\ndev = ['pytest-xdist>=3.5']\n")
    (tmp_path / "uv.lock").write_text("")
    assert detect_command(tmp_path) == "uv run pytest -q -n 4"


def test_uv_project_with_xdist_only_in_lock_parallelizes(tmp_path):
    """A transitive or group-declared xdist shows up in uv.lock even when
    pyproject names it indirectly; the lock is what `uv run` installs from."""
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    (tmp_path / "uv.lock").write_text('name = "pytest-xdist"\n')
    assert detect_command(tmp_path) == "uv run pytest -q -n 4"


def test_bare_pytest_never_gets_dash_n(tmp_path):
    """Without uv nothing guarantees xdist is installed in whatever
    environment runs the command; `-n` with the plugin missing is exit code 4
    (an invocation error), not a test verdict. The bare path stays serial."""
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    (tmp_path / "requirements-dev.txt").write_text("pytest-xdist\n")
    assert detect_command(tmp_path) == "pytest -q"


def test_plain_pytest_project(tmp_path):
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    assert detect_command(tmp_path) == "pytest -q"


def test_polyglot_pytest_over_maven(tmp_path):
    # The polyglot shape that motivated this: a root pom.xml (Java build) but the
    # real test suite is pytest under src/tests with a root pytest.ini. Must NOT
    # be misrouted to `mvn` (the shadow-validation finding).
    (tmp_path / "pom.xml").write_text("<project/>")
    (tmp_path / "pytest.ini").write_text("[pytest]\ntestpaths = src/tests\n")
    (tmp_path / "src" / "tests").mkdir(parents=True)
    assert detect_command(tmp_path) == "pytest -q"


def test_requirements_with_pytest(tmp_path):
    (tmp_path / "pom.xml").write_text("<project/>")
    (tmp_path / "requirements-dev.txt").write_text("pytest>=8\nrich\n")
    assert detect_command(tmp_path) == "pytest -q"


def test_pure_maven_still_maven(tmp_path):
    (tmp_path / "pom.xml").write_text("<project/>")
    assert detect_command(tmp_path) == "mvn -q test"


def test_node_project(tmp_path):
    (tmp_path / "package.json").write_text("{}")
    assert detect_command(tmp_path) == "npm test --silent"


def test_unknown_returns_none(tmp_path):
    assert detect_command(tmp_path) is None


# --------------------------------------------------------------------------- #
# Regression: Bug 1 — Node repo with tests/ dir must NOT detect as pytest     #
# --------------------------------------------------------------------------- #


def test_node_with_tests_dir_not_pytest(tmp_path):
    """Node-repo shape: package.json + tests/fixtures/ (no .py files).
    Must detect as npm, NOT pytest."""
    (tmp_path / "package.json").write_text('{"scripts":{"test":"vitest run"}}')
    fixtures = tmp_path / "tests" / "fixtures"
    fixtures.mkdir(parents=True)
    (fixtures / "data.json").write_text("{}")
    assert not _looks_like_pytest(tmp_path)
    assert detect_command(tmp_path) == "npm test --silent"


def test_node_with_pyproject_not_pytest(tmp_path):
    """Node repo that also has pyproject.toml (e.g. for a Python linter tool).
    package.json should win."""
    (tmp_path / "package.json").write_text("{}")
    (tmp_path / "pyproject.toml").write_text("[tool.black]\nline-length=88\n")
    assert not _looks_like_pytest(tmp_path)
    assert detect_command(tmp_path) == "npm test --silent"


def test_tests_dir_with_python_files_is_pytest(tmp_path):
    """A tests/ dir WITH .py files should still detect as pytest."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_foo.py").write_text("def test_x(): pass")
    assert _looks_like_pytest(tmp_path)
    assert detect_command(tmp_path) == "pytest -q"


# --------------------------------------------------------------------------- #
# Bug 3: vitest / jest parsers                                                #
# --------------------------------------------------------------------------- #


def test_parse_vitest_all_pass():
    output = """\
 ✓ src/copilot.test.js  (14 tests) 4ms
 ✓ src/parser.test.js  (20 tests) 5ms

 Test Files  2 passed (2)
      Tests  34 passed (34)
   Start at  13:24:04
   Duration  1.23s
"""
    assert _parse_vitest(output) == (34, 0, 0)


def test_parse_vitest_with_failures():
    output = """\
 Test Files  1 failed | 2 passed (3)
      Tests  3 failed | 31 passed (34)
"""
    assert _parse_vitest(output) == (31, 3, 0)


def test_parse_vitest_no_match():
    assert _parse_vitest("no output") == (0, 0, 0)


def test_parse_jest_all_pass():
    output = """\
Test Suites: 5 passed, 5 total
Tests:       42 passed, 42 total
"""
    assert _parse_jest(output) == (42, 0, 0)


def test_parse_jest_with_failures():
    output = """\
Test Suites: 1 failed, 4 passed, 5 total
Tests:       3 failed, 39 passed, 42 total
"""
    assert _parse_jest(output) == (39, 3, 0)


def test_parse_jest_no_match():
    assert _parse_jest("no output") == (0, 0, 0)


# --------------------------------------------------------------------------- #
# _parse_test_output dispatcher                                               #
# --------------------------------------------------------------------------- #


def test_dispatch_vitest_by_output():
    output = " Test Files  2 passed (2)\n      Tests  34 passed (34)"
    assert _parse_test_output("npm test", output) == (34, 0, 0)


def test_dispatch_jest_by_output():
    output = "Tests:       42 passed, 42 total"
    assert _parse_test_output("npm test", output) == (42, 0, 0)


def test_dispatch_vitest_by_command():
    output = "✓ src/foo.test.js (5 tests)\n Tests  5 passed (5)"
    assert _parse_test_output("npx vitest run", output) == (5, 0, 0)


def test_dispatch_pytest_by_command():
    output = "42 passed, 3 failed in 12s"
    assert _parse_test_output("pytest -q", output) == (42, 3, 0)


def test_dispatch_npm_test_with_pytest_output():
    output = "10 passed, 2 failed, 1 error in 5s"
    assert _parse_test_output("npm test", output) == (10, 2, 1)


# --------------------------------------------------------------------------- #
# Lint helpers (I1/I4)                                                         #
# --------------------------------------------------------------------------- #


def test_lint_supports_file_args_common_tools():
    assert _lint_supports_file_args("ruff check")
    assert _lint_supports_file_args("flake8 --max-line-length=120")
    assert _lint_supports_file_args("eslint --fix")
    assert _lint_supports_file_args("black --check")
    assert _lint_supports_file_args("mypy --strict")
    assert not _lint_supports_file_args("npm run lint")
    assert not _lint_supports_file_args("make lint")


def test_run_lint_no_command(tmp_path):
    result = run_lint_on_changed(tmp_path, lint_cmd=None)
    assert not result.ran
    assert result.ok


def test_run_lint_passes(tmp_path):
    (tmp_path / "ok.py").write_text("x = 1\n")
    result = run_lint_on_changed(tmp_path, lint_cmd="true")
    assert result.ran
    assert result.ok


def test_run_lint_fails(tmp_path):
    result = run_lint_on_changed(tmp_path, lint_cmd="false")
    assert result.ran
    assert not result.ok


def test_run_lint_scoped_to_changed_files(tmp_path):
    """When changed_files is provided and linter supports args, only those files are passed."""
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "b.py").write_text("y = 2\n")
    # Use "echo ruff check" so _lint_supports_file_args matches and the
    # shell just echoes the command + appended file args.
    result = run_lint_on_changed(
        tmp_path,
        lint_cmd="echo ruff check",
        changed_files=["a.py"],
    )
    assert result.ran
    assert result.ok
    assert "a.py" in result.output
    assert "b.py" not in result.output


# --------------------------------------------------------------------------- #
# The repo's virtualenv                                                        #
# --------------------------------------------------------------------------- #

def _fake_venv(repo: Path, name: str = ".venv") -> Path:
    """A venv whose `python` is a shell script that identifies itself."""
    bin_dir = repo / name / "bin"
    bin_dir.mkdir(parents=True)
    py = bin_dir / "python"
    py.write_text("#!/bin/sh\necho VENV_PYTHON_USED\n")
    py.chmod(0o755)
    return bin_dir


def test_run_tests_uses_the_repo_venv(tmp_path):
    """`run_tests` shells out with the SERVER's environment, which on a real
    machine has `python3` but no `python` at all. A confirmed profile in use
    says `python run_tests.py`, so every attempt handed the reviewer a
    failed invocation and TESTING never ran once across 8 tasks."""
    from no_human.testing.runner import run_tests

    _fake_venv(tmp_path)
    result = run_tests(tmp_path, "python run_tests.py")

    assert "VENV_PYTHON_USED" in result.output
    assert result.ok


def test_venv_bin_prefers_dot_venv_then_globs(tmp_path):
    from no_human.testing.runner import _venv_bin

    assert _venv_bin(tmp_path) is None          # no venv → unchanged behavior
    bin312 = _fake_venv(tmp_path, ".venv312")
    assert _venv_bin(tmp_path) == bin312        # found by glob
    canonical = _fake_venv(tmp_path, ".venv")
    assert _venv_bin(tmp_path) == canonical     # `.venv` wins when both exist


def test_env_for_sets_virtual_env_and_prepends_path(tmp_path):
    from no_human.testing.runner import _env_for

    bin_dir = _fake_venv(tmp_path)
    env = _env_for(tmp_path)

    assert env["PATH"].startswith(str(bin_dir))
    assert env["VIRTUAL_ENV"] == str(tmp_path / ".venv")


# --------------------------------------------------------------------------- #
# Test-output parsing                                                          #
# --------------------------------------------------------------------------- #

def test_unittest_summary_block_is_parsed_not_grepped():
    """The pytest fallback grepped the whole LOG for "N failed" instead of
    reading the summary block, so a run whose own `Failed:` line said 0, and
    which exited 0, was reported as failing — the reviewer's own evidence,
    wrong. Counts here are illustrative: a real suite's size is a fingerprint
    and the bug does not depend on the number."""
    from no_human.testing.runner import _parse_test_output

    output = (
        "some test printed the word failed 4 times, whatever\n"
        "============================================================\n"
        "Total tests run: 100\n"
        "Passed: 98\n"
        "Failed: 0\n"
        "Errors: 0\n"
    )
    assert _parse_test_output("python run_tests.py", output) == (98, 0, 0)


def test_pytest_output_still_parses():
    from no_human.testing.runner import _parse_test_output
    assert _parse_test_output("pytest -q", "5 passed, 2 failed in 1.2s") == (5, 2, 0)


def test_node_test_tap_summary_is_parsed():
    """Routing web changes to `node --test` must not read '# pass 40' as 0
    passed (the pytest fallback would). Real format captured live 2026-07-11."""
    from no_human.testing.runner import _parse_test_output
    out = "ok 1 - x\n# tests 40\n# pass 40\n# fail 0\n# duration_ms 120\n"
    assert _parse_test_output("node --test src/", out) == (40, 0, 0)
    fail_out = "# tests 40\n# pass 38\n# fail 2\n"
    assert _parse_test_output("node --test src/", fail_out) == (38, 2, 0)
    # Command-agnostic: the TAP summary alone triggers it.
    assert _parse_test_output("make check", "# pass 5\n# fail 0\n") == (5, 0, 0)


# --- process-group kill on timeout (venv-teardown-race, timeout half) --------

def test_run_shell_normal_completion(tmp_path):
    import os
    from no_human.testing.runner import _run_shell
    rc, out, timed_out = _run_shell("echo hello", tmp_path, 10, dict(os.environ))
    assert rc == 0 and "hello" in out and timed_out is False


def test_timeout_kills_the_whole_process_group(tmp_path):
    """On timeout the shell's backgrounded grandchild (stand-in for an xdist
    worker) must die too — not survive to hold the worktree .venv open. The
    grandchild would create a marker at t=2s; if the group is killed at the 1s
    timeout it never does. Plain subprocess.run only kills the direct child, so
    this marker WOULD appear (the bug)."""
    import os, time
    from no_human.testing.runner import _run_shell
    survived = tmp_path / "survived.marker"
    cmd = f"(sleep 2; touch {survived}) & wait"
    rc, out, timed_out = _run_shell(cmd, tmp_path, 1, dict(os.environ))
    assert timed_out is True
    time.sleep(2.5)  # past when the grandchild WOULD have touched the marker
    assert not survived.exists(), "grandchild survived timeout — process group not killed"


def test_terminate_running_kills_a_test_orphaned_by_cancellation(tmp_path):
    """A test subprocess left running (its awaiting coroutine cancelled) must be
    killed by terminate_running BEFORE the worktree teardown rmtree's its .venv."""
    import os, threading, time
    from no_human.testing.runner import _run_shell, terminate_running
    survived = tmp_path / "survived.marker"
    cmd = f"(sleep 3; touch {survived}) & wait"
    # Background thread mimics asyncio.to_thread: it registers itself, then we
    # call terminate_running from "the teardown" while it is still running.
    t = threading.Thread(target=_run_shell, args=(cmd, tmp_path, 30, dict(os.environ)))
    t.start()
    time.sleep(0.6)                       # let it register + spawn the grandchild
    killed = terminate_running(tmp_path)
    assert killed >= 1
    t.join(timeout=5)
    time.sleep(3.2)                       # past when the grandchild WOULD touch it
    assert not survived.exists(), "orphan survived terminate_running — race not closed"


def test_terminate_running_is_a_noop_when_nothing_runs(tmp_path):
    from no_human.testing.runner import terminate_running
    assert terminate_running(tmp_path) == 0


# ── Phase 0.1: pin the interpreter/venv resolution that fixes the historical
# "0 passed, 0 failed, 1 errors" env failure (python missing on the server PATH).
# The server shell has python3 but no `python`; a repo venv or the python3
# fallback must resolve it, and it must be classified as an invocation error
# (retryable) — never reported as a genuine test failure. ────────────────────

def test_python_not_found_classified_as_invocation_error_not_test_failure():
    # returncode!=0 with 0/0/0 counts + "command not found" is an invocation
    # error (retry with a fixed command), NOT "1 errors" reported as a failure.
    from no_human.testing.runner import _is_invocation_error
    out = "/bin/sh: python: command not found"
    assert _is_invocation_error(
        returncode=127, output=out, passed=0, failed=0, errors=0) is True


def test_fix_invocation_rewrites_python_to_python3():
    from no_human.testing.runner import _fix_invocation
    out = "/bin/sh: python: command not found"
    # the exact command shape from a confirmed real profile
    assert _fix_invocation("python run_tests.py", out, Path("/tmp")) == "python3 run_tests.py"
    assert _fix_invocation("python -m pytest -q", out, Path("/tmp")) == "python3 -m pytest -q"


def test_fix_invocation_leaves_non_python_and_found_python_alone():
    from no_human.testing.runner import _fix_invocation
    # python present (no "command not found") → no rewrite
    assert _fix_invocation("python x.py", "3 passed", Path("/tmp")) is None
    # a node command is never rewritten to python3
    assert _fix_invocation("node --test", "node: command not found", Path("/tmp")) is None


def test_fix_invocation_reruns_bare_pytest_via_our_interpreter(tmp_path):
    # A bare `pytest` whose only problem is that pytest-the-tool isn't
    # importable on that PATH is re-run through no_human's own interpreter,
    # which always has pytest. Args after `pytest` are preserved.
    import sys

    from no_human.testing.runner import _fix_invocation

    out = "ModuleNotFoundError: No module named 'pytest'"
    fixed = _fix_invocation("pytest -q", out, tmp_path)
    assert fixed is not None
    assert fixed.startswith(sys.executable)
    assert fixed.endswith("-m pytest -q")


def test_fix_invocation_leaves_a_project_dep_gap_honest(tmp_path):
    # A ModuleNotFoundError naming some OTHER module is a project-dependency
    # gap, not pytest-the-tool missing — it must NOT be silently swapped, so the
    # run stays at honest "no test evidence".
    from no_human.testing.runner import _fix_invocation

    out = "ModuleNotFoundError: No module named 'requests'"
    assert _fix_invocation("pytest -q", out, tmp_path) is None


def test_fix_invocation_does_not_hijack_uv_run_pytest(tmp_path):
    # `uv run pytest ...` manages its own environment (a different failure
    # mode) — the bare-pytest rule must not touch it.
    from no_human.testing.runner import _fix_invocation

    assert _fix_invocation("uv run pytest -q", "No module named pytest", tmp_path) is None


def test_run_tests_recovers_evidence_when_only_pytest_was_missing(tmp_path, monkeypatch):
    # Integration: a bare `pytest` that can't import pytest is retried once via
    # `sys.executable -m pytest`, which passes — so the run yields real test
    # evidence (ok, not the advisory invocation_error path).
    import sys

    import no_human.testing.runner as runner_mod

    calls = []

    def fake_run_shell(cmd, work_dir, timeout, env):
        calls.append(cmd)
        if len(calls) == 1:
            return 1, "ModuleNotFoundError: No module named 'pytest'", False
        return 0, "1 passed in 0.01s", False

    monkeypatch.setattr(runner_mod, "_run_shell", fake_run_shell)

    result = runner_mod.run_tests(tmp_path, "pytest -q", timeout=3)

    assert len(calls) == 2, ("expected exactly one fixed-command retry", calls)
    assert calls[1].startswith(sys.executable), calls[1]
    assert result.ok is True
    assert result.invocation_error is False
    assert result.passed == 1


def test_venv_bin_finds_repo_python(tmp_path):
    from no_human.testing.runner import _venv_bin
    assert _venv_bin(tmp_path) is None  # no venv yet
    bin_dir = tmp_path / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "python").write_text("#!/bin/sh\n")
    assert _venv_bin(tmp_path) == bin_dir


def test_env_for_activates_repo_venv(tmp_path):
    from no_human.testing.runner import _env_for
    bin_dir = tmp_path / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "python").write_text("#!/bin/sh\n")
    env = _env_for(tmp_path)
    # venv bin prepended to PATH (so `python` resolves) + VIRTUAL_ENV set
    assert env["PATH"].split(":")[0] == str(bin_dir)
    assert env["VIRTUAL_ENV"] == str(tmp_path / ".venv")


# --------------------------------------------------------------------------- #
# SCRUM-35: hermetic web tests in worktrees. `node_modules` is gitignored, so
# a freshly created task worktree (`git worktree add --detach`) never has it —
# unlike a Python venv, which is physically present and auto-reused by
# `_venv_bin`/`_env_for` above. Live 2026-07-25 (SCRUM-33): a worktree-only
# missing-deps failure read as "2335 passed, 1 failed" and burned the whole
# bounded loop chasing a test that was never broken (359/359 green against the
# real `web/node_modules`). Fix: symlink node deps from the source checkout
# before a node/npm test command runs, and — when that isn't possible —
# classify a module-resolution failure as `invocation_error` (INFRA) instead
# of a genuine test failure.
# --------------------------------------------------------------------------- #

def test_ensure_node_deps_symlinks_from_source(tmp_path):
    """A bare worktree with no `node_modules` gets one symlinked in from the
    source repo's populated install, at the same relative path as the cwd."""
    from no_human.testing.runner import run_tests

    source_repo = tmp_path / "source_repo"
    (source_repo / "web" / "node_modules" / "left-pad").mkdir(parents=True)
    work_dir = tmp_path / "work_dir"
    (work_dir / "web").mkdir(parents=True)

    run_tests(
        work_dir, "node --test src/",
        cwd=work_dir / "web", source_repo=source_repo,
    )

    linked = work_dir / "web" / "node_modules"
    assert linked.is_symlink()
    assert linked.resolve() == (source_repo / "web" / "node_modules").resolve()


def test_ensure_node_deps_noop_when_present(tmp_path):
    """Deps already present in the worktree are left alone — never replaced
    by a symlink, even when a source repo is given."""
    from no_human.testing.runner import run_tests

    source_repo = tmp_path / "source_repo"
    (source_repo / "web" / "node_modules").mkdir(parents=True)
    work_dir = tmp_path / "work_dir"
    existing = work_dir / "web" / "node_modules"
    existing.mkdir(parents=True)
    marker = existing / "REAL_INSTALL"
    marker.write_text("present")

    run_tests(
        work_dir, "node --test src/",
        cwd=work_dir / "web", source_repo=source_repo,
    )

    assert not existing.is_symlink(), "a real install must not be replaced"
    assert marker.exists()


def test_ensure_node_deps_skips_python_command(tmp_path):
    """A pytest command never triggers node provisioning — existing
    python/venv behaviour is unaffected by the new source_repo param."""
    from no_human.testing.runner import run_tests

    source_repo = tmp_path / "source_repo"
    (source_repo / "node_modules").mkdir(parents=True)
    work_dir = tmp_path / "work_dir"
    work_dir.mkdir()

    run_tests(work_dir, "true", source_repo=source_repo)

    assert not (work_dir / "node_modules").exists()


def _fake_node_script(bin_dir: Path, body: str) -> dict[str, str]:
    """A fake `node` on PATH that prints canned output instead of running a
    real suite — real subprocess execution, no mocking of the runner."""
    import os as _os
    bin_dir.mkdir(parents=True, exist_ok=True)
    node = bin_dir / "node"
    node.write_text(f"#!/bin/sh\n{body}\n")
    node.chmod(0o755)
    return {"PATH": f"{bin_dir}{_os.pathsep}{_os.environ.get('PATH', '')}"}


def test_node_missing_deps_classified_as_invocation_error(tmp_path):
    """A node module-resolution failure — the SCRUM-33 shape, most tests
    collected fine but one file can't resolve its import — must classify as
    `invocation_error`, not a plain test failure the coder gets blamed for."""
    from no_human.testing.runner import run_tests

    env = _fake_node_script(
        tmp_path / "bin",
        "printf '# tests 3\\n# pass 2\\n# fail 0\\n'\n"
        "printf \"Error [ERR_MODULE_NOT_FOUND]: Cannot find package "
        "'left-pad'\\n\" 1>&2\n"
        "exit 1\n",
    )

    result = run_tests(tmp_path, "node --test src/", env=env)

    assert result.invocation_error is True
    assert result.failed == 0, "a dependency-resolution crash is not a test failure count"


def test_node_real_test_failure_is_not_invocation_error(tmp_path):
    """Guard against over-broad classification: a genuine assertion failure,
    with no module-resolution error in the output, must still read as a real
    test failure."""
    from no_human.testing.runner import run_tests

    env = _fake_node_script(
        tmp_path / "bin",
        "printf '# tests 3\\n# pass 2\\n# fail 1\\n'\n"
        "printf 'not ok 3 - assertion mismatch\\n'\n"
        "printf '  AssertionError: expected 2 to equal 3\\n'\n"
        "exit 1\n",
    )

    result = run_tests(tmp_path, "node --test src/", env=env)

    assert result.invocation_error is False
    assert result.failed == 1


# --------------------------------------------------------------------------- #
# SCRUM-37: name failing tests + INFRA-classify the xdist teardown race       #
# --------------------------------------------------------------------------- #


def test_parse_pytest_anchors_to_summary_line_not_cross_line_match():
    """The old regex scanned the WHOLE output with `\\s+` (which matches a
    newline), so a digit ending one line and a keyword starting the next
    ('...retry 1\\nfailed to acquire lock...') was misread as '1 failed'.
    Anchoring to the single summary LINE fixes it."""
    output = (
        "collected 5 items, retrying flaky setup 1\n"
        "failed to acquire lock on first try, continuing\n"
        "5 passed in 1.02s\n"
    )
    assert _parse_pytest(output) == (5, 0, 0)


def test_pytest_failing_tests_extracts_and_dedupes_names():
    output = (
        "FAILED tests/test_x.py::test_y - AssertionError: boom\n"
        "ERROR tests/test_z.py::test_w - RuntimeError: kaboom\n"
        "FAILED tests/test_x.py::test_y - AssertionError: boom\n"
        "2 failed, 1 error, 10 passed in 1.02s\n"
    )
    assert _pytest_failing_tests(output) == [
        "tests/test_x.py::test_y",
        "tests/test_z.py::test_w",
    ]


def test_pytest_failing_tests_empty_when_no_named_failures():
    assert _pytest_failing_tests("5 passed in 1.02s\n") == []


def test_is_teardown_race_detects_signature():
    assert _is_teardown_race(
        "OSError: [Errno 66] Directory not empty: "
        "'/tmp/pytest-of-dev/garbage-0/popen-gw3'"
    )
    assert not _is_teardown_race("AssertionError: expected 2 to equal 3")


def _teardown_race_script(tmp_path, counter_path, *, retry_output_ok=True):
    """A fake test command that increments *counter_path* on every call.
    Call 1 always emits the teardown-race OSError signature after a clean
    pytest summary and exits non-zero (the reproduced bug). Call 2+ emits
    *retry_output_ok*'s outcome, so the test can assert exactly one retry."""
    script = tmp_path / "fake_pytest.sh"
    second_call_body = (
        "echo '5 passed in 1.02s'\nexit 0\n"
        if retry_output_ok else
        "echo '5 passed in 1.02s'\n"
        "echo \"OSError: [Errno 66] Directory not empty: "
        "'/tmp/pytest-of-dev/garbage-0/popen-gw3'\" 1>&2\n"
        "exit 1\n"
    )
    script.write_text(
        "#!/bin/sh\n"
        f"n=$(cat {counter_path} 2>/dev/null || echo 0)\n"
        "n=$((n+1))\n"
        f"echo $n > {counter_path}\n"
        'if [ "$n" = "1" ]; then\n'
        "  echo '5 passed in 1.02s'\n"
        "  echo \"OSError: [Errno 66] Directory not empty: "
        "'/tmp/pytest-of-dev/garbage-0/popen-gw3'\" 1>&2\n"
        "  exit 1\n"
        "fi\n"
        f"{second_call_body}"
    )
    script.chmod(0o755)
    return script


def test_teardown_race_not_counted_as_failed_and_retried_exactly_once(tmp_path):
    """The reproduced SCRUM-37 shape: tests actually passed (summary shows 0
    failed), but the late xdist teardown race made the process exit non-zero.
    Must retry exactly once and, once the race clears, report a clean pass —
    never a bare failure count."""
    counter = tmp_path / "count"
    script = _teardown_race_script(tmp_path, counter, retry_output_ok=True)

    result = run_tests(tmp_path, str(script))

    assert counter.read_text().strip() == "2", "expected exactly one retry (2 total invocations)"
    assert result.ok is True
    assert result.failed == 0
    assert result.failing_tests == []


def test_teardown_race_retry_still_racing_reports_failure_after_one_retry_only(tmp_path):
    """If the retry ALSO hits the race, the bounded single retry doctrine
    still applies: exactly one retry, then report whatever that retry found —
    never an unbounded retry loop."""
    counter = tmp_path / "count"
    script = _teardown_race_script(tmp_path, counter, retry_output_ok=False)

    result = run_tests(tmp_path, str(script))

    assert counter.read_text().strip() == "2", "expected exactly one retry (2 total invocations)"
    assert result.ok is False


def test_real_failure_with_teardown_noise_still_names_the_failing_test(tmp_path):
    """Guard against over-broad classification (mirrors the node real-failure
    guard above): a genuine named failure alongside teardown noise in the
    SAME run must be reported as-is, with the test named — never swallowed
    as INFRA, and never retried."""
    counter = tmp_path / "count"
    script = tmp_path / "fake_pytest_real_failure.sh"
    script.write_text(
        "#!/bin/sh\n"
        f"n=$(cat {counter} 2>/dev/null || echo 0)\n"
        "n=$((n+1))\n"
        f"echo $n > {counter}\n"
        "echo 'FAILED tests/test_x.py::test_y - AssertionError: boom'\n"
        "echo '1 failed, 4 passed in 1.02s'\n"
        "echo \"OSError: [Errno 66] Directory not empty: "
        "'/tmp/pytest-of-dev/garbage-0/popen-gw3'\" 1>&2\n"
        "exit 1\n"
    )
    script.chmod(0o755)

    result = run_tests(tmp_path, str(script))

    assert counter.read_text().strip() == "1", "a real named failure must not trigger the teardown retry"
    assert result.ok is False
    assert result.failed == 1
    assert result.failing_tests == ["tests/test_x.py::test_y"]


def test_teardown_race_detected_without_popen_gw_token():
    """SCRUM-37 review finding: the race's OSError often names only the
    garbage-*/pytest-of-* staging dir, with no popen-gw token on that line —
    the exact output this repo's own suite produced live."""
    from no_human.testing.runner import _is_teardown_race

    live = (
        "  <class 'OSError'>: [Errno 66] Directory not empty: "
        "'/private/var/folders/kc/4f5s3w2933q0q8nl6jvn89f40000gn/T/"
        "pytest-of-user/garbage-b8ca300b-dd84-4fc0-a262-474229fcdafd'\n"
        "    warnings.warn(\n"
    )
    assert _is_teardown_race(live) is True
    # And a real test failure mentioning a directory never matches.
    assert _is_teardown_race(
        "FAILED tests/test_x.py::test_dir - AssertionError: Directory not empty"
    ) is False


def test_worktree_src_leads_pythonpath(tmp_path):
    """SCRUM-18's doom loop: a worktree's tests imported MAIN's code because
    the shared venv's editable install shadowed the worktree src/. When the
    test cwd is a different tree than repo_path and has a src/ dir, that src/
    must lead PYTHONPATH so the worktree's package wins."""
    repo = tmp_path / "repo"
    wt = tmp_path / "wt"
    (wt / "src").mkdir(parents=True)
    repo.mkdir()
    script = wt / "t.sh"
    script.write_text("#!/bin/sh\necho PP=$PYTHONPATH\necho 1 passed in 0.1s\n")
    script.chmod(0o755)

    result = run_tests(repo, str(script), cwd=wt)

    assert result.ok is True
    assert f"PP={wt / 'src'}" in result.output


def test_worktree_src_prepends_not_replaces_pythonpath(tmp_path):
    """An operator-supplied PYTHONPATH must survive BEHIND the worktree src."""
    repo = tmp_path / "repo"
    wt = tmp_path / "wt"
    (wt / "src").mkdir(parents=True)
    repo.mkdir()
    script = wt / "t.sh"
    script.write_text("#!/bin/sh\necho PP=$PYTHONPATH\necho 1 passed in 0.1s\n")
    script.chmod(0o755)

    result = run_tests(repo, str(script), cwd=wt, env={"PYTHONPATH": "/keep/me"})

    assert f"PP={wt / 'src'}:/keep/me" in result.output


def test_no_src_dir_leaves_pythonpath_alone(tmp_path):
    """A flat-layout worktree (no src/) must not get a phantom entry."""
    repo = tmp_path / "repo"
    wt = tmp_path / "wt"
    wt.mkdir()
    repo.mkdir()
    script = wt / "t.sh"
    script.write_text("#!/bin/sh\necho PP=[$PYTHONPATH]\necho 1 passed in 0.1s\n")
    script.chmod(0o755)

    result = run_tests(repo, str(script), cwd=wt, env={"PYTHONPATH": ""})

    assert "PP=[]" in result.output


def test_same_tree_run_does_not_inject_src(tmp_path):
    """cwd == repo_path (the main checkout itself): the editable install IS
    that tree, injection is pointless — must stay a no-op."""
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    script = repo / "t.sh"
    script.write_text("#!/bin/sh\necho PP=[$PYTHONPATH]\necho 1 passed in 0.1s\n")
    script.chmod(0o755)

    result = run_tests(repo, str(script), env={"PYTHONPATH": ""})

    assert "PP=[]" in result.output


# --- SCRUM-40: traceback excerpts ------------------------------------------

def _make_failure_block(name, n_lines, line_text="line"):
    header = f"_____ {name} _____\n"
    body = "".join(f"    {line_text} {i}\n" for i in range(n_lines))
    return header + body


def test_traceback_excerpt_single_failure():
    output = (
        "=================================== FAILURES ===================================\n"
        "_________________________________ test_y _________________________________\n"
        "\n"
        "    def test_y():\n"
        ">       assert 1 == 2\n"
        "E       AssertionError: assert 1 == 2\n"
        "\n"
        "tests/test_x.py:5: AssertionError\n"
        "=========================== short test summary info ============================\n"
        "FAILED tests/test_x.py::test_y - AssertionError: assert 1 == 2\n"
        "1 failed in 0.10s\n"
    )
    failing_tests = ["tests/test_x.py::test_y"]

    excerpts = _pytest_traceback_excerpts(output, failing_tests)

    assert set(excerpts) == {"tests/test_x.py::test_y"}
    assert "AssertionError: assert 1 == 2" in excerpts["tests/test_x.py::test_y"]


def test_traceback_excerpts_cap_to_three_largest_first():
    """5 failing tests, differing block sizes: only the 3 largest (by
    uncapped line count) survive, tiebroken by original failure order, but
    the RESULT preserves chronological order."""
    failing_tests = [f"tests/test_x.py::test_{c}" for c in "abcde"]
    sizes = {"test_a": 3, "test_b": 10, "test_c": 10, "test_d": 6, "test_e": 8}
    output = (
        "================================== FAILURES ===================================\n"
        + "".join(_make_failure_block(name, n) for name, n in sizes.items())
        + "=========================== short test summary info ============================\n"
        + "5 failed in 1.0s\n"
    )

    excerpts = _pytest_traceback_excerpts(output, failing_tests)

    assert list(excerpts.keys()) == [
        "tests/test_x.py::test_b",
        "tests/test_x.py::test_c",
        "tests/test_x.py::test_e",
    ]
    assert "tests/test_x.py::test_a" not in excerpts
    assert "tests/test_x.py::test_d" not in excerpts


def test_traceback_excerpt_capped_at_40_lines_and_2kb():
    output = (
        _make_failure_block("test_big", 60, line_text="x" * 60)
        + "=========================== short test summary info ============================\n"
        + "1 failed in 1.0s\n"
    )
    failing_tests = ["tests/test_x.py::test_big"]

    excerpts = _pytest_traceback_excerpts(output, failing_tests)
    excerpt = excerpts["tests/test_x.py::test_big"]

    assert len(excerpt.splitlines()) <= 41  # 40 lines + truncation marker
    assert len(excerpt) <= 2048 + len("\n… [truncated]")
    assert "truncated" in excerpt


def test_traceback_excerpts_empty_on_passing_output():
    assert _pytest_traceback_excerpts("5 passed in 1.0s\n", []) == {}


def test_traceback_excerpts_malformed_output_returns_empty():
    output = "1 failed in 0.1s\nFAILED tests/test_x.py::test_y - boom\n"
    failing_tests = ["tests/test_x.py::test_y"]

    assert _pytest_traceback_excerpts(output, failing_tests) == {}


def test_traceback_excerpt_maps_class_and_param_names():
    output = (
        "_____ TestC.test_y _____\n"
        "    assert False\n"
        "_____ test_y[p] _____\n"
        "    assert False\n"
        "=========================== short test summary info ============================\n"
        "2 failed in 1.0s\n"
    )
    failing_tests = [
        "tests/test_x.py::TestC::test_y",
        "tests/test_x.py::test_y[p]",
    ]

    excerpts = _pytest_traceback_excerpts(output, failing_tests)

    assert set(excerpts) == set(failing_tests)


def test_render_traceback_block_is_human_readable_and_untruncated():
    excerpts = {
        "tests/test_x.py::test_y": "line1\nline2\nline3",
    }

    rendered = render_traceback_excerpts(excerpts)

    assert rendered.startswith("Traceback excerpts:")
    assert "tests/test_x.py::test_y" in rendered
    assert "line1" in rendered and "line2" in rendered and "line3" in rendered
    assert render_traceback_excerpts({}) == ""


def test_run_tests_populates_traceback_excerpts(tmp_path):
    script = tmp_path / "fake_pytest.sh"
    script.write_text(
        "#!/bin/sh\n"
        "cat <<'PYEOF'\n"
        "=================================== FAILURES ===================================\n"
        "_________________________________ test_y _________________________________\n"
        "\n"
        "    def test_y():\n"
        ">       assert 1 == 2\n"
        "E       AssertionError: assert 1 == 2\n"
        "\n"
        "tests/test_x.py:5: AssertionError\n"
        "=========================== short test summary info ============================\n"
        "FAILED tests/test_x.py::test_y - AssertionError: assert 1 == 2\n"
        "1 failed in 0.10s\n"
        "PYEOF\n"
        "exit 1\n"
    )
    script.chmod(0o755)

    result = run_tests(tmp_path, str(script))

    assert result.failing_tests == ["tests/test_x.py::test_y"]
    assert "tests/test_x.py::test_y" in result.traceback_excerpts
    assert "AssertionError: assert 1 == 2" in result.traceback_excerpts["tests/test_x.py::test_y"]
    assert "AssertionError: assert 1 == 2" in result.traceback_block


def test_worktree_repo_with_source_repo_injects_its_own_src(tmp_path):
    """Review 2026-07-25 (empirically proven gap): the orchestrator's real
    call shape is run_tests(repo_path=<worktree>, cwd=None, source_repo=
    <primary>) — work_dir == repo_path, so the original gate never fired.
    source_repo being set IS the worktree signal; the worktree's own src/
    must lead PYTHONPATH."""
    primary = tmp_path / "primary"
    wt = tmp_path / "wt"
    (wt / "src").mkdir(parents=True)
    primary.mkdir()
    script = wt / "t.sh"
    script.write_text("#!/bin/sh\necho PP=$PYTHONPATH\necho 1 passed in 0.1s\n")
    script.chmod(0o755)

    result = run_tests(wt, str(script), source_repo=primary)

    assert result.ok is True
    assert f"PP={wt / 'src'}" in result.output


def test_no_source_repo_and_same_dir_still_injects_nothing(tmp_path):
    """Primary-checkout runs (source_repo=None, cwd=None) stay untouched."""
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    script = repo / "t.sh"
    script.write_text("#!/bin/sh\necho PP=[$PYTHONPATH]\necho 1 passed in 0.1s\n")
    script.chmod(0o755)

    result = run_tests(repo, str(script), env={"PYTHONPATH": ""})

    assert "PP=[]" in result.output


def test_failing_test_extraction_ignores_log_lines_without_node_ids():
    """Review 2026-07-25: 'ERROR urllib3.connectionpool - retry' (log_cli
    format) minted a phantom failing test AND defeated the teardown-race
    INFRA gate. Only real ::-bearing node ids count."""
    from no_human.testing.runner import _pytest_failing_tests

    out = (
        "ERROR urllib3.connectionpool - Connection pool is full\n"
        "FAILED tests/test_x.py::test_y - AssertionError: boom\n"
        "ERROR tests/test_z.py::test_w - RuntimeError\n"
    )
    assert _pytest_failing_tests(out) == [
        "tests/test_x.py::test_y", "tests/test_z.py::test_w"]


def test_teardown_race_retry_timeout_is_not_an_invocation_error(tmp_path):
    """Review 2026-07-25 residue: a timeout must read the same wherever it
    happens. The first-run timeout deliberately does NOT set
    invocation_error (a hanging suite must never earn the advisory
    proceed-without-test-evidence path via the base-tree check); the
    teardown-race retry's timeout must behave identically."""
    counter = tmp_path / "count"
    script = tmp_path / "fake_pytest_retry_hang.sh"
    script.write_text(
        "#!/bin/sh\n"
        f"n=$(cat {counter} 2>/dev/null || echo 0)\n"
        "n=$((n+1))\n"
        f"echo $n > {counter}\n"
        'if [ "$n" = "1" ]; then\n'
        "  echo '5 passed in 1.02s'\n"
        "  echo \"OSError: [Errno 66] Directory not empty: "
        "'/tmp/pytest-of-dev/garbage-0/popen-gw3'\" 1>&2\n"
        "  exit 1\n"
        "fi\n"
        "sleep 30\n"
    )
    script.chmod(0o755)

    result = run_tests(tmp_path, str(script), timeout=3)

    assert counter.read_text().strip() == "2", "expected exactly one retry"
    assert result.ok is False
    assert "timed out" in result.output
    assert result.invocation_error is False


def test_teardown_race_retry_invocation_error_is_reclassified(tmp_path):
    """Review 2026-07-25 residue: if the teardown-race retry run itself hits
    an invocation error (runner broke between runs — e.g. command vanished),
    it must be classified invocation_error=True like every other invocation
    error, not blamed on the coder as a plain test failure."""
    counter = tmp_path / "count"
    script = tmp_path / "fake_pytest_retry_invoke.sh"
    script.write_text(
        "#!/bin/sh\n"
        f"n=$(cat {counter} 2>/dev/null || echo 0)\n"
        "n=$((n+1))\n"
        f"echo $n > {counter}\n"
        'if [ "$n" = "1" ]; then\n'
        "  echo '5 passed in 1.02s'\n"
        "  echo \"OSError: [Errno 66] Directory not empty: "
        "'/tmp/pytest-of-dev/garbage-0/popen-gw3'\" 1>&2\n"
        "  exit 1\n"
        "fi\n"
        "echo 'sh: pytest: command not found' 1>&2\n"
        "exit 127\n"
    )
    script.chmod(0o755)

    result = run_tests(tmp_path, str(script), timeout=30)

    assert counter.read_text().strip() == "2", "expected exactly one retry"
    assert result.ok is False
    assert result.invocation_error is True


def test_traceback_excerpts_duplicate_short_names_across_files():
    """Review 2026-07-25 residue: tests/a.py::test_y and tests/b.py::test_y
    share the short header name test_y. Each node id must get ITS OWN block
    (paired in order), never the first block duplicated under both ids."""
    output = (
        "=================================== FAILURES ===================================\n"
        "_________________________________ test_y _________________________________\n"
        "\n"
        "    def test_y():\n"
        ">       assert 1 == 2\n"
        "E       AssertionError: from FILE A\n"
        "\n"
        "tests/a.py:5: AssertionError\n"
        "_________________________________ test_y _________________________________\n"
        "\n"
        "    def test_y():\n"
        ">       assert 3 == 4\n"
        "E       AssertionError: from FILE B\n"
        "\n"
        "tests/b.py:9: AssertionError\n"
        "=========================== short test summary info ============================\n"
        "FAILED tests/a.py::test_y - AssertionError: from FILE A\n"
        "FAILED tests/b.py::test_y - AssertionError: from FILE B\n"
        "2 failed in 0.10s\n"
    )
    failing_tests = ["tests/a.py::test_y", "tests/b.py::test_y"]

    excerpts = _pytest_traceback_excerpts(output, failing_tests)

    assert set(excerpts) == {"tests/a.py::test_y", "tests/b.py::test_y"}
    assert "from FILE A" in excerpts["tests/a.py::test_y"]
    assert "from FILE B" in excerpts["tests/b.py::test_y"]
    assert "from FILE B" not in excerpts["tests/a.py::test_y"]
    assert "from FILE A" not in excerpts["tests/b.py::test_y"]


def test_invocation_retry_timeout_is_not_an_invocation_error(tmp_path, monkeypatch):
    """Review 2026-07-25 residue follow-up: the timeout doctrine applies to
    EVERY retry path — a suite that hangs after a _fix_invocation retry must
    not earn the advisory invocation_error path either (same rule as the
    first-run and teardown-retry timeouts)."""
    import no_human.testing.runner as runner_mod

    calls = []

    def fake_run_shell(cmd, work_dir, timeout, env):
        calls.append(cmd)
        if len(calls) == 1:
            # Invocation-error shape with a fixable cause: pytest + bad flags.
            return 4, "ERROR: usage: pytest\nunrecognized arguments: --nope", False
        return 0, "", True  # the fixed retry hangs -> timeout

    monkeypatch.setattr(runner_mod, "_run_shell", fake_run_shell)

    result = runner_mod.run_tests(tmp_path, "pytest -q --nope", timeout=3)

    assert len(calls) == 2, "expected exactly one fixed-command retry"
    assert result.ok is False
    assert "timed out" in result.output
    assert result.invocation_error is False


def test_teardown_race_retry_that_hits_an_invocation_error_keeps_the_names(tmp_path):
    """The teardown-race RETRY has its own `invocation_error` return, and it
    dropped `failing_tests` — so a retry that both stumbled on module
    resolution AND named real failures reached the PR body with an empty
    "- failing tests:" list it had promised to fill.

    Reachable exactly as written: call 1 is the race (clean summary, non-zero
    exit, OSError signature), call 2 produces real counts, named failures and a
    ModuleNotFoundError, which `_is_invocation_error` flags DESPITE the counts.
    """
    counter = tmp_path / "count"
    script = tmp_path / "fake_pytest.sh"
    script.write_text(
        "#!/bin/sh\n"
        f"n=$(cat {counter} 2>/dev/null || echo 0)\n"
        "n=$((n+1))\n"
        f"echo $n > {counter}\n"
        'if [ "$n" = "1" ]; then\n'
        "  echo '5 passed in 1.02s'\n"
        "  echo \"OSError: [Errno 66] Directory not empty: "
        "'/tmp/pytest-of-dev/garbage-0/popen-gw3'\" 1>&2\n"
        "  exit 1\n"
        "fi\n"
        "echo 'FAILED test_alpha.py::test_needs_the_dep - ModuleNotFoundError'\n"
        "echo '1 failed, 4 passed in 0.90s'\n"
        "echo \"ModuleNotFoundError: No module named 'left_pad'\" 1>&2\n"
        "exit 1\n"
    )
    script.chmod(0o755)

    result = run_tests(tmp_path, str(script))

    assert counter.read_text().strip() == "2", "expected exactly one retry"
    assert result.invocation_error is True, "not the invocation-error retry path"
    assert result.passed + result.failed + result.errors > 0, (
        "not the partial-run shape — this retry produced no counts")
    assert result.failing_tests, (
        "the teardown-race retry dropped the failing test names, so the PR "
        "body's '- failing tests:' block cannot render for this run")
    assert any("test_needs_the_dep" in f for f in result.failing_tests), (
        result.failing_tests)


def test_fixable_retry_that_hits_an_invocation_error_keeps_the_names(
    tmp_path, monkeypatch,
):
    """The THIRD `invocation_error=True` return, and the one nothing covered.

    `run_tests` has three of them and all three gained `failing_tests=` in the
    same change. Dropping it from the teardown-race return reddens
    `test_teardown_race_retry_that_hits_an_invocation_error_keeps_the_names`;
    dropping it from the no-fixable-retry return reddens
    `test_a_partial_run_reaches_the_body_with_its_failing_test_names`. Dropping
    it from THIS one — the `_fix_invocation` retry whose corrected command still
    trips `_is_invocation_error` — was green across the runner and PR-body
    suites. An unobserved write path, not a covered one.

    Reachable exactly as production reaches it: call 1 is `pytest` with a flag
    its addopts rejects (`_fix_invocation`'s second rule, so the retry command
    really is different), and call 2 runs the corrected command, names a real
    failure, and still prints a ModuleNotFoundError — which
    `_is_invocation_error` flags DESPITE the non-zero counts, because
    `_INVOCATION_ERROR_PATTERNS` matches whenever the run is non-zero.
    """
    import no_human.testing.runner as runner_mod

    calls = []

    def fake_run_shell(cmd, work_dir, timeout, env):
        calls.append(cmd)
        if len(calls) == 1:
            return 4, "ERROR: usage: pytest\nunrecognized arguments: --nope", False
        return 1, (
            "FAILED tests/test_alpha.py::test_needs_the_dep - ModuleNotFoundError\n"
            "ModuleNotFoundError: No module named 'left_pad'\n"
            "1 failed, 4 passed in 0.90s\n"
        ), False

    monkeypatch.setattr(runner_mod, "_run_shell", fake_run_shell)

    result = runner_mod.run_tests(tmp_path, "pytest -q --nope", timeout=3)

    assert len(calls) == 2, ("expected exactly one fixed-command retry", calls)
    assert calls[1] != calls[0], "the retry ran the same command — path not taken"
    assert result.invocation_error is True, (
        "not the retry-still-broken path", result)
    assert result.passed + result.failed + result.errors > 0, (
        "not the partial-run shape — this retry produced no counts")
    assert result.failing_tests, (
        "the fixable retry dropped the failing test names, so the PR body's "
        "'- failing tests:' block cannot render for this run")
    assert any("test_needs_the_dep" in f for f in result.failing_tests), (
        result.failing_tests)


# --- node TAP failing-id parsing (round-3 review MAJOR) ---------------------
#
# `runner` only ever had `_pytest_failing_tests`, so `failing_tests` stayed
# `[]` for every node run — the per-test ownership gate
# (`ownership.owned_failing_ids` / orchestrator's `_owned_failing_tests`) was
# a silent no-op for the whole ecosystem, because there was never an id to
# look up. `_node_tap_failing_tests` / `_failing_ids` fix that.


def test_node_tap_failing_ids_and_locations():
    from no_human.testing.runner import _node_tap_failing_tests

    output = (
        "TAP version 13\n"
        "ok 1 - the button renders enabled\n"
        "not ok 2 - the built board bundle retains the label map\n"
        "  ---\n"
        "  error: expected 'true' to be truthy\n"
        "  location: '/repo/web/src/eventLabels.test.mjs:12:3'\n"
        "  ...\n"
        "ok 3 - a skipped-looking name that still passed\n"
        "not ok 4 - todo not yet implemented # TODO\n"
        "  ---\n"
        "  error: not implemented\n"
        "  ...\n"
        "not ok 5 - flaky under load # SKIP flaky\n"
        "  ---\n"
        "  ...\n"
        "not ok 6 - a failure with no location line at all\n"
        "  ---\n"
        "  error: boom\n"
        "  ...\n"
        "1..6\n"
        "# tests 6\n"
        "# pass 2\n"
        "# fail 2\n"
    )

    ids = _node_tap_failing_tests(output, repo_path=Path("/repo"), work_dir=Path("/repo"))

    # SKIP/TODO-directive blocks are not failures and are dropped.
    assert ids == [
        "web/src/eventLabels.test.mjs::the built board bundle retains the label map",
        "a failure with no location line at all",
    ]


def test_node_tap_failing_ids_relativize_against_work_dir_first():
    from no_human.testing.runner import _node_tap_failing_tests

    output = (
        "not ok 1 - name\n"
        "  ---\n"
        "  location: '/repo/worktrees/attempt-1/src/x.test.mjs:1:1'\n"
        "  ...\n"
    )
    ids = _node_tap_failing_tests(
        output,
        repo_path=Path("/repo"),
        work_dir=Path("/repo/worktrees/attempt-1"),
    )
    assert ids == ["src/x.test.mjs::name"]


def test_node_tap_failing_ids_is_deduplicated_and_ordered():
    from no_human.testing.runner import _node_tap_failing_tests

    output = (
        "not ok 1 - dup name\n"
        "  ---\n"
        "  location: '/repo/x.test.mjs:1:1'\n"
        "  ...\n"
        "not ok 2 - dup name\n"
        "  ---\n"
        "  location: '/repo/x.test.mjs:1:1'\n"
        "  ...\n"
    )
    ids = _node_tap_failing_tests(output, repo_path=Path("/repo"), work_dir=Path("/repo"))
    assert ids == ["x.test.mjs::dup name"]


def test_node_tap_failing_ids_empty_on_no_not_ok_line():
    from no_human.testing.runner import _node_tap_failing_tests
    assert _node_tap_failing_tests("ok 1 - all good\n# pass 1\n# fail 0\n") == []
    assert _node_tap_failing_tests("") == []


def test_failing_ids_prefers_pytest_and_falls_back_to_node_tap():
    from no_human.testing.runner import _failing_ids

    pytest_out = "FAILED tests/test_x.py::test_y - AssertionError: boom\n"
    assert _failing_ids(pytest_out) == ["tests/test_x.py::test_y"]

    node_out = (
        "not ok 1 - a node failure\n"
        "  ---\n"
        "  location: '/repo/x.test.mjs:1:1'\n"
        "  ...\n"
    )
    assert _failing_ids(
        node_out, repo_path=Path("/repo"), work_dir=Path("/repo"),
    ) == ["x.test.mjs::a node failure"]


def test_run_tests_populates_failing_tests_on_a_node_run(tmp_path):
    """End to end through the real `run_tests()`: a node TAP failure with a
    `location:` line must show up in `TestRunResult.failing_tests` — before
    this fix it was always `[]` for node, which made
    `_owned_failing_tests`/`ownership.owned_failing_ids` a permanent no-op for
    the whole ecosystem (round-3 review MAJOR)."""
    script = tmp_path / "fake_node_test.sh"
    test_file = tmp_path / "x.test.mjs"
    test_file.write_text("// pretend node test file\n")
    script.write_text(
        "#!/bin/sh\n"
        "cat <<TAPEOF\n"
        "TAP version 13\n"
        "not ok 1 - it fails\n"
        "  ---\n"
        f"  location: '{test_file}:3:1'\n"
        "  ...\n"
        "1..1\n"
        "# tests 1\n"
        "# pass 0\n"
        "# fail 1\n"
        "TAPEOF\n"
        "exit 1\n"
    )
    script.chmod(0o755)

    result = run_tests(tmp_path, str(script))

    assert result.failing_tests == ["x.test.mjs::it fails"], result.failing_tests


def test_dist_enoent_rule_has_a_word_boundary():
    """The bare `dist` alternative in the ENOENT rules must not match a
    substring of an unrelated name (`redistribute.json`) while still matching
    the real build-output directory (`web/dist/assets`) — round-3 review
    MINOR."""
    from no_human.testing.runner import missing_prerequisite_reason

    false_positive = (
        "Error: ENOENT: no such file or directory, open "
        "'/repo/config/redistribute.json'\n"
    )
    assert missing_prerequisite_reason(false_positive) is None

    true_positive = (
        "Error: ENOENT: no such file or directory, open "
        "'/repo/web/dist/assets/index.js'\n"
    )
    assert missing_prerequisite_reason(true_positive) is not None
