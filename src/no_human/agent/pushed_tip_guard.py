"""Refuse to rewrite a branch that has already been pushed.

A coder that hits a base-refresh merge conflict sometimes reaches for
``git rebase`` (or a ``git reset`` below the pushed tip, a ``commit
--amend`` of it, or a handful of other forms that move a branch pointer
backwards — ``pull --rebase``/``pull -r``, ``checkout -B``,
``switch -C``/``switch --force-create``, ``branch -f``, ``update-ref``,
``filter-branch``) instead of resolving the conflict
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
(``orchestrator.py`` ~:7725) is reached only after ``_assert_delivery_sha``
(``orchestrator.py`` ~:7478) has already pinned the reviewed sha for THIS
push — so that force can only ever re-send the same pinned commit under
lease, never resurrect a branch this module refused to let get rewritten.
The draft-PR retry's ``_is_non_fast_forward(err) and await
self._mechanical_round(task)`` check in ``_open_draft_pr_for_review``
(``orchestrator.py`` ~:13541) is different: it fires during a
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
this module with a recognizable git argv. Denying every *lexical spelling*
of the git forms listed above closes the direct path; it cannot close
indirection through another interpreter. The retry's ``force_with_lease``
therefore stays exactly as load-bearing as ``GitRepo.push``'s own docstring
in ``git.py`` says (~:1384-1396) and as the ``_finalize`` retry's comment
in ``orchestrator.py`` says (~:7713-7724): most rewrites should now be
caught before they run, but the lease is real defense-in-depth for the ones
that are not.)

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

  Phase A parses argv the way git's own parse-options does — short/long
  synonyms are one option, an option that takes a value swallows the next
  token, and `--` ends the operands — because reading it any other way is
  a silent bypass, not a cosmetic inaccuracy: see the block comment above
  the `_*_OPTS` tables for the three spellings (`pull -r`, `switch
  --force-create`, `update-ref -m <reason> <ref> <target>`) that were
  measured ALLOWING a real, executed rewrite because of exactly those two
  reading errors.
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
            with open(os.path.join(git_dir, state_dir, "head-name"),
                      "r", encoding="utf-8") as f:
                ref = f.read().strip()
        except (OSError, UnicodeError):
            # `encoding="utf-8"` rather than the platform default: this dies
            # on the first multi-byte character under a non-UTF-8 locale
            # otherwise (issue #267, and `tests/
            # test_text_reads_declare_encoding.py` fails the suite over it).
            # `UnicodeError` joins `OSError` because this function promises
            # never to raise — it runs inside a PreToolUse hook, and a
            # decode error on a ref name is not a reason to break every Bash
            # call the coder makes.
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


# Per-subcommand option tables, transcribed from `git <sub> -h` on git
# 2.50.1 (Apple Git-155). Two properties of git's own parser make an
# ad-hoc "a token starting with `-` is an option, everything else is an
# operand" scan wrong, and BOTH were live defects here (measured 2026-09-13,
# each command EXECUTED against a real bare remote with a diverged
# origin/main, each one leaving the pushed tip no longer an ancestor of HEAD
# while this module said ALLOW):
#
#   1. An option spelled two ways is one option. `git pull -r` is
#      `git pull --rebase`; `git switch --force-create` is `git switch -C`.
#      Recognizing only one spelling is a silent bypass.
#   2. An option that TAKES A VALUE swallows the next token. In
#      `git update-ref -m reason refs/heads/feature origin/main`, `reason`
#      is `-m`'s value, not operand 0 — reading it as operand 0 made the
#      `refs/heads/` test look at `reason` and classify nothing.
#
# So every classifier below goes through `_parse`, which is given that
# subcommand's option table as `(value_opts, flag_opts, value_shorts,
# optarg_shorts)`:
#
#   value_opts    long options that take a REQUIRED value, so `--opt <v>`
#                 consumes the next token as well as `--opt=<v>`
#   flag_opts     every other long option git documents for that
#                 subcommand, including the ones whose value is OPTIONAL
#                 (`--color[=<when>]`) and therefore only ever attached
#   value_shorts  short options with a REQUIRED value (`-m msg`, `-mmsg`)
#   optarg_shorts short options with an OPTIONAL value, which take the rest
#                 of their own token and NEVER the next one — git's
#                 `PARSE_OPT_OPTARG` rule, and the reason `-Xtheirs` must
#                 not be read as containing the `-r` of `--rebase`
#
# `flag_opts` is not decoration: git resolves `--force` on `switch` by
# EXACT match to `--discard-changes`, never as an abbreviation of
# `--force-create`. Listing only the value-taking options made `_parse`
# read `git switch --force -C feature origin/main` as `--force` taking the
# value `-C`, which left the real branch reset unclassified and ALLOWED.
# Anything not in either long list is treated as a flag — fail-closed for
# the operand scan, since an unrecognized option's value then shows up as
# an extra operand rather than silently shifting the one that matters.
#
# Transcribed from `git <sub> -h` on git 2.50.1 (Apple Git-155). `--no-`
# negations are omitted throughout: a `--no-X` spelling can never be read
# as an abbreviation of a positive `--X` (the prefix test fails), and no
# value option here has a `--no-` form.
_REBASE_OPTS = (
    ("--onto", "--whitespace", "--empty", "--exec", "--strategy",
     "--strategy-option"),
    ("--keep-base", "--verify", "--quiet", "--verbose", "--stat", "--signoff",
     "--committer-date-is-author-date", "--reset-author-date",
     "--ignore-whitespace", "--force-rebase", "--ff", "--continue", "--skip",
     "--abort", "--quit", "--edit-todo", "--show-current-patch", "--apply",
     "--merge", "--interactive", "--rerere-autoupdate", "--autosquash",
     "--update-refs", "--gpg-sign", "--autostash", "--rebase-merges",
     "--fork-point", "--root", "--reschedule-failed-exec",
     "--reapply-cherry-picks"),
    "xsXC",
    "Sr",
)

_PULL_OPTS = (
    ("--cleanup", "--strategy", "--strategy-option", "--upload-pack",
     "--depth", "--shallow-since", "--shallow-exclude", "--deepen",
     "--refmap", "--server-option", "--negotiation-tip"),
    ("--verbose", "--quiet", "--progress", "--recurse-submodules", "--rebase",
     "--stat", "--log", "--signoff", "--squash", "--commit", "--edit", "--ff",
     "--ff-only", "--verify", "--verify-signatures", "--autostash",
     "--gpg-sign", "--allow-unrelated-histories", "--all", "--append",
     "--force", "--tags", "--prune", "--jobs", "--dry-run", "--keep",
     "--unshallow", "--update-shallow", "--ipv4", "--ipv6"),
    "sXo",
    "Srj",
)

_RESET_OPTS = (
    ("--pathspec-from-file",),
    ("--quiet", "--refresh", "--mixed", "--soft", "--hard", "--merge",
     "--keep", "--recurse-submodules", "--patch", "--intent-to-add",
     "--pathspec-file-nul"),
    "",
    "",
)

_COMMIT_OPTS = (
    ("--file", "--author", "--date", "--message", "--reedit-message",
     "--reuse-message", "--fixup", "--squash", "--trailer", "--template",
     "--cleanup", "--pathspec-from-file"),
    ("--quiet", "--verbose", "--dry-run", "--long", "--null", "--branch",
     "--porcelain", "--short", "--status", "--reset-author", "--amend",
     "--allow-empty", "--allow-empty-message", "--no-verify", "--signoff",
     "--gpg-sign", "--untracked-files", "--all", "--include", "--only",
     "--interactive", "--patch", "--pathspec-file-nul"),
    "FmcCt",
    "Su",
)

_CHECKOUT_OPTS = (
    ("--orphan", "--conflict", "--pathspec-from-file"),
    ("--guess", "--progress", "--force", "--merge", "--detach", "--track",
     "--ours", "--theirs", "--patch", "--overlay", "--quiet",
     "--overwrite-ignore", "--ignore-other-worktrees", "--recurse-submodules",
     "--ignore-skip-worktree-bits", "--pathspec-file-nul"),
    "bB",
    "t",
)

_SWITCH_OPTS = (
    ("--create", "--force-create", "--orphan", "--conflict"),
    ("--guess", "--discard-changes", "--quiet", "--recurse-submodules",
     "--progress", "--merge", "--detach", "--track", "--force",
     "--overwrite-ignore", "--ignore-other-worktrees"),
    "cC",
    "t",
)

_BRANCH_OPTS = (
    ("--set-upstream-to", "--contains", "--no-contains", "--merged",
     "--no-merged", "--points-at", "--sort", "--format"),
    ("--verbose", "--quiet", "--track", "--unset-upstream", "--color",
     "--remotes", "--abbrev", "--all", "--delete", "--move", "--omit-empty",
     "--copy", "--list", "--show-current", "--create-reflog",
     "--edit-description", "--force", "--column", "--ignore-case",
     "--recurse-submodules"),
    "u",
    "t",
)

_UPDATE_REF_OPTS = (
    (),
    ("--no-deref", "--deref", "--stdin", "--create-reflog", "--batch-updates"),
    "m",
    "",
)


def _parse(
    rest: list[str],
    table: tuple[tuple[str, ...], tuple[str, ...], str, str] = ((), (), "", ""),
) -> tuple[list[tuple[str, str | None]], list[str]]:
    """Split a git subcommand's argv into (options, operands) the way git's
    own parse-options does: long options may be `--opt`, `--opt=<v>`,
    `--opt <v>` or an unambiguous >=3-char abbreviation, with an EXACT
    spelling always winning over a longer option it prefixes; short options
    bundle (`-qf`), take their value attached (`-mmsg`) or as the next
    token (`-m msg`); and everything after a bare `--` is pathspec, not an
    operand.

    Returns options as (name, value) pairs — name is the long spelling as
    written (before any `=`) or `-<char>` for a short — and the positional
    operands in order. A caller that only needs presence uses `_flag`; one
    that needs the value uses `_value`."""
    value_opts, flag_opts, value_shorts, optarg_shorts = table
    opts: list[tuple[str, str | None]] = []
    operands: list[str] = []
    i, n = 0, len(rest)
    while i < n:
        tok = rest[i]
        if tok == "--":
            break
        if tok.startswith("--") and len(tok) > 2:
            word, eq, val = tok.partition("=")
            if eq:
                opts.append((word, val))
            elif _takes_next_token(word, value_opts, flag_opts) and i + 1 < n:
                opts.append((word, rest[i + 1]))
                i += 2
                continue
            else:
                opts.append((word, None))
            i += 1
            continue
        if tok.startswith("-") and len(tok) > 1:
            body = tok[1:]
            j = 0
            while j < len(body):
                ch = body[j]
                if ch in value_shorts or ch in optarg_shorts:
                    tail = body[j + 1:]
                    if tail:
                        opts.append((f"-{ch}", tail))
                    elif ch in value_shorts and i + 1 < n:
                        opts.append((f"-{ch}", rest[i + 1]))
                        i += 1
                    else:
                        opts.append((f"-{ch}", None))
                    break
                opts.append((f"-{ch}", None))
                j += 1
            i += 1
            continue
        operands.append(tok)
        i += 1
    return opts, operands


def _takes_next_token(
    word: str, value_opts: tuple[str, ...], flag_opts: tuple[str, ...],
) -> bool:
    """True when the long option spelled `word` consumes the FOLLOWING token
    as its value. Resolved the way git resolves it: an exact spelling is
    that option and nothing else, even when it also prefixes a longer one
    (`git rebase --strategy ours` is `--strategy`, not an ambiguous stab at
    `--strategy-option`); otherwise an abbreviation counts only when it is
    unique across the subcommand's WHOLE option table, `flag_opts` included.

    That last clause is what keeps `git switch --force -C <branch> <target>`
    classified: `--force` prefixes the value-taking `--force-create`, and
    reading it as one made `--force` swallow the `-C` and the branch reset
    go unclassified — ALLOWED. It needs no separate exact-flag branch,
    because a `word` that is itself in `flag_opts` always matches at least
    itself there, so the count can never come out unique-and-value-taking.

    Anything unrecognized is treated as a flag — the operand it precedes
    then shows up as an extra operand, which denies more, never less."""
    if word in value_opts:
        return True
    matches = [full for full in value_opts + flag_opts if _abbrev_of(word, full)]
    return len(matches) == 1 and matches[0] in value_opts


def _abbrev_of(word: str, full: str) -> bool:
    """True when `word` is `full` or an unambiguous (>=3 char) prefix of it —
    git's own long-option abbreviation rule. A `--no-` negation never
    abbreviates the positive form (`--no-rebase` is not `--rebase`), which
    falls out of the prefix test."""
    return word.startswith("--") and len(word) >= 3 and full.startswith(word)


def _flag(opts: list[tuple[str, str | None]], *names: str) -> bool:
    """True when any of `names` (a long option, matched by git's abbreviation
    rule, or an exact `-<char>` short) is present in `opts`."""
    for name, _ in opts:
        for full in names:
            if name == full or (full.startswith("--") and _abbrev_of(name, full)):
                return True
    return False


def _value(
    opts: list[tuple[str, str | None]], *names: str,
    exclude: tuple[str, ...] = (),
) -> str | None:
    """The value of the first of `names` present in `opts`, or None when none
    of them is present. Same matching rules as `_flag` — so a present option
    with no value is indistinguishable from an absent one here; callers that
    need to tell them apart pair this with `_flag`."""
    for name, val in opts:
        if name in exclude:
            continue
        for full in names:
            if name == full or (full.startswith("--") and _abbrev_of(name, full)):
                return val
    return None


def _rebase_is_wind_back(opts: list[tuple[str, str | None]]) -> bool:
    """True when the parsed options carry --abort/--skip (any unambiguous
    prefix) — forms `rebase` must not deny (see `_REBASE_WIND_BACK`). Reads
    the PARSED options, not raw argv, so `git rebase -x '--abort' main`
    (where `--abort` is `--exec`'s value, not an option) is not mistaken for
    a wind-back and let through."""
    return _flag(opts, *_REBASE_WIND_BACK)


def _classify_rebase(rest: list[str]) -> tuple | None:
    """A rebase replays `<upstream>..HEAD` onto a NEW BASE and leaves that
    new base an ancestor of the result — always, on every backend and for
    every option combination, since the replayed commits are committed on
    top of it. So "does the pushed tip survive this rebase?" is exactly
    "is the tip an ancestor of the new base?", and a rebase that carries a
    new base on its argv is a `target`, not an `outright`.

    That distinction is load-bearing in the ALLOW direction: `git rebase -i
    HEAD~2` to squash two UNPUSHED commits before delivery is a normal thing
    to do and leaves the pushed tip untouched (measured — the executed row in
    `tests/test_pushed_tip_rewrite_guard.py`'s allowed table). Denying it
    outright, as this function's first version did, denied normal work; a
    guard that denies normal work gets switched off.

    Where the new base is NOT on the argv, this still denies outright:

    * `--continue`/`--edit-todo` — the rewrite is already in flight and its
      base was chosen by the command that started it, which is not this one.
      (`--abort`/`--skip` wind it BACK and are handled above.)
    * `--quit` and `--show-current-patch` do not themselves rewrite
      anything; they are denied here only because they carry no base to
      measure, and `--abort` is the escape hatch that stays allowed.
    * `--root` rewrites every commit up to the root — there is no surviving
      base at all.
    * `--keep-base` computes the base as `merge-base(<upstream>, HEAD)`,
      which is not a literal on the argv for Phase B to resolve.

    With no `--onto`, no `--root` and no positional `<upstream>`, git falls
    back to the branch's configured upstream, which is exactly what the
    revision expression `@{upstream}` names — so that is what Phase B is
    handed, and it resolves (or fails to, and denies) the same way any other
    expression does."""
    opts, operands = _parse(rest, _REBASE_OPTS)
    if _rebase_is_wind_back(opts):
        return None
    if _flag(opts, "--continue", "--edit-todo", "--quit",
             "--show-current-patch", "--root", "--keep-base"):
        return ("outright",)
    onto = _value(opts, "--onto")
    if onto is None and not _flag(opts, "--onto"):
        newbase = operands[0] if operands else "@{upstream}"
    elif onto is None:
        # `--onto` present with no value at all: nothing to measure.
        return ("outright",)
    else:
        newbase = onto
    # `git rebase [--onto <newbase>] [<upstream>] [<branch>]`: a second
    # operand names the branch being rebased, which git checks out first.
    # Only the CURRENT branch has a pushed tip this module knows about.
    if len(operands) >= 2:
        return ("branch_target", operands[1], newbase)
    return ("target", newbase)


def _pull_is_rebase_flavored(rest: list[str], config_values: list[str]) -> bool:
    """An explicit `-r`/`--rebase[=<v>]`/`--no-rebase` on the `pull` command
    itself wins over a `-c pull.rebase=<v>` global (matching git's own
    precedence); with neither, falls back to the config value. `-r` is git's
    documented short spelling of `--rebase` and takes the same OPTIONAL value
    (`-rfalse`), so it is read through the same parse as the long form — an
    earlier version matched only the long spellings, and `git pull -r origin
    main` measured ALLOW while stranding the pushed tip for real.

    A config FILE's `pull.rebase = true` (as opposed to `-c` on this argv) is
    out of an argv-only rule's reach — the same limitation
    `guard._sequencer_clobbers` already documents."""
    opts, _ = _parse(rest, _PULL_OPTS)
    explicit: bool | None = None
    for name, val in opts:
        if name == "-r" or _abbrev_of(name, "--rebase"):
            explicit = _is_truthy_rebase(val) if val is not None else True
        elif _abbrev_of(name, "--no-rebase"):
            explicit = False
    if explicit is not None:
        return explicit
    return any(
        val.split("=", 1)[1].strip().lower() in _PULL_REBASE_TRUTHY
        for val in config_values
        if val.split("=", 1)[0].strip() == "pull.rebase" and "=" in val
    )


def _is_truthy_rebase(val: str) -> bool:
    return val.strip().lower() not in ("false", "no", "0")


def _classify_reset(rest: list[str]) -> tuple | None:
    _, operands = _parse(rest, _RESET_OPTS)
    if not operands:
        return None
    return ("target", operands[0])


def _classify_commit(rest: list[str]) -> tuple | None:
    opts, _ = _parse(rest, _COMMIT_OPTS)
    if _flag(opts, "--amend"):
        return ("head_parent",)
    return None


def _classify_checkout(rest: list[str]) -> tuple | None:
    # `-B` has no long synonym in git (verified against `git checkout -h`,
    # 2.50.1) — unlike `switch`'s `-C`/`--force-create` below.
    opts, operands = _parse(rest, _CHECKOUT_OPTS)
    name = _value(opts, "-B")
    if name is None:
        return None
    start_point = operands[0] if operands else "HEAD"
    return ("branch_target", name, start_point)


def _classify_switch(rest: list[str]) -> tuple | None:
    opts, operands = _parse(rest, _SWITCH_OPTS)
    # `--force` is `--discard-changes`, a DIFFERENT option that git resolves
    # by exact match — it must never be read as an abbreviation of
    # `--force-create`. Without this exclusion `git switch --force -C
    # <branch> <start-point>` reads `--force` as the force-create (value
    # None) and classifies nothing at all: a real branch reset, ALLOWED.
    exclude = ("--force", "--no-force", "--no-force-create")
    name = _value(opts, "-C", "--force-create", exclude=exclude)
    if name is None:
        return None
    start_point = operands[0] if operands else "HEAD"
    return ("branch_target", name, start_point)


def _classify_branch(rest: list[str]) -> tuple | None:
    opts, operands = _parse(rest, _BRANCH_OPTS)
    if not _flag(opts, "-f", "--force"):
        return None
    if len(operands) < 2:
        return None
    return ("branch_target", operands[0], operands[1])


def _classify_update_ref(rest: list[str]) -> tuple | None:
    opts, operands = _parse(rest, _UPDATE_REF_OPTS)
    if _flag(opts, "--stdin"):
        # `--stdin` reads the actual ref updates from stdin, which this
        # argv-only phase never sees — it could rewrite refs/heads/<branch>
        # to anything with no operand on the command line to inspect.
        # Denying outright (once a pushed tip exists) matches the other
        # OUTRIGHT forms below rather than silently falling through None.
        return ("outright",)
    delete = _flag(opts, "-d", "--delete")
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
        return _classify_rebase(rest)
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
            if name == branch and target_denies(expr):
                return _message(seg, remote, branch, tip)
            continue
        if tag == "branch_outright":
            name = kind[1]
            if name == branch:
                return _message(seg, remote, branch, tip)
            continue
    return None
