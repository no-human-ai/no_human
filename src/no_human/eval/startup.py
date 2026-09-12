"""The startup-company scenario: one codebase, one sprint of related tickets.

WHY THIS EXISTS, stated so it is not mistaken for a bigger golden set. Both
existing corpora measure ONE task at a time:

* ``eval/golden_tasks/`` — a two-file toy repo per task, built fresh, thrown
  away. Nothing a task does can affect another task, because no two tasks share
  a line of code.
* ``eval/northstar_tasks/`` — real historical requests, each against a
  different real repository, with no acceptance criteria and no held-out tests
  (both are leak vectors there, and ``tests/test_northstar_corpus.py`` enforces
  that they stay empty).

Neither can answer the question a small engineering team actually has: *if I
hand this thing a sprint, do I get back a set of PRs I can merge?* The failure
that question exposes is the one isolated tasks cannot produce — ticket 3
quietly undoing ticket 1 while its own acceptance criteria are met, and a
per-task scorecard reporting 5/5.

So a scenario is a fictional company's codebase plus an ORDERED list of
tickets. Ticket *n* is replayed against the codebase as it stands once tickets
``1..n-1`` are merged — expressed entirely in the existing ``BenchTask`` schema,
via each spec's ``repo.pin``. There is no second runner: ``materialize`` builds
the git history and emits ordinary bench specs, and ``nh bench run --specs-dir``
runs them exactly as it runs any other spec.

Pinning to the KNOWN-GOOD history rather than to the agent's own previous output
is deliberate. Chaining onto the agent's result would make ticket 1's failure
cascade into four more, and the corpus would then measure one thing five times.
Each ticket is scored against the codebase a human team would actually have.

The cross-ticket property is carried by the held-out tests: a ticket's holdout
asserts its OWN acceptance criteria *and* re-asserts the behaviour earlier
tickets shipped. Which earlier tickets is declared in ``regression_of``, and
``break_probe`` is what keeps those assertions from rotting into decoration —
a one-line mutation that disables exactly that ticket's behaviour, so a test can
prove the later holdouts still notice. A regression assertion nothing can make
fail is not an assertion.

One ticket in a sprint is a founder request that contradicts behaviour the same
sprint already shipped (``expect_escalation`` + ``conflicts_with``). That is the
coordination failure a small team hits constantly, and the only correct outcome
is an honest escalation — never a PR, and never a weakened test.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import yaml

from .bench_task import BenchTask

SCENARIO_DIR = Path(__file__).resolve().parents[3] / "eval" / "startup_scenario"
DEFAULT_SCENARIO = SCENARIO_DIR / "parcelo.yaml"

#: The ``subset`` every emitted spec carries. Deliberately NOT "core": the
#: north-star gate's coverage arithmetic is defined over the curated core
#: corpus, and a startup sprint is a different measurement with a different
#: denominator. See ``docs/eval.md``.
SUBSET = "startup"


# --------------------------------------------------------------------------- #
# Schema                                                                       #
# --------------------------------------------------------------------------- #

@dataclass
class Ticket:
    id: str
    title: str
    request: str
    kind: str = "feature"
    acceptance_criteria: list[str] = field(default_factory=list)
    #: path -> FULL new content, applied on top of the previous ticket's state.
    solution: dict[str, str] = field(default_factory=dict)
    holdout: str = ""
    #: ids of earlier tickets whose behaviour this ticket's holdout re-asserts.
    regression_of: list[str] = field(default_factory=list)
    #: {path, find, replace, breaks} — a mutation that disables THIS ticket's
    #: behaviour, so the guards can prove later holdouts still catch it.
    break_probe: dict[str, str] = field(default_factory=dict)
    expect_escalation: bool = False
    #: {path, test, why} — the shipped test this ticket contradicts.
    conflicts_with: dict[str, str] = field(default_factory=dict)

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "Ticket":
        return Ticket(
            id=str(data.get("id", "")),
            title=str(data.get("title", "")),
            request=str(data.get("request", "")),
            kind=str(data.get("kind", "feature")),
            acceptance_criteria=list(data.get("acceptance_criteria") or []),
            solution=dict(data.get("solution") or {}),
            holdout=str(data.get("holdout") or ""),
            regression_of=list(data.get("regression_of") or []),
            break_probe=dict(data.get("break_probe") or {}),
            expect_escalation=bool(data.get("expect_escalation", False)),
            conflicts_with=dict(data.get("conflicts_with") or {}),
        )


@dataclass
class Scenario:
    id: str
    name: str
    summary: str = ""
    base: dict[str, str] = field(default_factory=dict)
    tickets: list[Ticket] = field(default_factory=list)
    path: Path | None = None

    @property
    def ticket_ids(self) -> list[str]:
        return [t.id for t in self.tickets]


def load_scenario(path: Path | str = DEFAULT_SCENARIO) -> Scenario:
    """Parse a scenario file. Structure only — see :func:`validate_scenario`."""
    p = Path(path)
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{p}: expected a mapping at the top level")
    return Scenario(
        id=str(data.get("id", "")),
        name=str(data.get("name", "")),
        summary=str(data.get("summary", "")),
        base=dict(data.get("base") or {}),
        tickets=[Ticket.from_dict(t) for t in (data.get("tickets") or [])],
        path=p,
    )


# --------------------------------------------------------------------------- #
# Validation                                                                   #
# --------------------------------------------------------------------------- #

def validate_scenario(scenario: Scenario) -> list[str]:
    """Structural problems with *scenario*; empty list = well-formed.

    Everything here is decidable by reading the file. Whether the scenario
    actually MEASURES anything — a holdout that already passes before the
    ticket is done measures nothing at all — needs the repo built and pytest
    run, which is what ``tests/test_startup_scenario.py`` does.
    """
    problems: list[str] = []
    if not scenario.id.strip():
        problems.append("scenario has no id")
    if not scenario.name.strip():
        problems.append("scenario has no name")
    if not scenario.base:
        problems.append("scenario has no base codebase")
    if len(scenario.tickets) < 2:
        problems.append(
            f"a sprint needs at least 2 tickets, got {len(scenario.tickets)} — "
            "one ticket is the per-task corpus, not a scenario")

    for rel in scenario.base:
        problems += _path_problems(rel, "base")
    if not any(rel.startswith("tests/") for rel in scenario.base):
        problems.append("base codebase ships no tests — the agent would inherit "
                        "a repo with no suite to keep green")

    seen: set[str] = set()
    for position, ticket in enumerate(scenario.tickets, start=1):
        where = ticket.id or f"ticket #{position}"
        if not ticket.id.strip():
            problems.append(f"{where}: no id")
        elif ticket.id in seen:
            problems.append(f"{where}: duplicate ticket id")
        seen.add(ticket.id)
        if not ticket.title.strip():
            problems.append(f"{where}: no title")
        if not ticket.request.strip():
            problems.append(f"{where}: no request — a ticket is what the human asked")
        if not ticket.acceptance_criteria:
            problems.append(f"{where}: no acceptance criteria")

        if ticket.expect_escalation:
            # The must-escalate ticket. A solution would contradict the whole
            # point, and an unstated conflict makes the expectation unfalsifiable.
            if ticket.solution:
                problems.append(
                    f"{where}: expect_escalation with a solution — the two cannot "
                    "both be true")
            if ticket.holdout.strip():
                problems.append(
                    f"{where}: expect_escalation with a holdout — the correct "
                    "outcome is an honest stop, so there is no result to test")
            for key in ("path", "test", "why"):
                if not str(ticket.conflicts_with.get(key, "")).strip():
                    problems.append(
                        f"{where}: conflicts_with.{key} is required — an "
                        "expected escalation must name the shipped test it "
                        "contradicts, or nothing can check the conflict is real")
        else:
            if not ticket.solution:
                problems.append(
                    f"{where}: no solution — the sprint history cannot be built, "
                    "and later tickets would be pinned to a codebase missing this one")
            if not ticket.holdout.strip():
                problems.append(
                    f"{where}: no holdout — without held-out tests the ticket is "
                    "scored on nothing but the judge")
            for rel in ticket.solution:
                problems += _path_problems(rel, where)

        for earlier in ticket.regression_of:
            if earlier not in seen or earlier == ticket.id:
                problems.append(
                    f"{where}: regression_of names {earlier!r}, which is not an "
                    "EARLIER ticket in this sprint")

        if ticket.break_probe:
            problems += _probe_problems(where, ticket.break_probe)

    # Every ticket another ticket claims to regress on must be breakable, or
    # the regression assertions can rot into decoration with nothing noticing.
    regressed = {r for t in scenario.tickets for r in t.regression_of}
    by_id = {t.id: t for t in scenario.tickets}
    for rid in sorted(regressed):
        if rid in by_id and not by_id[rid].break_probe:
            problems.append(
                f"{rid}: named in regression_of but has no break_probe — nothing "
                "can prove the later holdouts would notice it breaking")

    if not any(t.expect_escalation for t in scenario.tickets):
        problems.append(
            "no ticket expects an escalation — a sprint with no contradictory "
            "request cannot measure whether the loop stops honestly")
    if not regressed:
        problems.append(
            "no ticket declares regression_of — without cross-ticket assertions "
            "this is the per-task corpus with extra steps")
    return problems


def _path_problems(rel: str, where: str) -> list[str]:
    """A file path in a scenario becomes a write into a materialised repo."""
    p = Path(rel)
    if p.is_absolute() or ".." in p.parts:
        return [f"{where}: unsafe path {rel!r} — must be relative and inside the repo"]
    return []


def _probe_problems(where: str, probe: dict[str, str]) -> list[str]:
    out: list[str] = []
    for key in ("path", "find", "breaks"):
        if not str(probe.get(key, "")).strip():
            out.append(f"{where}: break_probe.{key} is required")
    if "replace" not in probe:
        out.append(f"{where}: break_probe.replace is required (may be empty)")
    if probe.get("find") is not None and probe.get("find") == probe.get("replace"):
        out.append(f"{where}: break_probe replaces text with itself — a no-op "
                   "mutation always 'survives' and proves nothing")
    out += _path_problems(str(probe.get("path", "")), where)
    return out


# --------------------------------------------------------------------------- #
# Materialisation                                                              #
# --------------------------------------------------------------------------- #

@dataclass
class MaterialisedSprint:
    scenario: Scenario
    root: Path
    repo: Path
    specs_dir: Path
    specs: list[BenchTask]
    #: ticket id -> the commit its spec is pinned to (all EARLIER tickets merged)
    pins: dict[str, str]
    #: ticket id -> the full file tree at that pin
    pin_states: dict[str, dict[str, str]]
    #: the tree once every ticket's known-good solution is merged
    final_state: dict[str, str]

    @property
    def by_id(self) -> dict[str, BenchTask]:
        return {s.id: s for s in self.specs}


def _git(cwd: Path, *args: str) -> str:
    out = subprocess.run(["git", *args], cwd=cwd, check=True,
                         capture_output=True, text=True)
    return out.stdout.strip()


def write_tree(repo: Path, files: dict[str, str]) -> None:
    """Write *files* (path -> content) into *repo*, creating parents."""
    for rel, content in files.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def materialise(scenario: Scenario, dest: Path,
                *, strict: bool = True) -> MaterialisedSprint:
    """Build the sprint's git history and the bench specs that replay it.

    One commit for the sprint base, then one per ticket that has a known-good
    solution. Ticket *n*'s spec pins the commit where tickets ``1..n-1`` are
    merged and ticket *n* is not — which is exactly the tree a human engineer
    would have picked the ticket up from.

    ``strict`` refuses to build a scenario that ``validate_scenario`` rejects.
    Turning it off is for tests that need a deliberately broken scenario on
    disk; nothing in the product does.
    """
    if strict:
        problems = validate_scenario(scenario)
        if problems:
            raise ValueError(
                f"{scenario.path or scenario.id}: scenario is malformed\n  - "
                + "\n  - ".join(problems))

    dest = Path(dest)
    repo = dest / "repo"
    specs_dir = dest / "specs"
    repo.mkdir(parents=True, exist_ok=True)
    specs_dir.mkdir(parents=True, exist_ok=True)

    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "founders@parcelo.invalid")
    _git(repo, "config", "user.name", "parcelo")

    state: dict[str, str] = dict(scenario.base)
    write_tree(repo, state)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", f"{scenario.name}: sprint base")

    specs: list[BenchTask] = []
    pins: dict[str, str] = {}
    pin_states: dict[str, dict[str, str]] = {}

    for position, ticket in enumerate(scenario.tickets, start=1):
        pin = _git(repo, "rev-parse", "HEAD")
        pins[ticket.id] = pin
        pin_states[ticket.id] = dict(state)
        specs.append(BenchTask(
            id=ticket.id,
            title=ticket.title,
            request=ticket.request,
            source={
                "kind": "synthetic-scenario",
                "scenario": scenario.id,
                "position": position,
                "label": "startup-company",
            },
            repo={"path": str(repo), "pin": pin, "branch": "main"},
            # No original session: a fictional sprint has no babysat human run
            # to be cheaper than. Left empty so every cost ratio reads "n/a"
            # rather than inventing a denominator (`_bench_cost_cell` already
            # refuses a 0.0 ratio as a non-result).
            original={},
            acceptance_criteria=list(ticket.acceptance_criteria),
            holdout=ticket.holdout,
            subset=SUBSET,
            runnable=True,
            expect_escalation=ticket.expect_escalation,
        ))
        if ticket.solution:
            state.update(ticket.solution)
            write_tree(repo, ticket.solution)
            _git(repo, "add", "-A")
            _git(repo, "commit", "-m", f"{ticket.title} (merged)")

    write_specs(specs, specs_dir)
    return MaterialisedSprint(
        scenario=scenario, root=dest, repo=repo, specs_dir=specs_dir,
        specs=specs, pins=pins, pin_states=pin_states, final_state=state,
    )


# American spelling alias, because the rest of the tree uses both.
materialize = materialise


def write_specs(specs: Iterable[BenchTask], out_dir: Path) -> list[Path]:
    """Write one ``<id>.yaml`` per spec into *out_dir*."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for spec in specs:
        p = out_dir / f"{spec.id}.yaml"
        p.write_text(yaml.safe_dump(spec.to_dict(), sort_keys=False,
                                    allow_unicode=True, width=100), encoding="utf-8")
        written.append(p)
    return written


def apply_break_probe(repo: Path, probe: dict[str, str]) -> None:
    """Apply a ``break_probe`` to a checked-out tree, in place.

    Raises when the mutation did NOT take — a probe whose ``find`` no longer
    matches, or matches more than once, silently degrades into "the tree was
    not modified", and every holdout then passes for the wrong reason. A
    mutation probe that cannot prove the mutant was actually introduced is
    worse than no probe: it reports a hole that is not there, or misses one
    that is.
    """
    target = Path(repo) / probe["path"]
    if not target.exists():
        raise ValueError(f"break_probe path does not exist: {probe['path']}")
    text = target.read_text(encoding="utf-8")
    hits = text.count(probe["find"])
    if hits != 1:
        raise ValueError(
            f"break_probe for {probe['path']} matched {hits} times, expected "
            "exactly 1 — the mutant would not be introduced (0) or would be "
            "ambiguous (>1)")
    mutated = text.replace(probe["find"], probe["replace"])
    if mutated == text:
        raise ValueError(f"break_probe for {probe['path']} changed nothing")
    target.write_text(mutated, encoding="utf-8")


# --------------------------------------------------------------------------- #
# The sprint verdict — what "success" means for a scenario                     #
# --------------------------------------------------------------------------- #

@dataclass
class SprintVerdict:
    total: int
    scored: int
    delivered: int
    deliverable: int
    escalations_expected: int
    escalations_honest: int
    failed: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.reasons

    def as_dict(self) -> dict[str, Any]:
        return {
            "total": self.total, "scored": self.scored,
            "delivered": self.delivered, "deliverable": self.deliverable,
            "escalations_expected": self.escalations_expected,
            "escalations_honest": self.escalations_honest,
            "failed": list(self.failed), "reasons": list(self.reasons),
            "passed": self.passed,
        }


def _field(score: Any, name: str, default: Any = None) -> Any:
    if isinstance(score, dict):
        return score.get(name, default)
    return getattr(score, name, default)


def sprint_verdict(scores: Iterable[Any], scenario: Scenario) -> SprintVerdict:
    """Grade a whole sprint from the per-spec bench scores.

    A sprint is not a percentage. A small team that gets four good PRs and one
    that silently regressed a teammate's ticket has had a BAD week, not an 80%
    week — the bad PR costs more than the four good ones saved. So the verdict
    is all-or-nothing, and it names what failed.

    Deliberately strict about coverage: a run that skipped or crashed a ticket
    is not a 4/4 sprint, it is an unmeasured one. Scoring only what ran is the
    failure mode where a corpus looks BETTER the more of it is broken.
    """
    by_id = {str(_field(s, "task_id")): s for s in scores}
    reasons: list[str] = []
    failed: list[str] = []

    expected = scenario.ticket_ids
    missing = [tid for tid in expected if tid not in by_id]
    if missing:
        reasons.append(
            f"{len(missing)} ticket(s) were never scored: {', '.join(missing)}")

    deliverable = sum(1 for t in scenario.tickets if not t.expect_escalation)
    escalations_expected = len(expected) - deliverable
    delivered = 0
    escalations_honest = 0

    for ticket in scenario.tickets:
        score = by_id.get(ticket.id)
        if score is None:
            failed.append(ticket.id)
            continue
        status = str(_field(score, "outcome_status", ""))
        if status in {"skipped", "crashed"}:
            failed.append(ticket.id)
            reasons.append(f"{ticket.id}: not measured ({status})")
            continue
        if ticket.expect_escalation:
            if bool(_field(score, "escalated_honestly", False)) and \
                    bool(_field(score, "goal_satisfied", False)):
                escalations_honest += 1
            else:
                failed.append(ticket.id)
                reasons.append(
                    f"{ticket.id}: a request that contradicts shipped behaviour "
                    f"was not escalated (ended {status or 'unknown'})")
            continue
        mergeable = _field(score, "mergeable")
        if bool(_field(score, "goal_satisfied", False)) and mergeable is not False:
            delivered += 1
        else:
            failed.append(ticket.id)
            reasons.append(
                f"{ticket.id}: no reviewable PR that keeps the held-out tests "
                f"green (ended {status or 'unknown'}, mergeable={mergeable})")

    return SprintVerdict(
        total=len(expected), scored=len([t for t in expected if t in by_id]),
        delivered=delivered, deliverable=deliverable,
        escalations_expected=escalations_expected,
        escalations_honest=escalations_honest,
        failed=failed, reasons=reasons,
    )


def render_sprint_verdict(verdict: SprintVerdict, scenario: Scenario) -> str:
    """Human-readable sprint result. No numeric self-score, by rule."""
    head = "SPRINT PASSED" if verdict.passed else "SPRINT FAILED"
    lines = [
        "=" * 60,
        f"  {scenario.name} — startup sprint verdict: {head}",
        "=" * 60,
        f"  tickets:            {verdict.total} ({verdict.scored} scored)",
        f"  delivered:          {verdict.delivered}/{verdict.deliverable}"
        "  (reviewable PR, held-out tests green)",
        f"  honest escalations: {verdict.escalations_honest}/"
        f"{verdict.escalations_expected}",
    ]
    if verdict.reasons:
        lines.append("-" * 60)
        lines += [f"  x {r}" for r in verdict.reasons]
    lines.append("=" * 60)
    return "\n".join(lines)
