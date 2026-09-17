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
``_finalize`` retry's ``forced = _is_non_fast_forward(exc)`` decision
(``orchestrator.py`` ~:7981) is reached only after ``_assert_delivery_sha``
(``orchestrator.py`` ~:7734) has already pinned the reviewed sha for THIS
push — so that force can only ever re-send the same pinned commit under
lease, never resurrect a branch this module refused to let get rewritten.
The draft-PR retry's ``_is_non_fast_forward(err) and await
self._mechanical_round(task)`` check in ``_open_draft_pr_for_review``
(``orchestrator.py`` ~:13862) is different: it fires during a
``pr_conflict`` mechanical round, *before* any review verdict exists, so it
does not sit behind ``_assert_delivery_sha`` at all, and its reachability
has no provenance condition — it fires regardless of *how* the branch
became non-fast-forwardable.

This module does NOT make that force-with-lease retry unreachable, and does
not try to. It is a lexical guard over one channel — a git invocation
proposed as a coder Bash command — not an enforced capability: ``git
checkout --detach HEAD`` (this alone classifies ``None`` — it does not
touch the current branch) followed, in a SEPARATE Bash call once HEAD is
detached, by ``git branch -f <branch> <target>`` evades this module only
because by then ``git symbolic-ref HEAD`` no longer names the branch for
``_current_branch`` to compare against (see ``_classify_branch`` and the
detached-HEAD failure-policy paragraph below) — the same ``branch -f`` run
on an ATTACHED branch is still DENIED. A shell script that runs ``git
rebase`` from inside ``sh script.sh``, or a ``subprocess`` call from inside
``python3 -c``, rewrite the branch the same way: without ever presenting
this module with a recognizable git argv, because ``guard._git_invocations``
never sees inside another interpreter's own command string. That is not the
only way to slip past a lexical parser without leaving the same shell,
either: ``guard._git_invocations`` (unlike its sibling
``_forge_invocations``) does not strip a brace group or a command
substitution before the verb — ``{ git rebase origin/main; }`` and
``$(which git) rebase origin/main`` both run `git rebase` in the SAME
shell, yet neither tokenizes to a recognizable ``git rebase`` argv today.
Fixing that is a change to ``_git_invocations`` itself, which this module
does not own and does not attempt here. Denying every *lexical spelling*
this module recognizes closes the direct path; it cannot close indirection
through another interpreter, and it does not yet close every same-shell
grouping/substitution form either. The retry's ``force_with_lease``
therefore stays exactly as load-bearing as ``GitRepo.push``'s own docstring
in ``git.py`` says (~:1588-1611) and as the ``_finalize`` retry's comment
in ``orchestrator.py`` says (~:7969-7980): most rewrites should now be
caught before they run, but the lease is real defense-in-depth for the ones
that are not.)

Two more single-command-scoped gaps, named honestly rather than closed,
because closing either needs more than argv lexing:

* ``git -C <dir> rebase ...`` (a *global* ``-C``, before the subcommand,
  naming a different repo to operate in) is skipped by ``_subcommand`` like
  any other global option-with-argument, but Phase B's subprocess calls
  (``_pushed_tip``, ``_git``) still run against the PreToolUse hook's own
  session ``cwd`` — never against ``<dir>``. The classification (`OUTRIGHT`
  for a bare ``rebase``, say) is therefore checked against the wrong
  worktree's pushed tip whenever ``<dir>`` differs from the session cwd:
  this can misfire in either direction, an ALLOW on a rewrite of a pushed
  branch in ``<dir>``, or a spurious DENY driven by ``cwd``'s unrelated
  state. Resolving it means plumbing ``<dir>`` through as the effective cwd
  for the rest of classification, which this module does not do.
* ``git branch -f`` is the only OUTRIGHT-triggering spelling
  ``_classify_branch`` recognizes; an un-forced ``git branch -m <branch>
  <tmp>`` (rename the pushed branch out of the way) followed, in a SEPARATE
  Bash call, by an un-forced ``git branch <branch> <target>`` (recreate the
  name fresh, since it no longer exists locally) rewrites the same pointer
  without either command ever carrying ``-f``/``--force``. This is the same
  class of gap as the detached-HEAD ``branch -f`` bypass above: a two-command
  sequence that never presents this module with a single recognizable
  rewrite of an existing branch.

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

Multi-remote repositories get a deliberately simple answer, not a
most-current one: ``_remotes`` sorts the locally configured remotes with
``origin`` first and the rest alphabetically, and ``_pushed_tip`` returns the
tip of the FIRST of those that has a tracking ref for the current branch —
not whichever remote's tip is actually newest. ``GitRepo`` (this harness's
own git wrapper) always pushes to and reads from a hardcoded
``remote="origin"``, so the harness's own path never has more than one
candidate remote and cannot observe this; a repo a coder hand-configured
with a second remote could, in principle, get a stale non-``origin`` tip
back if ``origin``'s own tracking ref happened to be absent, or a stale
``origin`` tip preferred while a second remote is actually ahead. This is a
deliberate simplification, not an oversight: comparing tips across every
configured remote would add subprocesses to the common case the Phase A/B
split above is written specifically to keep cheap, to resolve an ordering
question the harness's own single-remote flow never asks. Left undocumented
in earlier drafts of this module; noted here explicitly rather than fixed,
since fixing it would mean either a network round-trip (ruled out above) or
guessing at multi-remote intent this module has no basis to guess at.

A detached HEAD *during* a rebase is resolved rather than treated as
failure: ``git rebase --continue`` only ever runs while a rebase is in
progress, and git detaches HEAD for that whole duration (see
``_rebase_head_name``), so failing open there would silently defeat the
``--continue`` denial in the exact situation it exists to catch. A detached
HEAD OUTSIDE a rebase is different and deliberately left fail-open, even
though it means e.g. ``git branch -f <task-branch> <target>`` run while
detached evades this module entirely. This is not because no local signal
exists — ``git reflog HEAD -1``, ``@{-1}``, and ``git branch --points-at
HEAD`` can each recover a candidate branch in the common case. It is a
deliberate choice not to build a heuristic on top of them: each can name
the wrong branch (a commit reachable from several branches), no branch at
all (HEAD sits on a commit no branch currently points at — mid-cherry-pick,
mid-bisect, or after a detached ``reset --hard`` off the branch tip), or a
stale one (``@{-1}`` names whichever branch checkout last visited, not
necessarily the one HEAD detached from). Guessing wrong here denies a
legitimate detached-HEAD workflow with no narrow, testable trigger to catch
the mistake — worse than the known, documented gap this leaves instead, and
``tests/test_pushed_tip_rewrite_guard.py``'s
``test_a_detached_head_and_a_bare_reset_hard_fall_through`` pins exactly
this as intended.

An earlier draft of this paragraph justified staying open here by claiming
``guard._git_worktree_denial`` is an independent backstop that "already
blocks every tree-clobbering git form regardless of what this module
decides". Measured directly, that is false for exactly the forms this
module exists to catch: ``_git_worktree_denial`` denies a bare
``rebase --abort``/``--skip``/``--autostash`` (via ``_sequencer_clobbers``)
and a hard ``reset``/``clean``/``checkout -f`` (via ``_reset_clobbers``/
``_clean_clobbers``/``_checkout_clobbers``), but it does NOT deny a plain
``git rebase <base>``, ``pull --rebase``, ``commit --amend``,
``update-ref``, ``checkout -B``, or ``branch -f`` — precisely the
OUTRIGHT/TARGET/HEAD_PARENT forms this module classifies. Of the three
sequencer wind-back forms `_git_worktree_denial` already covers, only
``--abort``/``--skip`` are excluded from THIS module's own OUTRIGHT denial
(see ``_REBASE_WIND_BACK`` above, and its comment on why `--autostash` is
deliberately left out of that tuple): `--autostash` is NOT excluded here,
so a bare ``rebase --autostash`` is denied twice over, once by each module
— redundant, not a gap. If this module fails open on one
of those (detached HEAD outside a rebase; no remotes; a timeout; an
unreadable cwd), ``_git_worktree_denial`` does not catch it either.

What actually backstops a rewrite that slips past every lexical guard —
this module and ``_git_worktree_denial`` both — is the property the second
paragraph of this docstring describes: delivery's own branch push
(``GitRepo.push_sha_fast_forward``) is fast-forward-only and refuses a
rewritten branch outright, and ``force_with_lease`` is the documented,
load-bearing recovery for exactly that refusal (see ``git.py``'s ``push``
docstring, ~:1613-1630, for the lease's safety properties). That backstop
lives downstream of this module, at
delivery time, not in another PreToolUse guard alongside it. Two things
justify leaving the detached-HEAD-outside-a-rebase case open rather than
guessing at a heuristic: a false positive here would break the legitimate
rebase-on-a-never-pushed-branch workflow that ``tests/test_guard.py``'s
``_SEQUENCER_PAIRS`` pins as allowed, and the downstream fast-forward
refusal still catches the rewrite before it reaches the remote. Getting
this wrong in the deny direction breaks real work; getting it wrong in the
allow direction defers the catch to delivery time instead of losing it.
"""

from __future__ import annotations

import os
import re
import subprocess

#: Prefix-matched (>=3 chars, git's own abbreviation rule) against these to
#: recognize the sequencer wind-back forms that `rebase` must NOT deny —
#: they're already denied, with a more specific working-tree message, by
#: `guard._sequencer_clobbers`. Deliberately excludes `--autostash`: unlike
#: `--abort`/`--skip`, it does not undo a rewrite in progress, it starts one.
_REBASE_WIND_BACK = ("--abort", "--skip")

#: A local copy of `guard._UNRESOLVABLE` (same "must not import guard.py"
#: reasoning as `_GIT_GLOBAL_OPT_WITH_ARG` below). A `$`/backtick marks a
#: shell variable or command substitution that this module — a lexical
#: parser of the argv string, not a shell — sees only as un-expanded
#: literal text: `git checkout -B $B origin/main` never lexically equals
#: the real branch name, so a bare `==` against it always says "not the
#: current branch" for a case exactly this rule exists to catch. See
#: `_name_denies` below, and
#: `test_a_shell_variable_branch_name_is_denied_like_an_unresolvable_target`
#: for this run for real and denied.
_UNRESOLVABLE = re.compile(r"[$`]")

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
            capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace",
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
            capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace",
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
            with open(os.path.join(git_dir, state_dir, "head-name"), "r", encoding="utf-8") as f:
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
    """Scans `rest` for an option `flag` (`-B name`/`--force-create name`,
    or glued `-Bname`/`--force-create=name`), returning its value (or None
    if absent) and the remaining non-flag tokens (operands), in order, with
    the flag and its value removed. The `=` in a glued long option is
    stripped; a bundled short option never has one, so stripping it there
    too is a no-op."""
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
            if value.startswith("="):
                value = value[1:]
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
    """An explicit `--rebase[=<v>]`/`-r`/`--no-rebase` on the `pull` command
    itself wins over a `-c pull.rebase=<v>` global (matching git's own
    precedence); with neither, falls back to the config value. A config
    FILE's `pull.rebase = true` (as opposed to `-c` on this argv) is out of
    an argv-only rule's reach — the same limitation `guard._sequencer_clobbers`
    already documents."""
    explicit: bool | None = None
    for tok in rest:
        if tok == "--rebase" or tok == "-r":
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
    """`git reset [<tree-ish>] [--] <pathspec>...` is git's documented
    index-only form: any operand before a `--` is the optional source
    tree-ish for staging the named paths, not a branch-move target — this
    form never touches the branch ref, no matter what precedes `--`. Once
    `--` is seen this returns None immediately instead of falling through
    to the collected operand, or `git reset HEAD~1 -- f.txt` (a coder
    restaging one file from another ref while resolving a base-merge
    conflict) would be misread as moving the branch to `HEAD~1` and denied
    even though it never leaves the index. See
    `test_a_tree_ish_before_the_pathspec_separator_stays_an_index_only_reset`
    for this run for real and proven not to move HEAD."""
    operands = []
    for tok in rest:
        if tok == "--":
            return None
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
    """`-C <name>` and its long spelling `--force-create[=<name>]` are the
    same flag (git accepts either); both must be recognized or the long
    spelling is a lexical bypass of the pushed-tip check `-C` triggers."""
    name, operands = _split_flag_value_operands(rest, "-C")
    if name is None:
        name, operands = _split_flag_value_operands(rest, "--force-create")
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
    """`-m <reason>` takes a value that is stripped before the positional
    ref/newvalue/oldvalue operands are read off — otherwise the reason
    string itself would be misread as `ref`, hiding the actual update from
    this classifier."""
    if "--stdin" in rest:
        # `--stdin` reads the actual ref updates from stdin, which this
        # argv-only phase never sees — it could rewrite refs/heads/<branch>
        # to anything with no operand on the command line to inspect.
        # Denying outright (once a pushed tip exists) matches the other
        # OUTRIGHT forms below rather than silently falling through None.
        return ("outright",)
    delete = any(f in ("-d", "--delete") for f in rest)
    _reason, rest = _split_flag_value_operands(rest, "-m")
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
    """True when `expr` names a path `target_denies` should treat as git's
    own pathspec fallback rather than an unresolvable rev: `git reset
    <pathspec>` (no `--`, operand not a valid rev, path present on disk) is
    git's own fallback reading — it resets the index for that path and
    never moves the branch. Any other unresolvable operand (a shell
    variable, a command substitution the guard sees only as the
    un-expanded literal text, a typo) is NOT given this pass — it denies
    instead.

    Deliberately disk-only — an earlier version of this function also
    passed a path missing from disk but still known to the index or to
    HEAD (reasoning that `rm del.txt && git reset del.txt` "still only
    touches the index, exactly like the on-disk case"). Measured on git
    2.50.1 (`test_git_itself_decides_what_a_bare_reset_pathspec_does` in
    `tests/test_pushed_tip_rewrite_guard.py` runs this): that command is
    NOT a pathspec fallback at all — it is `fatal: ambiguous argument
    'del.txt': unknown revision or path not in the working tree`, rc 128,
    identically whether the path was removed with plain `rm` or with `git
    rm`. It touches nothing, index included. The spelling that actually
    resets a since-deleted tracked path, `git reset -- del.txt`, was
    already allowed before it ever reaches this function, because
    `_classify_reset` breaks its operand scan at `--` and returns no
    target to classify. So the widened check restored no real workflow;
    it has been removed. Its only OTHER effect — pinned, not restored — is
    that it also let an un-anchored pathspec glob such as `git reset
    '*.txt'` through via `ls-files --error-unmatch`'s own glob matching,
    even though no file is literally named `*.txt`. With the widening
    gone, that glob spelling is now DENIED by this function (no literal
    disk match) even though running it for real only resets the index —
    the false-deny is accepted rather than silently documented, the same
    posture this module already takes for the never-guessed detached-HEAD
    case below; the fix is the same `--` spelling: `git reset -- '*.txt'`."""
    return bool(cwd and expr and os.path.exists(os.path.join(cwd, expr)))


def _name_denies(name: str, branch: str) -> bool:
    """True when `name` — the argv-parsed branch-name literal from
    `checkout -B`/`switch -C`/`branch -f`/`update-ref`'s `refs/heads/<name>`
    — is either literally the current branch, or an unresolvable shell
    expansion this module cannot rule out. Mirrors `target_denies`'s
    conservative-deny-unless-provably-safe stance (see its docstring) on the
    branch-NAME side of the same commands: an unresolved `$B`/`` `cmd` ``
    reaches here as literal text too, and a bare `==` against it can never
    match the real branch even when it names it at runtime."""
    return name == branch or bool(_UNRESOLVABLE.search(name))


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
            # only when the operand names a path that actually exists on
            # disk (see `_is_existing_path`). A tracked path already
            # deleted from disk does NOT qualify: `git reset <path>` with
            # no `--` for a path missing from disk is a git error (rc 128
            # on 2.50.1), not a pathspec fallback — see `_is_existing_path`
            # for the measurement and the `--` spelling that does work.
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
            if _name_denies(name, branch) and target_denies(expr):
                return _message(seg, remote, branch, tip)
            continue
        if tag == "branch_outright":
            name = kind[1]
            if _name_denies(name, branch):
                return _message(seg, remote, branch, tip)
            continue
    return None
