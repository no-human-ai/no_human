"""Whether a delivered PR's recorded base is still fresh against the real
trunk — the tri-state answer `blockers/wake.py`'s stale-but-mergeable rung
acts on, plus the delivery-time helper that records what to compare against.

THE DEFECT THIS CLOSES. `core/orchestrator.py:_finalize` persisted
`pr_watch`/`pr_branch`/`pr_delivered_url`/`pr_comment_since` and stamped
`merge_policy`/`ci_rollup` state at delivery, but never recorded the trunk tip
the PR was measured against — so nothing could tell a fresh AWAITING_APPROVAL
task from a stale one. `blockers.wake.WakeWatcher._check_open_pr`'s ladder
acts only on MERGED / CLOSED / CONFLICTING / new comments; a PR that stayed
MERGEABLE while trunk moved past it matched none of those rungs and was never
re-measured or woken. Trunk cadence measured over the last 60 landings: p25
507s, median 1507s — a task delivered just before a landing goes stale within
minutes, and the only way back was a fresh coder attempt racing the next
landing. This module gives delivery a base sha to record and the watcher a
way to ask, from the real ref, whether that sha is still the trunk tip.

THE TRI-STATE (mirrors `vcs/task_pr.AlreadySatisfiedLanding`'s
UNVERIFIABLE/NOTHING_TO_LAND/LANDING_REQUIRED shape: fail CLOSED, never guess
fresh). `FRESH` and `STALE` both mean the trunk tip resolved and could be
compared; `UNDETERMINED` means it could not be asked at all (unreadable repo,
unresolvable ref, no sha was ever recorded) — a caller that only checks for
`FRESH` before short-circuiting can never mistake one for the other, because
`UNDETERMINED` is never coerced into `FRESH`.

TRUNK-REF LADDER, DELIBERATELY UNTOUCHED. `resolve_trunk_tip` is a thin pass-
through to `derived_conflict.resolve_base_tip` — the one and only ladder used
on both the delivery side and the re-measure side, so this module introduces
no NEW disagreement about how a base ref resolves. Any existing disagreement
between that ladder and other surfaces is a separate, already-filed finding;
this module does not touch it.
"""

from __future__ import annotations

from dataclasses import dataclass

from .derived_conflict import resolve_base_tip
from .pr_watcher import _git_rc

FRESH = "fresh"
STALE = "stale"
UNDETERMINED = "undetermined"


@dataclass(frozen=True)
class BaseFreshness:
    """The answer `measure` gives. `recorded_sha`/`observed_sha` are ``""``
    when unknown — never `None` — so `as_dict()` round-trips cleanly through
    `task.context` (JSON has no tuple/None-vs-missing subtlety to preserve
    here)."""

    state: str
    base_ref: str
    recorded_sha: str
    observed_sha: str
    reason: str = ""

    def as_dict(self) -> dict:
        return {
            "state": self.state,
            "base_ref": self.base_ref,
            "recorded_sha": self.recorded_sha,
            "observed_sha": self.observed_sha,
            "reason": self.reason,
        }


async def fetch_base_ref(repo_path: str, base: str) -> bool:
    """Best-effort ``git fetch origin <base>`` — refreshes the local
    tracking ref before asking it a question. Deliberately NOT
    `derived_conflict.fetch_conflict_refs`: that fetches ``origin <base>
    <branch>`` in one command, so a task branch absent from the remote (a
    PR whose head branch was already deleted, or the fetch racing a rebase)
    fails the whole fetch and leaves the base ref silently stale. This
    fetches the base alone. Never raises (`_git_rc` already swallows
    `OSError` and enforces its own timeout); a failed fetch is not itself
    "undetermined" — the ref may still resolve locally from a previous
    fetch, so the caller proceeds to ask regardless."""
    if not repo_path or not base:
        return False
    rc, _ = await _git_rc(repo_path, "fetch", "--quiet", "origin", base)
    return rc == 0


async def resolve_trunk_tip(repo_path: str, base: str) -> str | None:
    """The live trunk tip for ``base``, or ``None`` when it cannot be
    resolved at all. A thin pass-through to
    `derived_conflict.resolve_base_tip` — see the module docstring for why
    this must stay the sole ladder."""
    return await resolve_base_tip(repo_path, base)


async def measure(repo_path: str | None, base: str | None,
                   recorded_sha: str | None, *, fetch: bool = True) -> BaseFreshness:
    """The tri-state freshness answer, driven by the ACTUAL ref state — never
    by a flag a caller passes in. Fails CLOSED: anything that prevents a real
    comparison (missing inputs, an unreadable repo, an unresolvable ref, a
    raised exception) comes back `UNDETERMINED`, never `FRESH`. This is the
    sole fail-closed point in the module and must never itself decide
    `FRESH`.
    """
    base = base or ""
    recorded_sha = recorded_sha or ""
    try:
        if not repo_path or not base:
            missing = "repo_path" if not repo_path else "base"
            return BaseFreshness(UNDETERMINED, base, recorded_sha, "",
                                  f"no {missing} to measure freshness against")
        fetch_ok = True
        if fetch:
            fetch_ok = await fetch_base_ref(repo_path, base)
        observed = await resolve_trunk_tip(repo_path, base)
        if observed is None:
            return BaseFreshness(
                UNDETERMINED, base, recorded_sha, "",
                f"trunk tip for {base!r} did not resolve (fetch_ok={fetch_ok})")
        if not recorded_sha:
            # A base sha may be absent because delivery predates this change,
            # or because `record_at_delivery` itself could not resolve the
            # tip. Either way this is "never measured", not "measured and
            # equal" — the caller can backfill `observed_sha` going forward,
            # but the freshness record itself stays undetermined so a
            # backfill is never mistakable for a determined-fresh answer.
            return BaseFreshness(UNDETERMINED, base, recorded_sha, observed,
                                  "no base sha was recorded at delivery")
        if observed == recorded_sha:
            return BaseFreshness(FRESH, base, recorded_sha, observed)
        return BaseFreshness(STALE, base, recorded_sha, observed)
    except Exception as exc:  # noqa: BLE001 — a watcher rung may never raise
        return BaseFreshness(UNDETERMINED, base, recorded_sha, "",
                              f"measurement raised: {exc!r}")


async def record_at_delivery(repo_path: str, base: str) -> dict:
    """What `_finalize` should merge into `task.context` at the instant a PR
    is opened: the trunk tip the PR was measured against. Returns ``{}``
    (the keys absent, not empty strings) when the tip could not be resolved
    — load-bearing, because an absent `pr_base_sha` reads back through
    `measure` as `UNDETERMINED`, never as fresh. Never raises."""
    try:
        tip = await resolve_trunk_tip(repo_path, base)
    except Exception:  # noqa: BLE001 — delivery must never fail on this
        tip = None
    if not tip:
        return {}
    return {"pr_base_sha": tip, "pr_base_ref": base}
