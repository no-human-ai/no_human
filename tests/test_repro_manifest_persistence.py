"""A repro manifest a passing gate wrote must survive a re-created worktree —
and when it cannot be recovered for free, the coder gets ONE bounded
regenerate nudge before the attempt is failed, not immediately.

BUG: `.no_human/**` is gitignored, so `.no_human/repro_tests.json` (written
by a passing repro gate) lives only as untracked state in the attempt's
worktree. A worktree re-created for the same task (e.g. a dogfood server
restart mints a new pid -> a new `~/.no_human/worktrees/<task>.<pid>.<hash>`)
starts with no `.no_human/` at all — the gate then reads that absence as
`waived` and discards a whole coder round that may already have been correct
and fully proven, purely because its OWN evidence file did not survive the
directory swap.

FIX (two parts):
  1. `no_human.testing.repro_gate.persist_manifest`/`restore_manifest` copy
     the manifest to/from a per-TASK (not per-worktree) location under
     `~/.no_human/artifacts/<task_id>/repro_tests.json` —
     `Orchestrator._verification_artifact_path`'s existing storage pattern.
     `_repro_gate_step` persists on every `pass`; `_run_task_body` restores
     right after acquiring a worktree, and `_repro_gate_step` itself
     restores again as a zero-LLM-spend backstop when the first verdict of
     an attempt is still `waived`.
  2. When restore cannot recover it (no persisted copy, or a persisted copy
     that fails to parse) but `has_persisted_manifest` proves this task DID
     pass the gate before, the coder gets ONE bounded, low-effort,
     one-turn nudge (`_REPRO_REGENERATE_NUDGE`, reusing
     `_repro_corrective_round` with its new `effort` override) asking it to
     name the fails-before test it already wrote, before falling through to
     today's unchanged 15-turn/`effort="high"` corrective round and, only
     after THAT still fails, the existing `"repro gate waived: …"` text.

These tests drive `_run_attempt`/`_run_task_body` directly against a real
bare-repo checkout with a scripted backend (the pattern
`test_repro_waived_corrective_round.py` and `test_worktree_isolation.py`
use), so the whole pipeline — gate, restore, nudge, corrective round,
commit, tamper check — runs for real except for the LLM call itself.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from no_human.agent.claude_backend import AgentEvent, AgentResult
from no_human.config import load_config
from no_human.core.infra_breaker import infra_breaker
from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task, TaskStatus
from no_human.notify.slack import SlackNotifier
from no_human.testing.repro_gate import (
    MANIFEST as REPRO_MANIFEST,
    has_persisted_manifest,
    persist_manifest,
    restore_manifest,
    task_manifest_path,
)
from no_human.vcs import GitRepo


@pytest.fixture(autouse=True)
def _clean_infra_breaker_singleton():
    """The breaker is a process-wide singleton; reset it around every test in
    this file so one test's infra failures can never leak into the next
    one's assertions — copied from `test_repro_waived_corrective_round.py`."""
    infra_breaker().reset()
    yield
    infra_breaker().reset()


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def bare_repo(tmp_path):
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True,
                   capture_output=True)
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.email", "u@e.com")
    _git(work, "config", "user.name", "u")
    # A product file + an existing, already-passing test — the base the repro
    # gate's `fails-before` check runs against.
    (work / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (work / "test_calc.py").write_text(
        "from calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n"
    )
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "init")
    _git(work, "remote", "add", "origin", str(bare))
    _git(work, "push", "-u", "origin", "main")
    return work


def _config(tmp_path):
    cfg = load_config(tmp_path / "config.yaml")
    cfg.data.setdefault("planning", {})["enabled"] = False
    cfg.data.setdefault("reviewer", {})["allow_advisory"] = True
    cfg.data.setdefault("blockers", {})["challenge"] = False
    return cfg


def _mul_files(cwd: Path) -> None:
    """A real fix (`mul`) plus its demonstrating test — no manifest."""
    cwd.joinpath("calc.py").write_text(
        "def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a * b\n"
    )
    cwd.joinpath("test_calc.py").write_text(
        "from calc import add, mul\n\n"
        "def test_add():\n    assert add(1, 2) == 3\n\n"
        "def test_mul():\n    assert mul(2, 3) == 6\n"
    )


async def _run_one_bugfix_attempt(store, bare_repo, tmp_path, backend):
    """Walk a fresh bugfix task to PLANNING and hand back everything a test
    needs to drive `_run_attempt` directly — copied from
    `test_repro_waived_corrective_round.py`."""
    cfg = _config(tmp_path)
    events = []
    orch = Orchestrator(store, cfg.data, backend, SlackNotifier(None),
                        event_sink=events.append)
    task = Task.new("fix mul()", repo_path=str(bare_repo), kind="bugfix")
    task.acceptance_criteria = ["mul(a,b) returns a*b"]
    await store.create_task(task)
    await store.set_status(task, TaskStatus.CONTEXT)
    await store.set_status(task, TaskStatus.PLANNING)
    repo = GitRepo(bare_repo)
    return orch, task, repo, events


def _edit_event():
    return AgentEvent("tool_use", tool_name="Edit", tool_input={"file_path": "calc.py"})


# --------------------------------------------------------------------------- #
# AC1 — persisted per task, restored into a re-created worktree               #
# --------------------------------------------------------------------------- #


class _PassingBackend:
    """One coder turn: real fix + a valid manifest, straight to `pass`."""

    def __init__(self):
        self.calls = 0

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        self.calls += 1
        cwd = Path(cwd)
        if on_event is not None:
            on_event(_edit_event())
        _mul_files(cwd)
        cwd.joinpath(".no_human").mkdir(exist_ok=True)
        (cwd / REPRO_MANIFEST).write_text('{"tests": ["test_calc.py::test_mul"]}')
        return AgentResult(final_text="done", num_turns=2, is_error=False,
                           tokens_used=100, session_id="s", stop_reason="end_turn")


async def test_a_passing_gate_persists_the_manifest_under_the_task_artifacts_dir(
        bare_repo, tmp_path, store):
    """RED before the fix: `task_manifest_path`/`persist_manifest` do not
    exist yet, so nothing survives a passing gate outside the worktree."""
    backend = _PassingBackend()
    orch, task, repo, events = await _run_one_bugfix_attempt(
        store, bare_repo, tmp_path, backend)

    outcome = await orch._run_attempt(task, repo, 1, "main")

    assert outcome.status is TaskStatus.AWAITING_APPROVAL, outcome.detail
    gate_events = [e for e in events if e["kind"] == "repro_gate"]
    assert [e["verdict"] for e in gate_events] == ["pass"], gate_events

    persisted = task_manifest_path(task.id)
    assert persisted.is_file(), persisted
    assert persisted.read_text() == (repo.path / REPRO_MANIFEST).read_text()

    persist_events = [e for e in events if e["kind"] == "repro_manifest_persisted"]
    assert len(persist_events) == 1, persist_events


async def test_a_recreated_worktree_gets_the_manifest_back_before_the_attempt_runs(
        bare_repo, tmp_path, store):
    """RED before the fix: `_restore_repro_manifest` does not exist, so a
    worktree that lost its `.no_human/repro_tests.json` stays lost."""
    backend = _PassingBackend()
    orch, task, repo, events = await _run_one_bugfix_attempt(
        store, bare_repo, tmp_path, backend)
    outcome = await orch._run_attempt(task, repo, 1, "main")
    assert outcome.status is TaskStatus.AWAITING_APPROVAL, outcome.detail

    original = (repo.path / REPRO_MANIFEST).read_bytes()
    (repo.path / REPRO_MANIFEST).unlink()
    assert not (repo.path / REPRO_MANIFEST).exists()
    events.clear()

    wrote = orch._restore_repro_manifest(task, repo)

    assert wrote is True
    assert (repo.path / REPRO_MANIFEST).read_bytes() == original
    restored_events = [e for e in events if e["kind"] == "repro_manifest_restored"]
    assert len(restored_events) == 1, restored_events


async def test_the_restore_runs_on_worktree_acquisition(
        bare_repo, tmp_path, store, monkeypatch):
    """`_run_task_body` must call `_restore_repro_manifest` right after
    `_acquire_worktree` succeeds — before `_drive_watched`/the first attempt
    — so the restore can never silently drift to after an attempt has
    already read a missing manifest as `waived`."""
    cfg = _config(tmp_path)
    cfg.data.setdefault("isolation", {})["worktree_root"] = str(tmp_path / "wt")
    orch = Orchestrator(store, cfg.data, object(), SlackNotifier(None))
    task = Task.new("fix mul()", repo_path=str(bare_repo), kind="bugfix")
    await store.create_task(task)

    calls: list[str] = []
    real_acquire = orch._acquire_worktree
    real_restore = orch._restore_repro_manifest

    def _acquire(main_repo, wt_path, base):
        result = real_acquire(main_repo, wt_path, base)
        calls.append("acquire")
        return result

    def _restore(t, r):
        calls.append("restore")
        return real_restore(t, r)

    monkeypatch.setattr(orch, "_acquire_worktree", _acquire)
    monkeypatch.setattr(orch, "_restore_repro_manifest", _restore)

    async def _capture(t, r):
        from no_human.core.orchestrator import TaskOutcome
        return TaskOutcome(t, status=TaskStatus.DONE, detail="stub")

    monkeypatch.setattr(orch, "_drive_watched", _capture)

    await orch.run_task(task)

    assert calls == ["acquire", "restore"], calls


def test_restore_never_overwrites_a_live_manifest_and_never_enshrines_junk(tmp_path):
    home = tmp_path / "home"
    repo_path = tmp_path / "repo"
    (repo_path / ".no_human").mkdir(parents=True)

    # A worktree manifest already present is left untouched — the live tree
    # always wins, even when a persisted copy exists for this task.
    live_manifest = '{"tests": ["test_calc.py::test_live"]}'
    (repo_path / REPRO_MANIFEST).write_text(live_manifest)
    persisted_dir = home / "artifacts" / "task-1"
    persisted_dir.mkdir(parents=True)
    (persisted_dir / "repro_tests.json").write_text(
        '{"tests": ["test_calc.py::test_other"]}')

    wrote = restore_manifest(repo_path, "task-1", home=home)

    assert wrote is False
    assert (repo_path / REPRO_MANIFEST).read_text() == live_manifest

    # A corrupt worktree manifest is never enshrined as the task's record.
    (repo_path / REPRO_MANIFEST).write_text("not json{{{")

    ok = persist_manifest(repo_path, "task-2", home=home)

    assert ok is False
    assert not (home / "artifacts" / "task-2").exists()


class _OneTurnNoManifestBackend:
    """One coder turn: real fix + test, no manifest — the gate must be
    restored to `pass` for free from a pre-seeded persisted copy, with no
    second backend call."""

    def __init__(self):
        self.calls = 0

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        self.calls += 1
        cwd = Path(cwd)
        if on_event is not None:
            on_event(_edit_event())
        _mul_files(cwd)
        return AgentResult(final_text="done", num_turns=2, is_error=False,
                           tokens_used=100, session_id="s", stop_reason="end_turn")


async def test_the_gate_restores_and_passes_with_no_backend_call(
        bare_repo, tmp_path, store):
    """RED before the fix: with no restore backstop, a persisted manifest
    that exists but never made it into THIS worktree is invisible to the
    gate, which reads `waived` and burns a full corrective round on it."""
    backend = _OneTurnNoManifestBackend()
    orch, task, repo, events = await _run_one_bugfix_attempt(
        store, bare_repo, tmp_path, backend)

    persisted = task_manifest_path(task.id)
    persisted.parent.mkdir(parents=True, exist_ok=True)
    persisted.write_text('{"tests": ["test_calc.py::test_mul"]}')

    outcome = await orch._run_attempt(task, repo, 1, "main")

    assert outcome.status is TaskStatus.AWAITING_APPROVAL, outcome.detail
    assert backend.calls == 1, "no backend call beyond the coder turn"

    gate_events = [e for e in events if e["kind"] == "repro_gate"]
    assert [e["verdict"] for e in gate_events] == ["waived", "pass"], gate_events

    restored_events = [e for e in events if e["kind"] == "repro_manifest_restored"]
    assert len(restored_events) == 1, restored_events

    round_events = [e for e in events if e["kind"] == "repro_corrective_round"]
    assert not round_events, round_events
    nudge_events = [e for e in events if e["kind"] == "repro_manifest_regenerate_nudge"]
    assert not nudge_events, nudge_events


# --------------------------------------------------------------------------- #
# AC2 — exactly one bounded regenerate nudge, both outcomes                   #
# --------------------------------------------------------------------------- #


class _CorruptPersistedThenNudgeWritesBackend:
    """Coder turn: real fix + test, no manifest -> waived (the persisted
    record is corrupt, so the zero-spend restore backstop cannot recover
    it). The regenerate nudge (call 2) writes a valid manifest -> pass."""

    def __init__(self):
        self.calls = 0
        self.recorded_calls = []

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        self.calls += 1
        self.recorded_calls.append(
            {"prompt": prompt, "max_turns": max_turns, "effort": effort})
        cwd = Path(cwd)
        if self.calls == 1:
            if on_event is not None:
                on_event(_edit_event())
            _mul_files(cwd)
            return AgentResult(final_text="done", num_turns=2, is_error=False,
                               tokens_used=100, session_id="s", stop_reason="end_turn")
        cwd.joinpath(".no_human").mkdir(exist_ok=True)
        (cwd / REPRO_MANIFEST).write_text('{"tests": ["test_calc.py::test_mul"]}')
        return AgentResult(final_text="wrote manifest", num_turns=1, is_error=False,
                           tokens_used=10, session_id="s2", stop_reason="end_turn")


def _seed_corrupt_persisted_manifest(task_id: str) -> None:
    persisted = task_manifest_path(task_id)
    persisted.parent.mkdir(parents=True, exist_ok=True)
    persisted.write_text("not valid json {{{")


async def test_a_lost_manifest_buys_exactly_one_one_turn_low_effort_regenerate_nudge(
        bare_repo, tmp_path, store):
    """RED before the fix: `_repro_gate_step` has no regenerate-nudge path at
    all — a still-`waived` gate after the restore backstop falls straight
    into the existing 15-turn round (or, before that existed, straight to
    failure), never a one-turn/low-effort nudge."""
    backend = _CorruptPersistedThenNudgeWritesBackend()
    orch, task, repo, events = await _run_one_bugfix_attempt(
        store, bare_repo, tmp_path, backend)
    _seed_corrupt_persisted_manifest(task.id)
    assert has_persisted_manifest(task.id)

    outcome = await orch._run_attempt(task, repo, 1, "main")

    assert outcome.status is TaskStatus.AWAITING_APPROVAL, outcome.detail
    assert backend.calls == 2

    nudge_call = backend.recorded_calls[-1]
    assert nudge_call["max_turns"] == 1
    assert nudge_call["effort"] == "low"
    assert REPRO_MANIFEST in nudge_call["prompt"]

    nudge_events = [e for e in events if e["kind"] == "repro_manifest_regenerate_nudge"]
    assert len(nudge_events) == 1, nudge_events

    gate_events = [e for e in events if e["kind"] == "repro_gate"]
    assert [e["verdict"] for e in gate_events] == ["waived", "pass"], gate_events

    # Never falls through to the full 15-turn round on top of a successful nudge.
    round_events = [e for e in events if e["kind"] == "repro_corrective_round"]
    assert not round_events, round_events


class _CorruptPersistedNudgeAndRoundWriteNothingBackend:
    """Coder turn: real fix + test, no manifest -> waived. Neither the
    regenerate nudge (call 2) nor the subsequent 15-turn corrective round
    (call 3) write anything -> still waived, and the attempt must fail with
    today's exact text."""

    def __init__(self):
        self.calls = 0
        self.recorded_calls = []

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        self.calls += 1
        self.recorded_calls.append(
            {"prompt": prompt, "max_turns": max_turns, "effort": effort})
        cwd = Path(cwd)
        if self.calls == 1:
            if on_event is not None:
                on_event(_edit_event())
            _mul_files(cwd)
            return AgentResult(final_text="done", num_turns=2, is_error=False,
                               tokens_used=100, session_id="s", stop_reason="end_turn")
        return AgentResult(final_text="nothing to add", num_turns=1, is_error=False,
                           tokens_used=5, session_id=f"s{self.calls}",
                           stop_reason="end_turn")


async def test_a_nudge_that_writes_nothing_still_fails_with_the_existing_waived_text(
        bare_repo, tmp_path, store):
    backend = _CorruptPersistedNudgeAndRoundWriteNothingBackend()
    orch, task, repo, events = await _run_one_bugfix_attempt(
        store, bare_repo, tmp_path, backend)
    _seed_corrupt_persisted_manifest(task.id)

    outcome = await orch._run_attempt(task, repo, 1, "main")

    assert outcome.status is TaskStatus.FAILED
    assert (outcome.detail or "").startswith("repro gate waived"), outcome.detail
    assert backend.calls == 3  # coder turn, nudge, full round — no retry beyond these

    attempts = await store.list_attempts(task.id)
    assert len(attempts) == 1
    assert attempts[0]["status"] == "failed"
    assert (attempts[0]["failure_reason"] or "").startswith("repro gate waived")

    # Exactly one nudge — no second nudge bought by re-entering the same attempt.
    nudge_events = [e for e in events if e["kind"] == "repro_manifest_regenerate_nudge"]
    assert len(nudge_events) == 1, nudge_events

    # The existing fallthrough round still ran exactly once, unchanged.
    round_events = [e for e in events if e["kind"] == "repro_corrective_round"]
    assert len(round_events) == 1, round_events


# --------------------------------------------------------------------------- #
# AC3 — a task that never had a persisted manifest sees today's exact shape   #
# --------------------------------------------------------------------------- #


class _WaivedTwiceBackend:
    """Coder turn: real fix, no manifest -> waived. Corrective round writes
    nothing at all -> still waived on the re-run. Copied from
    `test_repro_waived_corrective_round.py`."""

    def __init__(self):
        self.calls = 0

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        self.calls += 1
        cwd = Path(cwd)
        if self.calls == 1:
            if on_event is not None:
                on_event(_edit_event())
            _mul_files(cwd)
            return AgentResult(final_text="done", num_turns=2, is_error=False,
                               tokens_used=100, session_id="s", stop_reason="end_turn")
        return AgentResult(final_text="nothing to add", num_turns=1, is_error=False,
                           tokens_used=5, session_id="s2", stop_reason="end_turn")


async def test_the_plain_waived_path_is_untouched(bare_repo, tmp_path, store):
    """A task that never had a persisted manifest (this is its first attempt
    ever) must see EXACTLY today's shape: no restore, no nudge, just the
    existing 15-turn/`effort="high"` corrective round — `backend.calls == 2`,
    matching `test_waived_twice_fails_the_attempt_exactly_as_today`."""
    backend = _WaivedTwiceBackend()
    orch, task, repo, events = await _run_one_bugfix_attempt(
        store, bare_repo, tmp_path, backend)
    assert not has_persisted_manifest(task.id)

    outcome = await orch._run_attempt(task, repo, 1, "main")

    assert outcome.status is TaskStatus.FAILED
    assert (outcome.detail or "").startswith("repro gate waived"), outcome.detail
    assert backend.calls == 2

    gate_events = [e for e in events if e["kind"] == "repro_gate"]
    assert [e["verdict"] for e in gate_events] == ["waived", "waived"], gate_events

    round_events = [e for e in events if e["kind"] == "repro_corrective_round"]
    assert len(round_events) == 1, round_events

    assert not [e for e in events if e["kind"] == "repro_manifest_restored"]
    assert not [e for e in events if e["kind"] == "repro_manifest_regenerate_nudge"]
