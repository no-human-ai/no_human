"""A human landing/approval event records a PROCESS-DERIVED actor, so a
landing is attributable when several agent sessions share one git identity
(fleet task 61c219c8).

Two properties are load-bearing and both are asserted here:

* the actor comes from the SERVER process (pid + the `NO_HUMAN_AGENT_SESSION`
  mark when present), never from a client-supplied value — an inbound request
  header (`x-request-id` or any other) must NOT appear in it; and
* the actor is sanitised to `[A-Za-z0-9._:-]`, so it cannot smuggle rich
  markup (`[...]`) through `nh task show`.

`nh task show` must also actually render the actor for every completion kind
we stamp it on — otherwise the attribution is written but invisible.
"""

from __future__ import annotations

import os
import subprocess

import pytest
import pytest_asyncio
from click.testing import CliRunner
from httpx import ASGITransport, AsyncClient

from no_human.api.app import app
from no_human.blockers import process_actor
from no_human.blockers.landed_override import (
    LANDED_OVERRIDE_KIND, approve_landed_override,
)
from no_human.cli.commands import _COMPLETION_EVENT_KINDS, cli
from no_human.core.db import Store
from no_human.core.task import Task, TaskStatus

pytestmark = pytest.mark.usefixtures("isolated_env_file")


# --------------------------------------------------------------------------- #
# process_actor — the security core                                           #
# --------------------------------------------------------------------------- #

def test_actor_is_process_derived(monkeypatch):
    monkeypatch.delenv("NO_HUMAN_AGENT_SESSION", raising=False)
    monkeypatch.delenv("NO_HUMAN_AGENT_SESSION_KIND", raising=False)
    assert process_actor() == f"pid:{os.getpid()}"


def test_actor_folds_the_session_mark_when_present(monkeypatch):
    monkeypatch.setenv("NO_HUMAN_AGENT_SESSION", "1")
    monkeypatch.setenv("NO_HUMAN_AGENT_SESSION_KIND", "claude")
    actor = process_actor()
    assert actor == f"pid:{os.getpid()}:session:claude"


def test_actor_neutralises_markup_and_truncates(monkeypatch):
    # A mark kind carrying rich markup + separators must not survive into a
    # string that `nh task show` renders through the markup path.
    monkeypatch.setenv("NO_HUMAN_AGENT_SESSION", "1")
    monkeypatch.setenv("NO_HUMAN_AGENT_SESSION_KIND", "[bold]evil[/] a b" + "x" * 200)
    actor = process_actor()
    assert "[" not in actor and "]" not in actor and " " not in actor
    assert len(actor) <= 96


# --------------------------------------------------------------------------- #
# API landing endpoints — each records the actor, none leaks a header         #
# --------------------------------------------------------------------------- #

@pytest_asyncio.fixture
async def store(tmp_path):
    s = await Store(tmp_path / "test.db").connect()
    yield s
    await s.close()


@pytest_asyncio.fixture
async def client(store, tmp_path):
    from no_human.config import load_config
    app.state.store = store
    app.state.config = load_config(tmp_path / "config.yaml")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost") as c:
        yield c


async def _seed(store, *, status=TaskStatus.AWAITING_APPROVAL, context=None):
    t = Task.new("Fix thing", repo_path="/tmp/repo")
    t.acceptance_criteria = ["Should work"]
    if context:
        t.context = context
    await store.create_task(t)
    if status != TaskStatus.PENDING:
        await store.set_status(t, status, validate=False)
    return t


@pytest.mark.asyncio
async def test_shipped_records_process_actor(client, store):
    t = await _seed(store)
    r = await client.post(f"/api/tasks/{t.id}/shipped", json={"sha": "deadbeef1234"})
    assert r.status_code == 200, r.text
    ev = [e for e in await store.list_events(t.id) if e.get("kind") == "human_merged"][0]
    assert ev["actor"] == process_actor()
    assert ev["actor"].startswith("pid:")


@pytest.mark.asyncio
async def test_shipped_request_header_does_not_leak_into_actor(client, store):
    """The injection guard: an attacker-controllable request header must not
    reach the recorded actor. `x-request-id` is the exact header an earlier
    attempt's reviewer caught being folded in."""
    t = await _seed(store)
    poison = "attacker-injected-actor-value"
    r = await client.post(
        f"/api/tasks/{t.id}/shipped", json={"sha": "deadbeef1234"},
        headers={"x-request-id": poison, "x-forwarded-for": poison},
    )
    assert r.status_code == 200, r.text
    ev = [e for e in await store.list_events(t.id) if e.get("kind") == "human_merged"][0]
    assert poison not in ev["actor"]
    assert ev["actor"] == process_actor()


@pytest.mark.asyncio
async def test_finish_review_records_actor(client, store):
    t = await _seed(store)
    r = await client.post(f"/api/tasks/{t.id}/finish-review")
    assert r.status_code == 200, r.text
    ev = [e for e in await store.list_events(t.id) if e.get("kind") == "review_finished"][0]
    assert ev["actor"] == process_actor()


@pytest.mark.asyncio
async def test_approve_already_satisfied_records_actor(client, store):
    # already-satisfied report + no PR -> the approve route completes here
    # with an `approved_already_satisfied` event.
    t = await _seed(store, context={
        "already_satisfied_report": "nothing to change",
        # Satisfying commit reachable from origin/base — truly nothing to
        # land; without this the landing classifier would re-derive from
        # git and find no repo at the seeded `repo_path`, refusing.
        "already_satisfied_landing": {
            "on_base": True, "sha": "deadbeef", "branch": "",
            "ship_ref": "origin/main",
        },
    })
    r = await client.post(f"/api/tasks/{t.id}/approve")
    assert r.status_code == 200, r.text
    ev = [e for e in await store.list_events(t.id)
          if e.get("kind") == "approved_already_satisfied"][0]
    assert ev["actor"] == process_actor()


# --------------------------------------------------------------------------- #
# landed-override — real repo, actor on the audit event                       #
# --------------------------------------------------------------------------- #

def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _make_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("orig\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "initial")
    return repo


@pytest.mark.asyncio
async def test_landed_override_records_actor(tmp_path, store):
    repo = _make_repo(tmp_path)
    sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        text=True, capture_output=True, check=True).stdout.strip()
    t = Task.new("Fix thing", repo_path=str(repo))
    t.context = {"base_branch": "main", "pr_branch": "feature"}
    await store.create_task(t)
    await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)

    await approve_landed_override(store, t, sha, "supervisor squash train — eyeball diff")

    ev = [e for e in await store.list_events(t.id)
          if e.get("kind") == LANDED_OVERRIDE_KIND][0]
    assert ev["actor"] == process_actor()


# --------------------------------------------------------------------------- #
# render coverage — nh task show displays the actor for every kind we stamp   #
# --------------------------------------------------------------------------- #

def test_every_stamped_kind_is_in_the_completion_render_set():
    for kind in ("human_merged", "approved_already_satisfied",
                 "review_finished", LANDED_OVERRIDE_KIND):
        assert kind in _COMPLETION_EVENT_KINDS, kind


def _make_runner(path, monkeypatch):
    import no_human.cli.commands as cmd_mod

    class _Cfg:
        data: dict = {}
        db_path = path

        def get(self, key, default=None):
            return self.data.get(key, default)

    monkeypatch.setattr(cmd_mod, "load_config", lambda: _Cfg())
    monkeypatch.setattr(cmd_mod, "assert_subscription_mode", lambda **kw: None)
    return CliRunner()


@pytest.mark.parametrize("kind", [
    "human_merged", "approved_already_satisfied",
    "review_finished", LANDED_OVERRIDE_KIND,
])
def test_task_show_renders_the_actor(tmp_path, monkeypatch, kind):
    import asyncio

    db = tmp_path / "test.db"
    actor = "pid:4242:session:claude"

    async def _seed_done():
        async with Store(db) as s:
            t = Task.new("Fix thing", repo_path="/tmp/repo")
            await s.create_task(t)
            await s.set_status(
                t, TaskStatus.DONE, validate=False,
                event={"source": "human", "kind": kind,
                       "text": "landed", "actor": actor})
            return t.id

    tid = asyncio.run(_seed_done())
    runner = _make_runner(db, monkeypatch)
    result = runner.invoke(cli, ["task", "show", tid[:8]])
    assert result.exit_code == 0, result.output
    assert actor in result.output, result.output
