"""SQLite store: create/get/list/transition/attempts."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from no_human.core.db import Store
from no_human.core.task import IllegalTransition, Task, TaskStatus


@pytest.fixture
async def store(tmp_path):
    s = await Store(tmp_path / "t.db").connect()
    yield s
    await s.close()


async def test_create_and_get(store):
    t = Task.new("do a thing", repo_path="/tmp/r")
    await store.create_task(t)
    got = await store.get_task(t.id)
    assert got is not None and got.title == "do a thing"


async def test_find_by_prefix(store):
    t = Task.new("x", repo_path="/tmp/r")
    await store.create_task(t)
    assert (await store.find_task(t.id[:8])).id == t.id


async def test_set_status_enforces_transitions(store):
    t = Task.new("x", repo_path="/tmp/r")
    await store.create_task(t)
    await store.set_status(t, TaskStatus.CONTEXT)
    assert (await store.get_task(t.id)).status is TaskStatus.CONTEXT
    with pytest.raises(IllegalTransition):
        await store.set_status(t, TaskStatus.DONE)


async def test_set_status_cas_guard_blocks_stale_write_over_done_row(store):
    """SCRUM-73: a worker coroutine holding a stale IMPLEMENTING handle must
    not resurrect a row a human's `shipped` verb already moved to DONE, even
    though IMPLEMENTING->REVIEWING passes `assert_transition` against the
    stale in-memory status. The blocked write is a no-op: it returns the
    falsy sentinel (None) and the DB row is untouched."""
    t = Task.new("x", repo_path="/tmp/r")
    await store.create_task(t)
    await store.set_status(t, TaskStatus.CONTEXT)
    await store.set_status(t, TaskStatus.PLANNING)
    await store.set_status(t, TaskStatus.IMPLEMENTING)
    # A human `shipped` verb elsewhere writes DONE directly to the DB row.
    await store.set_status(
        t, TaskStatus.DONE, validate=False, human_override=True,
        event={"source": "test", "kind": "test_seed"})

    stale = Task.new("x", repo_path="/tmp/r")
    stale.id = t.id
    stale.status = TaskStatus.IMPLEMENTING  # stale copy, unaware of DONE
    result = await store.set_status(stale, TaskStatus.REVIEWING)

    assert not result  # falsy sentinel — blocked, not applied
    assert (await store.get_task(t.id)).status is TaskStatus.DONE


async def test_set_status_cas_guard_blocks_stale_write_over_cancelled_row(store):
    """A FAILED row with a `cancel_reason` in context is an explicit human
    cancel, not a plain failure — it must be guarded exactly like DONE, so a
    stale in-flight write can never resurrect a row the human just killed."""
    t = Task.new("x", repo_path="/tmp/r")
    await store.create_task(t)
    # Setup only — jump straight to a mid-run status (the legal ladder is
    # not what this test pins).
    await store.set_status(t, TaskStatus.IMPLEMENTING, validate=False)
    t.context = await store.merge_context(t.id, {"cancel_reason": "nope"})
    await store.set_status(
        t, TaskStatus.FAILED, validate=False, human_override=True)

    stale = Task.new("x", repo_path="/tmp/r")
    stale.id = t.id
    stale.status = TaskStatus.IMPLEMENTING
    result = await store.set_status(stale, TaskStatus.REVIEWING)

    assert not result
    assert (await store.get_task(t.id)).status is TaskStatus.FAILED


async def test_set_status_cas_guard_applies_to_validate_false(store):
    """The guard must not be bypassable via validate=False — several
    orchestrator writes use it, and terminal is terminal regardless, unless
    the caller explicitly claims human_override."""
    t = Task.new("x", repo_path="/tmp/r")
    await store.create_task(t)
    await store.set_status(
        t, TaskStatus.DONE, validate=False, human_override=True,
        event={"source": "test", "kind": "test_seed"})

    result = await store.set_status(t, TaskStatus.PENDING, validate=False)

    assert not result
    assert (await store.get_task(t.id)).status is TaskStatus.DONE


async def test_set_status_failed_to_pending_retry_not_blocked(store):
    """A plain FAILED row (no cancel_reason) is not terminal for the CAS
    guard's purposes — writes land without needing human_override, so any
    caller that reaches this state can still move it."""
    t = Task.new("x", repo_path="/tmp/r")
    await store.create_task(t)
    await store.set_status(t, TaskStatus.FAILED, validate=False)

    result = await store.set_status(t, TaskStatus.PENDING, validate=False)

    assert result.status is TaskStatus.PENDING
    assert (await store.get_task(t.id)).status is TaskStatus.PENDING


async def test_set_status_human_override_revives_cancelled_row(store):
    """`nh task retry` / `POST /api/tasks/{id}/retry` move a row OUT of a
    cancelled (FAILED + cancel_reason) state via human_override=True — the
    sanctioned escape hatch the guard must not block."""
    t = Task.new("x", repo_path="/tmp/r")
    await store.create_task(t)
    t.context = await store.merge_context(t.id, {"cancel_reason": "nope"})
    await store.set_status(
        t, TaskStatus.FAILED, validate=False, human_override=True)

    result = await store.set_status(
        t, TaskStatus.PENDING, validate=False, human_override=True)

    assert result.status is TaskStatus.PENDING
    assert (await store.get_task(t.id)).status is TaskStatus.PENDING


async def test_update_task_cas_guard_blocks_stale_status_over_done_row(store):
    """update_task rewrites the whole row (SCRUM-73) — it must not resurrect
    a DONE row's status column either, while still writing every other
    column normally (e.g. the Jira poller's write-back keeps updating
    context markers on an already-DONE row long after completion)."""
    t = Task.new("x", repo_path="/tmp/r")
    await store.create_task(t)
    await store.set_status(t, TaskStatus.DONE, validate=False,
                           event={"source": "test", "kind": "test_seed"})

    stale = await store.get_task(t.id)
    stale.status = TaskStatus.IMPLEMENTING  # stale caller resurrecting it
    stale.blocker = {"category": "AMBIGUITY", "question": "?"}
    result = await store.update_task(stale)

    assert result.status is TaskStatus.DONE
    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.DONE
    assert fresh.blocker["category"] == "AMBIGUITY"  # other columns still write


async def test_update_task_cas_guard_blocks_stale_status_over_cancelled_row(store):
    """update_task's guard mirrors set_status's terminal definition — a
    FAILED row with a cancel_reason is protected too, not just DONE."""
    t = Task.new("x", repo_path="/tmp/r")
    await store.create_task(t)
    t.context = await store.merge_context(t.id, {"cancel_reason": "nope"})
    await store.set_status(
        t, TaskStatus.FAILED, validate=False, human_override=True)

    stale = await store.get_task(t.id)
    stale.status = TaskStatus.PENDING  # stale caller resurrecting it
    stale.blocker = {"category": "AMBIGUITY", "question": "?"}
    result = await store.update_task(stale)

    assert result.status is TaskStatus.FAILED
    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.FAILED
    assert fresh.blocker["category"] == "AMBIGUITY"  # other columns still write


async def test_update_task_leaves_status_to_set_status(store):
    """update_task never moves the status column (R15, 2026-08-09 incident:
    a poller's stale full-row write-back reverted a live task's advance and
    stranded it). Only set_status moves status; the handle is refreshed to
    the row's truth."""
    t = Task.new("x", repo_path="/tmp/r")
    await store.create_task(t)
    t.status = TaskStatus.CONTEXT           # a caller trying the old way
    result = await store.update_task(t)
    assert result.status is TaskStatus.PENDING      # handle refreshed
    assert (await store.get_task(t.id)).status is TaskStatus.PENDING
    # ...and the sanctioned path still works.
    await store.set_status(t, TaskStatus.CONTEXT)
    assert (await store.get_task(t.id)).status is TaskStatus.CONTEXT


async def test_attempts_lifecycle(store):
    t = Task.new("x", repo_path="/tmp/r")
    await store.create_task(t)
    aid = await store.create_attempt(t.id, 1)
    await store.update_attempt(aid, branch_name="no-human/x", turns_used=3,
                               test_results={"ok": True})
    attempts = await store.list_attempts(t.id)
    assert attempts[0]["branch_name"] == "no-human/x"
    assert attempts[0]["turns_used"] == 3
    assert await store.count_attempts(t.id) == 1


async def test_list_by_status(store):
    a = Task.new("a", repo_path="/r")
    b = Task.new("b", repo_path="/r")
    await store.create_task(a)
    await store.create_task(b)
    await store.set_status(b, TaskStatus.CONTEXT)
    pend = await store.list_tasks(TaskStatus.PENDING)
    assert {t.id for t in pend} == {a.id}


async def test_list_tasks_is_newest_first(store):
    """Board order stays DESC — pinned separately from the scheduler's claim
    order (`list_claimable_tasks`), which is the opposite."""
    oldest = Task.new("oldest", repo_path="/r")
    oldest.created_at = "2026-08-01T08:00:00+00:00"
    await store.create_task(oldest)
    middle = Task.new("middle", repo_path="/r")
    middle.created_at = "2026-08-05T08:00:00+00:00"
    await store.create_task(middle)
    newest = Task.new("newest", repo_path="/r")
    newest.created_at = "2026-08-10T08:00:00+00:00"
    await store.create_task(newest)

    all_ids = [t.id for t in await store.list_tasks()]
    assert all_ids == [newest.id, middle.id, oldest.id]

    pend_ids = [t.id for t in await store.list_tasks(TaskStatus.PENDING)]
    assert pend_ids == [newest.id, middle.id, oldest.id]


async def test_list_tasks_limit_offset_pushdown(store):
    """P5: `?limit/offset` on GET /api/tasks is pushed down to SQL — a page
    request must not hydrate every row and slice in Python (that would still
    pay the full 700+-row cost on the production fleet, just later)."""
    ids = []
    for i in range(10):
        t = Task.new(f"t{i}", repo_path="/r")
        t.created_at = f"2026-08-{i + 1:02d}T08:00:00+00:00"
        await store.create_task(t)
        ids.append(t.id)
    ids.reverse()  # newest first, matching ORDER BY created_at DESC

    page1 = await store.list_tasks(limit=3)
    assert [t.id for t in page1] == ids[:3]

    page2 = await store.list_tasks(limit=3, offset=3)
    assert [t.id for t in page2] == ids[3:6]

    # No limit → unchanged full-list behavior (existing callers/tests).
    full = await store.list_tasks()
    assert [t.id for t in full] == ids


async def test_list_tasks_created_at_tie_spanning_a_page_boundary(store):
    """Review finding: `ORDER BY created_at DESC` alone has no tie-break, and
    `list_claimable_tasks` (this file, oldest-first sibling) documents why
    that's live: `created_at` is an ISO string, and rows created in the same
    tight loop (e.g. `/split`'s children) can share one value. A tie must
    still resolve DETERMINISTICALLY and consistently with the DESC ("newest
    first") convention this query promises — most-recently-created of a tied
    group first, mirroring the sibling's own `rowid` tie-break — not to
    whatever order SQLite's query planner happens to produce absent one,
    which is not part of the SQL contract and cannot be relied on to stay
    stable across a plan change. Six rows share ONE `created_at`, split
    across a limit=3 page boundary."""
    ids = []
    for i in range(6):
        t = Task.new(f"tie{i}", repo_path="/r")
        t.created_at = "2026-08-15T08:00:00+00:00"
        await store.create_task(t)
        ids.append(t.id)
    # DESC + a rowid tie-break must put the LAST-inserted (highest rowid) of
    # the tied group first, same direction as created_at DESC itself.
    expected = list(reversed(ids))

    page1 = await store.list_tasks(limit=3, offset=0)
    page2 = await store.list_tasks(limit=3, offset=3)
    paged = [t.id for t in page1] + [t.id for t in page2]
    assert paged == expected, (
        "a created_at tie spanning the page boundary did not resolve "
        "deterministically DESC by rowid — list_tasks needs the same "
        "tie-break as list_claimable_tasks"
    )
    assert sorted(paged) == sorted(ids), (
        "paging across the tie must recover every row exactly once"
    )


async def test_list_tasks_limit_offset_composes_with_status(store):
    a = Task.new("a", repo_path="/r")
    b = Task.new("b", repo_path="/r")
    a.created_at = "2026-08-01T08:00:00+00:00"
    b.created_at = "2026-08-02T08:00:00+00:00"
    await store.create_task(a)
    await store.create_task(b)
    page = await store.list_tasks(TaskStatus.PENDING, limit=1)
    assert [t.id for t in page] == [b.id]


async def test_list_claimable_tasks_is_oldest_first(store):
    """The scheduler's claim query is the exact reverse of the board's, and
    ties (identical `created_at`) break on ascending rowid — insertion
    order — so the FIFO guarantee is deterministic, not luck."""
    oldest = Task.new("oldest", repo_path="/r")
    oldest.created_at = "2026-08-01T08:00:00+00:00"
    await store.create_task(oldest)
    middle = Task.new("middle", repo_path="/r")
    middle.created_at = "2026-08-05T08:00:00+00:00"
    await store.create_task(middle)
    newest = Task.new("newest", repo_path="/r")
    newest.created_at = "2026-08-10T08:00:00+00:00"
    await store.create_task(newest)

    claim_ids = [t.id for t in await store.list_claimable_tasks(TaskStatus.PENDING)]
    assert claim_ids == [oldest.id, middle.id, newest.id]

    # Tie-break: two rows with an identical created_at stamp order by
    # ascending rowid (insertion order), not arbitrarily.
    tie_first = Task.new("tie first", repo_path="/r")
    tie_first.created_at = "2026-09-01T08:00:00+00:00"
    await store.create_task(tie_first)
    tie_second = Task.new("tie second", repo_path="/r")
    tie_second.created_at = "2026-09-01T08:00:00+00:00"
    await store.create_task(tie_second)

    claim_ids = [t.id for t in await store.list_claimable_tasks(TaskStatus.PENDING)]
    assert claim_ids[-2:] == [tie_first.id, tie_second.id]


async def test_list_imported_tasks_filters_source_and_external_id(store):
    """SCRUM-54: the picker projection returns only (external_id, id, status,
    created_at) for jira-sourced tasks with a linked external_id — never
    freeform tasks, never jira tasks still missing an external_id."""
    jira_task = Task.new("SCRUM-1", source="jira", external_id="SCRUM-1")
    await store.create_task(jira_task)
    freeform_task = Task.new("not from jira", source="freeform",
                              external_id="SCRUM-2")
    await store.create_task(freeform_task)
    unlinked_jira_task = Task.new("jira but not yet linked", source="jira")
    await store.create_task(unlinked_jira_task)

    # A Linear task carrying the SAME external_id: the projection is scoped by
    # SOURCE, so it must not leak into the Jira read (dedupe keys on the pair,
    # and the Backlog page now lists both trackers side by side).
    linear_collider = Task.new("NO-1 on Linear", source="linear",
                               external_id="SCRUM-1")
    await store.create_task(linear_collider)

    rows = await store.list_imported_tasks("jira")
    assert {r.id for r in rows} == {jira_task.id}
    row = rows[0]
    assert row.external_id == "SCRUM-1"
    assert row.status == jira_task.status.value
    assert row.created_at == jira_task.created_at

    linear_rows = await store.list_imported_tasks("linear")
    assert {r.id for r in linear_rows} == {linear_collider.id}


async def test_list_memories_project_scoped(store):
    """A task on repo A sees A's rules + globals, never repo B's (B3)."""
    await store.add_memory(mem_type="rule", title="ra", content="for A",
                           project="/repo/a", confirmed=True)
    await store.add_memory(mem_type="rule", title="rb", content="for B",
                           project="/repo/b", confirmed=True)
    await store.add_memory(mem_type="rule", title="rg", content="global",
                           project=None, confirmed=True)

    scoped = await store.list_memories(confirmed=True, project="/repo/a")
    titles = {m["title"] for m in scoped}
    assert titles == {"ra", "rg"}  # A's rule + global, not B's

    scoped_only = await store.list_memories(
        confirmed=True, project="/repo/a", include_global=False
    )
    assert {m["title"] for m in scoped_only} == {"ra"}

    # No project filter → all rows (back-compat).
    everything = await store.list_memories(confirmed=True)
    assert {m["title"] for m in everything} == {"ra", "rb", "rg"}


async def test_save_and_list_events_persist_across_restart(tmp_path):
    """Events saved via save_events survive a Store reconnect (simulated restart)."""
    t = Task.new("x", repo_path="/tmp/r")
    db_path = tmp_path / "events.db"

    s1 = await Store(db_path).connect()
    await s1.create_task(t)
    events = [
        {"ts": 1.0, "kind": "tool_use", "source": "agent", "tool_name": "Read"},
        {"ts": 2.0, "kind": "result", "source": "orchestrator", "text": "done"},
    ]
    await s1.save_events(t.id, events)
    await s1.close()

    # Simulate a server restart: fresh Store instance, same db file.
    s2 = await Store(db_path).connect()
    persisted = await s2.list_events(t.id)
    assert len(persisted) == 2
    assert persisted[0]["kind"] == "tool_use"
    assert persisted[1]["text"] == "done"
    await s2.close()


async def test_list_events_empty_for_unknown_task(store):
    assert await store.list_events("no-such-task") == []


async def test_merge_context_concurrent_writers_both_survive(tmp_path):
    """The lost-update class behind the 2026-07-10 incident: two writers
    holding stale Task copies clobber each other via update_task. merge_context
    must let concurrent merges of different keys BOTH land — same connection
    and across connections (the CLI and the server are different processes)."""
    import asyncio
    from no_human.core.db import Store
    from no_human.core.task import Task

    db = tmp_path / "nh.db"
    async with Store(db) as s:
        t = Task.new("x", repo_path="/tmp/x")
        t.context = {"seed": 1}
        await s.create_task(t)
        await asyncio.gather(
            s.merge_context(t.id, {"watcher_key": "a", "nested": {"w": 1}}),
            s.merge_context(t.id, {"coder_key": "b", "nested": {"c": 2}}),
        )
        ctx = (await s.get_task(t.id)).context
        assert ctx["seed"] == 1
        assert ctx["watcher_key"] == "a" and ctx["coder_key"] == "b"
        assert ctx["nested"] == {"w": 1, "c": 2}  # recursive merge, no clobber

    # Cross-connection (cross-process shape): a second Store on the same file.
    async with Store(db) as s1, Store(db) as s2:
        tid = t.id
        await asyncio.gather(
            s1.merge_context(tid, {"proc1": True}),
            s2.merge_context(tid, {"proc2": True}),
        )
        ctx = (await s1.get_task(tid)).context
        assert ctx["proc1"] is True and ctx["proc2"] is True


async def test_merge_context_none_deletes_and_lists_replace(tmp_path):
    from no_human.core.db import Store
    from no_human.core.task import Task
    async with Store(tmp_path / "nh.db") as s:
        t = Task.new("x", repo_path="/tmp/x")
        t.context = {"ci_gate": {"pipeline_id": "1"}, "rounds": [1, 2]}
        await s.create_task(t)
        merged = await s.merge_context(t.id, {"ci_gate": None, "rounds": [3]})
        assert "ci_gate" not in merged
        assert merged["rounds"] == [3]


async def test_append_context_list_is_atomic_and_creates(tmp_path):
    import asyncio
    from no_human.core.db import Store
    from no_human.core.task import Task
    async with Store(tmp_path / "nh.db") as s:
        t = Task.new("x", repo_path="/tmp/x")
        await s.create_task(t)
        await asyncio.gather(
            s.append_context_list(t.id, "send_back_feedback", {"m": "a"}),
            s.append_context_list(t.id, "send_back_feedback", {"m": "b"}),
        )
        fb = (await s.get_task(t.id)).context["send_back_feedback"]
        assert sorted(x["m"] for x in fb) == ["a", "b"]  # both appends land


async def test_update_task_columns_never_touches_context(tmp_path):
    from no_human.core.db import Store
    from no_human.core.task import Task, TaskStatus
    async with Store(tmp_path / "nh.db") as s:
        t = Task.new("x", repo_path="/tmp/x")
        t.context = {"fresh": 1}
        await s.create_task(t)
        # Another writer merges while our copy is stale.
        await s.merge_context(t.id, {"concurrent": True})
        t.context = {"fresh": 1}  # stale copy — would clobber via update_task
        t.blocker = {"category": "AMBIGUITY", "question": "?"}
        await s.update_task_columns(t)
        fresh = await s.get_task(t.id)
        assert fresh.context.get("concurrent") is True  # survived
        assert fresh.blocker["category"] == "AMBIGUITY"  # column written


async def test_playbook_crud_and_project_scope(store):
    """1.4: operator playbooks round-trip; project scope includes globals."""
    pid = await store.add_playbook(
        title="P", trigger_keywords=["x"], procedure="do",
        postconditions=["done"], project="/tmp/r")
    await store.add_playbook(title="G", trigger_keywords=["y"])  # global
    scoped = await store.list_playbooks(project="/tmp/r")
    assert {p["title"] for p in scoped} == {"P", "G"}
    assert await store.delete_playbook(pid[:8]) is True
    assert {p["title"] for p in await store.list_playbooks()} == {"G"}


async def test_pr_edges_round_trip(store):
    """2.2: PR dependency edges round-trip and clear when a PR merges."""
    await store.add_pr_edge(child_pr="pr/2", parent_pr="pr/1")
    await store.add_pr_edge(child_pr="pr/3", parent_pr="pr/2")
    await store.add_pr_edge(child_pr="pr/2", parent_pr="pr/1")  # dup → ignored
    edges = await store.list_pr_edges()
    assert set(edges) == {("pr/2", "pr/1"), ("pr/3", "pr/2")}
    removed = await store.delete_pr_edges_for("pr/2")
    assert removed == 2  # both edges touching pr/2
    assert await store.list_pr_edges() == []


async def test_failed_attempt_never_has_an_empty_reason(store):
    """Store-level backstop (C2): a failed attempt with no reason is an
    observability bug — the row gets a loud sentinel instead of silence."""
    t = Task.new("x", repo_path="/tmp/r")
    await store.create_task(t)
    a = await store.create_attempt(t.id, 1)
    await store.update_attempt(a, status="failed", test_results={"ok": False})
    row = (await store.list_attempts(t.id))[-1]
    assert "no failure reason recorded" in (row["failure_reason"] or "")


async def test_failed_attempt_backstop_never_clobbers_a_real_reason(store):
    t = Task.new("x", repo_path="/tmp/r")
    await store.create_task(t)
    a = await store.create_attempt(t.id, 1)
    await store.update_attempt(a, failure_reason="tests failed: 1 failed")
    await store.update_attempt(a, status="failed")  # status set later
    row = (await store.list_attempts(t.id))[-1]
    assert row["failure_reason"] == "tests failed: 1 failed"


async def test_interrupted_attempts_get_a_reason_stamped(store):
    """Crash-orphaned rows (marked interrupted by the next create_attempt)
    must say why they stopped — they evaded both the failed-only backstop and
    the failed-only doctor check."""
    t = Task.new("x", repo_path="/tmp/r")
    await store.create_task(t)
    await store.create_attempt(t.id, 1)          # stays in_progress (crash)
    await store.create_attempt(t.id, 2)          # marks #1 interrupted
    rows = await store.list_attempts(t.id)
    assert rows[0]["status"] == "interrupted"
    assert "superseded by a newer attempt" in (rows[0]["failure_reason"] or "")


# --------------------------------------------------------------------------
# Attempt rows left open on a task that already finished. Measured 2026-08-11:
# 42 of them, oldest open 32 days; one task (`a8ffc957`) stayed hidden behind
# one for nine days, and `nh task cancel` creates them (it marks the task FAILED
# and leaves the row open). They also corrupt any event-window query, because a
# row with completed_at NULL coalesces to *now* and swallows every later event.
# --------------------------------------------------------------------------

async def _task_with_open_attempt(store, status: TaskStatus):
    t = Task.new(f"t-{status.value}", repo_path="/tmp/r")
    await store.create_task(t)
    await store.create_attempt(t.id, 1)
    event = {"source": "test", "kind": "test_seed"} if status is TaskStatus.DONE else None
    await store.set_status(t, status, validate=False, event=event)
    return t


async def _open_count(store, task_id: str) -> int:
    rows = await store.list_attempts(task_id)
    return sum(1 for r in rows if r["status"] == "in_progress")


async def test_open_attempts_on_finished_tasks_are_retired(store):
    done = await _task_with_open_attempt(store, TaskStatus.DONE)
    failed = await _task_with_open_attempt(store, TaskStatus.FAILED)
    assert await _open_count(store, done.id) == 1
    assert await _open_count(store, failed.id) == 1

    n = await store.close_attempts_of_terminal_tasks()

    assert n == 2, "both terminal tasks' rows should be retired"
    assert await _open_count(store, done.id) == 0
    assert await _open_count(store, failed.id) == 0


async def test_a_live_tasks_open_attempt_is_never_touched(store):
    """THE safety property. A task still in flight owns its open row; closing
    it would break the resume `latest_open_attempt` exists to protect."""
    live = await _task_with_open_attempt(store, TaskStatus.IMPLEMENTING)
    done = await _task_with_open_attempt(store, TaskStatus.DONE)

    n = await store.close_attempts_of_terminal_tasks()

    assert n == 1, "only the finished task's row may be retired"
    assert await _open_count(store, live.id) == 1, (
        "a running task's attempt row was closed out from under it")


async def test_retiring_is_idempotent_and_reports_zero_when_clean(store):
    await _task_with_open_attempt(store, TaskStatus.DONE)
    assert await store.close_attempts_of_terminal_tasks() == 1
    assert await store.close_attempts_of_terminal_tasks() == 0


async def test_an_existing_failure_reason_is_preserved(store):
    """COALESCE, not overwrite: a row that already explained itself keeps its
    explanation, so this reconciliation cannot erase a real diagnosis."""
    t = Task.new("t", repo_path="/tmp/r")
    await store.create_task(t)
    a = await store.create_attempt(t.id, 1)
    await store.update_attempt(a, failure_reason="the real reason")
    await store.set_status(t, TaskStatus.FAILED, validate=False)

    await store.close_attempts_of_terminal_tasks()

    rows = await store.list_attempts(t.id)
    assert rows[0]["failure_reason"] == "the real reason"


async def test_latest_review_attempt_and_verdict_agree_on_same_second_rows(store):
    """`latest_review_verdict` delegates to `latest_review_attempt` — the
    verdict is exactly `int(row["review_passed"])` of the row the other
    helper returns, even when two attempts land in the SAME second and only
    rowid breaks the tie."""
    t = Task.new("x", repo_path="/tmp/r")
    await store.create_task(t)
    a1 = await store.create_attempt(t.id, 1)
    await store.update_attempt(
        a1, status="succeeded", review_passed=1, started_at="2026-01-01 00:00:00")
    a2 = await store.create_attempt(t.id, 2)
    await store.update_attempt(
        a2, status="succeeded", review_passed=0, started_at="2026-01-01 00:00:00")

    rows = await store.list_attempts(t.id)
    assert rows[0]["started_at"] == rows[1]["started_at"], (
        "the fixture must exercise the rowid tie-break, not started_at ordering")

    row = await store.latest_review_attempt(t.id)
    assert row["review_passed"] == 0
    assert await store.latest_review_verdict(t.id) == 0
    assert await store.latest_review_verdict(t.id) == int(row["review_passed"])


async def test_latest_review_verdict_is_none_when_no_attempt_recorded_one(store):
    t = Task.new("x", repo_path="/tmp/r")
    await store.create_task(t)
    await store.create_attempt(t.id, 1)  # never records a review_passed

    assert await store.latest_review_attempt(t.id) is None
    assert await store.latest_review_verdict(t.id) is None


async def test_latest_failed_attempt_is_the_newest_reason_bearing_row(store):
    """The empty-string reason on #2 is treated as absent — #3's reason
    (same second as #1, so rowid breaks the tie) is what comes back."""
    t = Task.new("x", repo_path="/tmp/r")
    await store.create_task(t)
    a1 = await store.create_attempt(t.id, 1)
    await store.update_attempt(
        a1, status="failed", failure_reason="first failure",
        started_at="2026-01-01 00:00:00")
    a2 = await store.create_attempt(t.id, 2)
    await store.update_attempt(a2, status="succeeded", failure_reason="")
    a3 = await store.create_attempt(t.id, 3)
    await store.update_attempt(
        a3, status="failed", failure_reason="third failure",
        started_at="2026-01-01 00:00:00")

    row = await store.latest_failed_attempt(t.id)
    assert row["failure_reason"] == "third failure"


async def test_historical_compound_parent_and_subtask_rows_still_load(store):
    """The LeadAgent subsystem that WROTE `parent_id` and `COMPOUND_PARENT`
    was removed 2026-08-12 (operator decision A1), but rows it left behind
    before removal must still round-trip: the `parent_id` column and the
    `COMPOUND_PARENT` status stay in the schema/enum for exactly this reason,
    even though nothing creates new rows shaped this way any more."""
    parent = Task.new("compound", repo_path="/tmp/r")
    await store.create_task(parent)
    await store.set_status(parent, TaskStatus.IMPLEMENTING, validate=False)
    await store.set_status(parent, TaskStatus.COMPOUND_PARENT, validate=False)

    sub = Task.new("sub", repo_path="/tmp/r", parent_id=parent.id)
    await store.create_task(sub)

    reloaded_parent = await store.get_task(parent.id)
    assert reloaded_parent.status == TaskStatus.COMPOUND_PARENT

    reloaded_sub = await store.get_task(sub.id)
    assert reloaded_sub.parent_id == parent.id

    subtasks = await store.list_subtasks(parent.id)
    assert [s.id for s in subtasks] == [sub.id]
    assert await store.count_subtasks(parent.id) == 1


async def test_migration_normalizes_text_ts_to_epoch_float(tmp_path):
    """migrations/0014: a TEXT ts (the approved_landed_override incident
    shape — ISO written straight into the REAL column) is normalized to its
    equivalent epoch float on the next connect, byte-preserving every other
    column, with the instant itself round-tripping through datetime."""
    db_path = tmp_path / "t.db"
    original = datetime(2026, 8, 20, 12, 34, 56, 123000, tzinfo=timezone.utc)
    iso = original.isoformat()
    data = json.dumps({"kind": "approved_landed_override", "ts": iso})

    s1 = await Store(db_path).connect()
    t = Task.new("x", repo_path="/tmp/r")
    await s1.create_task(t)
    await s1.db.execute(
        "INSERT INTO task_events (task_id, ts, data) VALUES (?, ?, ?)",
        (t.id, iso, data),
    )
    await s1.db.commit()
    before_count = (await s1.query_one(
        "SELECT count(*) AS c FROM task_events"))["c"]
    await s1.close()

    # Reconnect: re-runs `_migrate`, which runs migrations/0014 again.
    s2 = await Store(db_path).connect()
    row = (await s2.query_one(
        "SELECT typeof(ts) AS t, ts, task_id, data FROM task_events "
        "WHERE task_id = ?", (t.id,)))
    assert row["t"] == "real"
    migrated = datetime.fromtimestamp(row["ts"], timezone.utc)
    assert abs(migrated - original) < timedelta(milliseconds=10)
    assert row["task_id"] == t.id
    assert row["data"] == data

    after_count = (await s2.query_one(
        "SELECT count(*) AS c FROM task_events"))["c"]
    assert after_count == before_count

    non_real = (await s2.query_one(
        "SELECT count(*) AS c FROM task_events WHERE typeof(ts) != 'real'"))["c"]
    assert non_real == 0
    await s2.close()


async def test_migration_is_idempotent_on_repeated_connect(tmp_path):
    """migrations/0014 re-runs on every connect (no schema-version gate in
    this repo) — a second reconnect after the rows are already REAL must
    leave the value and row count unchanged, never re-touch or drop rows."""
    db_path = tmp_path / "t.db"
    original = datetime(2026, 8, 20, 12, 34, 56, 123000, tzinfo=timezone.utc)
    iso = original.isoformat()

    s1 = await Store(db_path).connect()
    t = Task.new("x", repo_path="/tmp/r")
    await s1.create_task(t)
    await s1.db.execute(
        "INSERT INTO task_events (task_id, ts, data) VALUES (?, ?, ?)",
        (t.id, iso, json.dumps({"kind": "approved_landed_override"})),
    )
    await s1.db.commit()
    await s1.close()

    s2 = await Store(db_path).connect()
    row2 = (await s2.query_one(
        "SELECT ts FROM task_events WHERE task_id = ?", (t.id,)))
    count2 = (await s2.query_one("SELECT count(*) AS c FROM task_events"))["c"]
    await s2.close()

    # third connect (second reconnect after normalization) — pin idempotency
    s3 = await Store(db_path).connect()
    row3 = (await s3.query_one(
        "SELECT typeof(ts) AS t, ts FROM task_events WHERE task_id = ?", (t.id,)))
    count3 = (await s3.query_one("SELECT count(*) AS c FROM task_events"))["c"]
    non_real = (await s3.query_one(
        "SELECT count(*) AS c FROM task_events WHERE typeof(ts) != 'real'"))["c"]
    await s3.close()

    assert row3["t"] == "real"
    assert row3["ts"] == row2["ts"]
    assert count3 == count2
    assert non_real == 0


async def test_attempts_has_a_verifier_results_column(store):
    """migrations/0015 (comment-only) documents the column; the actual ALTER
    lives in `db.py`'s PRAGMA-guarded `att_wanted` dict — this proves the
    column really exists on a freshly connected store, not just in the
    migration file's prose."""
    cols = {row["name"] for row in await store.db.execute_fetchall(
        "PRAGMA table_info(attempts)")}
    assert "verifier_results" in cols


async def test_verifier_results_round_trips_and_survives_a_reconnect(tmp_path):
    """The `att_wanted` PRAGMA-guard runs on every connect, same as every
    other attempts column — a reconnect must not drop or reset a value
    already written, mirroring the `test_migration_is_idempotent_on_repeated_
    connect` pattern above for the review-history/task_events case."""
    db_path = tmp_path / "t.db"
    s1 = await Store(db_path).connect()
    t = Task.new("x", repo_path="/tmp/r")
    await s1.create_task(t)
    a = await s1.create_attempt(t.id, 1)
    verifiers = [{"verifier_id": "no-todo", "passed": False,
                  "no_verdict": False, "evidence": "e", "file": "a.py",
                  "line": 1, "comment": "c", "severity": "high",
                  "files_checked": ["a.py"], "tokens_used": 500}]
    await s1.update_attempt(a, verifier_results=verifiers)
    await s1.close()

    s2 = await Store(db_path).connect()
    row = await s2.query_one(
        "SELECT verifier_results FROM attempts WHERE id = ?", (a,))
    await s2.close()
    assert json.loads(row["verifier_results"]) == verifiers


# --- task_phases (PR-D1-3 / D1.1) ---------------------------------------------


@pytest.fixture
async def task(store):
    t = Task.new("phase task", repo_path="/tmp/r")
    await store.create_task(t)
    return t


@pytest.fixture
def freeze_time(monkeypatch):
    """A tick()-able clock that monkeypatches the timestamp function Store uses
    for started_at/ended_at (`db._now`), so a test can advance wall time in
    controlled steps without sleeping."""
    from no_human.core import db as _db

    class Clock:
        def __init__(self) -> None:
            self.t = datetime(2026, 1, 1, tzinfo=timezone.utc)

        def now(self) -> str:
            return self.t.isoformat()

        def tick(self, seconds: float) -> None:
            self.t += timedelta(seconds=seconds)

    clock = Clock()
    monkeypatch.setattr(_db, "_now", clock.now)
    return clock


async def test_open_phase_closes_previous_as_superseded(store, task):
    await store.open_phase(task.id, 1, "code")
    await store.open_phase(task.id, 1, "test")
    rows = await store.phases_for(task.id)
    assert [r["phase"] for r in rows] == ["code", "test"] and \
        rows[0]["outcome"] == "superseded" and rows[0]["ended_at"]


async def test_close_phase_records_outcome_and_reason(store, task):
    await store.open_phase(task.id, 1, "review")
    await store.close_phase(task.id, "failed", "tamper: test count dropped")
    assert (await store.phases_for(task.id))[-1]["reason"] == \
        "tamper: test count dropped"


async def test_active_seconds_sums_closed_and_open_phases(store, task, freeze_time):
    await store.open_phase(task.id, 1, "code")
    freeze_time.tick(60)
    await store.close_phase(task.id, "done")
    freeze_time.tick(600)  # parked gap — no open phase
    await store.open_phase(task.id, 2, "code")
    freeze_time.tick(30)
    assert abs(await store.active_seconds(task.id) - 90) < 1


async def test_set_status_records_phase_timeline(store, task, freeze_time):
    # D1.2: set_status is the ONE writer of task.status and now the writer of
    # the phase timeline. Drive a task through the happy-path spine and assert
    # each active state opened its phase, that they nest (each closed by the
    # next, no overlap), and that active_seconds is Σ of their durations with
    # the human-approval wait EXCLUDED — the exact "ran vs wall" the chip shows.
    await store.set_status(task, TaskStatus.CONTEXT)       # -> intake
    freeze_time.tick(10)
    await store.set_status(task, TaskStatus.PLANNING)      # -> plan
    freeze_time.tick(20)
    await store.set_status(task, TaskStatus.IMPLEMENTING)  # -> code
    freeze_time.tick(30)
    await store.set_status(task, TaskStatus.REVIEWING)     # -> review
    freeze_time.tick(40)
    await store.set_status(task, TaskStatus.TESTING)       # -> test
    freeze_time.tick(50)
    # awaiting_approval carries no phase: it CLOSES the open 'test' row so the
    # human-approval wait (the tick below) is not counted as active time.
    await store.set_status(task, TaskStatus.AWAITING_APPROVAL)
    freeze_time.tick(9999)  # human-approval wait — must NOT count

    rows = await store.phases_for(task.id)
    assert [r["phase"] for r in rows] == ["intake", "plan", "code", "review", "test"]
    for r in rows:
        assert r["ended_at"] is not None            # each closed by the next move
        assert r["started_at"] <= r["ended_at"]     # never negative/absurd
    # 10 + 20 + 30 + 40 + 50 = 150; the 9999s approval wait is excluded.
    assert abs(await store.active_seconds(task.id) - 150) < 1


async def test_set_status_idempotent_rewrite_records_no_phase(store, task):
    # A no-op rewrite (rowcount=1 but no boundary crossed) must NOT reset the
    # open phase's started_at — otherwise a re-asserted status would zero the
    # accrued active time. Guarded by `already_there` in set_status.
    await store.set_status(task, TaskStatus.CONTEXT)
    await store.set_status(task, TaskStatus.CONTEXT)  # idempotent
    rows = await store.phases_for(task.id)
    assert [r["phase"] for r in rows] == ["intake"]  # one row, not two
