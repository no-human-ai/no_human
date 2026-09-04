"""Task 7: `follows_id` — a follow-up task's sibling link to the task it
continues. Deliberately NOT `parent_id`: that column is the compound-child
relation `core/task.py`'s transition table still knows about (COMPOUND_PARENT,
~line 170) even though nothing creates new rows shaped that way any more (the
LeadAgent decomposition subsystem was deleted 2026-08-12) — reusing it would
make a follow-up render as a sub-task with a progress chip on its predecessor.
"""

from no_human.core.task import Task


async def test_follows_id_round_trips(store, tmp_path):
    a = Task.new("a", repo_path=str(tmp_path))
    await store.create_task(a)
    b = Task.new("b", repo_path=str(tmp_path), follows_id=a.id)
    await store.create_task(b)

    got = await store.get_task(b.id)
    assert got.follows_id == a.id
    # a follow-up is NOT a compound child: count_subtasks/list_subtasks key off
    # parent_id only, and b never set that column.
    assert await store.count_subtasks(a.id) == 0
    assert await store.list_subtasks(a.id) == []


async def test_follows_id_defaults_to_none(store, tmp_path):
    t = Task.new("plain", repo_path=str(tmp_path))
    await store.create_task(t)
    got = await store.get_task(t.id)
    assert got.follows_id is None


async def test_follows_id_survives_update_task(store, tmp_path):
    """update_task's SET list names columns explicitly (db.py ~1594) — a typo'd
    or omitted follows_id there would silently null the column on the first
    unrelated mutation of the follow-up row."""
    a = Task.new("a", repo_path=str(tmp_path))
    await store.create_task(a)
    b = Task.new("b", repo_path=str(tmp_path), follows_id=a.id)
    await store.create_task(b)

    b.priority = "high"
    await store.update_task(b)

    reloaded = await store.get_task(b.id)
    assert reloaded.follows_id == a.id
    assert reloaded.priority == "high"


async def test_follows_id_survives_update_task_columns(store, tmp_path):
    """Same guard for the second writer of this column list (db.py ~1724)."""
    a = Task.new("a", repo_path=str(tmp_path))
    await store.create_task(a)
    b = Task.new("b", repo_path=str(tmp_path), follows_id=a.id)
    await store.create_task(b)

    b.priority = "low"
    await store.update_task_columns(b)

    reloaded = await store.get_task(b.id)
    assert reloaded.follows_id == a.id
    assert reloaded.priority == "low"
