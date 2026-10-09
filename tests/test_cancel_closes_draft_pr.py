"""Tests for Issue #479: cancelling a task retitles and closes its draft pull request.

Acceptance criteria:
- A task cancelled through `nh task cancel` or the board's cancel endpoint has its
  outstanding draft pull request retitled and closed, and records one event naming
  the reason.
- After that close, the task's draft slot no longer names the pull request and the
  abandoned list contains it, so the task-level resolver no longer returns the
  closed pull request from its draft slot.
- A forge error while closing leaves the task cancelled with its bookkeeping
  written, and raises nothing.
- A pull request the task delivered for review is not closed by a cancel.

The board's cancel and split endpoints run the close-out after the response is
sent, so a slow forge never delays the cancel. The orchestrator's own abandon
path (`_abandon_draft_pr`) delegates to the same helper; the funnel and
branch-move routes into it are tested in `test_abandoned_draft_closed.py` and
`test_pr_body_truthfulness.py`.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

import pytest
import pytest_asyncio
from click.testing import CliRunner
from httpx import ASGITransport, AsyncClient

from no_human import telemetry
from no_human.api.app import app
from no_human.blockers.cancel_pr_closeout import close_draft_pr_on_cancel
from no_human.core.db import Store
from no_human.core.task import Task, TaskStatus
from no_human.vcs import comment_poster
from no_human.vcs.task_pr import resolve_task_pr, task_pr_urls

pytestmark = pytest.mark.usefixtures("isolated_env_file")


class FakeForge:
    """Mock forge recording retitle and close operations."""

    def __init__(self, **initial_state):
        self.state = dict(initial_state)
        self.closes: list[str] = []
        self.titles: list[tuple[str, str]] = []

    def close_pr(self, url: str) -> dict[str, Any]:
        self.closes.append(url)
        self.state[url] = "closed"
        return {"ok": True, "error": ""}

    def set_pr_title(self, url: str, title: str) -> dict[str, Any]:
        self.titles.append((url, title))
        return {"ok": True, "error": ""}


class _StubCfg:
    primary_model = "claude-sonnet-4-6"
    review_model = "claude-sonnet-4-6"
    data: dict = {}
    db_path = None

    def get(self, key, default=None):
        return self.data.get(key, default)

    def __getitem__(self, key):
        return self.data[key]


@pytest_asyncio.fixture
async def client(store, tmp_path):
    from no_human.config import load_config
    app.state.store = store
    app.state.config = load_config(tmp_path / "config.yaml")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost") as c:
        yield c


def test_cli_task_cancel_retitles_and_closes_draft_pr(tmp_path, monkeypatch):
    import no_human.cli.commands as cmd_mod

    db_path = tmp_path / "test.db"
    url = "https://github.com/org/repo/pull/42"
    branch = "nh/task-1234"

    async def _seed():
        async with Store(db_path) as s:
            t = Task.new("Fix authentication bug", repo_path="/tmp/repo")
            t.external_id = "TASK-1234"
            t.context = {
                "pr_draft_created": url,
                "pr_draft_branch": branch,
            }
            await s.create_task(t)
            await s.set_status(t, TaskStatus.IMPLEMENTING, validate=False)
            return t.id

    task_id = asyncio.run(_seed())

    forge = FakeForge(**{url: "open"})
    monkeypatch.setattr(comment_poster, "close_pr", forge.close_pr)
    monkeypatch.setattr(comment_poster, "set_pr_title", forge.set_pr_title)
    monkeypatch.setattr(telemetry, "record", lambda *a, **k: None)

    cfg = _StubCfg()
    cfg.db_path = db_path
    monkeypatch.setattr(cmd_mod, "_server_owns_worker", lambda _cfg: False)
    monkeypatch.setattr(cmd_mod, "load_config", lambda: cfg)
    monkeypatch.setattr(cmd_mod, "assert_subscription_mode", lambda **kw: None)

    runner = CliRunner()
    result = runner.invoke(cmd_mod.cli, ["task", "cancel", task_id, "--reason", "no longer needed"],
                           catch_exceptions=False)

    assert result.exit_code == 0
    assert "cancelled" in result.output

    # 1. Forge retitled with prefix and closed
    assert forge.closes == [url]
    assert len(forge.titles) == 1
    t_url, title = forge.titles[0]
    assert t_url == url
    assert title.startswith("[ABANDONED — not delivered] ")
    assert "TASK-1234: Fix authentication bug" in title

    async def _verify():
        async with Store(db_path) as s:
            fresh = await s.find_task(task_id)
            assert fresh.status == TaskStatus.FAILED

            # 2. Bookkeeping: draft slot cleared, abandoned list updated
            assert "pr_draft_created" not in (fresh.context or {})
            assert "pr_draft_branch" not in (fresh.context or {})
            assert fresh.context.get("abandoned_pr_urls") == [url]

            # 3. One pr_draft_abandoned event recorded naming the reason
            events = await s.list_events(task_id)
            abandon_events = [e for e in events if e.get("kind") == "pr_draft_abandoned"]
            assert len(abandon_events) == 1
            ev = abandon_events[0]
            assert ev["pr_url"] == url
            assert ev["reason"] == "no longer needed"
            assert "no longer needed" in ev["text"]
            assert ev["retitle_ok"] is True
            assert ev["close_ok"] is True

            # 4. Resolver returns none
            resolved = await resolve_task_pr(s, fresh)
            assert resolved.source == "none"
            assert resolved.url == ""

    asyncio.run(_verify())


def test_cli_task_cancel_relabel_failed_closes_draft_pr(tmp_path, monkeypatch):
    """When a task is already FAILED, nh task cancel re-labels it and still closes the draft PR."""
    import no_human.cli.commands as cmd_mod

    db_path = tmp_path / "test.db"
    url = "https://github.com/org/repo/pull/43"
    branch = "nh/task-relabel"

    async def _seed():
        async with Store(db_path) as s:
            t = Task.new("Failed task to relabel", repo_path="/tmp/repo")
            t.context = {
                "pr_draft_created": url,
                "pr_draft_branch": branch,
            }
            await s.create_task(t)
            await s.set_status(t, TaskStatus.FAILED, validate=False)
            return t.id

    task_id = asyncio.run(_seed())

    forge = FakeForge(**{url: "open"})
    monkeypatch.setattr(comment_poster, "close_pr", forge.close_pr)
    monkeypatch.setattr(comment_poster, "set_pr_title", forge.set_pr_title)
    monkeypatch.setattr(telemetry, "record", lambda *a, **k: None)

    cfg = _StubCfg()
    cfg.db_path = db_path
    monkeypatch.setattr(cmd_mod, "_server_owns_worker", lambda _cfg: False)
    monkeypatch.setattr(cmd_mod, "load_config", lambda: cfg)
    monkeypatch.setattr(cmd_mod, "assert_subscription_mode", lambda **kw: None)

    runner = CliRunner()
    result = runner.invoke(cmd_mod.cli, ["task", "cancel", task_id, "--reason", "abandoning failed task"],
                           catch_exceptions=False)

    assert result.exit_code == 0
    assert "(was failed)" in result.output

    # Forge retitled and closed
    assert forge.closes == [url]
    assert len(forge.titles) == 1
    assert forge.titles[0][0] == url

    async def _verify():
        async with Store(db_path) as s:
            fresh = await s.find_task(task_id)
            assert "pr_draft_created" not in (fresh.context or {})
            assert fresh.context.get("abandoned_pr_urls") == [url]

            events = await s.list_events(task_id)
            abandon_events = [e for e in events if e.get("kind") == "pr_draft_abandoned"]
            assert len(abandon_events) == 1
            ev = abandon_events[0]
            assert ev["pr_url"] == url
            assert ev["reason"] == "abandoning failed task"
            assert ev["retitle_ok"] is True
            assert ev["close_ok"] is True

    asyncio.run(_verify())


@pytest.mark.asyncio
async def test_api_task_cancel_retitles_and_closes_draft_pr(client, store, monkeypatch):
    url = "https://github.com/org/repo/pull/99"
    branch = "nh/task-api-99"

    t = Task.new("Add telemetry endpoint", repo_path="/tmp/repo")
    t.context = {
        "pr_draft_created": url,
        "pr_draft_branch": branch,
    }
    await store.create_task(t)
    await store.set_status(t, TaskStatus.IMPLEMENTING, validate=False)

    forge = FakeForge(**{url: "open"})
    monkeypatch.setattr(comment_poster, "close_pr", forge.close_pr)
    monkeypatch.setattr(comment_poster, "set_pr_title", forge.set_pr_title)

    resp = await client.post(f"/api/tasks/{t.id}/cancel", json={"reason": "cancelled via board"})
    assert resp.status_code == 200

    assert forge.closes == [url]
    assert len(forge.titles) == 1
    t_url, title = forge.titles[0]
    assert t_url == url
    assert title == "[ABANDONED — not delivered] Add telemetry endpoint"

    fresh = await store.find_task(t.id)
    assert fresh.status == TaskStatus.FAILED
    assert "pr_draft_created" not in (fresh.context or {})
    assert "pr_draft_branch" not in (fresh.context or {})
    assert fresh.context.get("abandoned_pr_urls") == [url]

    events = await store.list_events(t.id)
    abandon_events = [e for e in events if e.get("kind") == "pr_draft_abandoned"]
    assert len(abandon_events) == 1
    assert abandon_events[0]["pr_url"] == url
    assert abandon_events[0]["reason"] == "cancelled via board"
    assert abandon_events[0]["retitle_ok"] is True
    assert abandon_events[0]["close_ok"] is True

    resolved = await resolve_task_pr(store, fresh)
    assert resolved.source == "none"
    assert resolved.url == ""


@pytest.mark.asyncio
async def test_resolver_and_task_pr_urls_after_cancel(store):
    url = "https://github.com/org/repo/pull/77"
    branch = "nh/task-resolver"

    t = Task.new("Resolver verification task", repo_path="/tmp/repo")
    t.context = {
        "pr_draft_created": url,
        "pr_draft_branch": branch,
    }
    await store.create_task(t)

    # Before cancel, resolver returns the draft PR
    pre_resolved = await resolve_task_pr(store, t)
    assert pre_resolved.source == "draft"
    assert pre_resolved.url == url

    # Closeout via helper
    fake_forge = FakeForge()
    abandoned = await close_draft_pr_on_cancel(
        store, t, reason="cancelled by user",
        close=fake_forge.close_pr, set_title=fake_forge.set_pr_title,
    )
    assert abandoned == url

    # After cancel, draft slot is cleared and abandoned_pr_urls contains url
    fresh = await store.find_task(t.id)
    assert "pr_draft_created" not in fresh.context
    assert fresh.context.get("abandoned_pr_urls") == [url]

    # Resolver no longer returns draft PR
    post_resolved = await resolve_task_pr(store, fresh)
    assert post_resolved.source == "none"
    assert post_resolved.url == ""

    # task_pr_urls filters out abandoned URLs
    urls = await task_pr_urls(store, fresh)
    assert url not in urls


@pytest.mark.asyncio
async def test_forge_error_leaves_task_cancelled_and_raises_nothing(store, monkeypatch):
    url = "https://github.com/org/repo/pull/13"
    t = Task.new("Error test task", repo_path="/tmp/repo")
    t.context = {
        "pr_draft_created": url,
        "pr_draft_branch": "nh/err-branch",
    }
    await store.create_task(t)

    def raising_title(*a, **k):
        raise RuntimeError("forge retitle failed: connection reset")

    def raising_close(*a, **k):
        raise RuntimeError("forge close failed: 500 internal server error")

    # Helper must not raise
    abandoned = await close_draft_pr_on_cancel(
        store, t, reason="timeout cancel",
        close=raising_close, set_title=raising_title,
    )
    assert abandoned == url

    # Bookkeeping is still persisted
    fresh = await store.find_task(t.id)
    assert "pr_draft_created" not in (fresh.context or {})
    assert fresh.context.get("abandoned_pr_urls") == [url]

    # Event is still recorded with error details
    events = await store.list_events(t.id)
    abandon_events = [e for e in events if e.get("kind") == "pr_draft_abandoned"]
    assert len(abandon_events) == 1
    assert abandon_events[0]["pr_url"] == url
    assert abandon_events[0]["retitle_ok"] is False
    assert "connection reset" in abandon_events[0]["retitle_error"]
    assert abandon_events[0]["close_ok"] is False
    assert "500 internal server error" in abandon_events[0]["close_error"]


@pytest.mark.asyncio
async def test_delivered_pr_is_not_closed_on_cancel(store):
    fake = FakeForge()
    url = "https://github.com/org/repo/pull/555"

    # Case 1: explicit pr_delivered_url
    t1 = Task.new("Delivered PR explicit", repo_path="/tmp/repo")
    t1.context = {
        "pr_draft_created": url,
        "pr_delivered_url": url,
    }
    await store.create_task(t1)
    res1 = await close_draft_pr_on_cancel(store, t1, reason="cancel", close=fake.close_pr, set_title=fake.set_pr_title)
    assert res1 == ""
    assert fake.closes == []
    assert fake.titles == []
    assert (t1.context or {}).get("pr_draft_created") == url

    # Case 2: pr_watch matches
    t2 = Task.new("Delivered PR watch", repo_path="/tmp/repo")
    t2.context = {
        "pr_draft_created": url,
        "pr_watch": url,
    }
    await store.create_task(t2)
    res2 = await close_draft_pr_on_cancel(store, t2, reason="cancel", close=fake.close_pr, set_title=fake.set_pr_title)
    assert res2 == ""
    assert fake.closes == []
    assert (t2.context or {}).get("pr_draft_created") == url

    # Case 3: pr_branch matches pr_draft_branch
    t3 = Task.new("Delivered PR branch match", repo_path="/tmp/repo")
    t3.context = {
        "pr_draft_created": url,
        "pr_draft_branch": "feature/branch-x",
        "pr_branch": "feature/branch-x",
    }
    await store.create_task(t3)
    res3 = await close_draft_pr_on_cancel(store, t3, reason="cancel", close=fake.close_pr, set_title=fake.set_pr_title)
    assert res3 == ""
    assert fake.closes == []
    assert (t3.context or {}).get("pr_draft_created") == url


@pytest.mark.asyncio
async def test_abandoned_urls_capped_at_six(store):
    fake = FakeForge()
    t = Task.new("Capping test", repo_path="/tmp/repo")
    initial_abandoned = [f"https://github.com/org/repo/pull/{i}" for i in range(1, 8)]
    url = "https://github.com/org/repo/pull/99"
    t.context = {
        "pr_draft_created": url,
        "abandoned_pr_urls": initial_abandoned,
    }
    await store.create_task(t)

    await close_draft_pr_on_cancel(store, t, reason="cancel", close=fake.close_pr, set_title=fake.set_pr_title)

    fresh = await store.find_task(t.id)
    abandoned = fresh.context.get("abandoned_pr_urls", [])
    assert len(abandoned) == 6
    assert abandoned[-1] == url


@pytest.mark.asyncio
async def test_commit_prefix_in_retitle(store):
    fake = FakeForge()
    url = "https://github.com/org/repo/pull/88"
    t = Task.new("Add feature", repo_path="/tmp/repo")
    t.external_id = "PRJ-88"
    t.context = {"pr_draft_created": url}
    await store.create_task(t)

    cfg = {"git": {"commit_prefix": "feat: "}}
    await close_draft_pr_on_cancel(
        store, t, reason="cancel", config=cfg,
        close=fake.close_pr, set_title=fake.set_pr_title,
    )

    assert len(fake.titles) == 1
    _, title = fake.titles[0]
    assert title == "[ABANDONED — not delivered] feat: PRJ-88: Add feature"


@pytest.mark.asyncio
async def test_split_cancels_parent_and_closes_draft_pr(client, store, monkeypatch):
    url = "https://github.com/org/repo/pull/777"
    parent = Task.new("Big thing to split", repo_path="/tmp/repo")
    parent.context = {
        "pr_draft_created": url,
        "pr_draft_branch": "nh/parent-branch",
    }
    await store.create_task(parent)

    forge = FakeForge(**{url: "open"})
    monkeypatch.setattr(comment_poster, "close_pr", forge.close_pr)
    monkeypatch.setattr(comment_poster, "set_pr_title", forge.set_pr_title)

    r = await client.post(f"/api/tasks/{parent.id}/split", json={"drafts": [
        {"title": "Sub 1", "description": "do 1"},
        {"title": "Sub 2", "description": "do 2"},
    ]})
    assert r.status_code == 201

    assert forge.closes == [url]
    assert len(forge.titles) == 1
    assert forge.titles[0][1].startswith("[ABANDONED — not delivered] ")

    fresh = await store.find_task(parent.id)
    assert fresh.status == TaskStatus.FAILED
    assert "pr_draft_created" not in (fresh.context or {})
    assert fresh.context.get("abandoned_pr_urls") == [url]


@pytest.mark.asyncio
async def test_event_records_forge_outcome_when_repo_gone(store):
    """When forge returns an error dict without raising, outcome and error are recorded in event."""
    url = "https://github.com/org/repo/pull/404"
    t = Task.new("Repo gone test", repo_path="/tmp/repo")
    t.context = {"pr_draft_created": url}
    await store.create_task(t)

    def failing_close(u):
        return {"ok": False, "error": "404 Not Found: repository deleted"}

    def ok_title(u, t):
        return {"ok": True, "error": ""}

    await close_draft_pr_on_cancel(
        store, t, reason="repo gone",
        close=failing_close, set_title=ok_title,
    )

    events = await store.list_events(t.id)
    abandon_events = [e for e in events if e.get("kind") == "pr_draft_abandoned"]
    assert len(abandon_events) == 1
    ev = abandon_events[0]
    assert ev["retitle_ok"] is True
    assert ev["retitle_error"] == ""
    assert ev["close_ok"] is False
    assert "404 Not Found" in ev["close_error"]


@pytest.mark.asyncio
async def test_concurrent_merge_context_survives_during_closeout(store):
    """A concurrent merge_context made while forge calls are in flight is not lost."""
    url = "https://github.com/org/repo/pull/50"
    t = Task.new("Race condition test", repo_path="/tmp/repo")
    t.context = {
        "pr_draft_created": url,
        "pr_draft_branch": "nh/branch-race",
        "initial_key": "initial_val",
    }
    await store.create_task(t)

    # The forge writes run in a worker thread, so the fake blocks on
    # threading events while the test writes to the store on the event loop.
    in_flight = threading.Event()
    proceed = threading.Event()

    def slow_set_title(u, title):
        in_flight.set()
        proceed.wait(timeout=10)
        return {"ok": True, "error": ""}

    def fake_close(u):
        return {"ok": True, "error": ""}

    closeout_task = asyncio.create_task(
        close_draft_pr_on_cancel(
            store, t, reason="cancel",
            close=fake_close, set_title=slow_set_title,
        )
    )

    assert await asyncio.to_thread(in_flight.wait, 10)
    # Concurrently write to context from another task while slow_set_title is suspended
    await store.merge_context(t.id, {"concurrent_worker_note": "preserved_data"})
    proceed.set()

    await closeout_task

    fresh = await store.find_task(t.id)
    assert "pr_draft_created" not in fresh.context
    assert fresh.context.get("abandoned_pr_urls") == [url]
    # The concurrent write must NOT have been clobbered!
    assert fresh.context.get("concurrent_worker_note") == "preserved_data"
    assert fresh.context.get("initial_key") == "initial_val"
    # In-memory t.context must also reflect the merged keys
    assert t.context.get("concurrent_worker_note") == "preserved_data"
    assert t.context.get("initial_key") == "initial_val"


@pytest.mark.asyncio
async def test_api_cancel_stops_and_kills_before_forge_call(client, store, monkeypatch):
    """The API cancel endpoint cancels and kills coder processes BEFORE touching the forge."""
    import sys
    app_mod = sys.modules["no_human.api.app"]

    url = "https://github.com/org/repo/pull/123"
    t = Task.new("Ordering test task", repo_path="/tmp/repo")
    t.context = {"pr_draft_created": url, "pr_draft_branch": "nh/order-test"}
    await store.create_task(t)
    await store.set_status(t, TaskStatus.IMPLEMENTING, validate=False)

    call_sequence: list[str] = []

    class FakeScheduler:
        inflight = set()

        def request_task_cancel(self, task_id, reason):
            call_sequence.append("request_task_cancel")
            return True

        def get_live_status(self, task_id):
            return None

    monkeypatch.setattr(app.state, "scheduler", FakeScheduler(), raising=False)

    async def fake_kill(task_id):
        call_sequence.append("kill_task_processes")
        return 1
    monkeypatch.setattr(app_mod, "_kill_task_processes", fake_kill)

    def fake_set_title(u, title):
        call_sequence.append("forge_set_title")
        # Assert cancellation and process killing have already happened before network call
        assert "request_task_cancel" in call_sequence
        assert "kill_task_processes" in call_sequence
        return {"ok": True, "error": ""}

    def fake_close(u):
        call_sequence.append("forge_close")
        return {"ok": True, "error": ""}

    monkeypatch.setattr(comment_poster, "set_pr_title", fake_set_title)
    monkeypatch.setattr(comment_poster, "close_pr", fake_close)

    resp = await client.post(f"/api/tasks/{t.id}/cancel", json={"reason": "cancelled test"})
    assert resp.status_code == 200

    assert call_sequence == [
        "request_task_cancel",
        "kill_task_processes",
        "forge_set_title",
        "forge_close",
    ]


@pytest.mark.asyncio
async def test_non_http_draft_url_is_retired_without_forge_calls(store):
    """A draft slot that does not name an http(s) URL gets no forge writes,
    but its bookkeeping and event are still written, with the reason the
    forge was not attempted."""
    fake = FakeForge()
    t = Task.new("Local-only draft", repo_path="/tmp/repo")
    t.context = {"pr_draft_created": "local:draft-1",
                 "pr_draft_branch": "nh/local-1"}
    await store.create_task(t)

    out = await close_draft_pr_on_cancel(
        store, t, reason="cancel", close=fake.close_pr, set_title=fake.set_pr_title)

    assert out == "local:draft-1"
    assert fake.titles == [] and fake.closes == []
    fresh = await store.find_task(t.id)
    assert "pr_draft_created" not in fresh.context
    assert "pr_draft_branch" not in fresh.context
    assert fresh.context.get("abandoned_pr_urls") == ["local:draft-1"]
    events = [e for e in await store.list_events(t.id)
              if e.get("kind") == "pr_draft_abandoned"]
    assert len(events) == 1
    ev = events[0]
    assert ev["retitle_ok"] is False and ev["retitle_error"] == "non-http URL"
    assert ev["close_ok"] is False and ev["close_error"] == "non-http URL"


@pytest.mark.asyncio
async def test_a_non_dict_forge_result_is_recorded_as_a_failure(store):
    """Only a dict with a truthy ``ok`` counts as success; anything else the
    forge returns is recorded as a failed write, not silently as success."""
    url = "https://github.com/org/repo/pull/61"
    t = Task.new("Odd forge result", repo_path="/tmp/repo")
    t.context = {"pr_draft_created": url}
    await store.create_task(t)

    await close_draft_pr_on_cancel(
        store, t, reason="cancel",
        close=lambda u: None, set_title=lambda u, title: "done")

    ev, = [e for e in await store.list_events(t.id)
           if e.get("kind") == "pr_draft_abandoned"]
    assert ev["retitle_ok"] is False
    assert ev["retitle_error"] == "unexpected str result"
    assert ev["close_ok"] is False
    assert ev["close_error"] == "unexpected NoneType result"


@pytest.mark.asyncio
async def test_orchestrator_abandon_titles_with_the_configured_commit_prefix(
    store, tmp_path, monkeypatch,
):
    """`_abandon_draft_pr` passes the orchestrator's config to the helper, so
    the abandoned title carries the funnel's commit prefix exactly as
    `_commit_message` builds it."""
    from no_human.config import load_config
    from no_human.core.orchestrator import Orchestrator
    from no_human.notify.slack import SlackNotifier

    class _Backend:
        async def run(self, *a, **k):  # pragma: no cover
            raise AssertionError("backend should not run here")

    cfg = load_config(tmp_path / "config.yaml")
    cfg.data["git"]["commit_prefix"] = "feat: "
    orch = Orchestrator(store, cfg.data, _Backend(), SlackNotifier(None))

    forge = FakeForge()
    monkeypatch.setattr(comment_poster, "close_pr", forge.close_pr)
    monkeypatch.setattr(comment_poster, "set_pr_title", forge.set_pr_title)

    url = "https://github.com/org/repo/pull/314"
    t = Task.new("Add feature", repo_path="/tmp/repo")
    t.external_id = "PRJ-314"
    t.context = {"pr_draft_created": url, "pr_draft_branch": "nh/a-1"}
    await store.create_task(t)

    out = await orch._abandon_draft_pr(t, "attempt failed", reason_from_agent=False)

    assert out == url
    assert forge.titles == [
        (url, "[ABANDONED — not delivered] " + orch._commit_message(t))]
    assert forge.titles[0][1] == "[ABANDONED — not delivered] feat: PRJ-314: Add feature"
    assert forge.closes == [url]


async def _asgi_post(path: str, on_response_done) -> None:
    """Drive the app with one POST and call ``on_response_done`` the moment
    the full response has been sent — before any background work the app
    runs afterwards. (httpx's ASGITransport returns only once the whole ASGI
    call, background tasks included, has finished, so it cannot see this.)"""
    body = b'{"reason": "slow forge"}' if path.endswith("/cancel") else (
        b'{"drafts": [{"title": "Sub 1", "description": "do 1"},'
        b' {"title": "Sub 2", "description": "do 2"}]}')
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "POST", "scheme": "http", "path": path,
        "raw_path": path.encode(), "query_string": b"", "root_path": "",
        "headers": [(b"host", b"localhost"),
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode())],
        "client": ("127.0.0.1", 50000), "server": ("localhost", 80),
    }
    delivered = False

    async def receive():
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        await asyncio.Event().wait()  # the client never disconnects

    async def send(message):
        if message["type"] == "http.response.body" and not message.get("more_body"):
            on_response_done()

    await app(scope, receive, send)


class _BlockingForge(FakeForge):
    """A forge whose retitle blocks until released — a slow or unreachable
    forge. The writes run in a worker thread, so a threading event is used."""

    def __init__(self, sequence: list[str]):
        super().__init__()
        self.sequence = sequence
        self.release = threading.Event()

    def set_pr_title(self, url, title):
        self.sequence.append("forge_set_title")
        self.release.wait(timeout=10)
        return super().set_pr_title(url, title)


async def _assert_response_does_not_wait_on_the_forge(
    store, monkeypatch, task, path, expect_broadcast,
):
    import sys
    app_mod = sys.modules["no_human.api.app"]

    sequence: list[str] = []

    async def fake_broadcast(msg):
        sequence.append(f"broadcast:{msg.get('type')}")

    monkeypatch.setattr(app_mod._mgr, "broadcast", fake_broadcast)
    forge = _BlockingForge(sequence)
    monkeypatch.setattr(comment_poster, "close_pr", forge.close_pr)
    monkeypatch.setattr(comment_poster, "set_pr_title", forge.set_pr_title)

    responded = asyncio.Event()

    call = asyncio.create_task(_asgi_post(path, responded.set))
    try:
        # The forge is held for the whole wait, so the response can only
        # arrive if it does not wait on the forge.
        await asyncio.wait_for(responded.wait(), timeout=5)
        assert forge.closes == []
    finally:
        forge.release.set()
        await asyncio.wait_for(call, timeout=15)

    url = task.context["pr_draft_created"]
    assert forge.closes == [url]
    # The broadcast goes out before the forge is touched. (The response and
    # the start of the background close-out can interleave — the http
    # middleware forwards the body while the background task starts — so
    # what is pinned for the response is that it arrived while the forge was
    # still held, above.)
    assert expect_broadcast in sequence
    assert sequence.index(expect_broadcast) < sequence.index("forge_set_title")
    fresh = await store.find_task(task.id)
    assert fresh.status == TaskStatus.FAILED
    assert fresh.context.get("abandoned_pr_urls") == [url]


@pytest.mark.asyncio
async def test_api_cancel_responds_and_broadcasts_before_a_slow_forge(
    client, store, monkeypatch,
):
    t = Task.new("Slow forge cancel", repo_path="/tmp/repo")
    t.context = {"pr_draft_created": "https://github.com/org/repo/pull/901",
                 "pr_draft_branch": "nh/slow-1"}
    await store.create_task(t)
    await store.set_status(t, TaskStatus.IMPLEMENTING, validate=False)

    import sys
    async def no_kill(task_id):
        return 1
    monkeypatch.setattr(sys.modules["no_human.api.app"], "_kill_task_processes", no_kill)

    await _assert_response_does_not_wait_on_the_forge(
        store, monkeypatch, t, f"/api/tasks/{t.id}/cancel",
        expect_broadcast="broadcast:task_updated")


@pytest.mark.asyncio
async def test_api_split_creates_children_and_responds_before_a_slow_forge(
    client, store, monkeypatch,
):
    parent = Task.new("Slow forge split", repo_path="/tmp/repo")
    parent.context = {"pr_draft_created": "https://github.com/org/repo/pull/902",
                      "pr_draft_branch": "nh/slow-2"}
    await store.create_task(parent)

    await _assert_response_does_not_wait_on_the_forge(
        store, monkeypatch, parent, f"/api/tasks/{parent.id}/split",
        expect_broadcast="broadcast:task_split")
    children = [x for x in await store.list_tasks() if x.parent_id == parent.id]
    assert len(children) == 2
