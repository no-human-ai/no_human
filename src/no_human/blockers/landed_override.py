"""The human landed-override: an explicit human confirmation that completes
a task whose content landed via a path automated containment cannot verify.

Five eligible shapes, resolved by ``_resolve_shape``:

- ``"awaiting_approval"`` — a supervising session's squash train that a later
  train car's classification-decision edits, or a union-resolved real source
  conflict, leaves with no candidate commit whose tree matches the branch
  verbatim (see ``vcs/pr_watcher.py``'s ``_contained_at``/``default_branch_shipped``).
- ``"failed_pre_pr"`` — a task that died BEFORE ever opening a PR (budget
  exhaustion, a pre-review test failure, a compile error — any pre-PR cause)
  whose branch content a human later lands by hand. Live incident: task
  5b2246c1 review-PASSed head 9ba09affa, hit the lifetime budget cap before a
  PR ever opened, and the content was hand-landed at ca7bc32cf — leaving the
  board showing FAILED for shipped work, with neither `nh approve --landed`
  (required AWAITING_APPROVAL) nor `nh task restore-approval` (required PR
  evidence) able to repair it. This shape is narrowly gated — no
  `cancel_reason` (a human's explicit kill is never overridden) and no PR
  evidence (`vcs.task_pr.task_has_pr_evidence`) — so it stays confined to the
  actual gap instead of becoming a generic "force any failed task DONE"
  lever; a FAILED task that already opened a PR is refused here and pointed
  at the existing `nh task restore-approval` -> `nh approve --landed` pair.
- ``"pending_never_ran"`` — a task a supervising session hand-lands BEFORE
  any coder attempt ever dispatched: PENDING, no PR, no `base_branch` (the
  normal dispatch-time write, `Orchestrator._implicit_base_branch`, never
  ran either). Live incident: task 855f1263's fix was implemented and landed
  by the supervising session before the coder ran; `nh approve --landed`
  refused PENDING outright, and once refused ONE more time on "no recorded
  base_branch" — a task that never ran can never have recorded one. This
  shape, and ONLY this shape, accepts an explicit ``--base``/``base=`` from
  the human when none was ever recorded (see `approve_landed_override`'s
  base-resolution step) — it is NEVER guessed from git or a profile.
  (Amended below: ``--base`` now narrows the candidate check for every
  eligible shape, not only this one — but a shape that already recorded a
  base, or resolves a project default, still never needs ``--base`` to be
  rescued; this shape remains the one that has NOTHING to fall back on
  without it.) An
  earlier version of this fix DID auto-default from
  ``vcs/pr_watcher.resolve_default_branch``'s ``origin/HEAD`` probe, falling
  back to the checkout's own current branch when nothing configures
  ``origin/HEAD`` — which is every onboarded profile on record. That
  fallback is indistinguishable, at the call site, from a real project
  default: an independent fresh-context review of this very fix caught it as
  a second, quieter instance of the false-completion risk task 855f1263 was
  about (a wrong guessed base is worse than refusing), so it was replaced
  with a required, explicit human assertion — `resolve_default_branch` is
  now used only to compose a non-binding hint in the refusal message, never
  to fill in a value. This shape is gated exactly like `failed_pre_pr` on
  carrying no PR evidence (`task_has_pr_evidence`) — a pending task that
  already has a PR belongs to a different repair (or simply awaiting
  review), not this one — and additionally on carrying no pending
  cancellation request (`Store.get_cancel_request`): a cancel racing a
  hand-land must not be silently dropped by the override.
- ``"escalated_hand_landed"`` — a task that ESCALATED because an attempt
  stopped and asked a human instead of faking done, whose content that human
  then landed by hand. Escalating is the product working correctly, so there
  must be an honest terminal state for what happens next: today the only
  reachable one is `nh task cancel`, which writes FAILED with a
  `cancel_reason` — booking shipped, hand-landed work as a failure in the
  attempt-economics rows computed over that column. This shape is gated
  exactly like `failed_pre_pr` and `pending_never_ran`: no
  `context["cancel_reason"]`, no pending cancellation request
  (`Store.get_cancel_request`), and no PR evidence (`task_has_pr_evidence`,
  read fail-closed — an unreadable event log is refused, never treated as
  "no evidence"). An escalated task that still has an open PR is refused here
  and pointed at the existing `nh task restore-approval` -> `nh approve
  --landed` pair, exactly like the other PR-evidence refusals above.
- ``"done_no_evidence"`` — a task whose status is already DONE (the
  completion was real) but whose event log carries none of
  `vcs.task_pr.DONE_EVIDENCE_KINDS`, so `nh doctor` reports it as an evidence
  gap forever with no verb able to repair it — `nh approve --landed` refused
  it outright because `_resolve_shape` accepted only AWAITING_APPROVAL or a
  FAILED/PENDING task. Live incident: task 16f850ae's PR #230 was closed
  UNMERGED (2026-08-11T22:27:32Z), its content was hand-landed on `main` as
  commit 2a7495bf9 the same night, and `nh doctor` has reported the row as an
  evidence gap ever since. This shape is gated on carrying NONE of
  `DONE_EVIDENCE_KINDS` on record (`pr_open`, `approved_landed_override`,
  `human_merged`, `approved_already_satisfied`) — a DONE task that already
  has one of those kinds is refused, since re-asserting a landing over
  evidence that already stands is exactly what this verb must never allow.
  Because the `approved_landed_override` event this shape itself writes is
  one of the kinds it checks for, the repair is self-sealing against replay:
  the first call accepts, every later call on the same row refuses. Like
  `failed_pre_pr` and `pending_never_ran`, this shape is ALSO gated on
  carrying no PR evidence (`vcs.task_pr.task_has_pr_evidence`, broader than
  `DONE_EVIDENCE_KINDS` — it additionally catches a draft PR recorded only in
  `context["pr_draft_created"]`) and no pending cancellation request
  (`Store.get_cancel_request`): a DONE row with a still-open PR is not the
  no-evidence gap this shape repairs — landing it here would let
  `close_task_prs_on_completion` close out a PR nobody ever merged, and a
  cancel racing the hand-land must not be silently dropped by the override
  either. Unlike the other three shapes, this one's completion write bypasses
  `Store.set_status` entirely (see the dedicated comment at the call site) —
  the row is already DONE, so nothing about `task.status` changes; only the
  missing audit event is persisted.

Single shared implementation for both the CLI (`nh approve --landed`) and the
API (`POST /api/tasks/{id}/approve-landed`) — the exact validation and audit
event, so the two surfaces cannot drift.

**No git state is ever touched.** This module imports only read-only probes
(``pr_watcher.commit_is_ancestor``, ``pr_watcher.containment_residue``); it
never constructs ``vcs.git.GitRepo``, never calls ``vcs.approve_merge.land_task``,
never merges or pushes. The override is a human ASSERTION recorded on the
task, not a merge action — constraint #2 (the agent never merges) is
untouched because there is no merge here to begin with. Closing the task's
PR(s) after the DONE write (``pr_closeout.close_task_prs_on_completion``) is
not a merge or a push either — it changes no code and no state the forge
gates on, the same justification ``approve_merge._close_pr`` and the
abandon path already stand on.

This is a HUMAN override of automated containment, not a containment pass:
the audit event's text says so explicitly, and the event's ``kind`` —
``approved_landed_override`` — is deliberately distinct from every kind an
automated path can write (``shipped``, ``approved_landed``, ``human_merged``),
so a reader of the event log can always tell which class of evidence stands
behind a completion.

**Amended — the recorded base is a CANDIDATE, not the only accepted answer.**
A supervising session's squash train can leave a task's ``context["base_branch"]``
pointing at another task's stacked branch (dispatch-time recording of whatever
was live then), while the content genuinely lands on the repo's real default
branch. The original version of this module checked ancestry against exactly
the recorded ``base_branch`` and nothing else, so a task like that stayed
refused forever with no way to rescue it — ``--base`` was honoured only for
the ``pending_never_ran`` shape, which never had a recorded base to begin
with. ``approve_landed_override`` now tries every resolvable candidate branch
— the project's configured/declared default branch, the recorded
``base_branch``, and (when given) the human's ``--base`` — and accepts the
first one ``sha`` is an ancestor of, naming which one matched in the event,
the context, and the human-facing text. **One deliberate exception (F2,
independent review of d6249458f):** the default-branch candidate is a rescue
for a *wrong recorded* base, so it is only ever tried for ``awaiting_approval``
and ``failed_pre_pr`` — both shapes that always have a recorded base to begin
with. ``pending_never_ran`` never gets it, recorded or not: admitting it
there would quietly readmit the pre-855f1263 auto-guess this module exists to
refuse. ``--base`` NARROWS the candidate set to exactly itself, for every
shape, not just ``pending_never_ran``; it still never fills in a missing
recorded base by inference.

**What "must still name a branch" actually means (F3, same review).** Every
candidate — recorded, default, or ``--base`` — is checked with
``refs_resolvable`` before any ancestry work, and that check is honest about
what it accepts: any git commit-ish (a branch, a tag, or a bare commit sha),
not only a branch, despite the CLI help text and earlier revisions of this
docstring saying "branch." This is a pre-existing, unchanged property, not a
new widening, and it is not a hole in the refusal *by itself* — ancestry
still has to hold. But it does mean ``--base <sha>`` is a tautology if that
sha is (or descends from) the same ``--landed`` sha: a commit is its own
ancestor, so a human who fat-fingers the same value into both flags gets an
override that "passes" while asserting nothing. Nothing here silently
narrows what a legitimate ``--base <tag>`` or ``--base <sha>`` can do, so this
docstring records the tautology rather than closing it: **the CLI help text
was corrected to say so** (``cli/commands.py``'s ``--base`` option help), and
``--because`` (required on every call, this one included) is the only actual
control against it — a human still has to write down *why* they believe
whatever they typed.

**A narrower reopening of the same class of risk (F1, same review).**
``_preferred_ref_form`` (below) falls back from a bare candidate name to
``origin/<name>`` when the bare form does not resolve — see its own
docstring for why (a checkout whose local ``main`` is absent or stale). That
fallback hardcodes trust in a remote literally named ``origin``, whatever it
actually points to. ``_base_tips`` in ``vcs/pr_watcher.py`` deliberately does
**not** glob every ``refs/remotes/*/<name>`` for exactly this reason — an OSS
fork checkout where ``origin`` is the *contributor's* fork and the canonical
upstream lives under a differently-named remote would otherwise accept a sha
that only landed on the fork's copy of a branch name as if it had landed on
the real one (see that function's docstring for the full case). This
fallback reopens a narrower version of the same risk: it trusts a SPECIFIC
remote name rather than every remote, so a checkout with no remote literally
named ``origin`` gets no false positive from it, but a checkout with an
``origin`` that is itself a fork does. This is accepted, documented risk, not
an oversight fixed by this docstring — ``tests/test_landed_override.py``
pins the current (fork-accepting) behavior with a dedicated test rather than
silently relying on it holding.

None of the above weakens the refusal: a sha that is an ancestor of nothing
named above is still refused, and the refusal names every branch (and its
tip) that was tried, so a human can tell at a glance whether the tool checked
the right places.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from ..core.task import Task, TaskStatus
from ..vcs.pr_watcher import (
    commit_is_ancestor, containment_residue, ref_tip_sha, refs_resolvable,
    resolve_default_branch, resolve_project_default_branch,
)
from ..vcs.task_pr import DONE_EVIDENCE_KINDS, task_has_pr_evidence
from .pr_closeout import close_task_prs_on_completion
from .taxonomy import process_actor

LANDED_OVERRIDE_KIND = "approved_landed_override"

#: The ONE definition of which statuses the landed override can even consider.
#: `_resolve_shape` below owns the real eligibility decision (each status
#: carries its own guards); the API's cheap pre-filter imports this rather than
#: restating it, so the two layers cannot drift into two hand-maintained copies
#: (the defect this change fixes: adding a shape at one layer only moved the
#: refusal one layer deeper). Membership here is NECESSARY, never sufficient.
LANDED_OVERRIDE_ELIGIBLE_STATUSES: frozenset[TaskStatus] = frozenset({
    TaskStatus.AWAITING_APPROVAL, TaskStatus.FAILED, TaskStatus.PENDING,
    TaskStatus.DONE, TaskStatus.ESCALATED,
})


def ineligible_status_reason(status: TaskStatus) -> str:
    """The ONE wording used to refuse an ineligible status, at either layer."""
    return (
        f"task is {status.value!r}, not awaiting_approval, a pre-PR failed "
        "task, a never-dispatched pending task, an escalated task whose "
        "content a human landed by hand, or a done task with no completion "
        "evidence on record"
    )


IsAncestor = Callable[[str, str, str], Awaitable[bool]]
ResidueProbe = Callable[[str, str, str], Awaitable[list[str] | None]]


class OverrideRefused(Exception):
    """Raised when a landed-override request fails a precondition. The
    message (``.reason``) is safe to show a human verbatim — it never
    includes anything the caller did not already supply."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _resolve_shape(store: Any, task: Task) -> str:
    """Which eligible shape *task* is, or raise ``OverrideRefused``.

    Fail-closed, checked before any write: an ``AWAITING_APPROVAL`` task is
    the original shape, byte-for-byte unchanged. A ``FAILED`` task is the
    ``"failed_pre_pr"`` shape ONLY when a human never explicitly cancelled it
    (``context["cancel_reason"]``) and it never opened a PR
    (``task_has_pr_evidence`` — the one place that answers that question). A
    ``PENDING`` task (never dispatched) is the ``"pending_never_ran"`` shape
    ONLY when it never opened a PR (the same evidence gate — a pending task
    has no ``context["cancel_reason"]`` concept to begin with, since a human
    cancelling a PENDING task moves it straight to FAILED) and has no PENDING
    cancellation request either (``Store.get_cancel_request`` — the DB
    column a live cancel races through before the status flip lands). An
    ``ESCALATED`` task (an attempt stopped and asked a human) is the
    ``"escalated_hand_landed"`` shape ONLY when it was never explicitly
    cancelled (``context["cancel_reason"]``), has no pending cancellation
    request (``Store.get_cancel_request``), and never opened a PR
    (``task_has_pr_evidence``, read fail-closed — an unreadable event log is
    refused, never treated as "no evidence"). A
    ``DONE`` task is the ``"done_no_evidence"`` shape ONLY when it has no
    PENDING cancellation request (``Store.get_cancel_request`` — the same
    guard ``pending_never_ran`` uses; a cancel racing a hand-land must not be
    silently dropped by the override here either), carries NONE of
    ``DONE_EVIDENCE_KINDS`` on its event log, and carries no PR evidence
    either (``task_has_pr_evidence`` — broader than ``DONE_EVIDENCE_KINDS``:
    it also catches a draft PR recorded only in ``context["pr_draft_created"]``,
    which is not itself a ``DONE_EVIDENCE_KINDS`` event kind — a DONE task
    with a still-open, unclosed-out PR is not the no-evidence shape and must
    not have its PR silently closed by ``close_task_prs_on_completion``). A
    DONE task that already has one of ``DONE_EVIDENCE_KINDS`` on record
    (``pr_open``, ``approved_landed_override``, ``human_merged``,
    ``approved_already_satisfied``) is refused: re-asserting a landing over
    evidence that already stands is exactly what this verb must never allow,
    and it is what makes this shape self-sealing against replay (the event
    this shape itself writes, ``approved_landed_override``, is one of the
    kinds it checks for). Reading the event log fails CLOSED: an unreadable
    log is refused, never silently treated as "no evidence". Any other
    status, or a FAILED/PENDING/ESCALATED/DONE task that fails its evidence
    or cancel gate, is refused.
    """
    if task.status is TaskStatus.AWAITING_APPROVAL:
        return "awaiting_approval"

    if task.status is TaskStatus.FAILED:
        ctx = task.context or {}
        if ctx.get("cancel_reason"):
            raise OverrideRefused(
                "task was cancelled by a human — refusing")
        pr_url = await task_has_pr_evidence(store, task)
        if pr_url:
            raise OverrideRefused(
                f"task has a PR ({pr_url}) — this is not the pre-PR shape; "
                "use `nh task restore-approval` then `nh approve --landed`"
            )
        return "failed_pre_pr"

    if task.status is TaskStatus.PENDING:
        cancel_requested = await store.get_cancel_request(task.id)
        if cancel_requested:
            raise OverrideRefused(
                "task has a pending cancellation request "
                f"({cancel_requested!r}) — refusing"
            )
        pr_url = await task_has_pr_evidence(store, task)
        if pr_url:
            raise OverrideRefused(
                f"task is pending but has a PR ({pr_url}) — this is not the "
                "never-ran shape; use `nh task restore-approval` then "
                "`nh approve --landed`"
            )
        return "pending_never_ran"

    if task.status is TaskStatus.ESCALATED:
        # An attempt stopped and asked a human instead of faking done — the
        # product working correctly. When that human then lands the content by
        # hand there must be a way to say so; today the only reachable terminal
        # state is `nh task cancel`, which writes FAILED with a cancel_reason and
        # so books shipped work as a failure in the attempt-economics rows.
        if (task.context or {}).get("cancel_reason"):
            raise OverrideRefused("task was cancelled by a human — refusing")
        cancel_requested = await store.get_cancel_request(task.id)
        if cancel_requested:
            raise OverrideRefused(
                "task has a pending cancellation request "
                f"({cancel_requested!r}) — refusing")
        # LOAD-BEARING: `task_has_pr_evidence` reads the event log as its last
        # rung and does NOT catch. Unwrapped, an unreadable log escapes as a raw
        # exception (API 500, CLI traceback) instead of a refusal — and must
        # never read as "no PR evidence". Do not remove this wrapper.
        try:
            pr_url = await task_has_pr_evidence(store, task)
        except Exception as exc:  # noqa: BLE001 — fail-closed, never a pass
            raise OverrideRefused(
                f"could not read this task's event log ({type(exc).__name__}) "
                "— refusing (the PR-evidence gate must fail closed)") from exc
        if pr_url:
            raise OverrideRefused(
                f"task is escalated but has a PR ({pr_url}) — this is not the "
                "hand-landed shape; use `nh task restore-approval` then "
                "`nh approve --landed`")
        return "escalated_hand_landed"

    if task.status is TaskStatus.DONE:
        cancel_requested = await store.get_cancel_request(task.id)
        if cancel_requested:
            raise OverrideRefused(
                "task has a pending cancellation request "
                f"({cancel_requested!r}) — refusing"
            )
        try:
            events = await store.list_events(task.id)
        except Exception as exc:  # noqa: BLE001 — fail-closed, never a pass
            raise OverrideRefused(
                f"could not read this task's event log ({type(exc).__name__}) "
                "— refusing (the no-evidence repair must fail closed)"
            ) from exc
        standing = sorted({e.get("kind") for e in events} & DONE_EVIDENCE_KINDS)
        if standing:
            raise OverrideRefused(
                "task is already done with completion evidence on record "
                f"({'/'.join(standing)}) — re-asserting a landing over "
                "evidence that already stands is refused"
            )
        pr_url = await task_has_pr_evidence(store, task)
        if pr_url:
            raise OverrideRefused(
                f"task has a PR ({pr_url}) that was never closed out — this "
                "is not the no-evidence shape; use `nh task restore-approval` "
                "then `nh approve --landed`"
            )
        return "done_no_evidence"

    raise OverrideRefused(ineligible_status_reason(task.status))


async def _base_hint(task: Task) -> str:
    """Best-effort, NON-BINDING text appended to a refusal message ONLY —
    this is never used to fill in a ``base`` value automatically.

    An earlier version of this fix used ``resolve_default_branch`` (and a
    profile's ``default_branch``) as an authoritative default for the
    ``pending_never_ran`` shape. Independent review found that, in practice,
    every onboarded profile on this product has an empty ``default_branch``
    and nothing ever calls ``git remote set-head``, so
    ``resolve_default_branch`` always fell through to the checkout's own
    *current* branch — the parked branch of whatever repo happened to be
    checked out, not the project's real default. That made the base
    unreliable exactly where the ancestor check depends on it being
    trustworthy (point 5 below), so it is never used as a value here — only
    as advisory text a human can sanity-check before typing ``--base``.
    """
    try:
        guess = (await resolve_default_branch(task.repo_path) or "").strip()
    except Exception:  # noqa: BLE001 — advisory text, never load-bearing
        guess = ""
    if not guess:
        return " — pass --base <branch> (the tool will not guess one)"
    return (
        " — pass --base <branch> (hint: this repo's checkout currently "
        f"reports {guess!r}, which may or may not be the project's real "
        "default — confirm before using it)"
    )


async def _resolve_default_branch_value(store: Any, task: Task) -> str:
    """A default-branch CANDIDATE for the ancestry check — never a value
    filled into ``base``, only one more branch name tried alongside it.

    Order: the project profile's configured ``default_branch``, then the
    repo's own declared remote default (``origin/HEAD``, strict — see
    ``pr_watcher.resolve_project_default_branch``, which has **no**
    current-branch fallback, unlike ``resolve_default_branch`` above, which
    this module keeps using only for non-binding hint text). Any failure (no
    profile on record, an unreadable repo) folds to ``""``, and the caller
    simply has one fewer candidate to try — this never raises and never
    considers the checkout's current branch, for the same reason point 4 of
    ``approve_landed_override``'s docstring gives.
    """
    prof = None
    try:
        prof = await store.get_profile(task.repo_path)
    except Exception:  # noqa: BLE001 — best-effort; falls through below
        prof = None
    # str() first: a YAML `default_branch: yes` arrives as bool True and
    # `.strip()` on it is an AttributeError (the same trap
    # `Orchestrator._implicit_base_branch` guards against).
    configured = (
        str(getattr(prof, "default_branch", "") or "") if prof else ""
    ).strip()
    if configured:
        return configured
    try:
        return (await resolve_project_default_branch(task.repo_path) or "").strip()
    except Exception:  # noqa: BLE001 — never load-bearing enough to raise
        return ""


async def _preferred_ref_form(
    repo_path: str, name: str, *,
    sha: str | None = None,
    is_ancestor: "IsAncestor | None" = None,
) -> str:
    """The form of *name* — bare, or ``origin/<name>`` — that ``git`` can
    actually resolve to a commit in *repo_path*. Falls back to *name*
    unchanged when NEITHER form resolves, so an unresolvable candidate still
    surfaces under its original name in refusal text and fails closed
    through ``is_ancestor``'s own git-error handling, exactly as before this
    helper existed.

    Every candidate branch name reaching this module — the repo's declared
    default (``resolve_project_default_branch``, always a BARE name split
    off ``origin/HEAD``), the task's recorded ``base_branch``, and a human's
    ``--base`` — is only ever checked for ancestry through
    ``commit_is_ancestor`` -> ``_base_tips``, which resolves a *bare*
    branch's remote-tracking counterpart solely via ``<bare>@{upstream}``,
    itself defined only when a LOCAL branch of that name exists. A checkout
    whose local ``main`` is simply ABSENT — the normal shape once a task's
    content is squash-landed and pushed from a throwaway worktree, advancing
    ``refs/remotes/origin/main`` while no local ``main`` is ever fetched
    into this checkout — leaves the bare name unresolvable, and
    ``_base_tips`` has nothing else to fall back to (it does not glob
    ``refs/remotes/*/<base>``, by design — see its own docstring). Handing
    it the ``origin/<name>`` form directly sidesteps that gap without
    touching ``_base_tips`` itself. Bare is preferred when it resolves, so
    an ordinary local branch keeps reading as itself in messages and events.

    **F1 (independent review of d6249458f): this hardcodes trust in a remote
    literally named ``origin``, whatever it points to.** ``_base_tips``
    refuses to glob every ``refs/remotes/*/<name>`` specifically to avoid an
    OSS-fork checkout (``origin`` = the contributor's fork, canonical
    upstream under another remote name) accepting a sha that only landed on
    the fork's copy of a branch name. Trusting ``origin/<name>`` by name
    alone here reopens a narrower version of that same risk — see the module
    docstring's "Amended" section for the full accounting and why this is
    documented rather than closed.

    A local ``main`` can also be STALE rather than absent — present, with no
    upstream configured, sitting behind the commit that actually landed (a
    long-lived checkout that never re-fetched). ``refs_resolvable`` only
    proves the bare name exists, never that it is caught up, so blindly
    preferring bare would keep silently losing ancestry against a
    perfectly-good ``origin/<name>`` in that shape. When *sha* and
    *is_ancestor* are both supplied, a bare ref that resolves but whose tip
    does NOT contain *sha* as an ancestor is no longer preferred
    unconditionally: if ``origin/<name>`` also resolves AND *does* satisfy
    ancestry, that form is returned instead, so ``matched_branch`` names the
    ref that actually vouches for ``sha``. Callers that omit *sha*/
    *is_ancestor* keep the exact original existence-only preference (no new
    git calls, no behavior change) — this keeps the helper's contract
    backward compatible for any caller that only wants a resolvable name,
    not an ancestry-checked one.
    """
    if not name:
        return name
    origin_form = f"origin/{name}"
    if await refs_resolvable(repo_path, name):
        if sha and is_ancestor is not None:
            try:
                bare_ok = await is_ancestor(repo_path, sha, name)
            except Exception:  # noqa: BLE001 — fail closed, fall through to bare
                bare_ok = True
            if not bare_ok and await refs_resolvable(repo_path, origin_form):
                try:
                    origin_ok = await is_ancestor(repo_path, sha, origin_form)
                except Exception:  # noqa: BLE001 — fail closed, keep bare
                    origin_ok = False
                if origin_ok:
                    return origin_form
        return name
    if await refs_resolvable(repo_path, origin_form):
        return origin_form
    return name


_CANDIDATE_ROLE = {
    "recorded": "the task's recorded base",
    "default_branch": "the repo's default branch",
    "human_asserted": "the branch you named with --base",
}

#: Same roles, phrased for the human-facing completion text rather than a
#: refusal — see point 6 of ``approve_landed_override``'s docstring.
_MATCHED_ROLE_TEXT = {
    "recorded": "the task's recorded base branch",
    "default_branch": "the repo's default branch",
    "human_asserted": "a branch the human named with --base",
}


async def _candidate_phrase(repo_path: str, branch: str, source: str) -> str:
    tip = await ref_tip_sha(repo_path, branch)
    tip_text = f"tip {tip}" if tip else "tip unknown"
    return f"{_CANDIDATE_ROLE[source]} {branch} ({tip_text})"


async def _refusal_text(
    repo_path: str, sha: str, candidates: list[tuple[str, str]],
) -> str:
    """The widened refusal: names every candidate branch that was actually
    tried (and its tip), not just one. Message order favors how a human
    reads it (recorded base first, then the default branch) — independent of
    the priority order the ancestry check itself uses."""
    ordered = list(reversed(candidates)) if len(candidates) > 1 else candidates
    phrases = [await _candidate_phrase(repo_path, b, s) for b, s in ordered]
    joined = " or ".join(phrases)
    return (
        f"{sha} is not an ancestor of {joined} — refusing. If it landed "
        "somewhere else, re-run with --base <branch> (still requires "
        "--because)."
    )


async def approve_landed_override(
    store: Any, task: Task, sha: str, justification: str, *,
    is_ancestor: IsAncestor | None = None,
    residue_probe: ResidueProbe | None = None,
    base: str | None = None,
    human: str = "human",
) -> dict[str, Any]:
    """Validate and record a human landed-override, or raise ``OverrideRefused``.

    Every check below runs — and can refuse — BEFORE any write. Order:

    1. ``task.status`` must resolve to an eligible shape (``_resolve_shape``):
       ``AWAITING_APPROVAL``, a ``FAILED`` task that was neither
       human-cancelled nor ever opened a PR (the pre-PR shape), a
       ``PENDING`` task that never opened a PR (the never-ran shape), an
       ``ESCALATED`` task that was neither human-cancelled (nor has a pending
       cancellation request) nor ever opened a PR (the hand-landed shape), or
       a ``DONE`` task that carries none of ``vcs.task_pr.DONE_EVIDENCE_KINDS``
       on its event log (the no-evidence shape — a DONE task that already
       has one of those kinds is refused).
    2. ``justification`` must not be blank.
    3. ``sha`` must not be blank.
    4. ``task.repo_path`` must be recorded. A CANDIDATE branch list is then
       built — never a single ``base`` value picked in advance:
       * When the caller passes ``base=`` (CLI: ``--base``; API: the request
         body's ``base`` field), it must first name a branch that actually
         resolves in this repo (``refs_resolvable``, tried both bare and as
         ``origin/<name>`` — never guessed, never inferred from the
         checkout's current branch); if it does not resolve, this refuses
         immediately, before any ancestry work. A resolvable ``--base``
         NARROWS the candidate list to exactly itself, for every eligible
         shape — not only ``pending_never_ran`` as an earlier version of this
         function required. This is still a required human ASSERTION, never
         a default: omitting it never fills one in.
       * Otherwise, for the ``awaiting_approval`` and ``failed_pre_pr``
         shapes, the candidates are the project's default branch (project
         profile's configured ``default_branch``, else the repo's declared
         ``origin/HEAD`` — see ``_resolve_default_branch_value``) and
         ``task.context["base_branch"]`` (the recorded base), whichever of
         the two resolve, with the default branch tried first when both are
         present and differ. Every candidate — ``--base``, the default
         branch, and the recorded base alike — is stored in whichever of its
         bare or ``origin/<name>`` form ``_preferred_ref_form`` finds
         resolvable, since the ancestry check below only walks a bare
         branch's remote-tracking counterpart when a LOCAL branch of that
         name exists; a checkout with no local ``main`` but a current
         ``refs/remotes/origin/main`` needs the latter form or the check
         never resolves anything to compare against.
         For ``pending_never_ran`` **the default-branch candidate is never
         tried** — this is the one deliberate exception, not an oversight:
         the shape has no recorded base to rescue with it (its whole
         definition is that dispatch never ran), so admitting the project's
         default branch here would silently resurrect the pre-855f1263
         auto-guess this module was rewritten to remove (see the shape's own
         docstring above). Without ``--base``, this shape therefore always
         refuses (`F2, independent review of d6249458f — a resolvable
         default branch used to be accepted here with no explicit
         assertion at all; it no longer is`), with a non-binding hint
         (``_base_hint``) naming what the checkout's current branch happens
         to be — never a value the call trusts. The other two shapes keep
         the strict "a recorded base or nothing" rule unconditionally when
         neither the default branch nor the recorded base resolves (the same
         rule ``complete_if_content_landed`` documents: a wrong guessed base
         is worse than refusing).
    5. ``sha`` must be an ancestor of the FIRST candidate it matches (local
       git only, ``commit_is_ancestor`` — ``git merge-base --is-ancestor``,
       fail-closed on any git error or exception: a probe failure counts as
       "did not match", never a pass). This is what discharges "refused for
       tasks whose content is NOT landed" for every shape: a branch that
       never reached any candidate has no ancestor sha to name anywhere. If
       ``sha`` is an ancestor of none of the candidates tried, the refusal
       names every one of them — branch and tip — so a human can tell at a
       glance whether the right places were even checked, and can retry with
       an explicit ``--base`` if the content landed somewhere else (still
       requires ``--because``). For ``pending_never_ran`` with no other
       candidate, this check is only as trustworthy as the explicit ``base=``
       itself, which is exactly why point 4 never lets it be guessed: it is
       always either a value a real prior dispatch recorded, the repo's own
       declared default, or a branch the human typed explicitly for this
       call — never a checkout's incidental current branch.
    6. Whichever candidate ``sha`` matched is recorded as the MATCHED branch
       — in the human-facing confirmation text (naming its role: recorded
       base, default branch, or human-asserted), in the completion event
       (``base``, ``base_source``, and ``matched_branch`` — the last one
       added alongside the other two, never replacing them), and in
       ``task.context`` (``landed_override_base`` / ``landed_override_base_source``
       always; ``base_branch`` itself only when this call is the first thing
       to ever record one — narrowing an already-recorded base with
       ``--base`` never overwrites the original recorded value).

    Only once every check above has passed does this write anything: it
    records ``landed_override_sha`` (deliberately a DIFFERENT context key
    from ``landed_sha`` — a human assertion must never later be re-read by
    the watcher as a machine-verified landing) and completes the task with
    an ``approved_landed_override`` event carrying the sha, the justification
    verbatim, the resolved shape, an ``equivalence`` verdict, and the
    containment residue at that sha — best-effort: a residue-probe failure
    records ``residue: None`` and a note, and never turns the override into a
    refusal or a pass; the whole point of this class of task is that
    automated containment already refused (or, for the pre-PR shape, never
    ran at all).

    Returns ``{"sha": ..., "residue": ..., "text": ..., "shape": ...,
    "prior_status": ..., "matched_branch": ..., "base_source": ...}`` for the
    caller's own message/response formatting — ``matched_branch`` is the
    branch ``sha`` was actually found to be an ancestor of (see point 6
    above), and ``base_source`` names its role (``"recorded"``,
    ``"default_branch"``, or ``"human_asserted"``).
    """
    is_ancestor = is_ancestor or commit_is_ancestor
    residue_probe = residue_probe or containment_residue

    shape = await _resolve_shape(store, task)
    prior_status = task.status.value

    justification = (justification or "").strip()
    if not justification:
        raise OverrideRefused("justification must not be empty")

    sha = (sha or "").strip()
    if not sha:
        raise OverrideRefused("sha must not be empty")

    base_override = (base or "").strip()

    if not task.repo_path:
        raise OverrideRefused("task has no recorded repo_path — refusing")

    ctx = task.context or {}
    recorded_base = ctx.get("base_branch")

    if base_override:
        resolvable = (
            await refs_resolvable(task.repo_path, base_override)
            or await refs_resolvable(task.repo_path, f"origin/{base_override}")
        )
        if not resolvable:
            raise OverrideRefused(
                f"--base {base_override!r} does not name a branch in this "
                "repo — refusing"
            )
        # Resolve to whichever form (bare vs `origin/<name>`) `is_ancestor`
        # can actually walk — `refs_resolvable` above only proved ONE of the
        # two forms exists, and `commit_is_ancestor` needs the resolving one,
        # not necessarily the bare one the human typed. Passing `sha`/
        # `is_ancestor` also lets a STALE bare ref (present, but behind
        # `sha`, no upstream) defer to `origin/<name>` when that form is the
        # one that actually vouches for `sha` — see `_preferred_ref_form`'s
        # docstring.
        resolved_override = await _preferred_ref_form(
            task.repo_path, base_override, sha=sha, is_ancestor=is_ancestor)
        candidates: list[tuple[str, str]] = [(resolved_override, "human_asserted")]
    else:
        default_branch_raw = await _resolve_default_branch_value(store, task)
        default_branch = (
            await _preferred_ref_form(
                task.repo_path, default_branch_raw, sha=sha, is_ancestor=is_ancestor)
            if default_branch_raw else ""
        )
        recorded_base_form = (
            await _preferred_ref_form(
                task.repo_path, recorded_base, sha=sha, is_ancestor=is_ancestor)
            if recorded_base else recorded_base
        )
        # F2 (independent review of d6249458f): the default-branch candidate
        # is a RESCUE for a task that already has *some* recorded base but
        # points it at the wrong branch (a supervising session's stacked
        # train, see the module docstring's "Amended" section) — it must
        # never become a second, silent way to auto-fill a base for
        # `pending_never_ran`, which by definition has no recorded base to
        # rescue. Before this guard, a project with a resolvable default
        # branch (profile-configured, or a real `origin/HEAD`) let a
        # `pending_never_ran` task complete against it with no `--base` at
        # all — reopening, in a new shape, exactly the false-completion risk
        # task 855f1263's fix removed (see the shape's own docstring above:
        # "this shape, and ONLY this shape, accepts an explicit --base ...
        # it is NEVER guessed from git or a profile").
        candidates = []
        if shape != "pending_never_ran":
            if default_branch and default_branch == recorded_base_form:
                candidates.append((recorded_base_form, "recorded"))
            else:
                if default_branch:
                    candidates.append((default_branch, "default_branch"))
                if recorded_base_form:
                    candidates.append((recorded_base_form, "recorded"))
        elif recorded_base_form:
            # Unreachable today (this shape never has a recorded base — see
            # `_seed_pending` in tests/test_landed_override.py) but kept for
            # defense in depth rather than assumed: if a recorded base ever
            # exists for this shape, it is still eligible on its own merits,
            # exactly like the other two shapes.
            candidates.append((recorded_base_form, "recorded"))

    if not candidates:
        hint = await _base_hint(task) if shape == "pending_never_ran" else ""
        raise OverrideRefused(
            "task has no recorded base_branch — refusing" + hint)

    matched_branch: str | None = None
    base_source: str | None = None
    for cand_branch, cand_source in candidates:
        try:
            matched = await is_ancestor(task.repo_path, sha, cand_branch)
        except Exception:  # noqa: BLE001 — fail-closed, never a pass
            matched = False
        if matched:
            matched_branch, base_source = cand_branch, cand_source
            break

    if matched_branch is None:
        raise OverrideRefused(
            await _refusal_text(task.repo_path, sha, candidates))

    base = matched_branch

    branch = ctx.get("pr_branch") or ctx.get("pr_draft_branch") or ""
    if not branch and shape in ("failed_pre_pr", "escalated_hand_landed"):
        try:
            row = await store.latest_attempt_branch(task.id)
            branch = row.get("branch") or row.get("commit_sha") or ""
        except Exception:  # noqa: BLE001 — metadata read, never a crash
            branch = ""

    residue: list[str] | None
    residue_note: str | None = None
    try:
        residue = await residue_probe(task.repo_path, sha, branch)
    except Exception:  # noqa: BLE001 — best-effort audit data, never a gate
        residue = None
    if residue is None:
        residue_note = "could not be computed"

    equivalence = (
        "content_equivalent" if residue == [] else
        "asserted" if residue is None else
        "asserted_with_residue"
    )

    ts_iso = _now_iso()
    context_patch = {
        "landed_override_sha": sha, "approved_at": ts_iso,
        "landed_override_base": base,
        "landed_override_base_source": base_source,
    }
    if base_source == "human_asserted" and not recorded_base:
        context_patch["base_branch"] = base
    await store.merge_context(task.id, context_patch)

    sha12 = sha[:12]
    residue_text = ", ".join(residue) if residue else (
        residue_note or "none (fully contained)")
    text = (
        "HUMAN OVERRIDE of automated containment (not a containment pass): "
        f"a human asserts this task's content landed at {sha12} on {base} "
        f"({_MATCHED_ROLE_TEXT[base_source]}). "
        f"Automated containment refused; residue at that commit: {residue_text}. "
        f"Justification: {justification}"
    )
    if shape == "failed_pre_pr":
        text += " prior status: failed (no PR was ever opened)."
    elif shape == "pending_never_ran":
        text += " prior status: pending (no attempt ever ran)."
    elif shape == "escalated_hand_landed":
        text += (
            " prior status: escalated (an attempt stopped and asked a "
            "human, who landed the content by hand)."
        )
    elif shape == "done_no_evidence":
        text += (
            " prior status: done (the completion was real; only its "
            "evidence event was missing)."
        )
    event = {
        "source": human,
        "kind": LANDED_OVERRIDE_KIND,
        "actor": process_actor(),
        "sha": sha,
        "justification": justification,
        "residue": residue,
        "base": base,
        "base_source": base_source,
        "matched_branch": base,
        "branch": branch,
        # unix float, same clock every other emitter uses for task_events.ts
        # (a REAL column) — context["approved_at"] above stays ISO for the
        # drawer; only this event-level clock must match the column's type.
        "ts": time.time(),
        "text": text,
        "shape": shape,
        "equivalence": equivalence,
        "prior_status": prior_status,
    }
    if residue_note is not None:
        event["residue_note"] = residue_note

    if shape == "done_no_evidence":
        # `Store.set_status` deliberately drops the event when
        # `task.status is new_status` (the `already_there` guard in
        # `core/db.py` — it exists so a second `set_status(DONE)` call never
        # double-records a real completion). This task's row is ALREADY
        # DONE, so routing through `set_status` here would silently swallow
        # the very audit event this repair exists to write, leaving `nh
        # doctor` flagging the row forever — the exact defect this shape
        # fixes. Persist the event directly instead; the status column needs
        # no write (the row is already correct). Do not "simplify" this back
        # into the shared `set_status` call below.
        await store.save_events(task.id, [event])
    else:
        moved = await store.set_status(
            task, TaskStatus.DONE, validate=False, event=event)
        if moved is None:
            raise OverrideRefused(
                "the store refused the transition — nothing was recorded")
    await close_task_prs_on_completion(
        store, task, completion_path=LANDED_OVERRIDE_KIND)

    return {
        "sha": sha, "residue": residue, "text": text,
        "shape": shape, "prior_status": prior_status,
        "matched_branch": base, "base_source": base_source,
    }
