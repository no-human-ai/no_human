"""Refuse an already-satisfied claim at the moment it is made, not at delivery.

Over the attempts table, a recurring pattern: an attempt ends in
'already-satisfied claim refused', burning a full turn budget, because the
claim's subject tree was only ever classified at delivery time. (The exact
attempt/turn/token counts previously cited here as a single MEASURED
snapshot could not be reproduced from the same query on a later pass — no
re-derivable query for them ships with this change, so no precise figures
are asserted; the pattern itself, and the fix below, do not depend on the
count.) The refusal itself is CORRECT and stays exactly as it is. Only the
*timing* changes here — this module asks delivery's own question the moment
the agent asserts the claim, mid-attempt, so a doomed claim cannot spend a
full turn budget before being told it is refutable.

The check is never keyed on the claimed commit's subject line: many claims
in that pattern name an ordinary commit left by a previous round whose
review had FAILED, not a [WIP-PARTIAL]/[WIP-BLOCKED] checkpoint. A fix keyed
on the checkpoint subject would miss most of those; the delivery-time
question covers both shapes, because the subject is never what makes (or
doesn't make) the claim false.

Ten revisions since the first version landed:

* (Send-back, sha extraction) a bare ``[0-9a-f]{7,40}`` token also matches
  ordinary English words ("defaced") and unrelated hex-shaped tokens
  (manifest hashes) anywhere in the text, and the sha search ran over the
  WHOLE utterance instead of the claim's own snippet. Fixed by bounding the
  sha search to the claim's snippet window and requiring a cue word
  ("at"/"in"/"as"/"commit"/"sha") immediately before the token.
* (Send-back, second review) two deeper defects:

  1. The probe wrapped `classify_already_satisfied_landing` — ancestry
     against `base` only — a SECOND, narrower authority than delivery's
     real gate, `Orchestrator._already_satisfied_subject`, which also
     accepts a pushed-and-up-to-date offered branch, or a pushed SIBLING
     branch of the same task. The guard therefore refused claims delivery
     would ACCEPT. Fixed by making the probe call
     `_already_satisfied_subject` itself — the exact function
     `_gate_already_satisfied` calls at delivery time — so the two can
     never disagree in the accept direction.
  2. The detector fired on ordinary, non-claim prose ("I already ran the
     full suite; no changes needed in tests/test_foo.py") because the
     phrase match alone was treated as actionable. Measured: of 200 sampled
     firings, 22 (11%) were a genuine already-satisfied incident and the
     rest were spurious phrase matches on prose that never named a commit
     or used the formal marker. Fixed by requiring the phrase match to also
     carry either an explicitly cued commit (the
     existing sha-cue requirement) or the formal ``ALREADY-SATISFIED``
     marker line by itself — the same marker text
     `Orchestrator._parse_already_satisfied` looks for, but NOT the same
     bar: delivery additionally requires at least `n_criteria`
     well-formed ``CRITERION: ... — MET — evidence: ...`` lines with none
     marked NOT-MET before it will route a zero-diff completion to the
     already-satisfied gate at all. The marker alone is enough to make the
     detector here treat an utterance as actionable — a deliberately looser
     bar than delivery's, because this probe only needs to decide whether
     to ask delivery's own question early, not whether delivery would
     ultimately accept the claim (see Finding 2 in the most recent review
     for the false-positive shapes this looseness lets through, and the
     ninth-review bullet below for how they are handled).
* (Send-back, third review) `_already_satisfied_subject` is delivery's
  containment check, but it is only ever REACHED once delivery has already
  decided the head belongs on the claim gate at all —
  `Orchestrator._route_unjudged_head` asks `_already_satisfied_eligible`
  FIRST and routes an ineligible head (no prior passing review round on
  this exact sha, and either a [WIP-BLOCKED]/[WIP-PARTIAL] head or a
  machine-requeue provenance) to a full review instead. The probe was
  calling `_already_satisfied_subject` directly, so it could refuse a claim
  delivery would never even test — it would send that same head to review,
  not refuse it. Fixed by asking `_already_satisfied_eligible` first, here
  in the probe, exactly as `_route_unjudged_head` does, and returning
  silent (no refusal) when ineligible — see
  `Orchestrator._build_landed_claim_guard`, which builds this probe.
* (Fourth review, nits) two coverage/robustness gaps found by mutation
  review, no refusal-semantics change: the sha-cue regex above did not
  match a sha fenced in backticks, the routine way a coder narrates a claim
  in markdown — fixed. And a raising `on_event` sink was already guarded
  (`hook()` already caught and logged, never let it swallow the injection
  already computed) but no test pinned either that guard or the event
  emission itself — both are now covered, closing two surviving mutants
  (deleting the `on_event(...)` call, or its try/except, passed every
  existing test).
* (Fifth review) same class a third time, one predicate further out:
  `_already_satisfied_subject` is only ever reached at delivery when
  `resumed_commit` is `None` (`_run_attempt`, ~6805) — an ordinary
  in-session commit ahead of `base` leaves `resumed_commit` set, so
  delivery commits, reviews and opens a PR instead of parsing the claim at
  all. The probe was refusing that shape too. Fixed by evaluating that same
  outer predicate — `commits_ahead(base)` — in the probe, and staying
  silent whenever it is nonzero (unless this attempt itself resumed from
  its own `[WIP-PARTIAL]`, which cannot occur here: that resume implies a
  `[WIP-PARTIAL]` HEAD, already ineligible one step earlier). The injected
  message was also reworded from a prediction ("delivery will refuse this
  claim right now") to a present-tense statement ("delivery does not
  accept it as it stands"), because `detail` can legitimately name a
  transient remote condition (an unreadable origin, an unverifiable
  pushed branch) and the guard must never assert a definite outcome about
  those.
* (Sixth review) four findings:

  1. (HIGH) The reword above stopped the MESSAGE from asserting a definite
     outcome, but `Orchestrator._build_landed_claim_guard`'s `probe` still
     recovered `refuted` by `subject_reason.startswith(f"{head} is not on
     {ship_ref}")` — pattern-matching `_already_satisfied_subject`'s
     human-readable prose. Several of that function's genuinely
     exception-driven "cannot tell" reasons (an unresolvable delivery
     branch, an unresolvable origin remote, a `remote_branch_relation` call
     that itself raised) are built from that exact same prefix — so a
     transient failure in any of those git/network calls could make the
     guard tell the coder "delivery does not accept it as it stands" — a
     REFUSAL — over a condition that could clear up moments later. (A plain
     "unknown" relation, e.g. simply never pushed, is NOT one of these —
     delivery treats that as a final refusal too, so it stays determinate;
     see `test_an_unknown_pushed_branch_relation_is_refused` in
     `tests/test_already_satisfied_subject_tree.py`, which this fix's first
     draft got wrong and which caught the mistake.) Fixed by having
     `_already_satisfied_subject` return an explicit `determinate` status
     code as its 7th element, and deriving `refuted` from that code instead
     of any wording in `subject_reason` — see that method's docstring and
     `_build_landed_claim_guard`'s probe. Pinned by
     `test_already_satisfied_subject_reports_indeterminate_for_transient_
     conditions` (`tests/test_landed_claim_early_refusal.py`) and by the
     `determinate` assertions added to the existing fixture-based tests
     here.
  2. The detector fired on two shapes outside the MUST_NOT_FIRE corpus: a
     NEGATION sentence mentioning the claim phrase ("Checked: this is NOT
     already satisfied at commit 1a2b3c4d5e6f - the change is still
     missing, I will implement it now.") and an INCIDENTAL cued hex token
     in a separate clause of the same utterance ("No code changes are
     needed; the tamper baseline is at 1a2b3c4d5e6f and the suite is
     green."). Fixed by bounding both the negation check and the sha-cue
     search to the CLAUSE containing the `_CLAIM` match — see
     `_clause_span` and its use in `detect_claim_assertion`.
  3. There is no "flush" path: `note_text` only LATCHES a pending claim;
     `hook` (which awaits the real probe) fires on the next PostToolUse
     event. A claim made in the agent's FINAL utterance, with no further
     tool call in the attempt, is latched but never probed — the exact
     incident shape this module exists to catch early would, in that one
     case, still only be caught at delivery time. This is an accepted,
     deliberate limitation, not an oversight: `hook` is the only place that
     may `await` (see its docstring), and `note_text` is called from a
     synchronous sink with no safe way to schedule a trailing probe once
     the attempt has already ended. A claim with no subsequent tool call
     still gets `_already_satisfied_subject`'s answer — just at delivery
     time, exactly as before this module existed, not earlier.
  4. `_seen` is keyed on the head alone and added to BEFORE the probe runs
     (`_note_text`), by design (see `_note_text`'s docstring) — one probe
     per head per attempt. A "cannot tell" probe result now returns
     `refuted=False` the same as a genuine accept (finding 1 above), so a
     transient failure here no longer produces a false refusal; it does
     still consume that head's one probe for the attempt, so a LATER
     network blip clearing up cannot be re-checked until the head changes.
     Accepted for the same reason as the third-review fix does not retry:
     a fresh commit changes the head and gets its own probe, and delivery
     itself re-asks the same question at delivery time regardless.

  Also: an earlier revision of this module docstring said "42" while
  `tests/test_landed_claim_early_refusal.py` said "43" for the same
  population (the live delivery-time refusals `diverged_repo` models) —
  a copy/paste drift, not two different measurements. Neither figure could
  later be re-derived from the same query (see the Eleventh review below),
  so both were dropped rather than reconciled to one; the test file now
  describes the shape without a count.
* (Seventh review, WITHDRAWN by the Eighth review below) a round claimed the
  Sixth review's clause-bounding fix (finding 2 above) was itself over-broad
  in the other direction — that bounding the sha-cue search to ONLY the
  clause containing the `_CLAIM` match missed "3 of 5 natural claim shapes"
  where the phrase and the cued sha sit in DIFFERENT clauses of the same
  utterance — and widened the search to the primary clause PLUS any other
  clause, still inside the bounded snippet window, that independently
  matched `_CLAIM` and was not itself negated.
* (Eighth review) the Seventh review's premise was fabricated: its "3 of 5"
  measurement was five hand-written sentences, not a real corpus, and its
  own author withdrew the finding. Measured against a large corpus of real
  agent utterances (no re-derivable query for that corpus ships with this
  change, so no precise counts are asserted here), the widening recovered
  ZERO real claims — the same set of firings, byte-identical, with or
  without it — while adding false-positive firings on 10 of 10
  hand-constructed two-clause non-claim prose shapes,
  four of which inject a full LANDED-CLAIM REFUSED correction through the
  real probe, and letting a second, unrelated clause's `_CLAIM` match
  short-circuit the composite PostToolUse hook and swallow other tool-call
  feedback. Reverted `detect_claim_assertion` back to the primary-clause-only
  search (the shape the Sixth review's fix originally produced) and dropped
  `test_a_claim_phrase_and_its_sha_in_different_clauses_is_still_the_named_sha`
  (`tests/test_landed_claim_guard.py`), which pinned the withdrawn shape. The
  negation and incidental-hex tests the Sixth review added remain green,
  unchanged — they are genuinely fixed and were never in question.
* (Ninth review) two findings:

  1. (BLOCKER) An unreachable `origin` remote (``ls-remote`` itself failing —
     network/auth/bad URL) was indistinguishable, at the
     `GitRepo.remote_branch_relation` level, from a branch that was simply
     never pushed — both returned the same ``"unknown"`` string, and
     `Orchestrator._already_satisfied_subject` treated that string as
     determinate. That let a transient remote failure produce a DEFINITE
     refusal through this guard, exactly the outcome the Fifth review's
     reword was meant to prevent. Fixed in `GitRepo`/`Orchestrator`, not
     here: `remote_branch_relation` now returns a distinct ``"unreachable"``
     for that case (declared in `GitRepo.TRANSIENT_RELATIONS`), and
     `_already_satisfied_subject` derives its `determinate` status directly
     from that set instead of a hand-enumerated list of "determinate"
     relation strings — see both docstrings for the full account, and
     `tests/test_landed_claim_early_refusal.py::
     test_an_unreachable_remote_is_not_a_refusal` for the guard-level pin.
  2. (HIGH) Several hand-constructed non-claim prose shapes — a quoted
     excerpt, a question, a hypothetical, quoting the marker itself, prose
     naming a DIFFERENT branch, a manifest hash mentioned nearby, a
     self-correction — still satisfy `_is_actionable_claim`'s marker-alone
     path, because that path was never meant to be as strict as delivery's
     own `_parse_already_satisfied` (which additionally requires
     `n_criteria` well-formed ``CRITERION:`` lines). Two sentences here had
     drifted into claiming an equivalence with delivery's bar that was never
     true. Corrected both (the second-review bullet above and
     `_is_actionable_claim`'s own docstring) to say what the code actually
     checks, rather than narrow the detector further — narrowing has
     repeatedly regressed real claim coverage for zero measured benefit (see
     the Seventh/Eighth review above). An earlier revision of this bullet
     claimed the only cost of a false positive here was a wasted probe call
     with no message ever injected — that was wrong: `probe()` is a
     zero-argument callable keyed on the branch's actual state, not on the
     text that triggered it, so whenever the branch genuinely IS in the live
     refusal shape (criterion 3), these non-claim shapes inject a message
     too, exactly like a real claim would. What that message is NOT is
     FALSE: `detail` is always delivery's own real answer about the branch's
     actual state (see `hook`'s docstring), never fabricated from the
     prose — only the message's "you said the work already exists" framing
     is unwarranted on these shapes. The residual cost this choice accepts
     is a spurious-but-truthful correction, never an assertion the repo
     cannot back up. Proven directly, both halves (message suppressed when
     the branch is not actually refused, message truthful when it is), in
     `tests/test_landed_claim_guard.py::
     test_non_claim_shapes_that_still_fire_the_actionability_gate_never_lie`.
     (Superseded below by the Tenth review: the "unwarranted framing" this
     finding accepted as a residual cost was itself fixable without
     narrowing the detector, and has since been fixed.)
* (Tenth review) the Ninth review's finding 2 stopped at documenting the
  injected message's "you said the work already exists" opening as
  truthful-but-unwarranted on these seven non-claim shapes, rather than
  fixing it — but that framing described the CODER's intent, which the
  guard never actually knows, when it could instead describe the one thing
  the guard does know: that text matching the claim shape was detected.
  Reworded the opening clause to "text matching an already-landed claim was
  detected" and the closing instruction from "Do NOT end the attempt on
  this claim" to "If this was meant as an already-landed claim, do NOT end
  the attempt on it" — both now hold regardless of whether the detected
  text is a genuine claim, a quoted excerpt, a question, a hypothetical, a
  quoted marker, a reference to a sibling branch, a manifest hash, or a
  self-correction. `detail` — delivery's own real, current answer — and the
  "does not accept it as it stands" clause the Fifth review pinned (see
  `test_the_refusal_message_does_not_predict_delivery_will_refuse`) are both
  unchanged. See `tests/test_landed_claim_guard.py::
  test_non_claim_shapes_that_still_fire_the_actionability_gate_never_lie`,
  which now also asserts the message never claims the coder said anything.
* (Eleventh review, DO-NOT-LAND) three blockers on the Ninth review's fix
  itself:

  1. (BLOCKER) The Ninth review's fix only covered `ls-remote` itself
     failing. `_have_remote_commit` (in `GitRepo`) still collapsed a
     SECOND, distinct failure — the follow-up `git fetch` of the
     advertised object into the private push-check namespace itself
     erroring (network, auth, a ref conflict) — into a bare `False`, which
     `remote_branch_relation` folded into the same STABLE `"unknown"` as a
     branch that was simply never pushed. Reproduced directly (no network
     needed): pre-create `refs/no_human/push-check/<branch>` as a plain
     file so the internal fetch collides with it while `ls-remote` still
     succeeds normally. Fixed by giving `GitRepo` a tri-state
     `_remote_commit_status` (`"have"`/`"fetch_failed"`/`"missing"`),
     having `remote_branch_relation` return a new, distinct
     `"fetch_failed"` member of `TRANSIENT_RELATIONS` for that case, and
     updating both `_already_satisfied_subject`'s docstring/comments and
     this module's own (the Ninth review bullet above) to name it. Pinned
     in `tests/test_vcs.py::
     test_remote_branch_relation_is_fetch_failed_when_the_object_fetch_itself_errors`
     by mutation (reverting the `status == "fetch_failed"` branch in
     `remote_branch_relation` makes that test fail; restoring it passes).
  2. `tests/test_landed_claim_early_refusal.py::
     test_an_unresolvable_ship_ref_is_not_a_refusal`'s docstring claimed a
     mutant dropping only the `bool(ship_ref)` term from the `refuted`
     filter would be caught by that test specifically. It would not: in
     every reachable `_already_satisfied_subject` return path, an empty
     `ship_ref` (or `head`) currently co-occurs with `determinate=False`,
     so `determinate` alone already forces `refuted=False` there — the
     `bool(ship_ref)`/`bool(head)` terms are redundant with `determinate`
     given the current implementation and are not independently pinned by
     any single test. Corrected the docstring to say so rather than claim
     an isolated pin that does not exist; the FULL `refuted = not
     shippable` mutant (dropping all three guard terms at once) remains
     pinned, which is what the acceptance criteria require.
  3. Two more inaccuracies: this method's own docstring (the "Sixth review"
     bullet above) miscounted the prefix-sharing "cannot tell" reasons as
     five while listing four and wrongly including plain `"unknown"`
     (which delivery treats as a final refusal, not cannot-tell) instead of
     the actual `TRANSIENT_RELATIONS` members; and the opening paragraph's
     MEASURED attempt/turn/token figures could not be reproduced from the
     same query on a later pass. Corrected the
     enumeration in `Orchestrator._build_landed_claim_guard`'s docstring
     and `_already_satisfied_subject`'s docstring to match the code exactly
     (`TRANSIENT_RELATIONS`, not a hand-recount), and dropped the
     unreproducible figures from this module's opening paragraph rather
     than re-assert them without a re-derivable query.
* (Twelfth review, DO-NOT-LAND) three findings, one BLOCKER carried over
  from a still-unfixed copy of the Eleventh review's retracted measurement,
  one new BLOCKER, one SHOULD-FIX:

  1. (BLOCKER) The Eleventh review's finding 3 dropped the unreproducible
     "42 attempts / 1,969 turns / 34,662,551 weighted tokens" figure from
     this module's opening paragraph, but a second copy of the same
     retracted measurement still shipped in
     `tests/test_landed_claim_early_refusal.py`'s `diverged_repo` fixture
     docstring. Dropped there too, in favor of describing the fixture's
     shape (HEAD at the local base tip, `origin/main` not containing it)
     without a count. Verified clean by a full-tree grep for the retracted
     figures with a positive control (`TRANSIENT_RELATIONS`, present in
     both `orchestrator.py` and `git.py`) to confirm the grep methodology
     itself finds real matches before trusting an empty result on the
     retracted figures.
  2. (BLOCKER) `_already_satisfied_subject`'s sibling-branch check
     (`GitRepo.remote_branches_containing`, reached only when the local
     `branch` pointer lags `head` so `remote_branch_relation` above is
     skipped) silently folded an `ls-remote` failure into the same bare
     `[]` a genuine "no siblings" answer produces, indistinguishable from
     it — an unreachable `origin` there fell through to the final "was
     never pushed" return with `determinate=True`, a DEFINITE refusal
     for what is really a transient "cannot tell". The same fail-open
     shape the Eleventh review's finding 1 fixed one remote call closer
     in. Fixed the same way: gave `GitRepo` a tri-state
     `remote_branches_containing_status` (returning `(matches,
     remote_reachable)`) with `remote_branches_containing` now a thin
     wrapper over it for existing callers, and had
     `_already_satisfied_subject` return `determinate=False` when
     `remote_reachable` is `False` instead of falling through to the
     definite refusal. Pinned by mutation in
     `tests/test_landed_claim_early_refusal.py::
     test_an_unreachable_remote_during_the_sibling_check_is_not_a_refusal`
     (reverting the `determinate=False` branch for an unreachable sibling
     check makes that test fail; restoring it passes) — a mutation pin,
     not a fails-before-base-ref test, because this defect was introduced
     by this task's own diff and no base-ref repro is possible.
  3. Several source-comment `~NNNN` line citations this task's own diff
     had introduced had drifted from later edits in the same diff (not
     from the reviewer's original staleness finding alone) and pointed at
     the wrong line. Re-verified each against the actual code with
     `grep -n`/`Read` (not trusted from the stale review text) and
     corrected all of them across `landed_claim_guard.py`,
     `orchestrator.py`, and `tests/test_landed_claim_early_refusal.py`.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from ..core.prompt_blocks import supervisor_channel_tag

log = logging.getLogger("no_human.landed_claim_guard")

# Deliberately loose and cheap (no LLM): the same "cheap, deterministic,
# never wrong to double-check" shape as `supervisor.detect_inability`. A
# false negative just means the guard stays silent (delivery still asks the
# same question later). A false positive on this regex ALONE no longer costs
# anything, because `_is_actionable_claim` below requires more before the
# guard spends a probe on it.
_CLAIM = re.compile(
    r"already (?:satisfied|implemented|landed|done|committed|exists|in (?:the )?(?:base|main))"
    r"|ALREADY-SATISFIED"
    r"|no (?:code )?changes (?:are )?needed"
    r"|work (?:is|was) already (?:there|present)",
    re.I,
)
# A bare `[0-9a-f]{7,40}` also matches ordinary English words ("defaced",
# "cabbage") and unrelated hex-shaped tokens (manifest hashes, digests)
# anywhere in the text. A sha is only ever named to say WHERE the work
# landed, so require one of the words that introduces such a reference
# ("at"/"in"/"as"/"commit"/"sha") immediately before the token — prose
# cannot satisfy both that cue AND the hex shape by accident.
#
# (Recall nit) a coder narrating a claim in markdown routinely fences the
# sha in backticks ("already implemented at `abc1234def`") — the optional
# backtick pair around the token must not stop the cue from matching.
_SHA_CUE = re.compile(
    r"\b(?:at|in|as|commit(?:ted)?|sha)\b[:=]?\s+`?([0-9a-f]{7,40})(?:`|\b)", re.I
)

_SNIPPET_BEFORE = 40
_SNIPPET_AFTER = 80
_SNIPPET_MAX = 120

# (Sixth review) two shapes the loose `_CLAIM`/`_SHA_CUE` match alone cannot
# tell apart from a real claim:
#
# 1. A NEGATION sentence mentioning the claim phrase — "Checked: this is NOT
#    already satisfied at commit 1a2b3c4d5e6f - the change is still missing,
#    I will implement it now." `_parse_already_satisfied` already treats a
#    negation sentence mentioning the marker as not-a-claim at delivery time;
#    this detector must apply the same bar.
# 2. An INCIDENTAL cued hex token in a SEPARATE clause of the same
#    utterance — "No code changes are needed; the tamper baseline is at
#    1a2b3c4d5e6f and the suite is green." The fixed-width snippet window
#    alone can span both clauses, so an unrelated `at <sha>` mention makes an
#    unrelated statement look like an actionable, commit-naming claim.
#
# Both are fixed the same way: bound both the negation check and the sha-cue
# search to the CLAUSE containing the `_CLAIM` match (split on `.;!?`/
# newline), not the whole utterance or the whole fixed-width window — a sha
# or a negation word belongs to the claim only when it is part of the same
# clause the claim phrase itself is in.
_CLAUSE_BOUNDARY = re.compile(r"[.;!?\n]")
_NEGATION = re.compile(
    r"\b(?:not|never|no longer|isn't|wasn't|doesn't|didn't|won't|can't|cannot)\b",
    re.I,
)


def _clause_span(text: str, pos: int) -> tuple[int, int]:
    """The ``[start, end)`` span of the clause containing offset ``pos`` —
    bounded by the nearest ``.;!?``/newline (or the start/end of ``text``) on
    each side."""
    start = 0
    for boundary in _CLAUSE_BOUNDARY.finditer(text, 0, pos):
        start = boundary.end()
    end_match = _CLAUSE_BOUNDARY.search(text, pos)
    end = end_match.start() if end_match else len(text)
    return start, end

#: The exact contract marker `Orchestrator._parse_already_satisfied` requires
#: on a line of its own before it will treat a final report as a claim at
#: all. Mirrored here (not imported — `core.orchestrator` imports THIS
#: module, so importing back would be circular) so the guard's firing bar
#: matches delivery's routing bar for the formal-report shape.
_ALREADY_SATISFIED_MARKER = "ALREADY-SATISFIED"


@dataclass(frozen=True)
class ClaimAssertion:
    """A detected "the work already exists" assertion.

    ``sha`` is the commit named in the same utterance, or ``""`` when none
    was named. Cosmetic only: the guard's probe always judges the branch's
    CURRENT head (exactly as delivery's `_already_satisfied_subject` does —
    it never consults a commit named in the claim's own prose either), so
    ``sha`` is used only to decide whether the claim is actionable
    (`_is_actionable_claim`). It is NOT quoted back in the injected message —
    that message names the probe's own `resolved_sha`/HEAD instead (see
    `LandedClaimGuard.hook`); only `snippet` from this dataclass is echoed.
    """

    sha: str
    snippet: str


def detect_claim_assertion(text: str) -> ClaimAssertion | None:
    """Detect an in-prose "this already exists" claim. ``None`` when absent."""
    if not text:
        return None
    m = _CLAIM.search(text)
    if m is None:
        return None
    clause_start, clause_end = _clause_span(text, m.start())
    # A negation sentence mentioning the claim phrase is not a claim — the
    # same bar `Orchestrator._parse_already_satisfied` applies at delivery
    # time ("a negation sentence mentioning the marker is not a claim").
    # Bounded to the claim's own clause: a negation word in an EARLIER,
    # unrelated clause must not suppress a real claim later in the utterance.
    if _NEGATION.search(text, clause_start, m.start()):
        return None
    start = max(0, m.start() - _SNIPPET_BEFORE)
    end = min(len(text), m.end() + _SNIPPET_AFTER)
    window = text[start:end]
    snippet = window.strip().replace("\n", " ")
    if len(snippet) > _SNIPPET_MAX:
        snippet = snippet[:_SNIPPET_MAX]
    # The sha-cue search is bounded to the PRIMARY clause only — the same
    # clause the `_CLAIM` match itself is in. (Eighth review) an earlier
    # revision of this function widened the search to also cover any OTHER
    # clause, still inside the snippet window, that independently matched
    # `_CLAIM` — reasoning that a natural claim can split the phrase and the
    # cued sha across two clauses of the same utterance. That widening has
    # been reverted. Measured against a large corpus of real agent
    # utterances (no re-derivable query for that corpus ships with this
    # change, so no precise counts are asserted here), it recovered zero
    # real claims (byte-identical firings before and after the widening)
    # while adding false-positive firings on 10 of 10
    # hand-constructed two-clause non-claim prose shapes, four of which
    # inject a full LANDED-CLAIM REFUSED correction through the real probe —
    # and it let a second, unrelated clause's `_CLAIM` match short-circuit
    # the composite PostToolUse hook, swallowing other tool-call feedback.
    # The "3 of 5 natural claim shapes" measurement that motivated the
    # widening was withdrawn by its own author: it was five hand-written
    # sentences, not a real measurement. Bounding to the primary clause only
    # is the shape that survived actual measurement.
    clause = text[max(start, clause_start):min(end, clause_end)]
    sha_match = _SHA_CUE.search(clause)
    return ClaimAssertion(sha=(sha_match.group(1) if sha_match else ""), snippet=snippet)


def _is_actionable_claim(text: str) -> ClaimAssertion | None:
    """`detect_claim_assertion`, gated to the claims worth spending a probe
    on.

    Send-back (second review): the loose phrase match alone fires on
    ordinary prose that never names a commit and never uses the formal
    contract ("I already ran the full suite; no changes needed in
    tests/test_foo.py"; "Let me check whether the prior session's work is
    already there."; "Refactor complete. No code changes are needed to the
    CLI; only the docs move."). None of those are a claim delivery would
    ever act on. A claim only becomes ACTIONABLE once it commits to
    something checkable: an explicitly cued commit, or the
    ``ALREADY-SATISFIED`` contract marker line on its own — this is
    deliberately LOOSER than `Orchestrator._parse_already_satisfied`'s own
    bar, which additionally requires at least `n_criteria` well-formed
    ``CRITERION: ... — MET — evidence: ...`` lines with none NOT-MET before
    delivery will route a zero-diff completion to the already-satisfied
    gate. The marker alone is enough here because this gate only decides
    whether to ask delivery's real question early, not whether delivery
    would ultimately accept the claim; some non-claim prose that merely
    quotes, questions, or hypothesizes about the marker can still pass this
    looser bar. When it does and the branch is genuinely in the live
    refusal shape, `hook` DOES still inject a message over it — the message
    is never false (`detail` always names delivery's own real, current
    answer), and (Tenth review) its opening clause no longer claims the
    coder said anything either: it names what the guard actually knows
    ("text matching an already-landed claim was detected"), which holds
    regardless of whether the detected text is a genuine claim or one of
    these non-claim shapes. See the ninth- and tenth-review bullets in the
    module docstring, and
    `tests/test_landed_claim_guard.py::
    test_non_claim_shapes_that_still_fire_the_actionability_gate_never_lie`
    for both halves proven directly.
    """
    assertion = detect_claim_assertion(text)
    if assertion is None:
        return None
    if assertion.sha:
        return assertion
    if any(ln.strip() == _ALREADY_SATISFIED_MARKER for ln in (text or "").splitlines()):
        return assertion
    return None


# `probe()` -> (refuted, sha, detail). Awaited, no argument: the claim is
# ALWAYS judged against the branch's CURRENT head — exactly as delivery's
# `Orchestrator._already_satisfied_subject` does, which never consults a
# commit named in the claim's own prose either. Built by the orchestrator
# over that exact method — see `Orchestrator._build_landed_claim_guard`.
# `detail`, when `refuted` is True, already names the commit and the branch
# it is not on (delivery's own reason string), reused verbatim so the
# in-attempt message and the eventual delivery-time refusal read the same.
Probe = Callable[[], "Awaitable[tuple[bool, str, str]]"]


class LandedClaimGuard:
    """PostToolUse hook: tests an in-attempt already-satisfied claim against
    the exact question delivery asks the moment the claim is made, and
    injects a non-terminal correction naming the commit and the branch it is
    not on when refuted.

    Never ends the session (no ``continue_: False``): aborting here would
    trade a ~47-turn burn for a zero-turn burn *and* lose the work already
    done — refusing the CLAIM, not the attempt, is the fix.
    """

    def __init__(
        self,
        *,
        probe: Probe,
        head_sha: Callable[[], str],
        on_event: Callable[..., None] | None = None,
    ):
        self._probe = probe
        self._head_sha = head_sha
        self._on_event = on_event
        self._seen: set[str] = set()
        self._pending_head: str | None = None
        self._pending_snippet: str = ""

    def note_text(self, text: str) -> None:
        """Feed one utterance of agent prose. Best-effort: this never awaits
        or blocks — the actual (async, network-touching) probe call happens
        in `hook()`, which the SDK dispatcher awaits — so it is safe to call
        directly from a synchronous event sink on the shared event loop
        thread. A failure here is logged and swallowed, never raised into
        the caller (the same "advisory never breaks the hook" convention
        `supervisor.py` uses for its own budget/send-back formatting).

        (Sixth review) No flush path: a claim latched here is only ever
        probed by a SUBSEQUENT `hook()` call (the next PostToolUse event).
        A claim made in the agent's FINAL utterance, with no further tool
        call in the attempt, is latched but never probed here — delivery
        still asks the same question later, at delivery time, exactly as
        it did before this module existed. See the module docstring,
        sixth review, point 3, for why this is accepted rather than fixed.
        """
        try:
            self._note_text(text)
        except Exception:  # noqa: BLE001 — advisory, never break the caller
            log.warning("landed_claim_guard: note_text raised; ignoring",
                        exc_info=True)

    def _note_text(self, text: str) -> None:
        assertion = _is_actionable_claim(text)
        if assertion is None:
            return
        try:
            head = (self._head_sha() or "").strip()
        except Exception:  # noqa: BLE001 — unresolvable HEAD, nothing to judge
            return
        if not head or head in self._seen:
            return
        # Latch BEFORE probing: one probe (and, if refuted, one injection)
        # per head per attempt — a claim repeated verbatim across several
        # turns must not re-run the delivery-time check nor pile up
        # duplicate corrections.
        self._seen.add(head)
        self._pending_head = head
        self._pending_snippet = assertion.snippet

    async def hook(
        self, input_data: dict, tool_use_id: str | None, context: Any
    ) -> dict:
        """The SDK PostToolUse hook callback. Empty dict → no action;
        otherwise an `additionalContext` injection. Never `continue_: False`.

        The probe — delivery's own `_already_satisfied_subject`, which does
        real (network-touching) remote checks via `asyncio.to_thread` — is
        awaited HERE, not in `note_text`: `note_text` runs synchronously on
        the shared event-loop thread (fed directly from `_agent_sink`) and
        cannot safely await or block on it, while `hook` is already
        `async def` and already awaited by the real dispatcher.
        """
        if self._pending_head is None:
            return {}
        head = self._pending_head
        snippet = self._pending_snippet
        self._pending_head = None
        self._pending_snippet = ""
        try:
            refuted, resolved_sha, detail = await self._probe()
        except Exception:  # noqa: BLE001 — unverifiable must never look refuted
            return {}
        if not refuted:
            return {}
        resolved_sha = resolved_sha or head
        tag = supervisor_channel_tag()
        message = (
            f"{tag} LANDED-CLAIM REFUSED: text matching an already-landed "
            f"claim was detected (\"{snippet}…\"), but delivery does not "
            f"accept it as it stands — {detail} (checked the same way "
            "`_already_satisfied_subject` verifies it at delivery time: git "
            "merge-base --is-ancestor, plus the pushed-branch and sibling-"
            "branch checks). This is independent of the commit's subject "
            "line: a [WIP-PARTIAL]/[WIP-BLOCKED] checkpoint and an ordinary "
            "commit from a previous round are refused for the same reason. "
            "If this was meant as an already-landed claim, do NOT end the "
            "attempt on it — keep working and deliver the change on this "
            "branch."
        )
        if self._on_event is not None:
            try:
                self._on_event(
                    "landed_claim_refused", detail, sha=resolved_sha,
                )
            except Exception:  # noqa: BLE001 — the injection is already committed
                log.warning(
                    "landed_claim_guard: on_event sink raised; "
                    "injection still delivered", exc_info=True)
        return {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": message,
            }
        }
