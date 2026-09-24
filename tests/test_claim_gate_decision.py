"""`claim_gate_decision` is the one place delivery's zero-diff claim decision
lives. `_run_attempt`'s commit section calls it and dispatches on
`gate.stage`; it must hold none of the predicates behind that decision
itself — else the dispatch and the decision can drift apart exactly the way
they did before this function existed."""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest

import no_human.core.orchestrator as orchestrator_module
from no_human.config import load_config
from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task
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


def _config(tmp_path):
    cfg = load_config(tmp_path / "config.yaml")
    cfg.data.setdefault("planning", {})["enabled"] = False
    cfg.data.setdefault("blockers", {})["challenge"] = False
    return cfg


class _NullBackend:
    """`claim_gate_decision` never touches the backend — these tests call it
    directly, so any placeholder satisfies the constructor."""


def _orch(store, tmp_path):
    return Orchestrator(store, _config(tmp_path).data, _NullBackend(),
                        SlackNotifier(None))


def test_the_decision_sequence_exists_once():
    """The predicates behind delivery's claim decision — an uncommitted
    tree, a report-kind completion, a resumed/unjudged head, and the subject
    classification a parsed claim is judged against — must live only in
    `claim_gate_decision`. `_run_attempt`'s dispatch on `gate.stage` (the
    `if/elif/else` chain immediately reading it) is found structurally, by
    its `gate.stage == ...` test, not by a hardcoded line range, so it stays
    correct as the file grows around it."""
    src = Path(orchestrator_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)

    def find(name):
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
                return node
        raise AssertionError(f"{name} not found")

    run_attempt = find("_run_attempt")
    dispatch = None
    for node in ast.walk(run_attempt):
        if isinstance(node, ast.If):
            test = node.test
            if (isinstance(test, ast.Compare) and isinstance(test.left, ast.Attribute)
                    and test.left.attr == "stage"):
                dispatch = node
                break
    assert dispatch is not None, (
        "_run_attempt no longer dispatches on gate.stage — did the "
        "commit section change shape?")
    dispatch_src = ast.get_source_segment(src, dispatch)

    decision_src = ast.get_source_segment(src, find("claim_gate_decision"))

    predicates = [
        "has_changes", "_REPORT_KINDS", "commits_ahead(",
        "_route_unjudged_head(", "_parse_already_satisfied(",
        "_already_satisfied_subject(",
    ]
    for needle in predicates:
        assert needle in decision_src, (
            f"{needle!r} missing from claim_gate_decision — the decision "
            "moved without this predicate")
        assert needle not in dispatch_src, (
            f"{needle!r} is duplicated in _run_attempt's gate.stage "
            "dispatch — that is a second copy of the decision, the exact "
            "shape this function exists to prevent")


def test_run_attempt_calls_the_shared_function():
    """`_run_attempt` must ask `claim_gate_decision` itself, not recompute
    an answer inline — found via AST, not a text search, so a rename would
    fail this test loudly instead of leaving it vacuously green."""
    src = Path(orchestrator_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "_run_attempt":
            run_attempt = node
            break
    else:
        raise AssertionError("_run_attempt not found")

    calls_it = False
    for node in ast.walk(run_attempt):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "claim_gate_decision"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "self"):
            calls_it = True
            break
    assert calls_it, "_run_attempt no longer calls self.claim_gate_decision"


async def test_each_stage_on_a_real_repo(bare_repo, tmp_path, store):
    """One real git tree driven through every `ClaimGate.stage` value —
    the shared function's own contract, not the guard's use of it."""
    orch = _orch(store, tmp_path)
    repo = GitRepo(bare_repo)

    # uncommitted: a dirty tree ends the decision before any claim is
    # even possible.
    (bare_repo / "calc.py").write_text("def add(a, b):\n    return a + b + 0\n")
    task = Task.new("t", repo_path=str(bare_repo), kind="feature")
    gate = await orch.claim_gate_decision(
        task, repo, base="main", branch="main",
        branched_from_own_partial=False, final_text="ALREADY-SATISFIED",
        announce=False)
    assert gate.stage == "uncommitted"
    _git(bare_repo, "checkout", "--", "calc.py")

    # report: a report-kind task with non-empty final text never reaches
    # a landed-work claim at all.
    report_task = Task.new("t", repo_path=str(bare_repo), kind="investigation")
    gate = await orch.claim_gate_decision(
        report_task, repo, base="main", branch="main",
        branched_from_own_partial=False, final_text="Findings: done.",
        announce=False)
    assert gate.stage == "report"

    # resumed: a branch carrying commits ahead of base that no completed
    # review judged routes to a full review, not the claim gate.
    _git(bare_repo, "checkout", "-b", "attempt-branch")
    (bare_repo / "more.py").write_text("x = 1\n")
    _git(bare_repo, "add", "-A")
    _git(bare_repo, "commit", "-m", "more work")
    gate = await orch.claim_gate_decision(
        task, repo, base="main", branch="attempt-branch",
        branched_from_own_partial=False, final_text="",
        announce=False)
    assert gate.stage == "resumed"
    assert gate.resumed_commit is not None
    _git(bare_repo, "checkout", "main")
    _git(bare_repo, "branch", "-D", "attempt-branch")

    # no_claim: a clean, non-report, non-resumed tree whose final text does
    # not parse as an ALREADY-SATISFIED claim.
    gate = await orch.claim_gate_decision(
        task, repo, base="main", branch="main",
        branched_from_own_partial=False,
        final_text="I looked around but made no changes.",
        announce=False)
    assert gate.stage == "no_claim"

    # claim: a parsed claim is classified against the subject tree
    # delivery itself ships on.
    claim_task = Task.new("t", repo_path=str(bare_repo), kind="feature")
    claim_task.acceptance_criteria = ["existing"]
    claim_text = "ALREADY-SATISFIED\nCRITERION: existing — MET — evidence: calc.py:1\n"
    gate = await orch.claim_gate_decision(
        claim_task, repo, base="main", branch="main",
        branched_from_own_partial=False, final_text=claim_text,
        announce=False)
    assert gate.stage == "claim"
    assert gate.claim is not None
    assert gate.subject is not None
