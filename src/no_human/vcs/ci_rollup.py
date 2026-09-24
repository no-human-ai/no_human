"""Aggregate a delivered PR's GitHub check rollup into a single CI state.

WHY. `core.merge_policy._check_ci` reads `GateFacts.ci_state`, but nothing
wrote `task.context["ci_status"]` on a GitHub-Actions repo (only the
enterprise `ci_runner` path did, via `Orchestrator._run_ci`). This module is
the missing poll: it reuses `pr_watcher.default_pr_checks` (the same `gh`
call PR delivery/wake already make — no new dependency, no second `gh`
invocation shape) and reduces its normalized checks to
``(state, failing_check_names)``.

Scope: required/blocking checks only, when the rollup carries that data —
these are the checks GitHub's own merge policy enforces, and matching that
scope is what prevents a false-ready verdict (the File-inventory incident).
`pr_watcher.default_pr_checks` resolves `required` with a SECOND `gh`
call, `gh pr checks --required` (the generic `--json statusCheckRollup`
export cannot answer `isRequired` itself — that field takes a
`pullRequestId` argument plain `--json` field reflection does not supply).
A rollup with no `required` data at all (no entry has it truthy — a failed
required-lookup, or a repo with genuinely zero required checks) falls back
to evaluating every entry, so "no required-ness data" never silently looks
like "no checks".

TWO REDUCERS, TWO CONSUMERS — do not conflate them. `vcs.pr_outcome.
classify_ci` is a different reducer over a different (`gh pr checks --json`,
not `default_pr_checks`) shape; it feeds `pr_outcomes.ci_status`, the
outcome ledger surfaced by the API/db layer. `aggregate_rollup` below is
the one this module owns: it feeds `task.context["ci_status"]` and, via
`stamp_delivered_ci_status`, the merge gate's `ci` rule. Fix the reducer
whose consumer you actually mean to change.

THE MISSING-REQUIRED DISTINCTION. A rollup can report `state == "success"`
while every required context is simply absent (nothing named that context
ever posted a status) — e.g. only a non-required always-green job ran.
That is not the same fact as "CI state unknown": `aggregate_rollup` now
also returns which required names have no entry at all on this head,
computed from a *caller-supplied* required-name set. The two cases this
must tell apart:

  (a) required-ness data could not be obtained (the `gh pr checks
      --required` lookup failed) -> `required_names` is empty -> TOLERATE,
      same as today (an outage must never read as a block).
  (b) required-ness data WAS obtained and names contexts absent from the
      rollup -> `required_names` is non-empty and `missing_required` is
      non-empty -> the merge gate BLOCKS.

A non-empty `required_names` set is itself the positive evidence that the
lookup succeeded (`pr_watcher._required_check_names` returns `set()` on
every failure/degradation) — this module never inspects the fail-open
`required=False` flags on individual checks to make that call.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Callable, Collection

from ..core import merge_policy
from . import pr_watcher
from .pr_watcher import parse_pr_url

if TYPE_CHECKING:
    from ..core.task import Task
    from .git import GitRepo

log = logging.getLogger(__name__)


def aggregate_rollup(
    checks: list[dict] | None,
    required_names: Collection[str] | None = None,
) -> tuple[str | None, tuple[str, ...], tuple[str, ...]]:
    """Reduce `pr_watcher.default_pr_checks`-shaped dicts to a CI verdict.

    Pure, no I/O. ``checks`` entries carry ``name``/``status`` (``"fail"`` |
    ``"pass"`` | ``"pending"``) and, additively, ``required``.

    - Empty/None → ``(None, (), missing)`` — the "genuinely no CI, or could
      not ask" case (`[]` is also what `default_pr_checks` returns for no
      `gh` on PATH, an unparseable/non-GitHub ref, and a network/auth
      failure — all four conditions must map to "not reported", never to
      "success").
    - Any check with an unrecognised status never counts as passing, so
      `aggregate_rollup` can never silently manufacture a "success".

    Returns ``(state, failing, missing_required)``. ``missing_required``
    names every entry of ``required_names`` that has NO check in ``checks``
    at all — i.e. it demonstrably never ran, even when ``state`` itself is
    ``"success"`` (a non-required check can be all that ran). It is
    computed on EVERY return path, including the empty/None-checks one, so
    a head with a known required set and nothing reported names all of them
    as missing rather than reporting `()`.

    ``required_names=None`` (the default) or an empty collection both mean
    "no required-ness data" — indistinguishable between a failed
    `gh pr checks --required` lookup and a repo with genuinely zero
    required checks — so ``missing_required`` is always `()` in that case:
    there is no positive evidence to block on. Only a NON-empty
    ``required_names`` is treated as evidence the lookup succeeded; the
    per-check ``required`` flags are never consulted here, because they
    fail open to `False` on that same lookup failure.
    """
    present = {str(c.get("name") or "") for c in (checks or [])}
    missing_required = tuple(sorted(
        n for n in (required_names or ()) if n not in present
    ))
    if not checks:
        return None, (), missing_required
    scoped = [c for c in checks if c.get("required")]
    if not scoped:
        scoped = list(checks)
    statuses = {str(c.get("status") or "") for c in scoped}
    if "fail" in statuses:
        failing = tuple(sorted(
            str(c.get("name") or "unnamed check")
            for c in scoped if c.get("status") == "fail"
        ))
        return "failure", failing, missing_required
    if "pending" in statuses:
        return "pending", (), missing_required
    if statuses and statuses <= {"pass"}:
        return "success", (), missing_required
    # Unrecognised vocabulary (should not happen given `default_pr_checks`'s
    # own normalization, but this function must never manufacture a pass
    # from data it doesn't understand).
    return None, (), missing_required


async def fetch_ci_rollup(
    pr_url: str,
) -> tuple[str | None, tuple[str, ...], tuple[str, ...]]:
    """Fetch and aggregate the delivered PR's check rollup.

    GitHub-only (a non-GitHub/unparseable URL degrades to ``(None, (), ())``,
    same as "no checks"). Never raises: a `gh`/network/auth failure is
    logged and degrades to ``(None, (), ())`` so an awaiting_approval
    transition is never blocked by a CI poll going wrong — and, per
    `aggregate_rollup`'s contract, an outage can never manufacture missing-
    required evidence (the required-name set is empty on every degradation
    path here too).
    """
    parsed = parse_pr_url(pr_url)
    if not parsed or parsed[0] != "github":
        return None, (), ()
    try:
        checks, required_names = await pr_watcher.default_pr_checks_and_required(pr_url)
    except Exception as exc:  # noqa: BLE001 — advisory poll, never blocks
        log.warning("ci rollup fetch failed for %s: %s", pr_url, exc)
        return None, (), ()
    return aggregate_rollup(checks, required_names)


async def stamp_delivered_ci_status(
    *, store: Any, emit: Callable[..., None],
    advisory: Callable[[str], None], task: "Task", pr_url: str | None,
    head_sha: str, facts: merge_policy.GateFacts | None,
    extra_problems: tuple[str, ...], repo: "GitRepo",
) -> None:
    """Poll the delivered PR's GitHub check rollup and write it onto the
    task the moment it enters AWAITING_APPROVAL — this is the fix for the
    incident where `_check_ci` always read `ci_state=None` (nothing wrote
    `task.context["ci_status"]` on a GitHub-Actions repo; only the
    enterprise `ci_runner` path did, via `Orchestrator._run_ci`).

    Called from `Orchestrator._finalize` right after PR delivery, passing
    its own collaborators explicitly (`store`/`emit`/`advisory`) rather than
    `self`, so this module — not `_finalize` — carries the line weight of
    the stamping logic; `_finalize` only spends one call on it.

    Writes `task.context["ci_status"]` on every call (re-fetched on each
    entry to AWAITING_APPROVAL, so a re-delivery catches a check that
    finished or failed since the last poll) — this stays a single state
    string; the missing-required distinction is carried separately below,
    not folded into this vocabulary. When a state was obtained, OR when
    required contexts are known to be missing even though `state` came back
    (e.g. `"success"` from a lone non-required check), also recomputes and
    re-persists the merge-policy verdict for THIS head
    (`task.context["merge_policy"][head_sha]`) from the same
    `facts`/`extra_problems` `_finalize` already computed, with
    `ci_state`/`ci_failed_checks`/`ci_missing_required` refreshed — so the
    persisted verdict (what the API's `merge_ready` and `nh approve` read)
    reflects the real CI outcome, not the "none reported (tolerated)"
    default the pre-delivery compute necessarily used.

    Best-effort/advisory only: wrapped end-to-end so a `gh`/network
    failure, or any other error here, can never fail a delivered PR or
    block reaching AWAITING_APPROVAL.
    """
    if not pr_url or not head_sha or facts is None:
        return
    parsed = parse_pr_url(pr_url)
    if not parsed or parsed[0] != "github":
        return
    try:
        state, failing, missing = await fetch_ci_rollup(pr_url)
        task.context = await store.merge_context(
            task.id, {"ci_status": state})
        if state is not None or missing:
            verdict = merge_policy.evaluate_repo(
                repo.path, extra_problems=extra_problems,
                facts=replace(
                    facts, ci_state=state, ci_failed_checks=failing,
                    ci_missing_required=missing,
                ),
            )
            task.context = await store.merge_context(
                task.id, {"merge_policy": {head_sha: verdict.as_dict()}})
            emit("merge_policy", verdict.summary, ready=verdict.ready)
    except Exception as exc:  # noqa: BLE001 — advisory only; never blocks
        advisory(f"delivered PR CI status not polled: {exc}")
