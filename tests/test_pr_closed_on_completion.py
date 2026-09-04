"""LIVE GAP (PRs #387, #382, #375 found dangling): only the merge path
(`vcs.approve_merge.land_task`) and the draft-abandon path
(`orchestrator._abandon_draft_pr`) ever closed a task's PR. `nh approve
--landed` (`blockers/landed_override.py`) and the shipped/containment
landed-completion rungs (`blockers/shipped.py`) wrote DONE and walked away,
leaving the PR open forever.

`blockers/pr_closeout.close_task_prs_on_completion` is the shared hook both
now call right after the DONE write. These tests drive all three completion
paths end to end against a real `Store`, with a `FakeForge` standing in for
the network close (injected via `close=` for the direct unit tests, and via
`monkeypatch.setattr(no_human.vcs.comment_poster, "close_pr", ...)` for the
tests that drive `approve_landed_override`/`complete_if_content_landed`
directly, which do not thread a `close=` parameter through) — so nothing
here touches a network or needs credentials.
"""

from __future__ import annotations

import subprocess
import time

import pytest

from no_human.blockers.landed_override import approve_landed_override
from no_human.blockers.pr_closeout import (
    PR_CLOSED_ON_COMPLETION_KIND, close_task_prs_on_completion,
)
from no_human.blockers.shipped import (
    complete_if_approved_and_landed, complete_if_content_landed,
)
from no_human.core.db import Store
from no_human.core.task import Task, TaskStatus
from no_human.vcs import comment_poster
from tests.test_approve_merge import land_env  # noqa: F401
from no_human.vcs.approve_merge import land_task

pytestmark = pytest.mark.usefixtures("isolated_env_file")


# --------------------------------------------------------------------------- #
# plumbing                                                                     #
# --------------------------------------------------------------------------- #

def _git(repo_path, *args):
    subprocess.run(["git", "-C", str(repo_path), *args], check=True,
                    capture_output=True)


def _git_out(repo_path, *args):
    return subprocess.run(["git", "-C", str(repo_path), *args], text=True,
                          capture_output=True, check=True).stdout.strip()


def _make_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("orig\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "initial")
    return repo


class FakeForge:
    """Per-URL ``"open"`` -> ``"closed"`` state, recording every call."""

    def __init__(self, **initial_state):
        self.state = dict(initial_state)
        self.closes: list[str] = []

    def close_pr(self, url):
        self.closes.append(url)
        if self.state.get(url) == "open":
            self.state[url] = "closed"
            return {"ok": True, "error": ""}
        return {"ok": False, "error": f"not open: {self.state.get(url)!r}"}


async def _seed_awaiting(store, repo_path, *, url, base_branch="main",
                         pr_branch="feature"):
    t = Task.new("pr-closeout-check", repo_path=str(repo_path))
    t.context = {"base_branch": base_branch, "pr_branch": pr_branch,
                "pr_watch": url}
    await store.create_task(t)
    await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)
    return t


async def _pr_closed_events(store, task_id):
    events = await store.list_events(task_id)
    return [e for e in events if e["kind"] == PR_CLOSED_ON_COMPLETION_KIND]


def _always_false_is_terminal():
    async def _f(task):
        return False
    return _f


# --------------------------------------------------------------------------- #
# --landed override                                                           #
# --------------------------------------------------------------------------- #

async def test_landed_override_closes_open_pr_and_records_event(
    tmp_path, store, monkeypatch,
):
    repo = _make_repo(tmp_path)
    sha = _git_out(repo, "rev-parse", "HEAD")
    url = "https://github.com/o/r/pull/387"
    forge = FakeForge(**{url: "open"})
    monkeypatch.setattr(comment_poster, "close_pr", forge.close_pr)

    t = await _seed_awaiting(store, repo, url=url, pr_branch="")

    await approve_landed_override(store, t, sha, "supervisor squash train")

    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.DONE
    assert forge.state[url] == "closed"

    events = await _pr_closed_events(store, t.id)
    assert len(events) == 1
    ev = events[0]
    assert ev["task_id"] == t.id
    assert ev["pr_id"] == url
    assert ev["closed_timestamp"]
    assert ev["completion_path"] == "approved_landed_override"


# --------------------------------------------------------------------------- #
# shipped-completion                                                          #
# --------------------------------------------------------------------------- #

async def test_shipped_completion_closes_open_pr_and_records_event(
    tmp_path, store, monkeypatch,
):
    url = "https://github.com/o/r/pull/382"
    forge = FakeForge(**{url: "open"})
    monkeypatch.setattr(comment_poster, "close_pr", forge.close_pr)

    t = Task.new("shipped-check", repo_path="/repo")
    t.context = {"base_branch": "main", "pr_branch": "feature",
                "pr_watch": url}
    await store.create_task(t)
    await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)

    async def fake_pr_shipped(repo_path, branch, base):
        return True

    events_seen = []
    result = await complete_if_content_landed(
        store, t, url,
        pr_shipped=fake_pr_shipped,
        is_terminal=_always_false_is_terminal(),
        on_event=lambda kind, text: events_seen.append((kind, text)),
        forge_state="CLOSED", action="shipped",
        situation="was closed",
    )

    assert result == "shipped"
    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.DONE
    assert forge.state[url] == "closed"

    events = await _pr_closed_events(store, t.id)
    assert len(events) == 1
    assert events[0]["completion_path"] == "shipped"
    assert events[0]["pr_id"] == url
    assert events[0]["task_id"] == t.id


# --------------------------------------------------------------------------- #
# containment landed-completion (approve path, no merge)                      #
# --------------------------------------------------------------------------- #

async def test_approved_landed_completion_closes_open_pr_and_records_event(
    tmp_path, store, monkeypatch,
):
    url = "https://github.com/o/r/pull/375"
    forge = FakeForge(**{url: "open"})
    monkeypatch.setattr(comment_poster, "close_pr", forge.close_pr)

    t = Task.new("approve-landed-check", repo_path="/repo")
    t.context = {"base_branch": "main", "pr_branch": "feature",
                "pr_watch": url}
    await store.create_task(t)
    await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)

    async def fake_probe(repo_path, branch, base):
        return True

    result = await complete_if_approved_and_landed(
        store, t, url, branch="feature", probe=fake_probe,
        is_terminal=_always_false_is_terminal(),
    )

    assert result == "approved_landed"
    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.DONE
    assert forge.state[url] == "closed"

    events = await _pr_closed_events(store, t.id)
    assert len(events) == 1
    assert events[0]["completion_path"] == "approved_landed"
    assert events[0]["pr_id"] == url


# --------------------------------------------------------------------------- #
# every open PR of the task closes, not just the resolved one                 #
# --------------------------------------------------------------------------- #

async def test_all_task_prs_close_not_just_the_resolved_one(
    tmp_path, store, monkeypatch,
):
    repo = _make_repo(tmp_path)
    sha = _git_out(repo, "rev-parse", "HEAD")
    url_a = "https://github.com/o/r/pull/1"
    url_b = "https://github.com/o/r/pull/2"
    forge = FakeForge(**{url_a: "open", url_b: "open"})
    monkeypatch.setattr(comment_poster, "close_pr", forge.close_pr)

    t = await _seed_awaiting(store, repo, url=url_a, pr_branch="")
    await store.save_events(t.id, [{
        "source": "system", "kind": "pr_draft", "pr_url": url_b,
        "ts": time.time(), "text": url_b,
    }])

    await approve_landed_override(store, t, sha, "closing both")

    assert forge.state[url_a] == "closed"
    assert forge.state[url_b] == "closed"
    events = await _pr_closed_events(store, t.id)
    assert {e["pr_id"] for e in events} == {url_a, url_b}
    assert len(events) == 2


# --------------------------------------------------------------------------- #
# control: a PR belonging to a DIFFERENT task is never touched                #
# --------------------------------------------------------------------------- #

async def test_other_tasks_pr_is_never_touched(tmp_path, store, monkeypatch):
    repo = _make_repo(tmp_path)
    sha = _git_out(repo, "rev-parse", "HEAD")
    url_a1 = "https://github.com/o/r/pull/10"
    url_b1 = "https://github.com/o/r/pull/11"
    forge = FakeForge(**{url_a1: "open", url_b1: "open"})
    monkeypatch.setattr(comment_poster, "close_pr", forge.close_pr)

    task_a1 = await _seed_awaiting(store, repo, url=url_a1, pr_branch="")
    task_b1 = await _seed_awaiting(store, repo, url=url_b1, pr_branch="")

    await approve_landed_override(store, task_a1, sha, "task A completes")

    assert forge.state[url_a1] == "closed"
    assert forge.state[url_b1] == "open"
    assert await _pr_closed_events(store, task_b1.id) == []

    url_a2 = "https://github.com/o/r/pull/12"
    url_b2 = "https://github.com/o/r/pull/13"
    forge.state[url_a2] = "open"
    forge.state[url_b2] = "open"

    task_a2 = Task.new("shipped-check-2", repo_path="/repo")
    task_a2.context = {"base_branch": "main", "pr_branch": "feature",
                       "pr_watch": url_a2}
    await store.create_task(task_a2)
    await store.set_status(task_a2, TaskStatus.AWAITING_APPROVAL, validate=False)

    task_b2 = await _seed_awaiting(store, repo, url=url_b2, pr_branch="")

    async def fake_pr_shipped(repo_path, branch, base):
        return True

    await complete_if_content_landed(
        store, task_a2, url_a2, pr_shipped=fake_pr_shipped,
        is_terminal=_always_false_is_terminal(), on_event=lambda k, t: None,
        forge_state="CLOSED", action="shipped", situation="was closed",
    )

    assert forge.state[url_a2] == "closed"
    assert forge.state[url_b2] == "open"
    assert await _pr_closed_events(store, task_b2.id) == []


# --------------------------------------------------------------------------- #
# control: the merge path's existing close behavior is unchanged              #
# --------------------------------------------------------------------------- #

def test_merge_path_close_behavior_unchanged(land_env, monkeypatch):
    import json

    import no_human.blockers.pr_closeout as pr_closeout_mod

    called = []

    async def _spy(*a, **kw):
        called.append((a, kw))
        return []

    monkeypatch.setattr(pr_closeout_mod, "close_task_prs_on_completion", _spy)

    branch, head_sha = land_env.cut_branch("no-human/t-close-control")
    result = land_task(
        repo_path=str(land_env.clone), branch=branch, pr_url=land_env.pr_url,
        task_id="deadbeef", task_title="Add feature", review_evidence="review PASS",
        config=land_env.config,
    )

    assert result.ok, result.stderr
    assert called == [], (
        "the merge path must not invoke the completion closeout hook — its "
        "own _close_pr step is the control surface and stays as-is")

    argvs = [json.loads(l) for l in land_env.gh_log.read_text().splitlines()
            if l.strip()]
    close_calls = [a for a in argvs if a[:2] == ["pr", "close"]]
    assert len(close_calls) == 1, f"merge path must close exactly once: {argvs}"
    for a in argvs:
        assert "--comment" not in a
        assert a[:2] != ["pr", "comment"]


# --------------------------------------------------------------------------- #
# bare close: no comment is ever posted                                       #
# --------------------------------------------------------------------------- #

async def test_completion_close_posts_no_comment(tmp_path, store, monkeypatch):
    repo = _make_repo(tmp_path)
    sha = _git_out(repo, "rev-parse", "HEAD")
    url = "https://github.com/o/r/pull/42"
    calls = []

    def fake_run(argv, timeout=15):
        calls.append(argv)
        return True, ""

    monkeypatch.setattr(comment_poster, "_run", fake_run)

    t = await _seed_awaiting(store, repo, url=url, pr_branch="")
    await approve_landed_override(store, t, sha, "no comment expected")

    assert calls, "close_pr never shelled out"
    joined = [" ".join(a) for a in calls]
    assert not any("comment" in j or "note" in j or "-f body=" in j
                  for j in joined), calls


# --------------------------------------------------------------------------- #
# failure is best-effort: a close failure never undoes completion             #
# --------------------------------------------------------------------------- #

async def test_close_failure_does_not_undo_completion(tmp_path, store):
    repo = _make_repo(tmp_path)
    sha = _git_out(repo, "rev-parse", "HEAD")
    url = "https://github.com/o/r/pull/99"

    def failing_close(url):
        return {"ok": False, "error": "403 forbidden"}

    t = await _seed_awaiting(store, repo, url=url, pr_branch="")

    closed = await close_task_prs_on_completion(
        store, t, completion_path="approved_landed_override", close=failing_close)

    assert closed == []
    events = await _pr_closed_events(store, t.id)
    assert events == []


async def test_close_failure_does_not_undo_completion_via_landed_override(
    tmp_path, store, monkeypatch,
):
    repo = _make_repo(tmp_path)
    sha = _git_out(repo, "rev-parse", "HEAD")
    url = "https://github.com/o/r/pull/98"

    monkeypatch.setattr(
        comment_poster, "close_pr",
        lambda url: {"ok": False, "error": "403 forbidden"})

    t = await _seed_awaiting(store, repo, url=url, pr_branch="")

    await approve_landed_override(store, t, sha, "close will fail, must still complete")

    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.DONE
    assert await _pr_closed_events(store, t.id) == []
