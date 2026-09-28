"""Mid-attempt warning for a "this already landed" claim in coder prose.

A coder mid-attempt sometimes asserts, in plain text, that its work already
exists on some commit ("already landed at <sha>", "already implemented",
an `ALREADY-SATISFIED` block) well before the attempt actually reaches
delivery's own zero-diff claim gate. When that claim is one delivery will go
on to refuse, the coder otherwise only finds out at the very end of the
attempt, after spending its whole turn budget on a foregone conclusion.

This module recognizes that claim shape in prose (`looks_like_landed_claim`)
and, on a match, asks `Orchestrator.claim_gate_decision` — delivery's own
decision function — the exact same question against the live tree
(`LandedClaimGuardHook`). It holds no independent opinion: everything about
whether a claim would be accepted, refused, or is undetermined comes back
from that one call, never recomputed here. A decision that could not be
determined (a git call inside it failed or timed out) is surfaced as
"could not be checked", never as a refusal — an unproven refusal is worse
than no warning at all.

The hook never blocks the coder's turn: the git-backed decision runs as a
background asyncio task kicked off by one `hook()` call and read back by a
later one, so the event loop stays free while it resolves.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from typing import Any

#: A claim phrase ("already landed/exists/shipped/merged/committed/
#: implemented") within this many characters of a hex commit-sha-shaped
#: token. Bounded so the phrase and the sha plausibly refer to each other
#: without requiring them adjacent — coder prose routinely separates them
#: with "at" / "as" / a colon / a line break.
_PROXIMITY_WINDOW = 200

_CLAIM_VERB_RE = re.compile(
    r"already\s+(?:landed|exists?|shipped|merged|committed|implemented)",
    re.IGNORECASE,
)
_SHA_TOKEN_RE = re.compile(r"\b[0-9a-f]{7,40}\b")
_ALREADY_SATISFIED_LINE_RE = re.compile(
    r"^\s*ALREADY-SATISFIED\s*$", re.IGNORECASE | re.MULTILINE,
)


def looks_like_landed_claim(text: str) -> str | None:
    """*text* (the coder's own prose), or `None` if it names no landed-work
    claim.

    Precision over recall: a missed claim costs nothing beyond the status
    quo (no warning), a falsely-recognized one costs the coder one nudge it
    can simply disregard. Two shapes both count: a standalone
    `ALREADY-SATISFIED` line (the contract `_parse_already_satisfied` itself
    parses), and a landed-work verb phrase with a hex sha within
    `_PROXIMITY_WINDOW` characters of it. This is claim *recognition* only —
    it says nothing about whether delivery would accept the claim.
    """
    if not text:
        return None
    if _ALREADY_SATISFIED_LINE_RE.search(text):
        return text
    for verb in _CLAIM_VERB_RE.finditer(text):
        start = max(0, verb.start() - _PROXIMITY_WINDOW)
        end = min(len(text), verb.end() + _PROXIMITY_WINDOW)
        if _SHA_TOKEN_RE.search(text[start:end]):
            return text
    return None


class LandedClaimGuardHook:
    """Non-blocking PostToolUse hook: warn a coder immediately when its own
    landed-work claim is one delivery will refuse.

    `decide` is the caller's bound `claim_gate_decision(..., final_text=...)`
    — this hook supplies only the claim text via `note_text`; every
    predicate behind the answer belongs to `decide`, not to this class.
    """

    def __init__(
        self,
        *,
        decide: Callable[..., Awaitable[Any]],
        on_event: Callable[[str, str], None] | None = None,
    ) -> None:
        self._decide = decide
        self._on_event = on_event or (lambda kind, text: None)
        self._pending: str | None = None
        self._task: asyncio.Task | None = None
        self._done = False

    def note_text(self, text: str) -> None:
        """Latch *text* as the claim to check, once, the first time it looks
        like a landed-work claim. Bounded, no I/O — safe to call on every
        assistant utterance."""
        if self._done or self._task is not None or self._pending is not None:
            return
        claim_text = looks_like_landed_claim(text or "")
        if claim_text is not None:
            self._pending = claim_text

    @staticmethod
    def _context(message: str) -> dict:
        return {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": message,
            }
        }

    async def hook(
        self, input_data: dict, tool_use_id: str | None, context: Any,
    ) -> dict:
        """Never blocks: starts the background check on the first call after
        a claim is latched and returns immediately; reads the result back
        (at most once) on a later call once it has resolved."""
        if self._done:
            return {}
        if self._task is None:
            if self._pending is None:
                return {}
            self._task = asyncio.ensure_future(self._decide(final_text=self._pending))
            return {}
        if not self._task.done():
            return {}
        self._done = True
        try:
            gate = self._task.result()
        except Exception:  # noqa: BLE001 — an advisory must never break the hook
            return {}
        return self._render(gate)

    def _render(self, gate: Any) -> dict:
        undetermined = getattr(gate, "undetermined", "")
        if undetermined:
            self._on_event("landed_claim_undetermined", undetermined)
            return {}
        if getattr(gate, "stage", None) != "claim":
            return {}
        if not getattr(gate, "refuses", False):
            return {}
        message = (
            "LANDED-CLAIM REFUSED — delivery will refuse this claim: "
            f"{gate.reason}. Do the work on this branch, or state what "
            "remains."
        )
        self._on_event("landed_claim_refused", message)
        return self._context(message)
