"""`nh task cancel` raises the cooperative stop flag (`cancel_requested`) and
then, on the branch where it could NOT confirm a server hard-stopped the
attempt, used to immediately withdraw that same flag before writing FAILED.
`_server_owns_worker` returns a false negative whenever `nh serve` is
running (it binds no socket, so the probe cannot see it) or the pidfile
fallback misses — so the branch that clears the flag is exactly the one
where a live attempt, in another process, may still be reading it. Clearing
it there destroyed the only cooperative signal that attempt could observe:
a cancelled task kept a worker slot, kept spending tokens, and opened its
own pull request twenty minutes after the operator killed it.

`test_cancel_keeps_the_stop_flag_when_the_server_could_not_be_confirmed`
below is RED at HEAD: it asserts `get_cancel_request(id)` is still the
cancel reason after the CLI call returns, which fails against the
unfixed `task_cancel._go`.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from no_human.core.db import Store
from no_human.core.task import Task, TaskStatus

from tests.test_task_lifecycle import _seed_task, _get_task, _make_runner
from tests.test_task_lifecycle import _cancel_flag

pytestmark = pytest.mark.usefixtures("isolated_env_file")


# --------------------------------------------------------------------------- #
# Criterion 1 — the unconfirmed branch keeps the flag. RED at HEAD.           #
# --------------------------------------------------------------------------- #

def test_cancel_keeps_the_stop_flag_when_the_server_could_not_be_confirmed(
        tmp_path, monkeypatch):
    import no_human.cli.commands as cmd_mod

    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.IMPLEMENTING)
    runner = _make_runner(db, monkeypatch)  # `_server_owns_worker` -> False

    result = runner.invoke(
        cmd_mod.cli,
        ["task", "cancel", task_id, "--reason", "cli said stop"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0

    t = _get_task(db, task_id)
    assert t.status == TaskStatus.FAILED
    assert t.context["cancel_reason"] == "cli said stop"

    # The RED assert: the probe could not confirm a hard stop, so an attempt
    # may still be alive in another process — the flag it depends on must
    # survive this CLI call, not be withdrawn under it.
    assert _cancel_flag(db, task_id) == "cli said stop"


# --------------------------------------------------------------------------- #
# Criterion 1b — the already-correct server-owner-but-POST-failed branch,     #
# pinned so a refactor collapsing the two branches cannot re-break this.      #
# --------------------------------------------------------------------------- #

def test_cancel_keeps_the_stop_flag_when_the_server_cancel_post_fails(
        tmp_path, monkeypatch):
    import no_human.cli.commands as cmd_mod

    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.IMPLEMENTING)
    runner = _make_runner(db, monkeypatch)
    monkeypatch.setattr(cmd_mod, "_server_owns_worker", lambda _cfg: True)
    monkeypatch.setattr(cmd_mod, "_post_server_cancel", lambda *a, **kw: False)

    result = runner.invoke(
        cmd_mod.cli, ["task", "cancel", task_id, "--reason", "cli said stop"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    assert "cancel requested" in result.output

    # Still IMPLEMENTING — this branch returns before writing FAILED, exactly
    # so the running attempt can checkpoint and stop on its own.
    t = _get_task(db, task_id)
    assert t.status == TaskStatus.IMPLEMENTING
    assert _cancel_flag(db, task_id) == "cli said stop"


# --------------------------------------------------------------------------- #
# Criterion 2 — the CONFIRMED hard-stop path still withdraws the flag. This   #
# is the CLI's confirmed branch's server-side half: `_post_server_cancel`    #
# POSTs to exactly this endpoint, and `cancel_task` is what actually clears  #
# `cancel_requested` once the scheduler confirms the session stopped.        #
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


async def test_a_confirmed_hard_stop_withdraws_the_flag(api_client, api_store):
    from no_human.api.app import app as fastapi_app

    t = Task.new("do a thing", repo_path="/tmp/repo")
    await api_store.create_task(t)
    await api_store.set_status(t, TaskStatus.IMPLEMENTING, validate=False)
    await api_store.request_cancel(t.id, "cli said stop")

    fastapi_app.state.scheduler = SimpleNamespace(
        inflight=set(), get_live_status=lambda _id: None,
        request_task_cancel=lambda task_id, reason: True,
    )

    r = await api_client.post(
        f"/api/tasks/{t.id}/cancel", json={"reason": "cli said stop"})
    assert r.status_code == 200

    events = await api_store.list_events(t.id)
    kinds = {e.get("kind") for e in events}
    assert "cancel_stopped_session" in kinds

    assert await api_store.get_cancel_request(t.id) is None


# --------------------------------------------------------------------------- #
# Criterion 3 — the cancelled task's open attempt row. Option B, decided in   #
# the PR: it is NOT force-closed here. `Store.close_open_attempts`'s own     #
# docstring says it is safe only "when nothing is running", and this branch  #
# is exactly the one where a live worker cannot be ruled out — force-closing #
# the row here would corrupt resume/orphan-recovery and write a false        #
# `interrupted` reason over a row a live process still owns. Instead the row #
# is left for its own worker to close via the surviving flag  (the           #
# `_run_attempt` cancel unwind, pinned elsewhere by                          #
# `test_request_task_cancel_stops_the_live_session_within_one_tick`), with   #
# `Store.close_attempts_of_terminal_tasks` — already called from the         #
# scheduler's startup sweep — as the documented backstop for a row whose     #
# worker never comes back. This test proves both halves: the row survives   #
# the cancel call itself, and the backstop retires it once run.             #
# --------------------------------------------------------------------------- #

def test_a_cancelled_tasks_open_attempt_row_is_left_for_its_worker_then_swept(
        tmp_path, monkeypatch):
    import no_human.cli.commands as cmd_mod

    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.IMPLEMENTING)

    async def _open_attempt():
        async with Store(db) as s:
            await s.create_attempt(task_id, 2)
    asyncio.run(_open_attempt())

    runner = _make_runner(db, monkeypatch)
    result = runner.invoke(
        cmd_mod.cli, ["task", "cancel", task_id, "--reason", "cli said stop"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0

    async def _check_open():
        async with Store(db) as s:
            row = await s.latest_attempt(task_id)
            flag = await s.get_cancel_request(task_id)
            return row, flag
    row, flag = asyncio.run(_check_open())

    # (a) The row is untouched — still in_progress — AND the flag that lets
    # the live worker notice and close it on its own is still set.
    assert row["status"] == "in_progress"
    assert flag == "cli said stop"

    async def _sweep():
        async with Store(db) as s:
            n = await s.close_attempts_of_terminal_tasks()
            return n, await s.latest_attempt(task_id)
    swept, row_after = asyncio.run(_sweep())

    # (b) The documented backstop retires it — it never stays open forever.
    assert swept >= 1
    assert row_after["status"] == "interrupted"
    assert row_after["completed_at"] is not None


# --------------------------------------------------------------------------- #
# The genuinely-dead-task sibling — pins that keeping the flag changes       #
# nothing for a task with no live worker to signal.                          #
# --------------------------------------------------------------------------- #

def test_cancel_of_a_queued_task_still_ends_failed_and_fires_task_ended_cancelled(
        tmp_path, monkeypatch):
    import no_human.cli.commands as cmd_mod
    from no_human import telemetry

    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.PENDING)
    runner = _make_runner(db, monkeypatch)

    sent = []
    monkeypatch.setattr(
        telemetry, "record",
        lambda kind, config=None, **props: sent.append((kind, props)))

    result = runner.invoke(
        cmd_mod.cli, ["task", "cancel", task_id, "--reason", "no longer needed"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0

    t = _get_task(db, task_id)
    assert t.status == TaskStatus.FAILED

    terminal = [(k, p) for k, p in sent
                if k in ("task_ended", "task_completed", "task_failed")]
    assert len(terminal) == 1, f"expected exactly one terminal event, got {terminal}"
    kind, props = terminal[0]
    assert kind == "task_ended"
    assert props["outcome"] == "cancelled"

    # Flag survives (this branch never clears it), but it is inert: nothing
    # is running to read it, and the task is already terminal.
    assert _cancel_flag(db, task_id) == "no longer needed"
