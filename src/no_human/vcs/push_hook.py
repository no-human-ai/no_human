"""The protected-branch rule's SECOND enforcement point: a git ``pre-push``
hook installed into every agent worktree.

Why a second one exists
-----------------------
The first is :func:`no_human.agent.guard._push_targets_protected`, a PreToolUse
check that tokenizes the command line the agent *proposed* and looks for a
protected branch name. It is lexical, and lexical analysis cannot resolve shell
expansion: ``git push origin $(echo main)``, ``B=main; git push origin $B`` and
``git push origin `echo main` `` all reach the same ref while carrying no token
that reads as ``main``. The coder runs with ``permission_mode="bypassPermissions"``
(``agent/claude_backend.py``), so on the Bash path that guard is the only gate —
the product's own :meth:`GitRepo.push` raises ``ProtectedBranch``, but an agent
shelling out to ``git push`` never reaches that code.

A ``pre-push`` hook is *below* the expansion. Git hands it
``<local ref> <local sha> <remote ref> <remote sha>`` on stdin after it has
already resolved the refspec, so the hook sees ``refs/heads/main`` no matter how
the command line spelled it. No amount of quoting, indirection or command
substitution changes what git resolved.

Where it is installed, and why *there*
--------------------------------------
A linked worktree does NOT get its own hooks directory. Measured on git 2.49:
a hook dropped in the per-worktree admin dir (``.git/worktrees/<id>/hooks/``)
never fires; the one that fires is the shared ``$GIT_COMMON_DIR/hooks`` — i.e.
the *user's primary checkout's* ``.git/hooks``. Writing there is not an option:
it would clobber whatever hook the user already has and would block the human's
own pushes to main.

So the hook lives in a private directory inside the worktree's admin dir and is
selected with a **per-worktree** ``core.hooksPath``. Plain ``git config
core.hooksPath`` from inside a linked worktree writes to the *shared* config
(measured), which would leak to the primary checkout — so the setting goes in
``config.worktree`` via ``git config --worktree``, gated on
``extensions.worktreeConfig``. The only shared-config write is that one
additive ``extensions.worktreeConfig = true`` key, which changes no existing
value and leaves the primary checkout's hooks untouched.

The hook directory sits in the admin dir rather than the working tree so that a
``git clean``, a branch switch, or the agent deleting files does not remove it,
and so that ``git worktree remove`` disposes of it with everything else.

Fail closed
-----------
The hook reads the protected-branch patterns from a file written beside it at
install time from the *same* ``git.never_push_to`` config the guard is given —
the list is not restated anywhere. If that file is missing or unreadable the
hook refuses the push rather than allowing it. If the installer cannot make the
per-worktree ``core.hooksPath`` take effect it raises, so a worktree is never
handed to an agent with the guard silently absent.
"""

from __future__ import annotations

import os
import shlex
import stat
import subprocess
from pathlib import Path


#: Name of the private hooks directory created inside the worktree admin dir.
HOOK_DIR_NAME = "no_human-hooks"

#: Name of the generated patterns file read by the hook.
PATTERNS_FILENAME = "never_push_to"

#: Hooks that get a pass-through shim so overriding ``core.hooksPath`` does not
#: silently disable a repository's existing hooks inside agent worktrees.
_MIRRORED_HOOKS = (
    "pre-commit", "prepare-commit-msg", "commit-msg", "post-commit",
    "post-checkout", "post-merge", "pre-rebase", "post-rewrite",
    "pre-applypatch", "post-applypatch", "applypatch-msg", "pre-merge-commit",
)

_EXEC_MODE = (
    stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH
)


class PushHookError(RuntimeError):
    """The pre-push guard could not be installed. Never swallowed: a worktree
    without the guard is a worktree where the string matcher is the only gate."""


# The hook is plain POSIX sh on purpose. It must run under whatever git invokes
# it with, in a worktree whose interpreter/venv the agent may well have broken,
# so it depends on nothing but the shell. Pattern matching uses `case`, whose
# globbing is the same shape as the `fnmatch` the guard uses.
_HOOK_TEMPLATE = """#!/bin/sh
# no_human protected-branch pre-push guard -- GENERATED, do not edit.
#
# Reads the refspec git ALREADY RESOLVED (stdin:
# "<local ref> <local sha> <remote ref> <remote sha>"), so it is unaffected by
# how the command line spelled the branch -- $(...), backticks, and variables
# have all been expanded by the time this runs.
#
# Fails closed: if the pattern list cannot be read, the push is refused.

GUARD_DIR={guard_dir}
PATTERNS="$GUARD_DIR/{patterns_name}"
DELEGATE={delegate}

if [ ! -r "$PATTERNS" ]; then
    echo "no_human pre-push guard: cannot read the protected-branch list at" >&2
    echo "  $PATTERNS" >&2
    echo "Refusing the push: the guard cannot tell whether this ref is" >&2
    echo "protected, and it fails closed. Restore the file or recreate the" >&2
    echo "worktree." >&2
    exit 1
fi

# Buffer stdin so the ref list can be examined and still handed to a delegate.
STDIN_COPY=$(mktemp 2>/dev/null) || {{
    echo "no_human pre-push guard: mktemp failed; refusing the push." >&2
    exit 1
}}
trap 'rm -f "$STDIN_COPY"' EXIT INT TERM
cat > "$STDIN_COPY"

blocked=0
while read -r local_ref local_sha remote_ref remote_sha
do
    [ -n "$remote_ref" ] || continue
    # Same normalisation as guard._branch_protected.
    branch=${{remote_ref#refs/heads/}}
    branch=${{branch#origin/}}
    while IFS= read -r pattern || [ -n "$pattern" ]
    do
        case "$pattern" in
            ''|'#'*) continue ;;
        esac
        # $pattern is deliberately unquoted: it is a glob, not a literal.
        case "$branch" in
            $pattern)
                echo "no_human pre-push guard: REFUSED." >&2
                echo "  resolved remote ref : $remote_ref" >&2
                echo "  matches protected   : $pattern" >&2
                blocked=1
                ;;
        esac
    done < "$PATTERNS"
done < "$STDIN_COPY"

if [ "$blocked" -ne 0 ]; then
    echo "" >&2
    echo "The agent never pushes to a protected branch and never merges." >&2
    echo "Push your own task branch and open a PR; a human merges it." >&2
    echo "This check reads the ref git resolved, so rewriting the command" >&2
    echo "line will not get past it." >&2
    exit 1
fi

if [ -n "$DELEGATE" ] && [ -x "$DELEGATE" ]; then
    "$DELEGATE" "$@" < "$STDIN_COPY"
    exit $?
fi
exit 0
"""

_SHIM_TEMPLATE = """#!/bin/sh
# no_human: pass-through to the repository's own hook. GENERATED, do not edit.
exec {target} "$@"
"""


def _git(cwd: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True
    )
    if check and proc.returncode != 0:
        raise PushHookError(
            f"git {' '.join(args)} failed ({proc.returncode}): "
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )
    return proc.stdout.strip()


def _write_exec(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(_EXEC_MODE)


def hook_dir_for(worktree_path: Path | str) -> Path:
    """Absolute path of the private hooks directory for ``worktree_path``.

    Lives in the worktree's own git admin dir, which for a linked worktree is
    ``<common>/.git/worktrees/<id>`` — outside the working tree, so nothing the
    agent does to its checkout can reach it, and ``git worktree remove`` takes
    it away with the rest."""
    wt = Path(worktree_path).expanduser()
    admin = Path(_git(wt, "rev-parse", "--absolute-git-dir"))
    return admin / HOOK_DIR_NAME


def _write_patterns(hooks: Path, never_push_to: list[str]) -> None:
    """The one generated file the hook reads its branch list from. Shared by
    `install_pre_push_guard` (first install) and `refresh_protected_patterns`
    (later re-installs a per-task base into an already-installed guard) so
    there is exactly one place that decides the file's format."""
    patterns = hooks / PATTERNS_FILENAME
    patterns.write_text(
        "# no_human protected-branch patterns -- GENERATED from the same\n"
        "# git.never_push_to config the PreToolUse guard is given.\n"
        + "".join(f"{p}\n" for p in never_push_to),
        encoding="utf-8",
    )


def install_pre_push_guard(
    worktree_path: Path | str, never_push_to: list[str],
) -> Path:
    """Install the protected-branch ``pre-push`` hook into ``worktree_path``.

    ``never_push_to`` must be the very list the PreToolUse guard is given, so
    the two enforcement points cannot drift. Returns the hooks directory.
    Raises :class:`PushHookError` if the guard could not be made to take
    effect — the caller must not proceed without it.
    """
    wt = Path(worktree_path).expanduser()
    if not wt.exists():
        raise PushHookError(f"worktree does not exist: {wt}")

    # `core.worktree` in the SHARED config changes meaning once
    # extensions.worktreeConfig is on (it stops being main-worktree-only and
    # applies everywhere), which would point every linked worktree at the wrong
    # tree. Refuse rather than corrupt the repo or install nothing.
    if _git(wt, "config", "--local", "--get", "core.worktree", check=False):
        raise PushHookError(
            "core.worktree is set in the repository's shared config; enabling "
            "extensions.worktreeConfig would apply it to every linked "
            "worktree. Move it to the main worktree's config.worktree first. "
            "Refusing to create an agent worktree without the pre-push guard."
        )

    # The hooks directory that WAS in effect here, resolved before we override
    # it, so existing repository hooks keep working inside the agent worktree.
    try:
        prev_hooks = Path(_git(wt, "rev-parse", "--git-path", "hooks"))
        if not prev_hooks.is_absolute():
            prev_hooks = (wt / prev_hooks).resolve()
    except PushHookError:
        prev_hooks = None

    hooks = hook_dir_for(wt)
    hooks.mkdir(parents=True, exist_ok=True)

    _write_patterns(hooks, never_push_to)

    delegate = ""
    if prev_hooks is not None and prev_hooks != hooks:
        candidate = prev_hooks / "pre-push"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            delegate = str(candidate)
        for name in _MIRRORED_HOOKS:
            src = prev_hooks / name
            if src.is_file() and os.access(src, os.X_OK):
                _write_exec(
                    hooks / name,
                    _SHIM_TEMPLATE.format(target=shlex.quote(str(src))),
                )

    _write_exec(
        hooks / "pre-push",
        _HOOK_TEMPLATE.format(
            guard_dir=shlex.quote(str(hooks)),
            patterns_name=PATTERNS_FILENAME,
            delegate=shlex.quote(delegate),
        ),
    )

    # Per-worktree config. `git config core.hooksPath` without --worktree writes
    # to the SHARED config and would follow the user into their own checkout.
    if _git(wt, "config", "--local", "--get", "extensions.worktreeConfig",
            check=False).lower() != "true":
        _git(wt, "config", "extensions.worktreeConfig", "true")
    _git(wt, "config", "--worktree", "core.hooksPath", str(hooks))

    effective = _git(wt, "config", "--get", "core.hooksPath", check=False)
    if Path(effective or "") != hooks:
        raise PushHookError(
            f"pre-push guard did not take effect in {wt}: core.hooksPath "
            f"resolved to {effective!r}, expected {str(hooks)!r}"
        )
    return hooks


def refresh_protected_patterns(
    worktree_path: Path | str, never_push_to: list[str],
) -> bool:
    """Rewrite the pattern file of an ALREADY-INSTALLED guard so a base
    branch discovered per-task (`Orchestrator._protect_base_branch`) reaches
    the hook, not just the PreToolUse lexical guard — closing a
    direct-push-to-base hole the lexical guard alone cannot: it only sees
    the agent's proposed argv, and `git config remote.origin.push
    HEAD:refs/heads/<base>` plus a bare `git push origin` leaves it no
    branch-name token to read at all.

    Refreshes ONLY when the guard is already installed and active at
    `worktree_path` — `hooks/pre-push` must exist AND the effective
    `core.hooksPath` must already resolve to that directory — else returns
    `False` and writes nothing. That is what keeps a primary checkout
    (`isolation.enabled: false`, `install_pre_push_guard` never ran there)
    from ever getting a patterns file, a hooksPath, or anything else
    written into it; `install_pre_push_guard` itself refuses to touch a repo
    where `core.worktree` would leak for the same reason, and this function
    must not become a second way to reach that same corruption.

    Purely additive/replacing of the one generated `never_push_to` file —
    same bytes for the same input, no git config writes, no hook-script
    rewrite, no delegate/shim re-discovery. The caller treats a `False`
    return, or an exception raised out of here, as advisory: a missed
    refresh must never be fatal to the attempt, only to the base's coverage
    by this specific enforcement point (the lexical guard still applies).
    """
    wt = Path(worktree_path).expanduser()
    hooks = hook_dir_for(wt)
    if not (hooks / "pre-push").exists():
        return False
    effective = _git(wt, "config", "--get", "core.hooksPath", check=False)
    if Path(effective or "") != hooks:
        return False
    _write_patterns(hooks, never_push_to)
    return True
