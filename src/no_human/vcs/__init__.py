"""VCS facade: branch/commit/push + open-PR dispatch. The agent never merges."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import github, gitlab
from .approve_merge import LandResult, land_task
from .git import CommitResult, GitError, GitRepo, ProtectedBranch, PushBehindRemote
from .manifest_repair import (
    commit_with_manifest_repair,
    is_gate_refusal,
    parse_manifest_refusal,
)
from .outbound_scrub import scrub_outbound

__all__ = [
    "GitRepo",
    "GitError",
    "ProtectedBranch",
    "PushBehindRemote",
    "CommitResult",
    "PrResult",
    "open_pr",
    "promote_draft_pr",
    "commit_with_manifest_repair",
    "is_gate_refusal",
    "parse_manifest_refusal",
    "land_task",
    "LandResult",
]


@dataclass
class PrResult:
    url: str
    kind: str  # github | gitlab | local
    branch: str
    # The SHA `repo.push()` actually sent, captured at push time — the receipt
    # check must compare the forge's PR head against THIS, never against a
    # HEAD re-resolved later (HEAD can drift while a long-running task waits
    # on CI/review). Empty when a caller fabricates a PrResult without a real
    # push (tests); receipts.py treats a falsy local_sha as "skip the check".
    pushed_sha: str = ""


def open_pr(
    repo: GitRepo,
    branch: str,
    title: str,
    body: str,
    *,
    base: str = "main",
    github_hosts: list[str] | None = None,
    update_existing_body: bool = False,
    force_with_lease: bool = False,
) -> PrResult:
    """Push the branch and open a PR/MR against the detected remote.

    ``force_with_lease`` is passed straight to ``GitRepo.push`` and is set
    True by TWO callers, both in ``orchestrator.py`` and both a delivery
    retry after a non-fast-forward rejection: ``_finalize``'s PR-open retry
    (``open_pr(..., force_with_lease=forced)``, only when the rejection is
    actually non-fast-forward) and the draft-PR ``pr_conflict`` round's
    retry (``open_pr(..., force_with_lease=True)``, unconditional once that
    round is reached). ``GitRepo.push`` itself has a THIRD caller —
    ``evidence_ledger.py``'s unconditional force-push of its own nh-evidence
    side branch — that goes through ``repo.push`` directly, never through
    this function. See ``GitRepo.push`` for why a rebased agent branch
    cannot be delivered any other way, and why the protected-branch refusal
    is unaffected.

    A non-fast-forward rejection caused by the local branch being BEHIND its
    own remote tip (not diverged from it) raises ``PushBehindRemote`` out of
    ``GitRepo.push`` instead of being retried with force — forcing there would
    destroy an earlier attempt's already-published commits. This propagates
    out of ``open_pr`` unhandled: no PR is opened for a branch that could not
    be published, and the caller decides and reports rather than this
    function silently choosing a remedy.

    ``github_hosts`` lists extra GitHub Enterprise hosts (from ``git.github_hosts``)
    so a GHE remote like code.example.com is recognized as GitHub.

    For a local bare-repo remote (Phase 0 testing target) there is no PR API, so
    we push the branch and return a ``local`` marker — the push itself proves the
    branch/commit/PR-open code path without touching a real forge.
    """
    # Scrub before anything else: no title/body reaches classification, push,
    # or a forge adapter unscrubbed.
    title = scrub_outbound(title, "pr_title")
    body = scrub_outbound(body, "pr_body")

    # Classify the remote BEFORE pushing. The old order pushed first and gave
    # any unrecognized https host a fake `local-pr://` marker afterwards — the
    # branch landed on a forge we couldn't even name, and the task reported
    # success with a PR URL that opens nothing.
    url = repo.remote_url() or ""
    is_github = github.is_github_remote(url, github_hosts or [])
    is_gitlab = gitlab.is_gitlab_remote(url)
    if not is_github and not is_gitlab and url.startswith(("http://", "https://", "git@")):
        raise RuntimeError(
            f"remote host not recognized as GitHub or GitLab: {url!r} — "
            "refusing to push. Add the host to git.github_hosts if it is a "
            "GitHub Enterprise instance."
        )

    pushed_sha = repo.push(branch, force_with_lease=force_with_lease)
    if is_github:
        return PrResult(github.open_pr(repo.path, branch, title, body, base=base,
                                       update_existing_body=update_existing_body),
                        "github", branch, pushed_sha=pushed_sha)
    if is_gitlab:
        return PrResult(gitlab.open_mr(repo.path, branch, title, body, base=base),
                        "gitlab", branch, pushed_sha=pushed_sha)

    # Local (file-path) remote — the Phase 0 testing target: no PR API, the
    # push itself proves the branch/commit/PR-open path.
    marker = f"local-pr://{Path(url).name or 'remote'}/{branch}"
    return PrResult(marker, "local", branch, pushed_sha=pushed_sha)


def promote_draft_pr(repo: GitRepo, pr_url: str, *, github_hosts: list[str] | None = None) -> str:
    """Promote a draft PR/MR to ready-for-review. Returns an outcome token
    (see ``github.mark_pr_ready``), never raises.

    GitHub only: ``vcs/gitlab.py``'s ``open_mr`` never opens a draft, so
    there is nothing to promote on GitLab, and any other remote has no PR
    API at all — both return a ``"not_applicable: ..."`` token instead of a
    new call.
    """
    url = repo.remote_url() or ""
    if github.is_github_remote(url, github_hosts or []):
        return github.mark_pr_ready(repo.path, pr_url)
    if gitlab.is_gitlab_remote(url):
        return "not_applicable: gitlab has no draft state"
    return "not_applicable: remote has no PR API"
