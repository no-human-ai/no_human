"""Unit tests for `landed_claim_guard.py`.

No real git here — a fake repo double exposing only `branch_sha`/`is_ancestor`
(the documented duck type `classify_already_satisfied_landing` uses) drives
the "same containment question" tests; the rest exercise `LandedClaimGuard`
and `detect_claim_assertion` directly with a hand-built probe.
"""

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


def _real_probe(repo, *, branch="attempt/task-1", base="main"):
    def probe(sha: str) -> tuple[bool, str, str]:
        landing = classify_already_satisfied_landing(
            repo, sha=sha, branch=branch, base=base,
        )
        return (landing.verdict == LANDING_REQUIRED, landing.sha, landing.base_ref)
    return probe


# --- tested when made, not at delivery -------------------------------------

def test_note_text_refutes_the_claim_before_any_delivery():
    """A probe result is available on the very next hook() call — no
    orchestrator delivery path is involved."""
    calls = []

    def probe(sha: str) -> tuple[bool, str, str]:
        calls.append(sha)
        return (True, sha, "main")

    guard = LandedClaimGuard(probe=probe, head_sha=lambda: "deadbeef")
    guard.note_text("this is already implemented in abc1234def")

    result = _run(guard.hook({}, None, None))
    assert result, "the refusal must be visible before any delivery step runs"
    assert calls == ["abc1234def"]
    assert "hookSpecificOutput" in result


# --- same containment question ---------------------------------------------

def test_landing_required_refuses_and_nothing_to_land_does_not():
    sha = "1234567890abcdef1234567890abcdef12345678"

    refuted_probe = _real_probe(_FakeRepo(ancestor=False))
    guard = LandedClaimGuard(probe=refuted_probe, head_sha=lambda: sha)
    guard.note_text(f"already satisfied: the work already exists at {sha}")
    assert _run(guard.hook({}, None, None)), "not-an-ancestor must refuse"

    accepted_probe = _real_probe(_FakeRepo(ancestor=True))
    guard2 = LandedClaimGuard(probe=accepted_probe, head_sha=lambda: sha)
    guard2.note_text(f"already satisfied: the work already exists at {sha}")
    assert _run(guard2.hook({}, None, None)) == {}, "an ancestor must not be blocked"


def test_unverifiable_does_not_block():
    sha = "1234567890abcdef1234567890abcdef12345678"
    probe = _real_probe(_FakeRepo(unresolvable=True))
    guard = LandedClaimGuard(probe=probe, head_sha=lambda: sha)
    guard.note_text(f"already satisfied: the work already exists at {sha}")
    assert _run(guard.hook({}, None, None)) == {}, "an unresolvable base must fail open"


# --- reason names commit and branch -----------------------------------------

def test_refusal_names_the_commit_and_the_branch():
    sha = "1234567890abcdef1234567890abcdef12345678"
    probe = _real_probe(_FakeRepo(ancestor=False))
    guard = LandedClaimGuard(probe=probe, head_sha=lambda: sha)
    guard.note_text(f"already satisfied: the work already exists at {sha}")
    result = _run(guard.hook({}, None, None))
    message = result["hookSpecificOutput"]["additionalContext"]
    assert sha in message
    assert "main" in message
    assert "is not an ancestor of" in message


# --- continues, does not end -------------------------------------------------

def test_refusal_never_aborts_the_session():
    def probe(sha: str) -> tuple[bool, str, str]:
        return (True, sha, "main")

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
    def probe(sha: str) -> tuple[bool, str, str]:
        return (True, sha, "main")

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


def test_latch_injects_once_per_sha():
    calls = []

    def probe(sha: str) -> tuple[bool, str, str]:
        calls.append(sha)
        return (True, sha, "main")

    guard = LandedClaimGuard(probe=probe, head_sha=lambda: "deadbeef")
    guard.note_text("this is already implemented in abc1234def")
    guard.note_text("as said, this is already implemented in abc1234def")
    guard.note_text("this is already implemented in abc1234def, again")

    assert calls == ["abc1234def"], "the same sha must be probed at most once per attempt"

    # Only the first hook() call after the first note_text sees the pending
    # injection; the latch does not requeue it on the repeats above.
    first = _run(guard.hook({}, None, None))
    assert first
    second = _run(guard.hook({}, None, None))
    assert second == {}


def test_note_text_never_raises():
    def probe(sha: str) -> tuple[bool, str, str]:
        raise RuntimeError("boom")

    guard = LandedClaimGuard(probe=probe, head_sha=lambda: "deadbeef")
    guard.note_text("this is already implemented in abc1234def")  # must not raise
    assert _run(guard.hook({}, None, None)) == {}

    def raising_head_sha() -> str:
        raise RuntimeError("HEAD unresolvable")

    guard2 = LandedClaimGuard(probe=lambda sha: (True, sha, "main"), head_sha=raising_head_sha)
    guard2.note_text("the work is already there, nothing to do")  # no named sha, head_sha raises
    assert _run(guard2.hook({}, None, None)) == {}
