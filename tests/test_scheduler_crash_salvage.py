"""A crash after the coder commits must not strand the work behind a plain
FAILED with no human-visible record.

Reproduces the incident directly: an unhandled exception from
`Orchestrator._assert_delivery_sha` (called first thing inside `_finalize`,
AFTER the coder's commit and BEFORE `open_pr` — see `orchestrator.py:7916`,
wrapped only in `except ReviewedShaMismatch`, so any other exception type —
an `AttributeError` from an unguarded `.strip()`, the incident's exact
shape — propagates unhandled all the way to `Scheduler._run`'s pool-crash
`except Exception` handler). That handler now calls
`Scheduler._salvage_committed_work` before it marks the task, and the whole
point of this suite is to drive that seam with a REAL git repo, a REAL
`Orchestrator`, and a REAL `Scheduler` — never by monkeypatching the
scheduler's own recording function, which would prove nothing about the
actual crash path.

Modelled on `tests/test_e2e_orchestrator.py` (the `bare_repo` fixture, the
`FakeBackend` harness, `_config`) and `tests/test_scheduler.py` (the
`CrashingOrch`-driven pool-crash pattern, `_mk_tasks`).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from no_human.api.models import TaskOut
from no_human.blockers.landed_override import approve_landed_override
from no_human.config import load_config
from no_human.core.lanes import lane_for
from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task, TaskStatus
from no_human.notify.slack import SlackNotifier
from no_human.core.scheduler import Scheduler

# Same pattern `tests/test_evidence_ledger.py` uses to reach a sibling test
# module's fixtures (`import test_pr_evidence as _P`): these are bare,
# same-directory imports, not package-relative ones, so the directory has to
# be on `sys.path` first — it usually isn't when pytest is pointed at this
# single file rather than collecting the whole `tests/` tree.
sys.path.insert(0, str(Path(__file__).parent))
from test_e2e_orchestrator import FakeBackend, bare_repo  # noqa: F401 — fixture
from test_scheduler import _mk_tasks


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   text=True)


def _git_out(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


def _repo_with_squash_landed(tmp_path, name="landed-override-repo"):
    """A minimal, self-contained inline of the same shape
    `tests/test_landed_override.py::_repo_with_squash_landed` builds (not
    imported from there — that file is a separate suite, out of this
    change's scope): `feature`'s content is hand-landed onto `main` as a
    SEPARATE commit whose tree is content-equivalent to `feature`'s tip.
    Returns ``(repo, feature_sha, landed_sha)``."""
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("orig\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "initial")
    _git(repo, "checkout", "-b", "feature")
    (repo / "b.txt").write_text("new\n")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-m", "feature: add b.txt")
    feature_sha = _git_out(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "main")
    (repo / "b.txt").write_text("new\n")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-m", "hand-landed: add b.txt")
    landed_sha = _git_out(repo, "rev-parse", "main")
    return repo, feature_sha, landed_sha


def _config(tmp_path):
    """Same deliberate, stated skips as `test_e2e_orchestrator.py::_config` —
    these tests exercise the pool-crash/salvage path, not the review gate or
    the escalation-quality gate, and no real Claude calls are made."""
    cfg = load_config(tmp_path / "config.yaml")
    cfg.data.setdefault("planning", {})["enabled"] = False
    cfg.data.setdefault("reviewer", {})["allow_advisory"] = True
    cfg.data.setdefault("blockers", {})["challenge"] = False
    return cfg


def _boom_assert_delivery_sha(self, task, repo, branch, human_gated_resume=False):
    """Stands in for the production seam that actually raised, unguarded,
    during the incident: `_finalize`'s `try/except ReviewedShaMismatch` around
    `_assert_delivery_sha` does not catch `AttributeError`, so this propagates
    all the way to `Scheduler._run`'s pool-crash handler — exactly like the
    live incident did."""
    raise AttributeError("'NoneType' object has no attribute 'strip'")


async def _run_crash_after_commit(bare_repo, tmp_path, store, monkeypatch):
    """Drives one real attempt through a real `Orchestrator`/`Scheduler`: the
    coder edits + commits real, non-tampering content, then `_finalize`
    crashes before a PR ever opens. Returns the task id."""
    def mutate(cwd):
        (cwd / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\n"
            "def mul(a, b):\n    return a * b\n"
        )
        (cwd / "test_calc.py").write_text(
            "from calc import add, mul\n\n"
            "def test_add():\n    assert add(1, 2) == 3\n\n"
            "def test_mul():\n    assert mul(2, 3) == 6\n"
        )

    monkeypatch.setattr(Orchestrator, "_assert_delivery_sha",
                        _boom_assert_delivery_sha)

    cfg = _config(tmp_path)
    sched = Scheduler(
        store,
        lambda task: Orchestrator(store, cfg.data, FakeBackend(mutate),
                                  SlackNotifier(None)),
        max_workers=1,
    )
    t = Task.new("add mul()", repo_path=str(bare_repo))
    t.acceptance_criteria = ["mul(a,b) returns a*b"]
    await store.create_task(t)

    await sched.tick()
    await sched.wait_idle()

    return t.id, sched


async def test_a_crash_after_the_commit_records_the_branch_and_sha(
        bare_repo, tmp_path, store, monkeypatch):
    task_id, _sched = await _run_crash_after_commit(
        bare_repo, tmp_path, store, monkeypatch)

    task = await store.get_task(task_id)
    rec = task.context.get("salvaged_work")
    assert rec, f"no salvaged_work recorded; context={task.context}"

    branch = rec["branch"]
    assert branch.startswith("no-human/"), branch
    real_sha = _git_out(bare_repo, "rev-parse", "--verify", branch)
    assert rec["commit_sha"] == real_sha, (
        "the recorded sha must match the real branch tip, read independently"
    )

    # Human-visible, TOP-LEVEL fields — not nested in context/details.
    attempts = await store.list_attempts(task_id)
    out = TaskOut.from_task(task, attempts)
    assert out.salvaged_branch == branch
    assert out.salvaged_commit_sha == real_sha

    newest = attempts[-1]
    assert branch in (newest.get("failure_reason") or "")
    assert real_sha[:8] in (newest.get("failure_reason") or "")


async def test_a_crash_after_the_commit_is_not_a_bare_failed(
        bare_repo, tmp_path, store, monkeypatch):
    task_id, _sched = await _run_crash_after_commit(
        bare_repo, tmp_path, store, monkeypatch)

    task = await store.get_task(task_id)
    assert task.status is TaskStatus.PARTIAL_SUCCESS
    assert lane_for({"status": task.status.value,
                     "blocker_wake_condition": None}) == "failed", (
        "a salvaged task must still be reachable on the board's failed lane"
    )


async def test_a_crash_with_no_commit_still_reports_exactly_what_it_reports_today(
        store):
    """The unchanged case (criteria 3 & 5): a crash with no attempt row and
    no commit at all — the exact `CrashingOrch` shape already covered by
    `tests/test_scheduler.py:1630` — must still be a bare FAILED with the
    same crash-event shape, and no salvage anything."""
    class CrashingOrch:
        async def run_task(self, task):
            raise RuntimeError("boom inside the pool")

    sched = Scheduler(store, lambda task=None: CrashingOrch(), max_workers=1)
    ids = await _mk_tasks(store, 1)

    await sched.tick()
    await sched.wait_idle()

    task = await store.find_task(ids[0])
    assert task.status is TaskStatus.FAILED

    events = await store.list_events(ids[0])
    crashed = [e for e in events if e.get("kind") == "task_crashed"]
    assert crashed, "the crash record itself must be unchanged"
    assert "boom inside the pool" in crashed[0]["text"]
    assert "RuntimeError" in crashed[0]["text"]
    assert not any(e.get("kind") == "work_salvaged" for e in events)
    assert not (task.context or {}).get("salvaged_work")

    attempts = await store.list_attempts(ids[0])
    out = TaskOut.from_task(task, attempts)
    assert out.salvaged_branch is None
    assert out.salvaged_commit_sha is None

    # The two crash shapes are queryable as genuinely DIFFERENT terminal
    # states — not merely two records that happen to read differently.
    assert TaskStatus.FAILED is not TaskStatus.PARTIAL_SUCCESS
    failed_rows = await store.list_claimable_tasks(TaskStatus.FAILED)
    partial_rows = await store.list_claimable_tasks(TaskStatus.PARTIAL_SUCCESS)
    assert any(t.id == ids[0] for t in failed_rows)
    assert not any(t.id == ids[0] for t in partial_rows)


async def test_the_crash_record_survives_the_salvage(
        bare_repo, tmp_path, store, monkeypatch):
    """The whole point: salvaging the work must not quiet the crash. If
    someone later 'fixes' the unknown `None` by defaulting the `.strip()`
    call site, this exception stops firing and this test goes red."""
    task_id, _sched = await _run_crash_after_commit(
        bare_repo, tmp_path, store, monkeypatch)

    events = await store.list_events(task_id)
    crashed = [e for e in events if e.get("kind") == "task_crashed"]
    assert crashed, "the crash record must still exist after a salvage"
    assert "AttributeError" in crashed[0]["text"]
    assert "'NoneType' object has no attribute 'strip'" in crashed[0]["text"]
    assert crashed[0]["exit_code"] is None, (
        "an ordinary Python exception must not fabricate an exit status"
    )

    salvaged = [e for e in events if e.get("kind") == "work_salvaged"]
    assert salvaged, "a durable work_salvaged event must exist alongside the crash"


async def test_a_salvaged_task_is_never_re_dispatched(
        bare_repo, tmp_path, store, monkeypatch):
    task_id, sched = await _run_crash_after_commit(
        bare_repo, tmp_path, store, monkeypatch)

    before = await store.get_task(task_id)
    assert before.status is TaskStatus.PARTIAL_SUCCESS

    await sched.tick()
    await sched.wait_idle()

    after = await store.get_task(task_id)
    assert after.status is TaskStatus.PARTIAL_SUCCESS, (
        "a terminal, salvaged task must never be re-claimed by a later tick"
    )
    assert task_id not in sched.inflight


async def test_a_salvaged_task_is_still_eligible_for_the_landed_override(
        tmp_path, store):
    """`PARTIAL_SUCCESS` must resolve to the same `failed_pre_pr` repair
    shape `FAILED` does — the new status must never be strictly harder to
    hand-land-rescue than the one it replaces. Built directly (not via a
    real crash-and-salvage run, and not via `test_landed_override.py`'s
    helpers — a separate suite, out of this change's scope): a task whose
    only open attempt recorded a branch+commit, exactly the shape
    `Scheduler._salvage_committed_work` itself produces."""
    repo, feature_sha, landed_sha = _repo_with_squash_landed(tmp_path)
    t = Task.new("landed-override-check", repo_path=str(repo))
    t.context = {"base_branch": "main"}
    await store.create_task(t)
    attempt_id = await store.create_attempt(t.id, 1)
    await store.update_attempt(
        attempt_id, branch_name="feature", commit_sha=feature_sha,
        status="failed", failure_reason=(
            f"crashed after committing {feature_sha[:8]} on feature, "
            "before a PR was opened"))
    await store.merge_context(t.id, {"salvaged_work": {
        "branch": "feature", "commit_sha": feature_sha,
        "at": "2026-01-01T00:00:00+00:00", "error": None, "pushed": False}})
    await store.set_status(t, TaskStatus.PARTIAL_SUCCESS, validate=False)
    assert t.status is TaskStatus.PARTIAL_SUCCESS

    result = await approve_landed_override(
        store, t, landed_sha, "hand-landed by operator")

    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.DONE
    assert result["shape"] == "failed_pre_pr"
    assert result["prior_status"] == "partial_success"
