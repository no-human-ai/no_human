"""GitHub review-SUMMARY feedback and `user.type == "Bot"` classification
(no_human/430, plan task 1.4/E2).

Two bugs, one theme — "what counts as a human asking for changes":
  1. `fetch_github_pr_comments` never read `/pulls/{n}/reviews`, so a review's
     own SUMMARY body (as opposed to its line comments) never reached the
     coder — a `CHANGES_REQUESTED` review whose feedback lives only in the
     summary never woke the task at all.
  2. `_is_bot_author` only recognized a `[bot]`-suffixed login; GitHub's
     built-in AI reviewer posts line comments from a login WITHOUT that
     suffix but with `user.type == "Bot"`, so it was taken as human feedback.

All fixtures are recorded ``gh api --paginate`` JSON-array shapes; no network
calls (``pr_watcher._run_cli`` and ``shutil.which`` are monkeypatched, the
idiom already used throughout ``tests/test_pr_watcher.py``).
"""
from __future__ import annotations

import json
from pathlib import Path

from no_human.vcs import pr_watcher as pw
from no_human.vcs.pr_watcher import PrComment
from no_human.blockers.wake import WakeWatcher
from no_human.core.task import Task, TaskStatus

FIXTURES = Path(__file__).parent / "fixtures" / "github_pr_reviews"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


REVIEWS = json.loads(_load("reviews.json"))
REVIEW_COMMENTS = json.loads(_load("review_comments.json"))
ISSUE_COMMENTS = json.loads(_load("issue_comments.json"))

# Fixture indices, named for readability at the call sites below.
_CHANGES_REQUESTED = REVIEWS[0]      # body-only feedback — the bug
_APPROVED = REVIEWS[1]
_COMMENTED_WITH_BODY = REVIEWS[2]
_COMMENTED_EMPTY = REVIEWS[3]
_CHANGES_REQUESTED_MARKER = REVIEWS[4]
_PENDING = REVIEWS[5]
_NULL_USER = REVIEWS[6]              # deleted-account author — the crash fix


def _install_fixture_cli(monkeypatch, *, reviews=True, review_comments=True,
                          issue_comments=True):
    """Route `pr_watcher._run_cli` to the recorded fixtures by endpoint,
    dispatching on the REST path embedded in `cmd` (the idiom ~20 existing
    tests in tests/test_pr_watcher.py already use)."""

    async def fake_run_cli(cmd):
        path = next((a for a in cmd if isinstance(a, str) and a.startswith("repos/")), "")
        if path.endswith("/reviews"):
            return _load("reviews.json") if reviews else "[]"
        if "/pulls/" in path and path.endswith("/comments"):
            return _load("review_comments.json") if review_comments else "[]"
        if "/issues/" in path and path.endswith("/comments"):
            return _load("issue_comments.json") if issue_comments else "[]"
        return "[]"

    monkeypatch.setattr(pw, "_run_cli", fake_run_cli)
    monkeypatch.setattr(pw.shutil, "which", lambda name: f"/usr/bin/{name}")


# --------------------------------------------------------------------------- #
# fetch_github_pr_comments: review SUMMARY bodies                             #
# --------------------------------------------------------------------------- #


async def test_changes_requested_review_body_is_returned_as_feedback(monkeypatch):
    _install_fixture_cli(monkeypatch, review_comments=False, issue_comments=False)
    comments = await pw.fetch_github_pr_comments("org/repo", 42)

    matches = [c for c in comments if c.body == _CHANGES_REQUESTED["body"]]
    assert len(matches) == 1, comments
    assert matches[0].path is None
    assert matches[0].author == _CHANGES_REQUESTED["user"]["login"]
    assert matches[0].created_at == _CHANGES_REQUESTED["submitted_at"]


async def test_commented_review_with_body_is_returned(monkeypatch):
    _install_fixture_cli(monkeypatch, review_comments=False, issue_comments=False)
    comments = await pw.fetch_github_pr_comments("org/repo", 42)

    assert any(c.body == _COMMENTED_WITH_BODY["body"] for c in comments)
    # The empty-bodied COMMENTED review produces nothing.
    assert not any(c.body == "" for c in comments)


async def test_approved_review_body_is_never_feedback(monkeypatch):
    _install_fixture_cli(monkeypatch, review_comments=False, issue_comments=False)
    comments = await pw.fetch_github_pr_comments("org/repo", 42)

    assert not any(c.body == _APPROVED["body"] for c in comments)


async def test_pending_review_with_no_submitted_at_is_dropped(monkeypatch):
    _install_fixture_cli(monkeypatch, review_comments=False, issue_comments=False)
    comments = await pw.fetch_github_pr_comments("org/repo", 42)

    assert not any(c.body == _PENDING["body"] for c in comments)


async def test_deleted_account_null_user_review_does_not_crash_the_fetch(monkeypatch):
    """POSITIVE CONTROL for the null-user crash: GitHub sends `"user": null`
    for a review whose author has since deleted their account. `dict.get(key,
    default)` only substitutes the default when `key` is ABSENT — a
    present-but-null `"user"` still returns None, so a bare
    `r.get("user", {}).get("login")` raises AttributeError on `None.get`.
    Before the `(r.get("user") or {})` fix, this line — the `reviews.json`
    dispatch below — is what raises and takes out the whole fetch (no line
    comments, no issue comments, no other reviews reach the caller either)."""
    _install_fixture_cli(monkeypatch)
    comments = await pw.fetch_github_pr_comments("org/repo", 42)

    # The null-user review itself is still surfaced, with a fallback author.
    null_user_matches = [c for c in comments if c.body == _NULL_USER["body"]]
    assert len(null_user_matches) == 1, comments
    assert null_user_matches[0].author == "unknown"
    assert null_user_matches[0].author_type == ""

    # And the fetch did not raise out before reaching the OTHER endpoints.
    assert any(c.body == _CHANGES_REQUESTED["body"] for c in comments)
    assert any(c.body == REVIEW_COMMENTS[0]["body"] for c in comments)
    assert any(c.body == ISSUE_COMMENTS[0]["body"] for c in comments)


async def test_review_line_comments_and_issue_comments_still_flow(monkeypatch):
    """The new reviews block is ADDITIVE — the two existing endpoints keep
    working, and a bot-typed line comment carries `author_type` through."""
    _install_fixture_cli(monkeypatch)
    comments = await pw.fetch_github_pr_comments("org/repo", 42)

    line_bodies = {c["body"] for c in REVIEW_COMMENTS}
    assert line_bodies <= {c.body for c in comments}
    bot_comment = next(c for c in comments if c.body == REVIEW_COMMENTS[1]["body"])
    assert bot_comment.author_type == "Bot"
    assert not bot_comment.author.endswith("[bot]")

    assert any(c.body == ISSUE_COMMENTS[0]["body"] for c in comments)


# --------------------------------------------------------------------------- #
# `user.type == "Bot"` classification                                        #
# --------------------------------------------------------------------------- #


async def test_bot_typed_author_without_suffix_is_not_human(store):
    w = WakeWatcher(store, {})
    bot_comment = PrComment(author="ai-reviewer", body="nit", author_type="Bot")
    assert w._is_self_or_bot(bot_comment) is True

    human_comment = PrComment(author="ai-reviewer", body="nit", author_type="User")
    assert w._is_self_or_bot(human_comment) is False


async def test_allow_comment_bot_authors_opts_a_bot_back_in(store):
    w = WakeWatcher(store, {"blockers": {"allow_comment_bot_authors": ["ai-reviewer"]}})
    bot_comment = PrComment(author="ai-reviewer", body="nit", author_type="Bot")
    assert w._is_self_or_bot(bot_comment) is False


async def test_ignore_list_still_wins_over_the_opt_in(store):
    w = WakeWatcher(store, {"blockers": {
        "ignore_comment_authors": ["ai-reviewer"],
        "allow_comment_bot_authors": ["ai-reviewer"],
    }})
    bot_comment = PrComment(author="ai-reviewer", body="nit", author_type="Bot")
    assert w._is_self_or_bot(bot_comment) is True


# --------------------------------------------------------------------------- #
# Agent's own marker + submitted_at freshness                                 #
# --------------------------------------------------------------------------- #


async def test_review_body_with_agent_marker_is_filtered(monkeypatch, store):
    _install_fixture_cli(monkeypatch, review_comments=False, issue_comments=False)
    comments = await pw.fetch_github_pr_comments("org/repo", 42)

    marker_comment = next(
        c for c in comments if c.body == _CHANGES_REQUESTED_MARKER["body"])
    w = WakeWatcher(store, {})
    assert w._is_self_or_bot(marker_comment) is True


def test_review_fixture_pins_no_created_at_field():
    """Documents WHY the freshness check must read `submitted_at`: GitHub's
    review object has no `created_at` at all. This only pins the fixture's
    shape (a literal recorded payload) — it cannot fail on a code change, so
    it is not itself coverage for the `submitted_at` keying; that behavior is
    covered by `test_review_at_or_before_the_cursor_is_not_fresh` below."""
    assert "created_at" not in _CHANGES_REQUESTED


async def test_review_at_or_before_the_cursor_is_not_fresh(monkeypatch):
    _install_fixture_cli(monkeypatch, review_comments=False, issue_comments=False)
    cursor = _CHANGES_REQUESTED["submitted_at"]

    comments = await pw.fetch_github_pr_comments("org/repo", 42, since=cursor)

    # At-or-before the cursor: dropped.
    assert not any(c.body == _CHANGES_REQUESTED["body"] for c in comments)
    # Strictly after the cursor: still present.
    assert any(c.body == _COMMENTED_WITH_BODY["body"] for c in comments)


# --------------------------------------------------------------------------- #
# One review batch -> exactly one resume, one revision round                  #
# --------------------------------------------------------------------------- #


async def test_one_review_batch_produces_one_resume_and_one_round(store):
    """A review body + its 2 line comments arrive together in one
    `pr_comment` fetch. `_append_comments_as_feedback` appends N feedback
    entries but bumps `revision_rounds` ONCE per batch, and
    `_check_approval_pr_comments` resumes ONCE — not once per comment."""
    t = Task.new("pr review batch", repo_path="/tmp/r")
    t.context = {"pr_watch": "https://github.com/org/repo/pull/42"}
    await store.create_task(t)
    await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)

    review_body = PrComment(
        author=_CHANGES_REQUESTED["user"]["login"],
        body=_CHANGES_REQUESTED["body"],
        created_at=_CHANGES_REQUESTED["submitted_at"],
        author_type="User",
    )
    line1 = PrComment(
        author=REVIEW_COMMENTS[0]["user"]["login"],
        body=REVIEW_COMMENTS[0]["body"],
        path=REVIEW_COMMENTS[0]["path"], line=REVIEW_COMMENTS[0]["line"],
        created_at=REVIEW_COMMENTS[0]["created_at"], author_type="User",
    )
    line2 = PrComment(
        author=REVIEW_COMMENTS[1]["user"]["login"],
        body=REVIEW_COMMENTS[1]["body"],
        path=REVIEW_COMMENTS[1]["path"], line=REVIEW_COMMENTS[1]["line"],
        created_at=REVIEW_COMMENTS[1]["created_at"], author_type="User",
    )
    batch = [review_body, line1, line2]

    async def pr_comment(url):
        return batch

    w = WakeWatcher(store, {}, pr_comment=pr_comment)

    resume_calls = {"n": 0}
    orig_resume = w._resume

    async def counted_resume(task, **kw):
        resume_calls["n"] += 1
        return await orig_resume(task, **kw)

    w._resume = counted_resume

    out = await w._check_approval_pr_comments(t)
    assert out == "resumed"
    assert resume_calls["n"] == 1

    refreshed = await store.get_task(t.id)
    assert refreshed.context["revision_rounds"] == 1
    assert len(refreshed.context["send_back_feedback"]) == 3
