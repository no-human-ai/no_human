"""PR comment watcher — poll open agent PRs for new human comments.

Phase C (WS-C): the only permitted human touchpoints are (1) commenting
on the PR and (2) approving/merging.  This module detects new comments and
feeds them back into the task's ``send_back_feedback`` queue so the next
attempt addresses them.

Supports GitHub (``gh`` CLI), GitLab (``glab`` CLI), and a test-friendly
injectable callable for unit tests.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import signal
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Awaitable

from .outbound_scrub import ScrubDrainedError, scrub_outbound

log = logging.getLogger("no_human.pr_watcher")


@dataclass
class PrComment:
    """A single human comment on a PR."""
    author: str
    body: str
    path: str | None = None    # file path for inline/line comments
    line: int | None = None
    diff_hunk: str | None = None
    created_at: str = ""


@dataclass
class PrFeedback:
    """Aggregated feedback from all new comments on a PR."""
    pr_url: str
    comments: list[PrComment] = field(default_factory=list)

    def to_send_back_entries(self) -> list[dict[str, Any]]:
        """Convert to the ``send_back_feedback`` format consumed by the orchestrator."""
        entries: list[dict[str, Any]] = []
        for c in self.comments:
            msg = c.body
            if c.path:
                loc = f"{c.path}"
                if c.line:
                    loc += f":{c.line}"
                msg = f"[{loc}] {msg}"
            if c.diff_hunk:
                msg += f"\n\nContext:\n```\n{c.diff_hunk[:500]}\n```"
            entries.append({
                "at": c.created_at or datetime.now(timezone.utc).isoformat(),
                "message": msg,
                "author": c.author,
                "source": "pr_comment",
                "pr_url": self.pr_url,
            })
        return entries


# ---------------------------------------------------------------------------
# GitHub comment fetcher
# ---------------------------------------------------------------------------

async def _run_cli(cmd: list[str]) -> str | None:
    """Run a CLI command; return stdout or None on failure."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        if proc.returncode != 0:
            log.debug("cmd %s failed: %s", cmd[:3], err.decode()[:200])
            return None
        return out.decode()
    except FileNotFoundError:
        log.debug("CLI not found: %s", cmd[0])
        return None


async def fetch_github_pr_comments(
    repo: str, pr_number: int, *, since: str | None = None,
    agent_login: str = "no-human[bot]", host: str | None = None,
) -> list[PrComment]:
    """Fetch PR review comments + issue comments from GitHub via ``gh`` CLI.

    ``since`` is an ISO timestamp; only comments after it are returned.
    Comments authored by ``agent_login`` are excluded (avoid self-loops).
    ``host`` targets a GitHub Enterprise host (e.g. ``code.example.com``); when
    None, ``gh`` uses its default (github.com).
    """
    if not shutil.which("gh"):
        return []

    host_args = ["--hostname", host] if host else []
    comments: list[PrComment] = []

    # 1. Review comments (line-level)
    out = await _run_cli([
        "gh", "api", *host_args,
        f"repos/{repo}/pulls/{pr_number}/comments",
        "--paginate",
    ])
    if out:
        for c in json.loads(out):
            if c.get("user", {}).get("login") == agent_login:
                continue
            created = c.get("created_at", "")
            if since and created <= since:
                continue
            comments.append(PrComment(
                author=c.get("user", {}).get("login", "unknown"),
                body=c.get("body", ""),
                path=c.get("path"),
                line=c.get("original_line") or c.get("line"),
                diff_hunk=c.get("diff_hunk"),
                created_at=created,
            ))

    # 2. Issue comments (general PR comments)
    out = await _run_cli([
        "gh", "api", *host_args,
        f"repos/{repo}/issues/{pr_number}/comments",
        "--paginate",
    ])
    if out:
        for c in json.loads(out):
            if c.get("user", {}).get("login") == agent_login:
                continue
            created = c.get("created_at", "")
            if since and created <= since:
                continue
            comments.append(PrComment(
                author=c.get("user", {}).get("login", "unknown"),
                body=c.get("body", ""),
                created_at=created,
            ))

    return comments


def gitlab_project_path(project: str) -> str:
    """Percent-encode a GitLab project for the REST API — idempotently.

    GitLab addresses a project as ``group%2Frepo``; a RAW slash splits the URL
    path and 404s. Verified against the live API (2026-08-06)::

        projects/gitlab-org%2Fgitlab-foss  -> HTTP 200
        projects/gitlab-org/gitlab-foss    -> HTTP 404

    Every project has a namespace, so this is the normal case, not an edge one.
    A 404 here is silent — ``_run_cli`` returns None, the state reads "", and a
    ``pr_merged:`` blocker stays parked forever, which is the exact failure the
    GitLab lifecycle support was added to remove.

    Callers arrive in BOTH forms and neither can be made to go away: this
    module's own :func:`parse_pr_url` already encodes, while the ``project!iid``
    short ref — GitLab's own native notation, and the only form a human or an
    LLM writes by hand — carries a raw slash. Decoding before encoding makes the
    function idempotent, so both land on the same argv, and a MIXED ref such as
    ``grp%2Fsub/proj!7`` is normalized rather than half-encoded.

    The round trip cannot lose information: a GitLab path segment is
    ``[A-Za-z0-9_.-]`` joined by ``/``, so a literal ``%`` cannot occur in one
    and can only ever be an escape introducer. ``unquote`` leaves a malformed
    escape untouched, so a stray ``%`` re-encodes to ``%25`` rather than
    silently corrupting the path.
    """
    from urllib.parse import quote, unquote
    return quote(unquote(project), safe="")


async def fetch_gitlab_mr_comments(
    project_id: str, mr_iid: int, *, since: str | None = None,
    agent_username: str = "no-human-bot",
) -> list[PrComment]:
    """Fetch MR notes from GitLab via ``glab`` CLI.

    ``project_id`` may be given either encoded (``group%2Frepo``) or as a plain
    path (``group/repo``); :func:`gitlab_project_path` normalizes it.
    """
    if not shutil.which("glab"):
        return []

    project_id = gitlab_project_path(project_id)
    comments: list[PrComment] = []
    out = await _run_cli([
        "glab", "api",
        f"projects/{project_id}/merge_requests/{mr_iid}/notes",
        "--paginate",
    ])
    if out:
        for c in json.loads(out):
            if c.get("author", {}).get("username") == agent_username:
                continue
            if c.get("system", False):
                continue
            created = c.get("created_at", "")
            if since and created <= since:
                continue
            # GitLab MR notes may have position data for inline comments.
            pos = c.get("position") or {}
            comments.append(PrComment(
                author=c.get("author", {}).get("username", "unknown"),
                body=c.get("body", ""),
                path=pos.get("new_path") or pos.get("old_path"),
                line=pos.get("new_line") or pos.get("old_line"),
                created_at=created,
            ))

    return comments


# ---------------------------------------------------------------------------
# Generic PR comment checker (for WakeWatcher integration)
# ---------------------------------------------------------------------------

# Type alias: takes "owner/repo#123" and returns list of new comments.
PrCommentChecker = Callable[[str], Awaitable[list[PrComment]]]


def parse_pr_url(url: str) -> tuple[str, str, str, int] | None:
    """Parse a PR/MR URL into ``(forge, host, slug, number)``.

    Handles GitHub/GHE (``https://<host>/<owner>/<repo>/pull/<n>``) and GitLab
    (``https://<host>/<group>/<repo>/-/merge_requests/<n>``). Returns None if it
    can't be parsed. The ``host`` lets us target GitHub Enterprise (e.g.
    ``code.example.com``) rather than defaulting to github.com.
    """
    import re
    from urllib.parse import quote
    gh = re.match(r"https?://([^/]+)/(.+?)/pull/(\d+)", url)
    if gh:
        return ("github", gh.group(1), gh.group(2), int(gh.group(3)))
    gl = re.match(r"https?://([^/]+)/(.+?)(?:/-)?/merge_requests/(\d+)", url)
    if gl:
        return ("gitlab", gl.group(1), quote(gl.group(2), safe=""), int(gl.group(3)))
    return None


async def default_pr_merged(ref: str) -> bool:
    """Resolve a ``pr_merged:`` wake condition — gh for GitHub/GHE, glab for
    GitLab.

    ``ref`` may be a full PR/MR URL, ``owner/repo#num`` (GitHub) or
    ``project!iid`` (GitLab). Unknown/unsupported → False (never falsely report
    merged). Used by serve/wake/api watchers.

    Derived from :func:`default_pr_state` rather than running its own query:
    the two used to be separate `gh` calls that could in principle disagree,
    and every forge added to one had to be remembered in the other. A GitLab MR
    returned False here forever, with no error anywhere, so a task parked on
    ``pr_merged:<MR>`` never woke.
    """
    return (await default_pr_state(ref)) == "MERGED"


async def check_pr_comments(
    pr_ref: str,
    *,
    since: str | None = None,
) -> list[PrComment]:
    """Check for new comments on a PR/MR.

    ``pr_ref`` may be:
    - a full PR/MR URL (preferred — carries the host for GitHub Enterprise)
    - GitHub short ref ``owner/repo#123``
    - GitLab short ref ``project_id!123`` (using ``!`` for MR)

    Returns new comments since ``since`` (ISO timestamp), or all if None.
    """
    if pr_ref.startswith("http"):
        parsed = parse_pr_url(pr_ref)
        if not parsed:
            log.warning("could not parse PR URL: %s", pr_ref)
            return []
        forge, host, slug, num = parsed
        if forge == "github":
            return await fetch_github_pr_comments(slug, num, since=since, host=host)
        return await fetch_gitlab_mr_comments(slug, num, since=since)

    if "!" in pr_ref:
        # GitLab MR
        project_id, _, iid_str = pr_ref.partition("!")
        try:
            iid = int(iid_str)
        except ValueError:
            log.warning("invalid GitLab MR ref: %s", pr_ref)
            return []
        return await fetch_gitlab_mr_comments(project_id, iid, since=since)

    if "#" in pr_ref:
        # GitHub PR
        repo, _, num_str = pr_ref.partition("#")
        try:
            num = int(num_str)
        except ValueError:
            log.warning("invalid GitHub PR ref: %s", pr_ref)
            return []
        return await fetch_github_pr_comments(repo, num, since=since)

    log.warning("unrecognized PR ref format: %s", pr_ref)
    return []


# ---------------------------------------------------------------------------
# Post reply comment (after agent addresses feedback)
# ---------------------------------------------------------------------------

# Invisible HTML comment stamped on every PR comment no_human posts. The
# forge renders it as nothing, so copy-pasting the RENDERED comment does not
# carry it. Copying the RAW markdown does — GitHub's "Quote reply" is exactly
# that — which is why `is_agent_comment` anchors to the start of the BODY
# instead of scanning the whole body. Load-bearing: comments are posted under
# the operator's own gh login, so author identity CANNOT distinguish the
# product's comments from the human's — the 2026-07-10 incident (the CI_GATE
# results comment resumed its own task into the budget gate) is exactly this
# gap.
AGENT_COMMENT_MARKER = "<!-- no_human-agent-comment -->"


# Every product-authored comment marker starts with this prefix
# (AGENT_COMMENT_MARKER above, the orchestrator's
# `<!-- no_human:verification-receipts -->`, and any future surface). R18
# (2026-08-10): filtering on the one agent marker let the receipts comment
# re-wake its own finished task 22 seconds after the PR opened.
_SELF_MARKER_PREFIX = "<!-- no_human"


def is_agent_comment(body: str | None) -> bool:
    """True if a PR comment body was authored by no_human itself.

    The marker must OPEN THE BODY — position 0, not merely the start of some
    line. An unanchored substring test misreads a human who used GitHub's
    "Quote reply" (it copies the raw markdown, HTML comments included, with a
    "> " on every quoted line) as the product's own comment. Anchoring
    per-LINE still misread four human shapes: a marker at column 0 inside a
    ``` fence, a human's own text followed by a raw paste at column 0 on line
    2+, and the separators `str.splitlines` honours but no forge renders as a
    line break (`\\x0c`, `\\u2028`, `\\x1c`).

    Since this verdict gates WAKING, a false "self" costs the whole review:
    the task never wakes and escalates 48h later as a timeout with the human's
    comment never delivered. Body-anchoring is safe because EVERY IN-REPO
    producer emits the marker at line 1, column 0 — `post_reply_comment`,
    `upsert_agent_comment`, the orchestrator's verification receipts, and
    `comment_poster._stamped` (which keeps it there when it inserts a location
    prefix). A marker anywhere else came from a human's keyboard. "In-repo" is
    the real bound: no tool allowlist restricts the coder/reviewer agent's Bash,
    so an agent could `gh pr comment` an unmarked body that reads as human —
    identically so under the previous per-line anchor, hence a pre-existing
    residual of the unrestricted tool, not of this predicate.

    RESIDUAL, unchanged: a human whose comment OPENS with an un-quoted raw
    paste of ours is still indistinguishable and still reads as self. That
    direction costs one skipped tick, not the review, so it is the cheaper one.
    """
    return bool(body) and body.startswith(_SELF_MARKER_PREFIX)


async def post_reply_comment(pr_ref: str, message: str) -> bool:
    """Post a reply comment on a PR/MR after addressing feedback.

    Every body is stamped with AGENT_COMMENT_MARKER so the wake watcher can
    recognize the product's own comments (see marker docstring).
    Returns True on success.
    """
    try:
        message = scrub_outbound(message, "pr_reply_comment")
    except ScrubDrainedError:
        return False
    message = f"{AGENT_COMMENT_MARKER}\n{message}"
    if "!" in pr_ref:
        project_id, _, iid_str = pr_ref.partition("!")
        project_id = gitlab_project_path(project_id)
        out = await _run_cli([
            "glab", "api", "--method", "POST",
            f"projects/{project_id}/merge_requests/{iid_str}/notes",
            "--field", f"body={message}",
        ])
        return out is not None

    if "#" in pr_ref:
        repo, _, num_str = pr_ref.partition("#")
        out = await _run_cli([
            "gh", "pr", "comment", num_str,
            "--repo", repo,
            "--body", message,
        ])
        return out is not None

    return False


def _find_marker_id(list_json: str | None, marker: str) -> str | None:
    """First comment/note id whose body contains *marker*, or None."""
    if not list_json:
        return None
    try:
        for c in json.loads(list_json):
            if marker in (c.get("body") or ""):
                return str(c.get("id"))
    except (json.JSONDecodeError, ValueError, TypeError):
        pass
    return None


async def upsert_agent_comment(pr_ref: str, message: str, key: str = "") -> bool:
    """Post OR update the ONE agent comment scoped by *key* on a PR/MR.

    Re-runs must UPDATE a single comment, not pile up: the CI_GATE gate posting a
    fresh comment on every attempt is how a PR ends up with a wall of
    near-identical comments. *key* (e.g. "ci_gate") scopes the comment so distinct
    report types each keep exactly one, updated in place. Falls back to a plain
    create if listing/patching isn't possible. Never mentions "no_human" in the
    visible body — the marker is an invisible HTML comment.
    """
    try:
        message = scrub_outbound(message, "pr_agent_comment")
    except ScrubDrainedError:
        return False
    submarker = f"<!-- nh:{key} -->" if key else ""
    body = f"{AGENT_COMMENT_MARKER}{submarker}\n{message}"
    find = submarker or AGENT_COMMENT_MARKER

    if "!" in pr_ref:  # GitLab MR: "project!iid"
        project, _, iid = pr_ref.partition("!")
        base = f"projects/{gitlab_project_path(project)}/merge_requests/{iid}/notes"
        nid = _find_marker_id(await _run_cli(["glab", "api", f"{base}?per_page=100"]), find)
        if nid and await _run_cli(["glab", "api", "--method", "PUT",
                                   f"{base}/{nid}", "--field", f"body={body}"]) is not None:
            return True
        return await _run_cli(["glab", "api", "--method", "POST", base,
                               "--field", f"body={body}"]) is not None

    if "#" in pr_ref:  # GitHub/GHE: "host/owner/repo#num"
        repo, _, num = pr_ref.partition("#")
        host, _, slug = repo.partition("/")
        hostarg = ["--hostname", host] if host else []
        listing = await _run_cli(["gh", "api", *hostarg,
                                  f"repos/{slug}/issues/{num}/comments", "--paginate"])
        cid = _find_marker_id(listing, find)
        if cid and await _run_cli(["gh", "api", *hostarg, "-X", "PATCH",
                                   f"repos/{slug}/issues/comments/{cid}",
                                   "-f", f"body={body}"]) is not None:
            return True
        return await _run_cli(["gh", "api", *hostarg, "-X", "POST",
                               f"repos/{slug}/issues/{num}/comments",
                               "-f", f"body={body}"]) is not None

    return False


#: GitLab MR ``state`` → the GitHub vocabulary the rest of this module speaks.
#: Deliberately partial: GitLab also reports ``locked``, which is neither open
#: nor finished, and guessing it into CLOSED would escalate a live MR. Anything
#: not listed falls through to "" — unknown, which callers treat as no action.
_GITLAB_MR_STATE = {"merged": "MERGED", "closed": "CLOSED", "opened": "OPEN"}


async def _gitlab_mr_state(project: str, iid: str, *, host: str = "") -> str:
    """One GitLab MR's lifecycle state via ``glab``, in GitHub's vocabulary.

    ``project`` may be encoded (``group%2Frepo``, what :func:`parse_pr_url`
    produces) or a plain path (``group/repo``, what the ``project!iid`` short
    ref carries); :func:`gitlab_project_path` normalizes both. ``host`` is
    required for self-hosted GitLab: ``glab`` defaults to gitlab.com, which is
    the exact failure ``ci/gitlab.py`` records for ``glab ci run``.
    """
    if not shutil.which("glab"):
        return ""
    project = gitlab_project_path(project)
    hostarg = ["--hostname", host] if host else []
    out = await _run_cli(
        ["glab", "api", *hostarg, f"projects/{project}/merge_requests/{iid}"]
    )
    if not out:
        return ""
    try:
        raw = json.loads(out)
    except json.JSONDecodeError:
        return ""
    if not isinstance(raw, dict):
        return ""
    return _GITLAB_MR_STATE.get(str(raw.get("state") or "").lower(), "")


async def default_pr_state(ref: str) -> str:
    """The PR/MR's lifecycle state: "MERGED" | "CLOSED" | "OPEN" | "".

    "" means unknown (no gh/glab, unparseable ref, network error, a state this
    module does not map) — callers must treat unknown as "no action", never as
    closed. An awaiting-approval task previously watched only comments, so a
    merged PR left it parked forever and a closed-unmerged PR was polled until
    the end of time.

    GitHub/GHE goes through ``gh``, GitLab through ``glab``. Before that split
    existed every GitLab ref returned "" here — silently, so an MR that had
    been merged for days looked exactly like a network blip.
    """
    if ref.startswith("http"):
        parsed = parse_pr_url(ref)
        if not parsed:
            return ""
        forge, host, slug, num = parsed
        if forge == "gitlab":
            return await _gitlab_mr_state(slug, str(num), host=host)
        repo_arg, num_str = f"{host}/{slug}", str(num)
    elif "!" in ref:                       # GitLab short ref: "project!iid"
        project, _, num_str = ref.partition("!")
        return await _gitlab_mr_state(project, num_str)
    elif "#" in ref:
        repo, _, num_str = ref.partition("#")
        repo_arg = repo
    else:
        return ""
    if not shutil.which("gh"):
        return ""
    out = await _run_cli(
        ["gh", "pr", "view", num_str, "--repo", repo_arg, "--json", "state"]
    )
    if not out:
        return ""
    try:
        return str(json.loads(out).get("state") or "").upper()
    except json.JSONDecodeError:
        return ""


async def default_pr_checks(ref: str) -> list[dict]:
    """The PR head's CI checks, normalized: [{name, status, link}].

    status ∈ "fail" | "pass" | "pending". Sources both GitHub check-runs
    (conclusion) and commit statuses (state) from statusCheckRollup — the
    Jenkins integration on code.example.com reports plain commit statuses
    (e.g. continuous-integration/jenkins/pr-head), which `gh pr checks`
    renders but scripts often miss. Empty list = unknown/no checks.
    """
    if not shutil.which("gh"):
        return []
    if ref.startswith("http"):
        parsed = parse_pr_url(ref)
        if not parsed or parsed[0] != "github":
            return []
        _, host, slug, num = parsed
        repo_arg, num_str = f"{host}/{slug}", str(num)
    elif "#" in ref:
        repo, _, num_str = ref.partition("#")
        repo_arg = repo
    else:
        return []
    out = await _run_cli([
        "gh", "pr", "view", num_str, "--repo", repo_arg,
        "--json", "statusCheckRollup",
    ])
    if not out:
        return []
    try:
        rollup = json.loads(out).get("statusCheckRollup") or []
    except json.JSONDecodeError:
        return []
    checks: list[dict] = []
    for c in rollup:
        name = c.get("name") or c.get("context") or "unnamed check"
        raw = (c.get("conclusion") or c.get("state") or "").upper()
        if raw in ("FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED"):
            status = "fail"
        elif raw in ("SUCCESS", "NEUTRAL", "SKIPPED"):
            status = "pass"
        else:  # PENDING, EXPECTED, IN_PROGRESS, QUEUED, "" (still running)
            status = "pending"
        checks.append({
            "name": name, "status": status,
            "link": c.get("targetUrl") or c.get("detailsUrl") or "",
            "required": bool(c.get("isRequired")),
        })
    return checks


def _gh_repo_and_number(ref: str) -> tuple[str, str] | None:
    """Normalize a PR ref (URL or ``owner/repo#n``) to gh's (repo_arg, number).

    GitHub/GHE only — returns None for GitLab or unparseable refs.
    """
    if ref.startswith("http"):
        parsed = parse_pr_url(ref)
        if not parsed or parsed[0] != "github":
            return None
        _, host, slug, num = parsed
        return f"{host}/{slug}", str(num)
    if "#" in ref:
        repo, _, num_str = ref.partition("#")
        return repo, num_str
    return None


async def default_pr_head(ref: str) -> str:
    """The PR's current head commit SHA, or "" (unknown must never look like
    a real head — the CI_GATE gate keys its once-per-head guard on this)."""
    if not shutil.which("gh"):
        return ""
    target = _gh_repo_and_number(ref)
    if not target:
        return ""
    repo_arg, num_str = target
    out = await _run_cli(
        ["gh", "pr", "view", num_str, "--repo", repo_arg, "--json", "headRefOid"]
    )
    if not out:
        return ""
    try:
        return str(json.loads(out).get("headRefOid") or "")
    except json.JSONDecodeError:
        return ""


async def default_pr_mergeable(ref: str) -> dict:
    """The PR head's mergeability via gh: {"mergeable": ..., "mergeStateStatus": ...}.

    ``mergeable`` is one of "MERGEABLE" | "CONFLICTING" | "UNKNOWN" | "" (gh
    missing / unparseable ref / network error — treated identically to
    "UNKNOWN" by callers: never act on it). GitHub computes ``mergeable``
    asynchronously after every push (including the rebase this rung itself
    asks for), so "UNKNOWN" is the normal state for a few seconds after a
    push, not a real signal — callers must never treat it as either resolved
    or still-conflicting.
    """
    if not shutil.which("gh"):
        return {"mergeable": "", "mergeStateStatus": ""}
    target = _gh_repo_and_number(ref)
    if not target:
        return {"mergeable": "", "mergeStateStatus": ""}
    repo_arg, num_str = target
    out = await _run_cli([
        "gh", "pr", "view", num_str, "--repo", repo_arg,
        "--json", "mergeable,mergeStateStatus",
    ])
    if not out:
        return {"mergeable": "", "mergeStateStatus": ""}
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return {"mergeable": "", "mergeStateStatus": ""}
    return {
        "mergeable": str(data.get("mergeable") or "").upper(),
        "mergeStateStatus": str(data.get("mergeStateStatus") or "").upper(),
    }


async def default_pr_files(ref: str) -> list[str]:
    """Changed file paths of the PR, or [] (unknown). The CI_GATE gate treats
    an empty list as unclassifiable and refuses latest_dev images for it only
    when runtime code might be touched — callers decide."""
    if not shutil.which("gh"):
        return []
    target = _gh_repo_and_number(ref)
    if not target:
        return []
    repo_arg, num_str = target
    out = await _run_cli(
        ["gh", "pr", "view", num_str, "--repo", repo_arg, "--json", "files"]
    )
    if not out:
        return []
    try:
        files = json.loads(out).get("files") or []
        return [f.get("path", "") for f in files if f.get("path")]
    except json.JSONDecodeError:
        return []


async def default_ci_log_excerpt(link: str) -> str:
    """A short excerpt of a failing Jenkins build's console log, or "".

    `<build>/consoleText` answers HTTP Basic with the credentials from
    ~/.no_human/.env even where the browser URL redirects to SSO — the API path
    and the human path authenticate differently, which is the whole reason this
    reaches for Basic. `link` arrives from forge-supplied PR check data (a
    check's `targetUrl`/`detailsUrl` points wherever the forge says), so both
    the credentials AND the request are scoped to the configured Jenkins
    controller (`ci_gate.jenkins_controller`, an https URL, compared as a
    scheme+host+port+path prefix): a link outside that controller gets no
    request at all — `/consoleText` is a Jenkins endpoint, and firing at an
    arbitrary host, port or path would leak credentials or make a request on
    the forge's behalf. TLS is always verified — against
    `ci_gate.jenkins_ca_bundle` (a PEM path) when set, else the system trust
    store. Best-effort by design: "" simply means the feedback carries only the
    check name + link.
    """
    if "/display/redirect" in link:
        link = link.split("/display/redirect")[0]
    from urllib.parse import unquote, urlparse

    from ..config import load_config, load_env_var

    # load_config reads YAML from disk: off the event loop, like the other
    # blocking work in this process (api/app.py uses asyncio.to_thread too).
    ci_gate = (await asyncio.to_thread(load_config)).data.get("ci_gate") or {}
    controller = urlparse((ci_gate.get("jenkins_controller") or "").strip())
    # Credentials and the fetch go ONLY to the configured Jenkins controller,
    # compared as an https scheme+host+port+path prefix. The diagnostics fire
    # only when SSO credentials exist: without them there is no excerpt to lose.
    if controller.scheme != "https" or not controller.hostname:
        if load_env_var("SSO_USERNAME"):
            log.warning(
                "SSO credentials are set but ci_gate.jenkins_controller is %s; the CI log "
                "excerpt is disabled until it is the controller's https URL",
                "empty" if not controller.geturl() else f"not an https URL ({controller.geturl()!r})")
        return ""
    prefix = (f"https://{controller.hostname.lower()}:{controller.port or 443}"
              f"{controller.path.rstrip('/')}/")
    lk = urlparse(link)
    if lk.scheme != "https" or not lk.hostname:
        log.debug("CI log excerpt skipped: %r is not an https link", link)
        return ""
    # httpx applies RFC 3986 dot-segment removal AFTER this compare, so
    # `/ctrl/../other` would pass a raw prefix test and be sent to `/other`;
    # a percent-encoded `%2e%2e` or `..%2f` is the same segment once the
    # server decodes it, and `..;x` is the same segment once a servlet
    # container strips the path parameter. A Jenkins build URL never contains
    # a dot segment or an encoded separator: refuse any, rather than normalise
    # and trust the normaliser. (A single-encoded `%2F` inside a segment is
    # refused too — Jenkins double-encodes those itself, so the common
    # multibranch URL still passes.)
    if any(seg.split(";", 1)[0] in (".", "..") or "/" in seg or "\\" in seg
           for seg in map(unquote, lk.path.split("/"))):
        log.debug("CI log excerpt skipped: %r contains a dot segment or an encoded separator", link)
        return ""
    if not f"https://{lk.hostname.lower()}:{lk.port or 443}{lk.path}".startswith(prefix):
        log.debug("CI log excerpt skipped: %s is outside the configured Jenkins controller", link)
        return ""
    # The credentials live in ~/.no_human/.env; the server process does not
    # export them, so reading os.environ alone would always come up empty.
    user = load_env_var("SSO_USERNAME")
    password = load_env_var("SSO_PASSWORD")
    if not (user and password):
        return ""
    # An internal Jenkins CA is a trust anchor to supply, never a reason to stop
    # verifying: a PEM path verifies against that CA, its absence against the
    # system store.
    verify = (ci_gate.get("jenkins_ca_bundle") or "").strip() or True
    import httpx
    try:
        async with httpx.AsyncClient(verify=verify, timeout=25, auth=(user, password)) as client:
            resp = await client.get(link.rstrip("/") + "/consoleText")
            if resp.status_code != 200:
                return ""
            text = resp.text
    except Exception as exc:  # noqa: BLE001 — a log fetch must never break the watcher
        log.warning("CI log fetch failed for %s: %s", link[:80], exc)
        return ""
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        low = ln.lower()
        if "error" in low or "exception" in low or "failure" in low:
            return "\n".join(lines[max(0, i - 2): i + 18])[:2000]
    return "\n".join(lines[-15:])[:2000]


async def default_ci_annotations(ref: str, check_name: str = "") -> str:
    """Check-run ANNOTATION text for the PR head's failing GitHub check(s), or "".

    A job GitHub blocks at START (billing wall — "recent account payments
    have failed" / "spending limit needs to be increased") never runs, so the
    job-LOG API (`default_ci_log_excerpt`'s Jenkins consoleText fetch) returns
    NOTHING for it: the failure text lives only in the check-run annotation
    (verified live via `gh api repos/<owner>/<repo>/check-runs/<id>/annotations`
    on PRs #171/#183, 2026-08-11/12 outage). GitHub/GHE only, mirroring
    `_gh_repo_and_number`: GitLab and Jenkins-reported commit statuses (no
    check-run id) return "".

    `_gh_repo_and_number` returns a HOST-PREFIXED repo arg
    (``host/owner/repo``, what every real ``pr_watch`` URL resolves to) meant
    for `gh`'s own `--repo` flag, which accepts that form. `gh api` does NOT:
    it takes a bare REST path (`repos/owner/repo/...`) and the host separately
    via `--hostname` — so the prefix is split off here the same way
    `upsert_agent_comment`'s GitHub branch already does.

    Best-effort by design, same contract as `default_ci_log_excerpt`: gh
    missing, unparseable ref, or any API/permission failure returns "".
    """
    if not shutil.which("gh"):
        return ""
    target = _gh_repo_and_number(ref)
    if not target:
        return ""
    repo_arg, _num_str = target
    host, _, slug = repo_arg.partition("/")
    hostarg = ["--hostname", host] if host else []
    sha = await default_pr_head(ref)
    if not sha:
        return ""
    out = await _run_cli([
        "gh", "api", *hostarg, f"repos/{slug}/commits/{sha}/check-runs",
    ])
    if not out:
        return ""
    try:
        runs = json.loads(out).get("check_runs") or []
    except json.JSONDecodeError:
        return ""
    texts: list[str] = []
    for run in runs:
        conclusion = str(run.get("conclusion") or "").lower()
        if conclusion not in ("failure", "timed_out", "cancelled", "action_required"):
            continue
        if check_name and run.get("name") != check_name:
            continue
        run_id = run.get("id")
        if not run_id:
            continue
        # Read-only, additive, idempotent — repeated calls change nothing.
        # Paginated (default 30/page) but left unpaginated: the billing
        # annotation is the only one on a never-started job, so one page
        # always covers it.
        ann_out = await _run_cli([
            "gh", "api", *hostarg,
            f"repos/{slug}/check-runs/{run_id}/annotations",
        ])
        if not ann_out:
            continue
        try:
            for a in json.loads(ann_out):
                text = f"{a.get('title', '')}\n{a.get('message', '')}".strip()
                if text:
                    texts.append(text)
        except json.JSONDecodeError:
            continue
    return "\n".join(texts)[:2000]


# `start_new_session` is POSIX-only and Windows rejects it outright, so it is
# read through a constant rather than passed inline (same shape as
# `testing.runner._NEW_GROUP_KWARGS`, kept local so `vcs` does not import
# `testing`). On Windows the timeout below still returns; only the tree kill
# degrades to killing git alone. UNTESTED ON WINDOWS.
_NEW_SESSION: dict[str, object] = {} if os.name == "nt" else {"start_new_session": True}

# A ceiling, not a budget: every command below is local and the slow one
# (`merge-tree` on a large repo) is seconds, not minutes. It exists because
# `merge-tree` EXECUTES the repo's custom merge drivers -- arbitrary commands
# from the USER'S repo, run up to twice per candidate base tip -- so one that
# blocks would otherwise wedge the watcher for good. Overrun reads as a
# non-zero rc, i.e. the same fail-closed False as every other git failure.
# It bounds ONE command, so the probe's real ceiling is `2 x len(tips) x` this
# -- 480s at two candidate tips, not 120s and not 240s: on the ordinary path
# BOTH driver-executing passes run for every tip. Measured with an instrumented
# driver at `_GIT_TIMEOUT=5`: 2 invocations / 4.5s for a one-tip probe, 4 / 8.6s
# for two. The `and` below short-circuits pass 2 only where pass 1 FAILS or
# times out (a driver that hangs: 1 invocation, 5.0s), so it lowers the worst
# case in the failing case alone; a driver finishing just inside the bound costs
# the full 2x (4.5s sleep: 2 invocations, 9.5s). The three `rev-parse` calls are
# bounded too, so the strict command-count ceiling is higher still -- but they
# cannot execute user code, which is why the driver passes are the ones that
# matter.
_GIT_TIMEOUT = 120.0


async def _git_rc(repo_path: str, *args: str) -> tuple[int, str]:
    """Run a local git command; return (returncode, stripped stdout)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", repo_path, *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            # Its own process group, so the timeout below can reap the merge
            # driver too. Killing git alone is not enough and not even
            # sufficient to return: the driver INHERITS git's stdout pipe, so
            # `communicate`/`wait` stay blocked until the grandchild exits —
            # measured at the driver's full runtime, i.e. no bound at all.
            **_NEW_SESSION,
        )
    except OSError:
        # FileNotFoundError (git absent) and E2BIG (argv too long on a very
        # large touched set) both land here. The caller's contract is "never
        # a false shipped", so any failure must read as a non-zero rc.
        return 1, ""
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), _GIT_TIMEOUT)
    except TimeoutError:  # asyncio.TimeoutError is an alias of it since 3.11
        # Every failure mode of the cleanup is swallowed: `os.killpg` does not
        # exist on Windows, the group may already be gone, and this runs on the
        # watcher's event loop, where a raise would take down the poll.
        with contextlib.suppress(Exception):
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        with contextlib.suppress(Exception):
            proc.kill()
        log.warning("git %s timed out after %ss in %s", args[0] if args else "",
                    _GIT_TIMEOUT, repo_path)
        return 1, ""
    return proc.returncode, out.decode("utf-8", "replace").strip()


async def refs_resolvable(repo_path: str, *refs: str) -> bool:
    """Whether every ``ref`` names something git can resolve in ``repo_path``.

    A PRECONDITION probe, not a fate probe. ``default_branch_shipped`` below
    deliberately collapses "the content is not on base" and "the check could
    not run" into a single ``False``, because its one production caller must
    never see a false "shipped". That is the right default for a caller that
    ACTS on the answer, and the wrong one for a caller that RECORDS it: a
    merged PR whose head branch was deleted afterwards — the common case, since
    a squash-merged branch has no further use — makes ``merge-base`` fail, and
    a recorder that trusted the resulting ``False`` would write "closed without
    merging" as a settled verdict about a PR that in fact landed.

    So telemetry asks this first and treats an unresolvable ref as "cannot
    tell" (``None``) rather than as evidence of absence. Uses ``rev-parse
    --verify`` with a trailing ``^{commit}`` so a ref that exists but does not
    name a commit is not mistaken for one.
    """
    if not repo_path:
        return False
    for ref in refs:
        if not ref:
            return False
        rc, _ = await _git_rc(repo_path, "rev-parse", "--verify", "--quiet",
                              f"{ref}^{{commit}}")
        if rc != 0:
            return False
    return True


async def resolve_default_branch(repo_path: str) -> str:
    """The repo's default branch, LOCAL refs only — ``""`` when it cannot be
    read.

    Order: ``origin/HEAD`` (the remote's declared default, set by `git clone`
    or `git remote set-head`), then the checkout's own current branch. Both
    are read-only and local — deliberately **no** `git remote show origin`
    (a network round trip). ``origin/HEAD`` is rarely set in practice (no
    onboarded profile on record configures it), so this usually falls
    through to the checkout's *current* branch — the parked branch of
    whatever happens to be checked out, not necessarily the project's real
    default. That makes it unreliable as an authoritative value: the caller
    (`blockers/landed_override.py`'s ``_base_hint``) uses it ONLY to compose
    non-binding advisory text in a refusal message for the
    ``pending_never_ran`` shape — never to fill in a ``base`` value. Fails
    soft to ``""``, which that caller turns into a plain hint to pass
    ``--base`` with no guess attached.
    """
    rc, out = await _git_rc(
        repo_path, "symbolic-ref", "-q", "refs/remotes/origin/HEAD")
    if rc == 0 and out:
        return out.rsplit("/", 1)[-1]
    rc, out = await _git_rc(repo_path, "rev-parse", "--abbrev-ref", "HEAD")
    if rc == 0 and out and out != "HEAD":
        return out
    return ""


async def _base_tips(repo_path: str, base: str) -> list[str]:
    """``base`` plus its remote-tracking counterpart — every ref that can
    legitimately be holding the landing.

    The checkout a watcher inspects routinely has a STALE local base. This repo
    lands every PR as an identity-normalized squash committed and pushed from a
    throwaway worktree, which advances ``refs/remotes/origin/main`` while the
    long-lived checkout's own ``main`` stays exactly where it was. Asking only
    the local ref reports a fully landed PR as absent — live on 2026-08-10, two
    tasks escalated "closed without merging" within minutes of their content
    reaching main, with local ``main`` 20+ commits behind ``origin/main``.

    Resolved through ``@{upstream}`` rather than a hardcoded ``origin/`` prefix,
    so a repo whose remote is named otherwise still works. ``@{upstream}`` names
    the ONE remote branch this base tracks -- which is the whole point, because
    the question is about the tip the PR TARGETS and no other.

    ``@{upstream}`` is defined only for a LOCAL BRANCH of that name, so a
    worktree or a fresh clone sitting on the task branch, a base created
    ``--no-track``, or a customised ``remote.origin.fetch`` all leave it unset;
    then only the local ref is offered, and if that is missing too the probe
    returns False and the task escalates spuriously.

    A ``refs/remotes/*/<base>`` fallback for that case was implemented and then
    REMOVED, deliberately. It cures noise and causes silence: in the standard
    OSS fork layout (``origin`` = the developer's fork, ``upstream`` =
    canonical) the glob returns ``origin/main`` alongside ``upstream/main``, and
    ``default_branch_shipped`` ORs across the list -- so a branch merged into
    the developer's OWN fork reports shipped for a PR that canonical never
    merged, and the CLOSED rung writes ``TaskStatus.DONE`` with no human in the
    loop. Nothing available here distinguishes the remote the PR targets from
    any other remote carrying a branch of that name. A spurious escalation has
    a human on the other end of it; a false DONE does not.
    """
    tips = [base]
    rc, upstream = await _git_rc(repo_path, "rev-parse", "--abbrev-ref",
                                 f"{base}@{{upstream}}")
    if rc == 0 and upstream and upstream != base:
        tips.append(upstream)
    return tips


async def resolve_project_default_branch(repo_path: str) -> str:
    """The repo's DECLARED default branch — ``origin/HEAD`` only, ``""`` when
    it is unset.

    Unlike ``resolve_default_branch`` above, this deliberately has **no**
    fallback to the checkout's current branch. That fallback is exactly right
    for advisory text (nothing acts on it) and exactly wrong for a value used
    to widen an ancestry check: the checkout's current branch is an accident
    of whatever a worktree happens to be parked on, and inferring a base from
    it is the false-completion shape this codebase has already been bitten by
    (see `_base_tips`'s docstring). So this probe answers a narrower question
    — "what does the remote say is the default?" — and fails soft to ``""``
    rather than guess, which its caller (`blockers/landed_override.py`)
    treats as "no default-branch candidate", never as a value to check
    ancestry against.
    """
    rc, out = await _git_rc(
        repo_path, "symbolic-ref", "-q", "refs/remotes/origin/HEAD")
    if rc == 0 and out:
        return out.rsplit("/", 1)[-1]
    return ""


async def ref_tip_sha(repo_path: str, ref: str) -> str:
    """The 12-character tip sha of ``ref`` (tried via `_base_tips`, so its
    remote-tracking counterpart is also considered), or ``""`` when neither
    resolves. Never raises — best-effort, for composing human-readable text.
    """
    if not repo_path or not ref:
        return ""
    for tip in await _base_tips(repo_path, ref):
        rc, out = await _git_rc(
            repo_path, "rev-parse", "--verify", "--quiet", f"{tip}^{{commit}}")
        if rc == 0 and out:
            return out[:12]
    return ""


#: The ONE path held out of the containment comparison, because a landing
#: RE-DERIVES it rather than taking the branch's rows, so it diverges on every
#: landing and nearly every branch touches it (changing any shipped file
#: re-pins). It is a generated ledger OVER the other files — one
#: `<sha256>  <path>` row each — and carries no content of its own.
#:
#: 🔴 A HARDCODED EXACT REPO-ROOT PATH. IT MUST NOT BECOME A PATTERN, AND IT
#: MUST NOT GROW. Every name added here is content that stops being checked; a
#: glob or basename match would let real work through under a lookalike name
#: (`docs/RELEASE_MANIFEST.txt`) — pinned by test.
#:
#: WHY EXACTLY ONE NAME. This briefly also held `EXPORT_CLASSIFICATION.txt`,
#: added by analogy rather than by measurement, and it was removed on review.
#: The manifest is PURE DERIVATION: a row is a sha256 of a file whose own path
#: is still compared, so excluding it provably hides nothing — if the file's
#: content is unlanded, the file's own path is in the residue. A classification
#: row is a DECISION (`<ship|drop>  <count>  <pattern>`), and excluding it hid
#: an unlanded `drop` — a file somebody decided to stop publishing that keeps
#: publishing — inside this repo's primary privacy mechanism. It was also never
#: required: in the measured incident (PR #183) the manifest was the sole
#: residue in both directions and the classification merged cleanly.
#:
#: NARROWED FURTHER (live class, 2026-08-14): a classification-only divergence
#: used to fall through to a rebase round or a human unconditionally, which was
#: the safe side of the trade until it collided with how the supervising
#: session actually lands multiple branches — a squash train UNION-edits every
#: landed rule's win-COUNT (`ship N pattern`) across all the branches it folds
#: in, so every branch after the first in a train diverges from the landing
#: candidate in that integer alone, forever: the count is a re-derived tally of
#: matched files, not a decision, and re-deriving it the way `RELEASE_MANIFEST
#: .txt` is re-derived is exactly what left those tasks stuck. So the ledger is
#: now compared at LINE granularity (`_classification_settled`, below) instead
#: of being all-or-nothing excluded: if EVERY differing line between the two
#: sides is the same rule's count integer — same `ship`/`drop`, same pattern,
#: only the count differs — the divergence is forgiven; ANY other difference
#: (a flipped decision, a changed or added/removed pattern, a reordered or
#: malformed line, a comment/header edit) still blocks, exactly as before. This
#: is not a subset relaxation of the name-exclusion above: the ledger's own
#: PATH is never dropped from comparison, only a specific SHAPE of content
#: divergence in it is. `test_a_classification_only_divergence_is_not_shipped`
#: still pins the ship→drop flip as unforgiven; the new count-only behaviour is
#: pinned by `test_a_classification_count_only_divergence_is_shipped`.
_GENERATED_LEDGERS = frozenset({"RELEASE_MANIFEST.txt"})

#: The one classification ledger eligible for the narrower, line-granularity
#: forgiveness above. An exact repo-root path, same doctrine as
#: `_GENERATED_LEDGERS`: never a basename or glob match, so
#: `docs/EXPORT_CLASSIFICATION.txt` does not qualify. Deliberately NOT folded
#: into `_GENERATED_LEDGERS` itself — that frozenset means "excluded outright",
#: and this path is never excluded outright, only forgiven when every
#: differing line is a count integer.
_CLASSIFICATION_LEDGER = "EXPORT_CLASSIFICATION.txt"


async def _classification_decisions(repo_path: str, ref: str) -> list[str] | None:
    """The ``ref``'s ``EXPORT_CLASSIFICATION.txt``, canonicalised line-by-line
    with each rule line's win-count integer elided — the comparison basis for
    ``_classification_settled``.

    Mirrors the grammar in ``scripts/build_public_export.py:parse_classification``
    (not imported: ``scripts/`` is outside the package) without importing it:
    a line whose ``.split()`` is exactly three fields with the first field
    ``ship``/``drop`` and the second an integer is a rule line, canonicalised
    to ``f"{decision} {pattern}"`` — the count dropped on purpose, since it is
    a derived tally, not a decision. Every other line (comments, blanks,
    malformed rule lines) is kept verbatim (``.strip()``ped), so any change to
    them still counts as a real divergence.

    Order is preserved: the file is last-matching-rule-wins, so a reordering
    changes which rule wins and is a decision change, not a count edit.

    ``None`` — "cannot tell" — on any git failure or an empty read: the file
    absent on this side (deleted, or predates the ledger), or a `git show`
    that failed outright. The caller treats ``None`` as unsettled, never as
    "no rules to compare".

    Also reused, as the public alias ``classification_decisions`` below, by
    ``vcs.derived_conflict.classification_count_only`` to decide whether a
    conflicted classification file differs ONLY by rule win-counts — that
    caller depends on this exact contract (count elided, everything else
    verbatim, order preserved), so a change here changes what that module
    considers mechanically resolvable.
    """
    rc, text = await _git_rc(repo_path, "show", f"{ref}:{_CLASSIFICATION_LEDGER}")
    if rc != 0 or not text:
        return None
    decisions: list[str] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[0] in ("ship", "drop") and parts[1].isdigit():
            decisions.append(f"{parts[0]} {parts[2]}")
        else:
            decisions.append(line.strip())
    return decisions


#: Public alias for cross-module reuse (``vcs.derived_conflict``) — same
#: function, no behaviour change. Kept private-named above because every
#: existing in-module caller predates this alias.
classification_decisions = _classification_decisions


async def _classification_settled(repo_path: str, commit: str, branch: str) -> bool:
    """Whether ``commit`` and ``branch`` carry the SAME classification
    decisions, ignoring only each rule's win-count integer.

    Both sides read via ``_classification_decisions``; ``None`` on either
    side (file absent, or any git failure) settles nothing — ``False``. This
    is direction-independent (a set of decisions is or is not the same
    regardless of which side is asked first), so ``_contained_at`` computes it
    at most once per call and reuses it for both merge directions.

    Sequence equality gives the required semantics for free: a pure count
    edit (the union-edit train case) canonicalises to identical sequences and
    settles ✅; a ship↔drop flip, a pattern change, an added/removed/reordered
    rule, or a comment/header edit all change the sequence and stay unsettled
    ❌ — the same outcome as before this function existed.
    """
    ours = await _classification_decisions(repo_path, commit)
    theirs = await _classification_decisions(repo_path, branch)
    if ours is None or theirs is None:
        return False
    return ours == theirs


async def _unsettled_paths(repo_path: str, want_tree: str, ours: str,
                           theirs: str) -> set[str] | None:
    """Paths where merging ``theirs`` into ``ours`` leaves ``want_tree`` short.

    TWO sources, unioned, because neither sees the other's cases:

    1. the TREE DIFFERENCE against ``want_tree`` — content the branch would add
       to the tip; and
    2. the CONFLICTED PATHS ``merge-tree`` names — content the merge could not
       decide, which the tree difference can be blind to.

    🔴 (2) IS LOAD-BEARING AND IS NOT A BELT-AND-BRACES DUPLICATE OF (1).
    ``merge-tree`` resolves a modify/delete conflict by KEEPING THE MODIFIED
    SIDE, so when the branch's outstanding work is a DELETION of a file base
    has since modified, the merge writes base's copy back and the merged tree
    IS the tip's tree — in BOTH directions. Measured:

        rc=1
        100644 3367afd… 1  dead.py
        100644 89710fb… 2  dead.py
        CONFLICT (modify/delete): dead.py deleted in feature and modified in
          main.  Version main of dead.py left in tree.
        git diff --name-only <tip_tree> <merged>   ->   (empty)

    On (1) alone that reads as "nothing left to contribute" and completes a
    task whose deliverable — the removal — never landed. `merge-tree` reports a
    conflict for EVERY modify/delete, so a forge's CONFLICTING actively selects
    for the shape, and "remove the deprecated module" is an ordinary task.

    ``None`` is "the question could not be asked" — never an empty set, which
    is the answer that means "the branch has nothing left to contribute" and
    would be a false ``shipped``. 128 is an unresolvable ref or unrelated
    histories, 129 a git too old for ``--write-tree``, and ``_git_rc`` itself
    reports git-absent and argv-too-long as ``(1, "")``, which is why an empty
    first line is rejected separately from the rc.

    rc **1 is accepted**, and that is the change the live incident forced: 1 is
    a CONFLICTING merge, which still writes a tree (conflict markers inlined in
    the contested blobs) as the first output field. Rejecting it outright — the
    behaviour before 2026-08-11 — is what made a ledger-only conflict
    indistinguishable from unlanded code. Accepting it is only safe BECAUSE of
    (2): the earlier version of this function justified it as "a conflict on a
    real path puts that path in the diff, because a blob carrying conflict
    markers is not the tip's blob", which is true of content conflicts and
    FALSE of modify/delete — where no marker blob is written at all.

    ``-z`` on ``merge-tree`` is what makes (2) parseable rather than guessed.
    The output is NUL-separated fields: the tree OID, then one
    ``<mode> <object> <stage>\\t<path>`` entry per conflicted stage, then an
    EMPTY field closing that section, then free-text messages. Without ``-z``
    git QUOTES unusual paths (``"w\\303\\251ird name.py"``), which would not
    match its own name in the ledger set; with it they arrive verbatim. A field
    in that section that does not parse yields ``None`` — an output shape this
    code does not understand is "cannot tell", never "nothing left".

    The empty-first-line rejection is a FAIL-FAST, not the load-bearing check,
    and is documented as such so nobody mistakes it for one: with it removed,
    the diff below runs with an empty rev argument, which git rejects —
    ``git diff --name-only --no-renames -z <tree> "" --`` exits 128 (measured)
    — so the ``rc != 0`` guard returns ``None`` anyway. It survives mutation
    testing for that reason: it saves a subprocess, it does not decide.

    ``--no-renames`` and ``-z`` are load-bearing, not hygiene. Rename detection
    reports a move as its DESTINATION alone, so a branch that moves a real file
    to a ledger's name could otherwise present as ledger-only noise; ``-z``
    turns off the quoting that would otherwise make an unusual path fail to
    match its own name in the frozenset. The trailing ``--`` keeps a tree OID
    from ever being read as a pathspec.
    """
    tree_conflicts = await merge_tree_conflicts(repo_path, ours, theirs)
    if tree_conflicts is None:
        return None
    merged, conflicted = tree_conflicts
    rc, names = await _git_rc(repo_path, "diff", "--name-only", "--no-renames",
                              "-z", want_tree, merged, "--")
    if rc != 0:
        return None
    return conflicted | {n for n in names.split("\0") if n}


async def merge_tree_conflicts(repo_path: str, ours: str,
                               theirs: str) -> tuple[str, set[str]] | None:
    """(merged tree OID, conflicted paths) for merging ``theirs`` into
    ``ours``, or ``None`` when the question could not be asked (rc not in
    (0, 1), empty tree field, or an output shape this parser does not
    recognise).

    Extracted from ``_unsettled_paths`` (see its docstring for the full
    rationale on ``-z`` parsing, why rc 1 is accepted, and why an unrecognised
    field shape must return ``None`` rather than guess). This half of that
    function — the ``merge-tree --write-tree -z`` call and its conflicted-path
    parse — is also what a derived-artefact-only conflict needs to enumerate
    the paths a mechanical regenerate could resolve, a different question from
    ``_unsettled_paths``'s "is there unlanded content", so it is shared rather
    than duplicated.
    """
    rc, out = await _git_rc(repo_path, "merge-tree", "--write-tree", "-z",
                            ours, theirs)
    if rc not in (0, 1):
        return None
    fields = out.split("\0")
    merged = fields[0].strip()
    if not merged:
        return None
    conflicted: set[str] = set()
    for field in fields[1:]:
        if not field:
            break  # the empty field closes the conflicted-path section
        meta, tab, path = field.partition("\t")
        if not tab or not path or len(meta.split()) != 3:
            return None  # unrecognised shape — refuse to answer, never assume
        conflicted.add(path)
    return merged, conflicted


async def _contained_at(repo_path: str, commit: str, branch: str) -> bool:
    """The per-candidate containment question: is ``branch`` fully contained
    in ``commit`` (both directions, generated ledgers excluded)?

    Extracted from ``default_branch_shipped`` so the question can be asked at
    an arbitrary point in history — the BASE TIP, or an earlier commit a
    later same-file edit has since made the tip diverge from — rather than
    only at the tip. Every rule documented on ``default_branch_shipped``
    (both-directions, ``_GENERATED_LEDGERS``, fail-closed on any git
    failure) applies unchanged; this is that body with ``commit`` in place
    of the base tip.
    """
    rc, tree = await _git_rc(repo_path, "rev-parse", f"{commit}^{{tree}}")
    if rc != 0 or not tree:
        return False
    # Computed at most once, lazily, and reused for both directions: the
    # line-granularity classification comparison is direction-independent, so
    # asking it twice would be a wasted `git show` pair on the common case
    # where the classification never shows up as residue at all.
    settled_classification: bool | None = None
    # Short-circuited on purpose: the second direction is a whole extra
    # merge-tree, and the first one failing already settles the answer.
    for ours, theirs in ((commit, branch), (branch, commit)):
        unsettled = await _unsettled_paths(repo_path, tree, ours, theirs)
        if unsettled is None:
            return False
        residue = unsettled - _GENERATED_LEDGERS
        # Equality, not subset-with-discard: the classification is forgiven
        # only when it is the SOLE remaining residue, so any real unlanded
        # path still fails without the extra `git show` pair ever running.
        if residue == {_CLASSIFICATION_LEDGER}:
            if settled_classification is None:
                settled_classification = await _classification_settled(
                    repo_path, commit, branch)
            if settled_classification:
                residue = set()
        if residue:
            return False
    return True


async def containment_residue(repo_path: str, commit: str, branch: str) -> list[str] | None:
    """Paths that keep ``branch`` from being contained at ``commit`` — the
    honest reason automated containment refused, exposed for the audit
    trail a human override records. ``None`` means the question could not be
    asked at all (a git failure); ``[]`` means ``branch`` IS contained.

    Mirrors ``_contained_at``'s body (both directions, ``_GENERATED_LEDGERS``,
    classification-settled forgiveness) but *returns* the residue rather than
    reducing it to a bool. Deliberately NOT a refactor of ``_contained_at`` to
    call this and check emptiness: ``_contained_at`` short-circuits on the
    FIRST direction that fails, because for a plain yes/no that is the
    cheaper answer (one `merge-tree` instead of two on the common failing
    case); this function needs to see BOTH directions before it can report
    the full, honest residue, so it always evaluates both and unions them.
    Do not fold this back into a shared body without keeping that split.
    """
    rc, tree = await _git_rc(repo_path, "rev-parse", f"{commit}^{{tree}}")
    if rc != 0 or not tree:
        return None
    settled_classification: bool | None = None
    union: set[str] = set()
    for ours, theirs in ((commit, branch), (branch, commit)):
        unsettled = await _unsettled_paths(repo_path, tree, ours, theirs)
        if unsettled is None:
            return None
        residue = unsettled - _GENERATED_LEDGERS
        if residue == {_CLASSIFICATION_LEDGER}:
            if settled_classification is None:
                settled_classification = await _classification_settled(
                    repo_path, commit, branch)
            if settled_classification:
                residue = set()
        union |= residue
    return sorted(union)


async def _branch_paths(repo_path: str, base_tip: str, branch: str) -> list[str] | None:
    """Paths ``branch`` has touched since it diverged from ``base_tip`` —
    the path filter that keeps the history scan in ``branch_landed_commit``
    cheap: only commits that touched one of these paths can possibly be the
    commit where this branch's content landed.

    ``None`` on any git failure ("cannot tell"), which the caller must treat
    as "do not scan" rather than "nothing to filter on" — widening to the
    whole history on a git failure would defeat the cost bound the path
    filter exists for.
    """
    rc, mb = await _git_rc(repo_path, "merge-base", base_tip, branch)
    if rc != 0 or not mb:
        return None
    rc, out = await _git_rc(repo_path, "diff", "--name-only", "--no-renames",
                            "-z", mb, branch, "--")
    if rc != 0:
        return None
    return [n for n in out.split("\0") if n]


#: How far back ``branch_landed_commit`` scans a base tip's first-parent
#: history, once the tip itself no longer contains the branch, looking for
#: the commit where it WAS landed. A module constant, not a config key: a
#: user's `blockers:` section in config.yaml REPLACES the defaults map
#: wholesale (the deep-merge shadowing trap), so a config key here could
#: silently resolve to an unset/zero depth and disable the fix it exists for.
_LANDING_SCAN_DEPTH = 50


async def commit_is_ancestor(repo_path: str, sha: str, base: str = "main") -> bool:
    """Whether ``sha`` is an ancestor of ``base`` (or its upstream) — the
    cheap re-check for a previously recorded ``landed_sha``: no merge-tree at
    all, just ``git merge-base --is-ancestor``. False on any failure or if
    ``sha`` is no longer reachable (fail-closed, same contract as every
    other probe in this module — a caller falls back to the full scan)."""
    if not repo_path or not sha or not base:
        return False
    for tip in await _base_tips(repo_path, base):
        rc, _ = await _git_rc(repo_path, "merge-base", "--is-ancestor", sha, tip)
        if rc == 0:
            return True
    return False


async def branch_landed_commit(
    repo_path: str, branch: str, base: str = "main", *,
    landed_sha: str | None = None, depth: int = _LANDING_SCAN_DEPTH,
) -> str | None:
    """Where ``branch``'s content actually landed, or ``None`` if it did not
    (or the question could not be asked). ``default_branch_shipped`` is now a
    thin ``bool()`` wrapper over this.

    THE DEFECT THIS FIXES. ``default_branch_shipped`` used to ask containment
    only at the base tip. A landing routinely predates OTHER commits that
    later touch the same file (a follow-up task, an unrelated edit) — once
    one does, the tip's tree no longer equals what merging the branch in
    would produce, even though the branch's own content landed cleanly
    earlier and was never disturbed. Live incident 2026-08-12: two squash
    trains landed back-to-back, both touching ``orchestrator.py``; the
    EARLIER one's task re-escalated "closed without merging" the moment the
    LATER one landed, because the probe kept asking the question at a tip
    that had moved on. The honest question is "did this land, ever", not
    "is it still exactly what the tip would produce today".

    ORDER OF ATTEMPTS, cheapest and most certain first:

    1. ``landed_sha``, if given and still an ancestor of a base tip
       (``commit_is_ancestor``) — the steady-state path once a landing has
       been recorded once: no merge-tree at all.
    2. Each tip from ``_base_tips`` (unchanged: base plus its configured
       upstream) — ``_contained_at(tip, branch)``. This is the ORIGINAL
       check, asked at the tip, and covers every branch that has not been
       disturbed by a later same-file edit.
    3. Failing that, a bounded, PATH-FILTERED walk of the tip's first-parent
       history: ``_branch_paths`` names what ``branch`` touched, and only
       commits that touched one of those paths are even candidates
       (``git rev-list --first-parent -n depth <tip> -- <paths>``) — so the
       cost is bounded by how many of the branch's own files anyone else
       ever re-touches, not by how much history exists. Each candidate is
       asked the same ``_contained_at`` question a later commit's edits to
       an UNRELATED file can never make it fail. ``_branch_paths`` returning
       ``None`` (git failure) skips the scan for that tip entirely — it
       never widens to the whole history.

    Returns ``None`` (never a manufactured SHA) the moment every tip and
    every candidate has been asked and failed, or on the same preconditions
    ``default_branch_shipped`` refuses on (missing repo/branch/base).
    """
    if not repo_path or not branch or not base:
        return None
    if landed_sha and await commit_is_ancestor(repo_path, landed_sha, base):
        return landed_sha
    for tip in await _base_tips(repo_path, base):
        rc, tip_sha = await _git_rc(repo_path, "rev-parse", tip)
        if rc != 0 or not tip_sha:
            continue
        if await _contained_at(repo_path, tip_sha, branch):
            return tip_sha
        paths = await _branch_paths(repo_path, tip_sha, branch)
        if not paths:
            continue  # cannot tell (None), or branch has nothing to filter on
        rc, out = await _git_rc(
            repo_path, "rev-list", "--first-parent", "-n", str(depth),
            tip_sha, "--", *paths)
        if rc != 0 or not out:
            continue
        for candidate in out.splitlines():
            candidate = candidate.strip()
            if candidate and await _contained_at(repo_path, candidate, branch):
                return candidate
    return None


#: How far back `orphan_landed_evidence`'s squash-subject scan walks a base
#: tip's first-parent history looking for a `(#N)` PR reference. A module
#: constant for the same reason as `_LANDING_SCAN_DEPTH`: a user's
#: `blockers:` config section replaces the defaults map wholesale, so a
#: config key here could silently resolve to an unset/zero depth.
_ORPHAN_SUBJECT_SCAN_DEPTH = 500


async def orphan_landed_evidence(
    repo_path: str, base: str, *, commit_sha: str = "", pr_url: str = "",
    depth: int = _ORPHAN_SUBJECT_SCAN_DEPTH,
) -> dict | None:
    """Did an orphaned attempt's recorded work land on *base* — LOCAL GIT
    ONLY, never the network. The one probe `Scheduler._reconcile_landed_orphan`
    is allowed to call from the scheduler tick (see that method): unlike
    `default_pr_state`/`default_pr_merged`, this never shells out to `gh`/
    `glab`, so a caller on the tick's hot path stays network-free.

    Fail-closed on every ambiguity, the same contract as every other probe in
    this module: returns ``None``, never a manufactured sha, the moment the
    evidence is anything less than certain.

    1. PRECONDITIONS: `repo_path`, `base`, and at least one of `commit_sha`/
       `pr_url` — otherwise ``None``. `base` is never defaulted to
       ``"main"`` — mirrors `blockers/shipped.py`'s documented refusal to
       guess a base; a caller with no recorded `base_branch` has nothing
       honest to check ancestry against.
    2. ANCESTRY: if `commit_sha` and `commit_is_ancestor(repo_path,
       commit_sha, base)` — the attempt's own commit is still reachable from
       a base tip (the branch sha survived, e.g. a fast-forward or a rebase
       merge) — return ``{"kind": "commit", "sha": commit_sha, "base": base}``.
    3. SQUASH-SUBJECT: this repo (and GitHub generally) lands PRs as a squash
       commit that carries none of the branch's own commits, so ancestry
       above is blind to the common case. If `pr_url` parses (`parse_pr_url`)
       to a PR number `n`, walk each tip from `_base_tips(repo_path, base)`
       with `git log --first-parent -n depth --format=%H%x00%s <tip>` and
       return `{"kind": "pr", "sha": <that commit>, "pr": n, "base": base}`
       for the first subject matching the ANCHORED pattern `\\(#n\\)(\\s|$)`
       — the GitHub/`nh` squash-merge subject suffix. Anchoring matters: an
       unanchored `#12` would match `(#123)` too, misattributing PR 123's
       landing to PR 12 (or, per the prefix case below, PR 4 to `(#42)`).
    4. Anything else — a git command failing, no ancestry, no subject match
       — ``None``.

    Only `_git_rc` (a local `git` subprocess rooted at `repo_path`) is used.
    `_base_tips` is local-refs-only by construction (see its docstring),
    which is what keeps this network-free. The forge PR states
    (`open|closed|merged|draft`, GitLab's `opened|merged|closed|locked`) are
    deliberately NOT consulted here — that is what `default_pr_state`/
    `default_pr_merged` are for, and they are not safe to call from a
    scheduler tick.
    """
    if not repo_path or not base or not (commit_sha or pr_url):
        return None
    if commit_sha and await commit_is_ancestor(repo_path, commit_sha, base):
        return {"kind": "commit", "sha": commit_sha, "base": base}
    parsed = parse_pr_url(pr_url) if pr_url else None
    if not parsed:
        return None
    n = parsed[3]
    pattern = re.compile(rf"\(#{n}\)(\s|$)")
    for tip in await _base_tips(repo_path, base):
        rc, out = await _git_rc(
            repo_path, "log", "--first-parent", "-n", str(depth),
            "--format=%H%x00%s", tip)
        if rc != 0 or not out:
            continue
        for line in out.splitlines():
            sha, _, subject = line.partition("\x00")
            if sha and pattern.search(subject):
                return {"kind": "pr", "sha": sha, "pr": n, "base": base}
    return None


#: Matches a bare git commit sha (full 40-hex or the git-standard 9-hex
#: abbreviation) as a whole token — the `(?<!...)`/`(?!...)` hex-boundary
#: guards keep this from matching the *middle* of a longer hex run.
_SHA_TOKEN = re.compile(r"(?<![0-9a-fA-F])([0-9a-fA-F]{40}|[0-9a-fA-F]{9})(?![0-9a-fA-F])")

#: Bounds how many candidate shas `landing_sha_candidates` can hand back, so
#: a caller that probes each one with `commit_is_ancestor` (a git subprocess)
#: has a fixed worst case per row rather than one proportional to however
#: much free text a `cancel_reason` happens to contain.
_MAX_LANDING_SHA_CANDIDATES = 5


def landing_sha_candidates(text: str) -> list[str]:
    """Pull candidate commit shas out of free text (e.g. a `cancel_reason`).

    PURE — no git, no network, no I/O of any kind; just a regex scan. This is
    deliberately the *only* thing free text is ever allowed to yield: a sha
    to hand to `orphan_landed_evidence`'s existing `commit_sha=` ancestry
    check. It never extracts or resolves a PR *number* from text — that
    would require a forge-API lookup to turn into a commit, which is exactly
    the free-text-PR-number-to-commit question this feature does not answer.

    De-duplicated, case-normalised to lowercase, in first-seen order within
    each length class, all 40-hex matches before any 9-hex match (a full sha
    is unambiguous; a 9-hex one is a probabilistic abbreviation), and capped
    at `_MAX_LANDING_SHA_CANDIDATES`.
    """
    if not text:
        return []
    seen: set[str] = set()
    long_matches: list[str] = []
    short_matches: list[str] = []
    for m in _SHA_TOKEN.finditer(text):
        token = m.group(1).lower()
        if token in seen:
            continue
        seen.add(token)
        (long_matches if len(token) == 40 else short_matches).append(token)
    return (long_matches + short_matches)[:_MAX_LANDING_SHA_CANDIDATES]


async def default_branch_shipped(repo_path: str, branch: str, base: str = "main") -> bool:
    """Whether ``branch``'s changes are actually present in ``base``, checked
    by tree CONTENT rather than commit ancestry.

    A branch's changes routinely land on ``base`` as a SQUASH merge — one
    brand-new commit carrying the content but none of the branch's commits.
    When they do, `git merge-base --is-ancestor <branch> <base>` is FALSE even
    though the change is fully landed: ancestry tracks commit lineage, and a
    squash commit has no lineage back to the branch it came from. Ancestry is
    therefore not a valid "did this ship" test here.

    Instead, ask whether the branch still has anything left to contribute: `git
    merge-tree --write-tree <base-tip> <branch>` builds the tree that merging
    the branch into the tip would produce, and the branch has landed when that
    merge leaves NOTHING UNSETTLED that matters — no tree difference against
    the tip AND no conflicted path (see `_unsettled_paths`; a merged tree can
    equal the tip's while a conflict hides an unlanded deletion). However the
    commit graph got there.

    "THAT MATTERS" IS ONE NAMED GENERATED LEDGER AND NOTHING ELSE
    (`_GENERATED_LEDGERS`). This was an exact tree equality until a live
    incident on 2026-08-11 showed the feature could essentially never fire on
    this repo's own PRs. Task 5ef97879's PR #183 was fully landed on main as
    squash 5433835d8; the probe said NOT shipped and the task escalated
    "abandon or rework?". Measured on the real refs:

        git merge-tree --write-tree origin/main no-human/5ef97879-2   -> rc=1
          CONFLICT (content): Merge conflict in RELEASE_MANIFEST.txt
        git diff --name-only <origin/main^{tree}> <merged>
          -> RELEASE_MANIFEST.txt

    …in both directions, with that one path the ONLY difference (the
    classification ledger merged cleanly and was never implicated). The repo's
    merge-result rule RE-DERIVES `RELEASE_MANIFEST.txt` at landing rather than
    taking the branch's rows, so a branch's copy and the landed copy disagree
    by construction — and nearly every branch touches it, because adding or
    changing any shipped file re-pins. The real files' content identity is the
    true signal; a derived ledger's rows are a restatement of it that the
    landing rewrites.

    WHAT EXCLUDING IT DOES NOT COST. The manifest is pure derivation: a row is
    a sha256 of a file whose own path is still compared, so if that file's
    content is unlanded its own path is in the residue and the answer is False
    anyway. Excluding it provably hides nothing.

    An earlier revision of this also excluded `EXPORT_CLASSIFICATION.txt`, by
    analogy rather than by measurement, and it was REMOVED on review — the
    residual it carried is gone with it, and this paragraph records the trade
    so nobody re-adds the name. A classification row is a DECISION
    (`<ship|drop>  <count>  <pattern>`), not a derivation, so excluding it hid
    an unlanded `drop` — a file somebody decided to stop publishing that goes
    on publishing — inside this repo's primary privacy mechanism. The obvious
    sharper guard (require the branch's decision LINES to be present on base)
    does not work as a byte comparison, because the win-count is IN-BAND and
    every landing rewrites the text of rows it decided nothing about.
    `test_a_classification_only_divergence_is_not_shipped` still pins the
    ship→drop flip as unforgiven, and
    `test_the_exclusion_is_one_exact_path_not_a_name_pattern` still pins the
    exact-path boundary.

    NARROWED FURTHER (live class, 2026-08-14): the byte comparison above is too
    blunt for how this repo actually lands multiple branches — a supervising
    squash train UNION-edits every rule's win-count across all the branches it
    folds in, so every branch after the first in a train diverges from its own
    landing candidate in that integer alone, forever, and `branch_landed_commit`
    never returns for it. `_classification_settled` now compares the ledger at
    LINE granularity instead of all-or-nothing: each rule line's count is
    elided before comparing (`_classification_decisions`), so a pure count
    edit settles and a changed pattern, a flipped decision, an added/removed/
    reordered rule, or a comment edit does not — see `_GENERATED_LEDGERS` for
    the full accounting and `test_a_classification_count_only_divergence_is_
    shipped` for the pin.

    That replaced a per-file blob comparison over the branch's touched paths,
    which demanded BYTE EQUALITY on every one of them and so could never
    recognise a PR touching a file some later landing had extended. The live
    case is ``RELEASE_MANIFEST.txt``: every merge rewrites that generated
    checksum list, so a PR that edits it is byte-different from base the moment
    anything else lands, and task 2f29209f escalated with its content already on
    main. A three-way merge answers containment instead of equality, and it
    needs no pathspecs. A no-op merge writes no new objects, because the trees
    it would write already exist.

    (That paragraph used to end "…which also retires the `--no-renames` / `-z`
    / trailing-`--` hazards the old path had to work around". It no longer
    does, and the correction matters: comparing the merged tree to the tip's
    by NAME, rather than by a single OID, brings all three back. They are
    handled explicitly in ``_unsettled_paths`` — read that before touching the
    diff invocation.)

    Both the local base and its upstream are tried; see ``_base_tips``.

    The merge is run in BOTH directions, and both must leave nothing unsettled
    outside the excluded ledger. One direction is not enough because `merge-tree` honours `.gitattributes`:
    a repo that routes a path to a merge driver DISCARDING the incoming side
    (`f.py merge=keepours` + `merge.keepours.driver = true`, the documented way
    to own a generated or lockfile-shaped path) resolves every contested path
    to whichever commit was named FIRST -- exit 0, and the tree written is the
    tip's own, for content that never landed. `pr_watcher` runs against the
    USER'S repo, so any customer repo may carry such a driver, and the caller
    turns a true here into `TaskStatus.DONE` with no human in the loop. Merging
    the other way is what the question actually asks -- "does the branch have
    anything base lacks?"

    WHAT THAT DOES AND DOES NOT COVER -- stated precisely, because the rule is
    narrower than "a merge driver cannot manufacture a landing":

    * COVERED: drivers that resolve by POSITION, i.e. by which side git handed
      them as %A (ours) and which as %B (theirs). Keep-ours (`true`) yields the
      tip's tree on the forward pass and the BRANCH's tree on the reverse one;
      keep-theirs (`cp %B %A`) yields them the other way round. Neither can
      answer both passes with the tip's tree, so swapping the arguments defeats
      the whole class. The attribute SOURCE does not matter, which
      `.gitattributes`-specific neutralisation (`--attr-source`,
      `core.attributesFile`) could not say: `$GIT_DIR/info/attributes` outranks
      both and no override switches it off. Pinned by test against
      `.gitattributes`, `$GIT_DIR/info/attributes`, `core.attributesFile`, a
      `*` wildcard, and attributes committed on the BASE side only -- each
      asserting the driver actually ran. Attributes that are not present in the
      CHECKOUT are never consulted: `merge-tree` reads the CHECKOUT's
      attributes, not either commit's. So a rule committed on the BRANCH side
      only is a weaker no rather than a defeated driver *while the checkout
      sits on base* -- which `task.repo_path` (`blockers/wake.py`) usually
      does, being the user's long-lived checkout -- and IS consulted once a
      human checks the agent's branch out. Verified in both configurations;
      the answer is False either way, because the both-directions rule defeats
      the driver wherever it does fire.

    * NOT COVERED, and this is a KNOWN, UNFIXED LIMITATION: drivers that
      resolve by CONTENT rather than position -- a constant emitter
      (`cp /some/fixed/file %A`), a regenerator that rebuilds the path from
      elsewhere in the tree, or a rule-picker (`if grep -q MARKER %A; then
      cp %B %A; fi`). Those are direction-INDEPENDENT by construction: they
      write the same bytes whichever side is %A, so if that output happens to
      equal the tip's blob they satisfy both passes and this returns True for
      content that never landed -- a silent `TaskStatus.DONE` on undelivered
      work, the same failure mode the position case had. Both shapes are
      confirmed live, and both behave identically on the single-direction
      predecessor, so this is a pre-existing hole that the both-directions rule
      narrows rather than closes. Closing it needs a merge engine that ignores
      merge drivers entirely, which `merge-tree` does not offer at any flag;
      that is out of scope here. Do not restate this rule as "a merge driver
      cannot manufacture a landing": the true claim is "a POSITION-resolving
      merge driver cannot". `test_a_content_resolving_merge_driver_is_a_known_
      residual` pins the hole so a later fix cannot land without this paragraph
      being revisited.

    * NOT COVERED, in the other direction: a GENUINE landing on a path owned by
      a POSITION-resolving driver, where base has since moved on, now reads
      False. Once a later task edits the same path, both sides carry an edit
      git must merge, so the driver fires: the forward pass keeps the tip's
      blob and answers True, the reverse pass keeps the BRANCH's blob and
      answers False, and the probe reports absent for work that really shipped.
      That is a spurious escalation with a human on the other end of it, never
      a silent DONE -- the same asymmetry the both-directions rule exists to
      buy, paid in the other currency. It is not curable here for the same
      reason as the bullet above: it needs a merge that ignores merge drivers.
      `test_a_genuine_landing_under_a_position_driver_reads_absent_once_base_
      moves` and its no-driver control pin the pair, so this cannot rot
      silently either.

    Returns False (never a false "shipped") on any git failure: missing repo,
    missing/deleted branch, unrelated histories, a conflicting merge, or a git
    older than 2.38 (no ``--write-tree``) -- callers must treat False as
    "can't tell", same as GitHub's PR state going unknown.

    TIP-ANCHORED, WHICH WAS ITSELF A DEFECT (fixed 2026-08-12): this used to
    ask the containment question ONLY at the base tip, so a later commit that
    touched the same file as the branch -- a follow-up task, an unrelated
    edit, nothing to do with this branch's own work -- made a genuinely
    landed PR read as absent again. This is now a thin ``bool()`` wrapper
    over ``branch_landed_commit``, which asks the same question at the tip
    FIRST (unchanged cost for the common case) and, only if that fails, at
    each candidate in a bounded, path-filtered walk of the tip's history —
    see that function's docstring for the walk and its cost bound. Every rule
    documented above (both-directions, ``_GENERATED_LEDGERS``, the merge-
    driver limits, fail-closed on any git failure) is unchanged; it now
    applies at whichever commit is being asked about, not only the tip.
    """
    return bool(await branch_landed_commit(repo_path, branch, base))
