"""`setup_cmds` — profile-declared build prerequisites for a FRESH task
worktree.

The defect: a task worktree is a throwaway `git worktree add` checkout, which
structurally cannot contain anything gitignored (`node_modules`, a built
`web/dist`...). A repo whose node suites need those has no way to get them —
`nh doctor`/the attempt loop just fails every node test in every fresh
worktree, forever.

The fix is a profile-declared, optional `setup_cmds` list
(`profile.ProjectProfile.setup_cmds`) that `core/worktree.py`'s
`run_setup_commands` runs, in order, from the worktree ROOT, once per fresh
worktree, before anything test-shaped — wired in from
`Orchestrator._run_task_body` right after the worktree is acquired. It is
resolved ONLY via `_usable_profile` (the confirmed/policy-gated profile),
never from the repo's own untrusted `.no_human.yml`.

Fixtures below (`live_checkout` / `_orchestrator` / `_task` / `_git` /
`_finished`) are copied locally from `tests/test_worktree_isolation.py`
rather than imported, per that file's own established convention of keeping
each test file's fixtures self-contained.
"""
from __future__ import annotations

import asyncio
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from no_human.config import load_config
from no_human.core.task import TaskStatus

# asyncio_mode = "auto" (pyproject.toml) auto-detects `async def` tests below
# — no `pytestmark` needed, and one would wrongly tag the sync tests too.


# --------------------------------------------------------------------------- #
# Fixtures (copied from tests/test_worktree_isolation.py)                     #
# --------------------------------------------------------------------------- #


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def live_checkout(tmp_path):
    """A minimal repo with one committed file — the worktree machinery's
    subject. No uncommitted work is needed for these tests (contrast
    test_worktree_isolation.py, which pins that separately)."""
    work = tmp_path / "live"
    work.mkdir()
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.email", "u@e.com")
    _git(work, "config", "user.name", "u")
    (work / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "init")
    return work


def _orchestrator(store, cfg):
    from no_human.core.orchestrator import Orchestrator
    from no_human.notify.slack import SlackNotifier

    return Orchestrator(store, cfg.data, object(), SlackNotifier(None))


async def _task(store, repo):
    from no_human.core.task import Task

    t = Task.new("add mul()", repo_path=str(repo))
    await store.create_task(t)
    return t


def _finished(task):
    from no_human.core.orchestrator import TaskOutcome

    return TaskOutcome(task, status=TaskStatus.DONE, detail="stub")


def _confirmed_profile(repo_path, **overrides):
    """A profile that clears `_profile_usable_under_policy` (human-confirmed,
    with a proven test command) — the only kind `_usable_profile` will ever
    hand `setup_cmds` off of."""
    from no_human.profile import ProjectProfile

    fields = dict(
        repo_path=str(repo_path),
        ecosystem="python-pytest",
        test_cmd="true",
        confirmed=True,
        proven={"test_cmd": True},
    )
    fields.update(overrides)
    return ProjectProfile(**fields)


def _cfg(tmp_path):
    cfg = load_config(tmp_path / "config.yaml")
    cfg.data["isolation"]["worktree_root"] = str(tmp_path / "wt")
    return cfg


def _make_worktree(live_checkout, tmp_path, name="wt1"):
    """A real linked worktree, without going through the orchestrator — for
    the two tests that pin `run_setup_commands`/the marker path directly."""
    wt = tmp_path / name
    _git(live_checkout, "worktree", "add", "--detach", str(wt), "HEAD")
    return wt


# --------------------------------------------------------------------------- #
# End to end, through the orchestrator                                        #
# --------------------------------------------------------------------------- #


async def test_profile_setup_cmds_run_in_order_before_the_test_command(
    live_checkout, tmp_path, store,
):
    await store.upsert_profile(_confirmed_profile(
        live_checkout,
        setup_cmds=["printf 1 >> order.txt", "printf 2 >> order.txt"],
    ))
    orch = _orchestrator(store, _cfg(tmp_path))
    t = await _task(store, live_checkout)

    seen = {}

    async def _capture(task, repo):
        seen["path"] = Path(repo.path)
        seen["order"] = (Path(repo.path) / "order.txt").read_text()
        return _finished(task)

    orch._drive_watched = _capture
    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.DONE, outcome.detail
    assert seen["order"] == "12", (
        "setup commands did not run, in order, before the test command")
    assert seen["path"] != live_checkout
    assert not (live_checkout / "order.txt").exists(), (
        "setup ran in the live checkout, not the disposable worktree")


async def test_a_failing_setup_command_fails_the_task_naming_the_command(
    live_checkout, tmp_path, store,
):
    await store.upsert_profile(_confirmed_profile(
        live_checkout,
        setup_cmds=["exit 3", "printf should_not_run >> ran.txt"],
    ))
    orch = _orchestrator(store, _cfg(tmp_path))
    t = await _task(store, live_checkout)

    events = []
    orch._sink = events.append

    async def _never(task, repo):
        raise AssertionError(
            "the test command ran after a setup command failed")

    orch._drive_watched = _never
    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.FAILED
    # Names the EXACT failing command — never a test failure.
    assert "exit 3" in outcome.detail
    failed = [e for e in events if e.get("kind") == "failed"]
    assert failed, "no `failed` event was emitted"
    assert failed[-1].get("reason_category") == "infra", (
        f"wrong failure category: {failed[-1]}")


async def test_a_profile_without_setup_cmds_changes_nothing(
    live_checkout, tmp_path, store,
):
    """Byte-identical to pre-feature behaviour when the profile declares no
    `setup_cmds` (the default, and every profile onboarded before this field
    existed)."""
    await store.upsert_profile(_confirmed_profile(live_checkout, setup_cmds=[]))
    orch = _orchestrator(store, _cfg(tmp_path))
    t = await _task(store, live_checkout)

    events = []
    orch._sink = events.append

    async def _capture(task, repo):
        return _finished(task)

    orch._drive_watched = _capture
    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.DONE
    assert not any(str(e.get("kind", "")).startswith("worktree_setup")
                   for e in events), (
        "a no-op setup_cmds list still emitted setup events")


async def test_setup_cmds_are_never_read_from_the_untrusted_repo_config(
    live_checkout, tmp_path, store,
):
    """The repo's own `.no_human.yml` is attacker-controlled content living
    IN the checkout. `project_config._WHITELIST` deliberately does not
    include `setup_cmds` — declaring it there must never get shell run in an
    unattended task."""
    (live_checkout / ".no_human.yml").write_text(
        'setup_cmds:\n  - "printf should_not_run >> ran.txt"\n'
    )
    _git(live_checkout, "add", "-A")
    _git(live_checkout, "commit", "-m", "untrusted setup_cmds")

    # The CONFIRMED profile declares none — only the repo's own file does.
    await store.upsert_profile(_confirmed_profile(live_checkout, setup_cmds=[]))
    orch = _orchestrator(store, _cfg(tmp_path))
    t = await _task(store, live_checkout)

    events = []
    orch._sink = events.append
    seen = {}

    async def _capture(task, repo):
        seen["ran_file_exists"] = (Path(repo.path) / "ran.txt").exists()
        return _finished(task)

    orch._drive_watched = _capture
    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.DONE
    assert seen["ran_file_exists"] is False, (
        "a command from the repo's own untrusted .no_human.yml executed")
    assert not any(str(e.get("kind", "")).startswith("worktree_setup")
                   for e in events), (
        "a setup-command event fired even though the CONFIRMED profile "
        "declared no setup_cmds")


# --------------------------------------------------------------------------- #
# `run_setup_commands` / the marker, directly                                 #
# --------------------------------------------------------------------------- #


def test_setup_runs_once_per_worktree(live_checkout, tmp_path):
    from no_human.core.worktree import run_setup_commands

    wt = _make_worktree(live_checkout, tmp_path)
    counter = wt / "counter.txt"
    cmds = [f"printf x >> {counter}"]

    first = run_setup_commands(wt, cmds)
    assert first == cmds
    assert counter.read_text() == "x"

    second = run_setup_commands(wt, cmds)
    assert second == [], "setup ran a second time in the same worktree"
    assert counter.read_text() == "x", (
        "the setup command actually executed a second time")


def test_setup_marker_is_not_in_the_working_tree(live_checkout, tmp_path):
    from no_human.core.worktree import (
        SETUP_MARKER_NAME,
        _setup_marker_path,
        run_setup_commands,
    )

    wt = _make_worktree(live_checkout, tmp_path)
    run_setup_commands(wt, ["true"])

    marker = _setup_marker_path(wt)
    assert marker is not None and marker.exists(), "no marker was written"
    assert wt not in marker.parents, (
        f"marker landed inside the working tree: {marker}")
    assert not list(wt.rglob(SETUP_MARKER_NAME)), (
        "the marker is discoverable by walking the working tree")

    # A worktree that ran no setup commands producing files has nothing to
    # report — any output at all here means the marker itself leaked into
    # the working tree.
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=wt, capture_output=True, text=True, check=True,
    )
    assert status.stdout.strip() == "", (
        f"the marker leaked into `git status`: {status.stdout!r}")


# --------------------------------------------------------------------------- #
# Persistence: profile.py + the CLI                                          #
# --------------------------------------------------------------------------- #


async def test_setup_cmds_round_trip_through_yaml_and_the_db(tmp_path, store):
    from no_human.profile import ProjectProfile, profile_divergence

    repo = tmp_path / "repo"
    repo.mkdir()
    cmds = ["npm --prefix web ci", "npm --prefix web run build"]
    profile = ProjectProfile(
        repo_path=str(repo), confirmed=True, test_cmd="true",
        proven={"test_cmd": True}, setup_cmds=list(cmds),
    )
    profile.save()
    await store.upsert_profile(profile)

    reloaded_yaml = ProjectProfile.load(repo)
    reloaded_db = await store.get_profile(str(repo))

    assert reloaded_yaml is not None and reloaded_db is not None
    assert reloaded_yaml.setup_cmds == cmds
    assert reloaded_db.setup_cmds == cmds

    # A DB row that has since gained a command the yaml file does not (the
    # ordinary shape of "operator ran `nh repo setup-cmds` but never hand-
    # edited the yaml") must be flagged as diverged on `setup_cmds`.
    diverged_db = replace(
        reloaded_db, setup_cmds=reloaded_db.setup_cmds + ["npm --prefix desktop ci"])
    assert "setup_cmds" in profile_divergence(diverged_db, reloaded_yaml)
    # Agreeing sides never diverge on it.
    assert "setup_cmds" not in profile_divergence(reloaded_db, reloaded_yaml)


def test_repo_setup_cmds_cli_records_the_commands(tmp_path, monkeypatch):
    from click.testing import CliRunner

    import no_human.cli.commands as cmd_mod
    from no_human.cli.commands import cli
    from no_human.core.db import Store
    from no_human.profile import ProjectProfile

    db = tmp_path / "test.db"
    repo = tmp_path / "repo"
    repo.mkdir()
    resolved = str(repo.resolve())

    class _Cfg:
        primary_model = "claude-sonnet-4-6"
        review_model = "claude-sonnet-4-6"
        data: dict = {}
        db_path = db

        def get(self, key, default=None):
            return self.data.get(key, default)

        def __getitem__(self, key):
            return self.data[key]

    monkeypatch.setattr(cmd_mod, "load_config", lambda: _Cfg())
    monkeypatch.setattr(cmd_mod, "assert_subscription_mode", lambda **kw: None)
    runner = CliRunner()

    def _get_profile():
        async def _go():
            async with Store(db) as s:
                return await s.get_profile(resolved)
        return asyncio.run(_go())

    def _seed():
        async def _go():
            async with Store(db) as s:
                await s.upsert_profile(ProjectProfile(repo_path=resolved))
        asyncio.run(_go())

    # No profile onboarded yet: errors out pointing at `nh onboard`, writes
    # nothing.
    no_profile = runner.invoke(cli, ["repo", "setup-cmds", str(repo)])
    assert no_profile.exit_code != 0
    assert "nh onboard" in no_profile.output
    assert _get_profile() is None

    _seed()

    # Declares, replacing any (empty) list.
    result = runner.invoke(cli, [
        "repo", "setup-cmds", str(repo),
        "npm --prefix web ci", "npm --prefix web run build",
    ])
    assert result.exit_code == 0, result.output
    prof = _get_profile()
    assert prof.setup_cmds == ["npm --prefix web ci", "npm --prefix web run build"]

    # Inspect: prints the current list, changes nothing.
    inspect_result = runner.invoke(cli, ["repo", "setup-cmds", str(repo)])
    assert inspect_result.exit_code == 0, inspect_result.output
    assert "npm --prefix web ci" in inspect_result.output
    assert "npm --prefix web run build" in inspect_result.output
    assert _get_profile().setup_cmds == prof.setup_cmds

    # A second declaration REPLACES, it does not append.
    replace_result = runner.invoke(cli, [
        "repo", "setup-cmds", str(repo), "npm --prefix desktop ci",
    ])
    assert replace_result.exit_code == 0, replace_result.output
    assert _get_profile().setup_cmds == ["npm --prefix desktop ci"]

    # --clear empties it.
    clear_result = runner.invoke(cli, ["repo", "setup-cmds", str(repo), "--clear"])
    assert clear_result.exit_code == 0, clear_result.output
    assert _get_profile().setup_cmds == []

    # Commands and --clear together is a usage error, not a silent pick.
    both = runner.invoke(cli, [
        "repo", "setup-cmds", str(repo), "some cmd", "--clear",
    ])
    assert both.exit_code != 0
