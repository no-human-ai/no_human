"""Suite-wide fixtures (EH1): no test eats a real production backoff.

The fast suite claimed ~50s and took ~12 minutes; part of that was real
sleeps leaking out of retry paths (a 30s PR-open retry pause, 120s CI infra
backoffs) whenever a test tripped them. Production delays are class/module
constants precisely so this file can zero them for every test — a test that
WANTS to observe a delay can set it back explicitly.
"""

import os
import shlex
import shutil
import sys

import pytest

from no_human.updates import DISABLE_ENV_VAR

# Fail-CLOSED, not detect-after. `no_human.testing.pytest_isolated_home` is
# meant to redirect HOME before `no_human.config` can bind DB_PATH/CONFIG_PATH
# to the operator's real one, but its usual registration path (the pytest11
# entry point in pyproject.toml) is skipped under
# `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` or by a stale editable-install dist-info
# (needs `uv sync` after this repo's pyproject.toml changes) — and in either
# case nothing aborts the run; `test_plugin_is_registered` only *detects* the
# gap, and only once its turn comes up, by which point earlier test modules
# may already have imported `no_human.config` against the real HOME. This
# conftest is collected before ANY test module, so importing the plugin here
# — and refusing to proceed if it did not activate — is the earliest and most
# reliable point to stop the suite before that can happen. The import itself
# is also a second, independent activation path: if the entry point never
# fired, this import is the plugin's first import and its module-level
# `_activate()` runs right here, before any test module's `import
# no_human.config`.
import no_human.testing.pytest_isolated_home as _isolated_home

if _isolated_home.ISOLATED_HOME is None:
    raise RuntimeError(
        "no_human.testing.pytest_isolated_home did not activate for this "
        "suite: HOME was not redirected before no_human.config could bind "
        "DB_PATH/CONFIG_PATH to it, so running further risks writing into "
        "the operator's real ~/.no_human. Likely causes: this checkout is "
        "not discoverable from the current working directory "
        "(no_human.testing.pytest_isolated_home._repo_root_or_none() "
        "returned None), or NH_TEST_HOME_ISOLATION=0 was set on purpose. "
        "Fix the cause or run from the repo root; do not bypass this check."
    )

# Hermetic: the update check must never depend on what PyPI holds. When
# 0.1.3 published while branches still carried 0.1.2, the notice appended
# after the JSON body of `nh status --json` and failed 8 tests across four
# modules. Module scope (not an autouse fixture) is load-bearing: it also
# covers tests that spawn `nh` as a subprocess, which inherits os.environ.
# Imported by name rather than hardcoding the string so a rename cannot
# silently un-guard the suite. tests/test_updates.py — the module that tests
# the check itself — undoes this locally via its own autouse fixture.
os.environ[DISABLE_ENV_VAR] = "1"


@pytest.fixture(autouse=True)
def _no_agent_mark(monkeypatch):
    """Every test starts UNMARKED, regardless of what launched the suite
    process itself (`session_mark.py`: a `no_human` coding backend stamps
    its own subprocesses, and this repo's own dev/CI loop can itself run
    inside one). Without this, a test asserting the "unmarked" behaviour of
    `refuse_if_marked`/`request_is_marked` would pass or fail depending on
    ambient state the test never controls — the same hazard `DISABLE_ENV_VAR`
    above exists to close for the update check. Tests that want the "marked"
    branch re-set the two vars explicitly via `monkeypatch.setenv`."""
    from no_human.agent.session_mark import (
        NO_HUMAN_AGENT_SESSION, NO_HUMAN_AGENT_SESSION_KIND,
    )
    monkeypatch.delenv(NO_HUMAN_AGENT_SESSION, raising=False)
    monkeypatch.delenv(NO_HUMAN_AGENT_SESSION_KIND, raising=False)


@pytest.fixture(autouse=True)
def _no_real_backoffs(monkeypatch):
    from no_human.core.orchestrator import Orchestrator
    monkeypatch.setattr(Orchestrator, "PR_OPEN_RETRY_DELAY", 0)
    # CI infra backoffs (module constants, 120s each — the project's 2-minute
    # infra-retry rule; the tests that exercise retries patch sleep themselves,
    # but one unpatched path used to cost 2 real minutes).
    import no_human.ci.gitlab as _gl
    import no_human.ci.jenkins as _jk
    monkeypatch.setattr(_gl, "_INFRA_BACKOFF_SECONDS", 0)
    monkeypatch.setattr(_jk, "_INFRA_BACKOFF_SECONDS", 0)


@pytest.fixture
def mock_ambient_probes(monkeypatch):
    """Opt-in hermetic seam for the SCRUM-81 ambient-auth probes
    (no_human/integrations/__init__.py). NOT autouse — the tamper guard
    correctly rejects an autouse conftest fixture that monkeypatches, since a
    conftest that does so is the single highest-leverage way to make a whole
    suite lie and no reviewer can easily tell hermetic mocking from faking the
    system-under-test green. Request this BY NAME in any test that reaches
    `list_integrations_with_ambient` / `GET /api/integrations` /
    `POST /api/integrations/{name}/test`, so it never transitively shells out
    to a real `gh`/`git credential fill` — proven live: an instrumented PATH
    shim recorded 5 real `gh auth status` invocations from tests that didn't
    mock this. Defaults both probes to False (mirrors "CLI not
    authenticated"/never-ambient) and gives the test a fresh cache; override
    `reg._AMBIENT_PROBES[name]` afterward (via the same `monkeypatch`
    fixture) for the ambient=True path."""
    from no_human import integrations as reg
    monkeypatch.setitem(reg._AMBIENT_PROBES, "github", lambda: False)
    monkeypatch.setitem(reg._AMBIENT_PROBES, "gitlab", lambda: False)
    monkeypatch.setattr(reg, "_AMBIENT_CACHE", {})
    return reg


@pytest.fixture
def isolated_env_file(tmp_path, monkeypatch):
    """Point ``config.ENV_PATH`` at an empty temp file for this test.

    Stubbing the transport does NOT stop a test reading the operator's real
    ``~/.no_human/.env``. `config.load_env_var` reads the FILE FIRST and only
    then falls back to the process env (".env wins over an inherited token: it
    is the curated source", config.py), and the call sites reach it before any
    seam a test can patch — `gitlab._subprocess_run` calls
    `_load_gitlab_token()` on entry; `SlackWorker.__init__` resolves
    `SLACK_BOT_TOKEN`/`SLACK_APP_TOKEN` before it constructs a client;
    `jenkins`/`jenkins_session` resolve `JENKINS_USER`, `JENKINS_API_TOKEN`,
    `SSO_USERNAME`, `SSO_PASSWORD`; `api/app.py` and the intake CLI verbs
    resolve `JIRA_API_TOKEN` / `LINEAR_API_KEY` / `MONDAY_API_TOKEN`. Whatever
    it finds it also EXPORTS into `os.environ`.

    This is not hygiene alone. `monkeypatch.setenv` LOSES to a populated file,
    so on an operator's machine that has configured Slack the suite reads
    their token instead of the test's and asserts `'xoxb-…' == 'B1'` — the
    tests are green only while the operator happens to have no such key on
    disk. Request this wherever a test can reach `config._read_env_file`, so
    the suite's verdict stops depending on the operator's `.env`. Read the last
    paragraph before reading that as "on nothing outside the repo": HOME is a
    bigger hole than `.env` and this fixture does not close it.

    Requested BY NAME — as a test argument, or `pytestmark =
    pytest.mark.usefixtures("isolated_env_file")` at the top of a module where
    every test can reach one of those call sites — and NEVER autouse.
    `tamper_guard.count_faking_fixtures` is `min(autouse markers, faking-patch
    calls)` per file, and it is right to score that shape: an autouse fixture
    that monkeypatches is the single highest-leverage way to make a suite lie.

    WHAT THE SPELLING COSTS, MEASURED — an earlier draft of this paragraph
    cited a number that cannot be reproduced. It said the requesting modules
    "score 0 and would score 1", which is arithmetically true and is not
    evidence of anything: a module carrying no autouse marker scores 0 whatever
    else it does, because `count_faking_fixtures` returns before it counts
    anything if its autouse search does not match. tests/test_cli_commands.py
    holds 67 faking-patch calls and scores 0, on main and here alike. The
    reproducible figures come from `runner.tamper_check_between(repo,
    "c0daad33", <tip>)` over the whole tree, and they are:

      · as written here — fake-fixtures 15 -> 15, clean, and no autouse reason
        in the report;
      · the identical isolation re-spelled as an autouse wrapper in the same
        six modules — fake-fixtures 15 -> 21, TAMPERED, six named reasons, one
        per module.

    Per file, tests/conftest.py scores 2 both before and after: its two
    long-standing autouse fixtures, which this change neither adds to nor
    disturbs. So the spelling IS load-bearing, and what makes it so is the +6
    the other spelling would add — not the 0 those modules already had.

    WHAT THIS DOES NOT PIN. It stops the suite READING the operator's file; it
    does not exercise the precedence rule quoted at the top. For the Slack,
    Jenkins and intake keys `load_env_var` lets a populated `.env` overwrite an
    inherited value, and no test drives that conflict:
    tests/test_linear_issues_endpoint.py and tests/test_jira_issues_endpoint.py
    populate a temp `.env` with the process env deliberately EMPTY, which pins
    "the file is read", not "the file wins". The one place precedence is pinned
    runs the OPPOSITE way and covers one key — the GITLAB_TOKEN test in
    tests/test_ci.py puts a stale token in a populated temp `.env` against an
    exported live one and requires the shell to win, which is
    `_load_gitlab_token`'s early return rather than `load_env_var`'s rule. This
    fixture returns `env_path` precisely so a test that wants the conflict can
    write to it. None does yet.

    THE GAP THIS FIXTURE WAS NOT BIG ENOUGH TO CLOSE, AND WHAT CLOSED IT.
    `config.ENV_PATH` can be redirected at all only because `load_env_var`
    resolves it at CALL time. `DB_PATH` and `CONFIG_PATH` are bound at IMPORT
    time from `NO_HUMAN_HOME = Path.home() / ".no_human"` (config.py) — no
    per-test fixture can un-bind an import-time value, because a fixture body
    only runs after collection has already imported every test module (and
    transitively `no_human.config`). That gap is what let a branch carrying a
    migration replay it into the operator's live database on 2026-08-10 — not
    a hypothetical.

    It is now closed by `no_human.testing.pytest_isolated_home`, a
    session-scoped pytest plugin registered via the `pytest11` entry point
    (`pyproject.toml`, `[project.entry-points.pytest11]` — requires `uv sync`
    to refresh the editable install's dist-info after this file changed). It
    sets `HOME` to a fresh temp directory before `no_human.config` is
    importable at all — the earliest hook pytest offers, and earlier than any
    fixture can run — so `DB_PATH`/`CONFIG_PATH` bind under the temp HOME
    from the first import, never under the operator's real one. This is a
    guard, not a procedure: `tests/test_home_isolation.py` proves it,
    including a subprocess control showing the same assertion FAILS when the
    plugin is blocked.
    """
    import no_human.config as nh_config

    env_path = tmp_path / "isolated.env"
    env_path.write_text("")
    env_path.chmod(0o600)
    monkeypatch.setattr(nh_config, "ENV_PATH", env_path)
    return env_path


class _HermeticUtilityBackend:
    """Stands in for every ClaudeBackend the ORCHESTRATOR constructs itself
    (utility eval, distillation, supervisor LLM, planners). Those calls are
    advisory by design — a junk answer degrades a hint, never a verdict — so
    an empty deterministic result is a legal outcome of each one.

    Why: the suite was spawning REAL claude-haiku subprocesses (found live
    during the 2026-07-17 overnight gate: chunk2 blocked minutes on one under
    subscription saturation). Real calls burn quota, are nondeterministic
    (live intake enrichment expanded acceptance_criteria mid-test), and hang
    the gate exactly when the bench saturates the subscription.
    """

    def __init__(self, *args, **kwargs):
        self.model = kwargs.get("model", "hermetic-stub")

    async def run(self, prompt, **kwargs):
        from no_human.agent.claude_backend import AgentResult
        return AgentResult(
            final_text="", num_turns=1, is_error=False, tokens_used=0,
            session_id="hermetic", stop_reason="end_turn",
        )


@pytest.fixture(autouse=True)
def _hermetic_sdk(request, monkeypatch):
    """No test reaches the real Claude API unless it says so explicitly
    (NH_TESTS_LIVE_SDK=1, or the `real_backend` marker for tests that
    exercise the REAL ClaudeBackend class over a mocked SDK client — e.g.
    the stream-accounting tests). Tests that exercise the stubbed paths
    inject their own fakes at closer seams (SupervisorHook(llm_call=...),
    reviewer backends, planner mocks) — this catches what nothing stubbed."""
    import os
    if os.environ.get("NH_TESTS_LIVE_SDK") == "1":
        yield
        return
    if request.node.get_closest_marker("real_backend"):
        yield
        return
    # THE SOURCE MODULE first: every lazy `from ..agent.claude_backend import
    # ClaudeBackend` executed at CALL time (intake/evaluator.py:121+187,
    # review/reviewer.py:916, api/app.py:63) resolves against this attribute.
    # Review of PR #105 (round 1) proved the orchestrator alias alone left the
    # intake evaluator LIVE: 33 real haiku subprocesses under a green suite.
    monkeypatch.setattr(
        "no_human.agent.claude_backend.ClaudeBackend", _HermeticUtilityBackend)
    # Names bound at IMPORT time don't follow the source module — patch each.
    monkeypatch.setattr(
        "no_human.core.orchestrator.ClaudeBackend", _HermeticUtilityBackend)
    monkeypatch.setattr(
        "no_human.cli.commands.ClaudeBackend", _HermeticUtilityBackend)
    yield


@pytest.fixture
def own_pytest_on_path(tmp_path, monkeypatch):
    """Give a `bare_repo`-shaped subprocess a `pytest` it can resolve on PATH.

    `tests/test_e2e_orchestrator.py`'s `bare_repo` fixture ships no `uv.lock`
    and no `.venv`, so `detect_command` (runner.py:177) returns bare
    `"pytest -q"`, `_venv_bin` returns None, and `_env_for` (runner.py:763)
    passes the PARENT process's PATH through to a `shell=True` `/bin/sh`. In
    the normal dev/CI loop that parent is `uv run pytest`, which happens to
    put a `pytest` console script on PATH — so three tests that assert a
    GREEN pipeline outcome (test_flaky_rerun_attribution.py,
    test_holdout_gate.py, test_owned_test_attribution.py) silently depend on
    the launcher's ambient PATH rather than anything they set up themselves
    (see CONTRIBUTING.md 104-118). Run bare, e.g. under a plain
    `.venv/bin/python -m pytest` with no `pytest` script on PATH, all three
    fail on `/bin/sh: pytest: command not found` surfacing as two distinct
    production shapes:

      1. holdout — `run_held_out_tests` (runner.py:1158) hardcodes
         `f"pytest -q {held_path}"` with no retry or fallback -> the held-out
         suite is reported FAIL: "0 passed, 0 failed, 0 errors" -> ESCALATED.
      2. flaky/owned — the main run is rescued by the rc=127 invocation retry
         (runner.py:1014), but the flaky re-run's substituted command is then
         discarded by orchestrator.py:10702 (pinned by
         test_a_substituted_command_is_not_the_verdict_we_asked_for) -> no
         excuse lands -> ESCALATED.

    This fixture supplies RESOLUTION, not a stub: it writes a real shim that
    execs `sys.executable -m pytest`, so the subprocess still runs the actual
    test suite — it just no longer depends on what launched the outer suite.
    Named, requested by argument, and deliberately NOT autouse: an autouse
    fixture that monkeypatches would raise this file's
    `tamper_guard.count_faking_fixtures` score (see `isolated_env_file`
    above for the same doctrine spelled out in full).
    """
    bin_dir = tmp_path / "_pytest_shim_bin"
    bin_dir.mkdir()

    posix_shim = bin_dir / "pytest"
    posix_shim.write_text(
        f"#!/bin/sh\nexec {shlex.quote(sys.executable)} -m pytest \"$@\"\n"
    )
    posix_shim.chmod(0o755)

    # Written unconditionally (cheap) so both platforms' fixture bodies stay
    # identical rather than branching on os.name.
    windows_shim = bin_dir / "pytest.bat"
    windows_shim.write_text(f'@echo off\r\n"{sys.executable}" -m pytest %*\r\n')

    monkeypatch.setenv(
        "PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"
    )

    resolved = shutil.which("pytest")
    assert resolved and str(bin_dir) in resolved, (
        "own_pytest_on_path did not make itself the resolved `pytest` — a "
        f"silent no-op here would re-hide the bug: shutil.which -> {resolved!r}"
    )
    return bin_dir


@pytest.fixture
async def store_factory(tmp_path):
    """Exception-safe Store lifecycle, owned in ONE place.

    Under `-n 4`, aiosqlite's non-daemon worker thread calls
    call_soon_threadsafe on the test loop when it closes. A store closed in a
    test BODY is skipped whenever that body raises, so the connection outlives
    the loop and the close lands on a CLOSED loop — poisoning whichever
    unrelated test the xdist worker runs next (the ui_evidence tests were the
    usual victims). Teardown here runs in the `finally` of a fixture, i.e.
    still on the test's own loop, on every exit path.

    Deliberately NOT autouse and it monkeypatches nothing: an autouse conftest
    fixture that patches is what `tamper_guard.count_faking_fixtures` scores,
    and this file's `isolated_env_file` docstring spells out why we don't add
    to that score.

    `name` is a filename under tmp_path, an absolute path, or ":memory:".
    """
    stores = []

    async def _make(name="nh.db"):
        from no_human.core.db import Store
        path = name if str(name) == ":memory:" else tmp_path / str(name)
        s = await Store(path).connect()
        stores.append(s)
        return s

    try:
        yield _make
    finally:
        for s in reversed(stores):
            await s.close()   # idempotent (core/db.py:539); joins the aiosqlite thread


@pytest.fixture
async def store(store_factory):
    return await store_factory()
