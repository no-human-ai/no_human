"""post-review-comments: an inline comment that 422s (file:line not in THIS PR's
diff — the cross-repo review case) falls back to a general issue comment."""

import subprocess

import pytest_asyncio
from httpx import AsyncClient, ASGITransport

from no_human.api.app import app
from no_human.core.task import Task


@pytest_asyncio.fixture
async def client(store):
    app.state.store = store
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as c:
        yield c


async def _task_with_finding(store):
    t = Task.new("review", repo_path="/r")
    t.kind = "code_review"
    t.context = {"pr_url": "https://code.example.com/dev/acme-test/pull/7001"}
    await store.create_task(t)
    aid = await store.create_attempt(t.id, 1)
    await store.update_attempt(aid, review_checklist={
        "passed": False,
        "items": [{
            "label": "x", "passed": False,
            "file": "workload/backend-service.yaml", "line": 138,  # NOT in the acme-test PR
            "comment": "CI is red on the enabling MR.", "severity": "low",
        }],
    }, status="succeeded")
    return t


async def test_inline_422_falls_back_to_issue_comment(store, client, monkeypatch):
    calls = []

    def fake_run(argv, **kw):
        calls.append(" ".join(argv))
        joined = " ".join(argv)

        class R:
            stdout = ""
            stderr = ""
            returncode = 0

        r = R()
        if "--jq" in argv:                                   # head SHA lookup
            r.stdout = "abc123"
        elif "/pulls/" in joined and "/comments" in joined:  # inline → 422
            r.returncode = 1
            r.stderr = "gh: Validation Failed (HTTP 422)"
        elif "/issues/" in joined and "/comments" in joined:  # fallback → OK
            r.returncode = 0
        return r

    monkeypatch.setattr(subprocess, "run", fake_run)

    t = await _task_with_finding(store)
    resp = await client.post(f"/api/tasks/{t.id}/post-review-comments", json={"items": [0]})
    data = resp.json()

    assert data["ok"] is True and data["posted"] == 1
    assert data["results"][0].get("mode") == "issue_comment"
    # It tried inline first, THEN the issue-comment fallback.
    assert any("/pulls/" in c and "/comments" in c for c in calls)
    assert any("/issues/" in c and "/comments" in c for c in calls)


async def test_inline_success_does_not_fall_back(store, client, monkeypatch):
    calls = []

    def fake_run(argv, **kw):
        calls.append(" ".join(argv))

        class R:
            stdout = "abc123" if "--jq" in argv else ""
            stderr = ""
            returncode = 0

        return R()

    monkeypatch.setattr(subprocess, "run", fake_run)

    t = await _task_with_finding(store)
    resp = await client.post(f"/api/tasks/{t.id}/post-review-comments", json={"items": [0]})
    data = resp.json()

    assert data["ok"] is True and data["results"][0].get("mode") == "inline"
    assert not any("/issues/" in c and "/comments" in c for c in calls)  # no fallback


async def test_finish_review_marks_a_stuck_code_review_done(store, client):
    """Human posted some comments but not all — the task must not stay stuck in
    Review PR. finish-review takes it to Done regardless of posted count."""
    from no_human.core.task import TaskStatus

    t = Task.new("review", repo_path="/r")
    t.kind = "code_review"
    t.context = {
        "pr_url": "https://code.example.com/dev/acme-test/pull/1",
        "draft_review_comments": [
            {"file": "a", "line": 1, "comment": "x", "posted": True},
            {"file": "b", "line": 2, "comment": "y", "posted": False},  # human skipped this one
        ],
    }
    await store.create_task(t)
    await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)

    resp = await client.post(f"/api/tasks/{t.id}/finish-review")
    data = resp.json()
    assert data["ok"] and data["posted"] == 1 and data["total"] == 2
    assert (await store.find_task(t.id)).status is TaskStatus.DONE


async def test_finish_review_rejects_a_non_awaiting_task(store, client):
    from no_human.core.task import TaskStatus

    t = Task.new("review", repo_path="/r")
    t.kind = "code_review"
    await store.create_task(t)  # status defaults to pending, not awaiting_approval
    resp = await client.post(f"/api/tasks/{t.id}/finish-review")
    assert resp.status_code == 409
