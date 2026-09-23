"""An UNEXPECTED honest stop (escalated/awaiting_input/blocked on a spec that
did not `expect_escalation`) was scored `goal_satisfied=False` and dropped —
nothing ever recorded whether the stop itself was the right call. issue #424.

The judge now runs on that path too, and its verdict lands in a NEW field,
`stop_judged_correct` — never inside `goal_satisfied` or any success rate:
the task was not completed either way, but "stopped for a real reason" and
"stopped for no reason" are different facts about the same failure.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from no_human.eval.bench_task import BenchTask
from no_human.eval.judge import GoalVerdict
from no_human.eval.northstar import BenchScore, NorthStarRunner


def _src_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "srcrepo"
    repo.mkdir()
    for args in (["init", "-b", "main"], ["config", "user.email", "t@t"],
                 ["config", "user.name", "t"]):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    (repo / "app.py").write_text("def f():\n    return 1\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True,
                   capture_output=True)
    return repo


def _spec(repo: Path, **kw) -> BenchTask:
    defaults = dict(
        id="ns-stop0001", title="t", request="do the thing",
        repo={"path": str(repo), "pin": "", "branch": "main"},
    )
    defaults.update(kw)
    return BenchTask(**defaults)


class _FakeOutcome:
    def __init__(self, status):
        self.status = status
        self.detail = ""


class _StubGoalJudge:
    def __init__(self, satisfied=True):
        self._satisfied = satisfied
        self.seen: dict | None = None

    async def judge(self, *, request, criteria, agent_diff, outcome_status,
                    repo_path, report=""):
        self.seen = {"request": request, "outcome_status": outcome_status}
        return GoalVerdict(satisfied=self._satisfied, evidence="stub verdict")


@pytest.mark.asyncio
@pytest.mark.parametrize("status_name", ["ESCALATED", "AWAITING_INPUT", "BLOCKED"])
async def test_an_unexpected_honest_stop_is_judged(tmp_path, status_name):
    from no_human.core.task import TaskStatus

    repo = _src_repo(tmp_path)
    spec = _spec(repo)  # expect_escalation defaults to False
    judge = _StubGoalJudge(satisfied=True)
    runner = NorthStarRunner({}, backend_factory=lambda s: None, goal_judge=judge)

    status = getattr(TaskStatus, status_name)
    score = await runner._score(
        spec, _FakeOutcome(status), repo, "HEAD", attempts=[], elapsed=1.0)

    assert score.goal_satisfied is False       # unchanged: the task was not done
    assert judge.seen is not None, "the judge was never called"
    assert score.stop_judged_correct is True
    assert "stop judged" in score.notes


@pytest.mark.asyncio
async def test_an_unsatisfied_stop_verdict_scores_false(tmp_path):
    from no_human.core.task import TaskStatus

    repo = _src_repo(tmp_path)
    spec = _spec(repo)
    judge = _StubGoalJudge(satisfied=False)
    runner = NorthStarRunner({}, backend_factory=lambda s: None, goal_judge=judge)

    score = await runner._score(
        spec, _FakeOutcome(TaskStatus.ESCALATED), repo, "HEAD",
        attempts=[], elapsed=1.0)

    assert score.goal_satisfied is False
    assert score.stop_judged_correct is False


@pytest.mark.asyncio
async def test_an_expected_escalation_is_not_judged(tmp_path):
    """expect_escalation=True is the ORIGINAL, already-tested path — the
    escalation IS the success criterion, so no judge call is warranted and
    the new field must stay untouched."""
    from no_human.core.task import TaskStatus

    repo = _src_repo(tmp_path)
    spec = _spec(repo, expect_escalation=True)
    judge = _StubGoalJudge(satisfied=True)
    runner = NorthStarRunner({}, backend_factory=lambda s: None, goal_judge=judge)

    score = await runner._score(
        spec, _FakeOutcome(TaskStatus.ESCALATED), repo, "HEAD",
        attempts=[], elapsed=1.0)

    assert score.goal_satisfied is True
    assert judge.seen is None, "expected escalations must not be judged"
    assert score.stop_judged_correct is None


@pytest.mark.asyncio
async def test_a_non_honest_non_gate_status_is_not_judged(tmp_path):
    """FAILED/PAUSED_QUOTA etc are non-gate but NOT honest stops — nothing
    about them claims to be a deliberate, justified stop, so there is no
    "was the stop correct" question to ask the judge."""
    from no_human.core.task import TaskStatus

    repo = _src_repo(tmp_path)
    spec = _spec(repo)
    judge = _StubGoalJudge(satisfied=True)
    runner = NorthStarRunner({}, backend_factory=lambda s: None, goal_judge=judge)

    score = await runner._score(
        spec, _FakeOutcome(TaskStatus.FAILED), repo, "HEAD",
        attempts=[], elapsed=1.0)

    assert score.goal_satisfied is False
    assert judge.seen is None
    assert score.stop_judged_correct is None


def _score(task_id, status, sat, stop_judged=None):
    return BenchScore(
        task_id=task_id, title=task_id, outcome_status=status,
        goal_satisfied=sat, escalated_honestly=(stop_judged is not None),
        mergeable=None, nh_tokens=1000, nh_cache_tokens=0,
        nh_cache_creation_tokens=0, nh_turns=1, nh_wall_clock_s=1.0,
        orig_tokens=10_000, orig_cache_tokens=0, orig_cache_creation_tokens=0,
        orig_wall_clock_s=1.0, orig_corrections=0,
        stop_judged_correct=stop_judged)


def test_card_reports_stop_judged_outside_the_success_rate(tmp_path):
    from no_human.eval.northstar_card import NorthStarCard

    scores = [
        _score("ok-1", "done", True),
        _score("ok-2", "done", True),
        _score("stop-correct", "escalated", False, stop_judged=True),
        _score("stop-wrong", "escalated", False, stop_judged=False),
        _score("stop-unjudged", "escalated", False, stop_judged=None),
    ]
    card = NorthStarCard(scores=scores, label="t")
    assert card.stops_judged == 2
    assert card.stops_judged_correct == 1
    # Never folded into the success rate: 2 of 5 measured rows satisfied,
    # regardless of how the 3 honest-stop rows' verdicts come out.
    assert card.success_rate == 2 / 5

    path = tmp_path / "card.json"
    card.save(path)
    loaded = NorthStarCard.load(path)
    assert loaded.stops_judged == 2
    assert loaded.stops_judged_correct == 1
    by_id = {s.task_id: s.stop_judged_correct for s in loaded.scores}
    assert by_id["stop-correct"] is True
    assert by_id["stop-wrong"] is False
    assert by_id["stop-unjudged"] is None
    assert by_id["ok-1"] is None


def test_a_legacy_score_dict_without_the_field_loads_as_none(tmp_path):
    """A card saved before this change has no `stop_judged_correct` key at
    all — it must load as `None` (no verdict), never `False` (a verdict of
    "not justified"). Those mean different things."""
    import json

    from no_human.eval.northstar_card import NorthStarCard

    legacy = NorthStarCard(scores=[_score("legacy-1", "done", True)], label="t")
    data = legacy.as_dict()
    for s in data["scores"]:
        s.pop("stop_judged_correct", None)
    del data["aggregate"]["stops_judged"]
    del data["aggregate"]["stops_judged_correct"]
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps(data), encoding="utf-8")

    loaded = NorthStarCard.load(path)
    assert loaded.scores[0].stop_judged_correct is None
