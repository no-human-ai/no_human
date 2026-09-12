"""Refuse an already-satisfied claim at the moment it is made, not at delivery.

MEASURED 2026-09-10 over the whole attempts table: 42 attempts ended in
'already-satisfied claim refused' — burning 1,969 turns (avg 46.9) and
34,662,551 weighted tokens (avg 825,298 each) — because the claim's subject
tree was only ever classified at delivery time. The refusal itself is
CORRECT and stays exactly as it is. Only the *timing* changes here — this
module asks delivery's own question the moment the agent asserts the claim,
mid-attempt, so a doomed claim cannot spend a full turn budget before being
told it is refutable.

The check is never keyed on the claimed commit's subject line: of the 42
attempts, only 9 claimed a [WIP-PARTIAL]/[WIP-BLOCKED] checkpoint — the
other 33 claimed an ordinary commit left by a previous round whose review
had FAILED. A fix keyed on the checkpoint subject would cover only the 9
(21%); the delivery-time question covers all 42, because the subject is
never what makes (or doesn't make) the claim false.

Four revisions since the first version landed:

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
     contract marker `Orchestrator._parse_already_satisfied` requires at
     delivery — i.e. the SAME bar delivery itself uses to decide whether a
     zero-diff completion is even a claim worth routing anywhere.
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
  `resumed_commit` is `None` (`_run_attempt`, ~6501) — an ordinary
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
    ``sha`` is used only to decide whether the claim is actionable and to
    quote it back in the injected message.
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
    start = max(0, m.start() - _SNIPPET_BEFORE)
    end = min(len(text), m.end() + _SNIPPET_AFTER)
    window = text[start:end]
    snippet = window.strip().replace("\n", " ")
    if len(snippet) > _SNIPPET_MAX:
        snippet = snippet[:_SNIPPET_MAX]
    # Search the same bounded window the snippet uses, not the whole text:
    # an unrelated cued hex token elsewhere in a long utterance must not be
    # mistaken for the commit this particular claim names.
    sha_match = _SHA_CUE.search(window)
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
    ``ALREADY-SATISFIED`` contract marker — the exact bar
    `Orchestrator._parse_already_satisfied` applies before delivery will
    even route a zero-diff completion to the already-satisfied gate.
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
        `supervisor.py` uses for its own budget/send-back formatting)."""
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
            f"{tag} LANDED-CLAIM REFUSED: you said the work already exists "
            f"(\"{snippet}…\"), but delivery does not accept it as it "
            f"stands — {detail} (checked the same way "
            "`_already_satisfied_subject` verifies it at delivery time: git "
            "merge-base --is-ancestor, plus the pushed-branch and sibling-"
            "branch checks). This is independent of the commit's subject "
            "line: a [WIP-PARTIAL]/[WIP-BLOCKED] checkpoint and an ordinary "
            "commit from a previous round are refused for the same reason. "
            "Do NOT end the attempt on this claim — keep working and "
            "deliver the change on this branch."
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
