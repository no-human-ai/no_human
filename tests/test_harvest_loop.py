"""The scheduled harvest loop (PLAN.md, this ticket): both existing harvest
loops — `eval.harvest.harvest` (bench candidates) and
`LearningQueue.harvest_supervisor_corrections` (B2) — now recur inside the
existing `nh serve` scheduler tick via `HarvestJob`, on a 12h-default cadence,
alongside a NEW third input to the learning harvest,
`LearningQueue.harvest_failure_signals` (escalations, reviewer FAIL findings,
tamper trips — clustered the identical `(project, source, gist)` way, the
identical `>=2` recurrence rule).

No cron, no queue, no daemon, no new thread/process — `HarvestJob` rides
`Scheduler.tick`, matching `RetirementSweepJob`'s `due()`/`maybe_run()` shape
exactly (see `tests/test_scheduler.py`'s retirement-sweep tests, the direct
template for the cadence tests below).

Both outputs stay PROPOSALS a human reviews, never something auto-applied:
bench candidates are written `runnable: false` (frozen shape, `eval/harvest.py`,
untouched here); learning proposals land `source="proposed"`, `confirmed=0`,
invisible to `confirmed_rules` until `nh learnings --confirm <id>`. See
`learning/failures.py`'s module docstring for why that boundary is the design,
not a missing feature — Tessl's loop proposes PRs, this one proposes
reviewable entries.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace


from no_human.core import scheduler as scheduler_mod
from no_human.core.db import Store
from no_human.core.scheduler import HarvestJob, Scheduler
from no_human.core.task import Task, TaskStatus
from no_human.learning import LearningQueue
from no_human.learning.queue import (
    ORIGIN_ESCALATION,
    ORIGIN_REVIEW_FAIL,
    ORIGIN_TAMPER,
    _sig,
    correction_dedupe_key,
)
from no_human.learning.corrections import CorrectionCluster, CorrectionRecord

REPO = "/repo/harvest-loop"


class FakeOrch:
    """Never completes a task until `hold` is set — enough to reach the
    scheduler's job blocks (reanalysis/wiki/retirement/harvest) with a free
    slot, matching `tests/test_scheduler.py`'s own `FakeOrch`."""

    def __init__(self, store, *, hold=None):
        self.store = store
        self.hold = hold
        self.started: list[str] = []

    async def run_task(self, task):
        self.started.append(task.id)
        if self.hold is not None:
            await self.hold.wait()
        await self.store.set_status(task, TaskStatus.AWAITING_APPROVAL, validate=False)
        return SimpleNamespace(status=TaskStatus.AWAITING_APPROVAL, task=task)


# --------------------------------------------------------------------------- #
# Seeding helpers — mirror tests/test_supervisor_learning.py's                #
# `_seed_corrections`, one per new signal.                                    #
# --------------------------------------------------------------------------- #


async def _seed_corrections(store, messages, *, repo=REPO, title="Wire the exporter"):
    t = Task.new(title, repo_path=repo)
    await store.create_task(t)
    await store.save_events(t.id, [
        {"source": "orchestrator", "kind": "supervisor_decision",
         "text": "correct", "message": m, "ts": float(i + 1), "task_id": t.id}
        for i, m in enumerate(messages)
    ])
    return t


async def _seed_escalations(store, questions, *, repo=REPO, category="OTHER",
                             title="Fix the flaky retry"):
    t = Task.new(title, repo_path=repo)
    await store.create_task(t)
    await store.save_events(t.id, [
        {"source": "orchestrator", "kind": "escalated", "task_id": t.id,
         "ts": float(i + 1),
         "blocker": {"question": q, "category": category}}
        for i, q in enumerate(questions)
    ])
    return t


async def _seed_tamper_trips(store, summaries, *, repo=REPO, tampered=True,
                              title="Fix the flaky retry"):
    t = Task.new(title, repo_path=repo)
    await store.create_task(t)
    await store.save_events(t.id, [
        {"source": "orchestrator", "kind": "tamper", "task_id": t.id,
         "ts": float(i + 1), "text": s, "tampered": tampered}
        for i, s in enumerate(summaries)
    ])
    return t


async def _seed_review_fails(store, findings, *, repo=REPO, severity="high",
                              title="Fix the flaky retry"):
    """One task, one attempt per (label, evidence) pair — `list_review_fails`
    returns one row per attempt, so N recurring findings across N attempts is
    the natural way to make a `>=2` cluster."""
    t = Task.new(title, repo_path=repo)
    await store.create_task(t)
    for i, (label, evidence) in enumerate(findings):
        attempt_id = await store.create_attempt(t.id, i + 1)
        await store.update_attempt(
            attempt_id,
            review_checklist={"items": [
                {"label": label, "passed": False, "severity": severity,
                 "evidence": evidence, "file": "src/x.py", "line": 10,
                 "comment": ""},
            ]},
            review_passed=0,
        )
    return t


# --------------------------------------------------------------------------- #
# Cadence / due() / maybe_run() — the RetirementSweepJob template.            #
# --------------------------------------------------------------------------- #


async def test_harvest_job_not_due_until_interval_elapses(store, tmp_path):
    job = HarvestJob(store, interval_seconds=9999, out_dir=tmp_path / "harvest")
    assert job.due()  # _last_run == 0.0 at construction — always due at boot
    result = await job.maybe_run()
    assert result is not None
    # D3 (2026-08-31 operator directive): `auto_manage` defaults True, so
    # every tick also runs `LearningQueue.auto_activate` — a no-op on an
    # empty store, but the result dict carries its (zero) counters too.
    assert result == {"candidates": 0, "proposals": 0, "supervisor": 0,
                       "failures": 0, "notes": [],
                       "activated": 0, "auto_archived": 0, "cap_hit": False}
    assert not job.due()
    assert await job.maybe_run() is None


async def test_scheduler_tick_does_not_harvest_every_tick(store, tmp_path):
    job = HarvestJob(store, interval_seconds=9999, out_dir=tmp_path / "harvest")
    calls = []
    real_run = job._run

    async def _counting_run():
        calls.append(1)
        return await real_run()

    job._run = _counting_run

    fake = FakeOrch(store, hold=asyncio.Event())
    sched = Scheduler(store, lambda task=None: fake, max_workers=1,
                       harvest_job=job)
    await sched.tick()
    await sched.tick()
    await sched.tick()
    assert len(calls) == 1, "only the first tick after boot is due"


async def test_scheduler_disabled_harvest_job_never_writes(store, tmp_path):
    """`harvest_job=None` (the job simply never being constructed/passed)
    means tick() never touches the harvest output dir or the memories table."""
    out_dir = tmp_path / "harvest"
    fake = FakeOrch(store, hold=asyncio.Event())
    events = []
    sched = Scheduler(store, lambda task=None: fake, max_workers=1,
                       on_event=lambda k, t: events.append((k, t)))
    await sched.tick()
    assert not out_dir.exists()
    assert not any(k == "harvest" for k, _ in events)
    assert sched.harvest is None


# --------------------------------------------------------------------------- #
# Observability — the pass fires an event, including the zero case.          #
# --------------------------------------------------------------------------- #


async def test_harvest_pass_records_zero(store, tmp_path):
    """Empty store: maybe_run() still returns a dict (not None, since it IS
    due), and a Scheduler wired with it emits exactly one `harvest` event
    naming the zero counts — unlike its neighbours, which suppress zero."""
    job = HarvestJob(store, interval_seconds=60, out_dir=tmp_path / "harvest")
    events = []
    fake = FakeOrch(store, hold=asyncio.Event())
    sched = Scheduler(store, lambda task=None: fake, max_workers=1,
                       on_event=lambda k, t: events.append((k, t)),
                       harvest_job=job)
    await sched.tick()
    harvest_events = [t for k, t in events if k == "harvest"]
    assert len(harvest_events) == 1
    assert harvest_events[0] == (
        "0 bench candidate(s), 0 learning proposal(s) "
        "(0 supervisor, 0 escalation/review-fail/tamper)"
    )


async def test_scheduler_tick_triggers_harvest_with_candidates_and_proposals(store, tmp_path):
    t = Task.new("fix the flaky retry", repo_path=REPO,
                 description="fails only on CI")
    t.status = TaskStatus.ESCALATED
    t.blocker = {"category": "OTHER", "question": "which token should it use?",
                 "root_cause_hypothesis": "no token"}
    t.acceptance_criteria = ["passes on CI"]
    await store.create_task(t)
    await _seed_corrections(store, ["use the pinned venv interpreter, not system python"] * 2)

    job = HarvestJob(store, interval_seconds=60, out_dir=tmp_path / "harvest")
    events = []
    fake = FakeOrch(store, hold=asyncio.Event())
    sched = Scheduler(store, lambda task=None: fake, max_workers=1,
                       on_event=lambda k, t: events.append((k, t)),
                       harvest_job=job)
    await sched.tick()
    harvest_events = [text for k, text in events if k == "harvest"]
    assert len(harvest_events) == 1
    assert harvest_events[0] == (
        "1 bench candidate(s), 1 learning proposal(s) "
        "(1 supervisor, 0 escalation/review-fail/tamper)"
    )


# --------------------------------------------------------------------------- #
# `nh serve` wiring — construction only, real Scheduler.__init__.            #
# --------------------------------------------------------------------------- #


def _make_serve_cfg(db_path, *, harvest_enabled=True):
    class _Cfg:
        primary_model = "claude-sonnet-4-6"
        review_model = "claude-sonnet-4-6"
        data = {
            "server": {"port": 8420},
            "concurrency": {"enabled": True, "max_workers": 1},
            "integrations": {"jira": {"enabled": False}, "linear": {"enabled": False}},
            "harvest": {"enabled": harvest_enabled},
        }

        def get(self, key, default=None):
            return self.data.get(key, default)

        def __getitem__(self, key):
            return self.data[key]
    _Cfg.db_path = db_path
    return _Cfg()


def test_no_harvest_flag_exists_and_defaults_to_false():
    from no_human.cli.commands import cli
    params = {p.name: p for p in cli.commands["serve"].params}
    assert "no_harvest" in params
    assert params["no_harvest"].default is False


def _drive_serve(monkeypatch, tmp_path, *, cfg, extra_args=()):
    from click.testing import CliRunner

    import no_human.cli.commands as cmd_mod

    captured = {}

    class _Sched(scheduler_mod.Scheduler):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            captured["harvest"] = self.harvest

        async def run_forever(self, *, stop=None, poll_interval=None, until_empty=False):
            return None

        async def failed_dispatched(self):
            return []

        async def unclaimable_orphans(self):
            return []

        async def queue_is_drained(self):
            return True

    monkeypatch.setattr(scheduler_mod, "Scheduler", _Sched)
    monkeypatch.setattr(cmd_mod, "load_config", lambda: cfg)
    monkeypatch.setattr(cmd_mod, "assert_subscription_mode", lambda *a, **k: None)
    monkeypatch.setattr(cmd_mod, "_assert_backend_usable", lambda *a, **k: None)
    monkeypatch.setattr(cmd_mod, "_acquire_pid_lock", lambda *a, **k: True)
    monkeypatch.setattr(cmd_mod, "_release_pid_lock", lambda *a, **k: None)

    runner = CliRunner()
    result = runner.invoke(cmd_mod.cli, ["serve", "--until-empty", *extra_args])
    return result, captured


async def _make_empty_db(db_path):
    s = await Store(db_path).connect()
    await s.close()


def test_serve_skips_harvest_when_disabled(monkeypatch, tmp_path):
    """CliRunner().invoke drives `serve()`'s own `asyncio.run(_go())`, so this
    (like `test_serve_until_empty_EXITS_2_when_a_row_is_stranded`) must be a
    SYNC test — an `async def` test would already be inside a running loop."""
    db_path = tmp_path / "serve.db"
    asyncio.run(_make_empty_db(db_path))
    cfg = _make_serve_cfg(db_path, harvest_enabled=False)
    result, captured = _drive_serve(monkeypatch, tmp_path, cfg=cfg)
    assert result.exit_code == 0, result.output
    assert captured["harvest"] is None


def test_serve_skips_harvest_with_no_harvest_flag(monkeypatch, tmp_path):
    db_path = tmp_path / "serve.db"
    asyncio.run(_make_empty_db(db_path))
    cfg = _make_serve_cfg(db_path, harvest_enabled=True)
    result, captured = _drive_serve(monkeypatch, tmp_path, cfg=cfg,
                                     extra_args=["--no-harvest"])
    assert result.exit_code == 0, result.output
    assert captured["harvest"] is None


def test_serve_constructs_harvest_job_when_enabled(monkeypatch, tmp_path):
    db_path = tmp_path / "serve.db"
    asyncio.run(_make_empty_db(db_path))
    cfg = _make_serve_cfg(db_path, harvest_enabled=True)
    result, captured = _drive_serve(monkeypatch, tmp_path, cfg=cfg)
    assert result.exit_code == 0, result.output
    assert isinstance(captured["harvest"], HarvestJob)


# --------------------------------------------------------------------------- #
# Escalations                                                                 #
# --------------------------------------------------------------------------- #


async def test_escalation_proposes_at_two_and_not_at_one(store):
    await _seed_escalations(store, ["which token should the retry use?"] * 2)
    q = LearningQueue(store)
    written = await q.harvest_failure_signals()
    assert len(written) == 1
    rows = await store.list_memories(confirmed=False)
    match = [r for r in rows if r["id"] == written[0]]
    assert len(match) == 1
    assert match[0]["origin"] == ORIGIN_ESCALATION
    assert match[0]["confirmed"] in (0, False)

    store2_written = await LearningQueue(store).harvest_failure_signals(project="/repo/only-one")
    assert store2_written == []


async def test_escalation_single_occurrence_proposes_nothing(store):
    await _seed_escalations(store, ["a one-off question nobody else ever asked"])
    written = await LearningQueue(store).harvest_failure_signals()
    assert written == []


async def test_non_learnable_escalation_category_is_not_mined(store):
    await _seed_escalations(store, ["need a budget top-up"] * 2, category="BUDGET_EXHAUSTED")
    written = await LearningQueue(store).harvest_failure_signals()
    assert written == []


# --------------------------------------------------------------------------- #
# Reviewer FAILs                                                              #
# --------------------------------------------------------------------------- #


async def test_review_fail_proposes_at_two_and_not_at_one(store):
    await _seed_review_fails(store, [
        ("missing null check", "the pointer at line 40 is dereferenced unchecked"),
        ("missing null check", "the pointer at line 40 is dereferenced unchecked"),
    ])
    written = await LearningQueue(store).harvest_failure_signals()
    assert len(written) == 1
    rows = await store.list_memories(confirmed=False)
    match = [r for r in rows if r["id"] == written[0]]
    assert match[0]["origin"] == ORIGIN_REVIEW_FAIL


async def test_review_fail_single_occurrence_proposes_nothing(store):
    await _seed_review_fails(store, [("a one-off finding", "seen exactly once anywhere")])
    written = await LearningQueue(store).harvest_failure_signals()
    assert written == []


async def test_reviewer_crash_sentinel_is_not_learned(store):
    await _seed_review_fails(store, [
        ("reviewer crashed", "ReviewerUnavailable: the SDK call raised"),
        ("reviewer crashed", "ReviewerUnavailable: the SDK call raised"),
    ])
    written = await LearningQueue(store).harvest_failure_signals()
    assert written == []


# --------------------------------------------------------------------------- #
# Tamper trips                                                                #
# --------------------------------------------------------------------------- #


async def test_tamper_trip_proposes_at_two_and_not_at_one(store):
    await _seed_tamper_trips(store, ["edited the review checklist after FAIL"] * 2)
    written = await LearningQueue(store).harvest_failure_signals()
    assert len(written) == 1
    rows = await store.list_memories(confirmed=False)
    match = [r for r in rows if r["id"] == written[0]]
    assert match[0]["origin"] == ORIGIN_TAMPER


async def test_untripped_tamper_check_is_not_mined(store):
    await _seed_tamper_trips(store, ["a clean pass, nothing tampered"] * 2, tampered=False)
    written = await LearningQueue(store).harvest_failure_signals()
    assert written == []


# --------------------------------------------------------------------------- #
# Curation boundary — proposals only, nothing applied.                       #
# --------------------------------------------------------------------------- #


async def test_scheduled_harvest_applies_nothing_with_the_kill_switch_off(store, tmp_path):
    """D3 (2026-08-31 operator directive) flipped the DEFAULT: with
    `auto_manage` at its default (True), this exact scenario auto-activates
    both proposals (see `test_learning_auto_activation.py`). This test now
    pins the KILL SWITCH (`auto_manage=False`) instead — the pre-D3
    behaviour this file's name always described, still reachable and still
    exact, one config flip away."""
    from no_human.eval.bench_task import NORTHSTAR_DIR

    corpus_before = len(list(NORTHSTAR_DIR.glob("*.yaml"))) if NORTHSTAR_DIR.exists() else 0

    t = Task.new("fix the flaky retry", repo_path=REPO, description="fails on CI only")
    t.status = TaskStatus.ESCALATED
    t.blocker = {"category": "OTHER", "question": "which token?",
                 "root_cause_hypothesis": "none"}
    t.acceptance_criteria = ["passes"]
    await store.create_task(t)
    await _seed_corrections(store, ["always run pytest with -n auto in this repo"] * 2)
    await _seed_escalations(store, ["which token should it use for CI?"] * 2)

    out_dir = tmp_path / "harvest"
    job = HarvestJob(store, interval_seconds=60, out_dir=out_dir, auto_manage=False)
    result = await job.maybe_run()
    assert result["candidates"] == 1
    assert result["proposals"] == 2

    corpus_after = len(list(NORTHSTAR_DIR.glob("*.yaml"))) if NORTHSTAR_DIR.exists() else 0
    assert corpus_after == corpus_before, "the golden corpus must never be touched"

    yaml_files = list(out_dir.glob("*.yaml"))
    assert len(yaml_files) == 1
    assert "runnable: false" in yaml_files[0].read_text()

    assert await store.list_memories(confirmed=True) == []
    proposed = await store.list_memories(confirmed=False)
    assert len(proposed) == 2
    for row in proposed:
        assert row["confirmed"] in (0, False)
        assert row["source"] == "proposed"


async def test_second_pass_proposes_nothing_new(store, tmp_path):
    await _seed_corrections(store, ["always run pytest with -n auto in this repo"] * 2)
    q = LearningQueue(store)
    first = await q.harvest_supervisor_corrections()
    assert len(first) == 1
    second = await q.harvest_supervisor_corrections()
    assert second == []


async def test_rejected_proposal_is_not_requeued(store):
    await _seed_escalations(store, ["which token should it use for CI?"] * 2)
    q = LearningQueue(store)
    written = await q.harvest_failure_signals()
    assert len(written) == 1
    assert await q.reject(written[0]) is True
    again = await q.harvest_failure_signals()
    assert again == [], "a human's rejection must survive the next harvest pass"


# --------------------------------------------------------------------------- #
# Dedupe key stability — the supervisor key must not shift with the new field.#
# --------------------------------------------------------------------------- #


def test_supervisor_dedupe_key_is_unchanged_by_the_source_field():
    record = CorrectionRecord(task_id="t1", project=REPO, message="m", ts=1.0)
    cluster = CorrectionCluster(project=REPO, gist="pinned venv python interpreter",
                                 records=[record])
    assert cluster.source == "supervisor"
    assert correction_dedupe_key(cluster) == _sig(
        "supervisor", cluster.gist, cluster.project or "")


# --------------------------------------------------------------------------- #
# Docstring pin — the "why proposals, not PRs" rationale must survive.        #
# --------------------------------------------------------------------------- #


def test_failures_module_docstring_records_why_not_prs():
    from no_human.learning import failures
    doc = (failures.__doc__ or "").lower()
    assert "proposes prs" in doc
    assert "reviewable entries" in doc
