"""Cancelling a task must stop its running coder session, not just flip the
DB status. The Agent SDK's bundled ``claude`` process carries no task id, so
the pre-existing ``pkill -f <task_id>`` fallback in
``no_human.api.app._kill_task_processes`` matched nothing and left it
spending tokens for the rest of the attempt after the row was already
failed.

The fix: `Orchestrator.request_task_cancel` cancels the `asyncio.Task`
wrapping the coder's `backend.run(...)` directly (`_active_backend_task`),
which the pre-existing attempt-unwind path in `_run_attempt` already knows
how to turn into a closed, checkpointed attempt row. `Scheduler.
request_task_cancel` delegates to whichever orchestrator (if any) is running
that task right now; the API and CLI both reach it through the same
`POST /api/tasks/{id}/cancel` route.

Every test here is RED on unfixed `main`: none of `request_task_cancel`,
`cancel_stopped_session`, `cancel_session_not_found` exist there.
"""
from __future__ import annotations

import asyncio
import importlib
import urllib.request
from types import SimpleNamespace

import pytest
from click.testing import CliRunner
from httpx import ASGITransport, AsyncClient

from no_human.core.db import Store
from no_human.core.orchestrator import Orchestrator
from no_human.core.scheduler import Scheduler
from no_human.core.task import Task, TaskStatus
from no_human.notify.slack import SlackNotifier
from no_human.vcs import GitRepo

from tests.test_infra_not_work import _config, bare_repo  # noqa: F401 — shared fixtures

pytestmark = pytest.mark.usefixtures("isolated_env_file")


# --------------------------------------------------------------------------- #
# A fake backend that never returns on its own — only a hard cancel of the    #
# asyncio.Task wrapping it can interrupt it (stands in for a live SDK         #
# session, which never emits a cooperative "please stop" event either).       #
# --------------------------------------------------------------------------- #

class _SleepingBackend:
    def __init__(self):
        self.started = asyncio.Event()
        self.was_cancelled = False

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, on_compact=None,
                  **kwargs):
        self.started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            self.was_cancelled = True
            raise
        raise AssertionError(  # pragma: no cover - only if the sleep above returns
            "backend.run returned on its own instead of being cancelled")


async def _live_attempt(store, bare_repo, tmp_path):
    """Bring up an Orchestrator with a `_SleepingBackend` and start its
    `_run_attempt` as a background task, returning once the backend's `run()`
    is confirmed live (so a cancel sent after this point has something real
    to interrupt)."""
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
    return orch, backend, task, attempt_task


# --------------------------------------------------------------------------- #
# Criterion 1 — the backend.run task is cancelled and the existing unwind     #
# path closes the attempt row with its checkpoint and true spend.             #
# --------------------------------------------------------------------------- #

async def test_request_task_cancel_stops_the_live_session_within_one_tick(
        store, bare_repo, tmp_path):
    orch, backend, task, attempt_task = await _live_attempt(store, bare_repo, tmp_path)

    stopped = orch.request_task_cancel(task.id, "operator cancelled")
    assert stopped is True, "request_task_cancel reported nothing to stop"

    # "Within one scheduler tick": the outcome must resolve almost
    # immediately, not by waiting out the (3600s) sleep.
    outcome = await asyncio.wait_for(attempt_task, timeout=5)

    assert backend.was_cancelled, "the backend session was never interrupted"
    assert outcome.status == TaskStatus.FAILED
    assert outcome.off_ramp is True, (
        "without off_ramp=True, _drive would immediately start a fresh "
        "attempt and silently undo the cancel")
    assert "cancelled" in outcome.detail

    attempts = await store.list_attempts(task.id)
    row = attempts[-1]
    assert row["status"] == "failed"
    assert "cancelled" in (row["failure_reason"] or "")


async def test_request_task_cancel_is_false_with_no_live_session(
        store, bare_repo, tmp_path):
    """No in-flight attempt for this orchestrator right now — must report
    False (never raise), so the caller knows to emit
    cancel_session_not_found rather than pretend it stopped something."""
    cfg = _config(tmp_path)
    orch = Orchestrator(store, cfg.data, _SleepingBackend(), SlackNotifier(None),
                        event_sink=[].append)
    assert orch.request_task_cancel("nonexistent-task-id", "why") is False


# --------------------------------------------------------------------------- #
# Criterion 2 — the stop mechanism does not depend on the task id appearing   #
# in a process's argv (mutation: pkill path disabled).                        #
# --------------------------------------------------------------------------- #

async def test_cancel_stops_the_session_with_pkill_entirely_disabled(
        store, bare_repo, tmp_path, monkeypatch):
    """Mutation guard for criterion 2: neuter the argv-matching fallback
    (`_kill_task_processes`) to a no-op that can never claim to have stopped
    anything, and prove the session is still interrupted — the asyncio.Task
    cancel is doing the real work, not the pkill call.

    `no_human.api.__init__` does `from .app import app`, which rebinds the
    package's `app` attribute to the FastAPI instance — so
    `import no_human.api.app as app_mod` would silently hand back that
    instance, not the module, and `monkeypatch.setattr` would patch nothing.
    `importlib.import_module` goes through `sys.modules` directly and gets
    the real module.
    """
    app_mod = importlib.import_module("no_human.api.app")

    async def _dead_pkill(task_id):  # never matches — simulates pkill disabled
        return 0

    monkeypatch.setattr(app_mod, "_kill_task_processes", _dead_pkill)

    orch, backend, task, attempt_task = await _live_attempt(store, bare_repo, tmp_path)

    sched = Scheduler(store, orchestrator_factory=lambda *a, **k: None)
    sched._running[task.id] = orch

    stopped = sched.request_task_cancel(task.id, "operator cancelled")
    assert stopped is True

    outcome = await asyncio.wait_for(attempt_task, timeout=5)
    assert backend.was_cancelled
    assert outcome.status == TaskStatus.FAILED


# --------------------------------------------------------------------------- #
# Scheduler.request_task_cancel — pure delegation, exercised with no process  #
# involved at all (itself proof the mechanism is process/argv independent).   #
# --------------------------------------------------------------------------- #

class _FakeOrch:
    def __init__(self, stopped: bool):
        self._stopped = stopped
        self.calls: list[tuple[str, str]] = []

    def request_task_cancel(self, task_id, reason):
        self.calls.append((task_id, reason))
        return self._stopped


def test_scheduler_request_task_cancel_delegates_to_the_running_orchestrator():
    sched = Scheduler(store=None, orchestrator_factory=lambda *a, **k: None)
    fake = _FakeOrch(True)
    sched._running["abc123"] = fake

    assert sched.request_task_cancel("abc123", "operator said stop") is True
    assert fake.calls == [("abc123", "operator said stop")]


def test_scheduler_request_task_cancel_false_when_task_is_not_running():
    sched = Scheduler(store=None, orchestrator_factory=lambda *a, **k: None)
    assert sched.request_task_cancel("nope", "why") is False


def test_scheduler_request_task_cancel_survives_a_bad_hook():
    """A misbehaving orchestrator must not crash cancel — the caller falls
    back to reporting cancel_session_not_found."""
    class _Boom:
        def request_task_cancel(self, task_id, reason):
            raise RuntimeError("boom")

    sched = Scheduler(store=None, orchestrator_factory=lambda *a, **k: None)
    sched._running["t1"] = _Boom()
    assert sched.request_task_cancel("t1", "why") is False


# --------------------------------------------------------------------------- #
# Criterion 3 — API cancel emits cancel_stopped_session / cancel_session_not_ #
# found, never a silent no-op.                                                #
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


async def test_api_cancel_emits_cancel_stopped_session_when_a_live_session_exists(
        api_client, api_store):
    from no_human.api.app import app as fastapi_app

    t = await _seed_task(api_store)

    seen = {}

    def _request_task_cancel(task_id, reason):
        seen["task_id"] = task_id
        return True

    fastapi_app.state.scheduler = SimpleNamespace(
        inflight=set(), get_live_status=lambda _id: None,
        request_task_cancel=_request_task_cancel,
    )

    r = await api_client.post(f"/api/tasks/{t.id}/cancel")
    assert r.status_code == 200
    assert seen["task_id"] == t.id

    events = await api_store.list_events(t.id)
    kinds = {e.get("kind") for e in events}
    assert "cancel_stopped_session" in kinds
    assert "cancel_session_not_found" not in kinds


async def test_api_cancel_emits_cancel_session_not_found_with_no_live_session(
        api_client, api_store):
    from no_human.api.app import app as fastapi_app

    t = await _seed_task(api_store)

    fastapi_app.state.scheduler = SimpleNamespace(
        inflight=set(), get_live_status=lambda _id: None,
        request_task_cancel=lambda task_id, reason: False,
    )

    r = await api_client.post(f"/api/tasks/{t.id}/cancel")
    assert r.status_code == 200

    events = await api_store.list_events(t.id)
    kinds = {e.get("kind") for e in events}
    assert "cancel_session_not_found" in kinds
    assert "cancel_stopped_session" not in kinds


async def test_api_cancel_reports_not_found_when_there_is_no_scheduler_at_all(
        api_client, api_store):
    """No scheduler attached to the app at all (e.g. a board-only process) —
    must never silently no-op, and must never crash."""
    t = await _seed_task(api_store)
    r = await api_client.post(f"/api/tasks/{t.id}/cancel")
    assert r.status_code == 200
    events = await api_store.list_events(t.id)
    kinds = {e.get("kind") for e in events}
    assert "cancel_session_not_found" in kinds


# --------------------------------------------------------------------------- #
# Criterion 4 — `nh task cancel` and `POST /api/tasks/{id}/cancel` share the  #
# same path (CLI commands call asyncio.run() internally so this is sync).    #
# --------------------------------------------------------------------------- #

class _StubCfg:
    primary_model = "claude-sonnet-4-6"
    review_model = "claude-sonnet-4-6"
    data: dict = {}

    def get(self, key, default=None):
        return self.data.get(key, default)

    def __getitem__(self, key):
        return self.data[key]


def test_cli_task_cancel_hits_the_same_server_cancel_path_as_the_api(
        tmp_path, monkeypatch):
    """`nh task cancel` must POST to the exact route `cancel_task` is
    registered on — proving the CLI and the board's cancel button drive the
    same server-side stop path, not two divergent implementations."""
    import no_human.cli.commands as cmd_mod

    db_path = tmp_path / "test.db"

    async def _seed():
        async with Store(db_path) as s:
            t = Task.new("do a thing", repo_path="/tmp/repo")
            await s.create_task(t)
            await s.set_status(t, TaskStatus.IMPLEMENTING, validate=False)
            return t.id

    task_id = asyncio.run(_seed())

    seen = {}

    def _fake_post_server_cancel(config, tid, reason):
        seen["task_id"] = tid
        seen["reason"] = reason
        return True

    cfg = _StubCfg()
    cfg.db_path = db_path

    monkeypatch.setattr(cmd_mod, "_post_server_cancel", _fake_post_server_cancel)
    monkeypatch.setattr(cmd_mod, "_server_owns_worker", lambda _cfg: True)
    monkeypatch.setattr(cmd_mod, "load_config", lambda: cfg)
    monkeypatch.setattr(cmd_mod, "assert_subscription_mode", lambda **kw: None)

    runner = CliRunner()
    result = runner.invoke(cmd_mod.cli, ["task", "cancel", task_id],
                            catch_exceptions=False)

    assert result.exit_code == 0
    assert seen["task_id"] == task_id
    assert "the server stopped" in result.output


def test_post_server_cancel_posts_to_the_exact_cancel_route(monkeypatch):
    """Assert the CLI's HTTP call targets the identical route the API test
    above proves stops the session — the literal shared path, not just a
    behavioral coincidence.

    `_post_server_cancel` does `import urllib.request` locally, but that name
    still resolves through `sys.modules['urllib.request']` — the same module
    object this test imports at the top of the file — so patching `urlopen`
    here is patching the exact call the CLI makes.
    """
    import no_human.cli.commands as cmd_mod  # noqa: F401 — imported for parity/clarity

    captured = {}

    class _FakeResp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _fake_urlopen(req, timeout=5.0):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        return _FakeResp()

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    cfg = _StubCfg()
    ok = cmd_mod._post_server_cancel(cfg, "task-123", "because")

    assert ok is True
    assert captured["method"] == "POST"
    assert captured["url"] == "http://127.0.0.1:8420/api/tasks/task-123/cancel"
