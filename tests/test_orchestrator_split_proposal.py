"""SCRUM-34: `_raise_blocker` is the single funnel both the orchestrator-raised
(over-size) and agent-raised (parse_blocker) SCOPE_EXPLOSION paths go through.
On that category it attaches a non-binding split proposal to task.context,
appends it to the blocker's evidence, and emits it under its own
``split_proposal`` event kind — never mutating scope/criteria or creating a
task, and never doing any of that for other blocker categories."""

import logging


from no_human.blockers import Blocker, BlockerCategory, parse_blocker
from no_human.config import load_config
from no_human.core.events import EventPersister
from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task
from no_human.notify.slack import SlackNotifier

PROPOSAL = "Split proposal:\n\n1. Part one\nDo the first part.\nContract: c1"

AGENT_BLOCKER_TEXT = """
Ran into trouble.

BLOCKER_JSON_START
{
  "category": "SCOPE_EXPLOSION",
  "transient": false,
  "confidence": 0.9,
  "root_cause_hypothesis": "change is too large for one PR",
  "evidence": "diff touches 40 files",
  "goal": "implement the feature",
  "question": "split into smaller PRs?"
}
BLOCKER_JSON_END
"""


class _Backend:
    async def run(self, *a, **k):  # pragma: no cover
        raise AssertionError("backend should not run here")


def _orch(store, tmp_path, sink=None):
    cfg = load_config(tmp_path / "config.yaml")
    return Orchestrator(store, cfg.data, _Backend(), SlackNotifier(None), event_sink=sink)


async def _new_task(store, title="oversized task"):
    t = Task.new(title, repo_path="/r")
    t.acceptance_criteria = ["keep me"]
    await store.create_task(t)
    return t


def _scope_blocker(evidence="orig"):
    return Blocker(
        category=BlockerCategory.SCOPE_EXPLOSION,
        transient=False, confidence=0.9, goal="implement the feature",
        root_cause_hypothesis="too large", evidence=evidence,
        question="split into smaller PRs?",
    )


async def test_scope_explosion_attaches_context_event_evidence(
    store, tmp_path, monkeypatch
):
    calls = []

    async def fake_generate(task, files_to_change=None, surfaces=None, **kw):
        calls.append((task.id, files_to_change, surfaces))
        return PROPOSAL

    monkeypatch.setattr(
        "no_human.core.orchestrator.generate_split_proposal", fake_generate
    )

    t = await _new_task(store)
    blocker = _scope_blocker(evidence="orig")

    async with EventPersister(store, t.id, interval=0.01) as persister:
        orch = _orch(store, tmp_path, sink=persister.record)
        await orch._raise_blocker(t, blocker)

    got = await store.get_task(t.id)
    assert got.context.get("split_proposal") == PROPOSAL
    assert PROPOSAL in blocker.evidence
    assert "orig" in blocker.evidence

    events = await store.list_events(t.id)
    sp_events = [e for e in events if e["kind"] == "split_proposal"]
    assert len(sp_events) == 1
    assert sp_events[0]["text"] == PROPOSAL
    assert calls  # the generator was actually invoked


async def test_agent_raised_path_same_hook(store, tmp_path, monkeypatch):
    async def fake_generate(task, files_to_change=None, surfaces=None, **kw):
        return PROPOSAL

    monkeypatch.setattr(
        "no_human.core.orchestrator.generate_split_proposal", fake_generate
    )

    t = await _new_task(store)
    blocker = parse_blocker(AGENT_BLOCKER_TEXT)
    assert blocker is not None
    assert blocker.category == BlockerCategory.SCOPE_EXPLOSION

    async with EventPersister(store, t.id, interval=0.01) as persister:
        orch = _orch(store, tmp_path, sink=persister.record)
        await orch._raise_blocker(t, blocker)

    got = await store.get_task(t.id)
    assert got.context.get("split_proposal") == PROPOSAL
    assert PROPOSAL in blocker.evidence

    events = await store.list_events(t.id)
    assert [e for e in events if e["kind"] == "split_proposal"]


async def test_non_scope_category_no_proposal(store, tmp_path, monkeypatch):
    called = []

    async def fake_generate(*a, **k):
        called.append(True)
        return PROPOSAL

    monkeypatch.setattr(
        "no_human.core.orchestrator.generate_split_proposal", fake_generate
    )

    t = await _new_task(store)
    blocker = Blocker(
        category=BlockerCategory.TRANSIENT_INFRA,
        transient=True, confidence=0.9, goal="g",
        root_cause_hypothesis="network blip", evidence="conn refused",
    )

    async with EventPersister(store, t.id, interval=0.01) as persister:
        orch = _orch(store, tmp_path, sink=persister.record)
        await orch._raise_blocker(t, blocker)

    assert not called
    got = await store.get_task(t.id)
    assert "split_proposal" not in got.context
    events = await store.list_events(t.id)
    assert not [e for e in events if e["kind"] == "split_proposal"]


async def test_no_task_mutation(store, tmp_path, monkeypatch):
    async def fake_generate(task, files_to_change=None, surfaces=None, **kw):
        return PROPOSAL

    monkeypatch.setattr(
        "no_human.core.orchestrator.generate_split_proposal", fake_generate
    )

    t = await _new_task(store, title="do not change me")
    before_title, before_desc, before_criteria = (
        t.title, t.description, list(t.acceptance_criteria))
    before_count = len(await store.list_tasks())

    blocker = _scope_blocker()

    async with EventPersister(store, t.id, interval=0.01) as persister:
        orch = _orch(store, tmp_path, sink=persister.record)
        await orch._raise_blocker(t, blocker)

    assert t.title == before_title
    assert t.description == before_desc
    assert t.acceptance_criteria == before_criteria
    after_count = len(await store.list_tasks())
    assert after_count == before_count


async def test_generator_none_no_event(store, tmp_path, monkeypatch):
    async def fake_generate(*a, **k):
        return None

    monkeypatch.setattr(
        "no_human.core.orchestrator.generate_split_proposal", fake_generate
    )

    t = await _new_task(store)
    blocker = _scope_blocker(evidence="orig")

    async with EventPersister(store, t.id, interval=0.01) as persister:
        orch = _orch(store, tmp_path, sink=persister.record)
        await orch._raise_blocker(t, blocker)

    got = await store.get_task(t.id)
    assert "split_proposal" not in got.context
    assert blocker.evidence == "orig"
    events = await store.list_events(t.id)
    assert not [e for e in events if e["kind"] == "split_proposal"]


async def test_existing_proposal_in_context_blocks_regeneration(
    store, tmp_path, monkeypatch
):
    """SCRUM-36: dedupe is global per task — a scope-explosion blocker must
    not overwrite a proposal a different trigger already attached.
    SCRUM-39: the repeat escalation's evidence must still carry the
    existing proposal even though regeneration is skipped."""
    calls = []

    async def fake_generate(*a, **k):
        calls.append(True)
        return "SHOULD NOT BE STORED"

    monkeypatch.setattr(
        "no_human.core.orchestrator.generate_split_proposal", fake_generate
    )

    t = await _new_task(store)
    t.context = {"split_proposal": "EXISTING PROPOSAL"}
    await store.update_task(t)
    blocker = _scope_blocker(evidence="orig")

    async with EventPersister(store, t.id, interval=0.01) as persister:
        orch = _orch(store, tmp_path, sink=persister.record)
        await orch._raise_blocker(t, blocker)

    got = await store.get_task(t.id)
    assert got.context["split_proposal"] == "EXISTING PROPOSAL"
    assert not calls
    assert "EXISTING PROPOSAL" in blocker.evidence
    assert "--- Split Proposal ---" in blocker.evidence
    assert "orig" in blocker.evidence
    events = await store.list_events(t.id)
    assert not [e for e in events if e["kind"] == "split_proposal"]


async def test_repeat_scope_explosion_evidence_includes_existing_proposal(
    store, tmp_path, monkeypatch
):
    """SCRUM-39: a repeat SCOPE_EXPLOSION escalation must append the
    already-attached proposal to the new blocker's evidence, without
    regenerating it (mutation-detectable: a regeneration-based fix would
    leak the sentinel below into evidence)."""
    calls = []

    async def fake_generate(*a, **k):
        calls.append(True)
        return "SHOULD NOT REGENERATE"

    monkeypatch.setattr(
        "no_human.core.orchestrator.generate_split_proposal", fake_generate
    )

    t = await _new_task(store)
    t.context = {"split_proposal": "EXISTING PROPOSAL"}
    await store.update_task(t)
    blocker = _scope_blocker(evidence="diff touches 40 files")

    async with EventPersister(store, t.id, interval=0.01) as persister:
        orch = _orch(store, tmp_path, sink=persister.record)
        await orch._raise_blocker(t, blocker)

    assert not calls
    assert "EXISTING PROPOSAL" in blocker.evidence
    assert "--- Split Proposal ---" in blocker.evidence
    assert "diff touches 40 files" in blocker.evidence
    assert "SHOULD NOT REGENERATE" not in blocker.evidence


async def test_scope_explosion_populates_files_from_spec(store, tmp_path, monkeypatch):
    """SCRUM-38: the files section must be populated from ctx['spec']
    ['files_to_change'] — the only key the plan path actually writes."""
    calls = []

    async def fake_generate(task, files_to_change=None, surfaces=None, **kw):
        calls.append(files_to_change)
        return PROPOSAL

    monkeypatch.setattr(
        "no_human.core.orchestrator.generate_split_proposal", fake_generate
    )

    t = await _new_task(store)
    t.context = {
        "spec": {
            "files_to_change": [
                "src/no_human/core/orchestrator.py",
                "src/no_human/intake/split_proposal.py",
            ]
        }
    }
    await store.update_task(t)
    blocker = _scope_blocker(evidence="orig")

    async with EventPersister(store, t.id, interval=0.01) as persister:
        orch = _orch(store, tmp_path, sink=persister.record)
        await orch._raise_blocker(t, blocker)

    assert calls
    assert calls[0] == [
        "src/no_human/core/orchestrator.py",
        "src/no_human/intake/split_proposal.py",
    ]


async def test_no_spec_omits_files_section(store, tmp_path, monkeypatch):
    """No spec in context: files_to_change stays None (current behavior)."""
    calls = []

    async def fake_generate(task, files_to_change=None, surfaces=None, **kw):
        calls.append(files_to_change)
        return PROPOSAL

    monkeypatch.setattr(
        "no_human.core.orchestrator.generate_split_proposal", fake_generate
    )

    t = await _new_task(store)
    blocker = _scope_blocker(evidence="orig")

    async with EventPersister(store, t.id, interval=0.01) as persister:
        orch = _orch(store, tmp_path, sink=persister.record)
        await orch._raise_blocker(t, blocker)

    assert calls
    assert calls[0] is None


async def test_spec_with_blank_entries_omits(store, tmp_path, monkeypatch):
    """Whitespace-only entries don't count as a populated file list."""
    calls = []

    async def fake_generate(task, files_to_change=None, surfaces=None, **kw):
        calls.append(files_to_change)
        return PROPOSAL

    monkeypatch.setattr(
        "no_human.core.orchestrator.generate_split_proposal", fake_generate
    )

    t = await _new_task(store)
    t.context = {"spec": {"files_to_change": ["", "  "]}}
    await store.update_task(t)
    blocker = _scope_blocker(evidence="orig")

    async with EventPersister(store, t.id, interval=0.01) as persister:
        orch = _orch(store, tmp_path, sink=persister.record)
        await orch._raise_blocker(t, blocker)

    assert calls
    assert calls[0] is None


async def test_generator_exception_skips_and_logs_typename(
    store, tmp_path, monkeypatch, caplog
):
    async def fake_generate(*a, **k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(
        "no_human.core.orchestrator.generate_split_proposal", fake_generate
    )

    t = await _new_task(store)
    blocker = _scope_blocker(evidence="orig")

    async with EventPersister(store, t.id, interval=0.01) as persister:
        orch = _orch(store, tmp_path, sink=persister.record)
        with caplog.at_level(logging.WARNING, logger="no_human.orchestrator"):
            outcome = await orch._raise_blocker(t, blocker)

    assert outcome is not None
    got = await store.get_task(t.id)
    assert "split_proposal" not in got.context
    events = await store.list_events(t.id)
    assert not [e for e in events if e["kind"] == "split_proposal"]
    assert "RuntimeError" in caplog.text
