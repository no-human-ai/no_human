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
`Orchestrator._run_worktree_setup`, called from `_drive_watched` (so the
cancellation watcher started there is already alive while setup runs, and
`nh task cancel` is observed instead of going unobserved for the length of
the setup commands). It is resolved ONLY via `_usable_profile` (the
confirmed/policy-gated profile — which, with no DB row for the repo, falls
back to `<repo>/.no_human/project.yml` exactly as it does for `test_cmd`),
never from the repo's own untrusted `.no_human.yml`.

Fixtures below (`live_checkout` / `_orchestrator` / `_task` / `_git` /
`_finished`) are copied locally from `tests/test_worktree_isolation.py`
rather than imported, per that file's own established convention of keeping
each test file's fixtures self-contained.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import time
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

    # Setup now runs INSIDE `_drive_watched` (see `_run_worktree_setup`), so
    # the fake that used to replace `_drive_watched` wholesale would bypass
    # setup entirely. Replace `_drive` instead — the real `_drive_watched`
    # (and its real setup call) stays in the loop.
    async def _capture(task, repo):
        seen["path"] = Path(repo.path)
        seen["order"] = (Path(repo.path) / "order.txt").read_text()
        return _finished(task)

    orch._drive = _capture
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

    # See the comment in the previous test: `_drive`, not `_drive_watched`,
    # is the fake target now that setup lives inside `_drive_watched`.
    orch._drive = _never
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

    # Route through the real `_drive_watched` (and its real
    # `_run_worktree_setup` call) rather than replacing it wholesale — that
    # is what actually proves the no-op path emits nothing.
    async def _capture(task, repo):
        return _finished(task)

    orch._drive = _capture
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

    orch._drive = _capture
    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.DONE
    assert seen["ran_file_exists"] is False, (
        "a command from the repo's own untrusted .no_human.yml executed")
    assert not any(str(e.get("kind", "")).startswith("worktree_setup")
                   for e in events), (
        "a setup-command event fired even though the CONFIRMED profile "
        "declared no setup_cmds")


async def test_a_cancel_during_setup_is_honoured_and_kills_the_setup_process(
    live_checkout, tmp_path, store, monkeypatch,
):
    """MAJOR-2: setup now runs INSIDE `_drive_watched`, with the cancellation
    watcher already alive, precisely so `nh task cancel` is observed instead
    of going unobserved for the length of a long `npm ci`. Drives a REAL
    setup command (a background sleep) through the real orchestrator, arms
    `store.request_cancel` once the command has actually started, and asserts
    both that the task parks via `_honor_cancel` (not a FAILED/infra outcome,
    not a multi-second wait) and that the setup process is truly dead
    afterward — not merely disowned."""
    import no_human.core.orchestrator as orch_mod

    monkeypatch.setattr(orch_mod, "_CANCEL_POLL_SECONDS", 0.05)

    started = tmp_path / "setup_started"
    pid_file = tmp_path / "setup.pid"
    cmd = f"touch {started}; sleep 30 & echo $! > {pid_file}; wait"
    await store.upsert_profile(_confirmed_profile(live_checkout, setup_cmds=[cmd]))
    orch = _orchestrator(store, _cfg(tmp_path))
    t = await _task(store, live_checkout)

    async def _never(task, repo):
        raise AssertionError(
            "the test command ran after a cancel was requested during setup")

    orch._drive = _never

    async def _cancel_once_started():
        deadline = time.monotonic() + 10
        while not started.exists() and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        assert started.exists(), "the setup command never started"
        await store.request_cancel(t.id, "operator says stop")

    canceller = asyncio.create_task(_cancel_once_started())
    outcome = await orch.run_task(t)
    await canceller

    assert outcome.status is TaskStatus.BLOCKED, outcome.detail
    assert "operator says stop" in outcome.detail

    deadline = time.monotonic() + 10
    while not pid_file.exists() and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    assert pid_file.exists(), "the backgrounded sleep never recorded its pid"
    pid = int(pid_file.read_text().strip())

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.1)
    else:
        pytest.fail(
            "the setup command's process survived the cancel — "
            "terminate_running(wt_path) did not kill it")


async def test_setup_cmds_never_run_with_isolation_disabled(
    live_checkout, tmp_path, store,
):
    """`setup_cmds` only ever runs from `Orchestrator._run_worktree_setup`,
    reached only via `_drive_watched(task, repo, setup_in=wt_path)` — the
    isolation-ON call site in `_run_task_body`. With isolation off,
    `_drive_watched(task, main_repo)` is called with no `setup_in` at all, so
    a declared `setup_cmds` command must never touch the live checkout."""
    sentinel = live_checkout / "sentinel.txt"
    await store.upsert_profile(_confirmed_profile(
        live_checkout, setup_cmds=[f"touch {sentinel}"],
    ))
    cfg = _cfg(tmp_path)
    cfg.data["isolation"]["enabled"] = False
    orch = _orchestrator(store, cfg)
    t = await _task(store, live_checkout)

    events = []
    orch._sink = events.append

    async def _capture(task, repo):
        return _finished(task)

    orch._drive = _capture
    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.DONE, outcome.detail
    assert not sentinel.exists(), (
        "a setup_cmds command ran with worktree isolation disabled")
    assert not any(str(e.get("kind", "")).startswith("worktree_setup")
                   for e in events), (
        "a setup-command event fired even though isolation is disabled")


async def test_setup_events_reach_the_event_sink_from_the_worker_thread(
    live_checkout, tmp_path, store,
):
    """`run_setup_commands` calls `emit` from inside `asyncio.to_thread` — a
    worker thread, not the event loop thread. `self.emit` is not thread-safe
    (`scheduler.py`'s `_sink` sets an `asyncio.Event`), so
    `_run_worktree_setup` must marshal every call with
    `loop.call_soon_threadsafe` rather than invoking it straight from that
    thread. This proves delivery actually lands on the loop thread, not just
    that it "doesn't crash" (a direct cross-thread call to `self.emit` would
    not raise here — `asyncio.Event.set()` from the wrong thread is a silent
    correctness hazard, not an exception)."""
    import threading

    loop_thread = threading.current_thread()
    await store.upsert_profile(_confirmed_profile(
        live_checkout, setup_cmds=["true"],
    ))
    orch = _orchestrator(store, _cfg(tmp_path))
    t = await _task(store, live_checkout)

    threads_seen = []
    orch._sink = lambda event: threads_seen.append(threading.current_thread())

    async def _capture(task, repo):
        return _finished(task)

    orch._drive = _capture
    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.DONE, outcome.detail
    assert threads_seen, "no setup event reached the sink at all"
    assert all(th is loop_thread for th in threads_seen), (
        "a setup event was delivered from the worker thread instead of "
        "being marshalled onto the event loop via call_soon_threadsafe")


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


@pytest.mark.skipif(os.name == "nt", reason="process-group kill is POSIX-only")
def test_a_setup_timeout_leaves_no_orphaned_grandchild(live_checkout, tmp_path):
    """MAJOR-2: the measured defect was a bare `subprocess.run(cmd,
    timeout=...)`, which only kills the direct child (the shell) on timeout —
    a backgrounded grandchild (`sleep 40` under `sh -c '... &'`) was left
    running at ppid 1 forever. `run_setup_commands` now goes through
    `runner._run_shell`, which spawns in its own process group and kills the
    WHOLE tree via `_kill_process_tree` on timeout. Proves the grandchild is
    actually dead, not merely disowned."""
    from no_human.core.worktree import WorktreeSetupError, run_setup_commands

    wt = _make_worktree(live_checkout, tmp_path)
    pid_file = wt / "grandchild.pid"
    marker = wt / "slept_through"
    # `sleep 40` is backgrounded, so it is a CHILD OF THE SHELL (a grandchild
    # of this test process) — not the shell itself, which a naive
    # `Popen.pid`-only kill would still miss.
    cmd = f"sleep 40 & echo $! > {pid_file}; wait; touch {marker}"

    with pytest.raises(WorktreeSetupError) as exc_info:
        run_setup_commands(wt, [cmd], timeout=2)
    assert "timed out after 2s" in exc_info.value.detail

    deadline = time.monotonic() + 2
    while not pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert pid_file.exists(), "the backgrounded grandchild never started"
    pid = int(pid_file.read_text().strip())

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        pytest.fail(
            "the setup command's grandchild survived the timeout — only "
            "the shell was killed, not the whole process tree")

    # `wait; touch marker` never got to run — the tree was killed, not just
    # the top-level shell racing ahead of its own background job.
    time.sleep(2)
    assert not marker.exists(), (
        "the command continued running after the timeout fired")


def test_a_command_that_cannot_be_spawned_fails_the_task_naming_it(
    live_checkout, tmp_path, monkeypatch,
):
    """MINOR: `Popen` raises `OSError`/`FileNotFoundError` before
    `runner._register` ever runs (no process to register). This branch is
    easy to leave untested because it never touches a real process — proves
    it is still surfaced as a `WorktreeSetupError` naming the exact command,
    not an uncaught `OSError` crashing the task."""
    from no_human.core.worktree import WorktreeSetupError, run_setup_commands
    from no_human.testing import runner as runner_mod

    wt = _make_worktree(live_checkout, tmp_path)

    def _boom(cmd, work_dir, timeout, run_env):
        raise OSError("no such shell")

    monkeypatch.setattr(runner_mod, "_run_shell", _boom)

    with pytest.raises(WorktreeSetupError) as exc_info:
        run_setup_commands(wt, ["totally-not-a-real-command"])

    err = exc_info.value
    assert err.command == "totally-not-a-real-command"
    assert err.detail.startswith("could not start:"), err.detail


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


def test_setup_cmds_refuses_when_there_is_no_db_row_even_with_a_repo_file(
    tmp_path, monkeypatch,
):
    """MAJOR-3: the measured defect was a repo-internal `.no_human/
    project.yml` with `confirmed: true` (content the REPO AUTHOR controls,
    not an operator) being enough, by itself, to make `nh repo setup-cmds`
    write a CONFIRMED DB row — file-conferred trust with no `nh onboard` ever
    having run. `_usable_profile`'s DB-first-then-file-fallback for RUNNING an
    already-confirmed profile's `setup_cmds` is untouched and still correct
    (see `profile.py`); this is a narrower, additional refusal specific to
    the CLI command that WRITES the DB row in the first place."""
    from click.testing import CliRunner

    import no_human.cli.commands as cmd_mod
    from no_human.cli.commands import cli
    from no_human.core.db import Store
    from no_human.profile import ProjectProfile

    db = tmp_path / "test.db"
    repo = tmp_path / "repo"
    repo.mkdir()
    resolved = str(repo.resolve())

    # The repo's own file claims it is already confirmed, with setup_cmds
    # already baked in — this file must never be read by this command.
    ProjectProfile(
        repo_path=resolved, confirmed=True, test_cmd="true",
        proven={"test_cmd": True}, setup_cmds=["printf already_here >> x"],
    ).save()

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

    result = runner.invoke(cli, ["repo", "setup-cmds", str(repo), "npm ci"])

    assert result.exit_code != 0
    assert "nh onboard" in result.output

    def _get_profile():
        async def _go():
            async with Store(db) as s:
                return await s.get_profile(resolved)
        return asyncio.run(_go())

    assert _get_profile() is None, (
        "the repo's own project.yml conferred trust into a DB row")


async def test_setup_cmds_survive_a_reonboard_and_a_prove(live_checkout, store):
    """MAJOR-4: `/api/onboarding/repos/onboard` and `/repos/prove` each build
    a brand-new `ProjectProfile` from freshly detected evidence and then
    REPLACE the whole DB row (`store.upsert_profile`) — before
    `operator_carry_kwargs`/`carry_operator_fields` (`profile.py`) existed,
    that silently wiped a `setup_cmds` list an operator had declared with
    `nh repo setup-cmds` on every re-onboard or re-prove. Drives the real HTTP
    endpoints end to end: onboard, declare, re-onboard, prove — asserting the
    list survives each step."""
    import types

    from httpx import ASGITransport, AsyncClient

    from no_human.api.app import app

    app.state.store = store
    app.state.config = types.SimpleNamespace(
        data={"git": {"github_hosts": ["github.com"]}})
    repo_path = str(live_checkout)
    declared = ["npm --prefix web ci", "npm --prefix web run build"]

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost") as client:
        r = await client.post(
            "/api/onboarding/repos/onboard", json={"repo_path": repo_path})
        assert r.status_code == 200, r.text
        prof = await store.get_profile(repo_path)
        assert prof.setup_cmds == [], "a fresh onboard invented setup_cmds"

        # Declare, the way `nh repo setup-cmds` does: replace the DB row's
        # setup_cmds field, changing nothing else about the profile.
        prof = replace(prof, setup_cmds=list(declared))
        await store.upsert_profile(prof)

        # Re-onboard: a fresh derive must carry the declared list forward
        # rather than silently rebuilding an empty one.
        r = await client.post(
            "/api/onboarding/repos/onboard", json={"repo_path": repo_path})
        assert r.status_code == 200, r.text
        prof = await store.get_profile(repo_path)
        assert prof.setup_cmds == declared, (
            "re-onboarding wiped the operator-declared setup_cmds")

        # Prove: drain the SSE stream to completion, then check the row.
        async with client.stream(
            "POST", "/api/onboarding/repos/prove",
            json={"repo_path": repo_path},
        ) as resp:
            assert resp.status_code == 200
            async for _line in resp.aiter_lines():
                pass  # drain to `stream_end` / connection close

        prof = await store.get_profile(repo_path)
        assert prof.setup_cmds == declared, (
            "proving wiped the operator-declared setup_cmds")


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
