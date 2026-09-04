"""The branch-point fix, tested through `_run_attempt` — not in isolation.

An independent review proved the helper-level tests in test_resume_branch_point.py
certify nothing about the WIRING: with `_record_wip_checkpoint` stubbed to `pass`,
or with the branch point reverted to `resume_sha or (wip_sha if attempt_n > 1
else "")`, all 2822 tests still passed. That is this repo's own documented trap —
"break the WIRING while the function stays correct".

Two behaviours are pinned here, and each fails on 631ab7e:

1. attempt 2 INHERITS attempt 1's [WIP-PARTIAL] instead of re-doing it from base;
2. inheriting it does NOT let an attempt that edited nothing be recorded
   `succeeded` — the zero-diff honesty gate must still fire. Crediting inherited
   work as an attempt's own deliverable is how a do-nothing attempt would open a
   PR on abandoned half-work and stop `unproductive_streak` from incrementing.
"""
import subprocess

import pytest

from no_human.agent.claude_backend import AgentResult
from no_human.core.orchestrator import Orchestrator, StuckAbort
from no_human.core.task import Task, TaskStatus
from no_human.notify.slack import SlackNotifier

from .test_e2e_orchestrator import _config, bare_repo  # noqa: F401


class ScriptedBackend:
    """Runs one scripted step per attempt; the last step repeats."""

    def __init__(self, *steps):
        self.steps = list(steps)
        self.calls = 0

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        step = self.steps[min(self.calls, len(self.steps) - 1)]
        self.calls += 1
        return step(cwd)


def _ok(final_text="done"):
    return AgentResult(final_text=final_text, num_turns=2, is_error=False,
                       tokens_used=100, session_id="s", stop_reason="end_turn")


def _tree(repo_path, branch):
    return subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", branch],
        cwd=repo_path, capture_output=True, text=True).stdout.split()


async def test_attempt_two_inherits_attempt_ones_partial_work(bare_repo, tmp_path, store):
    """THE WIRING. Attempt 1 leaves [WIP-PARTIAL] via a stuck-abort; attempt 2
    must start from it, not from base — otherwise every attempt re-explores and
    re-implements what the previous one already paid for (task afe1ed12: 4
    attempts, 12,071,981 tokens, no PR)."""
    def attempt1(cwd):
        (cwd / "partial.py").write_text("# tens of turns of work\n")
        raise StuckAbort("doom-loop: identical tool call repeated 3x")

    def attempt2(cwd):
        (cwd / "finished.py").write_text("def done():\n    return True\n")
        return _ok()

    orch = Orchestrator(store, _config(tmp_path).data,
                        ScriptedBackend(attempt1, attempt2), SlackNotifier(None))
    t = Task.new("inherit the partial work", repo_path=str(bare_repo))
    await store.create_task(t)
    await orch.run_task(t)

    attempts = await store.list_attempts(t.id)
    assert len(attempts) >= 2, [a["attempt_number"] for a in attempts]
    second = attempts[1]["branch_name"]
    tree = _tree(bare_repo, second)
    assert "partial.py" in tree, (
        f"attempt 2 branched without attempt 1's checkpointed work: {tree}")
    assert "finished.py" in tree


async def test_inherited_work_is_not_credited_as_a_do_nothing_attempts_own(
        bare_repo, tmp_path, store):
    """THE HONESTY GATE. Attempt 1 stuck-aborts leaving partial work; attempt 2
    edits NOTHING. Because attempt 2's branch already carries the inherited
    commit, a naive `commits_ahead(base) > 0` check reads that as "this attempt
    produced a change" — recording it `succeeded` and opening a PR on abandoned
    half-work. The attempt contributed nothing and must not be credited."""
    def attempt1(cwd):
        (cwd / "half.py").write_text("raise NotImplementedError\n")
        raise StuckAbort("doom-loop: identical tool call repeated 3x")

    def does_nothing(cwd):
        return _ok("I reviewed the code and believe it is already complete.")

    orch = Orchestrator(store, _config(tmp_path).data,
                        ScriptedBackend(attempt1, does_nothing), SlackNotifier(None))
    t = Task.new("do-nothing attempt must not be credited", repo_path=str(bare_repo))
    await store.create_task(t)
    await orch.run_task(t)

    final = await store.get_task(t.id)
    attempts = await store.list_attempts(t.id)
    credited = [a for a in attempts
                if a["status"] == "succeeded" and a["attempt_number"] > 1]
    assert not credited, (
        "an attempt that edited nothing was credited with the inherited work: "
        f"{[(a['attempt_number'], a['status'], a['failure_reason']) for a in attempts]}")
    assert final.status is not TaskStatus.AWAITING_APPROVAL, (
        "a PR was opened for work no attempt actually produced")
