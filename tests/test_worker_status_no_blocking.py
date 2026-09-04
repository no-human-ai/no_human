"""`/api/worker/status` must never block the event loop.

Motivating incident (2026-09-03): the endpoint's own body is pure memory
except one line — `loaded_code_stale` used to be measured inline via
`_loaded_code_stale`, whose single-flight winner runs `git rev-parse HEAD`
(and `git merge-base --is-ancestor` too, once behind) synchronously under
`asyncio.to_thread`. Live samples: 13.9s then 0.044s (lock winner under load,
then a lock loser / same-HEAD fast return); 5.5s another poll. The fix moves
the measurement to a background refresher; the request path only reads the
last answer from `_stale_cache`. These tests pin that the handler makes no
subprocess call and no thread-pool hop, and stays sub-second even when git
is slow or the executor is saturated.
"""
from __future__ import annotations

import asyncio
import importlib
import time

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

from no_human.api.app import app

pytestmark = pytest.mark.usefixtures("isolated_env_file")


@pytest_asyncio.fixture
async def client(store, tmp_path):
    from no_human.config import load_config
    app.state.store = store
    app.state.config = load_config(tmp_path / "config.yaml")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost") as c:
        yield c


async def test_status_handler_makes_no_subprocess_call(client, monkeypatch):
    """AC #3. The handler must not fork git — the mechanism behind the
    13.9s/5.5s live samples."""
    build_info = importlib.import_module("no_human.core.build_info")
    api = importlib.import_module("no_human.api.app")

    def _forked(*args, **kwargs):
        pytest.fail("the status handler forked a subprocess")

    monkeypatch.setattr(build_info.subprocess, "run", _forked)
    monkeypatch.setattr(api, "_stale_cache", ("a" * 40, "behind"))

    r = await client.get("/api/worker/status")
    assert r.status_code == 200
    assert r.json()["loaded_code_stale"] == "behind"

    # Positive control: the patch DOES intercept a real subprocess call, so
    # the assertion above cannot be passing merely because the patch missed
    # its target.
    with pytest.raises(pytest.fail.Exception):
        api._loaded_code_stale()


async def test_status_handler_does_not_hop_the_thread_pool(client, monkeypatch):
    """Even a cheap `to_thread` hop queues behind a saturated executor
    (app.py has ~30 sites with multi-second timeouts) — the request path
    must not use one at all."""
    api = importlib.import_module("no_human.api.app")

    async def _hopped(*args, **kwargs):
        pytest.fail("the status handler hopped the thread pool")

    monkeypatch.setattr(api.asyncio, "to_thread", _hopped)

    r = await client.get("/api/worker/status")
    assert r.status_code == 200


async def test_status_is_subsecond_while_git_is_slow(client, monkeypatch):
    """Direct regression pin for the 13.9s-then-0.044s live sample: even
    with git measurements stubbed to take 5s each, the handler (which no
    longer calls them) must answer both consecutive polls in well under 1s,
    and a concurrently running heartbeat coroutine must never see the event
    loop go quiet for more than 0.25s."""
    build_info = importlib.import_module("no_human.core.build_info")
    api = importlib.import_module("no_human.api.app")

    def _slow_head(*args, **kwargs):
        time.sleep(5)
        return "b" * 40

    def _slow_note(*args, **kwargs):
        time.sleep(5)
        return "loaded code aaaaaaaa is behind HEAD bbbbbbbb"

    monkeypatch.setattr(build_info, "head_sha", _slow_head)
    monkeypatch.setattr(build_info, "staleness_note", _slow_note)
    monkeypatch.setattr(api, "_stale_cache", ("a" * 40, "current"))

    gaps: list[float] = []
    stop = asyncio.Event()

    async def _heartbeat():
        last = time.perf_counter()
        while not stop.is_set():
            await asyncio.sleep(0.01)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now

    hb = asyncio.create_task(_heartbeat())
    try:
        for _ in range(2):
            start = time.perf_counter()
            r = await client.get("/api/worker/status")
            elapsed = time.perf_counter() - start
            assert r.status_code == 200
            assert elapsed < 1.0, f"status took {elapsed:.3f}s"
    finally:
        stop.set()
        await hb

    assert gaps, "heartbeat never ran"
    assert max(gaps) < 0.25, f"event loop stalled for {max(gaps):.3f}s"


async def test_status_is_subsecond_with_the_executor_saturated(client):
    """Second, weaker mechanism from the plan: even without the git call, a
    saturated default executor would delay any `to_thread` hop. With the hop
    removed from the request path entirely this cannot touch the handler."""
    loop = asyncio.get_running_loop()
    futures = [loop.run_in_executor(None, time.sleep, 3) for _ in range(40)]
    try:
        start = time.perf_counter()
        r = await client.get("/api/worker/status")
        elapsed = time.perf_counter() - start
        assert r.status_code == 200
        assert elapsed < 1.0, f"status took {elapsed:.3f}s"
    finally:
        await asyncio.gather(*futures)


async def test_the_refresher_updates_the_cache_and_the_handler_serves_it(
        client, monkeypatch):
    """The fix must not turn the flag into a permanently frozen value: the
    refresher's one measurement (`await asyncio.to_thread(_loaded_code_stale)`
    — the exact call `_refresh_stale_note` makes each tick) re-keys the cache,
    and the next poll serves the new note."""
    build_info = importlib.import_module("no_human.core.build_info")
    api = importlib.import_module("no_human.api.app")

    new_head = "b" * 40
    new_note = "loaded code aaaaaaaa is behind HEAD bbbbbbbb"

    monkeypatch.setattr(api, "_stale_cache", ("a" * 40, None))
    monkeypatch.setattr(build_info, "head_sha", lambda *a, **k: new_head)
    monkeypatch.setattr(build_info, "staleness_note", lambda *a, **k: new_note)

    await asyncio.to_thread(api._loaded_code_stale)

    assert api._stale_cache == (new_head, new_note)

    r = await client.get("/api/worker/status")
    assert r.json()["loaded_code_stale"] == new_note


async def test_a_cold_cache_reads_as_no_answer_not_as_current(client, monkeypatch):
    """Matches the existing documented cold-miss silence: `None` renders as
    "no banner", not "current"."""
    api = importlib.import_module("no_human.api.app")
    monkeypatch.setattr(api, "_stale_cache", None)

    r = await client.get("/api/worker/status")
    body = r.json()
    assert body["loaded_code_stale"] is None
    assert "loaded_code_stale" in body
