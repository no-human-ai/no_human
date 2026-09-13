r"""Scheduling wrappers must not launder a command past the argv-based gates.

Issue #329. `taskset`/`chrt`/`ionice`/`setsid` are priority/affinity wrappers:
`taskset 0x3 <cmd>` runs `<cmd>` on CPUs 0 and 1. They belong to the same
family as `nice`/`timeout`/`env`, which the guard already strips, but they were
absent from the lists the merge and git gates consult, so a four-character
prefix walked a command past those gates. The worst cell reached constraint #2:

    taskset 0x3 nh approve 7      was ALLOW      (nh approve ends the merge gate)

Two root causes, one per gate (both confirmed by measurement, see the issue):

* the approve/forge path reads `_FORGE_RUNNER_NAMES` (= `_SHELL_RUNNERS` |
  `_TRAILING_ARGV_RUNNERS`), and `taskset` was the one scheduling wrapper
  missing from `_TRAILING_ARGV_RUNNERS` while `chrt`/`ionice`/`setsid` were in
  it; and
* `_git_invocations` peeled only the narrower `_SHELL_RUNNERS`, so ALL four
  scheduling wrappers walked past the git gate regardless of that list.

The fix is `taskset` into `_TRAILING_ARGV_RUNNERS`, and `_git_invocations`
reading `_FORGE_RUNNER_NAMES` like `_forge_invocations` already does.

Measured with the Linux reading (`_IS_WINDOWS=False`), because these are Linux
binaries an agent on the fleet actually has.
"""
from __future__ import annotations

import pytest

from no_human.agent import guard
from no_human.agent.guard import evaluate

SCHEDULERS = ["taskset 0x3", "chrt 10", "ionice -c3", "setsid"]


@pytest.fixture(autouse=True)
def _linux(monkeypatch):
    # These wrappers exist on Linux; measure the Linux reading whatever the host.
    monkeypatch.setattr(guard, "_IS_WINDOWS", False)


def _deny(command: str) -> bool:
    return not evaluate(
        "Bash", {"command": command},
        forbidden_paths=[], never_push_to=["main"], cwd=".", env={"PATH": ""}).allow


def test_a_scheduling_wrapper_cannot_launder_the_merge_gate():
    """The headline, constraint #2: the agent never merges. `nh approve` ends
    the human merge gate, and `taskset 0x3 nh approve 7` was allowed."""
    assert _deny("nh approve 7"), "bare form must deny or the test proves nothing"
    assert _deny("taskset 0x3 nh approve 7"), "taskset laundered the merge gate"


@pytest.mark.parametrize("wrapper", SCHEDULERS)
@pytest.mark.parametrize("victim", ["git checkout -- .", "git stash"])
def test_scheduling_wrappers_cannot_launder_destructive_git(wrapper, victim):
    """The working-tree-destructive git forms have no raw-text twin (the clobber
    decision needs argv plus a cwd check), so the argv gate is their only line
    and every scheduling wrapper walked past it."""
    assert _deny(victim), "bare form must deny"
    assert _deny(f"{wrapper} {victim}"), f"{wrapper} laundered {victim!r}"


@pytest.mark.parametrize("command", [
    "taskset -c 0-1 nh approve 7",        # range operand, not a hex mask
    "chrt -f 1 nh approve 7",             # flag-form operand
    "env taskset 0x3 nh approve 7",       # a stripped wrapper before the scheduler
    'sh -c "taskset 0x3 nh approve 7"',   # scheduler nested in a shell runner
    'taskset 0x3 sh -c "git stash"',      # shell runner nested in a scheduler
])
def test_the_operand_and_nesting_shapes_are_covered_too(command):
    assert _deny(command)


def test_taskset_membership_is_load_bearing(monkeypatch):
    """Reverting change 1 reopens the merge gate. `chrt`/`ionice`/`setsid` were
    already in the set; `taskset` is the name that was missing."""
    monkeypatch.setattr(guard, "_TRAILING_ARGV_RUNNERS",
                        guard._TRAILING_ARGV_RUNNERS - {"taskset"})
    monkeypatch.setattr(guard, "_FORGE_RUNNER_NAMES",
                        guard._SHELL_RUNNERS | guard._TRAILING_ARGV_RUNNERS)
    assert not _deny("taskset 0x3 nh approve 7"), (
        "removing taskset from the runner set should reopen the bypass; if this "
        "fails the fix now rests on something else and this test is stale")


def test_the_git_gate_uses_the_wide_runner_set(monkeypatch):
    """Reverting change 2 reopens the destructive-git cells. With the git gate
    back on the narrow `_SHELL_RUNNERS`, the schedulers walk past it again."""
    monkeypatch.setattr(guard, "_FORGE_RUNNER_NAMES", guard._SHELL_RUNNERS)
    assert not _deny("setsid git stash"), (
        "with _git_invocations on the narrow set, setsid should launder the git "
        "gate again; if this fails the git path no longer reads the wide set")


@pytest.mark.parametrize("command", [
    "taskset 0x3 git reset --hard HEAD",
    "taskset 0x3 git push origin main",
])
def test_reset_and_push_deny_independent_of_the_wrapper_set(command, monkeypatch):
    """Control: the wrapper widening must not become the ONLY line of defence.

    `git reset --hard` and `git push` to a protected branch are also caught by
    a raw-text matcher, so they deny even with every scheduling wrapper stripped
    from the runner sets. This pins that second line, so a future narrowing of
    the wrapper lists cannot silently reopen these while looking safe."""
    monkeypatch.setattr(guard, "_TRAILING_ARGV_RUNNERS", frozenset())
    monkeypatch.setattr(guard, "_FORGE_RUNNER_NAMES", guard._SHELL_RUNNERS)
    assert _deny(command), (
        "the raw-text path no longer catches this; the wrapper set is now the "
        "only defence, which is exactly what this control exists to prevent")


def test_a_legitimate_wrapped_command_is_not_newly_denied():
    """The fix only widens what the gates SEE, so it cannot deny more than the
    bare command does. A scheduler in front of an ordinary command stays
    allowed."""
    for command in ["taskset 0x3 git status", "taskset -c 0-3 git log --oneline",
                    "setsid git diff", "chrt 10 git fetch",
                    "taskset 0x3 git commit -am wip"]:
        assert not _deny(command), f"{command!r} was newly denied"
