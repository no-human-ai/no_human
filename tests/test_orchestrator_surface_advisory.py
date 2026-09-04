"""SCRUM-24: the orchestrator attaches a non-blocking multi-surface advisory
to task.context and a task event when a plan spans >=2 surfaces, and does
neither for a single-surface plan."""


from no_human.config import load_config
from no_human.core.events import EventPersister
from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task, TaskSpec
from no_human.notify.slack import SlackNotifier

TWO_SURFACE_PLAN = """
## FILES TO CHANGE/CREATE

- src/x.py
- web/y.js
"""

ONE_SURFACE_PLAN = """
## FILES TO CHANGE/CREATE

- src/x.py
- src/y.py
"""


class _Backend:
    async def run(self, *a, **k):  # pragma: no cover
        raise AssertionError("backend should not run here")


def _orch(store, tmp_path, sink=None):
    cfg = load_config(tmp_path / "config.yaml")
    return Orchestrator(store, cfg.data, _Backend(), SlackNotifier(None), event_sink=sink)


async def test_two_surface_spec_emits_advisory_and_context(store, tmp_path, monkeypatch):
    async def fake_generate(task, files_to_change=None, surfaces=None, **kw):
        return "Split proposal:\n\n1. Part one\nDo it.\nContract: c1"

    monkeypatch.setattr(
        "no_human.core.orchestrator.generate_split_proposal", fake_generate
    )

    t = Task.new("multi-surface task", repo_path="/r")
    t.acceptance_criteria = ["keep me"]
    await store.create_task(t)
    status_before = t.status

    async with EventPersister(store, t.id, interval=0.01) as persister:
        orch = _orch(store, tmp_path, sink=persister.record)
        spec = TaskSpec.from_plan(TWO_SURFACE_PLAN)
        ctx = t.context or {}
        await orch._apply_surface_advisory(ctx, spec, t)
        t.context = ctx
        await store.update_task(t)

    got = await store.get_task(t.id)
    assert "surface_advisory" in got.context
    assert "backend" in got.context["surface_advisory"]
    assert "frontend" in got.context["surface_advisory"]
    # SCRUM-36: the split proposal is attached alongside the advisory.
    assert "split_proposal" in got.context

    events = await store.list_events(t.id)
    surface_events = [e for e in events if e["kind"] == "surface_advisory"]
    assert len(surface_events) == 1
    assert "backend" in surface_events[0]["text"]
    assert [e for e in events if e["kind"] == "split_proposal"]

    # advisory-only: no gating, no status change, no criteria mutation.
    assert got.status == status_before
    assert got.acceptance_criteria == ["keep me"]
    # doctor.py's advisory_degradations mechanism must NOT see this event.
    assert not [e for e in events if e["kind"] == "advisory"]


async def test_one_surface_spec_no_advisory(store, tmp_path):
    t = Task.new("single-surface task", repo_path="/r")
    await store.create_task(t)

    async with EventPersister(store, t.id, interval=0.01) as persister:
        orch = _orch(store, tmp_path, sink=persister.record)
        spec = TaskSpec.from_plan(ONE_SURFACE_PLAN)
        ctx = t.context or {}
        await orch._apply_surface_advisory(ctx, spec, t)
        t.context = ctx
        await store.update_task(t)

    got = await store.get_task(t.id)
    assert "surface_advisory" not in got.context
    assert "split_proposal" not in got.context

    events = await store.list_events(t.id)
    assert not [e for e in events if e["kind"] == "surface_advisory"]
    assert not [e for e in events if e["kind"] == "split_proposal"]
