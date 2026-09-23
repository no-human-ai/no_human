"""Reap every process an attempt's shell commands leave running.

THE INCIDENT this file exists to prevent from recurring silently: 36 orphaned
``.../Python3.9 -c "while True: pass"`` processes, 6h33m-6h37m old, ~33% CPU
each (roughly 12 of a machine's 18 cores), load average peaked at 104.76 -- a
CODER AGENT's own exploratory shell command, spawned during a prior task's
implementation, that NOTHING reaped when that attempt ended. The committed
test file from that same task (``tests/test_pr497_timing_gates_are_load_
independent.py``) is not the source and is out of scope here -- it only
monkeypatches, it never spawns a real process. The root defect proved (and
fixed) by this file is structural: an attempt is legitimately free to run
shell commands (``agent/guard.py`` is not touched to forbid that), but until
now nothing tracked what it spawned, so nothing could ever clean it up. See
``core/attempt_procs.py``'s own module docstring for the full writeup this
file's tests are keyed to.

Every wait below is a bounded poll (``_wait_dead``), never a bare ``sleep``.
Every test that spawns a real process group is POSIX-only (marked
individually, not module-wide, since the pure no-scope/fail-open tests and
the mocked-spawn-kwargs test are honestly meaningful on any platform) -- the
Windows branch of ``core/attempt_launcher.py`` is left honestly untested, the
same posture ``runner._kill_process_tree`` already takes. Real subprocesses
throughout, no monkeypatching of the reaper itself, keeping
``tamper_guard.count_faking_fixtures`` (``tests/conftest.py``) clean --
except ``test_the_codex_backend_spawns_in_a_new_session``, which monkeypatches
``asyncio.create_subprocess_exec`` deliberately, the same seam
``tests/test_codex_backend.py`` already patches for every one of its tests.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from no_human.agent.claude_backend import AgentEvent, AgentResult, ClaudeBackend
from no_human.core import attempt_procs
from no_human.core.orchestrator import CancelRequested, Orchestrator
from no_human.core.task import Task, TaskStatus
from no_human.notify.slack import SlackNotifier
from no_human.proc import hidden_console_kwargs
import no_human.agent.codex_backend as cx
import no_human.core.orchestrator as orch_mod

from .test_codex_backend import FAKE_ENV, _HAPPY, _fake_codex, _stub_cli  # noqa: F401
from .test_e2e_orchestrator import _config, bare_repo  # noqa: F401

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"

_POSIX_ONLY = pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX process groups (setsid/getpgid/killpg); "
           "core/attempt_launcher.py's Windows branch is honestly untested",
)

_SLEEP_120 = [sys.executable, "-c", "import time; time.sleep(120)"]


def _wait_dead(pid: int, *, timeout: float = 10.0) -> bool:
    """Bounded poll for ``pid``'s death.

    Tolerant of the zombie window a parent that has not yet reaped a child
    leaves: ``os.kill(pid, 0)`` (and, inside the reaper, ``os.killpg``) can
    read a zombie as "alive" (or even raise ``PermissionError``) until
    SOMETHING calls ``waitpid`` on it -- diagnosed live while building this
    fix, not a real defect, but a real trap for a test helper. Each
    iteration proactively reaps via a non-blocking ``waitpid`` when this
    process happens to be ``pid``'s real OS parent (the common case here:
    the test itself is the direct parent of the launcher it spawned via
    ``subprocess.Popen``), so a genuinely-killed child does not read as
    falsely alive for the whole timeout.
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            pass  # not our child (e.g. a grandchild reparented elsewhere)
        except OSError:
            pass
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            pass  # exists, but not ours to signal -- keep polling/reaping
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def _mutate(cwd):
    Path(cwd, "calc.py").write_text(
        "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n"
    )


# ---------------------------------------------------------------------------
# child does not survive a normal attempt
# ---------------------------------------------------------------------------

@_POSIX_ONLY
def test_a_child_of_a_finished_attempt_is_dead(tmp_path):
    """The shape that actually leaked: a GRANDCHILD of the wrapped shell
    command, not the wrapped command itself. Both must be alive while the
    attempt is live, and both dead once its scope's ``with`` block exits."""
    with attempt_procs.attempt_scope(
        "task12345678", "attempt12345678", root=tmp_path
    ) as scope:
        argv = attempt_procs.launch_argv(
            ["sh", "-c",
             f'{sys.executable} -c "import time; time.sleep(120)" & '
             f'echo GRANDCHILD $!; wait'],
            scope,
        )
        # `start_new_session=True` at the FORK, same as every production
        # spawn site (`hidden_console_kwargs(new_group=True)`, wrapped here
        # as `attempt_procs.spawn_kwargs()`) -- without it, the launcher's
        # own `os.setsid()` (attempt_launcher.main -> `_own_group`) still
        # wins the group eventually, but only after it has actually started
        # running, which loses the race against this line reading
        # `os.getpgid(proc.pid)` immediately after `Popen` returns (fork+exec
        # syncing only guarantees the exec happened, not that the new
        # program has run any of its own code yet).
        proc = subprocess.Popen(
            argv, stdout=subprocess.PIPE, text=True, **attempt_procs.spawn_kwargs()
        )
        assert attempt_procs.register_group(proc.pid, scope)
        line = proc.stdout.readline()
        assert line.startswith("GRANDCHILD "), line
        grandchild_pid = int(line.split()[-1])

        # alive while the attempt is live
        os.kill(proc.pid, 0)
        os.kill(grandchild_pid, 0)

    assert _wait_dead(proc.pid), "the launcher survived attempt_scope's exit"
    assert _wait_dead(grandchild_pid), (
        "the grandchild -- the incident's own shape -- survived "
        "attempt_scope's exit"
    )


# ---------------------------------------------------------------------------
# end-to-end, through a real attempt
# ---------------------------------------------------------------------------

class _SpawningBackend:
    """A coder session that also does real out-of-band shell work and never
    cleans up after itself -- exactly the freedom (and the gap) the incident
    is about. Proves the wiring in ``orchestrator._run_attempt``, not just
    the ``attempt_procs`` module in isolation."""

    def __init__(self, mutate):
        self.mutate = mutate
        self.spawned_pid: int | None = None

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        argv = attempt_procs.launch_argv(_SLEEP_120)
        proc = subprocess.Popen(argv, **attempt_procs.spawn_kwargs())
        attempt_procs.register_group(proc.pid)
        self.spawned_pid = proc.pid
        if on_event:
            on_event(AgentEvent("tool_use", tool_name="Edit",
                                tool_input={"file_path": "calc.py"}))
        self.mutate(cwd)
        return AgentResult(final_text="done", num_turns=2, is_error=False,
                           tokens_used=100, session_id="s", stop_reason="end_turn")


@_POSIX_ONLY
async def test_an_orchestrator_attempt_reaps_the_coder_session_children(
    store, bare_repo, tmp_path
):
    cfg = _config(tmp_path)
    backend = _SpawningBackend(_mutate)
    orch = Orchestrator(store, cfg.data, backend, SlackNotifier(None))
    t = Task.new("add mul()", repo_path=str(bare_repo))
    await store.create_task(t)

    await orch.run_task(t)

    assert backend.spawned_pid is not None
    assert _wait_dead(backend.spawned_pid), (
        "the coder session's own leftover child survived a finished attempt "
        "-- the exact shape of the incident, through the real _run_attempt "
        "wiring"
    )


# ---------------------------------------------------------------------------
# CANCELLED attempt
# ---------------------------------------------------------------------------

class _CancelSpawningBackend:
    def __init__(self, store, task_id):
        self.store = store
        self.task_id = task_id
        self.spawned_pid: int | None = None

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        argv = attempt_procs.launch_argv(_SLEEP_120)
        proc = subprocess.Popen(argv, **attempt_procs.spawn_kwargs())
        attempt_procs.register_group(proc.pid)
        self.spawned_pid = proc.pid
        self.mutate(cwd) if hasattr(self, "mutate") else None
        _mutate(cwd)
        await self.store.request_cancel(self.task_id, "operator")
        for _ in range(500):
            await asyncio.sleep(0.01)
            if on_event:
                on_event(AgentEvent("tool_use", tool_name="Read", tool_input={}))
        raise AssertionError("the session was never interrupted")


@_POSIX_ONLY
async def test_a_cancelled_attempt_reaps_its_children(
    store, bare_repo, tmp_path, monkeypatch
):
    monkeypatch.setattr(orch_mod, "_CANCEL_POLL_SECONDS", 0.01)
    task = Task.new("add mul()", repo_path=str(bare_repo))
    await store.create_task(task)
    cfg = _config(tmp_path)
    backend = _CancelSpawningBackend(store, task.id)
    events = []
    orch = Orchestrator(store, cfg.data, backend, SlackNotifier(None),
                        event_sink=events.append)

    outcome = await asyncio.wait_for(orch.run_task(task), timeout=60)

    assert outcome.status is TaskStatus.BLOCKED
    assert backend.spawned_pid is not None
    assert _wait_dead(backend.spawned_pid), (
        "a cancelled attempt left its own spawned child running"
    )


@_POSIX_ONLY
def test_an_exception_out_of_the_scope_still_reaps(tmp_path):
    """``attempt_scope``'s cleanup lives in a ``finally`` -- CancelRequested
    (or asyncio.CancelledError, or a budget/timeout raise) unwinding the
    caller's body must reap exactly like an ordinary return."""
    pid_box: dict[str, int] = {}
    with pytest.raises(CancelRequested):
        with attempt_procs.attempt_scope(
            "task12345678", "attempt87654321", root=tmp_path
        ) as scope:
            proc = subprocess.Popen(
                attempt_procs.launch_argv(_SLEEP_120, scope),
                **attempt_procs.spawn_kwargs(),
            )
            assert attempt_procs.register_group(proc.pid, scope)
            pid_box["pid"] = proc.pid
            os.kill(proc.pid, 0)  # alive mid-attempt
            raise CancelRequested("operator stop")

    assert _wait_dead(pid_box["pid"]), (
        "an exception unwinding attempt_scope must still reap"
    )


# ---------------------------------------------------------------------------
# worker dies without running cleanup
# ---------------------------------------------------------------------------

_CRASH_WORKER_SCRIPT = """
import os, sys, subprocess
from pathlib import Path
sys.path.insert(0, {src!r})
from no_human.core import attempt_procs

scope_cm = attempt_procs.attempt_scope({task!r}, {attempt!r}, root=Path({root!r}))
scope = scope_cm.__enter__()  # never __exit__'d -- this IS the crash
argv = attempt_procs.launch_argv({cmd!r}, scope)
# DEVNULL, not inherited: this script's own stdout is the pipe the outer
# test reads via `subprocess.run(capture_output=True)`. If the launcher
# inherited that same fd (production behaviour, correct for a real coder
# session's pipes), the outer read would block on EOF until the launcher
# itself exits -- which only happens once its watchdog notices this
# process is gone, so the outer call would never observe "still alive
# right after the crash" at all, only "already reaped by the watchdog".
proc = subprocess.Popen(
    argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL, **attempt_procs.spawn_kwargs(),
)
ok = attempt_procs.register_group(proc.pid, scope)
assert ok, "register_group refused the launcher's own group"
print(proc.pid, flush=True)
os._exit(1)  # a crashed worker: no `finally`, no atexit, no cleanup at all
"""


def _run_crash_worker(*, task_id: str, attempt_id: str, root: Path) -> int:
    script = _CRASH_WORKER_SCRIPT.format(
        src=str(SRC), task=task_id, attempt=attempt_id, root=str(root),
        cmd=_SLEEP_120,
    )
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, timeout=30, env=env,
    )
    assert result.returncode == 1, (result.stdout, result.stderr)
    return int(result.stdout.strip())


@_POSIX_ONLY
def test_a_record_left_by_a_dead_worker_is_reaped_at_startup(tmp_path):
    root = tmp_path / "records"
    sleeper_pid = _run_crash_worker(
        task_id="task12345678", attempt_id="attemptdeadbeef", root=root,
    )

    # the crashed worker never ran attempt_scope's `finally` -- the sleeper
    # must still be alive, and the record must still be on disk.
    os.kill(sleeper_pid, 0)
    assert len(list(root.glob("*.json"))) == 1

    results = attempt_procs.reap_stale(root=root)

    assert len(results) == 1
    marker, killed = results[0]
    assert marker == attempt_procs.marker_token("task12345678", "attemptdeadbeef")
    assert killed >= 1
    assert _wait_dead(sleeper_pid), (
        "reap_stale did not reap the dead worker's leftover child"
    )
    assert not list(root.glob("*.json")), (
        "reap_stale must remove the record it consumed"
    )


@_POSIX_ONLY
def test_a_record_owned_by_a_live_process_is_not_reaped(tmp_path):
    """The sweep only ever covers a DEAD owner. A record whose owner is still
    alive -- another live server, or this same process's own live scope --
    must never be swept: that would reap a peer's in-flight attempt."""
    root = tmp_path / "records"
    with attempt_procs.attempt_scope(
        "task12345678", "attemptalive000", root=root
    ) as scope:
        proc = subprocess.Popen(
            attempt_procs.launch_argv(_SLEEP_120, scope),
            **attempt_procs.spawn_kwargs(),
        )
        assert attempt_procs.register_group(proc.pid, scope)

        results = attempt_procs.reap_stale(root=root)

        assert results == [], (
            "reap_stale swept a record this same live process still owns"
        )
        os.kill(proc.pid, 0)  # still alive -- untouched

    assert _wait_dead(proc.pid)


# ---------------------------------------------------------------------------
# attributable from `ps` alone
# ---------------------------------------------------------------------------

@_POSIX_ONLY
def test_the_launcher_command_line_carries_the_task_and_attempt_id(tmp_path):
    with attempt_procs.attempt_scope(
        "task12345678", "attempt87654321", root=tmp_path
    ) as scope:
        proc = subprocess.Popen(
            attempt_procs.launch_argv(_SLEEP_120, scope),
            **attempt_procs.spawn_kwargs(),
        )
        assert attempt_procs.register_group(proc.pid, scope)
        try:
            ps = subprocess.run(
                ["ps", "-o", "command=", "-p", str(proc.pid)],
                capture_output=True, text=True,
            )
            line = ps.stdout
            assert "task1234" in line, line
            assert scope.marker in line, line
        finally:
            pass


@_POSIX_ONLY
def test_an_orphans_pgid_leads_back_to_the_marked_launcher(tmp_path):
    """``ps -eo pid,pgid,command`` must resolve an anonymous orphan back to
    the marked launcher line in ONE step -- the entire point of the marker,
    versus hours of event-stream archaeology for the incident's own
    orphans."""
    with attempt_procs.attempt_scope(
        "task12345678", "attemptcafebabe", root=tmp_path
    ) as scope:
        argv = attempt_procs.launch_argv(
            ["sh", "-c",
             f'{sys.executable} -c "import time; time.sleep(120)" & '
             f'echo GRANDCHILD $!; wait'],
            scope,
        )
        proc = subprocess.Popen(
            argv, stdout=subprocess.PIPE, text=True, **attempt_procs.spawn_kwargs()
        )
        assert attempt_procs.register_group(proc.pid, scope)
        line = proc.stdout.readline()
        grandchild_pid = int(line.split()[-1])

        assert os.getpgid(grandchild_pid) == proc.pid, (
            "the grandchild's pgid must be the launcher's own pid (the "
            "session/group leader), so ps -eo pid,pgid,command resolves it "
            "back to the marked launcher line in one step"
        )


# ---------------------------------------------------------------------------
# no behaviour change to a live long-running child
# ---------------------------------------------------------------------------

@_POSIX_ONLY
def test_a_child_still_needed_mid_attempt_is_not_killed(tmp_path):
    with attempt_procs.attempt_scope(
        "task12345678", "attemptstillup1", root=tmp_path
    ) as scope:
        long_proc = subprocess.Popen(
            attempt_procs.launch_argv(_SLEEP_120, scope),
            **attempt_procs.spawn_kwargs(),
        )
        assert attempt_procs.register_group(long_proc.pid, scope)

        short_proc = subprocess.Popen(
            attempt_procs.launch_argv([sys.executable, "-c", "pass"], scope),
            **attempt_procs.spawn_kwargs(),
        )
        assert attempt_procs.register_group(short_proc.pid, scope)
        short_proc.wait(timeout=10)  # the second group finishes on its own

        time.sleep(1.0)  # give a (nonexistent) mid-attempt reaper a chance to misfire
        os.kill(long_proc.pid, 0)  # still alive -- nothing inside a live attempt reaps

    assert _wait_dead(long_proc.pid)


# ---------------------------------------------------------------------------
# no behaviour change outside an attempt
# ---------------------------------------------------------------------------

def test_without_a_scope_nothing_is_wrapped_and_nothing_is_killed():
    """The regression guard for every existing caller and every CLI
    one-shot: no live attempt means ``attempt_procs`` is a complete no-op."""
    assert attempt_procs.current_scope() is None
    cmd = [sys.executable, "-c", "print('hi')"]
    assert attempt_procs.launch_argv(cmd) == cmd
    assert attempt_procs.register_group(os.getpid()) is False

    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.2)"])
    proc.wait(timeout=10)
    assert proc.returncode == 0, "an unwrapped child ran and exited untouched"


# ---------------------------------------------------------------------------
# the mandate is at the spawn sites
# ---------------------------------------------------------------------------

async def test_the_codex_backend_spawns_in_a_new_session(monkeypatch, tmp_path):
    captured: dict[str, object] = {}
    inner_spawn = _fake_codex(_HAPPY)

    async def capturing_spawn(*args, **kwargs):
        captured["argv"] = args
        captured["kwargs"] = kwargs
        return await inner_spawn(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capturing_spawn)
    _stub_cli(monkeypatch)
    backend = cx.CodexBackend(env=FAKE_ENV)

    with attempt_procs.attempt_scope(
        "task12345678", "attemptfeedface", root=tmp_path
    ) as scope:
        events = [
            e async for e in backend.stream("do the thing", cwd=Path("/repo"), max_turns=5)
        ]

    assert events, "the stubbed stream produced no events at all"
    expected = hidden_console_kwargs(new_group=True)
    for key, value in expected.items():
        assert captured["kwargs"].get(key) == value, (
            f"every in-attempt spawn must start a new session unconditionally "
            f"-- missing/mismatched {key!r} in {captured['kwargs']}"
        )
    argv = [str(a) for a in captured["argv"]]
    assert any("no_human.core.attempt_launcher" in a for a in argv), argv
    assert any(scope.marker in a for a in argv), argv


def test_the_claude_options_wrap_the_cli_in_a_launcher_shim(tmp_path):
    fake_cli = tmp_path / "fake-claude"
    fake_cli.write_text("#!/bin/sh\necho fake\n")
    fake_cli.chmod(0o755)
    backend = ClaudeBackend(model="claude-x", cli_path=str(fake_cli))

    with attempt_procs.attempt_scope(
        "task12345678", "attemptshimshim", root=tmp_path
    ) as scope:
        options = backend._options(tmp_path, 40)

        shim_path = Path(options.cli_path)
        assert shim_path != fake_cli
        assert shim_path.exists()
        text = shim_path.read_text()
        assert "no_human.core.attempt_launcher" in text
        assert scope.marker in text
        assert str(fake_cli) in text
        assert options.env.get(attempt_procs.RECORD_ENV) == str(scope.record_path), (
            "RECORD_ENV must survive scrub_foreign_secrets_into"
        )


def test_no_scope_leaves_cli_path_untouched(tmp_path):
    fake_cli = tmp_path / "fake-claude"
    fake_cli.write_text("#!/bin/sh\necho fake\n")
    fake_cli.chmod(0o755)
    backend = ClaudeBackend(model="claude-x", cli_path=str(fake_cli))

    assert attempt_procs.current_scope() is None
    options = backend._options(tmp_path, 40)

    assert options.cli_path == str(fake_cli)
    assert attempt_procs.RECORD_ENV not in options.env


# ---------------------------------------------------------------------------
# the self-kill guard
# ---------------------------------------------------------------------------

@_POSIX_ONLY
def test_the_reaper_never_records_the_servers_own_group(tmp_path):
    with attempt_procs.attempt_scope(
        "task12345678", "attemptselfkill", root=tmp_path
    ) as scope:
        ok = attempt_procs.register_group(os.getpid(), scope)

        assert ok is False
        assert scope.groups == set(), (
            "the server's own process group must never become a kill target"
        )
