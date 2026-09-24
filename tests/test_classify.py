"""WS-A: task-type classification routes the four reference shapes correctly."""

import json
from pathlib import Path

import pytest

from no_human.core.task import Task
from no_human.intake.classify import (
    TaskKind,
    classify,
    classify_kind,
    kind_criteria_mismatch,
    parse_agent_verdict,
    find_test_bearing_signal,
)
from no_human.intake.github_issues import GitHubAdapter

REPO_ROOT = Path(__file__).resolve().parents[1]
ISSUE_428_FIXTURE = REPO_ROOT / "testdata" / "github_issue_428.json"


def _task(title, *, description=None, criteria=None, source="freeform", external_id=None):
    t = Task.new(title, source=source, description=description, external_id=external_id)
    t.acceptance_criteria = list(criteria or [])
    return t


def _load_issue_428() -> Task:
    """Load the offline fixture for no-human-ai/no_human#428 and run it
    through the real GitHub intake path (body -> description AND extracted
    acceptance criteria), the same way `nh intake` would."""
    raw = json.loads(ISSUE_428_FIXTURE.read_text(encoding="utf-8"))
    raw["_ref"] = {"host": "github.com", "owner": "no-human-ai", "repo": "no_human"}
    return GitHubAdapter().normalize(raw)


# The four shapes named in the WS-A DoD. ----------------------------------- #

@pytest.mark.parametrize("title,expected", [
    ("Fix the NullPointerException when the user list is empty", TaskKind.BUGFIX),
    ("OAuth token refresh is broken and crashes the worker", TaskKind.BUGFIX),
    ("Make the failing CI build green again", TaskKind.CI_FIX),
    ("The monorepo build is red on PR-042", TaskKind.CI_FIX),
    ("Add a missing test for the date parser", TaskKind.TEST_GAP),
    ("Increase coverage: the retry helper is untested", TaskKind.TEST_GAP),
    ("Link the ATCs and record the automation status for PROJ-42", TaskKind.TRACEABILITY),
    ("Link the ATCs for PROJ-42", TaskKind.TRACEABILITY),
    ("This defect has no test automation — add traceability", TaskKind.TRACEABILITY),
    ("Add a dark-mode toggle to the settings page", TaskKind.FEATURE),
])
def test_reference_shapes(title, expected):
    assert classify_kind(_task(title)).kind == expected


def test_ci_fix_beats_bugfix_when_both_signals_present():
    # "fix the failing build" has both a bug-ish "fix" and a ci signal; ci wins
    # because the rule order is most-specific-first.
    v = classify_kind(_task("Fix the failing build for the release branch"))
    assert v.kind == TaskKind.CI_FIX
    assert "ci_fix" in v.reason


def test_incidental_bug_words_in_description_do_not_route_to_bugfix():
    # A greenfield feature spec that discusses bugs as prior art and negates a
    # worked example. Every one of these substrings used to match the broad
    # bugfix rule and route the task to the "reproduce the defect first" pipeline.
    v = classify_kind(_task(
        "Per-PR CI_GATE Integration Test Pipeline",
        description=(
            "On a timeout/infra failure post a clear error explanation "
            "(not a raw stack trace).\n"
            'Two review findings from that run were WRONG - do not "fix" these.\n'
            "The agent faithfully copied the repo's own unpaginated "
            "comment-listing bug (defect 1 above) because the spec said so."
        ),
        criteria=["New Jenkinsfile stage gated on env.CHANGE_ID",
                  "Kill-switch via env.CI_GATE_INTEGRATION_TESTS_ENABLED"],
    ))
    assert v.kind == TaskKind.FEATURE
    assert v.source == "default"


def test_bug_word_in_title_still_routes_to_bugfix():
    # The intent zone keeps the full, broad pattern.
    assert classify_kind(_task("Fix the pagination bug")).kind == TaskKind.BUGFIX
    assert classify_kind(_task("Checkout", criteria=["The crash must not recur"])).kind \
        == TaskKind.BUGFIX


@pytest.mark.parametrize("description", [
    "Clicking pay throws a NullPointerException before the charge is made.",
    "The worker crashes when the user list is empty.",
    "This is a regression introduced by the retry rewrite.",
    "The totals are incorrect after a partial refund.",
])
def test_high_precision_bug_phrases_in_description_still_route_to_bugfix(description):
    v = classify_kind(_task("Checkout", description=description))
    assert v.kind == TaskKind.BUGFIX
    assert "description" in v.reason


def test_prose_zone_still_routes_the_specific_kinds():
    # Only the broad bugfix pattern is distrusted in prose; the specific rules
    # keep their reach.
    assert classify_kind(_task("Nightly", description="the CI build is red")).kind \
        == TaskKind.CI_FIX
    assert classify_kind(_task("Parser", description="add a missing test for it")).kind \
        == TaskKind.TEST_GAP


def test_override_wins():
    v = classify_kind(_task("Add a dark-mode toggle"), override="bugfix")
    assert v.kind == TaskKind.BUGFIX
    assert v.source == "override"


def test_override_coerces_aliases():
    assert classify_kind(_task("x"), override="ci").kind == TaskKind.CI_FIX
    assert classify_kind(_task("x"), override="atc").kind == TaskKind.TRACEABILITY
    assert classify_kind(_task("x"), override="nonsense").kind == TaskKind.FEATURE


def test_evidence_is_cited_never_a_score():
    v = classify_kind(_task("Add a missing test"))
    assert v.reason and isinstance(v.reason, str)
    assert not any(ch.isdigit() for ch in v.reason)  # no numeric self-score


def test_task_kind_round_trips_through_row():
    t = _task("Fix bug")
    t.kind = "bugfix"
    assert Task.from_row(t.to_row()).kind == "bugfix"


def test_from_row_defaults_kind_when_absent():
    row = _task("legacy").to_row()
    del row["kind"]
    assert Task.from_row(row).kind == "feature"


# Agent fallback ----------------------------------------------------------- #

def test_parse_agent_verdict():
    text = 'reasoning...\n```json\n{"kind": "ci_fix", "reason": "build is red"}\n```'
    v = parse_agent_verdict(text)
    assert v.kind == TaskKind.CI_FIX
    assert v.source == "agent"


def test_parse_agent_verdict_none_on_garbage():
    assert parse_agent_verdict("no json here") is None


class _FakeResult:
    def __init__(self, text):
        self.final_text = text


class _FakeBackend:
    def __init__(self, text):
        self.text = text
        self.calls = 0

    async def run(self, prompt, **kw):
        self.calls += 1
        return _FakeResult(self.text)


@pytest.mark.asyncio
async def test_agent_fallback_used_only_for_ambiguous_freeform():
    backend = _FakeBackend('```json\n{"kind": "bugfix", "reason": "it crashes"}\n```')
    # Ambiguous freeform (no rule match, default) → agent consulted.
    v = await classify(_task("do the needful with the thing"), backend=backend)
    assert v.kind == TaskKind.BUGFIX
    assert backend.calls == 1


@pytest.mark.asyncio
async def test_agent_not_called_when_rule_matches():
    backend = _FakeBackend('```json\n{"kind": "feature", "reason": "x"}\n```')
    v = await classify(_task("Add a missing test for parser"), backend=backend)
    assert v.kind == TaskKind.TEST_GAP
    assert backend.calls == 0  # deterministic rule already resolved it


@pytest.mark.asyncio
async def test_agent_failure_falls_back_to_deterministic():
    class _Boom:
        async def run(self, *a, **k):
            raise RuntimeError("sdk down")

    v = await classify(_task("ambiguous freeform blah"), backend=_Boom())
    assert v.kind == TaskKind.FEATURE  # deterministic default survives


# ── verify/validate/audit → investigation (read-only), not feature (the CI_GATE
#    mis-scope: a "validate X" request was built as a code change) ──────────────

def _t(title, description=""):
    from types import SimpleNamespace
    return SimpleNamespace(title=title, description=description,
                           acceptance_criteria=[], requirements=[],
                           external_id="", source="freeform")


def test_verification_tasks_route_to_investigation_not_feature():
    from no_human.intake.classify import classify_kind, TaskKind
    for title in [
        "Verify CI_GATE integration-test namespaces are torn down",
        "validate that every namespace is destroyed",
        "Audit whether all resources are cleaned up",
        "Verify the config values are correct",
    ]:
        assert classify_kind(_t(title)).kind is TaskKind.INVESTIGATION, title


def test_verify_validate_audit_NOUNS_stay_feature():
    """Feature-nouns must NOT be mis-read as verification: 'email verification',
    'data validation', 'audit log', 'validate the input' are all implement work."""
    from no_human.intake.classify import classify_kind, TaskKind
    for title in [
        "Add email verification to signup",
        "Implement data validation for the form",
        "validate the input before submit",
        "Add an audit log for admin actions",
        "verify the signature",
    ]:
        assert classify_kind(_t(title)).kind is TaskKind.FEATURE, title


# ── design/architecture DOCUMENT requests → design_doc (report, not code) ────

def test_design_doc_requests_route_to_design_doc():
    from no_human.intake.classify import classify_kind, TaskKind
    for title in [
        "Write a design doc for the retention pipeline",
        "Architecture proposal for multi-tenant ingestion",
        "Draft an RFC for the new export API",
        "Produce an architecture document for the alerting stack",
        "design document: session-memory recall",
    ]:
        assert classify_kind(_t(title)).kind is TaskKind.DESIGN_DOC, title


def test_design_VERBS_stay_feature():
    """'Design/redesign X' without document intent is implement work — routing
    it to a report pipeline would return prose where the user wanted code."""
    from no_human.intake.classify import classify_kind, TaskKind
    for title in [
        "Design the API endpoint for exports",
        "Redesign the board UI",
        "Add a well-designed retry helper",
    ]:
        assert classify_kind(_t(title)).kind is not TaskKind.DESIGN_DOC, title


def test_design_doc_coerce_aliases():
    from no_human.intake.classify import TaskKind
    assert TaskKind.coerce("design_doc") is TaskKind.DESIGN_DOC
    assert TaskKind.coerce("architecture") is TaskKind.DESIGN_DOC
    assert TaskKind.coerce("rfc") is TaskKind.DESIGN_DOC
    assert TaskKind.coerce("adr") is TaskKind.DESIGN_DOC


def test_design_doc_outranks_incidental_ci_and_traceability_keywords():
    """'Write a design doc for the failing CI pipeline' is a DOCUMENT about CI,
    not a ci_fix — the explicit document noun wins over incidental keywords."""
    from no_human.intake.classify import classify_kind, TaskKind
    for title in [
        "Write a design doc for the failing CI pipeline",
        "RFC for test automation strategy",
        "Architecture proposal for the broken build recovery flow",
        "RFC: session-memory recall design",
        "ADR: adopt SQLite for storage",
    ]:
        assert classify_kind(_t(title)).kind is TaskKind.DESIGN_DOC, title


# ── report-shaped intents (v6 taxonomy 2026-07-16): explicit code-review asks,
#    question-form asks, and plan-document asks must not fall to the feature
#    pipeline, where they park asking "is my answer the deliverable?" or grind
#    the token budget at implement→test→PR. ──────────────────────────────────

def test_refd_review_asks_route_to_code_review():
    """A review ask that NAMES a PR/MR routes to the code_review pipeline
    (which needs the ref: _run_code_review hard-fails without one)."""
    from no_human.intake.classify import classify_kind, TaskKind
    for title in [
        "review this PR https://code.example.com/dev/acme-test/pull/7002",
        "do an in-depth code review of https://code.example.com/dev/acme-test/pull/7002",
        "review https://gitlab.example/g/r/-/merge_requests/45 please",
    ]:
        assert classify_kind(_t(title)).kind is TaskKind.CODE_REVIEW, title


def test_bare_review_asks_route_to_investigation():
    """A review ask WITHOUT a PR/MR ref is a repo-level review — the
    code_review pipeline would fast-fail on the missing ref, so it routes to
    investigation (cited findings report)."""
    from no_human.intake.classify import classify_kind, TaskKind
    for title in [
        "use every rule and skill to do a thorough code review as a senior engineer",
        "in-depth code review of the analytics-export service",
        "peer review of the retention change",
    ]:
        assert classify_kind(_t(title)).kind is TaskKind.INVESTIGATION, title


def test_review_plus_implement_stays_out_of_code_review():
    """A review ask whose real terminal is code must keep an implement pipeline —
    a read-only review would return prose where the user wanted a fix."""
    from no_human.intake.classify import classify_kind, TaskKind
    for title in [
        "Review the changes and fix the issues you find",
        "review this PR and address the comments",
        "address the review comments on my PR",
        "apply the code review feedback",
    ]:
        assert classify_kind(_t(title)).kind is not TaskKind.CODE_REVIEW, title


def test_question_form_asks_route_to_investigation():
    from no_human.intake.classify import classify_kind, TaskKind
    for title in [
        "what are the allowed columns in the report endpoint",
        "why is the export endpoint slow on large ranges",
        'read the code and tell me when the uploader returns: "Import completed"',
        "figure out why alerting issues are not coming through",
        "explain how the retention pipeline decides what to delete",
        "I have this report, what should I do?",
    ]:
        assert classify_kind(_t(title)).kind is TaskKind.INVESTIGATION, title


def test_preposition_fronted_questions_route_to_investigation():
    """v7 live miss (ns-1643229f), a preposition-fronted environment question:
    it classified FEATURE because the interrogative rule only caught a LEADING
    wh-word. A fronted preposition + wh-word is the same question form.

    The probes below are written fresh rather than copied from the spec: the
    spec corpus is manifest-dropped because it is verbatim real-conversation
    text, and a fixture that reproduces one puts it back on export surface.
    What the rule keys on is the SHAPE, so a fresh sentence of the same shape
    tests exactly as much."""
    from no_human.intake.classify import classify_kind, TaskKind
    for title in [
        "in which environments does the service use a cache and which in prod",
        "on which cluster does the retention job run",
        "for what date range is the export cache valid",
    ]:
        assert classify_kind(_t(title)).kind is TaskKind.INVESTIGATION, title


def test_preposition_fronted_wh_is_not_stolen_from_implement_work():
    from no_human.intake.classify import classify_kind, TaskKind
    for title in [
        # possessive wh ("What's New") is a UI label, not a question
        "In What's New panel, add a link to the release notes",
        # diagnose-AND-FIX keeps the implement pipeline
        "in what envs does the export break, then fix the config",
    ]:
        assert classify_kind(_t(title)).kind is not TaskKind.INVESTIGATION, title


def test_leading_rhetorical_what_is_not_a_question():
    """'what? you need to work on X' is an exclamation prefacing implement
    work, not a question — only an interrogative CLAUSE routes to a report."""
    from no_human.intake.classify import classify_kind, TaskKind
    v = classify_kind(_t(
        "what? you need to work on the earlier request I sent: add the retry"))
    assert v.kind is not TaskKind.INVESTIGATION


def test_conditional_when_clauses_stay_feature():
    """'When X happens, do Y' is a spec, not a question."""
    from no_human.intake.classify import classify_kind, TaskKind
    for title in [
        "When the user clicks save, persist the draft",
        "when the export completes, send a notification",
        "Show a spinner when the board is loading",
    ]:
        assert classify_kind(_t(title)).kind is TaskKind.FEATURE, title


def test_plan_document_asks_route_to_design_doc():
    from no_human.intake.classify import classify_kind, TaskKind
    for title in [
        "we need to onboard a new team to the alerting service. Create a plan to do this",
        "Write a plan for migrating the events table",
        "draft a rollout plan for the new gate",
    ]:
        assert classify_kind(_t(title)).kind is TaskKind.DESIGN_DOC, title


def test_plan_plus_implement_stays_out_of_design_doc():
    from no_human.intake.classify import classify_kind, TaskKind
    for title in [
        "Create a plan and implement it",
        "write a plan then execute it end to end",
    ]:
        assert classify_kind(_t(title)).kind is not TaskKind.DESIGN_DOC, title


def test_report_shaped_rules_do_not_fire_from_prose():
    """The new intent rules are TITLE/criteria-zone only: a question or review
    mention buried in a long feature description must not steal the task from
    the implement pipeline (same precision posture as the prose bugfix swap)."""
    from no_human.intake.classify import classify_kind, TaskKind
    v = classify_kind(_t(
        "Add per-column export filters",
        description=(
            "Background: users keep asking what are the allowed columns in the "
            "export endpoint. A code review last sprint flagged the validator. "
            "Create a plan section in the PR description."
        ),
    ))
    assert v.kind is TaskKind.FEATURE


def test_explicitly_read_only_asks_route_to_investigation():
    from no_human.intake.classify import classify_kind, TaskKind
    v = classify_kind(_t(
        "Fetch the diff for PR https://x/y/pull/1 and output the complete diff",
        description="Fetch the diff and output it. Do NOT make any changes. Read-only.",
    ))
    assert v.kind is TaskKind.INVESTIGATION


def test_scoped_no_change_constraints_do_not_steal_implement_work():
    from no_human.intake.classify import classify_kind, TaskKind
    for title, desc in [
        ("Fix the crash in the export worker", "do not make changes to the tests"),
        ("Make the endpoint read-only", ""),
        ("Add a read-only mode toggle to settings", ""),
    ]:
        assert classify_kind(_t(title, desc)).kind is not TaskKind.INVESTIGATION, title


# ── fresh-context review findings (2026-07-16): precision hardening ─────────

def test_criteria_zone_cannot_weld_into_a_code_review_match():
    """Review D1: without re.S the proximity window must not cross the \\n
    between title and criteria — a backport task citing a PR is implement
    work even when a criterion mentions review."""
    from no_human.intake.classify import classify_kind, TaskKind
    t = _t("Backport the throttling change from PR #4821")
    t.acceptance_criteria = ["the change compiles", "code passes review"]
    assert classify_kind(t).kind is not TaskKind.CODE_REVIEW
    t2 = _t("review this PR https://x/y/pull/9")
    t2.acceptance_criteria = ["each finding cites file:line and suggests how to fix"]
    assert classify_kind(t2).kind is TaskKind.CODE_REVIEW  # lookahead must not cross either


def test_refless_review_tokens_downgrade_to_investigation():
    """Review D2: a review ask whose 'ref' the pipeline's own parser cannot
    parse (hostless 'PR 7002', 'merge request !45') must NOT route to the
    hard-failing code_review pipeline."""
    from no_human.intake.classify import classify_kind, TaskKind
    for title in [
        "Fetch the diff for PR 7002 and output a review of it",
        "review the merge request !45 on gitlab",
    ]:
        assert classify_kind(_t(title)).kind is TaskKind.INVESTIGATION, title


def test_diagnose_and_fix_stays_implement_work():
    """Review D3: a question clause followed by an implement ask is a fix
    task, not a report task."""
    from no_human.intake.classify import classify_kind, TaskKind
    for title in [
        "figure out why the export endpoint 500s on empty ranges and fix it",
        "check if the cache is stale and invalidate it",
        "understand why the retry loops and then patch the backoff",
    ]:
        assert classify_kind(_t(title)).kind is not TaskKind.INVESTIGATION, title


def test_scoped_no_change_prepositions_beyond_to():
    """Review D4: 'until/outside/…' scope guards, both zones."""
    from no_human.intake.classify import classify_kind, TaskKind
    t = _t("Fix the flaky retry test")
    t.acceptance_criteria = ["do not make changes until you reproduce the failure"]
    assert classify_kind(t).kind is not TaskKind.INVESTIGATION
    v = classify_kind(_t("Add config-only deploy support",
                         "Ship it. No code changes outside the deploy scripts."))
    assert v.kind is not TaskKind.INVESTIGATION


def test_plan_to_x_then_implement_stays_implement():
    """Review D5: the plan-noun lookahead can't see past 'to' — the trailing
    guard must."""
    from no_human.intake.classify import classify_kind, TaskKind
    v = classify_kind(_t(
        "write a plan to fix the flaky tests and then implement the fixes"))
    assert v.kind is not TaskKind.DESIGN_DOC


def test_whats_new_and_why_first_titles_are_not_questions():
    """Review D6: apostrophe/hyphen after a leading interrogative word."""
    from no_human.intake.classify import classify_kind, TaskKind
    for title in [
        "What's New panel: add release notes feed",
        "Why-first refactor of the onboarding docs",
    ]:
        assert classify_kind(_t(title)).kind is not TaskKind.INVESTIGATION, title


def test_review_before_merge_is_still_a_review_ask():
    """Review D7: 'before I merge it' must not demote a pure review ask —
    the agent never merges anyway."""
    from no_human.intake.classify import classify_kind, TaskKind
    v = classify_kind(_t("review this PR https://x/y/pull/9 before I merge it"))
    assert v.kind is TaskKind.CODE_REVIEW


def test_discourse_openers_and_spliced_imperatives_are_not_questions():
    """Review findings on the preposition rule (PR #102): 'From what I can
    tell…' is a discourse opener, and a comma/dash-spliced imperative after a
    wh-clause is implement work — neither may terminate as a zero-change
    report."""
    from no_human.intake.classify import classify_kind, TaskKind
    # HIGH: bugfix theft via the 'from' entry point
    v = classify_kind(_t("From what I can tell the cache invalidation is broken, fix it"))
    assert v.kind is TaskKind.BUGFIX, v
    v = classify_kind(_t("From what I can see the retention job never runs, fix the cron"))
    assert v.kind is TaskKind.BUGFIX, v
    # MEDIUM: comma/dash-spliced imperatives keep the implement pipeline
    for title in [
        "For which users should we enable the flag, implement accordingly",
        "In which module add the retry helper",
        "At what point the queue overflows, add backpressure",
        "Under what conditions the retry fires, implement exponential backoff",
        "Of what use is the legacy adapter — remove it",
        "For what it's worth, refactor the parser to use pathlib",
    ]:
        v = classify_kind(_t(title))
        assert v.kind is not TaskKind.INVESTIGATION, (title, v)


def test_preposition_questions_still_route_after_the_tightening():
    """The tightened rule must keep the genuine fronted questions (aux verb
    continuation, no spliced imperative)."""
    from no_human.intake.classify import classify_kind, TaskKind
    for title in [
        "in which environments does the service use a cache and which in prod",
        "on which cluster does the retention job run",
        "for what date range is the export cache valid",
        "under what conditions does the dedupe cache evict entries",
    ]:
        assert classify_kind(_t(title)).kind is TaskKind.INVESTIGATION, title


def test_verb_as_noun_after_punctuation_stays_a_question():
    """Re-review of PR #102: the splice guard must not fire on verb-as-NOUN
    ('update seems stuck', 'patch notes', 'change management') — a bare
    comma/dash carries no imperative signal, so the splice guard is scoped to
    the preposition alternation only; these route as master routed them."""
    from no_human.intake.classify import classify_kind, TaskKind
    for title in [
        "why is the export slow, update seems stuck",
        "how does the scheduler work - update loop or event-driven?",
        "check why deploys are slow; update to the new runner didn't help",
        "what changed in the last release, patch notes are unclear",
        "explain how rollbacks work, change management asked",
        "how do feature flags work here - enable path specifically",
        "what should we do about the flaky test, disable is not an option",
        "what should I do, remove or keep the legacy adapter?",
    ]:
        assert classify_kind(_t(title)).kind is TaskKind.INVESTIGATION, title


# ── kind/criteria consistency guard (defect 204f2177) ─────────────────────── #
# A report-only kind (design_doc/investigation) completes without ever
# shipping code — so criteria demanding a test/CLI/endpoint/shipped artifact
# can never be satisfied by it. `kind_criteria_mismatch` names that clash so
# intake can refuse it and completion can refuse to silently finish it.

def test_design_doc_with_a_cli_flag_criterion_is_flagged_red_first():
    """Red-first repro of the live defect shape: design_doc kind + a criterion
    naming a CLI flag and tests must be caught, not silently accepted."""
    reason = kind_criteria_mismatch(
        "design_doc",
        [
            "nh approve --landed marks the PR landed",
            "a red-first test proves the flag's behaviour before the fix",
            "a control test proves the flag is a no-op without --landed",
        ],
    )
    assert reason is not None
    assert "design_doc" in reason


def test_design_doc_with_only_prose_criteria_completes_report_only():
    """Control: a genuine design_doc ticket with prose-only criteria (no
    test-bearing signal) is NOT flagged — it must still complete report-only."""
    reason = kind_criteria_mismatch(
        "design_doc",
        ["document covers options and a recommendation"],
    )
    assert reason is None


def test_investigation_with_a_test_criterion_is_flagged():
    reason = kind_criteria_mismatch(
        "investigation",
        ["add a test that reproduces the root cause"],
    )
    assert reason is not None
    assert "investigation" in reason


def test_feature_kind_is_never_flagged_regardless_of_criteria():
    """The guard is scoped to report-only kinds — a feature/bugfix task
    demanding tests is completely normal and must not be flagged."""
    reason = kind_criteria_mismatch(
        "feature",
        ["add a red-first test", "ship a new CLI endpoint"],
    )
    assert reason is None


def test_kind_criteria_mismatch_accepts_taskkind_enum_too():
    assert kind_criteria_mismatch(
        TaskKind.DESIGN_DOC, ["shipped as a CLI flag"]) is not None
    assert kind_criteria_mismatch(
        TaskKind.DESIGN_DOC, ["prose only, no signal words"]) is None


@pytest.mark.parametrize("word", [
    "test", "red-first", "test-bearing", "lands", "CLI", "endpoint",
    "shipped", "artifact",
])
def test_each_documented_keyword_is_detected(word):
    assert find_test_bearing_signal([f"the change {word} correctly"]) is not None


def test_no_false_positive_on_substrings():
    """`\\btest\\b` must not fire on 'testable', 'contest', etc. — a keyword
    embedded in a larger word is not the signal word itself."""
    assert find_test_bearing_signal(["the criteria are testable and clear"]) is None
    assert find_test_bearing_signal(["do not contest the outcome"]) is None


# Position-aware masking (defect: issue #428 quoted "no tests ran" read as a
# test_gap signal). --------------------------------------------------------- #

def test_issue_428_quoted_no_tests_does_not_route_to_test_gap():
    """Regression fixture: no-human-ai/no_human#428 is a repro-gate bugfix.
    Its only occurrence of the test_gap signal is inside a quoted symptom —
    `instead of pytest's "no tests ran"` — in an acceptance criterion, not a
    statement that tests are missing. On main this classifies test_gap; the
    fix must not classify it that way."""
    task = _load_issue_428()
    verdict = classify_kind(task)
    assert verdict.kind is not TaskKind.TEST_GAP
    assert "no tests" not in verdict.reason


@pytest.mark.parametrize("wrapped", [
    "`no tests`",
    "```\nno tests ran\n```",
    "> no tests ran",
    '"no tests ran"',
    "'no tests ran'",
])
def test_signal_inside_code_span_fence_blockquote_or_quote_is_not_a_signal(wrapped):
    task = _task(
        "Add a dark-mode toggle",
        description=f"Running the old script prints:\n{wrapped}\nplease add it anyway.",
    )
    verdict = classify_kind(task)
    assert verdict.kind is TaskKind.FEATURE
    assert verdict.source == "default"


def test_same_signal_in_bare_prose_still_classifies():
    """Positive control (same test body, per spec): remove the delimiters
    around the identical words and the signal fires again — proving the fix
    narrows *position*, not the word list, and does so kind-wide, not just
    for test_gap."""
    test_gap_task = _task("Parser", description="the retry helper has no tests")
    assert classify_kind(test_gap_task).kind is TaskKind.TEST_GAP

    test_gap_task2 = _task("Parser", description="add a missing test for it")
    assert classify_kind(test_gap_task2).kind is TaskKind.TEST_GAP

    ci_fix_task = _task("Nightly", description="the CI build is red")
    assert classify_kind(ci_fix_task).kind is TaskKind.CI_FIX


def test_apostrophes_do_not_mask_surrounding_prose():
    """The single-quote mask is word-boundary-guarded: without that guard,
    the apostrophe in "pytest's" plus a later stray apostrophe could swallow
    everything between them as one giant 'quoted string', blinding the
    classifier to real prose in between."""
    task = _task(
        "Parser", description="pytest's runner has no tests for the parser")
    verdict = classify_kind(task)
    assert verdict.kind is TaskKind.TEST_GAP


def test_stray_inch_marks_do_not_mask_surrounding_prose():
    """The double-quote mask is word-boundary-guarded like the single-quote
    one (see test_apostrophes_do_not_mask_surrounding_prose above): two
    unrelated straight quotes used as inch marks sit directly against
    digits, so they must not pair up across the sentence and swallow the
    real signal words between them."""
    task = _task(
        "Enclosure",
        description=(
            'the panel is 24" wide, the retry helper has no tests, '
            'and the frame is 30" tall'))
    assert classify_kind(task).kind is TaskKind.TEST_GAP

    # CONTROL: identical sentence with the two stray inch marks removed —
    # must classify exactly the same way, proving the guard narrows
    # position (unpaired quote vs. real quote) and not content.
    control_task = _task(
        "Enclosure",
        description=(
            'the panel is 24 wide, the retry helper has no tests, '
            'and the frame is 30 tall'))
    assert classify_kind(control_task).kind is TaskKind.TEST_GAP


def test_stray_backtick_does_not_mask_a_later_line():
    """An inline code span must not cross a newline: without that
    restriction, a single unterminated backtick pairs with the NEXT real
    backtick — even lines later — and blanks everything in between,
    including a real signal in the author's own prose."""
    task = _task(
        "Pipeline",
        description=(
            "opened with `nh task add`\n"
            "note the stray ` in this line\n"
            "the CI build is red on every PR and `x` ends it"))
    assert classify_kind(task).kind is TaskKind.CI_FIX

    # CONTROL: identical text with the stray backtick removed — must
    # classify exactly the same way (the properly paired `x` span on the
    # last line still masks normally; only the stray delimiter is gone).
    control_task = _task(
        "Pipeline",
        description=(
            "opened with `nh task add`\n"
            "note the stray in this line\n"
            "the CI build is red on every PR and `x` ends it"))
    assert classify_kind(control_task).kind is TaskKind.CI_FIX


def test_override_beats_even_a_masked_body():
    task = _load_issue_428()
    verdict = classify_kind(task, override="bugfix")
    assert verdict.kind is TaskKind.BUGFIX
    assert verdict.source == "override"


def test_reason_names_matched_text_and_zone():
    prose_verdict = classify_kind(_task("Parser", description="add a missing test for it"))
    assert "test" in prose_verdict.reason
    assert "description" in prose_verdict.reason
    assert not any(ch.isdigit() for ch in prose_verdict.reason)

    intent_verdict = classify_kind(_task("Add a missing test"))
    assert "test" in intent_verdict.reason
    assert "title/criteria" in intent_verdict.reason
    assert not any(ch.isdigit() for ch in intent_verdict.reason)
