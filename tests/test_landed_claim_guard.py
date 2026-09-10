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
    ],
)
def test_unnamed_sha_prose_without_the_contract_marker_never_reaches_the_probe(text):
    """Send-back (second review), Blocker 2: measured 257/400 (64%) of
    attempts fired the old detector-only gate at least once, none of those
    firings a genuine incident, and at least 31/400 (8%) confirmed spurious
    — quoting these three exact strings. `detect_claim_assertion` may still
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
    ``ALREADY-SATISFIED`` contract marker `Orchestrator._parse_already_satisfied`
    requires at delivery IS actionable even when it names no cued sha —
    matching delivery's own bar for routing a zero-diff completion at all."""
    text = "ALREADY-SATISFIED\nCRITERION: it works — MET — evidence: ran it"

    async def probe() -> tuple[bool, str, str]:
        return (True, "deadbeefdeadbeef", "deadbeefdeadbeef is not an ancestor of main")

    guard = LandedClaimGuard(probe=probe, head_sha=lambda: "deadbeefdeadbeef")
    guard.note_text(text)
    assert _run(guard.hook({}, None, None)), "the formal marker must be actionable on its own"


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

    async def probe() -> tuple[bool, str, str]:
        return (True, "deadbeef", "deadbeef is not an ancestor of main")

    guard2 = LandedClaimGuard(probe=probe, head_sha=raising_head_sha)
    guard2.note_text("the work is already there, nothing to do")  # no named sha, head_sha raises
    assert _run(guard2.hook({}, None, None)) == {}
