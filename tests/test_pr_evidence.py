"""Part 2 of the c7da49d4 decomposition: the evidence PIPELINE.

`core/pr_evidence.py`'s `PrEvidence` is the single structured object every
truth pin (a concrete, measured-fact sentence) in the PR body must be backed
by — gathered once by `Orchestrator._gather_evidence` and threaded through
every section renderer. These tests hold that guarantee against the REAL
renderer (`Orchestrator._pr_body`), not against a hand-built string: a
checker that only reads `PrEvidence.truth_pins()` in isolation could pass
while the renderer quietly drifted away from it.
"""
import json
import re
from pathlib import Path


from no_human.config import load_config
from no_human.core.orchestrator import Orchestrator
from no_human.core.pr_evidence import PrEvidence, visible_chars
from no_human.core.task import Task
from no_human.notify.slack import SlackNotifier


class _Backend:
    async def run(self, *a, **k):  # pragma: no cover
        raise AssertionError("backend should not run here")


class _Commit:
    files_changed = 4
    insertions = 120
    deletions = 18
    sha = "a" * 40


class _Result:
    final_text = ("Added the retry-with-backoff wrapper around the flaky "
                  "webhook call and a regression test that pins the 3-retry "
                  "cap.")
    num_turns = 9


def _orch(store, tmp_path):
    cfg = load_config(tmp_path / "config.yaml")
    return Orchestrator(store, cfg.data, _Backend(), SlackNotifier(None))


def _receipts():
    cmds = [
        ("uv run pytest tests/test_webhook_retry.py -q",
         "5 passed in 1.21s", "test"),
        ("uv run ruff check src/no_human/net/webhook.py",
         "All checks passed!", "lint"),
    ]
    return [{"command": c, "output_excerpt": o, "kind": k, "truncated": False,
             "output_bytes": len(o)} for c, o, k in cmds]


def _task():
    """A representative task: the fixture criterion 3's char-count numbers
    and the truth-pin predicate below are both driven against."""
    t = Task.new("retry the flaky webhook call", repo_path="/r")
    t.acceptance_criteria = [
        "webhook POST retries up to 3 times on a 5xx before failing",
        "a regression test pins the retry cap and the backoff delay",
    ]
    t.context = {"review_history": [
        {"round": 1, "sha": "a" * 40, "passed": False,
         "blocking": ["the backoff delay is not tested"]},
        {"round": 2, "sha": "a" * 40, "passed": True, "blocking": []},
    ]}
    return t


# Every regex family a "truth pin" — a concrete, measured-fact sentence — can
# take in the rendered body. Anything matching one of these must be a value in
# `evidence.truth_pins()`; anything in `evidence.truth_pins()` must appear in
# the body verbatim. Both directions are checked by
# `test_every_rendered_truth_pin_has_backing_evidence` below.
_PIN_PATTERNS = [
    re.compile(r"\*\*(?:PASSED|not passed)\*\* — \d+ rounds?"),
    re.compile(r"\d+ commands? recorded"),
    re.compile(r"(?:PASS|FAIL) — \d+ passed, \d+ failed, \d+ errors"),
    re.compile(r"\| CI \| [^|]+ \|"),
    # The merge-policy row's pin is `PolicyVerdict.summary` (see
    # `PrEvidence.merge_policy_pin`) — NOT the whole rendered row (unlike the
    # CI pattern above, whose pin IS the whole row) — so this pattern
    # captures just the summary text after the glyph, matching what
    # `truth_pins()` actually stores.
    re.compile(r"\| Merge policy \| \S+ ((?:not )?ready — [^|\n]+) \|"),
]


def _pins_in_body(body: str) -> list[str]:
    found: list[str] = []
    for pat in _PIN_PATTERNS:
        for m in pat.finditer(body):
            found.append(m.group(1) if m.groups() else m.group(0))
    return found


def test_every_rendered_truth_pin_has_backing_evidence(store, tmp_path):
    """ACCEPTANCE CRITERION 2's predicate: every truth pin the PR body
    renders is a value of `evidence.truth_pins()` — so a fact printed in the
    body can never drift from the evidence object supposed to back it.
    Driven against a REAL `_pr_body()` render, not a hand-built string; see
    `test_the_predicate_catches_an_unbacked_truth_pin` for proof this check
    actually fails on an unbacked pin rather than passing vacuously.
    """
    orch = _orch(store, tmp_path)
    task = _task()
    test_evidence = {"ran": True, "ok": True, "passed": 5, "failed": 0,
                     "errors": 0}
    receipts = _receipts()
    head_sha = _Commit.sha
    evidence = orch._gather_evidence(
        task, test_evidence=test_evidence, receipts=receipts,
        head_sha=head_sha)
    body = orch._pr_body(task, _Commit(), _Result(),
                         test_evidence=test_evidence, receipts=receipts)

    pins_backed = set(evidence.truth_pins().values())
    pins_rendered = _pins_in_body(body)
    assert pins_rendered, (
        "the fixture renders no truth pin at all — the predicate below "
        "would pass vacuously")
    for pin in pins_rendered:
        assert pin in pins_backed, (
            f"truth pin rendered with NO backing evidence field: {pin!r}\n"
            f"evidence.truth_pins() = {evidence.truth_pins()!r}")

    # ...and the other direction: every pin the object claims to back really
    # does reach the body. Declaring a pin nobody renders is not a lie, but a
    # renderer that stops reading an object's own field IS the drift this
    # module exists to prevent.
    for field, pin in evidence.truth_pins().items():
        assert pin in body, (
            f"evidence.{field} backs {pin!r}, which the body never renders")


def _verifier_dict(*, verifier_id: str, passed: bool, evidence: str = "e",
                    file: str = "", line: int = 0,
                    files_checked: list[str] | None = None,
                    unavailable: bool = False) -> dict:
    return {
        "verifier_id": verifier_id, "passed": passed, "no_verdict": False,
        "unavailable": unavailable,
        "evidence": evidence, "file": file, "line": line, "comment": "",
        "severity": "high", "files_checked": files_checked or [],
        "tokens_used": 0,
    }


def test_verifiers_pin_renders_the_summary_line_shape():
    """AC 6. `verifiers_pin()` is `None` (no field-backed pin at all — not
    an empty-string pin) when no verifiers ran; otherwise the same
    "N of M satisfied" / "K of M failed — ids" shape `summary_line`
    produces (duplicated over dicts on purpose — see the docstring)."""
    assert PrEvidence(verifiers=None).verifiers_pin() is None
    assert PrEvidence(verifiers=[]).verifiers_pin() is None

    all_pass = PrEvidence(verifiers=[
        _verifier_dict(verifier_id="a", passed=True),
        _verifier_dict(verifier_id="b", passed=True),
    ])
    assert all_pass.verifiers_pin() == "2 of 2 satisfied"

    one_fail = PrEvidence(verifiers=[
        _verifier_dict(verifier_id="a", passed=True),
        _verifier_dict(verifier_id="b", passed=False),
    ])
    assert one_fail.verifiers_pin() == "1 of 2 failed — b"


def test_the_evidence_table_carries_a_verifiers_row(store, tmp_path):
    """AC 1 / AC 6: the verifiers row sits in the Evidence table (between
    the review row and the tamper section, per `_evidence_section`'s
    ordering) and its per-rule detail folds behind its own `<details>`."""
    orch = _orch(store, tmp_path)
    task = _task()
    head_sha = _Commit.sha
    task.context["verifier_results"] = {
        head_sha: [
            _verifier_dict(verifier_id="no-todo", passed=True,
                            files_checked=["a.py", "b.py"]),
            _verifier_dict(verifier_id="no-print", passed=False,
                            evidence="found a print()", file="c.py", line=4),
            _verifier_dict(verifier_id="docs-updated", passed=False,
                            evidence="docs not touched"),
            _verifier_dict(verifier_id="typed", passed=True, files_checked=["a.py"]),
        ]
    }
    test_evidence = {"ran": True, "ok": True, "passed": 5, "failed": 0,
                     "errors": 0}
    body = orch._pr_body(task, _Commit(), _Result(),
                         test_evidence=test_evidence, receipts=_receipts())
    ev = body.split("## Evidence\n", 1)[1].split("\n## ", 1)[0]
    assert "| Verifiers | ❌ 2 of 4 failed — docs-updated, no-print |" in ev
    assert "<details><summary>Verifiers (4)</summary>" in ev
    assert "no-todo" in ev and "no-print" in ev and "c.py:4" in ev
    assert "found a print()" in ev


def test_an_unavailable_verifier_renders_distinctly_from_a_genuine_failure(
    store, tmp_path,
):
    """A rule that reached no verdict even after `run_verifiers`'s bounded
    retry (`unavailable=True`) is an infra/config signal, not a violation —
    a human reading the PR must be able to tell "unverified" from
    "violated" per rule, in the rule's own row (task requirement 3). The
    unavailable-only case must not use the genuine-failure glyph (❌) at
    either the summary-row or per-item level."""
    orch = _orch(store, tmp_path)
    task = _task()
    head_sha = _Commit.sha
    task.context["verifier_results"] = {
        head_sha: [
            _verifier_dict(verifier_id="no-todo", passed=True,
                            files_checked=["a.py"]),
            _verifier_dict(verifier_id="flaky-rule", passed=False,
                            evidence="no verdict after retry: judge unavailable",
                            unavailable=True),
        ]
    }
    body = orch._pr_body(task, _Commit(), _Result(), receipts=_receipts())
    ev = body.split("## Evidence\n", 1)[1].split("\n## ", 1)[0]
    assert "| Verifiers | ⚠️" in ev, (
        "an unavailable-only round must not render the genuine-failure glyph")
    assert "❌" not in ev
    # Per-item: the unavailable rule's own bullet carries the warning glyph,
    # never the genuine-failure one. Bullets (not the summary table row,
    # which is checked above) start with "- ".
    item_lines = [line for line in ev.splitlines()
                  if "flaky-rule" in line and line.lstrip().startswith("- ")]
    assert item_lines and all("⚠️" in line and "❌" not in line for line in item_lines)


def test_a_genuine_failure_keeps_its_glyph_alongside_an_unavailable_sibling(
    store, tmp_path,
):
    """The mixed case (task requirement / attempt-1 review finding): a
    genuinely failing rule in the SAME round as an unavailable one must
    still render with the genuine-failure glyph — the unavailable sibling
    must never soften or hide it."""
    orch = _orch(store, tmp_path)
    task = _task()
    head_sha = _Commit.sha
    task.context["verifier_results"] = {
        head_sha: [
            _verifier_dict(verifier_id="no-print", passed=False,
                            evidence="found a print()", file="c.py", line=4),
            _verifier_dict(verifier_id="flaky-rule", passed=False,
                            evidence="no verdict after retry: judge unavailable",
                            unavailable=True),
        ]
    }
    body = orch._pr_body(task, _Commit(), _Result(), receipts=_receipts())
    ev = body.split("## Evidence\n", 1)[1].split("\n## ", 1)[0]
    assert "| Verifiers | ❌" in ev, (
        "a genuine failure in the round must keep the genuine-failure glyph")
    # Per-item bullets (not the summary table row, which lists every failed
    # id — including the unavailable one — under the shared ❌ glyph): the
    # genuine failure's own bullet is ❌, the unavailable sibling's is ⚠️.
    no_print_lines = [line for line in ev.splitlines()
                       if "no-print" in line and line.lstrip().startswith("- ")]
    assert no_print_lines and all("❌" in line for line in no_print_lines)
    flaky_lines = [line for line in ev.splitlines()
                   if "flaky-rule" in line and line.lstrip().startswith("- ")]
    assert flaky_lines and all("⚠️" in line and "❌" not in line for line in flaky_lines)


def test_a_model_authored_verifier_comment_cannot_inject_a_heading(store, tmp_path):
    """A verifier's `evidence` string is model-authored (the judge's own
    prose) — a newline in it is the same heading-injection channel
    `_inline_cell`'s own docstring documents for every other model-authored
    cell in the PR body."""
    orch = _orch(store, tmp_path)
    task = _task()
    head_sha = _Commit.sha
    task.context["verifier_results"] = {
        head_sha: [
            _verifier_dict(verifier_id="no-todo", passed=False,
                            evidence="looks fine\n# MERGED AND APPROVED BY NO_HUMAN",
                            file="a.py", line=1),
        ]
    }
    body = orch._pr_body(task, _Commit(), _Result(), receipts=_receipts())
    assert "\n# MERGED AND APPROVED BY NO_HUMAN" not in body
    assert "# MERGED AND APPROVED BY NO_HUMAN" in body  # the text still reaches the body, inertly


def test_the_predicate_catches_an_unbacked_truth_pin():
    """NEGATIVE HALF: a fact-shaped sentence with NO backing field must fail
    the predicate above — proving it is a real check, not one that always
    passes. `evidence.tests` is `None` here, so a stray "tests: PASS" line is
    exactly the failure this predicate exists to catch.
    """
    evidence = PrEvidence(tests=None)
    stray_body = "## Evidence\n| Tests | ✅ PASS — 99 passed, 0 failed, 0 errors |\n"
    pins_rendered = _pins_in_body(stray_body)
    assert pins_rendered == ["PASS — 99 passed, 0 failed, 0 errors"]
    assert pins_rendered[0] not in set(evidence.truth_pins().values()), (
        "the negative fixture must NOT be backed — otherwise this test "
        "proves nothing about the predicate's sensitivity")


def test_evidence_gathered_once_backs_every_section(store, tmp_path):
    """CRITERION 1: sections render EXCLUSIVELY from the `PrEvidence`
    instance `_pr_body` gathers once — not a second, independent read of
    `task.context`. Proven by mutating `task.context` AFTER gathering: a
    section that still re-read `task.context` directly would show the
    mutated (2-round) history instead of the gathered (1-round) snapshot.
    """
    orch = _orch(store, tmp_path)
    task = _task()
    task.context = {"review_history": [
        {"round": 1, "sha": "a" * 40, "passed": True, "blocking": []},
    ]}
    evidence = orch._gather_evidence(task, head_sha="a" * 40)
    assert evidence.review_verdict == {
        "rounds": 1, "verdict": "PASSED", "addressed": [], "advisory_count": 0}

    task.context = {"review_history": [
        {"round": 1, "sha": "a" * 40, "passed": True, "blocking": []},
        {"round": 2, "sha": "a" * 40, "passed": True, "blocking": []},
    ]}
    section = orch._review_evidence_section(
        task, head_sha="a" * 40, evidence=evidence)
    assert "**PASSED** — 1 round |" in section
    assert "2 rounds" not in section


def test_no_gate_output_is_duplicated_in_the_rendered_body(store, tmp_path):
    """CRITERION 5: repro/tests/review_verdict each appear exactly once in
    the rendered body — no gate output is duplicated inline anymore."""
    orch = _orch(store, tmp_path)
    task = _task()
    test_evidence = {"ran": True, "ok": True, "passed": 5, "failed": 0,
                     "errors": 0}
    receipts = _receipts()
    body = orch._pr_body(task, _Commit(), _Result(),
                         test_evidence=test_evidence, receipts=receipts)
    assert body.count("**PASSED** — 2 rounds") == 1
    # D1.1: the body no longer carries the full receipts log at all (it moved
    # to the artifact file / PR comment) — the short pointer states the count
    # exactly once.
    assert body.count("commands recorded") == 1
    assert body.count("| Tests | ✅ PASS") == 1
    # RESTORED, WIDER (review finding #5): the row-wrapped substring above
    # would not have caught a duplicate rendered WITHOUT the `| Tests | … |`
    # wrapper — which is exactly the shape an earlier `_verification_section`
    # shipped (its own bullet, reusing the unwrapped verdict text). Assert
    # the unwrapped verdict text itself is not duplicated anywhere in the body.
    assert body.count("PASS — 5 passed, 0 failed, 0 errors") == 1


def test_the_pr_body_char_count_drops_at_least_10_percent_when_collapsed(
        store, tmp_path):
    """CRITERION 3, executable: the SCANNABLE character count (everything
    outside a `<details>` fold — what a reader sees without expanding
    anything, since GitHub renders `<details>` closed by default) drops by
    at least 10% relative to the raw total, on this module's representative
    fixture (`_task()` / `_receipts()` above).

    2026-08-21 (pre-D1.1): 6442 chars total/visible before this fixture had
    any `<details>` fold at all, 6536 total (+1.5%) / 890 visible after —
    an 86.2% drop, almost entirely the receipts appendix folding away.

    2026-08-31 (D1.1): the receipts no longer reach the body at all — folded
    or not (`_verification_appendix` still renders that same appendix
    verbatim, but only to this attempt's artifact file / PR comment now, via
    `_verification_section`'s short pointer) — so both totals shrink far
    below the 2026-08-21 numbers: 999 total / 873 visible, a 12.6% drop from
    what still folds on this fixture (the review-addressed list — the one
    finding this fixture's first round raised and the second addressed).
    """
    orch = _orch(store, tmp_path)
    task = _task()
    test_evidence = {"ran": True, "ok": True, "passed": 5, "failed": 0,
                     "errors": 0}
    receipts = _receipts()
    body = orch._pr_body(task, _Commit(), _Result(),
                         test_evidence=test_evidence, receipts=receipts)
    total = len(body)
    visible = visible_chars(body)
    assert visible < total
    reduction = (total - visible) / total
    assert reduction >= 0.10, f"only a {reduction:.1%} reduction: {body!r}"


def test_the_merge_policy_row_renders_from_a_real_policyverdict(store, tmp_path):
    """DEFECT 2's fix: `_PIN_PATTERNS` (and the pin-backing predicate above)
    must actually exercise a REAL `PolicyVerdict.as_dict()` — not just the
    review/tests/CI rows — so a vacuous pin-coverage gap (a merge-policy row
    nobody ever renders in this suite) cannot hide behind the other patterns.
    A real 3-rule, all-passing verdict must render the exact row and fold
    `_merge_policy_evidence_section` promises.
    """
    from no_human.core.merge_policy import PolicyVerdict, RuleVerdict

    verdict = PolicyVerdict(
        ready=True,
        rules=(
            RuleVerdict(name="review_passed", passed=True, detail="PASSED — 2 rounds"),
            RuleVerdict(name="tests_ran_and_passed", passed=True, detail="5 passed, 0 failed"),
            RuleVerdict(name="tamper_guard_clear", passed=True, detail="guard never fired"),
        ),
        source="default",
    )
    assert verdict.summary == "ready — 3 of 3 rules satisfied"

    orch = _orch(store, tmp_path)
    task = _task()
    test_evidence = {"ran": True, "ok": True, "passed": 5, "failed": 0,
                     "errors": 0}
    receipts = _receipts()
    head_sha = _Commit.sha
    evidence = orch._gather_evidence(
        task, test_evidence=test_evidence, receipts=receipts,
        head_sha=head_sha, merge_policy=verdict.as_dict())
    body = orch._pr_body(task, _Commit(), _Result(),
                         test_evidence=test_evidence, receipts=receipts,
                         merge_policy=verdict.as_dict())

    ev = body.split("## Evidence\n", 1)[1].split("\n## ", 1)[0]
    assert "| Merge policy | ✅ ready — 3 of 3 rules satisfied |" in ev
    assert "<details><summary>Merge-ready policy (3 rules, source: default)</summary>" in ev
    assert "review_passed" in ev and "tamper_guard_clear" in ev

    # ...and the pin predicate this module holds actually sees it: the row's
    # pin must be a value of `evidence.truth_pins()`.
    pins_rendered = _pins_in_body(body)
    assert "ready — 3 of 3 rules satisfied" in pins_rendered
    assert "ready — 3 of 3 rules satisfied" in set(evidence.truth_pins().values())


def test_merge_policy_section_discloses_a_failed_compute():
    """`_merge_policy_evidence_section` is a pure function of `PrEvidence` —
    exercised directly here (unit-level, no orchestrator run) against its
    three distinguishable states:

    1. ``merge_policy=None, merge_policy_error="RuntimeError"`` — the compute
       was attempted and raised: a NOT-COMPUTED row naming the exception
       class, and neither a fabricated ✅/❌ verdict nor a fold with nothing
       to show.
    2. ``merge_policy=None, merge_policy_error=None`` — no policy was ever
       attempted for this head (the ordinary case for most of this repo's
       history, and for any head with no `.no_human/merge_policy.yaml`
       compute yet run): renders "", exactly as before this field existed.
    3. ``merge_policy={...ready...}, merge_policy_error="RuntimeError"`` — a
       stamped verdict always wins over a stale error string (this shape
       cannot arise from `_finalize`'s own `replace(...)` calls, but the
       renderer's OWN precedence must not depend on that never happening).
    """
    failed = PrEvidence(merge_policy_error="RuntimeError")
    section = Orchestrator._merge_policy_evidence_section(failed)
    assert "| Merge policy |" in section
    assert "NOT COMPUTED — the merge-ready verdict could not be computed" in section
    assert "RuntimeError" in section
    assert "✅" not in section
    assert "❌" not in section

    empty = PrEvidence()
    assert Orchestrator._merge_policy_evidence_section(empty) == ""

    from no_human.core.merge_policy import PolicyVerdict, RuleVerdict

    verdict = PolicyVerdict(
        ready=True,
        rules=(RuleVerdict(name="review_passed", passed=True, detail="PASSED — 2 rounds"),),
        source="default",
    )
    stamped_and_stale_error = PrEvidence(
        merge_policy=verdict.as_dict(), merge_policy_error="RuntimeError")
    section2 = Orchestrator._merge_policy_evidence_section(stamped_and_stale_error)
    assert "| Merge policy | ✅ ready — 1 of 1 rules satisfied |" in section2
    assert "NOT COMPUTED" not in section2
    assert "RuntimeError" not in section2


def test_merge_policy_error_is_inline_cell_safe():
    """A `merge_policy_error` string is an exception CLASS NAME, so in
    practice it can never carry a newline — but the renderer routes it
    through `_inline_cell` anyway rather than trusting that invariant, the
    same discipline every other model-influenced cell in the PR body follows
    (see `test_a_model_authored_verifier_comment_cannot_inject_a_heading`
    for the sibling case on a verifier's `evidence` string). This proves the
    discipline is actually wired for THIS field, not merely assumed safe."""
    evidence = PrEvidence(merge_policy_error="Boom\n# MERGED AND APPROVED BY NO_HUMAN")
    section = Orchestrator._merge_policy_evidence_section(evidence)
    assert "\n# MERGED AND APPROVED BY NO_HUMAN" not in section
    assert "# MERGED AND APPROVED BY NO_HUMAN" in section  # still reaches the row, inertly


def test_evidence_renders_as_a_table_with_one_row_per_gate(store, tmp_path):
    """The operator's directive (2026-08-21): a reviewer meets the gate results
    FIRST, as one table of mechanical facts, and the detail behind each row is
    delivered folded — never dropped."""
    orch = _orch(store, tmp_path)
    task = _task()
    test_evidence = {"ran": True, "ok": True, "passed": 5, "failed": 0,
                     "errors": 0}
    body = orch._pr_body(task, _Commit(), _Result(),
                         test_evidence=test_evidence, receipts=_receipts())
    ev = body.split("## Evidence\n", 1)[1].split("\n## ", 1)[0]
    assert ev.startswith("| Check | Result |\n|---|---|\n")
    assert "| Independent review | ✅ **PASSED** — 2 rounds |" in ev
    assert "| Tests | ✅ PASS — 5 passed, 0 failed, 0 errors |" in ev
    assert "Decisive first" not in body
    assert "### Independent review" not in body
    assert "### Test evidence" not in body
    assert "<details><summary>1 finding raised and addressed" in ev
    assert "the backoff delay is not tested" in ev


# ═══ 2026-08-21: the body is SHORT by construction, pinned on a real PR ═════ #

_FIXTURE_574 = Path(__file__).resolve().parent.parent / "testdata" / "pr_body_574.json"


def _visible_words(body: str) -> int:
    return len(re.sub(r"<details>.*?</details>", "", body, flags=re.S).split())


def test_template_overhead_is_under_900_visible_chars(store, tmp_path):
    """With NOTHING to say, the template's own prose is bounded — a body's
    length must come from the task, not from boilerplate."""
    orch = _orch(store, tmp_path)
    t = Task.new("t", repo_path="/r")
    t.acceptance_criteria = []
    body = orch._pr_body(t, _Commit(), _Result(), test_evidence=None, receipts=[])
    assert visible_chars(body) < 900, (visible_chars(body), body)


def test_a_real_pr_renders_under_300_visible_words_beyond_its_criteria(
        store, tmp_path):
    """no_human-private #574 shipped 2,791 words (771 before the first fold).
    The same inputs — its title, criteria, intake Q&A, review history, test
    run, ten receipts and the coder's report — must now render under 300
    visible words on top of the criteria text, which is the task's own.

    D1.1 (2026-08-31): the ten receipts no longer reach the body AT ALL —
    raw or folded — they move to this attempt's artifact file and the PR
    comment; the body carries only the short pointer to them plus the FINAL
    test verdict, which is why this fixture's `visible_chars` dropped from
    6,536 (all `<details>` folds, receipts included) to well under 3,000
    (see `test_the_short_section_carries_no_receipt_text` for the exact
    per-receipt check)."""
    fx = json.loads(_FIXTURE_574.read_text())
    orch = _orch(store, tmp_path)
    t = Task.new(fx["title"], repo_path="/r")
    t.acceptance_criteria = fx["acceptance_criteria"]
    t.context = fx["context"]
    r = _Result()
    r.final_text = fx["final_text"]
    body = orch._pr_body(t, _Commit(), r, test_evidence=fx["test_evidence"],
                         receipts=fx["receipts"])
    crit_words = sum(len(c.split()) for c in t.acceptance_criteria)
    extra = _visible_words(body) - crit_words
    assert extra < 300, (extra, re.sub(r"<details>.*?</details>", "", body, flags=re.S))
    assert visible_chars(body) < 3000, visible_chars(body)
    # ...and what WAS dropped is exactly the raw receipts — nothing else:
    # the report's evidence and the test verdict are still there, in full.
    for rec in fx["receipts"]:
        assert rec["command"] not in _scannable(body), rec["command"]
    assert "Full verification log:" in body
    assert "| Independent review | ✅ **PASSED** — 1 round |" in body
    assert "| Tests | ✅ PASS — 9380 passed, 0 failed, 0 errors |" in body


# ══════ D1.1 (2026-08-31): receipts out of the PR body, final results only ══ #
#
# The operator's directive: `_verification_section` embedded up to 40 commands
# with 12 raw fenced outputs of whatever the coder ran mid-work — "the endless
# in-progress test runs" the operator sees. These four tests are the ones the
# task brief names directly: (a) no fenced command output reaches the body;
# (b) exactly one line per test layer, the FINAL run only; (c) a pointer to the
# full log; (d) the body stays under the 6,000-char budget (measured as
# `visible_chars` — everything a reader sees without expanding a fold, since a
# later task's media section and any long folded content are explicitly
# outside this budget).

_MID_WORK_RECEIPT = {
    "command": "uv run pytest tests/test_webhook_retry.py -q",
    "output_excerpt": "2 failed, 3 passed in 0.81s", "kind": "test",
    "truncated": False, "output_bytes": 27,
}
_FINAL_RECEIPT = {
    "command": "uv run pytest tests/test_webhook_retry.py -q",
    "output_excerpt": "5 passed in 1.02s", "kind": "test",
    "truncated": False, "output_bytes": 18,
}


def _two_run_fixture():
    """Two receipts of the SAME check: a failing run mid-work, then the run
    that actually passed. `test_evidence` is what the orchestrator's OWN
    layered test run recorded for this attempt — its FINAL state, the
    5-passed run, exactly as `testing/plan_runner.py` only ever runs a layer
    once per attempt."""
    task = Task.new("fix the flaky retry", repo_path="/r")
    task.acceptance_criteria = ["a 5xx retries up to 3 times before failing"]
    receipts = [_MID_WORK_RECEIPT, _FINAL_RECEIPT]
    test_evidence = {"layers": ["unit: ✅ PASS — 5 passed, 0 failed, 0 errors"]}
    return task, receipts, test_evidence


def _scannable(text: str) -> str:
    """*text* with every `<details>` fold removed — what a reader sees
    without clicking, the same cut `visible_chars` measures."""
    return re.sub(r"<details>.*?</details>", "", text, flags=re.DOTALL)


def test_the_body_carries_no_fenced_command_output(store, tmp_path):
    """(a) Two pytest receipts — the first failing mid-work, the second
    passing — and NEITHER's raw output reaches the SCANNABLE body (what a
    reader sees before expanding anything): no fenced block, no excerpt text
    from either run. Since #23 the final run's excerpt does reach the body,
    but only inside the per-kind `<details>` fold of "How I verified this";
    the mid-work run's never does."""
    orch = _orch(store, tmp_path)
    task, receipts, test_evidence = _two_run_fixture()
    body = orch._pr_body(task, _Commit(), _Result(),
                         test_evidence=test_evidence, receipts=receipts)
    assert "```" not in _scannable(body), "a fenced code block reached the scannable body"
    assert "2 failed, 3 passed in 0.81s" not in body
    assert "5 passed in 1.02s" not in _scannable(body)
    assert "5 passed in 1.02s" in body, "the final run's output must be one click away"


def test_the_body_shows_only_the_final_layer_result(store, tmp_path):
    """(b) Exactly one line per test layer, its FINAL PASS/FAIL/NOT-RUN — the
    mid-work failure never appears anywhere, only the layer's last state.

    D1.1 review finding #5: an earlier version of `_verification_section`
    ALSO repeated this line as its own bullet, and the no-duplication guard
    was narrowed to the table-row shape so it could not catch that. The
    Evidence table is now the SINGLE one-line-per-layer surface — the final
    line appears EXACTLY ONCE in the whole body, not merely once in its
    `| Tests | … |` form."""
    orch = _orch(store, tmp_path)
    task, receipts, test_evidence = _two_run_fixture()
    body = orch._pr_body(task, _Commit(), _Result(),
                         test_evidence=test_evidence, receipts=receipts)
    assert body.count("unit: ✅ PASS — 5 passed, 0 failed, 0 errors") == 1
    assert "2 failed" not in body
    assert "3 passed" not in body


def test_the_body_points_to_the_full_log(store, tmp_path):
    """(c) A pointer line naming `nh logs <id>` reaches the body."""
    orch = _orch(store, tmp_path)
    task, receipts, test_evidence = _two_run_fixture()
    body = orch._pr_body(task, _Commit(), _Result(),
                         test_evidence=test_evidence, receipts=receipts)
    assert f"nh logs {task.id[:8]}" in body
    assert "Full verification log:" in body


def test_the_body_stays_under_the_6000_char_budget(store, tmp_path):
    """(d) `visible_chars(body) <= 6000` on the standard two-receipt fixture —
    the budget applies to what a reader sees before anything folded, per the
    controller's ruling; a later task's media section is explicitly outside
    it and is not exercised here."""
    orch = _orch(store, tmp_path)
    task, receipts, test_evidence = _two_run_fixture()
    body = orch._pr_body(task, _Commit(), _Result(),
                         test_evidence=test_evidence, receipts=receipts)
    assert visible_chars(body) <= 6000, visible_chars(body)


def test_ui_evidence_media_section_is_excluded_from_the_body_budget(store, tmp_path):
    """D1.2: the controller's ruling that visual proof lives OUTSIDE the
    6,000-visible-char budget, exercised directly (the test this module's
    own D1.1 comment above flagged as not yet covered). A media section big
    enough to blow the budget on its own must still leave `## Changes`
    UNTRIMMED — only `criteria_block` and `ui_evidence_section` are meant to
    be subtracted before the comparison, so a media section by itself must
    never trigger the trim marker."""
    orch = _orch(store, tmp_path)
    task, receipts, test_evidence = _two_run_fixture()
    huge_media = "## UI evidence\n" + ("![shot](https://raw.githubusercontent.com/"
                                       "acme/widget/nh-evidence/x/shot.png)\n" * 200)
    assert len(huge_media) > 6000, "premise: the media section alone must exceed the budget"
    body = orch._pr_body(task, _Commit(), _Result(),
                         test_evidence=test_evidence, receipts=receipts,
                         ui_evidence_section=huge_media)
    assert "(trimmed further to keep the PR body under its size budget)" not in body, (
        "a media section alone must never trigger the Changes-section trim")
    assert huge_media in body, "the full, untrimmed media section must reach the body"


def test_the_short_section_carries_no_receipt_text(store, tmp_path):
    """The short section's SCANNABLE text (outside every fold) never embeds
    a receipt's command or output, and a mid-work run's output appears
    nowhere in it — direct unit-level pin, independent of the end-to-end
    fixture above."""
    receipts = [_MID_WORK_RECEIPT, _FINAL_RECEIPT]
    section = Orchestrator._verification_section(
        receipts, task_id="deadbeef")
    for rec in receipts:
        assert rec["command"] not in _scannable(section)
        assert rec["output_excerpt"] not in _scannable(section)
    assert _MID_WORK_RECEIPT["output_excerpt"] not in section
    assert "```" not in _scannable(section)
    assert "nh logs deadbeef" in section


# ═══ D1.1 review findings #2, #5, #7, #8 (2026-08-31 fix round) ════════════ #

def test_the_pointer_never_leaks_an_absolute_home_path(store, tmp_path):
    """Review finding #2: an absolute path
    (`/Users/<local-username>/.no_human/...`) leaks the operator's local
    account name into a document a stranger reviews. The pointer must carry
    the `~`-relative form `docs/pr-body.md` already documents."""
    from pathlib import Path

    home = Path.home()
    real_path = str(home / ".no_human" / "artifacts" / "deadbeef" / "verification-attempt-1.md")
    section = Orchestrator._verification_section(
        [_MID_WORK_RECEIPT], task_id="deadbeef", artifact_path=real_path)
    assert real_path not in section, "the raw absolute path reached the pointer"
    assert str(home) not in section, "the home directory leaked in some other form"
    assert "~/.no_human/artifacts/deadbeef/verification-attempt-1.md" in section


def test_display_path_falls_back_when_not_under_home():
    """`_display_path`'s other branch: a path outside `$HOME` (a test
    fixture, or a relocated `NO_HUMAN_HOME`) is returned as-is rather than
    fabricating a `~` that would not resolve back to it."""
    assert Orchestrator._display_path("") == ""
    assert Orchestrator._display_path("/var/elsewhere/verification.md") == \
        "/var/elsewhere/verification.md"


def test_the_short_section_never_duplicates_a_layer_line(store, tmp_path):
    """Review finding #5 RULING: the Evidence table is the single
    one-line-per-layer surface; `_verification_section` shrinks to the
    pointer line (+ command count) with NO duplicated layer bullets."""
    section = Orchestrator._verification_section(
        [_MID_WORK_RECEIPT], task_id="deadbeef")
    assert "PASS" not in section and "FAIL" not in section and "NOT RUN" not in section, (
        "the short section renders a test verdict — that belongs to the "
        "Evidence table alone")


def test_the_artifact_is_attempt_scoped_not_task_scoped(store, tmp_path):
    """Review finding #7: a task's attempts run sequentially, each often on
    its own branch/PR — attempt 2's write must never clobber the file
    attempt 1's (still-open) PR body points a reader at."""
    orch = _orch(store, tmp_path)
    task = Task.new("t", repo_path="/r")
    p1 = orch._write_verification_artifact(
        task, [{"command": "pytest -q attempt1", "kind": "test",
                "output_excerpt": "1 passed", "truncated": False, "output_bytes": 8}],
        attempt_n=1)
    p2 = orch._write_verification_artifact(
        task, [{"command": "pytest -q attempt2", "kind": "test",
                "output_excerpt": "2 passed", "truncated": False, "output_bytes": 8}],
        attempt_n=2)
    assert p1 and p2 and p1 != p2, (p1, p2)
    from pathlib import Path
    assert "pytest -q attempt1" in Path(p1).read_text(), (
        "attempt 2's write clobbered attempt 1's artifact file")
    assert "pytest -q attempt2" in Path(p2).read_text()


def test_an_unknown_attempt_number_still_writes_something(store, tmp_path):
    """A best-effort attempt-number lookup that comes back empty must not
    crash the write — it falls back to a literal, still-unique-enough name
    rather than raising."""
    orch = _orch(store, tmp_path)
    task = Task.new("t", repo_path="/r")
    path = orch._write_verification_artifact(
        task, [{"command": "pytest -q", "kind": "test", "output_excerpt": "1 passed",
                "truncated": False, "output_bytes": 8}],
        attempt_n=None)
    assert path, "the write failed outright on a missing attempt number"
    assert "unknown" in path


# ─────────────────────── Finding #8: the hard size budget ─────────────────── #

def _oversized_result():
    """A normal-shaped report — several distinct paragraphs, no NOT-MET —
    that reaches ~1,500 visible chars by `_summary_section`'s OWN ordinary
    fold. Legally trimmable in full: nothing in it is exempt from a cap."""
    r = _Result()
    r.final_text = "\n\n".join(
        f"Paragraph {i}: rewrote the retry path in fetcher.py so a 429 "
        "backs off instead of failing the batch. " * 3
        for i in range(10)
    )
    return r


def _huge_verifiers_task():
    """A task whose Evidence table alone is large: `verifiers_pin()` joins
    EVERY failed verifier id into the row's own VISIBLE cell (never
    folded — only the per-rule breakdown below the row is), which is a
    realistic way a body's non-`## Changes` content grows large on its own.
    Combined with an ordinary-sized `## Changes` report (see
    `_oversized_result`), the two TOGETHER exceed 6,000 visible chars even
    though NEITHER is individually pathological."""
    t = _task()
    head_sha = _Commit.sha
    t.context["verifier_results"] = {
        head_sha: [
            _verifier_dict(verifier_id=f"rule-check-{i:04d}", passed=False)
            for i in range(280)
        ]
    }
    return t


def test_the_body_budget_trims_the_changes_section_when_oversized(store, tmp_path):
    """Review finding #8 (promoted from minor): the 6,000-visible-char
    budget is ENFORCED, not merely asserted by a test against a fixture that
    happens to be small. An oversized body — here, a large Evidence table
    PLUS an ordinary-sized `## Changes` report, together over budget — must
    be brought back under it by trimming `## Changes` further than its own
    ordinary 1,500-char fold, with an explicit marker — and Evidence,
    criteria and the verification pointer must survive intact."""
    orch = _orch(store, tmp_path)
    task = _huge_verifiers_task()
    body = orch._pr_body(task, _Commit(), _oversized_result(),
                         test_evidence={"ran": True, "ok": True, "passed": 5,
                                        "failed": 0, "errors": 0},
                         receipts=_receipts())
    assert visible_chars(body) <= 6000, visible_chars(body)
    assert "(trimmed further to keep the PR body under its size budget)" in body
    # Evidence (including the huge Verifiers row), criteria and the pointer
    # must be untouched by the trim — only `## Changes` may shrink.
    assert "| Independent review | ✅ **PASSED** — 2 rounds |" in body
    assert "280 of 280 failed" in body, "the Verifiers row itself was touched"
    for c in task.acceptance_criteria:
        assert c in body
    assert "Full verification log:" in body


def test_the_body_budget_never_trims_the_standard_fixture(store, tmp_path):
    """The other direction: an ordinary, small report must NOT trigger the
    extra trim — the marker (and the further-reduced cap) must appear ONLY
    when the body genuinely would have exceeded the budget."""
    orch = _orch(store, tmp_path)
    task = _task()
    body = orch._pr_body(task, _Commit(), _Result(),
                         test_evidence={"ran": True, "ok": True, "passed": 5,
                                        "failed": 0, "errors": 0},
                         receipts=_receipts())
    assert "(trimmed further to keep the PR body under its size budget)" not in body
    assert visible_chars(body) <= 6000, visible_chars(body)


# ══════ #23: the body reads decisive-first and every verification line expands ══ #


def test_verification_digest_folds_one_details_per_kind():
    """One `<details>` per receipt kind, in `KINDS` order: the summary names
    the kind and the LAST command of that kind (with the run count when
    there were several); expanding it shows that command's captured output
    fenced. Earlier runs of the kind stay in the full log only."""
    receipts = [_MID_WORK_RECEIPT, _FINAL_RECEIPT, {
        "command": "uv run ruff check src", "output_excerpt": "All checks passed!",
        "kind": "lint", "truncated": False, "output_bytes": 18}]
    section = Orchestrator._verification_section(receipts, task_id="deadbeef")
    assert section.count("<details>") == 2
    tests_at = section.index("<b>Tests</b> (2 runs, last shown) — <code>uv run pytest tests/test_webhook_retry.py -q</code>")
    lint_at = section.index("<b>Lint</b> — <code>uv run ruff check src</code>")
    assert tests_at < lint_at
    assert "```\n5 passed in 1.02s\n```" in section
    assert "```\nAll checks passed!\n```" in section
    assert "2 failed, 3 passed" not in section
    assert section.count("commands recorded") == 1
    assert "3 commands recorded" in section
    assert "**How I verified this** comment" in section


def test_verification_summary_escapes_html_in_the_command():
    """The command is model-chosen text rendered inside `<summary>`: a
    `</summary>` in it must not close the element early."""
    receipts = [{"command": "echo </summary><h1>done</h1>", "output_excerpt": "",
                 "kind": "test", "truncated": False, "output_bytes": 0}]
    section = Orchestrator._verification_section(receipts, task_id="deadbeef")
    assert "</summary><h1>" not in section
    assert "&lt;/summary&gt;&lt;h1&gt;done&lt;/h1&gt;" in section
    assert "_nothing was captured on stdout or stderr for this command._" in section


def test_verdict_line_precedes_the_evidence_table(store, tmp_path):
    """The two facts a human reads first — the reviewer's verdict and the
    test run — open the body as one quoted line, ahead of `## Evidence`,
    worded differently from the table cells so nothing is rendered twice."""
    orch = _orch(store, tmp_path)
    task = _task()
    test_evidence = {"ran": True, "ok": True, "passed": 5, "failed": 0, "errors": 0}
    body = orch._pr_body(task, _Commit(), _Result(),
                         test_evidence=test_evidence, receipts=_receipts())
    line = "> **Review passed** (2 rounds) · **Tests passed** (5 passed, 0 failed)"
    assert body.count(line) == 1, body
    assert body.index(line) < body.index("## Evidence")
    assert body.count("**PASSED** — 2 rounds") == 1


def test_verdict_line_names_a_failed_review_and_a_failed_run(store, tmp_path):
    orch = _orch(store, tmp_path)
    task = _task()
    task.context["review_history"][-1] = {
        "round": 2, "sha": "a" * 40, "passed": False, "blocking": ["still untested"]}
    test_evidence = {"ran": True, "ok": False, "passed": 3, "failed": 2, "errors": 1}
    body = orch._pr_body(task, _Commit(), _Result(),
                         test_evidence=test_evidence, receipts=_receipts())
    assert ("> **Review not passed** (2 rounds) · "
            "**Tests failed** (3 passed, 2 failed, 1 errors)") in body


def test_verdict_line_follows_the_runner_verdict_not_the_counts():
    """`ok` is the runner's own verdict (`rc == 0`): `pytest && mypy` with
    mypy failing is ok=False with 0 failed, and the headline must not read
    as a pass when the table's row says FAIL."""
    ev = PrEvidence(tests={"ran": True, "ok": False, "passed": 5, "failed": 0, "errors": 0})
    assert ev.headline() == "> **Tests failed** (5 passed, 0 failed)\n\n"
    ev = PrEvidence(tests={"ran": True, "ok": False, "passed": 0, "failed": 0, "errors": 0})
    assert ev.headline() == "> **Tests failed**\n\n"
    ev = PrEvidence(tests={"ran": False})
    assert ev.headline() == "> **Tests not run**\n\n"
    ev = PrEvidence(tests={"layers": ["unit: ✅ PASS — 5 passed, 0 failed, 0 errors"]})
    assert ev.headline() == ""


def test_verification_summary_folds_a_multi_line_command_onto_one_line():
    """A heredoc-then-test command keeps its newlines in the receipt; a
    blank line inside `<summary>` would end the HTML block and leave the
    fold unclosed on GitHub. The summary shows it on one line, invisible
    and direction-changing characters dropped, as `md_inline_code` does."""
    receipts = [{"command": "cat <<'EOF' > x.py\nprint(1)\n\nEOF\nuv run pytest -q \u202e",
                 "output_excerpt": "1 passed", "kind": "test",
                 "truncated": False, "output_bytes": 8}]
    section = Orchestrator._verification_section(receipts, task_id="deadbeef")
    summary = section[section.index("<summary>"):section.index("</summary>")]
    assert "\n" not in summary
    assert "\u202e" not in summary
    assert "<code>cat &lt;&lt;'EOF' &gt; x.py print(1)  EOF uv run pytest -q</code>" in summary


def test_no_verdict_line_without_a_verdict(store, tmp_path):
    """No review, no test run: nothing to headline, so no quoted line."""
    orch = _orch(store, tmp_path)
    task = Task.new("t", repo_path="/r")
    body = orch._pr_body(task, _Commit(), _Result())
    assert "> **" not in body
