"""The citation rule: a blocking finding must point at code that exists.

Severity grading closed the biggest reviewer-FP channel (manufactured
"minor issues" items); the one that survives is the hallucinated location —
a critical-graded finding citing a file or line that isn't there
(citation-grounding, arXiv 2411.03079). Those demote to advisory before the
verdict, so they never fail the gate, and the demotion is loud.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from no_human.review.reviewer import _parse_review_output


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


def _review(items):
    import json
    return ("REVIEW_JSON_START\n"
            + json.dumps({"passed": False, "items": items})
            + "\nREVIEW_JSON_END")


def test_a_blocking_finding_citing_a_missing_file_demotes_and_the_gate_passes(repo):
    d = _parse_review_output(_review([
        {"label": "ghost", "passed": False, "severity": "critical",
         "evidence": "x", "file": "src/does_not_exist.py", "line": 10},
    ]), repo_path=repo, before_ref="HEAD~1")
    assert d.passed, "a hallucinated citation must not fail the gate"
    assert d.demoted_citations and "does_not_exist" in d.demoted_citations[0]
    item = d.checklist[0]
    assert item.severity == "low"
    assert "citation rule" in item.evidence


def test_a_verified_citation_stays_blocking(repo):
    d = _parse_review_output(_review([
        {"label": "real", "passed": False, "severity": "high",
         "evidence": "x", "file": "calc.py", "line": 2},
    ]), repo_path=repo, before_ref="HEAD~1")
    assert not d.passed
    assert not d.demoted_citations


def test_a_line_beyond_the_file_demotes(repo):
    d = _parse_review_output(_review([
        {"label": "off-end", "passed": False, "severity": "medium",
         "evidence": "x", "file": "calc.py", "line": 999},
    ]), repo_path=repo, before_ref="HEAD~1")
    assert d.passed
    assert "999" in d.demoted_citations[0]


def test_a_file_the_diff_deleted_verifies_against_the_before_ref(repo):
    """"You deleted gone.py, breaking X" is a real finding — the file is
    absent from the worktree by design and must not demote."""
    d = _parse_review_output(_review([
        {"label": "deletion", "passed": False, "severity": "high",
         "evidence": "x", "file": "gone.py", "line": 0},
    ]), repo_path=repo, before_ref="HEAD~1")
    assert not d.passed
    assert not d.demoted_citations


def test_a_blocking_finding_with_no_citation_is_untouched(repo):
    """Absence is not punished — only citations that name the nonexistent."""
    d = _parse_review_output(_review([
        {"label": "global", "passed": False, "severity": "critical",
         "evidence": "the whole approach is wrong", "file": "", "line": 0},
    ]), repo_path=repo, before_ref="HEAD~1")
    assert not d.passed
    assert not d.demoted_citations


def test_a_path_escaping_the_repo_demotes(repo):
    d = _parse_review_output(_review([
        {"label": "escape", "passed": False, "severity": "high",
         "evidence": "x", "file": "../../etc/passwd", "line": 1},
    ]), repo_path=repo, before_ref="HEAD~1")
    assert d.passed
    assert d.demoted_citations


def test_without_a_repo_path_parsing_is_unchanged(repo):
    d = _parse_review_output(_review([
        {"label": "ghost", "passed": False, "severity": "critical",
         "evidence": "x", "file": "nope.py", "line": 1},
    ]))
    assert not d.passed
    assert not d.demoted_citations


def test_passed_due_to_demotion_distinguishes_a_demoted_pass_from_a_clean_one(repo):
    """`passed_due_to_demotion` is True exactly on the demote-and-pass path
    this file exercises above, and False on an ordinary clean pass — see
    tests/test_citation_root_mismatch.py for the mismatch-path coverage."""
    demoted = _parse_review_output(_review([
        {"label": "ghost", "passed": False, "severity": "critical",
         "evidence": "x", "file": "src/does_not_exist.py", "line": 10},
    ]), repo_path=repo, before_ref="HEAD~1")
    assert demoted.passed and demoted.demoted_citations
    assert demoted.passed_due_to_demotion is True

    clean_text = ("REVIEW_JSON_START\n"
                  + json.dumps({"passed": True, "items": [
                      {"label": "ok", "passed": True, "severity": "",
                       "evidence": "", "file": "", "line": 0},
                  ]})
                  + "\nREVIEW_JSON_END")
    clean = _parse_review_output(clean_text, repo_path=repo, before_ref="HEAD~1")
    assert clean.passed
    assert clean.passed_due_to_demotion is False
