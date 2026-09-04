"""The coder's full final report has a human-reachable surface.

The PR body's implementation summary is cut at `Orchestrator._SUMMARY_MAX_CHARS`
(4000 chars) — a real cap, kept exactly where it was. What used to be missing is
somewhere the CUT TAIL could still be read: `attempts.full_final_text` now
stores the coder's raw `result.final_text` in full, and `report_surface.
render_full_report` (through `TaskOut.full_report` and `nh task show`) renders it —
filtered through the SAME drop-marker filter and outbound scrub the PR body
gets, but never capped.

These tests exercise the four things that make that claim true: the two
surfaces actually expose it, a filtered paragraph is filtered on both, a tail
that the PR body cuts survives on the new surface, and the truncation marker
now names that surface instead of claiming there is none.
"""

from __future__ import annotations

import asyncio
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from click.testing import CliRunner
from httpx import AsyncClient, ASGITransport

from no_human.agent.claude_backend import AgentResult
from no_human.api.app import app
from no_human.cli.commands import cli
from no_human.config import load_config
from no_human.core.db import Store
from no_human.core.orchestrator import Orchestrator, _FULL_REPORT_MAX_CHARS
from no_human.core.report_surface import render_full_report
from no_human.core.task import Task, TaskStatus
from no_human.notify.slack import SlackNotifier
from no_human.vcs import GitRepo

pytestmark = pytest.mark.usefixtures("isolated_env_file")

_ORCHESTRATOR_SRC = Path(__file__).resolve().parent.parent / "src" / "no_human" / "core" / "orchestrator.py"


def _unfold(text: str) -> str:
    """Strip the report fold's own markup so a length check sees the coder's
    text alone (`Orchestrator._fold_report`, mirrors `tests/test_pr_hygiene.py`)."""
    return re.sub(r"\n\n<details><summary>Rest of the coder's report[^<]*</summary>\n\n"
                  r"|\n\n</details>", "", text)


class _Backend:
    async def run(self, *a, **k):  # pragma: no cover
        raise AssertionError("backend should not run here")


class _Commit:
    files_changed = 2
    insertions = 10
    deletions = 1


class _Result:
    final_text = "did the thing"
    num_turns = 5


def _orch(store, tmp_path):
    cfg = load_config(tmp_path / "config.yaml")
    return Orchestrator(store, cfg.data, _Backend(), SlackNotifier(None))


def _attempt_config(tmp_path):
    """Mirrors `tests/test_infra_not_work.py`'s `_config` — disables planning/
    reviewer so `_run_attempt` reaches the coder-usage chokepoint without
    needing a review backend."""
    cfg = load_config(tmp_path / "config.yaml")
    cfg.data.setdefault("planning", {})["enabled"] = False
    cfg.data.setdefault("reviewer", {})["allow_advisory"] = True
    return cfg


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def bare_repo(tmp_path):
    """A real git remote + clone `_run_attempt` can branch/checkpoint
    against — copied from `tests/test_infra_not_work.py`'s fixture of the
    same name so this file drives the SAME production seam."""
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True,
                   capture_output=True)
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.email", "u@e.com")
    _git(work, "config", "user.name", "u")
    (work / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "init")
    _git(work, "remote", "add", "origin", str(bare))
    _git(work, "push", "-u", "origin", "main")
    return work


class _ScriptedBackend:
    """Stands in for ClaudeBackend: always returns the same scripted
    `AgentResult`, whatever attempt asks for it (verbatim from
    `tests/test_infra_not_work.py`)."""

    def __init__(self, result: AgentResult):
        self._result = result
        self.calls = 0

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        self.calls += 1
        return self._result


async def _run_one_attempt(store, bare_repo, tmp_path, result: AgentResult):
    cfg = _attempt_config(tmp_path)
    backend = _ScriptedBackend(result)
    orch = Orchestrator(store, cfg.data, backend, SlackNotifier(None),
                        event_sink=[].append)
    task = Task.new("do a thing", repo_path=str(bare_repo))
    await store.create_task(task)
    await store.set_status(task, TaskStatus.CONTEXT)
    await store.set_status(task, TaskStatus.PLANNING)
    repo = GitRepo(bare_repo)
    return orch, backend, task, repo


# --------------------------------------------------------------------------- #
# store / api-client / cli-runner fixtures                                    #
# --------------------------------------------------------------------------- #


@pytest_asyncio.fixture
async def client(store, tmp_path):
    app.state.store = store
    app.state.config = load_config(tmp_path / "config.yaml")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost") as c:
        yield c


def _seed_task_sync(db_path: Path, *, task_id: str | None = None) -> str:
    async def _go():
        async with Store(db_path) as s:
            t = Task.new("Fix thing", repo_path="/tmp/repo")
            if task_id is not None:
                t.id = task_id
            await s.create_task(t)
            return t.id
    return asyncio.run(_go())


def _seed_attempt_sync(db_path: Path, task_id: str, **fields) -> str:
    async def _go():
        async with Store(db_path) as s:
            aid = await s.create_attempt(task_id, 1)
            if fields:
                await s.update_attempt(aid, **fields)
            return aid
    return asyncio.run(_go())


def _make_runner(path: Path, monkeypatch) -> CliRunner:
    import no_human.cli.commands as cmd_mod

    class _Cfg:
        primary_model = "claude-sonnet-4-6"
        review_model = "claude-sonnet-4-6"
        data: dict = {}

        def get(self, key, default=None):
            return self.data.get(key, default)

        def __getitem__(self, key):
            return self.data[key]

    _Cfg.db_path = path

    monkeypatch.setattr(cmd_mod, "load_config", lambda: _Cfg())
    monkeypatch.setattr(cmd_mod, "assert_subscription_mode", lambda **kw: None)
    monkeypatch.setattr(
        cmd_mod, "_probe_pool",
        lambda _cfg: cmd_mod.PoolProbe(None, cmd_mod.POOL_REFUSED))
    return CliRunner()


# --------------------------------------------------------------------------- #
# 1. GET /api/tasks/{id} exposes the full report                              #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_detail_endpoint_exposes_full_report(client, store):
    t = Task.new("Fix thing", repo_path="/tmp/repo")
    await store.create_task(t)
    aid = await store.create_attempt(t.id, 1)
    await store.update_attempt(aid, full_final_text="Full coder report body.")

    r = await client.get(f"/api/tasks/{t.id}")
    assert r.status_code == 200
    assert r.json()["full_report"] == "Full coder report body."


@pytest.mark.asyncio
async def test_detail_endpoint_full_report_none_when_never_written(client, store):
    t = Task.new("Fix thing", repo_path="/tmp/repo")
    await store.create_task(t)
    await store.create_attempt(t.id, 1)   # no full_final_text ever written

    r = await client.get(f"/api/tasks/{t.id}")
    assert r.status_code == 200
    assert r.json()["full_report"] is None


@pytest.mark.asyncio
async def test_detail_endpoint_full_report_is_the_last_attempt_with_one(client, store):
    t = Task.new("Fix thing", repo_path="/tmp/repo")
    await store.create_task(t)
    a1 = await store.create_attempt(t.id, 1)
    await store.update_attempt(a1, full_final_text="first attempt report")
    a2 = await store.create_attempt(t.id, 2)
    await store.update_attempt(a2, full_final_text="second attempt report")

    r = await client.get(f"/api/tasks/{t.id}")
    assert r.json()["full_report"] == "second attempt report"


# --------------------------------------------------------------------------- #
# 2. `nh task show` prints the full report                                    #
# --------------------------------------------------------------------------- #

def test_nh_show_prints_full_report(tmp_path, monkeypatch):
    db_path = tmp_path / "nh.db"
    task_id = _seed_task_sync(db_path, task_id="abcd1234abcd1234")
    _seed_attempt_sync(db_path, task_id, full_final_text="The coder's full report text.")

    runner = _make_runner(db_path, monkeypatch)
    result = runner.invoke(cli, ["task", "show", task_id[:8]])
    assert result.exit_code == 0, result.output
    assert "The coder's full report text." in result.output
    assert "final report" in result.output


def test_nh_show_prints_nothing_extra_when_no_report_written(tmp_path, monkeypatch):
    db_path = tmp_path / "nh.db"
    task_id = _seed_task_sync(db_path, task_id="beef5678beef5678")
    _seed_attempt_sync(db_path, task_id)   # no full_final_text

    runner = _make_runner(db_path, monkeypatch)
    result = runner.invoke(cli, ["task", "show", task_id[:8]])
    assert result.exit_code == 0, result.output
    assert "final report" not in result.output


# --------------------------------------------------------------------------- #
# 3. drop-marker paragraph is filtered on both surfaces                       #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_drop_marker_paragraph_is_filtered_on_both_surfaces(client, store):
    marker = Orchestrator._SUMMARY_DROP_MARKERS[0]
    text = (
        "Real change in paragraph one.\n\n"
        f"Per {marker} I updated a fixture.\n\n"
        "Real change in paragraph three."
    )
    # direct unit check on the shared renderer
    rendered = render_full_report(text)
    assert "Real change in paragraph one." in rendered
    assert "Real change in paragraph three." in rendered
    assert marker not in rendered

    t = Task.new("Fix thing", repo_path="/tmp/repo")
    await store.create_task(t)
    aid = await store.create_attempt(t.id, 1)
    await store.update_attempt(aid, full_final_text=text)

    r = await client.get(f"/api/tasks/{t.id}")
    body = r.json()["full_report"]
    assert "Real change in paragraph one." in body
    assert marker not in body


# --------------------------------------------------------------------------- #
# 4. a tail cut from the PR body survives on the full-report surface          #
# --------------------------------------------------------------------------- #

def test_long_report_tail_survives_on_the_surface_and_not_in_the_pr_body(store, tmp_path):
    orch = _orch(store, tmp_path)
    t = Task.new("t", repo_path="/r")
    sentinel = "SENTINEL-TAIL-9f2b6c1e"
    result = _Result()
    result.final_text = ("y" * 4500) + f"\n\n{sentinel}\n\n" + ("z" * 200)

    pr_body = orch._pr_body(t, _Commit(), result)
    assert sentinel not in pr_body
    assert "summary truncated at" in pr_body

    # `_SUMMARY_MAX_CHARS` bounds the SUMMARY section, not the whole PR body
    # (headings/footer live outside it) — `_summary_section` is the same call
    # `_pr_body` makes internally, per `tests/test_pr_hygiene.py`.
    summary = orch._summary_section(SimpleNamespace(final_text=result.final_text))
    assert sentinel not in summary
    # The fold's own `<details>` wrapper is template markup, not counted
    # against the coder-text cap (`tests/test_pr_hygiene.py` established this).
    assert len(_unfold(summary)) <= Orchestrator._SUMMARY_MAX_CHARS

    full = render_full_report(result.final_text)
    assert sentinel in full


# --------------------------------------------------------------------------- #
# 5. the truncation marker names the surface                                  #
# --------------------------------------------------------------------------- #

def test_truncation_marker_names_a_real_cli_command(tmp_path, monkeypatch):
    """A substring check (e.g. `"nh show" in marker`) would also pass for the
    WRONG command `nh show` (there is no such top-level command — attempt 1's
    review caught exactly that). Extract the backtick-quoted command from the
    marker and actually drive it through the CLI, so a future rename of the
    `task show` subcommand fails this test instead of silently drifting."""
    match = re.search(r"`([^`]+)`", Orchestrator._SUMMARY_TRUNCATED_MARKER)
    assert match, Orchestrator._SUMMARY_TRUNCATED_MARKER
    command_template = match.group(1)
    assert command_template.startswith("nh "), command_template

    db_path = tmp_path / "nh.db"
    task_id = _seed_task_sync(db_path, task_id="cafe1234cafe1234")
    _seed_attempt_sync(db_path, task_id, full_final_text="the coder's report")

    args = command_template[len("nh "):].replace("<task-id>", task_id[:8]).split()
    runner = _make_runner(db_path, monkeypatch)
    result = runner.invoke(cli, args)
    assert result.exit_code == 0, result.output
    assert "the coder's report" in result.output


def test_truncation_marker_stays_inside_its_own_cap():
    rendered = Orchestrator._clean_summary("\n\n".join(["y" * 500] * 20))
    assert len(rendered) <= Orchestrator._SUMMARY_MAX_CHARS
    assert "nh task show" in rendered


def test_no_such_surface_comment_is_gone():
    src = _ORCHESTRATOR_SRC.read_text()
    assert "no human-reachable surface" not in src
    assert "I could not find any surface" not in src


# --------------------------------------------------------------------------- #
# 6. the column exists via the PRAGMA-guarded ALTER TABLE loop, not a new     #
#    table / migration runner                                                 #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_full_final_text_column_created_by_the_pragma_guard(tmp_path):
    db_path = tmp_path / "fresh.db"
    s = await Store(db_path).connect()
    try:
        cols = [row[1] for row in await s._fetchall("PRAGMA table_info(attempts)")]
        assert "full_final_text" in cols
    finally:
        await s.close()

    # Reconnecting to the SAME db must not raise "duplicate column name" — the
    # migration .sql file documents the column but does not ALTER it itself.
    s2 = await Store(db_path).connect()
    await s2.close()


# --------------------------------------------------------------------------- #
# 7. the write path caps the stored text — a size guard, not a redaction.     #
#    Driven through the REAL `_run_attempt` coder-usage chokepoint (the same  #
#    seam `tests/test_infra_not_work.py` uses for its accounting assertions)  #
#    rather than re-typing the production slice expression into the test —   #
#    deleting the `full_final_text=` kwarg from that `update_attempt` call    #
#    must fail these tests, not merely fail to be exercised by them.         #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_coder_chokepoint_persists_capped_full_report(
        store, bare_repo, tmp_path):
    long_text = "q" * (_FULL_REPORT_MAX_CHARS + 500)
    result = AgentResult(
        final_text=long_text, num_turns=22, is_error=True, tokens_used=140_000,
        session_id="s", stop_reason="error", cache_read_tokens=1_000,
        cache_creation_tokens=500,
    )
    orch, backend, task, repo = await _run_one_attempt(
        store, bare_repo, tmp_path, result)

    outcome = await orch._run_attempt(task, repo, 1, "main")

    assert backend.calls, "the backend never ran — the test proves nothing"
    assert outcome.status == TaskStatus.FAILED
    row = (await store.list_attempts(task.id))[-1]
    assert row["full_final_text"] is not None
    assert len(row["full_final_text"]) == _FULL_REPORT_MAX_CHARS


@pytest.mark.asyncio
async def test_coder_chokepoint_coerces_empty_report_to_null(
        store, bare_repo, tmp_path):
    result = AgentResult(
        final_text="   ", num_turns=22, is_error=True, tokens_used=140_000,
        session_id="s", stop_reason="error", cache_read_tokens=1_000,
        cache_creation_tokens=500,
    )
    orch, backend, task, repo = await _run_one_attempt(
        store, bare_repo, tmp_path, result)

    outcome = await orch._run_attempt(task, repo, 1, "main")

    assert backend.calls, "the backend never ran — the test proves nothing"
    assert outcome.status == TaskStatus.FAILED
    row = (await store.list_attempts(task.id))[-1]
    assert row["full_final_text"] is None
