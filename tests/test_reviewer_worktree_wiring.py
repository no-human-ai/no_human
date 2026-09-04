"""The orchestrator half of the reviewer-worktree integrity gate.

`tests/test_reviewer_worktree.py` covers the MODULE: `snapshot`, `compare` and
`revert` each raise `WorktreeCheckFailed` rather than reporting a clean
answer they could not obtain. That is only half the gate. The other half is
what `Orchestrator._run_reviewer` DOES with that exception, and it had no
test at all: the whole suite stayed green with the post-review handler
replaced by `except WorktreeCheckFailed: return decision`, i.e. with the gate
handing the reviewer's PASS straight through whenever integrity could not be
established. That is the exact failure this module's docstring says it
exists to replace ("the previous attempt's check failed OPEN").

Measured on base `4e15dec9a` with exactly that ablation: `10391 passed,
210 skipped`, and 10391 + 210 = 10601 = that tree's collected count — the
closure check is what makes the figure trustworthy. An earlier revision of
this docstring said 10381, a number carried over from a review of a DIFFERENT
tree; 10381 + 210 = 10591, which is no tree's collected count here.

The property pinned here is deliberately stated as a property, not as a list
of call sites:

    NO path through `_run_reviewer` may return the reviewer's decision when
    worktree integrity could not be established.

`_run_reviewer` makes three integrity calls — `snapshot` before the review,
`compare` after it, and `revert` when the reviewer wrote something — and each
is a separate `except WorktreeCheckFailed` clause, so each is separately
deletable. Every one is exercised below. If a fourth integrity call is ever
added, this file is where its case belongs.

Recovered from the superseded PR #706, whose
`test_uncheckable_is_not_treated_as_clean` covered the `compare` site.
"""

import subprocess

import pytest

from no_human.config import load_config
from no_human.core import reviewer_worktree as rw
from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task, TaskStatus
from no_human.notify.slack import SlackNotifier
from no_human.review.reviewer import ReviewDecision, ReviewerUnavailable
from no_human.review.selfcheck import ChecklistItem


class _Backend:
    async def run(self, *a, **k):  # pragma: no cover - never reached here
        raise AssertionError("the coding backend must not run in a review test")


class _PassingReviewer:
    """Returns an unambiguous PASS, so a leak is visible as a PASS."""

    def __init__(self):
        self.calls = 0

    async def review(self, task, **kwargs):
        self.calls += 1
        return ReviewDecision(
            passed=True,
            checklist=[ChecklistItem("everything", True, "the reviewer said so")],
            raw_output="PASS",
        )


def _git_repo(path):
    """A real, minimal git repo — `_run_reviewer` snapshots `repo_path`
    unconditionally and needs a resolvable `git rev-parse HEAD`."""
    def run(*a):
        return subprocess.run(["git", *a], cwd=path, check=True,
                              capture_output=True)

    path.mkdir(parents=True, exist_ok=True)
    run("init", "-q")
    run("config", "user.email", "t@t")
    run("config", "user.name", "t")
    run("commit", "--allow-empty", "-qm", "base")
    return path


def _task():
    return Task(id="aaa", source="test", title="t",
                status=TaskStatus.PENDING, acceptance_criteria=[])


def _boom(*a, **k):
    raise rw.WorktreeCheckFailed("git rev-parse HEAD timed out after 30.0s")


def _dirty(*a, **k):
    """A non-empty delta, so the `revert` call site is reached at all."""
    return rw.Delta(added=[], modified=["src/no_human/core/orchestrator.py"],
                    deleted=[])


# Which integrity call fails, and whether the reviewer has already run by then.
# `revert` is only reached when `compare` reports the reviewer DID write, so
# that case needs `compare` stubbed to a non-empty delta first.
_SITES = [
    ("snapshot", False),
    ("compare", True),
    ("revert", True),
]


@pytest.mark.parametrize("failing_call, reviewer_already_ran", _SITES)
async def test_an_unestablishable_integrity_check_never_returns_the_verdict(
    store, tmp_path, failing_call, reviewer_already_ran, monkeypatch,
):
    """Each integrity call, when it cannot answer, must raise rather than let
    the reviewer's PASS through — and must say so in an event a human can
    find. Asserting only "it raised" would leave the silent-failure half
    untested; asserting only the event would let the PASS leak."""
    events = []
    repo = _git_repo(tmp_path / "repo")
    cfg = load_config(tmp_path / "config.yaml")
    orch = Orchestrator(store, cfg.data, _Backend(), SlackNotifier(None),
                        event_sink=events.append)
    reviewer = _PassingReviewer()
    orch.reviewer = reviewer

    if failing_call == "revert":
        monkeypatch.setattr(rw, "compare", _dirty)
    monkeypatch.setattr(rw, failing_call, _boom)

    with pytest.raises(ReviewerUnavailable):
        await orch._run_reviewer(_task(), repo_path=repo)

    # The reviewer's own verdict must not have been consumed as the answer.
    assert reviewer.calls == (1 if reviewer_already_ran else 0), (
        f"{failing_call}: reviewer ran {reviewer.calls} time(s)")

    uncheckable = [e for e in events
                   if (e.get("kind") if isinstance(e, dict) else None)
                   == "reviewer_worktree_uncheckable"]
    assert uncheckable, (
        f"{failing_call} failed closed but emitted no "
        f"reviewer_worktree_uncheckable event: {events}")


async def test_a_clean_worktree_still_returns_the_reviewers_verdict(
    store, tmp_path,
):
    """The positive control for the three cases above. Without it, a change
    that made `_run_reviewer` raise unconditionally would keep them all green
    while destroying the gate in the other direction."""
    events = []
    repo = _git_repo(tmp_path / "repo")
    cfg = load_config(tmp_path / "config.yaml")
    orch = Orchestrator(store, cfg.data, _Backend(), SlackNotifier(None),
                        event_sink=events.append)
    orch.reviewer = _PassingReviewer()

    decision = await orch._run_reviewer(_task(), repo_path=repo)

    assert decision.passed is True
    assert not [e for e in events
                if (e.get("kind") if isinstance(e, dict) else None)
                == "reviewer_worktree_uncheckable"]


async def test_the_integrity_verdict_reaches_production_carrying_its_paths(
    store, tmp_path, monkeypatch,
):
    """The PRODUCTION wiring, which nothing asserted.

    A sibling test asserts what `_integrity_failure_decision` RETURNS, calling
    the helper directly. That leaves the call site untested: an independent
    review replaced the call in `_run_reviewer` with an inline, counts-only
    `ReviewDecision` and every then-existing reviewer test stayed GREEN
    (no count given: a number here is uncheckable from the shipped tree and
    this branch's own rule forbids those) — production would
    silently lose the paths, which is the entire defect this branch exists to
    fix. Extracting the helper moved the untested boundary up one level rather
    than closing it.

    So this drives the REAL `_run_reviewer` to the real call site and asserts
    the path survives all the way into the returned verdict.
    """
    written = "src/no_human/core/orchestrator.py"

    def _clean_revert(*a, **k):
        return None

    monkeypatch.setattr(rw, "compare", _dirty)
    monkeypatch.setattr(rw, "revert", _clean_revert)

    events = []
    repo = _git_repo(tmp_path / "repo")
    cfg = load_config(tmp_path / "config.yaml")
    orch = Orchestrator(store, cfg.data, _Backend(), SlackNotifier(None),
                        event_sink=events.append)
    orch.reviewer = _PassingReviewer()

    decision = await orch._run_reviewer(_task(), repo_path=repo)

    # The reviewer PASSED; the integrity failure must override it.
    assert decision.passed is False, (
        "a reviewer that wrote to the worktree it was judging had its PASS "
        "returned as the verdict")
    assert len(decision.checklist) == 1
    item = decision.checklist[0]
    assert item.label == "reviewer worktree integrity"
    assert item.passed is False
    assert written in item.evidence, (
        f"the production call site produced a verdict WITHOUT the path it "
        f"objected to — this is the counts-only regression, live: "
        f"{item.evidence!r}")


# --------------------------------------------------------------------------- #
# Outcome-level ablation pin for `_run_git`'s returncode check
# (task reviewer-worktree-returncode-audit)
# --------------------------------------------------------------------------- #

async def test_a_failing_git_status_after_the_review_is_never_read_as_a_clean_tree(
    store, tmp_path, monkeypatch,
):
    """End-to-end ablation pin for the `if proc.returncode != 0:` check in
    `reviewer_worktree._run_git` (reviewer_worktree.py, immediately after the
    `TimeoutExpired`/`OSError` `except` clauses).

    Only the `git status` call `compare()` makes AFTER the review is made to
    fail (armed by the reviewer's own `review()`, simulating a compromised
    session that also breaks the post-review probe); the PRE-review
    `snapshot()` call underneath `_run_reviewer` runs for real and succeeds,
    and `git rev-parse HEAD` always succeeds too — so nothing except the
    returncode check stands between this failure and `_run_reviewer` handing
    back the reviewer's own PASS untouched (the `delta.is_empty()` branch
    reading a falsely-empty post-review status as "the reviewer wrote
    nothing").

    Ablate the check (`if False and proc.returncode != 0:` in `_run_git`) and
    this test goes RED: `_run_reviewer` returns `decision.passed is True`
    instead of raising, and no `reviewer_worktree_uncheckable` event fires.
    """
    real_run = subprocess.run
    armed = {"on": False}

    def _fail_status_when_armed(*args, **kwargs):
        argv = args[0] if args else kwargs.get("args")
        if armed["on"] and argv and "status" in argv:
            return subprocess.CompletedProcess(
                argv, returncode=128, stdout="",
                stderr="fatal: not a git repository (or any parent)")
        return real_run(*args, **kwargs)

    monkeypatch.setattr(rw.subprocess, "run", _fail_status_when_armed)

    events = []
    repo = _git_repo(tmp_path / "repo")
    cfg = load_config(tmp_path / "config.yaml")
    orch = Orchestrator(store, cfg.data, _Backend(), SlackNotifier(None),
                        event_sink=events.append)

    class _ArmingReviewer(_PassingReviewer):
        """Arms the `git status` failure only once the reviewer has actually
        run — so the PRE-review `snapshot()` call is untouched (control) and
        only the POST-review `compare()` call sees the broken git."""

        async def review(self, task, **kwargs):
            armed["on"] = True
            return await super().review(task, **kwargs)

    reviewer = _ArmingReviewer()
    orch.reviewer = reviewer

    with pytest.raises(ReviewerUnavailable):
        await orch._run_reviewer(_task(), repo_path=repo)

    # The reviewer's PASS must not have been the thing that came back.
    assert reviewer.calls == 1

    uncheckable = [e for e in events
                   if (e.get("kind") if isinstance(e, dict) else None)
                   == "reviewer_worktree_uncheckable"]
    assert uncheckable, (
        f"a failed post-review `git status` did not raise/emit — it was "
        f"read as a clean tree: {events}")


async def test_a_reviewer_that_really_wrote_reports_its_path_and_is_reverted(
    store, tmp_path,
):
    """Positive control B: a REAL reviewer write, through the REAL
    `snapshot`/`compare`/`revert` machinery — no monkeypatching of any of the
    three. Companion to `test_a_clean_worktree_still_returns_the_reviewers_
    verdict` (positive control A, genuinely clean -> real verdict): together
    they rule out a fail-closed-on-everything regression, which a change that
    made every path raise `WorktreeCheckFailed` unconditionally would still
    pass if only the ablation/uncheckable tests existed.
    """
    planted = "reviewer_left_this.txt"

    class _WritingReviewer(_PassingReviewer):
        async def review(self, task, **kwargs):
            (kwargs["repo_path"] / planted).write_text("not part of the diff\n")
            return await super().review(task, **kwargs)

    events = []
    repo = _git_repo(tmp_path / "repo")
    cfg = load_config(tmp_path / "config.yaml")
    orch = Orchestrator(store, cfg.data, _Backend(), SlackNotifier(None),
                        event_sink=events.append)
    reviewer = _WritingReviewer()
    orch.reviewer = reviewer

    decision = await orch._run_reviewer(_task(), repo_path=repo)

    assert reviewer.calls == 1
    assert decision.passed is False, (
        "a reviewer that really wrote a file into the worktree it was "
        "judging had its own PASS returned as the verdict")
    assert len(decision.checklist) == 1
    item = decision.checklist[0]
    assert item.label == "reviewer worktree integrity"
    assert item.passed is False
    assert planted in item.evidence, (
        f"the real write was not reported by path: {item.evidence!r}")
    assert not (repo / planted).exists(), (
        "the reviewer's real write survived instead of being reverted")


async def test_a_bookkeeping_config_write_keeps_the_verdict_and_records_an_event(
    store, tmp_path, monkeypatch,
):
    """The production wiring for `delta.benign` (reviewer-worktree-benign-
    config-write): a `compare()` result whose ONLY change is an excused
    `.git/common/config` bookkeeping key must still return the reviewer's
    PASS, and must disclose the excused write through its own event kind —
    never through `reviewer_wrote`, which is reserved for a discard.
    """
    def _benign_only(*a, **k):
        return rw.Delta(added=[], modified=[], deleted=[],
                        benign=[".git/common/config"],
                        benign_keys=["branch.x.rebase"])

    monkeypatch.setattr(rw, "compare", _benign_only)

    events = []
    repo = _git_repo(tmp_path / "repo")
    cfg = load_config(tmp_path / "config.yaml")
    orch = Orchestrator(store, cfg.data, _Backend(), SlackNotifier(None),
                        event_sink=events.append)
    reviewer = _PassingReviewer()
    orch.reviewer = reviewer

    decision = await orch._run_reviewer(_task(), repo_path=repo)

    assert reviewer.calls == 1
    assert decision.passed is True, (
        "a benign-only .git/common/config write discarded the reviewer's "
        "PASS instead of accepting it")

    written = [e for e in events
               if (e.get("kind") if isinstance(e, dict) else None)
               == "reviewer_wrote"]
    assert not written, (
        f"a benign-only config write was reported through reviewer_wrote, "
        f"which discards the verdict: {written}")

    benign_events = [e for e in events
                     if (e.get("kind") if isinstance(e, dict) else None)
                     == "reviewer_worktree_benign_git_write"]
    assert len(benign_events) == 1, (
        f"expected exactly one benign-git-write event, got: {benign_events}")
    event = benign_events[0]
    assert ".git/common/config" in event.get("paths", []), event
    assert "branch.x.rebase" in event.get("keys", []), event

