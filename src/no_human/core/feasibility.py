"""A pre-flight feasibility HINT for a task, computed before any budget is spent.

The honest finding behind this (measured on the ledger, 2026-08-30): a task's
success cannot be *predicted* pre-flight with any confidence — the done-rate
spread across every signal knowable before planning is only ~38-60%. So this is
NOT a go/no-go gate and NOT a numeric self-score. It surfaces the feasibility
BAND that is already free to compute (the complexity tier + the intake eval
verdict), calibrated against this install's own per-tier done-rate, and NAMES a
one-click mitigation the human can take up front (split the task, or answer a
few clarifying questions) instead of discovering mid-run that a too-large task
grinds to budget-exhaustion.

Pure, read-only, fail-open — patterned on ``budget_floor.py``: it mutates
nothing, creates nothing, and any error returns ``None`` so a task proceeds
exactly as it would without a hint. The per-tier calibration is passed in by the
caller (a cheap DB query), keeping this module a pure function of the task.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .complexity import compute_tier, hint_signals

if TYPE_CHECKING:
    from .task import Task

log = logging.getLogger(__name__)

# Bands, worst → best. Only ``large``/``too_large`` produce a hint; ``likely``
# returns None so the UI never nags a task that looks fine.
BAND_TOO_LARGE = "too_large"
BAND_LARGE = "large"
BAND_LIKELY = "likely"

# One-click offers the UI wires to a mitigation.
OFFER_SPLIT = "split"
OFFER_CLARIFY = "clarify"


@dataclass(frozen=True)
class FeasibilityHint:
    """What to tell a human at task creation about how a task is likely to go."""

    band: str                    # too_large | large  (likely never returns a hint)
    tier: str                    # simple | standard | complex | trivial
    offer: str | None            # split | clarify | None
    done_rate_pct: int | None    # this install's done-rate for `tier`, if known
    signals: list[str]           # the fired complexity signals, for transparency
    hint_reasons: list[str] = field(default_factory=list)  # hint-only families' why

    def message(self) -> str:
        """One honest human line — a band and a calibrated rate, never a verdict.

        Deliberately says "similar tasks finished in one pass", never "this will
        fail": the predictor is weak and the copy must not overclaim it.
        """
        if self.band == BAND_TOO_LARGE:
            head = "This looks large — splitting it will likely finish faster"
        else:
            head = "This looks large"
        if self.done_rate_pct is not None:
            return (f"{head} — about {self.done_rate_pct}% of similar "
                    f"({self.tier}) tasks finished in one pass.")
        return f"{head}."


def estimate_feasibility(
    task: "Task",
    done_rate_by_tier: dict[str, int] | None = None,
    moa_cfg: dict | None = None,
    config: dict | None = None,
) -> FeasibilityHint | None:
    """The pre-flight hint for *task*, or ``None`` when nothing is worth offering.

    Uses only signals knowable BEFORE planning (the complexity tier + the intake
    eval verdict on ``context['eval_result']``) — no planning pass, no coder
    budget. ``done_rate_by_tier`` is this install's measured per-tier done-rate
    (``{tier: pct}``), supplied by the caller; ``None`` just omits the number.
    Fail-open: any error returns ``None``.

    ``config`` gates ``feasibility.hint_signals_enabled`` — it never affects
    the band/tier/offer decision above, only which *signals* the card shows
    (``compute_tier``'s own, or those plus hint-only families).
    """
    try:
        tier, signals = compute_tier(task, moa_cfg or {})
        verdict = ((task.context or {}).get("eval_result") or {}).get("verdict")

        # Thresholds (design C1): a `decompose` verdict or a `complex` tier is
        # large enough to offer a split; an ambiguous (`clarify`) spec offers
        # the clarify path; everything else looks fine — return no hint.
        if verdict == "decompose":
            band, offer = BAND_TOO_LARGE, OFFER_SPLIT
        elif tier == "complex":
            band, offer = BAND_LARGE, OFFER_SPLIT
        elif verdict == "clarify":
            band, offer = BAND_LARGE, OFFER_CLARIFY
        else:
            return None

        hints: list[str] = []
        try:
            signals, hints = hint_signals(task, moa_cfg or {}, config)
        except Exception as exc:  # advisory channel — never silent
            log.warning("hint signals skipped: %s", type(exc).__name__)

        pct = (done_rate_by_tier or {}).get(tier)
        return FeasibilityHint(
            band=band, tier=tier, offer=offer,
            done_rate_pct=int(pct) if pct is not None else None,
            signals=list(signals),
            hint_reasons=list(hints),
        )
    except Exception as exc:  # noqa: BLE001 — advisory, never blocks a create
        log.warning("feasibility hint skipped: %s", type(exc).__name__)
        return None
