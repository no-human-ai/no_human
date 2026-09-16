"""Post a review comment to the RIGHT PR/MR on the RIGHT forge.

A cross-repo code review spans change sets on *different* version
control systems — e.g. acme-test on code.example.com (GitHub Enterprise) and two
metrics-core MRs on gitlab.acme.net (GitLab). A finding must go to the change set
that actually contains its file, using that forge's API:
  - GitHub / GHE → ``gh api`` (inline review comment, falling back to an issue
    comment when the line isn't in the diff)
  - GitLab      → ``glab api`` (a merge-request note)

The attribution (which PR owns a finding's file) and file extraction are pure and
unit-tested here; the posting shells out and is covered at the endpoint.
"""

from __future__ import annotations

import re
import subprocess

from .outbound_scrub import ScrubDrainedError, scrub_outbound
from .pr_watcher import AGENT_COMMENT_MARKER, is_agent_comment, parse_pr_url

_DIFF_FILE = re.compile(r"^\+\+\+ b/(.+)$", re.MULTILINE)
_DIFF_GIT = re.compile(r"^diff --git a/.+ b/(.+)$", re.MULTILINE)


def files_in_diff(diff: str) -> list[str]:
    """Every file path touched in a unified diff (the ``b/`` side)."""
    files: list[str] = []
    seen: set[str] = set()
    for pat in (_DIFF_FILE, _DIFF_GIT):
        for m in pat.finditer(diff or ""):
            f = m.group(1).strip()
            if f and f != "/dev/null" and f not in seen:
                seen.add(f)
                files.append(f)
    return files


def pick_pr_for_file(file: str, pr_files: dict[str, list[str]], fallback: str | None) -> str | None:
    """Return the PR/MR URL whose change set contains *file*.

    The reviewer may cite a path more or less qualified than the diff's (a
    trailing-segment match handles ``a/b/x.yaml`` vs ``b/x.yaml``). Falls back to
    *fallback* (the anchor PR) when nothing matches — better a landed comment on
    the anchor than a lost one.
    """
    if not file:
        return fallback
    for url, files in (pr_files or {}).items():
        for f in files:
            if f == file or f.endswith("/" + file) or file.endswith("/" + f):
                return url
    return fallback


def _run(argv: list[str], timeout: int = 15) -> tuple[bool, str]:
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace")
        if p.returncode == 0:
            return True, ""
        return False, (p.stderr.strip() or p.stdout.strip())[:300]
    except subprocess.TimeoutExpired:
        return False, "timeout"


def _stamped(body: str, loc: str = "") -> str:
    """*body* with a self-marker in front, and *loc* leading the visible text.

    Comments are posted under the operator's own forge login, so author identity
    cannot tell the product's comments from the human's — the wake watcher does
    it with the `<!-- no_human` marker family (`pr_watcher.is_agent_comment`).
    An UNMARKED comment this module posts on a watched PR therefore reads as
    human feedback and re-wakes the task that posted it (R18, 2026-08-10: the
    abandoned-draft note and approved draft review comments both did). Stamped
    here, at the one function that talks to a forge, so a new caller cannot
    forget. A body that already carries a family marker (the verification
    receipts comment) is left alone — but *loc* still goes AFTER that marker's
    line, because `is_agent_comment` requires the marker to open the BODY and a
    prefix would push it off position 0, un-marking our own comment.
    """
    if not is_agent_comment(body):
        return f"{AGENT_COMMENT_MARKER}\n{loc}{body}"
    marker_line, sep, rest = body.partition("\n")
    return f"{marker_line}{sep}{loc}{rest}"


def post_to_pr(url: str, body: str, file: str | None = None, line: int | None = None) -> dict:
    """Post *body* to the PR/MR at *url* using its forge's API.

    Returns ``{"ok": bool, "mode": "inline"|"issue_comment"|"mr_note", "error": str}``.
    Every body is stamped with a self-marker — see `_stamped`.
    """
    try:
        body = scrub_outbound(body, "pr_comment")
    except ScrubDrainedError as e:
        return {"ok": False, "mode": "refused", "error": str(e)}
    parsed = parse_pr_url(url)
    if not parsed:
        return {"ok": False, "mode": None, "error": f"unparseable PR URL: {url}"}
    forge, host, slug, number = parsed
    host_args = ["--hostname", host]
    # Location prefix for the non-inline fallbacks, where the forge does not
    # anchor the comment to the line itself.
    loc = f"`{file}:{line}` — " if file and line else ""

    if forge == "gitlab":
        # A real review comments ON the code line. GitLab inline = a Discussion
        # with a diff position (the MR's base/start/head SHAs + file + line).
        if file and line and line > 0:
            ok, err = _post_gitlab_inline(host, slug, number, _stamped(body), file, line)
            if ok:
                return {"ok": True, "mode": "inline", "error": ""}
        # Fall back to a general MR note (location prefixed) when there's no line
        # or the position isn't a valid diff line.
        ok, err = _run(["glab", "api", *host_args, "-X", "POST",
                        f"projects/{slug}/merge_requests/{number}/notes",
                        "-f", f"body={_stamped(body, loc)}"])
        return {"ok": ok, "mode": "mr_note", "error": err}

    # GitHub / GHE. Try an inline review comment when we have a file:line.
    if file and line and line > 0:
        ok_sha, sha = _head_sha(host_args, slug, number)
        if ok_sha and sha:
            ok, err = _run(["gh", "api", *host_args, "-X", "POST",
                            f"repos/{slug}/pulls/{number}/comments",
                            "-f", f"body={_stamped(body)}", "-f", f"commit_id={sha}",
                            "-f", f"path={file}", "-F", f"line={line}",
                            "-f", "side=RIGHT"])
            if ok:
                return {"ok": True, "mode": "inline", "error": ""}
        # Inline failed (line not in this PR's diff, or SHA fetch failed): fall
        # back to a general issue comment with the location prefixed.
        ok, err = _run(["gh", "api", *host_args, "-X", "POST",
                        f"repos/{slug}/issues/{number}/comments",
                        "-f", f"body={_stamped(body, loc)}"])
        return {"ok": ok, "mode": "issue_comment", "error": err}

    ok, err = _run(["gh", "api", *host_args, "-X", "POST",
                    f"repos/{slug}/issues/{number}/comments",
                    "-f", f"body={_stamped(body)}"])
    return {"ok": ok, "mode": "issue_comment", "error": err}


def set_pr_title(url: str, title: str) -> dict:
    """Retitle the PR/MR at *url* on its own forge.

    Returns ``{"ok": bool, "error": str}``. Used to mark a draft that an
    attempt abandoned, so a human scanning a repo's open PRs can tell the live
    one from the corpses — three drafts from one task, all claiming their
    criteria met, is a real thing this product shipped.

    Retitling is not merging and not approving: it changes no code, no state
    the forge gates on, and touches only a PR no_human itself opened.
    """
    try:
        title = scrub_outbound(title, "pr_title_retitle")
    except ScrubDrainedError as e:
        return {"ok": False, "error": str(e)}
    parsed = parse_pr_url(url)
    if not parsed:
        return {"ok": False, "error": f"unparseable PR URL: {url}"}
    forge, host, slug, number = parsed
    if forge == "gitlab":
        ok, err = _run(["glab", "api", "--hostname", host, "-X", "PUT",
                        f"projects/{slug}/merge_requests/{number}",
                        "-f", f"title={title}"])
        return {"ok": ok, "error": err}
    ok, err = _run(["gh", "api", "--hostname", host, "-X", "PATCH",
                    f"repos/{slug}/pulls/{number}", "-f", f"title={title}"])
    return {"ok": ok, "error": err}


def close_pr(url: str) -> dict:
    """Close the PR/MR at *url*. No comment is posted — see the b1fd13ca hazard.

    Returns ``{"ok": bool, "error": str}``. Deliberately not ``gh pr close``:
    that needs a repo cwd and can post a `--comment`; the raw API call matches
    `set_pr_title`'s existing shape and cannot emit a comment.

    Closing an already-closed PR is a no-op (200) — idempotent, safe on
    retry/resume. Never raises: failure is reported in the return value, same
    contract as `set_pr_title`.
    """
    parsed = parse_pr_url(url)
    if not parsed:
        return {"ok": False, "error": f"unparseable PR URL: {url}"}
    forge, host, slug, number = parsed
    if forge == "gitlab":
        ok, err = _run(["glab", "api", "--hostname", host, "-X", "PUT",
                        f"projects/{slug}/merge_requests/{number}",
                        "-f", "state_event=close"])
        return {"ok": ok, "error": err}
    ok, err = _run(["gh", "api", "--hostname", host, "-X", "PATCH",
                    f"repos/{slug}/pulls/{number}", "-f", "state=closed"])
    return {"ok": ok, "error": err}


def marker_present_on_pr(url: str, marker: str) -> tuple[bool, bool]:
    """``(could_read, marker_found)`` for the comments already on *url*.

    The first element is not decoration: a forge read that FAILED must not look
    like "no comments there", or a transient error would licence a duplicate
    post on every retry. Callers treat "could not read" as "do not post".

    **Paginated.** A first version fetched `per_page=100` with no `--paginate`,
    so on a PR with more than 100 comments the marker fell off page 1 and the
    evidence comment was posted again on every delivery — the exact failure the
    marker exists to prevent, appearing only on the busiest PRs.

    The response is searched AS RAW TEXT rather than parsed into comment bodies.
    That is deliberate on two counts: it is immune to how each forge nests a
    body, and it means no third-party comment text is ever materialised in this
    process. The only thing that crosses back is one boolean.
    """
    parsed = parse_pr_url(url)
    if not parsed:
        return False, False
    forge, host, slug, number = parsed
    if forge == "gitlab":
        argv = ["glab", "api", "--hostname", host, "--paginate",
                f"projects/{slug}/merge_requests/{number}/notes?per_page=100"]
    else:
        argv = ["gh", "api", "--hostname", host, "--paginate",
                f"repos/{slug}/issues/{number}/comments?per_page=100"]
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=60, encoding="utf-8", errors="replace")
        if p.returncode != 0:
            return False, False
    except Exception:  # noqa: BLE001 — unreadable is not empty
        return False, False
    # A marker embedded in a JSON string survives verbatim: it contains no
    # character JSON escapes. Compare, then discard.
    return True, (marker in (p.stdout or ""))


def post_to_pr_once(url: str, body: str, marker: str) -> dict:
    """Post *body* to *url* unless a comment carrying *marker* is already there.

    ``marker`` is an HTML comment the caller embeds in *body* — invisible in the
    rendered PR, greppable in the raw text. The forge is the source of truth
    rather than a local flag: a resumed task, a re-run against a fresh database
    or a second process all see the same posted comment, whereas a flag in
    `task.context` only knows about the run that set it.

    Returns the `post_to_pr` shape plus ``mode="skipped_duplicate"`` when the
    marker was already present, and ``mode="unverifiable"`` when the existing
    comments could not be read — in which case NOTHING is posted, because a
    duplicated wall of evidence on every retry is worse than a missing one.
    """
    if marker and marker not in body:
        return {"ok": False, "mode": None,
                "error": "marker must be embedded in the body it guards"}
    could_read, found = marker_present_on_pr(url, marker)
    if not could_read:
        return {"ok": False, "mode": "unverifiable",
                "error": f"could not read existing comments on {url}; not posting"}
    if found:
        return {"ok": True, "mode": "skipped_duplicate", "error": ""}
    return post_to_pr(url, body)


def _gitlab_diff_refs(host: str, slug: str, number: int) -> dict | None:
    """The MR's {base_sha, start_sha, head_sha} — required to anchor an inline
    discussion to a diff line."""
    import json as _json
    try:
        p = subprocess.run(
            ["glab", "api", "--hostname", host, f"projects/{slug}/merge_requests/{number}"],
            capture_output=True, text=True, timeout=20, encoding="utf-8", errors="replace",
        )
        if p.returncode != 0:
            return None
        return (_json.loads(p.stdout) or {}).get("diff_refs")
    except Exception:  # noqa: BLE001
        return None


def _post_gitlab_inline(host: str, slug: str, number: int, body: str,
                        file: str, line: int) -> tuple[bool, str]:
    """Post an inline GitLab MR discussion anchored to ``file:line`` (RIGHT side).

    Sends a JSON body with a ``Content-Type: application/json`` header — glab's
    ``-f position[...]`` form encoding silently drops the nested position and the
    comment lands as a general note instead of on the line (learned the hard way).
    """
    import json as _json

    refs = _gitlab_diff_refs(host, slug, number)
    if not refs or not refs.get("head_sha"):
        return False, "no diff_refs for inline position"
    payload = _json.dumps({
        "body": body,
        "position": {
            "position_type": "text",
            "base_sha": refs.get("base_sha", ""),
            "start_sha": refs.get("start_sha", ""),
            "head_sha": refs["head_sha"],
            "new_path": file,
            "old_path": file,
            "new_line": line,
        },
    })
    try:
        p = subprocess.run(
            ["glab", "api", "--hostname", host, "-X", "POST",
             "-H", "Content-Type: application/json",
             f"projects/{slug}/merge_requests/{number}/discussions", "--input", "-"],
            input=payload, capture_output=True, text=True, timeout=20, encoding="utf-8", errors="replace",
        )
        if p.returncode == 0:
            return True, ""
        return False, (p.stderr.strip() or p.stdout.strip())[:300]
    except subprocess.TimeoutExpired:
        return False, "timeout"


def _head_sha(host_args: list[str], slug: str, number: int) -> tuple[bool, str]:
    try:
        p = subprocess.run(
            ["gh", "api", *host_args, f"repos/{slug}/pulls/{number}", "--jq", ".head.sha"],
            capture_output=True, text=True, timeout=15, encoding="utf-8", errors="replace",
        )
        return (p.returncode == 0, p.stdout.strip())
    except subprocess.TimeoutExpired:
        return False, ""
