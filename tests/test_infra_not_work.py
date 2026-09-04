"""Auth/limit SDK errors are INFRA, not work.

INCIDENT (2026-08-13 17:14-18:15): the operator's subscription hit its weekly
limit and the server kept dispatching. 7 tasks failed BUDGET_EXHAUSTED at
attempts 9/9 — four of them (79183501, 2893fa27, e58a81d6, edeff716) with
ZERO tokens across ALL 9 attempts. Every dispatch died in the SDK with the
bare ``Exception: Claude Code returned an error result: success`` (the
subtype "success" has nothing to do with the real cause: the account's
weekly-limit banner never reached the SDK's own error text at all). The
existing ``paused_quota`` machinery did NOT engage for this shape — it did
engage for the Aug 12 session-quota cut, so classification is shape-sensitive
— and every dead dispatch burned a lifetime attempt on a wall none of them
could ever get past.

Follows ``tests/test_reviewer_infra_park.py``'s idiom: a fake backend
returning ``AgentResult``, a ``Store`` on ``tmp_path``, ``asyncio_mode =
"auto"`` (set repo-wide) so no ``@pytest.mark.asyncio`` is needed.
"""

from __future__ import annotations

import subprocess

import pytest

from no_human.agent.claude_backend import AgentResult
from no_human.blockers import BlockerCategory
from no_human.config import load_config
from no_human.core.bounds import Bounds, QuotaExhausted
from no_human.core.db import Store
from no_human.core.infra_breaker import InfraBreaker, infra_breaker
from no_human.core.orchestrator import Orchestrator, _infra_sdk_failure
from no_human.core.task import Task, TaskStatus
from no_human.notify.slack import SlackNotifier
from no_human.vcs import GitRepo

# Replayed verbatim from ~/.no_human/server.out during the incident.
_INCIDENT_TEXT = "Exception: Claude Code returned an error result: success"
_GENUINE_FAILURE = (
    "Traceback (most recent call last):\n  File \"x.py\", line 1\n"
    "TypeError: 'bool' object is not subscriptable"
)


@pytest.fixture(autouse=True)
def _clean_infra_breaker_singleton():
    """The breaker is a process-wide singleton (`infra_breaker.py`'s module
    docstring explains why); reset it around every test in this file so one
    test's infra failures can never leak into the next one's assertions."""
    infra_breaker().reset()
    yield
    infra_breaker().reset()


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def bare_repo(tmp_path):
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True,
                   capture_output=True)
    work = tmp_path / "work"
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


def _config(tmp_path):
    cfg = load_config(tmp_path / "config.yaml")
    cfg.data.setdefault("planning", {})["enabled"] = False
    cfg.data.setdefault("reviewer", {})["allow_advisory"] = True
    return cfg


class _ScriptedBackend:
    """Stands in for ClaudeBackend: always returns the same scripted
    `AgentResult`, whatever attempt asks for it."""

    def __init__(self, result: AgentResult):
        self._result = result
        self.calls = 0

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        self.calls += 1
        return self._result


def _incident_result(**overrides) -> AgentResult:
    fields = dict(
        final_text=_INCIDENT_TEXT, num_turns=1, is_error=True, tokens_used=0,
        session_id=None, stop_reason="error", cache_read_tokens=0,
        cache_creation_tokens=0,
    )
    fields.update(overrides)
    return AgentResult(**fields)


def _genuine_failure_result(**overrides) -> AgentResult:
    fields = dict(
        final_text=_GENUINE_FAILURE, num_turns=22, is_error=True,
        tokens_used=140_000, session_id="s", stop_reason="error",
        cache_read_tokens=1_000, cache_creation_tokens=500,
    )
    fields.update(overrides)
    return AgentResult(**fields)


# --------------------------------------------------------------------------- #
# (1)/(2)/(3) — the pure classifier                                           #
# --------------------------------------------------------------------------- #


def test_incident_shape_is_classified_infra():
    """The incident's exact shape: zero tokens, turn 1, no cache. RED before
    the fix — `_infra_sdk_failure` does not exist on unfixed code."""
    result = _incident_result()
    assert _infra_sdk_failure(result) is not None


def test_genuine_failure_with_tokens_is_not_infra():
    """Known-positive control (criterion 2): a real traceback with real spend
    is never infra, whatever it looks like otherwise."""
    result = _genuine_failure_result()
    assert _infra_sdk_failure(result) is None


@pytest.mark.parametrize("api_error_status,text", [
    (401, "some SDK/transport failure"),
    (403, "some SDK/transport failure"),
    (429, "some SDK/transport failure"),
    (None, "Error: invalid token supplied to the API"),
    (None, "Error: payment required to continue this subscription"),
    (None, "You've hit your weekly limit — try again later"),
    (None, "the account suspended message came back from the API"),
])
def test_auth_and_limit_signatures_are_infra(api_error_status, text):
    """The text/status arm proven independently of the zero-token arm: real
    turns, real tokens, but the SDK named an auth/billing wall."""
    result = AgentResult(
        final_text=text, num_turns=10, is_error=True, tokens_used=5_000,
        session_id="s", stop_reason="error", cache_read_tokens=100,
        cache_creation_tokens=0, api_error_status=api_error_status,
    )
    assert _infra_sdk_failure(result) is not None, (api_error_status, text)


def test_a_healthy_result_is_never_infra():
    result = AgentResult(
        final_text="done", num_turns=5, is_error=False, tokens_used=1_000,
        session_id="s", stop_reason="end_turn", cache_read_tokens=0,
        cache_creation_tokens=0,
    )
    assert _infra_sdk_failure(result) is None


def test_a_real_max_turns_exhaustion_with_tokens_is_not_infra():
    """A turn-starved session that DID stream tokens is a real, chargeable
    failure — the structural zero-token check must not also catch it."""
    result = AgentResult(
        final_text="ran out of turns", num_turns=1, is_error=True,
        tokens_used=0, session_id="s", stop_reason="max_turns",
        cache_read_tokens=0, cache_creation_tokens=0,
    )
    assert _infra_sdk_failure(result) is None


# --------------------------------------------------------------------------- #
# (4)/(6) — attempt-level accounting, driven through the real `_run_attempt`   #
# --------------------------------------------------------------------------- #


async def _run_one_attempt(store, bare_repo, tmp_path, result: AgentResult):
    cfg = _config(tmp_path)
    backend = _ScriptedBackend(result)
    orch = Orchestrator(store, cfg.data, backend, SlackNotifier(None),
                        event_sink=[].append)
    task = Task.new("do a thing", repo_path=str(bare_repo))
    await store.create_task(task)
    # `_run_attempt` transitions the task to IMPLEMENTING itself — only legal
    # from PLANNING (the main-flow spine, `core/task.py`), so walk it there
    # the way `_drive` would have before reaching the attempt loop.
    await store.set_status(task, TaskStatus.CONTEXT)
    await store.set_status(task, TaskStatus.PLANNING)
    repo = GitRepo(bare_repo)
    return orch, backend, task, repo


async def test_incident_attempt_leaves_lifetime_attempts_unchanged(
        store, bare_repo, tmp_path):
    """Criterion 1, first half — RED before the fix: today this attempt is
    charged (`used_attempts == 1`)."""
    orch, backend, task, repo = await _run_one_attempt(
        store, bare_repo, tmp_path, _incident_result())

    with pytest.raises(QuotaExhausted):
        await orch._run_attempt(task, repo, 1, "main")

    assert backend.calls, "the backend never ran — the test proves nothing"
    used_attempts, _, _ = await store.lifetime_usage_by_class(task.id)
    assert used_attempts == 0, (
        "a dead SDK dispatch charged a lifetime attempt")
    assert await store.count_attempts(task.id) == 1, (
        "the attempt row must still exist — it is the only durable record")


async def test_quota_prose_wall_leaves_lifetime_attempts_unchanged(
        store, bare_repo, tmp_path):
    """The OTHER shape of the same wall — the CLI's own prose ("You've hit
    your weekly limit"), which `_quota_signal` already parks correctly. RED
    before the fix: the park never closed the attempt row, so it stayed
    `in_progress`, the next attempt tombstoned it as "superseded by a newer
    attempt", and the budget gate charged it. INCIDENT (2026-08-20, task
    021899de): seven hourly quota-wake retries, ZERO tokens each, consumed
    attempts 2-8 of 9 before the wall lifted."""
    orch, backend, task, repo = await _run_one_attempt(
        store, bare_repo, tmp_path, _incident_result(
            final_text="You've hit your weekly limit · resets 2am (Asia/Jerusalem)"))

    with pytest.raises(QuotaExhausted):
        await orch._run_attempt(task, repo, 1, "main")

    assert backend.calls, "the backend never ran — the test proves nothing"
    used_attempts, _, _ = await store.lifetime_usage_by_class(task.id)
    assert used_attempts == 0, "a quota wall charged a lifetime attempt"
    attempts = await store.list_attempts(task.id)
    assert len(attempts) == 1, "the row must still exist — it is the record"
    assert attempts[0]["status"] != "in_progress", (
        "the park must close its own row, or the next attempt tombstones it "
        "as 'superseded' and the budget gate charges it")
    assert attempts[0]["failure_reason"].startswith("quota: "), attempts[0]


async def test_quota_park_inherits_the_checkpoint_it_was_resumed_from(
        store, bare_repo, tmp_path):
    """Second half of the same incident: the park wrote a blocker with NO
    checkpoint, so the next wake deleted `resume_from.sha` and branched from
    base — attempts 3-9 of 021899de redid attempt 1's 224 turns. With no repo
    in hand (the planner-level catch) the park must carry the checkpoint the
    loop was already on."""
    from no_human.blockers.taxonomy import resume_checkpoint
    orch, _backend, task, _repo = await _run_one_attempt(
        store, bare_repo, tmp_path, _incident_result())
    task.blocker = {"category": "TRANSIENT_INFRA", "resume_commit": "c0e1aede",
                    "resume_branch": "no-human/abc"}

    outcome = await orch._park_quota(task, QuotaExhausted("weekly limit"))

    assert outcome.status == TaskStatus.PAUSED_QUOTA
    assert resume_checkpoint(task.blocker) == {
        "sha": "c0e1aede", "branch": "no-human/abc"}
    assert task.blocker["wake_condition"] == "quota_refreshed"


async def test_quota_park_with_repo_checkpoints_head(store, bare_repo, tmp_path):
    """With the repo in hand (the attempt-loop catch) the park records the
    tree's HEAD — the branch point this attempt was given, or its WIP — the
    same rule `_honor_cancel` uses."""
    from no_human.blockers.taxonomy import resume_checkpoint
    orch, _backend, task, repo = await _run_one_attempt(
        store, bare_repo, tmp_path, _incident_result())
    task.blocker = None

    await orch._park_quota(task, QuotaExhausted("weekly limit"), repo=repo)

    cp = resume_checkpoint(task.blocker)
    assert cp and cp["sha"] == repo.head_sha() and cp["branch"] == "main"


async def test_quota_park_on_pr_revision_path_records_no_checkpoint(
        store, bare_repo, tmp_path):
    """A task with `pr_branch` set resumes by checking the PR branch out by
    name, so a checkpoint here is inert for branching — and a `wake`-stamped
    sha would make `_is_own_partial` fail a correct 'nothing to add' revision
    round as fabrication (D15 class). Independent review of the first cut."""
    from no_human.blockers.taxonomy import resume_checkpoint
    orch, _backend, task, repo = await _run_one_attempt(
        store, bare_repo, tmp_path, _incident_result())
    task.context = {**(task.context or {}), "pr_branch": "no-human/abc"}
    await store.update_task(task)
    task.blocker = None

    await orch._park_quota(task, QuotaExhausted("weekly limit"), repo=repo)

    assert resume_checkpoint(task.blocker) is None
    assert task.blocker["wake_condition"] == "quota_refreshed"


async def test_quota_park_prefers_the_freshest_resume_from_over_a_stale_blocker(
        store, bare_repo, tmp_path):
    """An orphan requeue re-stamps `resume_from` with a NEWER sha without
    clearing the older blocker; the planner-level park must adopt the newer
    one, never its own earlier stamp."""
    from no_human.blockers.taxonomy import resume_checkpoint
    orch, _backend, task, _repo = await _run_one_attempt(
        store, bare_repo, tmp_path, _incident_result())
    task.context = {**(task.context or {}),
                    "resume_from": {"sha": "newer111", "branch": "b", "by": "orphan_recovery"}}
    await store.update_task(task)
    task.blocker = {"category": "TRANSIENT_INFRA", "resume_commit": "older000"}

    await orch._park_quota(task, QuotaExhausted("weekly limit"))

    assert resume_checkpoint(task.blocker)["sha"] == "newer111"


async def test_genuine_failure_still_consumes_an_attempt_and_fails(
        store, bare_repo, tmp_path):
    """Criterion 2 — must be GREEN both before and after: no weakening of
    honest failure accounting."""
    orch, backend, task, repo = await _run_one_attempt(
        store, bare_repo, tmp_path, _genuine_failure_result())

    outcome = await orch._run_attempt(task, repo, 1, "main")

    assert backend.calls, "the backend never ran — the test proves nothing"
    assert outcome.status == TaskStatus.FAILED
    used_attempts, _, _ = await store.lifetime_usage_by_class(task.id)
    assert used_attempts == 1
    attempts = await store.list_attempts(task.id)
    assert len(attempts) == 1
    assert not attempts[0].get("infra_failure"), (
        "a genuine failure must never be flagged infra")


# --------------------------------------------------------------------------- #
# (5) — full `run_task` replay: paused, not failed                            #
# --------------------------------------------------------------------------- #


async def test_incident_task_is_paused_not_failed(store, bare_repo, tmp_path):
    """Criterion 1, second half. Mirrors the assertion shape at
    `tests/test_e2e_orchestrator.py:6510`."""
    cfg = _config(tmp_path)
    backend = _ScriptedBackend(_incident_result())
    orch = Orchestrator(store, cfg.data, backend, SlackNotifier(None),
                        event_sink=[].append)
    task = Task.new("do a thing", repo_path=str(bare_repo))
    await store.create_task(task)

    outcome = await orch.run_task(task)
    parked = await store.get_task(task.id)

    assert backend.calls, "the backend never ran — the test proves nothing"
    assert outcome.status is TaskStatus.PAUSED_QUOTA, (
        f"an auth/limit wall burned a lifetime attempt instead of parking: "
        f"{outcome.status} {outcome.detail}")
    assert parked.blocker["wake_condition"] == "quota_refreshed"
    assert parked.wake_check_at, "a quota park with no re-check stamp never wakes"
    used_attempts, _, _ = await store.lifetime_usage_by_class(task.id)
    assert used_attempts == 0


# --------------------------------------------------------------------------- #
# (7) — the exact incident arithmetic                                         #
# --------------------------------------------------------------------------- #


def _orch(store):
    o = Orchestrator.__new__(Orchestrator)
    o.store = store
    o.bounds = Bounds.from_config({})
    o.config = {}
    o._sink = lambda e: None
    return o


async def test_nine_incident_attempts_would_not_exhaust_the_budget(store):
    """Regression on the exact incident arithmetic: 9 dead dispatches must
    not trip BUDGET_EXHAUSTED, while 9 genuine ones still do (the default cap
    is `lifetime_attempts: 9`, bounds.py — unchanged by this fix)."""
    infra_task = Task.new("infra-only", repo_path="/tmp/x")
    await store.create_task(infra_task)
    for n in range(1, 10):
        aid = await store.create_attempt(infra_task.id, n)
        await store.update_attempt(
            aid, status="failed", infra_failure=1,
            tokens_used=0, cache_read_tokens=0, cache_creation_tokens=0)

    assert await _orch(store)._check_lifetime_budget(infra_task) is None, (
        "9 infra-classified attempts must never exhaust the lifetime budget")

    genuine_task = Task.new("genuine", repo_path="/tmp/x")
    await store.create_task(genuine_task)
    for n in range(1, 10):
        aid = await store.create_attempt(genuine_task.id, n)
        await store.update_attempt(
            aid, status="failed", tokens_used=100,
            cache_read_tokens=0, cache_creation_tokens=0)

    blocker = await _orch(store)._check_lifetime_budget(genuine_task)
    assert blocker is not None
    assert blocker.category is BlockerCategory.BUDGET_EXHAUSTED
    assert "attempts 9/9" in blocker.root_cause_hypothesis


# --------------------------------------------------------------------------- #
# (8) — the fleet breaker, in isolation                                       #
# --------------------------------------------------------------------------- #


async def test_all_infra_task_does_not_loop_forever_the_fleet_breaker_bounds_it(
        store, bare_repo, tmp_path):
    """Devil's-advocate answer, made concrete: with infra tokens excluded from
    the per-task budget (`Store._lifetime_included_sql`), what still stops a
    task whose EVERY attempt is infra-classified from looping forever on a
    dead SDK session at real cost? Not the per-task token cap — that is the
    whole point of the fix under test. Not even `lifetime_attempts` in the
    way one might hope — an infra row is excluded from that count too, so a
    single task retrying itself alone does not trip it (see
    `test_breaker_trips_on_three_distinct_tasks_and_not_on_one`'s "one task
    retrying itself is that task's problem, not the fleet's"). What DOES fire
    is the fleet-wide `InfraBreaker`: real dispatches through
    `orch._run_attempt`, each one dying the exact incident shape, each one
    recording an infra failure against the SAME task id. Three distinct
    TASKS trip it — so this one task alone never trips the breaker by
    itself, but it demonstrates the actual dispatch path
    (`_infra_sdk_failure` classification -> `record_infra_failure`) that the
    pool relies on, and folding in two more tasks is exactly
    `test_breaker_trips_on_three_distinct_tasks_and_not_on_one`, already
    green. Together the two tests are the full answer: no single task can be
    stopped by its own budget once its attempts are all infra, but the fleet
    breaker still stops the POOL from burning real dispatch cost on a dead
    session, independent of any one task's token or attempt tally."""
    breaker = infra_breaker()
    orch, backend, task, repo = await _run_one_attempt(
        store, bare_repo, tmp_path, _incident_result())

    with pytest.raises(QuotaExhausted):
        await orch._run_attempt(task, repo, 1, "main")

    assert backend.calls, "the backend never ran — the test proves nothing"
    used_attempts, _, _ = await store.lifetime_usage_by_class(task.id)
    assert used_attempts == 0, "the infra dispatch must not consume an attempt"
    assert await _orch(store)._check_lifetime_budget(task) is None, (
        "an all-infra task must not read as budget-exhausted")

    # This one task's own dispatch already recorded one strike against the
    # fleet breaker (real orchestrator wiring, not a simulated call) — two
    # more DISTINCT tasks doing the same trips the pool into cooldown, which
    # is the actual mechanism that bounds unattended infra looping. Confirmed
    # here rather than merely asserted: the breaker is not yet tripped after
    # this one task (matching the "one task retrying itself" contract above),
    # and does trip once two more distinct tasks join it.
    assert breaker.tripped() is None, (
        "one task's own infra failures must not alone trip the fleet breaker")
    assert breaker.record_infra_failure("other-task-1") is False, (
        "the SECOND distinct task must not yet trip it either — only the "
        "THIRD reaches the threshold")
    assert breaker.record_infra_failure("other-task-2") is True, (
        "the fleet breaker must trip once infra failures span 3 distinct "
        "tasks — the mechanism that actually bounds unattended infra "
        "looping when a single task's own budget cannot")
    assert breaker.tripped() is not None


def test_breaker_trips_on_three_distinct_tasks_and_not_on_one():
    distinct = InfraBreaker()
    assert distinct.tripped() is None
    distinct.record_infra_failure("t1")
    assert distinct.tripped() is None
    distinct.record_infra_failure("t2")
    assert distinct.tripped() is None
    just_tripped = distinct.record_infra_failure("t3")
    assert just_tripped is True
    assert distinct.tripped() is not None

    one_task = InfraBreaker()
    one_task.record_infra_failure("same")
    one_task.record_infra_failure("same")
    one_task.record_infra_failure("same")
    assert one_task.tripped() is None, (
        "one task retrying itself is that task's problem, not the fleet's")

    healed = InfraBreaker()
    healed.record_infra_failure("t1")
    healed.record_infra_failure("t2")
    healed.record_healthy()
    healed.record_infra_failure("t3")
    assert healed.tripped() is None, (
        "record_healthy() must reset the consecutive streak")


# --------------------------------------------------------------------------- #
# A pending stop is honoured before _drive's route split                       #
# --------------------------------------------------------------------------- #


async def test_drive_honours_a_pending_stop_before_the_human_gated_route(
        store, bare_repo, tmp_path, monkeypatch):
    """A machine re-entry keeps a human's stale stop so the loop parks on turn
    zero — but the human-gated-CI resume route went straight to `_finalize`
    and OPENED THE PR, and the plan-correction route burned a planner round,
    before any check. `_drive` now checks first."""
    from no_human.core.orchestrator import Orchestrator
    orch, _backend, task, repo = await _run_one_attempt(
        store, bare_repo, tmp_path, _incident_result())
    task.context = {**(task.context or {}),
                    "human_gated_ci": {"branch": "main", "pr_url": "x"}}
    await store.update_task(task)
    await store.set_status(task, TaskStatus.IMPLEMENTING, validate=False)
    await store.request_cancel(task.id, "Paused from board")

    async def _must_not_run(*a, **k):
        raise AssertionError("the human-gated route ran despite a pending stop")
    monkeypatch.setattr(Orchestrator, "_resume_human_gated", _must_not_run)

    outcome = await orch._drive(task, repo)

    assert outcome.status == TaskStatus.BLOCKED
    fresh = await store.get_task(task.id)
    assert fresh.blocker["category"] == "USER_PAUSED"
    assert await store.get_cancel_request(task.id) is None, "honoured stops are cleared"


async def test_honoured_stop_keeps_a_richer_parked_checkpoint(
        store, bare_repo, tmp_path):
    """R1 from the `_drive` pre-check review: at `_drive` entry the checkout
    sits at BASE, so honouring a stop with a clean tree used to overwrite the
    parked blocker's real `resume_commit`/`resume_branch` with the base sha
    and a blank branch. A prior checkpoint that descends from HEAD stands."""
    import subprocess
    from no_human.blockers.taxonomy import resume_checkpoint
    orch, _backend, task, repo = await _run_one_attempt(
        store, bare_repo, tmp_path, _incident_result())
    # real work on the task's branch, then back to base (the _drive-entry shape)
    _git(bare_repo, "checkout", "-q", "-b", "no-human/work")
    (bare_repo / "work.py").write_text("work\n")
    _git(bare_repo, "add", "work.py")
    _git(bare_repo, "commit", "-qm", "[WIP-PARTIAL] real work")
    work_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=bare_repo,
                              capture_output=True, text=True, check=True).stdout.strip()
    _git(bare_repo, "checkout", "-q", "main")
    task.blocker = {"category": "CI_GATE", "wake_condition": "after:2h",
                    "resume_commit": work_sha, "resume_branch": "no-human/work"}
    await store.update_task_columns(task)

    outcome = await orch._honor_cancel(task, repo, None, "Paused from board")

    assert outcome.status == TaskStatus.BLOCKED
    fresh = await store.get_task(task.id)
    assert fresh.blocker["category"] == "USER_PAUSED"
    assert resume_checkpoint(fresh.blocker) == {"sha": work_sha, "branch": "no-human/work"}
    assert fresh.blocker["paused_over"] == {"category": "CI_GATE", "wake_condition": "after:2h"}
    assert fresh.wake_check_at is None


async def test_honoured_stop_with_fresh_wip_records_the_new_commit(
        store, bare_repo, tmp_path):
    """Control: uncommitted work is committed and recorded — a prior that is
    an ANCESTOR of the new commit never wins (by the ancestry rule; the
    `committed_now` clause is defensive redundancy, not what this pins)."""
    from no_human.blockers.taxonomy import resume_checkpoint
    orch, _backend, task, repo = await _run_one_attempt(
        store, bare_repo, tmp_path, _incident_result())
    base = repo.head_sha()
    task.blocker = {"category": "CI_GATE", "resume_commit": base, "resume_branch": "x"}
    _git(bare_repo, "checkout", "-q", "-b", "no-human/live")   # main is protected
    (bare_repo / "calc.py").write_text("def add(a, b):\n    return b + a\n")

    await orch._honor_cancel(task, repo, "no-human/live", "Paused from board")

    fresh = await store.get_task(task.id)
    cp = resume_checkpoint(fresh.blocker)
    assert cp["sha"] != base and cp["branch"] == "no-human/live"


async def test_honoured_stop_with_no_measurable_checkpoint_keeps_the_prior(
        store, bare_repo, tmp_path):
    """A checkpoint that cannot be measured (no repo in hand, or a
    protected-branch refusal) must not erase a prior one."""
    from no_human.blockers.taxonomy import resume_checkpoint
    orch, _backend, task, _repo = await _run_one_attempt(
        store, bare_repo, tmp_path, _incident_result())
    task.blocker = {"category": "CI_GATE", "resume_commit": "a" * 40, "resume_branch": "no-human/w"}

    await orch._honor_cancel(task, None, None, "Paused from board")

    fresh = await store.get_task(task.id)
    assert resume_checkpoint(fresh.blocker) == {"sha": "a" * 40, "branch": "no-human/w"}


async def test_honoured_stop_writes_the_shared_human_pause_event(
        store, bare_repo, tmp_path):
    """b404b872: a human pause of a RUNNING task is honoured by the
    orchestrator, not the CLI, so it is the one human status change the CLI
    verbs cannot record. `_honor_cancel` must write the SAME `human_event`
    shape, in the same `set_status` transaction, carrying the status and
    blocker the pause landed on. RED on the PR head without this hunk: the
    park happens, no `human_pause` row exists."""
    orch, _backend, task, _repo = await _run_one_attempt(
        store, bare_repo, tmp_path, _incident_result())
    task.blocker = {"category": "CI_GATE", "wake_condition": "ci_green",
                    "resume_commit": "a" * 40, "resume_branch": "no-human/w"}
    prior_status = task.status

    await orch._honor_cancel(task, None, None, "Paused from board")

    events = [e for e in await store.list_events(task.id)
              if e.get("kind") == "human_pause"]
    assert len(events) == 1, events
    ev = events[0]
    assert ev["source"] == "human"
    assert ev["actor"] == "orchestrator"
    assert ev["reason"] == "Paused from board"
    assert ev["prior_status"] == prior_status.value
    assert ev["prior_blocker"]["category"] == "CI_GATE"
    assert ev["prior_blocker"]["wake_condition"] == "ci_green"
    fresh = await store.get_task(task.id)
    assert fresh.status == TaskStatus.BLOCKED


async def test_honoured_stop_respects_a_send_backs_cleared_checkpoint(
        store, bare_repo, tmp_path):
    """D1 of the first review: a human's send-back writes `resume_from` with
    NO sha ("branch from base, do not credit the abandoned partial") and
    leaves the blocker's older sha in place. The pause must not revive it —
    the next resume would stamp it `by: human` and disarm the zero-diff gate
    over sent-back work."""
    import subprocess
    from no_human.blockers import resume_checkpoint, resume_provenance
    orch, _backend, task, repo = await _run_one_attempt(
        store, bare_repo, tmp_path, _incident_result())
    _git(bare_repo, "checkout", "-q", "-b", "no-human/work")
    (bare_repo / "work.py").write_text("work\n")
    _git(bare_repo, "add", "work.py")
    _git(bare_repo, "commit", "-qm", "[WIP-PARTIAL] abandoned")
    work_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=bare_repo,
                              capture_output=True, text=True, check=True).stdout.strip()
    _git(bare_repo, "checkout", "-q", "main")
    task.blocker = {"category": "CI_GATE", "resume_commit": work_sha, "resume_branch": "no-human/work"}
    await store.update_task_columns(task)
    task.context = await store.merge_context(
        task.id, {"resume_from": resume_provenance(None, "human")})   # the send-back's write

    await orch._honor_cancel(task, repo, None, "Paused from board")

    fresh = await store.get_task(task.id)
    assert resume_checkpoint(fresh.blocker) is None or resume_checkpoint(fresh.blocker)["sha"] == repo.head_sha(), (
        "the pause revived the checkpoint the human's send-back cleared")
    assert (resume_checkpoint(fresh.blocker) or {}).get("sha") != work_sha
