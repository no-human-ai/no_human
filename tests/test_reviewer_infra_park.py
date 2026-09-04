"""A review gate that never RAN is an infra incident, not a human decision.

Observed live twice on 2026-08-11 — tasks ``ad5cde99`` and ``7d63dbe1`` both sat
in ``escalated`` for hours, waiting for a person, over a quota outage. The
blocker they carried:

    category: NOVEL_UNKNOWN, transient: false, wake_condition: null
    "the reviewer reached no verdict after 2 rounds (reviewer session error
     (error)). The review gate did not run, so this diff is unreviewed."

Every word of the prose is true and the ROUTING is wrong: the reviewer's Agent
SDK session errored, which is the same class of failure as the dead transport
one method above already parks. Nothing about the diff was judged, so there is
nothing for a human to decide — the honest move is to park with a
machine-checkable wake condition and run the gate again.

What must NOT change, and is pinned here as the control: a reviewer that RAN and
could not reach a verdict on the CONTENT (turn-starved, no parseable
REVIEW_JSON, no reviewer configured) still escalates to a person, and the diff
never passes unreviewed either way.
"""

from datetime import datetime, timedelta, timezone

import pytest

from no_human.agent.claude_backend import AgentResult
from no_human.blockers.taxonomy import BlockerCategory
from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task, TaskStatus
from no_human.learning.queue import NON_LEARNABLE_CATEGORIES
from no_human.review.reviewer import AdversarialReviewer, ReviewerUnavailable

# The CLI's own wording for the wall these two tasks hit, and a generic session
# death that is NOT a transport failure (no "stream closed" / "connection
# error"), so the two branches are exercised by the shapes that actually occur.
_QUOTA_TEXT = (
    "You've hit your weekly limit. Your limit will reset at 3pm (Europe/Berlin)."
)
_GENERIC_ERROR_TEXT = (
    "Error: the agent session ended abnormally\n"
    "Traceback (most recent call last):\n"
    "  File \"cli.py\", line 1, in <module>\n"
    "RuntimeError: subprocess exited with code 1\n"
)

# The 2026-08-20 16:54 UTC incident shape: the account wall kills the SDK
# session before the CLI's own banner ever reaches `final_text`, so what
# lands here is the SDK's OWN wrapper exception and ITS OWN internal
# traceback frames — never the CLI's prose, and never a traceback naming
# user code or a missing binary.
_BARE_SDK_SUCCESS_TEXT = (
    "Claude Code returned an error result: success\n\n"
    "Traceback (most recent call last):\n"
    "  File \".venv/lib/python3.12/site-packages/claude_agent_sdk/_internal/"
    "query.py\", line 852, in _read_messages\n"
    "    raise Exception(\n"
    "Exception: Claude Code returned an error result: success\n"
)

# A genuinely different crash that happens to share nothing with the bare
# shape above except also being a session death — a missing binary, not the
# SDK's own wrapper exception. Corroboration alone must never be sufficient
# to reclassify this as quota.
_MISSING_BINARY_TEXT = (
    "Error: the agent session ended abnormally\n"
    "Traceback (most recent call last):\n"
    "  File \"claude_backend.py\", line 512, in run\n"
    "    proc = await asyncio.create_subprocess_exec(\"claude\", ...)\n"
    "FileNotFoundError: [Errno 2] No such file or directory: 'claude'\n"
)


class _ErroringBackend:
    """A reviewer backend whose SESSION dies — the SDK never raises, it hands
    the failure back as a normal result with ``is_error`` set."""

    def __init__(self, text: str, stop_reason: str = "error"):
        self.text = text
        self.stop_reason = stop_reason
        self.rounds = 0

    async def run(self, prompt, **kwargs):
        self.rounds += 1
        return AgentResult(
            final_text=self.text, num_turns=0, is_error=True, tokens_used=7,
            session_id=None, stop_reason=self.stop_reason)


async def _real_reason(text: str, tmp_path, *, stop_reason: str = "error") -> str:
    """The escalation detail THE REAL REVIEWER produces for a dead session.

    Hand-spelling the string here is the blind spot this avoids: the consumer
    would then be tested against an input the producer never emits (exactly the
    coupling `test_the_transport_marker_survives_the_reviewers_tail_window`
    exists for, one file over).
    """
    backend = _ErroringBackend(text, stop_reason)
    reviewer = AdversarialReviewer(backend=backend)
    with pytest.raises(ReviewerUnavailable) as exc:
        await reviewer._agent_review("prompt", tmp_path)
    assert backend.rounds == 2, "the bounded infra retry still runs first"
    return str(exc.value)


# --------------------------------------------------------------------------- #
# harness — the real store, the real `_raise_blocker`, the real watcher.        #
# --------------------------------------------------------------------------- #


def _orch_with(store, notes):
    class _Notifier:
        def notify(self, kind, line):
            notes.append((kind, line))

    orch = Orchestrator.__new__(Orchestrator)
    orch.store = store
    orch.notifier = _Notifier()
    orch.emit = lambda *a, **k: None
    orch.config = {}
    orch.learning_queue = None
    return orch


async def _new_task(store) -> Task:
    task = Task.new("do a thing", repo_path="/tmp/x")
    await store.create_task(task)
    await store.set_status(task, TaskStatus.CONTEXT)
    return task


async def _seed_paused_quota_sibling(
    store, *, wake_check_at: str, raised_at: str, auth_profile: str | None = None,
) -> Task:
    """A sibling task already parked `paused_quota` — the exact blocker shape
    `Orchestrator._park_quota` itself writes (category, wake_condition,
    raised_at, auth_profile), built directly against the store rather than
    through a live `QuotaExhausted` exception so the fixture stays a pure DB
    fact, independent of the coder path this task does not touch.

    `auth_profile=None` on purpose by default: the corroboration this task
    fixes only excludes a sibling when BOTH sides name a profile and they
    differ (`_corroborated_quota_wall`, mirroring
    `Scheduler.recover_quota_cooldown`) — an unattributed park, like a real
    single-profile install's, must still corroborate.
    """
    sibling = Task.new("some other reviewer task", repo_path="/tmp/y")
    await store.create_task(sibling)
    await store.set_status(sibling, TaskStatus.CONTEXT)
    sibling.wake_check_at = wake_check_at
    sibling.blocker = {
        "category": "QUOTA",
        "wake_condition": "quota_refreshed",
        "raised_at": raised_at,
        "root_cause_hypothesis": "subscription quota exhausted",
        "confidence": 1.0,
        "auth_profile": auth_profile,
    }
    await store.update_task_columns(sibling)
    await store.set_status(sibling, TaskStatus.PAUSED_QUOTA)
    return sibling


# --------------------------------------------------------------------------- #
# (a) the quota family                                                         #
# --------------------------------------------------------------------------- #


async def test_a_quota_killed_review_gate_parks_on_quota_and_never_escalates(
        store, tmp_path):
    """The exact incident. A quota wall took the reviewer's session down; the
    diff is unreviewed, not rejected, and a person has nothing to decide."""
    notes: list = []
    orch = _orch_with(store, notes)
    task = await _new_task(store)
    detail = await _real_reason(_QUOTA_TEXT, tmp_path)

    outcome = await orch._escalate_reviewer_unavailable(task, detail)
    parked = await store.get_task(task.id)

    assert outcome.status == TaskStatus.PAUSED_QUOTA, (
        "a billing wall burned a human escalation instead of parking")
    assert parked.blocker["category"] == BlockerCategory.QUOTA.value
    assert parked.blocker["transient"] is True
    assert parked.blocker["wake_condition"] == "quota_refreshed"
    assert parked.wake_check_at, "a quota park with no re-check stamp never wakes"
    assert parked.blocker["category"] in NON_LEARNABLE_CATEGORIES, (
        "an outage was proposed to the human as a durable code lesson")
    # The evidence a human reads still says the gate did not run.
    assert "unreviewed" in parked.blocker["evidence"]


# --------------------------------------------------------------------------- #
# (a2) the bare-shape death — quota only when the POOL corroborates it         #
# --------------------------------------------------------------------------- #


async def test_a_corroborated_bare_shape_death_parks_on_the_walls_own_clock(
        store, tmp_path):
    """2026-08-20 16:54 UTC: three reviewer sessions died with the bare,
    causeless "error result: success" in the SAME minute a sibling task
    parked `paused_quota` with the CLI's own banner. The dead session has no
    prose of its own to match — the pool already holds the answer. FAILS on
    unfixed code: the corroborated branch does not exist, so this death still
    falls through to TRANSIENT_INFRA/after:30m."""
    notes: list = []
    orch = _orch_with(store, notes)
    task = await _new_task(store)
    wall_resets_at = (
        datetime.now(timezone.utc) + timedelta(hours=2)
    ).isoformat()
    sibling_raised_at = (
        datetime.now(timezone.utc) - timedelta(minutes=2)
    ).isoformat()
    sibling = await _seed_paused_quota_sibling(
        store, wake_check_at=wall_resets_at, raised_at=sibling_raised_at)
    detail = await _real_reason(_BARE_SDK_SUCCESS_TEXT, tmp_path)

    outcome = await orch._escalate_reviewer_unavailable(task, detail)
    parked = await store.get_task(task.id)

    assert outcome.status == TaskStatus.PAUSED_QUOTA, (
        "a corroborated bare-shape death still burned TRANSIENT_INFRA/after:30m")
    assert parked.blocker["category"] == BlockerCategory.QUOTA.value
    assert parked.blocker["wake_condition"] == "quota_refreshed"
    assert parked.wake_check_at == wall_resets_at, (
        "the corroborated park must wake on the WALL's own clock, not a "
        "generic poll")
    assert sibling.id in parked.blocker["root_cause_hypothesis"], (
        "the blocker must name the corroborating park")
    assert parked.blocker["category"] in NON_LEARNABLE_CATEGORIES


async def test_a_reviewer_side_quota_park_is_stamped_with_the_paying_profile(
        store, tmp_path, monkeypatch):
    """The `_park_quota` contract: a park carries `auth_profile` as a FIELD
    so `Scheduler.recover_quota_cooldown` can tell whose wall it is after
    `nh auth use <other>` + restart. The reviewer-side park left it
    unstamped (review finding on PR #565), which the scheduler honours for
    EVERY profile — the next profile would idle behind this one's wall."""
    from no_human.core import orchestrator as orch_mod

    monkeypatch.setattr(orch_mod, "active_auth_profile", lambda: "personal")
    notes: list = []
    orch = _orch_with(store, notes)
    task = await _new_task(store)
    wall_resets_at = (
        datetime.now(timezone.utc) + timedelta(hours=2)
    ).isoformat()
    await _seed_paused_quota_sibling(
        store, wake_check_at=wall_resets_at,
        raised_at=(datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat())
    detail = await _real_reason(_BARE_SDK_SUCCESS_TEXT, tmp_path)

    outcome = await orch._escalate_reviewer_unavailable(task, detail)
    parked = await store.get_task(task.id)

    assert outcome.status == TaskStatus.PAUSED_QUOTA
    assert parked.blocker["auth_profile"] == "personal", parked.blocker
    assert parked.wake_check_at == wall_resets_at


async def test_a_sibling_park_whose_wall_is_still_closed_corroborates_beyond_the_window(
        store, tmp_path):
    """The spec's first disjunct — "the pool is in quota cooldown" — is the
    scheduler's predicate: a park whose recorded reset still lies ahead.
    A reviewer that dies 15 minutes into a 2-hour wall is on that wall
    even though no park was raised within the 10-minute window."""
    notes: list = []
    orch = _orch_with(store, notes)
    task = await _new_task(store)
    wall_resets_at = (
        datetime.now(timezone.utc) + timedelta(hours=2)
    ).isoformat()
    await _seed_paused_quota_sibling(
        store, wake_check_at=wall_resets_at,
        raised_at=(datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat())
    detail = await _real_reason(_BARE_SDK_SUCCESS_TEXT, tmp_path)

    outcome = await orch._escalate_reviewer_unavailable(task, detail)
    parked = await store.get_task(task.id)

    assert outcome.status == TaskStatus.PAUSED_QUOTA, (
        "a still-closed wall must corroborate regardless of when its park was raised")
    assert parked.wake_check_at == wall_resets_at


async def test_a_lapsed_stale_sibling_park_is_history_not_corroboration(
        store, tmp_path):
    """A park whose reset has PASSED and that was raised long ago says
    nothing about now: the bare-shape death keeps today's routing."""
    notes: list = []
    orch = _orch_with(store, notes)
    task = await _new_task(store)
    await _seed_paused_quota_sibling(
        store,
        wake_check_at=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
        raised_at=(datetime.now(timezone.utc) - timedelta(hours=3)).isoformat())
    detail = await _real_reason(_BARE_SDK_SUCCESS_TEXT, tmp_path)

    outcome = await orch._escalate_reviewer_unavailable(task, detail)
    parked = await store.get_task(task.id)

    assert outcome.status == TaskStatus.BLOCKED
    assert parked.blocker["category"] == BlockerCategory.TRANSIENT_INFRA.value


async def test_an_uncorroborated_bare_shape_death_still_parks_transient_infra(
        store, tmp_path):
    """The same bare-shape text, with NO sibling park and NO cooldown to
    corroborate it, must behave exactly as it does today: TRANSIENT_INFRA,
    after:30m. Corroboration is what changes the routing, not the marker
    alone — the identical phrase is also a genuine, quota-unrelated SDK
    crash (2026-08-13 incident)."""
    notes: list = []
    orch = _orch_with(store, notes)
    task = await _new_task(store)
    detail = await _real_reason(_BARE_SDK_SUCCESS_TEXT, tmp_path)

    outcome = await orch._escalate_reviewer_unavailable(task, detail)
    parked = await store.get_task(task.id)

    assert outcome.status == TaskStatus.BLOCKED
    assert parked.blocker["category"] == BlockerCategory.TRANSIENT_INFRA.value
    assert parked.blocker["wake_condition"] == "after:30m"


async def test_a_non_quota_cause_is_never_reclassified_during_a_cooldown(
        store, tmp_path):
    """A reviewer death whose text names a real, non-quota cause (a missing
    `claude` binary) must not be swept into QUOTA just because a sibling
    task happens to be parked on the wall at the same time — corroboration
    alone is not sufficient, the bare-shape marker must ALSO be present."""
    notes: list = []
    orch = _orch_with(store, notes)
    task = await _new_task(store)
    wall_resets_at = (
        datetime.now(timezone.utc) + timedelta(hours=2)
    ).isoformat()
    sibling_raised_at = (
        datetime.now(timezone.utc) - timedelta(minutes=2)
    ).isoformat()
    await _seed_paused_quota_sibling(
        store, wake_check_at=wall_resets_at, raised_at=sibling_raised_at)
    detail = await _real_reason(_MISSING_BINARY_TEXT, tmp_path)

    outcome = await orch._escalate_reviewer_unavailable(task, detail)
    parked = await store.get_task(task.id)

    assert outcome.status == TaskStatus.BLOCKED
    assert parked.blocker["category"] == BlockerCategory.TRANSIENT_INFRA.value
    assert parked.blocker["wake_condition"] == "after:30m"
    assert parked.wake_check_at != wall_resets_at


# --------------------------------------------------------------------------- #
# (b) any other dead reviewer session                                          #
# --------------------------------------------------------------------------- #


async def test_a_generic_dead_reviewer_session_parks_transient_with_a_timer(
        store, tmp_path):
    from no_human.blockers.wake import WakeWatcher

    notes: list = []
    orch = _orch_with(store, notes)
    task = await _new_task(store)
    detail = await _real_reason(_GENERIC_ERROR_TEXT, tmp_path)

    outcome = await orch._escalate_reviewer_unavailable(task, detail)
    parked = await store.get_task(task.id)

    assert outcome.status == TaskStatus.BLOCKED
    assert parked.blocker["category"] == BlockerCategory.TRANSIENT_INFRA.value
    assert parked.blocker["transient"] is True
    assert parked.blocker["wake_condition"] == "after:30m"
    # ...and the park really does self-fire, through the real watcher.
    cfg = {"blockers": {"max_park_duration": "48h"}}
    due = datetime.fromisoformat(parked.wake_check_at) + timedelta(minutes=1)
    assert (task.id, "resumed") in await WakeWatcher(store, cfg).tick(now=due)
    # The human still hears about an unreviewed diff the same minute (the same
    # `notify_override` reasoning the transport sibling documents).
    assert notes, "a dead review gate parked SILENTLY for max_park (48h)"


# --------------------------------------------------------------------------- #
# (c) THE CONTROL — a reviewer that RAN still reaches a human                   #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("detail", [
    # the reviewer explored, spoke, and produced nothing parseable
    "the reviewer reached no verdict after 2 rounds (no REVIEW_JSON block)",
    # turn-starved twice: it RAN, it was cut off mid-exploration
    "the reviewer reached no verdict after 2 rounds "
    "(reviewer session error (max_turns))",
    # an operator problem, not an outage
    "no reviewer is configured, so the review gate cannot run.",
])
async def test_a_reviewer_that_ran_still_escalates_to_a_human(store, detail):
    """Without this control the fix above passes for a function that parks
    EVERY unavailable reviewer — turning a genuinely stuck gate, and a
    misconfigured one, into an invisible retry loop."""
    notes: list = []
    orch = _orch_with(store, notes)
    task = await _new_task(store)

    outcome = await orch._escalate_reviewer_unavailable(task, detail)
    escalated = await store.get_task(task.id)

    assert outcome.status == TaskStatus.ESCALATED
    assert escalated.blocker["category"] == BlockerCategory.NOVEL_UNKNOWN.value
    assert escalated.blocker["transient"] is False
    assert not escalated.blocker.get("wake_condition")
    assert escalated.wake_check_at is None


async def test_two_truncated_rounds_are_not_a_dead_session(tmp_path):
    """The producer half of the control: a reviewer cut off at its turn budget
    must NOT be marked as a dead session, or the routing above cannot tell
    "turn-starved" from "the session died"."""
    from no_human.review.reviewer import REVIEW_SESSION_ERROR_MARKER

    detail = await _real_reason("partial exploration, no verdict yet",
                                tmp_path, stop_reason="max_turns")
    assert "reviewer session error (max_turns)" in detail
    assert REVIEW_SESSION_ERROR_MARKER not in detail


def test_the_marker_the_orchestrator_matches_is_the_one_the_reviewer_writes():
    """One constant, imported by both ends. A literal repeated in two files is
    the shape that silently stops matching (the transport marker's own lesson)."""
    from no_human.core import orchestrator
    from no_human.review import reviewer

    assert (orchestrator._SESSION_ERROR_BLOCKER_MARKER
            is reviewer.REVIEW_SESSION_ERROR_MARKER)


# --------------------------------------------------------------------------- #
# (d) the cap — an infinite park loop is worse than an escalation               #
# --------------------------------------------------------------------------- #


async def test_the_fourth_consecutive_infra_park_escalates_for_real(
        store, tmp_path):
    from no_human.core.orchestrator import _MAX_REVIEW_INFRA_PARKS

    assert _MAX_REVIEW_INFRA_PARKS == 3
    notes: list = []
    orch = _orch_with(store, notes)
    task = await _new_task(store)
    detail = await _real_reason(_GENERIC_ERROR_TEXT, tmp_path)

    for n in range(_MAX_REVIEW_INFRA_PARKS):
        outcome = await orch._escalate_reviewer_unavailable(task, detail)
        assert outcome.status == TaskStatus.BLOCKED, (
            f"park {n + 1} of {_MAX_REVIEW_INFRA_PARKS} should still self-heal")

    outcome = await orch._escalate_reviewer_unavailable(task, detail)
    final = await store.get_task(task.id)

    assert outcome.status == TaskStatus.ESCALATED, (
        "the gate has now failed to run 4 times — parking again is a loop")
    assert final.wake_check_at is None
    assert not final.blocker["wake_condition"], (
        "an escalated task the watcher never sweeps still advertises a retry")
    # The honest text survives: the human is told the diff is UNREVIEWED, and
    # that this was infrastructure rather than a finding about the change.
    report = f"{final.blocker['root_cause_hypothesis']} {final.blocker['evidence']}"
    assert "unreviewed" in report.lower()
    assert final.blocker["category"] in NON_LEARNABLE_CATEGORIES, (
        "four outages are still an outage, not a lesson about the repo")


async def test_a_quota_park_and_an_infra_park_share_one_budget(store, tmp_path):
    """The cap counts REVIEW PARKS, not one counter per category — alternating
    failures must not reset each other into an unbounded loop."""
    from no_human.core.orchestrator import _MAX_REVIEW_INFRA_PARKS

    orch = _orch_with(store, [])
    task = await _new_task(store)
    quota = await _real_reason(_QUOTA_TEXT, tmp_path)
    generic = await _real_reason(_GENERIC_ERROR_TEXT, tmp_path)

    details = [quota, generic, quota, generic][:_MAX_REVIEW_INFRA_PARKS + 1]
    statuses = [(await orch._escalate_reviewer_unavailable(task, d)).status
                for d in details]

    assert statuses[-1] == TaskStatus.ESCALATED
    assert TaskStatus.ESCALATED not in statuses[:-1]


async def test_a_corroborated_bare_shape_park_is_capped_like_any_other(
        store, tmp_path):
    """A prior review round found the corroborated branch returning through
    `_park_quota` BEFORE the `if spent:` escalation, so no number of
    consecutive corroborated bare-shape deaths could ever reach a human — an
    uncapped loop wearing `_MAX_REVIEW_INFRA_PARKS`'s clothes. Corroboration
    stays present on every attempt (the sibling park never expires mid-test),
    so the ONLY thing that can stop the loop is the shared park counter."""
    from no_human.core.orchestrator import _MAX_REVIEW_INFRA_PARKS

    orch = _orch_with(store, [])
    task = await _new_task(store)
    wall_resets_at = (
        datetime.now(timezone.utc) + timedelta(hours=2)
    ).isoformat()
    sibling_raised_at = (
        datetime.now(timezone.utc) - timedelta(minutes=2)
    ).isoformat()
    await _seed_paused_quota_sibling(
        store, wake_check_at=wall_resets_at, raised_at=sibling_raised_at)
    detail = await _real_reason(_BARE_SDK_SUCCESS_TEXT, tmp_path)

    for n in range(_MAX_REVIEW_INFRA_PARKS):
        outcome = await orch._escalate_reviewer_unavailable(task, detail)
        assert outcome.status == TaskStatus.PAUSED_QUOTA, (
            f"corroborated park {n + 1} of {_MAX_REVIEW_INFRA_PARKS} should "
            "still self-heal on the wall's clock")

    outcome = await orch._escalate_reviewer_unavailable(task, detail)
    final = await store.get_task(task.id)

    assert outcome.status == TaskStatus.ESCALATED, (
        "a corroborated bare-shape death bypassed the shared infra-park cap "
        "and parked forever instead of ever reaching a human")
    assert final.wake_check_at is None
    assert not final.blocker["wake_condition"]
    assert final.blocker["category"] in NON_LEARNABLE_CATEGORIES


# --------------------------------------------------------------------------- #
# (e) the wake resumes the REVIEW, from the coder's finished work               #
# --------------------------------------------------------------------------- #


async def test_the_wake_resumes_from_the_checkpoint_not_from_base(
        store, tmp_path):
    """The coder's work is COMPLETE when the gate dies — the resume must
    continue from the checkpoint the blocker recorded, or the next attempt
    branches from base and redoes everything the parked attempt committed."""
    from no_human.blockers.wake import WakeWatcher

    sha = "a" * 40

    class _Repo:
        path = tmp_path

        def has_changes(self):
            return False

        def head_sha(self):
            return sha

    orch = _orch_with(store, [])
    task = await _new_task(store)
    detail = await _real_reason(_GENERIC_ERROR_TEXT, tmp_path)

    await orch._escalate_reviewer_unavailable(
        task, detail, repo=_Repo(), branch="nh/t1-review")
    parked = await store.get_task(task.id)

    assert parked.blocker["resume_commit"] == sha
    assert parked.blocker["resume_branch"] == "nh/t1-review"

    due = datetime.fromisoformat(parked.wake_check_at) + timedelta(minutes=1)
    actions = await WakeWatcher(
        store, {"blockers": {"max_park_duration": "48h"}}).tick(now=due)
    assert (task.id, "resumed") in actions

    resumed = await store.get_task(task.id)
    assert resumed.context["resume_from"]["sha"] == sha, (
        "the resume threw away the reviewed-but-never-judged commit")
    assert resumed.context["resume_from"]["by"] == "wake"
