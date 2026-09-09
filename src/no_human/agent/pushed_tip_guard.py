"""Refuse to rewrite a branch that has already been pushed.

A coder that hits a base-refresh merge conflict sometimes reaches for
``git rebase`` (or a ``git reset --hard`` below the pushed tip) instead of
resolving the conflict with a merge. If the branch has already been pushed,
that rewrites history the remote already has: the branch's pushed tip stops
being an ancestor of HEAD, and delivery — which only ever fast-forwards a
branch's remote ref, never force-pushes — refuses it outright with "remote
tip <sha> ... is not an ancestor of the reviewed sha". The task then
escalates instead of shipping. This module is the policy that catches that
*before* the coder runs the command, and tells them to merge instead.

Detection is local-only: it reads the current branch's remote-tracking ref
(``refs/remotes/<remote>/<branch>``), never ``ls-remote`` or any other
network round-trip. ``GitRepo.fetch_remote_branch_sha`` (base-refresh) can
afford a network call because it runs once per attempt; this module runs on
*every* Bash tool call in the PreToolUse hook, so a network round-trip here
would add real latency to every command a coder runs. A branch the harness
actually pushed always has a live tracking ref locally, so this is not a
loss of coverage — just a difference in how "pushed" is confirmed.

Failure policy is **fail OPEN**, deliberately inverted from most of this
guard package's conservative-deny convention: a missing cwd, a non-git
directory, a detached HEAD that isn't mid-rebase, no remotes, no tracking
ref, a failed git invocation, or a timeout — every one of these returns
``None`` (no denial), never a raise. A detached HEAD *during* a rebase is
the one case resolved rather than treated as failure: ``git rebase
--continue`` only ever runs while a rebase is in progress, and git detaches
HEAD for that whole duration (see ``_rebase_head_name``), so failing open
there would silently defeat the ``--continue`` denial in the exact
situation it exists to catch. Two things justify the rest staying open:
this rule is an *addition*
stacked on top of ``guard._git_worktree_denial``, which already
independently blocks every tree-clobbering git form regardless of what this
module decides; and a false positive here would break the legitimate
rebase-on-a-never-pushed-branch workflow that ``tests/test_guard.py``'s
``_SEQUENCER_PAIRS`` pins as allowed. Getting this wrong in the deny
direction breaks real work; getting it wrong in the allow direction just
means the pre-existing, less specific denial (or no denial, if the branch
truly was never pushed) applies instead.
"""

from __future__ import annotations

import os
import subprocess

_ABORT_LIKE = ("--abort", "--skip", "--autostash")

_RESET_CLOBBER_FLAGS = ("--hard", "--merge", "--keep")

# A local copy of guard.py's global-option skip list, kept in sync by hand:
# this module must not import guard.py (that would be a needless coupling
# and, worse, a potential import cycle since guard.py imports this module).
_GIT_GLOBAL_OPT_WITH_ARG = frozenset({
    "-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path",
    "--super-prefix", "--config-env", "--attr-source",
})


def _git(cwd: str, *args: str, timeout: float = 5) -> str | None:
    """Run `git -C <cwd> *args`, returning stripped stdout on success or
    None on any failure (non-zero exit, missing git, timeout, not a repo).
    Never raises — this runs inside a PreToolUse hook."""
    try:
        proc = subprocess.run(
            ["git", "-C", cwd, *args],
            capture_output=True, text=True, timeout=timeout,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def _git_ok(cwd: str, *args: str, timeout: float = 5) -> bool:
    """Like _git but for exit-code-only checks (e.g. `merge-base
    --is-ancestor`), which print nothing on success."""
    try:
        proc = subprocess.run(
            ["git", "-C", cwd, *args],
            capture_output=True, text=True, timeout=timeout,
        )
    except Exception:
        return False
    return proc.returncode == 0


def _rebase_head_name(cwd: str) -> str | None:
    """`git rebase --continue` only ever runs while a rebase is IN
    PROGRESS, and git detaches HEAD for the duration of a rebase (both the
    apply and merge backends) — so `symbolic-ref HEAD` can't name the
    branch there. Both backends record the branch being rebased in
    `<git-dir>/rebase-merge/head-name` or `<git-dir>/rebase-apply/head-name`
    as `refs/heads/<branch>` for exactly this duration. Reads the file
    directly (not a git subcommand) since `git symbolic-ref` refuses a
    detached HEAD outright; returns None outside a rebase or on any
    filesystem hiccup, never raises."""
    git_dir = _git(cwd, "rev-parse", "--git-dir")
    if not git_dir:
        return None
    git_dir = git_dir if os.path.isabs(git_dir) else os.path.join(cwd, git_dir)
    for state_dir in ("rebase-merge", "rebase-apply"):
        try:
            with open(os.path.join(git_dir, state_dir, "head-name"), "r") as f:
                ref = f.read().strip()
        except OSError:
            continue
        if ref.startswith("refs/heads/"):
            return ref[len("refs/heads/"):]
    return None


def _current_branch(cwd: str) -> str | None:
    return (
        _git(cwd, "symbolic-ref", "--quiet", "--short", "HEAD")
        or _rebase_head_name(cwd)
        or None
    )


def _remotes(cwd: str) -> list[str]:
    out = _git(cwd, "remote")
    if not out:
        return []
    names = [n for n in out.splitlines() if n.strip()]
    names.sort(key=lambda n: (n != "origin", n))
    return names


def _pushed_tip(cwd: str | None) -> tuple[str, str, str] | None:
    """Returns (remote, branch, tip_sha) for the current branch's pushed
    remote tip, or None if the branch was never pushed (or cwd/HEAD/remote
    state can't establish that with a purely local check)."""
    if not cwd:
        return None
    branch = _current_branch(cwd)
    if not branch:
        return None
    for remote in _remotes(cwd):
        tip = _git(
            cwd, "rev-parse", "--verify", "--quiet",
            f"refs/remotes/{remote}/{branch}^{{commit}}",
        )
        if tip:
            return remote, branch, tip
    return None


def _subcommand(argv: list[str]) -> tuple[str, list[str]]:
    """Local copy of guard._git_subcommand: skip global options to find the
    git subcommand and its remaining args."""
    i = 1
    while i < len(argv):
        tok = argv[i]
        if not tok.startswith("-"):
            return tok, argv[i + 1:]
        if tok in _GIT_GLOBAL_OPT_WITH_ARG:
            i += 2
            continue
        i += 1
    return "", []


def _is_sequencer_wind_back(rest: list[str]) -> bool:
    """True when `rest` carries --abort/--skip/--autostash (any unambiguous
    prefix). These forms are already denied by guard._sequencer_clobbers
    with a more specific working-tree message, so this module skips them
    rather than double-denying with a less relevant one."""
    for tok in rest:
        word = tok.split("=", 1)[0]
        if word.startswith("--") and len(word) >= 3 and any(
            full.startswith(word) for full in _ABORT_LIKE
        ):
            return True
    return False


def _is_strictly_below_tip(cwd: str, target: str, tip: str) -> bool:
    resolved = _git(cwd, "rev-parse", "--verify", "--quiet", f"{target}^{{commit}}")
    if not resolved or resolved == tip:
        return False
    return _git_ok(cwd, "merge-base", "--is-ancestor", resolved, tip)


def _message(seg: str, remote: str, branch: str, tip: str) -> str:
    return (
        f"rewriting a pushed branch is blocked: {seg}. {remote}/{branch} is "
        f"already pushed at {tip} — a rebase (or a hard reset below that "
        "tip) rewrites commits the remote already has, and delivery is "
        "fast-forward-only against it: it will refuse the branch with "
        f'"remote tip {tip} ... is not an ancestor of the reviewed sha" and '
        "the task escalates. Bring the base in with a MERGE instead: "
        "`git merge <base sha>`, resolve the conflicts, `git commit`. A "
        "branch that was never pushed is unaffected by this rule."
    )


def denial_reason(
    invocations: list[tuple[str, list[str]]], cwd: str | None,
) -> str | None:
    """Given the parsed git invocations found in a proposed Bash command
    (as returned by guard._git_invocations) and the session cwd, return a
    denial reason if any of them would rewrite the current branch below its
    already-pushed remote tip — else None (see module docstring: this fails
    open on any uncertainty)."""
    if not invocations:
        return None
    pushed = _pushed_tip(cwd)
    if not pushed:
        return None
    remote, branch, tip = pushed
    for seg, argv in invocations:
        sub, rest = _subcommand(argv)
        if sub == "rebase":
            if _is_sequencer_wind_back(rest):
                continue
            return _message(seg, remote, branch, tip)
        if sub == "reset":
            if not any(flag in rest for flag in _RESET_CLOBBER_FLAGS):
                continue
            operands = [tok for tok in rest if not tok.startswith("-")]
            if not operands:
                continue
            if _is_strictly_below_tip(cwd, operands[0], tip):
                return _message(seg, remote, branch, tip)
    return None
