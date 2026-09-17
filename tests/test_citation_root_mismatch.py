"""The citation root must describe the reviewed range, or a demotion is
not evidence.

`_citation_fails` opens files under `repo_path` on disk and trusts that tree
to describe the AFTER side of whatever range is being reviewed. When a
caller supplies `diff_override` (a diff computed elsewhere) that trust is
unverified: a dirty or wrong-commit worktree answers citation questions
about a DIFFERENT tree than the one the diff describes, and a citation that
"fails" for that reason says nothing about the finding — it was silently
demoted anyway, turning a real blocking finding into an advisory one and a
FAIL into a PASS.

`_citation_root_mismatch` detects that condition with cheap, read-only git
plumbing; `_verify_citations` keeps a failed-citation finding BLOCKING
(instead of demoting it) when the root cannot be trusted;
`ReviewDecision.passed_due_to_demotion` lets a caller tell "nothing was
wrong" apart from "nothing survived verification". This file proves all
three, and that the original hallucination-guard behaviour on a clean,
matching tree is untouched.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from no_human.agent.claude_backend import AgentResult
from no_human.core.task import Task
from no_human.review.reviewer import (
    AdversarialReviewer,
    _citation_root_mismatch,
    _parse_review_output,
)

# --------------------------------------------------------------------------- #
# Fixture and JSON builder — duplicated from tests/test_citation_rule.py on
# purpose (that file's fixture is not touched or imported from).
# --------------------------------------------------------------------------- #


@pytest.fixture
def repo(tmp_path):
    def git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)
    git("init", "-b", "main")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    git("add", "-A")
    git("commit", "-m", "base")
    (tmp_path / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a * b\n"
    )
    (tmp_path / "gone.py").write_text("x = 1\n")
    git("add", "-A")
    git("commit", "-m", "before")
    subprocess.run(["git", "rm", "-q", "gone.py"], cwd=tmp_path, check=True,
                   capture_output=True)
    git("commit", "-m", "after: delete gone.py")
    return tmp_path


def _review(items, passed=False):
    return ("REVIEW_JSON_START\n"
            + json.dumps({"passed": passed, "items": items})
            + "\nREVIEW_JSON_END")


def _agent_result(text: str) -> AgentResult:
    return AgentResult(
        final_text=text, num_turns=1, is_error=False, tokens_used=100,
        session_id="fake", stop_reason="end_turn",
        cache_read_tokens=0, cache_creation_tokens=0, output_tokens=None,
    )


class ScriptedBackend:
    """One scripted `AgentResult` per `run()` call, in order (copied from
    tests/test_review_refute_pass.py's ScriptedBackend; not imported so this
    file stays self-contained and does not touch that one)."""

    model = "claude-opus-5"

    def __init__(self, script: list):
        self._script = list(script)
        self.calls: list[dict] = []

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                   on_event=None, supervisor_hook=None):
        idx = len(self.calls)
        self.calls.append({"prompt": prompt, "max_turns": max_turns})
        return self._script[idx]


# --------------------------------------------------------------------------- #
# AC1 — a diverged worktree must not be able to demote a blocking finding
# --------------------------------------------------------------------------- #


def test_a_beyond_eof_citation_is_not_demoted_when_the_worktree_diverges(repo):
    # Uncommitted edit: the worktree now disagrees with the reviewed range.
    (repo / "calc.py").write_text("x = 1\n")

    root_mismatch = _citation_root_mismatch(repo)
    assert root_mismatch, "a dirty worktree must be detected as a mismatch"

    d = _parse_review_output(_review([
        {"label": "off-by-one in the new loop", "passed": False,
         "severity": "high", "evidence": "x", "file": "calc.py", "line": 400},
    ]), repo_path=repo, before_ref="HEAD~1", root_mismatch=root_mismatch)

    assert d.passed is False, (
        "a citation that fails only because the tree diverged must not "
        "demote the last blocking finding and flip the gate to PASS")
    item = d.checklist[0]
    assert item.severity == "high", "severity must be untouched, not demoted"
    assert d.demoted_citations == []
    assert "does not match the reviewed change" in item.evidence
    assert d.citation_root_mismatch == root_mismatch


# --------------------------------------------------------------------------- #
# AC2 — the original rule, on a clean matching tree, is untouched
# --------------------------------------------------------------------------- #


def test_a_hallucinated_citation_in_a_matching_tree_is_still_demoted_and_passes(repo):
    assert _citation_root_mismatch(repo) == "", "the fixture's tree is clean"

    d = _parse_review_output(_review([
        {"label": "off-by-one in the new loop", "passed": False,
         "severity": "high", "evidence": "x", "file": "calc.py", "line": 400},
    ]), repo_path=repo, before_ref="HEAD~1", root_mismatch="")

    assert d.passed is True
    item = d.checklist[0]
    assert item.severity == "low"
    assert d.demoted_citations
    assert "demoted to advisory" in item.evidence


# --------------------------------------------------------------------------- #
# `_citation_root_mismatch` unit coverage
# --------------------------------------------------------------------------- #


def test_a_dirty_worktree_is_detected_and_a_clean_one_is_not(repo):
    assert _citation_root_mismatch(repo) == ""

    (repo / "calc.py").write_text("dirty\n")
    assert _citation_root_mismatch(repo) != ""

    subprocess.run(["git", "checkout", "--", "calc.py"], cwd=repo, check=True,
                    capture_output=True)
    assert _citation_root_mismatch(repo) == ""

    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                           check=True, capture_output=True, text=True).stdout.strip()
    assert _citation_root_mismatch(repo, reviewed_sha=head) == ""
    assert _citation_root_mismatch(repo, reviewed_sha="0" * 40) != ""

    non_git = repo.parent / "not-a-repo"
    non_git.mkdir()
    assert _citation_root_mismatch(non_git) == "", (
        "our own inability to run git is our error, not the tree's — never "
        "a mismatch"
    )


# --------------------------------------------------------------------------- #
# AC4 — "passed with nothing wrong" vs "passed only after demotion"
# --------------------------------------------------------------------------- #


def test_a_pass_after_demotion_is_distinguishable_from_a_clean_pass(repo):
    assert _citation_root_mismatch(repo) == ""

    # Case A: the ONLY blocking finding is a hallucinated citation.
    d_a = _parse_review_output(_review([
        {"label": "ghost", "passed": False, "severity": "critical",
         "evidence": "x", "file": "does_not_exist.py", "line": 10},
    ]), repo_path=repo, before_ref="HEAD~1", root_mismatch="")
    assert d_a.passed is True
    assert d_a.passed_due_to_demotion is True

    # Case B: a clean pass — no failing items at all.
    d_b = _parse_review_output(_review([
        {"label": "ok", "passed": True, "severity": "", "evidence": "",
         "file": "", "line": 0},
    ], passed=True), repo_path=repo, before_ref="HEAD~1", root_mismatch="")
    assert d_b.passed is True
    assert d_b.passed_due_to_demotion is False

    # Case C: one demoted finding, but a second blocking finding with a REAL
    # citation survives — the gate must still fail, and must not claim the
    # pass-via-demotion shape it never reached.
    d_c = _parse_review_output(_review([
        {"label": "ghost", "passed": False, "severity": "critical",
         "evidence": "x", "file": "does_not_exist.py", "line": 10},
        {"label": "real bug", "passed": False, "severity": "high",
         "evidence": "x", "file": "calc.py", "line": 2},
    ]), repo_path=repo, before_ref="HEAD~1", root_mismatch="")
    assert d_c.passed is False
    assert d_c.passed_due_to_demotion is False


# --------------------------------------------------------------------------- #
# AC1, end-to-end — `review()` computes the mismatch itself for diff_override
# --------------------------------------------------------------------------- #


async def test_the_review_entrypoint_computes_the_mismatch_for_diff_override(repo):
    # Dirty worktree: review() must detect this itself, with no help from the
    # caller, before it ever trusts a demotion.
    (repo / "calc.py").write_text("x = 1\n")

    # Critical severity keeps this out of the refute pass's candidate set and
    # a FAIL never opens the (pass-only) angle-pass block, so a single
    # scripted backend response is the whole session.
    verdict_text = _review([
        {"label": "off-by-one in the new loop", "passed": False,
         "severity": "critical", "evidence": "x", "file": "calc.py", "line": 400},
    ])
    backend = ScriptedBackend([_agent_result(verdict_text)])
    reviewer = AdversarialReviewer(backend=backend)
    task = Task.new("fix the loop")

    decision = await reviewer.review(
        task, repo_path=repo, diff_override="--- a/calc.py\n+++ b/calc.py\n",
        before_ref="HEAD~1",
    )

    assert decision.passed is False, (
        "a diverged worktree must not be able to launder a blocking finding "
        "into a silent PASS through the citation rule")
    assert decision.citation_root_mismatch != ""
    assert decision.demoted_citations == []
    assert decision.checklist[0].severity == "critical"
