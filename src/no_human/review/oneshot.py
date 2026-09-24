"""The one-shot review gate: reviewer + tamper guard, no Store, no server.

This module is where the fresh-session reviewer is constructed for every
no-daemon path, so the model/timeout config and the single-turn/no-tools
safety property stay in one place. Two entry points sit on top of it.
:func:`run_gate` is the whole gate for a checkout — it resolves the refs,
computes the diff, runs the tamper guard and materializes the head — and
backs the `nh gate` CLI verb and the `review-this-branch` plugin skill.
:func:`review_diff` is the seam for a caller that already has a diff and its
refs, today the GitHub Action, which brings its own refs, file cap,
credential handling and comment rendering. Both refuse a diff larger than the
reviewer's internal cap rather than review a truncated prefix; nothing else
in `src/` may construct the reviewer for a one-shot review.

Reads and reports only: every git call this module makes against the user's
own checkout is read-only plumbing — `rev-parse`, `merge-base`, `diff`,
`status --porcelain`, `symbolic-ref` (the local-only half of
`default_branch()`; see below), `config --get remote.origin.url` (to verify
a `--pr` URL names this checkout's own repository — deliberately not
`remote get-url`, which would apply any `insteadOf` rewrite instead of
reporting the repo's actual declared origin; see `_origin_owner_repo`), and,
in PR mode, a single `ls-remote` of the PR's ref (a pure network read, no
local write at all — see `_ls_remote_exact` and `_resolve_pr_mode`'s
FETCH_HEAD-race comment) followed by an additive `fetch` of that same ref
(which writes objects and `FETCH_HEAD` but creates no branch and moves no
ref the user owns). No network call is made by default:
`default_branch(local_only=True)` reads only the local
`refs/remotes/origin/HEAD`, never `git remote show origin` (that fallback
does real network I/O and, offline, hangs instead of answering) — a checkout
where that local ref was never recorded refuses by name and points at
`--base` rather than guessing over the network. Every git call that touches
the network (PR mode's `ls-remote` and `fetch`) runs with
`GIT_TERMINAL_PROMPT=0` and a no-op askpass so a private repo the caller
lacks credentials for fails fast instead of blocking on a credential prompt
forever (see `_no_prompt_env`). The tamper guard this module calls
(`testing.runner.tamper_check_between`) also runs its own read-only git
plumbing, including `ls-tree` and `show`, against the same checkout — that
module's calls are not enumerated here since they are not this module's to
promise. Taken together, `run_gate` never commits, pushes, merges, or edits a
file in the user's checkout.

Both modes materialize the reviewed head (`after_ref`) into a throwaway
local clone (`git clone --local --shared`, in a temp directory, deleted
before `run_gate` returns) before handing it to the reviewer, so the
reviewer's citation check (`reviewer._citation_fails`, which reads files
straight off disk at whatever path it is given) always reads the exact tree
that was diffed — never the user's live, possibly-dirty working tree. That
clone is read-only against the user's repo — `--local --shared` only ever
reads objects there — and every write it makes (the clone itself, the
detached checkout inside it) lands solely in the temp directory. The
reviewer's own backend is constructed read-only via `AdversarialReviewer`/
`ClaudeBackend(readonly=True)`.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ..agent.backend import BackendUnavailable
from ..agent.backend_check import find_claude_cli
from ..config import (
    AuthError,
    ConfigError,
    MissingCredentialError,
    load_config,
    assert_subscription_mode,
)
from ..core.role_backend_settings import effective_role_backend
from ..core.runtime import assert_task_backend_usable
from ..core.task import Task
from ..review.diff_coverage import DiffCoverageError, budget_diff
from ..review.reviewer import _DIFF_CAP, AdversarialReviewer, ReviewDecision, ReviewerUnavailable
from ..testing import tamper_guard
from ..testing.runner import TamperCheckUnavailable, tamper_check_between
from ..vcs.git import GitError, GitRepo

_IS_WINDOWS = os.name == "nt"

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
# The host is anchored to either the very start of the URL or right after a
# `/` or `@` — otherwise `github\.com` matches as a bare substring, so
# `https://evilgithub.com/acme/widgets` (host `evilgithub.com`, not GitHub at
# all) would read as owner `acme` repo `widgets` on GitHub, and a `--pr` URL
# could be laundered through a lookalike origin as "this checkout's own
# repository".
_ORIGIN_URL_RE = re.compile(
    r"(?:^|[/@])github\.com[:/](?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$"
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
    # §6d: the reviewer's own backend/model, and whether that is an operator
    # override of the Claude default pin for the reviewer role. Populated
    # from the same `effective_role_backend` resolver `_check_credential`
    # previews and `AdversarialReviewer.from_config` itself consults — never
    # re-derived a second way. `render_markdown` always prints the backend
    # and model, default or not, so a reader can always tell which model
    # produced the checklist; it adds an "overridden" note only when
    # `reviewer_backend_is_default` is False.
    reviewer_backend: str = "claude"
    reviewer_model: str = ""
    reviewer_backend_is_default: bool = True
    # The changed-file paths for `before_ref..after_ref` — the SAME two refs
    # this run resolved and reviewed, read via `_changed_files` off of
    # those exact shas, never a second, independent lookup by PR number.
    # A crossed head resolution (see `_resolve_pr_mode`'s FETCH_HEAD-race
    # comment) would then show up here as files absent from the named PR's
    # real file list, making the verdict self-contradictory on its face
    # instead of silently citing the wrong PR's diff as if it were correct.
    changed_files: list[str] = field(default_factory=list)


def _no_prompt_env() -> dict[str, str]:
    """Env for every git call this module makes: refuse to prompt, ever.

    Mirrors `integrations/__init__.py`'s `_git_credential_present` exactly —
    `GIT_TERMINAL_PROMPT=0` plus a no-op askpass/GCM setting means `git
    fetch` (PR mode) against a repo the caller lacks credentials for fails
    fast instead of blocking on an interactive credential prompt forever.
    Applied to every call here, not just `fetch`, so no future call site can
    reintroduce the hang by accident.
    """
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    if _IS_WINDOWS:
        env["GCM_INTERACTIVE"] = "never"
    else:
        env["GIT_ASKPASS"] = "/usr/bin/true"
    return env


def _git(repo_path: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=repo_path, capture_output=True, text=True,
        env=_no_prompt_env(),
    )


def _resolve_repo_root(path: Path) -> Path:
    """The real repo root for `path`, so the gate works from any subdirectory.

    `GitRepo.__init__` requires `.git` to exist directly under the given
    path (`vcs/git.py`), which is only true at the exact repo root — so
    without this, invoking the gate from a subdirectory of a perfectly
    normal checkout (a very common shape: `cd src && nh gate`) fails with a
    misleading "not a git repository" message. `git rev-parse
    --show-toplevel` finds the real root regardless of where inside the
    checkout it is run. On failure this returns `path` unchanged, so
    `GitRepo`'s own accurate refusal still fires for a directory that
    genuinely is not a git checkout at all.
    """
    proc = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], cwd=path,
        capture_output=True, text=True, env=_no_prompt_env(),
    )
    if proc.returncode != 0:
        return path
    top = proc.stdout.strip()
    return Path(top) if top else path


def _is_shallow_repo(repo_path: Path) -> bool:
    proc = _git(repo_path, "rev-parse", "--is-shallow-repository")
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def _is_detached_head(repo_path: Path) -> bool:
    proc = _git(repo_path, "symbolic-ref", "-q", "HEAD")
    return proc.returncode != 0


def _rev_parse(repo_path: Path, ref: str) -> str | None:
    proc = _git(repo_path, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    sha = proc.stdout.strip()
    return sha if proc.returncode == 0 and sha else None


def _ls_remote_exact(repo_path: Path, ref: str, remote: str = "origin") -> str | None:
    """The sha `remote` currently advertises for `ref`, read live over the
    network — never read back from any local, per-repo mutable state.

    This is the fix for the FETCH_HEAD race documented on `_resolve_pr_mode`:
    `git ls-remote <remote> <ref>` returns the answer directly in this call's
    own stdout, with no shared on-disk slot a second, concurrent `git fetch`
    (from another gate run against the same checkout) could overwrite before
    this call reads it back. `ls-remote` matches on *tail* path components,
    so a decoy ref that merely ends in `ref` could otherwise surface too —
    every returned line is checked against `ref` byte-for-byte before being
    trusted, and two-or-more DISTINCT shas both exactly matching `ref`
    (should not happen for one real ref) is treated as ambiguous rather than
    guessed at, same as every other unreadable state (non-zero exit, empty
    stdout): all return `None`.

    Mirrors `GitRepo.ls_remote_exact` (`vcs/git.py`) byte for byte, but goes
    through this module's own `_git` (not `GitRepo`'s) so the same
    `GIT_TERMINAL_PROMPT=0` / no-op askpass protection this module's `fetch`
    already gets also covers this network read — `GitRepo.ls_remote_exact`
    does not set that env, so it is not reused here.
    """
    proc = _git(repo_path, "ls-remote", remote, ref)
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    exact_shas: set[str] = set()
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        sha, name = parts
        if name == ref:
            exact_shas.add(sha)
    if len(exact_shas) != 1:
        return None
    return next(iter(exact_shas))


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


def _changed_files(repo_path: Path, before: str, after: str) -> list[str]:
    """The paths changed between `before` and `after` — the exact same two
    refs `_diff` was just asked to diff for this same run, never a second,
    independent lookup by PR number.

    This backs the changed-file list `render_markdown` discloses in every
    verdict. The point is detectability of a crossed head resolution (see
    `_resolve_pr_mode`'s FETCH_HEAD-race comment): if `after` ever ends up
    resolved to the wrong PR's head — this race or a different bug entirely —
    this reads off files from THAT wrong head, so the verdict names the
    requested PR but lists files that PR does not actually contain, and a
    reader can catch the mismatch on sight. A file list refetched from the
    forge by PR number instead would silently agree with the (wrong) header
    even on a crossed review, and catch nothing — see
    `test_a_crossed_head_resolution_produces_a_self_contradictory_verdict`.
    """
    proc = _git(repo_path, "diff", "--no-color", "--name-only", f"{before}..{after}")
    if proc.returncode != 0:
        raise GateUnavailable(
            f"could not list changed files for {before}..{after}: "
            f"{proc.stderr.strip()}"
        )
    return [line for line in proc.stdout.splitlines() if line.strip()]


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
    """Reuse the product's own auth check; never invent a second one.

    §6d: an operator's explicit `llm.role_backends.reviewer` Settings choice
    overrides the Claude default pin for the reviewer role, and credential
    checks apply PER backend, not just Claude's. `effective_role_backend` is
    the same resolver `AdversarialReviewer.from_config` itself would consult
    (via `make_backend(..., role="reviewer")`), so this stays a read-only
    preview of what construction will actually build, never a second
    opinion of "what backend does the reviewer run on". A reviewer pinned to
    codex or local is checked with `assert_task_backend_usable` — the same
    per-task preflight the orchestrator runs before its own first coder
    turn — instead of the claude-CLI-on-PATH/subscription check below, which
    only applies when the reviewer really is running on claude.
    """
    backend_name = effective_role_backend(config.data, "reviewer")["backend"]
    if backend_name != "claude":
        try:
            assert_task_backend_usable(backend_name, config.data)
        except (AuthError, BackendUnavailable) as exc:
            raise GateUnavailable(
                f"the reviewer backend {backend_name!r} is not usable: {exc}"
            ) from exc
        return

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
        # local_only=True: the non-local fallback (`git remote show origin`)
        # does real network I/O and, offline, hangs instead of answering
        # (vcs/git.py's own docstring on `default_branch`) — the default,
        # no-flags `nh gate` invocation must never reach the network, so a
        # checkout that never recorded a local origin/HEAD refuses by name
        # here instead of guessing over the wire.
        default_name = repo.default_branch(local_only=True)
        if not default_name:
            raise GateUnavailable(
                "no upstream to compare against: could not resolve "
                "origin/HEAD locally (this checkout may never have run "
                "`git remote set-head origin -a`); pass --base"
            )
        base_ref = f"origin/{default_name}"

    merge_base = _merge_base(repo_path, base_ref, "HEAD")
    shallow = _is_shallow_repo(repo_path)
    if not merge_base:
        if shallow:
            raise GateUnavailable(
                f"this checkout is a shallow clone: no merge base between "
                f"HEAD and {base_ref} exists within the fetched history — "
                "run `git fetch --unshallow` and retry"
            )
        raise GateUnavailable(f"no merge base between HEAD and {base_ref}")
    if merge_base == head:
        if shallow:
            raise GateUnavailable(
                f"this checkout is a shallow clone: HEAD and {base_ref} "
                "resolve to the same commit within the fetched history, "
                "which may only be because the shallow history doesn't "
                "reach the true merge base — run `git fetch --unshallow` "
                "and retry"
            )
        raise GateUnavailable(
            f"the current branch has no commits beyond {base_ref}"
        )

    uncommitted = _uncommitted_paths(repo_path)
    if _is_detached_head(repo_path):
        comparison = (
            f"working tree in detached HEAD @ `{head[:7]}` against merge "
            f"base with `{base_ref}` @ `{merge_base[:7]}`"
        )
    else:
        branch_name = repo.current_branch()
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

    pr_ref = f"refs/pull/{number}/head"
    # FETCH_HEAD is a SINGLE file per repository, shared by every process
    # running against this checkout. Two `nh gate --pr` runs sharing
    # `repo_path` (e.g. #547 and #583) each `fetch` into that one slot; the
    # OLD code then read FETCH_HEAD straight back, so whichever gate's read
    # landed after the OTHER gate's fetch resolved the OTHER PR's head —
    # last writer wins, and a PASS got attributed to a diff that was never
    # the named PR's. `ls-remote` closes this at the resolution step: it
    # answers with the sha directly, in this call's own return value, with
    # no shared on-disk slot for a concurrent gate to stomp on before this
    # one reads it — so two gates can still run at the same time, they just
    # no longer share a single mutable answer. The `fetch` below is now
    # purely to materialize the already-known `head`'s objects locally; its
    # own write still lands only in FETCH_HEAD (no `:` destination in the
    # refspec, exactly as before), so no branch is created and no ref the
    # user owns is moved — but nothing here reads FETCH_HEAD back anymore,
    # so a concurrent gate clobbering it can no longer cross this verdict
    # onto its head.
    head = _ls_remote_exact(repo_path, pr_ref)
    if not head:
        raise GateUnavailable(
            f"could not resolve pull request #{number}'s head on origin"
        )

    proc = _git(repo_path, "fetch", "origin", pr_ref)
    if proc.returncode != 0:
        raise GateUnavailable(
            f"could not fetch pull request #{number} from origin: "
            f"{proc.stderr.strip()}"
        )
    if not _rev_parse(repo_path, head):
        raise GateUnavailable(
            f"could not resolve pull request #{number} head {head[:7]} "
            "after fetching"
        )

    if base:
        base_ref = base
    else:
        # local_only=True here too: PR mode already made its one, expected
        # network call above (fetching the PR ref) — resolving the default
        # branch has no reason to risk a second, unbounded one via `git
        # remote show origin` when the cheap local ref read already covers
        # every normally-configured checkout.
        default_name = repo.default_branch(local_only=True)
        if not default_name:
            raise GateUnavailable(
                "no upstream to compare against: could not resolve "
                "origin/HEAD locally (this checkout may never have run "
                "`git remote set-head origin -a`); pass --base"
            )
        base_ref = f"origin/{default_name}"

    merge_base = _merge_base(repo_path, base_ref, head)
    shallow = _is_shallow_repo(repo_path)
    if not merge_base:
        if shallow:
            raise GateUnavailable(
                f"this checkout is a shallow clone: no merge base between "
                f"pull request #{number} and {base_ref} exists within the "
                "fetched history — run `git fetch --unshallow` and retry"
            )
        raise GateUnavailable(
            f"no merge base between pull request #{number} and {base_ref}"
        )
    if merge_base == head:
        if shallow:
            raise GateUnavailable(
                f"this checkout is a shallow clone: pull request #{number} "
                f"and {base_ref} resolve to the same commit within the "
                "fetched history, which may only be because the shallow "
                "history doesn't reach the true merge base — run `git "
                "fetch --unshallow` and retry"
            )
        raise GateUnavailable(
            f"pull request #{number} has no commits beyond {base_ref}"
        )

    comparison = (
        f"pull request {owner}/{name}#{number} head `{head[:7]}` against "
        f"merge base with `{base_ref}` @ `{merge_base[:7]}`"
    )
    return merge_base, head, base_ref, comparison, []


@contextmanager
def _materialized_head(repo_path: Path, sha: str):
    """Check out ``sha`` into a throwaway local clone and yield its path.

    The reviewer's citation check reads files straight off disk at whatever
    ``repo_path`` it is given (see ``reviewer._citation_fails``), comparing a
    cited line number against that file's CURRENT on-disk line count. Handing
    it the user's raw, live checkout is wrong in both modes: in PR mode the
    PR head only ever lands in ``FETCH_HEAD`` — the user's actual working
    tree stays on whatever branch they had checked out — and in branch mode
    the working tree can be dirty, so an uncommitted edit that merely
    shortens a file could silently demote a real, correctly-cited finding to
    a pass even though the reviewed diff never touched that edit. Reviewing
    against a clone detached at the exact reviewed ``sha`` closes both holes
    the same way. This clones ``repo_path`` locally (``--local --shared``:
    read-only against the source, objects are shared rather than copied)
    into a temp directory and checks out ``sha`` there, so the reviewer
    always reads the exact tree that was diffed. The clone is removed on the
    way out, success or failure.
    """
    tmp_dir = Path(tempfile.mkdtemp(prefix="no_human_gate_"))
    try:
        clone = subprocess.run(
            ["git", "clone", "--local", "--shared", "--no-checkout", "-q",
             str(repo_path), str(tmp_dir)],
            capture_output=True, text=True, env=_no_prompt_env(),
        )
        if clone.returncode != 0:
            raise GateUnavailable(
                "could not materialize the reviewed commit for review: "
                f"{clone.stderr.strip()}"
            )
        checkout = subprocess.run(
            ["git", "checkout", "--detach", "-q", sha],
            cwd=tmp_dir, capture_output=True, text=True, env=_no_prompt_env(),
        )
        if checkout.returncode != 0:
            raise GateUnavailable(
                f"could not check out {sha} for review: "
                f"{checkout.stderr.strip()}"
            )
        yield tmp_dir
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _format_tamper(tamper: "tamper_guard.TamperReport") -> str:
    """One-line tamper guard summary (`TamperReport.summary` plus its
    `reasons`), used by the `GateUnavailable` messages that surface an
    already-computed tamper result rather than drop it when a later
    precondition fails."""
    summary = tamper.summary
    if tamper.reasons:
        summary += "; " + "; ".join(tamper.reasons)
    return summary


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
    repo_path = _resolve_repo_root(Path(repo_path))

    # `load_config()` itself can raise `AuthError` (an `ANTHROPIC_API_KEY`
    # smuggled into config.yaml — `_reject_api_key_in_config`), `ValueError`
    # (an invalid `llm.role_backends` entry — `_reject_invalid_role_backends`
    # raises a bare `ValueError`, not `ConfigError`), `ConfigError` (config
    # asks for the removed decomposition path — `_reject_decomposition_
    # enabled`), or `yaml.YAMLError` (a malformed config.yaml) — all above
    # and outside `_check_credential`'s own handling of the auth check it
    # performs. Any of these is "cannot run the gate", the same as every
    # other named precondition here, not a crash that should escape as an
    # unhandled exit 1.
    try:
        # `create_if_missing=False`: a read-only gate run must never have the
        # side effect of materializing `~/.no_human/config.yaml` on a machine
        # that has never run `nh init` — see `db.py`'s identical
        # `create_if_missing=False` comment on its own `load_config` call.
        config = load_config(create_if_missing=False)
        _check_credential(config)
    except (AuthError, ConfigError, ValueError, yaml.YAMLError) as exc:
        raise GateUnavailable(f"cannot load the no_human config: {exc}") from exc

    # Every `GitRepo` call below (`head_sha`, `current_branch`, the mode
    # resolvers) runs `git` with `check=True` and raises `GitError` on a
    # non-zero exit — e.g. `rev-parse HEAD` on a repo with no commits yet.
    # That is exactly "cannot run the gate", the same as every other named
    # precondition here, and must never escape as an unhandled exception
    # (the CLI would report a generic exit 1, indistinguishable from a real
    # review FAIL, instead of refusing by name at exit 2).
    try:
        repo = GitRepo(repo_path)
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
    except GitError as exc:
        raise GateUnavailable(str(exc)) from exc

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

    # Same two refs the diff above was just computed from — never a second,
    # independent lookup by PR number — so the verdict's file list can only
    # ever describe what was actually reviewed.
    changed_files = _changed_files(repo_path, before_ref, after_ref)

    # `AdversarialReviewer.review`'s `diff_override` path now bounds an
    # over-cap diff through `_bounded_override_diff` and DISCLOSES the cut
    # files to the reviewer instead of silently dropping them (see that
    # helper's docstring) — but disclosure is not enough for a gate: a
    # verdict reached on a partial view is still a verdict on a partial
    # view. So the gate keeps its own, stricter policy: refuse by name
    # BEFORE the tamper guard runs or the reviewer is constructed (and
    # billed) at all, naming which changed files would have lost patch
    # content, so this must be exit 2 ("could not run"), never exit 1 with
    # an empty or partial checklist.
    if len(diff) > _DIFF_CAP:
        try:
            _, would_cut = budget_diff(diff, _DIFF_CAP)
        except DiffCoverageError:
            would_cut = []
        cut_note = (
            "; the patches these changed file(s) would lose: "
            + ", ".join(would_cut[:10])
            + (f" (+{len(would_cut) - 10} more)" if len(would_cut) > 10 else "")
        ) if would_cut else ""
        raise GateUnavailable(
            f"the diff is {len(diff):,} characters, over the single-turn "
            f"review cap of {_DIFF_CAP:,} characters — refusing rather than "
            "construct and bill a reviewer that would only see a truncated "
            "prefix of the change" + cut_note
        )

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
    try:
        reviewer = AdversarialReviewer.from_config(config.data)
    except (AuthError, BackendUnavailable) as exc:
        # Construction itself can fail preflight (e.g. a reviewer pinned to
        # a backend whose credential vanished between `_check_credential`'s
        # preview and now) — that is still "cannot run", not a crash. The
        # tamper guard already ran above; surface it rather than drop it, so
        # a real tamper finding is not silently lost behind this refusal.
        raise GateUnavailable(
            f"could not construct the reviewer: {exc} "
            f"(tamper guard already ran: {_format_tamper(tamper)})"
        ) from exc

    try:
        # The reviewer's citation check reads `review_repo_path` straight off
        # disk (`reviewer._citation_fails`), comparing cited line numbers
        # against whatever is on disk right now. Materialize `after_ref`
        # into a throwaway clone in BOTH modes rather than handing the
        # reviewer `repo_path` directly: in PR mode the user's checkout never
        # holds the PR head's content at all (only `FETCH_HEAD` does), and in
        # branch mode `repo_path` is the user's live working tree, which can
        # be dirty — an uncommitted edit could otherwise silently demote a
        # real citation-backed finding to a pass. See `_materialized_head`.
        with _materialized_head(repo_path, after_ref) as review_repo_path:
            decision = await reviewer.review(
                task, repo_path=review_repo_path, diff_override=diff,
                before_ref=before_ref,
            )
    except ReviewerUnavailable as exc:
        # The reviewer itself reaches "no verdict" and escalates rather than
        # guessing (reviewer.py:2611) — that means the gate did not run, the
        # same shape as every other unmet precondition here.
        raise GateUnavailable(
            f"the reviewer could not reach a verdict: {exc} "
            f"(tamper guard already ran: {_format_tamper(tamper)})"
        ) from exc
    except (AuthError, BackendUnavailable) as exc:
        # A reviewer backend whose credential/CLI vanished between
        # `_check_credential`'s preview and this call (e.g. a codex-pinned
        # reviewer whose CLI disappeared) — still "cannot run", not a crash.
        raise GateUnavailable(
            f"the reviewer backend became unusable: {exc} "
            f"(tamper guard already ran: {_format_tamper(tamper)})"
        ) from exc

    if decision.transport_error:
        # A timeout or transport failure inside the single-turn reviewer
        # (reviewer.py's `_fast_review`) comes back as an ordinary-looking
        # FAILING decision — an unclassified "timeout" checklist item that
        # `blocking_items`/`_refute_candidates` cannot tell apart from a real
        # finding (it does not match `_reached_no_verdict`'s "structured
        # output present" sentinel, the only other "gate did not really run"
        # signal `reviewer.py` exposes). Left unchecked, that is a review
        # that never happened reading as a real FAIL — the same class of bug
        # this module exists to prevent for every other unmet precondition.
        # `reviewer.py` is out of scope for this change, so the refute pass
        # it may already have run against the bogus "timeout" item cannot be
        # suppressed from here; this only fixes the verdict that reaches the
        # caller.
        if decision.checklist:
            reason = decision.checklist[0].evidence or decision.checklist[0].label
        else:
            reason = "no checklist recorded"
        raise GateUnavailable(
            f"the reviewer did not complete (transport error / timeout): "
            f"{reason} (tamper guard already ran: {_format_tamper(tamper)})"
        )

    passed = decision.passed and not tamper.tampered

    role_backend = effective_role_backend(config.data, "reviewer")

    return GateResult(
        passed=passed,
        comparison=comparison,
        before_ref=before_ref,
        after_ref=after_ref,
        mode=mode,
        tamper=tamper,
        decision=decision,
        uncommitted=uncommitted,
        truncated=False,
        reviewer_backend=role_backend["backend"],
        reviewer_model=role_backend["model"],
        reviewer_backend_is_default=role_backend["is_default"],
        changed_files=changed_files,
    )


async def review_diff(
    task: Task,
    *,
    repo_path: Path,
    diff: str,
    before_ref: str,
    after_ref: str,
    model: str | None = None,
) -> ReviewDecision:
    """Review one pre-computed diff, single-turn, no daemon and no database.

    The caller owns everything around the review — where the diff came from,
    how it was capped, the credential, and what happens to the verdict. This
    module owns the construction of the reviewer and the ``diff_override``
    call itself, so that stays in exactly one place (see
    ``test_only_one_module_constructs_the_oneshot_reviewer_call``); a second
    caller growing its own parallel path is the thing that test exists to
    stop. ``run_gate`` is the full gate for a checkout; this is the seam for
    a caller that already has the diff and its refs, such as the CI Action.

    ``model`` pins the reviewer's model explicitly; without it the reviewer
    is built from config exactly as ``run_gate`` builds it. Raises whatever
    the reviewer raises (notably ``ReviewerUnavailable`` for "no verdict"),
    so the caller decides how a non-verdict is reported.
    """
    reviewer = (
        AdversarialReviewer(model=model) if model
        else AdversarialReviewer.from_config(load_config(create_if_missing=False).data)
    )
    return await reviewer.review(
        task,
        repo_path=repo_path,
        diff_override=diff,
        before_ref=before_ref,
        after_ref=after_ref,
        single_turn=True,
        reviewed_sha=after_ref,
    )


def render_markdown(result: GateResult) -> str:
    """The pass/fail checklist with file:line citations, as Markdown."""
    lines: list[str] = []
    verdict = "PASS" if result.passed else "FAIL"
    lines.append(f"## no_human gate — {verdict}")
    lines.append(f"**Compared:** {result.comparison}")
    # Derived from the SAME before_ref..after_ref this run resolved and
    # reviewed (see `GateResult.changed_files`), never a second lookup by
    # PR number — so a crossed head resolution surfaces here as files that
    # don't belong to the named PR, instead of silently disappearing.
    files_suffix = (
        ", ".join(result.changed_files) if result.changed_files else "(none)"
    )
    lines.append(f"**Files reviewed:** {files_suffix}")
    model_suffix = f" ({result.reviewer_model})" if result.reviewer_model else ""
    override_note = (
        "" if result.reviewer_backend_is_default
        else " — overridden from the Claude default via `llm.role_backends.reviewer`"
    )
    lines.append(
        f"**Reviewer backend:** `{result.reviewer_backend}`{model_suffix}{override_note}"
    )
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
