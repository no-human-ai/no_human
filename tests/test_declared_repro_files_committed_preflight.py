"""An attempt can END with `REPRO_MANIFEST`-declared test file(s) absent from
the attempt's own COMMITTED tree — the gate's disk-based missing-file check
does not catch it if the file happens to sit in the working tree, so a later
full round (a fresh checkout of the branch) fails on "declared test file(s)
missing from the attempt tree" long after the attempt believed itself done.

FIX: `Orchestrator._declared_files_preflight` runs before the repro gate's
first read, for `enforced` attempts only. Zero LLM spend for the check
itself (`declared_test_files` reads the manifest off disk, `_paths_at_head`
is one `git ls-tree` against HEAD). If the manifest declares file(s) missing
from HEAD, it buys ONE bounded round (`_repro_corrective_round`, reused) to
commit exactly those files — naming them explicitly and forbidding removing
the declaration as a way out. If still missing after that one round, this
method does NOT end the attempt itself: it falls straight through to
`run_repro_gate`'s own pre-existing missing-file check, which stays the
backstop, unchanged.

Same house pattern as `tests/test_repro_waived_corrective_round.py` (not
modified by this file): a real bare-repo checkout + a scripted backend,
driving `orch._run_attempt` directly so the whole pipeline — gate, corrective
round, commit, tamper check — runs for real except for the LLM call itself.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from no_human.agent.claude_backend import AgentEvent, AgentResult
from no_human.config import load_config
from no_human.core.infra_breaker import infra_breaker
from no_human.core.orchestrator import (
    Orchestrator, declared_files_send_back_message,
)
from no_human.core.task import Task, TaskStatus
from no_human.notify.slack import SlackNotifier
from no_human.testing.repro_gate import MANIFEST as REPRO_MANIFEST
from no_human.testing.repro_gate import declared_test_files, run_repro_gate
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


def _mul_fix(cwd: Path) -> None:
    """A real fix (`mul`) — no accompanying test, no manifest."""
    cwd.joinpath("calc.py").write_text(
        "def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a * b\n"
    )


def _write_manifest(cwd: Path, tests: list[str]) -> None:
    cwd.joinpath(".no_human").mkdir(exist_ok=True)
    (cwd / REPRO_MANIFEST).write_text(json.dumps({"tests": tests}))


async def _run_one_bugfix_attempt(store, bare_repo, tmp_path, backend, *, kind="bugfix"):
    """Walk a fresh task to PLANNING and hand back everything a test needs to
    drive `_run_attempt` directly, mirroring
    `test_repro_waived_corrective_round.py`'s `_run_one_bugfix_attempt`."""
    cfg = _config(tmp_path)
    events = []
    orch = Orchestrator(store, cfg.data, backend, SlackNotifier(None),
                        event_sink=events.append)
    task = Task.new("fix mul()", repo_path=str(bare_repo), kind=kind)
    task.acceptance_criteria = ["mul(a,b) returns a*b"]
    await store.create_task(task)
    await store.set_status(task, TaskStatus.CONTEXT)
    await store.set_status(task, TaskStatus.PLANNING)
    repo = GitRepo(bare_repo)
    return orch, task, repo, events


# --------------------------------------------------------------------------- #
# AC1 (RED-first) — a declared file absent from the commit buys ONE round,    #
# then the attempt ends exactly as if the gate had simply passed.             #
# --------------------------------------------------------------------------- #


class _DeclaredMissingThenCommittedBackend:
    """Coder turn: real fix, commits it, and declares `test_mul.py::test_mul`
    in the manifest — but never creates `test_mul.py`. Corrective round:
    creates and commits `test_mul.py`."""

    def __init__(self):
        self.calls = 0

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        self.calls += 1
        cwd = Path(cwd)
        if self.calls == 1:
            if on_event is not None:
                on_event(AgentEvent("tool_use", tool_name="Edit",
                                    tool_input={"file_path": "calc.py"}))
            _mul_fix(cwd)
            _write_manifest(cwd, ["test_mul.py::test_mul"])
            return AgentResult(final_text="done", num_turns=2, is_error=False,
                               tokens_used=100, session_id="s", stop_reason="end_turn")
        if on_event is not None:
            on_event(AgentEvent("tool_use", tool_name="Write",
                                tool_input={"file_path": "test_mul.py"}))
        cwd.joinpath("test_mul.py").write_text(
            "from calc import mul\n\ndef test_mul():\n    assert mul(2, 3) == 6\n"
        )
        return AgentResult(final_text="wrote test_mul.py", num_turns=1, is_error=False,
                           tokens_used=10, session_id="s2", stop_reason="end_turn")


async def test_declared_file_absent_from_the_commit_buys_one_round_then_ends_clean(
        bare_repo, tmp_path, store):
    """RED before the fix: today nothing runs before the gate's own read, so
    this attempt would fail outright on "declared test file(s) missing from
    the attempt tree" instead of getting a corrective round."""
    backend = _DeclaredMissingThenCommittedBackend()
    orch, task, repo, events = await _run_one_bugfix_attempt(
        store, bare_repo, tmp_path, backend)

    outcome = await orch._run_attempt(task, repo, 1, "main")

    assert outcome.status is TaskStatus.AWAITING_APPROVAL, outcome.detail
    assert backend.calls == 2

    missing_events = [e for e in events if e["kind"] == "declared_files_uncommitted"]
    assert len(missing_events) == 1, missing_events
    assert missing_events[0]["missing"] == ["test_mul.py"], missing_events

    # The gate never saw a missing file — it ran exactly once, and passed.
    gate_events = [e for e in events if e["kind"] == "repro_gate"]
    assert [e["verdict"] for e in gate_events] == ["pass"], gate_events

    # Same attempt throughout — not a restart on a fresh branch off main.
    attempts = await store.list_attempts(task.id)
    assert len(attempts) == 1, attempts

    # The committed tree really does contain the declared file now.
    committed = subprocess.run(
        ["git", "ls-tree", "--name-only", "HEAD"], cwd=repo.path,
        check=True, capture_output=True, text=True,
    ).stdout.split()
    assert "test_mul.py" in committed


class _DeclaredMissingNeverFixedBackend:
    """Coder turn: real fix, commits it, declares `test_mul.py::test_mul` —
    never creates it. Corrective round writes nothing at all."""

    def __init__(self):
        self.calls = 0

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        self.calls += 1
        cwd = Path(cwd)
        if self.calls == 1:
            if on_event is not None:
                on_event(AgentEvent("tool_use", tool_name="Edit",
                                    tool_input={"file_path": "calc.py"}))
            _mul_fix(cwd)
            _write_manifest(cwd, ["test_mul.py::test_mul"])
            return AgentResult(final_text="done", num_turns=2, is_error=False,
                               tokens_used=100, session_id="s", stop_reason="end_turn")
        return AgentResult(final_text="nothing to add", num_turns=1, is_error=False,
                           tokens_used=5, session_id="s2", stop_reason="end_turn")


async def test_still_missing_after_the_round_falls_through_to_the_gate_naming_the_files(
        bare_repo, tmp_path, store):
    backend = _DeclaredMissingNeverFixedBackend()
    orch, task, repo, events = await _run_one_bugfix_attempt(
        store, bare_repo, tmp_path, backend)

    outcome = await orch._run_attempt(task, repo, 1, "main")

    assert outcome.status is TaskStatus.FAILED
    assert "missing from the attempt tree" in (outcome.detail or ""), outcome.detail
    assert "test_mul.py" in (outcome.detail or ""), outcome.detail

    # No third barrier: exactly the coder turn + the one bounded round.
    assert backend.calls == 2

    missing_events = [e for e in events if e["kind"] == "declared_files_uncommitted"]
    assert len(missing_events) == 2, missing_events
    assert missing_events[0]["missing"] == ["test_mul.py"]
    assert missing_events[1].get("still_missing") is True
    assert missing_events[1]["missing"] == ["test_mul.py"]

    # The pre-flight itself never ends the attempt — the gate's own
    # missing-file check does, exactly as it always has.
    gate_events = [e for e in events if e["kind"] == "repro_gate"]
    assert [e["verdict"] for e in gate_events] == ["fail"], gate_events

    attempts = await store.list_attempts(task.id)
    assert len(attempts) == 1
    assert attempts[0]["status"] == "failed"


# --------------------------------------------------------------------------- #
# AC2 — every declared file already committed: byte-identical to today,      #
# nothing emitted, nothing spent on a round.                                  #
# --------------------------------------------------------------------------- #


class _AllDeclaredFilesCommittedBackend:
    """One turn: fix, test, and manifest — all committed together."""

    def __init__(self):
        self.calls = 0

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        self.calls += 1
        cwd = Path(cwd)
        if on_event is not None:
            on_event(AgentEvent("tool_use", tool_name="Edit",
                                tool_input={"file_path": "calc.py"}))
            on_event(AgentEvent("tool_use", tool_name="Write",
                                tool_input={"file_path": "test_calc.py"}))
        cwd.joinpath("calc.py").write_text(
            "def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a * b\n"
        )
        cwd.joinpath("test_calc.py").write_text(
            "from calc import add, mul\n\n"
            "def test_add():\n    assert add(1, 2) == 3\n\n"
            "def test_mul():\n    assert mul(2, 3) == 6\n"
        )
        _write_manifest(cwd, ["test_calc.py::test_mul"])
        return AgentResult(final_text="done", num_turns=3, is_error=False,
                           tokens_used=100, session_id="s", stop_reason="end_turn")


async def test_all_declared_files_committed_is_unchanged_and_spends_nothing(
        bare_repo, tmp_path, store):
    backend = _AllDeclaredFilesCommittedBackend()
    orch, task, repo, events = await _run_one_bugfix_attempt(
        store, bare_repo, tmp_path, backend)

    outcome = await orch._run_attempt(task, repo, 1, "main")

    assert outcome.status is TaskStatus.AWAITING_APPROVAL, outcome.detail
    assert backend.calls == 1

    assert not [e for e in events if e["kind"] == "declared_files_uncommitted"]
    assert not [e for e in events if e["kind"] == "repro_corrective_round"]

    gate_events = [e for e in events if e["kind"] == "repro_gate"]
    assert [e["verdict"] for e in gate_events] == ["pass"], gate_events


async def test_advisory_task_with_a_missing_declared_file_spends_no_round(
        bare_repo, tmp_path, store):
    """Non-bugfix tasks are not `enforced`, so the pre-flight is never
    entered — matching `_repro_gate_step`'s own `if enforced:` gate."""
    backend = _DeclaredMissingNeverFixedBackend()
    orch, task, repo, events = await _run_one_bugfix_attempt(
        store, bare_repo, tmp_path, backend, kind="feature")

    await orch._run_attempt(task, repo, 1, "main")

    assert backend.calls == 1
    assert not [e for e in events if e["kind"] == "declared_files_uncommitted"]
    assert not [e for e in events if e["kind"] == "repro_corrective_round"]


# --------------------------------------------------------------------------- #
# AC3 — the gate's own missing-file check is untouched (existing backstop).   #
# --------------------------------------------------------------------------- #


def test_repro_gate_missing_file_check_is_untouched(tmp_path):
    def git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)
    git("init", "-b", "main")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a - b  # bug\n")
    git("add", "-A")
    git("commit", "-m", "base (buggy)")
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    git("add", "-A")
    git("commit", "-m", "fix, but no repro test committed")
    (tmp_path / ".no_human").mkdir()
    (tmp_path / REPRO_MANIFEST).write_text(
        json.dumps({"tests": ["test_repro.py::test_add_fixed"]})
    )
    r = run_repro_gate(tmp_path, "HEAD")
    assert r.verdict == "fail"
    assert "declared test file(s) missing from the attempt tree" in r.reasons[0]
    assert "test_repro.py" in r.reasons[0]


# --------------------------------------------------------------------------- #
# Message / seam pinning — pure, no git, no backend.                          #
# --------------------------------------------------------------------------- #


def test_send_back_message_names_every_missing_file_and_forbids_removal():
    msg = declared_files_send_back_message(["test_mul.py", "tests/test_extra.py"])
    assert "test_mul.py" in msg
    assert "tests/test_extra.py" in msg
    assert REPRO_MANIFEST in msg
    assert "tamper" in msg.lower() or "not an option" in msg.lower()
    assert "commit" in msg.lower()


def test_declared_test_files_strips_node_ids_and_dedupes(tmp_path):
    (tmp_path / ".no_human").mkdir()
    (tmp_path / REPRO_MANIFEST).write_text(json.dumps({
        "tests": [
            "tests/test_a.py::test_one",
            "tests/test_a.py::test_two",
            "test_b.py::test_three",
        ],
    }))
    assert declared_test_files(tmp_path) == ["tests/test_a.py", "test_b.py"]


def test_declared_test_files_with_no_manifest_is_empty(tmp_path):
    assert declared_test_files(tmp_path) == []
