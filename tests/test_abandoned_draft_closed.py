"""An attempt that walks away from a draft PR it opened must CLOSE it, not
just retitle it (refile of 1dfed378).

A retitled-but-open draft accumulates as clutter, confuses the PR-inheritance
resolver (newest non-empty ``pr_url``), and is a comment-resume hazard (the
b1fd13ca / ed2266eff class: an unmarked comment on a watched PR reads as
human feedback and re-wakes the task).

``_abandon_draft_pr`` (core/orchestrator.py) is the single shared seam. Its
only two callers are:
  * ``_raise_blocker``'s ESCALATED/FAILED branch — terminal failure and
    budget exhaustion (``budget.exhaustion_terminal`` routes BUDGET_EXHAUSTED
    straight to FAILED with no human asked).
  * ``_open_draft_pr_for_review``'s branch-move retire — a fresh attempt gets
    a new branch, so the prior attempt's draft can never land (stuck-reset).

These tests drive both callers, the seam directly for the delivered-PR guard
and the honest-failure path, and a resumable park to pin what must NOT close.
"""

import subprocess

import pytest

from no_human.blockers import Blocker
from no_human.blockers.taxonomy import BlockerCategory
from no_human.config import load_config
from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task, TaskStatus
from no_human.notify.slack import SlackNotifier
from no_human.vcs.git import GitRepo


class _Backend:
    async def run(self, *a, **k):  # pragma: no cover
        raise AssertionError("backend should not run here")


def _orch(store, tmp_path, events=None):
    cfg = load_config(tmp_path / "config.yaml")
    return Orchestrator(
        store, cfg.data, _Backend(), SlackNotifier(None),
        event_sink=(events.append if events is not None else None))


class _Forge:
    """Records every forge write `_abandon_draft_pr` can make; never a
    comment. `close_ok`/`close_error` let the honest-failure tests simulate a
    forge that rejects the close without raising."""

    def __init__(self, close_ok=True, close_error=""):
        self.titles = []
        self.comments = []
        self.closes = []
        self._close_ok = close_ok
        self._close_error = close_error

    def set_pr_title(self, url, title):
        self.titles.append((url, title))
        return {"ok": True, "error": ""}

    def post_to_pr(self, url, body, *a, **k):
        self.comments.append((url, body))
        return {"ok": True, "error": ""}

    def close_pr(self, url):
        self.closes.append(url)
        return {"ok": self._close_ok, "error": self._close_error}


@pytest.fixture
def forge(monkeypatch):
    f = _Forge()
    import no_human.vcs.comment_poster as cp
    monkeypatch.setattr(cp, "set_pr_title", f.set_pr_title)
    monkeypatch.setattr(cp, "post_to_pr", f.post_to_pr)
    monkeypatch.setattr(cp, "close_pr", f.close_pr)
    return f


def _patch_forge(monkeypatch, f):
    import no_human.vcs.comment_poster as cp
    monkeypatch.setattr(cp, "set_pr_title", f.set_pr_title)
    monkeypatch.setattr(cp, "post_to_pr", f.post_to_pr)
    monkeypatch.setattr(cp, "close_pr", f.close_pr)


def _blocker(**kw):
    kw.setdefault("goal", "Add the thing")
    kw.setdefault("confidence", 0.9)
    return Blocker(**kw)


def _git(path, *args):
    return subprocess.run(["git", "-C", str(path), *args],
                          capture_output=True, text=True, check=True).stdout.strip()


def _repo_with_a_commit(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q")
    _git(work, "config", "user.email", "a@b.c")
    _git(work, "config", "user.name", "T")
    (work / "f.txt").write_text("x\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "init")
    return work


class _Commit:
    files_changed = 1
    insertions = 1
    deletions = 0
    sha = ""


class _Result:
    final_text = "Implemented the change."
    num_turns = 3


class _FakePR:
    kind = "github"

    def __init__(self, url, pushed_sha):
        self.url = url
        self.pushed_sha = pushed_sha


# ---------------------------------------------------------------------------
# Criterion: stuck-reset abandonment path
# ---------------------------------------------------------------------------

async def test_stuck_reset_new_branch_abandon_closes_the_draft(
    store, tmp_path, forge, monkeypatch,
):
    """`_open_draft_pr_for_review` retires a prior-branch draft — each attempt
    gets its own branch, so a retry's fresh branch means attempt N's draft can
    never land. The seam must CLOSE it, not merely retitle it, and must post
    no comment."""
    import no_human.core.orchestrator as orch_mod
    import no_human.vcs.github as gh_mod

    work = _repo_with_a_commit(tmp_path)
    _git(work, "remote", "add", "origin", "https://github.com/o/r.git")
    repo = GitRepo(work)
    orch = _orch(store, tmp_path)

    def fake_open_pr(repo, branch, title, body, **kw):
        return _FakePR("https://github.com/o/r/pull/500", repo.head_sha())

    monkeypatch.setattr(orch_mod, "open_pr", fake_open_pr)
    monkeypatch.setattr(gh_mod, "_existing_pr_url", lambda *a, **k: "")

    task = Task.new("Fix the thing", repo_path=str(work))
    task.context = {"pr_draft_created": "https://github.com/o/r/pull/106",
                    "pr_draft_branch": "nh/attempt-1"}
    await store.create_task(task)
    commit = _Commit()
    commit.sha = repo.head_sha()
    await orch._open_draft_pr_for_review(
        task, repo, "nh/attempt-2", "main", "attempt-2",
        commit=commit, result=_Result())

    assert forge.closes == ["https://github.com/o/r/pull/106"]
    assert forge.comments == [], "the stuck-reset abandon path must post no comment"
    (_url, title), = forge.titles
    assert title.startswith("[ABANDONED — not delivered] "), title


# ---------------------------------------------------------------------------
# Criterion: blocker-park (terminal) abandonment path
# ---------------------------------------------------------------------------

async def test_terminal_budget_park_closes_the_draft(store, tmp_path, forge):
    """BUDGET_EXHAUSTED routes straight to FAILED under
    `budget.exhaustion_terminal` (the default) — the task ends with no human
    even asked, so a draft it opened is dead and must be closed, not left
    open claiming its acceptance criteria are met."""
    orch = _orch(store, tmp_path)
    t = Task.new("Add the thing", repo_path="/r")
    t.context = {"pr_draft_created": "https://github.com/o/r/pull/9",
                 "pr_draft_branch": "nh/a1"}
    await store.create_task(t)
    blocker = _blocker(category=BlockerCategory.BUDGET_EXHAUSTED,
                       root_cause_hypothesis="lifetime budget spent")
    out = await orch._raise_blocker(t, blocker)
    assert out.status == TaskStatus.FAILED
    assert forge.closes == ["https://github.com/o/r/pull/9"]
    assert forge.comments == []


# ---------------------------------------------------------------------------
# Criterion: terminal-failure (ESCALATED) abandonment path
# ---------------------------------------------------------------------------

async def test_escalated_terminal_failure_closes_the_draft(store, tmp_path, forge):
    """NOVEL_UNKNOWN routes straight to ESCALATED (no `escalate_now` override
    needed) — the ordinary max-attempts-exhausted shape."""
    orch = _orch(store, tmp_path)
    t = Task.new("Add the thing", repo_path="/r")
    t.context = {"pr_draft_created": "https://github.com/o/r/pull/11",
                 "pr_draft_branch": "nh/a1"}
    await store.create_task(t)
    blocker = _blocker(category=BlockerCategory.NOVEL_UNKNOWN,
                       root_cause_hypothesis="max_attempts (3) reached")
    out = await orch._raise_blocker(t, blocker)
    assert out.status == TaskStatus.ESCALATED
    assert forge.closes == ["https://github.com/o/r/pull/11"]
    assert forge.comments == []


# ---------------------------------------------------------------------------
# Negative: a RESUMABLE park must keep its draft open
# ---------------------------------------------------------------------------

async def test_a_resumable_park_does_not_close_its_draft(store, tmp_path, forge):
    """TRANSIENT_INFRA parks to BLOCKED and resumes on the SAME branch —
    `_finalize` refreshes exactly that draft on resume, so closing here would
    kill a live PR the task is about to reuse."""
    orch = _orch(store, tmp_path)
    t = Task.new("Add the thing", repo_path="/r")
    t.context = {"pr_draft_created": "https://github.com/o/r/pull/12",
                 "pr_draft_branch": "nh/a1"}
    await store.create_task(t)
    blocker = _blocker(category=BlockerCategory.TRANSIENT_INFRA, transient=True,
                       root_cause_hypothesis="CI infra flaked")
    out = await orch._raise_blocker(t, blocker)
    assert out.status == TaskStatus.BLOCKED
    assert forge.closes == []
    assert forge.comments == []
    assert t.context.get("pr_draft_created") == "https://github.com/o/r/pull/12", (
        "a resumable park must not pop the draft slot either")


# ---------------------------------------------------------------------------
# Criterion: the delivered-for-review PR is NEVER closed
# ---------------------------------------------------------------------------

async def test_the_delivered_pr_is_never_closed_on_attempt_failure(store, tmp_path, forge):
    orch = _orch(store, tmp_path)
    url = "https://github.com/o/r/pull/200"
    branch = "nh/attempt-1"
    t = Task.new("Add the thing", repo_path="/r")
    t.context = {"pr_draft_created": url, "pr_delivered_url": url,
                 "pr_watch": url, "pr_branch": branch, "pr_draft_branch": branch}
    await store.create_task(t)
    out = await orch._abandon_draft_pr(t, "attempt failed review", reason_from_agent=False)
    assert out == ""
    assert forge.closes == []
    assert forge.titles == []
    assert t.context.get("pr_draft_created") == url, (
        "the delivered draft slot must survive untouched")


async def test_the_delivered_discriminator_is_an_explicit_field_not_a_title(
    store, tmp_path, forge,
):
    """Only `pr_delivered_url` is set — no `pr_watch`, and the draft's own
    title is entirely ordinary — and it must still not be closed: the field
    alone carries the delivered-for-review fact, never a title pattern."""
    orch = _orch(store, tmp_path)
    url = "https://github.com/o/r/pull/201"
    t = Task.new("An entirely ordinary title, no markers at all", repo_path="/r")
    t.context = {"pr_draft_created": url, "pr_delivered_url": url}
    await store.create_task(t)
    out = await orch._abandon_draft_pr(t, "some reason", reason_from_agent=False)
    assert out == ""
    assert forge.closes == []
    assert forge.titles == []


# ---------------------------------------------------------------------------
# Criterion: honest failure — a close-API failure never blocks the off-ramp
# ---------------------------------------------------------------------------

async def test_a_close_failure_is_logged_and_does_not_block_the_abandon(
    store, tmp_path, monkeypatch,
):
    events = []
    orch = _orch(store, tmp_path, events=events)
    f = _Forge(close_ok=False, close_error="422 Unprocessable")
    _patch_forge(monkeypatch, f)

    url = "https://github.com/o/r/pull/13"
    t = Task.new("Add the thing", repo_path="/r")
    t.context = {"pr_draft_created": url, "pr_draft_branch": "nh/a1"}
    await store.create_task(t)
    out = await orch._abandon_draft_pr(t, "max attempts reached", reason_from_agent=False)

    assert out == url
    assert f.closes == [url]
    assert f.comments == [], "a close failure must not fall back to posting a comment"
    advisories = [e["text"] for e in events if e.get("kind") == "advisory"]
    assert any("could not close abandoned draft" in a for a in advisories), advisories
    assert "pr_draft_created" not in (t.context or {}), (
        "bookkeeping must complete even when the close call fails")
    assert t.context.get("abandoned_pr_urls") == [url]
    abandon_events = [e for e in events if e.get("kind") == "pr_draft_abandoned"]
    assert abandon_events, "the pr_draft_abandoned event was not emitted"


async def test_a_close_that_raises_does_not_break_the_off_ramp(
    store, tmp_path, monkeypatch,
):
    events = []
    orch = _orch(store, tmp_path, events=events)
    import no_human.vcs.comment_poster as cp

    def fake_set_pr_title(url, title):
        return {"ok": True, "error": ""}

    def raising_close_pr(url):
        raise RuntimeError("network exploded")

    def must_not_comment(*a, **k):
        raise AssertionError("the abandon path must never post a comment")

    monkeypatch.setattr(cp, "set_pr_title", fake_set_pr_title)
    monkeypatch.setattr(cp, "close_pr", raising_close_pr)
    monkeypatch.setattr(cp, "post_to_pr", must_not_comment)

    url = "https://github.com/o/r/pull/14"
    t = Task.new("Add the thing", repo_path="/r")
    t.context = {"pr_draft_created": url, "pr_draft_branch": "nh/a1"}
    await store.create_task(t)
    out = await orch._abandon_draft_pr(t, "max attempts reached", reason_from_agent=False)

    assert out == url
    assert "pr_draft_created" not in (t.context or {}), (
        "bookkeeping must complete even when close_pr raises")
    assert t.context.get("abandoned_pr_urls") == [url]
    abandon_events = [e for e in events if e.get("kind") == "pr_draft_abandoned"]
    assert abandon_events
