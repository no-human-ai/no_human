"""The process that makes an orphan attributable and reapable.

Run as ``python -m no_human.core.attempt_launcher <marker> --record <path>
-- <argv...>``. Its OWN command line is what makes a leaked descendant
attributable from ``ps`` alone — the incident this whole feature answers
(see ``core/attempt_procs.py``'s module docstring) was 36 orphaned processes
whose argv carried no task id, no attempt id, and no marker at all, so the
only way to find their source was six hours of event-stream archaeology.
This process puts itself in its own session/group, records that group on
the attempt's on-disk ledger, runs the real command with inherited stdio,
and kills the whole group when the child exits, when it is signalled, or
when its own parent dies without telling it to (a crashed/`SIGKILL`ed
worker) — the last line of defence ahead of ``attempt_procs.reap_stale``.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from ..proc import hidden_console_kwargs
from . import attempt_procs

_POSIX = os.name == "posix"
_WATCHDOG_INTERVAL_S = 2.0
_GRACE_S = 0.5


def _parse(argv: list[str]) -> tuple[str, Path, list[str]]:
    if "--" not in argv:
        raise SystemExit(
            "attempt_launcher: usage: <marker> --record <path> -- <cmd...>")
    sep = argv.index("--")
    head, cmd = argv[:sep], argv[sep + 1:]
    if not head or "--record" not in head or not cmd:
        raise SystemExit(
            "attempt_launcher: usage: <marker> --record <path> -- <cmd...>")
    marker = head[0]
    record = Path(head[head.index("--record") + 1])
    return marker, record, cmd


def _own_group() -> int:
    """Become session/group leader; tolerate already being one."""
    if not _POSIX:
        return os.getpid()
    try:
        os.setsid()
    except OSError:
        pass  # already a session leader - continue with whatever group this is
    return os.getpgid(0)


def _killpg(pgid: int, sig: "signal.Signals") -> None:
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass  # already gone, or not ours - either way nothing more to do here


def _kill_group(pgid: int) -> None:
    _killpg(pgid, signal.SIGTERM)
    time.sleep(_GRACE_S)
    _killpg(pgid, signal.SIGKILL)


def _watch_parent(stop: threading.Event, do_kill) -> None:
    """Daemon thread: if this process is reparented to init (ppid == 1), the
    server that started it died without running its own cleanup. Kill the
    group and exit rather than leaving it running unattended — the exact
    shape of the incident this module exists to close.

    Takes the idempotent kill callback rather than calling `_kill_group`
    itself, so a race with a concurrently delivered signal (which also kills
    the group) can never double-fire the self-directed-SIGTERM recursion.
    """
    while not stop.wait(_WATCHDOG_INTERVAL_S):
        if os.getppid() == 1:
            do_kill()
            os._exit(1)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    marker, record, cmd = _parse(argv)

    pgid = _own_group()
    if not attempt_procs.append_group_to_record(record, pgid):
        # The record is already gone: the attempt ended before this launcher
        # got to register. Nothing will ever reap what we'd spawn, so don't
        # spawn it at all - the parent has moved on.
        return 0

    if not _POSIX:
        # UNTESTED ON WINDOWS - see runner._kill_process_tree for the same
        # honesty about this branch.
        proc = subprocess.Popen(cmd, **hidden_console_kwargs(new_group=True))
        code = proc.wait()
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                        capture_output=True, check=False)
        return code

    # Inherited stdio (no pipes): the Agent SDK's own pipes are THIS
    # process's fds 0/1/2 and must pass through untouched.
    proc = subprocess.Popen(cmd)

    # `_kill_group` signals our OWN process group (we are the group leader),
    # which re-delivers the very signal we're handling back to us. Called
    # directly from a handler for that signal, this re-enters the handler
    # while it is still installed and recurses without bound (observed live:
    # a SIGTERM sent to kill the group re-triggered `_forward`, which sent
    # another SIGTERM, ... until the interpreter's recursion limit). Guard
    # with an idempotency flag and reset the handlers to default disposition
    # BEFORE signalling, so the self-directed signal has nothing left to
    # re-enter and a second caller (handler + watchdog + finally, in any
    # order/race) only ever runs the kill once.
    killed = threading.Event()

    def _do_kill() -> None:
        if killed.is_set():
            return
        killed.set()
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            try:
                signal.signal(sig, signal.SIG_DFL)
            except (ValueError, OSError):  # pragma: no cover - defensive
                pass
        _kill_group(pgid)

    def _forward(signum, frame):  # noqa: ANN001 - signal handler signature
        _do_kill()
        os._exit(128 + signum)

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, _forward)

    stop = threading.Event()
    watchdog = threading.Thread(
        target=_watch_parent, args=(stop, _do_kill), daemon=True,
        name=f"attempt-launcher-watchdog-{marker}",
    )
    watchdog.start()

    code = proc.returncode
    try:
        code = proc.wait()
    finally:
        stop.set()
        # The coder's own leftover children (the incident's
        # `while True: pass`) live here, siblings of the child we waited on.
        _do_kill()
    return code if code is not None else 0


if __name__ == "__main__":
    sys.exit(main())
