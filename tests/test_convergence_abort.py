"""Turn-cap convergence early-abort (P2).

`StuckDetector`'s hard tiers (`test_stuck_abort.py`) end a DETERMINISTIC
runaway: the same tool call, or the same alternating pair, repeated. They
never fire on an attempt that keeps VARYING its tool calls — a new file read
here, a new grep there, never the same signature twice — while never
converging on a fix, because every call is "new" by the doom-loop signature.
That attempt is free to spend the whole `max_turns_per_attempt` (500, left
UNCHANGED by this feature) looking around without ever writing or verifying
anything.

`ConvergenceTracker` (core/bounds.py) closes that gap, wired into the SAME
sink (`_agent_sink`) and the SAME abort/checkpoint path `StuckAbort` already
uses — no parallel framework. These tests pin:

1. a synthetic non-converging stream (reads/greps only, past the threshold)
   raises `ConvergenceAbort` — the attempt fails honestly with its work
   checkpointed, exactly like a `StuckAbort`;
2. a converging stream (a file edit, or a test-runner invocation, at least
   once per window) never raises, however many turns it runs;
3. the HOLD-OUT: a stream far past the real measured ~328-turn successful
   attempt, with progress on a steady cadence, is NOT aborted — the raw cap
   must stay the only thing that can end a genuinely long, productive run;
4. the kill switch (`worker.abort_non_converging: false`) reproduces case 1's
   exact input WITHOUT aborting — today's behaviour, unchanged.

ROUND 2 (independent review — scoping bugs found by replaying this
install's real event corpus against the round-1 defaults, ~758k events; see
`ConvergenceTracker`'s docstring for the reproducible anchor):

5. a tracker latched by one task's attempt must never fire inside a
   DIFFERENT task's session on the same reused Orchestrator, and must not
   crash `_run_code_review`'s diff-fetch fallback specifically (it has no
   `ConvergenceAbort` handler);
6. a `_REPORT_KINDS`-shaped small per-attempt cap (80 turns) clamps
   `min_turns`, and a write into the agent's own sanctioned scratch dir
   counts as progress even though it is not committable;
7. a command that only MENTIONS a test runner (`rg pytest`) — never runs
   one — must not count as progress;
8. a malformed config value must not raise (the feature can be off and a
   config typo must not still kill every attempt).
"""

import subprocess

import pytest

from no_human.agent.claude_backend import AgentEvent, AgentResult
from no_human.core.bounds import ConvergenceTracker
from no_human.core.orchestrator import CODER_ROLE, ConvergenceAbort, Orchestrator
from no_human.core.task import Task, TaskStatus
from no_human.notify.slack import SlackNotifier
from no_human.vcs.git import GitRepo

from .test_e2e_orchestrator import FakeBackend, _config, bare_repo  # noqa: F401


def _mutate(cwd):
    (cwd / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n"
    )


def _orch(store, tmp_path, *, worker=None, backend=None, events=None, reviewer=None):
    cfg = _config(tmp_path)
    if worker:
        cfg.data.setdefault("worker", {}).update(worker)
    return Orchestrator(
        store, cfg.data, backend or FakeBackend(_mutate), SlackNotifier(None),
        event_sink=(events.append if events is not None else None),
        reviewer=reviewer,
    )


def _arm(orch, tracker: ConvergenceTracker, *, task_id: str = "task-1") -> None:
    """Set the scoped `(task_id, tracker)` pair `_agent_sink` reads via
    `_active_convergence()` — the same shape as `_token_ceiling`."""
    orch._convergence = (task_id, tracker)


def _read(i: int) -> AgentEvent:
    """A Read/Grep tool call whose signature is DIFFERENT every turn — the
    doom-loop detector must never see a repeat, and neither must this
    fixture's shape rely on it: convergence is checked even when `_stuck`
    is never armed on the orchestrator at all (these tests never set it)."""
    if i % 2 == 0:
        return AgentEvent("tool_use", tool_name="Read",
                           tool_input={"file_path": f"src/file_{i}.py"})
    return AgentEvent("tool_use", tool_name="Grep",
                       tool_input={"query": f"needle_{i}", "path": "src/"})


_USAGE = AgentEvent("usage", meta={"tokens_used": 10, "cache_read_tokens": 0,
                                    "cache_creation_tokens": 0})


def _edit(path: str) -> AgentEvent:
    return AgentEvent("tool_use", tool_name="Edit",
                       tool_input={"file_path": path, "old_string": "a",
                                   "new_string": "b"})


def _scratch_write(i: int) -> AgentEvent:
    """A Write into the agent's own sanctioned scratch dir — not
    committable, but (round-2 review fix) still convergence progress."""
    return AgentEvent("tool_use", tool_name="Write",
                       tool_input={"file_path": f".no_human/scratch/note_{i}.md",
                                   "content": f"finding {i}"})


def _test_run() -> AgentEvent:
    return AgentEvent("tool_use", tool_name="Bash",
                       tool_input={"command": "uv run pytest -q"})


# ------------------------- 1. non-converging fixture ------------------------ #


def test_sink_aborts_on_reads_and_greps_past_the_threshold(store, tmp_path):
    """A synthetic stream that ONLY reads/greps for `window` turns past
    `min_turns` raises ConvergenceAbort — no edit, no test run, ever."""
    orch = _orch(store, tmp_path, worker={
        "convergence_check_after_turns": 5, "convergence_window_turns": 3,
    })
    orch._active_task_id = "task-1"
    _arm(orch, ConvergenceTracker.from_config(orch.config.get("worker")))

    with pytest.raises(ConvergenceAbort, match="no file edit or test run"):
        for i in range(20):
            orch._agent_sink(_read(i), role=CODER_ROLE)
            orch._agent_sink(_USAGE, role=CODER_ROLE)


def test_convergence_abort_is_not_raised_below_the_threshold(store, tmp_path):
    """Below min_turns, an all-reads stream must NOT abort — early
    exploration is normal, not stalling."""
    orch = _orch(store, tmp_path, worker={
        "convergence_check_after_turns": 20, "convergence_window_turns": 5,
    })
    orch._active_task_id = "task-1"
    tracker = ConvergenceTracker.from_config(orch.config.get("worker"))
    _arm(orch, tracker)

    for i in range(15):  # under min_turns=20 — must never raise
        orch._agent_sink(_read(i), role=CODER_ROLE)
        orch._agent_sink(_USAGE, role=CODER_ROLE)
    assert tracker.non_converging_reason is None


@pytest.mark.parametrize("role", ["planner", "reviewer", "aggregator"])
def test_only_the_implementer_session_convergence_aborts(store, tmp_path, role):
    """Same role gate `StuckDetector` uses: a non-coder session (planner,
    reviewer, MoA aggregator) shares this sink but must never be aborted by
    it, however long its own read-only stream runs."""
    orch = _orch(store, tmp_path, worker={
        "convergence_check_after_turns": 5, "convergence_window_turns": 3,
    })
    orch._active_task_id = "task-1"
    tracker = ConvergenceTracker.from_config(orch.config.get("worker"))
    _arm(orch, tracker)

    for i in range(20):
        orch._agent_sink(_read(i), role=role)
        orch._agent_sink(_USAGE, role=role)  # must not raise
    # Non-coder roles return before `conv.tick()` — the tracker must never
    # have been advanced at all, not merely "not yet past threshold".
    assert tracker._turns == 0


# --------------------------- 2. converging fixture --------------------------- #


def test_file_edits_keep_a_long_run_from_aborting(store, tmp_path):
    """An edit at least once per window resets the clock — the attempt runs
    on for far longer than the threshold+window would otherwise allow."""
    orch = _orch(store, tmp_path, worker={
        "convergence_check_after_turns": 5, "convergence_window_turns": 3,
    })
    orch._active_task_id = "task-1"
    tracker = ConvergenceTracker.from_config(orch.config.get("worker"))
    _arm(orch, tracker)

    for i in range(30):
        orch._agent_sink(_read(i), role=CODER_ROLE)
        if i % 2 == 0:  # every 2 turns — inside the window=3
            orch._agent_sink(_edit(f"src/calc_{i}.py"), role=CODER_ROLE)
        orch._agent_sink(_USAGE, role=CODER_ROLE)  # must never raise
    assert tracker.non_converging_reason is None
    assert tracker._turns == 30


def test_test_runner_invocations_keep_a_long_run_from_aborting(store, tmp_path):
    """A recognized test-runner Bash command is the OTHER convergence
    signal — the attempt is verifying, not just looking around."""
    orch = _orch(store, tmp_path, worker={
        "convergence_check_after_turns": 5, "convergence_window_turns": 3,
    })
    orch._active_task_id = "task-1"
    tracker = ConvergenceTracker.from_config(orch.config.get("worker"))
    _arm(orch, tracker)

    for i in range(30):
        orch._agent_sink(_read(i), role=CODER_ROLE)
        if i % 2 == 0:  # every 2 turns — inside the window=3
            orch._agent_sink(_test_run(), role=CODER_ROLE)
        orch._agent_sink(_USAGE, role=CODER_ROLE)  # must never raise
    assert tracker.non_converging_reason is None
    assert tracker._turns == 30


def test_a_non_test_bash_command_is_not_progress(store, tmp_path):
    """An ordinary Bash command (not a recognized test runner) must NOT
    count as progress — otherwise every attempt that shells out at all,
    however aimlessly, would be immune to this check."""
    orch = _orch(store, tmp_path, worker={
        "convergence_check_after_turns": 5, "convergence_window_turns": 3,
    })
    orch._active_task_id = "task-1"
    _arm(orch, ConvergenceTracker.from_config(orch.config.get("worker")))
    ls = AgentEvent("tool_use", tool_name="Bash", tool_input={"command": "ls -la"})

    with pytest.raises(ConvergenceAbort):
        for _ in range(20):
            orch._agent_sink(ls, role=CODER_ROLE)
            orch._agent_sink(_USAGE, role=CODER_ROLE)


# ------------------------------- 3. the HOLD-OUT ----------------------------- #


def test_hold_out_a_328_plus_turn_run_with_periodic_progress_is_not_aborted(
    store, tmp_path,
):
    """THE HOLD-OUT (task-p2-brief.md): a real successful attempt has run
    ~328 turns (PLAN.md 4.3's own measurement behind `max_turns_per_attempt
    = 500`). This fixture runs 400 turns — past that measured real success —
    on the SHIPPED default knobs (`convergence_check_after_turns=80`,
    `convergence_window_turns=40`), editing a file every 30 turns (inside
    the 40-turn window — well inside the round-2 review's measured 25-turn
    widest gap in any real converging attempt). It must complete every turn
    without ever raising: the raw cap, not this heuristic, must be the only
    thing that can end a genuinely long, converging run."""
    orch = _orch(store, tmp_path)  # shipped defaults, no override
    orch._active_task_id = "task-1"
    tracker = ConvergenceTracker.from_config(orch.config.get("worker"))
    _arm(orch, tracker)
    assert tracker.min_turns == 80
    assert tracker.window == 40

    for i in range(400):
        orch._agent_sink(_read(i), role=CODER_ROLE)
        if i % 30 == 0:
            orch._agent_sink(_edit(f"src/calc_{i}.py"), role=CODER_ROLE)
        orch._agent_sink(_USAGE, role=CODER_ROLE)  # must never raise


# ------------------------------ 4. the kill switch --------------------------- #


def test_kill_switch_off_reproduces_todays_behaviour_exactly(store, tmp_path):
    """The EXACT input that aborts in test 1 above, replayed with
    `worker.abort_non_converging: false` — must run to completion. Off means
    off: only the raw cap and the hard stuck tiers can end the attempt."""
    orch = _orch(store, tmp_path, worker={
        "abort_non_converging": False,
        "convergence_check_after_turns": 5, "convergence_window_turns": 3,
    })
    orch._active_task_id = "task-1"
    tracker = ConvergenceTracker.from_config(orch.config.get("worker"))
    _arm(orch, tracker)
    assert tracker.enabled is False

    for i in range(20):
        orch._agent_sink(_read(i), role=CODER_ROLE)
        orch._agent_sink(_USAGE, role=CODER_ROLE)  # must never raise


def test_worker_config_default_is_abort_on(tmp_path):
    """`worker.abort_non_converging` defaults to True — the feature ships
    ON, per the brief."""
    from no_human.config import DEFAULT_CONFIG
    assert DEFAULT_CONFIG["worker"]["abort_non_converging"] is True


# --------------------- 5. cross-task / cross-session scoping ---------------- #


def test_a_latched_tracker_cannot_fire_in_a_different_tasks_session(store, tmp_path):
    """The worker pool reuses one Orchestrator: task A's attempt sets
    `_convergence`, never clears it, and `_run_attempt` never resets
    `_active_task_id` back to empty either. A tracker armed and already PAST
    its own threshold+window for task A must not fire once `_active_task_id`
    moves on to a DIFFERENT task B — same scoping shape as
    `test_budget_ceiling_is_scoped_to_the_running_task` in
    `test_stuck_abort.py`."""
    orch = _orch(store, tmp_path)
    stale = ConvergenceTracker(min_turns=1, window=1)
    stale.tick()
    stale.tick()
    stale.tick()
    assert stale.non_converging_reason is not None  # armed and already firing-ready
    _arm(orch, stale, task_id="task-A")

    orch._active_task_id = "task-B"  # a DIFFERENT task now owns the sink
    for i in range(10):
        orch._agent_sink(_read(i), role=CODER_ROLE)
        orch._agent_sink(_USAGE, role=CODER_ROLE)  # must not raise — wrong task


def test_active_convergence_returns_none_when_unset(store, tmp_path):
    """No `_convergence` at all (a fresh Orchestrator, or one that never ran
    an attempt) must read as None, not raise."""
    orch = _orch(store, tmp_path)
    orch._active_task_id = "task-1"
    assert orch._active_convergence() is None


async def test_run_code_review_diff_fetch_survives_a_latched_tracker(
    store, bare_repo, tmp_path, monkeypatch,
):
    """Regression for the exact crash the round-2 review's corpus replay
    found: `_run_code_review`'s read-only diff-fetch fallback streams
    through `_agent_sink` under the coder role with NO `ConvergenceAbort`
    handler, and — because a code_review task never calls `_run_attempt` —
    `_active_task_id` is never reassigned for it, so an id-scope check ALONE
    cannot tell this session apart from whatever earlier, unrelated task's
    attempt last latched a tracker. A tracker armed and already past its own
    threshold+window, left exactly as a reused Orchestrator would leave it,
    must not crash this fallback."""
    class _CleanReviewer:
        async def review(self, task, **kwargs):
            from no_human.review.reviewer import ReviewDecision
            return ReviewDecision(
                passed=True, checklist=[], raw_output="LGTM, no defects found.",
                tokens_used=10, cache_read_tokens=0, cache_creation_tokens=0,
            )

    class _DiffFetchBackend:
        """The fallback session: emits one 'usage' event — the exact tick a
        latched, already-past-threshold tracker would fire on if
        `_run_code_review` did not explicitly disarm it first."""

        async def run(self, prompt, *, cwd, max_turns, effort=None,
                      resume=None, on_event=None, supervisor_hook=None,
                      **kwargs):
            if on_event:
                on_event(_USAGE)
            return AgentResult(
                final_text="diff --git a/f.py b/f.py\n+x = 1\n", num_turns=1,
                is_error=False, tokens_used=10, session_id="s",
                stop_reason="end_turn",
            )

    orch = _orch(store, tmp_path, backend=_DiffFetchBackend(),
                 reviewer=_CleanReviewer())

    # The latch: an earlier, unrelated task's attempt left this armed and
    # already past its own threshold+window — nothing about a reused
    # Orchestrator or a code_review task's own pipeline ever clears it
    # except the fix under test.
    stale = ConvergenceTracker(min_turns=1, window=1)
    stale.tick()
    stale.tick()
    stale.tick()
    assert stale.non_converging_reason is not None
    _arm(orch, stale, task_id="some-earlier-task")
    orch._active_task_id = "some-earlier-task"  # _run_code_review never reassigns this

    monkeypatch.setattr(orch, "_fetch_pr_diff", lambda repo, url: "")

    async def _no_comments(pr_url):
        return ""
    monkeypatch.setattr(orch, "_fetch_pr_comments_text", _no_comments)

    t = Task.new("review this PR https://forge.example/x/y/pull/1",
                 repo_path=str(bare_repo), kind="code_review")
    await store.create_task(t)

    # The point of the test: this must not raise ConvergenceAbort (or crash
    # at all) despite the pre-armed, already-firing-ready tracker.
    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.DONE
    assert "LGTM" in (outcome.report or "")


# ------------------ 6. report-kind cap clamp + scratch-write credit --------- #


def test_from_config_clamps_min_turns_to_half_the_cap(tmp_path):
    """`_REPORT_KINDS` tasks run with an 80-turn `max_turns_per_attempt` —
    unclamped, the default `min_turns=80` sits almost exactly AT that cap.
    `cap=80` must clamp `min_turns` down to 40; a normal 500-turn cap must
    leave the default untouched (`min(80, 250) == 80`)."""
    small = ConvergenceTracker.from_config({}, cap=80)
    assert small.min_turns == 40

    normal = ConvergenceTracker.from_config({}, cap=500)
    assert normal.min_turns == 80

    # An explicit operator override below half the cap is never RAISED back up.
    already_small = ConvergenceTracker.from_config(
        {"convergence_check_after_turns": 10}, cap=80)
    assert already_small.min_turns == 10


def test_report_kind_shaped_run_is_not_aborted_before_its_final_write(
    store, tmp_path,
):
    """A synthetic `_REPORT_KINDS` shape (round-2 review corpus): an 80-turn
    per-attempt cap, periodic scratch-dir drafts (not committable, but real
    progress) every 15 turns, and real ticks running to 95 — past the
    nominal 80 — before the task ends. On the unclamped, scratch-blind
    behaviour this fired at turn 41 with zero progress ever recorded; fixed,
    it must run to completion."""
    orch = _orch(store, tmp_path)  # shipped defaults + the cap clamp
    orch._active_task_id = "task-1"
    tracker = ConvergenceTracker.from_config(orch.config.get("worker"), cap=80)
    _arm(orch, tracker)
    assert tracker.min_turns == 40  # clamped from the default 80

    for i in range(95):
        orch._agent_sink(_read(i), role=CODER_ROLE)
        if i % 15 == 0:
            orch._agent_sink(_scratch_write(i), role=CODER_ROLE)
        orch._agent_sink(_USAGE, role=CODER_ROLE)  # must never raise


def test_scratch_write_alone_does_not_reach_the_git_facing_edited_files_set(
    store, tmp_path,
):
    """The scratch-write progress credit must NOT leak into
    `_agent_edited_files` (what gets committed) or the edit-loop detector —
    both exist to bound COMMITTABLE churn, and an agent-owned path can never
    be committed."""
    orch = _orch(store, tmp_path)
    orch._active_task_id = "task-1"
    orch._active_repo_root = "/repo"
    _arm(orch, ConvergenceTracker())
    orch._stuck = None

    orch._agent_sink(
        AgentEvent("tool_use", tool_name="Write",
                   tool_input={"file_path": "/repo/.no_human/scratch/n.md",
                               "content": "x"}),
        role=CODER_ROLE,
    )

    assert getattr(orch, "_agent_edited_files", set()) == set()


# ---------------------- 7. test-runner MENTION vs EXECUTION ------------------ #


def test_a_grep_mentioning_a_runner_is_not_progress(store, tmp_path):
    """The planted red herring (round-2 review): `rg pytest` SEARCHES for
    the string "pytest" — it never runs anything. Must not count."""
    orch = _orch(store, tmp_path, worker={
        "convergence_check_after_turns": 5, "convergence_window_turns": 3,
    })
    orch._active_task_id = "task-1"
    _arm(orch, ConvergenceTracker.from_config(orch.config.get("worker")))
    rg = AgentEvent("tool_use", tool_name="Bash", tool_input={"command": "rg pytest"})

    with pytest.raises(ConvergenceAbort):
        for _ in range(20):
            orch._agent_sink(rg, role=CODER_ROLE)
            orch._agent_sink(_USAGE, role=CODER_ROLE)


def test_a_mention_followed_by_a_real_run_still_counts(store, tmp_path):
    """A compound command's EXECUTING segment still counts even when an
    earlier segment is a read-only search."""
    orch = _orch(store, tmp_path, worker={
        "convergence_check_after_turns": 5, "convergence_window_turns": 3,
    })
    orch._active_task_id = "task-1"
    tracker = ConvergenceTracker.from_config(orch.config.get("worker"))
    _arm(orch, tracker)
    compound = AgentEvent("tool_use", tool_name="Bash",
                          tool_input={"command": "rg pytest && pytest -q"})

    for i in range(30):
        orch._agent_sink(_read(i), role=CODER_ROLE)
        if i % 2 == 0:
            orch._agent_sink(compound, role=CODER_ROLE)
        orch._agent_sink(_USAGE, role=CODER_ROLE)  # must never raise
    assert tracker.non_converging_reason is None
    assert tracker._turns == 30


def test_a_git_log_mentioning_a_runner_is_not_progress(store, tmp_path):
    """`git log`/`git show`/`git diff`/`git grep` are read-only inspection
    too — a commit message or diff hunk that happens to mention a runner's
    name must not count."""
    orch = _orch(store, tmp_path, worker={
        "convergence_check_after_turns": 5, "convergence_window_turns": 3,
    })
    orch._active_task_id = "task-1"
    _arm(orch, ConvergenceTracker.from_config(orch.config.get("worker")))
    gitlog = AgentEvent("tool_use", tool_name="Bash",
                        tool_input={"command": 'git log --oneline --grep pytest'})

    with pytest.raises(ConvergenceAbort):
        for _ in range(20):
            orch._agent_sink(gitlog, role=CODER_ROLE)
            orch._agent_sink(_USAGE, role=CODER_ROLE)


# ------------------------------ end to end ----------------------------------- #


class _NonConvergingThenFixBackend:
    """Attempt 1 makes one real edit, then spins reads with no further
    progress past the convergence window; attempt 2 does the real work —
    same shape as `test_stuck_abort.py`'s `_DoomLoopThenFixBackend`."""

    def __init__(self):
        self.calls = 0

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        self.calls += 1
        if self.calls == 1:
            (cwd / "calc.py").write_text(
                "def add(a, b):\n    return a + b\n# WIP\n")
            if on_event:
                on_event(_edit("calc.py"))
                on_event(_USAGE)
                for i in range(20):
                    on_event(_read(i))
                    on_event(_USAGE)
            raise AssertionError("convergence check never aborted the attempt")
        if on_event:
            on_event(_edit("calc.py"))
        _mutate(cwd)
        return AgentResult(final_text="done", num_turns=2, is_error=False,
                           tokens_used=100, session_id="s", stop_reason="end_turn")


async def test_non_convergence_fails_the_attempt_and_the_loop_retries(
    store, bare_repo, tmp_path,
):
    """Full pipeline: a real Task, a real attempt loop, `run_task` end to
    end. The non-converging attempt fails honestly (checkpoint + true
    failure_reason) and the BOUNDED LOOP retries with fresh context — the
    task is never parked or faked done."""
    task = Task.new("add mul()", repo_path=str(bare_repo))
    task.acceptance_criteria = ["mul(a,b) returns a*b"]
    await store.create_task(task)

    backend = _NonConvergingThenFixBackend()
    orch = _orch(store, tmp_path, backend=backend, worker={
        "convergence_check_after_turns": 5, "convergence_window_turns": 3,
    })
    outcome = await orch.run_task(task)

    # ended the attempt, not the task: the bounded loop got its retry
    assert backend.calls == 2
    assert outcome.status is not TaskStatus.BLOCKED

    attempts = await store.list_attempts(task.id)
    first = attempts[0]
    assert first["status"] == "failed"
    assert "non-converging" in (first["failure_reason"] or "")
    assert "no file edit or test run" in (first["failure_reason"] or "")

    # attempt 1's edit-then-stall work survived as a checkpoint
    log = subprocess.run(
        ["git", "log", "--all", "--pretty=%s"], cwd=bare_repo,
        capture_output=True, text=True, check=True,
    ).stdout
    assert "[WIP-PARTIAL]" in log
