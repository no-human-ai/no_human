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
import shlex
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


def _not_ok_cannot_find_module_owned(
    n: int, *, name: str = "it fails", location: str = "x.test.mjs:3:1",
) -> str:
    """Same incident text as `_not_ok_cannot_find_module` (still matches both
    `runner._INVOCATION_ERROR_PATTERNS` and `runner.missing_prerequisite_
    reason`) plus a node `location:` line — the real `--test-reporter=tap`
    shape `runner._node_tap_failing_tests` (src/no_human/testing/runner.py:508-512)
    needs to resolve a file-scoped, OWNABLE id (`x.test.mjs::it fails`)
    instead of the bare, always-unownable name the plain incident block
    produces (round-4 review MAJOR-1)."""
    return (
        f"not ok {n} - {name}\n"
        "  ---\n"
        "  error: Cannot find module 'app-builder-lib/scheme.json'\n"
        "  Require stack:\n"
        "  - /repo/desktop/electron-builder.config.cjs\n"
        f"  location: '{location}'\n"
        "  ---\n"
    )


def _incident_tap_owned(total: int = 417, first: int = 182, second: int = 343) -> str:
    """`_incident_tap`, but the `first` block is `_not_ok_cannot_find_module_
    owned` instead of the bare-name variant — everything else (padding,
    ordinals, the second em-dash block) is identical. Round-4 review
    MAJOR-1: this is what lets a test drive the REAL runner end to end and
    still get a file-scoped id ownership can attribute, so the ownership-
    before-base-tree-check fix can be proven without stubbing
    `_owned_failing_tests`.
    """
    lines = []
    for n in range(1, total + 1):
        if n == first:
            lines.append(_not_ok_cannot_find_module_owned(n))
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
    # round-4 review MINOR-4: name the status, not just what it isn't —
    # `escalate_now=True` in `_environment_test_failure`'s `_raise_blocker`
    # call forces exactly `TaskStatus.ESCALATED` (src/no_human/core/
    # orchestrator.py), so pin that instead of two "is not" negatives.
    assert outcome.status is TaskStatus.ESCALATED

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


async def test_layered_ownership_gate_bills_an_owned_failing_id(bare_repo, tmp_path, store):
    # Round-3 review MAJOR: the layered gate's OWNERSHIP check (orchestrator.py
    # `_layered_tests_failed_outcome`, the `_owned_failing_tests` call gated on
    # `runner.prerequisite_reason_for(fail_result) is not None`) had no test at
    # all — only its escalation sibling above did. Same incident TAP, same
    # prerequisite signature, but this time `failing_tests` names an id this
    # attempt's own diff added — it must never be excused as environment (or
    # pre-existing): it can only ever be BILLED, same as the single-run gate's
    # `test_..._never_excused` case already covers for that call site.
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
        failing_tests=["x.test.mjs::it fails"],
    )
    lr = LayerResult(layer_name="desktop", gating=Gating.BLOCKING, result=tr)

    def fake_run_test_plan(test_plan, task_repo, **kwargs):
        return PlanResult(layer_results=[lr])

    async def fake_resolve_test_plan(task):
        return plan

    def _mutate_node_test(cwd):
        (cwd / "x.test.mjs").write_text("// this attempt's own edit\n")

    cfg = _config(tmp_path)
    orch = Orchestrator(store, cfg.data, FakeBackend(_mutate_node_test), SlackNotifier(None))
    task = Task.new("desktop npm test", repo_path=str(bare_repo))
    await store.create_task(task)
    await store.set_status(task, TaskStatus.CONTEXT)
    await store.set_status(task, TaskStatus.PLANNING)
    repo = GitRepo(bare_repo)

    with patch.object(orch, "_resolve_test_plan", fake_resolve_test_plan), \
         patch.object(plan_runner_mod, "run_test_plan", fake_run_test_plan):
        outcome = await orch._run_attempt(task, repo, 1, "main")

    assert outcome.status is TaskStatus.FAILED, outcome.detail
    assert not outcome.detail.startswith("tests could not run:"), outcome.detail
    assert outcome.detail.startswith("tests failed:"), outcome.detail

    attempts = await store.list_attempts(task.id)
    assert len(attempts) == 1, [a.get("failure_reason") for a in attempts]
    row = attempts[0]
    assert row["status"] == "failed", row["status"]
    assert not row["failure_reason"].startswith("tests could not run:")
    assert row["infra_failure"] in (0, None, False), (
        "an id this attempt's own diff added must be BILLED, never waved "
        "through as environment, even though its text carries the same "
        "prerequisite signature the sibling escalation test above uses: "
        + str(row)
    )
    assert _persisted(row).get("environment_error") is not True


# --------------------------------------------------------------------------- #
# Round-3 review BLOCKER: real runner, not a hand-built TestRunResult         #
# --------------------------------------------------------------------------- #
#
# Every gate test above stubs `_run_tests_once` with a hand-built
# `runner.TestRunResult(...)` that never sets `invocation_error` — which is
# exactly why round 2's suite never caught the BLOCKER: the incident TAP's
# "Cannot find module" text ALSO matches `runner._INVOCATION_ERROR_PATTERNS`,
# so the REAL `runner.run_tests()` sets `invocation_error=True` on it. Before
# the fix, the single-run gate only consulted the prerequisite classifier
# `if not invocation_error`, so this exact incident skipped the classifier
# entirely and fell into the base-tree reproduction check instead — which (a
# fixed, diff-independent `cat` command reproduces identically on base)
# reported "genuinely environmental" and let the attempt SUCCEED with a PR
# opened. Worse than before: a SILENT pass on a build-prerequisite failure.
# These three tests drive the REAL runner end to end (a fake `test_cmd` that
# `cat`s the incident TAP and exits 1) to prove: (1) the escalation now wins
# regardless of `invocation_error`, (2) a real assertion failure with no
# prerequisite signature still takes the normal failed-attempt route, and
# (3) the reorder is load-bearing — disable the classifier and the old,
# wrong, silent-pass behaviour comes back.


def _orch(store, tmp_path, backend):
    cfg = _config(tmp_path)
    # One attempt is enough to prove the gate; retries would just repeat it.
    cfg.data["bounds"] = {"max_attempts": 1}
    return Orchestrator(store, cfg.data, backend, SlackNotifier(None))


async def test_incident_tap_escalates_as_environment_through_the_real_runner(
    bare_repo, tmp_path, store
):
    """The incident TAP is 417 TESTS (429 lines, 62,870 bytes, measured via
    `_incident_tap().splitlines()` / `.encode("utf-8")` — round-4 review
    MINOR-3: not "417 lines", that count is the test total, not the line
    count) — too large for an inline `printf` test_cmd (see `tests/
    test_base_tree_gate.py`'s tiny one), so it is written to a temp file and
    `cat`; the shell `test_cmd` runs with `shell=True` (`runner.
    _run_shell`), so a compound `cat <path>; exit 1` command works exactly
    like a real broken node test script would."""
    tap_path = tmp_path / "incident.tap"
    tap_path.write_text(_incident_tap())
    node_err_cmd = f"cat {shlex.quote(str(tap_path))}; exit 1"

    from no_human.profile import ProjectProfile
    prof = ProjectProfile(
        repo_path=str(bare_repo), ecosystem="node",
        test_cmd=node_err_cmd,
        derived_from=["test"], proven={"test_cmd": True}, confirmed=True,
    )
    await store.upsert_profile(prof)

    orch = _orch(store, tmp_path, FakeBackend(_mutate))
    t = Task.new("desktop npm test", repo_path=str(bare_repo))
    await store.create_task(t)

    outcome = await orch.run_task(t)

    # The classifier wins outright: TRANSIENT_INFRA blocker, never a PR, and
    # exactly one attempt row — never AWAITING_APPROVAL with a PR opened
    # (the old, buggy, silent-pass shape this test guards against).
    assert outcome.detail.startswith("tests could not run:"), outcome.detail
    assert outcome.off_ramp is True
    # round-4 review MINOR-4: name the status by value, same reasoning as
    # the stubbed-output sibling test above.
    assert outcome.status is TaskStatus.ESCALATED
    assert outcome.pr_url is None

    attempts = await store.list_attempts(t.id)
    assert len(attempts) == 1, [a.get("failure_reason") for a in attempts]
    row = attempts[0]
    assert row["failure_reason"].startswith("tests could not run:")
    assert row["status"] == "failed", row["status"]
    assert row["infra_failure"] == 1
    assert _persisted(row)["environment_error"] is True


async def test_assertion_only_tap_takes_the_normal_route(bare_repo, tmp_path, store):
    """Positive control for the fix above: a TAP with only a real assertion
    failure (no prerequisite signature anywhere in it) must still fail the
    attempt normally — the reorder must not turn EVERY red node run into an
    environment escalation, only ones the classifier actually matches."""
    node_assert_cmd = (
        "printf 'not ok 3 - the button renders disabled\\n"
        "  ---\\n"
        "  AssertionError: expected 2 to equal 3\\n"
        "  ---\\n"
        "1..3\\n# tests 3\\n# pass 2\\n# fail 1\\n'; exit 1"
    )
    from no_human.profile import ProjectProfile
    prof = ProjectProfile(
        repo_path=str(bare_repo), ecosystem="node",
        test_cmd=node_assert_cmd,
        derived_from=["test"], proven={"test_cmd": True}, confirmed=True,
    )
    await store.upsert_profile(prof)

    orch = _orch(store, tmp_path, FakeBackend(_mutate))
    t = Task.new("desktop npm test", repo_path=str(bare_repo))
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is not TaskStatus.AWAITING_APPROVAL
    assert outcome.pr_url is None
    attempts = await store.list_attempts(t.id)
    row = attempts[-1]
    assert row["status"] == "failed", row["status"]
    assert not (row["failure_reason"] or "").startswith("tests could not run:"), row
    assert _persisted(row).get("environment_error") is not True


async def test_prerequisite_gate_is_load_bearing(bare_repo, tmp_path, store, monkeypatch):
    """Mutation test: with `runner.prerequisite_reason_for` disabled (always
    None, as if round 3's reorder fix were reverted or the classifier never
    fired), the exact same incident-TAP scenario as the first test above must
    fall through to the OLD path — the base-tree reproduction check — and
    reach the old, wrong, silent-pass verdict: AWAITING_APPROVAL with a PR
    opened. This proves the reorder in the single-run gate (orchestrator.py,
    `_run_attempt`) is load-bearing, not incidental: disabling only the
    classifier flips the outcome, with nothing else in the test changed."""
    import no_human.testing.runner as runner_mod
    monkeypatch.setattr(runner_mod, "prerequisite_reason_for", lambda *a, **kw: None)

    tap_path = tmp_path / "incident.tap"
    tap_path.write_text(_incident_tap())
    node_err_cmd = f"cat {shlex.quote(str(tap_path))}; exit 1"

    from no_human.profile import ProjectProfile
    prof = ProjectProfile(
        repo_path=str(bare_repo), ecosystem="node",
        test_cmd=node_err_cmd,
        derived_from=["test"], proven={"test_cmd": True}, confirmed=True,
    )
    await store.upsert_profile(prof)

    orch = _orch(store, tmp_path, FakeBackend(_mutate))
    t = Task.new("desktop npm test", repo_path=str(bare_repo))
    await store.create_task(t)

    outcome = await orch.run_task(t)

    # Same shape as test_base_tree_gate.py's
    # test_node_missing_deps_invocation_error_does_not_fail_attempt: a fixed,
    # diff-independent command reproduces identically on the base tree, so
    # the base-tree check calls it "genuinely environmental" and proceeds —
    # the exact silent-pass behaviour the reorder fix eliminates.
    assert outcome.status is TaskStatus.AWAITING_APPROVAL
    assert outcome.pr_url is not None
    attempts = await store.list_attempts(t.id)
    assert len(attempts) == 1
    assert attempts[-1]["status"] != "failed"


# --------------------------------------------------------------------------- #
# Round-4 review MAJOR-1: an OWNED id must bill the attempt even through the  #
# invocation_error branch's base-tree check, single-run call site            #
# --------------------------------------------------------------------------- #
#
# `test_layered_ownership_gate_bills_an_owned_failing_id` above already
# proves ownership wins at the LAYERED call site with a hand-built
# `TestRunResult`. This pair proves the SINGLE-RUN call site
# (`_run_attempt`'s `if getattr(test_result, "invocation_error", False):`
# block, orchestrator.py) the same way `test_incident_tap_escalates_as_
# environment_through_the_real_runner` above proves the escalation case: the
# REAL runner end to end, `_owned_failing_tests` NOT stubbed, a real git diff
# via `FakeBackend`. Before the round-4 fix, an id this attempt's own diff
# added — `x.test.mjs::it fails` — whose text ALSO matches
# `runner._INVOCATION_ERROR_PATTERNS` ("Cannot find module") fell through to
# `_invocation_error_reproduces_on_base`, which reproduces identically on a
# diff-independent `cat` command and reports "genuinely environmental" —
# letting the attempt SUCCEED with a PR opened despite the coder's own test
# failing. The control right after it pins that the fix keys off the diff,
# not off the TAP text: an identically-shaped ownable id this attempt did
# NOT add still gets the environment verdict.


async def test_owned_invocation_error_bills_the_attempt_through_the_real_runner(
    bare_repo, tmp_path, store,
):
    """Real runner, real diff, `_owned_failing_tests` unstubbed. `_incident_
    tap_owned()`'s first block carries a `location: 'x.test.mjs:3:1'` line,
    so the real `runner._node_tap_failing_tests` (src/no_human/testing/
    runner.py:463-516) resolves the id to `x.test.mjs::it fails` — a
    file-scoped id `ownership.parse_file_scoped_id` / `_is_owned_file_scoped`
    (src/no_human/testing/ownership.py:93-113, 264-278) can attribute. The
    `FakeBackend` mutate below writes `x.test.mjs` into the attempt's working
    tree, so the diff `owned_failing_ids` sees for it is an ADD (status
    "A") — owned outright. Must bill: `TaskStatus.FAILED`, no PR, one
    attempt row `status="failed"` whose `failure_reason` names the owned id.

    Drives `_run_attempt` directly (`GitRepo` + CONTEXT/PLANNING transitions,
    the same shape as `test_layered_missing_prerequisite_never_retries`
    above), not `run_task`: `run_task`'s outer retry loop wraps ANY exhausted,
    still-failing attempt (billed or not) in a separate `max_attempts (N)
    reached...` ESCALATED outcome once `bounds.max_attempts` is hit — a
    real FAILED attempt at attempt 1 of 1 would get wrapped exactly like the
    environment escalation is deliberately NOT wrapped (off-ramp bypasses
    it). That wrapper is a different, already-tested code path (see the
    module docstring above); this test is about the verdict `_run_attempt`
    itself produces for THIS attempt, matching `_run_attempt_with_stubbed_
    test_output`'s doc comment on the same distinction.
    """
    tap_path = tmp_path / "incident_owned.tap"
    tap_path.write_text(_incident_tap_owned())
    node_err_cmd = f"cat {shlex.quote(str(tap_path))}; exit 1"

    from no_human.profile import ProjectProfile
    prof = ProjectProfile(
        repo_path=str(bare_repo), ecosystem="node",
        test_cmd=node_err_cmd,
        derived_from=["test"], proven={"test_cmd": True}, confirmed=True,
    )
    await store.upsert_profile(prof)

    def _mutate_owns_x_test(cwd):
        (cwd / "x.test.mjs").write_text("// this attempt's own test\n")

    cfg = _config(tmp_path)
    orch = Orchestrator(store, cfg.data, FakeBackend(_mutate_owns_x_test), SlackNotifier(None))
    t = Task.new("desktop npm test", repo_path=str(bare_repo))
    await store.create_task(t)
    await store.set_status(t, TaskStatus.CONTEXT)
    await store.set_status(t, TaskStatus.PLANNING)
    repo = GitRepo(bare_repo)

    outcome = await orch._run_attempt(t, repo, 1, "main")

    # Ownership wins outright: billed as a real test failure, never the
    # environment escalation, never a PR — the exact shape the old,
    # base-tree-check-first order got wrong for this text.
    assert not outcome.detail.startswith("tests could not run:"), outcome.detail
    assert outcome.status is TaskStatus.FAILED, outcome.detail
    assert outcome.pr_url is None

    attempts = await store.list_attempts(t.id)
    assert len(attempts) == 1, [a.get("failure_reason") for a in attempts]
    row = attempts[0]
    assert row["status"] == "failed", row["status"]
    assert "x.test.mjs::it fails" in row["failure_reason"], row["failure_reason"]
    assert _persisted(row).get("environment_error") is not True


async def test_owned_control_same_tap_without_owning_the_file_still_escalates(
    bare_repo, tmp_path, store,
):
    """Control for the test above: byte-identical TAP (same `location:`
    line, same ownable-shaped id `x.test.mjs::it fails`), but the diff never
    touches `x.test.mjs` (`FakeBackend(_mutate)` only writes `calc.py` /
    `test_calc.py`) — the real, unstubbed `_owned_failing_tests` then
    returns `[]`, so `_environment_test_failure` is consulted and wins
    exactly like the plain-incident test above. Proves the round-4 fix keys
    off the diff (`owned`), never off what the failure text merely looks
    like."""
    tap_path = tmp_path / "incident_owned_control.tap"
    tap_path.write_text(_incident_tap_owned())
    node_err_cmd = f"cat {shlex.quote(str(tap_path))}; exit 1"

    from no_human.profile import ProjectProfile
    prof = ProjectProfile(
        repo_path=str(bare_repo), ecosystem="node",
        test_cmd=node_err_cmd,
        derived_from=["test"], proven={"test_cmd": True}, confirmed=True,
    )
    await store.upsert_profile(prof)

    orch = _orch(store, tmp_path, FakeBackend(_mutate))  # never touches x.test.mjs
    t = Task.new("desktop npm test", repo_path=str(bare_repo))
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.detail.startswith("tests could not run:"), outcome.detail
    assert outcome.status is TaskStatus.ESCALATED
    assert outcome.pr_url is None

    attempts = await store.list_attempts(t.id)
    assert len(attempts) == 1, [a.get("failure_reason") for a in attempts]
    row = attempts[0]
    assert row["failure_reason"].startswith("tests could not run:")
    assert row["status"] == "failed", row["status"]
    assert row["infra_failure"] == 1
    assert _persisted(row)["environment_error"] is True
