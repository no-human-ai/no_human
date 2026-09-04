"""D3 (2026-08-31 operator directive): the auto-activation pipeline.

The operator directive explicitly OVERRIDES the internal "a human confirms
every learning" contract stated in `learning/curator.py` and
`core/scheduler.py`'s `HarvestJob` — both docstrings are rewritten to cite
this directive. This file pins the code those docstrings now describe:

  (a) a harvested proposal that passes the dedupe/PII/provenance/term
      screens auto-activates on the next `HarvestJob` tick;
  (b) the daily cap holds — an 11th otherwise-eligible proposal stays inert;
  (c) a `paused=1` row is never injected;
  (d) `learning.auto_manage: false` is a kill switch that restores today's
      exact behaviour (byte-identical prompt block), mirroring
      `brain/__init__.py`'s invariant-test style;
  (e) auto-activated rows retire automatically at 90 days unused;
      manually-added/pinned rows never do.

Every screen-failure and cap case is also pinned directly, and the
2026-09-01 effectiveness study's two follow-ons (word-boundary trigger
matching, and the `learning_events` audit trail recording WHICH TAGS FIRED
per injection) get their own tests here alongside the pipeline they gate.
"""

from __future__ import annotations

import pytest_asyncio

from no_human.core.db import Store
from no_human.core.orchestrator import Orchestrator
from no_human.core.prompt_blocks import build_memories_block
from no_human.core.scheduler import HarvestJob, RetirementSweepJob
from no_human.core.task import Task
from no_human.eval.vendor_terms import BANNED_TERMS
from no_human.learning import LearningQueue, TYPE_RULE
from no_human.learning.retire import sweep_auto_activated


@pytest_asyncio.fixture
async def queue(store):
    return LearningQueue(store)


def _bare_orchestrator(store):
    """An Orchestrator with only what `_load_active_memories` touches — see
    `test_knowledge_triggers.py`'s identical helper."""
    orch = Orchestrator.__new__(Orchestrator)
    orch.store = store
    return orch


async def _seed_clean_proposal(store, *, title="A repo requirement", n=0,
                                task_id="task-0000"):
    """A pending proposal that passes every D3 screen: real provenance
    (`origin` + `evidence.task_id`), no PII, no banned term, and a
    dedupe key distinct from anything already active."""
    return await store.add_memory(
        mem_type=TYPE_RULE, title=f"{title} {n}",
        content=f"Always do the thing #{n} in this repository.",
        tags=["depend"], source="proposed", confirmed=False,
        origin="review", evidence={"kind": "review_finding", "task_id": task_id},
        dedupe_key=f"clean-{n}",
    )


# --------------------------------------------------------------------------- #
# (a) a passing proposal auto-activates on the next HarvestJob tick           #
# --------------------------------------------------------------------------- #

async def test_a_passing_proposal_auto_activates_on_harvest_tick(store):
    mem_id = await _seed_clean_proposal(store, n=1)

    job = HarvestJob(store, interval_seconds=60, out_dir=None)
    result = await job.maybe_run()

    assert result["activated"] == 1
    assert result["auto_archived"] == 0
    row = await store.find_memory(mem_id)
    assert row["confirmed"] == 1
    assert row["source"] == "auto"
    assert row["confirmed_by"] == "auto"
    assert row["activated_at"], "activated_at must be stamped"

    events = await store.list_learning_events(memory_id=mem_id)
    assert any(e["event"] == "activate" for e in events)


async def test_the_positive_control_can_fail(store):
    """Known positive: without ever calling auto_activate, the same proposal
    stays exactly pending — proving the test above is not vacuous."""
    mem_id = await _seed_clean_proposal(store, n=1)
    row = await store.find_memory(mem_id)
    assert row["confirmed"] == 0
    assert row["source"] == "proposed"


# --------------------------------------------------------------------------- #
# (b) the daily cap holds                                                    #
# --------------------------------------------------------------------------- #

async def test_daily_cap_holds_the_11th_proposal_stays_inert(queue, store):
    ids = [await _seed_clean_proposal(store, n=i, task_id=f"task-{i:04d}")
           for i in range(11)]

    report = await queue.auto_activate(cap=10)

    assert len(report.activated) == 10
    assert report.cap_hit is True
    activated_rows = [await store.find_memory(i) for i in report.activated]
    assert all(r["confirmed"] == 1 for r in activated_rows)

    # The 11th (oldest-first order, so it's whichever one didn't fit) is
    # left EXACTLY where it was — pending, not archived, not touched.
    remaining = [i for i in ids if i not in set(report.activated)]
    assert len(remaining) == 1
    leftover = await store.find_memory(remaining[0])
    assert leftover["confirmed"] == 0
    assert leftover["archived"] == 0
    assert leftover["source"] == "proposed"


async def test_cap_is_a_rolling_24h_window_not_per_call(queue, store):
    """A second call in the same window sees the FIRST call's activations
    already counted — the cap is a property of the day, not of one
    `auto_activate` invocation."""
    for i in range(10):
        await _seed_clean_proposal(store, n=i, task_id=f"task-{i:04d}")
    first = await queue.auto_activate(cap=10)
    assert len(first.activated) == 10

    await _seed_clean_proposal(store, n=99, task_id="task-0099")
    second = await queue.auto_activate(cap=10)
    assert second.activated == []
    assert second.cap_hit is True


# --------------------------------------------------------------------------- #
# (c) a paused row is never injected                                         #
# --------------------------------------------------------------------------- #

async def test_a_paused_row_is_never_injected(store, queue):
    mem_id = await store.add_memory(
        mem_type=TYPE_RULE, title="kafka rule", content="x",
        tags=["kafka"], confirmed=True)
    assert await queue.pause(mem_id) is True

    task = Task.new("Fix the Kafka topic creation", repo_path="")
    all_scoped, triggered = await _bare_orchestrator(store)._load_active_memories(task)

    assert mem_id not in {m["id"] for m in all_scoped}, (
        "list_memories(confirmed=True) must exclude a paused row by default "
        "— the same wall `_load_active_memories` relies on without "
        "filtering for it itself")
    assert triggered == []

    events = await store.list_learning_events(memory_id=mem_id)
    assert any(e["event"] == "pause" for e in events)


async def test_unpausing_restores_injection(store, queue):
    """Known positive: the row above WOULD have fired if not paused."""
    mem_id = await store.add_memory(
        mem_type=TYPE_RULE, title="kafka rule", content="x",
        tags=["kafka"], confirmed=True)
    task = Task.new("Fix the Kafka topic creation", repo_path="")
    _, triggered = await _bare_orchestrator(store)._load_active_memories(task)
    assert [m["id"] for m in triggered] == [mem_id]

    await queue.pause(mem_id)
    _, triggered = await _bare_orchestrator(store)._load_active_memories(task)
    assert triggered == []

    await queue.unpause(mem_id)
    _, triggered = await _bare_orchestrator(store)._load_active_memories(task)
    assert [m["id"] for m in triggered] == [mem_id]


async def test_a_paused_row_is_never_injected_via_the_sessions_route(store, queue):
    """`context/sessions.py:SessionsSource` is a SECOND, independent raw-SQL
    route from `memories` into the implement prompt (its own module
    docstring names it as such) — `_load_active_memories` respecting
    `paused` says nothing about this bypass, which has its own WHERE clause.
    Mirrors `test_memory_quarantine.py::test_quarantined_never_injected`'s
    shape for the sibling `quarantined` flag."""
    from no_human.context.sessions import SessionsSource

    mem_id = await store.add_memory(
        mem_type=TYPE_RULE, title="widgetgadget rule",
        content="applies to widgetgadget tasks", confirmed=True, source="board")
    assert await queue.pause(mem_id) is True

    task = Task.new("Fix widgetgadget rendering")
    chunks = await SessionsSource(store).gather(task)
    assert not any("widgetgadget rule" in c.title for c in chunks), (
        "a paused rule reached the prompt through the sessions bypass route")


async def test_unpausing_restores_the_sessions_route_too(store, queue):
    """Known positive for the test above: the SAME row, unpaused, IS found
    by the sessions route — proving the absence above is the pause, not a
    keyword mismatch or an unrelated bug in the query."""
    from no_human.context.sessions import SessionsSource

    mem_id = await store.add_memory(
        mem_type=TYPE_RULE, title="widgetgadget rule",
        content="applies to widgetgadget tasks", confirmed=True, source="board")
    task = Task.new("Fix widgetgadget rendering")

    chunks = await SessionsSource(store).gather(task)
    assert any("widgetgadget rule" in c.title for c in chunks), (
        "control: the unpaused row must be found, or the test above is "
        "vacuous")

    await queue.pause(mem_id)
    chunks = await SessionsSource(store).gather(task)
    assert not any("widgetgadget rule" in c.title for c in chunks)

    await queue.unpause(mem_id)
    chunks = await SessionsSource(store).gather(task)
    assert any("widgetgadget rule" in c.title for c in chunks)


# --------------------------------------------------------------------------- #
# (d) the kill switch restores today's byte-identical behaviour               #
#     (mirrors brain/__init__.py's byte-identical-prompt invariant style)     #
# --------------------------------------------------------------------------- #

async def _seed_preexisting_confirmed_rule(store) -> str:
    """A rule that is ALREADY active before either scenario below runs — so
    the baseline prompt block is non-empty, and byte-identity is a real
    claim about what the harvested proposal did or didn't add, not an
    empty-string-equals-empty-string vacuity."""
    return await store.add_memory(
        mem_type=TYPE_RULE, title="Pre-existing pinned rule",
        content="An operator-added lesson that predates this harvest tick.",
        confirmed=True)


async def test_kill_switch_restores_byte_identical_prompt_block(store):
    """`auto_manage=False`: the proposal that (b)/(a) above prove would
    otherwise activate instead stays pending FOREVER through this job, so
    the confirmed-memories prompt block a coder/reviewer would see is
    BYTE-IDENTICAL to a store that never saw this proposal at all — proven
    against a NON-EMPTY baseline (a pre-existing confirmed rule), so the
    equality is a claim about the harvested proposal's absence, not two
    empty strings agreeing with each other."""
    await _seed_preexisting_confirmed_rule(store)
    baseline_block = build_memories_block(
        await store.list_memories(confirmed=True), 8000, 4000)
    assert baseline_block != "", (
        "control: the baseline must be non-empty or byte-identity here "
        "proves nothing")

    await _seed_clean_proposal(store, n=1)
    job = HarvestJob(store, interval_seconds=60, out_dir=None, auto_manage=False)
    result = await job.maybe_run()
    assert "activated" not in result, (
        "the kill switch must not even shape the result dict with D3 keys")

    off_block = build_memories_block(
        await store.list_memories(confirmed=True), 8000, 4000)
    assert off_block == baseline_block, (
        "auto_manage=False must leave the active set exactly as it was "
        "before this proposal ever existed — the pre-existing rule's block "
        "byte-for-byte, nothing from the new proposal added")


async def test_the_kill_switch_test_can_actually_fail(store):
    """Known positive, required by the brief: with the SAME non-empty
    baseline but the default (`auto_manage=True`), the prompt block DOES
    change — proving the byte-identity assertion above is not vacuous."""
    await _seed_preexisting_confirmed_rule(store)
    baseline_block = build_memories_block(
        await store.list_memories(confirmed=True), 8000, 4000)

    await _seed_clean_proposal(store, n=1)
    job = HarvestJob(store, interval_seconds=60, out_dir=None)  # auto_manage=True
    await job.maybe_run()

    on_block = build_memories_block(
        await store.list_memories(confirmed=True), 8000, 4000)
    assert on_block != baseline_block
    assert "Pre-existing pinned rule" in on_block, (
        "the pre-existing rule must still be present — auto-activation adds, "
        "it does not replace")
    assert "Always do the thing" in on_block


# --------------------------------------------------------------------------- #
# (e) auto-activated rows retire at 90 days unused; pinned rows never do      #
# --------------------------------------------------------------------------- #

async def test_auto_activated_retires_at_90_days_but_pinned_never_does(store):
    from datetime import datetime, timedelta, timezone

    auto_id = await _seed_clean_proposal(store, n=1)
    assert await store.activate_memory_auto(auto_id)
    stale = (datetime.now(timezone.utc) - timedelta(days=100)).isoformat()
    await store.db.execute(
        "UPDATE memories SET activated_at = ? WHERE id = ?", (stale, auto_id))
    await store.db.commit()

    # An operator-pinned/manually-added row: confirmed the ordinary way
    # (`confirm_memory`, the human path), never touched by auto-activation,
    # equally old and equally unused.
    pinned_id = await store.add_memory(
        mem_type=TYPE_RULE, title="pinned rule", content="y", confirmed=True)
    await store.db.execute(
        "UPDATE memories SET created_at = ? WHERE id = ?", (stale, pinned_id))
    await store.db.commit()

    report = await sweep_auto_activated(store, days=90)

    assert auto_id in report.archived_ids
    assert pinned_id not in report.archived_ids
    auto_row = await store.find_memory(auto_id)
    assert auto_row["archived"] == 1
    pinned_row = await store.find_memory(pinned_id)
    assert pinned_row["archived"] == 0

    events = await store.list_learning_events(memory_id=auto_id)
    assert any(e["event"] == "auto_retire" for e in events)


async def test_a_freshly_activated_row_is_not_retired_before_its_window(store):
    """Known positive / control: the ONLY thing distinguishing the retired
    row above from a safe one is age — a row activated (or used) recently
    must survive the same sweep."""
    auto_id = await _seed_clean_proposal(store, n=1)
    assert await store.activate_memory_auto(auto_id)  # activated_at = now

    report = await sweep_auto_activated(store, days=90)
    assert auto_id not in report.archived_ids
    row = await store.find_memory(auto_id)
    assert row["archived"] == 0


async def test_retirement_sweep_job_runs_the_auto_retire_sweep(store):
    """Wired end-to-end through the scheduled job, not just the bare
    `retire.py` function."""
    from datetime import datetime, timedelta, timezone

    auto_id = await _seed_clean_proposal(store, n=1)
    await store.activate_memory_auto(auto_id)
    stale = (datetime.now(timezone.utc) - timedelta(days=100)).isoformat()
    await store.db.execute(
        "UPDATE memories SET activated_at = ? WHERE id = ?", (stale, auto_id))
    await store.db.commit()

    job = RetirementSweepJob(store, interval_seconds=60)
    result = await job.maybe_run()
    assert result["auto_retired"] == 1
    row = await store.find_memory(auto_id)
    assert row["archived"] == 1


async def test_kill_switch_also_disables_the_auto_retire_sweep(store):
    from datetime import datetime, timedelta, timezone

    auto_id = await _seed_clean_proposal(store, n=1)
    await store.activate_memory_auto(auto_id)
    stale = (datetime.now(timezone.utc) - timedelta(days=100)).isoformat()
    await store.db.execute(
        "UPDATE memories SET activated_at = ? WHERE id = ?", (stale, auto_id))
    await store.db.commit()

    job = RetirementSweepJob(store, interval_seconds=60, auto_manage=False)
    result = await job.maybe_run()
    assert "auto_retired" not in result
    row = await store.find_memory(auto_id)
    assert row["archived"] == 0, "the kill switch must leave the row alone"


# --------------------------------------------------------------------------- #
# screens: dedupe / PII / provenance / term — each archives, never queues     #
# --------------------------------------------------------------------------- #

async def _auto_archive_reason(store, mem_id: str) -> str | None:
    """The `reason` an `auto_archive` learning_events row recorded for
    *mem_id*, or None — so each screen test below can assert WHICH screen
    caught the row, not merely that some screen did."""
    import json
    events = await store.list_learning_events(memory_id=mem_id)
    for e in events:
        if e["event"] == "auto_archive":
            detail = json.loads(e["detail"]) if e.get("detail") else {}
            return detail.get("reason")
    return None


async def test_screen_dedupe_archives_a_near_duplicate_of_an_active_row(queue, store):
    await store.add_memory(
        mem_type=TYPE_RULE, title="Always vendor deps", content="pin them",
        confirmed=True)
    dup_id = await store.add_memory(
        mem_type=TYPE_RULE, title="Always vendor deps", content="pin them",
        source="proposed", confirmed=False, origin="review",
        evidence={"task_id": "t1"}, dedupe_key="dupe-1")

    report = await queue.auto_activate(cap=10)
    assert report.activated == []
    assert dup_id in report.archived
    assert await _auto_archive_reason(store, dup_id) == "dedupe"


async def test_screen_dedupe_sees_a_paused_row_not_just_active_ones(queue, store):
    """The treadmill `reject()`'s own docstring names: pausing a rule must
    STICK against a re-harvested near-duplicate, not merely against a
    repeat of the exact same proposal (dedupe_key). `list_memories`'s
    default excludes a paused row — without `include_paused=True` at this
    read site, the paused row is invisible to the dedupe scan and the
    duplicate sails through to auto-activation, silently undoing the pause."""
    active_id = await store.add_memory(
        mem_type=TYPE_RULE, title="Always vendor deps", content="pin them",
        confirmed=True)
    assert await queue.pause(active_id) is True

    # A near-duplicate PROPOSAL — same near-duplicate key, but NOT the exact
    # same row (a fresh harvest pass would write a fresh dedupe_key for what
    # it thinks is a new proposal; only the title+content collapse it).
    dup_id = await store.add_memory(
        mem_type=TYPE_RULE, title="Always vendor deps", content="pin them",
        source="proposed", confirmed=False, origin="review",
        evidence={"task_id": "t2"}, dedupe_key="dupe-paused-1")

    report = await queue.auto_activate(cap=10)
    assert report.activated == [], (
        "a near-duplicate of a PAUSED rule must not auto-activate — that "
        "would silently undo the pause")
    assert dup_id in report.archived
    assert await _auto_archive_reason(store, dup_id) == "dedupe"

    # The paused row itself is untouched — still paused, still not active.
    paused_row = await store.find_memory(active_id)
    assert paused_row["paused"] == 1
    assert paused_row["confirmed"] == 1


async def test_find_superseded_sees_a_paused_row_too(store):
    """`learning.retire.find_superseded` (the AC3 supersede-on-confirm
    mechanism) is the OTHER dedupe read site the same paused-row-invisible
    bug applies to: a human confirming a fresh near-duplicate of a rule they
    had just paused must still supersede it, not leave two copies of the
    same lesson (one paused, one newly active) coexisting."""
    from no_human.learning.retire import find_superseded

    old_id = await store.add_memory(
        mem_type=TYPE_RULE, title="Always vendor deps", content="pin them",
        confirmed=True)
    assert await LearningQueue(store).pause(old_id) is True

    new_id = await store.add_memory(
        mem_type=TYPE_RULE, title="Always vendor deps", content="pin them",
        confirmed=True)
    new_row = await store.find_memory(new_id)

    dupes = await find_superseded(store, new_row)
    assert [d["id"] for d in dupes] == [old_id], (
        "a paused row must still be found as a near-duplicate candidate — "
        "pausing is not archiving, the lesson is still there")


async def test_screen_pii_archives_rather_than_activates(queue, store):
    pii_id = await store.add_memory(
        mem_type=TYPE_RULE, title="Contact rule",
        # A CONSUMER mailbox domain (learning/pii.py only flags these —
        # a corporate/test domain like example.com is deliberately KEPT).
        # No dot/underscore in the local part: `contains_pii` needs only the
        # consumer domain, but `tests/test_identity_scrub_guard.py`'s
        # vocabulary-free `personal-email` SHAPE additionally fires on any
        # `first.last@`/`first_last@` local part regardless of domain —
        # a hyphen (like a real address's) is deliberately not a name
        # separator for that guard.
        content="Email contact-approvals@gmail.com for approval",
        source="proposed", confirmed=False, origin="review",
        evidence={"task_id": "t1"}, dedupe_key="pii-1")

    report = await queue.auto_activate(cap=10)
    assert report.activated == []
    assert pii_id in report.archived
    assert await _auto_archive_reason(store, pii_id) == "pii"
    row = await store.find_memory(pii_id)
    assert row["confirmed"] == 0
    assert row["archived"] == 1


async def test_screen_provenance_archives_a_row_with_no_origin_and_no_task_id(queue, store):
    no_prov_id = await store.add_memory(
        mem_type=TYPE_RULE, title="No provenance", content="do the thing",
        source="proposed", confirmed=False, dedupe_key="noprov-1")

    report = await queue.auto_activate(cap=10)
    assert report.activated == []
    assert no_prov_id in report.archived
    assert await _auto_archive_reason(store, no_prov_id) == "provenance"


async def test_screen_term_archives_a_row_carrying_a_banned_vendor_term(queue, store):
    term = BANNED_TERMS[0]
    term_id = await store.add_memory(
        mem_type=TYPE_RULE, title=f"The {term} deployment runner",
        content="Drain the queue before deploying.",
        source="proposed", confirmed=False, origin="review",
        evidence={"task_id": "t1"}, dedupe_key="term-1")
    # The write-time quarantine gate (`add_memory`) uses the same matcher and
    # would also quarantine this row on insert — un-quarantine so the screen
    # under test is isolated (same technique as
    # test_a_rule_held_by_the_term_screen_is_not_recorded_as_used).
    await store.set_quarantine(term_id, False, None)

    report = await queue.auto_activate(cap=10)
    assert report.activated == []
    assert term_id in report.archived
    assert await _auto_archive_reason(store, term_id) == "term"


async def test_a_clean_proposal_is_the_positive_control_for_every_screen(queue, store):
    """Every screen test above proves a NEGATIVE (this fails to activate).
    Without this, all four could be vacuously true because NOTHING ever
    activates. It does."""
    clean_id = await _seed_clean_proposal(store, n=1)
    report = await queue.auto_activate(cap=10)
    assert clean_id in report.activated
    assert report.archived == []


# --------------------------------------------------------------------------- #
# 2026-09-01 effectiveness study: word-boundary trigger matching               #
# --------------------------------------------------------------------------- #

def test_word_boundary_trigger_does_not_fire_on_a_substring():
    from no_human.learning.triggers import memory_is_triggered

    mem = {"title": "x", "tags": ["fact"]}
    assert memory_is_triggered(mem, "refactor the artefact pipeline") is False, (
        "'fact' must not fire inside 'artefact' — the bare substring bug "
        "the 2026-09-01 effectiveness study found")


def test_word_boundary_trigger_still_fires_on_the_whole_word():
    """Known positive for the test above: the SAME tag, on text that
    contains it as a real word, still fires."""
    from no_human.learning.triggers import memory_is_triggered

    mem = {"title": "x", "tags": ["fact"]}
    assert memory_is_triggered(mem, "state the fact plainly") is True


def test_word_boundary_trigger_still_matches_a_multi_word_phrase_tag():
    from no_human.learning.triggers import memory_is_triggered

    mem = {"title": "x", "tags": ["suffix rule"]}
    assert memory_is_triggered(mem, "an AC mentioning the suffix rule here") is True
    assert memory_is_triggered(mem, "an unrelated suffix elsewhere") is False


# --------------------------------------------------------------------------- #
# learning_events must record WHICH TAGS FIRED per injection                  #
# --------------------------------------------------------------------------- #

async def test_injection_audit_event_records_which_tags_fired(store):
    mem_id = await store.add_memory(
        mem_type=TYPE_RULE, title="kafka rule", content="x",
        tags=["kafka", "clickhouse"], confirmed=True)
    task = Task.new("Fix the Kafka topic creation", repo_path="")
    await _bare_orchestrator(store)._load_active_memories(task)

    events = await store.list_learning_events(memory_id=mem_id)
    inject_events = [e for e in events if e["event"] == "inject"]
    assert len(inject_events) == 1
    import json
    detail = json.loads(inject_events[0]["detail"])
    assert detail["tags"] == ["kafka"], (
        "only the tag that actually fired ('kafka') must be recorded — "
        "'clickhouse' never matched this task's text")


async def test_no_injection_event_for_a_rule_that_did_not_fire(store):
    """Known positive / control: an unrelated rule that is fetched but does
    NOT trigger gets no `inject` audit row at all."""
    held_id = await store.add_memory(
        mem_type=TYPE_RULE, title="css rule", content="x",
        tags=["css"], confirmed=True)
    task = Task.new("Fix the Kafka topic creation", repo_path="")
    await _bare_orchestrator(store)._load_active_memories(task)

    events = await store.list_learning_events(memory_id=held_id)
    assert events == []


async def test_injection_audit_records_unconditional_not_an_empty_tag_list(store):
    """An UNTAGGED memory always injects (no tags = unconditional) — it has
    no tag to name as "why". `matched_tags` returns `[]` for it, and its own
    docstring warns that a caller must not read that back as "no reason".
    The audit row must say `unconditional: true`, never a bare `tags: []`
    that would misreport an always-on rule as one whose trigger evaluated to
    nothing."""
    mem_id = await store.add_memory(
        mem_type=TYPE_RULE, title="always-on rule", content="x",
        confirmed=True)  # no tags at all
    task = Task.new("Fix the Kafka topic creation", repo_path="")
    await _bare_orchestrator(store)._load_active_memories(task)

    events = await store.list_learning_events(memory_id=mem_id)
    inject_events = [e for e in events if e["event"] == "inject"]
    assert len(inject_events) == 1
    import json
    detail = json.loads(inject_events[0]["detail"])
    assert detail.get("unconditional") is True
    assert "tags" not in detail, (
        f"an untagged rule's audit row must not carry a 'tags' key at all "
        f"(empty or otherwise): {detail}")


# --------------------------------------------------------------------------- #
# audit writes are best-effort: a failed learning_events write must never    #
# turn an already-completed transition into a reported failure               #
# --------------------------------------------------------------------------- #

async def test_pause_succeeds_even_when_the_audit_write_fails(store, queue, monkeypatch):
    mem_id = await store.add_memory(
        mem_type=TYPE_RULE, title="x", content="x", confirmed=True)

    async def _boom(*a, **k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(store, "record_learning_event", _boom)
    assert await queue.pause(mem_id) is True, (
        "a failed audit write must not make pause() report failure")
    row = await store.find_memory(mem_id)
    assert row["paused"] == 1, "the transition itself must still have landed"


async def test_delete_succeeds_even_when_the_audit_write_fails(store, queue, monkeypatch):
    mem_id = await store.add_memory(
        mem_type=TYPE_RULE, title="x", content="x", confirmed=True)

    async def _boom(*a, **k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(store, "record_learning_event", _boom)
    assert await queue.delete(mem_id) is True
    row = await store.find_memory(mem_id)
    assert row["archived"] == 1


async def test_retire_succeeds_even_when_the_audit_write_fails(store, queue, monkeypatch):
    mem_id = await store.add_memory(
        mem_type=TYPE_RULE, title="x", content="x", confirmed=True)

    async def _boom(*a, **k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(store, "record_learning_event", _boom)
    assert await queue.retire(mem_id) is True
    row = await store.find_memory(mem_id)
    assert row["archived"] == 1


async def test_confirm_succeeds_even_when_the_audit_write_fails(store, queue, monkeypatch):
    mem_id = await store.add_memory(
        mem_type=TYPE_RULE, title="x", content="x", confirmed=False,
        source="proposed")

    async def _boom(*a, **k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(store, "record_learning_event", _boom)
    assert await queue.confirm(mem_id) is True
    row = await store.find_memory(mem_id)
    assert row["confirmed"] == 1


async def test_auto_retire_sweep_does_not_abort_on_a_mid_loop_audit_failure(
        store, monkeypatch):
    """`retire.sweep_auto_activated`'s per-id audit loop must not let a
    SINGLE failed write abandon auditing (or, worse, raise and abort) every
    id still to come — the archive for every id already committed in one
    batched statement before this loop runs at all."""
    from datetime import datetime, timedelta, timezone

    ids = []
    for i in range(3):
        mid = await store.add_memory(
            mem_type=TYPE_RULE, title=f"stale {i}", content="x",
            confirmed=False, source="proposed", origin="review",
            evidence={"task_id": f"t{i}"}, dedupe_key=f"stale-{i}")
        assert await store.activate_memory_auto(mid)
        ids.append(mid)
    stale = (datetime.now(timezone.utc) - timedelta(days=100)).isoformat()
    for mid in ids:
        await store.db.execute(
            "UPDATE memories SET activated_at = ? WHERE id = ?", (stale, mid))
    await store.db.commit()

    calls = {"n": 0}
    real_record = store.record_learning_event

    async def _fail_on_second(*a, **k):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("database is locked")
        return await real_record(*a, **k)

    monkeypatch.setattr(store, "record_learning_event", _fail_on_second)
    report = await sweep_auto_activated(store, days=90)

    # All three still archived — the archive is one batched statement, not
    # gated on the audit loop at all.
    assert set(report.archived_ids) == set(ids)
    for mid in ids:
        row = await store.find_memory(mid)
        assert row["archived"] == 1
    assert calls["n"] == 3, (
        "the loop must have attempted every id, not stopped at the failure")


# --------------------------------------------------------------------------- #
# efficiency minor (D3.1's deferred item): the injection loop's audit trail   #
# is batched — ONE `record_learning_events` executemany+commit for every     #
# injected memory, not one `record_learning_event` write+commit PER memory,  #
# mirroring the already-batched `record_memory_uses`/`touch_memories_used`   #
# right beside it in `_load_active_memories`.                                #
# --------------------------------------------------------------------------- #

async def test_record_learning_events_batch_writes_all_rows_in_one_call(
        store, monkeypatch):
    """`Store.record_learning_events` — the batched sibling of
    `record_learning_event` — writes N rows via a SINGLE `executemany` +
    SINGLE `commit`, not N round trips."""
    mem_ids = [
        await store.add_memory(mem_type=TYPE_RULE, title=f"r{i}", content="x",
                                confirmed=True)
        for i in range(4)
    ]
    executemany_calls = {"n": 0}
    real_executemany = store.db.executemany

    async def _counting_executemany(*a, **k):
        executemany_calls["n"] += 1
        return await real_executemany(*a, **k)

    monkeypatch.setattr(store.db, "executemany", _counting_executemany)

    rows = [(mid, "inject", {"tags": ["t"]}) for mid in mem_ids]
    ids = await store.record_learning_events(rows)

    assert len(ids) == 4
    assert len(set(ids)) == 4, "each row gets its own event id"
    assert executemany_calls["n"] == 1, (
        "N injected memories must produce N audit rows via ONE write call")

    for mid in mem_ids:
        events = await store.list_learning_events(memory_id=mid)
        assert len(events) == 1
        assert events[0]["event"] == "inject"

    assert await store.record_learning_events([]) == [], (
        "empty input is a no-op, mirroring record_memory_uses")


async def test_record_learning_events_batch_row_shape_matches_the_per_row_form(
        store):
    """The batched write's row content must be identical in SHAPE to what
    `record_learning_event` (the per-row form) would have written — only the
    write mechanism changed, not what's recorded."""
    import json

    mem_a = await store.add_memory(mem_type=TYPE_RULE, title="a", content="x",
                                    confirmed=True)
    mem_b = await store.add_memory(mem_type=TYPE_RULE, title="b", content="x",
                                    confirmed=True)

    detail_a = {"task_id": "task-1", "attempt_id": "att-1", "tags": ["kafka"]}
    detail_b = {"task_id": "task-1", "attempt_id": "att-1", "unconditional": True}

    # Per-row form, for the baseline.
    await store.record_learning_event(mem_a, "inject", detail=detail_a)
    # Batched form, for the same shape of input.
    await store.record_learning_events([(mem_b, "inject", detail_b)])

    events_a = await store.list_learning_events(memory_id=mem_a)
    events_b = await store.list_learning_events(memory_id=mem_b)
    assert len(events_a) == 1 and len(events_b) == 1
    row_a, row_b = events_a[0], events_b[0]

    # Same columns populated, same event name, same JSON-serialized detail
    # shape (only the memory_id and detail payload differ between the two).
    assert set(row_a.keys()) == set(row_b.keys())
    assert row_a["event"] == row_b["event"] == "inject"
    assert row_a["memory_id"] == mem_a
    assert row_b["memory_id"] == mem_b
    assert json.loads(row_a["detail"]) == detail_a
    assert json.loads(row_b["detail"]) == detail_b
    assert row_a["id"] != row_b["id"]
    assert row_a["created_at"] and row_b["created_at"]


async def test_load_active_memories_uses_the_batched_write_at_the_injection_site(
        store, monkeypatch):
    """`_load_active_memories`'s injection loop calls the BATCHED form, not
    the per-row one, for its `learning_events` audit rows — this is the
    actual call-site change, not just a new store method sitting unused."""
    mem_ids = [
        await store.add_memory(
            mem_type=TYPE_RULE, title=f"always-on {i}", content="x",
            confirmed=True)  # untagged -> unconditional, always injects
        for i in range(3)
    ]
    task = Task.new("Fix the Kafka topic creation", repo_path="")

    per_row_calls = {"n": 0}
    batch_calls = {"n": 0, "rows": None}
    real_batch = store.record_learning_events

    async def _count_per_row(*a, **k):
        per_row_calls["n"] += 1

    async def _count_batch(rows):
        batch_calls["n"] += 1
        batch_calls["rows"] = rows
        return await real_batch(rows)

    monkeypatch.setattr(store, "record_learning_event", _count_per_row)
    monkeypatch.setattr(store, "record_learning_events", _count_batch)

    await _bare_orchestrator(store)._load_active_memories(task)

    assert batch_calls["n"] == 1, (
        "the injection loop must call the batched form exactly once, "
        "regardless of how many memories were injected")
    assert per_row_calls["n"] == 0, (
        "the per-row form must not be called at all from this call site"
    )
    assert len(batch_calls["rows"]) == 3

    for mem_id in mem_ids:
        events = await store.list_learning_events(memory_id=mem_id)
        inject_events = [e for e in events if e["event"] == "inject"]
        assert len(inject_events) == 1


async def test_injection_proceeds_when_the_batched_audit_write_fails(
        store, monkeypatch):
    """Best-effort guard, batched form: a raising `record_learning_events`
    must not stop the attempt — `_load_active_memories` must return
    normally, and the OTHER ledger writes (`touch_memories_used`,
    `record_memory_uses`) beside it must still have landed."""
    mem_id = await store.add_memory(
        mem_type=TYPE_RULE, title="always-on rule", content="x",
        confirmed=True)
    task = Task.new("Fix the Kafka topic creation", repo_path="")

    async def _boom(*a, **k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(store, "record_learning_events", _boom)

    # Must not raise.
    await _bare_orchestrator(store)._load_active_memories(task)

    # The sibling ledger writes are unaffected by the audit-write failure.
    mem = await store.find_memory(mem_id)
    assert mem["use_count"] == 1
    assert mem["last_used_at"]
    # And no learning_events row exists for this memory — the failure was
    # real, not silently swallowed into a fake success.
    events = await store.list_learning_events(memory_id=mem_id)
    assert events == []
