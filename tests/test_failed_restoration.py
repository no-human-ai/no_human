"""Failed-restoration fingerprint (gap-close W4, Paperclip P2).

D6 stagnation needs both attempts to reach REVIEW; most burn-then-quit
attempts die earlier (tests, tamper, build). When a retry ends in a
byte-equivalent durable state — same normalized failure detail, same diff
against base — the corrective preamble demonstrably changed nothing, and the
next attempt would be the 20×-cost class re-proving it. These tests pin: an
identical repeat escalates honestly after TWO attempts (never a third), a
retry that actually changes something keeps its full bounded loop, and the
escalation carries the STAGNATION blocker with the attempt log.
"""

from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task, TaskStatus
from no_human.notify.slack import SlackNotifier
from no_human.profile import ProjectProfile

from .test_e2e_orchestrator import FakeBackend, _config, bare_repo  # noqa: F401

# Fails on the broken tree, passes on base — so the already-fails-on-base
# repro comparison stays out of the way and the failure is REAL each attempt.
_TREE_GATE = "sh -c 'python3 -c \"from calc import add; assert add(1,2)==3\"'"


async def _gate_profile(store, repo_path):
    prof = ProjectProfile(
        repo_path=str(repo_path), ecosystem="custom", test_cmd=_TREE_GATE,
        derived_from=["test"], proven={"test_cmd": True}, confirmed=True,
    )
    await store.upsert_profile(prof)


def _broken_calc_same_way(cwd):
    """A real diff whose gate fails — identically, every attempt."""
    (cwd / "calc.py").write_text(
        "def add(a, b):\n    return a - b\n"   # the same wrong change each time
    )


async def test_identical_failing_retry_escalates_after_two_attempts(
    bare_repo, tmp_path, store
):
    cfg = _config(tmp_path)
    await _gate_profile(store, bare_repo)
    backend = FakeBackend(_broken_calc_same_way)
    orch = Orchestrator(store, cfg.data, backend, SlackNotifier(None))
    t = Task.new("make add subtract-proof", repo_path=str(bare_repo),
                 kind="feature")
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.ESCALATED
    assert len(await store.list_attempts(t.id)) == 2, (
        "the byte-equivalent second failure must stop the loop — a third "
        "attempt is the 20×-cost class re-proving the same wall")
    blocker = outcome.task.blocker
    assert blocker["category"] == "STAGNATION"
    assert "identical" in blocker["root_cause_hypothesis"]
    assert blocker["tried"], "the human sees the per-attempt log"


async def test_a_retry_that_changes_the_state_keeps_its_bounded_loop(
    bare_repo, tmp_path, store
):
    """Different failure each attempt = real exploration — the fingerprint
    must NOT fire; the loop runs its full bound and escalates through the
    ordinary exhaustion path instead."""
    calls = {"n": 0}

    def _broken_differently_each_time(cwd):
        calls["n"] += 1
        (cwd / "calc.py").write_text(
            f"def add(a, b):\n    return a - b - {calls['n']}\n"
        )

    cfg = _config(tmp_path)
    await _gate_profile(store, bare_repo)
    backend = FakeBackend(_broken_differently_each_time)
    orch = Orchestrator(store, cfg.data, backend, SlackNotifier(None))
    t = Task.new("make add robust", repo_path=str(bare_repo), kind="feature")
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.ESCALATED
    assert len(await store.list_attempts(t.id)) == 3, (
        "distinct failures are progress signals — the bounded loop keeps all "
        "its attempts")
    hypo = (outcome.task.blocker or {}).get("root_cause_hypothesis", "")
    assert "identical diff" not in hypo


async def test_a_resumed_loop_gets_its_full_bound_back(bare_repo, tmp_path, store):
    """`task.context` survives escalate -> `nh reply` -> fresh bounded loop, so
    a fingerprint left behind by the PREVIOUS loop made the resumed loop
    escalate after ONE attempt — the human answers, and their new direction
    never gets the corrective-preamble retry the bounded loop promises
    (the bounded-loop rule, constraint 5). Measured before the loop-entry clear existed: 'LOOP 2
    attempts: 1'."""
    cfg = _config(tmp_path)
    await _gate_profile(store, bare_repo)
    backend = FakeBackend(_broken_calc_same_way)
    orch = Orchestrator(store, cfg.data, backend, SlackNotifier(None))
    t = Task.new("make add subtract-proof", repo_path=str(bare_repo),
                 kind="feature")
    await store.create_task(t)

    first = await orch.run_task(t)
    assert first.status is TaskStatus.ESCALATED
    assert len(await store.list_attempts(t.id)) == 2

    # the human answers: the task re-enters the loop with its context intact
    resumed = await store.get_task(t.id)
    # The fingerprint is still ON the context here, deliberately: it is cleared
    # at the next loop's ENTRY, not at this loop's exit, so the stored value
    # stays readable as diagnosis. What must not happen is the NEXT loop
    # comparing against it.
    assert (resumed.context or {}).get("attempt_fingerprint")
    resumed.status = TaskStatus.PENDING
    await store.update_task(resumed)
    await store.set_status(resumed, TaskStatus.PENDING)

    before = len(await store.list_attempts(t.id))
    await orch.run_task(resumed)
    after = len(await store.list_attempts(t.id))
    assert after - before == 2, (
        f"the resumed loop got {after - before} attempt(s); a fresh loop must "
        "spend its own two before the fingerprint can fire")


async def test_an_unproductive_attempt_does_not_erase_the_fingerprint(
    bare_repo, tmp_path, store
):
    """The `unproductive_streak == 0` guard, pinned by the case that shows it
    is load-bearing — which the previous commit wrongly called defensive.

    A-B-A: a real-work failure, then a zero-diff attempt, then the SAME
    real-work failure. The zero-diff attempt reaches this block with
    `unproductive_streak == 1` (the >= 2 rung only RETURNS at 2). Without the
    guard it overwrites the stored fingerprint with its own signature, so the
    identical repeat two attempts later matches nothing and the stagnation
    escalation is lost — the loop then runs to its bound and reports
    "max_attempts reached" instead of "twice the same state". On report kinds,
    whose bound is 8, that is up to five extra attempts.
    """
    calls = {"n": 0}

    def _a_b_a(cwd):
        calls["n"] += 1
        if calls["n"] == 2:
            return None                      # zero-diff attempt: change nothing
        (cwd / "calc.py").write_text("def add(a, b):\n    return a - b\n")

    cfg = _config(tmp_path)
    cfg.data["bounds"]["max_attempts"] = 3
    await _gate_profile(store, bare_repo)
    orch = Orchestrator(store, cfg.data, FakeBackend(_a_b_a), SlackNotifier(None))
    t = Task.new("A then nothing then A", repo_path=str(bare_repo), kind="feature")
    await store.create_task(t)

    outcome = await orch.run_task(t)

    hypo = ((outcome.task.blocker or {}).get("root_cause_hypothesis") or "").lower()
    assert "identical" in hypo or "same state" in hypo, (
        "the identical repeat was not recognised — an unproductive attempt in "
        f"between overwrote the fingerprint: {hypo!r}")


def test_durations_are_normalized_out_of_the_fingerprint():
    """Two byte-identical runs differ only in reported seconds, and
    `error_signature` does not strip them — without the normalization the
    fingerprint never matches and the whole check is dead. Removing it changed
    nothing in any suite when this was reviewed."""
    from no_human.core.bounds import error_signature
    from no_human.core.orchestrator import _fingerprint_detail

    a = "tests failed: 1 error in 0.42s"
    b = "tests failed: 1 error in 1.7s"
    # control: without normalization these are DIFFERENT, so the assertion
    # below cannot pass by accident.
    assert error_signature(a) != error_signature(b)
    assert error_signature(_fingerprint_detail(a)) == error_signature(
        _fingerprint_detail(b))
