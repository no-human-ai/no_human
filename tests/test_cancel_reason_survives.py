"""A cancel from `implementing` recorded its reason ONLY as an event, leaving
`context.cancel_reason` unset — so the task read as a genuine failure on
every surface that filters on that field (the board, `/api/tasks`,
`nh status`, `core/metrics.py`'s failure query, `db.py:1526`'s CAS
predicate).

Root cause (see `Store.update_task`'s docstring): `POST /api/tasks/{id}/cancel`
already wrote `context.cancel_reason` via `merge_context` — and then a
still-running attempt's next `update_task(task)`, snapshotted BEFORE the
cancel landed, rewrote the whole context blob from its stale in-memory copy
and silently erased it. That race is exactly why `cancel_session_not_found`
(no live backend task to interrupt, so the attempt keeps running past the
cancel) is the shape that reliably loses it.

The fix: a single writer, `Store.record_cancel_reason`, used by every cancel
path (the API handler, the CLI's two write sites, and the orchestrator's
hard-cancel unwind); and `update_task` can no longer erase a live
`$.cancel_reason` from a stale handle.

Follows `tests/test_cancel_stops_session.py`'s idiom: the same shared
fixtures, the same `SimpleNamespace` fake scheduler, `api_store`/`api_client`
for the HTTP surface.
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest
from click.testing import CliRunner
from httpx import ASGITransport, AsyncClient

from no_human.blockers import human_event
from no_human.core.db import Store
from no_human.core.metrics import playbook_outcomes
from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task, TaskStatus
from no_human.notify.slack import SlackNotifier
from no_human.vcs import GitRepo

from tests.test_infra_not_work import _config, bare_repo  # noqa: F401 — shared fixtures
from tests.test_cancel_stops_session import _SleepingBackend  # noqa: F401 — reused below

pytestmark = pytest.mark.usefixtures("isolated_env_file")


# --------------------------------------------------------------------------- #
# API-level fixtures — identical to test_cancel_stops_session.py's, so the    #
# HTTP surface behaves exactly as it does for the tests that must stay green. #
# --------------------------------------------------------------------------- #

@pytest.fixture
async def api_store(tmp_path):
    s = await Store(tmp_path / "test.db").connect()
    yield s
    await s.close()


@pytest.fixture
async def api_client(api_store, tmp_path):
    from no_human.api.app import app as fastapi_app
    from no_human.config import load_config
    fastapi_app.state.store = api_store
    fastapi_app.state.config = load_config(tmp_path / "config.yaml")
    transport = ASGITransport(app=fastapi_app)
    async with AsyncClient(transport=transport, base_url="http://localhost") as c:
        yield c
    if hasattr(fastapi_app.state, "scheduler"):
        del fastapi_app.state.scheduler


async def _seed_task(store: Store, *, status=TaskStatus.IMPLEMENTING) -> Task:
    t = Task.new("Fix thing", repo_path="/tmp/repo")
    await store.create_task(t)
    await store.set_status(t, status, validate=False)
    return t


async def _cancel_reason_column(store: Store, task_id: str):
    """The RAW predicate `db.py:1526` and `metrics.py:402` actually use — not
    the Python-side `Task.context` dict, which could theoretically diverge
    from what is stored."""
    row = await store.query_one(
        "SELECT json_extract(context, '$.cancel_reason') AS r FROM tasks "
        "WHERE id = ?", (task_id,))
    return row["r"] if row else None


LEGAL_CANCEL_STATES = [
    TaskStatus.IMPLEMENTING, TaskStatus.PENDING, TaskStatus.BLOCKED,
    TaskStatus.ESCALATED, TaskStatus.AWAITING_APPROVAL, TaskStatus.CONTEXT,
    TaskStatus.PLANNING, TaskStatus.REVIEWING, TaskStatus.TESTING,
]


# --------------------------------------------------------------------------- #
# Criterion 1 — every legal cancel-from state sets context.cancel_reason,     #
# via the API AND via the CLI.                                                #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("status", LEGAL_CANCEL_STATES)
async def test_cancel_from_every_legal_state_sets_cancel_reason(
        api_client, api_store, status):
    t = await _seed_task(api_store, status=status)

    r = await api_client.post(
        f"/api/tasks/{t.id}/cancel", json={"reason": "verified premise invalid"})
    assert r.status_code == 200

    reloaded = await api_store.get_task(t.id)
    assert reloaded.context.get("cancel_reason") == "verified premise invalid", (
        f"cancel from {status.value} did not set context.cancel_reason")

    raw = await _cancel_reason_column(api_store, t.id)
    assert raw == "verified premise invalid", (
        "json_extract($.cancel_reason) — the predicate db.py:1526 and "
        "metrics.py:402 actually use — does not see the write")


class _StubCfg:
    primary_model = "claude-sonnet-4-6"
    review_model = "claude-sonnet-4-6"
    data: dict = {}

    def get(self, key, default=None):
        return self.data.get(key, default)

    def __getitem__(self, key):
        return self.data[key]


@pytest.mark.parametrize("status", LEGAL_CANCEL_STATES)
def test_cli_cancel_from_every_legal_state_sets_cancel_reason(
        tmp_path, monkeypatch, status):
    import no_human.cli.commands as cmd_mod

    db_path = tmp_path / "test.db"

    async def _seed():
        async with Store(db_path) as s:
            t = Task.new("do a thing", repo_path="/tmp/repo")
            await s.create_task(t)
            await s.set_status(t, status, validate=False)
            return t.id

    task_id = asyncio.run(_seed())

    cfg = _StubCfg()
    cfg.db_path = db_path

    # No live server to reach — forces the CLI's own direct write branch
    # (cli/commands.py ~2110-2122) for every status, active or not, so the
    # parametrization is uniform.
    monkeypatch.setattr(cmd_mod, "_server_owns_worker", lambda _cfg: False)
    monkeypatch.setattr(cmd_mod, "load_config", lambda: cfg)
    monkeypatch.setattr(cmd_mod, "assert_subscription_mode", lambda **kw: None)

    runner = CliRunner()
    result = runner.invoke(
        cmd_mod.cli, ["task", "cancel", task_id, "--reason", "cli said stop"],
        catch_exceptions=False)
    assert result.exit_code == 0

    async def _reload():
        async with Store(db_path) as s:
            return await s.get_task(task_id)

    reloaded = asyncio.run(_reload())
    assert reloaded.context.get("cancel_reason") == "cli said stop", (
        f"CLI cancel from {status.value} did not set context.cancel_reason")


# --------------------------------------------------------------------------- #
# Criterion 2 — survives when NO live in-process session is found (the exact  #
# case that lost it: the attempt keeps running past the cancel).              #
# --------------------------------------------------------------------------- #

async def test_cancel_reason_survives_when_no_live_session_found(
        api_client, api_store):
    from no_human.api.app import app as fastapi_app

    t = await _seed_task(api_store)
    fastapi_app.state.scheduler = SimpleNamespace(
        inflight=set(), get_live_status=lambda _id: None,
        request_task_cancel=lambda task_id, reason: False,
    )

    r = await api_client.post(
        f"/api/tasks/{t.id}/cancel", json={"reason": "session was gone"})
    assert r.status_code == 200

    events = await api_store.list_events(t.id)
    kinds = {e.get("kind") for e in events}
    assert "cancel_session_not_found" in kinds

    reloaded = await api_store.get_task(t.id)
    assert reloaded.context.get("cancel_reason") == "session was gone"


# --------------------------------------------------------------------------- #
# Criterion 3 — the clobber regression (root cause). RED before the fix.      #
# --------------------------------------------------------------------------- #

async def test_a_stale_update_task_cannot_erase_cancel_reason(api_store):
    t = await _seed_task(api_store, status=TaskStatus.IMPLEMENTING)

    # Snapshot BEFORE the cancel — the stale in-memory handle a still-running
    # attempt would be holding when its next update_task(task) fires.
    stale = await api_store.get_task(t.id)
    assert "cancel_reason" not in (stale.context or {})

    await api_store.record_cancel_reason(t.id, "verified premise invalid")
    reloaded = await api_store.get_task(t.id)
    assert reloaded.context.get("cancel_reason") == "verified premise invalid"

    # This is literally what the still-running attempt does next: write the
    # WHOLE (pre-cancel) blob back over the row.
    await api_store.update_task(stale)

    after = await api_store.get_task(t.id)
    assert after.context.get("cancel_reason") == "verified premise invalid", (
        "a stale update_task() erased the cancel marker — the exact loss "
        "this bug report describes")


async def test_retry_still_clears_cancel_reason(api_store):
    """The preservation above is not a one-way trap: the sanctioned clearing
    path (merge_context({"cancel_reason": None}), used by `nh task retry` /
    `POST /retry`) must still delete the key."""
    t = await _seed_task(api_store, status=TaskStatus.IMPLEMENTING)
    await api_store.record_cancel_reason(t.id, "was cancelled")
    reloaded = await api_store.get_task(t.id)
    assert reloaded.context.get("cancel_reason") == "was cancelled"

    await api_store.merge_context(t.id, {"cancel_reason": None})
    after = await api_store.get_task(t.id)
    assert "cancel_reason" not in (after.context or {})

    raw = await _cancel_reason_column(api_store, t.id)
    assert raw is None


# --------------------------------------------------------------------------- #
# Criterion 4 — /api/tasks reports cancelled=true; excluded from the failure  #
# count `core/metrics.py`'s query computes.                                   #
# --------------------------------------------------------------------------- #

async def test_cancelled_task_reports_cancelled_true_on_the_api(
        api_client, api_store):
    t = await _seed_task(api_store)
    r = await api_client.post(
        f"/api/tasks/{t.id}/cancel", json={"reason": "no longer needed"})
    assert r.status_code == 200

    listing = await api_client.get("/api/tasks")
    assert listing.status_code == 200
    row = next(x for x in listing.json() if x["id"] == t.id)
    assert row["cancelled"] is True

    detail = await api_client.get(f"/api/tasks/{t.id}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["cancelled"] is True
    assert body["failure_reason"] is None


async def _emit_playbook_accessed(store: Store, task_id: str, name: str):
    await store.save_events(task_id, [{
        "kind": "playbook_accessed", "text": f"applying playbook: {name}",
        "ts": time.time(),
    }])


async def test_cancelled_task_is_not_counted_as_a_real_failure(
        api_client, api_store):
    t = await _seed_task(api_store)
    await _emit_playbook_accessed(api_store, t.id, "my-playbook")

    r = await api_client.post(
        f"/api/tasks/{t.id}/cancel", json={"reason": "withdrawn"})
    assert r.status_code == 200

    rows = await playbook_outcomes(api_store)
    row = next(r for r in rows if r["playbook"] == "my-playbook")
    assert row["cancelled"] == 1
    assert row["escalated_or_failed"] == 0


# --------------------------------------------------------------------------- #
# POSITIVE CONTROL — a genuine failure must still read as a failure.          #
# --------------------------------------------------------------------------- #

async def test_a_genuine_failure_still_reports_cancelled_false(
        api_client, api_store):
    t = await _seed_task(api_store)
    attempt_id = await api_store.create_attempt(t.id, 1)
    await api_store.update_attempt(
        attempt_id, status="failed", failure_reason="TypeError: boom")
    await api_store.set_status(t, TaskStatus.FAILED, validate=False)

    reloaded = await api_store.get_task(t.id)
    assert "cancel_reason" not in (reloaded.context or {})

    detail = await api_client.get(f"/api/tasks/{t.id}")
    body = detail.json()
    assert body["cancelled"] is False
    assert body["failure_reason"] == "TypeError: boom"

    listing = await api_client.get("/api/tasks")
    row = next(x for x in listing.json() if x["id"] == t.id)
    assert row["cancelled"] is False


async def test_a_genuine_failure_still_counts_in_the_metrics_failure_query(
        api_store):
    t = await _seed_task(api_store)
    await _emit_playbook_accessed(api_store, t.id, "another-playbook")
    attempt_id = await api_store.create_attempt(t.id, 1)
    await api_store.update_attempt(
        attempt_id, status="failed", failure_reason="TypeError: boom")
    await api_store.set_status(t, TaskStatus.FAILED, validate=False)

    rows = await playbook_outcomes(api_store)
    row = next(r for r in rows if r["playbook"] == "another-playbook")
    assert row["escalated_or_failed"] == 1
    assert row["cancelled"] == 0


async def test_a_paused_task_is_not_marked_cancelled(store, bare_repo, tmp_path):
    """`nh task pause` and a running cancel with no live session both raise
    the flag `_pending_cancel` reads, and BOTH are honoured by the SAME
    handler, `Orchestrator._honor_cancel` — it cannot tell a pause request
    from a cancel request apart. It must park BLOCKED without ever writing
    `cancel_reason`, so this drives the REAL handler (as
    `tests/test_infra_not_work.py` and `tests/test_user_paused_typed_stop.py`
    do) rather than hand-setting the status — a fix that stamped
    `cancel_reason` for anything reaching this cooperative-stop path would
    wrongly mark every pause as a cancel, and a test that never calls into
    the handler could not catch that."""
    cfg = _config(tmp_path)
    orch = Orchestrator(store, cfg.data, _SleepingBackend(), SlackNotifier(None),
                         event_sink=[].append)
    task = Task.new("do a thing", repo_path=str(bare_repo))
    await store.create_task(task)
    await store.set_status(task, TaskStatus.IMPLEMENTING, validate=False)
    repo = GitRepo(bare_repo)

    outcome = await orch._honor_cancel(task, repo, None, "Paused from board")

    assert outcome.status is TaskStatus.BLOCKED
    reloaded = await store.get_task(task.id)
    assert "cancel_reason" not in (reloaded.context or {})
    assert reloaded.status is TaskStatus.BLOCKED


# --------------------------------------------------------------------------- #
# Criterion 5 — the event write is retained (this ADDS the field write).      #
# --------------------------------------------------------------------------- #

async def test_cancel_still_writes_the_event_stream(api_client, api_store):
    from no_human.api.app import app as fastapi_app

    live = await _seed_task(api_store)
    fastapi_app.state.scheduler = SimpleNamespace(
        inflight=set(), get_live_status=lambda _id: None,
        request_task_cancel=lambda task_id, reason: True,
    )
    r = await api_client.post(
        f"/api/tasks/{live.id}/cancel", json={"reason": "stop it"})
    assert r.status_code == 200
    events = await api_store.list_events(live.id)
    kinds = {e.get("kind") for e in events}
    assert "human_cancel" in kinds
    assert "cancel_stopped_session" in kinds
    human = next(e for e in events if e.get("kind") == "human_cancel")
    assert human.get("prior_status") == TaskStatus.IMPLEMENTING.value
    reloaded = await api_store.get_task(live.id)
    assert reloaded.context.get("cancel_reason") == "stop it"

    del fastapi_app.state.scheduler
    not_found = await _seed_task(api_store)
    fastapi_app.state.scheduler = SimpleNamespace(
        inflight=set(), get_live_status=lambda _id: None,
        request_task_cancel=lambda task_id, reason: False,
    )
    r2 = await api_client.post(
        f"/api/tasks/{not_found.id}/cancel", json={"reason": "stop it too"})
    assert r2.status_code == 200
    events2 = await api_store.list_events(not_found.id)
    kinds2 = {e.get("kind") for e in events2}
    assert "human_cancel" in kinds2
    assert "cancel_session_not_found" in kinds2
    reloaded2 = await api_store.get_task(not_found.id)
    assert reloaded2.context.get("cancel_reason") == "stop it too"


# --------------------------------------------------------------------------- #
# Orchestrator hard-cancel path — no HTTP handler in the loop at all.         #
# --------------------------------------------------------------------------- #

async def test_orchestrator_hard_cancel_records_the_reason(
        store, bare_repo, tmp_path):
    cfg = _config(tmp_path)
    backend = _SleepingBackend()
    orch = Orchestrator(store, cfg.data, backend, SlackNotifier(None),
                        event_sink=[].append)
    task = Task.new("do a thing", repo_path=str(bare_repo))
    await store.create_task(task)
    await store.set_status(task, TaskStatus.CONTEXT)
    await store.set_status(task, TaskStatus.PLANNING)
    repo = GitRepo(bare_repo)

    attempt_task = asyncio.ensure_future(orch._run_attempt(task, repo, 1, "main"))
    await asyncio.wait_for(backend.started.wait(), timeout=5)

    stopped = orch.request_task_cancel(task.id, "orchestrator-level cancel")
    assert stopped is True

    outcome = await asyncio.wait_for(attempt_task, timeout=5)
    assert outcome.status == TaskStatus.FAILED

    reloaded = await store.get_task(task.id)
    assert reloaded.context.get("cancel_reason") == "orchestrator-level cancel"
