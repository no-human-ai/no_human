"""What actually HAPPENED to the PR — the question the success metric never asked.

A task reached ``AWAITING_APPROVAL``/``DONE``. That is all "success" has ever
meant here (``core/autonomy.py``: ``_PR_REACHED_STATUSES`` is the whole
predicate, and it contains no forge query). This module records the forge's
answer to the next question — did the PR merge, was it closed unmerged, is it
still open, or does nobody know — into the ``pr_outcomes`` table.

AND IT DOES NOT EVEN IMPLY A PR EXISTS. The first version of this sentence said
"A task that reached AWAITING_APPROVAL/DONE opened a pull request", which is
false and was the very claim this work was commissioned to retire — stated here
as a flat premise while the same change corrected it in ``core/autonomy.py`` and
on ``nh autonomy``. Three orchestrator paths reach ``AWAITING_APPROVAL`` and
only one of them opens a PR; the "already satisfied, no code change needed" path
and the code-review path (which ends with draft comments awaiting approval, none
posted) both reach it with no pull request at all. So this module's population
is "tasks the pipeline settled", not "pull requests", and the two denominators
must never be blended.

WHAT "MERGED" DOES AND DOES NOT ESTABLISH
-----------------------------------------
Read ``MERGED_CAVEAT`` below before quoting any number this module produces. It
is printed verbatim next to every rendered figure, because the failure this
whole module exists to correct — measuring the thing that is easy to compute
instead of the thing that was asked — is available one level up just as easily.

DELEGATION, NOT REIMPLEMENTATION
--------------------------------
Every forge query here is ``vcs.pr_watcher``'s: ``default_pr_state``,
``default_pr_checks``, ``default_branch_shipped``, ``refs_resolvable``,
``parse_pr_url``. Nothing in this file talks to ``gh``/``glab`` or shells out
to git, and the classification below is the only new logic. Two copies of a
forge client is how a forge client drifts.

``refs_resolvable`` was ADDED to ``pr_watcher`` rather than written here, for
the same reason: it is a git probe, git probes live there next to ``_git_rc``,
and a second module growing its own ``git rev-parse`` is the first step of the
drift this rule prevents.

THE "CLOSED" TRAP, and why it needed the shipped probe
------------------------------------------------------
GitHub reports ``MERGED`` only when the merge happened THROUGH GitHub. The
operator's hard rule is a local, identity-normalized squash merge (never
``gh pr merge``), so a squash commit lands on base with a fresh SHA and no
lineage back to the branch — and every successfully shipped PR still reports
``CLOSED``. ``wake.py``'s state rung already learned this the expensive way
(SCRUM-68 follow-up: trusting the flag escalated every successful task). A
classifier that mapped ``CLOSED`` to ``closed_unmerged`` would therefore report
approximately zero merges on the operator's own repos and be WRONG in the same
shape as the metric it replaces. So ``CLOSED`` is resolved by asking git — not
GitHub — whether the branch's content is present on its base, via
``pr_watcher.default_branch_shipped``; and when that probe is unavailable the
answer is ``unknown``, never ``closed_unmerged``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Iterable, Sequence

from .pr_watcher import default_branch_shipped, parse_pr_url, refs_resolvable

log = logging.getLogger("no_human.pr_outcome")


# --------------------------------------------------------------------------- #
# The vocabulary. Four values, and the fail-state is UNKNOWN.
# --------------------------------------------------------------------------- #

MERGED = "merged"
CLOSED_UNMERGED = "closed_unmerged"
OPEN = "open"
UNKNOWN = "unknown"

#: Every legal value of ``pr_outcomes.outcome``. Order is report order: the
#: two settled outcomes, the one still in flight, then the one that means
#: "not measured".
OUTCOMES: tuple[str, ...] = (MERGED, CLOSED_UNMERGED, OPEN, UNKNOWN)

CI_PASS = "pass"
CI_FAIL = "fail"
CI_PENDING = "pending"
CI_UNKNOWN = "unknown"

CI_STATUSES: tuple[str, ...] = (CI_PASS, CI_FAIL, CI_PENDING, CI_UNKNOWN)

#: How the row was obtained. ``BACKFILL`` marks a row reconstructed after the
#: fact; it is never silently equivalent to a live observation.
SOURCE_LIVE = "live"
SOURCE_BACKFILL = "backfill"

# Evidence tokens — WHY an outcome says what it says. A verdict with no
# evidence cannot be audited, and three of these exist only because a forge
# "CLOSED" is genuinely ambiguous in this repo.
EVIDENCE_FORGE_MERGED = "forge_merged"
EVIDENCE_CONTENT_ON_BASE = "content_on_base"
EVIDENCE_CLOSED_CONTENT_ABSENT = "forge_closed_content_absent"
EVIDENCE_CLOSED_SHIP_UNVERIFIED = "forge_closed_ship_unverified"
EVIDENCE_FORGE_OPEN = "forge_open"
EVIDENCE_STATE_UNAVAILABLE = "forge_state_unavailable"
EVIDENCE_OPENED_BY_AGENT = "pr_opened_by_agent"
EVIDENCE_NO_FORGE = "local_remote_no_forge"
EVIDENCE_NOT_RECORDED = "not_recorded"

#: THE HONEST STATEMENT. Rendered verbatim beside every figure derived from
#: this module — in `nh pr-outcomes show`, in `nh bench report`'s new section,
#: and in the tracked benchmark report. Kept as one string with one owner so
#: the caveat cannot be dropped from one surface and kept on another.
MERGED_CAVEAT = (
    "`merged` means a human accepted the change into the base branch. It does "
    "NOT establish quality, and it does NOT establish that no_human's output "
    "was what merged: a human may have reviewed, corrected, rebased or "
    "rewritten the PR before merging, and none of that is visible here. It "
    "also says nothing about cost. This number answers 'did the work land', "
    "which is strictly more than 'did a PR exist' and strictly less than the "
    "north star's 'success at high quality and low cost'."
)

#: The second half of the honesty statement — what an `unknown` is.
UNKNOWN_CAVEAT = (
    "`unknown` means the outcome could not be determined (no forge for this "
    "remote, no `gh` on this machine, no network, an unparseable PR ref, or a "
    "row that predates this telemetry). It is NEVER counted as a success and "
    "never guessed at; it is reported as its own column so a low merge rate "
    "and an unmeasured one cannot be confused."
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# Classification — pure functions, no I/O.
# --------------------------------------------------------------------------- #

def classify_outcome(forge_state: str | None, *,
                     shipped: bool | None = None) -> tuple[str, str]:
    """``(outcome, evidence)`` from a forge state, plus an optional ship probe.

    ``forge_state`` is exactly what ``pr_watcher.default_pr_state`` returns:
    ``"MERGED" | "CLOSED" | "OPEN" | ""``, where ``""`` is *unknown* (no ``gh``,
    unparseable ref, network error). Anything unrecognised is also unknown —
    a state this function does not understand is not evidence of a fate.

    ``shipped`` is a THREE-valued answer about the PR's branch: ``True`` (its
    content is on base), ``False`` (it is genuinely not), ``None`` (nobody could
    tell).

    A ``True`` is decisive on its OWN, whatever the forge state — that is not a
    widening, it is the rule stated four paragraphs down and always relied on
    here: "a false ``merged`` needs the forge's own merged flag or a positive
    content match". The probe merges the branch into the base tip in BOTH
    directions and demands the tip's exact tree; nothing about that answer
    depends on what the forge says, and the forge routinely lags it. This repo
    lands PRs as a LOCAL squash pushed straight to the base, so the content is
    on base BEFORE the PR is closed — ``wake.py``'s CONFLICTING rung completes
    tasks in exactly that window (review finding E, 2026-08-11). Dropping the
    probe for ``OPEN`` filed those landings as ``open`` permanently, since
    ``refresh_outcomes`` re-probes containment only for ``CLOSED`` rows.

    ``False`` and ``None`` are NOT symmetric with it and never settle anything
    on their own: ``False`` is the documented "absent OR could not run"
    collapse, so it settles a PR as ``closed_unmerged`` only when the forge has
    independently said ``CLOSED``. The forge's own merged flag still outranks
    the probe for PROVENANCE (a ``MERGED`` state reports ``forge_merged``, not
    ``content_on_base``), so the evidence token always names what was actually
    observed.

    🔴 CALLERS MUST NOT PASS ``default_branch_shipped``'s RAW BOOLEAN HERE.
    That function is documented to return ``False`` for BOTH "the content is
    not on base" and "the check could not run", and it says so explicitly:
    "callers must treat False as 'can't tell'". Feeding its raw ``False`` in
    would produce ``closed_unmerged`` — a SETTLED outcome, never re-polled —
    from a git command that merely failed. The most common reason it fails is
    that the branch no longer exists, which is the normal state of affairs
    AFTER a successful squash merge, so the error would concentrate on the PRs
    that did land and invert the very number this module computes.

    Use ``probe_shipped`` below, which resolves the refs first and returns
    ``None`` rather than ``False`` when it cannot see them. ``wake.py`` passes
    ``True``-or-``None`` for the same reason. With that discipline both
    directions are honest: a false ``merged`` needs the forge's own merged flag
    or a positive content match, and a false ``closed_unmerged`` needs a probe
    that resolved both refs and still found the content absent.
    """
    state = (forge_state or "").strip().upper()
    if state == "MERGED":
        return MERGED, EVIDENCE_FORGE_MERGED
    if state == "CLOSED":
        if shipped is True:
            return MERGED, EVIDENCE_CONTENT_ON_BASE
        if shipped is False:
            return CLOSED_UNMERGED, EVIDENCE_CLOSED_CONTENT_ABSENT
        return UNKNOWN, EVIDENCE_CLOSED_SHIP_UNVERIFIED
    if shipped is True:
        # OPEN, or a state that could not be read at all. Positive containment
        # is its own evidence and outlives a stale or missing forge answer;
        # `False`/`None` fall through, because neither is evidence of absence.
        return MERGED, EVIDENCE_CONTENT_ON_BASE
    if state == "OPEN":
        return OPEN, EVIDENCE_FORGE_OPEN
    return UNKNOWN, EVIDENCE_STATE_UNAVAILABLE


def classify_ci(checks: Sequence[dict] | None) -> str:
    """``pass``/``fail``/``pending``/``unknown`` from ``default_pr_checks``.

    An EMPTY list is ``unknown``, never ``pass``: ``default_pr_checks`` returns
    ``[]`` for "no ``gh``", "unparseable ref", "network error" AND "this repo
    runs no CI", and none of those is a green build. Any failing check makes
    the whole head ``fail``; otherwise any pending check makes it ``pending``.

    The adjacent case the empty-list handling above does NOT cover: the repo
    DOES run CI, but CI never ran on THIS head, and some other always-green,
    non-required job did (e.g. a ``pull_request_target`` "CLA nudge" that
    fires on every PR regardless of what branch protection actually
    requires). Measured on a real PR: ``[{'name': 'CLA nudge', 'status':
    'pass', 'required': False}]`` while branch protection on that head still
    listed six unsatisfied required contexts. ``statuses == {CI_PASS}`` alone
    cannot tell that apart from a real green build, so a green verdict also
    needs at least one ``required`` check in the list — otherwise there is no
    evidence the checks the merge actually gates on ever ran, and this
    returns ``unknown`` rather than fabricating a ``pass``.

    Why ``unknown`` and not ``fail`` for an all-non-required list: the
    ``required`` flag itself comes from a ``gh`` lookup
    (``pr_watcher._required_check_names``) that fails OPEN — if that lookup
    fails, every entry comes back ``required=False``, which is "no
    required-ness data", not "confirmed zero required checks". ``unknown`` is
    honest under both readings and gets re-polled later; ``fail`` would be a
    fabricated red. This is the desired outcome, not a gap to close.

    Consumer chain: ``observe_pr`` calls this and writes the result to the
    ``pr_outcomes.ci_status`` column, read by ``nh pr-outcomes show`` and the
    API row mapper; ``UNKNOWN_CAVEAT`` already keeps ``unknown`` from ever
    being counted as a success there. Note what this does NOT change: the
    merge gate itself (``core.merge_policy._check_ci``) reads
    ``task.context["ci_status"]``, which is populated by
    ``vcs.ci_rollup.aggregate_rollup`` — a separate reducer with its own
    "no required-ness data ⇒ evaluate every entry" fallback — so on a
    PR-#541-shaped head the merge gate can still render ``ci: success`` even
    though the outcome recorded here is now ``unknown``.
    """
    if not checks:
        return CI_UNKNOWN
    statuses = {str(c.get("status") or "").strip().lower() for c in checks}
    if CI_FAIL in statuses:
        return CI_FAIL
    if CI_PENDING in statuses:
        return CI_PENDING
    if statuses == {CI_PASS} and any(c.get("required") for c in checks):
        return CI_PASS
    # A status vocabulary we do not recognise, or a green list with no
    # required check on it. Not a pass.
    return CI_UNKNOWN


async def probe_shipped(repo_path: str | None, branch: str | None,
                        base: str = "main") -> bool | None:
    """Did ``branch``'s content land on ``base``? ``True``/``False``/``None``.

    The three-valued wrapper ``classify_outcome`` requires, and the whole reason
    ``pr_watcher.refs_resolvable`` exists. ``default_branch_shipped`` answers
    the content question well but reports "I could not run" as ``False``,
    which a recorder must never read as "the content is absent".

    So: check that git can resolve BOTH refs first. If it cannot — the repo is
    gone, the worktree was cleaned up, the branch was deleted after merging —
    the honest answer is ``None`` and the PR stays ``unknown``, which is
    unsettled and will be asked about again. Only when both refs resolve is a
    ``False`` from the content check trustworthy enough to settle a PR as
    ``closed_unmerged``.

    Note what this does NOT fix: a ``False`` can still come from an exotic git
    failure that survives ref resolution (an unreadable object, a `diff` that
    dies on argv length). That residue is small and it errs toward reporting a
    merge as a non-merge — the direction that under-claims success — but it is
    a residue, not zero, and the evidence token on the row records which probe
    produced the verdict so the population stays auditable.
    """
    if not repo_path or not branch:
        return None
    try:
        if not await refs_resolvable(repo_path, branch, base):
            return None
        return await default_branch_shipped(repo_path, branch, base)
    except Exception:  # noqa: BLE001 — a probe failure is "cannot tell", not "absent"
        log.warning("ship probe failed for %s in %s", branch, repo_path,
                    exc_info=True)
        return None


def is_settled(outcome: str) -> bool:
    """True once the PR's fate will not change on its own (merged/closed).

    ``open`` and ``unknown`` are both UNSETTLED and are what a refresh re-polls:
    an open PR can still merge hours later, and an unknown can still become
    knowable when ``gh`` is installed or the network comes back.
    """
    return outcome in (MERGED, CLOSED_UNMERGED)


def rollup(outcomes: Iterable[str]) -> str:
    """One outcome for a task that opened SEVERAL PRs (multi-repo tasks).

    THE ONE PLACE THIS RULE IS WRITTEN. Conservative by construction, in this
    precedence:

    1. no rows at all              → ``unknown`` (we recorded nothing)
    2. any ``unknown``             → ``unknown`` (part of the delivery is unmeasured,
                                     so the whole delivery is)
    3. all ``merged``              → ``merged``
    4. any ``open``                → ``open`` (still in flight)
    5. otherwise                   → ``closed_unmerged``

    Rule 2 comes before rules 3–5 on purpose: a two-repo task with one merged
    PR and one unmeasurable PR has NOT been shown to have landed, and calling
    it ``merged`` is exactly the inflation this module refuses.
    """
    seen = [o for o in outcomes]
    if not seen:
        return UNKNOWN
    if any(o not in OUTCOMES or o == UNKNOWN for o in seen):
        return UNKNOWN
    if all(o == MERGED for o in seen):
        return MERGED
    if any(o == OPEN for o in seen):
        return OPEN
    return CLOSED_UNMERGED


def counts(outcomes: Iterable[str]) -> dict[str, int]:
    """``{outcome: n}`` over ``OUTCOMES``, every key present (including zeros).

    Every key present, always: a distribution that omits its zero buckets makes
    "we measured none of these" and "this bucket does not exist" render the
    same, and the zero that matters most here is ``merged``.
    """
    out = {o: 0 for o in OUTCOMES}
    for o in outcomes:
        out[o if o in out else UNKNOWN] += 1
    return out


def merged_rate(outcomes: Iterable[str]) -> tuple[int, int, int]:
    """``(merged, settled, total)`` — the numerator and BOTH denominators.

    Two denominators because there are two honest questions and one number
    cannot answer both:

    - ``merged / total``   — of everything we opened, how much is known to have
      landed. Unknowns count AGAINST it. This is the conservative headline.
    - ``merged / settled`` — of the PRs whose fate we actually know, how many
      landed. Unknowns leave the denominator, so it reads HIGHER; it is only
      meaningful beside the unknown count, which is why this returns all three
      numbers and no ratio. Never publish it alone.

    No ratio is returned at all: a caller that wants one has to divide, and
    dividing forces it to pick which denominator it means.
    """
    seen = list(outcomes)
    total = len(seen)
    merged = sum(1 for o in seen if o == MERGED)
    settled = sum(1 for o in seen if is_settled(o))
    return merged, settled, total


# --------------------------------------------------------------------------- #
# Recording + refreshing.
# --------------------------------------------------------------------------- #

def forge_identifiers(pr_url: str) -> dict[str, Any]:
    """``{forge, forge_host, repo_slug, pr_number}`` parsed from a PR URL.

    Delegates to ``pr_watcher.parse_pr_url``. A ``local-pr://…`` marker (the
    bench sandbox's bare-repo remote, ``vcs/__init__.py``) parses to nothing and
    is labelled ``local`` — that is a real, reportable fact and not a failure:
    a PR that was never opened on a forge has no forge outcome to look up, ever.
    """
    parsed = parse_pr_url(pr_url or "")
    if parsed:
        forge, host, slug, num = parsed
        return {"forge": forge, "forge_host": host,
                "repo_slug": slug, "pr_number": num}
    forge = "local" if str(pr_url or "").startswith("local-pr://") else ""
    return {"forge": forge, "forge_host": "", "repo_slug": "", "pr_number": None}


async def record_pr_opened(store: Any, task_id: str, pr_url: str) -> None:
    """Record, at PR-open time, that this task produced this PR.

    The outcome written here is the one thing we know for certain at this
    instant: we just opened it, so it is ``open`` — EXCEPT where the remote is a
    local bare repo, in which case there is no forge and the outcome is
    ``unknown`` with that stated as its evidence, forever. This is the bench
    sandbox's case and it must not be papered over: the north-star benchmark
    pushes to ``remote.git`` beside its sandbox clone, so no bench spec can
    ever produce a real merge outcome, and a report that implied otherwise
    would be the original defect rebuilt one level up.

    Never raises. Telemetry that can fail a task is worse than no telemetry.
    """
    ids = forge_identifiers(pr_url)
    if ids["forge"] in ("github", "gitlab"):
        outcome, evidence = OPEN, EVIDENCE_OPENED_BY_AGENT
    else:
        outcome, evidence = UNKNOWN, EVIDENCE_NO_FORGE
    try:
        await store.record_pr_outcome(
            task_id=task_id, pr_url=pr_url, outcome=outcome,
            outcome_evidence=evidence, ci_status=None,
            observed_source=SOURCE_LIVE, opened_at=_now(), checked_at=_now(),
            **ids)
    except Exception:  # noqa: BLE001 — telemetry must never break a task
        log.warning("could not record PR outcome for %s", task_id[:8],
                    exc_info=True)


async def observe_pr(
    store: Any, task_id: str, pr_url: str, *,
    forge_state: str | None,
    checks: Sequence[dict] | None = None,
    shipped: bool | None = None,
) -> str:
    """Write an OBSERVED outcome for one PR; return the outcome recorded.

    Takes the forge's answers as ARGUMENTS rather than fetching them, so the
    one caller that already has them in hand — ``wake.py``'s awaiting-approval
    ladder, which polls state/checks/shipped every tick anyway — records the
    outcome for free and adds not one forge call. See ``refresh_outcomes`` for
    the caller that does need to fetch.

    ``checks=None`` means "this observation did not look at CI" and leaves the
    stored ``ci_status`` alone. It is NOT the same as ``[]``, which means "we
    asked and got nothing back" and stores ``unknown``. The wake watcher's
    state rung is exactly the first case: it polls the PR's state without
    fetching its checks, and collapsing the two would let it erase a ``fail``
    a previous refresh had really measured.

    Never raises.
    """
    outcome, evidence = classify_outcome(forge_state, shipped=shipped)
    try:
        await store.record_pr_outcome(
            task_id=task_id, pr_url=pr_url, outcome=outcome,
            outcome_evidence=evidence,
            ci_status=None if checks is None else classify_ci(checks),
            observed_source=SOURCE_LIVE, checked_at=_now(),
            **forge_identifiers(pr_url))
    except Exception:  # noqa: BLE001 — telemetry must never break the watcher
        log.warning("could not observe PR outcome for %s", task_id[:8],
                    exc_info=True)
    return outcome


ShippedProbe = Callable[[str, str], Awaitable[bool | None]]


async def refresh_outcomes(
    store: Any, *,
    pr_state: Callable[[str], Awaitable[str]],
    pr_checks: Callable[[str], Awaitable[list[dict]]] | None = None,
    shipped_probe: ShippedProbe | None = None,
    limit: int | None = None,
) -> dict[str, int]:
    """Re-poll every UNSETTLED recorded PR; return ``{outcome: n}`` for the rows
    this call touched.

    WHY A SEPARATE REFRESH EXISTS AT ALL. A PR merges hours or days after the
    run that opened it ended, so an outcome written once at task end is a
    snapshot of "open" and nothing else. There are two refresh paths and they
    cover different populations:

    - the **live watcher** (``wake.py``'s ``_check_open_pr``) already polls
      state, checks and shipped-ness on every tick of every AWAITING_APPROVAL
      task, and now records what it saw. It costs nothing extra and it is what
      catches a merge that happens while the server is up.
    - **this function**, driven by ``nh pr-outcomes refresh``, covers what the
      watcher does not: tasks that already left AWAITING_APPROVAL (DONE,
      ESCALATED, FAILED) and rows recorded while offline. The watcher stops
      looking at a task the moment it goes terminal, so without this a PR that
      merged after its task was escalated would stay ``unknown`` forever.

    Only rows where ``is_settled`` is false are re-polled: a merged PR does not
    unmerge, and re-polling settled rows would spend forge calls to learn
    nothing. ``limit`` bounds one invocation.

    ``shipped_probe(pr_url, task_id) -> bool | None`` resolves the CLOSED
    ambiguity; ``None`` from it (or no probe at all) yields ``unknown``, never
    ``closed_unmerged``.

    A per-row failure is logged and the row is left as it was — one unreachable
    PR must not abort the sweep.
    """
    rows = await store.list_pr_outcomes(unsettled_only=True, limit=limit)
    tally = {o: 0 for o in OUTCOMES}
    for row in rows:
        pr_url = row.get("pr_url") or ""
        task_id = row.get("task_id") or ""
        # A local-remote PR has no forge to ask. Re-polling it would ask `gh`
        # about a `local-pr://` marker, get "" back, and rewrite a precise
        # evidence token ("there is no forge") with a vague one ("the state was
        # unavailable"). Leave it exactly as recorded.
        if row.get("forge") not in ("github", "gitlab"):
            tally[row.get("outcome") if row.get("outcome") in tally else UNKNOWN] += 1
            continue
        try:
            state = await pr_state(pr_url)
        except Exception:  # noqa: BLE001 — one bad PR must not abort the sweep
            log.warning("PR state poll failed for %s", pr_url[:120], exc_info=True)
            state = ""
        # None, not [] — "we did not look at CI" must not overwrite a stored
        # `fail` with `unknown`. Only a probe that actually ran produces a list.
        checks: list[dict] | None = None
        if pr_checks is not None:
            try:
                checks = await pr_checks(pr_url) or []
            except Exception:  # noqa: BLE001
                log.warning("PR checks poll failed for %s", pr_url[:120],
                            exc_info=True)
                checks = None
        shipped: bool | None = None
        if shipped_probe is not None and (state or "").strip().upper() == "CLOSED":
            try:
                shipped = await shipped_probe(pr_url, task_id)
            except Exception:  # noqa: BLE001
                log.warning("shipped probe failed for %s", pr_url[:120],
                            exc_info=True)
                shipped = None
        outcome = await observe_pr(store, task_id, pr_url, forge_state=state,
                                   checks=checks, shipped=shipped)
        tally[outcome] += 1
    return tally
