"""A node/desktop test failure caused by a MISSING BUILD PREREQUISITE (an
uninstalled package, an unbuilt `web/dist`, a missing `node_modules`) is an
ENVIRONMENT error, not failed code: nothing about the diff was judged, so
retrying the coder just burns attempts.

Observed (task 82644133, verification-attempt-4.md): a desktop `npm test` run
with `app-builder-lib` never installed and `web/dist` never built showed 2 of
417 tests red (at TAP ordinals 182 and 343) and was judged "tests failed:
FAIL: 415 passed, 2 failed, 0 errors" — then retried three times. Both
failing tests fail CLOSED on purpose (`desktop/packagedFiles.test.mjs` and
the web/dist bundle check) and must keep failing that way; only the VERDICT
changes.

Round-2 review found the round-1 fix classified from `TestRunResult.output`,
which is only the LAST 8000 BYTES of a run's output (see `runner.run_tests`).
In the real incident the two `not ok` blocks sit tens of kilobytes from the
end of a 417-test TAP dump, so the round-1 classifier never actually fired on
the real incident — it only worked on hand-shrunk fixtures. This file's
`_incident_tap()` reconstructs a faithful, full-size TAP dump so the tests
here exercise the SAME truncation the real run hits; the classifier now reads
`TestRunResult.failure_blocks` (parsed off the untruncated output — see
`runner._tap_failure_blocks` / `runner.prerequisite_reason_for`), never
`.output`.

This file has three halves:
  * unit tests of `runner.missing_prerequisite_reason` / `prerequisite_reason_for`
    / `_tap_failure_blocks` — the classifier plumbing, plus negatives;
  * a `runner.run_tests()` integration test proving the failing content
    survives the `[-8000:]` truncation (round-2 BLOCKER-1);
  * gate-level tests observing the orchestrator's produced verdict — both the
    single-run AND the layered call sites (round-2 MAJOR-5), a real assertion
    failure, a Python `ModuleNotFoundError` (round-2 MAJOR-6, deliberately NOT
    added as a rule), and a coder-OWNED failing test id, which must never be
    excused no matter what its own text says (round-2 MAJOR-4).
"""

import contextlib
import json as _json
from pathlib import Path
from unittest.mock import patch

from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task, TaskStatus
from no_human.notify.slack import SlackNotifier
from no_human.testing import runner
from no_human.vcs import GitRepo

from .test_e2e_orchestrator import FakeBackend, _config, _git, bare_repo  # noqa: F401

# --------------------------------------------------------------------------- #
# A faithful, full-size incident reconstruction                               #
# --------------------------------------------------------------------------- #

_PAD = (
    "keeps its own invariant green after every refactor lands and stays "
    "boringly fast on every worker, every single time it runs in CI"
)


def _ok_block(n: int) -> str:
    return f"ok {n} - subtest {n} {_PAD}\n"


def _not_ok_cannot_find_module(n: int) -> str:
    return (
        f"not ok {n} - the config exports the config and nothing else, or "
        "electron-builder refuses to build\n"
        "  ---\n"
        "  error: Cannot find module 'app-builder-lib/scheme.json'\n"
        "  Require stack:\n"
        "  - /repo/desktop/electron-builder.config.cjs\n"
        "  ---\n"
    )


def _not_ok_web_dist_missing(n: int) -> str:
    # Real incident bytes: an EM DASH (e2 80 94), not a hyphen.
    return (
        f"not ok {n} - the built bundle carries no landed-override strings\n"
        "  ---\n"
        "  error: web/dist/assets is missing — run `npm run build` in web/ first\n"
        "  ---\n"
    )


def _incident_tap(total: int = 417, first: int = 182, second: int = 343) -> str:
    """A faithful reconstruction of the task-82644133 incident: `total` node
    TAP tests, with `not ok` blocks at ordinals `first` (bare-module) and
    `second` (build-artefact, em dash) — padded long enough that BOTH sit
    outside the last 8000 bytes of the full dump, exactly like the real run.
    """
    lines = []
    for n in range(1, total + 1):
        if n == first:
            lines.append(_not_ok_cannot_find_module(n))
        elif n == second:
            lines.append(_not_ok_web_dist_missing(n))
        else:
            lines.append(_ok_block(n))
    lines.append(f"1..{total}\n")
    lines.append(f"# tests {total}\n")
    lines.append(f"# pass {total - 2}\n")
    lines.append("# fail 2\n")
    return "".join(lines)


def test_the_incident_signatures_are_outside_the_eight_kilobyte_tail():
    tap = _incident_tap()
    encoded = tap.encode("utf-8")
    for needle in (b"Cannot find module", "web/dist/assets is missing".encode()):
        idx = encoded.index(needle)
        distance_from_end = len(encoded) - idx
        assert distance_from_end > 8000, (needle, distance_from_end)
    # The tail alone — what `TestRunResult.output` actually keeps — carries
    # NEITHER signature. This is the exact truncation the real incident hit.
    tail = encoded[-8000:].decode("utf-8", errors="replace")
    assert "Cannot find module" not in tail
    assert "web/dist/assets is missing" not in tail


# --------------------------------------------------------------------------- #
# Unit half: the classifier plumbing, plus negatives                          #
# --------------------------------------------------------------------------- #


def test_app_builder_lib_module_text_classifies_as_environment():
    reason = runner.missing_prerequisite_reason(_not_ok_cannot_find_module(182))
    assert reason is not None
    assert "app-builder-lib/scheme.json" in reason


def test_web_dist_missing_text_with_ascii_hyphen_still_classifies():
    # The widened rule keeps matching a plain hyphen too — only the em-dash
    # test below pins the REAL incident bytes.
    output = (
        "not ok 343 - the built bundle carries no landed-override strings\n"
        "  ---\n"
        "  error: web/dist/assets is missing - run `npm run build` in web/ first\n"
        "  ---\n"
    )
    assert runner.missing_prerequisite_reason(output) is not None


def test_web_dist_missing_text_pins_the_real_em_dash_bytes():
    block = _not_ok_web_dist_missing(343)
    encoded = block.encode("utf-8")
    assert b"\xe2\x80\x94" in encoded, "real incident text uses an em dash, not a hyphen"
    assert b" - run `npm run build`" not in encoded
    reason = runner.missing_prerequisite_reason(block)
    assert reason is not None


def test_enoent_on_node_modules_classifies_as_environment():
    output = (
        "Error: ENOENT: no such file or directory, open "
        "'/Users/x/repo/desktop/node_modules/electron/package.json'\n"
        "    at Object.openSync (node:fs:596:3)\n"
    )
    reason = runner.missing_prerequisite_reason(output)
    assert reason is not None


def test_enoent_path_before_errno_classifies_as_environment():
    # spawnSync's error names the PATH before ENOENT, not after — the
    # opposite order from the case above; half-covered before this fix.
    output = (
        "Error: spawnSync /repo/desktop/node_modules/.bin/electron-builder ENOENT\n"
        "    at Object.spawnSync (node:internal/child_process:...)\n"
    )
    reason = runner.missing_prerequisite_reason(output)
    assert reason is not None
    assert "node_modules/.bin/electron-builder" in reason


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


def test_python_module_not_found_error_is_not_environment():
    # Round-2 MAJOR-6: a bare Python import gap is owned by the base-tree
    # gate (`_invocation_error_reproduces_on_base`), never excused here — and
    # this file must NOT add a "No module named" rule to make it match.
    output = "ModuleNotFoundError: No module named 'requests'\n"
    assert runner.missing_prerequisite_reason(output) is None


def test_prerequisite_reason_ignores_a_mixed_output_tail():
    # Round-2 BLOCKER-1 fix, unit-scoped: a signature sitting in PASSING
    # output must not excuse a real failure elsewhere in the same run.
    # `prerequisite_reason_for` only ever looks at `failure_blocks` /
    # `traceback_excerpts` — never `.output`.
    output = (
        "ok 5 - build cache path resolves via node_modules/dist even when "
        "ENOENT would be a red herring in this sentence\n"
        "not ok 6 - the button renders disabled\n"
        "  ---\n"
        "  AssertionError: expected 2 to equal 3\n"
        "  ---\n"
    )
    blocks = runner._tap_failure_blocks(output)
    assert blocks and "AssertionError" in blocks[0]
    tr = runner.TestRunResult(
        ran=True, ok=False, passed=1, failed=1, errors=0,
        command="node --test", output=output, failure_blocks=blocks,
    )
    assert runner.prerequisite_reason_for(tr) is None


def test_run_tests_carries_the_failing_blocks_past_truncation(monkeypatch, tmp_path):
    # Round-2 BLOCKER-1, end to end through the real `runner.run_tests()`:
    # the full incident-size TAP is fed through `_run_shell`, and the
    # classifier must still find the signature even though `.output` itself
    # is truncated to the last 8000 bytes.
    import no_human.testing.runner as runner_mod

    tap = _incident_tap()

    def fake_run_shell(cmd, work_dir, timeout, env):
        return 1, tap, False

    monkeypatch.setattr(runner_mod, "_run_shell", fake_run_shell)
    result = runner_mod.run_tests(tmp_path, "node --test", timeout=5)

    assert len(result.output.encode("utf-8")) <= 8000
    assert "Cannot find module" not in result.output, "the tail must not carry the signature"
    assert result.failure_blocks, "the untruncated TAP blocks must survive"
    reason = runner_mod.prerequisite_reason_for(result)
    assert reason is not None, "the classifier must find the signature via failure_blocks"


# --------------------------------------------------------------------------- #
# Gate half: the produced verdict, both call sites, and the never-excused rule #
# --------------------------------------------------------------------------- #


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


_ASSERTION_OUTPUT = (
    "not ok 3 - the button renders disabled\n"
    "  ---\n"
    "  AssertionError: expected 2 to equal 3\n"
    "  ---\n"
)


async def _run_attempt_with_stubbed_test_output(
    store, tmp_path, bare_repo, output, *, failing_tests=None, owned=None,
):
    """Drives the real `_run_attempt` directly — the established pattern for
    observing a single attempt's own `TaskOutcome` (see
    `tests/test_infra_not_work.py::_run_one_attempt`) — instead of
    `run_task`, whose outer retry loop wraps any exhausted, still-failing
    attempt in a SEPARATE `max_attempts (N) reached...` escalation whose
    `detail` only guarantees the original text as a SUBSTRING, not a prefix
    (confirmed at `tests/test_e2e_orchestrator.py:4996-5009`:
    `assert detail in outcome.detail`). That wrapper is out of scope here;
    this file is about the verdict `_run_attempt` itself produces.

    `failure_blocks` is derived from *output* via the real `_tap_failure_blocks`
    parser — matching what `runner.run_tests()` itself would have produced —
    since `_environment_test_failure` reads `.failure_blocks`, never `.output`
    (round-2 BLOCKER-1). `owned`, when given, stubs `_owned_failing_tests` so
    a test can prove a coder-owned failing id is never excused (round-2
    MAJOR-4) without needing a real git diff.
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
        failing_tests=list(failing_tests or []),
        failure_blocks=runner._tap_failure_blocks(output),
    )

    async def fake_run_tests_once(repo, cmd, cwd=None):
        return tr, False

    async def fake_owned_failing_tests(repo, base, failing, *, cwd=None):
        return list(owned)

    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.object(orch, "_run_tests_once", fake_run_tests_once))
        if owned is not None:
            stack.enter_context(
                patch.object(orch, "_owned_failing_tests", fake_owned_failing_tests))
        outcome = await orch._run_attempt(task, repo, 1, "main")

    attempts = await store.list_attempts(task.id)
    return outcome, attempts, events


async def test_missing_prerequisite_run_reports_tests_could_not_run_and_never_retries(
    bare_repo, tmp_path, store,
):
    outcome, attempts, _events = await _run_attempt_with_stubbed_test_output(
        store, tmp_path, bare_repo, _incident_tap())

    assert outcome.detail.startswith("tests could not run:"), outcome.detail
    assert outcome.off_ramp is True
    assert outcome.status is not TaskStatus.FAILED

    # Never a retry: exactly one attempt row exists.
    assert len(attempts) == 1, [a.get("failure_reason") for a in attempts]
    row = attempts[0]
    assert row["failure_reason"].startswith("tests could not run:")
    assert not row["failure_reason"].startswith("tests failed")
    assert row["status"] == "failed", row["status"]
    assert row["infra_failure"] == 1
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


async def test_a_coder_owned_failure_is_never_excused(bare_repo, tmp_path, store):
    # Round-2 MAJOR-4: this attempt's OWN diff added/modified the failing
    # test id — even though its failure text contains a matched prerequisite
    # signature, it must fail the attempt exactly like a real bug, because
    # the classifier must never even be consulted for an owned id.
    outcome, attempts, _events = await _run_attempt_with_stubbed_test_output(
        store, tmp_path, bare_repo, _incident_tap(),
        failing_tests=["desktop/packagedFiles.test.mjs"],
        owned=["desktop/packagedFiles.test.mjs"],
    )

    assert not outcome.detail.startswith("tests could not run:"), outcome.detail
    assert outcome.status is TaskStatus.FAILED
    failed_rows = [a for a in attempts if a.get("status") == "failed"]
    assert failed_rows
    assert not failed_rows[-1]["failure_reason"].startswith("tests could not run:")
    assert _persisted(failed_rows[-1]).get("environment_error") is not True


async def test_layered_missing_prerequisite_never_retries(bare_repo, tmp_path, store):
    # Round-2 MAJOR-5: the LAYERED call site (`_layered_tests_failed_outcome`)
    # was untested before this fix — deleting it left the suite green. Drives
    # it directly via `_resolve_test_plan` + `run_test_plan`, mirroring
    # `tests/test_e2e_orchestrator.py::test_layered_test_plan_threads_source_repo`.
    from no_human.testing.plan_runner import LayerResult, PlanResult
    import no_human.testing.plan_runner as plan_runner_mod
    from no_human.testing.test_layers import Gating, TestLayer, TestPlan

    tap = _incident_tap()
    plan = TestPlan(layers=[
        TestLayer(name="desktop", command="node --test", gating=Gating.BLOCKING),
    ])
    tr = runner.TestRunResult(
        ran=True, ok=False, passed=415, failed=2, errors=0,
        command="node --test", output=tap[-8000:],
        failure_blocks=runner._tap_failure_blocks(tap),
    )
    lr = LayerResult(layer_name="desktop", gating=Gating.BLOCKING, result=tr)

    def fake_run_test_plan(test_plan, task_repo, **kwargs):
        return PlanResult(layer_results=[lr])

    async def fake_resolve_test_plan(task):
        return plan

    cfg = _config(tmp_path)
    events = []
    orch = Orchestrator(store, cfg.data, FakeBackend(_mutate), SlackNotifier(None),
                        event_sink=events.append)
    task = Task.new("desktop npm test", repo_path=str(bare_repo))
    await store.create_task(task)
    await store.set_status(task, TaskStatus.CONTEXT)
    await store.set_status(task, TaskStatus.PLANNING)
    repo = GitRepo(bare_repo)

    with patch.object(orch, "_resolve_test_plan", fake_resolve_test_plan), \
         patch.object(plan_runner_mod, "run_test_plan", fake_run_test_plan):
        outcome = await orch._run_attempt(task, repo, 1, "main")

    assert outcome.detail.startswith("tests could not run:"), outcome.detail
    assert outcome.off_ramp is True

    attempts = await store.list_attempts(task.id)
    assert len(attempts) == 1, [a.get("failure_reason") for a in attempts]
    row = attempts[0]
    assert row["failure_reason"].startswith("tests could not run:")
    assert row["status"] == "failed", row["status"]
    assert row["infra_failure"] == 1
    assert _persisted(row)["environment_error"] is True
