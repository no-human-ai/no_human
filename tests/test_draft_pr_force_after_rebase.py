"""Draft-PR-before-review must reuse `_finalize`'s scoped force-with-lease
after a rebase round, so a `pr_conflict` round never reviews without its PR.

EVIDENCE (dc3b72f7 attempt 3, 2026-08-20 11:18:23): a `pr_conflict` rebase
round rewrites the already-pushed task branch by construction, so the plain
push at `_open_draft_pr_for_review` was rejected non-fast-forward on EVERY
such round, not transiently — and the review then ran with NO PR, exactly
the state the 0a / PR-021 rationale (orchestrator.py ~4725) exists to
prevent. `_finalize` already solves the identical rejection for delivery
(orchestrator.py ~5510, `forced = _is_non_fast_forward(exc)`); this fixes
the draft site to reuse that same predicate, scoped to a mechanical
(`pr_conflict`) round.
"""

import inspect
from types import SimpleNamespace

import pytest

import no_human.core.orchestrator as orch_mod
from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task
from no_human.vcs import GitError, PushBehindRemote
from no_human.vcs import github as _gh


async def _async_noop(*_a, **_kw):
    return None


async def _empty_receipts(*_a, **_kw):
    return []


def _make_orchestrator(events, tmp_path, *, mechanical: bool):
    """Build a bare Orchestrator plus a `pr_conflict` (or not) task, following
    tests/test_review_fail_closed.py:330+'s idiom."""
    orch = Orchestrator.__new__(Orchestrator)
    orch._sink = events.append
    orch._active_attempt_id = None
    orch.config = {"git": {"github_hosts": []}}

    verdict = 1 if mechanical else 0

    async def _latest_review_verdict(*_a, **_kw):
        return verdict

    async def _latest_review_attempt(*_a, **_kw):
        return {"review_passed": verdict, "status": "succeeded",
                "started_at": "2026-01-01 00:00:00", "_rowid": 1}

    async def _latest_failed_attempt(*_a, **_kw):
        return None

    orch.store = SimpleNamespace(
        update_task=_async_noop,
        list_verification_receipts=_empty_receipts,
        latest_review_verdict=_latest_review_verdict,
        latest_review_attempt=_latest_review_attempt,
        latest_failed_attempt=_latest_failed_attempt,
    )

    repo = SimpleNamespace(
        remote_url=lambda: "https://github.com/o/r.git", path=tmp_path)
    task = Task.new("fix the thing", repo_path=str(tmp_path))
    if mechanical:
        task.context = {"send_back_feedback": [{"source": "pr_conflict"}]}
    return orch, repo, task


NFF_MESSAGE = (
    "git push -u origin no-human/dc3b72f7-2 failed (1): "
    "! [rejected] no-human/dc3b72f7-2 -> no-human/dc3b72f7-2 (non-fast-forward)"
)


async def test_a_non_fast_forward_on_a_pr_conflict_round_retries_with_force_with_lease(
        tmp_path, monkeypatch):
    """AC 1 — the repro. Fails on current code: one call, url "", absent="open failed"."""
    calls: list[dict] = []

    def fake_open_pr(repo, branch, title, body, **kw):
        calls.append(kw)
        if len(calls) == 1:
            raise GitError(NFF_MESSAGE)
        return SimpleNamespace(url="https://github.com/o/r/pull/42")

    monkeypatch.setattr(orch_mod, "open_pr", fake_open_pr)
    monkeypatch.setattr(_gh, "_existing_pr_url", lambda *a: "")

    orch, repo, task = _make_orchestrator([], tmp_path, mechanical=True)

    url = await orch._open_draft_pr_for_review(
        task, repo, "no-human/dc3b72f7-2", "main", "att-1",
        commit=SimpleNamespace(files_changed=1, insertions=1, deletions=0, sha="a"),
        result=SimpleNamespace(final_text="did the thing", num_turns=1))

    assert len(calls) == 2, f"expected a retry after the non-fast-forward, got {calls}"
    assert calls[1]["force_with_lease"] is True, (
        f"the retry after a pr_conflict rebase must force-with-lease, got {calls[1]}")
    assert url == "https://github.com/o/r/pull/42", (
        f"the retry succeeded but the helper returned {url!r} instead of the url")
    assert orch._draft_pr_absent == "", (
        "the draft PR was opened on retry, so _draft_pr_absent must stay clear so "
        "_run_review is handed the draft, not the absent-marker"
    )


async def test_the_same_rejection_without_a_rebase_round_is_not_forced(
        tmp_path, monkeypatch):
    """AC 2a — negative: no pr_conflict round, no force. Today's behaviour."""
    calls: list[dict] = []

    def fake_open_pr(repo, branch, title, body, **kw):
        calls.append(kw)
        raise GitError(NFF_MESSAGE)

    monkeypatch.setattr(orch_mod, "open_pr", fake_open_pr)
    monkeypatch.setattr(_gh, "_existing_pr_url", lambda *a: "")

    events: list[dict] = []
    orch, repo, task = _make_orchestrator(events, tmp_path, mechanical=False)

    url = await orch._open_draft_pr_for_review(
        task, repo, "no-human/xyz", "main", "att-1",
        commit=SimpleNamespace(files_changed=1, insertions=1, deletions=0, sha="a"),
        result=SimpleNamespace(final_text="did the thing", num_turns=1))

    assert len(calls) == 1, f"a non-rebase round must not retry, got {calls}"
    assert not any(kw.get("force_with_lease") for kw in calls), (
        f"no call should carry force_with_lease without a rebase round: {calls}")
    assert url == ""
    assert orch._draft_pr_absent == "open failed"
    assert any(e.get("kind") == "pr_open_retry" for e in events), (
        "today's behaviour must still emit pr_open_retry so finalize opens it later")


async def test_push_behind_remote_still_propagates_from_the_draft_site(
        tmp_path, monkeypatch):
    """AC 2b — PushBehindRemote is a different class and stays escalated,
    even on a pr_conflict round; no force retry is ever attempted for it."""
    calls: list[dict] = []

    def fake_open_pr(repo, branch, title, body, **kw):
        calls.append(kw)
        raise PushBehindRemote("local is BEHIND the remote tip")

    monkeypatch.setattr(orch_mod, "open_pr", fake_open_pr)
    monkeypatch.setattr(_gh, "_existing_pr_url", lambda *a: "")

    orch, repo, task = _make_orchestrator([], tmp_path, mechanical=True)

    with pytest.raises(PushBehindRemote):
        await orch._open_draft_pr_for_review(
            task, repo, "no-human/behind", "main", "att-1",
            commit=SimpleNamespace(files_changed=1, insertions=1, deletions=0, sha="a"),
            result=SimpleNamespace(final_text="did the thing", num_turns=1))

    assert len(calls) == 1, f"PushBehindRemote must never be retried/forced: {calls}"


async def test_a_forced_retry_that_fails_again_falls_through_to_the_skip(
        tmp_path, monkeypatch):
    """Intake Q&A 2 — a failed forced retry is non-escalating and falls
    through to the existing skip (pr_open_retry emitted, finalize opens it)."""
    calls: list[dict] = []

    def fake_open_pr(repo, branch, title, body, **kw):
        calls.append(kw)
        raise GitError(NFF_MESSAGE)

    monkeypatch.setattr(orch_mod, "open_pr", fake_open_pr)
    monkeypatch.setattr(_gh, "_existing_pr_url", lambda *a: "")

    events: list[dict] = []
    orch, repo, task = _make_orchestrator(events, tmp_path, mechanical=True)

    url = await orch._open_draft_pr_for_review(
        task, repo, "no-human/dc3b72f7-2", "main", "att-1",
        commit=SimpleNamespace(files_changed=1, insertions=1, deletions=0, sha="a"),
        result=SimpleNamespace(final_text="did the thing", num_turns=1))

    assert len(calls) == 2, f"one forced retry expected, got {calls}"
    assert calls[1]["force_with_lease"] is True
    assert url == ""
    assert orch._draft_pr_absent == "open failed"
    assert any(e.get("kind") == "pr_open_retry" for e in events)


def test_the_force_decision_is_finalizes_single_source_no_second_heuristic():
    """AC 3 — the comment block at the draft site names `_finalize`'s retry as
    the single source of the force decision; `_is_non_fast_forward(` (the
    actual call, not prose about it) appears exactly once."""
    source = inspect.getsource(Orchestrator._open_draft_pr_for_review)
    assert source.count("_is_non_fast_forward(") == 1, (
        "the draft site must call the module-level predicate exactly once — a "
        "second occurrence would mean a second heuristic was introduced"
    )
    assert "_finalize" in source, (
        "the comment block must name _finalize's retry as the single source of "
        "the force decision"
    )
