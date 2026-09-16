"""Measure how many live tasks have a branch diverged from its own pushed tip.

BACKGROUND. `vcs/recut.py` fixes the loop this measures: a task whose branch
was rewritten (rebased, squashed, amended) after being pushed once used to
retry forever, hitting the same non-fast-forward delivery refusal every
time. This module answers a different, narrower question — acceptance
criterion 5 of that fix — "how many tasks are (or were) stuck in that state
right now", so the size of the problem is measured rather than assumed.

Read-only, by construction: `audit_diverged_tasks` never pushes, never
writes a ref, never fetches into a tracking ref (`GitRepo.remote_branch_
relation` already avoids that — see its own docstring), and never touches
the store beyond the initial `list_tasks` reads. It is safe to run at any
time, against a live install, with no side effects.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..vcs.git import GitError, GitRepo
from ..vcs.recut import branch_stem
from .task import TaskStatus

__all__ = ["AuditReport", "AuditRow", "LIVE_STATUSES", "audit_diverged_tasks"]

# The statuses a task can be in while it is still "live" — i.e. still capable
# of running another attempt and hitting the recut/refusal path again. A task
# already DONE or FAILED can no longer loop; ESCALATED/AWAITING_INPUT/
# PAUSED_QUOTA/COMPOUND_PARENT/REVIEWING/CONTEXT/PLANNING are excluded on the
# same grounds this task's PLAN specifies verbatim.
LIVE_STATUSES: tuple[TaskStatus, ...] = (
    TaskStatus.IMPLEMENTING,
    TaskStatus.TESTING,
    TaskStatus.AWAITING_APPROVAL,
    TaskStatus.BLOCKED,
    TaskStatus.PENDING,
)


@dataclass(frozen=True)
class AuditRow:
    task_id: str
    title: str
    branch: str
    local_sha: str | None
    remote_sha: str | None
    state: str  # "up_to_date" | "behind" | "diverged" | "unknown"


@dataclass
class AuditReport:
    rows: list[AuditRow] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    scanned: int = 0  # live tasks scanned, whether or not they had a readable repo
    # Distinct task ids with >= 1 diverged row. `_candidate_branches` can
    # return more than one branch for the same task (the current `pr_branch`
    # plus older same-stem local branches from before a recut), so `counts`
    # — incremented once per *row* — is not the right source for "how many
    # TASKS are diverged" (acceptance criterion 5's literal wording). This
    # set is task-deduplicated so `diverged_count` cannot overcount a single
    # looping task as more than one.
    diverged_task_ids: set[str] = field(default_factory=set)

    @property
    def diverged_count(self) -> int:
        return len(self.diverged_task_ids)


def _branch_prefix(config) -> str:
    if isinstance(config, dict):
        return (config.get("git") or {}).get("branch_prefix") or "no-human/"
    return "no-human/"


def _candidate_branches(repo: GitRepo, task, stem: str) -> list[str]:
    """`ctx["pr_branch"]` plus every local `refs/heads/<stem>*` — the same
    branch family a recut would allocate the next name from (see
    `vcs.recut.next_recut_branch`), so an already-recut task's OLD branch is
    still included even after `pr_branch` has moved on to the new one.

    Read-only: `for-each-ref` inspects local refs only, no network access.
    """
    ctx = task.context if isinstance(task.context, dict) else {}
    names: list[str] = []
    pr_branch = ctx.get("pr_branch")
    if pr_branch:
        names.append(pr_branch)
    local = repo._run(
        "for-each-ref", "--format=%(refname:short)", f"refs/heads/{stem}*",
        check=False,
    )
    for name in local.splitlines():
        name = name.strip()
        if name and name not in names:
            names.append(name)
    return names


async def audit_diverged_tasks(
    store, config, *, statuses: tuple[TaskStatus, ...] = LIVE_STATUSES,
) -> AuditReport:
    """Classify every live task's branch(es) against their own remote tip.

    For each live task with a readable local repo (`task.repo_path`
    resolving to a real git working tree), every candidate branch (see
    `_candidate_branches`) is classified with `GitRepo.remote_branch_
    relation`: `"up_to_date"`, `"behind"`, `"diverged"`, or `"unknown"`
    (never pushed, remote unreachable, or the repo itself is unreadable —
    fails open, exactly `remote_branch_relation`'s own contract).

    A task with no readable repo, or with no candidate branches at all,
    contributes nothing to `rows`/`counts` but is still counted in
    `scanned` — "N diverged of M live tasks scanned" must account for every
    live task, not just the ones a branch could be found for.
    """
    report = AuditReport()
    prefix = _branch_prefix(config)
    for status in statuses:
        tasks = await store.list_tasks(status)
        for task in tasks:
            report.scanned += 1
            repo_path = getattr(task, "repo_path", None)
            if not repo_path:
                continue
            try:
                repo = GitRepo(repo_path)
            except GitError:
                continue
            stem = branch_stem({"git": {"branch_prefix": prefix}}, task.id)
            for branch in _candidate_branches(repo, task, stem):
                try:
                    state = repo.remote_branch_relation(branch)
                except Exception:  # noqa: BLE001 — never let one bad repo abort the scan
                    state = "unknown"
                local_sha = repo._run("rev-parse", branch, check=False) or None
                try:
                    remote_sha = repo.fetch_remote_branch_sha(branch)
                except Exception:  # noqa: BLE001 — advisory column only
                    remote_sha = None
                report.rows.append(AuditRow(
                    task_id=task.id, title=task.title, branch=branch,
                    local_sha=(local_sha.strip() if local_sha else None),
                    remote_sha=remote_sha, state=state,
                ))
                report.counts[state] = report.counts.get(state, 0) + 1
                if state == "diverged":
                    report.diverged_task_ids.add(task.id)
    return report
