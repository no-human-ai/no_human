"""The one-shot review gate: reviewer + tamper guard, no Store, no server.

This is the SINGLE entry point for running the fresh-session reviewer without
the orchestrator's daemon/Store machinery. Anything that wants "run the gate
once, right now, over a diff" — the `nh gate` CLI verb, the
`review-this-branch` plugin skill, and (per its own task) the GitHub Action —
calls :func:`run_gate` here rather than constructing `AdversarialReviewer`
itself. Keeping exactly one construction site means the model/timeout config,
the diff-cap, and the single-turn/no-tools safety property stay in one place.

Reads and reports only: every git call against the user's own checkout is
read-only (`rev-parse`, `merge-base`, `diff`, `status --porcelain`, and — in
PR mode — a single additive `fetch` of the PR's refs, which writes objects
and `FETCH_HEAD` but creates no branch and moves no ref the user owns). It
never commits, pushes, merges, or edits a file in the user's checkout.

PR mode additionally materializes the fetched PR head into a throwaway local
clone (`git clone --local --shared`, in a temp directory, deleted before
`run_gate` returns) so the reviewer's citation check reads the PR's actual
file content instead of the user's currently checked-out branch. That clone
is read-only against the user's repo — `--local --shared` only ever reads
objects there — and every write it makes (the clone itself, the detached
checkout inside it) lands solely in the temp directory. The reviewer's own
backend is constructed read-only via `AdversarialReviewer`/
`ClaudeBackend(readonly=True)`.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from ..agent.backend_check import find_claude_cli
from ..config import AuthError, load_config, assert_subscription_mode
from ..core.task import Task
from ..review.reviewer import AdversarialReviewer, ReviewDecision
from ..testing import tamper_guard
from ..testing.runner import TamperCheckUnavailable, tamper_check_between
from ..vcs.git import GitError, GitRepo

_PR_URL_RE = re.compile(r"/pull/(\d+)(?:/|$)")


class GateUnavailable(RuntimeError):
    """A named precondition failed — the gate could not run.

    Every code path that cannot produce a real verdict raises this instead of
    returning a `GateResult`. Callers must treat it as a refusal, never as a
    pass: a gate that answers "no problems" when it never looked is worse
    than no gate at all.
    """


@dataclass
class GateResult:
    passed: bool
    comparison: str
    before_ref: str
    after_ref: str
    mode: str  # "branch" | "pr"
    tamper: tamper_guard.TamperReport
    decision: ReviewDecision
    uncommitted: list[str]


def _git(repo_path: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=repo_path, capture_output=True, text=True,
    )


def _rev_parse(repo_path: Path, ref: str) -> str | None:
    proc = _git(repo_path, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    sha = proc.stdout.strip()
    return sha if proc.returncode == 0 and sha else None


def _merge_base(repo_path: Path, a: str, b: str) -> str | None:
    proc = _git(repo_path, "merge-base", a, b)
    sha = proc.stdout.strip()
    return sha if proc.returncode == 0 and sha else None


def _diff(repo_path: Path, before: str, after: str) -> str:
    proc = _git(repo_path, "diff", "--no-color", f"{before}..{after}")
    return proc.stdout


def _uncommitted_paths(repo_path: Path) -> list[str]:
    proc = _git(repo_path, "status", "--porcelain")
    paths: list[str] = []
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        # `git status --porcelain`: two status chars, a space, then the path
        # (renames use "old -> new"; keep the destination side).
        path = line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        paths.append(path)
    return paths


def _check_credential(config) -> None:
    """Reuse the product's own auth check; never invent a second one."""
    if find_claude_cli() is None:
        raise GateUnavailable("the claude CLI is not on PATH")
    llm = config.get("llm") or {}
    try:
        assert_subscription_mode(
            profile=llm.get("auth_profile"),
            auth_mode=llm.get("auth_mode", "subscription"),
        )
    except AuthError as exc:
        raise GateUnavailable(f"no credential: {exc}") from exc


def _resolve_branch_mode(
    repo: GitRepo, repo_path: Path, base: str | None,
) -> tuple[str, str, str, str, list[str]]:
    """Returns (before_ref, after_ref, base_label, comparison, uncommitted)."""
    head = repo.head_sha()
    if base:
        base_ref = base
    else:
        default_name = repo.default_branch(local_only=False)
        if not default_name:
            raise GateUnavailable(
                "no upstream to compare against: could not resolve "
                "origin/HEAD; pass --base"
            )
        base_ref = f"origin/{default_name}"

    merge_base = _merge_base(repo_path, base_ref, "HEAD")
    if not merge_base:
        raise GateUnavailable(f"no merge base between HEAD and {base_ref}")
    if merge_base == head:
        raise GateUnavailable(
            f"the current branch has no commits beyond {base_ref}"
        )

    branch_name = repo.current_branch()
    uncommitted = _uncommitted_paths(repo_path)
    comparison = (
        f"working tree branch `{branch_name}` @ `{head[:7]}` against merge "
        f"base with `{base_ref}` @ `{merge_base[:7]}`"
    )
    return merge_base, head, base_ref, comparison, uncommitted


def _resolve_pr_mode(
    repo: GitRepo, repo_path: Path, pr_url: str,
) -> tuple[str, str, str, str, list[str]]:
    """Returns (before_ref, after_ref, base_label, comparison, uncommitted)."""
    match = _PR_URL_RE.search(pr_url)
    if not match:
        raise GateUnavailable("only GitHub pull request URLs are supported")
    number = match.group(1)

    proc = _git(repo_path, "fetch", "origin", f"refs/pull/{number}/head")
    if proc.returncode != 0:
        raise GateUnavailable(
            f"could not fetch pull request #{number} from origin: "
            f"{proc.stderr.strip()}"
        )
    head = _rev_parse(repo_path, "FETCH_HEAD")
    if not head:
        raise GateUnavailable(
            f"could not resolve FETCH_HEAD after fetching pull request #{number}"
        )

    default_name = repo.default_branch(local_only=False)
    if not default_name:
        raise GateUnavailable(
            "no upstream to compare against: could not resolve origin/HEAD"
        )
    base_ref = f"origin/{default_name}"

    merge_base = _merge_base(repo_path, base_ref, head)
    if not merge_base:
        raise GateUnavailable(
            f"no merge base between pull request #{number} and {base_ref}"
        )

    comparison = (
        f"pull request #{number} head `{head[:7]}` against merge base with "
        f"`{base_ref}` @ `{merge_base[:7]}`"
    )
    return merge_base, head, base_ref, comparison, []


@contextmanager
def _materialized_pr_head(repo_path: Path, sha: str):
    """Check out ``sha`` into a throwaway local clone and yield its path.

    The reviewer's citation check reads files straight off disk at whatever
    ``repo_path`` it is given (see ``reviewer._citation_fails``). In branch
    mode ``repo_path`` IS the working tree at ``after_ref``, so that read is
    already correct. In PR mode the PR head only ever lands in ``FETCH_HEAD``
    of the user's checkout — the user's actual working tree stays on
    whatever branch they had checked out — so handing the reviewer the raw
    ``repo_path`` would verify citations against the wrong tree. This clones
    ``repo_path`` locally (``--local --shared``: read-only against the
    source, objects are shared rather than copied) into a temp directory and
    checks out ``sha`` there, so the reviewer reads the PR's real content.
    The clone is removed on the way out, success or failure.
    """
    tmp_dir = Path(tempfile.mkdtemp(prefix="no_human_gate_pr_"))
    try:
        clone = subprocess.run(
            ["git", "clone", "--local", "--shared", "--no-checkout", "-q",
             str(repo_path), str(tmp_dir)],
            capture_output=True, text=True,
        )
        if clone.returncode != 0:
            raise GateUnavailable(
                "could not materialize the pull request head for review: "
                f"{clone.stderr.strip()}"
            )
        checkout = subprocess.run(
            ["git", "checkout", "--detach", "-q", sha],
            cwd=tmp_dir, capture_output=True, text=True,
        )
        if checkout.returncode != 0:
            raise GateUnavailable(
                f"could not check out pull request head {sha} for review: "
                f"{checkout.stderr.strip()}"
            )
        yield tmp_dir
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


async def run_gate(
    repo_path: Path,
    *,
    pr_url: str | None = None,
    base: str | None = None,
    title: str = "",
    description: str = "",
) -> GateResult:
    """Run the reviewer and the tamper guard once, over a diff, no daemon.

    ``repo_path`` must be a git checkout; ``pr_url`` (GitHub only) selects PR
    mode over the default working-tree-vs-merge-base branch mode. Every
    unmet precondition raises :class:`GateUnavailable` naming the condition —
    this function never returns a passing :class:`GateResult` for a
    condition it could not check.
    """
    repo_path = Path(repo_path)

    config = load_config()
    _check_credential(config)

    try:
        repo = GitRepo(repo_path)
    except GitError as exc:
        raise GateUnavailable(str(exc)) from exc

    if pr_url:
        mode = "pr"
        before_ref, after_ref, _base_label, comparison, uncommitted = (
            _resolve_pr_mode(repo, repo_path, pr_url)
        )
        label = f"pull request {pr_url}"
    else:
        mode = "branch"
        before_ref, after_ref, _base_label, comparison, uncommitted = (
            _resolve_branch_mode(repo, repo_path, base)
        )
        label = repo.current_branch()

    if uncommitted:
        comparison += (
            f"; {len(uncommitted)} uncommitted file(s) are NOT reviewed — "
            "commit them to include them"
        )

    diff = _diff(repo_path, before_ref, after_ref)

    try:
        tamper = tamper_check_between(
            repo_path, before_ref=before_ref, after_ref=after_ref,
        )
    except TamperCheckUnavailable as exc:
        raise GateUnavailable(f"tamper guard could not run: {exc}") from exc

    task = Task.new(
        title or f"gate: {label}",
        repo_path=str(repo_path),
        description=description or None,
    )
    reviewer = AdversarialReviewer.from_config(config.data)

    if mode == "pr":
        # The reviewer's citation check reads `review_repo_path` straight off
        # disk (`reviewer._citation_fails`). The user's own checkout never
        # holds the PR head's content — only `FETCH_HEAD` does — so review
        # against a throwaway clone checked out at `after_ref` instead of
        # `repo_path` itself. See `_materialized_pr_head`.
        with _materialized_pr_head(repo_path, after_ref) as review_repo_path:
            decision = await reviewer.review(
                task, repo_path=review_repo_path, diff_override=diff,
                before_ref=before_ref,
            )
    else:
        decision = await reviewer.review(
            task, repo_path=repo_path, diff_override=diff, before_ref=before_ref,
        )

    passed = decision.passed and not tamper.tampered

    return GateResult(
        passed=passed,
        comparison=comparison,
        before_ref=before_ref,
        after_ref=after_ref,
        mode=mode,
        tamper=tamper,
        decision=decision,
        uncommitted=uncommitted,
    )


def render_markdown(result: GateResult) -> str:
    """The pass/fail checklist with file:line citations, as Markdown."""
    lines: list[str] = []
    verdict = "PASS" if result.passed else "FAIL"
    lines.append(f"## no_human gate — {verdict}")
    lines.append(f"**Compared:** {result.comparison}")
    lines.append("")

    tamper = result.tamper
    tamper_verdict = "TAMPERED" if tamper.tampered else "clean"
    lines.append(f"### Tamper guard — {tamper_verdict}")
    lines.append(
        f"- tests {tamper.tests_before}->{tamper.tests_after}, "
        f"assertions {tamper.assertions_before}->{tamper.assertions_after}, "
        f"skips {tamper.skips_before}->{tamper.skips_after}"
    )
    for reason in tamper.reasons:
        lines.append(f"- {reason}")
    lines.append("")

    lines.append("### Review checklist")
    for item in result.decision.checklist:
        mark = "✅" if item.passed else "❌"
        lines.append(f"- {mark} **{item.label}**" + (
            f" — `{item.file}:{item.line}`" if item.file else ""
        ))
        detail = item.comment or item.evidence
        if detail:
            lines.append(f"  {detail}")
    lines.append("")

    if result.decision.demoted_citations:
        lines.append(
            "_Demoted (citation did not check out): "
            + ", ".join(result.decision.demoted_citations) + "_"
        )

    return "\n".join(lines).rstrip() + "\n"
