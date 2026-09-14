"""#343: a watcher tick with nothing to do silently reverted a human's budget
raise.

`nh task config <id> lifetime_tokens=N` is the documented human-only escape
from a BUDGET_EXHAUSTED park. `update_task_columns` wrote `config` wholesale
from whatever the calling handle had loaded, and `blockers/wake.py` calls that
method from 14 sites — including paths that conclude "nothing to do". So a
raise landing while a tick was in flight was written back to its pre-raise
value: no event, no error, and a CLI that had already reported success. The
operator's next signal was the task dying at a cap they believed they lifted.

`update_task`'s copy of the same statement had the same exposure over a much
longer window — an orchestrator handle lives for a whole attempt.

The fix mirrors what `status` (set_status) and `context` (merge_context)
already do: `config` leaves both column lists and gets one writer,
`update_task_config`. These tests pin the exclusion AND the columns those two
methods must still write, so the exclusion cannot be widened by accident.
"""
from __future__ import annotations

import pytest

from no_human.core.task import Task, TaskStatus

pytestmark = pytest.mark.asyncio

RAISED = {"lifetime_tokens": 14_000_000, "budget_unit": "weighted"}
PRE_RAISE = {"lifetime_tokens": 10_000_000, "budget_unit": "weighted"}


async def _task_with_pre_raise_config(store) -> Task:
    t = Task.new("x", repo_path="/tmp/r")
    t.config = dict(PRE_RAISE)
    await store.create_task(t)
    return t


# --------------------------------------------------------------------------- #
# 1. A stale handle cannot revert a config write                               #
# --------------------------------------------------------------------------- #


async def test_a_watcher_tick_cannot_revert_a_budget_raise(store):
    """The reported incident: the tick's handle predates the raise."""
    t = await _task_with_pre_raise_config(store)
    stale = await store.get_task(t.id)           # the tick's snapshot

    await store.update_task_config(t.id, dict(RAISED))   # the human's raise

    stale.wake_check_at = None                   # what the tick actually wanted
    await store.update_task_columns(stale)

    fresh = await store.get_task(t.id)
    assert fresh.config["lifetime_tokens"] == 14_000_000


async def test_an_orchestrator_handle_cannot_revert_a_budget_raise(store):
    """Same column, same wholesale write, via `update_task` — the window here
    is a whole attempt, not a tick."""
    t = await _task_with_pre_raise_config(store)
    stale = await store.get_task(t.id)

    await store.update_task_config(t.id, dict(RAISED))

    stale.plan = "step 1"
    await store.update_task(stale)

    fresh = await store.get_task(t.id)
    assert fresh.config["lifetime_tokens"] == 14_000_000


async def test_a_raise_survives_repeated_stale_write_backs(store):
    """It was self-concealing on retry: raise again, and the next tick could
    revert it again. Once is not enough to pin that."""
    t = await _task_with_pre_raise_config(store)
    stale = await store.get_task(t.id)

    await store.update_task_config(t.id, dict(RAISED))
    for _ in range(3):
        await store.update_task_columns(stale)
        await store.update_task(stale)

    assert (await store.get_task(t.id)).config["lifetime_tokens"] == 14_000_000


# --------------------------------------------------------------------------- #
# 2. Accept controls: the two writers still write what they own               #
# --------------------------------------------------------------------------- #


async def test_update_task_columns_still_writes_its_own_columns(store):
    """Excluding one column must not quietly cost the others — the watcher
    writes `blocker`/`wake_check_at`/`priority` through this method."""
    t = await _task_with_pre_raise_config(store)
    t.blocker = {"category": "AMBIGUITY", "question": "?"}
    t.wake_check_at = "2026-09-14T10:00:00Z"
    t.priority = "high"
    t.plan = "step 1"

    await store.update_task_columns(t)

    fresh = await store.get_task(t.id)
    assert fresh.blocker["category"] == "AMBIGUITY"
    assert fresh.wake_check_at == "2026-09-14T10:00:00Z"
    assert fresh.priority == "high"
    assert fresh.plan == "step 1"


async def test_update_task_still_writes_its_own_columns(store):
    t = await _task_with_pre_raise_config(store)
    t.title = "renamed"
    t.description = "d"
    t.plan = "step 1"
    t.context = {"k": "v"}

    await store.update_task(t)

    fresh = await store.get_task(t.id)
    assert fresh.title == "renamed"
    assert fresh.description == "d"
    assert fresh.plan == "step 1"
    assert fresh.context["k"] == "v"


# --------------------------------------------------------------------------- #
# 3. The new writer is targeted: it writes config and nothing else             #
# --------------------------------------------------------------------------- #


async def test_update_task_config_writes_the_config_and_touches_the_row(store):
    """`updated_at` has to move: the scheduler's orphan sweep reads it, and a
    config write that left the row looking untouched would be a second way for
    this change to be invisible."""
    t = await _task_with_pre_raise_config(store)
    before = (await store.get_task(t.id)).updated_at

    await store.update_task_config(t.id, dict(RAISED))

    fresh = await store.get_task(t.id)
    assert fresh.config == RAISED
    assert fresh.updated_at > before


async def test_update_task_config_does_not_carry_a_stale_row_back(store):
    """The mirror image of the bug: this writer must not read-modify-write the
    row, or the CLI's own raise would revert whatever landed underneath it."""
    t = await _task_with_pre_raise_config(store)
    await store.set_status(t, TaskStatus.REVIEWING, validate=False)
    t.title = "renamed"
    await store.update_task_columns(t)

    await store.update_task_config(t.id, dict(RAISED))

    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.REVIEWING
    assert fresh.title == "renamed"
    assert fresh.config["lifetime_tokens"] == 14_000_000


async def test_an_empty_config_is_stored_as_an_object_not_null(store):
    """`Task.from_row` reads this column back through json.loads; a bare NULL
    or "" would come back as something no caller subscripts."""
    t = await _task_with_pre_raise_config(store)

    await store.update_task_config(t.id, {})

    assert (await store.get_task(t.id)).config == {}
