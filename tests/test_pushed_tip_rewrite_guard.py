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

from no_human.agent import guard

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
