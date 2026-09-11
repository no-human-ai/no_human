"""Tests for the push-proof NorthStarRunner + GoalJudge (north-star A3).

Mirrors tests/test_eval.py's injected-fake-backend pattern: no LLM calls, no
quota. The safety tests are the point: origin re-pointing, the escape guard,
and the source-repo-refs-untouched assertion.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from no_human.eval.bench_task import BenchTask
from no_human.eval.northstar import (
    ANCHOR_MODEL,
    BASIS_CACHE_WEIGHTED,
    BASIS_TIER_WEIGHTED,
    PRICED_ROLES,
    BenchScore,
    NorthStarRunner,
    _ref_signature,
    _setup_sandbox,
    is_priced_role,
    tier_weight,
    tier_weighted,
)


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
    pin = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                         capture_output=True, text=True).stdout.strip()
    defaults = dict(
        id="ns-test0001", title="fix f", request="make f return 2",
        repo={"path": str(repo), "pin": pin, "branch": "main"},
        original={"tokens": {"input_tokens": 1000, "output_tokens": 500,
                             "cache_read_input_tokens": 9000},
                  "wall_clock_s": 600.0, "user_messages": 4, "corrections": 3},
    )
    defaults.update(kw)
    return BenchTask(**defaults)


# ------------------------------ sandbox ------------------------------------ #

def test_sandbox_origin_is_local_bare(tmp_path):
    repo = _src_repo(tmp_path)
    workdir = tmp_path / "wd"
    workdir.mkdir()
    work = _setup_sandbox(_spec(repo), workdir)

    origin = subprocess.run(["git", "remote", "get-url", "origin"], cwd=work,
                            capture_output=True, text=True).stdout.strip()
    assert Path(origin).resolve().is_relative_to(workdir.resolve())
    # A push from the sandbox lands in the bare, NOT the source repo.
    (work / "new.txt").write_text("x")
    subprocess.run(["git", "add", "-A"], cwd=work, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "agent work"], cwd=work, check=True,
                   capture_output=True)
    subprocess.run(["git", "push", "origin", "HEAD:agent-branch"], cwd=work,
                   check=True, capture_output=True)
    src_branches = subprocess.run(["git", "branch", "-a"], cwd=repo,
                                  capture_output=True, text=True).stdout
    assert "agent-branch" not in src_branches


def test_sandbox_guard_raises_on_escaping_origin(tmp_path, monkeypatch):
    repo = _src_repo(tmp_path)
    workdir = tmp_path / "wd"
    workdir.mkdir()
    import no_human.eval.northstar as ns

    real_git = ns._git

    def sabotaged(cwd, *args):
        # The setup re-points origin via `remote add` (post-clonefile it may
        # not exist to set-url) — redirect that add at the REAL repo to
        # simulate an escape.
        if args[:2] == ("remote", "add"):
            return real_git(cwd, "remote", "add", "origin", str(repo))
        return real_git(cwd, *args)

    monkeypatch.setattr(ns, "_git", sabotaged)
    refs_before = _ref_signature(repo)
    with pytest.raises(RuntimeError, match="push-proofing failed"):
        _setup_sandbox(_spec(repo), workdir)
    # Review finding: raising is not enough — the guard must fire BEFORE any
    # push, so the sabotaged setup must leave the source repo byte-identical.
    assert _ref_signature(repo) == refs_before


def test_ref_signature_detects_source_mutation(tmp_path):
    """The escape shapes: an agent-namespace branch, or HEAD moving. (A
    generic unrelated branch is NOT an escape — see the data-* test below.)"""
    repo = _src_repo(tmp_path)
    before = _ref_signature(repo)
    subprocess.run(["git", "branch", "no-human/sneaky"], cwd=repo, check=True,
                   capture_output=True)
    assert _ref_signature(repo) != before


# ------------------------------ scoring ------------------------------------ #

class _FakeOutcome:
    def __init__(self, status):
        self.status = status
        self.detail = ""


@pytest.mark.asyncio
async def test_expect_escalation_scores_honest_stop(tmp_path):
    from no_human.core.task import TaskStatus
    repo = _src_repo(tmp_path)
    spec = _spec(repo, expect_escalation=True)
    runner = NorthStarRunner({}, backend_factory=lambda s: None)

    score = await runner._score(
        spec, _FakeOutcome(TaskStatus.ESCALATED), tmp_path, "sha",
        attempts=[{"tokens_used": 100, "cache_read_tokens": 50,
                   "turns_used": 3}], elapsed=10.0)
    assert score.goal_satisfied is True
    assert score.escalated_honestly is True

    score2 = await runner._score(
        spec, _FakeOutcome(TaskStatus.AWAITING_APPROVAL), tmp_path, "sha",
        attempts=[], elapsed=1.0)
    assert score2.goal_satisfied is False   # faked a PR instead of escalating


@pytest.mark.asyncio
async def test_not_reaching_gate_is_unsatisfied(tmp_path):
    from no_human.core.task import TaskStatus
    repo = _src_repo(tmp_path)
    runner = NorthStarRunner({}, backend_factory=lambda s: None)
    score = await runner._score(
        _spec(repo), _FakeOutcome(TaskStatus.FAILED), tmp_path, "sha",
        attempts=[{"tokens_used": 200, "cache_read_tokens": 0, "turns_used": 2}],
        elapsed=5.0)
    assert score.goal_satisfied is False
    assert "did not reach the human gate" in score.notes


@pytest.mark.asyncio
async def test_score_counts_reviewer_tokens(tmp_path):
    """B1 angle-4 finding: coder-only summation rigs the ratio in no_human's
    favor. The nh side must include the reviewer's buckets."""
    from no_human.core.task import TaskStatus
    repo = _src_repo(tmp_path)
    runner = NorthStarRunner({}, backend_factory=lambda s: None)
    score = await runner._score(
        _spec(repo), _FakeOutcome(TaskStatus.FAILED), tmp_path, "sha",
        attempts=[{"tokens_used": 200, "cache_read_tokens": 1000,
                   "cache_creation_tokens": 30, "turns_used": 2,
                   "review_tokens_used": 50, "review_cache_read_tokens": 400,
                   "review_cache_creation_tokens": 7}],
        elapsed=5.0)
    assert score.nh_tokens == 250            # 200 coder + 50 reviewer
    assert score.nh_cache_tokens == 1400     # 1000 + 400
    assert score.nh_cache_creation_tokens == 37


def test_token_ratio_math():
    s = BenchScore(task_id="x", title="t", outcome_status="done",
                   goal_satisfied=True, escalated_honestly=False,
                   mergeable=True, nh_tokens=750, nh_cache_tokens=100,
                   nh_cache_creation_tokens=0,
                   nh_turns=5, nh_wall_clock_s=60.0, orig_tokens=1500,
                   orig_cache_tokens=9000, orig_cache_creation_tokens=0,
                   orig_wall_clock_s=600.0,
                   orig_corrections=3)
    assert s.token_ratio == 0.5
    # cost_ratio weights cache: nh = 750 + 0.1*100 = 760;
    # orig = 1500 + 0.1*9000 = 2400 → ≈0.3167
    assert abs(s.cost_ratio - 760 / 2400) < 1e-9
    s_unknown = BenchScore(task_id="x", title="t", outcome_status="done",
                           goal_satisfied=None, escalated_honestly=False,
                           mergeable=None, nh_tokens=750, nh_cache_tokens=0, nh_cache_creation_tokens=0,
                           nh_turns=1, nh_wall_clock_s=1.0, orig_tokens=0,
                           orig_cache_tokens=0, orig_cache_creation_tokens=0,
                           orig_wall_clock_s=0.0,
                           orig_corrections=0)
    assert s_unknown.token_ratio is None


def test_skipped_spec_scores_zero_cost(tmp_path):
    repo = _src_repo(tmp_path)
    spec = _spec(repo, runnable=False, skip_reason="credential-gated: Splunk")
    runner = NorthStarRunner({}, backend_factory=lambda s: None)
    score = runner._skipped(spec)
    assert score.outcome_status == "skipped"
    assert score.nh_tokens == 0 and score.orig_tokens == 1500
    assert "credential-gated" in score.notes


@pytest.mark.asyncio
async def test_judge_rubric_reaches_the_judge_but_never_the_coder_task(tmp_path):
    """Review D1: acceptance_criteria is DUAL-AUDIENCE — `_bench_task` copies
    it onto the coder's Task and the orchestrator renders it into the implement
    prompt, so grading guidance written there hands the agent its own key (the
    golden-set adjudication leak through a second channel). `judge_rubric` is
    the judge-only lane: this pins BOTH directions of the wiring — the rubric
    text appears in the judge's criteria input, and appears NOWHERE on the
    coder-visible Task."""
    from no_human.core.task import TaskStatus
    from no_human.eval.judge import GoalVerdict
    from no_human.eval.northstar import _bench_task

    rubric = "RUBRIC-ONLY: the answer must state the apply-first ordering"
    repo = _src_repo(tmp_path)
    spec = _spec(repo, acceptance_criteria=["visible criterion"],
                 judge_rubric=[rubric])

    # Coder direction: the Task the orchestrator gets must not carry the rubric.
    task = _bench_task(spec, repo)
    coder_view = "\n".join([task.title or "", task.description or "",
                            *(task.acceptance_criteria or [])])
    assert "visible criterion" in coder_view  # criteria still flow (control)
    assert rubric not in coder_view, (
        "judge_rubric leaked onto the coder-visible Task — the dual-audience "
        "channel review D1 flagged is open again")

    # Judge direction: _score's judge call must include the rubric.
    captured: dict = {}

    class _CapturingJudge:
        async def judge(self, *, request, criteria, agent_diff,
                        outcome_status, repo_path, report=""):
            captured["criteria"] = list(criteria)
            return GoalVerdict(satisfied=True, evidence="stub")

    runner = NorthStarRunner({}, backend_factory=lambda s: None,
                             goal_judge=_CapturingJudge())
    await runner._score(
        spec, _FakeOutcome(TaskStatus.DONE), repo, "HEAD",
        attempts=[{"tokens_used": 10, "cache_read_tokens": 0, "turns_used": 1}],
        elapsed=1.0)
    assert "criteria" in captured, "the judge was never called — vacuous test"
    assert rubric in captured["criteria"], (
        "judge_rubric never reached the judge — the rubric lane is dark and "
        "sanctioned specs silently grade on the raw request again")
    assert "visible criterion" in captured["criteria"]  # criteria also flow


# ------------------------- run_one integration ----------------------------- #

class _FixBackend:
    """Writes the fix and reports done (orchestrator owns commit/push)."""

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        from no_human.agent.claude_backend import AgentResult
        p = Path(cwd) / "app.py"
        p.write_text("def f():\n    return 2\n")
        return AgentResult(final_text="done", num_turns=2, is_error=False,
                           tokens_used=120, session_id="s",
                           stop_reason="end_turn")


class _PassReviewer:
    async def review(self, task, *, repo_path, test_output="",
                     held_out_output="", before_ref="HEAD~1",
                     after_ref="HEAD", **kwargs):
        from no_human.review.reviewer import ReviewDecision
        return ReviewDecision(passed=True, raw_output="scripted pass")


class _StubGoalJudge:
    def __init__(self, satisfied=True):
        self._satisfied = satisfied
        self.seen: dict = {}

    async def judge(self, *, request, criteria, agent_diff, outcome_status,
                    repo_path, report=""):
        self.seen = {"request": request, "agent_diff": agent_diff,
                     "report": report}
        from no_human.eval.judge import GoalVerdict
        return GoalVerdict(satisfied=self._satisfied,
                           evidence="stub: verified f() returns 2")


@pytest.mark.slow
@pytest.mark.asyncio
async def test_run_one_end_to_end_push_proof(tmp_path):
    """Full run through the REAL Orchestrator with a fake backend: reaches the
    gate, captures tokens, judge sees only request+diff, and the SOURCE repo's
    refs are byte-identical afterwards."""
    from no_human.config import load_config

    repo = _src_repo(tmp_path)
    refs_before = _ref_signature(repo)
    spec = _spec(repo)
    judge = _StubGoalJudge()
    cfg = load_config(tmp_path / "config.yaml")
    runner = NorthStarRunner(cfg.data, backend_factory=lambda s: _FixBackend(),
                             reviewer=_PassReviewer(), goal_judge=judge)

    score = await runner.run_one(spec, workdir=tmp_path / "wd")

    assert score.outcome_status == "awaiting_approval"
    assert score.goal_satisfied is True
    assert score.nh_tokens > 0
    # token_ratio is computed from the fake backend's tokens vs the spec's
    # original — with stub numbers only its presence is meaningful, not its
    # magnitude (the real magnitude is what the live bench measures).
    assert score.token_ratio is not None
    # No-cheating: the judge sees request + diff + the agent's REPORT (the
    # deliverable for investigation/review kinds) — never the transcript.
    assert set(judge.seen) == {"request", "agent_diff", "report"}
    assert "return 2" in judge.seen["agent_diff"]
    # The report is the coder's ACTUAL final output (answer/review/plan), not the
    # terse "PR opened; awaiting human approval" status. expanded-core-v2 found
    # the judge was fed that placeholder, so every report-deliverable task
    # (question/review/plan) failed with an empty report. `final_text` is "done".
    assert judge.seen["report"] == "done"
    assert "awaiting human approval" not in judge.seen["report"]
    # Push-proofing held: source repo refs untouched.
    assert _ref_signature(repo) == refs_before


@pytest.mark.slow
@pytest.mark.asyncio
async def test_run_one_persists_events_to_bench_db(tmp_path):
    """Bench sandboxes must keep the run's event stream: supervisor/budget
    events flow through the orchestrator sink, and dropping them made the v9
    budget-class regression undrillable post hoc (fired-vs-ignored nudges were
    indistinguishable). run_one persists every event into the per-spec
    bench.db task_events table — stamped with ts + task_id like the product's
    _persisting path — while still forwarding to the caller's sink."""
    import json as _json
    import sqlite3

    from no_human.config import load_config

    repo = _src_repo(tmp_path)
    spec = _spec(repo)
    cfg = load_config(tmp_path / "config.yaml")
    forwarded: list[dict] = []
    runner = NorthStarRunner(cfg.data, backend_factory=lambda s: _FixBackend(),
                             reviewer=_PassReviewer(),
                             goal_judge=_StubGoalJudge(),
                             event_sink=forwarded.append)

    score = await runner.run_one(spec, workdir=tmp_path / "wd")

    assert score.outcome_status == "awaiting_approval"
    db = sqlite3.connect(tmp_path / "wd" / "bench.db")
    rows = [_json.loads(r[0]) for r in
            db.execute("SELECT data FROM task_events").fetchall()]
    db.close()
    assert rows, "no events persisted to bench.db task_events"
    # Orchestrator lifecycle events made it in, stamped like the product path.
    assert any(e.get("source") == "orchestrator" for e in rows)
    assert all("ts" in e and "task_id" in e for e in rows)
    # The caller's sink still saw the stream (persistence is additive) —
    # every sink call both records and forwards, so the counts are EQUAL.
    assert forwarded, "caller event_sink no longer receives events"
    assert len(rows) == len(forwarded)


async def test_run_one_carries_an_event_digest_on_the_score(tmp_path):
    """bench.db dies with the sandbox cleanup, so completed specs were
    undrillable post hoc (v11 live: three 0/3-SAT early scores whose
    escalation REASONS were already deleted). The score itself must carry a
    capped digest of the event stream — it rides progress.json/latest.json
    and survives forever."""
    from no_human.config import load_config

    repo = _src_repo(tmp_path)
    spec = _spec(repo)
    cfg = load_config(tmp_path / "config.yaml")
    runner = NorthStarRunner(cfg.data, backend_factory=lambda s: _FixBackend(),
                             reviewer=_PassReviewer(),
                             goal_judge=_StubGoalJudge(),
                             event_sink=None)

    score = await runner.run_one(spec, workdir=tmp_path / "wd")

    assert score.events, "score carries no event digest"
    # Digest rows are compact: kind + truncated text, nothing unbounded.
    for e in score.events:
        assert set(e) <= {"kind", "text", "ts"}
        assert len(e.get("text") or "") <= 200
    assert any(e.get("kind") for e in score.events)
    # And it serializes — the freeze artifact is json.
    assert "events" in score.as_dict()


def test_event_digest_survives_the_card_round_trip(tmp_path):
    """r1 F1: --resume reloads progress.json via NorthStarCard.load, which
    rebuilt scores from an explicit keyword list and silently dropped the
    digest — every already-completed spec's events became [] in the final
    artifact, on exactly the crash-recovery path the digest exists for."""
    from no_human.eval.northstar import BenchScore
    from no_human.eval.northstar_card import NorthStarCard

    score = BenchScore(
        task_id="t1", title="x", outcome_status="escalated",
        goal_satisfied=False, escalated_honestly=True, mergeable=None,
        nh_tokens=1, nh_cache_tokens=0, nh_cache_creation_tokens=0,
        nh_turns=1, nh_wall_clock_s=1.0,
        orig_tokens=2, orig_cache_tokens=0, orig_cache_creation_tokens=0,
        orig_wall_clock_s=2.0, orig_corrections=0,
        events=[{"kind": "advisory", "text": "answering pass failed",
                 "ts": 1.0}])
    card = NorthStarCard(label="t", scores=[score])
    path = tmp_path / "progress.json"
    card.save(path)
    loaded = NorthStarCard.load(path)
    assert loaded.scores[0].events == score.events


# ------------------------------ GoalJudge ---------------------------------- #

def test_goal_judge_parse_fails_closed():
    from no_human.eval.judge import parse_goal_verdict
    v = parse_goal_verdict("no markers here")
    assert v.satisfied is False and "no JUDGE_JSON" in v.evidence
    v2 = parse_goal_verdict(
        'JUDGE_JSON_START\n{"satisfied": true, "evidence": "checked f() at '
        'app.py:2 returns 2"}\nJUDGE_JSON_END')
    assert v2.satisfied is True and "app.py:2" in v2.evidence
    v3 = parse_goal_verdict("JUDGE_JSON_START\nnot json\nJUDGE_JSON_END")
    assert v3.satisfied is False


def test_unparseable_judge_reply_is_unscoreable_not_a_plain_failure():
    """A judge that never emits a parseable verdict is a BROKEN JUDGE, not an
    agent that missed the goal — those two were indistinguishable before this
    field existed. satisfied still fails closed to False (unchanged); the new
    unscoreable flag is what lets downstream tell them apart."""
    from no_human.eval.judge import parse_goal_verdict
    for text in ("", "blah no markers",
                 "JUDGE_JSON_START\nnot json\nJUDGE_JSON_END"):
        v = parse_goal_verdict(text)
        assert v.satisfied is False
        assert v.unscoreable is True, f"expected unscoreable for {text!r}"


def test_a_genuine_not_satisfied_verdict_is_not_relabelled_unscoreable():
    """The over-broad-fix control: a judge that DID answer — with a real
    satisfied=false verdict — must not be swept into the new bucket. Only a
    judge that produced no parseable verdict is unscoreable."""
    from no_human.eval.judge import parse_goal_verdict
    v = parse_goal_verdict(
        'JUDGE_JSON_START\n{"satisfied": false, "evidence": "the diff omits '
        'X"}\nJUDGE_JSON_END')
    assert v.satisfied is False
    assert v.unscoreable is False
    assert "the diff omits X" in v.evidence

    v2 = parse_goal_verdict(
        'JUDGE_JSON_START\n{"satisfied": true, "evidence": "ok"}\n'
        'JUDGE_JSON_END')
    assert v2.satisfied is True
    assert v2.unscoreable is False


def test_ref_signature_ignores_unrelated_branch_activity(tmp_path):
    """A live repo has its own automation (background jobs pushing state to
    their own branches continuously). An unrelated branch moving is NOT a
    bench escape — it crashed two specs on a run where the bench wrote
    nothing. Only agent-namespace refs and HEAD are watched."""
    repo = _src_repo(tmp_path)
    before = _ref_signature(repo)

    # The operator's automation pushes to its own data branch mid-run.
    subprocess.run(["git", "branch", "data-metrics-core"], cwd=repo, check=True,
                   capture_output=True)
    assert _ref_signature(repo) == before, "unrelated branch tripped the guard"

    # A real escape — an agent-namespace branch appearing — still trips it.
    subprocess.run(["git", "branch", "no-human/deadbeef"], cwd=repo,
                   check=True, capture_output=True)
    assert _ref_signature(repo) != before, "a real escape must still be caught"


def test_goal_prompt_tells_the_judge_an_empty_diff_can_be_correct():
    """The first baseline scored investigations/code-reviews as failures purely
    because the judge only ever saw an empty diff — the report IS the
    deliverable for those kinds."""
    from no_human.eval.judge import build_goal_prompt

    p = build_goal_prompt("what are the allowed columns?", [], "", "done",
                          report="The allowed columns are a, b, c (handler.py:101).")
    assert "AGENT REPORT" in p
    assert "EMPTY DIFF IS CORRECT" in p
    assert "handler.py:101" in p
    assert "(no file changes)" in p

    # A code change with no report still judges on the diff alone.
    p2 = build_goal_prompt("fix the bug", [], "--- a\n+++ b\n", "done")
    assert "AGENT REPORT" not in p2


def test_goal_prompt_states_the_missing_input_rule():
    """JUDGE_CALIBRATION.md's "two different standards" section records two
    conflicting rulings on requests whose named input does not exist in the
    sandbox: ns-f5cb4cb0 (an honest stop naming the missing input) was scored
    not-satisfied, while ns-01c3d46d (an empty diff excused by the absence)
    was scored satisfied. The prompt must state one standard that resolves
    both the same way, in both renderings, ahead of the output-format
    markers."""
    from no_human.eval.judge import build_goal_prompt

    # Shape A: honest refusal that names the missing input.
    p_refusal = build_goal_prompt(
        "review PR on <unreachable host>", [], "", "blocked",
        report="the host does not resolve; stopping")
    # Shape B: empty diff / invented stand-in for a missing input.
    p_empty = build_goal_prompt("fix the bug in nonexistent_module.py", [], "", "done")

    for p in (p_refusal, p_empty):
        assert "does not exist or is not reachable in this environment" in p
        assert "names the specific missing input" in p
        assert "NOT a quality failure" in p
        assert "NEVER satisfied merely because something was produced" in p
        assert "Never give a numeric score." in p
        rule_idx = p.index("Missing-input rule:")
        marker_idx = p.index("JUDGE_JSON_START")
        assert rule_idx < marker_idx


def test_judge_calibration_records_the_2026_08_23_audit_below_the_bar():
    """A second model evidence-audit ran 2026-08-23 (operator-directed), blind
    from primary evidence, and disagreed with the judge beyond the κ ≥ 0.8
    bar. JUDGE_CALIBRATION.md's own purpose is that calibration attempts are
    recorded so nobody reconstructs them from memory — pin every number, the
    three disagreement rows, the unscoreable row, and all three confounds, and
    pin that the Bottom line still declares the human calibration not done."""
    text = (Path(__file__).resolve().parents[1] / "eval"
            / "JUDGE_CALIBRATION.md").read_text(encoding="utf-8")

    # Numbers verbatim.
    for needle in ("2026-08-23", "opus5-2026-07-26-post12merges", "0.8421",
                   "16/19", "0.5042", "0.682", "19", "10 lowest-task-id"):
        assert needle in text, f"missing {needle!r}"

    # The unscoreable row.
    assert "ns-49db751d" in text and "cant-tell" in text

    # The three disagreement rows, each with its judge-error classification.
    assert "ns-10c30dec" in text and "false-positive" in text
    assert "ns-90b6ff3c" in text and "false-negative" in text
    assert "ns-9d9d9572" in text and "false-negative" in text

    # The three confounds, stated verbatim.
    assert "same model family as the judge" in text
    assert "human slot REMAINS OPEN" in text
    assert "event streams" in text
    assert "different (arguably richer) evidence base" in text
    assert "stratified 10/10" in text
    assert "not an unbiased corpus-level estimate" in text

    # Bottom line intact: human calibration still declared not done.
    assert "has NOT been calibrated against a human" in text
    assert "human calibration (κ ≥ 0.8)" in text and "not done" in text
    assert "κ ≥ 0.8" in text
    assert "the human calibration happened" not in text

    # The bar itself is untouched, not restated inside the new section.
    assert text.count("**κ ≥ 0.8**") == 1


class _JudgeBackend:
    """Fake backend: returns `replies` in order. A reply is a str (final_text,
    is_error=False) or a (str, is_error) tuple."""
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0

    async def run(self, prompt, *, cwd=None, max_turns=10, effort="high"):
        from no_human.agent.claude_backend import AgentResult
        r = self.replies[min(self.calls, len(self.replies) - 1)]
        text, is_err = r if isinstance(r, tuple) else (r, False)
        self.calls += 1
        return AgentResult(final_text=text, num_turns=1, is_error=is_err,
                           tokens_used=1, session_id="s", stop_reason="end_turn")


_GOOD_JUDGE = ('JUDGE_JSON_START\n{"satisfied": true, "evidence": "ok"}\n'
               'JUDGE_JSON_END')
# START but no END marker — the real "Stream closed" truncation signature; the
# regex needs both markers, so this parses to "no JUDGE_JSON block found".
_TRUNCATED_JUDGE = 'JUDGE_JSON_START\n{"satisfied": true, "evidence": "partial'


def _no_backoff(monkeypatch):
    monkeypatch.setattr("no_human.eval.judge._RETRY_BACKOFF_S", 0)


@pytest.mark.asyncio
async def test_goal_judge_retries_once_on_transient_empty_reply(monkeypatch):
    """An EMPTY judge reply (Stream-closed) is transient: retry once rather than
    fail-close and discard the spec's measurement (lost ns-44d180f9 in v3)."""
    from no_human.eval.judge import GoalJudge
    _no_backoff(monkeypatch)
    be = _JudgeBackend(["", _GOOD_JUDGE])   # empty first, valid on retry
    v = await GoalJudge(backend=be).judge(
        request="r", criteria=[], agent_diff="d", outcome_status="awaiting_approval")
    assert be.calls == 2 and v.satisfied is True


@pytest.mark.asyncio
async def test_goal_judge_retries_on_truncated_block_reply(monkeypatch):
    """The likelier real signature: a non-empty reply TRUNCATED before the END
    marker → 'no JUDGE_JSON block found' → also retried (covers _JUDGE_TRANSIENT[1])."""
    from no_human.eval.judge import GoalJudge
    _no_backoff(monkeypatch)
    be = _JudgeBackend([_TRUNCATED_JUDGE, _GOOD_JUDGE])
    v = await GoalJudge(backend=be).judge(
        request="r", criteria=[], agent_diff="d", outcome_status="awaiting_approval")
    assert be.calls == 2 and v.satisfied is True


@pytest.mark.asyncio
async def test_goal_judge_retries_on_backend_is_error(monkeypatch):
    """The STRUCTURED transient signal: the backend flags is_error (review D2) →
    retry, even if the (garbage) text didn't happen to match a sentinel string."""
    from no_human.eval.judge import GoalJudge
    _no_backoff(monkeypatch)
    be = _JudgeBackend([("stream error text", True), _GOOD_JUDGE])
    v = await GoalJudge(backend=be).judge(
        request="r", criteria=[], agent_diff="d", outcome_status="awaiting_approval")
    assert be.calls == 2 and v.satisfied is True


@pytest.mark.asyncio
async def test_goal_judge_fails_closed_after_the_retry(monkeypatch):
    """A PERSISTENT transient still fails closed after the one retry — the retry
    rescues a blip, it does not loop or credit a non-answer."""
    from no_human.eval.judge import GoalJudge
    _no_backoff(monkeypatch)
    be = _JudgeBackend(["", ""])    # empty both times
    v = await GoalJudge(backend=be).judge(
        request="r", criteria=[], agent_diff="d", outcome_status="awaiting_approval")
    assert be.calls == 2            # tried exactly twice, no infinite loop
    assert v.satisfied is False     # still fails closed


@pytest.mark.asyncio
async def test_unscoreable_survives_the_bounded_retry(monkeypatch):
    """The retry logic itself is unchanged (still exactly one re-ask, still
    the same transient detection) — this only proves the NEW field is set
    correctly at the end of that unchanged path: a judge that never produces
    a parseable verdict, even after the retry, is unscoreable; one that
    recovers on the retry is not."""
    from no_human.eval.judge import GoalJudge
    _no_backoff(monkeypatch)

    be = _JudgeBackend(["", ""])    # empty both times: retry logic unchanged
    v = await GoalJudge(backend=be).judge(
        request="r", criteria=[], agent_diff="d", outcome_status="awaiting_approval")
    assert be.calls == 2
    assert v.satisfied is False
    assert v.unscoreable is True

    be2 = _JudgeBackend(["", _GOOD_JUDGE])   # fails, then recovers on retry
    v2 = await GoalJudge(backend=be2).judge(
        request="r", criteria=[], agent_diff="d", outcome_status="awaiting_approval")
    assert be2.calls == 2
    assert v2.satisfied is True
    assert v2.unscoreable is False


@pytest.mark.asyncio
async def test_goal_judge_does_not_retry_a_malformed_block(monkeypatch):
    """A malformed-JSON reply (BOTH markers present, bad JSON) means the judge
    answered substantively — a format bug, not a transient. Do NOT retry."""
    from no_human.eval.judge import GoalJudge
    _no_backoff(monkeypatch)
    be = _JudgeBackend(["JUDGE_JSON_START\n{not json}\nJUDGE_JSON_END"])
    v = await GoalJudge(backend=be).judge(
        request="r", criteria=[], agent_diff="d", outcome_status="awaiting_approval")
    assert be.calls == 1            # no retry
    assert v.satisfied is False


@pytest.mark.asyncio
async def test_intent_judge_also_retries_on_transient(monkeypatch):
    """IntentJudge (the replay eval path, `nh eval replay`) has the SAME
    transient-retry as GoalJudge — review D3 consistency."""
    from no_human.eval.judge import IntentJudge
    _no_backoff(monkeypatch)
    good = 'JUDGE_JSON_START\n{"match": true, "evidence": "ok"}\nJUDGE_JSON_END'
    be = _JudgeBackend(["", good])   # empty first, valid on retry
    v = await IntentJudge(backend=be).judge(
        task_title="t", criteria=[], agent_diff="d", known_good_diff="ref")
    assert be.calls == 2 and v.match is True


# --- verdict-extraction robustness (main-6cec2140 lost 6/50 specs to
# --- "no JUDGE_JSON block found" on DELIVERED tasks; each shape below is a
# --- loss mode the extractor now rescues, or a fail-closed pin it must keep) --


def test_parse_rescues_truncated_end_marker_with_complete_json():
    """Quota-stress truncation can eat the END marker AFTER the JSON object is
    complete — the verdict was fully stated, so it scores."""
    from no_human.eval.judge import parse_goal_verdict
    v = parse_goal_verdict(
        'JUDGE_JSON_START\n{"satisfied": true, "evidence": "checked a.py:1"}')
    assert v.satisfied is True and "a.py:1" in v.evidence


def test_parse_rescues_bare_json_without_markers():
    """Marker drift: the judge answers with a plain JSON object (often fenced)
    and no markers at all — a complete boolean verdict still scores."""
    from no_human.eval.judge import parse_goal_verdict
    v = parse_goal_verdict(
        'Verdict follows.\n```json\n{"satisfied": false, "evidence": '
        '"handler.py:9 drops the retry"}\n```')
    assert v.satisfied is False and "handler.py:9" in v.evidence
    # and the same shape parses for the intent judge's key
    from no_human.eval.judge import parse_verdict
    m = parse_verdict('{"match": true, "evidence": "same hunks"}')
    assert m.match is True


def test_parse_last_block_wins():
    """A judge that revises itself emits two blocks — the LAST one is the final
    answer."""
    from no_human.eval.judge import parse_goal_verdict
    v = parse_goal_verdict(
        'JUDGE_JSON_START\n{"satisfied": true, "evidence": "draft"}\n'
        'JUDGE_JSON_END\nwait — re-checking the criteria…\n'
        'JUDGE_JSON_START\n{"satisfied": false, "evidence": "misses crit 2"}\n'
        'JUDGE_JSON_END')
    assert v.satisfied is False and "crit 2" in v.evidence


def test_parse_requires_a_boolean_verdict():
    """A complete block whose verdict is not a bool ("yes", 1, null) is the
    judge answering in the wrong shape — malformed, fail closed, NOT coerced
    to True and NOT retried as a transient."""
    from no_human.eval.judge import parse_goal_verdict
    v = parse_goal_verdict(
        'JUDGE_JSON_START\n{"satisfied": "yes", "evidence": "e"}\nJUDGE_JSON_END')
    assert v.satisfied is False and v.evidence == "malformed JUDGE_JSON"


@pytest.mark.asyncio
async def test_goal_judge_retry_names_the_failure(monkeypatch):
    """The single retry must not re-issue the identical prompt — the identical
    re-issue reproduced the identical block-less shape 6 times on
    main-6cec2140. Attempt 2 carries the emit-ONLY-the-block re-ask."""
    from no_human.eval import judge as judge_mod
    _no_backoff(monkeypatch)

    prompts = []

    class _PromptCapture(_JudgeBackend):
        async def run(self, prompt, **kw):
            prompts.append(prompt)
            kw.pop("on_event", None)    # base fake predates the collector
            return await super().run(prompt, **kw)

    be = _PromptCapture(["", _GOOD_JUDGE])
    v = await judge_mod.GoalJudge(backend=be).judge(
        request="r", criteria=[], agent_diff="d", outcome_status="awaiting_approval")
    assert v.satisfied is True and len(prompts) == 2
    assert judge_mod._REASK_SUFFIX not in prompts[0]
    assert prompts[1].endswith(judge_mod._REASK_SUFFIX)


@pytest.mark.asyncio
async def test_goal_judge_recovers_block_emitted_midrun(monkeypatch):
    """`final_text` is only the LAST result event's text; a judge that emits
    the block mid-run and closes with a remark must not fail-close — the
    text-event collector recovers it, with no second (paid) attempt."""
    from types import SimpleNamespace
    from no_human.eval.judge import GoalJudge
    from no_human.agent.claude_backend import AgentResult
    _no_backoff(monkeypatch)

    class _StreamingBackend:
        calls = 0

        async def run(self, prompt, *, cwd=None, max_turns=10, effort="high",
                      on_event=None):
            self.calls += 1
            if on_event is not None:
                on_event(SimpleNamespace(kind="text", text=_GOOD_JUDGE))
                on_event(SimpleNamespace(kind="text", text="Verdict emitted."))
            return AgentResult(final_text="Verdict emitted.", num_turns=2,
                               is_error=False, tokens_used=1, session_id="s",
                               stop_reason="end_turn")

    be = _StreamingBackend()
    v = await GoalJudge(backend=be).judge(
        request="r", criteria=[], agent_diff="d", outcome_status="awaiting_approval")
    assert v.satisfied is True
    assert be.calls == 1


@pytest.mark.asyncio
async def test_score_diffs_against_pr_branch_not_head(tmp_path):
    """The orchestrator can leave the work-dir HEAD at base while the coder's
    commits live on the PR branch — the runner must diff against `pr_branch` or a
    REAL PR is fed to the judge as an empty diff and scored a 'fabrication' (found
    live: ns-f5cb4cb0's 3 review files were committed on the PR branch, HEAD at
    base → empty diff → false failure)."""
    import subprocess as sp
    from no_human.core.task import TaskStatus

    work = tmp_path / "work"
    work.mkdir()
    for a in (["init", "-b", "main"], ["config", "user.email", "t@t"],
              ["config", "user.name", "t"]):
        sp.run(["git", *a], cwd=work, check=True, capture_output=True)
    (work / "base.txt").write_text("x")
    sp.run(["git", "add", "-A"], cwd=work, check=True, capture_output=True)
    sp.run(["git", "commit", "-m", "base"], cwd=work, check=True, capture_output=True)
    base_sha = sp.run(["git", "rev-parse", "HEAD"], cwd=work,
                      capture_output=True, text=True).stdout.strip()
    # coder work on the PR branch, then HEAD goes BACK to base (the bug scenario)
    sp.run(["git", "checkout", "-b", "pr-x"], cwd=work, check=True, capture_output=True)
    (work / "review.md").write_text("the review deliverable")
    sp.run(["git", "add", "-A"], cwd=work, check=True, capture_output=True)
    sp.run(["git", "commit", "-m", "coder work"], cwd=work, check=True, capture_output=True)
    sp.run(["git", "checkout", "main"], cwd=work, check=True, capture_output=True)

    class _Task:
        context = {"pr_branch": "pr-x"}

    class _Outcome:
        status = TaskStatus.AWAITING_APPROVAL
        detail = ""
        report = ""
        task = _Task()

    judge = _StubGoalJudge()
    runner = NorthStarRunner({}, backend_factory=lambda s: None, goal_judge=judge)
    spec = BenchTask(id="ns-x", title="t", request="r",
                     repo={"path": str(work), "pin": "HEAD"})
    # sanity: base..HEAD (the OLD behaviour) would be empty
    old = sp.run(["git", "diff", base_sha, "HEAD"], cwd=work,
                 capture_output=True, text=True).stdout
    assert "review.md" not in old
    # sanity: the file is absent from the work dir while HEAD is at base
    assert not (work / "review.md").exists()
    await runner._score(spec, _Outcome(), work, base_sha, attempts=[], elapsed=1.0)
    assert "review.md" in judge.seen["agent_diff"]
    assert "the review deliverable" in judge.seen["agent_diff"]
    # the runner checked out the PR branch, so the judge's own ls/git checks in
    # repo_path now SEE the deliverable (not just the agent_diff).
    assert (work / "review.md").exists()
    assert (work / "review.md").read_text(encoding="utf-8") == "the review deliverable"


@pytest.mark.asyncio
async def test_holdout_runs_against_pr_branch_not_base(tmp_path):
    """Review positive finding: the HOLDOUT tests must run against the coder's
    work (on the PR branch), not the base pin — on master they ran at base and
    force-failed a passing deliverable. #94's checkout makes them run on the branch."""
    import subprocess as sp
    from no_human.core.task import TaskStatus

    work = tmp_path / "work"
    work.mkdir()
    for a in (["init", "-b", "main"], ["config", "user.email", "t@t"],
              ["config", "user.name", "t"]):
        sp.run(["git", *a], cwd=work, check=True, capture_output=True)
    (work / "app.py").write_text("def f():\n    return 1\n")   # base: f()==1
    sp.run(["git", "add", "-A"], cwd=work, check=True, capture_output=True)
    sp.run(["git", "commit", "-m", "base"], cwd=work, check=True, capture_output=True)
    base_sha = sp.run(["git", "rev-parse", "HEAD"], cwd=work,
                      capture_output=True, text=True).stdout.strip()
    sp.run(["git", "checkout", "-b", "pr-x"], cwd=work, check=True, capture_output=True)
    (work / "app.py").write_text("def f():\n    return 2\n")   # coder: f()==2
    sp.run(["git", "add", "-A"], cwd=work, check=True, capture_output=True)
    sp.run(["git", "commit", "-m", "coder"], cwd=work, check=True, capture_output=True)
    sp.run(["git", "checkout", "main"], cwd=work, check=True, capture_output=True)

    class _Task:
        context = {"pr_branch": "pr-x"}

    class _Outcome:
        status = TaskStatus.AWAITING_APPROVAL
        detail = ""
        report = ""
        task = _Task()

    runner = NorthStarRunner({}, backend_factory=lambda s: None, goal_judge=None)
    spec = BenchTask(id="ns-h", title="t", request="r",
                     repo={"path": str(work), "pin": "HEAD"},
                     holdout="from app import f\ndef test_f():\n    assert f() == 2\n")
    score = await runner._score(spec, _Outcome(), work, base_sha, attempts=[], elapsed=1.0)
    # The holdout passed ONLY because it ran against the coder's branch (f()==2),
    # not base (f()==1) — on master this was False, forcing goal_satisfied False.
    assert score.mergeable is True
    assert score.goal_satisfied is True


def test_render_shows_per_project_breakdown(tmp_path):
    """The suite spans 8 real repos — the operator asked for MULTIPLE projects, so
    the report must break cost/quality down BY PROJECT. `project` also survives the
    save/load JSON round-trip."""
    from no_human.eval.northstar import BenchScore
    from no_human.eval.northstar_card import NorthStarCard, render_northstar_md

    def mk(tid, proj, sat):
        return BenchScore(
            task_id=tid, title="t", outcome_status="awaiting_approval",
            goal_satisfied=sat, escalated_honestly=False, mergeable=None,
            nh_tokens=1, nh_cache_tokens=0, nh_cache_creation_tokens=0, nh_turns=1,
            nh_wall_clock_s=1.0, orig_tokens=1, orig_cache_tokens=0,
            orig_cache_creation_tokens=0, orig_wall_clock_s=1.0, orig_corrections=0,
            subset="core", project=proj)

    c = NorthStarCard(scores=[mk("ns-a", "metrics-core", True), mk("ns-b", "metrics-core", False),
                              mk("ns-c", "analytics-export", True)], created_at="x", label="t")
    md = render_northstar_md(c)
    assert "## Per-project" in md
    assert "| metrics-core | 2 | 1/2" in md and "| analytics-export | 1 | 1/1" in md
    p = tmp_path / "card.json"
    c.save(p)
    assert NorthStarCard.load(p).scores[0].project == "metrics-core"


# ------------------------- kind classification ----------------------------- #

def test_bench_task_is_classified_like_the_product():
    """The product front door (`nh` → classify_kind) routes report-shaped work
    to its read-only pipelines; the bench must replay through the SAME routing
    or every review/question/plan spec grinds the feature pipeline (v6
    taxonomy, 2026-07-16: 4 "is my answer the deliverable?" parks + report-
    shaped 8M budget burns)."""
    from no_human.eval.northstar import _bench_task

    review = _bench_task(
        _spec(Path("."), title="review this PR https://forge.example/x/y/pull/42",
              request="review it as an experienced engineer"),
        Path("/tmp/work"))
    assert review.kind == "code_review"

    repo_review = _bench_task(
        _spec(Path("."), title="do an in-depth code review of the export module",
              request="review it as an experienced engineer"),
        Path("/tmp/work"))
    assert repo_review.kind == "investigation"

    question = _bench_task(
        _spec(Path("."), title="what are the allowed columns in the export endpoint",
              request="answer with citations"),
        Path("/tmp/work"))
    assert question.kind == "investigation"

    plain = _bench_task(
        _spec(Path("."), title="Add a dark-mode toggle to the settings page",
              request="implement it"),
        Path("/tmp/work"))
    assert plain.kind == "feature"
    assert plain.repo_path == "/tmp/work"
    assert plain.acceptance_criteria == []


@pytest.mark.asyncio
async def test_done_report_terminal_is_a_gate_state(tmp_path):
    """Review D9: report-deliverable pipelines (investigation / design_doc /
    clean code_review) terminate DONE with the report as the deliverable —
    the scorer must judge that report, not auto-fail the status."""
    from no_human.core.task import TaskStatus
    repo = _src_repo(tmp_path)
    runner = NorthStarRunner({}, backend_factory=lambda s: None)
    score = await runner._score(
        _spec(repo), _FakeOutcome(TaskStatus.DONE), tmp_path, "sha",
        attempts=[{"tokens_used": 100, "cache_read_tokens": 0, "turns_used": 2}],
        elapsed=2.0)
    assert "did not reach the human gate" not in (score.notes or "")
    assert score.goal_satisfied is not False  # judge decides (None here: no judge)


def test_sandbox_copy_is_instant_isolated_and_survives_a_dirty_source(tmp_path):
    """v8's two crashes were `git clone --no-hardlinks` COPYING a 3.8GB
    object store onto a starved disk. The fix is an APFS clonefile copy —
    instant at any size — and review of the hardlink alternative PROVED
    shared object inodes can be written through into the operator's live
    repo, so the property pinned here is ISOLATION: no object inode is
    shared. The source may also be DIRTY (v7's ns-4092c756 crashed checking
    out over one); the sandbox must come up clean at the pin regardless."""
    import subprocess
    from types import SimpleNamespace
    from no_human.eval.northstar import _setup_sandbox

    src = tmp_path / "src"
    src.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(src)], check=True)
    (src / "a.txt").write_text("x" * 4096)
    for cmd in (["add", "-A"], ["-c", "user.email=t@t", "-c", "user.name=t",
                                "commit", "-qm", "init"], ["gc", "-q"]):
        subprocess.run(["git", "-C", str(src), *cmd], check=True)
    # Dirty the source: a tracked-file edit AND an untracked file.
    (src / "a.txt").write_text("DIRTY")
    (src / "untracked.tmp").write_text("stray")

    spec = SimpleNamespace(repo={"path": str(src), "pin": "HEAD"})
    # Production pre-creates the workdir; without it cp -Rc fails on
    # the missing parent and the test silently pins the FALLBACK git
    # clone instead of the clonefile path (review F3).
    (tmp_path / "wd").mkdir(exist_ok=True)
    work = _setup_sandbox(spec, tmp_path / "wd")

    # Isolation: no shared object inodes with the source store.
    src_packs = {p.name: p.stat().st_ino
                 for p in (src / ".git" / "objects" / "pack").glob("*.pack")}
    assert src_packs, "fixture must have a pack"
    for p in (work / ".git" / "objects" / "pack").glob("*.pack"):
        assert p.stat().st_ino != src_packs.get(p.name), (
            f"{p.name} shares an inode with the source — the write-through "
            "corruption vector is back")
    # Clean at the pin despite the dirty source.
    assert (work / "a.txt").read_text(encoding="utf-8") == "x" * 4096
    assert not (work / "untracked.tmp").exists()
    # And the source's dirt was untouched.
    assert (src / "a.txt").read_text(encoding="utf-8") == "DIRTY"


def test_sandbox_dirty_seed_restores_a_real_dirty_tree(tmp_path):
    """V3 corpus audit (2026-08-04): the reset+clean+push in _setup_sandbox
    PRE-SATISFIES any "make sure everything is committed and pushed" request —
    ns-90b6ff3c was a guaranteed pass because the harness destroyed the task's
    own precondition. A spec with `dirty_seed` must come up with a genuinely
    dirty tree (a MODIFIED tracked entry via append + an UNTRACKED junk file),
    written AFTER the initial push, so nothing seeded is on the remote."""
    repo = _src_repo(tmp_path)
    workdir = tmp_path / "wd"
    workdir.mkdir()
    spec = _spec(repo, dirty_seed={
        "app.py": "# WIP uncommitted edit\n",
        "debug.log": "junk line\n",
    })
    work = _setup_sandbox(spec, workdir)

    porcelain = subprocess.run(
        ["git", "status", "--porcelain"], cwd=work,
        capture_output=True, text=True).stdout
    assert porcelain.strip(), "dirty_seed produced a clean tree — seed not applied"
    codes = {line[:2].strip(): line[3:].strip()
             for line in porcelain.splitlines()}
    assert codes.get("M") == "app.py", (
        f"expected app.py MODIFIED (tracked file appended), got: {porcelain!r}")
    assert codes.get("??") == "debug.log", (
        f"expected debug.log UNTRACKED, got: {porcelain!r}")
    # Append, not overwrite: the tracked file keeps its real content.
    assert (work / "app.py").read_text(encoding="utf-8").startswith("def f():"), (
        "the seed OVERWROTE the tracked file instead of appending")
    # Seeded after the push: the remote carries the CLEAN tree only.
    bare = workdir / "remote.git"
    remote_app = subprocess.run(
        ["git", "--git-dir", str(bare), "show", "HEAD:app.py"],
        capture_output=True, text=True).stdout
    assert "WIP uncommitted edit" not in remote_app, (
        "the seed was committed/pushed — it must stay working-tree-only")


def test_sandbox_without_dirty_seed_stays_pristine(tmp_path):
    """The control for the seed test: a spec with no dirty_seed must keep the
    exact clean-at-the-pin sandbox every other spec has always had — empty
    porcelain, nothing appended anywhere."""
    repo = _src_repo(tmp_path)
    workdir = tmp_path / "wd"
    workdir.mkdir()
    work = _setup_sandbox(_spec(repo), workdir)
    porcelain = subprocess.run(
        ["git", "status", "--porcelain"], cwd=work,
        capture_output=True, text=True).stdout
    assert porcelain.strip() == "", (
        f"a seedless spec's sandbox is dirty: {porcelain!r}")


def test_sandbox_dirty_seed_cannot_escape_the_sandbox(tmp_path):
    """A dirty_seed path is curator data, but '../' must never write onto the
    real machine — the whole sandbox contract is that spec content stays in.

    The sibling payload is review D2's PROVEN BYPASS of the first guard: the
    sandbox checkout is `<workdir>/work`, so `../work-evil/pwned.txt` resolves
    to `<workdir>/work-evil/…`, whose STRING starts with `…/work` — a
    startswith() prefix check waves it through while it lands outside the
    repo. The guard must compare path components, not characters."""
    repo = _src_repo(tmp_path)
    for payload in ("../escape.txt", "../work-evil/pwned.txt"):
        workdir = tmp_path / f"wd-{abs(hash(payload))}"
        workdir.mkdir()
        spec = _spec(repo, dirty_seed={payload: "out\n"})
        with pytest.raises(RuntimeError, match="escapes the sandbox"):
            _setup_sandbox(spec, workdir)
        assert not (workdir / "escape.txt").exists()
        assert not (workdir / "work-evil").exists(), (
            "the sibling-directory payload was WRITTEN before the guard fired")


def test_sandbox_copy_strips_source_hooks(tmp_path):
    """Clone parity: a file copy carries ACTIVE hooks (a real work repo did
    ship a pre-push) that would execute foreign code on sandbox pushes —
    git clone never copies hooks."""
    import subprocess
    from types import SimpleNamespace
    from no_human.eval.northstar import _setup_sandbox

    src = tmp_path / "src"
    src.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(src)], check=True)
    (src / "a.txt").write_text("x")
    subprocess.run(["git", "-C", str(src), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(src), "-c", "user.email=t@t",
                    "-c", "user.name=t", "commit", "-qm", "init"], check=True)
    hook = src / ".git" / "hooks" / "pre-push"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)

    # Production pre-creates the workdir; without it cp -Rc fails on
    # the missing parent and the test silently pins the FALLBACK git
    # clone instead of the clonefile path (review F3).
    (tmp_path / "wd").mkdir(exist_ok=True)
    work = _setup_sandbox(SimpleNamespace(repo={"path": str(src), "pin": "HEAD"}),
                          tmp_path / "wd")
    assert not (work / ".git" / "hooks" / "pre-push").exists(), (
        "the source's active pre-push rode into the sandbox — it would "
        "execute on every coder push")


def test_sandbox_strips_every_ride_along_remote(tmp_path):
    """Review F4: the copy carries ALL of the source's remotes while the
    push-proof guard resolves only origin — an upstream remote would be a
    guard-invisible escape. After setup, origin→local bare is the ONLY
    remote."""
    import subprocess
    from types import SimpleNamespace
    from no_human.eval.northstar import _setup_sandbox

    src = tmp_path / "src"
    src.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(src)], check=True)
    (src / "a.txt").write_text("x")
    subprocess.run(["git", "-C", str(src), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(src), "-c", "user.email=t@t",
                    "-c", "user.name=t", "commit", "-qm", "init"], check=True)
    subprocess.run(["git", "-C", str(src), "remote", "add", "origin",
                    "https://example.com/REAL.git"], check=True)
    subprocess.run(["git", "-C", str(src), "remote", "add", "upstream",
                    "https://example.com/REAL-UPSTREAM.git"], check=True)

    (tmp_path / "wd").mkdir(exist_ok=True)
    work = _setup_sandbox(SimpleNamespace(repo={"path": str(src), "pin": "HEAD"}),
                          tmp_path / "wd")
    remotes = subprocess.run(["git", "remote", "-v"], cwd=work,
                             capture_output=True, text=True).stdout
    assert "upstream" not in remotes and "example.com" not in remotes, remotes
    assert str(tmp_path / "wd" / "remote.git") in remotes


@pytest.mark.slow
@pytest.mark.asyncio
async def test_bench_grill_runs_in_the_sandbox_not_the_source(tmp_path, monkeypatch):
    """No-leak property for the §6 intake grill: when the bench replays a
    spec, the grill's repo evidence comes from the PINNED SANDBOX copy —
    never the operator's source repo (whose HEAD may already contain the
    historical solution)."""
    from no_human.config import load_config
    from no_human.intake import evaluator as ev

    seen = {}

    async def _fake_grill(title, description, criteria, repo_path, *,
                          backend=None, model=None, questions=None,
                          usage_sink=None, outcome_sink=None,
                          questions_outcome_sink=None, probe=True):
        seen["repo_path"] = str(repo_path)
        return None
    monkeypatch.setattr(ev, "grill_spec", _fake_grill)

    repo = _src_repo(tmp_path)
    spec = _spec(repo)
    cfg = load_config(tmp_path / "config.yaml")
    runner = NorthStarRunner(cfg.data, backend_factory=lambda s: _FixBackend(),
                             reviewer=_PassReviewer(),
                             goal_judge=_StubGoalJudge())

    score = await runner.run_one(spec, workdir=tmp_path / "wd")

    assert score.outcome_status == "awaiting_approval"
    assert seen, "the bench pipeline never ran the intake grill"
    work = str((tmp_path / "wd" / "work").resolve())
    assert seen["repo_path"] in (work, str(tmp_path / "wd" / "work"))
    assert seen["repo_path"] != str(repo)


@pytest.mark.asyncio
async def test_judge_evidence_notes_keep_2000_chars(tmp_path):
    """#119 pin (review r1 finding): a silent revert to the old 400-char cap
    would re-amputate judge rationales (ns-7ef821b2 was cut mid-'BUT …') and
    only be noticed at the next drill. Feed 3000 chars, expect exactly 2000."""
    from no_human.core.task import TaskStatus
    from no_human.eval.judge import GoalVerdict

    class _LongEvidenceJudge:
        async def judge(self, **kwargs):
            return GoalVerdict(satisfied=False, evidence="E" * 3000)

    repo = _src_repo(tmp_path)
    spec = _spec(repo)
    runner = NorthStarRunner({}, backend_factory=lambda s: None,
                             goal_judge=_LongEvidenceJudge())

    score = await runner._score(
        spec, _FakeOutcome(TaskStatus.AWAITING_APPROVAL), repo, "HEAD", [], 1.0)
    assert len(score.notes) == 2000


# --------------------------- multi-repo sandbox ----------------------------- #

def _second_repo(tmp_path: Path) -> Path:
    """A SECOND real source repo, to stand in for a linked one."""
    repo = tmp_path / "srcrepo2"
    repo.mkdir()
    for args in (["init", "-b", "main"], ["config", "user.email", "t@t"],
                 ["config", "user.name", "t"]):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    (repo / "client.py").write_text("def call():\n    return 'v1'\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True,
                   capture_output=True)
    return repo


def test_a_linked_repo_is_sandboxed_and_the_task_never_sees_the_real_path(tmp_path):
    """The safety property of the multi-repo tier, stated as a test.

    `Task.linked_repos` makes the orchestrator open a branch and push a PR in
    EVERY listed repo. The push-proof guard protects whatever it is pointed at,
    so handing the Task the spec's real linked path — the operator's other
    checkout — would give an agent write access to a repo nothing is watching.
    """
    from no_human.eval.northstar import (
        _bench_task, _setup_linked_sandboxes, _setup_sandbox,
    )

    primary, secondary = _src_repo(tmp_path), _second_repo(tmp_path)
    pin2 = subprocess.run(["git", "rev-parse", "HEAD"], cwd=secondary,
                          capture_output=True, text=True).stdout.strip()
    spec = _spec(primary, linked_repos=[
        {"path": str(secondary), "pin": pin2, "branch": "main"}])

    workdir = tmp_path / "wd"
    work = _setup_sandbox(spec, workdir)
    linked = _setup_linked_sandboxes(spec, workdir)

    assert len(linked) == 1
    sandboxed = Path(linked[0])
    # NOT the real repo, and inside the workdir.
    assert sandboxed != secondary
    assert sandboxed.is_relative_to(workdir)
    assert (sandboxed / "client.py").exists()      # it really is that repo
    # Same push-proof guarantee as the primary: origin cannot escape.
    origin = subprocess.run(["git", "remote", "get-url", "origin"],
                            cwd=sandboxed, capture_output=True,
                            text=True).stdout.strip()
    assert Path(origin).resolve().is_relative_to(workdir.resolve())

    task = _bench_task(spec, work, linked)
    assert task.linked_repos == [str(sandboxed)]
    assert str(secondary) not in task.linked_repos


def test_a_single_repo_spec_still_gets_no_linked_repos(tmp_path):
    """The corpus is single-repo today. The new path must be inert for it."""
    from no_human.eval.northstar import _setup_linked_sandboxes, _bench_task

    spec = _spec(_src_repo(tmp_path))
    assert _setup_linked_sandboxes(spec, tmp_path / "wd") == []
    assert _bench_task(spec, tmp_path / "work").linked_repos == []


def test_an_unresolvable_linked_repo_is_refused_not_silently_skipped(tmp_path):
    """A linked repo that does not exist must stop the spec. Skipping it would
    run a multi-repo task as a single-repo one and score the result as if the
    whole thing had been attempted."""
    from no_human.eval.northstar import _setup_linked_sandboxes

    spec = _spec(_src_repo(tmp_path),
                 linked_repos=[{"path": str(tmp_path / "nope"), "pin": "HEAD"}])
    with pytest.raises(RuntimeError, match="does not resolve"):
        _setup_linked_sandboxes(spec, tmp_path / "wd")


# --------------------------- tool calls in the digest ----------------------- #

def test_a_tool_call_reaches_the_digest_instead_of_an_empty_string():
    """A tool_use event has no `text` — it carries tool_name/tool_input. The
    digest only ever read `text`, so every tool call in every score record was
    stored as "", and the calls behind a failed spec were unreadable afterwards.
    """
    from no_human.eval.northstar import _digest_events

    rows = [
        {"kind": "tool_use", "tool_name": "Bash",
         "tool_input": {"command": "pytest -q tests/"}, "ts": 1},
        {"kind": "tool_use", "tool_name": "Edit",
         "tool_input": {"file_path": "parcelo/rates.py"}, "ts": 2},
        {"kind": "text", "text": "thinking out loud", "ts": 3},
    ]
    digest = _digest_events(rows)
    assert digest[0]["text"] == "Bash: pytest -q tests/"
    assert digest[1]["text"] == "Edit: parcelo/rates.py"
    assert digest[2]["text"] == "thinking out loud"   # unchanged


def test_a_tool_input_is_bounded_and_single_line():
    """A Write's input is a whole file and this rides into a TRACKED results
    JSON, so the rendering is capped far below the text cap."""
    from no_human.eval.northstar import _DIGEST_MAX_TOOL_INPUT, _digest_events

    rows = [{"kind": "tool_use", "tool_name": "Write",
             "tool_input": {"file_path": "x.py", "content": "y" * 10_000}}]
    text = _digest_events(rows)[0]["text"]
    assert len(text) <= len("Write: ") + _DIGEST_MAX_TOOL_INPUT
    assert "\n" not in text


def test_a_tool_call_naming_the_local_repo_path_is_redacted(tmp_path):
    """The digest is written into eval/results/northstar/*.json, which is
    tracked. A tool input routinely names paths, and after the repo-map
    translation that path is the operator's real checkout."""
    from no_human.eval.northstar import _digest_events

    spec = _spec(tmp_path)
    spec.repo["path"] = "/Users/someone/git/private-thing"
    spec.spec_repo_path = "/repo"
    rows = [{"kind": "tool_use", "tool_name": "Bash",
             "tool_input": {"command": "ls /Users/someone/git/private-thing/src"}}]
    text = _digest_events(rows, spec)[0]["text"]
    assert "/Users/someone" not in text
    assert "/repo/src" in text


def test_an_unknown_tool_shape_does_not_break_the_digest():
    from no_human.eval.northstar import _digest_events

    rows = [{"kind": "tool_use", "tool_name": "Weird", "tool_input": None},
            {"kind": "tool_use", "tool_name": "Other", "tool_input": {"z": 1, "a": 2}},
            {"kind": "tool_use"}]
    out = [e["text"] for e in _digest_events(rows)]
    assert out == ["Weird", "Other: a,z", ""]


def test_a_long_tool_input_keeps_BOTH_ends():
    """Head-only truncation made every path in a doom-loop render as the same
    120 characters of shared prefix, so five different files read as one
    repeated call — the loop was indistinguishable from a stutter."""
    from no_human.eval.northstar import _DIGEST_MAX_TOOL_INPUT, _digest_events

    prefix = "/private/tmp/claude-501/-Users-someone-very-long-path/deep/nested/dir"
    rows = [{"kind": "tool_use", "tool_name": "Read",
             "tool_input": {"file_path": f"{prefix}/{leaf}"}}
            for leaf in ("alpha_module.py", "beta_module.py")]
    a, b = (e["text"] for e in _digest_events(rows))
    assert a != b, "two different files must not render identically"
    assert a.endswith("alpha_module.py") and b.endswith("beta_module.py")
    assert len(a) <= len("Read: ") + _DIGEST_MAX_TOOL_INPUT


@pytest.mark.asyncio
async def test_a_sandbox_that_did_not_test_itself_LEADS_the_score_note(tmp_path):
    """The pre-flight's finding conditions every other number on the row, so a
    reader has to meet it first — not after 2000 characters of judge evidence.
    A warning nobody sees is the same as no warning."""
    from no_human.core.task import TaskStatus

    spec = _spec(_src_repo(tmp_path), expect_escalation=True)
    runner = NorthStarRunner({}, backend_factory=lambda s: None)

    warned = await runner._score(
        spec, _FakeOutcome(TaskStatus.ESCALATED), tmp_path, "sha",
        attempts=[], elapsed=1.0,
        wrong_tree=["thing imports from /elsewhere/thing/__init__.py"])
    assert warned.notes.startswith("⚠ sandbox did not test itself:")
    assert "/elsewhere/thing" in warned.notes
    assert "honestly escalated as expected" in warned.notes

    clean = await runner._score(
        spec, _FakeOutcome(TaskStatus.ESCALATED), tmp_path, "sha",
        attempts=[], elapsed=1.0)
    assert not clean.notes.startswith("⚠")
    assert clean.notes == "honestly escalated as expected"


@pytest.mark.asyncio
async def test_the_warning_also_leads_a_did_not_reach_the_gate_note(tmp_path):
    """The failure mode it was found in: an escalated spec whose feedback loop
    was pointed at another tree. That row must not read as a plain capability
    failure."""
    from no_human.core.task import TaskStatus

    spec = _spec(_src_repo(tmp_path))
    runner = NorthStarRunner({}, backend_factory=lambda s: None)
    score = await runner._score(
        spec, _FakeOutcome(TaskStatus.ESCALATED), tmp_path, "sha",
        attempts=[], elapsed=1.0, wrong_tree=["pkg imports from /other/pkg"])
    assert score.notes.startswith("⚠ sandbox did not test itself:")
    assert "did not reach the human gate" in score.notes


# ------------------------- tier-weighted cost_ratio ------------------------- #

def test_priced_roles_covers_every_metered_role_or_fails_closed():
    """Every `PRICED_ROLES` key must resolve to a real config tier, and every
    role `db.USAGE_ROLES` can emit must be either priced or fail closed to the
    table's HIGHEST ratio — never dropped, never priced below a known role."""
    from no_human.config import DEFAULT_CONFIG
    from no_human.core.db import USAGE_ROLES
    from no_human.core.pricing import MODEL_PRICES_USD_PER_MTOK

    assert ANCHOR_MODEL in MODEL_PRICES_USD_PER_MTOK
    for role, key in PRICED_ROLES.items():
        assert key in DEFAULT_CONFIG["llm"], f"{role} -> {key} is not a real tier"
        assert DEFAULT_CONFIG["llm"][key]

    for role in USAGE_ROLES.values():
        if is_priced_role(role):
            continue
        # Unpriced (e.g. "distill"): fails closed at the table's max ratio,
        # so it is never cheaper than a role that IS priced.
        assert tier_weight(role) >= tier_weight("reviewer")
        assert tier_weight(role) >= tier_weight("coder")


def test_tier_weighted_reads_the_canonical_price_table(monkeypatch):
    """`tier_weighted` must move when `core.pricing`'s ONE table moves — proof
    that northstar carries no second price list of its own."""
    import no_human.core.pricing as pricing
    from no_human.config import DEFAULT_CONFIG

    utility_model = DEFAULT_CONFIG["llm"]["utility_model"]
    before_rate = pricing.MODEL_PRICES_USD_PER_MTOK[utility_model]

    before = tier_weighted(1000, "utility")
    monkeypatch.setitem(pricing.MODEL_PRICES_USD_PER_MTOK, utility_model,
                        (before_rate[0] * 2, before_rate[1]))
    after = tier_weighted(1000, "utility")

    assert after == pytest.approx(before * 2)


def test_is_priced_role_is_the_only_predicate():
    assert is_priced_role("implementer") is is_priced_role("coder") is True
    assert is_priced_role("distill") is False
    assert is_priced_role("Reviewer") is True

    # Behavioural check: a role the predicate REJECTS still contributes
    # non-zero weighted spend — it must never be silently dropped.
    s = BenchScore(
        task_id="x", title="t", outcome_status="done", goal_satisfied=True,
        escalated_honestly=False, mergeable=True,
        nh_tokens=10, nh_cache_tokens=0, nh_cache_creation_tokens=0,
        nh_turns=1, nh_wall_clock_s=1.0,
        orig_tokens=100, orig_cache_tokens=0, orig_cache_creation_tokens=0,
        orig_wall_clock_s=1.0, orig_corrections=0,
        nh_role_tokens={"distill": {"tokens_used": 10, "cache_read_tokens": 0,
                                    "cache_creation_tokens": 0}})
    assert not is_priced_role("distill")
    assert s.nh_priced_tokens > 0.0


def test_cost_ratio_is_tier_weighted_across_mixed_tiers():
    """RED-FIRST: an Opus-tier role (reviewer) and a Haiku-tier role (utility)
    with tokens chosen so the plain class-weighted sum disagrees with the
    tier-weighted total. Must fail before `nh_priced_tokens`/`cost_ratio`
    consult `nh_role_tokens`."""
    reviewer_class_total = 100 + 0.1 * 1000 + 1.25 * 40   # 250.0
    utility_class_total = 500.0
    expected = (tier_weighted(reviewer_class_total, "reviewer")
               + tier_weighted(utility_class_total, "utility"))
    plain_sum = reviewer_class_total + utility_class_total
    assert expected != pytest.approx(plain_sum)   # the mismatch this proves

    s = BenchScore(
        task_id="x", title="t", outcome_status="done", goal_satisfied=True,
        escalated_honestly=False, mergeable=True,
        nh_tokens=600, nh_cache_tokens=1000, nh_cache_creation_tokens=40,
        nh_turns=1, nh_wall_clock_s=1.0,
        orig_tokens=1000, orig_cache_tokens=0, orig_cache_creation_tokens=0,
        orig_wall_clock_s=1.0, orig_corrections=0,
        nh_role_tokens={
            "reviewer": {"tokens_used": 100, "cache_read_tokens": 1000,
                        "cache_creation_tokens": 40},
            "utility": {"tokens_used": 500, "cache_read_tokens": 0,
                       "cache_creation_tokens": 0}})

    assert s.nh_priced_tokens == pytest.approx(expected)
    assert s.nh_priced_tokens != pytest.approx(plain_sum)
    assert s.cost_ratio == pytest.approx(expected / 1000)


def test_cost_ratio_without_a_breakdown_is_byte_identical():
    """No `nh_role_tokens` recorded (every pre-tier-weighting caller, every
    legacy `latest.json`) must give the EXACT pre-change number — a bare
    `BenchScore(...)` call is the compat path, not an opt-in."""
    s = BenchScore(task_id="x", title="t", outcome_status="done",
                   goal_satisfied=True, escalated_honestly=False,
                   mergeable=True, nh_tokens=750, nh_cache_tokens=100,
                   nh_cache_creation_tokens=0,
                   nh_turns=5, nh_wall_clock_s=60.0, orig_tokens=1500,
                   orig_cache_tokens=9000, orig_cache_creation_tokens=0,
                   orig_wall_clock_s=600.0, orig_corrections=3)
    assert s.nh_role_tokens == {}
    assert s.nh_priced_tokens == pytest.approx(750 + 0.1 * 100)
    assert s.cost_ratio == pytest.approx((750 + 0.1 * 100) / (1500 + 0.1 * 9000))


@pytest.mark.asyncio
async def test_score_breakdown_sums_to_the_scalar_totals(tmp_path):
    """Extends `test_score_counts_reviewer_tokens`: with coder + reviewer +
    utility columns populated, the per-role breakdown's class sums must equal
    the scalar `nh_tokens`/`nh_cache_tokens`/`nh_cache_creation_tokens` —
    they are DERIVED from the same breakdown, so they cannot disagree."""
    from no_human.core.task import TaskStatus
    repo = _src_repo(tmp_path)
    runner = NorthStarRunner({}, backend_factory=lambda s: None)
    score = await runner._score(
        _spec(repo), _FakeOutcome(TaskStatus.FAILED), tmp_path, "sha",
        attempts=[{"tokens_used": 200, "cache_read_tokens": 1000,
                   "cache_creation_tokens": 30, "turns_used": 2,
                   "review_tokens_used": 50, "review_cache_read_tokens": 400,
                   "review_cache_creation_tokens": 7,
                   "utility_tokens_used": 9, "utility_cache_read_tokens": 2,
                   "utility_cache_creation_tokens": 1}],
        elapsed=5.0)

    assert set(score.nh_role_tokens) == {"coder", "reviewer", "utility"}
    fresh_sum = sum(v["tokens_used"] for v in score.nh_role_tokens.values())
    read_sum = sum(v["cache_read_tokens"] for v in score.nh_role_tokens.values())
    creation_sum = sum(
        v["cache_creation_tokens"] for v in score.nh_role_tokens.values())
    assert fresh_sum == score.nh_tokens
    assert read_sum == score.nh_cache_tokens
    assert creation_sum == score.nh_cache_creation_tokens


def test_role_breakdown_survives_card_reload(tmp_path):
    from no_human.eval.northstar_card import NorthStarCard

    orig = BenchScore(
        task_id="ns-1", title="t", outcome_status="done", goal_satisfied=True,
        escalated_honestly=False, mergeable=True,
        nh_tokens=600, nh_cache_tokens=1000, nh_cache_creation_tokens=40,
        nh_turns=1, nh_wall_clock_s=1.0,
        orig_tokens=1000, orig_cache_tokens=0, orig_cache_creation_tokens=0,
        orig_wall_clock_s=1.0, orig_corrections=0,
        nh_role_tokens={
            "reviewer": {"tokens_used": 100, "cache_read_tokens": 1000,
                        "cache_creation_tokens": 40},
            "utility": {"tokens_used": 500, "cache_read_tokens": 0,
                       "cache_creation_tokens": 0}},
        nh_role_models={"reviewer": "claude-opus-4-8",
                       "utility": "claude-haiku-4-5"})
    card = NorthStarCard(scores=[orig], created_at="2026-08-12", label="x")
    p = tmp_path / "latest.json"
    card.save(p)
    loaded = NorthStarCard.load(p)

    assert loaded is not None
    reloaded = loaded.scores[0]
    assert reloaded.nh_role_tokens == orig.nh_role_tokens
    assert reloaded.nh_role_models == orig.nh_role_models
    assert reloaded.cost_ratio == pytest.approx(orig.cost_ratio)

    # A legacy dict with NEITHER key must still load and take the compat path.
    legacy = orig.as_dict()
    del legacy["nh_role_tokens"]
    del legacy["nh_role_models"]
    legacy_path = tmp_path / "legacy.json"
    legacy_path.write_text(json.dumps({
        "created_at": "x", "label": "legacy", "aggregate": {},
        "scores": [legacy],
    }))
    legacy_card = NorthStarCard.load(legacy_path)
    assert legacy_card is not None
    legacy_score = legacy_card.scores[0]
    assert legacy_score.nh_role_tokens == {}
    assert legacy_score.cost_ratio == pytest.approx(
        (600 + 0.1 * 1000 + 1.25 * 40) / 1000)


# ------------------- bench cost ratio part 2: basis label -------------------- #

def _score(**kw) -> BenchScore:
    base = dict(
        task_id="x", title="t", outcome_status="done", goal_satisfied=True,
        escalated_honestly=False, mergeable=True,
        nh_tokens=600, nh_cache_tokens=1000, nh_cache_creation_tokens=40,
        nh_turns=1, nh_wall_clock_s=1.0,
        orig_tokens=1000, orig_cache_tokens=0, orig_cache_creation_tokens=0,
        orig_wall_clock_s=1.0, orig_corrections=0,
    )
    base.update(kw)
    return BenchScore(**base)


def test_cost_ratio_basis_is_tier_weighted_iff_a_breakdown_was_recorded():
    """The label must branch on the EXACT same emptiness check
    `nh_priced_tokens` branches on — never a second, independently
    maintained test that could disagree with the number it labels."""
    with_breakdown = _score(nh_role_tokens={
        "reviewer": {"tokens_used": 100, "cache_read_tokens": 1000,
                    "cache_creation_tokens": 40},
    })
    assert with_breakdown.cost_ratio_basis == BASIS_TIER_WEIGHTED
    assert with_breakdown.cost_ratio_basis == "tier-weighted"

    without_breakdown = _score()
    assert without_breakdown.nh_role_tokens == {}
    assert without_breakdown.cost_ratio_basis == BASIS_CACHE_WEIGHTED
    assert without_breakdown.cost_ratio_basis == "cache-weighted"


def test_cost_ratio_basis_travels_with_the_ratio_in_as_dict():
    """The wire shape carries the basis unconditionally — even a score with
    no baseline (`cost_ratio is None`) still says what basis it WOULD use,
    so a reader is never left to assume."""
    scored = _score(nh_role_tokens={
        "utility": {"tokens_used": 500, "cache_read_tokens": 0,
                   "cache_creation_tokens": 0}})
    d = scored.as_dict()
    assert d["cost_ratio_basis"] == "tier-weighted"
    assert d["cost_ratio"] is not None

    no_baseline = _score(orig_tokens=0, orig_cache_tokens=0)
    d2 = no_baseline.as_dict()
    assert d2["cost_ratio"] is None
    assert d2["cost_ratio_basis"] == "cache-weighted"


def test_bench_score_carries_unscoreable_into_the_results_json():
    """The judge-broke/agent-failed distinction only matters if it survives
    into the persisted per-spec results JSON — this is the wire boundary."""
    scored = _score(unscoreable=True)
    assert scored.as_dict()["unscoreable"] is True

    default = _score()
    assert default.unscoreable is False
    assert default.as_dict()["unscoreable"] is False
