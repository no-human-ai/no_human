"""The reaper for a dead pool's stranded IMPLEMENTING attempt.

`Store.close_attempts_of_terminal_tasks` retires an `in_progress` attempt row
left open on a task that already reached `done`/`failed` — but nothing
retires the other shape: a task still `IMPLEMENTING` whose attempt row is
`in_progress` because the WHOLE POOL that owned it died (crash, kill -9, host
reboot) rather than just the one worker coroutine. `Scheduler._recover_orphans`
cannot see it either — that sweep iterates `_ORPHANABLE`
(CONTEXT/PLANNING/REVIEWING/TESTING), and IMPLEMENTING is deliberately absent
from that tuple, because a live worker owns most IMPLEMENTING rows most of
the time and requeueing one out from under a live worker was incident
6408aba0. So a dead pool's IMPLEMENTING row is invisible to every existing
sweep: on restart the new pool wastes a full attempt cycle rediscovering
already-done work before escalating, and every reconciliation verb stays
fail-closed against `implementing` status forever, because the attempt row
never stops lying about being open.

This module is the fix, kept OUT of `core/db.py` and `core/scheduler.py`
(both line-budget frozen, see `tests/test_structural_budget.py`) rather than
folded into either. It never imports from `cli` (core must not depend on
cli) — the server-probe / pidfile-liveness logic mirrored from
`cli.commands._server_owns_worker`/`_pidfile_owner_alive` is reimplemented
here, independently, deliberately duplicated rather than shared.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Awaitable, Callable

from .db import Store
from .task import TaskStatus, assert_stranded_reap

log = logging.getLogger("no_human.stranded_attempts")

#: How old a `scheduler_heartbeat` row may be before this reaper treats its
#: holder as gone rather than live. This is a SEPARATE constant from
#: `Scheduler._HEARTBEAT_STALE_S` (same 300s value, same intent — "how long
#: before a quiet holder's lease itself is considered abandoned") — not a
#: reuse of it, because this module must not import from `scheduler.py` (the
#: dependency would run the other way: `scheduler.py` imports this module to
#: wire the reaper into `run_forever`).
_LEASE_STALE_S = 300.0

#: Timeout for the local HTTP liveness probe below — mirrors
#: `cli.commands._server_owns_worker`'s 1.5s budget exactly.
_SERVER_PROBE_TIMEOUT_S = 1.5

_REASON = ("interrupted: its pool was no longer running (dead server, dead "
           "pidfile owner, expired lease, and no live worktree owner) while "
           "this row was left open")


async def _default_server_probe(config: dict[str, Any]) -> bool:
    """True when an `nh start`/`nh serve` HTTP API answers at `server.host`
    /`server.port`. Reimplements `cli.commands._server_owns_worker`'s HTTP
    half locally (core must not import cli) — any doubt (unreachable,
    non-200, unparsable body) reads as NOT live from this signal alone; the
    pidfile and lease signals still get their own say."""
    import json as _json
    import urllib.error
    import urllib.request

    srv = config.get("server", {}) or {}
    host = srv.get("host", "127.0.0.1")
    port = srv.get("port", 8420)
    try:
        with urllib.request.urlopen(
            f"http://{host}:{port}/api/tasks", timeout=_SERVER_PROBE_TIMEOUT_S,
        ) as resp:
            if resp.status != 200:
                return False
            _json.loads(resp.read() or b"null")
            return True
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return False


async def pool_owner_is_live(
    store: Store,
    config: dict[str, Any],
    *,
    my_pid: int | None = None,
    server_probe: Callable[[], Awaitable[bool] | bool] | None = None,
) -> bool:
    """Fail-CLOSED check: is there any evidence the pool that owns the
    IMPLEMENTING rows below is still alive?

    Three signals, checked in order, ANY of which being live is enough to
    say so; any exception anywhere in here also reads as live (erring
    toward never touching a row that might still be owned):

      1. The pidfile (`NO_HUMAN_HOME/nh.pid`) names a pid that is not ours
         and is alive.
      2. Failing that, the HTTP server probe answers (its own docstring
         explains why the pidfile is checked FIRST here, the opposite order
         of `_server_owns_worker`: a pidfile hit lets this skip the network
         call entirely for the common case of a live pool checking on
         itself).
      3. Failing both, the `scheduler_heartbeat` lease row names a pid/host
         that is not ours and is not yet stale.
    """
    import os

    from .. import config as config_module

    my_pid = os.getpid() if my_pid is None else my_pid

    try:
        pidfile = config_module.NO_HUMAN_HOME / "nh.pid"
        raw = pidfile.read_text(encoding="utf-8").strip()
        pid = int(raw)
    except (OSError, ValueError):
        pid = None
    except Exception:  # noqa: BLE001 — any doubt about the pidfile is "live"
        return True

    if pid is not None:
        if pid == my_pid:
            return False  # the pidfile names US — no server/lease to check
        try:
            if config_module.pid_alive(pid):
                return True
        except Exception:  # noqa: BLE001 — fail closed
            return True

    try:
        probe = server_probe if server_probe is not None else (
            lambda: _default_server_probe(config))
        result = probe()
        if hasattr(result, "__await__"):
            result = await result
        if result:
            return True
    except Exception:  # noqa: BLE001 — fail closed
        return True

    try:
        row = await store.read_scheduler_heartbeat()
    except Exception:  # noqa: BLE001 — an unreadable lease is "live"
        return True
    if row is not None:
        try:
            row_mine = int(row["pid"]) == my_pid
        except (TypeError, ValueError):
            row_mine = False
        if not row_mine:
            age = time.time() - float(row["ts"])
            if age < _LEASE_STALE_S:
                return True

    return False


def _worktree_owner_alive(task_id: str, config: dict[str, Any]) -> bool:
    """True when a worktree directory names *task_id* with a `owner_pid`
    that is provably alive — mirrors `worktree.salvage_dead_worktrees`'s own
    ownership walk. `owner_pid is None` (legacy bare-`<task_id>` dirs, or a
    name that does not parse) is NOT proof of a live owner — same
    convention as every other reader of `worktree_owner`. Any error walking
    the root (missing dir, permission) fails CLOSED (treated as live)."""
    from .. import config as config_module

    try:
        root = config_module.worktree_root(config)
        if not root.exists():
            return False
        for entry in root.iterdir():
            owner_task_id, owner_pid = config_module.worktree_owner(entry.name)
            if owner_task_id != task_id or owner_pid is None:
                continue
            if config_module.pid_alive(owner_pid):
                return True
    except Exception:  # noqa: BLE001 — fail closed
        return True
    return False


async def reap_stranded_implementing_attempts(
    store: Store,
    config: dict[str, Any],
    *,
    my_pid: int | None = None,
    row_is_live: Callable[[Any], Awaitable[bool]] | None = None,
    server_probe: Callable[[], Awaitable[bool] | bool] | None = None,
) -> tuple[int, int]:
    """Retire a dead pool's stranded `in_progress` attempt(s) and return
    their tasks to `pending`/`awaiting_approval`. Returns `(reaped,
    skipped)`. Never raises — every failure is a skip, not a crash, so one
    bad row can never abort the whole pass (see the caller,
    `Scheduler._reap_stranded_implementing_attempts`, for the boot-safety
    wrapper this still sits under).

    Fails CLOSED at every step: `pool_owner_is_live` first (any live signal
    at all means EVERY candidate is left untouched, not just the ones that
    happen to look busy), then per-task the worktree-owner check and the
    caller-supplied `row_is_live` (recent row/event activity — the
    scheduler wires its own `_row_is_live`), then the attempt-row CAS in
    `Store.close_stranded_attempt` itself (a live worker or a second sweep
    winning the race is a no-op, not a clobber).
    """
    candidates = await store.list_tasks(TaskStatus.IMPLEMENTING)
    if not candidates:
        return (0, 0)

    if await pool_owner_is_live(
        store, config, my_pid=my_pid, server_probe=server_probe,
    ):
        return (0, len(candidates))

    reaped = 0
    skipped = 0
    for t in candidates:
        row_reaped = False
        try:
            if row_is_live is not None and await row_is_live(t):
                continue
            if _worktree_owner_alive(t.id, config):
                continue
            attempt = await store.latest_open_attempt(t.id)
            if attempt is None:
                continue
            closed = await store.close_stranded_attempt(
                t.id, attempt["id"], reason=_REASON)
            if not closed:
                continue
            row_reaped = True

            pr_url = (attempt.get("pr_url") or "").strip()
            review_passed = attempt.get("review_passed")
            target = (TaskStatus.AWAITING_APPROVAL
                      if pr_url and review_passed == 1 else TaskStatus.PENDING)

            current = await store.get_task(t.id)
            if current is not None:
                await store.set_status(
                    current, target, reconciliation_gate=assert_stranded_reap)
        except Exception:  # noqa: BLE001 — one bad row must not abort the pass
            log.exception(
                "stranded-attempt reap failed for task %s", getattr(t, "id", "?"))
        finally:
            if row_reaped:
                reaped += 1
            else:
                skipped += 1

    return (reaped, skipped)
