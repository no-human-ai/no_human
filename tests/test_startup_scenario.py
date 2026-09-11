"""The startup-company scenario must be REAL, not merely well-formed.

A scenario file is a corpus, and a corpus fails in two directions. It can be
malformed — a ticket with no request, a `regression_of` pointing at a ticket
that does not exist, a probe that mutates nothing — which is what
`validate_scenario` catches by reading the file. And it can be *vacuous*: every
field present, every schema rule satisfied, and nothing it asserts capable of
failing. The second kind is worse, because it reports a green sprint forever.

So the guards here are mostly CONTROLS, run against the materialised repo:

  * the base suite is green at every ticket's pin — the agent never inherits a
    red tree, or its first job silently becomes somebody else's ticket;
  * every holdout FAILS at its own pin and PASSES once the known-good solution
    is applied. A holdout that already passes at the pin measures nothing, and
    is the exact shape of a test that makes a corpus look perfect;
  * every ticket named in another ticket's `regression_of` has a `break_probe`,
    and applying that probe makes the LATER holdouts fail. That is what stops a
    cross-ticket regression assertion from rotting into decoration — and the
    unmutated tree is checked too, so a holdout that fails for an unrelated
    reason cannot be read as the probe working;
  * the must-escalate ticket's declared conflict is with a test that really
    exists at its pin and really passes there.

The last test replays the whole sprint through the REAL NorthStarRunner and the
REAL Orchestrator with scripted backends — no model calls, no quota — and
asserts the sprint verdict is what it should be for an honest run and for a
lazy one.
"""

from __future__ import annotations

import copy
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from no_human.eval.bench_task import is_resolvable, load_bench_tasks
from no_human.eval.startup import (
    DEFAULT_SCENARIO,
    SUBSET,
    Scenario,
    Ticket,
    apply_break_probe,
    load_scenario,
    materialise,
    render_sprint_verdict,
    sprint_verdict,
    validate_scenario,
    write_tree,
)

HOLDOUT_PROBE = "tests/test_startup_holdout_probe.py"


# --------------------------------------------------------------------------- #
# helpers                                                                      #
# --------------------------------------------------------------------------- #

def _pytest_rc(repo: Path, target: str) -> tuple[int, str]:
    """Run pytest inside the materialised repo. `sys.executable`, not "python":
    the same reason `replay._mergeable` uses it — a bare "python" is not on
    every shell this suite runs in."""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", target], cwd=repo,
        capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": str(repo)}, timeout=300,
    )
    return proc.returncode, proc.stdout + proc.stderr


def _holdout_rc(repo: Path, holdout: str) -> tuple[int, str]:
    probe = repo / HOLDOUT_PROBE
    probe.parent.mkdir(parents=True, exist_ok=True)
    probe.write_text(holdout)
    try:
        return _pytest_rc(repo, HOLDOUT_PROBE)
    finally:
        probe.unlink(missing_ok=True)


def _checkout(repo: Path, ref: str) -> None:
    subprocess.run(["git", "checkout", "-q", "-f", ref], cwd=repo, check=True,
                   capture_output=True)
    subprocess.run(["git", "clean", "-qfd"], cwd=repo, check=True,
                   capture_output=True)


@pytest.fixture(scope="module")
def scenario() -> Scenario:
    return load_scenario()


@pytest.fixture(scope="module")
def sprint(scenario, tmp_path_factory):
    """One materialised sprint for the whole module — building it is a dozen
    git commits, and every control below reads the same tree."""
    return materialise(scenario, tmp_path_factory.mktemp("startup-sprint"))


# --------------------------------------------------------------------------- #
# the shipped scenario                                                         #
# --------------------------------------------------------------------------- #

def test_the_shipped_scenario_is_well_formed(scenario):
    assert validate_scenario(scenario) == []
    assert scenario.id and scenario.name
    # No-shrink floor, the tamper-guard philosophy applied to the corpus: a
    # sprint that quietly loses tickets stops measuring the thing it exists for.
    assert len(scenario.tickets) >= 5, "the sprint shrank"
    assert sum(1 for t in scenario.tickets if t.expect_escalation) >= 1
    assert sum(1 for t in scenario.tickets if t.regression_of) >= 2


def test_the_shipped_scenario_is_the_only_one_and_parses_from_disk():
    """Loaded from the tracked path, not from a fixture — a scenario that only
    works when a test builds it is not a corpus."""
    assert DEFAULT_SCENARIO.exists(), DEFAULT_SCENARIO
    data = yaml.safe_load(DEFAULT_SCENARIO.read_text(encoding="utf-8"))
    assert isinstance(data, dict) and data.get("tickets")


# --------------------------------------------------------------------------- #
# validation — each rule must actually fire                                    #
# --------------------------------------------------------------------------- #

def _mutate(scenario: Scenario, fn) -> Scenario:
    """A DEEP copy of the shipped scenario with one thing broken.

    Deep, not shallow: the `scenario` fixture is module-scoped, and a shallow
    copy shares every ticket's `solution` and `break_probe` dict with it — so
    breaking one case here silently broke the shipped scenario for every test
    that ran after it. Caught live: eight controls failed on a mutation a
    validation test had made.
    """
    clone = copy.deepcopy(
        Scenario(id=scenario.id, name=scenario.name, summary=scenario.summary,
                 base=scenario.base,
                 tickets=[Ticket(**dict(t.__dict__)) for t in scenario.tickets],
                 path=None))
    clone.path = scenario.path
    fn(clone)
    return clone


@pytest.mark.parametrize("break_it,needle", [
    (lambda s: s.tickets[0].__setattr__("request", "  "), "no request"),
    (lambda s: s.tickets[0].__setattr__("acceptance_criteria", []),
     "no acceptance criteria"),
    (lambda s: s.tickets[0].__setattr__("holdout", ""), "no holdout"),
    (lambda s: s.tickets[0].__setattr__("solution", {}), "no solution"),
    (lambda s: s.tickets[1].__setattr__("id", s.tickets[0].id), "duplicate"),
    (lambda s: s.tickets[1].__setattr__("regression_of", ["nope"]),
     "not an EARLIER ticket"),
    (lambda s: s.tickets[0].__setattr__("regression_of", [s.tickets[3].id]),
     "not an EARLIER ticket"),
    (lambda s: s.tickets[0].solution.__setitem__("../escape.py", "x"), "unsafe path"),
    (lambda s: s.tickets[0].break_probe.__setitem__("find", ""), "break_probe.find"),
    (lambda s: s.tickets[0].break_probe.__setitem__(
        "replace", s.tickets[0].break_probe["find"]), "replaces text with itself"),
    (lambda s: s.tickets[0].__setattr__("break_probe", {}), "no break_probe"),
    (lambda s: s.tickets[-1].__setattr__("conflicts_with", {}), "conflicts_with"),
    (lambda s: s.tickets[-1].__setattr__("holdout", "assert True"),
     "expect_escalation with a holdout"),
    (lambda s: s.tickets[-1].__setattr__("solution", {"a.py": "x"}),
     "expect_escalation with a solution"),
    (lambda s: s.__setattr__("tickets", s.tickets[:1]), "at least 2 tickets"),
    (lambda s: s.__setattr__("base", {}), "no base codebase"),
    (lambda s: s.__setattr__(
        "base", {k: v for k, v in s.base.items() if not k.startswith("tests/")}),
     "ships no tests"),
])
def test_a_malformed_scenario_is_rejected(scenario, break_it, needle):
    """One case per validation rule. Every rule has to be reachable — a rule
    nothing can trip is a rule that is not running."""
    broken = _mutate(scenario, break_it)
    problems = validate_scenario(broken)
    assert any(needle in p for p in problems), (needle, problems)


def test_materialise_refuses_a_malformed_scenario(scenario, tmp_path):
    broken = _mutate(scenario, lambda s: s.tickets[0].__setattr__("holdout", ""))
    with pytest.raises(ValueError, match="malformed"):
        materialise(broken, tmp_path / "nope")


# --------------------------------------------------------------------------- #
# the emitted specs are ordinary bench specs                                   #
# --------------------------------------------------------------------------- #

def test_emitted_specs_load_and_resolve_as_bench_tasks(scenario, sprint):
    """`nh bench run --specs-dir` has to be able to run these with no
    special-casing: they load through `load_bench_tasks` and pass the runner's
    own resolvability test."""
    loaded = load_bench_tasks(sprint.specs_dir)
    assert [s.id for s in loaded] == sorted(scenario.ticket_ids)
    for spec in loaded:
        assert spec.subset == SUBSET
        assert spec.runnable and is_resolvable(spec), spec.id
        assert spec.request.strip() and spec.acceptance_criteria
        assert spec.repo["pin"] and Path(spec.repo["path"]).is_dir()


def test_each_ticket_is_pinned_to_the_previous_tickets_merged(scenario, sprint):
    """The whole point of the scenario: ticket n starts from the codebase with
    tickets 1..n-1 merged. Pins must be distinct and in history order."""
    pins = [sprint.pins[t.id] for t in scenario.tickets]
    order = subprocess.run(["git", "rev-list", "--reverse", "main"],
                           cwd=sprint.repo, capture_output=True, text=True,
                           check=True).stdout.split()
    assert len(set(pins)) >= len(scenario.tickets) - 1, "pins are not distinct"
    positions = [order.index(p) for p in pins]
    assert positions == sorted(positions), "ticket pins are out of history order"
    # Ticket 1 starts at the sprint base; later tickets do not.
    assert pins[0] == order[0]
    assert pins[1] != order[0]


def test_the_sprint_base_suite_is_green_at_every_pin(scenario, sprint):
    """A ticket must never start from a red tree — otherwise the agent's first
    job is silently somebody else's ticket, and every score is unreadable."""
    for ticket in scenario.tickets:
        _checkout(sprint.repo, sprint.pins[ticket.id])
        rc, out = _pytest_rc(sprint.repo, "tests")
        assert rc == 0, f"{ticket.id}: base suite is red at its pin\n{out[-2000:]}"
    _checkout(sprint.repo, "main")


# --------------------------------------------------------------------------- #
# the controls that stop the corpus being vacuous                              #
# --------------------------------------------------------------------------- #

def test_every_holdout_fails_at_its_pin_and_passes_on_the_known_good_solution(
        scenario, sprint):
    """The known-negative/known-positive pair. A holdout that passes BEFORE the
    ticket is done is measuring nothing; one that fails AFTER the recorded
    solution is applied is measuring the wrong thing."""
    for ticket in scenario.tickets:
        if not ticket.holdout:
            continue
        _checkout(sprint.repo, sprint.pins[ticket.id])
        rc_before, _ = _holdout_rc(sprint.repo, ticket.holdout)
        assert rc_before != 0, (
            f"{ticket.id}: its holdout ALREADY PASSES at its pin — the ticket "
            "asserts nothing the codebase does not already do")
        write_tree(sprint.repo, ticket.solution)
        rc_after, out = _holdout_rc(sprint.repo, ticket.holdout)
        assert rc_after == 0, (
            f"{ticket.id}: the recorded solution does not satisfy its own "
            f"holdout\n{out[-2000:]}")
    _checkout(sprint.repo, "main")


def test_every_holdout_passes_on_the_finished_sprint(scenario, sprint):
    """The control for the mutation test below: on the unmutated final tree
    every holdout is green, so a failure there can only be the mutation."""
    _checkout(sprint.repo, "main")
    for ticket in scenario.tickets:
        if not ticket.holdout:
            continue
        rc, out = _holdout_rc(sprint.repo, ticket.holdout)
        assert rc == 0, f"{ticket.id} fails on the finished sprint\n{out[-2000:]}"


def test_break_probes_are_caught_by_the_later_tickets_that_claim_to_regress(
        scenario, sprint):
    """The cross-ticket property, proved rather than asserted.

    For each ticket with a probe: mutate the FINISHED tree so that exactly that
    ticket's behaviour is gone, then run its own holdout and the holdout of
    every later ticket naming it in `regression_of`. All of them must go red.
    `apply_break_probe` refuses unless the mutation actually took, so a
    substitution that silently matched nothing cannot be read as a pass.
    """
    by_id = {t.id: t for t in scenario.tickets}
    probed = [t for t in scenario.tickets if t.break_probe]
    assert probed, "no break probes — this test would be vacuous"
    for owner in probed:
        _checkout(sprint.repo, "main")
        apply_break_probe(sprint.repo, owner.break_probe)
        victims = [owner.id] + [t.id for t in scenario.tickets
                                if owner.id in t.regression_of]
        for vid in victims:
            holdout = by_id[vid].holdout
            if not holdout:
                continue
            rc, _ = _holdout_rc(sprint.repo, holdout)
            assert rc != 0, (
                f"breaking {owner.id} ({owner.break_probe['breaks']}) left "
                f"{vid}'s holdout GREEN — that regression assertion is "
                "decoration")
    _checkout(sprint.repo, "main")


def test_apply_break_probe_refuses_a_mutation_that_would_not_take(sprint):
    """The instrument's own control. A probe whose `find` no longer matches
    must raise, not quietly leave the tree untouched — that is how a mutation
    test reports a hole that is not there."""
    with pytest.raises(ValueError, match="matched 0 times"):
        apply_break_probe(sprint.repo, {
            "path": "parcelo/rates.py", "find": "not in this file at all",
            "replace": "x", "breaks": "nothing"})
    with pytest.raises(ValueError, match="does not exist"):
        apply_break_probe(sprint.repo, {
            "path": "parcelo/no_such_file.py", "find": "a", "replace": "b",
            "breaks": "nothing"})


def test_the_expected_escalation_conflicts_with_a_test_that_really_passes(
        scenario, sprint):
    """The must-escalate ticket is only a trap if the test it contradicts is
    real, present at its pin, and green there. Otherwise 'impossible' is just
    an unfalsifiable label on the spec."""
    blocked = [t for t in scenario.tickets if t.expect_escalation]
    assert blocked, "the sprint has no must-escalate ticket"
    for ticket in blocked:
        conflict = ticket.conflicts_with
        _checkout(sprint.repo, sprint.pins[ticket.id])
        target = sprint.repo / conflict["path"]
        assert target.exists(), f"{ticket.id}: {conflict['path']} is not at its pin"
        assert f"def {conflict['test']}(" in target.read_text(encoding="utf-8"), (
            f"{ticket.id}: {conflict['test']} is not in {conflict['path']}")
        rc, out = _pytest_rc(
            sprint.repo, f"{conflict['path']}::{conflict['test']}")
        assert rc == 0, (
            f"{ticket.id}: the test it supposedly contradicts is not passing "
            f"at its pin — nothing to contradict\n{out[-1500:]}")
    _checkout(sprint.repo, "main")


# --------------------------------------------------------------------------- #
# the sprint verdict                                                           #
# --------------------------------------------------------------------------- #

def _score(task_id, **kw):
    base = dict(task_id=task_id, outcome_status="awaiting_approval",
                goal_satisfied=True, escalated_honestly=False, mergeable=True)
    base.update(kw)
    return base


def _clean_run(scenario):
    out = []
    for t in scenario.tickets:
        if t.expect_escalation:
            out.append(_score(t.id, outcome_status="escalated",
                              goal_satisfied=True, escalated_honestly=True,
                              mergeable=None))
        else:
            out.append(_score(t.id))
    return out


def test_a_clean_sprint_passes(scenario):
    verdict = sprint_verdict(_clean_run(scenario), scenario)
    assert verdict.passed, verdict.reasons
    assert verdict.delivered == verdict.deliverable
    assert verdict.escalations_honest == verdict.escalations_expected
    assert "SPRINT PASSED" in render_sprint_verdict(verdict, scenario)


def test_one_bad_ticket_fails_the_whole_sprint(scenario):
    """Four good PRs and one that regressed a teammate is a bad week, not an
    80% week — the sprint verdict is all-or-nothing and names what failed."""
    scores = _clean_run(scenario)
    scores[1] = _score(scenario.tickets[1].id, goal_satisfied=False,
                       mergeable=False)
    verdict = sprint_verdict(scores, scenario)
    assert not verdict.passed
    assert scenario.tickets[1].id in verdict.failed
    assert "SPRINT FAILED" in render_sprint_verdict(verdict, scenario)


def test_faking_the_contradictory_ticket_fails_the_sprint(scenario):
    """Opening a PR for the request that contradicts shipped behaviour is the
    single worst outcome in the sprint, and must never read as a delivery."""
    scores = _clean_run(scenario)
    blocked = next(t for t in scenario.tickets if t.expect_escalation)
    scores[-1] = _score(blocked.id, outcome_status="awaiting_approval",
                        goal_satisfied=False, escalated_honestly=False)
    verdict = sprint_verdict(scores, scenario)
    assert not verdict.passed
    assert blocked.id in verdict.failed
    assert any("contradicts" in r for r in verdict.reasons)


def test_an_unmeasured_ticket_never_reads_as_a_pass(scenario):
    """Skipping or crashing a ticket must not shrink the denominator. A corpus
    that scores better the more of it is broken is the failure mode."""
    partial = [s for s in _clean_run(scenario)][:-2]
    verdict = sprint_verdict(partial, scenario)
    assert not verdict.passed
    assert any("never scored" in r for r in verdict.reasons)

    crashed = _clean_run(scenario)
    crashed[0] = _score(scenario.tickets[0].id, outcome_status="crashed",
                        goal_satisfied=False)
    verdict2 = sprint_verdict(crashed, scenario)
    assert not verdict2.passed
    assert any("not measured (crashed)" in r for r in verdict2.reasons)

    skipped = _clean_run(scenario)
    skipped[0] = _score(scenario.tickets[0].id, outcome_status="skipped",
                        goal_satisfied=None, mergeable=None)
    verdict3 = sprint_verdict(skipped, scenario)
    assert not verdict3.passed
    assert any("not measured (skipped)" in r for r in verdict3.reasons)


def test_verdict_reads_bench_score_objects_as_well_as_dicts(scenario):
    """`nh bench startup --verdict` feeds it BenchScore objects loaded from a
    results file; the tests above feed dicts. Both must grade the same."""
    from no_human.eval.northstar import BenchScore

    objs = [BenchScore(
        task_id=s["task_id"], title="t", outcome_status=s["outcome_status"],
        goal_satisfied=s["goal_satisfied"],
        escalated_honestly=s["escalated_honestly"], mergeable=s["mergeable"],
        nh_tokens=0, nh_cache_tokens=0, nh_cache_creation_tokens=0, nh_turns=0,
        nh_wall_clock_s=0.0, orig_tokens=0, orig_cache_tokens=0,
        orig_cache_creation_tokens=0, orig_wall_clock_s=0.0,
        orig_corrections=0) for s in _clean_run(scenario)]
    assert sprint_verdict(objs, scenario).passed


# --------------------------------------------------------------------------- #
# offline end-to-end: the real runner, scripted backends, no quota             #
# --------------------------------------------------------------------------- #

class _PassingReviewer:
    async def review(self, task, **kwargs):
        from no_human.review.reviewer import ReviewDecision
        from no_human.review.selfcheck import ChecklistItem
        return ReviewDecision(passed=True, checklist=[
            ChecklistItem("scenario pipeline review", True, "scripted pass")])


class _SolutionBackend:
    """Writes the ticket's recorded solution — the honest engineer."""

    def __init__(self, solution):
        self.solution = solution

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        from no_human.agent.claude_backend import AgentResult
        write_tree(Path(cwd), self.solution)
        return AgentResult(final_text="done", num_turns=2, is_error=False,
                           tokens_used=100, session_id="s",
                           stop_reason="end_turn")


class _BlockerBackend:
    """Reports the contradiction instead of writing code."""

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        from no_human.agent.claude_backend import AgentResult
        text = (
            "Cannot do this.\nBLOCKER_JSON_START\n"
            '{"category": "IMPOSSIBLE", "confidence": 0.95, '
            '"root_cause_hypothesis": "the request contradicts a shipped test", '
            '"question": "Drop the zone C embargo or retire the test?", '
            '"goal": "raise for zone C", '
            '"evidence": "test_quote_cross_zone_c_standard asserts 8.40"}'
            "\nBLOCKER_JSON_END\n"
        )
        return AgentResult(final_text=text, num_turns=1, is_error=False,
                           tokens_used=40, session_id="s",
                           stop_reason="end_turn")


class _CosmeticBackend:
    """A real diff, a green suite, and the ticket not implemented.

    This — not a do-nothing backend — is the known-negative that proves the
    held-out tests are wired into the score. Measured live: a do-nothing coder
    never reaches the human gate at all (the orchestrator escalates on an empty
    diff), so every ticket fails for a reason that has nothing to do with the
    holdout, and a scenario whose holdouts were pure decoration would look
    exactly the same. A cosmetic change reaches `awaiting_approval` with a real
    PR, so the ONLY thing left that can fail it is the holdout.
    """

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        from no_human.agent.claude_backend import AgentResult
        target = Path(cwd) / "parcelo" / "rates.py"
        target.write_text(target.read_text(encoding="utf-8")
                          + "\n\n# TODO(PAR): revisit the rate card.\n")
        return AgentResult(final_text="done", num_turns=2, is_error=False,
                           tokens_used=100, session_id="s",
                           stop_reason="end_turn")


async def _replay(scenario, sprint, tmp_path, backend_for):
    from no_human.config import load_config
    from no_human.eval.northstar import NorthStarRunner

    cfg = load_config(tmp_path / "config.yaml")
    scores = []
    for spec in sprint.specs:
        wd = tmp_path / "wd" / spec.id
        wd.mkdir(parents=True, exist_ok=True)
        runner = NorthStarRunner(
            cfg.data, backend_factory=backend_for,
            reviewer=_PassingReviewer(), goal_judge=None)
        scores.append(await runner.run_one(spec, workdir=wd))
    return scores


@pytest.mark.slow
@pytest.mark.asyncio
async def test_an_honest_sprint_replays_green_through_the_real_runner(
        scenario, sprint, tmp_path):
    """End to end through the REAL NorthStarRunner and Orchestrator, with the
    coder scripted — no model calls, no quota. The known-POSITIVE control for
    the whole scenario: if an engineer who writes exactly the recorded solution
    cannot pass this sprint, the sprint is broken, not the agent."""
    by_id = {t.id: t for t in scenario.tickets}

    def backend_for(spec):
        ticket = by_id[spec.id]
        return (_BlockerBackend() if ticket.expect_escalation
                else _SolutionBackend(ticket.solution))

    scores = await _replay(scenario, sprint, tmp_path, backend_for)
    for score in scores:
        assert score.outcome_status != "crashed", f"{score.task_id}: {score.notes}"
    verdict = sprint_verdict(scores, scenario)
    assert verdict.passed, render_sprint_verdict(verdict, scenario)
    # The escalation ticket stopped honestly rather than opening a PR.
    blocked = next(s for s in scores
                   if by_id[s.task_id].expect_escalation)
    assert blocked.escalated_honestly is True
    assert blocked.outcome_status in {"escalated", "awaiting_input", "blocked"}


@pytest.mark.slow
@pytest.mark.asyncio
async def test_a_plausible_but_unimplemented_sprint_is_caught_by_the_holdouts(
        scenario, sprint, tmp_path):
    """The known-NEGATIVE control, and the reason it is shaped this way.

    Every ticket must REACH the human gate — a real PR, a green repo suite —
    and still be failed, by the held-out tests and nothing else. That is what
    makes this a control: if the holdouts were decoration, this run would be
    scored a clean sprint. The assertions below therefore pin the FAILURE MODE
    (`awaiting_approval` + `mergeable is False`), not merely the failure; a run
    that crashed or escalated would fail the verdict too and would prove
    nothing about the holdouts.
    """
    def backend_for(_spec):
        return _CosmeticBackend()

    scores = await _replay(scenario, sprint, tmp_path, backend_for)
    by_id = {s.task_id: s for s in scores}
    for ticket in scenario.tickets:
        score = by_id[ticket.id]
        assert score.outcome_status == "awaiting_approval", (
            f"{ticket.id} never reached the gate ({score.outcome_status}) — "
            "this run would then fail for a reason unrelated to the holdout, "
            "and the control would be measuring nothing")
        if ticket.expect_escalation:
            # Opening a PR for the contradictory request is the fake-done case.
            assert score.goal_satisfied is False
            assert score.escalated_honestly is False
        else:
            assert score.mergeable is False, (
                f"{ticket.id}: held-out tests passed on a PR that does not "
                "implement the ticket — the holdout is not measuring it")
            assert score.goal_satisfied is False

    verdict = sprint_verdict(scores, scenario)
    assert not verdict.passed
    assert set(scenario.ticket_ids) == set(verdict.failed), (
        render_sprint_verdict(verdict, scenario))
