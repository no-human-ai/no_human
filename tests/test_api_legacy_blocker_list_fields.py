"""GET /api/tasks must not 500 on a legacy blocker row whose question/
category/wake_condition were persisted as JSON lists.

Independent-review advisory F3 on the blocker-prose normalisation landed
2026-09-01 (e18192e04): `Blocker.from_dict` now coerces list-shaped prose via
`blockers/taxonomy.py`'s `_prose`/`_machine_scalar`/`BlockerCategory.coerce`,
so no NEW blocker can carry a list into `task.blocker`. But rows persisted
BEFORE that landing still hold raw lists on disk, and
`TaskSummaryOut.from_task` (api/models.py) used to lift `question`/
`category`/`wake_condition` straight out of the dict into `str | None`
fields — a list there raises a pydantic ValidationError, 500ing the whole
board list endpoint for every caller the moment one such legacy row exists.

Mirrors tests/test_api_task_follows.py's client/store fixtures.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from no_human.api.app import app
from no_human.core.task import Task, TaskStatus

pytestmark = pytest.mark.usefixtures("isolated_env_file")


@pytest_asyncio.fixture
async def client(store, tmp_path):
    from no_human.config import load_config
    app.state.store = store
    app.state.config = load_config(tmp_path / "config.yaml")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost") as c:
        yield c


async def _blocked_task(store, blocker: dict) -> Task:
    t = Task.new("legacy blocker row", repo_path="/tmp/repo")
    t.status = TaskStatus.BLOCKED
    t.blocker = blocker
    await store.create_task(t)
    return t


@pytest.mark.asyncio
async def test_legacy_list_shaped_question_renders_instead_of_500(client, store):
    # A real incident row (task description: "evidence as a list"):
    # question/wake_condition persisted as JSON arrays, from before the
    # blocker-prose normalisation existed.
    task = await _blocked_task(store, {
        "category": "AMBIGUITY",
        "question": ["Which repo should this ship to?", "org/a or org/b?"],
        "wake_condition": ["after:2h"],
    })

    r = await client.get("/api/tasks")

    assert r.status_code == 200, r.text
    rows = {row["id"]: row for row in r.json()}
    assert task.id in rows
    row = rows[task.id]
    # `_prose` joins list lines for a prose field.
    assert row["blocker_question"] == (
        "Which repo should this ship to?\norg/a or org/b?"
    )
    # `_machine_scalar` unwraps a single-element list.
    assert row["blocker_wake_condition"] == "after:2h"
    assert row["blocker_category"] == "AMBIGUITY"


@pytest.mark.asyncio
async def test_legacy_multi_element_wake_condition_list_does_not_fabricate(
    client, store,
):
    # `_machine_scalar`: a MULTI-element list is not one machine-checkable
    # condition, so it becomes None rather than joining into something that
    # could self-fire early (see taxonomy.py's `_machine_scalar` docstring).
    task = await _blocked_task(store, {
        "category": "DEPENDENCY_WAIT",
        "question": "plain string, unaffected",
        "wake_condition": ["after:2h", "and PR org/repo#12 merged"],
    })

    r = await client.get("/api/tasks")

    assert r.status_code == 200, r.text
    rows = {row["id"]: row for row in r.json()}
    row = rows[task.id]
    assert row["blocker_wake_condition"] is None
    assert row["blocker_question"] == "plain string, unaffected"


@pytest.mark.asyncio
async def test_string_shaped_blocker_row_is_byte_identical(client, store):
    """Mirror: an ordinary, already-well-formed blocker row must serialise
    exactly as it did before the fix — the coercion is a no-op on scalars."""
    task = await _blocked_task(store, {
        "category": "AMBIGUITY",
        "question": "Which config key controls this?",
        "wake_condition": "after:2h",
        "human_stopped": False,
    })

    r = await client.get("/api/tasks")

    assert r.status_code == 200, r.text
    rows = {row["id"]: row for row in r.json()}
    row = rows[task.id]
    assert row["blocker_question"] == "Which config key controls this?"
    assert row["blocker_category"] == "AMBIGUITY"
    assert row["blocker_wake_condition"] == "after:2h"
    assert row["blocker_human_stopped"] is False
