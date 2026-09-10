"""Integration: the landed-claim guard, wired into a real `Orchestrator` over
a real temp git repo, refuses a refutable "already satisfied" claim the
moment it is asserted — using the exact same ancestry question
(`classify_already_satisfied_landing`, `git merge-base --is-ancestor`)
delivery asks, just earlier."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from no_human.agent.backend import AgentEvent, AgentResult
from no_human.config import load_config
from no_human.core.bounds import QuotaExhausted
from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task, TaskStatus
from no_human.notify.slack import SlackNotifier
from no_human.vcs import GitRepo


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True,
                          capture_output=True, text=True)


@pytest.fixture
def bare_repo(tmp_path):
    bare = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", "-b", "main", str(bare))
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.email", "u@example.test")
    _git(work, "config", "user.name", "u")
    (work / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "initial")
    _git(work, "remote", "add", "origin", str(bare))
    _git(work, "push", "-u", "origin", "main")
    return work


def _config(tmp_path):
    cfg = load_config(tmp_path / "config.yaml")
    cfg.data.setdefault("planning", {})["enabled"] = False
    cfg.data.setdefault("blockers", {})["challenge"] = False
    return cfg


class _Backend:
    async def run(self, *a, **k):  # pragma: no cover — never invoked here
        raise AssertionError("backend should not run in this test")


def _orch(store, tmp_path):
    return Orchestrator(
        store, _config(tmp_path).data, _Backend(), SlackNotifier(None),
    )


async def test_build_landed_claim_guard_fires_on_a_refutable_claim_via_the_real_probe(
    bare_repo, tmp_path, store,
):
    # This test drives `_build_landed_claim_guard`/`guard.hook(...)` directly
    # — it does NOT run `_run_attempt`, so it proves the guard/probe logic is
    # correct but nothing about the wiring that gets the guard onto the real
    # attempt's `on_event`/composed-hook path. See
    # `test_guard_is_wired_into_the_real_run_attempt_and_fires_on_a_refutable_claim`
    # below for that (send-back, Blocker 3: this test's old name overclaimed
    # "wired into the attempt" without ever calling `_run_attempt`).
    #
    # An ORDINARY commit (no [WIP-*] subject) left by a previous, review-
    # failed round — 33 of the 42 measured incidents look like this, not a
    # checkpoint.
    attempt_branch = "no-human/task-attempt-1"
    _git(bare_repo, "checkout", "-b", attempt_branch)
    (bare_repo / "fix.py").write_text("def fix():\n    return True\n")
    _git(bare_repo, "add", "-A")
    _git(bare_repo, "commit", "-m", "attempt at the fix, review FAILED")
    claimed_sha = GitRepo(bare_repo).head_sha()

    orch = _orch(store, tmp_path)
    task = Task.new("existing", repo_path=str(bare_repo), kind="feature")
    await store.create_task(task)

    guard = orch._build_landed_claim_guard(
        task, GitRepo(bare_repo), base="main", branch=attempt_branch,
    )
    assert guard is not None

    guard.note_text(
        f"This is already implemented — the work already exists at "
        f"{claimed_sha}, no changes needed."
    )
    result = await guard.hook({}, None, None)

    assert result, "a commit not reachable from main must be refused"
    message = result["hookSpecificOutput"]["additionalContext"]
    assert claimed_sha in message
    assert "main" in message
    # `_already_satisfied_subject`'s own reason string (reused verbatim by
    # the guard) is `"{head} is not on {ship_ref}"` — the exact phrase the
    # acceptance criterion itself names ("names the commit + the branch it
    # is not on"). An earlier revision's probe (`classify_already_satisfied_
    # landing`, a narrower, second authority — see Blocker 1 in the module
    # docstring) phrased this "is not an ancestor of" instead.
    assert "is not on" in message
    # Non-terminal: the attempt must be told to keep going.
    assert "continue_" not in result


async def test_a_commit_that_is_on_the_base_branch_is_not_blocked(
    bare_repo, tmp_path, store,
):
    main_tip = GitRepo(bare_repo).head_sha()
    attempt_branch = "no-human/task-attempt-2"
    _git(bare_repo, "checkout", "-b", attempt_branch)

    orch = _orch(store, tmp_path)
    task = Task.new("existing", repo_path=str(bare_repo), kind="feature")
    await store.create_task(task)

    guard = orch._build_landed_claim_guard(
        task, GitRepo(bare_repo), base="main", branch=attempt_branch,
    )
    assert guard is not None

    guard.note_text(
        f"This is already implemented — the work already exists at "
        f"{main_tip}, no changes needed."
    )
    result = await guard.hook({}, None, None)

    assert result == {}, "a commit already reachable from main must not be blocked"


@pytest.mark.parametrize(
    "text",
    [
        "I already ran the full suite; no changes needed in tests/test_foo.py",
        "Let me check whether the prior session's work is already there.",
        "Refactor complete. No code changes are needed to the CLI; only the "
        "docs move.",
    ],
)
async def test_ordinary_prose_never_reaches_the_real_probe(
    text, bare_repo, tmp_path, store,
):
    """Send-back (second review), Blocker 2, pinned against the REAL probe
    (`_build_landed_claim_guard`/`_already_satisfied_subject`) rather than a
    fake one — the reviewer's literal wording: "against a repo whose HEAD is
    off base". `attempt_branch`'s head is genuinely not on `main`, so if this
    unnamed-sha, non-marker prose reached the probe it would be refused; the
    fix is that it must never reach the probe at all."""
    attempt_branch = "no-human/task-attempt-3"
    _git(bare_repo, "checkout", "-b", attempt_branch)
    (bare_repo / "fix.py").write_text("def fix():\n    return True\n")
    _git(bare_repo, "add", "-A")
    _git(bare_repo, "commit", "-m", "attempt at the fix, review FAILED")

    orch = _orch(store, tmp_path)
    task = Task.new("existing", repo_path=str(bare_repo), kind="feature")
    await store.create_task(task)

    guard = orch._build_landed_claim_guard(
        task, GitRepo(bare_repo), base="main", branch=attempt_branch,
    )
    assert guard is not None

    guard.note_text(text)
    result = await guard.hook({}, None, None)
    assert result == {}, (
        f"non-actionable prose must never reach the probe: {text!r}")


async def test_a_pushed_sibling_branch_of_the_same_task_is_not_blocked(
    bare_repo, tmp_path, store,
):
    """Blocker 1: the probe must be delivery's own authority
    (`_already_satisfied_subject`), which additionally accepts a pushed
    SIBLING branch of this same task — not the narrower, ancestry-against-
    base-only `classify_already_satisfied_landing` an earlier revision
    wrapped. Pinned end to end: `_already_satisfied_subject` itself must say
    `shippable is True` for this shape, AND the guard's `hook()` must be a
    no-op for the SAME claim, in the same test.

    `offered_branch` is a LOCAL-ONLY branch left pointing at the OLD (main)
    tip — deliberately NOT advanced to `head` — so `local_is_reviewed` is
    False and `_already_satisfied_subject` skips the pushed-branch check
    (which would need `offered_branch` itself pushed) and falls through to
    the sibling-branch check, which only needs the SIBLING pushed."""
    orch = _orch(store, tmp_path)
    task = Task.new("existing", repo_path=str(bare_repo), kind="feature")
    await store.create_task(task)

    stem = f"no-human/{task.id[:8]}"
    offered_branch = f"{stem}-1"
    _git(bare_repo, "branch", offered_branch)  # local-only, stays at main tip

    sibling_branch = f"{stem}-2"
    _git(bare_repo, "checkout", "-b", sibling_branch)
    (bare_repo / "fix.py").write_text("def fix():\n    return True\n")
    _git(bare_repo, "add", "-A")
    _git(bare_repo, "commit", "-m", "attempt at the fix")
    _git(bare_repo, "push", "-u", "origin", sibling_branch)
    head = GitRepo(bare_repo).head_sha()

    shippable, probed_head, _subject, subject_reason, _on_main, ship_ref = (
        await orch._already_satisfied_subject(
            task, GitRepo(bare_repo), base="main", branch=offered_branch,
        )
    )
    assert shippable is True, subject_reason
    assert probed_head == head

    guard = orch._build_landed_claim_guard(
        task, GitRepo(bare_repo), base="main", branch=offered_branch,
    )
    assert guard is not None
    guard.note_text(
        f"This is already implemented — the work already exists at "
        f"{head}, no changes needed."
    )
    assert await guard.hook({}, None, None) == {}, (
        "a pushed sibling branch of this task must not be blocked")


def _incident_result() -> AgentResult:
    # Same shape `test_infra_not_work.py` replays from the 2026-08-13
    # incident: zero tokens, turn 1 — trips `_infra_sdk_failure` so
    # `_run_attempt` raises `QuotaExhausted` shortly after the backend call
    # returns, letting this test bound a real `_run_attempt` call without
    # modelling the whole downstream review/delivery pipeline.
    return AgentResult(
        final_text="Exception: Claude Code returned an error result: success",
        num_turns=1, is_error=True, tokens_used=0, session_id=None,
        stop_reason="error", cache_read_tokens=0, cache_creation_tokens=0,
    )


class _ClaimFeedingBackend:
    """Drives a claim through the REAL `_agent_sink` -> `LandedClaimGuard.
    note_text` path (kills mutant M8: deleting `on_event=self._agent_sink`
    from the real `backend.run(...)` call — if `on_event` were never passed,
    `captured_on_event` below would be `None` and the claim would never be
    fed anywhere) and captures the REAL composed post-tool hook object built
    by `_run_attempt`'s own wiring (kills mutants M9/M10: nulling
    `claim_guard` before composition, or passing `None` instead of the
    composed hooks into `self.backend.run` — either would leave
    `captured_lint_hook` either absent the claim guard or `None`, so
    `hook_result` below could never carry a refusal)."""

    def __init__(self, result: AgentResult):
        self._result = result
        self.calls = 0
        self.captured_on_event = None
        self.captured_lint_hook = None
        self.hook_result: dict | None = None
        self.claimed_sha: str | None = None

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        self.calls += 1
        # Simulate the coder making a real, divergent-from-base commit — the
        # same shape as 33 of the 42 measured incidents (an ordinary commit
        # left by a previous round, no [WIP-*] subject) — so the probe finds
        # a head that is genuinely not on `main` rather than one that is
        # still literally on it (the attempt branch is freshly cut from
        # `main` and has no commits of its own yet at this point).
        (Path(cwd) / "fix.py").write_text("def fix():\n    return True\n")
        subprocess.run(["git", "add", "-A"], cwd=cwd, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "attempt at the fix"], cwd=cwd,
                       check=True, capture_output=True)
        self.claimed_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=cwd, check=True,
            capture_output=True, text=True,
        ).stdout.strip()

        self.captured_on_event = on_event
        self.captured_lint_hook = kwargs.get("lint_hook")
        if on_event is not None:
            on_event(AgentEvent(
                kind="text",
                text=(
                    f"This is already implemented — the work already "
                    f"exists at {self.claimed_sha}, no changes needed."
                ),
            ))
        if self.captured_lint_hook is not None:
            self.hook_result = await self.captured_lint_hook.hook({}, None, None)
        return self._result


async def test_guard_is_wired_into_the_real_run_attempt_and_fires_on_a_refutable_claim(
    bare_repo, tmp_path, store,
):
    """Send-back, Blocker 3: the previous test with this name never called
    `_run_attempt` at all — it called `_build_landed_claim_guard` and
    `guard.hook(...)` directly, so mutants deleting the `on_event` wiring
    (M8) or nulling/dropping the composed claim guard before it reaches
    `self.backend.run` (M9/M10) all survived with the whole suite green.
    This test drives the REAL `_run_attempt`, through a backend that feeds a
    claim through the REAL captured `on_event` (`_agent_sink`) and captures
    the REAL composed hook object `_run_attempt` builds and passes to the
    backend."""
    cfg = _config(tmp_path)
    backend = _ClaimFeedingBackend(_incident_result())
    orch = Orchestrator(store, cfg.data, backend, SlackNotifier(None),
                        event_sink=[].append)
    task = Task.new("existing", repo_path=str(bare_repo), kind="feature")
    await store.create_task(task)
    await store.set_status(task, TaskStatus.CONTEXT)
    await store.set_status(task, TaskStatus.PLANNING)
    repo = GitRepo(bare_repo)

    with pytest.raises(QuotaExhausted):
        await orch._run_attempt(task, repo, 1, "main")

    assert backend.calls, "the backend never ran — the test proves nothing"
    assert backend.captured_on_event is not None, (
        "on_event must reach the backend — see M8")
    assert backend.captured_lint_hook is not None, (
        "the composed post-tool hook must reach the backend — see M9/M10")
    assert backend.hook_result, (
        "the real wiring must have refused the claim — see M8/M9/M10")
    message = backend.hook_result["hookSpecificOutput"]["additionalContext"]
    assert backend.claimed_sha in message
    assert "main" in message
    assert "is not on" in message
    assert "continue_" not in backend.hook_result


def test_composed_post_tool_hooks_place_the_claim_guard_after_receipts():
    r, lint, scope, claim = object(), object(), object(), object()
    assert Orchestrator._ordered_post_tool_hooks(r, lint, scope, claim_hook=claim) == [
        r, claim, lint, scope,
    ]
    # Pre-existing 3-positional-arg call sites are unaffected: no claim hook,
    # same order as before this change.
    assert Orchestrator._ordered_post_tool_hooks(r, lint, scope) == [r, lint, scope]
    assert Orchestrator._ordered_post_tool_hooks(r, None, scope, claim_hook=claim) == [
        r, claim, scope,
    ]
