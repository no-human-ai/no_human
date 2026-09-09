"""A cold `node_modules` must not fail an attempt on a flake it didn't cause.

Incident (tasks ba602e95 / f8af7f46, 2026-09-08): `node --test`'s default
PARALLEL runner turned a cold-cache Electron binary download mid-suite into a
red run on `desktop/mainSaveFailure.test.mjs` and
`desktop/mainStartupFailure.test.mjs` — files that pass on any later, serial
run. Each failed attempt cost ~1.7M tokens before this fix. Root cause (the
Electron download itself) is task 5c5e3361, out of scope here; this only
covers `Orchestrator._node_serial_rerun` and its call site in `_run_attempt`'s
plain-red branch: one serial re-run of ONLY the failing files before a red
`node --test` result is attributed to the change.

Three cases, all driven through `run_task` (the real pipeline), with
`no_human.testing.runner.run_tests` monkeypatched by an order-based recording
stub (pattern from `tests/test_flaky_rerun_attribution.py`'s `_stub`):

  * green serial re-run -> not counted against the change, attempt proceeds
    exactly as if the TESTING run itself had been green (AC1a), and the
    excusal event is pinned to land AFTER the concurrent run's own red
    summary event (AC2 landing requirement).
  * still-red serial re-run -> the attempt fails exactly as it did before this
    change, with `serial_rerun_failed` on the record (AC1b).
  * a plain `pytest` command never takes this path at all — the existing
    pytest-only re-run/attribution machinery (`_flaky_on_rerun`,
    `_newly_failing_vs_base`) is untouched and is the only thing that may run
    a second time (AC1c).
"""

import json as _json

from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task, TaskStatus
from no_human.notify.slack import SlackNotifier
from no_human.profile import ProjectProfile
from no_human.testing import runner

from .test_e2e_orchestrator import (  # noqa: F401
    FakeBackend,
    _config,
    _git,
    bare_repo,
)

NODE_CMD = "node --test desktop/*.test.mjs && uv run pytest -q -m repoguard"
ID1 = "desktop/mainSaveFailure.test.mjs::boot"
ID2 = "desktop/mainStartupFailure.test.mjs::boot"


def _orch(store, tmp_path, backend, sink=None):
    cfg = _config(tmp_path)
    # One attempt is enough to prove attribution; retries would just repeat
    # the scripted sequence and blow up the call count non-deterministically.
    cfg.data["bounds"] = {"max_attempts": 1}
    return Orchestrator(store, cfg.data, backend, SlackNotifier(None),
                        event_sink=sink)


def _add_mul(cwd):
    """A benign, test-file-untouched mutation — no test is ever 'owned'."""
    (cwd / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n"
    )


def _persisted(attempt):
    tr = attempt["test_results"]
    return _json.loads(tr) if isinstance(tr, str) else (tr or {})


def _sequenced_stub(monkeypatch, scripted):
    """Records every call and answers from *scripted* by call order.

    Each entry is a dict of `TestRunResult` field overrides; a call beyond
    the scripted list gets a plain red result with no parseable ids (the same
    fail-closed default `_stub` in test_flaky_rerun_attribution.py uses),
    so an unexpectedly EXTRA call can never accidentally read as green.
    `command` is always echoed back exactly as invoked — the runner's own
    contract, and what the substitution guards in `_node_serial_rerun` /
    `_flaky_on_rerun` read.
    """
    calls: list[str] = []

    def run_tests(repo_path, command=None, **kw):
        idx = len(calls)
        calls.append(command)
        fields = dict(ran=True, ok=False, passed=0, failed=1, errors=0,
                      command=command, output="", failing_tests=[],
                      passed_tests=[])
        if idx < len(scripted):
            fields.update(scripted[idx])
        return runner.TestRunResult(**fields)

    monkeypatch.setattr(runner, "run_tests", run_tests)
    return calls


async def _run(bare_repo, tmp_path, store, monkeypatch, test_cmd, scripted,
               *, mutate=_add_mul, env_setup=True):
    prof = ProjectProfile(
        repo_path=str(bare_repo), ecosystem="custom", test_cmd=test_cmd,
        derived_from=["test"], proven={"test_cmd": True}, confirmed=True,
    )
    await store.upsert_profile(prof)
    calls = _sequenced_stub(monkeypatch, scripted)

    events = []
    orch = _orch(store, tmp_path, FakeBackend(mutate), events.append)
    t = Task.new("add mul()", repo_path=str(bare_repo))
    if env_setup:
        # A no-op setup command short-circuits `_newly_failing_vs_base` (its
        # `env_dependent` fail-closed guard) so the base-tree recheck this
        # change does NOT touch never issues its own `runner.run_tests` call
        # — keeping the call count attributable entirely to the code under
        # test here (`_node_serial_rerun`), not to unrelated pre-existing
        # machinery. `_newly_failing_vs_base` is explicitly out of scope for
        # this task (`.no_human/PLAN.md`).
        t.config = {"env_setup": ["true"]}
    await store.create_task(t)

    outcome = await orch.run_task(t)
    attempts = await store.list_attempts(t.id)
    return outcome, attempts[-1], events, calls


async def test_serial_rerun_green_lets_the_attempt_proceed(
    bare_repo, tmp_path, store, monkeypatch
):
    """AC1a: a red `node --test` run that goes green on a serial re-run of
    ONLY the failing files is not counted against the change, and (AC2, the
    landing requirement) the excusal event is pinned to land AFTER the
    concurrent run's own red summary event."""
    scripted = [
        # call 0: TESTING — red under node's parallel runner.
        dict(failing_tests=[ID1, ID2], output="2 failed"),
        # call 1: the serial re-run — green.
        dict(ok=True, passed=2, failed=0, failing_tests=[],
             passed_tests=[ID1, ID2]),
    ]
    outcome, attempt, events, calls = await _run(
        bare_repo, tmp_path, store, monkeypatch, NODE_CMD, scripted,
        env_setup=False,  # the green path never reaches the base-tree check
    )

    assert len(calls) == 2, calls
    # Only the failing files, serially, with the compound tail preserved.
    node_seg, tail = calls[1].split("&&", 1)
    assert node_seg.strip().startswith("node --test --test-concurrency=1")
    assert "desktop/mainSaveFailure.test.mjs" in node_seg
    assert "desktop/mainStartupFailure.test.mjs" in node_seg
    assert tail.strip() == "uv run pytest -q -m repoguard"

    assert outcome.status is TaskStatus.AWAITING_APPROVAL
    tr = _persisted(attempt)
    assert tr["ok"] is False  # the ORIGINAL testing run stays red on the record
    assert sorted(tr["serial_rerun_passed"]) == sorted([ID1, ID2])
    assert tr["ok_after_serial_rerun"] is True
    assert "serial_rerun_failed" not in tr

    tests_events = [e for e in events if e["kind"] == "tests"]
    # AC2 landing requirement: the excusal event must land AFTER the
    # concurrent run's own red summary event, never before it — a mutation
    # that swaps the emit order must turn this assertion red.
    red_summary_idx = [
        i for i, e in enumerate(tests_events)
        if e.get("ok") is False and e.get("failing_tests")
    ]
    excusal_idx = [
        i for i, e in enumerate(tests_events) if e.get("serial_rerun_passed")
    ]
    assert red_summary_idx, [e for e in tests_events]
    assert excusal_idx, [e for e in tests_events]
    assert excusal_idx[0] > red_summary_idx[0], (
        "the serial-rerun excusal event must be emitted AFTER the "
        "concurrent run's red summary event, not before it: "
        f"{[e for e in tests_events]}"
    )

    named = [tests_events[i] for i in excusal_idx]
    assert ID1 in named[0]["text"] and ID2 in named[0]["text"]
    assert named[0]["ok"] is True


async def test_serial_rerun_still_red_fails_as_today(
    bare_repo, tmp_path, store, monkeypatch
):
    """AC1b: still red on the serial re-run -> fails exactly as it did before
    this change, and the re-run is on the record."""
    scripted = [
        # call 0: TESTING — red under node's parallel runner.
        dict(failing_tests=[ID1, ID2], output="2 failed"),
        # call 1: the serial re-run — still red (one id, per the incident
        # shape: a cold cache can still be cold on the immediate re-run of a
        # genuinely broken file).
        dict(ok=False, passed=1, failed=1, failing_tests=[ID1],
             passed_tests=[ID2]),
        # call 2 (default, unscripted): `_flaky_on_rerun`'s bounded stage-1
        # pre-filter — reports nothing parseable, which the helper reads as
        # "refuse, never excuse" (see test_flaky_rerun_attribution.py).
    ]
    outcome, attempt, events, calls = await _run(
        bare_repo, tmp_path, store, monkeypatch, NODE_CMD, scripted,
    )

    assert len(calls) == 3, calls
    assert "--test-concurrency=1" in calls[1]

    # With `bounds.max_attempts = 1` a failed attempt off-ramps the whole
    # task (escalated), same as any other single-attempt failure elsewhere
    # in the suite (test_flaky_rerun_attribution.py asserts the ATTEMPT row,
    # not the task-level TaskStatus, for exactly this reason).
    assert outcome.status is not TaskStatus.AWAITING_APPROVAL
    assert attempt["status"] == "failed"
    tr = _persisted(attempt)
    assert sorted(tr["serial_rerun_failed"]) == sorted([ID1, ID2])
    assert "serial_rerun_passed" not in tr
    assert "ok_after_serial_rerun" not in tr


async def test_pytest_command_is_never_serially_rerun(
    bare_repo, tmp_path, store, monkeypatch
):
    """AC1c: a plain pytest command never takes this path at all — only the
    pre-existing (out-of-scope) pytest machinery may run again."""
    # `_flaky_on_rerun` is untouched by this change and, for a real pytest
    # command, would issue its own bounded re-run — a second `run_tests`
    # call that has nothing to do with `_node_serial_rerun`. Stubbing it
    # (sanctioned by .no_human/PLAN.md's TEST PLAN) isolates the assertion
    # to the code this task actually changed.
    async def _no_flaky_excuse(self, repo, test_cmd, attributed, cwd=None):
        return None

    monkeypatch.setattr(Orchestrator, "_flaky_on_rerun", _no_flaky_excuse)

    scripted = [
        dict(failing_tests=["test_calc.py::test_add"], output="1 failed"),
    ]
    outcome, attempt, events, calls = await _run(
        bare_repo, tmp_path, store, monkeypatch, "pytest -q", scripted,
    )

    assert len(calls) == 1, calls
    assert all("--test-concurrency=1" not in c for c in calls)

    assert outcome.status is not TaskStatus.AWAITING_APPROVAL
    assert attempt["status"] == "failed"
    tr = _persisted(attempt)
    assert "serial_rerun_passed" not in tr
    assert "serial_rerun_failed" not in tr
    assert "ok_after_serial_rerun" not in tr
