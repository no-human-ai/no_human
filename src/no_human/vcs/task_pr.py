"""Single task-level PR resolver.

A task's "current PR" has never had one home. `_finalize` writes
`ctx["pr_watch"]` after a *delivering* PR open (`orchestrator.py`); an
already-satisfied resume (`_gate_already_satisfied`) sets AWAITING_APPROVAL
without ever writing it; a code-review draft (`_open_draft_pr_for_review`)
deliberately does not write `attempts.pr_url` either. Live incident: task
`16f850ae` opened draft PR #230 on attempt 4 (an already-satisfied resume),
no attempt ever recorded a `pr_url`, `pr_watch` was never set — so
`WakeWatcher._check_open_pr` read `ctx.get("pr_watch")`, got nothing, and the
task heartbeated "nothing to do" for over an hour after the PR was merged.

This module is the one place that answers "what PR does this task have,
right now" — every caller (the watcher's rungs, the already-satisfied
backfill) goes through it instead of reading `pr_watch` directly.

Rungs, in priority order, first non-empty wins:
  1. ``pr_watch``    — a `_finalize`-delivered PR. Unchanged current
                        behaviour; every task that already worked keeps
                        resolving here first.
  2. an attempt's ``pr_url`` — the newest non-empty one across all attempts
                        of the task (`Store.latest_attempt_pr_url`). This is
                        the inheritance rung the acceptance criteria pin.
  3. a live draft slot — ``ctx["pr_draft_created"]``, unless that URL was
                        already abandoned (`ctx["abandoned_pr_urls"]`). This
                        is the rung the *live incident* actually needed.

``None`` and ``""`` both collapse to "absent" everywhere in here — never
treated as different facts.

No writes, no network calls. ``store``/``task`` are duck-typed (only
``store.latest_attempt_pr_url`` and ``task.id``/``task.context`` are used) so
this module does not import ``core``.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ResolvedPR:
    """The task's current PR, and which rung produced it.

    ``source`` is one of ``"pr_watch"``, ``"attempt"``, ``"draft"``, or
    ``"none"`` — ``url``/``branch`` are ``""`` exactly when ``source`` is
    ``"none"``.
    """

    url: str
    branch: str
    source: str


def _clean(value: Any) -> str:
    """``None``/whitespace/anything falsy collapses to ``""`` — the one place
    null-vs-empty-string ambiguity is resolved, so no caller has to."""
    return str(value or "").strip()


async def resolve_task_pr(store: Any, task: Any) -> ResolvedPR:
    ctx = task.context or {}

    watch_url = _clean(ctx.get("pr_watch"))
    if watch_url:
        return ResolvedPR(watch_url, _clean(ctx.get("pr_branch")), "pr_watch")

    attempt_url = _clean(await store.latest_attempt_pr_url(task.id))
    if attempt_url:
        branch = _clean(ctx.get("pr_branch")) or _clean(ctx.get("pr_draft_branch"))
        return ResolvedPR(attempt_url, branch, "attempt")

    draft_url = _clean(ctx.get("pr_draft_created"))
    if draft_url:
        abandoned = {_clean(u) for u in (ctx.get("abandoned_pr_urls") or [])}
        if draft_url not in abandoned:
            return ResolvedPR(draft_url, _clean(ctx.get("pr_draft_branch")), "draft")

    return ResolvedPR("", "", "none")


#: The event kinds that mean "there is a PR" — `pr_open` from `_finalize`'s
#: delivering-PR open and `_gate_already_satisfied`'s promoted-draft path,
#: `pr_draft` from `_open_draft_pr_for_review`. The one home for "does this
#: task have a PR", shared by `task_pr`, `doctor` and `restore-approval`.
PR_EVENT_KINDS = frozenset({"pr_open", "pr_draft"})

#: Back-compat alias — the SAME object, so `is` still holds for both names.
_PR_EVENT_KINDS = PR_EVENT_KINDS

#: Evidence that legitimately backs `AWAITING_APPROVAL`: a PR (open or
#: draft) or a typed `already_satisfied` event from `_gate_already_satisfied`
#: (the no-PR already-satisfied case, which never produces a PR to point at).
AWAITING_APPROVAL_EVIDENCE_KINDS = PR_EVENT_KINDS | frozenset({"already_satisfied"})

#: Evidence that legitimately backs `DONE`: a delivered PR (`pr_open`, from
#: `_finalize`), a human override of a landed task
#: (`approved_landed_override`, from `blockers/landed_override.py`), a
#: manually-recorded merge (`human_merged`), or an approved already-satisfied
#: claim with no PR (`approved_already_satisfied`, from `cli/commands.py` /
#: `api/app.py`'s approve routes).
DONE_EVIDENCE_KINDS = frozenset({
    "pr_open",
    "approved_landed_override",
    "human_merged",
    "approved_already_satisfied",
})


async def task_has_pr_evidence(store: Any, task: Any) -> str:
    """The PR this task is known to have, or ``""`` — the ONE question "is
    there something for the human to merge?" fails CLOSED on.

    Live incident (8c8b36b5): a draft PR opened pre-review is recorded only
    as ``context["pr_draft_created"]``, never on an attempt row. The two
    "is there a PR to merge" call sites that read ``attempts.pr_url`` alone
    missed it and completed the task while its PR (#253) sat open. This is
    the ONE place both must call instead.

    First tries `resolve_task_pr` (covers `pr_watch` -> `attempts.pr_url` ->
    `pr_draft_created`, minus `abandoned_pr_urls`). If that comes up empty,
    scans persisted `task_events` for any event whose `kind` is in
    ``{"pr_open", "pr_draft"}`` and reads its URL from `pr_url` (the
    `pr_draft` shape — ``orchestrator.emit("pr_draft", ..., pr_url=url)``)
    falling back to `text` (the `pr_open` shape — the URL IS the emitted
    text there). Not abandoned, not counted. This is the belt-and-braces
    rung for a PR recorded only in the event log and nowhere else.
    """
    resolved = await resolve_task_pr(store, task)
    if resolved.url:
        return resolved.url

    ctx = task.context or {}
    abandoned = {_clean(u) for u in (ctx.get("abandoned_pr_urls") or [])}
    for e in await store.list_events(task.id):
        if e.get("kind") not in _PR_EVENT_KINDS:
            continue
        url = _clean(e.get("pr_url")) or _clean(e.get("text"))
        if url and url not in abandoned:
            return url
    return ""


#: `classify_already_satisfied_landing` verdicts — see its docstring. The
#: string values are also used as `already_satisfied_landing["verdict"]` in
#: persisted task context, so they are part of the on-disk contract: do not
#: rename.
LANDING_REQUIRED = "landing_required"
NOTHING_TO_LAND = "nothing_to_land"
UNVERIFIABLE = "unverifiable"


@dataclass(frozen=True)
class AlreadySatisfiedLanding:
    """The verdict `classify_already_satisfied_landing` reached, and why.

    ``verdict`` is one of `LANDING_REQUIRED`, `NOTHING_TO_LAND`,
    `UNVERIFIABLE`. ``reason`` is a human-readable sentence naming which case
    applied — the exact text `approve`'s console output and the
    orchestrator's gate `detail` string surface to a human, so its two
    non-`UNVERIFIABLE` phrasings ("satisfying commit reachable from base
    (nothing to land)" / "satisfying commit on task branch only (landing
    required)") are part of the human-facing contract: do not reword.
    """

    verdict: str
    sha: str
    branch: str
    base_ref: str
    reason: str


def classify_already_satisfied_landing(
    repo: Any, *, sha: str, branch: str, base: str,
) -> AlreadySatisfiedLanding:
    """Is the already-satisfied claim's satisfying commit reachable from the
    base branch already — or does it exist only on the task branch and still
    need to land?

    Root cause of the incident this closes: an already-satisfied approval
    used to be treated as "nothing to land" unconditionally, even when the
    satisfying commit was never pushed to (or merged into) the base branch —
    silently marking the task DONE while the deliverable sat stranded on the
    task branch. This function is the one place that tells the two cases
    apart, and it fails CLOSED: any ambiguity (empty inputs, an unresolvable
    base, a git error) comes back `UNVERIFIABLE`, never `NOTHING_TO_LAND`.

    ``repo`` is duck-typed — only ``branch_sha(name)`` (raises on an
    unresolvable ref) and ``is_ancestor(sha, descendant)`` (never raises,
    `False` on anything it cannot resolve) are used, the same contract
    `GitRepo` (`vcs/git.py`) exposes. No network calls are made here — a
    caller that wants a fresh remote view must `repo.fetch()` first.

    Base-ref candidates are tried in order, first that resolves wins:
    ``origin/<base>``, ``<base>``, ``origin/main``, ``main`` — mirroring
    `Orchestrator._already_satisfied_subject`'s own ladder, base-first
    instead of default-branch-first since there is no local `GitRepo` handle
    passed here for a `default_branch()` lookup.
    """
    sha = (sha or "").strip()
    branch = (branch or "").strip()
    base = (base or "").strip()

    if not sha:
        return AlreadySatisfiedLanding(
            UNVERIFIABLE, sha, branch, "", "no satisfying commit sha recorded")

    candidates = []
    if base:
        candidates.append(f"origin/{base}")
        candidates.append(base)
    candidates.append("origin/main")
    candidates.append("main")

    base_ref = ""
    base_sha = ""
    try:
        for candidate in candidates:
            try:
                base_sha = repo.branch_sha(candidate)
            except Exception:
                continue
            if base_sha:
                base_ref = candidate
                break
    except Exception as exc:
        return AlreadySatisfiedLanding(
            UNVERIFIABLE, sha, branch, "", f"base branch lookup failed: {exc}")

    if not base_ref or not base_sha:
        return AlreadySatisfiedLanding(
            UNVERIFIABLE, sha, branch, "",
            f"could not resolve a base ref among {candidates!r}")

    try:
        on_base = repo.is_ancestor(sha, base_sha)
    except Exception as exc:
        return AlreadySatisfiedLanding(
            UNVERIFIABLE, sha, branch, base_ref,
            f"ancestry check failed: {exc}")

    if on_base:
        return AlreadySatisfiedLanding(
            NOTHING_TO_LAND, sha, branch, base_ref,
            "satisfying commit reachable from base (nothing to land)")

    reason = "satisfying commit on task branch only (landing required)"
    if branch:
        reason = f"{reason} — branch {branch}"
    return AlreadySatisfiedLanding(LANDING_REQUIRED, sha, branch, base_ref, reason)


async def land_already_satisfied_claim(
    store: Any, task: Any, *, repo_path: str,
    identity_name: str = "no_human",
    identity_email: str = "no-human@acme.com",
    never_push_to: list[str] | None = None,
    github_hosts: list[str] | None = None,
) -> dict[str, str]:
    """Decide — and, when needed, act on — what an ALREADY-SATISFIED claim's
    approval must do next. The ONE place `cli/commands.py::approve` and
    `api/app.py::approve_task` both call, so the two surfaces can never
    diverge on this decision.

    Trusts a persisted ``already_satisfied_landing.on_base is True`` as
    "nothing to land" (the classification `_gate_already_satisfied` already
    computed at review time, `core/orchestrator.py`). Any other stored
    value — ``False``, missing, or the whole key absent (an older attempt,
    from before this classification existed) — is fail-closed: it is
    re-derived FRESH from git via `classify_already_satisfied_landing`,
    never trusted stale, since a once-unreachable commit could have landed
    by another route since it was stamped.

    Root-cause incident this closes: an already-satisfied approval used to
    be marked DONE unconditionally, even when its satisfying commit was
    never pushed to (or merged into) the base branch — the deliverable sat
    stranded on the task branch while the task reported done.

    Returns ``{"decision": "done" | "land" | "refuse", "pr_url": str,
    "branch": str, "reason": str}``:

    - ``"done"``: truly nothing to land — the caller may mark the task DONE.
    - ``"land"``: the satisfying commit IS the deliverable, and now has a PR
      open on it (opened here, pushing ``branch``, if one did not already
      exist) — the caller must land it through its normal PR-merge
      (`land_task`) path, never mark done directly.
    - ``"refuse"``: could not verify, or could not open a PR — the caller
      must NOT mark the task done; ``reason`` explains why. The approval
      itself still stands; the task stays `awaiting_approval`.

    Never raises: a git/PR-open failure comes back as ``"refuse"``, not an
    exception — this function's job is to keep a human-facing approve
    command from crashing, not to propagate git plumbing errors.
    """
    landing = (task.context or {}).get("already_satisfied_landing") or {}
    sha = _clean(landing.get("sha"))
    branch = _clean(landing.get("branch"))

    if landing.get("on_base") is True:
        return {"decision": "done", "pr_url": "", "branch": branch,
                "reason": "satisfying commit reachable from base (nothing to land)"}

    if not repo_path:
        return {"decision": "refuse", "pr_url": "", "branch": branch,
                "reason": "no repo_path recorded — cannot verify the "
                          "satisfying commit against the base branch"}

    from .git import GitError, GitRepo

    try:
        repo = GitRepo(
            repo_path, identity_name=identity_name, identity_email=identity_email,
            never_push_to=never_push_to or ["main", "master", "release/*"],
        )
        repo.fetch()
    except (GitError, OSError) as exc:
        return {"decision": "refuse", "pr_url": "", "branch": branch,
                "reason": f"could not fetch the repo to verify: {exc}"}

    base = _clean((task.context or {}).get("base_branch"))
    verdict = classify_already_satisfied_landing(repo, sha=sha, branch=branch, base=base)

    if verdict.verdict == NOTHING_TO_LAND:
        # Backfill the flag so a re-approve (or another reader of this
        # context) never re-derives it from git again.
        await store.merge_context(task.id, {
            "already_satisfied_landing": {**landing, "on_base": True}})
        return {"decision": "done", "pr_url": "", "branch": branch,
                "reason": verdict.reason}

    if verdict.verdict == UNVERIFIABLE:
        return {"decision": "refuse", "pr_url": "", "branch": branch,
                "reason": verdict.reason}

    # LANDING_REQUIRED — the satisfying commit lives only on the task
    # branch and is the deliverable. Ensure a PR exists for it (open one if
    # not) so the caller's normal merge path has something to land, exactly
    # like a task that shipped a real diff would.
    if not branch:
        return {"decision": "refuse", "pr_url": "", "branch": "",
                "reason": "no branch recorded for the satisfying commit — "
                          "cannot open a PR to land it"}

    pr_url = await task_has_pr_evidence(store, task)
    if not pr_url:
        from . import open_pr as _open_pr
        try:
            pr = await asyncio.to_thread(
                _open_pr, repo, branch, task.title or task.id,
                "Already-satisfied claim confirmed by approve — landing "
                f"the satisfying commit{(' ' + sha[:12]) if sha else ''}, "
                "found only on the task branch, never merged into base.",
                base=base or "main", github_hosts=github_hosts)
        except Exception as exc:  # noqa: BLE001 — never crash approve
            return {"decision": "refuse", "pr_url": "", "branch": branch,
                    "reason": f"opening a PR to land {branch} failed: {exc}"}
        pr_url = pr.url
        await store.merge_context(task.id, {"pr_watch": pr_url, "pr_branch": branch})
        # `save_events`, NOT `set_status` — there is no status change here
        # (the task is already `awaiting_approval`), and a same-status
        # `set_status(..., event=...)` call silently drops the event
        # (`core/db.py::_write_status` only inserts when the status
        # actually changes). This is the append-only primitive that exists
        # precisely for "record an event, no status transition".
        await store.save_events(task.id, [{
            "source": "human", "kind": "pr_open", "text": pr_url,
            "pr_url": pr_url, "pr_kind": pr.kind, "ts": time.time(),
        }])

    return {"decision": "land", "pr_url": pr_url, "branch": branch,
            "reason": verdict.reason}


async def task_pr_urls(store: Any, task: Any) -> list[str]:
    """Every PR URL this task is known to have, deduped, minus
    ``abandoned_pr_urls`` — read-only, no network.

    ``resolve_task_pr``/``task_has_pr_evidence`` each answer "the ONE PR to
    look at", first non-empty rung wins. A completion closeout needs the
    OPPOSITE question — "every PR this task might still have open" — because
    a task can accumulate more than one live URL (e.g. ``pr_watch`` plus a
    separately recorded ``pr_draft`` event for an earlier attempt's PR that
    was never explicitly abandoned). This scans every source those two
    functions read, unions them, and returns all surviving URLs instead of
    stopping at the first.
    """
    ctx = task.context or {}
    abandoned = {_clean(u) for u in (ctx.get("abandoned_pr_urls") or [])}

    urls: list[str] = []
    for candidate in (
        _clean(ctx.get("pr_watch")),
        _clean(ctx.get("pr_delivered_url")),
        _clean(ctx.get("pr_draft_created")),
        _clean(await store.latest_attempt_pr_url(task.id)),
    ):
        if candidate:
            urls.append(candidate)

    for e in await store.list_events(task.id):
        if e.get("kind") not in _PR_EVENT_KINDS:
            continue
        url = _clean(e.get("pr_url")) or _clean(e.get("text"))
        if url:
            urls.append(url)

    seen: set[str] = set()
    out: list[str] = []
    for url in urls:
        if not url.startswith("http") or url in abandoned or url in seen:
            continue
        seen.add(url)
        out.append(url)
    return out
