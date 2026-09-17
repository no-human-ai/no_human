"""Proves the REST path `docs/design/untrusted-pr-review-gate.md` chose for a
`workflow_run` review actually returns the shapes that design leans on.

This is the design's ONLY executable surface — there is no production
`pr_fetch.py` or client class to import. The `_get` helper below is local to
this file on purpose (see the design's section G: "the fetch path here is
designed as data, not shipped").

Pinned target: `no-human-ai/no_human` PR **27**, merged 2026-09-03 (immutable
once merged — the shape it returns cannot drift under this test). Verified
live during authoring against the unauthenticated API: `changed_files == 4`,
head sha `4efbad91a2fa2f1bea2b05e1e0a4ac28e96d61ea`, and
`tests/test_board_csp.py` is one of the four changed files, `status ==
"added"`, `size == 3746` at that ref.

No fixtures, no autouse, no monkeypatch (`tests/conftest.py:130-150` flags an
autouse fixture that monkeypatches as the single highest-leverage way to make
a suite lie; this module carries none). Unmarked, so the lane partition
pinned by `tests/test_test_lanes.py` is unaffected.

Skips rather than fails without a token or without network, so the suite
stays green offline and in this repo's own CI (the `python` job's "Run
tests" step, `.github/workflows/ci.yml:364-369`, sets no `GITHUB_TOKEN` or
`GH_TOKEN` env var). A wrong-shaped 200 response is never a skip — that is
this test doing its job.

This module's live assertions are therefore developer-machine-only today:
nothing in `.github/workflows/ci.yml` wires a token into the job that runs
this file, so `pytestmark` below skips all three tests in every CI run and
they execute only when a human runs this file locally with `GITHUB_TOKEN` or
`GH_TOKEN` set (e.g. `GITHUB_TOKEN="$(gh auth token)"`). Wiring `github.token`
into the test step so this proof runs in CI
is exactly the kind of workflow edit this design describes but does not
land — `.github/workflows/ci.yml` is out of scope for this change.
"""

from __future__ import annotations

import base64
import os

import httpx
import pytest

OWNER = "no-human-ai"
REPO = "no_human"
PR_NUMBER = 27
HEAD_SHA = "4efbad91a2fa2f1bea2b05e1e0a4ac28e96d61ea"
ADDED_FILE_PATH = "tests/test_board_csp.py"
ADDED_FILE_SIZE = 3746

_STATUS_VALUES = {"added", "removed", "modified", "renamed", "copied", "changed", "unchanged"}


def _token() -> str | None:
    return os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")


pytestmark = pytest.mark.skipif(
    not _token(), reason="no GITHUB_TOKEN/GH_TOKEN available — this test makes a live GitHub API call"
)


def _get(path: str, token: str, *, accept: str = "application/vnd.github+json") -> httpx.Response:
    """The whole "fetch path" this design chose (section F): one GET, GitHub's
    documented headers, no retry, no pagination beyond what a caller asks for
    by passing a full `path` (including query string) itself."""
    client = httpx.Client(
        base_url="https://api.github.com",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": accept,
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "no_human-design-proof",
        },
        timeout=30,
    )
    try:
        resp = client.get(path)
    except httpx.TransportError as exc:
        pytest.skip(f"no network reaching api.github.com: {exc}")
    finally:
        client.close()
    if resp.status_code == 403 and resp.headers.get("x-ratelimit-remaining") == "0":
        pytest.skip("GitHub API rate limit exhausted for this token")
    return resp


def test_changed_files_come_back_as_data_with_the_fields_the_gate_needs():
    """`GET .../pulls/{n}/files` — the chosen primary path (design section F).

    A fork PR's diff never has to be checked out: this response alone gives
    every field the follow-up implementation needs to route each changed
    file through `max_files` and into the reviewer's diff, all as ordinary
    JSON data reachable without ever executing the PR author's code.
    """
    token = _token()
    assert token is not None
    resp = _get(f"/repos/{OWNER}/{REPO}/pulls/{PR_NUMBER}/files?per_page=100", token)
    assert resp.status_code == 200, f"unexpected {resp.status_code}: {resp.text[:300]}"
    files = resp.json()
    assert len(files) == 4

    by_name = {f["filename"]: f for f in files}
    assert ADDED_FILE_PATH in by_name

    for entry in files:
        assert "filename" in entry
        assert "status" in entry
        assert entry["status"] in _STATUS_VALUES
        assert "sha" in entry
        assert "contents_url" in entry

    added = by_name[ADDED_FILE_PATH]
    assert added["status"] == "added"
    assert added.get("patch", "").startswith("@@"), (
        "an added file's patch must start with a hunk header — this is the "
        "text the design hands the reviewer as data, never a checkout"
    )


def test_a_changed_files_contents_come_back_base64_at_the_head_sha():
    """`GET .../contents/{path}?ref={sha}` — how the design fetches a
    changed file's text without checking anything out (design section F)."""
    token = _token()
    assert token is not None
    resp = _get(
        f"/repos/{OWNER}/{REPO}/contents/{ADDED_FILE_PATH}?ref={HEAD_SHA}", token
    )
    assert resp.status_code == 200, f"unexpected {resp.status_code}: {resp.text[:300]}"
    body = resp.json()
    assert body["encoding"] == "base64"
    assert body["size"] == ADDED_FILE_SIZE
    assert body["sha"]

    decoded = base64.b64decode(body["content"])
    assert decoded.startswith(b'"""'), "test_board_csp.py opens with a module docstring"


def test_the_unified_diff_media_type_returns_a_diff_not_json():
    """Records the shape of the alternative the design rejects as the primary
    path (design section F): one request, but no per-file granularity, so
    `max_files` cannot be enforced before the bytes are already spent."""
    token = _token()
    assert token is not None
    resp = _get(
        f"/repos/{OWNER}/{REPO}/pulls/{PR_NUMBER}",
        token,
        accept="application/vnd.github.diff",
    )
    assert resp.status_code == 200, f"unexpected {resp.status_code}: {resp.text[:300]}"
    assert resp.text.startswith("diff --git ")
