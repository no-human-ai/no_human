"""Minimal GitHub REST client for the Action — comment upsert plus a narrow,
read-only PR surface, nothing else.

WRITE-SURFACE ENFORCEMENT. Every request this client makes — including
reads — passes through :func:`_assert_write_allowed`, which raises
:class:`WriteSurfaceViolation` for any method/path pair outside an explicit
allowlist: listing or creating an issue comment, replacing the body of one it
already created, or a read-only ``GET`` of a pull request's metadata, its
changed-files listing, or a repository file's contents. This is the
mechanically-checkable form of "never merges, never pushes, never edits the
pull request beyond its own comment": there is no code path in this module
that can reach ``…/pulls/{n}/merge``, ``…/pulls/{n}/reviews``, a ``PATCH`` of
the pull request itself, or ``…/git/refs``, because the request method raises
before ``httpx`` is ever asked to send it. The read endpoints are GET-only —
a ``PUT``/``PATCH``/``DELETE`` against any of the same paths is refused just
as hard as an unlisted one, and the anchors are exact-match regexes (never a
prefix check), so ``…/pulls/{n}/merge`` cannot slip in under the ``…/pulls/{n}``
allowance.

IDENTITY. The listing endpoint returns every comment on the PR, not just
ours; :func:`find_marked_comment` narrows that to ones carrying our HTML
marker and, among those, picks the LOWEST id — so pagination order can never
flip which comment a later PATCH lands on, and a stray duplicate (the
race two concurrent runs cannot be prevented from creating, see the README)
converges on being ignored rather than multiplying.
"""

from __future__ import annotations

import base64
import re
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import quote

import httpx

API_VERSION = "2022-11-28"
DEFAULT_API_URL = "https://api.github.com"

#: GitHub rejects a comment body over 65536 bytes with a 422; stay comfortably
#: under that so our own truncation (see ``run.py``) never needs a second pass.
MAX_BODY_CHARS = 60_000

_MAX_LIST_PAGES = 10
_MAX_RATE_RETRIES = 2
_MAX_5XX_RETRIES = 3
_RATE_BACKOFF_CAP = 60.0
_5XX_BACKOFFS = (1.0, 2.0, 4.0)

_COMMENTS_LIST_PATH = re.compile(r"^/repos/[^/]+/[^/]+/issues/\d+/comments$")
_COMMENT_ITEM_PATH = re.compile(r"^/repos/[^/]+/[^/]+/issues/comments/\d+$")

#: The three read-only endpoints a `workflow_run` context needs to
#: reconstruct a PR's diff over REST (it has no checkout to run `git diff`
#: against). Each is an exact-match anchor, not a prefix — see
#: `_assert_write_allowed` for why that distinction is load-bearing.
_PULL_ITEM_PATH = re.compile(r"^/repos/[^/]+/[^/]+/pulls/\d+$")
_PULL_FILES_PATH = re.compile(r"^/repos/[^/]+/[^/]+/pulls/\d+/files$")
_CONTENTS_PATH = re.compile(r"^/repos/[^/]+/[^/]+/contents/[^?]+$")


def _is_safe_contents_path(path_only: str) -> bool:
    """Reject path traversal in a `.../contents/<sub>` request before the
    caller ever treats `_CONTENTS_PATH` matching it as "safe to send".

    `_CONTENTS_PATH`'s `[^?]+` matches any non-`?` character, including `.`
    and `/` — so on its own it would let a payload like
    `/repos/o/r/contents/../../pulls/1/merge` through the anchor. This
    checks the literal sub-path after `.../contents/` for a leading `/` or
    any `..` path segment (anywhere in the path, not just its start) and
    refuses both, so traversal can never launder a disallowed shape past
    this allowlist.
    """
    match = _CONTENTS_PATH.match(path_only)
    if not match:
        return False
    sub = path_only.split("/contents/", 1)[1]
    if sub.startswith("/"):
        return False
    return ".." not in sub.split("/")


class GitHubAPIError(RuntimeError):
    """The GitHub API call failed in a way the caller must treat as fatal.

    Every raise site names what happened and, where relevant, which
    permission or secret the operator needs to fix — this is the message
    that ends up in the run's exit-2 log, so it has to be actionable on its
    own without anyone reading this module's source.
    """


class WriteSurfaceViolation(RuntimeError):
    """A request outside the two-endpoint comment-only allowlist was attempted."""


def _assert_write_allowed(method: str, path: str) -> None:
    method = method.upper()
    path_only = path.split("?", 1)[0]
    if method == "GET" and _COMMENTS_LIST_PATH.match(path_only):
        return
    if method == "POST" and _COMMENTS_LIST_PATH.match(path_only):
        return
    if method == "PATCH" and _COMMENT_ITEM_PATH.match(path_only):
        return
    if method == "GET" and _PULL_ITEM_PATH.match(path_only):
        return
    if method == "GET" and _PULL_FILES_PATH.match(path_only):
        return
    if method == "GET" and _is_safe_contents_path(path_only):
        return
    raise WriteSurfaceViolation(
        f"refused {method} {path}: the Action's only allowed requests are "
        "GET/POST .../issues/{n}/comments, PATCH .../issues/comments/{id}, "
        "and read-only GET of .../pulls/{n}, .../pulls/{n}/files, and "
        ".../contents/{path}"
    )


@dataclass
class Comment:
    id: int
    body: str


class GitHubClient:
    """Talks to exactly one PR's comment thread, plus a read-only view of
    that PR's metadata, changed-file listing, and file contents. Nothing
    else is reachable — see `_assert_write_allowed`."""

    def __init__(
        self,
        *,
        token: str,
        api_url: str = DEFAULT_API_URL,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client = httpx.Client(
            base_url=api_url.rstrip("/"),
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": "no_human-review-gate-action",
            },
            transport=transport,
            timeout=30.0,
        )
        self._sleep = sleep

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "GitHubClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- the state machine --------------------------------------------------

    def _send(self, method: str, path: str, *, json: dict | None = None) -> httpx.Response:
        """One HTTP attempt with bounded, status-aware retries.

        Every branch below is a documented GitHub failure mode (see
        ``run.py``'s module docstring for the full state machine this
        implements) — a redirect is never followed, a 401 never retries, a
        403 either backs off (rate limit) or fails naming the exact
        permission block, and a 5xx or 429 gets a short bounded backoff.
        """
        _assert_write_allowed(method, path)
        body = json
        rate_retries = 0
        server_retries = 0
        truncated_once = False
        while True:
            try:
                resp = self._client.request(method, path, json=body)
            except httpx.TransportError as exc:
                if server_retries < _MAX_5XX_RETRIES:
                    self._sleep(_5XX_BACKOFFS[min(server_retries, len(_5XX_BACKOFFS) - 1)])
                    server_retries += 1
                    continue
                raise GitHubAPIError(
                    f"{method} {path} failed: transport error ({exc}) after "
                    f"{server_retries} retries"
                ) from exc

            if resp.status_code in (200, 201):
                return resp
            if resp.status_code == 301:
                raise GitHubAPIError(
                    f"{method} {path} was redirected (301) to "
                    f"{resp.headers.get('location', '?')} — refusing to follow a "
                    "redirect on a write path"
                )
            if resp.status_code == 401:
                raise GitHubAPIError(
                    f"{method} {path} returned 401 Unauthorized — the "
                    "`github_token` input is missing or invalid"
                )
            if resp.status_code == 403:
                if "Resource not accessible by integration" in resp.text:
                    raise GitHubAPIError(
                        f"{method} {path} returned 403: the token cannot write "
                        "comments on this repository. Add to the job:\n"
                        "  permissions:\n"
                        "    pull-requests: write"
                    )
                if resp.headers.get("x-ratelimit-remaining") == "0" or "Retry-After" in resp.headers:
                    if rate_retries < _MAX_RATE_RETRIES:
                        wait = min(
                            float(resp.headers.get("Retry-After", "5")), _RATE_BACKOFF_CAP
                        )
                        self._sleep(wait)
                        rate_retries += 1
                        continue
                raise GitHubAPIError(f"{method} {path} returned 403 (rate limited)")
            if resp.status_code == 404:
                raise GitHubAPIError(f"{method} {path} returned 404 Not Found")
            if resp.status_code == 410:
                raise GitHubAPIError(f"{method} {path} returned 410 Gone")
            if resp.status_code == 422:
                if not truncated_once and body is not None and "body" in body:
                    body = {**body, "body": body["body"][:MAX_BODY_CHARS]}
                    truncated_once = True
                    continue
                raise GitHubAPIError(f"{method} {path} returned 422 Unprocessable Entity")
            if resp.status_code == 429:
                if rate_retries < _MAX_RATE_RETRIES:
                    wait = min(float(resp.headers.get("Retry-After", "5")), _RATE_BACKOFF_CAP)
                    self._sleep(wait)
                    rate_retries += 1
                    continue
                raise GitHubAPIError(f"{method} {path} returned 429 Too Many Requests")
            if 500 <= resp.status_code < 600:
                if server_retries < _MAX_5XX_RETRIES:
                    self._sleep(_5XX_BACKOFFS[min(server_retries, len(_5XX_BACKOFFS) - 1)])
                    server_retries += 1
                    continue
                raise GitHubAPIError(
                    f"{method} {path} returned {resp.status_code} after "
                    f"{server_retries} retries"
                )
            raise GitHubAPIError(f"{method} {path} returned unexpected {resp.status_code}")

    # -- the two operations the Action needs --------------------------------

    def list_comments(self, repo: str, pr_number: int) -> list[Comment]:
        """All comments on the PR, across up to 10 pages of 100.

        Unreadable is not empty: a caller whose listing raised must NOT
        proceed to post — see the module docstring and ``run.py``'s upsert
        step for why a transient read failure must not be read as "no prior
        comment exists".

        Pagination is driven by an incrementing ``page=`` query parameter we
        construct ourselves, not by following the response's ``Link``
        header. GitHub returns that header in at least two shapes —
        ``https://api.github.com/repos/{owner}/{repo}/...`` and, in cases
        such as a renamed or ID-addressed repository,
        ``https://api.github.com/repositories/{id}/...`` — and the second
        form does not match ``_COMMENTS_LIST_PATH``'s ``/repos/...`` anchor.
        Following it verbatim would make :func:`_assert_write_allowed`
        correctly refuse a URL it cannot prove is the comments-list
        endpoint, which would surface as an exit-2 failure discarding an
        already-computed review verdict. Owning the URL ourselves avoids the
        ambiguity entirely: a short page (fewer than ``per_page`` items) is
        unambiguously the last one.
        """
        out: list[Comment] = []
        for page in range(1, _MAX_LIST_PAGES + 1):
            path = f"/repos/{repo}/issues/{pr_number}/comments?per_page=100&page={page}"
            resp = self._send("GET", path)
            items = resp.json()
            for item in items:
                out.append(Comment(id=item["id"], body=item.get("body") or ""))
            if len(items) < 100:
                break
        return out

    def create_comment(self, repo: str, pr_number: int, body: str) -> Comment:
        path = f"/repos/{repo}/issues/{pr_number}/comments"
        try:
            resp = self._send("POST", path, json={"body": body})
        except GitHubAPIError:
            # DUPLICATE HAZARD: a transport failure during a POST may still
            # have created the comment server-side. Re-list before giving up
            # so a retry loop in the caller finds it via PATCH instead of
            # creating a second one.
            raise
        item = resp.json()
        return Comment(id=item["id"], body=item.get("body") or "")

    def update_comment(self, repo: str, comment_id: int, body: str) -> Comment:
        path = f"/repos/{repo}/issues/comments/{comment_id}"
        resp = self._send("PATCH", path, json={"body": body})
        item = resp.json()
        return Comment(id=item["id"], body=item.get("body") or "")

    # -- the read-only PR surface (workflow_run mode has no checkout) ------

    def get_pull(self, repo: str, pr_number: int) -> dict:
        """A pull request's metadata: state, merged, head/base sha, title, body.

        This is how `workflow_run` mode learns whether the PR's head is a
        fork (there is no `pull_request` payload to read that from), and it
        is the source of truth for the head sha to review — see run.py's
        check that this still matches the sha the triggering event named.
        """
        path = f"/repos/{repo}/pulls/{pr_number}"
        resp = self._send("GET", path)
        data = resp.json()
        if not isinstance(data, dict):
            raise GitHubAPIError(f"GET {path} returned an unexpected shape: {type(data).__name__}")
        return data

    def list_pull_files(self, repo: str, pr_number: int) -> list[dict]:
        """Every changed file's metadata (filename, status, patch, ...),
        across up to 10 pages of 100 — the REST substitute for `git diff
        --name-only` when there is no checkout to run git against.
        """
        out: list[dict] = []
        for page in range(1, _MAX_LIST_PAGES + 1):
            path = f"/repos/{repo}/pulls/{pr_number}/files?per_page=100&page={page}"
            resp = self._send("GET", path)
            items = resp.json()
            if not isinstance(items, list):
                raise GitHubAPIError(f"GET {path} returned an unexpected shape: {type(items).__name__}")
            out.extend(items)
            if len(items) < 100:
                break
        return out

    def get_contents(self, repo: str, path: str, ref: str) -> str | None:
        """A single file's text content at `ref`, or `None` if it cannot be
        materialized as plain text (a directory, submodule, symlink, an
        encoding other than base64, or bytes that do not decode as UTF-8).

        `None` here is a caller signal to simply skip the file rather than
        fail closed — the diff synthesized from `.../pulls/{n}/files` still
        names it, so nothing about the file's *presence* in the review is
        silently dropped, only its on-disk body for citation-checking.
        """
        encoded = quote(path, safe="/")
        url = f"/repos/{repo}/contents/{encoded}?ref={quote(ref, safe='')}"
        resp = self._send("GET", url)
        data = resp.json()
        if not isinstance(data, dict):
            return None
        if data.get("type") != "file" or data.get("encoding") != "base64":
            return None
        raw = data.get("content", "")
        try:
            decoded = base64.b64decode(raw, validate=False)
        except (ValueError, TypeError):
            return None
        try:
            return decoded.decode("utf-8")
        except UnicodeDecodeError:
            return None


def find_marked_comment(comments: list[Comment], marker: str) -> Comment | None:
    """The lowest-id comment carrying *marker*, or ``None``.

    Lowest id, not first-in-list or last-in-list: pagination order is not a
    stable identity, and picking the lowest id means a stray duplicate
    (created by two concurrent runs racing each other — unsolvable
    client-side, see the README's ``concurrency:`` note) converges on being
    ignored by every future run rather than being multiplied further.
    """
    matches = [c for c in comments if marker in c.body]
    if not matches:
        return None
    return min(matches, key=lambda c: c.id)


def upsert_comment(
    client: GitHubClient, repo: str, pr_number: int, marker: str, body: str
) -> Comment:
    """Create-or-replace the Action's one comment on this PR.

    Listing MUST succeed before either branch runs — see
    :meth:`GitHubClient.list_comments`'s docstring on why "unreadable" must
    never be treated as "absent".
    """
    existing = find_marked_comment(client.list_comments(repo, pr_number), marker)
    if existing is not None:
        return client.update_comment(repo, existing.id, body)
    try:
        return client.create_comment(repo, pr_number, body)
    except GitHubAPIError:
        # The POST may have landed before the transport error. Re-list once
        # and PATCH if it now exists; otherwise the original failure stands.
        existing = find_marked_comment(client.list_comments(repo, pr_number), marker)
        if existing is not None:
            return client.update_comment(repo, existing.id, body)
        raise
