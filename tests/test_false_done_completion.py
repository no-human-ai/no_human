"""P1 record integrity: task 8c8b36b5 flipped DONE with its PR open and
unmerged, and its events stop with no trace of what completed it.

Root cause #1 (false-done): `cli/commands.py`'s `approve` and
`api/app.py`'s `POST /approve` decided "there is no PR to merge" from
`attempts.pr_url` alone. 8c8b36b5's PR #253 was recorded only in
`context["pr_draft_created"]` (a draft opened pre-review) — never on an
attempt row — so the guard read "no PR" and completed the task while its PR
sat open. Fixed by `vcs.task_pr.task_has_pr_evidence`, the one fail-closed
"is there a PR" predicate both call sites now use.

Root cause #2 (eventless done): `Store.set_status` had no obligation to
record WHY/WHO completed a task. Fixed by requiring an `event=` dict on every
DONE transition (`SilentCompletion` otherwise), inserted atomically with the
status write.

Root cause #3 (no repair verb): `nh task restore-approval` only handled
ESCALATED/FAILED rows. Extended to repair a DONE row that has no completion
event and a real outstanding PR — exactly 8c8b36b5's shape.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
import pytest_asyncio
from click.testing import CliRunner
from httpx import AsyncClient, ASGITransport

from no_human.api.app import app
from no_human.blockers.wake import WakeWatcher
from no_human.cli.commands import cli, task_restore_approval
from no_human.core.db import SilentCompletion, Store
from no_human.core.task import Task, TaskStatus
from no_human.vcs.task_pr import task_has_pr_evidence

pytestmark = pytest.mark.usefixtures("isolated_env_file")


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #

@pytest_asyncio.fixture
async def store(store_factory):
    # Variant: `_seed_cli_task` and other helpers below open tmp_path/"test.db"
    # directly, so the filename is load-bearing.
    return await store_factory("test.db")


@pytest_asyncio.fixture
async def client(store, tmp_path):
    from no_human.config import load_config
    app.state.store = store
    app.state.config = load_config(tmp_path / "config.yaml")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost") as c:
        yield c


async def _seed(store: Store, *, status=TaskStatus.AWAITING_APPROVAL,
                context=None, title="Fix thing") -> Task:
    t = Task.new(title, repo_path="/tmp/repo")
    t.acceptance_criteria = ["Should work"]
    if context:
        t.context = context
    await store.create_task(t)
    if status != TaskStatus.PENDING:
        await store.set_status(t, status, validate=False)
    return t


def _seed_cli_task(db_path: Path, *, context: dict, attempts_pr_url=("", ""),
                    status=TaskStatus.AWAITING_APPROVAL) -> str:
    """The exact 8c8b36b5 fixture, for CLI-runner-driven tests: a
    plain-sync helper so `asyncio.run()` inside the CLI command's own
    click callback never collides with a live event loop."""
    async def _go():
        async with Store(db_path) as s:
            t = Task.new("Fix thing", repo_path="/tmp/repo")
            t.acceptance_criteria = ["Should work"]
            t.context = context
            await s.create_task(t)
            await s.set_status(t, status, validate=False)
            for url in attempts_pr_url:
                aid = await s.create_attempt(t.id, 1)
                if url:
                    await s.update_attempt(aid, pr_url=url)
            return t.id
    return asyncio.run(_go())


def _make_runner(path: Path, monkeypatch) -> CliRunner:
    import no_human.cli.commands as cmd_mod

    class _Cfg:
        primary_model = "claude-sonnet-4-6"
        review_model = "claude-sonnet-4-6"
        data: dict = {}

        def get(self, key, default=None):
            return self.data.get(key, default)

        def __getitem__(self, key):
            return self.data[key]

    _Cfg.db_path = path

    monkeypatch.setattr(cmd_mod, "load_config", lambda: _Cfg())
    monkeypatch.setattr(cmd_mod, "assert_subscription_mode", lambda **kw: None)
    monkeypatch.setattr(
        cmd_mod, "_probe_pool",
        lambda _cfg: cmd_mod.PoolProbe(None, cmd_mod.POOL_REFUSED))
    return CliRunner()


_DRAFT_CTX = {
    "already_satisfied_report":
        "ALREADY-SATISFIED\nCRITERION: x — MET — evidence: a.py:1",
    "pr_draft_created": "https://github.com/o/r/pull/253",
    "pr_draft_branch": "feature-253",
}


# --------------------------------------------------------------------------- #
# AC1 — the false-completion path is closed                                   #
# --------------------------------------------------------------------------- #

def test_a_draft_only_pr_blocks_the_already_satisfied_completion_cli(
    tmp_path, monkeypatch,
):
    db = tmp_path / "test.db"
    task_id = _seed_cli_task(db, context=dict(_DRAFT_CTX))
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["approve", task_id[:8]])

    assert result.exit_code == 0, result.output
    assert "253" in result.output

    async def _check():
        async with Store(db) as s:
            return await s.find_task(task_id)
    t = asyncio.run(_check())
    assert t.status is TaskStatus.AWAITING_APPROVAL


@pytest.mark.asyncio
async def test_a_draft_only_pr_blocks_the_already_satisfied_completion_api(
    store, client,
):
    t = await _seed(store, context=dict(_DRAFT_CTX))
    aid1 = await store.create_attempt(t.id, 1)
    await store.update_attempt(aid1, pr_url="")
    aid2 = await store.create_attempt(t.id, 2)
    await store.update_attempt(aid2, pr_url="")

    r = await client.post(f"/api/tasks/{t.id}/approve")

    assert r.status_code == 200
    assert "merge the pr" in r.json()["message"].lower()
    refreshed = await store.find_task(t.id)
    assert refreshed.status is TaskStatus.AWAITING_APPROVAL


@pytest.mark.asyncio
async def test_a_pr_known_only_from_an_event_still_blocks_completion(store, client):
    """No context PR at all — only a persisted `pr_draft` event. Pins the
    event-scan rung of `task_has_pr_evidence`."""
    t = await _seed(store, context={
        "already_satisfied_report":
            "ALREADY-SATISFIED\nCRITERION: x — MET — evidence: a.py:1",
    })
    await store.save_events(t.id, [{
        "source": "orchestrator", "kind": "pr_draft",
        "text": "draft PR open before review: https://github.com/o/r/pull/999",
        "pr_url": "https://github.com/o/r/pull/999", "ts": time.time(),
    }])

    r = await client.post(f"/api/tasks/{t.id}/approve")

    assert r.status_code == 200
    assert "merge the pr" in r.json()["message"].lower()
    refreshed = await store.find_task(t.id)
    assert refreshed.status is TaskStatus.AWAITING_APPROVAL


@pytest.mark.asyncio
async def test_an_abandoned_draft_does_not_block_completion(store, client):
    """Boundary: the guard must not become 'never complete'."""
    url = "https://github.com/o/r/pull/253"
    t = await _seed(store, context={
        "already_satisfied_report":
            "ALREADY-SATISFIED\nCRITERION: x — MET — evidence: a.py:1",
        "pr_draft_created": url,
        "abandoned_pr_urls": [url],
    })

    r = await client.post(f"/api/tasks/{t.id}/approve")

    assert r.status_code == 200
    assert "already satisfied" in r.json()["message"].lower()
    refreshed = await store.find_task(t.id)
    assert refreshed.status is TaskStatus.DONE


@pytest.mark.asyncio
async def test_a_genuinely_prless_claim_still_completes(store, client):
    """Regression pin for PR #101's behaviour: no attempts, no context PR,
    no PR events -> DONE."""
    t = await _seed(store, context={
        "already_satisfied_report":
            "ALREADY-SATISFIED\nCRITERION: x — MET — evidence: a.py:1",
    })

    r = await client.post(f"/api/tasks/{t.id}/approve")

    assert r.status_code == 200
    assert "already satisfied" in r.json()["message"].lower()
    refreshed = await store.find_task(t.id)
    assert refreshed.status is TaskStatus.DONE


# --------------------------------------------------------------------------- #
# AC2 — every DONE transition emits a task_event                              #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_set_status_done_without_an_event_raises(store):
    t = await _seed(store, status=TaskStatus.AWAITING_APPROVAL)

    with pytest.raises(SilentCompletion):
        await store.set_status(t, TaskStatus.DONE, validate=False)

    fresh = await store.find_task(t.id)
    assert fresh.status is TaskStatus.AWAITING_APPROVAL


@pytest.mark.asyncio
async def test_a_refused_terminal_transition_writes_no_event(store):
    t = await _seed(store, status=TaskStatus.AWAITING_APPROVAL)
    event = {"source": "test", "kind": "human_merged"}

    first = await store.set_status(t, TaskStatus.DONE, validate=False, event=event)
    assert first is not None
    # The CAS's idempotent-rewrite branch matches a row already at DONE (it
    # is a real SQL match, rowcount=1) — but it is NOT a real transition, so
    # it must not append a second completion event.
    await store.set_status(t, TaskStatus.DONE, validate=False, event=event)

    events = await store.list_events(t.id)
    completion = [e for e in events if e.get("kind") == "human_merged"]
    assert len(completion) == 1


# One scenario per production DONE writer that is cheaply reachable without a
# real git checkout / Agent SDK backend. The orchestrator's two report-only
# DONE writes (investigation, design_doc) and its two code-review DONE writes
# (clean pass, all-comments-posted) are exercised end-to-end by the
# pre-existing (unmodified) tests in tests/test_e2e_orchestrator.py:
# test_investigation_report_only_completes_as_done,
# test_design_doc_report_only_completes_as_done,
# test_clean_code_review_done_carries_the_review_as_report — each calls
# `store.set_status(task, DONE, validate=False)` deep inside `run_task()`
# with no `event=`, so on unfixed code they now fail loudly with
# `SilentCompletion` instead of silently completing; they are the coverage
# for those four sites.
#
# A sixth scenario, "lead_agent_compound_done" (LeadAgent.check_completion's
# `compound_done` DONE write), was removed 2026-08-12 when the LeadAgent
# subsystem itself was deleted (task 14c0f71b, operator decision A1) — that
# DONE writer no longer exists, so there is nothing left for the scenario to
# exercise. Same pattern as tests/test_lead_agent.py's tombstone and
# tests/test_db.py's test_historical_compound_parent_and_subtask_rows_still_load.
@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", [
    "api_approve_prless", "api_finish_review", "api_shipped",
    "wake_merged", "wake_shipped",
])
async def test_every_done_writer_leaves_a_task_event(store, client, scenario):
    if scenario == "api_approve_prless":
        t = await _seed(store, context={"already_satisfied_report": "x"})
        before = len(await store.list_events(t.id))
        r = await client.post(f"/api/tasks/{t.id}/approve")
        assert r.status_code == 200
        completion_kinds = {"approved_already_satisfied"}

    elif scenario == "api_finish_review":
        t = await _seed(store, context={
            "pr_url": "https://code.example.com/o/r/pull/1",
            "draft_review_comments": [
                {"file": "a", "line": 1, "comment": "x", "posted": True},
            ],
        })
        before = len(await store.list_events(t.id))
        r = await client.post(f"/api/tasks/{t.id}/finish-review")
        assert r.status_code == 200
        completion_kinds = {"review_finished"}

    elif scenario == "api_shipped":
        t = await _seed(store, status=TaskStatus.IMPLEMENTING)
        before = len(await store.list_events(t.id))
        r = await client.post(f"/api/tasks/{t.id}/shipped",
                              json={"sha": "a" * 40, "note": "merged by hand"})
        assert r.status_code == 200
        completion_kinds = {"human_merged"}

    elif scenario == "wake_merged":
        url = "https://github.com/o/r/pull/1"
        t = await _seed(store, context={"pr_watch": url, "pr_branch": "feature"})
        before = len(await store.list_events(t.id))

        async def pr_state(_url):
            return "MERGED"
        w = WakeWatcher(store, {}, pr_state=pr_state)
        out = await w._check_open_pr(t)
        assert out == "merged"
        completion_kinds = {"merged"}

    elif scenario == "wake_shipped":
        url = "https://github.com/o/r/pull/1"
        t = await _seed(store, context={
            "pr_watch": url, "pr_branch": "feature", "base_branch": "main",
        })
        t.repo_path = "/tmp/does-not-need-to-exist"
        before = len(await store.list_events(t.id))

        async def pr_state(_url):
            return "CLOSED"

        async def pr_shipped(repo_path, branch, base):
            return True
        w = WakeWatcher(store, {}, pr_state=pr_state, pr_shipped=pr_shipped)
        out = await w._check_open_pr(t)
        assert out == "shipped_pr_closed"
        completion_kinds = {"shipped"}

    else:
        raise AssertionError(scenario)

    fresh = await store.find_task(t.id)
    assert fresh.status is TaskStatus.DONE, scenario
    events = await store.list_events(t.id)
    assert len(events) >= before + 1, scenario
    new_kinds = {e.get("kind") for e in events[before:]}
    assert new_kinds & completion_kinds, (scenario, new_kinds)


# --------------------------------------------------------------------------- #
# AC3 — restore-approval repairs a false-done                                 #
# --------------------------------------------------------------------------- #

def _seed_false_done_cli(db_path: Path) -> str:
    """The exact 8c8b36b5 fixture: DONE, PR only in `pr_draft_created`, no
    completion event on record — events stop at a `wake_tick`, matching the
    live incident."""
    async def _go():
        async with Store(db_path) as s:
            t = Task.new("Fix thing", repo_path="/tmp/repo")
            t.context = dict(_DRAFT_CTX)
            await s.create_task(t)
            await s.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)
            await s.save_events(t.id, [{
                "source": "watcher", "kind": "wake_tick",
                "text": "watcher checked (awaiting_approval): nothing to do",
                "ts": time.time(),
            }])
            # The false-done write itself: bypass the guard the way a
            # pre-fix build did, by writing the row directly.
            await s.db.execute(
                "UPDATE tasks SET status = ? WHERE id = ?",
                (TaskStatus.DONE.value, t.id),
            )
            await s.db.commit()
            return t.id
    return asyncio.run(_go())


def test_restore_approval_repairs_a_false_done(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    task_id = _seed_false_done_cli(db)
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(
        task_restore_approval, [task_id[:8], "--reason", "false DONE repair"])

    assert result.exit_code == 0, result.output

    async def _check():
        async with Store(db) as s:
            t = await s.find_task(task_id)
            events = await s.list_events(task_id)
            return t, events
    t, events = asyncio.run(_check())
    assert t.status is TaskStatus.AWAITING_APPROVAL
    assert "approved_at" not in (t.context or {})
    assert "already_satisfied_report" not in (t.context or {})
    repaired = [e for e in events if e.get("kind") == "human_restore_approval"]
    assert repaired, events
    assert "253" in repaired[-1]["text"]


def test_restore_approval_refuses_a_legitimately_done_task(tmp_path, monkeypatch):
    db = tmp_path / "test.db"

    async def _go():
        async with Store(db) as s:
            t = Task.new("Fix thing", repo_path="/tmp/repo")
            await s.create_task(t)
            await s.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)
            await s.set_status(
                t, TaskStatus.DONE, validate=False,
                event={"source": "human", "kind": "human_merged",
                       "sha": "a" * 40, "ts": time.time()},
            )
            return t.id
    task_id = asyncio.run(_go())
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(task_restore_approval, [task_id[:8]])

    assert result.exit_code != 0

    async def _check():
        async with Store(db) as s:
            return await s.find_task(task_id)
    t = asyncio.run(_check())
    assert t.status is TaskStatus.DONE
