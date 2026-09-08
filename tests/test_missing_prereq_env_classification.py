"""A node/desktop test failure caused by a MISSING BUILD PREREQUISITE (an
uninstalled package, an unbuilt `web/dist`, a missing `node_modules`) is an
ENVIRONMENT error, not failed code: nothing about the diff was judged, so
retrying the coder just burns attempts.

Observed (task 82644133, verification-attempt-4.md): a desktop `npm test` run
with `app-builder-lib` never installed and `web/dist` never built showed 2 of
410 tests red and was judged "tests failed: FAIL: 408 passed, 2 failed, 0
errors" — then retried three times. Both failing tests fail CLOSED on
purpose (`desktop/packagedFiles.test.mjs` and the web/dist bundle check) and
must keep failing that way; only the VERDICT changes.

This file has two halves:
  * unit tests of `runner.missing_prerequisite_reason` — the single
    classifier, on exactly the two artifact texts plus negatives;
  * gate-level tests observing the orchestrator's produced verdict string —
    a stubbed red run carrying those texts must off-ramp as an environment
    error with no retry, while a real assertion failure must still fail the
    attempt exactly as it does today.
"""

import json as _json
from unittest.mock import patch

from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task, TaskStatus
from no_human.notify.slack import SlackNotifier
from no_human.testing import runner
from no_human.vcs import GitRepo

from .test_e2e_orchestrator import FakeBackend, _config, _git, bare_repo  # noqa: F401

# --------------------------------------------------------------------------- #
# Unit half: the classifier, on the exact artifact texts + negatives           #
# --------------------------------------------------------------------------- #


def test_app_builder_lib_module_text_classifies_as_environment():
    output = (
        "not ok 182 - the config exports the config and nothing else, or "
        "electron-builder refuses to build\n"
        "  ---\n"
        "  error: Cannot find module 'app-builder-lib/scheme.json'\n"
        "  Require stack:\n"
        "  - /repo/desktop/electron-builder.config.cjs\n"
        "  ---\n"
    )
    reason = runner.missing_prerequisite_reason(output)
    assert reason is not None
    assert "app-builder-lib/scheme.json" in reason


def test_web_dist_missing_text_classifies_as_environment():
    output = (
        "not ok 343 - the built bundle carries no landed-override strings\n"
        "  ---\n"
        "  error: web/dist/assets is missing - run `npm run build` in web/ first\n"
        "  ---\n"
    )
    reason = runner.missing_prerequisite_reason(output)
    assert reason is not None


def test_enoent_on_node_modules_classifies_as_environment():
    output = (
        "Error: ENOENT: no such file or directory, open "
        "'/Users/x/repo/desktop/node_modules/electron/package.json'\n"
        "    at Object.openSync (node:fs:596:3)\n"
    )
    reason = runner.missing_prerequisite_reason(output)
    assert reason is not None


def test_real_assertion_failure_is_not_environment():
    output = (
        "not ok 3 - the button renders disabled\n"
        "  ---\n"
        "  AssertionError: expected 2 to equal 3\n"
        "  ---\n"
        "FAILED test_calc.py::test_add - assert 1 == 2\n"
    )
    assert runner.missing_prerequisite_reason(output) is None


def test_deleted_relative_module_is_not_environment():
    # A coder-deleted file must stay failed code, never excused as
    # environment: `./updatePolicy.mjs` is relative, not a package.
    output = "Error [ERR_MODULE_NOT_FOUND]: Cannot find module './updatePolicy.mjs'\n"
    assert runner.missing_prerequisite_reason(output) is None


# --------------------------------------------------------------------------- #
# Gate half: the produced verdict string, and the no-retry off-ramp           #
# --------------------------------------------------------------------------- #

_ARTIFACT_OUTPUT = (
    "not ok 182 - the config exports the config and nothing else, or "
    "electron-builder refuses to build\n"
    "  ---\n"
    "  error: Cannot find module 'app-builder-lib/scheme.json'\n"
    "  ---\n"
    "not ok 343 - the built bundle carries no landed-override strings\n"
    "  ---\n"
    "  error: web/dist/assets is missing - run `npm run build` in web/ first\n"
    "  ---\n"
)

_ASSERTION_OUTPUT = (
    "not ok 3 - the button renders disabled\n"
    "  ---\n"
    "  AssertionError: expected 2 to equal 3\n"
    "  ---\n"
)


def _persisted(attempt):
    tr = attempt["test_results"]
    return _json.loads(tr) if isinstance(tr, str) else (tr or {})


def _mutate(cwd):
    (cwd / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n\n"
        "def mul(a, b):\n    return a * b\n"
    )
    (cwd / "test_calc.py").write_text(
        "from calc import add, mul\n\n"
        "def test_add():\n    assert add(1, 2) == 3\n\n"
        "def test_mul():\n    assert mul(2, 3) == 6\n"
    )


async def _run_attempt_with_stubbed_test_output(store, tmp_path, bare_repo, output):
    """Drives the real `_run_attempt` directly — the established pattern for
    observing a single attempt's own `TaskOutcome` (see
    `tests/test_infra_not_work.py::_run_one_attempt`) — instead of
    `run_task`, whose outer retry loop wraps any exhausted, still-failing
    attempt in a SEPARATE `max_attempts (N) reached...` escalation whose
    `detail` only guarantees the original text as a SUBSTRING, not a prefix
    (confirmed at `tests/test_e2e_orchestrator.py:4996-5009`:
    `assert detail in outcome.detail`). That wrapper is out of scope here;
    this file is about the verdict `_run_attempt` itself produces.
    """
    cfg = _config(tmp_path)
    events = []
    orch = Orchestrator(store, cfg.data, FakeBackend(_mutate), SlackNotifier(None),
                        event_sink=events.append)
    task = Task.new("desktop npm test", repo_path=str(bare_repo))
    await store.create_task(task)
    # `_run_attempt` transitions the task to IMPLEMENTING itself — only legal
    # from PLANNING (the main-flow spine, `core/task.py`) — so walk it there
    # the way `_drive` would have before reaching the attempt loop.
    await store.set_status(task, TaskStatus.CONTEXT)
    await store.set_status(task, TaskStatus.PLANNING)
    repo = GitRepo(bare_repo)

    tr = runner.TestRunResult(
        ran=True, ok=False, passed=408, failed=2, errors=0,
        command="npm test", output=output,
    )

    async def fake_run_tests_once(repo, cmd, cwd=None):
        return tr, False

    with patch.object(orch, "_run_tests_once", fake_run_tests_once):
        outcome = await orch._run_attempt(task, repo, 1, "main")

    attempts = await store.list_attempts(task.id)
    return outcome, attempts, events


async def test_missing_prerequisite_run_reports_tests_could_not_run_and_never_retries(
    bare_repo, tmp_path, store,
):
    outcome, attempts, _events = await _run_attempt_with_stubbed_test_output(
        store, tmp_path, bare_repo, _ARTIFACT_OUTPUT)

    assert outcome.detail.startswith("tests could not run:"), outcome.detail
    assert outcome.off_ramp is True
    assert outcome.status is not TaskStatus.FAILED

    # Never a retry: exactly one attempt row exists.
    assert len(attempts) == 1, [a.get("failure_reason") for a in attempts]
    row = attempts[0]
    assert row["failure_reason"].startswith("tests could not run:")
    assert not row["failure_reason"].startswith("tests failed")
    assert _persisted(row)["environment_error"] is True


async def test_real_assertion_failure_still_fails_the_attempt(bare_repo, tmp_path, store):
    # Today's plain-fail behaviour on a single attempt is what's under test,
    # not the retry loop around it (out of scope, unchanged, covered
    # elsewhere by tests/test_flaky_rerun_attribution.py and friends).
    outcome, attempts, _events = await _run_attempt_with_stubbed_test_output(
        store, tmp_path, bare_repo, _ASSERTION_OUTPUT)

    assert outcome.detail.startswith("tests failed:"), outcome.detail
    assert outcome.status is TaskStatus.FAILED
    failed_rows = [a for a in attempts if a.get("status") == "failed"]
    assert failed_rows
    assert failed_rows[-1]["failure_reason"].startswith("tests failed:")
