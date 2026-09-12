"""The gate that stops a supervising session silently taking over a task branch.

Measured 2026-09-12: task 7ee2d939's attempt 5 died with "commit attribution
mismatch" because a supervising session had hand-finished its branch, and the
task went to BUDGET_EXHAUSTED having already passed review on attempt 4. Three
more live task branches were contaminated the same night before anyone noticed.

Every test here drives the real script against a real git repository. The
fixture commits in a DETACHED head on purpose: the supervising session works in
detached worktrees, so a gate keyed on the branch NAME would miss the only shape
that actually causes the damage.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

GATE = Path(__file__).resolve().parents[1] / "scripts" / "precommit_foreign_branch_gate.py"

AGENT = ("no_human", "no-human@users.noreply.github.com")
SUPERVISOR = ("eyalgolan", "5146175+eyalgolan@users.noreply.github.com")


def _git(cwd: Path, *args: str, who: tuple[str, str] = AGENT) -> None:
    name, email = who
    env = {
        "GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
        "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email,
        "PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(cwd),
    }
    subprocess.run(("git",) + args, cwd=cwd, env=env, check=True,
                   capture_output=True, text=True)


def _run_gate(cwd: Path, who: tuple[str, str], *, override: bool = False):
    name, email = who
    env = {
        "GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
        "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email,
        "PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(cwd),
    }
    if override:
        env["NH_ALLOW_FOREIGN_BRANCH_COMMIT"] = "1"
    return subprocess.run([sys.executable, str(GATE)], cwd=cwd, env=env,
                          capture_output=True, text=True)


@pytest.fixture
def agent_branch(tmp_path: Path) -> Path:
    """A repo whose HEAD is two AGENT commits ahead of origin/main, detached."""
    bare, work = tmp_path / "origin.git", tmp_path / "work"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    subprocess.run(["git", "init", "-q", str(work)], check=True)
    _git(work, "remote", "add", "origin", str(bare))
    _git(work, "commit", "-q", "--allow-empty", "-m", "base")
    _git(work, "push", "-q", "origin", "HEAD:main")
    _git(work, "fetch", "-q", "origin")
    _git(work, "checkout", "-q", "--detach")
    _git(work, "commit", "-q", "--allow-empty", "-m", "agent work 1")
    _git(work, "commit", "-q", "--allow-empty", "-m", "agent work 2")
    # The premise, asserted rather than assumed: an empty range would make every
    # assertion below pass for the wrong reason.
    ahead = subprocess.run(["git", "log", "--format=%ae", "origin/main..HEAD"],
                           cwd=work, capture_output=True, text=True, check=True)
    assert ahead.stdout.split() == [AGENT[1], AGENT[1]], ahead.stdout
    detached = subprocess.run(["git", "symbolic-ref", "-q", "HEAD"], cwd=work,
                              capture_output=True, text=True)
    assert detached.returncode != 0, "the fixture must be DETACHED"
    return work


def test_a_supervising_commit_onto_an_agent_branch_is_refused(agent_branch):
    r = _run_gate(agent_branch, SUPERVISOR)
    assert r.returncode == 1, r.stdout + r.stderr
    # The message must name both identities, or it cannot be acted on.
    assert AGENT[1] in r.stderr and SUPERVISOR[1] in r.stderr, r.stderr
    # And it must name the way out, or it just blocks people.
    assert "NH_ALLOW_FOREIGN_BRANCH_COMMIT=1" in r.stderr, r.stderr
    assert "nh reject" in r.stderr, r.stderr


def test_the_agent_committing_to_its_own_branch_is_allowed(agent_branch):
    """The positive control. Without it, a gate that refused EVERYTHING would
    pass the test above and this file would prove nothing."""
    r = _run_gate(agent_branch, AGENT)
    assert r.returncode == 0, r.stdout + r.stderr


def test_the_override_lets_a_deliberate_hand_landing_through(agent_branch):
    r = _run_gate(agent_branch, SUPERVISOR, override=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_a_fresh_branch_off_the_trunk_is_allowed(agent_branch):
    """Nobody else's commits in range: this is your own new work, not a
    takeover. Refusing here would block ordinary branching."""
    _git(agent_branch, "checkout", "-q", "--detach", "origin/main")
    r = _run_gate(agent_branch, SUPERVISOR)
    assert r.returncode == 0, r.stdout + r.stderr


def test_a_repo_with_no_upstream_is_allowed_rather_than_wedged(tmp_path):
    """Allow on uncertainty, deliberately: a false refusal wedges the repo for
    everyone, while a miss costs one attempt. This gate guards a workflow
    mistake, not a red main."""
    work = tmp_path / "solo"
    subprocess.run(["git", "init", "-q", str(work)], check=True)
    _git(work, "commit", "-q", "--allow-empty", "-m", "only commit")
    r = _run_gate(work, SUPERVISOR)
    assert r.returncode == 0, r.stdout + r.stderr
