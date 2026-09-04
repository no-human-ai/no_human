"""An already-satisfied claim refused because the reviewed commit is this
task's OWN unfinished ``[WIP-PARTIAL]``/``[WIP-BLOCKED]`` checkpoint fails the
attempt exactly like any other already-satisfied refusal — no extra
correction turn is spent on it.

A bounded same-session correction turn was tried for this (task bf645f3a) and
then WITHDRAWN on independent review: coder sessions are never resumed across
attempts (``resume=`` only exists inside a single attempt's own coder-session
lifetime), so a reply on THIS attempt's session can never reach attempt N+1 —
the mistaken-claim incident is cross-attempt, so a same-session turn cannot
fix it. The turn was also a crash path: an abort mid-turn re-raised out of an
unhandled call site and could escape ``run_task`` entirely. The actual fix is
the resume-prompt digest sentence (see ``tests/test_prompt_blocks.py``),
which is read on the NEXT attempt, before any already-satisfied claim can be
made again.

This file proves the refusal guard itself (``_already_satisfied_subject``) is
unchanged and that no correction turn fires — on a WIP checkpoint or
otherwise — regressing neither the message nor the (absence of) an extra
backend call.

Fixtures are copied from ``tests/test_already_satisfied_subject_tree.py``
(``bare_repo``/``store``/``_config``) and the ``RESUMABLE`` capability double
from ``tests/test_e2e_orchestrator.py``.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace as _SimpleNamespace

import pytest

from no_human.agent.backend import AgentResult
from no_human.config import load_config
from no_human.core.db import Store
from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task, TaskStatus
from no_human.notify.slack import SlackNotifier
from no_human.vcs import GitRepo


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True,
                          capture_output=True, text=True)


@pytest.fixture
def bare_repo(tmp_path):
    bare = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", "-b", "main", str(bare))
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.email", "u@example.test")
    _git(work, "config", "user.name", "u")
    (work / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "initial")
    _git(work, "remote", "add", "origin", str(bare))
    _git(work, "push", "-u", "origin", "main")
    return work


@pytest.fixture
async def store(tmp_path):
    result = await Store(tmp_path / "nh.db").connect()
    yield result
    await result.close()


def _config(tmp_path):
    cfg = load_config(tmp_path / "config.yaml")
    cfg.data.setdefault("planning", {})["enabled"] = False
    cfg.data.setdefault("blockers", {})["challenge"] = False
    return cfg


#: Same capability double as `tests/test_e2e_orchestrator.py` — a backend
#: that declares it can continue a session, exactly as a real one does.
RESUMABLE = _SimpleNamespace(name="fake", session_resume=True)
NOT_RESUMABLE = _SimpleNamespace(name="fake-no-resume", session_resume=False)

CLAIM = "ALREADY-SATISFIED\nCRITERION: existing — MET — evidence: calc.py:1\n"


class RecordingBackend:
    """Records every ``run()`` call it receives. Nothing in
    `_gate_already_satisfied`'s already-satisfied-refusal path calls the
    backend at all — the coder turn itself is never invoked here either (the
    claim/`result` are handed in directly, exactly as `_gate` does in
    `test_already_satisfied_subject_tree.py`) — so every test below asserts
    zero calls."""

    def __init__(self, capabilities=RESUMABLE):
        self.capabilities = capabilities
        self.calls: list[dict] = []

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        self.calls.append({
            "prompt": prompt, "max_turns": max_turns, "effort": effort,
            "resume": resume,
        })
        return AgentResult(final_text="Understood.", num_turns=1,
                           is_error=False, tokens_used=7, session_id="s2",
                           stop_reason="end_turn")


def _claim_result(session_id="s"):
    """The coder's own zero-diff report — what `_gate_already_satisfied`'s
    `result=` carries in production (the AgentResult of the attempt that
    produced `claim`)."""
    return AgentResult(final_text=CLAIM, num_turns=3, is_error=False,
                       tokens_used=50, session_id=session_id,
                       stop_reason="end_turn")


async def _gate(store, tmp_path, repo_path, backend, *, branch, attempt_id=None,
                result="use-default"):
    orch = Orchestrator(store, _config(tmp_path).data, backend,
                        SlackNotifier(None))
    task = Task.new("existing", repo_path=str(repo_path), kind="feature")
    task.acceptance_criteria = ["existing"]
    await store.create_task(task)
    if attempt_id is None:
        attempt_id = await store.create_attempt(task.id, 1)
    kwargs = {} if result == "use-default" else {"result": result}
    outcome = await orch._gate_already_satisfied(
        task, GitRepo(repo_path), attempt_id, CLAIM, branch=branch,
        attempt_n=1, base="main", **kwargs)
    return orch, outcome, task, attempt_id


def _commit_wip_checkpoint(bare_repo, *, subject="[WIP-PARTIAL] add mul()"):
    branch = "no-human/wip"
    _git(bare_repo, "checkout", "-b", branch)
    (bare_repo / "wip.txt").write_text("not shipped\n")
    _git(bare_repo, "add", "-A")
    _git(bare_repo, "commit", "-m", subject)
    return branch


async def test_an_already_satisfied_claim_on_a_wip_checkpoint_is_refused_with_no_correction_turn(
    bare_repo, tmp_path, store
):
    branch = _commit_wip_checkpoint(bare_repo)
    repo = GitRepo(bare_repo)
    head_before = repo.head_sha()
    backend = RecordingBackend()

    orch, outcome, task, attempt_id = await _gate(
        store, tmp_path, bare_repo, backend, branch=branch,
        result=_claim_result())

    assert outcome.status is TaskStatus.FAILED
    assert outcome.detail.startswith("already-satisfied claim refused: ")
    assert "unfinished checkpoint subject" in outcome.detail

    # No correction turn — the backend is never called for this refusal.
    assert backend.calls == []

    # The worktree and HEAD are untouched by the gate.
    assert repo.head_sha() == head_before
    assert not repo.has_changes()


async def test_a_second_gate_call_in_the_same_attempt_still_gets_no_correction(
    bare_repo, tmp_path, store
):
    branch = _commit_wip_checkpoint(bare_repo)
    backend = RecordingBackend()

    orch, outcome, task, attempt_id = await _gate(
        store, tmp_path, bare_repo, backend, branch=branch,
        result=_claim_result())
    assert backend.calls == []

    # Re-enter the gate with the SAME attempt_id — still no backend call.
    repo = GitRepo(bare_repo)
    second_outcome = await orch._gate_already_satisfied(
        task, repo, attempt_id, CLAIM, branch=branch, attempt_n=1,
        base="main", result=_claim_result())

    assert second_outcome.status is TaskStatus.FAILED
    assert backend.calls == []


async def test_a_non_wip_refusal_gets_no_correction_turn(bare_repo, tmp_path, store):
    """A refusal for a cause OTHER than the checkpoint subject — here, no
    delivery branch offered (`branch=None`) on an ordinary (non-WIP) unpushed
    commit — is refused the same way, with no backend call either."""
    branch_local = "no-human/ordinary"
    _git(bare_repo, "checkout", "-b", branch_local)
    (bare_repo / "extra.txt").write_text("ordinary work\n")
    _git(bare_repo, "add", "-A")
    _git(bare_repo, "commit", "-m", "ordinary unpushed work")
    backend = RecordingBackend()

    orch, outcome, task, attempt_id = await _gate(
        store, tmp_path, bare_repo, backend, branch=None,
        result=_claim_result())

    assert outcome.status is TaskStatus.FAILED
    assert outcome.detail.startswith("already-satisfied claim refused: ")
    assert "no delivery branch was offered" in outcome.detail
    assert "unfinished checkpoint subject" not in outcome.detail
    assert backend.calls == []


async def test_a_wip_refusal_with_a_non_resumable_backend_is_refused_identically(
    bare_repo, tmp_path, store
):
    """The refusal (and the absence of any correction turn) does not depend on
    backend session-resume capability at all — same outcome either way."""
    branch = _commit_wip_checkpoint(bare_repo)
    backend = RecordingBackend(capabilities=NOT_RESUMABLE)

    orch, outcome, task, attempt_id = await _gate(
        store, tmp_path, bare_repo, backend, branch=branch,
        result=_claim_result())

    assert outcome.status is TaskStatus.FAILED
    assert "unfinished checkpoint subject" in outcome.detail
    assert backend.calls == []
