"""code_review drafts comments and NEVER auto-posts — the operator approves
(all or one-by-one) before anything reaches the PR. Posting routes
each finding to its own change set via comment_poster.post_to_pr."""

from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task, TaskStatus
from no_human.notify.slack import SlackNotifier

from .test_e2e_orchestrator import FakeBackend, _config  # noqa: F401


def _task_with_drafts():
    t = Task.new("review", repo_path="/r")
    t.context = {
        "pr_url": "https://code.example.com/dev/acme-test/pull/7001",
        "draft_review_comments": [
            {"file": "a.java", "line": 10, "comment": "This can NPE.", "severity": "high", "posted": False},
            {"file": "b.java", "line": 20, "comment": "nit: rename x.", "severity": "low", "posted": False},
            {"file": None, "line": None, "comment": "Topic name mismatch across repos.", "severity": "high", "posted": False},
        ],
    }
    return t


def _orch(store, tmp_path):
    return Orchestrator(store, _config(tmp_path).data, FakeBackend(lambda c: None), SlackNotifier(None))


async def test_post_only_approved_indices_leaves_the_rest_as_draft(store, tmp_path, monkeypatch):
    orch = _orch(store, tmp_path)
    posted_comments = []

    def fake_post(url, comment, file=None, line=None):
        posted_comments.append(comment)
        return {"ok": True, "mode": "issue_comment"}

    monkeypatch.setattr("no_human.vcs.comment_poster.post_to_pr", fake_post)
    t = _task_with_drafts()
    await store.create_task(t)

    # Approve only comment #1 and #3 (0-based 0 and 2).
    posted, remaining = await orch.post_draft_comments(t, [0, 2])
    assert posted == 2 and remaining == 1
    drafts = t.context["draft_review_comments"]
    assert drafts[0]["posted"] and drafts[2]["posted"]
    assert not drafts[1]["posted"]                 # the un-approved one stays a draft
    assert t.status is not TaskStatus.DONE          # still work left → not done
    assert any("This can NPE." in c for c in posted_comments)
    assert all("nit: rename x." not in c for c in posted_comments)


async def test_approve_all_posts_everything_and_marks_done(store, tmp_path, monkeypatch):
    orch = _orch(store, tmp_path)
    monkeypatch.setattr("no_human.vcs.comment_poster.post_to_pr",
                        lambda *a, **k: {"ok": True, "mode": "issue_comment"})
    t = _task_with_drafts()
    await store.create_task(t)

    posted, remaining = await orch.post_draft_comments(t, "all")
    assert posted == 3 and remaining == 0
    assert (await store.find_task(t.id)).status is TaskStatus.DONE


async def test_a_failed_post_is_not_marked_posted(store, tmp_path, monkeypatch):
    orch = _orch(store, tmp_path)
    monkeypatch.setattr("no_human.vcs.comment_poster.post_to_pr",
                        lambda *a, **k: {"ok": False, "mode": None, "error": "no access"})
    t = _task_with_drafts()
    await store.create_task(t)

    posted, remaining = await orch.post_draft_comments(t, "all")
    assert posted == 0 and remaining == 3
    assert not any(d["posted"] for d in t.context["draft_review_comments"])
