"""The one-shot review gate: reviewer + tamper guard, no Store, no server.

This is the SINGLE entry point for running the fresh-session reviewer without
the orchestrator's daemon/Store machinery. Anything that wants "run the gate
once, right now, over a diff" — today that is the `nh gate` CLI verb and the
`review-this-branch` plugin skill — calls :func:`run_gate` here rather than
constructing `AdversarialReviewer` itself. Keeping exactly one construction
site means the model/timeout config, the diff-cap, and the single-turn/
no-tools safety property stay in one place.

Reads and reports only: every git call this module makes against the user's
own checkout is read-only plumbing — `rev-parse`, `merge-base`, `diff`,
`status --porcelain`, `config --get remote.origin.url` (to verify a `--pr`
URL names this checkout's own repository — deliberately not `remote
get-url`, which would apply any `insteadOf` rewrite instead of reporting the
repo's actual declared origin; see `_origin_owner_repo`), and, in PR mode,
a single additive `fetch` of the
PR's refs (which writes objects and `FETCH_HEAD` but creates no branch and
moves no ref the user owns). The tamper guard this module calls
(`testing.runner.tamper_check_between`) also runs its own read-only git
plumbing, including `ls-tree` and `show`, against the same checkout — that
module's calls are not enumerated here since they are not this module's to
promise. Taken together, `run_gate` never commits, pushes, merges, or edits a
file in the user's checkout.

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
from ..config import AuthError, MissingCredentialError, load_config, assert_subscription_mode
from ..core.task import Task
from ..review.reviewer import _DIFF_CAP, AdversarialReviewer, ReviewDecision, ReviewerUnavailable
from ..testing import tamper_guard
from ..testing.runner import TamperCheckUnavailable, tamper_check_between
from ..vcs.git import GitError, GitRepo

# Strict GitHub pull request URL: host must be exactly github.com, and both
# owner and repo are captured so the caller can be checked against this
# checkout's own `origin` remote (see `_origin_owner_repo`). Anything looser
# than this — a bare "owner/repo/pull/N", a non-GitHub host, a scheme other
# than https — used to match and get reviewed as if it were the request URL,
# which is how a same-numbered PR on the wrong repo could get reviewed and
# reported as a pass for this one. See `_resolve_pr_mode`.
_PR_URL_RE = re.compile(
    r"^https://github\.com/(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)"
    r"/(?P<repo>[A-Za-z0-9._-]+?)(?:\.git)?/pull/(?P<number>\d+)(?:/\S*)?/?$"
)

# `origin`'s remote URL, in either the https or the ssh form git accepts.
_ORIGIN_URL_RE = re.compile(
    r"github\.com[:/](?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$"
)


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
    truncated: bool = False


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
    if proc.returncode != 0:
        raise GateUnavailable(
            f"could not diff {before}..{after}: {proc.stderr.strip()}"
        )
    return proc.stdout


def _uncommitted_paths(repo_path: Path) -> list[str]:
    proc = _git(repo_path, "status", "--porcelain")
    if proc.returncode != 0:
        raise GateUnavailable(
            f"could not check for uncommitted changes: {proc.stderr.strip()}"
        )
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
    except MissingCredentialError as exc:
        # No credential on file at all.
        raise GateUnavailable(f"no credential: {exc}") from exc
    except AuthError as exc:
        # A credential problem that is NOT "nothing on file" — e.g. a stray
        # ANTHROPIC_API_KEY set while llm.auth_mode is "subscription"
        # (config.py:1280). Calling that "no credential" would be false; the
        # problem is an extra, disallowed one.
        raise GateUnavailable(f"credential problem: {exc}") from exc


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


def _origin_owner_repo(repo_path: Path) -> tuple[str, str] | None:
    """This checkout's ``origin`` remote, as a GitHub ``(owner, repo)`` pair.

    Returns ``None`` if ``origin`` has no URL or the URL is not a
    ``github.com`` remote (https or ssh form) — callers must treat that as
    "cannot verify", not as a pass.

    Reads ``remote.origin.url`` via ``git config --get`` rather than
    ``git remote get-url origin`` on purpose: the latter applies any
    ``url.<base>.insteadOf`` rewrite configured for fetches/pushes, which
    would report the rewritten fetch target instead of the repository's own
    declared identity — exactly the wrong thing to compare a `--pr` URL
    against.
    """
    proc = _git(repo_path, "config", "--get", "remote.origin.url")
    if proc.returncode != 0:
        return None
    match = _ORIGIN_URL_RE.search(proc.stdout.strip())
    if not match:
        return None
    return match.group("owner"), match.group("repo")


def _resolve_pr_mode(
    repo: GitRepo, repo_path: Path, pr_url: str, base: str | None,
) -> tuple[str, str, str, str, list[str]]:
    """Returns (before_ref, after_ref, base_label, comparison, uncommitted)."""
    match = _PR_URL_RE.match(pr_url.strip())
    if not match:
        raise GateUnavailable(
            f"not a GitHub pull request URL: {pr_url!r} — expected "
            "https://github.com/<owner>/<repo>/pull/<number>"
        )
    owner, name, number = match.group("owner"), match.group("repo"), match.group("number")

    origin = _origin_owner_repo(repo_path)
    if origin is None:
        raise GateUnavailable(
            "origin is not a github.com remote: cannot verify that "
            f"{pr_url!r} belongs to this checkout"
        )
    if (owner.lower(), name.lower()) != (origin[0].lower(), origin[1].lower()):
        raise GateUnavailable(
            f"pull request URL names {owner}/{name}, but this checkout's "
            f"origin is {origin[0]}/{origin[1]} — refusing to review a "
            "different repository than the one checked out"
        )

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

    merge_base = _merge_base(repo_path, base_ref, head)
    if not merge_base:
        raise GateUnavailable(
            f"no merge base between pull request #{number} and {base_ref}"
        )
    if merge_base == head:
        raise GateUnavailable(
            f"pull request #{number} has no commits beyond {base_ref}"
        )

    comparison = (
        f"pull request {owner}/{name}#{number} head `{head[:7]}` against "
        f"merge base with `{base_ref}` @ `{merge_base[:7]}`"
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
            _resolve_pr_mode(repo, repo_path, pr_url, base)
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
    if not diff.strip():
        # `AdversarialReviewer.review` treats `diff_override=""` as falsy —
        # identical to "no diff override" — and falls through to the
        # multi-turn, tool-enabled gate path (reviewer.py:2566,2588,2601).
        # That defeats the single-turn/no-tools property this module exists
        # to guarantee, so refuse outright rather than hand the reviewer an
        # empty string it would silently reinterpret.
        raise GateUnavailable(
            f"no changes to review between {before_ref[:7]} and "
            f"{after_ref[:7]}: the diff is empty"
        )

    # `AdversarialReviewer.review` silently truncates `diff_override` to
    # `_DIFF_CAP` chars (reviewer.py:2534) with no signal back to the
    # caller — a diff bigger than the cap would otherwise get a fraction of
    # itself reviewed and could still print a bare PASS on that partial
    # view. Detect it here and make sure a truncated review can never pass.
    truncated = len(diff) > _DIFF_CAP

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

    try:
        if mode == "pr":
            # The reviewer's citation check reads `review_repo_path` straight
            # off disk (`reviewer._citation_fails`). The user's own checkout
            # never holds the PR head's content — only `FETCH_HEAD` does — so
            # review against a throwaway clone checked out at `after_ref`
            # instead of `repo_path` itself. See `_materialized_pr_head`.
            with _materialized_pr_head(repo_path, after_ref) as review_repo_path:
                decision = await reviewer.review(
                    task, repo_path=review_repo_path, diff_override=diff,
                    before_ref=before_ref,
                )
        else:
            decision = await reviewer.review(
                task, repo_path=repo_path, diff_override=diff, before_ref=before_ref,
            )
    except ReviewerUnavailable as exc:
        # The reviewer itself reaches "no verdict" and escalates rather than
        # guessing (reviewer.py:2601) — that means the gate did not run, the
        # same shape as every other unmet precondition here.
        raise GateUnavailable(f"the reviewer could not reach a verdict: {exc}") from exc

    passed = decision.passed and not tamper.tampered and not truncated

    return GateResult(
        passed=passed,
        comparison=comparison,
        before_ref=before_ref,
        after_ref=after_ref,
        mode=mode,
        tamper=tamper,
        decision=decision,
        uncommitted=uncommitted,
        truncated=truncated,
    )


def render_markdown(result: GateResult) -> str:
    """The pass/fail checklist with file:line citations, as Markdown."""
    lines: list[str] = []
    verdict = "PASS" if result.passed else "FAIL"
    lines.append(f"## no_human gate — {verdict}")
    lines.append(f"**Compared:** {result.comparison}")
    if result.truncated:
        lines.append(
            f"**⚠ diff exceeded the {_DIFF_CAP:,}-char single-turn review "
            "cap and was truncated — this review only covers a prefix of "
            "the change, and the gate cannot pass on it.**"
        )
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
