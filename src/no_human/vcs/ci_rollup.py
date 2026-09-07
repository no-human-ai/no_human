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
"""

from __future__ import annotations

import logging

from . import pr_watcher
from .pr_watcher import parse_pr_url

log = logging.getLogger(__name__)


def aggregate_rollup(checks: list[dict] | None) -> tuple[str | None, tuple[str, ...]]:
    """Reduce `pr_watcher.default_pr_checks`-shaped dicts to a CI verdict.

    Pure, no I/O. ``checks`` entries carry ``name``/``status`` (``"fail"`` |
    ``"pass"`` | ``"pending"``) and, additively, ``required``.

    - Empty/None → ``(None, ())`` — the "genuinely no CI, or could not ask"
      case (`[]` is also what `default_pr_checks` returns for no `gh` on
      PATH, an unparseable/non-GitHub ref, and a network/auth failure — all
      four conditions must map to "not reported", never to "success").
    - Any check with an unrecognised status never counts as passing, so
      `aggregate_rollup` can never silently manufacture a "success".
    """
    if not checks:
        return None, ()
    scoped = [c for c in checks if c.get("required")]
    if not scoped:
        scoped = list(checks)
    statuses = {str(c.get("status") or "") for c in scoped}
    if "fail" in statuses:
        failing = tuple(sorted(
            str(c.get("name") or "unnamed check")
            for c in scoped if c.get("status") == "fail"
        ))
        return "failure", failing
    if "pending" in statuses:
        return "pending", ()
    if statuses and statuses <= {"pass"}:
        return "success", ()
    # Unrecognised vocabulary (should not happen given `default_pr_checks`'s
    # own normalization, but this function must never manufacture a pass
    # from data it doesn't understand).
    return None, ()


async def fetch_ci_rollup(pr_url: str) -> tuple[str | None, tuple[str, ...]]:
    """Fetch and aggregate the delivered PR's check rollup.

    GitHub-only (a non-GitHub/unparseable URL degrades to ``(None, ())``,
    same as "no checks"). Never raises: a `gh`/network/auth failure is
    logged and degrades to ``(None, ())`` so an awaiting_approval transition
    is never blocked by a CI poll going wrong.
    """
    parsed = parse_pr_url(pr_url)
    if not parsed or parsed[0] != "github":
        return None, ()
    try:
        checks = await pr_watcher.default_pr_checks(pr_url)
    except Exception as exc:  # noqa: BLE001 — advisory poll, never blocks
        log.warning("ci rollup fetch failed for %s: %s", pr_url, exc)
        return None, ()
    return aggregate_rollup(checks)
