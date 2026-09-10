"""A scheduler that genuinely cannot hold the pool lease must be VISIBLE
without reading a log file.

Incident this closes: before this fix, `Scheduler._lease_lost` was tracked
internally (`health_snapshot()` already exposed it as `idle_reason ==
"lease_lost"`) but nothing a human or an agent actually polls — `nh status`,
`/api/worker/status`'s `healthy` flag, `/api/queue/health` — ever looked at
it. A stopped/unleased scheduler read exactly like a healthy, idle pool with
free worker slots.

These tests drive the actual surfaces (an HTTP client hitting the real
FastAPI app, and a direct call to `queue_health()`) rather than reading the
code, per the acceptance criterion's own wording.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from no_human.api.app import app
from no_human.core.health import queue_health
from no_human.core.scheduler import Scheduler

pytestmark = pytest.mark.usefixtures("isolated_env_file")


class _NeverRunOrch:
    async def run_task(self, task):  # pragma: no cover - dispatch must not run
        raise AssertionError("dispatch must not run in these tests")


def _leased_out_sched(store):
    """A real `Scheduler`, already past its startup claim, whose lease has
    since been lost — the same shape `tick()` itself sets via
    `_lease_lost` (see `test_scheduler_lease_fail_closed.py`'s
    `test_health_snapshot_reports_lease_lost`), so this reflects a genuine
    post-loss state rather than an invented field."""
    sched = Scheduler(store, lambda task=None: _NeverRunOrch(), max_workers=4)
    sched._lease_lost = "the CAS write kept hitting a transient DB error after 3 attempt(s)"
    return sched


@pytest_asyncio.fixture
async def client(store, tmp_path):
    from no_human.config import load_config
    app.state.store = store
    app.state.config = load_config(tmp_path / "config.yaml")
    app.state.scheduler = _leased_out_sched(store)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost") as c:
        yield c


async def test_worker_status_reports_unhealthy_when_the_lease_is_lost(client):
    """Driving `/api/worker/status`, not reading the code: with a real,
    genuinely lease-lost `Scheduler` wired up, the endpoint that `nh status`
    and any external monitor polls must say `healthy: false` — never the
    free-worker-slots reading the incident actually produced."""
    r = await client.get("/api/worker/status")
    assert r.status_code == 200
    body = r.json()
    assert body["running"] is True
    assert body["lease_lost"], "the field must be populated, not silently dropped"
    assert body["healthy"] is False, (
        "a lease-lost scheduler must not read as healthy just because "
        "nothing is currently ticking/dispatching")


async def test_queue_health_reports_paused_lease_lost_over_http(client, store):
    """Driving `/api/queue/health`: with a task actually queued (so a
    healthy pool would show busy workers or a nonzero backlog, not silence),
    the lease-lost pool must report `paused: true, paused_reason:
    "lease_lost"` — this is the field `nh status`/`pool_probe.py` reads to
    print a stopped queue instead of `working 0/4`."""
    from no_human.core.task import Task

    t = Task.new("t-lease-lost-visibility", repo_path="/r")
    await store.create_task(t)

    r = await client.get("/api/queue/health")
    assert r.status_code == 200
    body = r.json()
    assert body["paused"] is True
    assert body["paused_reason"] == "lease_lost"


async def test_worker_status_is_healthy_once_the_lease_is_intact(client):
    """Negative control for the two tests above: the SAME endpoint, with
    the lease lost cleared, must go back to reporting healthy — proving
    `healthy: false` above is caused by `lease_lost` specifically and not
    by some other artifact of this fixture (e.g. `max_workers=4` with no
    real dispatch loop running)."""
    app.state.scheduler._lease_lost = None
    r = await client.get("/api/worker/status")
    body = r.json()
    assert not body.get("lease_lost")
    assert body["healthy"] is True


async def test_queue_health_direct_call_reports_paused_lease_lost(store):
    """Direct unit-level pin (mirrors this repo's existing
    `tests/test_queue_health.py` style of calling `queue_health()`
    directly) for the same behavior, independent of the HTTP/app wiring
    above: `lease_lost` must win over the normal empty-queue early return,
    so a caller sees "stopped" even with nothing currently queued."""
    h = await queue_health(store, max_workers=4, lease_lost="simulated lease loss")
    assert h.open_tasks == 0
    assert h.paused is True
    assert h.paused_reason == "lease_lost"
    d = h.as_dict()
    assert d["paused"] is True
    assert d["paused_reason"] == "lease_lost"


async def test_queue_health_direct_call_is_not_paused_when_lease_is_fine(store):
    """Negative control for the direct-call test above."""
    h = await queue_health(store, max_workers=4)
    assert h.paused is False
    assert h.paused_reason is None
