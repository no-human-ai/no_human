"""Refuse an already-satisfied claim at the moment it is made, not at delivery.

MEASURED 2026-09-10 over the whole attempts table: 42 attempts ended in
'already-satisfied claim refused' — burning 1,969 turns (avg 46.9) and
34,662,551 weighted tokens (avg 825,298 each) — because the claimed commit's
containment against the base branch was only ever checked at delivery time.
The refusal itself is CORRECT and stays exactly as it is: the claimed sha
genuinely was not an ancestor of the base branch. Only the *timing* changes
here — this module asks the same containment question the moment the agent
asserts the claim, mid-attempt, so a doomed claim cannot spend a full turn
budget before being told it is refutable.

The check is keyed on ancestry (``git merge-base --is-ancestor``, via
``classify_already_satisfied_landing``), never on the claimed commit's
subject line: of the 42 attempts, only 9 claimed a [WIP-PARTIAL]/[WIP-BLOCKED]
checkpoint — the other 33 claimed an ordinary commit left by a previous round
whose review had FAILED. A fix keyed on the checkpoint subject would cover
only the 9 (21%); ancestry covers all 42, because the subject is never what
makes (or doesn't make) the claim false.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Callable

from ..core.prompt_blocks import supervisor_channel_tag

log = logging.getLogger("no_human.landed_claim_guard")

# Deliberately loose and cheap (no LLM): the same "cheap, deterministic,
# never wrong to double-check" shape as `supervisor.detect_inability`. A
# false negative just means the guard stays silent (delivery still asks the
# same question later); a false positive costs one extra, true statement of
# fact injected into the transcript.
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
_SHA_CUE = re.compile(
    r"\b(?:at|in|as|commit(?:ted)?|sha)\b[:=]?\s+([0-9a-f]{7,40})\b", re.I
)

_SNIPPET_BEFORE = 40
_SNIPPET_AFTER = 80
_SNIPPET_MAX = 120


@dataclass(frozen=True)
class ClaimAssertion:
    """A detected "the work already exists" assertion.

    ``sha`` is the commit named in the same utterance, or ``""`` when none
    was named — the caller resolves that case to the branch's current HEAD,
    which is exactly the sha delivery's own `_already_satisfied_subject`
    judges when no other commit is named.
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


# `probe(sha)` -> (refuted, resolved_sha, base_ref). Built by the orchestrator
# over `classify_already_satisfied_landing` — see `Orchestrator.
# _build_landed_claim_guard`. Never expected to raise; `note_text` tolerates
# it anyway (see its docstring).
Probe = Callable[[str], "tuple[bool, str, str]"]


class LandedClaimGuard:
    """PostToolUse hook: tests an in-attempt already-satisfied claim against
    the base branch the moment it is made, and injects a non-terminal
    correction naming the commit and the branch it is not on when refuted.

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
        base_hint: str = "",
    ):
        self._probe = probe
        self._head_sha = head_sha
        self._on_event = on_event
        self._base_hint = base_hint
        self._seen: set[str] = set()
        self._pending: str | None = None

    def note_text(self, text: str) -> None:
        """Feed one utterance of agent prose. Best-effort: a probe or sink
        failure is logged and swallowed, never raised into the caller (the
        same "advisory never breaks the hook" convention `supervisor.py`
        uses for its own budget/send-back formatting)."""
        try:
            self._note_text(text)
        except Exception:  # noqa: BLE001 — advisory, never break the caller
            log.warning("landed_claim_guard: note_text raised; ignoring",
                        exc_info=True)

    def _note_text(self, text: str) -> None:
        assertion = detect_claim_assertion(text)
        if assertion is None:
            return
        sha = assertion.sha
        if not sha:
            try:
                sha = (self._head_sha() or "").strip()
            except Exception:  # noqa: BLE001 — unresolvable HEAD, nothing to judge
                sha = ""
        if not sha or sha in self._seen:
            return
        # Latch BEFORE probing: one probe (and, if refuted, one injection)
        # per sha per attempt — a claim repeated verbatim across several
        # turns must not re-run `git merge-base --is-ancestor` nor pile up
        # duplicate corrections.
        self._seen.add(sha)
        try:
            refuted, resolved_sha, base_ref = self._probe(sha)
        except Exception:  # noqa: BLE001 — unverifiable must never look refuted
            return
        if not refuted:
            return
        tag = supervisor_channel_tag()
        message = (
            f"{tag} LANDED-CLAIM REFUSED: you said the work already exists "
            f"(\"{assertion.snippet}…\"), but {resolved_sha} is not an "
            f"ancestor of {base_ref} — it is not on {base_ref}, so delivery "
            "will refuse this claim exactly as it is being refused now "
            "(verified with git merge-base --is-ancestor). This is "
            "independent of the commit's subject line: a [WIP-PARTIAL]/"
            "[WIP-BLOCKED] checkpoint and an ordinary commit from a previous "
            "round are both refused for the same reason. Do NOT end the "
            "attempt on this claim — keep working and deliver the change on "
            "this branch."
        )
        self._pending = message
        if self._on_event is not None:
            try:
                self._on_event(
                    "landed_claim_refused",
                    f"{resolved_sha} is not an ancestor of {base_ref}",
                    sha=resolved_sha, base_ref=base_ref,
                )
            except Exception:  # noqa: BLE001 — the injection is already committed
                log.warning(
                    "landed_claim_guard: on_event sink raised; "
                    "injection still delivered", exc_info=True)

    async def hook(
        self, input_data: dict, tool_use_id: str | None, context: Any
    ) -> dict:
        """The SDK PostToolUse hook callback. Empty dict → no action;
        otherwise an `additionalContext` injection. Never `continue_: False`."""
        if self._pending is None:
            return {}
        message = self._pending
        self._pending = None
        return {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": message,
            }
        }
