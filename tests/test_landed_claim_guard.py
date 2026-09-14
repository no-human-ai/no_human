"""Unit tests for `landed_claim_guard.py`.

No real git here — a fake repo double exposing only `branch_sha`/`is_ancestor`
(the documented duck type `classify_already_satisfied_landing` uses) drives
the "same containment question" tests; the rest exercise `LandedClaimGuard`
and `detect_claim_assertion` directly with a hand-built async probe.

`LandedClaimGuard.probe` is `Callable[[], Awaitable[tuple[bool, str, str]]]`
— async, zero-argument, always judging the branch's CURRENT head (never a
sha named in the claim's own prose), exactly like the real
`Orchestrator._already_satisfied_subject` it wraps in production. That is
a deliberate interface change from an earlier revision (sync, keyed on a
`sha` argument) — see `landed_claim_guard.py`'s module docstring, send-back
2: the probe must be delivery's own authority, and delivery's authority is
async and ignores any sha named in the claim text."""

from __future__ import annotations

import asyncio

import pytest

from no_human.agent.landed_claim_guard import (
    ClaimAssertion,
    LandedClaimGuard,
    _is_actionable_claim,
    detect_claim_assertion,
)
from no_human.vcs.task_pr import (
    LANDING_REQUIRED,
    NOTHING_TO_LAND,
    UNVERIFIABLE,
    classify_already_satisfied_landing,
)


def _run(coro):
    return asyncio.run(coro)


class _FakeRepo:
    """Duck-typed repo double: only `branch_sha`/`is_ancestor` are used by
    `classify_already_satisfied_landing`, matching `GitRepo`'s contract."""

    def __init__(self, *, base_sha="cafebabe" * 5, ancestor=False, unresolvable=False):
        self._base_sha = base_sha
        self._ancestor = ancestor
        self._unresolvable = unresolvable

    def branch_sha(self, name: str) -> str:
        if self._unresolvable:
            raise RuntimeError(f"unknown ref: {name}")
        return self._base_sha

    def is_ancestor(self, sha: str, descendant: str) -> bool:
        return self._ancestor


def _real_probe(repo, head_sha, *, branch="attempt/task-1", base="main"):
    """Adapts `classify_already_satisfied_landing`'s sync, sha-keyed shape to
    the async, zero-argument `Probe` contract `LandedClaimGuard` now
    requires. Mirrors how the real probe (built over
    `Orchestrator._already_satisfied_subject`) always judges the branch's
    CURRENT head — `head_sha` is called fresh on every probe, exactly like
    production, not whatever sha (if any) the claim's own prose named."""

    async def probe() -> tuple[bool, str, str]:
        sha = head_sha()
        landing = classify_already_satisfied_landing(
            repo, sha=sha, branch=branch, base=base,
        )
        refuted = landing.verdict == LANDING_REQUIRED
        detail = (
            f"{landing.sha} is not an ancestor of {landing.base_ref}"
            if refuted else ""
        )
        return (refuted, landing.sha, detail)

    return probe


# --- tested when made, not at delivery -------------------------------------

def test_note_text_refutes_the_claim_before_any_delivery():
    """A probe result is available on the very next hook() call — no
    orchestrator delivery path is involved, and the probe does not run at
    all until that hook() call (it is `note_text` that detects and latches
    the claim; the actual — async, network-touching in production — probe
    call happens in `hook()`, see the module docstring)."""
    calls = []

    async def probe() -> tuple[bool, str, str]:
        calls.append("probed")
        return (True, "abc1234def", "abc1234def is not an ancestor of main")

    guard = LandedClaimGuard(probe=probe, head_sha=lambda: "deadbeef")
    guard.note_text("this is already implemented in abc1234def")
    assert calls == [], "the probe must not run before hook() is awaited"

    result = _run(guard.hook({}, None, None))
    assert result, "the refusal must be visible before any delivery step runs"
    assert calls == ["probed"], "the probe must run exactly once, driven by hook()"
    assert "hookSpecificOutput" in result


# --- same containment question ---------------------------------------------

def test_landing_required_refuses_and_nothing_to_land_does_not():
    sha = "1234567890abcdef1234567890abcdef12345678"

    refuted_probe = _real_probe(_FakeRepo(ancestor=False), lambda: sha)
    guard = LandedClaimGuard(probe=refuted_probe, head_sha=lambda: sha)
    guard.note_text(f"already satisfied: the work already exists at {sha}")
    assert _run(guard.hook({}, None, None)), "not-an-ancestor must refuse"

    accepted_probe = _real_probe(_FakeRepo(ancestor=True), lambda: sha)
    guard2 = LandedClaimGuard(probe=accepted_probe, head_sha=lambda: sha)
    guard2.note_text(f"already satisfied: the work already exists at {sha}")
    assert _run(guard2.hook({}, None, None)) == {}, "an ancestor must not be blocked"


def test_unverifiable_does_not_block():
    sha = "1234567890abcdef1234567890abcdef12345678"
    probe = _real_probe(_FakeRepo(unresolvable=True), lambda: sha)
    guard = LandedClaimGuard(probe=probe, head_sha=lambda: sha)
    guard.note_text(f"already satisfied: the work already exists at {sha}")
    assert _run(guard.hook({}, None, None)) == {}, "an unresolvable base must fail open"


# --- reason names commit and branch -----------------------------------------

def test_refusal_names_the_commit_and_the_branch():
    sha = "1234567890abcdef1234567890abcdef12345678"
    probe = _real_probe(_FakeRepo(ancestor=False), lambda: sha)
    guard = LandedClaimGuard(probe=probe, head_sha=lambda: sha)
    guard.note_text(f"already satisfied: the work already exists at {sha}")
    result = _run(guard.hook({}, None, None))
    message = result["hookSpecificOutput"]["additionalContext"]
    assert sha in message
    assert "main" in message
    assert "is not an ancestor of" in message


def test_refusal_emits_a_landed_claim_refused_event():
    """Send-back (fourth review, surviving mutant): nothing in this file
    previously asserted the `on_event(...)` call inside `hook()` even runs —
    deleting it left every other assertion here green. `on_event` is how the
    refusal reaches the run's own event/telemetry stream (the orchestrator
    wires it to `self.emit`, see `_build_landed_claim_guard`); a silently
    dropped emit call would make a refused claim invisible to anything
    watching that stream even though the injection still landed."""
    sha = "1234567890abcdef1234567890abcdef12345678"
    probe = _real_probe(_FakeRepo(ancestor=False), lambda: sha)
    events: list[tuple[str, str, dict]] = []

    def on_event(kind, detail, **kw):
        events.append((kind, detail, kw))

    guard = LandedClaimGuard(probe=probe, head_sha=lambda: sha, on_event=on_event)
    guard.note_text(f"already satisfied: the work already exists at {sha}")
    result = _run(guard.hook({}, None, None))
    assert result, "the refusal itself must still fire"
    assert len(events) == 1, "exactly one event per refused claim"
    kind, detail, kw = events[0]
    assert kind == "landed_claim_refused"
    assert "is not an ancestor of" in detail
    assert kw.get("sha") == sha


def test_a_raising_on_event_does_not_swallow_the_injection():
    async def probe() -> tuple[bool, str, str]:
        return (True, "deadbeef", "deadbeef is not an ancestor of main")

    def raising_on_event(kind, detail, **kw):
        raise RuntimeError("sink is down")

    guard = LandedClaimGuard(
        probe=probe, head_sha=lambda: "deadbeef", on_event=raising_on_event)
    guard.note_text("this is already implemented in abc1234def")
    result = _run(guard.hook({}, None, None))
    assert result, "a broken telemetry sink must not cancel the refusal itself"
    assert "hookSpecificOutput" in result


# --- continues, does not end -------------------------------------------------

def test_refusal_never_aborts_the_session():
    async def probe() -> tuple[bool, str, str]:
        return (True, "deadbeef", "deadbeef is not an ancestor of main")

    guard = LandedClaimGuard(probe=probe, head_sha=lambda: "deadbeef")
    guard.note_text("this is already implemented in abc1234def")
    result = _run(guard.hook({}, None, None))
    assert "continue_" not in result
    assert result["hookSpecificOutput"]["hookEventName"] == "PostToolUse"


# --- subject-agnostic --------------------------------------------------------

@pytest.mark.parametrize(
    "subject",
    [
        "[WIP-PARTIAL] checkpoint before running out of budget",
        "[WIP-BLOCKED] blocked, already implemented in abc1234def",
        "fix the finding",
    ],
)
def test_wip_checkpoint_and_ordinary_commit_are_treated_identically(subject):
    async def probe() -> tuple[bool, str, str]:
        return (True, "abc1234def", "abc1234def is not an ancestor of main")

    guard = LandedClaimGuard(probe=probe, head_sha=lambda: "abc1234def")
    guard.note_text(
        f"commit {subject!r} — this work already exists at abc1234def"
    )
    result = _run(guard.hook({}, None, None))
    assert result, f"subject {subject!r} must be refused identically"


# --- detector -----------------------------------------------------------------

def test_detect_claim_assertion_extracts_named_sha_and_falls_back_to_head():
    named = detect_claim_assertion("this is already implemented in abc1234def, done.")
    assert named is not None
    assert named.sha == "abc1234def"

    unnamed = detect_claim_assertion("the work is already there, nothing to do")
    assert unnamed is not None
    assert unnamed.sha == ""


@pytest.mark.parametrize(
    "text",
    [
        "I already ran the tests and they pass",
        "I already checked the diff before committing",
        "let's already move to the next file",
        "",
    ],
)
def test_ordinary_prose_is_not_a_claim(text):
    assert detect_claim_assertion(text) is None


def test_a_negation_sentence_mentioning_the_claim_is_not_a_claim():
    """(Sixth review) `Orchestrator._parse_already_satisfied` explicitly
    treats a negation sentence mentioning the marker as not-a-claim at
    delivery time; the detector's looser `_CLAIM` regex + cued-sha
    actionability gate did not check for negation at all, so this exact
    shape (a named, cued sha AND the claim phrase) used to be read as an
    actionable claim even though the sentence explicitly says the work is
    NOT already satisfied."""
    text = (
        "Checked: this is NOT already satisfied at commit 1a2b3c4d5e6f - "
        "the change is still missing, I will implement it now."
    )
    assert detect_claim_assertion(text) is None


def test_an_incidental_cued_hex_in_a_separate_clause_is_not_the_named_sha():
    """(Sixth review) the fixed-width snippet window can span more than one
    clause of the same utterance — an unrelated `at <sha>` mention in a
    SEPARATE clause must not make an unrelated statement look like a claim
    that names a commit. The positive control (same sentence, token pushed
    outside the window) already passes
    (`test_manifest_hash_mentioned_alongside_the_claim_is_not_the_named_sha`);
    this pins the in-window/same-utterance shape that used to slip through."""
    text = (
        "No code changes are needed; the tamper baseline is at "
        "1a2b3c4d5e6f and the suite is green."
    )
    assertion = detect_claim_assertion(text)
    assert assertion is not None
    assert assertion.sha == ""


def test_a_second_clause_that_also_matches_claim_does_not_donate_its_sha():
    """(Eighth review) an earlier revision widened the sha-cue search from
    the primary clause only to the primary clause PLUS any OTHER clause,
    still in the snippet window, that independently matched `_CLAIM` —
    reasoning that a natural claim can split the phrase and the cued sha
    across two clauses of the same utterance. That widening was reverted:
    measured against a large corpus of real agent utterances it recovered
    zero real claims (no re-derivable query for that corpus ships with this
    change, so no precise count is asserted here), while firing on 10 of 10
    hand-constructed two-clause non-claim
    prose shapes shaped exactly like this one — a first clause that trips
    the loose `_CLAIM` regex with no sha of its own, and an unrelated
    SECOND clause that also happens to match `_CLAIM` and separately names
    a commit for an unrelated reason (an archived baseline, not this fix).
    Before the revert this text's sha was donated from the second clause
    into the first clause's claim, making an unrelated baseline mention
    look like an actionable, commit-naming claim; the amended criterion is
    that the detector must not fire on prose where a second clause merely
    happens to match the loose claim regex."""
    text = (
        "No changes needed here; the archived baseline commit already "
        "exists at 1a2b3c4d5e6f for reference."
    )
    assertion = detect_claim_assertion(text)
    assert assertion is not None
    assert assertion.sha == "", (
        "a sha cued in a DIFFERENT clause that merely also matches the "
        "loose claim regex must not be read as the named sha")


@pytest.mark.parametrize(
    "text, expected_sha",
    [
        ("this is already implemented at `abc1234def`", "abc1234def"),
        ("this is already implemented at `abc1234def`.", "abc1234def"),
        (
            "already satisfied: committed as "
            "`1234567890abcdef1234567890abcdef12345678`",
            "1234567890abcdef1234567890abcdef12345678",
        ),
    ],
)
def test_a_backticked_sha_is_read_as_the_named_sha(text, expected_sha):
    """(Recall nit) a coder narrating a claim in markdown routinely fences
    the sha in backticks. The closing backtick sits right up against the
    hex token with no trailing word character, so a plain `\\b` after an
    *optional* backtick never fires there — the fix anchors the trailing
    boundary on whichever delimiter (backtick or bare word edge) is
    actually present instead of assuming a word boundary in both cases."""
    assertion = detect_claim_assertion(text)
    assert assertion is not None
    assert assertion.sha == expected_sha


def test_unrelated_hex_token_before_the_claim_is_not_the_named_sha():
    """The sha is the commit named in the SAME utterance, not any hex-shaped
    token anywhere earlier in the text (e.g. a manifest hash mentioned in an
    unrelated sentence far outside the claim's snippet window)."""
    text = (
        "The review at 9999999abc noted a formatting nit unrelated to this "
        "change and was addressed in a follow-up note some time ago. "
        "Now, already satisfied: the work already exists at abc1234def"
    )
    assertion = detect_claim_assertion(text)
    assert assertion is not None
    assert assertion.sha == "abc1234def"
    assert assertion.sha != "9999999abc"


def test_english_word_shaped_like_hex_is_not_read_as_a_sha():
    """"defaced" is 7 letters, all in [a-f] — it must not be mistaken for a
    commit sha just because it fits `[0-9a-f]{7,40}`."""
    text = "The file was defaced earlier; the work is already there."
    assertion = detect_claim_assertion(text)
    assert assertion is not None
    assert assertion.sha == ""


def test_manifest_hash_mentioned_alongside_the_claim_is_not_the_named_sha():
    text = (
        "The manifest hash is 8a9049bcdbc7db86. Separately, no code changes "
        "are needed here."
    )
    assertion = detect_claim_assertion(text)
    assert assertion is not None
    assert assertion.sha == ""


# --- actionability gate (send-back, second review: detector false positives) -

@pytest.mark.parametrize(
    "text",
    [
        "I already ran the full suite; no changes needed in tests/test_foo.py",
        "Let me check whether the prior session's work is already there.",
        "Refactor complete. No code changes are needed to the CLI; only the "
        "docs move.",
        # (Sixth review) a negation sentence naming a cued sha — the sha
        # cue alone must not make this actionable when the sentence denies
        # the claim.
        "Checked: this is NOT already satisfied at commit 1a2b3c4d5e6f - "
        "the change is still missing, I will implement it now.",
        # (Sixth review) an incidental cued hex in a separate clause of the
        # same utterance — the sha cue belongs to an unrelated statement,
        # not to this claim.
        "No code changes are needed; the tamper baseline is at "
        "1a2b3c4d5e6f and the suite is green.",
    ],
)
def test_unnamed_sha_prose_without_the_contract_marker_never_reaches_the_probe(text):
    """Send-back (second review), Blocker 2: measured a majority of sampled
    attempts fired the old detector-only gate at least once, none of those
    firings a genuine incident, and a further share confirmed spurious (no
    re-derivable query for that sample ships with this change, so no
    precise counts are asserted here) — quoting these three exact strings.
    `detect_claim_assertion` may still
    match (it is deliberately loose — a false positive there costs nothing
    on its own), but the guard must never spend a probe on any of them: no
    explicitly cued commit, and no ``ALREADY-SATISFIED`` contract marker.
    Proven here by a probe that raises if it is ever awaited, against a
    repo/head that IS off base (so a real bug would show up as a raised
    exception, not silently as `{}` for an unrelated reason)."""

    async def probe() -> tuple[bool, str, str]:
        raise AssertionError("non-actionable prose must never reach the probe")

    guard = LandedClaimGuard(probe=probe, head_sha=lambda: "deadbeefdeadbeef")
    guard.note_text(text)
    assert _run(guard.hook({}, None, None)) == {}


def test_the_formal_already_satisfied_marker_is_actionable_even_without_a_cued_sha():
    """The other side of the Blocker 2 gate: a claim carrying the formal
    ``ALREADY-SATISFIED`` contract marker is actionable even when it names
    no cued sha and even when it does not (yet) carry the ``CRITERION``
    lines `Orchestrator._parse_already_satisfied` additionally requires —
    see `test_the_marker_alone_is_looser_than_delivery_s_own_bar` below for
    that gap pinned directly against delivery's own parser."""
    text = "ALREADY-SATISFIED\nCRITERION: it works — MET — evidence: ran it"

    async def probe() -> tuple[bool, str, str]:
        return (True, "deadbeefdeadbeef", "deadbeefdeadbeef is not an ancestor of main")

    guard = LandedClaimGuard(probe=probe, head_sha=lambda: "deadbeefdeadbeef")
    guard.note_text(text)
    assert _run(guard.hook({}, None, None)), "the formal marker must be actionable on its own"


def test_the_marker_alone_is_looser_than_delivery_s_own_bar():
    """(Send-back, Finding 2) `_is_actionable_claim`'s bar for the formal
    ``ALREADY-SATISFIED`` marker is the marker line by itself — deliberately
    LOOSER than `Orchestrator._parse_already_satisfied`'s own bar, which
    additionally requires at least `n_criteria` well-formed
    ``CRITERION: ... — MET — evidence: ...`` lines with none NOT-MET. An
    earlier revision's docstrings claimed these were "the SAME bar"/"the
    exact bar" delivery uses; they were not, and both have since been
    corrected (module docstring, "(Ninth review)"; `_is_actionable_claim`'s
    own docstring). Pinned here directly against delivery's real parser
    rather than just against prose, on the SAME marker-only text, so a
    future edit that quietly re-equalizes (or re-diverges) the two bars
    shows up as a test failure, not just a stale comment."""
    from no_human.core.orchestrator import _parse_already_satisfied

    text = "ALREADY-SATISFIED\nno criterion lines follow this."
    assert _is_actionable_claim(text) is not None, (
        "the guard's detector treats the marker alone as actionable")
    assert _parse_already_satisfied(text, 1) is None, (
        "delivery itself requires at least one well-formed MET CRITERION "
        "line and refuses to treat the bare marker as a claim at all — "
        "this is the gap the corrected docstrings describe")


@pytest.mark.parametrize(
    "text",
    [
        # Quoted excerpt: the marker appears verbatim, on its own line, but
        # only as a quotation of a PAST report — not an assertion about
        # this attempt.
        "The last status update literally read:\nALREADY-SATISFIED\n"
        "That was from a week ago, for a completely different task.",
        # Question: asking whether the condition holds, not asserting it.
        "Before I go implement this — is it possible the work is\n"
        "ALREADY-SATISFIED\nalready, and I'm about to duplicate it?",
        # Hypothetical: an "if this held" framing, not a claim.
        "If the marker below were true I could stop right now:\n"
        "ALREADY-SATISFIED\nbut I have not actually verified that yet.",
        # Quoting the marker itself, e.g. while explaining the contract.
        "For reference, the exact contract marker text is:\n"
        "ALREADY-SATISFIED\nwhich is what delivery's parser looks for.",
        # Referencing another branch, not this one.
        "Branch no-human/task-attempt-other reported:\n"
        "ALREADY-SATISFIED\nbut that is a sibling attempt, not this branch.",
        # Manifest hash mentioned nearby — unrelated numeric noise, not a
        # claim about this branch's code.
        "Manifest hash 8a9049bcdbc7db86 is unchanged.\nALREADY-SATISFIED\n"
        "(this line is only illustrating the report format, not asserting it)",
        # Self-correction: asserts, then immediately retracts, in a later
        # clause the marker's own clause/line does not span.
        "ALREADY-SATISFIED\nWait — scratch that, I have not actually "
        "finished, I still need to implement the change.",
    ],
    ids=[
        "quoted_excerpt",
        "question",
        "hypothetical",
        "quoting_the_marker_itself",
        "referencing_another_branch",
        "manifest_hash",
        "self_correction",
    ],
)
def test_non_claim_shapes_that_still_fire_the_actionability_gate_never_lie(text):
    """(Send-back, Finding 2) These seven hand-constructed shapes are NOT
    genuine "already exists" claims, yet each still satisfies
    `_is_actionable_claim`'s marker-alone path (the ``ALREADY-SATISFIED``
    line stands on its own, matching `_CLAIM` too, and outside any negation
    clause) — proving the false-positive premise Finding 2 raised. The
    chosen fix (see the module docstring's ninth-review bullet and
    `_is_actionable_claim`'s own docstring) was NOT to narrow the detector —
    narrowing has repeatedly regressed real claim coverage for zero measured
    benefit (Seventh/Eighth review) — but to accept the residual cost
    honestly. `probe()` is a zero-argument callable keyed on the branch's
    actual state, not on the text that triggered it, so this proves BOTH
    halves of that honesty directly, on the SAME shape:

    1. When the branch is not actually in the refusal shape (`probe`
       reports not-refuted, exactly what delivery's own
       `_already_satisfied_subject` would say when nothing is actually
       wrong), `hook` yields `{}` — no message at all.
    2. When the branch genuinely IS in the refusal shape (`probe` reports
       refuted, as it would for a branch that really isn't on
       `origin/main`), `hook` DOES still inject a message over this
       non-claim prose — but that message is never a lie: `detail` is
       always delivery's own real, current answer, named verbatim in the
       injected text. (Tenth review) the message's opening clause no
       longer frames this as the coder having SAID anything either — it
       now names only what the guard actually knows, that text matching
       an already-landed claim was detected, which holds regardless of
       whether the detected text is a genuine claim or one of these seven
       non-claim shapes.
    """
    assert _is_actionable_claim(text) is not None, (
        "this shape must fire the actionability gate — that's Finding 2's "
        "premise; if this assertion starts failing, the detector was "
        "narrowed and the shape below no longer demonstrates the gap")

    async def not_refuted_probe() -> tuple[bool, str, str]:
        return (False, "deadbeefdeadbeef", "")

    guard = LandedClaimGuard(probe=not_refuted_probe, head_sha=lambda: "deadbeefdeadbeef")
    guard.note_text(text)
    assert _run(guard.hook({}, None, None)) == {}, (
        "a non-claim shape that merely fires the actionability gate must "
        "never surface a refusal when the branch is not actually refused")

    truthful_detail = "deadbeefdeadbeef is not on origin/main"

    async def refuted_probe() -> tuple[bool, str, str]:
        return (True, "deadbeefdeadbeef", truthful_detail)

    guard2 = LandedClaimGuard(probe=refuted_probe, head_sha=lambda: "deadbeefdeadbeef")
    guard2.note_text(text)
    result = _run(guard2.hook({}, None, None))
    assert result, (
        "when the branch genuinely is refused, this shape DOES still "
        "inject a message — the fix accepted this cost rather than "
        "narrowing the detector")
    message = result["hookSpecificOutput"]["additionalContext"]
    assert truthful_detail in message, (
        "whatever the message says, it must be delivery's own real, "
        "current answer about the branch — never fabricated from the "
        "non-claim prose that happened to trigger the probe")
    assert "you said" not in message.lower(), (
        "(Tenth review) the message must never frame this as the coder "
        "having SAID an already-landed claim — a question, a hypothetical, "
        "a quotation, a reference to a sibling branch, or a self-correction "
        "never said any such thing; the message must instead name only "
        "what the guard actually knows, that matching text was detected")


def test_latch_injects_once_per_sha():
    calls = []

    async def probe() -> tuple[bool, str, str]:
        calls.append("probed")
        return (True, "abc1234def", "abc1234def is not an ancestor of main")

    guard = LandedClaimGuard(probe=probe, head_sha=lambda: "deadbeef")
    guard.note_text("this is already implemented in abc1234def")
    guard.note_text("as said, this is already implemented in abc1234def")
    guard.note_text("this is already implemented in abc1234def, again")

    # Only the first hook() call after the first note_text sees the pending
    # injection; the latch does not requeue it on the repeats above.
    first = _run(guard.hook({}, None, None))
    assert first
    assert calls == ["probed"], "the same head must be probed at most once per attempt"
    second = _run(guard.hook({}, None, None))
    assert second == {}

    # Send-back (third review, surviving mutant): the two assertions above
    # pass even with `self._seen.add(head)` deleted, because `_pending_head`
    # is cleared by `hook()` regardless of the latch. Pin the latch itself
    # by driving the REAL note->hook->note->hook interleave a real attempt
    # produces: the same head named again in a LATER turn, after the first
    # injection already fired, must not spend a second probe or a second
    # injection.
    guard.note_text("this is already implemented in abc1234def, still")
    third = _run(guard.hook({}, None, None))
    assert third == {}, "a later turn repeating the same head must stay latched"
    assert calls == ["probed"], "the latch must survive a later note/hook cycle too"


def test_note_text_never_raises():
    """`note_text` itself never raises. A raising probe is `hook()`'s concern
    now — the actual probe call moved there (see the module docstring) — so
    it must fail open there instead of ever propagating."""

    async def raising_probe() -> tuple[bool, str, str]:
        raise RuntimeError("boom")

    guard = LandedClaimGuard(probe=raising_probe, head_sha=lambda: "deadbeef")
    guard.note_text("this is already implemented in abc1234def")  # must not raise
    assert _run(guard.hook({}, None, None)) == {}

    def raising_head_sha() -> str:
        raise RuntimeError("HEAD unresolvable")

    probe_calls: list[str] = []

    async def probe() -> tuple[bool, str, str]:
        probe_calls.append("probed")
        return (True, "deadbeef", "deadbeef is not an ancestor of main")

    guard2 = LandedClaimGuard(probe=probe, head_sha=raising_head_sha)
    # Send-back (fourth review): the previous text here ("the work is
    # already there, nothing to do") carries neither a cued sha nor the
    # ALREADY-SATISFIED marker, so `_is_actionable_claim` already returns
    # `None` for it and `_note_text` returns before ever calling
    # `head_sha()` — the assertion below would pass even if `head_sha`
    # never raised at all. Use a cued-sha claim so the actionable gate is
    # actually cleared and the call reaches (and must survive) the raising
    # `head_sha`.
    guard2.note_text("this is already implemented in abc1234def")
    assert guard2._pending_head is None, (
        "a raising head_sha must never leave a pending injection latched")
    assert probe_calls == [], (
        "a raising head_sha must short-circuit before the probe is ever built")
    assert _run(guard2.hook({}, None, None)) == {}
    assert probe_calls == [], "hook() must not probe when head_sha raised in note_text"


def test_an_empty_head_sha_never_reaches_the_probe():
    """Mutant pin (STEP 3b): `_note_text`'s `if not head or head in self._seen:
    return` (~259) has two independently-testable halves; the `head in
    self._seen` half is already pinned by `test_latch_injects_once_per_sha`.
    This pins the OTHER half — `not head` — with a probe that refutes
    unconditionally and records every call, so a mutant that dropped `not
    head or` (leaving only the latch check) would let an empty-string HEAD
    (unresolvable, but not by raising — `head_sha` returning `""` is a
    distinct case from `test_note_text_never_raises`'s raising `head_sha`)
    through to a probe call and a bogus refusal."""
    probe_calls: list[str] = []

    async def probe() -> tuple[bool, str, str]:
        probe_calls.append("probed")
        return (True, "deadbeef", "deadbeef is not an ancestor of main")

    guard = LandedClaimGuard(probe=probe, head_sha=lambda: "")
    guard.note_text("this is already implemented in abc1234def")
    assert guard._pending_head is None, (
        "an empty head_sha must never leave a pending injection latched")
    assert probe_calls == [], (
        "an empty head_sha must short-circuit before the probe is ever built")
    assert _run(guard.hook({}, None, None)) == {}
    assert probe_calls == [], "hook() must not probe when head_sha was empty"


def test_the_refusal_message_does_not_predict_delivery_will_refuse():
    """(Fifth review) the injected message was reworded from a prediction
    ("delivery will refuse this claim right now") to a present-tense
    statement ("delivery does not accept it as it stands"), because `detail`
    can legitimately name a transient remote condition (an unreadable
    origin, an unverifiable pushed branch) and the guard must never assert a
    definite outcome about those."""
    sha = "1234567890abcdef1234567890abcdef12345678"
    probe = _real_probe(_FakeRepo(ancestor=False), lambda: sha)
    guard = LandedClaimGuard(probe=probe, head_sha=lambda: sha)
    guard.note_text(f"already satisfied: the work already exists at {sha}")
    result = _run(guard.hook({}, None, None))
    message = result["hookSpecificOutput"]["additionalContext"]
    assert "does not accept it as it stands" in message
    assert "will refuse this claim right now" not in message
