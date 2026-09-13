"""A coder must never rebase (or hard-reset below) an already-pushed task
branch — the guard refuses it and names the merge alternative.

Incident: a coder ran `git rebase origin/main` / `git rebase --continue` on
a branch whose tip was already pushed, after the harness's base-refresh
skipped a merge for a conflict. The rebase rewrote every commit, so the
pushed remote tip stopped being an ancestor of HEAD; delivery — which only
ever fast-forwards — refused the branch with "remote tip ... is not an
ancestor of the reviewed sha" and the task escalated.

These tests exercise `agent.pushed_tip_guard` through `guard.evaluate` on a
real bare remote: a pushed branch must deny rebase/reset-below-tip and name
the tip; a never-pushed branch must be unaffected.
"""
from __future__ import annotations

import subprocess
import tempfile

import pytest

from no_human.agent import guard, pushed_tip_guard

FORBIDDEN = [".env", "secrets/", "*.key", "*.pem"]
PROTECTED = ["main", "master", "release/*"]

#: An empty, non-git stand-in cwd — mirrors test_guard.py's `_WT`.
_WT = tempfile.mkdtemp(prefix="pushed-tip-guard-wt-")


def _ev(cmd, cwd):
    return guard.evaluate(
        "Bash", {"command": cmd},
        forbidden_paths=FORBIDDEN, never_push_to=PROTECTED, cwd=cwd,
    )


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def origin(tmp_path):
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)],
                    check=True, capture_output=True, text=True)
    return bare


@pytest.fixture
def repo(tmp_path, origin):
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    _git(work, "config", "user.email", "u@e.com")
    _git(work, "config", "user.name", "u")
    (work / "f.txt").write_text("1\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "init")
    _git(work, "remote", "add", "origin", str(origin))
    _git(work, "push", "-q", "origin", "main")
    return work


def _is_ancestor(work, ancestor, descendant):
    return subprocess.run(
        ["git", "merge-base", "--is-ancestor", ancestor, descendant],
        cwd=work, capture_output=True, text=True,
    ).returncode == 0


@pytest.fixture
def harness_repo(tmp_path):
    """Factory fixture: each call builds a fresh bare remote plus a fresh
    two-commit pushed branch (its own `work` dir, its own `origin`), so a
    test that actually EXECUTES a command (mutating the worktree) can call
    this once per table row without one row's mutation bleeding into the
    next. Returns (work_path, pushed_tip_sha)."""
    counter = [0]

    def make():
        counter[0] += 1
        n = counter[0]
        bare = tmp_path / f"origin{n}.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)],
                        check=True, capture_output=True, text=True)
        work = tmp_path / f"work{n}"
        work.mkdir()
        _git(work, "init", "-q", "-b", "main")
        _git(work, "config", "user.email", "u@e.com")
        _git(work, "config", "user.name", "u")
        (work / "f.txt").write_text("1\n")
        _git(work, "add", "-A")
        _git(work, "commit", "-q", "-m", "init")
        _git(work, "remote", "add", "origin", str(bare))
        _git(work, "push", "-q", "origin", "main")
        tip = _make_pushed_branch(work)
        return work, tip

    return make


_DENIED_FORMS = (
    "git rebase origin/main",
    "git rebase --onto origin/main HEAD~1",
    "git rebase -i HEAD~2",
    "git rebase --autostash origin/main",
    "git rebase --continue",
    "git pull --rebase origin main",
    "git -c pull.rebase=true pull origin main",
    "git reset --soft HEAD~1",
    "git reset HEAD~1",
    "git reset --mixed HEAD~1",
    "git reset --hard HEAD~1",
    "git reset --merge HEAD~1",
    "git reset --keep HEAD~1",
    "git reset --soft origin/main",
    "git reset --hard origin/main",
    "git commit --amend",
    "git commit --amend -m x",
    "git checkout -B feature origin/main",
    "git switch -C feature origin/main",
    "git branch -f feature origin/main",
    "git update-ref refs/heads/feature origin/main",
    "git filter-branch -- --all",
)


def test_every_rewrite_form_on_a_pushed_branch_is_denied_naming_the_tip_and_the_merge(
    harness_repo,
):
    for cmd in _DENIED_FORMS:
        work, tip = harness_repo()
        d = _ev(cmd, cwd=str(work))
        assert d.allow is False, f"{cmd!r} should be denied"
        assert d.severity == guard.GUARD_DESTRUCTIVE, f"{cmd!r} -> {d.severity}"
        assert tip in d.reason, f"{cmd!r} -> {d.reason}"
        assert "git merge" in d.reason, f"{cmd!r} -> {d.reason}"

    # The one row whose target is a literal sha rather than a symbolic ref
    # (`origin/main`), built once separately since it needs the base sha.
    work, tip = harness_repo()
    base_sha = _git(work, "rev-parse", "origin/main")
    d = _ev(f"git reset --hard {base_sha}", cwd=str(work))
    assert d.allow is False
    assert d.severity == guard.GUARD_DESTRUCTIVE
    assert tip in d.reason
    assert "git merge" in d.reason


def test_the_reset_to_base_rows_get_the_pushed_tip_message_not_the_generic_one(
    harness_repo,
):
    """Criterion 2: the old `_is_strictly_below_tip` asked "is the target an
    ancestor of the tip" instead of "is the tip an ancestor of the target".
    Those agree whenever the target is a strict ancestor of the tip, so the
    bug only shows once the base has moved on independently — exactly the
    incident's shape (main advanced while the branch was out for review),
    which is what this test reproduces: `origin/main` here is DIVERGED from
    the pushed branch, not a plain ancestor of it. The old (inverted)
    predicate answers "is origin/main an ancestor of tip?" -> False ->
    allow; the correct one answers "is tip an ancestor of origin/main?" ->
    also False -> deny. `git reset --hard origin/main` is the incident's
    exact command."""
    work, tip = harness_repo()
    _git(work, "checkout", "-q", "main")
    (work / "g.txt").write_text("main moved on\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "main advances")
    _git(work, "push", "-q", "origin", "main")
    base_sha = _git(work, "rev-parse", "origin/main")
    _git(work, "checkout", "-q", "feature")

    for cmd in ("git reset --hard origin/main", f"git reset --hard {base_sha}"):
        d = _ev(cmd, cwd=str(work))
        assert d.allow is False, cmd
        assert "destructive git command blocked" not in d.reason, d.reason
        assert "working-tree" not in d.reason, d.reason
        assert tip in d.reason, d.reason


def test_an_unresolvable_reset_target_is_denied_unless_it_is_an_existing_path(
    harness_repo,
):
    """MAJOR from the c4f717d8 review: `target_denies`'s `if not resolved:
    return False` let ANY unresolvable target through — a shell variable or
    a command substitution the guard sees as a literal, un-expanded string
    (`$(git rev-parse HEAD~2)`), not just the legitimate `git reset
    <pathspec>` case. Executed proof of the bug's real-world effect:
    `sh -c 'git reset --soft $(git rev-parse HEAD~2)'` leaves the pushed tip
    NOT an ancestor of the branch (delivery would refuse it), yet the old
    predicate allowed it. The fix: unresolvable denies UNLESS the operand
    names an existing path in the worktree (git's own pathspec fallback for
    a bare `git reset <path>`, which must stay allowed and never moves the
    branch)."""
    work, tip = harness_repo()
    (work / "f.txt").write_text("mutated for the pathspec case\n")

    for cmd in (
        "git reset --soft $(git rev-parse HEAD~2)",
        "git reset --hard $(git rev-parse HEAD~2)",
        "git reset --keep $(git rev-parse HEAD~2)",
        "git reset --merge $(git rev-parse HEAD~2)",
        "git reset --soft $SOME_SHELL_VAR",
    ):
        d = _ev(cmd, cwd=str(work))
        assert d.allow is False, f"{cmd!r} should be denied"
        assert tip in d.reason, f"{cmd!r} -> {d.reason}"
        assert "git merge" in d.reason, f"{cmd!r} -> {d.reason}"

    # Executed proof, not just the static verdict: the shell actually
    # expands the substitution before git ever sees it, and the resulting
    # reset really does strand the tip.
    subprocess.run(
        ["sh", "-c", "git reset --soft $(git rev-parse HEAD~2)"],
        cwd=work, check=True, capture_output=True, text=True,
    )
    assert not _is_ancestor(work, tip, "HEAD")

    # Control: a bare `git reset <existing path>` stays allowed — this is
    # git's own pathspec reading of an unresolvable reset operand, and it
    # never moves the branch.
    work2, _tip2 = harness_repo()
    d = _ev("git reset f.txt", cwd=str(work2))
    assert d.allow is True, d.reason


def test_git_itself_decides_what_a_bare_reset_pathspec_does(harness_repo):
    """The `_is_existing_path` widening this test used to cover (ce2630ba
    review, MINOR-3) claimed a bare `git reset <path>` for a path deleted
    from the working tree but still tracked in the index or HEAD "still only
    touches the index" and so could be safely allowed. Measured on git
    2.50.1 (Apple Git-155): that claim is FALSE. `git reset <path>` (no
    `--`) for a path missing from the *working tree* fails outright —
    `fatal: ambiguous argument '<path>': unknown revision or path not in
    the working tree.` (rc=128) — whether the deletion is staged (`git rm`)
    or not (`rm`), regardless of index/HEAD tracking state. The widening is
    REMOVED; `_is_existing_path` is back to plain `os.path.exists`. This
    test EXECUTES the real git commands (not only the guard's verdict) to
    prove what git actually does in each case.

    `git reset -- <path>` (the `--` spelling) was already allowed
    independently of any widening — `_classify_reset` breaks its operand
    scan at `--`, so there is no operand left to classify — and it really
    does succeed on git for the same missing-path cases.

    A quoted glob (`git reset '*.txt'`, no `--`) is git's own pathspec
    engine matching a literal glob string against the index/worktree, and it
    DOES succeed on git even though nothing with that literal name exists on
    disk. `_is_existing_path`'s disk-only check cannot see that: the guard
    denies it. This is an accepted, documented false-deny (the guard fails
    closed, not open, when it cannot tell) — pinned here so it cannot
    silently regress into a false-ALLOW instead.
    """
    # Case 1: `rm f.txt` (deletion not staged) + bare `git reset f.txt` —
    # f.txt is gone from disk but still present, unmodified, in the index;
    # git still refuses it.
    work, tip = harness_repo()
    (work / "f.txt").unlink()
    proc = subprocess.run(["git", "reset", "f.txt"], cwd=work,
                           capture_output=True, text=True)
    assert proc.returncode == 128, proc.stderr
    assert "ambiguous argument" in proc.stderr, proc.stderr
    d = _ev("git reset f.txt", cwd=str(work))
    assert d.allow is False, d.reason
    assert tip in d.reason, d.reason
    assert _is_ancestor(work, tip, "HEAD")

    # Case 2: `git rm f.txt` (deletion staged) + bare `git reset f.txt` —
    # f.txt is gone from disk AND the index, still present in HEAD; git
    # still refuses it the same way.
    work2, tip2 = harness_repo()
    _git(work2, "rm", "-q", "f.txt")
    proc2 = subprocess.run(["git", "reset", "f.txt"], cwd=work2,
                            capture_output=True, text=True)
    assert proc2.returncode == 128, proc2.stderr
    assert "ambiguous argument" in proc2.stderr, proc2.stderr
    d2 = _ev("git reset f.txt", cwd=str(work2))
    assert d2.allow is False, d2.reason
    assert tip2 in d2.reason, d2.reason
    assert _is_ancestor(work2, tip2, "HEAD")

    # Case 3: the `--` spelling succeeds on git for both deletion shapes,
    # and the guard already allows it independently (no operand to
    # classify).
    work3, tip3 = harness_repo()
    (work3 / "f.txt").unlink()
    proc3 = subprocess.run(["git", "reset", "--", "f.txt"], cwd=work3,
                            capture_output=True, text=True)
    assert proc3.returncode == 0, proc3.stderr
    d3 = _ev("git reset -- f.txt", cwd=str(work3))
    assert d3.allow is True, d3.reason
    assert _is_ancestor(work3, tip3, "HEAD")

    work4, tip4 = harness_repo()
    _git(work4, "rm", "-q", "f.txt")
    proc4 = subprocess.run(["git", "reset", "--", "f.txt"], cwd=work4,
                            capture_output=True, text=True)
    assert proc4.returncode == 0, proc4.stderr
    d4 = _ev("git reset -- f.txt", cwd=str(work4))
    assert d4.allow is True, d4.reason
    assert _is_ancestor(work4, tip4, "HEAD")

    # Case 4: a quoted glob succeeds on git (its own pathspec engine expands
    # it) but the guard's disk-only check cannot see that — an accepted,
    # documented false-deny, pinned so it cannot silently regress into a
    # false-allow.
    work5, tip5 = harness_repo()
    proc5 = subprocess.run(["git", "reset", "*.txt"], cwd=work5,
                            capture_output=True, text=True)
    assert proc5.returncode == 0, proc5.stderr
    d5 = _ev("git reset '*.txt'", cwd=str(work5))
    assert d5.allow is False, (
        f"accepted false-deny expected (guard cannot see git's own "
        f"pathspec glob expansion): {d5.reason}"
    )
    assert tip5 in d5.reason, d5.reason

    # Control: a genuinely unknown path (never tracked, not on disk) fails
    # on git AND is denied by the guard — this fix must not turn into
    # "allow any pathspec".
    work6, tip6 = harness_repo()
    proc6 = subprocess.run(["git", "reset", "never-existed-anywhere.txt"],
                            cwd=work6, capture_output=True, text=True)
    assert proc6.returncode == 128, proc6.stderr
    d6 = _ev("git reset never-existed-anywhere.txt", cwd=str(work6))
    assert d6.allow is False, d6.reason
    assert tip6 in d6.reason, d6.reason


def test_allowed_forms_keep_the_pushed_tip_an_ancestor_of_head(harness_repo):
    # git reset --hard HEAD: a no-op relative to the tip.
    work, tip = harness_repo()
    _git(work, "reset", "--hard", "HEAD")
    assert _is_ancestor(work, tip, "HEAD")

    # git reset --hard <tip sha>: resets exactly to the tip.
    work, tip = harness_repo()
    _git(work, "reset", "--hard", tip)
    assert _is_ancestor(work, tip, "HEAD")

    # git reset --soft <tip sha>: also exactly to the tip.
    work, tip = harness_repo()
    _git(work, "reset", "--soft", tip)
    assert _is_ancestor(work, tip, "HEAD")

    # commit --amend of an UNPUSHED commit made on top of the tip: the tip
    # itself never moves.
    work, tip = harness_repo()
    (work / "f.txt").write_text("unpushed\n")
    _git(work, "commit", "-aq", "-m", "unpushed")
    _git(work, "commit", "-q", "--amend", "-m", "unpushed amended")
    assert _is_ancestor(work, tip, "HEAD")

    # git pull --no-rebase: a merge-flavored pull, ancestry-preserving even
    # though nothing new is actually there to pull.
    work, tip = harness_repo()
    _git(work, "pull", "-q", "--no-rebase", "origin", "feature")
    assert _is_ancestor(work, tip, "HEAD")

    # git status / git log: no state change at all.
    work, tip = harness_repo()
    subprocess.run(["git", "status"], cwd=work, check=True,
                    capture_output=True, text=True)
    subprocess.run(["git", "log", "--oneline", "-1"], cwd=work, check=True,
                    capture_output=True, text=True)
    assert _is_ancestor(work, tip, "HEAD")

    # git rebase --abort / --skip: this MODULE allows them outright (Phase A
    # classifies them None before any subprocess) — they are still denied,
    # with a more specific working-tree message, by
    # guard._git_worktree_denial, so this asserts the module's own verdict
    # directly rather than `evaluate().allow`.
    work, tip = harness_repo()
    for cmd in ("git rebase --abort", "git rebase --skip"):
        assert pushed_tip_guard.denial_reason(
            guard._git_invocations(cmd), str(work)) is None, cmd


def test_a_never_pushed_branch_keeps_every_form_allowed(repo):
    """Control: repeat the whole denied table on a branch that was created
    but never pushed. `_pushed_tip` finds no tracking ref, so this module's
    verdict is None for every row (the generic rules may still deny some of
    them through `evaluate` — that's covered by
    `test_a_never_pushed_branch_is_unaffected` above and is fine; it just
    isn't this module)."""
    _git(repo, "checkout", "-q", "-b", "never-pushed")
    (repo / "f.txt").write_text("2\n")
    _git(repo, "commit", "-aq", "-m", "c2")
    base_sha = _git(repo, "rev-parse", "origin/main")
    forms = tuple(
        cmd.replace("feature", "never-pushed") for cmd in _DENIED_FORMS
    ) + (f"git reset --hard {base_sha}",)
    for cmd in forms:
        assert pushed_tip_guard.denial_reason(
            guard._git_invocations(cmd), str(repo)) is None, cmd


def test_update_ref_stdin_is_denied_outright_on_a_pushed_branch(harness_repo):
    """MINOR-2 from the c4f717d8 review: `--stdin` feeds the actual ref
    updates to git on stdin, invisible to this argv-only phase — a bare
    `git update-ref --stdin` could rewrite refs/heads/<branch> with no
    operand on the command line to inspect. It must not fall through
    unclassified (None); deny outright, like the other OUTRIGHT forms,
    whenever a pushed tip exists."""
    work, tip = harness_repo()
    d = _ev("git update-ref --stdin", cwd=str(work))
    assert d.allow is False
    assert d.severity == guard.GUARD_DESTRUCTIVE
    assert tip in d.reason
    assert "git merge" in d.reason


def test_no_git_subprocess_for_non_rewrite_commands(harness_repo, monkeypatch):
    """Criterion 3: Phase A classifies every one of these from argv alone —
    `_pushed_tip` (and everything after it) must never run for them."""
    work, _tip = harness_repo()
    real_run = subprocess.run
    calls = {"n": 0}

    def counting_run(*args, **kwargs):
        calls["n"] += 1
        return real_run(*args, **kwargs)

    monkeypatch.setattr(pushed_tip_guard.subprocess, "run", counting_run)

    for cmd in (
        "git status",
        "git diff",
        "git log --oneline -5",
        "git -C . show HEAD",
        "git add -A && git commit -m x",
    ):
        calls["n"] = 0
        _ev(cmd, cwd=str(work))
        assert calls["n"] == 0, f"{cmd!r} triggered {calls['n']} subprocess call(s)"

    # Positive control: a genuinely relevant command must still reach Phase
    # B, or this test would pass vacuously even with the stub miswired.
    calls["n"] = 0
    _ev("git rebase origin/main", cwd=str(work))
    assert calls["n"] > 0


def _make_pushed_branch(work, name="feature"):
    """A branch with two of its own commits, both pushed — HEAD == the
    pushed remote tip, exactly like a coder that just pushed and is now
    about to (wrongly) rebase or reset back below that tip."""
    _git(work, "checkout", "-q", "-b", name)
    (work / "f.txt").write_text("2\n")
    _git(work, "commit", "-aq", "-m", "c2")
    (work / "f.txt").write_text("3\n")
    _git(work, "commit", "-aq", "-m", "c3")
    _git(work, "push", "-q", "-u", "origin", name)
    tip = _git(work, "rev-parse", name)
    return tip


def test_rebase_onto_the_base_is_denied_naming_the_pushed_tip(repo):
    tip = _make_pushed_branch(repo)
    d = _ev("git rebase origin/main", cwd=str(repo))
    assert d.allow is False
    assert d.severity == guard.GUARD_DESTRUCTIVE
    assert tip in d.reason
    assert "git merge" in d.reason


def test_rebase_continue_is_denied_on_a_pushed_branch(repo):
    tip = _make_pushed_branch(repo)
    d = _ev("git rebase --continue", cwd=str(repo))
    assert d.allow is False
    assert d.severity == guard.GUARD_DESTRUCTIVE
    assert tip in d.reason
    assert "git merge" in d.reason


def test_a_hard_reset_below_the_pushed_tip_names_the_tip(repo):
    tip = _make_pushed_branch(repo)
    d = _ev("git reset --hard HEAD~1", cwd=str(repo))
    assert d.allow is False
    assert tip in d.reason
    assert "git merge" in d.reason


def test_a_never_pushed_branch_is_unaffected(repo):
    _git(repo, "checkout", "-q", "-b", "never-pushed")
    (repo / "f.txt").write_text("2\n")
    _git(repo, "commit", "-aq", "-m", "c2")
    (repo / "f.txt").write_text("3\n")
    _git(repo, "commit", "-aq", "-m", "c3")

    d = _ev("git rebase origin/main", cwd=str(repo))
    assert d.allow is True

    d2 = _ev("git reset --hard HEAD~1", cwd=str(repo))
    assert d2.allow is False
    assert d2.severity == guard.GUARD_DESTRUCTIVE
    # still denied (by the pre-existing, more generic rule), but this rule
    # never fired: the never-pushed branch has no tip to name.
    assert "pushed" not in d2.reason
    tip = _git(repo, "rev-parse", "HEAD")
    assert tip not in d2.reason


def test_the_rule_fails_open_outside_a_repo_and_with_no_cwd():
    d = guard.evaluate("Bash", {"command": "git rebase origin/main"},
                       forbidden_paths=FORBIDDEN, never_push_to=PROTECTED,
                       cwd=None)
    assert d.allow is True

    d2 = guard.evaluate("Bash", {"command": "git rebase origin/main"},
                        forbidden_paths=FORBIDDEN, never_push_to=PROTECTED,
                        cwd=_WT)
    assert d2.allow is True


def test_wrapped_and_compound_spellings_are_caught(repo):
    tip = _make_pushed_branch(repo)
    for cmd in (
        'bash -lc "git rebase origin/main"',
        "cd . && git rebase --continue",
        "git -C . rebase origin/main",
    ):
        d = _ev(cmd, cwd=str(repo))
        assert d.allow is False, f"{cmd!r} should be denied"
        assert tip in d.reason, f"{cmd!r} -> {d.reason}"


def test_rebase_continue_is_denied_while_a_rebase_is_actually_in_progress(repo):
    """The incident's real sequence: `git rebase origin/main` (already
    denied by the tests above, but a coder could bypass a single check, or
    this guard could regress) leaves the repo mid-rebase with a conflict —
    HEAD detached, `.git/rebase-merge` or `.git/rebase-apply` present — and
    *then* `git rebase --continue` runs. `_current_branch`'s plain
    `symbolic-ref` fails on the detached HEAD; the guard must still resolve
    the branch (and its pushed tip) via the on-disk rebase state, or this
    exact command — the one named in the incident and the ticket — is
    let through although the branch was pushed."""
    tip = _make_pushed_branch(repo)

    # Advance origin/main with a change that conflicts with the pushed
    # branch's own edits to the same file.
    _git(repo, "checkout", "-q", "main")
    (repo / "f.txt").write_text("main-conflict\n")
    _git(repo, "commit", "-aq", "-m", "main change")
    _git(repo, "push", "-q", "origin", "main")
    _git(repo, "checkout", "-q", "feature")

    result = subprocess.run(
        ["git", "rebase", "origin/main"], cwd=str(repo),
        capture_output=True, text=True,
    )
    assert result.returncode != 0, (
        "setup expected a conflicting rebase, got: "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    # Confirm the setup actually reached the state this test is about:
    # HEAD detached, mid-rebase.
    branch_ref = subprocess.run(
        ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
        cwd=str(repo), capture_output=True, text=True,
    )
    assert branch_ref.returncode != 0, "expected a detached HEAD mid-rebase"

    d = _ev("git rebase --continue", cwd=str(repo))
    assert d.allow is False
    assert d.severity == guard.GUARD_DESTRUCTIVE
    assert tip in d.reason
    assert "git merge" in d.reason


def test_a_detached_head_and_a_bare_reset_hard_fall_through(repo):
    _make_pushed_branch(repo)
    _git(repo, "checkout", "-q", "--detach", "HEAD")

    d = _ev("git rebase origin/main", cwd=str(repo))
    assert d.allow is True

    d2 = _ev("git reset --hard", cwd=str(repo))
    assert d2.allow is False
    assert "working-tree" in d2.reason


# --------------------------------------------------------------------------- #
# Five additional properties, re-cut onto the current tree: the documented
# detached-HEAD gap is real and bounded (not wider or narrower than the
# module docstring claims), both rebase backends are covered, git's own
# fast-forward refusal is exercised for real, the wind-back forms are told
# apart from --autostash by EXECUTING each rather than trusting the comment,
# and the module's runner coverage is proven identical to guard.py's own
# `_FORGE_RUNNER_NAMES` at runtime — the actual set `_git_invocations`
# recurses into (`_SHELL_RUNNERS | _TRAILING_ARGV_RUNNERS`, 18 names), not
# the narrower 13-name `_SHELL_RUNNERS` alone and not
# venv_install_guard's separate 8-name set the historical narrowing bug
# substituted.
# --------------------------------------------------------------------------- #


def test_the_detached_head_gap_is_real_and_the_attached_spelling_is_denied(repo):
    """Module docstring: `git checkout --detach HEAD` (classifies None — it
    doesn't touch the current branch) followed, in a SEPARATE Bash call once
    HEAD is detached, by `git branch -f <branch> <target>` evades this
    module — `_current_branch`'s `symbolic-ref` no longer names the branch
    for `_classify_branch`'s comparison to match against. The exact same
    `branch -f` run while still ATTACHED to the branch is still denied.
    Pins both halves so the documented gap cannot silently widen (denying
    more than documented) or narrow (denying the detached case too, which
    would contradict the docstring) without this test catching it — and
    executes the real rewrite in the detached case to show the gap's actual
    consequence, not just the guard's None verdict."""
    tip = _make_pushed_branch(repo)
    base_sha = _git(repo, "rev-parse", "origin/main")

    # Attached: denied.
    d = _ev(f"git branch -f feature {base_sha}", cwd=str(repo))
    assert d.allow is False
    assert tip in d.reason
    assert "git merge" in d.reason

    # Detach, then the identical command: this module specifically stands
    # down (isolated from the rest of the guard by calling the module
    # directly rather than `evaluate()`).
    _git(repo, "checkout", "-q", "--detach", "HEAD")
    branch_ref = subprocess.run(
        ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
        cwd=str(repo), capture_output=True, text=True,
    )
    assert branch_ref.returncode != 0, "expected a detached HEAD"

    assert pushed_tip_guard.denial_reason(
        guard._git_invocations(f"git branch -f feature {base_sha}"),
        str(repo),
    ) is None

    # And the rewrite really does happen: real git executed, not merely the
    # guard's verdict — the branch now points below what was its pushed tip.
    _git(repo, "branch", "-f", "feature", base_sha)
    assert _git(repo, "rev-parse", "feature") == base_sha
    assert not _is_ancestor(repo, tip, "feature")


def test_both_rebase_backends_record_the_head_name_the_guard_reads(harness_repo):
    """`_rebase_head_name` reads `rebase-merge/head-name` OR
    `rebase-apply/head-name` — git has two rebase backends (the default
    "merge"/ort backend and the legacy `--apply` patch backend) and only the
    merge backend was exercised by
    `test_rebase_continue_is_denied_while_a_rebase_is_actually_in_progress`.
    Runs a real conflicting rebase under each backend and confirms (a) git
    itself lands in the state directory this function reads, with the
    expected `refs/heads/<branch>` content, and (b) the guard still resolves
    the branch and denies `--continue` in both."""
    import os

    for flag, state_dir in (("--merge", "rebase-merge"), ("--apply", "rebase-apply")):
        work, tip = harness_repo()
        _git(work, "checkout", "-q", "main")
        (work / "f.txt").write_text("main-conflict\n")
        _git(work, "commit", "-aq", "-m", "main change")
        _git(work, "push", "-q", "origin", "main")
        _git(work, "checkout", "-q", "feature")

        result = subprocess.run(
            ["git", "rebase", flag, "origin/main"], cwd=str(work),
            capture_output=True, text=True,
        )
        assert result.returncode != 0, (
            f"{flag} setup expected a conflicting rebase, got: "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )

        head_name_path = os.path.join(str(work), ".git", state_dir, "head-name")
        assert os.path.exists(head_name_path), (
            f"{flag} rebase did not record state under {state_dir}/ "
            f"(stdout={result.stdout!r} stderr={result.stderr!r})"
        )
        with open(head_name_path, encoding="utf-8") as f:
            assert f.read().strip() == "refs/heads/feature"

        d = _ev("git rebase --continue", cwd=str(work))
        assert d.allow is False, flag
        assert d.severity == guard.GUARD_DESTRUCTIVE, flag
        assert tip in d.reason, f"{flag} -> {d.reason}"
        assert "git merge" in d.reason, f"{flag} -> {d.reason}"

        subprocess.run(["git", "rebase", "--abort"], cwd=str(work),
                        capture_output=True, text=True)


def test_git_itself_refuses_to_fast_forward_a_rewritten_pushed_branch(repo):
    """The stakes named in the module docstring, executed for real: after a
    rewrite (simulated here by resetting below the pushed tip, bypassing the
    guard on purpose to reach the state a coder would if the guard did not
    exist), a plain `git push` — no `--force` — is refused by git itself
    with a non-fast-forward error: the exact failure this module exists to
    keep a coder from ever reaching. This is real git output, not the
    guard's own reasoning about what git would do."""
    tip = _make_pushed_branch(repo)
    _git(repo, "reset", "--hard", "HEAD~1")
    assert _git(repo, "rev-parse", "HEAD") != tip

    result = subprocess.run(
        ["git", "push", "origin", "feature"], cwd=str(repo),
        capture_output=True, text=True,
    )
    assert result.returncode != 0, (
        "expected git to refuse a non-fast-forward push, got: "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert (
        "non-fast-forward" in result.stderr
        or "fetch first" in result.stderr
        or "rejected" in result.stderr
    ), result.stderr

    # And independent of git's own refusal: the guard would have denied the
    # `git reset --hard HEAD~1` that got us here, had it run first — the
    # pushed tip is read from the remote-tracking ref, unaffected by the
    # local reset that already happened above.
    d = pushed_tip_guard.denial_reason(
        guard._git_invocations("git reset --hard HEAD~1"), str(repo))
    assert d is not None
    assert tip in d


def test_the_wind_back_forms_really_undo_the_rewrite_and_autostash_starts_one(
    harness_repo,
):
    """`_REBASE_WIND_BACK`'s comment: --abort/--skip undo a rewrite in
    progress (so this module must not deny them — they're already denied,
    with a more specific message, by `guard._sequencer_clobbers`), while
    --autostash does NOT undo anything — it STARTS a rewrite, so it must
    stay denied. Executes real git for both halves rather than trusting the
    comment."""
    # --abort really does restore the pre-rebase tip.
    work, tip = harness_repo()
    _git(work, "checkout", "-q", "main")
    (work / "f.txt").write_text("main-conflict\n")
    _git(work, "commit", "-aq", "-m", "main change")
    _git(work, "push", "-q", "origin", "main")
    _git(work, "checkout", "-q", "feature")
    subprocess.run(["git", "rebase", "origin/main"], cwd=str(work),
                    capture_output=True, text=True)
    assert pushed_tip_guard.denial_reason(
        guard._git_invocations("git rebase --abort"), str(work)) is None
    subprocess.run(["git", "rebase", "--abort"], cwd=str(work),
                    capture_output=True, text=True)
    assert _git(work, "rev-parse", "feature") == tip
    assert _is_ancestor(work, tip, "feature")

    # --skip: same non-denial classification (Phase A only — this half
    # doesn't need to actually skip a commit to prove the classification).
    assert pushed_tip_guard.denial_reason(
        guard._git_invocations("git rebase --skip"), str(work)) is None

    # --autostash: denied, and executing the plain (unguarded) form for real
    # on a branch that genuinely diverges from main shows why it must stay
    # denied — unlike --abort/--skip, it actually moves the branch.
    work2, tip2 = harness_repo()
    _git(work2, "checkout", "-q", "main")
    (work2 / "g.txt").write_text("main advances, no conflict\n")
    _git(work2, "add", "-A")
    _git(work2, "commit", "-q", "-m", "main advances")
    _git(work2, "push", "-q", "origin", "main")
    _git(work2, "checkout", "-q", "feature")

    d = _ev("git rebase --autostash origin/main", cwd=str(work2))
    assert d.allow is False
    assert tip2 in d.reason
    assert "git merge" in d.reason

    (work2 / "f.txt").write_text("dirty, needs the autostash\n")
    result = subprocess.run(
        ["git", "rebase", "--autostash", "origin/main"], cwd=str(work2),
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert not _is_ancestor(work2, tip2, "HEAD"), (
        "the pushed tip is still an ancestor after --autostash — the setup "
        "did not actually produce a rewrite, so this test proves nothing"
    )


def test_the_pushed_tip_path_sees_every_runner_the_guard_knows(harness_repo):
    """The historical bug this guards against: a prior attempt at this exact
    fix read `venv_install_guard._SHELL_RUNNERS` (8 names) instead of
    `guard._SHELL_RUNNERS` (13 names), silently losing eval/flock/nice/
    script/stdbuf/timeout/watch/xargs as live bypasses. This module never
    derives its own runner list — it consumes `guard._git_invocations`,
    which (per `guard.py`'s own `_git_invocations`, read at
    `guard.py:2577` — `elif name in _FORGE_RUNNER_NAMES`) recurses into
    every name in `guard._FORGE_RUNNER_NAMES`, the union of
    `_SHELL_RUNNERS` (13) and `_TRAILING_ARGV_RUNNERS` (12, overlapping
    `_SHELL_RUNNERS` on xargs/timeout/nice/stdbuf/script/flock/watch), for
    18 names total — five more than `_SHELL_RUNNERS` alone
    (chrt/ionice/setsid/taskset/unbuffer). A prior version of this test
    asserted only against `_SHELL_RUNNERS` and a 13-row wrap table; that
    table actively forbade covering the five runners the guard really
    recurses into, so it would not have caught a regression that widened
    or narrowed that set. This test pins the full 18 AT RUNTIME (not a
    hardcoded literal copy) and confirms every single one denies a
    rewrite wrapped inside it."""
    from no_human.agent import venv_install_guard

    # The runner set actually consulted by `_git_invocations` (and
    # therefore by this module) must be the full 18-name
    # `_FORGE_RUNNER_NAMES`, not the narrower 13-name `_SHELL_RUNNERS` and
    # not the even narrower 8 the historical bug substituted.
    assert guard._SHELL_RUNNERS == frozenset({
        "sh", "bash", "zsh", "dash", "ksh", "eval", "xargs", "timeout",
        "nice", "stdbuf", "script", "watch", "flock",
    })
    assert guard._SHELL_RUNNERS <= guard._FORGE_RUNNER_NAMES
    assert guard._FORGE_RUNNER_NAMES == frozenset({
        "sh", "bash", "zsh", "dash", "ksh", "eval", "xargs", "timeout",
        "nice", "stdbuf", "script", "watch", "flock",
        "ionice", "chrt", "setsid", "unbuffer", "taskset",
    })
    # venv_install_guard's set is not a subset of guard's (it separately
    # carries Windows-shell names — cmd/powershell/pwsh — that are out of
    # scope for a POSIX git-rewrite check); what matters is that the eight
    # names the historical bug actually lost are present in guard's set and
    # absent from venv_install_guard's.
    lost_by_the_historical_bug = frozenset({
        "eval", "flock", "nice", "script", "stdbuf", "timeout", "watch",
        "xargs",
    })
    assert lost_by_the_historical_bug <= guard._FORGE_RUNNER_NAMES
    assert lost_by_the_historical_bug.isdisjoint(venv_install_guard._SHELL_RUNNERS), (
        "the eight names the historical bug lost must be genuinely absent "
        "from venv_install_guard._SHELL_RUNNERS, or this test would not "
        "reproduce the bug it is named for"
    )

    wrap = {
        "sh": 'sh -c "git rebase origin/main"',
        "bash": 'bash -c "git rebase origin/main"',
        "zsh": 'zsh -c "git rebase origin/main"',
        "dash": 'dash -c "git rebase origin/main"',
        "ksh": 'ksh -c "git rebase origin/main"',
        "eval": 'eval "git rebase origin/main"',
        "xargs": "xargs git rebase origin/main",
        "timeout": "timeout 30 git rebase origin/main",
        "nice": "nice -n5 git rebase origin/main",
        "stdbuf": "stdbuf -oL git rebase origin/main",
        "script": "script -q /dev/null git rebase origin/main",
        "watch": "watch -n1 git rebase origin/main",
        "flock": "flock /tmp/pushed-tip-guard-test.lock git rebase origin/main",
        "ionice": "ionice -c2 git rebase origin/main",
        "chrt": "chrt -f 1 git rebase origin/main",
        "setsid": "setsid git rebase origin/main",
        "unbuffer": "unbuffer git rebase origin/main",
        "taskset": "taskset 1 git rebase origin/main",
    }
    assert set(wrap) == guard._FORGE_RUNNER_NAMES, (
        "this table must cover exactly guard._FORGE_RUNNER_NAMES — the set "
        "guard._git_invocations actually recurses into — not a hand-copied "
        "subset of it"
    )

    for runner, cmd in wrap.items():
        work, tip = harness_repo()
        d = _ev(cmd, cwd=str(work))
        assert d.allow is False, f"runner {runner!r} ({cmd!r}) was not denied"
        assert tip in d.reason, f"runner {runner!r} -> {d.reason}"
        assert "git merge" in d.reason, f"runner {runner!r} -> {d.reason}"
