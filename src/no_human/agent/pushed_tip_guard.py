"""Refuse to rewrite a branch that has already been pushed.

A coder that hits a base-refresh merge conflict sometimes reaches for
``git rebase`` (or a ``git reset`` below the pushed tip, a ``commit
--amend`` of it, or a handful of other forms that move a branch pointer
backwards — ``pull --rebase``, ``checkout -B``/``switch -C``, ``branch
-f``, ``update-ref``, ``filter-branch``) instead of resolving the conflict
with a merge. If the branch has already been pushed, that rewrites history
the remote already has: the branch's pushed tip stops being an ancestor of
HEAD, and delivery's own branch push (``GitRepo.push_sha_fast_forward``) is
fast-forward-only and refuses it outright with "remote tip <sha> ... is not
an ancestor of the reviewed sha". The task then escalates instead of
shipping. This module is the policy that catches that *before* the coder
runs the command, and tells them to merge instead.

(``GitRepo.push``'s ``force_with_lease=True`` exists on two PR-open retry
paths in ``orchestrator.py``, and the two are NOT equally guarded. The
``_finalize`` retry (~:7654) is reached only after ``_assert_delivery_sha``
(~:7394) has already pinned the reviewed sha for THIS push — so that force
can only ever re-send the same pinned commit under lease, never resurrect a
branch this module refused to let get rewritten. The draft-PR retry in
``_open_draft_pr_for_review`` (~:13457) is different: it fires during a
``pr_conflict`` mechanical round, *before* any review verdict exists, so it
does not sit behind ``_assert_delivery_sha`` at all. It is still safe from
this module's perspective for an orthogonal reason, not that ordering: this
module only ever evaluates a git invocation proposed as a coder Bash
command, and once it denies the coder's own rebase/reset on a pushed
branch, that round has no coder-run rewrite for the retry to have to force
past — an already-pushed, ancestor-preserving tree fast-forwards, needing no
lease.)

Detection is local-only: it reads the current branch's remote-tracking ref
(``refs/remotes/<remote>/<branch>``), never ``ls-remote`` or any other
network round-trip. ``GitRepo.fetch_remote_branch_sha`` (base-refresh) can
afford a network call because it runs once per attempt; this module runs on
*every* Bash tool call in the PreToolUse hook, so a network round-trip here
would add real latency to every command a coder runs. A branch the harness
actually pushed always has a live tracking ref locally, so this is not a
loss of coverage — just a difference in how "pushed" is confirmed.

Detection is also **two-phase**, to keep that per-command cost near zero for
the overwhelming majority of Bash calls that have nothing to do with
rewriting history:

* **Phase A** (``_classify``) parses each invocation's argv only — no
  subprocess — into `OUTRIGHT` (deny once a pushed tip exists, no ancestry
  check needed), `TARGET(expr)` (deny iff the tip is not an ancestor of
  `expr`), `HEAD_PARENT` (deny iff the tip is not an ancestor of `HEAD^`,
  for `commit --amend`), or `None` (irrelevant to this rule). If every
  invocation in the command classifies `None`, `denial_reason` returns
  before touching a subprocess at all.
* **Phase B** runs only once something classified relevantly: it resolves
  the pushed tip once (`_pushed_tip`, up to 3 subprocesses) and then
  answers each classified invocation with `rev-parse`/`merge-base
  --is-ancestor` calls, memoized per expression within the call.

Failure policy is **fail OPEN**, deliberately inverted from most of this
guard package's conservative-deny convention: a missing cwd, a non-git
directory, a detached HEAD that isn't mid-rebase, no remotes, no tracking
ref, a failed git invocation, or a timeout — every one of these returns
``None`` (no denial), never a raise. An unresolvable TARGET/HEAD_PARENT
expression is the one exception NOT unconditionally fail-open any more
(the c4f717d8 review's MAJOR finding): it denies unless the operand names
an existing path in the worktree, git's own pathspec reading of a bare
``git reset <path>`` — a shell variable or a command substitution the
guard sees only as un-expanded literal text is not given that pass, since
it could just as easily be a real rewrite target as a typo (see
``_is_existing_path``).

A detached HEAD *during* a rebase is resolved rather than treated as
failure: ``git rebase --continue`` only ever runs while a rebase is in
progress, and git detaches HEAD for that whole duration (see
``_rebase_head_name``), so failing open there would silently defeat the
``--continue`` denial in the exact situation it exists to catch. A detached
HEAD OUTSIDE a rebase is different and deliberately left fail-open, even
though it means e.g. ``git branch -f <task-branch> <target>`` run while
detached evades this module entirely: outside a rebase there is no local
record of which branch (if any) HEAD's detachment relates to — inventing
one by guessing from remote-tracking refs risks a false deny with no
narrow, testable trigger, and ``tests/test_pushed_tip_rewrite_guard.py``'s
``test_a_detached_head_and_a_bare_reset_hard_fall_through`` pins exactly
this as intended. Two things justify the rest staying open: this rule is an
*addition* stacked on top of ``guard._git_worktree_denial``, which already
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

#: Prefix-matched (>=3 chars, git's own abbreviation rule) against these to
#: recognize the sequencer wind-back forms that `rebase` must NOT deny —
#: they're already denied, with a more specific working-tree message, by
#: `guard._sequencer_clobbers`. Deliberately excludes `--autostash`: unlike
#: `--abort`/`--skip`, it does not undo a rewrite in progress, it starts one.
_REBASE_WIND_BACK = ("--abort", "--skip")

#: Truthy `pull.rebase` config values (`-c pull.rebase=<value>`) that make a
#: `git pull` rebase-flavored. `false`/`0`/`no`/anything else is not.
_PULL_REBASE_TRUTHY = frozenset({"true", "merges", "interactive", "preserve"})

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


def _subcommand(argv: list[str]) -> tuple[str, list[str], list[str]]:
    """Local copy of guard._git_subcommand, extended to also return the
    values of every `-c <key>=<value>` global option seen before the
    subcommand (Phase A needs `pull.rebase` off of it without a subprocess).
    Returns (subcommand, remaining args, `-c` values)."""
    config_values: list[str] = []
    i = 1
    while i < len(argv):
        tok = argv[i]
        if not tok.startswith("-"):
            return tok, argv[i + 1:], config_values
        if tok == "-c" and i + 1 < len(argv):
            config_values.append(argv[i + 1])
            i += 2
            continue
        if tok in _GIT_GLOBAL_OPT_WITH_ARG:
            i += 2
            continue
        i += 1
    return "", [], config_values


def _split_flag_value_operands(
    rest: list[str], flag: str,
) -> tuple[str | None, list[str]]:
    """Scans `rest` for a short option `flag` (`-B name` or bundled
    `-Bname`), returning its value (or None if absent) and the remaining
    non-flag tokens (operands), in order, with the flag and its value
    removed."""
    value = None
    operands = []
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok == flag:
            if i + 1 < len(rest):
                value = rest[i + 1]
                i += 2
                continue
            i += 1
            continue
        if tok.startswith(flag) and len(tok) > len(flag):
            value = tok[len(flag):]
            i += 1
            continue
        if not tok.startswith("-"):
            operands.append(tok)
        i += 1
    return value, operands


def _prefix_matches(tok: str, full: str) -> bool:
    """True when `tok` (before any `=value`) is an unambiguous (>=3 char)
    prefix of `full`, git's own long-option abbreviation rule."""
    word = tok.split("=", 1)[0]
    return word.startswith("--") and len(word) >= 3 and full.startswith(word)


def _rebase_is_wind_back(rest: list[str]) -> bool:
    """True when `rest` carries --abort/--skip (any unambiguous prefix) —
    forms `rebase` must not deny (see `_REBASE_WIND_BACK`)."""
    return any(
        _prefix_matches(tok, full)
        for tok in rest for full in _REBASE_WIND_BACK
    )


def _pull_is_rebase_flavored(rest: list[str], config_values: list[str]) -> bool:
    """An explicit `--rebase[=<v>]`/`--no-rebase` on the `pull` command
    itself wins over a `-c pull.rebase=<v>` global (matching git's own
    precedence); with neither, falls back to the config value. A config
    FILE's `pull.rebase = true` (as opposed to `-c` on this argv) is out of
    an argv-only rule's reach — the same limitation `guard._sequencer_clobbers`
    already documents."""
    explicit: bool | None = None
    for tok in rest:
        if tok == "--rebase":
            explicit = True
        elif tok.startswith("--rebase="):
            explicit = tok.split("=", 1)[1].strip().lower() not in ("false", "no", "0")
        elif tok == "--no-rebase":
            explicit = False
    if explicit is not None:
        return explicit
    return any(
        val.split("=", 1)[1].strip().lower() in _PULL_REBASE_TRUTHY
        for val in config_values
        if val.split("=", 1)[0].strip() == "pull.rebase" and "=" in val
    )


def _classify_reset(rest: list[str]) -> tuple | None:
    operands = []
    for tok in rest:
        if tok == "--":
            break
        if not tok.startswith("-"):
            operands.append(tok)
    if not operands:
        return None
    return ("target", operands[0])


def _classify_commit(rest: list[str]) -> tuple | None:
    if any(_prefix_matches(tok, "--amend") for tok in rest):
        return ("head_parent",)
    return None


def _classify_checkout(rest: list[str]) -> tuple | None:
    name, operands = _split_flag_value_operands(rest, "-B")
    if name is None:
        return None
    start_point = operands[0] if operands else "HEAD"
    return ("branch_target", name, start_point)


def _classify_switch(rest: list[str]) -> tuple | None:
    name, operands = _split_flag_value_operands(rest, "-C")
    if name is None:
        return None
    start_point = operands[0] if operands else "HEAD"
    return ("branch_target", name, start_point)


def _classify_branch(rest: list[str]) -> tuple | None:
    if not any(f in ("-f", "--force") for f in rest):
        return None
    operands = [t for t in rest if not t.startswith("-")]
    if len(operands) < 2:
        return None
    return ("branch_target", operands[0], operands[1])


def _classify_update_ref(rest: list[str]) -> tuple | None:
    if "--stdin" in rest:
        # `--stdin` reads the actual ref updates from stdin, which this
        # argv-only phase never sees — it could rewrite refs/heads/<branch>
        # to anything with no operand on the command line to inspect.
        # Denying outright (once a pushed tip exists) matches the other
        # OUTRIGHT forms below rather than silently falling through None.
        return ("outright",)
    delete = any(f in ("-d", "--delete") for f in rest)
    operands = [t for t in rest if not t.startswith("-")]
    if not operands:
        return None
    ref = operands[0]
    if ref == "HEAD":
        if delete:
            return ("outright",)
        return ("target", operands[1]) if len(operands) >= 2 else None
    if ref.startswith("refs/heads/"):
        name = ref[len("refs/heads/"):]
        if delete:
            return ("branch_outright", name)
        return ("branch_target", name, operands[1]) if len(operands) >= 2 else None
    return None


def _classify(sub: str, rest: list[str], config_values: list[str]) -> tuple | None:
    """Phase A: pure argv classification, zero subprocess calls. Returns a
    tuple whose first element is one of "outright", "target",
    "head_parent", "branch_target", "branch_outright" (see `denial_reason`
    for how each is resolved in Phase B), or None if this invocation is
    irrelevant to the pushed-tip rule."""
    if sub == "rebase":
        return None if _rebase_is_wind_back(rest) else ("outright",)
    if sub == "pull":
        return ("outright",) if _pull_is_rebase_flavored(rest, config_values) else None
    if sub == "reset":
        return _classify_reset(rest)
    if sub == "commit":
        return _classify_commit(rest)
    if sub == "checkout":
        return _classify_checkout(rest)
    if sub == "switch":
        return _classify_switch(rest)
    if sub == "branch":
        return _classify_branch(rest)
    if sub == "update-ref":
        return _classify_update_ref(rest)
    if sub == "filter-branch":
        return ("outright",)
    return None


def _is_existing_path(cwd: str | None, expr: str) -> bool:
    """True when `expr` names an existing file or directory relative to
    `cwd`. This is the ONE exception `target_denies` grants an unresolvable
    operand: `git reset <pathspec>` (no `--`, operand not a valid rev) is
    git's own fallback reading — it resets the index for that path and never
    moves the branch. Any other unresolvable operand (a shell variable, a
    command substitution the guard sees only as the un-expanded literal
    text, a typo) is NOT given this pass — it denies instead."""
    if not cwd or not expr:
        return False
    return os.path.exists(os.path.join(cwd, expr))


def _message(seg: str, remote: str, branch: str, tip: str) -> str:
    return (
        f"rewriting a pushed branch is blocked: {seg}. {remote}/{branch} is "
        f"already pushed at {tip} — this command rewrites history the "
        "remote already has, and delivery is fast-forward-only against it: "
        f'it will refuse the branch with "remote tip {tip} ... is not an '
        'ancestor of the reviewed sha" and the task escalates. Bring the '
        "base in with a MERGE instead: `git merge <base sha>`, resolve the "
        "conflicts, `git commit`. A branch that was never pushed is "
        "unaffected by this rule."
    )


def denial_reason(
    invocations: list[tuple[str, list[str]]], cwd: str | None,
) -> str | None:
    """Given the parsed git invocations found in a proposed Bash command
    (as returned by guard._git_invocations) and the session cwd, return a
    denial reason if any of them would rewrite the current branch below its
    already-pushed remote tip — else None (see module docstring: this fails
    open on any uncertainty).

    Two phases: Phase A classifies every invocation from argv alone (no
    subprocess); if none are relevant, this returns before Phase B ever
    calls `_pushed_tip`."""
    if not invocations:
        return None

    classified = []
    for seg, argv in invocations:
        sub, rest, config_values = _subcommand(argv)
        kind = _classify(sub, rest, config_values)
        if kind is not None:
            classified.append((seg, kind))
    if not classified:
        return None

    pushed = _pushed_tip(cwd)
    if not pushed:
        return None
    remote, branch, tip = pushed

    resolved_cache: dict[str, str | None] = {}

    def resolve(expr: str) -> str | None:
        if expr not in resolved_cache:
            resolved_cache[expr] = _git(
                cwd, "rev-parse", "--verify", "--quiet", f"{expr}^{{commit}}")
        return resolved_cache[expr]

    def target_denies(expr: str) -> bool:
        resolved = resolve(expr)
        if not resolved:
            # Unresolvable is NOT automatically safe: a shell variable or a
            # command substitution (`$(git rev-parse HEAD~2)`) reaches here
            # as a literal, un-expanded string too, and denying nothing for
            # those let a real rewrite through (see the module's git history
            # for the incident this fixed). The one legitimate unresolvable
            # case is git's own pathspec fallback for a bare
            # `git reset <path>` — that never moves the branch — so allow
            # only when the operand names something that actually exists in
            # the worktree.
            return not _is_existing_path(cwd, expr)
        return not _git_ok(cwd, "merge-base", "--is-ancestor", tip, resolved)

    def head_parent_denies() -> bool:
        return not _git_ok(cwd, "merge-base", "--is-ancestor", tip, "HEAD^")

    for seg, kind in classified:
        tag = kind[0]
        if tag == "outright":
            return _message(seg, remote, branch, tip)
        if tag == "head_parent":
            if head_parent_denies():
                return _message(seg, remote, branch, tip)
            continue
        if tag == "target":
            if target_denies(kind[1]):
                return _message(seg, remote, branch, tip)
            continue
        if tag == "branch_target":
            name, expr = kind[1], kind[2]
            if name == branch and target_denies(expr):
                return _message(seg, remote, branch, tip)
            continue
        if tag == "branch_outright":
            name = kind[1]
            if name == branch:
                return _message(seg, remote, branch, tip)
            continue
    return None
