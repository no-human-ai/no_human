"""A human's budget raise must survive a concurrent watcher tick.

2026-09-13 incident: a human raised `lifetime_tokens` 10,000,000 ->
14,000,000 on a BUDGET_EXHAUSTED-parked task via `nh task config ID
lifetime_tokens=14000000` — the documented human-only escape from that park.
The CLI confirmed `applied lifetime_tokens=14000000`. A watcher tick holding
an in-memory `Task` handle SNAPSHOTTED BEFORE that raise then called
`update_task`/`update_task_columns`, which wrote `config=:config` from the
stale handle wholesale — silently reverting the raise back to 10,000,000
(and, since it is a whole-blob restore, `attempt_tokens`/`budget_unit` too).

`config` was the one remaining multi-writer column (alongside `status`,
`title`, `context`) with no concurrent-write protection: `status` is
protected by exclusion (`update_task`/`update_task_columns` never write it),
`title` by a `context.title_updated_at`-keyed CASE guard, `context` by
merge-only semantics (`merge_context`/`append_context_list`). The fix gives
`config` the identical marker-and-CASE-guard mechanism as `title`
(`context.config_updated_at`), plus a new `Store.update_task_config()`
single-column writer (modelled verbatim on `update_task_title`) that CLI/API
callers use to persist a `set_task_config` change before their own
now-current handle goes on to call `update_task_columns`/`update_task`.

Idiom copied from `tests/test_task_retitle.py`: sync tests, each owning its
own `asyncio.run`, `_bootstrap` patched via `mock.patch.object`, real
`CliRunner` invocations through the `task` group (`nh task config`) and the
top-level `cli` group (`nh reply`). Helpers copied locally, not imported.
"""

from __future__ import annotations

import asyncio
import inspect
import re
import unittest.mock as mock

from click.testing import CliRunner

from no_human.blockers import Blocker, BlockerCategory, BlockerOption
from no_human.cli.commands import cli, task
from no_human.core.db import Store
from no_human.core.task import Task, TaskStatus

# --------------------------------------------------------------------------- #
# CLI plumbing (copied from tests/test_task_retitle.py)                       #
# --------------------------------------------------------------------------- #

class _Cfg:
    db_path = None
    data: dict = {}

    def get(self, key, default=None):
        return self.data.get(key, default)


def _cfg(db_path):
    c = _Cfg()
    c.db_path = db_path
    return c


def _invoke(cmd, db, args):
    import no_human.cli.commands as cmd_mod
    with mock.patch.object(cmd_mod, "_bootstrap",
                           lambda require_auth=False: (_cfg(db), None)):
        return CliRunner().invoke(cmd, args)


def _task_state(db, task_id):
    async def _go():
        async with Store(db) as store:
            t = await store.get_task(task_id)
            events = await store.list_events(task_id)
            return t, events
    return asyncio.run(_go())


# --------------------------------------------------------------------------- #
# Task fixtures                                                              #
# --------------------------------------------------------------------------- #

_RAISED_CONFIG = {
    "attempt_tokens": 4_000_000,
    "lifetime_tokens": 10_000_000,
    "budget_unit": "weighted",
}


async def _seed(db, *, status, config=None, title="budget test task"):
    async with Store(db) as store:
        t = Task.new(title, repo_path="/tmp/does-not-matter")
        t.status = status
        t.acceptance_criteria = ["it does the thing"]
        t.context = {}
        t.config = dict(config) if config is not None else dict(_RAISED_CONFIG)
        await store.create_task(t)
        return t.id


async def _seed_blocked_task(db, *, config=None):
    """Mirrors `tests/test_cli_commands.py::_seed_blocked_task`, but the
    raise option carries a `lifetime_tokens` bump so `nh reply --choose`
    exercises the exact action the 2026-09-13 incident lost."""
    async with Store(db) as store:
        t = Task.new("budget exhausted task", repo_path="/tmp/repo")
        t.config = dict(config) if config is not None else dict(_RAISED_CONFIG)
        await store.create_task(t)
        t.blocker = Blocker(
            category=BlockerCategory.BUDGET_EXHAUSTED,
            confidence=0.9,
            question="This task exhausted its lifetime token budget.",
            options=[
                BlockerOption(label="stop here"),
                BlockerOption(
                    label="raise the lifetime budget",
                    action={"set_task_config": {"lifetime_tokens": 14_000_000}},
                ),
            ],
            resume_branch="scratch/x/abc-2",
            resume_commit="75c68e08",
        ).to_dict()
        await store.update_task(t)
        await store.set_status(t, TaskStatus.ESCALATED, validate=False)
        return t.id


# --------------------------------------------------------------------------- #
# AC1+AC3 — a stale watcher handle must not revert a raised lifetime cap     #
# --------------------------------------------------------------------------- #

def test_stale_handle_update_task_does_not_revert_a_raised_lifetime_cap(
    tmp_path,
):
    db = tmp_path / "nh.db"
    tid = asyncio.run(_seed(db, status=TaskStatus.AWAITING_APPROVAL))

    async def _load():
        async with Store(db) as store:
            return await store.get_task(tid)  # watcher's snapshot BEFORE the raise
    stale = asyncio.run(_load())

    result = _invoke(task, db, ["config", tid, "lifetime_tokens=14000000"])
    assert result.exit_code == 0, result.output
    assert "lifetime_tokens=14000000" in result.output

    async def _stale_write():
        async with Store(db) as store:
            await store.update_task(stale)
    asyncio.run(_stale_write())

    t, _ = _task_state(db, tid)
    assert t.config["lifetime_tokens"] == 14_000_000, t.config
    # Whole-blob restore is the reported failure mode — assert the siblings
    # the stale handle carried were not clobbered back either.
    assert t.config["attempt_tokens"] == 4_000_000, t.config
    assert t.config["budget_unit"] == "weighted", t.config


def test_stale_handle_update_task_columns_does_not_revert_a_raised_lifetime_cap(
    tmp_path,
):
    """The exact path the watcher tick took in the reported incident."""
    db = tmp_path / "nh.db"
    tid = asyncio.run(_seed(db, status=TaskStatus.AWAITING_APPROVAL))

    async def _load():
        async with Store(db) as store:
            return await store.get_task(tid)
    stale = asyncio.run(_load())

    result = _invoke(task, db, ["config", tid, "lifetime_tokens=14000000"])
    assert result.exit_code == 0, result.output

    async def _stale_write():
        async with Store(db) as store:
            await store.update_task_columns(stale)
    asyncio.run(_stale_write())

    t, _ = _task_state(db, tid)
    assert t.config["lifetime_tokens"] == 14_000_000, t.config
    assert t.config["attempt_tokens"] == 4_000_000, t.config
    assert t.config["budget_unit"] == "weighted", t.config


# --------------------------------------------------------------------------- #
# AC4 — a fresh handle can still lower AND raise config through the guard    #
# --------------------------------------------------------------------------- #

def test_a_fresh_handle_can_still_lower_and_raise_config(tmp_path):
    db = tmp_path / "nh.db"
    tid = asyncio.run(_seed(db, status=TaskStatus.AWAITING_APPROVAL))

    result = _invoke(task, db, ["config", tid, "lifetime_tokens=14000000"])
    assert result.exit_code == 0, result.output

    async def _fresh_edit():
        async with Store(db) as store:
            t = await store.get_task(tid)  # loaded AFTER the raise landed
            t.config["attempt_tokens"] = 2_000_000
            await store.update_task_columns(t)
    asyncio.run(_fresh_edit())

    t, _ = _task_state(db, tid)
    assert t.config["attempt_tokens"] == 2_000_000, t.config
    # The raise this fresh handle already carried in its own `config` dict
    # must survive too — the guard equal-or-newer marker means the handle
    # wins, not a wholesale drop of its write.
    assert t.config["lifetime_tokens"] == 14_000_000, t.config


def test_task_config_cli_still_reports_and_persists_priority_and_config_together(
    tmp_path,
):
    db = tmp_path / "nh.db"
    tid = asyncio.run(_seed(db, status=TaskStatus.AWAITING_APPROVAL))

    result = _invoke(
        task, db, ["config", tid, "lifetime_tokens=14000000", "priority=high"])
    assert result.exit_code == 0, result.output
    assert "lifetime_tokens=14000000" in result.output

    t, _ = _task_state(db, tid)
    assert t.config["lifetime_tokens"] == 14_000_000, t.config
    assert t.priority == "high", t.priority


# --------------------------------------------------------------------------- #
# AC2 — every multi-writer task column is excluded or guarded                #
# --------------------------------------------------------------------------- #

def _update_sql(method) -> str:
    """Pull just the `UPDATE tasks SET ... WHERE id=:id` literal(s) out of a
    Store method's source — NOT its docstring/comments, which name these
    columns in prose (e.g. `update_task`'s own incident writeup mentions the
    literal text `config=:config`) and would otherwise false-positive."""
    src = inspect.getsource(method)
    return "\n".join(re.findall(r"UPDATE tasks SET.*?WHERE id=:id", src, re.DOTALL))


def test_every_multi_writer_task_column_is_excluded_or_guarded():
    update_task_sql = _update_sql(Store.update_task)
    update_task_columns_sql = _update_sql(Store.update_task_columns)
    assert update_task_sql and update_task_columns_sql  # sanity: extraction worked

    # `status`: excluded from both — never appears as a plain `status=:status`
    # assignment (its one legitimate writer is `set_status`).
    assert re.search(r"\bstatus\s*=\s*:status\b", update_task_sql) is None
    assert re.search(r"\bstatus\s*=\s*:status\b", update_task_columns_sql) is None

    # `context`: `update_task_columns` never writes it at all (multi-writer
    # zones must go through `merge_context`/`append_context_list`); `update_task`
    # writes it only via `json_patch`, never a raw `context=:context` clobber.
    assert re.search(r"\bcontext\s*=\s*:context\b", update_task_columns_sql) is None
    assert "context = json_patch(" in update_task_sql
    assert re.search(r"\bcontext\s*=\s*:context\b", update_task_sql) is None

    # `title` and `config`: both present, both CASE-guarded on their own
    # `context.*_updated_at` marker rather than a bare `col=:col` assignment.
    for col, marker in (("title", "title_updated_at"),
                        ("config", "config_updated_at")):
        for sql in (update_task_sql, update_task_columns_sql):
            assert re.search(rf"\b{col}\s*=\s*:{col}\b", sql) is None, (
                f"{col} is written unguarded in {sql[:40]!r}...")
            case_pattern = (
                rf"{col}\s*=\s*CASE WHEN.*?\$\.{marker}.*?THEN {col} ELSE :{col} END"
            )
            assert re.search(case_pattern, sql, re.DOTALL), (
                f"{col} is missing its {marker}-keyed CASE guard")


# --------------------------------------------------------------------------- #
# AC3 — the same race through `nh reply --choose`'s set_task_config path     #
# --------------------------------------------------------------------------- #

def test_stale_handle_does_not_revert_a_budget_raise_applied_via_reply_choose(
    tmp_path,
):
    db = tmp_path / "nh.db"
    tid = asyncio.run(_seed_blocked_task(db))

    async def _load():
        async with Store(db) as store:
            return await store.get_task(tid)  # watcher's snapshot BEFORE the reply
    stale = asyncio.run(_load())

    result = _invoke(cli, db, ["reply", tid, "--choose", "2", "--no-run"])
    assert result.exit_code == 0, result.output
    assert "lifetime_tokens=14000000" in result.output

    async def _stale_write():
        async with Store(db) as store:
            await store.update_task_columns(stale)
    asyncio.run(_stale_write())

    t, _ = _task_state(db, tid)
    assert t.config["lifetime_tokens"] == 14_000_000, t.config
    assert t.config["attempt_tokens"] == 4_000_000, t.config
