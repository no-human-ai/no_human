"""Funnel-regression pass/fail criteria (Phase C, task C1).

One tier of the nightly funnel corpus passes when all five hold: a PR was
opened, the reviewer passed it, the tier's HELD-OUT test went green, and the
run stayed inside the tier's cost and wall-clock ceilings.

Quality is MEASURED, never model-judged. ``quality`` is one of
``holdout_green`` / ``holdout_red`` / ``no_holdout`` and comes from running the
tier's held-out test against whatever the agent left behind — the same
instrument as ``eval.northstar.NorthStarRunner._holdout_ok`` and
``eval.replay._mergeable``. There is no score here and there must never be one
(project constraint #3); a reviewer verdict is a boolean with cited evidence,
and everything else on the record is a number somebody can re-derive.

Fail-closed: a tier that DEMANDS a green holdout and has no measurement fails.
An unmeasured holdout is the same evidence as a red one — none — and the whole
reason the gate measures instead of judging is that "no signal" must never read
as "pass".

Records are plain dicts whose keys are the ledger's own column names
(``tokens_used`` / ``cache_read_tokens`` / ``cache_creation_tokens``), so an
unweighted record prices through ``core.pricing.weighted_tokens`` — the single
arithmetic every budget cap in the product already uses, never a second price
table here (the lesson `eval/northstar.py`'s ``cost_ratio`` docstring records).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..core.pricing import weighted_tokens

#: The three values ``FunnelVerdict.quality`` can take. Measured, not judged.
QUALITY_GREEN = "holdout_green"
QUALITY_RED = "holdout_red"
QUALITY_NONE = "no_holdout"


@dataclass(frozen=True)
class FunnelCriteria:
    """One tier's ceiling, loaded from its ``criteria.json``."""

    pr_opened: bool
    review_passed: bool
    holdout_green: bool
    max_weighted_tokens: int
    max_wall_seconds: int

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "FunnelCriteria":
        return FunnelCriteria(
            pr_opened=bool(data["pr_opened"]),
            review_passed=bool(data["review_passed"]),
            holdout_green=bool(data["holdout_green"]),
            max_weighted_tokens=int(data["max_weighted_tokens"]),
            max_wall_seconds=int(data["max_wall_seconds"]),
        )


@dataclass
class FunnelVerdict:
    passed: bool
    failures: list[str] = field(default_factory=list)
    cost: int = 0
    wall: float = 0.0
    quality: str = QUALITY_NONE


def _cost(record: dict[str, Any]) -> int:
    """Weighted spend for *record*, from the total if the runner wrote one and
    from the token classes otherwise.

    The runner (``funnel_eval._price``) always writes ``weighted_tokens``,
    output premium included, so the token-class fallback below is the rare
    path: a record built by hand (a test, an older report on disk) that never
    went through the runner at all."""
    total = record.get("weighted_tokens")
    if total is not None:
        return int(total)
    return weighted_tokens(
        tokens_used=int(record.get("tokens_used") or 0),
        cache_read_tokens=int(record.get("cache_read_tokens") or 0),
        cache_creation_tokens=int(record.get("cache_creation_tokens") or 0),
        output_tokens=record.get("output_tokens"),
        model=record.get("model"),
    )


def _quality(record: dict[str, Any]) -> str:
    held = record.get("holdout")
    if held is None:
        return QUALITY_NONE
    return QUALITY_GREEN if held else QUALITY_RED


def evaluate(record: dict[str, Any], criteria: FunnelCriteria) -> FunnelVerdict:
    """Score one tier's run record against its tier ceiling.

    Every broken criterion is reported, not just the first: a night that went
    red on four things and is read as one is a night somebody re-runs four
    times. Each failure names the criterion and the ACTUAL numbers, because
    "too expensive" without the number is a second investigation.
    """
    cost = _cost(record)
    wall = float(record.get("wall_seconds") or 0.0)
    quality = _quality(record)
    failures: list[str] = []

    if criteria.pr_opened and not record.get("pr_url"):
        failures.append(
            f"pr_opened: no PR was opened (status="
            f"{record.get('status') or 'unknown'})")
    if criteria.review_passed and not record.get("review_passed"):
        failures.append(
            f"review_passed: the reviewer did not pass this run "
            f"(verdict={record.get('review_passed')!r})")
    if criteria.holdout_green and quality != QUALITY_GREEN:
        failures.append(f"holdout_green: held-out tests measured {quality}")
    if cost > criteria.max_weighted_tokens:
        failures.append(
            f"max_weighted_tokens: {cost:,} > {criteria.max_weighted_tokens:,}")
    if wall > criteria.max_wall_seconds:
        failures.append(
            f"max_wall_seconds: {wall:,.0f}s > {criteria.max_wall_seconds:,}s")

    return FunnelVerdict(passed=not failures, failures=failures, cost=cost,
                         wall=wall, quality=quality)
