"""Integration: the landed-claim guard, wired into a real `Orchestrator` over
a real temp git repo, refuses a refutable "already satisfied" claim the
moment it is asserted — using delivery's own decision (`_route_unjudged_
head`/`_already_satisfied_eligible`, then `_already_satisfied_subject`),
just earlier, so the two can never disagree."""

from __future__ import annotations

import asyncio
import subprocess

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


@pytest.fixture
def diverged_repo(bare_repo):
    """The shape the 43 live delivery-time refusals actually are: HEAD sits at
    the LOCAL base tip (commits_ahead('main') == 0, so delivery's
    `resumed_commit` is None and the claim really is parsed) while the ship ref
    `origin/main` does NOT contain it."""
    (bare_repo / "calc.py").write_text("def add(a, b):\n    return a + b  # local\n")
    _git(bare_repo, "add", "-A")
    _git(bare_repo, "commit", "-m", "local base moved on; never pushed")
    return bare_repo


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
    diverged_repo, tmp_path, store,
):
    # This test drives `_build_landed_claim_guard`/`guard.hook(...)` directly
    # — it does NOT run `_run_attempt`, so it proves the guard/probe logic is
    # correct but nothing about the wiring that gets the guard onto the real
    # attempt's `on_event`/composed-hook path. See
    # `test_guard_is_wired_into_the_real_run_attempt_and_fires_on_a_refutable_claim`
    # below for that (send-back, Blocker 3: this test's old name overclaimed
    # "wired into the attempt" without ever calling `_run_attempt`).
    #
    # (Fifth review) Modelling the LIVE shape now: HEAD sits at the local
    # base tip (`commits_ahead("main") == 0`), so delivery really does reach
    # the claim gate — vs. the shape the guard used to model here (an
    # ordinary commit AHEAD of base), which is a shape delivery never even
    # parses a claim for; that regression now lives in
    # `test_a_branch_ahead_of_its_base_is_not_refused_because_delivery_never_
    # reaches_the_claim_gate` below.
    old_tip = _git(diverged_repo, "rev-parse", "origin/main").stdout.strip()
    attempt_branch = "no-human/task-attempt-1"
    # local-only, left at the OLD pushed tip — the same trick
    # `test_a_pushed_sibling_branch_of_the_same_task_is_not_blocked` uses —
    # so `local_is_reviewed` is False, `remote_branch_relation` is skipped,
    # and the refusal reason is deterministic and network-free.
    _git(diverged_repo, "branch", attempt_branch, old_tip)
    claimed_sha = GitRepo(diverged_repo).head_sha()

    orch = _orch(store, tmp_path)
    task = Task.new("existing", repo_path=str(diverged_repo), kind="feature")
    await store.create_task(task)

    guard = orch._build_landed_claim_guard(
        task, GitRepo(diverged_repo), base="main", branch=attempt_branch,
    )
    assert guard is not None

    guard.note_text(
        f"This is already implemented — the work already exists at "
        f"{claimed_sha}, no changes needed."
    )
    result = await guard.hook({}, None, None)

    assert result, "a commit not reachable from origin/main must be refused"
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
    # Criterion 6: the message states a present fact, not a prediction about
    # a condition (an unreachable remote) that could still change.
    assert "will refuse this claim right now" not in message
    # Converse of `test_a_wip_partial_checkpoint_is_not_blocked_because_
    # delivery_would_review_it_not_refuse_it` below: THIS head is eligible
    # (ordinary subject, no unreviewed-checkpoint shape), so delivery's own
    # `_route_unjudged_head` does NOT hoist it to review — it falls through
    # to the claim gate `_already_satisfied_subject` really refuses. Pinning
    # both in one test is what the third send-back asked for: eligibility
    # and the guard's verdict must be read off the SAME fixture.
    assert orch._route_unjudged_head(
        task, GitRepo(diverged_repo), "main") is None
    # And the shape itself: HEAD is on the local base, not ahead of it —
    # this is what makes delivery's `resumed_commit` at ~6501 be None, i.e.
    # the reachable-claim-gate state, unlike the over-refusal fixture below.
    assert GitRepo(diverged_repo).commits_ahead("main") == 0


async def test_a_wip_partial_checkpoint_is_not_blocked_because_delivery_would_review_it_not_refuse_it(
    bare_repo, tmp_path, store,
):
    """Send-back (third review), Blocker: `_run_attempt` hoists `_route_
    unjudged_head`/`_already_satisfied_eligible` (~12034/~11901) BEFORE the
    claim is even parsed. A `[WIP-PARTIAL]` (or `[WIP-BLOCKED]`) head one
    commit ahead of `main`, with no completed review verdict recorded
    against it, is routed straight to a full independent review —
    `_gate_already_satisfied` (and therefore `_already_satisfied_subject`)
    is never reached. A probe that asked `_already_satisfied_subject`
    alone would tell the coder "delivery will refuse this claim right now"
    in a shape where delivery instead reviews the diff for real (incidents
    0847f2c2 / d256ae60, and the neighbouring `[WIP-PARTIAL]` incident —
    see `_already_satisfied_eligible`'s docstring). The guard must stay
    silent here."""
    attempt_branch = "no-human/task-attempt-wip-partial"
    _git(bare_repo, "checkout", "-b", attempt_branch)
    (bare_repo / "fix.py").write_text("def fix():\n    return True\n")
    _git(bare_repo, "add", "-A")
    _git(bare_repo, "commit", "-m", "[WIP-PARTIAL] parked mid-turn by a wake resume")
    claimed_sha = GitRepo(bare_repo).head_sha()

    orch = _orch(store, tmp_path)
    task = Task.new("existing", repo_path=str(bare_repo), kind="feature")
    await store.create_task(task)

    assert orch._route_unjudged_head(
        task, GitRepo(bare_repo), "main") is not None, (
        "an unreviewed [WIP-PARTIAL] head off main must route to review")
    # Pin WHY the guard is silent here: ineligibility (this predicate),
    # not the new outer `commits_ahead` predicate this task adds — those
    # are two different silence reasons and must not be conflated.
    assert orch._already_satisfied_eligible(
        task, GitRepo(bare_repo), "main")[0] is False

    guard = orch._build_landed_claim_guard(
        task, GitRepo(bare_repo), base="main", branch=attempt_branch,
    )
    assert guard is not None
    guard.note_text(
        f"This is already implemented — the work already exists at "
        f"{claimed_sha}, no changes needed."
    )
    assert await guard.hook({}, None, None) == {}, (
        "delivery would route this head to a full review, not refuse the "
        "claim — the guard must not say it is refusing it")


async def test_an_ordinary_head_resumed_from_machine_requeue_provenance_is_not_blocked(
    bare_repo, tmp_path, store,
):
    """Same shape, the other trigger `_already_satisfied_eligible` treats
    identically: an ORDINARY subject (no `[WIP-*]` prefix) whose
    `task.context["resume_from"]["by"]` is in
    `blockers.MACHINE_REQUEUE_PROVENANCE` (task 8c8b36b5: a server restart
    killed the review mid-run, `_recover_orphans` resumed the task, and the
    branch already carried the whole diff no review had judged). Delivery
    routes this to a full review exactly as it does the checkpoint-subject
    shape; the guard must be silent for the same reason."""
    attempt_branch = "no-human/task-attempt-machine-resume"
    _git(bare_repo, "checkout", "-b", attempt_branch)
    (bare_repo / "fix.py").write_text("def fix():\n    return True\n")
    _git(bare_repo, "add", "-A")
    _git(bare_repo, "commit", "-m", "attempt at the fix")
    claimed_sha = GitRepo(bare_repo).head_sha()

    orch = _orch(store, tmp_path)
    task = Task.new("existing", repo_path=str(bare_repo), kind="feature")
    task.context["resume_from"] = {"by": "server_stop"}
    await store.create_task(task)

    assert orch._route_unjudged_head(
        task, GitRepo(bare_repo), "main") is not None, (
        "a machine-requeue-provenance head with no review verdict must "
        "route to review")
    # Same distinction as the [WIP-PARTIAL] test above: silence here is
    # ineligibility, not the new outer `commits_ahead` predicate.
    assert orch._already_satisfied_eligible(
        task, GitRepo(bare_repo), "main")[0] is False

    guard = orch._build_landed_claim_guard(
        task, GitRepo(bare_repo), base="main", branch=attempt_branch,
    )
    assert guard is not None
    guard.note_text(
        f"This is already implemented — the work already exists at "
        f"{claimed_sha}, no changes needed."
    )
    assert await guard.hook({}, None, None) == {}, (
        "delivery would route this head to a full review, not refuse the "
        "claim — the guard must not say it is refusing it")


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


async def test_a_branch_ahead_of_its_base_is_not_refused_because_delivery_never_reaches_the_claim_gate(
    bare_repo, tmp_path, store,
):
    """The actual defect this task fixes: delivery only ever parses an
    already-satisfied claim when `resumed_commit` is `None` (`_run_attempt`,
    ~6501) — i.e. no base, or nothing ahead of it, or a resume from this
    attempt's own `[WIP-PARTIAL]`. An ORDINARY commit ahead of `base` (no
    `[WIP-*]` subject, so eligible; not a checkpoint resume) is exactly the
    shape delivery commits, reviews, and opens a PR for — it never reaches
    `_already_satisfied_subject` at all. The old probe refused here anyway
    (30 actionable claims / 8 tasks measured with no matching delivery-time
    refusal); the fix is silence, evaluated at probe time since the coder
    commits while the attempt runs."""
    attempt_branch = "no-human/task-attempt-ahead"
    _git(bare_repo, "checkout", "-b", attempt_branch)
    (bare_repo / "fix.py").write_text("def fix():\n    return True\n")
    _git(bare_repo, "add", "-A")
    _git(bare_repo, "commit", "-m", "attempt at the fix, review FAILED")
    claimed_sha = GitRepo(bare_repo).head_sha()

    orch = _orch(store, tmp_path)
    task = Task.new("existing", repo_path=str(bare_repo), kind="feature")
    await store.create_task(task)

    # One instance, read before AND after the hook call, so eligibility and
    # `commits_ahead` are pinned off the exact same tree the guard probed.
    probe_repo = GitRepo(bare_repo)
    guard = orch._build_landed_claim_guard(
        task, probe_repo, base="main", branch=attempt_branch,
    )
    assert guard is not None
    guard.note_text(
        f"This is already implemented — the work already exists at "
        f"{claimed_sha}, no changes needed."
    )
    assert await guard.hook({}, None, None) == {}, (
        "a branch ahead of its base is a shape delivery ships, not one it "
        "refuses — the guard must stay silent")
    assert probe_repo.commits_ahead("main") > 0
    # And silence here is NOT because the head is ineligible — it is
    # eligible (ordinary subject, no unreviewed-checkpoint shape); it is the
    # new outer `commits_ahead` predicate doing the work, not
    # `_already_satisfied_eligible`.
    assert orch._already_satisfied_eligible(
        task, probe_repo, "main")[0] is True


class _AheadRaisesRepo:
    """A repo double whose `head_sha`/`_run` (subject) read normally but whose
    `commits_ahead` always raises — isolates the new outer predicate's OWN
    `except Exception` (~17024) from `_already_satisfied_eligible`'s
    unrelated, pre-existing `commits_ahead` try/except (~11985), which
    already treats a raise as "assume a diff exists" and is not what this
    test pins."""

    def head_sha(self):
        return "cafef00d" * 5

    def commits_ahead(self, base):
        raise RuntimeError("ahead unreadable")

    def _run(self, *args, **kwargs):
        return "an ordinary commit, not a checkpoint"


async def test_the_new_outer_predicate_stays_silent_on_its_own_commits_ahead_exception(
    tmp_path, store,
):
    """Mutant pin (STEP 3, mutation ladder #7): the new outer block's
    `except Exception: return False, "", ""` (~17024) must return a
    cannot-tell tuple, not propagate. Calling `guard.hook(...)` cannot
    distinguish a mutant that changes this to `raise` from the real code,
    because `hook()` (`landed_claim_guard.py` ~290) has its OWN outer
    `except Exception: return {}` around the whole probe call — either way
    the hook returns `{}`. So this test calls the probe callable directly
    (`guard._probe`, the exact object `LandedClaimGuard.__init__` stores at
    `self._probe`), bypassing that outer net, to pin THIS `except` clause on
    its own."""
    repo = _AheadRaisesRepo()
    orch = _orch(store, tmp_path)
    task = Task.new("existing", repo_path=str(tmp_path), kind="feature")
    await store.create_task(task)

    guard = orch._build_landed_claim_guard(
        task, repo, base="main", branch="no-human/task-attempt-x",
    )
    assert guard is not None
    # Eligibility must be True here — an ordinary subject, no machine-requeue
    # provenance, so `_already_satisfied_eligible`'s OWN (unrelated)
    # `commits_ahead` exception handling resolves to eligible — so the probe
    # actually reaches the new outer block's `commits_ahead` call.
    assert orch._already_satisfied_eligible(task, repo, "main")[0] is True

    result = await guard._probe()
    assert result == (False, "", ""), (
        "an unreadable `commits_ahead` inside the new outer predicate must "
        "be a cannot-tell (silent) result, not a raised exception")


async def test_an_unresolvable_ship_ref_is_not_a_refusal(tmp_path, store):
    """Mutant pin (STEP 3a): the `refuted = (...)` filter in
    `_build_landed_claim_guard`'s probe (~17007) must require BOTH a
    negative `shippable` AND a `reason` that actually names an unreachable
    branch — not merely `shippable is False`. When the ship ref itself
    cannot be resolved (no base, no remote, no local `main`),
    `_already_satisfied_subject` returns `shippable=False` with a reason
    that names no branch at all; a mutant that dropped the reason-shape
    check from `refuted` would refuse here too. It must not: refusing a
    claim by naming a branch that was never determined would be worse than
    silence."""
    work = tmp_path / "solo"
    work.mkdir()
    _git(work, "init", "-b", "work")
    _git(work, "config", "user.email", "u@example.test")
    _git(work, "config", "user.name", "u")
    (work / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "initial")
    # No remote, no `main` — `default_branch` and every ship-ref candidate
    # are unresolvable.
    head = GitRepo(work).head_sha()

    orch = _orch(store, tmp_path)
    task = Task.new("existing", repo_path=str(work), kind="feature")
    await store.create_task(task)

    shippable, probed_head, _subject, subject_reason, _on_main, ship_ref = (
        await orch._already_satisfied_subject(
            task, GitRepo(work), base=None, branch="work",
        )
    )
    assert shippable is False
    assert probed_head == head
    assert ship_ref == ""
    assert "cannot resolve the branch this task would ship to" in subject_reason

    guard = orch._build_landed_claim_guard(
        task, GitRepo(work), base=None, branch="work",
    )
    assert guard is not None
    guard.note_text(
        f"This is already implemented — the work already exists at "
        f"{head}, no changes needed."
    )
    assert await guard.hook({}, None, None) == {}, (
        "an unresolvable ship ref must not be reported as a refusal")


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
        # (Fifth review) Deliberately make NO commit here. `_run_attempt`
        # itself already cut `cwd`'s branch from `base` via `create_branch`
        # — against a `diverged_repo` `base`, that lands the attempt branch
        # exactly on the (unpushed) diverged tip, zero commits ahead. The
        # head is refutable because the SHIP ref (`origin/main`) lacks it —
        # not because it diverges from `base`, which it does not.
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
    diverged_repo, tmp_path, store,
):
    """Send-back, Blocker 3: the previous test with this name never called
    `_run_attempt` at all — it called `_build_landed_claim_guard` and
    `guard.hook(...)` directly, so mutants deleting the `on_event` wiring
    (M8) or nulling/dropping the composed claim guard before it reaches
    `self.backend.run` (M9/M10) all survived with the whole suite green.
    This test drives the REAL `_run_attempt`, through a backend that feeds a
    claim through the REAL captured `on_event` (`_agent_sink`) and captures
    the REAL composed hook object `_run_attempt` builds and passes to the
    backend.

    (Fifth review) Re-based onto `diverged_repo`, same reason as the
    real-probe test above: `_run_attempt` itself cuts the attempt branch
    from `base` via `create_branch`, and the backend below makes no commit
    of its own, so the attempt branch lands exactly on the diverged
    (unpushed) tip — `commits_ahead("main") == 0` — reaching the real claim
    gate instead of a shape delivery would never parse a claim for."""
    cfg = _config(tmp_path)
    backend = _ClaimFeedingBackend(_incident_result())
    orch = Orchestrator(store, cfg.data, backend, SlackNotifier(None),
                        event_sink=[].append)
    task = Task.new("existing", repo_path=str(diverged_repo), kind="feature")
    await store.create_task(task)
    await store.set_status(task, TaskStatus.CONTEXT)
    await store.set_status(task, TaskStatus.PLANNING)
    repo = GitRepo(diverged_repo)

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
    # The shape itself: the real `_run_attempt` cut the branch at the
    # diverged tip and the backend made no commit, so it never got ahead of
    # `main` — this really is the claim-gate-reachable state, not the
    # never-reaches-the-gate state the old fixture (mis)modelled here.
    assert GitRepo(diverged_repo).commits_ahead("main") == 0


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
