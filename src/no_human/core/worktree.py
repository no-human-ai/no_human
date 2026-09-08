"""Centralized worktree teardown, and the startup janitor that reclaims what a
missed teardown left behind.

``run_task``'s ``finally`` block used to remove a worktree with a bare
``main_repo.remove_worktree`` call and nothing that checked whether it worked.
``git worktree remove`` runs with ``check=False`` (``vcs/git.py``), so a
failure — a dirty tree, a stale lock, a main repo that no longer exists —
was silently swallowed and the directory stayed on disk forever.
``_reap_dead_worktrees`` only ever reclaims leftovers of the ONE task about to
run again, so a task that reaches ``done``/``failed`` and never runs again
leaves its directory unreachable for the rest of time. That is the structural
leak measured 2026-08-12: 106 directories, 2.2GB, under
``~/.no_human/worktrees``.

``teardown_worktree`` is the one place both callers (``run_task``'s
``finally`` and ``_reap_dead_worktrees``) now route through: it adds the
missing post-condition check and an ``rmtree`` fallback, and it is loud (an
ERROR log line, never swallowed) when a directory survives both. It never
raises — teardown must not be able to fail a task or crash boot.

``sweep_stale_worktrees`` is the janitor: a STARTUP-ONLY sweep (never a
per-tick one) of every directory under the worktree root, run once before the
scheduler starts dispatching. A directory is reclaimed only when it is
PROVABLY dead — never on age or mtime — per the same conservative-skip rule
``doctor.py``'s W2.6 orphan check already uses: an owner pid that is still
alive always wins over whatever the task's status says, a task this store
does not know about is left alone unless its worktree's own git admin
directory (the ``gitdir:`` target inside ``.git``) has itself vanished (the
"deleted bench sandbox" class — 88 of the 106 measured), and anything that
cannot be read or parsed is skipped with a loud warning rather than guessed
at.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Callable

from ..testing import runner
from .task import TERMINAL_STATES, TaskStatus

log = logging.getLogger("no_human.worktree")

# npm ci in a cold worktree (no shared cache) is minutes, not seconds — this
# is a per-COMMAND ceiling, not a whole-chain one.
SETUP_TIMEOUT_S = 1800
# Lives in the worktree's git ADMIN dir (see `_read_gitdir_target`), never in
# the working tree itself — so it never shows up in `git status`, never
# collides with a repo file, and is never a candidate residual file for the
# reviewer's scope guard.
SETUP_MARKER_NAME = "no_human_setup_done"


class WorktreeSetupError(RuntimeError):
    """A profile-declared `setup_cmds` entry failed, timed out, or could not
    be spawned. Always names the exact command — the orchestrator surfaces
    this as an infra/environment failure, never a test failure."""

    def __init__(self, command: str, detail: str):
        self.command = command
        self.detail = detail
        super().__init__(f"setup command failed: {command} — {detail}")


def _setup_marker_path(worktree_path: Path) -> Path | None:
    """Resolve the git ADMIN dir for a linked worktree so the idempotency
    marker never lands in the working tree. Returns ``None`` when it cannot
    be resolved — the caller's fallback is "no marker, always run", never
    "guess a path and maybe collide with something else"."""
    git_entry = Path(worktree_path) / ".git"
    admin_dir = _read_gitdir_target(Path(worktree_path))
    if admin_dir is not None:
        return admin_dir / SETUP_MARKER_NAME
    if git_entry.is_dir():
        return git_entry / SETUP_MARKER_NAME
    return None


def run_setup_commands(
    worktree_path: Path,
    cmds: list[str],
    *,
    emit: Callable[..., None] | None = None,
    timeout: int = SETUP_TIMEOUT_S,
) -> list[str]:
    """Run a profile's `setup_cmds` in order, from the worktree ROOT, once per
    fresh worktree — the build prerequisites (`npm ci`, `npm run build`...) a
    `git worktree add` checkout can never contain because they are
    gitignored. Returns the commands actually run (``[]`` when there was
    nothing to do, or when this exact worktree path already carries the
    marker from an earlier call). That second case guards a re-entry into the
    SAME worktree path — not a per-attempt saving. Task worktrees are minted
    one per RUN (`Orchestrator._worktree_path`, torn down in
    `_run_task_body`'s `finally`) and setup runs at most once per
    `_drive_watched`, before the attempt loop, so no caller today calls this
    twice on one path; the marker only matters if that ever changes. Raises
    `WorktreeSetupError` naming the failing command; never raises anything
    else."""
    normalized = [c.strip() for c in (cmds or []) if c and c.strip()]
    if not normalized:
        return []

    worktree_path = Path(worktree_path)
    marker = _setup_marker_path(worktree_path)
    if marker is not None and marker.exists():
        log.info("worktree setup SKIPPED for %s — marker already present at %s",
                  worktree_path, marker)
        if emit is not None:
            emit("worktree_setup_skipped", f"already ran in {worktree_path}")
        return []

    for cmd in normalized:
        log.info("worktree setup: running %r in %s", cmd, worktree_path)
        if emit is not None:
            emit("worktree_setup_running", cmd)
        # Reuse runner._run_shell rather than a second `subprocess.run(...,
        # timeout=)` here: a bare subprocess.run only kills the shell on
        # timeout, leaving grandchildren (e.g. `sleep 40` under `sh -c`)
        # orphaned at ppid 1 — measured. _run_shell spawns in its own process
        # group (_NEW_GROUP_KWARGS), registers under the worktree path
        # (runner._register) so teardown_worktree's terminate_running(wt_path)
        # reaps anything still alive, and on timeout kills the WHOLE tree via
        # _kill_process_tree/killpg instead of just the shell.
        try:
            # `_env_for` inherits this process's VIRTUAL_ENV and only
            # overrides it when the worktree ALREADY has a venv, so on a fresh
            # worktree these commands ran against the shared checkout's venv:
            # the same defect as env_setup, on a path added after that fix was
            # written (#128). `isolate_attempt_env` is applied last so it sees
            # whatever `_env_for` resolved.
            rc, output, timed_out = runner._run_shell(
                cmd, worktree_path, timeout,
                isolate_attempt_env(worktree_path, runner._env_for(worktree_path)))
        except OSError as exc:
            raise WorktreeSetupError(cmd, f"could not start: {exc}")
        if timed_out:
            raise WorktreeSetupError(cmd, f"timed out after {timeout}s")
        if rc != 0:
            tail = (output or "").strip()[-200:]
            raise WorktreeSetupError(
                cmd, f"exit {rc}: {tail}" if tail else f"exit {rc}")

    if marker is not None:
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text("\n".join(normalized))
        except OSError as exc:  # noqa: BLE001 — marker write must never fail the task
            log.warning("could not write setup marker at %s: %s", marker, exc)
    log.info("worktree setup OK for %s: %d command(s)", worktree_path, len(normalized))
    return normalized

# Worktree paths THIS PROCESS is currently running a task in. Only the reaper
# and the janitor read it, and only to answer one question: "is this
# directory, which carries my own pid, one I am using right now, or one my
# own earlier crashed run left behind?" — a question `os.kill(pid, 0)` cannot
# answer for our own pid.
#
# It is NOT a lock. Nothing waits on it, nothing is excluded by it, and two
# attempts of one task still start freely and run side by side; it only ever
# decides what is safe to DELETE. Serialising attempts would be the wrong
# fix: overlap is legitimate, the shared path was the defect.
_LIVE_WORKTREES: set[str] = set()

#: uv honours BOTH of these, so pinning one and leaving the other is a way a
#: redirect leaks back to the shared venv. **pip honours NEITHER**: it installs
#: into the interpreter it is running under, and a bare ``pip`` is chosen by
#: PATH. Pinning these two is therefore only half the job, and
#: `_prepend_venv_bin` is the other half. Measured on review of PR #145 with
#: two real venvs: with both variables pinned to the worktree but PATH still
#: leading with the shared venv's bin, ``pip install -e .`` rewrote the SHARED
#: venv's .pth, pin and all.
_VENV_KEYS = ("VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT")
#: Where an interpreter and its console scripts live inside a venv.
_VENV_BIN = "Scripts" if os.name == "nt" else "bin"
_PY_PROJECT_MARKERS = ("pyproject.toml", "setup.py", "setup.cfg")


def is_agent_worktree(
    path, config, *, live: set[str] | None = None,
) -> bool:
    """Fail-closed containment predicate: True only for a directory this
    process's own worktree machinery could have produced — never a human's
    primary checkout, and never anything outside the configured worktree
    root. `reset_agent_workspace` is destructive (hard reset + clean), so
    this is the guard that stands between it and an operator's live tree.

    All of the following must hold, else False:
      1. ``path``'s PARENT is exactly the configured ``worktree_root`` —
         nothing outside the root is ever in scope, however it is shaped.
      2. ``path``'s directory NAME parses under ``config.worktree_owner``
         into the ``<task_id>.<owner_pid>.<token>`` shape this process
         mints for a per-run worktree, OR ``path`` is registered in
         ``live`` (this process is running a task in it right now — covers
         legacy/bare-task-id directories that still predate the shaped
         name).
      3. ``path/.git`` is a FILE, not a directory — the marker of a linked
         worktree. A primary checkout's ``.git`` is a directory; treating
         one as agent-owned would let this reset a human's tree.

    Any exception (unreadable path, a symlink that does not resolve, a
    non-existent directory) reads as False — "cannot prove it is ours"
    is not "safe to reset"."""
    from ..config import worktree_owner, worktree_root

    if live is None:
        live = _LIVE_WORKTREES
    try:
        p = Path(path).resolve()
        root = worktree_root(config).resolve()
        if p.parent != root:
            return False
        _, owner_pid = worktree_owner(p.name)
        shaped = len(p.name.split(".")) >= 3 and owner_pid is not None
        if not (shaped or str(path) in live or str(p) in live):
            return False
        return (p / ".git").is_file()
    except Exception:  # noqa: BLE001 — unreadable/unresolvable reads as "not ours"
        return False


def reset_agent_workspace(
    repo, config, *, task_id: str, live: set[str] | None = None,
) -> list[str] | None:
    """Hard-reset + clean an agent-owned worktree immediately before a
    branch decision (`checkout` / `checkout -B`), so a previous attempt's
    uncommitted leftovers can never crash the next one's startup.

    Returns the discarded paths (``[]`` on an already-clean tree), or
    ``None`` when the guard declined — never raises, so a reset failure
    (or a workspace this process does not own) can never fail a task; the
    caller's own branch operation remains the loud symptom if something is
    still wrong afterward."""
    if not is_agent_worktree(repo.path, config, live=live):
        log.info(
            "workspace reset SKIPPED for task %s — %s is not an "
            "agent-owned worktree", task_id[:8], repo.path,
        )
        return None
    try:
        discarded = repo.reset_workspace()
    except Exception as exc:  # noqa: BLE001 — reset must never fail a task
        log.error(
            "workspace reset FAILED for task %s at %s: %s",
            task_id[:8], repo.path, exc,
        )
        return None
    if discarded:
        log.warning(
            "workspace reset discarded %d uncommitted leftover(s) for task "
            "%s before branching (previous attempt's leftovers): %s",
            len(discarded), task_id[:8], ", ".join(discarded[:5]),
        )
    return discarded


def teardown_worktree(
    main_repo, wt_path: Path, *, task_id: str, live: set[str] | None = None,
) -> bool:
    """Remove one task's worktree directory. Never raises.

    ``main_repo`` may be ``None`` — the bench-sandbox case, where no repo is
    left to hold a registration for the directory at all; only the
    filesystem side is left to clean up.

    Returns ``True`` when the directory is confirmed gone, ``False`` when it
    survived both the git removal and the rmtree fallback — a caller may
    ignore the return value (teardown must never be allowed to fail a task),
    but the failure is always logged at ERROR, never silently dropped."""
    if live is None:
        live = _LIVE_WORKTREES
    wt_path = Path(wt_path)
    try:
        # Kill any test subprocess still running in this worktree BEFORE
        # removing it — rmtree'ing a `.venv` out from under a live pytest
        # subprocess is the xdist INTERNALERROR this order avoids. A kill
        # failure must not stop the removal.
        try:
            killed = runner.terminate_running(wt_path)
            if killed:
                log.warning(
                    "killed %d orphaned test proc(s) in %s before teardown",
                    killed, task_id[:8],
                )
        except Exception:  # noqa: BLE001 — teardown must never crash
            pass

        if main_repo is not None:
            try:
                main_repo.remove_worktree(wt_path)
            except Exception as exc:  # noqa: BLE001 — post-condition catches this
                log.warning(
                    "git worktree remove failed for %s: %s", task_id[:8], exc)

        # Post-condition check — this is the fix. `remove_worktree` runs with
        # `check=False`, so a git failure above was silent; only checking
        # whether the directory is actually gone catches it.
        if wt_path.exists():
            try:
                shutil.rmtree(wt_path, ignore_errors=False)
            except Exception:  # noqa: BLE001
                try:
                    shutil.rmtree(wt_path, ignore_errors=True)
                except Exception:  # noqa: BLE001
                    pass

        if wt_path.exists():
            log.error(
                "worktree teardown FAILED for task %s: %s still on disk "
                "after git worktree remove + rmtree — reclaim by hand: "
                "git worktree remove --force %s", task_id[:8], wt_path, wt_path,
            )
            return False
        log.info("worktree teardown OK for task %s: %s", task_id[:8], wt_path)
        return True
    finally:
        live.discard(str(wt_path))


def _default_open_repo(task, config):
    """``GitRepo(task.repo_path)`` guarded against every way opening it can
    fail — an unreadable path, a repo that no longer exists on disk. The
    janitor must never let a bad repo path turn into an unhandled exception
    that aborts the whole sweep."""
    from ..vcs import GitRepo

    try:
        return GitRepo(
            Path(task.repo_path),
            identity_name=config["git"]["agent_identity_name"],
            identity_email=config["git"]["agent_identity_email"],
            never_push_to=config["git"]["never_push_to"],
        )
    except Exception:  # noqa: BLE001
        return None


def _read_gitdir_target(entry: Path) -> Path | None:
    """Parse the ``gitdir: <path>`` line out of a linked worktree's ``.git``
    file. Returns ``None`` when it is missing, unreadable, or unparseable —
    "cannot tell" must never be treated as "provably dead"."""
    try:
        text = (entry / ".git").read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("gitdir:"):
            target = line.split(":", 1)[1].strip()
            return Path(target) if target else None
    return None


def _is_linked_worktree(path: Path) -> bool:
    """True when ``path`` is a LINKED worktree, whose ``.git`` is a file
    carrying a ``gitdir:`` line, and not a primary checkout, whose ``.git`` is
    a directory. The distinction is the whole safety boundary here: a primary
    checkout is the operator's own tree and its venv is theirs, so nothing
    below may repoint it. ``isolation.enabled: false`` runs an attempt
    straight in the primary checkout, and this returns False there."""
    return (path / ".git").is_file() and _read_gitdir_target(path) is not None


def _inherited_venv(env: Mapping[str, str]) -> Path | None:
    """The venv an inherited environment already names, if any."""
    for key in _VENV_KEYS:
        value = env.get(key)
        if value:
            return Path(value)
    return None


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
    except (ValueError, OSError):
        return False
    return True


def isolate_attempt_env(
    repo_path, env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """The environment for a subprocess this process runs inside an attempt.

    PART of #128, not all of it: the incident that raised the issue came from
    the CODER session (``uv run --active`` inside the worktree), which this
    does not touch. 0 of 939 tasks have ever configured ``env_setup``, so what
    this establishes is the invariant for the commands the ORCHESTRATOR itself
    runs, not a fix for the observed event. The coder half belongs to
    ``agent/venv_install_guard``.

    An attempt's ``env_setup`` used to run with ``cwd`` set to the worktree and
    no ``env`` at all, so it inherited this process's ``VIRTUAL_ENV`` — which
    names the SHARED checkout's ``.venv``. An editable install from inside the
    worktree therefore rewrote that shared venv's ``.pth`` to a path under the
    worktree, and when the worktree was later torn down every fresh ``nh``
    process died with ``ModuleNotFoundError`` while the already-running server,
    holding its modules in memory, kept working and hid it (issue #128).

    ``venv_install_guard`` names this same residual in its own module
    docstring, and says closing it needs the environment scoped BEFORE the
    command runs rather than a smarter command pattern. This is that scoping,
    for the commands the ORCHESTRATOR itself runs.

    The pin applies only when all of these hold, so that ordinary runs are
    untouched:

    * ``repo_path`` is a linked worktree. With ``isolation.enabled: false``
      there is no worktree and the primary checkout is the operator's own.
    * the inherited environment actually names a venv. Nothing to displace
      otherwise.
    * that venv lies OUTSIDE the worktree. A venv already inside it is the
      isolation this function exists to create.

    When it does apply, both ``VIRTUAL_ENV`` and ``UV_PROJECT_ENVIRONMENT`` are
    pointed at ``<worktree>/.venv``, and that venv is created if the worktree
    looks like a Python project. It has to be created rather than merely
    named: ``uv pip install`` does not build a missing environment, it fails
    with "Python interpreter not found" (measured), and a pin with no venv
    behind it would turn a corrupting install into a failing one.

    Creation failure is NOT a reason to fall back to the shared venv. Falling
    back is precisely the bug. The pin stands, the command fails loudly, and
    the operator's venv survives.
    """
    root = Path(repo_path)
    result = dict(os.environ if env is None else env)
    if not _is_linked_worktree(root):
        return result
    current = _inherited_venv(result)
    if current is None or _is_within(current, root):
        return result

    target = root / ".venv"
    if not target.exists() and any(
            (root / name).is_file() for name in _PY_PROJECT_MARKERS):
        _create_venv(target, current)
    for key in _VENV_KEYS:
        result[key] = str(target)
    _prepend_venv_bin(result, target)
    return result


def _prepend_venv_bin(env: dict[str, str], venv: Path) -> None:
    """Put ``venv``'s bin directory at the FRONT of ``PATH``, in place.

    The two variables above route uv. They do NOT route pip, which installs
    into whichever interpreter is running it, and a bare ``pip`` is resolved by
    PATH. ``runner._env_for`` prepends a worktree venv only when one ALREADY
    exists at the moment it is called, which on a fresh worktree it does not,
    so PATH still led with the shared venv the server was started from and
    ``pip install -e .`` rewrote THAT venv's ``.pth`` with the pin fully in
    place (measured on review of PR #145).

    Only prepended once the venv exists on disk: pointing PATH at a directory
    that was never built would shadow nothing and hide the real failure behind
    a confusing one.
    """
    bin_dir = venv / _VENV_BIN
    if not bin_dir.is_dir():
        return
    current = env.get("PATH", "")
    entry = str(bin_dir)
    if current.split(os.pathsep)[:1] == [entry]:
        return
    env["PATH"] = entry + (os.pathsep + current if current else "")


def _builder_python(displaced: Path | None) -> str | None:
    """The interpreter that can BUILD a venv, or ``None`` if none is usable.

    Normally ``sys.executable``. But in a PyInstaller build ``sys.executable``
    is the frozen ``nh`` binary, not a Python: ``nh -m venv <path>`` re-enters
    the click CLI and exits 2 without creating anything (measured on review of
    PR #145). ``testing/repro_gate.py::_pytest_python`` already carries this
    scar for pytest, where the same mistake produced a confident but FALSE
    verdict; this is the same fallback for venv creation.

    The venv being DISPLACED is tried first: the caller only pins when the
    inherited environment already names one, so it exists by construction and
    is a real interpreter. Then ``python3``/``python`` on PATH.
    """
    if not getattr(sys, "frozen", False):
        return sys.executable
    if displaced is not None:
        for sub, name in ((("Scripts",), "python.exe"), (("bin",), "python")):
            candidate = displaced.joinpath(*sub, name)
            if candidate.is_file():
                return str(candidate)
    for name in ("python3", "python"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _create_venv(target: Path, displaced: Path | None = None) -> None:
    """Build the worktree's own venv with the standard library's ``venv``
    module, and deliberately not with ``uv venv``.

    ``uv venv`` may FETCH an interpreter, which would put a network call
    inside an attempt's environment setup and would need a line in
    ``tests/test_egress_allowlist.py`` declaring it. ``sys.executable -m
    venv`` provably cannot leave the machine, so there is no egress here to
    declare. The venv it writes is an ordinary one and ``uv pip install``
    installs into it happily.

    Best effort and never raises: if it cannot be built the caller still pins
    to it, so the attempt's own command fails instead of the operator's venv
    being rewritten."""
    python = _builder_python(displaced)
    if python is None:
        log.warning(
            "no usable interpreter to build a venv at %s (frozen build with "
            "no python on PATH); the attempt's environment commands will fail "
            "rather than write to the shared venv", target)
        return
    try:
        proc = subprocess.run([python, "-m", "venv", str(target)],
                              capture_output=True, text=True, timeout=120)
        failed = getattr(proc, "returncode", 1) != 0
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("worktree venv: %s -m venv failed: %s", python, exc)
        failed = True
    if failed:
        log.warning(
            "could not create a venv at %s; the attempt's environment "
            "commands will fail rather than write to the shared venv", target)


async def sweep_stale_worktrees(
    store, config, *, open_repo=None, live: set[str] | None = None,
) -> tuple[int, int]:
    """Startup-only janitor. Reclaims worktree directories left by tasks that
    have already reached a terminal status and will never run again — the
    class ``_reap_dead_worktrees`` structurally cannot reach, since it only
    ever runs scoped to the one task about to start.

    Returns ``(removed, skipped)``. Every skip is logged — an operator who
    disagrees can see exactly what the janitor declined and why."""
    from ..config import pid_alive, worktree_owner, worktree_root

    if live is None:
        live = _LIVE_WORKTREES
    root = worktree_root(config)
    if not root.is_dir():
        log.debug("worktree sweep: root %s does not exist, nothing to reclaim",
                  root)
        return (0, 0)

    resolve_repo = open_repo if open_repo is not None else (
        lambda task: _default_open_repo(task, config))

    removed = 0
    skipped = 0
    for entry in sorted(root.iterdir()):
        try:
            if not entry.is_dir():
                continue
            if str(entry) in live:
                continue  # a run in THIS process owns it right now

            task_id, owner_pid = worktree_owner(entry.name)
            if owner_pid is not None and pid_alive(owner_pid):
                log.info(
                    "worktree sweep: skipping %s — owner pid %d is alive",
                    entry, owner_pid)
                skipped += 1
                continue

            task = await store.get_task(task_id)
            if task is not None:
                if task.status not in TERMINAL_STATES:
                    log.info(
                        "worktree sweep: skipping %s — task %s is %s, not "
                        "terminal", entry, task_id[:8], task.status.value)
                    skipped += 1
                    continue
                try:
                    repo = resolve_repo(task)
                except Exception:  # noqa: BLE001
                    repo = None
                if teardown_worktree(repo, entry, task_id=task.id, live=live):
                    removed += 1
                else:
                    skipped += 1
                continue

            # Unknown task id: this store never heard of it (a different
            # install's leftover, or — the 88-directory class measured
            # 2026-08-12 — a deleted bench sandbox). Never age/mtime: the one
            # provable fact available is whether the worktree's own git admin
            # directory still exists. If it does, some repo somewhere might
            # still hold the registration; if it does not, no repo anywhere
            # can be using this worktree and there is no registration left to
            # prune, so the directory is provably reclaimable.
            gitdir_target = _read_gitdir_target(entry)
            if gitdir_target is None:
                log.warning(
                    "worktree sweep: skipping %s — unknown task id and no "
                    "readable .git gitdir; cannot prove it is dead", entry)
                skipped += 1
                continue
            if gitdir_target.exists():
                log.warning(
                    "worktree sweep: skipping %s — unknown task id but its "
                    "git admin dir %s still exists; leaving it alone",
                    entry, gitdir_target)
                skipped += 1
                continue
            if teardown_worktree(None, entry, task_id=task_id, live=live):
                removed += 1
            else:
                skipped += 1
        except Exception as exc:  # noqa: BLE001 — one bad entry must not
            # abort the whole sweep.
            log.warning("worktree sweep: error inspecting %s: %s", entry, exc)
            skipped += 1
    return (removed, skipped)


def _open_worktree_repo(entry: Path, config):
    """``GitRepo(entry)`` for a worktree directory itself (not the main
    repo `_default_open_repo` opens) — guarded the same way: any failure to
    open reads as "cannot salvage", never as an exception that aborts the
    whole pass."""
    from ..vcs import GitRepo

    try:
        return GitRepo(
            entry,
            identity_name=config["git"]["agent_identity_name"],
            identity_email=config["git"]["agent_identity_email"],
            never_push_to=config["git"]["never_push_to"],
        )
    except Exception:  # noqa: BLE001
        return None


async def _salvage_one(entry: Path, task, attempt, config, store) -> bool:
    """Commit + stamp one provably-dead IMPLEMENTING worktree. Returns
    ``True`` on a real salvage, ``False`` on any skip (already logged with
    its reason). Never raises — the caller wraps this in its own
    try/except, but every internal failure here is also caught so the log
    line names the real reason rather than a generic "error inspecting"."""
    from ..blockers import human_gate_armed, resume_provenance
    from ..vcs.git import ProtectedBranch
    from ..vcs.manifest_repair import commit_with_manifest_repair

    repo = _open_worktree_repo(entry, config)
    if repo is None:
        log.warning("worktree salvage: skipping %s — could not open as a "
                    "git repo", entry)
        return False

    try:
        if not repo.has_changes():
            log.debug("worktree salvage: skipping %s — clean, nothing to "
                      "salvage", entry)
            return False
    except Exception as exc:  # noqa: BLE001
        log.warning("worktree salvage: skipping %s — could not read status: "
                    "%s", entry, exc)
        return False

    ctx = task.context or {}
    # Identical to `Orchestrator._honor_server_stop` /
    # `Scheduler._inherited_checkpoint`: a human's gate is executed, never
    # decided over — but only while still ARMED. A consumed gate
    # (`Orchestrator._consume_human_gate`) is exactly the ordinary machine
    # salvage this skip was always meant to allow again.
    if human_gate_armed(ctx):
        log.info("worktree salvage: skipping %s — resume_from is human-gated, "
                 "not overwriting it", entry)
        return False

    branch = (attempt.get("branch_name") or "").strip()
    if not branch:
        log.warning("worktree salvage: skipping %s — attempt has no "
                    "recorded branch_name", entry)
        return False

    try:
        cur = repo.current_branch()
    except Exception as exc:  # noqa: BLE001
        log.warning("worktree salvage: skipping %s — could not read current "
                    "branch: %s", entry, exc)
        return False
    if cur != branch:
        try:
            repo.checkout(branch)
        except Exception as exc:  # noqa: BLE001
            log.warning("worktree salvage: skipping %s — could not check "
                        "out %s: %s", entry, branch, exc)
            return False

    # Routed through the same manifest-repair seam as every other checkpoint
    # commit (`Orchestrator._checkpoint_commit`): the pre-commit manifest
    # gate is live for this worktree too (`push_hook.py:72` mirrors it via
    # the per-worktree `hooksPath`), so a stale pin at the moment of a hard
    # kill must be repaired-and-retried here as well, not just on the
    # graceful-stop and attempt-timeout paths.
    repaired: list[tuple[list[str], str]] = []
    try:
        try:
            commit = await asyncio.to_thread(
                commit_with_manifest_repair, repo, None,
                f"[WIP-PARTIAL] salvaged from a killed run: {task.title}",
                on_repair=lambda p, note: repaired.append((p, note)),
            )
        except ProtectedBranch as exc:
            log.warning("worktree salvage: skipping %s — refusing to commit "
                        "on protected branch %s: %s", entry, branch, exc)
            return False
        except Exception as exc:  # noqa: BLE001
            log.warning("worktree salvage: skipping %s — commit failed: %s",
                        entry, exc)
            return False
    finally:
        # A ledger mutation is NEVER absent from the record, even when the
        # retry commit itself still failed (`export_guard.py approve`
        # rewrites RELEASE_MANIFEST.txt in the working tree BEFORE the
        # retry) — same rule as `Orchestrator._emit_manifest_repaired`.
        if repaired:
            rep_paths, rep_note = repaired[0]
            log.warning(
                "worktree salvage: %s re-approved %d pinned file(s) to "
                "salvage this worktree: %s — %s",
                entry, len(rep_paths), ", ".join(rep_paths)[:300],
                rep_note[:200],
            )

    await store.merge_context(task.id, {
        "resume_from": resume_provenance(
            {"sha": commit.sha, "branch": branch}, "hard_kill_salvage"),
        "handoff": {
            "wip_sha": commit.sha,
            "commit": commit.sha,
            "stopped_because": "killed mid-implementation (owner pid gone)",
            "turns_used": None,
            # RFC 7396: None deletes. An earlier `_persist_handoff(gate=...)`
            # record would otherwise survive the merge onto this salvaged
            # commit, and `build_resume_digest` would tell the next attempt a
            # gate rejected a commit no gate ever saw.
            "failed_gate": None,
            "failed_gate_summary": None,
            "own_partial": None,
        },
    })
    await store.update_attempt(
        attempt["id"], status="interrupted",
        failure_reason=(
            "interrupted: worker process died mid-implementation — "
            f"salvaged {commit.sha[:8]}"),
        infra_failure=1, commit_sha=commit.sha,
    )
    log.info(
        "worktree salvage: salvaged %d file(s) from dead worktree %s "
        "(task %s) as %s on %s",
        commit.files_changed, entry, task.id[:8], commit.sha[:8], branch,
    )
    return True


async def salvage_dead_worktrees(
    store, config, *, open_repo=None, live: set[str] | None = None,
) -> tuple[int, int]:
    """Startup-only. The hard-kill twin of `Orchestrator._honor_server_stop`:
    a task killed mid-IMPLEMENTING (SIGKILL, OOM, crash) keeps its dirty
    worktree through `sweep_stale_worktrees` (non-terminal tasks are always
    skipped there), and its own NEXT run reaps that worktree with no salvage
    commit (`Orchestrator._reap_dead_worktrees`) — this runs first, before
    that reap can happen, and turns the uncommitted diff into a
    ``[WIP-PARTIAL]`` commit with a machine-provenance ``resume_from`` stamp
    instead of silently discarding it.

    Deliberately separate from `sweep_stale_worktrees`: that janitor deletes
    terminal leftovers, this one commits non-terminal (IMPLEMENTING) ones and
    never deletes anything. Both are startup-only, both never raise.

    ``open_repo`` is accepted for signature parity with `sweep_stale_worktrees`
    but unused here — salvage always opens the WORKTREE itself (`entry`), not
    the main repo, via `_open_worktree_repo`.

    Returns ``(salvaged, skipped)``. Every skip is logged with its reason."""
    from ..config import pid_alive, worktree_owner, worktree_root

    if live is None:
        live = _LIVE_WORKTREES
    root = worktree_root(config)
    if not root.is_dir():
        log.debug("worktree salvage: root %s does not exist, nothing to "
                  "salvage", root)
        return (0, 0)

    salvaged = 0
    skipped = 0
    for entry in sorted(root.iterdir()):
        try:
            if not entry.is_dir():
                continue
            if str(entry) in live:
                continue  # a run in THIS process owns it right now

            task_id, owner_pid = worktree_owner(entry.name)
            if owner_pid is None:
                continue  # legacy bare-<task_id> shape: no salvage, uncounted
            if pid_alive(owner_pid):
                log.info(
                    "worktree salvage: skipping %s — owner pid %d is alive",
                    entry, owner_pid)
                skipped += 1
                continue

            task = await store.get_task(task_id)
            if task is None:
                continue  # unknown to this store: not ours to salvage
            if task.status is not TaskStatus.IMPLEMENTING:
                log.debug(
                    "worktree salvage: skipping %s — task %s is %s, not "
                    "implementing", entry, task_id[:8], task.status.value)
                skipped += 1
                continue

            attempt = await store.latest_open_attempt(task.id)
            if attempt is None:
                log.debug(
                    "worktree salvage: skipping %s — no open attempt, "
                    "nothing died", entry)
                skipped += 1
                continue

            if not is_agent_worktree(entry, config, live=live):
                log.warning(
                    "worktree salvage: skipping %s — not a recognizable "
                    "agent worktree", entry)
                skipped += 1
                continue

            if await _salvage_one(entry, task, attempt, config, store):
                salvaged += 1
            else:
                skipped += 1
        except Exception as exc:  # noqa: BLE001 — one bad entry must not
            # abort the whole pass.
            log.warning("worktree salvage: error inspecting %s: %s", entry, exc)
            skipped += 1
    return (salvaged, skipped)
