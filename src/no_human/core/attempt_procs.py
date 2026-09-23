"""Reap every process an attempt's shell commands leave running.

THE INCIDENT this closes: 36 orphaned ``.../Python3.9 -c "while True: pass"``
processes, all 6h33m-6h37m old, each ~33% CPU, together roughly 12 of 18
cores on an 18-core box (load average peaked at 104.76). They came from a
CODER AGENT's own exploratory shell command during implementation of an
unrelated task (``d41812aa``) — an attempt is free to run shell commands,
that is the product working as designed, and the committed test for that
task is a clean monkeypatch-only design that could not have produced them.
Two independent readings blamed the WORKER POOL for being over-concurrent
and were about to cut ``max_workers`` — which needed a server restart,
would have killed in-flight attempts, and would not have touched the cause.
``ps`` gave nothing to attribute the leak with: bare ``python -c "while
True: pass"``, no task id, no attempt id, no worktree path.

THE DEFECT was never the command — it is that nothing accounted for
processes an attempt (or its subagent) left running when the attempt ended.
An agent that is interrupted, budget-capped or crashed cannot clean up
after itself; the harness must not depend on it remembering to. This module
is the missing accounting: every attempt gets a scope, every process group
spawned under that scope is recorded to disk (durable across a crashed
worker or a server restart), and the scope's exit — an ordinary return, a
cancellation, a budget/timeout raise, or any other exception, because the
cleanup lives in a ``finally`` — kills every group it still owns. A startup
sweep (:func:`reap_stale`) covers the one path a live scope cannot: a
worker process that died without running its own cleanup at all.

Every process this module spawns or wraps carries ``NH_ATTEMPT=<task
id>:<attempt id>`` on its own command line (see :mod:`attempt_launcher`),
so the NEXT orphan — because there will be one, this system degrades to a
loud log rather than an exception when it cannot kill something — is
attributable from ``ps`` alone, in one step, instead of six hours of event
stream.

Every entry point here is optional: a bare ``nh run``, a unit test, or a CLI
one-shot with no live scope must behave exactly as it did before this module
existed. :func:`current_scope` returns ``None`` outside an attempt, and
every consumer (:func:`launch_argv`, :func:`register_group`, :func:`cli_shim`)
is a no-op in that case.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import os
import signal
import stat
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..config import NO_HUMAN_HOME
from ..proc import hidden_console_kwargs, real_python

log = logging.getLogger("no_human.attempt_procs")

# ps-greppable, never a generic pattern (`pkill -f "while True"` was the
# thing that could NOT attribute this incident's orphans; every process this
# module touches carries this on its own argv instead).
MARKER = "NH_ATTEMPT"

# The env var name that carries the record path to a descendant that
# re-execs (e.g. the Claude CLI shim). Deliberately NOT secret-shaped (no
# TOKEN/SECRET/KEY/... substring) so `child_env.scrub_foreign_secrets_into`
# never blanks it — verified by test, not assumed.
RECORD_ENV = "NH_ATTEMPT_RECORD"

_POSIX = os.name == "posix"
_SIGTERM = signal.SIGTERM
_SIGKILL = getattr(signal, "SIGKILL", signal.SIGTERM)


def marker_token(task_id: str, attempt_id: str) -> str:
    """The literal ``ps``-visible marker for one attempt."""
    return f"{MARKER}={task_id[:8]}:{attempt_id[:8]}"


@dataclass
class AttemptProcScope:
    """One attempt's process-group ledger: in-memory plus a durable record."""

    task_id: str
    attempt_id: str
    record_path: Path
    marker: str
    groups: set[int] = field(default_factory=set)
    lock: threading.Lock = field(default_factory=threading.Lock)


_CURRENT: contextvars.ContextVar[AttemptProcScope | None] = contextvars.ContextVar(
    "no_human_attempt_proc_scope", default=None
)


def current_scope() -> AttemptProcScope | None:
    """The calling task's process scope, or ``None`` outside an attempt.

    Every consumer in this module must work unchanged when this is
    ``None`` — the same contract as
    ``agent.worker_context.current_worker_context``.
    """
    return _CURRENT.get()


def _records_root(root: Path | None = None) -> Path:
    r = root if root is not None else (NO_HUMAN_HOME / "attempt_procs")
    r.mkdir(parents=True, exist_ok=True)
    return r


def _own_pgid() -> int | None:
    if not _POSIX:
        return None
    try:
        return os.getpgid(0)
    except OSError:  # pragma: no cover - defensive
        return None


def _atomic_write(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)


def _write_record(scope: AttemptProcScope) -> None:
    _atomic_write(scope.record_path, {
        "task_id": scope.task_id,
        "attempt_id": scope.attempt_id,
        "marker": scope.marker,
        "owner_pid": os.getpid(),
        "started_at": time.time(),
        "groups": sorted(scope.groups),
    })


def append_group_to_record(record_path: Path, pgid: int) -> bool:
    """Add ``pgid`` to an on-disk record's ``groups``, for a process (the
    launcher) that has only the record path, not the in-process
    :class:`AttemptProcScope`. Returns ``False`` without writing when the
    record is already gone — the attempt ended and the parent has moved on,
    so there is nothing left to append to and nothing to kill.
    """
    try:
        data = json.loads(record_path.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    groups = set(data.get("groups") or [])
    groups.add(pgid)
    data["groups"] = sorted(groups)
    try:
        _atomic_write(record_path, data)
    except OSError:
        return False
    return True


@contextlib.contextmanager
def attempt_scope(task_id: str, attempt_id: str, *, root: Path | None = None):
    """Bind the calling context to a fresh process scope for one attempt.

    On exit (however it happens — a normal return, ``CancelRequested``,
    ``asyncio.CancelledError``, a budget/timeout raise, or a crash out of
    the caller's body — because this lives in a ``finally``) every process
    group recorded under the scope is reaped, then the on-disk record is
    removed. This ``finally`` is the ONLY reaper on the ordinary attempt
    path; see the module docstring for why that is load-bearing.

    Failing to write the record or to reap must never fail the attempt
    itself (repo convention: cleanup never fails a task) — logged at
    ERROR/WARNING and swallowed.
    """
    marker = marker_token(task_id, attempt_id)
    records_root = _records_root(root)
    record_path = records_root / f"{attempt_id}.json"
    scope = AttemptProcScope(
        task_id=task_id, attempt_id=attempt_id,
        record_path=record_path, marker=marker,
    )
    try:
        _write_record(scope)
    except OSError:
        log.error("failed to write process record for %s", marker, exc_info=True)

    token = _CURRENT.set(scope)
    try:
        yield scope
    finally:
        _CURRENT.reset(token)
        try:
            reap(scope)
        except Exception:  # noqa: BLE001 - cleanup must never fail the attempt
            log.error("reap failed for %s", marker, exc_info=True)
        try:
            scope.record_path.unlink(missing_ok=True)
        except OSError:
            log.warning("could not remove process record for %s", marker)


def register_group(pid: int, scope: AttemptProcScope | None = None) -> bool:
    """Record the process group of ``pid`` as owned by ``scope`` (or the
    current scope). Returns whether anything was recorded.

    A no-op when there is no live scope — the regression guard for every
    existing caller and the CLI one-shots.

    Refuses to record a group equal to THIS process's own group — load
    bearing: without it, a child that failed to get its own session would
    make the reaper kill the server itself.
    """
    scope = scope if scope is not None else current_scope()
    if scope is None:
        return False
    if _POSIX:
        try:
            pgid = os.getpgid(pid)
        except OSError:
            return False
    else:
        pgid = pid
    own = _own_pgid()
    if own is not None and pgid == own:
        log.warning(
            "refusing to record this server's own process group (%s) for %s",
            pgid, scope.marker,
        )
        return False
    with scope.lock:
        scope.groups.add(pgid)
        try:
            _write_record(scope)
        except OSError:
            log.warning("failed to persist group %s for %s", pgid, scope.marker)
    return True


def _killpg_posix(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        pass  # already gone - the common, happy case
    except PermissionError:
        log.warning("no permission to signal group %s (marker unknown here)", pgid)


def _group_alive_posix(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just not ours to probe further


def reap(scope: AttemptProcScope, *, grace: float = 3.0) -> int:
    """Kill every process group ``scope`` owns. Returns the count attempted.

    POSIX: SIGTERM the group, poll for exit up to ``grace`` seconds, then
    SIGKILL whatever remains. A group already gone is a no-op, exactly like
    ``testing.runner.terminate_running``'s contract. A ``PermissionError``
    degrades to a WARNING naming the pgid and the marker — never raised.
    """
    with scope.lock:
        groups = sorted(scope.groups)
    if not groups:
        return 0
    if not _POSIX:
        for pid in groups:
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    capture_output=True, check=False,
                )
            except OSError:
                pass
        return len(groups)

    for pgid in groups:
        _killpg_posix(pgid, _SIGTERM)
    deadline = time.monotonic() + grace
    remaining = set(groups)
    while remaining and time.monotonic() < deadline:
        remaining = {pgid for pgid in remaining if _group_alive_posix(pgid)}
        if remaining:
            time.sleep(0.05)
    for pgid in remaining:
        _killpg_posix(pgid, _SIGKILL)
    return len(groups)


def _is_alive(pid: int) -> bool:
    if not _POSIX:
        return True  # Windows: unsupported, never claim staleness we can't check
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just not ours


def reap_stale(*, root: Path | None = None, is_alive=None) -> list[tuple[str, int]]:
    """Startup/restart sweep: reap every on-disk record left by a process
    that is no longer running (a crashed worker, a killed server).

    A record owned by THIS process, or by another still-live process (a
    live scope, or another ``nh serve`` instance), is left alone — this
    must never reap a peer's in-flight attempt. Returns
    ``[(marker, killed_count), ...]`` for the caller to log. A malformed or
    half-written record is deleted with a warning, never raised.
    """
    alive = is_alive if is_alive is not None else _is_alive
    records_root = _records_root(root)
    results: list[tuple[str, int]] = []
    for path in sorted(records_root.glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            log.warning("dropping unreadable process record %s", path)
            path.unlink(missing_ok=True)
            continue
        owner_pid = data.get("owner_pid")
        if owner_pid == os.getpid():
            continue  # a live scope in THIS process owns it
        if isinstance(owner_pid, int) and alive(owner_pid):
            continue  # another live server owns it - not ours to touch
        marker = data.get("marker", "<unknown>")
        groups = data.get("groups") or []
        scope = AttemptProcScope(
            task_id=data.get("task_id", ""), attempt_id=data.get("attempt_id", ""),
            record_path=path, marker=marker,
            groups=set(g for g in groups if isinstance(g, int)),
        )
        killed = reap(scope)
        results.append((marker, killed))
        path.unlink(missing_ok=True)
    return results


def launch_argv(argv: list[str], scope: AttemptProcScope | None = None,
                 *, python: str | None = None) -> list[str]:
    """Wrap ``argv`` so it runs under the launcher shim, attributable and
    reaped. Returns ``argv`` unchanged when there is no live scope, or when
    no separate Python interpreter is available (a frozen build) — fail
    open, never break the coder session to get a reaper.
    """
    scope = scope if scope is not None else current_scope()
    if scope is None:
        return argv
    py = python if python is not None else real_python()
    if py is None:
        log.warning("no python interpreter available - running %s unwrapped", argv[:1])
        return argv
    return [
        py, "-m", "no_human.core.attempt_launcher", scope.marker,
        "--record", str(scope.record_path), "--", *argv,
    ]


_SHIM_TEMPLATE = """#!/bin/sh
exec {python} -m no_human.core.attempt_launcher {marker} --record {record} -- {cli} "$@"
"""


def cli_shim(scope: AttemptProcScope | None, real_cli: str) -> Path | None:
    """A tiny POSIX shell shim that re-execs ``real_cli`` through the
    launcher, for callers (the Agent SDK) that build their own argv and
    accept no extra arguments. Returns ``None`` (leave ``cli_path`` alone)
    whenever the shim cannot be safely built - Windows, no scope, no
    interpreter, ``real_cli`` missing, or a write failure.
    """
    if scope is None or not _POSIX:
        return None
    py = real_python()
    if py is None:
        return None
    cli_path = Path(real_cli)
    if not cli_path.is_file():
        return None
    shim_path = scope.record_path.parent / f"{scope.attempt_id}-claude"
    try:
        shim_path.write_text(_SHIM_TEMPLATE.format(
            python=py, marker=scope.marker, record=scope.record_path,
            cli=cli_path,
        ))
        shim_path.chmod(shim_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    except OSError:
        log.warning("failed to write CLI shim for %s", scope.marker, exc_info=True)
        return None
    return shim_path


def spawn_kwargs() -> dict[str, object]:
    """One spelling of the new-session mandate for every in-attempt spawn
    site: ``hidden_console_kwargs(new_group=True)``, re-exported here so a
    caller does not need a separate import of ``proc`` just for this.
    """
    return hidden_console_kwargs(new_group=True)
