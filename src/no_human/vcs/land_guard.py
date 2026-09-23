"""Keep a report-only pre-push gate off the `nh approve` land's critical path.

A land pushes the squashed commit straight from the operator's MAIN checkout
(`approve_merge.py` step 7), not from a worktree. If that checkout carries its
own `core.hooksPath` pre-push hook — a local history/policy scan, distinct
from the per-worktree `push_hook.py` guard the agent's task-branch pushes go
through — `git push` runs it INLINE and blocks until it finishes. When the
gate's mode is report-only (it logs a verdict for every check but cannot
refuse the push), that wait buys nothing: the push was always going to
succeed or fail on its own merits, and the scan's only possible output is a
log line. A slow scan under load then reads exactly like a hung land, because
the `nh approve` parent sits at 0% CPU with no test process running — it is
waiting on a grandchild, not wedged.

This module decides, for one land's push, whether the checkout's effective
pre-push hook should run synchronously (today's behavior — always true for an
unknown/enforcing mode, so this is impossible to defeat with an unconditional
`--no-verify`) or be deferred: the push goes through with `--no-verify` and
the SAME hook is immediately re-run out of band, over the identical range, as
a detached child that the land never waits on. Nothing here talks about any
particular gate by name — `nh-guard` / `verify_public_history.py` are private-
checkout artefacts this repo does not carry, and this module stays generic
over "the repo's effective pre-push hook" so it works whether that hook
exists or not.

Every public function here is best-effort and MUST NOT raise into a land: a
land whose push already reached the remote must never be reported as failed
because a logging/observability side-effect (resolving a hook, spawning a
deferred re-run) went wrong.
"""

from __future__ import annotations

import datetime as dt
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

#: Selects the local pre-push gate's behavior. Unset/empty means "report" —
#: the gate logs a verdict for every check but cannot refuse a push. Any
#: other value (including a future "enforce", or a typo) is treated as
#: enforcing: the push always runs the hook synchronously, so a mode this
#: module does not specifically recognise as safe-to-defer never gets
#: deferred. This is what keeps the T2 flip to enforce safe with no further
#: code change here.
GUARD_MODE_ENV = "NH_GUARD_MODE"

#: Escape hatch: "0" forces the push to run the hook synchronously even in
#: report mode (e.g. a human wants to watch the scan run inline).
DEFER_ENV = "NH_LAND_GUARD_DEFER"

_HOOK_RESOLVE_TIMEOUT_S = 10
_REMOTE_URL_TIMEOUT_S = 10
#: How long the deferred runner will wait to acquire the cross-land lock
#: before giving up. Never applies to the hook itself once it starts — the
#: hook, once running, is never waited on and never given a timeout.
_LOCK_WAIT_S = 600

_CHILDREN: list[subprocess.Popen] = []

# A tiny detached runner: takes a (bounded-wait) exclusive lock so two
# concurrent lands don't run two competing scans, then execs the real hook
# with the exact argv/stdin git would have handed it. Kept as an inline `-c`
# script (no new file on disk) so the deferred process is a single `Popen`
# with no extra install step.
_RUNNER_SRC = """
import fcntl, os, sys, time
lockfile, stdin_path, hook, remote, remote_url = sys.argv[1:6]
fd = os.open(lockfile, os.O_CREAT | os.O_RDWR, 0o644)
deadline = time.time() + %d
acquired = False
while time.time() < deadline:
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        acquired = True
        break
    except OSError:
        time.sleep(1)
if not acquired:
    sys.exit(1)
stdin_fd = os.open(stdin_path, os.O_RDONLY)
os.dup2(stdin_fd, 0)
os.close(stdin_fd)
try:
    os.unlink(stdin_path)
except OSError:
    pass
os.execv(hook, [hook, remote, remote_url])
""" % _LOCK_WAIT_S


@dataclass(frozen=True)
class PushGuardPlan:
    """What one land's push should do about the checkout's pre-push hook."""

    #: True: push with --no-verify, then re-run the hook out of band.
    #: False: push runs the hook inline, exactly like today.
    defer: bool
    #: The resolved, executable pre-push hook, or None if the checkout has
    #: none (in which case `defer` is always False — there is nothing to
    #: defer, and the push is a plain `git push`, unchanged).
    hook: Path | None
    #: Normalised `NH_GUARD_MODE` — "report", or the raw (stripped/lowered)
    #: value of any other setting.
    mode: str
    #: Human-readable reason, folded into the land's own output so a
    #: synchronous scan is visible rather than reading as a hang, and a
    #: deferred one names itself rather than vanishing silently.
    reason: str


def guard_mode(env) -> str:
    """Normalise `NH_GUARD_MODE`. Unset or blank means "report"."""
    raw = (env.get(GUARD_MODE_ENV, "") or "").strip()
    return raw.lower() if raw else "report"


def resolve_pre_push_hook(repo_path) -> Path | None:
    """The checkout's effective pre-push hook, or None.

    Uses `git rev-parse --git-path hooks/pre-push` rather than reading
    `core.hooksPath` by hand: `--git-path` already resolves a WORKTREE-scoped
    `core.hooksPath` (set via `git config --worktree`, as `push_hook.py`
    does) for free, so this one call is correct for both the main checkout
    and any linked worktree without this module needing to know which one it
    was given.
    """
    repo_path = Path(repo_path)
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_path), "rev-parse", "--git-path", "hooks/pre-push"],
            capture_output=True, text=True, timeout=_HOOK_RESOLVE_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    rel = proc.stdout.strip()
    if not rel:
        return None
    hook = Path(rel).expanduser()
    if not hook.is_absolute():
        hook = (repo_path / hook).resolve()
    try:
        if hook.exists() and os.access(hook, os.X_OK):
            return hook
    except OSError:
        return None
    return None


def plan_push(repo_path, env) -> PushGuardPlan:
    """Decide defer-vs-synchronous for one land's push.

    Defers ONLY when every one of these holds — anything else is
    synchronous, i.e. today's behavior exactly:
      * `guard_mode(env) == "report"` exactly;
      * `env.get(DEFER_ENV) != "0"` (the escape hatch is not set);
      * `os.name != "nt"` (no `start_new_session` on Windows);
      * an executable pre-push hook actually resolved.
    """
    mode = guard_mode(env)
    hook = resolve_pre_push_hook(repo_path)
    if hook is None:
        return PushGuardPlan(defer=False, hook=None, mode=mode,
                              reason="no pre-push hook")
    if mode != "report":
        if mode == "enforce":
            reason = ("guard synchronous (NH_GUARD_MODE=enforce): the "
                       "pre-push gate can refuse this land")
        else:
            reason = (f"unrecognised NH_GUARD_MODE={mode!r}; running the "
                       "gate synchronously")
        return PushGuardPlan(defer=False, hook=hook, mode=mode, reason=reason)
    if env.get(DEFER_ENV) == "0":
        return PushGuardPlan(
            defer=False, hook=hook, mode=mode,
            reason=f"deferral disabled ({DEFER_ENV}=0)")
    if os.name == "nt":
        return PushGuardPlan(
            defer=False, hook=hook, mode=mode,
            reason="deferral unsupported on this platform (Windows)")
    return PushGuardPlan(
        defer=True, hook=hook, mode=mode,
        reason="guard deferred (NH_GUARD_MODE=report): history scan runs "
               "out of band")


def resolve_log_dir(repo_path) -> Path:
    """Where the deferred gate's own log/lock files live.

    `<repo>/.nh-local/guard-log` when the checkout carries a `.nh-local/`
    directory (where a private-checkout gate already logs), else
    `<git-common-dir>/no_human-guard-log` — always inside the repo's own git
    dir, never a new top-level location. No external job queue, scheduler or
    persistence layer: this is a plain directory the deferred child appends
    to.
    """
    repo_path = Path(repo_path)
    nh_local = repo_path / ".nh-local"
    if nh_local.is_dir():
        return nh_local / "guard-log"
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_path), "rev-parse", "--git-common-dir"],
            capture_output=True, text=True, timeout=_HOOK_RESOLVE_TIMEOUT_S,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            common = Path(proc.stdout.strip())
            if not common.is_absolute():
                common = (repo_path / common).resolve()
            return common / "no_human-guard-log"
    except (OSError, subprocess.TimeoutExpired):
        pass
    return repo_path / ".git" / "no_human-guard-log"


def _reap_children() -> None:
    """Drop handles to children that have already exited.

    The hook process is never waited on and never given a timeout — killing
    a legitimate scan is exactly what this module exists to avoid — but a
    long-lived API server calling `run_deferred_gate` repeatedly must not
    accumulate zombie `Popen` handles either, so each call polls (never
    blocks) whatever is left over from earlier calls.
    """
    still_running = []
    for proc in _CHILDREN:
        if proc.poll() is None:
            still_running.append(proc)
    _CHILDREN[:] = still_running


def run_deferred_gate(repo_path, hook: Path, remote: str, remote_url: str,
                       local_sha: str, remote_ref: str, remote_sha: str,
                       log_dir) -> tuple[bool, str, Path | None]:
    """Spawn *hook* out of band, over exactly the range git would have given it.

    Reproduces the pre-push protocol: argv `[hook, remote, remote_url]`, one
    stdin line `"<local_sha> <local_sha> <remote_ref> <remote_sha>"` (the
    shape git hands a pre-push hook for a single-ref push naming the source
    sha directly, as `land_task`'s push does). `remote_sha` is expected to be
    the PRE-push remote tip the land already captured before pushing, so the
    deferred run's `--since`-style range matches the one git would have used
    inline.

    Returns `(started, note, log_path)`. Never raises: every failure mode
    (hook vanished, unwritable log dir, spawn failure) returns
    `(False, "<reason>", None)` so a never-started scan is surfaced rather
    than silently missing, and never fails a push that already succeeded.
    """
    _reap_children()
    try:
        log_dir = Path(log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        stdin_line = f"{local_sha} {local_sha} {remote_ref} {remote_sha}\n"
        fd, stdin_name = tempfile.mkstemp(
            prefix="deferred-gate-stdin-", suffix=".txt", dir=str(log_dir))
        with os.fdopen(fd, "w") as f:
            f.write(stdin_line)
        ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        log_path = log_dir / f"deferred-gate-{ts}-{local_sha[:12]}.log"
        lock_path = log_dir / "deferred-gate.lock"
        with open(log_path, "ab") as log_f:
            proc = subprocess.Popen(
                [sys.executable, "-c", _RUNNER_SRC, str(lock_path),
                 stdin_name, str(hook), remote, remote_url or ""],
                cwd=str(repo_path), stdout=log_f, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, start_new_session=True,
                close_fds=True,
            )
    except OSError as exc:
        return False, f"failed to start deferred gate: {exc}", None
    _CHILDREN.append(proc)
    return True, f"deferred gate spawned (pid {proc.pid})", log_path
