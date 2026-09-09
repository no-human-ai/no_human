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
