"""Tests for C2: structured handoff artifact persistence and resume injection."""

import ast
import inspect
import subprocess

import pytest

from no_human.core.task import Task, TaskStatus
from no_human.notify.slack import SlackNotifier
from no_human.vcs import GitRepo

from .test_e2e_orchestrator import (  # noqa: F401  (re-exported fixtures/helpers)
    FakeBackend, FakeReviewer, _config, _git, bare_repo,
)


def _task(**kw):
    defaults = dict(
        id="abc123", source="test", title="Fix the widget",
        status=TaskStatus.IMPLEMENTING,
        acceptance_criteria=["Widget works", "Tests pass"],
    )
    defaults.update(kw)
    return Task(**defaults)


def test_resume_digest_includes_handoff():
    """When task.context has a handoff, _resume_digest should surface it."""
    from no_human.core.orchestrator import Orchestrator
    # Minimal orchestrator — we only need the _resume_digest method.
    orch = object.__new__(Orchestrator)
    task = _task(context={
        "handoff": {
            "summary": "Implemented the widget parser but tests are still failing on edge case X.",
            "changed_files": ["src/widget.py", "tests/test_widget.py"],
            "commit": "deadbeef",
            "turns_used": 38,
        }
    })
    digest = orch._resume_digest(task)
    assert "previous attempt ran out of turns" in digest
    assert "38" in digest
    assert "widget parser" in digest
    assert "src/widget.py" in digest


def test_resume_digest_no_handoff():
    """Without a handoff, _resume_digest should not crash or mention handoff."""
    from no_human.core.orchestrator import Orchestrator
    orch = object.__new__(Orchestrator)
    task = _task(context={})
    digest = orch._resume_digest(task)
    assert "previous attempt ran out of turns" not in digest


def test_resume_digest_handoff_plus_blocker():
    """Handoff and blocker info should both appear."""
    from no_human.core.orchestrator import Orchestrator
    from no_human.blockers import Blocker, BlockerCategory
    orch = object.__new__(Orchestrator)
    blocker = Blocker(
        category=BlockerCategory.AMBIGUITY,
        root_cause_hypothesis="need DB schema",
    )
    task = _task(
        blocker=blocker.to_dict(),
        context={
            "handoff": {
                "summary": "Partial impl done.",
                "changed_files": [],
                "commit": "",
                "turns_used": 40,
            }
        },
    )
    digest = orch._resume_digest(task)
    assert "previous attempt ran out of turns" in digest
    assert "missing_context" in digest.lower() or "need DB schema" in digest


def test_resume_digest_review_feedback_preserved():
    """Existing review feedback injection should still work alongside handoff."""
    from no_human.core.orchestrator import Orchestrator
    orch = object.__new__(Orchestrator)
    task = _task(context={
        "review_feedback": [
            {"label": "Missing null check", "file": "src/a.py", "line": 10,
             "comment": "Add null check"},
        ],
        "handoff": {
            "summary": "Done except for reviewer findings.",
            "changed_files": ["src/a.py"],
            "commit": "abc",
            "turns_used": 35,
        },
    })
    digest = orch._resume_digest(task)
    assert "Missing null check" in digest
    assert "previous attempt ran out of turns" in digest


# --------------------------------------------------------------------------- #
# Bugfix: a gate-failed attempt's commit is real, coder-produced work, and
# must be handed to the next attempt instead of discarded — see
# `Orchestrator._persist_handoff`'s docstring (task afe1ed12: 4 attempts,
# 12,071,981 tokens, no PR, because nothing persisted a handoff on a terminal
# tests/review gate FAILURE the way the abort paths already did).
# --------------------------------------------------------------------------- #

from no_human.core.orchestrator import Orchestrator  # noqa: E402
from no_human.review.reviewer import ReviewDecision  # noqa: E402
from no_human.review.selfcheck import ChecklistItem  # noqa: E402


async def test_a_tests_failed_attempt_hands_its_commit_to_the_next_attempt(
        bare_repo, tmp_path, store):
    """RED on main: `_resume_branch_point` returns "" after a tests-gate
    failure because nothing wrote `handoff.wip_sha` for that path.

    The bare repo layout (calc.py/test_calc.py at repo root, no pytest.ini/
    conftest.py/pyproject.toml) does not match `runner._looks_like_pytest`'s
    markers, so with no confirmed profile `detect_command` returns None and
    the tests step is a vacuous, `ran=False, ok=True` pass — the gate would
    never actually fire. A confirmed `ProjectProfile` with a real `test_cmd`
    (the same shape `test_run_task_uses_confirmed_profile_test_command`
    uses) makes the TESTS gate real and driven."""
    from no_human.profile import ProjectProfile

    def mutate(cwd):
        # A real, committed defect — no test tampering — so the TESTS gate
        # (not the tamper guard) is what fails this attempt.
        (cwd / "calc.py").write_text("def add(a, b):\n    return a - b  # wrong\n")

    prof = ProjectProfile(
        repo_path=str(bare_repo), ecosystem="python", test_cmd="pytest -q",
        derived_from=["test"], proven={"test_cmd": True}, confirmed=True,
    )
    await store.upsert_profile(prof)

    cfg = _config(tmp_path)
    cfg.data["bounds"] = {"max_attempts": 1}
    orch = Orchestrator(store, cfg.data, FakeBackend(mutate), SlackNotifier(None))
    t = Task.new("fix add()", repo_path=str(bare_repo))
    t.acceptance_criteria = ["add(a,b) returns a+b"]
    await store.create_task(t)

    outcome = await orch.run_task(t)
    assert outcome.status is not TaskStatus.AWAITING_APPROVAL, outcome.detail

    attempts = await store.list_attempts(t.id)
    commit_sha = attempts[-1]["commit_sha"]
    assert commit_sha, attempts[-1]

    refreshed = await store.get_task(t.id)
    handoff = (refreshed.context or {}).get("handoff") or {}
    assert handoff.get("wip_sha") == commit_sha, handoff
    assert handoff.get("failed_gate") == "tests", handoff

    repo = GitRepo(bare_repo)
    assert orch._resume_branch_point(repo, refreshed.context, attempt_n=2) == commit_sha


async def test_b_a_review_failed_attempt_hands_its_commit_to_the_next_attempt(
        bare_repo, tmp_path, store):
    """Same defect, this time caught by the independent reviewer rather than
    the test suite (the review gate runs, and fails, BEFORE the tests gate)."""
    def mutate(cwd):
        (cwd / "calc.py").write_text("def add(a, b):\n    return 0  # broken impl\n")
        # No test changes — the existing test_add would fail this too, but the
        # point of this test is the REVIEW gate's own handoff-persisting path,
        # which runs and fails first.

    failing_decision = ReviewDecision(
        passed=False,
        checklist=[
            ChecklistItem("add(a,b) returns correct sum", False,
                          "calc.py:2 returns 0, not a+b — implementation is wrong"),
        ],
    )
    cfg = _config(tmp_path)
    cfg.data["bounds"] = {"max_attempts": 1}
    reviewer = FakeReviewer(failing_decision)
    orch = Orchestrator(store, cfg.data, FakeBackend(mutate), SlackNotifier(None),
                        reviewer=reviewer)
    t = Task.new("fix add()", repo_path=str(bare_repo))
    t.acceptance_criteria = ["add(a,b) returns a+b"]
    await store.create_task(t)

    outcome = await orch.run_task(t)
    assert outcome.status is not TaskStatus.AWAITING_APPROVAL, outcome.detail
    assert reviewer.calls, "the review gate must have actually run"

    attempts = await store.list_attempts(t.id)
    commit_sha = attempts[-1]["commit_sha"]
    assert commit_sha, attempts[-1]

    refreshed = await store.get_task(t.id)
    handoff = (refreshed.context or {}).get("handoff") or {}
    assert handoff.get("wip_sha") == commit_sha, handoff
    assert handoff.get("failed_gate") == "review", handoff

    repo = GitRepo(bare_repo)
    assert orch._resume_branch_point(repo, refreshed.context, attempt_n=2) == commit_sha


async def test_k_a_layered_test_plan_gate_failure_hands_its_commit_to_the_next_attempt(
        bare_repo, tmp_path, store):
    """RED against deleting the `_persist_handoff(gate="tests", ...)` call at
    orchestrator.py:5789-5791 — the LAYERED test-plan branch's own
    tests-gate-failure handoff site, the third of the three write paths
    (single-command: `test_a`; review: `test_b`). Without it, a project with
    a layered TestPlan silently loses a gate-failed commit's handoff exactly
    like the original 621b9fef defect, just unbound on this one path.

    Forces the layered branch the same way
    `test_e2e_orchestrator.py::test_layered_test_plan_failure_detail_aggregates_traceback_blocks`
    does: patch `_resolve_test_plan` to return a `TestPlan` with a blocking
    layer, and patch `plan_runner.run_test_plan` (the module attribute — the
    orchestrator does a local `from ..testing.plan_runner import
    run_test_plan` at call time) to return a failing `PlanResult`."""
    from unittest.mock import patch as _patch

    from no_human.testing.plan_runner import LayerResult, PlanResult
    import no_human.testing.plan_runner as plan_runner_mod
    from no_human.testing.runner import TestRunResult
    from no_human.testing.test_layers import Gating, TestLayer, TestPlan

    def mutate(cwd):
        # A real, committed defect — no test tampering — so the (faked)
        # layered tests gate, not the tamper guard, is what fails this
        # attempt.
        (cwd / "calc.py").write_text("def add(a, b):\n    return a - b  # wrong\n")

    plan = TestPlan(layers=[
        TestLayer(name="unit", command="pytest -q", gating=Gating.BLOCKING),
    ])

    captured_layered = False

    def fake_run_test_plan(test_plan, task_repo, **kwargs):
        nonlocal captured_layered
        captured_layered = True
        return PlanResult(layer_results=[
            LayerResult(
                layer_name="unit", gating=Gating.BLOCKING,
                result=TestRunResult(
                    ran=True, ok=False, passed=0, failed=1, errors=0,
                    command="pytest -q", output="1 failed",
                    failing_tests=["test_calc.py::test_add"],
                ),
            ),
        ])

    async def fake_resolve_test_plan(task):
        return plan

    cfg = _config(tmp_path)
    cfg.data["bounds"] = {"max_attempts": 1}
    orch = Orchestrator(store, cfg.data, FakeBackend(mutate), SlackNotifier(None))
    t = Task.new("fix add()", repo_path=str(bare_repo))
    t.acceptance_criteria = ["add(a,b) returns a+b"]
    await store.create_task(t)

    with _patch.object(orch, "_resolve_test_plan", fake_resolve_test_plan), \
         _patch.object(plan_runner_mod, "run_test_plan", fake_run_test_plan):
        outcome = await orch.run_task(t)
    assert outcome.status is not TaskStatus.AWAITING_APPROVAL, outcome.detail
    # Pin that the LAYERED branch actually ran — a future refactor that
    # routes this repo down the single-command path must turn this test red
    # rather than let it vacuously bind the wrong site.
    assert captured_layered is True

    attempts = await store.list_attempts(t.id)
    commit_sha = attempts[-1]["commit_sha"]
    assert commit_sha, attempts[-1]

    refreshed = await store.get_task(t.id)
    handoff = (refreshed.context or {}).get("handoff") or {}
    # These three all die when the `_persist_handoff(gate="tests", ...)`
    # call at orchestrator.py:5789-5791 is removed — the handoff dict stays
    # empty and `failed_gate == "tests"` also proves the REVIEW gate passed
    # first, so this test cannot silently bind at the review site instead.
    assert handoff.get("wip_sha") == commit_sha, handoff
    assert handoff.get("failed_gate") == "tests", handoff
    assert handoff.get("own_partial") is True, handoff

    repo = GitRepo(bare_repo)
    assert orch._resume_branch_point(repo, refreshed.context, attempt_n=2) == commit_sha


def test_c_the_handoff_names_the_gate_and_the_failure():
    """`build_resume_digest` must render the gate name and its failure summary,
    and stop calling a gate-failed ordinary commit "WIP-PARTIAL" (it never was
    one — it carries the task's own ordinary commit subject)."""
    orch = object.__new__(Orchestrator)
    task = _task(context={
        "handoff": {
            "summary": "Implemented add() but the gate rejected the commit.",
            "changed_files": ["calc.py"],
            "commit": "d" * 40,
            "wip_sha": "d" * 40,
            "turns_used": 12,
            "stopped_because": "the tests gate failed",
            "failed_gate": "tests",
            "failed_gate_summary": "test_add: AssertionError: add(1, 2) == 2, expected 3",
            "own_partial": True,
        }
    })
    assert task.context["handoff"]["failed_gate"] == "tests"

    digest = orch._resume_digest(task)
    assert "tests" in digest
    assert "AssertionError: add(1, 2) == 2, expected 3" in digest
    assert "REJECTED that commit" in digest
    assert "do NOT restart from scratch" in digest
    assert "WIP-PARTIAL" not in digest
    assert "committed as " in digest and f"committed as {'d' * 8}" in digest


async def test_i_a_gate_failure_then_a_later_abort_clears_the_stale_gate_flags(
        tmp_path):
    """Review finding F1 (blocking): `_persist_handoff`'s `gate=""` path (the
    original error/turn-budget caller) used to just OMIT
    failed_gate/failed_gate_summary/own_partial from its patch when it had
    nothing to say about a gate — and `merge_context` is RFC 7396: an omitted
    key SURVIVES a merge unchanged. So sequence attempt 1 (tests-gate
    failure, sets failed_gate="tests") then attempt 2 (a budget-exhaustion
    abort, its own unrelated WIP-PARTIAL commit, routed through
    `_record_wip_checkpoint`, which never touched these keys either) left the
    STALE "tests" flag pointing at attempt 2's commit — and
    `build_resume_digest` would render "The tests gate REJECTED that commit"
    about a commit no gate ever saw. Both writers must now explicitly clear
    the three keys (RFC 7396 `None` delete) whenever they are not recording a
    gate failure."""
    from types import SimpleNamespace

    from no_human.core.db import Store

    db_store = await Store(tmp_path / "nh.db").connect()
    try:
        orch = object.__new__(Orchestrator)
        orch.store = db_store
        t = Task.new("t", repo_path=str(tmp_path))
        await db_store.create_task(t)

        # Attempt 1: a tests-gate failure records failed_gate/summary/own_partial.
        result1 = SimpleNamespace(final_text="attempt 1 output", num_turns=10)
        await orch._persist_handoff(
            t, result1, repo=None, wip_sha="a" * 40, gate="tests",
            gate_detail="tests failed: test_add AssertionError", own_partial=True)
        handoff = (t.context or {}).get("handoff") or {}
        assert handoff.get("failed_gate") == "tests", handoff

        # Attempt 2: an UNRELATED budget-exhaustion abort with its own WIP
        # commit — no gate involved at all.
        await orch._record_wip_checkpoint(
            t, "b" * 40, repo=None, stopped_because="budget exhausted")

        refreshed = await db_store.get_task(t.id)
        handoff = (refreshed.context or {}).get("handoff") or {}
        assert handoff.get("wip_sha") == "b" * 40, handoff
        assert handoff.get("failed_gate") is None, handoff
        assert handoff.get("failed_gate_summary") is None, handoff
        assert handoff.get("own_partial") is None, handoff

        digest = orch._resume_digest(refreshed)
        assert "REJECTED that commit" not in digest
        assert "tests gate" not in digest
    finally:
        await db_store.close()

    # And the reverse order: a NON-gate abort (attempt 1) followed by a gate
    # failure (attempt 2) must still show the gate — this is not a case where
    # the clearing logic accidentally suppresses a REAL, current gate record.
    db_store2 = await Store(tmp_path / "nh2.db").connect()
    try:
        orch = object.__new__(Orchestrator)
        orch.store = db_store2
        t2 = Task.new("t2", repo_path=str(tmp_path))
        await db_store2.create_task(t2)

        await orch._record_wip_checkpoint(
            t2, "c" * 40, repo=None, stopped_because="attempt timeout")
        result2 = SimpleNamespace(final_text="attempt 2 output", num_turns=8)
        await orch._persist_handoff(
            t2, result2, repo=None, wip_sha="d" * 40, gate="review",
            gate_detail="review failed: missing null check", own_partial=True)

        refreshed2 = await db_store2.get_task(t2.id)
        handoff2 = (refreshed2.context or {}).get("handoff") or {}
        assert handoff2.get("failed_gate") == "review", handoff2
        assert handoff2.get("wip_sha") == "d" * 40, handoff2

        digest2 = orch._resume_digest(refreshed2)
        assert "REJECTED that commit" in digest2
        assert "review gate" in digest2
    finally:
        await db_store2.close()


async def test_j_a_gate_failure_then_a_turn_budget_exhaustion_clears_the_stale_gate_flags(
        tmp_path):
    """A2: `test_i` above pins `_record_wip_checkpoint`'s half of the RFC 7396
    clearing (the budget/stuck/timeout abort writer) but never drives
    `_persist_handoff`'s OWN `gate=""` path — the reachable turn-budget-
    exhaustion / `result.is_error` caller shape at the `_persist_handoff(task,
    result, repo, wip_sha=wip_sha)` call site — so that writer's clearing had
    no binding test: making its three `handoff[...] = None` writes conditional
    on a truthy `gate` (i.e. reverting to the pre-fix omission) still leaves
    `test_i` green. This test fails under that mutation."""
    from types import SimpleNamespace

    from no_human.core.db import Store

    db_store = await Store(tmp_path / "nh.db").connect()
    try:
        orch = object.__new__(Orchestrator)
        orch.store = db_store
        t = Task.new("t", repo_path=str(tmp_path))
        await db_store.create_task(t)

        # Attempt 1: a tests-gate failure records failed_gate/summary/own_partial.
        result1 = SimpleNamespace(final_text="attempt 1 output", num_turns=10)
        await orch._persist_handoff(
            t, result1, repo=None, wip_sha="a" * 40, gate="tests",
            gate_detail="tests failed: test_add AssertionError", own_partial=True)
        handoff = (t.context or {}).get("handoff") or {}
        assert handoff.get("failed_gate") == "tests", handoff

        # Attempt 2: the turn-budget-exhaustion / result.is_error caller shape —
        # `_persist_handoff` itself, no gate kwargs — NOT `_record_wip_checkpoint`.
        result2 = SimpleNamespace(final_text="attempt 2 output", num_turns=40)
        await orch._persist_handoff(t, result2, repo=None, wip_sha="b" * 40)

        refreshed = await db_store.get_task(t.id)
        handoff = (refreshed.context or {}).get("handoff") or {}
        assert handoff.get("wip_sha") == "b" * 40, handoff
        assert handoff.get("failed_gate") is None, handoff
        assert handoff.get("failed_gate_summary") is None, handoff
        assert handoff.get("own_partial") is None, handoff

        digest = orch._resume_digest(refreshed)
        assert "REJECTED that commit" not in digest
        assert "tests gate" not in digest
        assert f"committed as WIP-PARTIAL {'b' * 8}" in digest
    finally:
        await db_store.close()


def _make_repo(path):
    """A standalone bare+work repo pair, hand-rolled (rather than the shared
    `bare_repo` fixture, which is bound to one `tmp_path` per test) so a single
    test can build two independent repos — needed because a tamper-fire and a
    repro-gate pass cannot both be driven inside one attempt (a blocking
    tamper fire returns the attempt immediately; `_repro_gate_step` is never
    reached that attempt — orchestrator.py's pipeline, out of scope to change)."""
    bare = path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)],
                   check=True, capture_output=True)
    work = path / "work"
    work.mkdir()
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.email", "u@e.com")
    _git(work, "config", "user.name", "u")
    (work / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (work / "test_calc.py").write_text(
        "from calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n"
    )
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "init")
    _git(work, "remote", "add", "origin", str(bare))
    _git(work, "push", "-u", "origin", "main")
    return work


@pytest.mark.slow  # EH1: two full two-attempt e2e pipelines, real subprocess work
async def test_d_an_inherited_gate_failed_commit_still_faces_the_tamper_and_repro_gates(
        tmp_path, store):
    """AC: 'An attempt inheriting a gate-failed commit still runs the tamper
    guard and the reproduction gate (driven, not flag-asserted).'

    Proved as two independent attempt-2 scenarios, each attempt 2 building on
    attempt 1's gate-failed (tests) inherited commit — because (see
    `_make_repo`'s docstring) one mutation cannot drive both gates to
    completion in the same attempt:
      scenario 1 — attempt 2 tampers -> the tamper guard still fires on the
                   INHERITED tree, not skipped because it came from a handoff.
      scenario 2 — attempt 2 makes a legitimate fix + repro manifest -> the
                   repro gate still runs and emits a verdict on the INHERITED
                   tree.
    """
    from no_human.profile import ProjectProfile
    from no_human.testing.repro_gate import MANIFEST as REPRO_MANIFEST

    # --- scenario 1: tamper still fires on an inherited gate-failed commit ---
    repo1 = _make_repo(tmp_path / "s1")
    # A confirmed profile with a real test_cmd: this repo's layout (calc.py/
    # test_calc.py at root, no pytest markers) makes `detect_command` return
    # None otherwise, and the tests step would be a vacuous ran=False/ok=True
    # pass — attempt 1's real defect would never actually fail the gate.
    await store.upsert_profile(ProjectProfile(
        repo_path=str(repo1), ecosystem="python", test_cmd="pytest -q",
        derived_from=["test"], proven={"test_cmd": True}, confirmed=True,
    ))
    calls1 = []

    def mutate1(cwd):
        calls1.append(1)
        if len(calls1) == 1:
            # attempt 1: a real defect, no test tampering -> TESTS gate fails
            (cwd / "calc.py").write_text("def add(a, b):\n    return a - b  # wrong\n")
        else:
            # attempt 2: inherits attempt 1's commit, then guts the test
            (cwd / "calc.py").write_text("def add(a, b):\n    return 0  # still broken\n")
            (cwd / "test_calc.py").write_text(
                "from calc import add\n\ndef test_add():\n    pass\n"
            )

    cfg1 = _config(tmp_path)
    cfg1.data["bounds"] = {"max_attempts": 2}
    events1 = []
    orch1 = Orchestrator(store, cfg1.data, FakeBackend(mutate1), SlackNotifier(None),
                         event_sink=events1.append)
    t1 = Task.new("fix add()", repo_path=str(repo1))
    t1.acceptance_criteria = ["add(a,b) returns a+b"]
    await store.create_task(t1)

    outcome1 = await orch1.run_task(t1)

    attempts1 = await store.list_attempts(t1.id)
    assert len(attempts1) == 2, attempts1
    repo1_obj = GitRepo(repo1)
    assert orch1._ancestor_of(
        repo1_obj, attempts1[0]["commit_sha"], attempts1[1]["commit_sha"]
    ), "attempt 2 must have built on attempt 1's inherited commit, or this " \
       "proves nothing about an INHERITED tree"
    tamper_events = [e for e in events1 if e["kind"] == "tamper"]
    assert any(e.get("tampered") for e in tamper_events), tamper_events
    assert outcome1.pr_url is None

    # --- scenario 2: the repro gate still runs on an inherited gate-failed commit ---
    repo2 = _make_repo(tmp_path / "s2")
    await store.upsert_profile(ProjectProfile(
        repo_path=str(repo2), ecosystem="python", test_cmd="pytest -q",
        derived_from=["test"], proven={"test_cmd": True}, confirmed=True,
    ))
    calls2 = []

    def mutate2(cwd):
        calls2.append(1)
        if len(calls2) == 1:
            (cwd / "calc.py").write_text("def add(a, b):\n    return a - b  # wrong\n")
        else:
            (cwd / "calc.py").write_text(
                "def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a * b\n"
            )
            (cwd / "test_calc.py").write_text(
                "from calc import add, mul\n\n"
                "def test_add():\n    assert add(1, 2) == 3\n\n"
                "def test_mul():\n    assert mul(2, 3) == 6\n"
            )
            (cwd / ".no_human").mkdir(exist_ok=True)
            (cwd / REPRO_MANIFEST).write_text('{"tests": ["test_calc.py::test_mul"]}')

    cfg2 = _config(tmp_path)
    cfg2.data["bounds"] = {"max_attempts": 2}
    events2 = []
    orch2 = Orchestrator(store, cfg2.data, FakeBackend(mutate2), SlackNotifier(None),
                         event_sink=events2.append)
    t2 = Task.new("fix add(), add mul()", repo_path=str(repo2))
    t2.acceptance_criteria = ["add(a,b) returns a+b", "mul(a,b) returns a*b"]
    await store.create_task(t2)

    outcome2 = await orch2.run_task(t2)

    attempts2 = await store.list_attempts(t2.id)
    assert len(attempts2) == 2, attempts2
    repo2_obj = GitRepo(repo2)
    assert orch2._ancestor_of(
        repo2_obj, attempts2[0]["commit_sha"], attempts2[1]["commit_sha"]
    ), "attempt 2 must have built on attempt 1's inherited commit, or this " \
       "proves nothing about an INHERITED tree"
    # The repro gate is advisory and runs every attempt, not just the one that
    # inherits the gate-failed commit — confirmed via a real run (event dicts
    # below, verbatim): attempt 1 has no manifest yet and legitimately reports
    # "waived" (the `repro_gate.py` waive path); attempt 2, on the INHERITED
    # tree, with the manifest attempt 2 itself wrote, actually runs and passes.
    # Pin BOTH events explicitly — exactly 2, in this order — rather than
    # loosening to "at least one": a regression that made attempt 2 skip the
    # gate (e.g. because it inherited a commit) would silently vanish under a
    # `>= 1` / `[-1]`-only check, and this AC is specifically about the gate
    # not being skipped on an inherited tree.
    #   [{'source': 'orchestrator', 'kind': 'repro_gate',
    #     'text': 'waived — no .no_human/repro_tests.json manifest [advisory]',
    #     'verdict': 'waived', 'resume_shape': False},
    #    {'source': 'orchestrator', 'kind': 'repro_gate',
    #     'text': 'pass (1 test(s)) [advisory]',
    #     'verdict': 'pass', 'resume_shape': False}]
    gate_events = [e for e in events2 if e["kind"] == "repro_gate"]
    assert len(gate_events) == 2, events2
    assert gate_events[0]["verdict"] == "waived", gate_events
    assert gate_events[1]["verdict"] == "pass", gate_events
    assert outcome2.status is TaskStatus.AWAITING_APPROVAL


async def test_e_a_gate_failure_with_no_commit_still_branches_from_base(tmp_path):
    """Negative control: if the attempt made no commit at all, `wip_sha` is ""
    and the next attempt must branch from base, not from thin air. Drives
    `_persist_handoff` directly with `repo=None` — safe, because the method's
    `repo.head_sha()` call (only reached when `wip_sha` is falsy) is inside a
    bare `except Exception: pass`."""
    from types import SimpleNamespace

    from no_human.core.db import Store

    db_store = await Store(tmp_path / "nh.db").connect()
    try:
        orch = object.__new__(Orchestrator)
        orch.store = db_store
        t = Task.new("t", repo_path=str(tmp_path))
        await db_store.create_task(t)

        result = SimpleNamespace(final_text="nothing to commit", num_turns=5)
        await orch._persist_handoff(
            t, result, repo=None, wip_sha="", gate="tests",
            gate_detail="tests failed: nothing to run", own_partial=True)

        refreshed = await db_store.get_task(t.id)
        handoff = (refreshed.context or {}).get("handoff") or {}
        assert not handoff.get("wip_sha"), handoff

        assert orch._resume_branch_point(None, refreshed.context, attempt_n=2) == ""
    finally:
        await db_store.close()


def test_f_a_newer_human_resume_from_still_outranks_a_gate_failed_handoff(tmp_path):
    """Precedence regression, specific to the new own_partial-flagged shape:
    `_resume_branch_point`'s ancestry check (the general rule is pinned in
    `tests/test_resume_branch_point.py::test_partial_work_off_the_resume_line_is_rejected`,
    out of scope here) must still reject a handoff that does not descend from
    a newer human resume point, even though the handoff is own_partial=True."""
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.email", "u@e.com")
    _git(work, "config", "user.name", "u")
    (work / "a.py").write_text("base\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "base")
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=work, check=True,
                          capture_output=True, text=True).stdout.strip()

    # A human resume point, newer than base.
    (work / "a.py").write_text("human resume point\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "resume point")
    resume_point = subprocess.run(["git", "rev-parse", "HEAD"], cwd=work, check=True,
                                  capture_output=True, text=True).stdout.strip()

    # A gate-failed handoff commit on an UNRELATED line (does not descend from
    # resume_point) — e.g. a stale handoff left from before the human's answer
    # redirected the run away from it.
    _git(work, "checkout", "-q", base)
    _git(work, "checkout", "-q", "-b", "unrelated")
    (work / "b.py").write_text("abandoned direction\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "ordinary task commit, gate-failed")
    orphan = subprocess.run(["git", "rev-parse", "HEAD"], cwd=work, check=True,
                            capture_output=True, text=True).stdout.strip()
    _git(work, "checkout", "-q", "main")

    repo = GitRepo(work)
    orch = object.__new__(Orchestrator)
    ctx = {
        "resume_from": {"sha": resume_point, "by": "human"},
        "handoff": {"wip_sha": orphan, "failed_gate": "tests", "own_partial": True},
    }
    assert orch._resume_branch_point(repo, ctx, attempt_n=2) == resume_point


def test_g_an_inherited_gate_failed_commit_is_still_the_loops_own_partial(tmp_path):
    """RED on main: `_is_own_partial` used to sniff the commit SUBJECT for a
    "[WIP-PARTIAL]"/"[WIP-BLOCKED]" marker to tell the loop's own abandoned
    work from a human's. A gate-failed commit carries neither marker — it has
    the ORDINARY task-commit subject — so before this fix `_is_own_partial`
    fell through to that subject-sniff and returned False for it, wrongly
    treating real inherited coder work as if a human supplied the branch
    point."""
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.email", "u@e.com")
    _git(work, "config", "user.name", "u")
    (work / "a.py").write_text("base\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "base")

    (work / "a.py").write_text("gate-failed coder work\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "fix add() to return the sum")  # ORDINARY subject
    branch_point = subprocess.run(["git", "rev-parse", "HEAD"], cwd=work, check=True,
                                  capture_output=True, text=True).stdout.strip()

    repo = GitRepo(work)
    orch = object.__new__(Orchestrator)
    ctx = {"handoff": {"wip_sha": branch_point, "failed_gate": "tests", "own_partial": True}}
    assert orch._is_own_partial(repo, ctx, branch_point) is True


def test_l_a_stale_handoff_from_an_older_commit_is_not_credited_as_own_partial(tmp_path):
    """RED against weakening `_is_own_partial`'s
    `handoff.get("own_partial") and handoff.get("wip_sha") == branch_point`
    conjunct (orchestrator.py:16537) to bare `handoff.get("own_partial")`:
    per the comment at orchestrator.py:16530-16536, "a stale flag from an
    OLDER handoff must not credit an unrelated sha, hence the `wip_sha ==
    branch_point` check". A handoff's `own_partial=True` was written for a
    specific commit (`wip_sha`); once the branch point has moved past it
    (e.g. a later, unrelated commit), that stale flag must NOT be credited
    to the new branch point."""
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.email", "u@e.com")
    _git(work, "config", "user.name", "u")
    (work / "a.py").write_text("base\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "base")

    # The attempt the handoff was ACTUALLY written for.
    (work / "a.py").write_text("first gate-failed attempt\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "fix add() to return the sum")  # ORDINARY subject
    stale_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=work, check=True,
                               capture_output=True, text=True).stdout.strip()

    # A later, unrelated commit that is now the actual branch point — the
    # handoff above (written for stale_sha) is stale relative to it.
    (work / "a.py").write_text("a later, unrelated commit\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "chore: unrelated commit a human made")  # ORDINARY subject
    branch_point = subprocess.run(["git", "rev-parse", "HEAD"], cwd=work, check=True,
                                  capture_output=True, text=True).stdout.strip()

    repo = GitRepo(work)
    orch = object.__new__(Orchestrator)

    # No `resume_from` — deliberately, so `resume.get("sha") != branch_point`
    # and the stale-handoff branch (rather than the provenance branch) is the
    # one exercised.
    ctx = {"handoff": {"wip_sha": stale_sha, "failed_gate": "tests", "own_partial": True}}
    assert orch._is_own_partial(repo, ctx, branch_point) is False

    # Positive control on the SAME fixture: a handoff whose wip_sha DOES
    # match the branch point is still credited — pins that the guard cannot
    # be satisfied by a mutation that just always returns False, which would
    # be a different, equally wrong guard.
    ctx_matched = {"handoff": {"wip_sha": branch_point, "own_partial": True}}
    assert orch._is_own_partial(repo, ctx_matched, branch_point) is True


def test_h_no_third_writer_of_the_handoff_key():
    """AC ('exactly one function writes task.context["handoff"]') is false on
    main as literally stated — main already has TWO writers,
    `_record_wip_checkpoint` and `_persist_handoff` (see PLAN.md). The honest
    executable form pins the count: exactly these two, so a quietly-added
    third writer INSIDE THIS MODULE is caught.

    Scope, stated honestly: this AST-walks only `orchestrator.py`. There is a
    known third writer OUTSIDE it —
    `src/no_human/core/worktree.py`'s hard-kill salvage path (~:442) merges
    `{"handoff": {...}}` directly when a worker process is found dead at
    startup, a case this task's scope (three terminal gate-failure returns in
    `orchestrator.py`) never touches and does not change. This test does not
    see it and does not claim to; it pins the in-module count only."""
    import no_human.core.orchestrator as orch_mod

    src = inspect.getsource(orch_mod)
    tree = ast.parse(src)

    writers = set()

    class _Visitor(ast.NodeVisitor):
        def visit_FunctionDef(self, node):
            self._check(node)
            self.generic_visit(node)

        def visit_AsyncFunctionDef(self, node):
            self._check(node)
            self.generic_visit(node)

        def _check(self, node):
            for sub in ast.walk(node):
                if (isinstance(sub, ast.Call)
                        and isinstance(sub.func, ast.Attribute)
                        and sub.func.attr == "merge_context"):
                    for arg in sub.args:
                        if isinstance(arg, ast.Dict):
                            for key in arg.keys:
                                if isinstance(key, ast.Constant) and key.value == "handoff":
                                    writers.add(node.name)

    _Visitor().visit(tree)
    assert writers == {"_record_wip_checkpoint", "_persist_handoff"}, writers
