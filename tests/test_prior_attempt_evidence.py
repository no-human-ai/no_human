"""Attempt N>1 receives the prior attempt's distilled review findings and
failing test ids — evidence, never instructions.

Extends the existing "fix_pairs" evidence-carrying pattern (cross-task
evidence "as evidence, never as an instruction") to also carry, WITHIN a
single task, the independent reviewer's blocking findings and the exact
newly-failing test IDs from an aborted attempt into the next attempt's
prompt after a stuck-detection context reset (PLAN.md Part 22). The reset
itself is DESIGNED and stays exactly as-is; only reviewer-verified findings
and failing test IDs ride across it — never coder corrections/narrative.

Covers all four acceptance criteria:
  AC1 — attempt N>1's prompt carries the distilled block (word-capped,
        test IDs survive when findings overflow).
  AC2 — the block is absent when there is no evidence; reset behavior is
        otherwise unchanged; evidence never stacks across attempts.
  AC3 — the block states its provenance and carries no corrections/narrative.
  AC4 — a fixture-level replay of 2cc879d5's scenario: attempt 1 names a
        failing test id, attempt 2's prompt contains that exact id, and the
        test passes once the producer/consumer wiring is exercised end to
        end against synthetic fixtures (never a real DB or production log).
"""

from no_human.core.orchestrator import Orchestrator
from no_human.core.prompt_blocks import (
    PRIOR_EVIDENCE_WORD_CAP,
    build_prior_attempt_evidence,
)
from no_human.core.task import Task, TaskStatus


# --------------------------------------------------------------- fixtures --

class _FakeStore:
    """Synthetic in-memory stand-in for ``Store`` — never a real DB or
    production log, per the plan's intake note. Callers seed ``attempts``
    (list of dicts, shaped like ``Store.list_attempts`` rows) and ``events``
    (list of dicts, shaped like ``Store.list_events`` rows) directly."""

    def __init__(self, attempts=None, events=None):
        self.attempts = attempts or []
        self.events = events or []
        self.updates = []

    async def list_attempts(self, task_id):
        return list(self.attempts)

    async def list_events(self, task_id):
        return list(self.events)

    async def update_task(self, task):
        self.updates.append(task)


def _orch_min(store=None):
    """Same idiom as tests/test_attempt_state_distill.py's ``_orch_min`` —
    ``_record_prior_attempt_evidence`` only needs ``store``, an event sink,
    and ``config`` (unused, present for parity with other seams)."""
    orch = object.__new__(Orchestrator)
    orch.config = {}
    orch._sink = lambda e: None
    orch.store = store
    return orch


def _orch_for_prompt():
    """Same idiom as tests/test_attempt_state_distill.py's
    ``_orch_for_prompt`` — ``_build_implement_prompt`` must not require an
    event sink to construct a prompt."""
    orch = object.__new__(Orchestrator)
    orch.config = {}
    orch.ci_runner = None
    orch._active_profile = None
    orch._active_memories = None
    return orch


def _checklist(*, blocking=True, label="bug", file="widget.py", line=12,
               comment="the widget parses input is still broken",
               severity="high"):
    return {
        "items": [
            {
                "label": label, "passed": False, "file": file, "line": line,
                "comment": comment, "severity": severity if blocking else "nit",
            }
        ]
    }


def _attempt_row(attempt_number, *, checklist=None, test_results=None,
                  status="failed", id=None, review_passed=None):
    return {
        "id": id,
        "attempt_number": attempt_number,
        "status": status,
        "review_checklist": checklist,
        "review_passed": review_passed,
        "test_results": test_results,
    }


def _tests_event(*, ok, failing_tests=None, flaky_excused=None,
                  failing_tests_dropped=None):
    ev = {"kind": "tests", "ok": ok, "failing_tests": failing_tests or []}
    if flaky_excused is not None:
        ev["flaky_excused"] = flaky_excused
    if failing_tests_dropped is not None:
        ev["failing_tests_dropped"] = failing_tests_dropped
    return ev


# ---------------------------------------- AC1: the block itself (unit-level)

def test_attempt_two_prompt_carries_the_distilled_evidence_block():
    t = Task.new("fix the widget", repo_path="/tmp/repo")
    t.acceptance_criteria = ["widget renders"]
    t.context = {
        "prior_attempt_evidence": {
            "from_attempt": 1,
            "source": "Attempt 1 review verdict and test events",
            "findings": [
                {"label": "bug", "file": "widget.py", "line": 12,
                 "comment": "widget crashes on empty input"},
            ],
            "failing_tests": ["tests/test_widget.py::test_empty_input"],
        },
    }
    orch = _orch_for_prompt()
    prompt = orch._build_implement_prompt(t, "/tmp/repo", attempt_n=2)

    assert "PRIOR-ATTEMPT EVIDENCE—NOT INSTRUCTIONS." in prompt
    assert "Source: Attempt 1 review verdict and test events." in prompt
    assert "widget crashes on empty input" in prompt
    assert "tests/test_widget.py::test_empty_input" in prompt


def test_the_block_never_exceeds_the_word_cap():
    evidence = {
        "from_attempt": 1,
        "findings": [
            {"label": f"finding-{i}", "file": "a.py", "line": i,
             "comment": "x " * 60}
            for i in range(30)
        ],
        "failing_tests": [f"tests/test_x.py::test_{i}" for i in range(80)],
    }
    block = build_prior_attempt_evidence(evidence)
    assert len(block.split()) <= PRIOR_EVIDENCE_WORD_CAP, block


def test_test_ids_survive_when_findings_overflow():
    """The fixed sections (header/provenance/test-IDs) are budgeted FIRST;
    the variable section (findings) gets whatever remains — so a pile of
    long findings must never crowd out the exact failing test ids."""
    ids = [f"tests/test_x.py::test_{i}" for i in range(60)]
    evidence = {
        "from_attempt": 1,
        "findings": [
            {"label": f"finding-{i}", "file": "a.py", "line": i,
             "comment": "y" * 300}
            for i in range(20)
        ],
        "failing_tests": ids,
    }
    block = build_prior_attempt_evidence(evidence)
    assert len(block.split()) <= PRIOR_EVIDENCE_WORD_CAP, block
    for tid in ids:
        assert tid in block, f"missing {tid}"


def test_many_failing_ids_with_a_finding_degrades_instead_of_raising():
    """B1 regression: 132 failing test ids (the documented minimum trigger)
    plus >=1 blocking finding used to raise ``RuntimeError`` — the residual
    "... and N more blocking finding(s)" marker is never counted in the
    fixed header/provenance/test-ID word budget, so once every real finding
    is trimmed away that marker alone could still push the block over
    ``PRIOR_EVIDENCE_WORD_CAP``, and the old trim loop's ``and kept_findings``
    exit condition stopped trying right as that state was reached — always
    raising on the live coder-prompt path. This must degrade, never raise:
    a shorter-but-honest block costs nothing; a raised exception kills the
    whole attempt. Fails against the pre-fix code (raises); passes now."""
    ids = [f"tests/test_x.py::test_{i}" for i in range(132)]
    evidence = {
        "from_attempt": 1,
        "source": "Attempt 1 review verdict and test events",
        "findings": [
            {"label": "bug", "file": "widget.py", "line": 12,
             "comment": "the widget parses input is still broken"},
        ],
        "failing_tests": ids,
    }

    # Unit-level: the producer function itself must not raise.
    block = build_prior_attempt_evidence(evidence)
    assert len(block.split()) <= PRIOR_EVIDENCE_WORD_CAP, block

    # End-to-end: driven through the actual coder-prompt seam, which is the
    # path the reviewer flagged as live (an uncaught raise here kills the
    # whole attempt, not just a unit test).
    t = Task.new("fix the widget", repo_path="/tmp/repo")
    t.acceptance_criteria = ["widget renders"]
    t.context = {"prior_attempt_evidence": evidence}
    orch = _orch_for_prompt()
    prompt = orch._build_implement_prompt(t, "/tmp/repo", attempt_n=2)

    assert "PRIOR-ATTEMPT EVIDENCE—NOT INSTRUCTIONS." in prompt
    assert ids[0] in prompt


def test_even_more_failing_ids_and_findings_still_degrades():
    """Push further past the boundary — many findings, not just one — to
    confirm the second-stage (drop the residual "and N more" marker) and,
    if ever needed, the third-stage (hard word-truncate with a visible
    marker) degrade paths are both reachable without raising."""
    ids = [f"tests/test_x.py::test_{i}" for i in range(200)]
    evidence = {
        "from_attempt": 1,
        "source": "Attempt 1 review verdict and test events",
        "findings": [
            {"label": f"finding-{i}", "file": "a.py", "line": i,
             "comment": "the widget is still broken in a slightly new way"}
            for i in range(10)
        ],
        "failing_tests": ids,
    }
    block = build_prior_attempt_evidence(evidence)
    assert len(block.split()) <= PRIOR_EVIDENCE_WORD_CAP, block


# ------------------------------------------- AC2: absence, no-stack, unchanged

def test_no_block_when_attempt_one_had_no_findings_and_no_failing_tests():
    assert build_prior_attempt_evidence(None) == ""
    assert build_prior_attempt_evidence({}) == ""
    assert build_prior_attempt_evidence(
        {"from_attempt": 1, "findings": [], "failing_tests": []}
    ) == ""

    t = Task.new("fix x", repo_path="/tmp/repo")
    t.acceptance_criteria = ["thing works"]
    t.context = {
        "prior_attempt_evidence": {
            "from_attempt": 1, "findings": [], "failing_tests": [],
        },
    }
    orch = _orch_for_prompt()
    prompt = orch._build_implement_prompt(t, "/tmp/repo", attempt_n=2)
    assert "PRIOR-ATTEMPT EVIDENCE" not in prompt


def test_attempt_one_prompt_is_unchanged():
    t = Task.new("fix x", repo_path="/tmp/repo")
    t.acceptance_criteria = ["thing works"]
    # Evidence tagged for a LATER attempt must never leak into attempt 1's
    # prompt even if present in context (defensive; the producer never
    # stamps from_attempt=1, but the consumer must not trust it either).
    t.context = {
        "prior_attempt_evidence": {
            "from_attempt": 1,
            "findings": [{"label": "bug", "file": "a.py", "line": 1,
                          "comment": "should never appear on attempt 1"}],
            "failing_tests": ["tests/test_a.py::test_x"],
        },
    }
    orch = _orch_for_prompt()
    prompt = orch._build_implement_prompt(t, "/tmp/repo", attempt_n=1)
    assert "PRIOR-ATTEMPT EVIDENCE" not in prompt
    assert "should never appear on attempt 1" not in prompt


def test_evidence_never_stacks_across_attempts():
    """Anti-stacking: the lineage guard accepts only from_attempt ==
    attempt_n - 1 — a doc from two attempts ago must never resurface, and
    each producer run overwrites the single key rather than appending."""
    t = Task.new("fix x", repo_path="/tmp/repo")
    t.acceptance_criteria = ["thing works"]
    t.context = {
        "prior_attempt_evidence": {
            "from_attempt": 1,
            "findings": [{"label": "old", "file": "a.py", "line": 1,
                          "comment": "STALE-FINDING-FROM-ATTEMPT-1"}],
            "failing_tests": ["tests/test_a.py::test_stale"],
        },
    }
    orch = _orch_for_prompt()

    # attempt 3 must not trust a doc stamped for attempt 1 (only attempt 2's
    # own producer run may stamp from_attempt=2).
    stale_prompt = orch._build_implement_prompt(t, "/tmp/repo", attempt_n=3)
    assert "STALE-FINDING-FROM-ATTEMPT-1" not in stale_prompt
    assert "PRIOR-ATTEMPT EVIDENCE" not in stale_prompt

    # a fresh producer run for attempt 2 overwrites the single key — the
    # attempt-1 doc cannot combine with it.
    t.context["prior_attempt_evidence"] = {
        "from_attempt": 2,
        "findings": [{"label": "new", "file": "b.py", "line": 2,
                      "comment": "FRESH-FINDING-FROM-ATTEMPT-2"}],
        "failing_tests": ["tests/test_b.py::test_fresh"],
    }
    fresh_prompt = orch._build_implement_prompt(t, "/tmp/repo", attempt_n=3)
    assert "FRESH-FINDING-FROM-ATTEMPT-2" in fresh_prompt
    assert "STALE-FINDING-FROM-ATTEMPT-1" not in fresh_prompt


async def test_stale_evidence_is_rejected_and_cleared():
    """A resumed attempt 1 must not let a prior run's evidence survive in
    task.context — same defect class as distilled_state, cleared by the
    same branch in _distill_attempt_state and actively persisted."""
    orch = _orch_min(_FakeStore())
    t = Task.new("fix x", repo_path="/tmp/repo")
    t.context = {
        "prior_attempt_evidence": {
            "from_attempt": 2,
            "findings": [{"label": "old", "file": "a.py", "line": 1,
                          "comment": "STALE"}],
            "failing_tests": ["tests/test_a.py::test_x"],
        },
    }

    class _FakeRepo:
        pass

    await orch._distill_attempt_state(t, _FakeRepo(), 1, "main")

    assert "prior_attempt_evidence" not in (t.context or {})
    assert orch.store.updates, "the clear must be persisted, not just in-memory"


async def test_no_correction_stacking_across_repeated_attempts():
    """A test that verifies no correction-stacking occurs, per AC2: running
    the producer across two consecutive attempts (1, then 2) must leave
    exactly ONE attempt's evidence in context at a time — never a growing
    accumulation of findings/failing tests from earlier attempts. Events and
    attempt rows accumulate incrementally, matching real call timing: the
    producer runs right after ITS OWN attempt's outcome, before the next
    attempt's rows/events exist in the store."""
    store = _FakeStore(
        attempts=[
            _attempt_row(1, checklist=_checklist(comment="ATTEMPT-1-FINDING"),
                         test_results={"failing_tests": ["tests/test_a.py::test_one"]}),
        ],
        events=[
            {"kind": "attempt_start"},
            _tests_event(ok=False, failing_tests=["tests/test_a.py::test_one"]),
        ],
    )
    orch = _orch_min(store)
    t = Task.new("fix x", repo_path="/tmp/repo")
    t.context = {}

    await orch._record_prior_attempt_evidence(t, 1)
    first = t.context["prior_attempt_evidence"]
    assert first["from_attempt"] == 1
    assert any("ATTEMPT-1-FINDING" in f.get("comment", "") for f in first["findings"])
    assert first["failing_tests"] == ["tests/test_a.py::test_one"]

    # attempt 2 runs, its own row/events are appended (matching real timing).
    store.attempts.append(
        _attempt_row(2, checklist=_checklist(comment="ATTEMPT-2-FINDING"),
                     test_results={"failing_tests": ["tests/test_a.py::test_two"]})
    )
    store.events.append({"kind": "attempt_start"})
    store.events.append(_tests_event(ok=False, failing_tests=["tests/test_a.py::test_two"]))

    await orch._record_prior_attempt_evidence(t, 2)
    second = t.context["prior_attempt_evidence"]
    assert second["from_attempt"] == 2
    # attempt 1's finding/id must NOT still be present — the record is
    # overwritten, not appended.
    assert not any("ATTEMPT-1-FINDING" in f.get("comment", "") for f in second["findings"])
    assert "tests/test_a.py::test_one" not in second["failing_tests"]
    assert second["failing_tests"] == ["tests/test_a.py::test_two"]
    # exactly one evidence record lives in context at a time.
    assert set(t.context.keys()) & {"prior_attempt_evidence"} == {"prior_attempt_evidence"}
    assert isinstance(t.context["prior_attempt_evidence"], dict)


async def test_resumed_task_attempt_number_collision_does_not_leak_a_prior_runs_row():
    """H1 (reviewer finding, blocking): ``attempt_number`` is a task-lifetime
    counter (every creation site computes ``len(list_attempts)+1``) while the
    bounded loop's ``attempt_n`` resets to 1 on every `nh reply` resume — so
    matching purely by ``attempt_number == attempt_n`` can hand back a PRIOR
    run's row. ``Store.latest_open_attempt``'s docstring documents this is not
    hypothetical: real production ties (four rows all numbered 1) and
    out-of-order numbers (a lower number written 15 minutes after a higher
    one). Simulate the collision directly: two rows share
    ``attempt_number == 1`` (an ancient row from an earlier run, and this
    run's fresh row) — the ancient row sorts FIRST in `list_attempts`'
    ``ORDER BY attempt_number`` result. Passing the row's own unique
    ``attempt_id`` (as `_run_attempt` now stamps via
    `self._active_attempt_id`) must select the fresh row regardless of the
    number collision or list order."""
    old_row = _attempt_row(
        1, id="old-row-from-a-prior-run",
        checklist=_checklist(comment="ANCIENT-FINDING-FROM-A-PRIOR-RUN"),
        test_results={"failing_tests": ["tests/test_a.py::test_ancient"]},
    )
    fresh_row = _attempt_row(
        1, id="fresh-row-this-run",
        checklist=_checklist(comment="FRESH-FINDING-THIS-RUN"),
        test_results={"failing_tests": ["tests/test_a.py::test_fresh"]},
    )
    store = _FakeStore(
        # Ancient row listed FIRST — `next(...)` without attempt_id would
        # return it, exactly reproducing the pre-fix bug.
        attempts=[old_row, fresh_row],
        events=[
            {"kind": "attempt_start"},
            _tests_event(ok=False, failing_tests=["tests/test_a.py::test_fresh"]),
        ],
    )
    orch = _orch_min(store)
    t = Task.new("fix x", repo_path="/tmp/repo")
    t.context = {}

    await orch._record_prior_attempt_evidence(t, 1, attempt_id="fresh-row-this-run")

    ev = t.context["prior_attempt_evidence"]
    assert any("FRESH-FINDING-THIS-RUN" in f.get("comment", "") for f in ev["findings"])
    assert not any(
        "ANCIENT-FINDING-FROM-A-PRIOR-RUN" in f.get("comment", "")
        for f in ev["findings"]
    )
    assert ev["failing_tests"] == ["tests/test_a.py::test_fresh"]
    assert "tests/test_a.py::test_ancient" not in ev["failing_tests"]

    # Negative control: the pre-fix number-only heuristic (no attempt_id
    # supplied) really does pick the wrong, first-listed row — proving this
    # fixture reproduces the bug the fix closes, not a scenario that never
    # exercised the collision.
    t2 = Task.new("fix x", repo_path="/tmp/repo")
    t2.context = {}
    await orch._record_prior_attempt_evidence(t2, 1)
    ev2 = t2.context["prior_attempt_evidence"]
    assert any(
        "ANCIENT-FINDING-FROM-A-PRIOR-RUN" in f.get("comment", "")
        for f in ev2["findings"]
    ), "fixture no longer reproduces the attempt_number collision"


async def test_review_pass_does_not_leak_stale_review_feedback_fallback():
    """H2 (reviewer finding, blocking): ``ctx['review_feedback']`` is written
    by `_record_review_feedback` on a review FAIL and is never cleared on a
    later PASS (both call sites only touch it on the FAIL branch). A row
    that HAS a checklist but zero BLOCKING items — a genuine review PASS,
    with tests failing afterward — must not fall through to that stale
    fallback: doing so re-presents an EARLIER attempt's already-addressed
    findings under THIS attempt's false 'review verdict' provenance, which
    is the exact 'review PASSES, tests then FAIL' scenario the reviewer
    named. The fallback is legitimate only when the row carries NO checklist
    at all (review never produced a verdict this round)."""
    pass_row = _attempt_row(
        2, id="attempt-2-row",
        checklist=_checklist(blocking=False, comment="advisory-only, not blocking"),
        review_passed=1,
        test_results={"failing_tests": ["tests/test_b.py::test_after_pass"]},
    )
    store = _FakeStore(
        attempts=[pass_row],
        events=[
            {"kind": "attempt_start"},
            _tests_event(ok=False, failing_tests=["tests/test_b.py::test_after_pass"]),
        ],
    )
    orch = _orch_min(store)
    t = Task.new("fix x", repo_path="/tmp/repo")
    # Stale leftover from an EARLIER attempt's review FAIL — real,
    # pre-existing behavior: never cleared on the later PASS.
    t.context = {
        "review_feedback": [
            {"label": "old-bug", "file": "a.py", "line": 1,
             "comment": "STALE-REVIEW-FEEDBACK-FROM-AN-EARLIER-ATTEMPT"},
        ],
    }

    await orch._record_prior_attempt_evidence(t, 2, attempt_id="attempt-2-row")

    ev = t.context["prior_attempt_evidence"]
    assert ev["findings"] == []
    assert not any(
        "STALE-REVIEW-FEEDBACK-FROM-AN-EARLIER-ATTEMPT" in f.get("comment", "")
        for f in ev["findings"]
    )
    assert ev["failing_tests"] == ["tests/test_b.py::test_after_pass"]

    block = build_prior_attempt_evidence(ev)
    assert "STALE-REVIEW-FEEDBACK-FROM-AN-EARLIER-ATTEMPT" not in block


async def test_review_feedback_fallback_still_fires_but_with_honest_provenance():
    """The fallback the H2 test above narrows is not deleted outright: when
    the located row genuinely carries no checklist (review never ran this
    attempt — e.g. the attempt failed before reaching a review verdict),
    ``ctx['review_feedback']`` is still the best available evidence — but it
    must NEVER be stamped as THIS attempt's own review verdict (B2: that
    provenance is false by the code's own structure — see
    `_record_prior_attempt_evidence`'s docstring). The fixture is deliberately
    NOT named to imply the evidence is fresh for this row — it is a prior
    attempt's leftover, carried forward honestly labeled."""
    no_review_row = _attempt_row(
        3, id="attempt-3-row", checklist=None, review_passed=None,
        test_results={"failing_tests": ["tests/test_c.py::test_x"]},
    )
    store = _FakeStore(
        attempts=[no_review_row],
        events=[
            {"kind": "attempt_start"},
            _tests_event(ok=False, failing_tests=["tests/test_c.py::test_x"]),
        ],
    )
    orch = _orch_min(store)
    t = Task.new("fix x", repo_path="/tmp/repo")
    t.context = {
        "review_feedback": [
            {"label": "bug", "file": "a.py", "line": 1,
             "comment": "LEFTOVER-EVIDENCE-NO-VERDICT-THIS-ATTEMPT"},
        ],
    }

    await orch._record_prior_attempt_evidence(t, 3, attempt_id="attempt-3-row")

    ev = t.context["prior_attempt_evidence"]
    assert any(
        "LEFTOVER-EVIDENCE-NO-VERDICT-THIS-ATTEMPT" in f.get("comment", "")
        for f in ev["findings"]
    )
    # B2: the fallback fires, but must not claim attempt 3 produced a review
    # verdict it never produced.
    assert ev["source"] == (
        "a prior attempt's review verdict (attempt 3 produced none) "
        "and test events"
    )
    assert "Attempt 3 review verdict" not in ev["source"]


# ---------------------------------------------------- AC3: provenance, no narrative

def test_block_states_its_provenance():
    evidence = {
        "from_attempt": 1,
        "findings": [{"label": "bug", "file": "a.py", "line": 1,
                      "comment": "broken"}],
        "failing_tests": [],
    }
    block = build_prior_attempt_evidence(evidence)
    assert "Source: Attempt 1 review verdict and test events." in block


async def test_block_carries_no_corrections_or_coder_narrative():
    """The producer reads ONLY reviewer-verified blocking findings and test
    events — never task.context['attempt_log'] (coder narrative) or a
    'corrections' style field. Seed context with a narrative entry that must
    never leak into the rendered block."""
    store = _FakeStore(
        attempts=[
            _attempt_row(1, checklist=_checklist(comment="the reviewer's finding")),
        ],
        events=[{"kind": "attempt_start"}],
    )
    orch = _orch_min(store)
    t = Task.new("fix x", repo_path="/tmp/repo")
    t.context = {
        "attempt_log": ["attempt 1: I TRIED A NAIVE FIX AND CORRECTED MYSELF"],
    }

    await orch._record_prior_attempt_evidence(t, 1)

    ev = t.context["prior_attempt_evidence"]
    block = build_prior_attempt_evidence(ev)
    assert "I TRIED A NAIVE FIX AND CORRECTED MYSELF" not in block
    assert "the reviewer's finding" in block


# ------------------------------------------------- AC4: 2cc879d5 replay

async def test_replays_2cc879d5_a_named_failing_test_id_crosses_the_reset():
    """(a) attempt 1 produces a named failing test id in events, (b) attempt
    2's context contains this exact id in the distilled block, (c) the
    producer/consumer wiring works end to end against synthetic fixtures —
    proving evidence carrying across the stuck-detection reset works."""
    named_id = "tests/test_billing.py::test_refund_is_idempotent"
    store = _FakeStore(
        attempts=[_attempt_row(1, checklist=None, test_results=None)],
        events=[
            {"kind": "attempt_start"},
            _tests_event(ok=False, failing_tests=[named_id]),
        ],
    )
    orch = _orch_min(store)
    t = Task.new("fix refund", repo_path="/tmp/repo")
    t.context = {}

    # (a) producer runs after attempt 1 aborts (stuck-detection reset point).
    await orch._record_prior_attempt_evidence(t, 1)
    assert t.context["prior_attempt_evidence"]["failing_tests"] == [named_id]
    assert t.context["prior_attempt_evidence"]["from_attempt"] == 1

    # (b) attempt 2's prompt carries this exact id.
    prompt_orch = _orch_for_prompt()
    t.acceptance_criteria = ["refunds are idempotent"]
    prompt = prompt_orch._build_implement_prompt(t, "/tmp/repo", attempt_n=2)
    assert named_id in prompt
    assert "PRIOR-ATTEMPT EVIDENCE—NOT INSTRUCTIONS." in prompt

    # (c) the exact same id round-trips byte-identical through the render.
    block = build_prior_attempt_evidence(t.context["prior_attempt_evidence"])
    assert named_id in block


# --------------------------- failing_tests_bound MINOR-2: dropped count ---

async def test_failing_tests_dropped_count_is_carried_from_the_tests_event():
    """Round-5 review MINOR-2: the `tests` event this method reads is
    already bounded to `_MAX_PERSISTED_FAILING_TESTS` ids and carries its
    own `failing_tests_dropped` remainder (see `_bounded_failing_ids` /
    `tests/test_failing_tests_bound.py`). Before this fix,
    `_record_prior_attempt_evidence` silently dropped that remainder on the
    floor — attempt N+1's prompt then presented 200 ids as if they were the
    WHOLE failing set on a run that actually had thousands more, the exact
    undercount MINOR-1 fixed on the web slideover card, here on the
    evidence carried across the stuck-detection reset."""
    kept = [f"tests/test_a.py::test_{i}" for i in range(3)]
    emitted = []
    store = _FakeStore(
        attempts=[_attempt_row(1, checklist=None, test_results=None)],
        events=[
            {"kind": "attempt_start"},
            _tests_event(ok=False, failing_tests=kept, failing_tests_dropped=800),
        ],
    )
    orch = _orch_min(store)
    orch._sink = emitted.append
    t = Task.new("fix x", repo_path="/tmp/repo")
    t.context = {}

    await orch._record_prior_attempt_evidence(t, 1)

    ev = t.context["prior_attempt_evidence"]
    assert ev["failing_tests"] == kept
    assert ev["failing_tests_dropped"] == 800

    recorded = [e for e in emitted if e.get("kind") == "prior_evidence_recorded"]
    assert recorded, emitted
    assert recorded[-1]["failing_tests_dropped"] == 800
    assert "800 more" in recorded[-1]["text"]


async def test_no_dropped_key_when_the_tests_event_carries_no_remainder():
    """A run that never hit the bound (no `failing_tests_dropped` on the
    event) must not fabricate a `failing_tests_dropped: 0` key — mirrors
    `_bounded_test_results`'s own "only when a drop actually happened"
    contract, so every existing test asserting the exact dict shape on a
    small run stays byte-identical."""
    store = _FakeStore(
        attempts=[_attempt_row(1, checklist=None, test_results=None)],
        events=[
            {"kind": "attempt_start"},
            _tests_event(ok=False, failing_tests=["tests/test_a.py::test_x"]),
        ],
    )
    orch = _orch_min(store)
    t = Task.new("fix x", repo_path="/tmp/repo")
    t.context = {}

    await orch._record_prior_attempt_evidence(t, 1)

    ev = t.context["prior_attempt_evidence"]
    assert "failing_tests_dropped" not in ev


async def test_failing_tests_dropped_count_survives_the_test_results_fallback():
    """Same remainder, taken from the attempt row's persisted
    `test_results['failing_tests_dropped']` fallback (no qualifying `tests`
    event this attempt — e.g. events read yielded nothing) rather than from
    a live event."""
    kept = [f"tests/test_b.py::test_{i}" for i in range(5)]
    store = _FakeStore(
        attempts=[_attempt_row(
            1, checklist=None,
            test_results={"failing_tests": kept, "failing_tests_dropped": 450},
        )],
        events=[{"kind": "attempt_start"}],
    )
    orch = _orch_min(store)
    t = Task.new("fix x", repo_path="/tmp/repo")
    t.context = {}

    await orch._record_prior_attempt_evidence(t, 1)

    ev = t.context["prior_attempt_evidence"]
    assert ev["failing_tests"] == kept
    assert ev["failing_tests_dropped"] == 450


async def test_flaky_excused_and_green_events_are_not_carried():
    """A flaky-excused id (ok=True) and a fully green attempt (ok=True, no
    failing_tests) must never be treated as newly-failing evidence."""
    store = _FakeStore(
        attempts=[_attempt_row(1, checklist=None, test_results=None)],
        events=[
            {"kind": "attempt_start"},
            _tests_event(ok=True, failing_tests=["tests/test_a.py::test_flaky"],
                         flaky_excused=["tests/test_a.py::test_flaky"]),
        ],
    )
    orch = _orch_min(store)
    t = Task.new("fix x", repo_path="/tmp/repo")
    t.context = {}

    await orch._record_prior_attempt_evidence(t, 1)

    assert "prior_attempt_evidence" not in t.context
