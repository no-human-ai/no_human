"""The protected-branch rule's second enforcement point.

`agent/guard.py` matches strings on the command line the agent PROPOSED. That
cannot resolve shell expansion, so `git push origin $(echo main)` carried no
token reading as `main` and was allowed straight through to a real push. The
coder runs with `permission_mode="bypassPermissions"`, so on the Bash path that
matcher was the only gate — `GitRepo.push`'s `ProtectedBranch` check is never
reached by an agent shelling out to `git`.

These tests cover the fix: a `pre-push` hook installed into every agent
worktree, which reads the refspec git ALREADY RESOLVED, plus the lexical half
that refuses an argv it cannot resolve.

The push tests drive REAL git against a REAL bare remote. A mocked git would
prove nothing here: the whole claim is about what git itself does with hooks in
a linked worktree, which is exactly the part a mock would assume away.
"""

import subprocess

import pytest

from no_human.agent import guard
from no_human.config import load_config
from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task
from no_human.notify.slack import SlackNotifier
from no_human.vcs.git import GitRepo
from no_human.vcs.push_hook import (
    PATTERNS_FILENAME,
    PushHookError,
    hook_dir_for,
    install_pre_push_guard,
)

PROTECTED = ["main", "master", "release/*"]


def _git(cwd, *args, check=True):
    return subprocess.run(
        ["git", "-c", "user.email=t@t.t", "-c", "user.name=t", *args],
        cwd=str(cwd), capture_output=True, text=True, check=check,
    )


@pytest.fixture
def repo_with_remote(tmp_path):
    """An upstream checkout on `main`, a bare remote, and an agent worktree
    created through the product's own `GitRepo.add_worktree`."""
    remote = tmp_path / "remote.git"
    up = tmp_path / "upstream"
    _git(tmp_path, "init", "-q", "--bare", str(remote))
    _git(tmp_path, "init", "-q", "-b", "main", str(up))
    (up / "README.md").write_text("hello\n")
    _git(up, "add", ".")
    _git(up, "commit", "-qm", "init")
    _git(up, "remote", "add", "origin", str(remote))
    _git(up, "push", "-q", "origin", "main")

    wt_path = tmp_path / "agent-wt"
    main_repo = GitRepo(up, never_push_to=list(PROTECTED))
    wt = main_repo.add_worktree(wt_path, base="main", detach=True)
    _git(wt_path, "checkout", "-q", "-b", "no-human/task-1")
    (wt_path / "README.md").write_text("hello\nagent\n")
    _git(wt_path, "commit", "-qam", "agent work")
    # Advance local main so a push of it is a REAL ref update: git short-circuits
    # an up-to-date push before it ever runs pre-push, which would make every
    # assertion below pass for the wrong reason.
    (up / "README.md").write_text("hello\nmoved\n")
    _git(up, "commit", "-qam", "advance main")
    return {"remote": remote, "up": up, "wt": wt_path, "repo": wt}


# --------------------------------------------------------------------------- #
# The hook itself, against real git.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("push_args", [
    # The reported bypass, in each of the forms that defeat string matching.
    ["push", "origin", "$(echo main)"],
    ["push", "origin", "`echo main`"],
    ["push", "--force", "origin", "HEAD:refs/heads/$(printf main)"],
    ["push", "origin", "--delete", "$(echo main)"],
], ids=["command-subst", "backticks", "hidden-refspec", "delete-main"])
def test_expanded_push_to_protected_branch_is_refused_by_git(
    repo_with_remote, push_args,
):
    """The command line names no protected branch; the RESOLVED ref does."""
    env = repo_with_remote
    before = _git(env["remote"], "rev-parse", "refs/heads/main").stdout.strip()

    # `sh -c` so the shell really performs the expansion, exactly as it would
    # under the agent's Bash tool.
    proc = subprocess.run(
        ["sh", "-c", "git " + " ".join(push_args)],
        cwd=str(env["wt"]), capture_output=True, text=True,
    )

    assert proc.returncode != 0, (
        f"push was NOT refused: {proc.stdout}\n{proc.stderr}"
    )
    assert "no_human pre-push guard: REFUSED" in proc.stderr, proc.stderr
    assert "refs/heads/main" in proc.stderr, proc.stderr
    after = _git(env["remote"], "rev-parse", "refs/heads/main").stdout.strip()
    assert after == before, "protected branch on the remote was modified"


def test_agent_can_still_push_its_own_task_branch(repo_with_remote):
    """The product's normal path. A guard that blocked this would be useless."""
    env = repo_with_remote
    proc = _git(env["wt"], "push", "-u", "origin", "no-human/task-1", check=False)
    assert proc.returncode == 0, f"legitimate push refused: {proc.stderr}"
    listed = _git(env["remote"], "rev-parse", "refs/heads/no-human/task-1")
    assert listed.stdout.strip(), "task branch did not reach the remote"

    # And through the product's own API, which is what actually runs in a task.
    (env["wt"] / "README.md").write_text("hello\nagent\nmore\n")
    _git(env["wt"], "commit", "-qam", "more")
    assert env["repo"].push(branch="no-human/task-1")


def test_hook_fails_closed_when_the_pattern_list_is_unreadable(repo_with_remote):
    """"Cannot determine the config" must mean refuse, not allow."""
    env = repo_with_remote
    (hook_dir_for(env["wt"]) / PATTERNS_FILENAME).unlink()
    proc = _git(env["wt"], "push", "origin", "no-human/task-1", check=False)
    assert proc.returncode != 0, "push allowed with no protected-branch list"
    assert "fails closed" in proc.stderr, proc.stderr


def test_hook_is_installed_outside_the_agents_working_tree(repo_with_remote):
    """It lives in the worktree admin dir, so a `git clean`, a branch switch or
    the agent rewriting its checkout cannot quietly remove the guard."""
    env = repo_with_remote
    hooks = hook_dir_for(env["wt"])
    assert hooks.is_dir()
    assert env["wt"] not in hooks.parents, (
        f"{hooks} is inside the agent's working tree"
    )


def test_pattern_list_comes_from_config_not_a_second_hardcoded_copy(tmp_path):
    """The hook must read the same `never_push_to` the guard is given. Passing
    a custom list must change what the hook blocks — if `main` were baked into
    the hook this test would fail."""
    up = tmp_path / "up"
    _git(tmp_path, "init", "-q", "-b", "main", str(up))
    (up / "f").write_text("x")
    _git(up, "add", ".")
    _git(up, "commit", "-qm", "init")
    wt = tmp_path / "wt"
    GitRepo(up, never_push_to=["sacred/*"]).add_worktree(
        wt, base="main", detach=True)
    text = (hook_dir_for(wt) / PATTERNS_FILENAME).read_text()
    assert "sacred/*" in text
    assert "\nmain\n" not in text, "hook has its own hardcoded branch list"


def test_primary_checkout_hooks_are_left_alone(repo_with_remote):
    """The shared `.git/hooks` is the human's. Installing there would clobber
    their hooks and block their own pushes to main."""
    env = repo_with_remote
    assert not (env["up"] / ".git" / "hooks" / "pre-push").exists()
    effective = _git(env["up"], "config", "--get", "core.hooksPath", check=False)
    assert not effective.stdout.strip(), (
        "core.hooksPath leaked into the primary checkout"
    )
    # The human can still push main from their own checkout.
    proc = _git(env["up"], "push", "origin", "main", check=False)
    assert proc.returncode == 0, f"human's push to main was blocked: {proc.stderr}"


def test_existing_repository_hooks_still_fire_in_the_agent_worktree(tmp_path):
    """Overriding core.hooksPath must not silently disable a repo's own hooks
    (husky, pre-commit) inside agent worktrees."""
    up = tmp_path / "up"
    _git(tmp_path, "init", "-q", "-b", "main", str(up))
    (up / "f").write_text("x")
    _git(up, "add", ".")
    _git(up, "commit", "-qm", "init")
    hook = up / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho REPO-PRE-COMMIT-RAN >&2\nexit 1\n")
    hook.chmod(0o755)

    wt = tmp_path / "wt"
    GitRepo(up, never_push_to=list(PROTECTED)).add_worktree(
        wt, base="main", detach=True)
    (wt / "f").write_text("y")
    proc = _git(wt, "commit", "-qam", "x", check=False)
    assert "REPO-PRE-COMMIT-RAN" in proc.stderr, (
        f"the repository's own pre-commit hook was disabled: {proc.stderr}"
    )


def test_install_refuses_when_core_worktree_would_leak(tmp_path):
    """`core.worktree` in the shared config changes meaning once
    extensions.worktreeConfig is on. Refuse rather than corrupt the repo."""
    up = tmp_path / "up"
    _git(tmp_path, "init", "-q", "-b", "main", str(up))
    (up / "f").write_text("x")
    _git(up, "add", ".")
    _git(up, "commit", "-qm", "init")
    wt = tmp_path / "wt"
    _git(up, "worktree", "add", "-q", "--detach", str(wt), "main")
    _git(up, "config", "core.worktree", str(up))
    with pytest.raises(PushHookError, match="core.worktree"):
        install_pre_push_guard(wt, PROTECTED)


# --------------------------------------------------------------------------- #
# `Orchestrator._protect_base_branch` reaching the hook, not just the guard.
# --------------------------------------------------------------------------- #


def _orch(tmp_path, never_push_to=PROTECTED):
    initial = list(never_push_to)

    class _Backend:
        model = "claude-sonnet-5"
        never_push_to = initial

    cfg = load_config(tmp_path / "config.yaml")
    return Orchestrator(None, cfg.data, _Backend(), SlackNotifier(None))


def test_protect_base_branch_hook_covers_base(repo_with_remote, tmp_path):
    """AC #2. `_protect_base_branch(base="develop", repo=...)` must not only
    extend the lexical guard's `never_push_to` — it must reach the installed
    pre-push hook's pattern file too, closing the hole where `git config
    remote.origin.push HEAD:refs/heads/develop` + a bare `git push origin`
    carries no branch-name token for the lexical guard to see at all."""
    env = repo_with_remote
    orch = _orch(tmp_path)
    task = Task.new("do a thing", repo_path=str(env["up"]))

    orch._protect_base_branch(task, "develop", repo=env["repo"])

    text = (hook_dir_for(env["wt"]) / PATTERNS_FILENAME).read_text()
    assert "develop" in text.splitlines(), text
    assert "develop" in orch.backend.never_push_to

    proc = _git(env["wt"], "push", "origin", "HEAD:refs/heads/develop", check=False)
    assert proc.returncode != 0, f"push to develop was NOT refused: {proc.stderr}"
    assert "no_human pre-push guard: REFUSED" in proc.stderr, proc.stderr

    # Negative control: the task's own branch still pushes fine.
    proc2 = _git(env["wt"], "push", "origin", "HEAD:refs/heads/no-human/x",
                 check=False)
    assert proc2.returncode == 0, f"legitimate push refused: {proc2.stderr}"

    # The primary checkout was never touched by this refresh.
    effective = _git(env["up"], "config", "--get", "core.hooksPath", check=False)
    assert not effective.stdout.strip(), (
        "core.hooksPath leaked into the primary checkout"
    )


def test_protect_base_branch_without_repo_is_a_noop(repo_with_remote, tmp_path):
    """Legacy 2-arg call (`repo` omitted) must keep working exactly as
    before — no hook pattern file touched, no exception — since not every
    caller has a `GitRepo` handy at this call site."""
    env = repo_with_remote
    orch = _orch(tmp_path)
    task = Task.new("do a thing", repo_path=str(env["up"]))
    patterns = hook_dir_for(env["wt"]) / PATTERNS_FILENAME
    before = patterns.read_text()

    orch._protect_base_branch(task, "develop")

    after = patterns.read_text()
    assert before == after, "the hook's pattern file must not change with no repo given"
    assert "develop" in orch.backend.never_push_to


# --------------------------------------------------------------------------- #
# The lexical half: defence in depth above the hook.
# --------------------------------------------------------------------------- #

def _decide(cmd):
    return guard.evaluate(
        "Bash", {"command": cmd}, forbidden_paths=[], never_push_to=PROTECTED,
    )


@pytest.mark.parametrize("cmd", [
    "git push origin $(echo main)",
    "B=main; git push origin $B",
    "git push origin `echo main`",
    'sh -c "git push origin $B"',
    "cd /tmp && git push origin $TARGET",
])
def test_unresolvable_push_argv_is_denied(cmd):
    """A push whose branch comes out of shell expansion cannot be resolved
    lexically, so the guard refuses instead of guessing."""
    d = _decide(cmd)
    assert not d.allow, f"allowed an unresolvable push: {cmd}"
    assert "literal branch name" in d.reason, d.reason


@pytest.mark.parametrize("cmd", [
    "git push --no-verify origin no-human/task-1",
    "git -c core.hooksPath=/dev/null push origin no-human/task-1",
])
def test_push_that_disarms_the_hook_is_denied(cmd):
    """`--no-verify` and a relocated hooksPath turn the enforcement point below
    this one off. A push that does that is refused here."""
    d = _decide(cmd)
    assert not d.allow, f"allowed a hook-disarming push: {cmd}"
    assert "pre-push hook" in d.reason, d.reason


@pytest.mark.parametrize("cmd", [
    "git -c color.ui=false push origin main",
    "git --no-pager push origin main",
])
def test_literal_protected_branch_behind_git_options_is_denied(cmd):
    """The old `\\bgit\\s+push\\b` trigger required the two words to be
    adjacent, so any global git option before `push` skipped the check
    entirely — with a plain literal `main` and no shell trickery at all."""
    d = _decide(cmd)
    assert not d.allow, f"allowed a literal push to main: {cmd}"


@pytest.mark.parametrize("cmd", [
    "git push origin no-human/task-abc123",
    "git push -u origin no-human/fix-thing",
    "git push origin HEAD:refs/heads/no-human/x",
    "git push -n origin no-human/x",          # -n is --dry-run, not --no-verify
    'grep -rn "push" $PWD/src',               # `$` outside any git push argv
    "git log --oneline | head -5",
    "echo $HOME",
])
def test_legitimate_commands_still_allowed(cmd):
    """The fail-closed rules must not swallow the product's normal path, nor
    ordinary shell work that merely contains a `$`."""
    d = _decide(cmd)
    assert d.allow, f"denied a legitimate command: {cmd} -> {d.reason}"
