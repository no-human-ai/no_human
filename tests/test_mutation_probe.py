"""Mutation probe engine: does a new/changed test actually fail when the
behaviour it is named for is mutated?

Covers the acceptance criteria at the mechanism level (see
``.no_human/PLAN.md``):

* AC1 — a real test is killed and the mutation is recorded.
* AC2 — a test that stays green under mutation is reported "survived".
* AC3 — the reviewed tree is byte-identical afterwards, proven by a content
  hash, never a revert command.
* AC4 — exactly one executable-code node changes per mutation; a string or
  comment edit is never accepted, and declarations/imports are never removed.
* AC5 — every failure path reports "could not check", never a silent pass.
"""

from __future__ import annotations

import ast
import hashlib
import subprocess
from pathlib import Path

import pytest

from no_human.core import reviewer_worktree
from no_human.testing import mutation_probe

_TIMEOUT = 30.0

CALC_SOURCE = (
    "def add(a, b):\n"
    "    if a is None:\n"
    "        return b\n"
    "    return a + b\n"
)

GENUINE_TEST = (
    "from calc import add\n\n\n"
    "def test_add_two_positive_numbers():\n"
    "    assert add(1, 2) == 3\n"
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _rev(repo: Path, ref: str = "HEAD") -> str:
    return subprocess.run(
        ["git", "rev-parse", ref], cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.strip()


def _init_repo(tmp_path: Path) -> str:
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "README.md").write_text("base\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "base")
    return _rev(tmp_path)


def _repo_with_test(
    tmp_path: Path, test_source: str, *,
    calc_source: str = CALC_SOURCE, test_filename: str = "tests/test_calc.py",
) -> tuple[Path, str, str]:
    before_ref = _init_repo(tmp_path)
    (tmp_path / "calc.py").write_text(calc_source)
    (tmp_path / "tests").mkdir(exist_ok=True)
    (tmp_path / test_filename).write_text(test_source)
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "add test")
    after_ref = _rev(tmp_path)
    return tmp_path, before_ref, after_ref


@pytest.fixture
def repo(tmp_path):
    return _repo_with_test(tmp_path, GENUINE_TEST)


# --------------------------------------------------------------------------- #
# AC1 — a real test is killed, and the mutation is recorded                   #
# --------------------------------------------------------------------------- #


def test_a_real_test_is_killed_and_the_mutation_is_recorded(repo):
    repo_path, before_ref, after_ref = repo
    result = mutation_probe.run_mutation_probe(repo_path, before_ref, after_ref)

    assert result.tree_intact is True
    assert len(result.probes) == 1
    probe = result.probes[0]
    assert probe.verdict == "killed"
    assert probe.target == "calc.py:add"
    assert probe.mutation
    assert "line" in probe.mutation
    assert probe.reason


def test_only_tests_the_diff_adds_or_changes_are_probed(tmp_path):
    before_ref = _init_repo(tmp_path)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_pre.py").write_text(
        "def test_pre_existing():\n    assert 1 == 1\n"
    )
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "pre-existing test")
    ref1 = _rev(tmp_path)

    (tmp_path / "tests" / "test_new.py").write_text(
        "def test_newly_added():\n    assert 2 == 2\n"
    )
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "add a new test")
    ref2 = _rev(tmp_path)

    changed, pre = mutation_probe.changed_test_functions(tmp_path, ref1, ref2)
    assert pre == []
    assert [c["node_id"] for c in changed] == ["tests/test_new.py::test_newly_added"]

    # Now change the BODY of the pre-existing test in a third commit — it
    # must become "changed" too, exactly like a newly added test would.
    (tmp_path / "tests" / "test_pre.py").write_text(
        "def test_pre_existing():\n    assert 1 == 1\n    assert True\n"
    )
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "modify the pre-existing test body")
    ref3 = _rev(tmp_path)

    changed2, pre2 = mutation_probe.changed_test_functions(tmp_path, ref2, ref3)
    assert pre2 == []
    assert [c["node_id"] for c in changed2] == ["tests/test_pre.py::test_pre_existing"]


# --------------------------------------------------------------------------- #
# AC2 — a test that stays green under mutation is reported "survived"         #
# --------------------------------------------------------------------------- #


def test_a_test_that_stays_green_under_mutation_is_reported_as_survived(tmp_path):
    repo_path, before_ref, after_ref = _repo_with_test(
        tmp_path,
        "from calc import add\n\n\n"
        "def test_add_is_always_add():\n"
        "    add(1, 2)\n"
        "    assert add is add\n",
    )
    result = mutation_probe.run_mutation_probe(repo_path, before_ref, after_ref)

    assert len(result.probes) == 1
    probe = result.probes[0]
    assert probe.verdict == "survived"
    assert probe.target == "calc.py:add"
    assert result.verdict == "fail"


# --------------------------------------------------------------------------- #
# AC3 — the given tree is byte-identical afterwards, proven by hash           #
# --------------------------------------------------------------------------- #


def test_the_given_tree_is_byte_identical_afterwards(repo):
    repo_path, before_ref, after_ref = repo
    tracked = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", "HEAD"],
        cwd=repo_path, capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    before_hashes = {
        rel: hashlib.sha256((repo_path / rel).read_bytes()).hexdigest()
        for rel in tracked
    }
    before_snap = reviewer_worktree.snapshot(repo_path, timeout=_TIMEOUT)

    result = mutation_probe.run_mutation_probe(repo_path, before_ref, after_ref)

    delta = reviewer_worktree.compare(repo_path, before_snap, timeout=_TIMEOUT)
    assert delta.is_empty()
    assert result.tree_intact is True

    after_hashes = {
        rel: hashlib.sha256((repo_path / rel).read_bytes()).hexdigest()
        for rel in tracked
    }
    assert after_hashes == before_hashes

    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo_path, capture_output=True, text=True, check=True,
    ).stdout
    assert status == ""


def test_the_integrity_proof_is_a_hash_comparison_not_a_revert(repo, monkeypatch):
    repo_path, before_ref, after_ref = repo
    calls: list[list[str]] = []
    real_run = subprocess.run

    def spy(cmd, *args, **kwargs):
        calls.append(list(cmd))
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(mutation_probe.subprocess, "run", spy)
    result = mutation_probe.run_mutation_probe(repo_path, before_ref, after_ref)

    assert result.tree_intact is True
    assert calls  # the spy actually observed the module's own git calls
    banned = {"checkout", "reset", "stash", "clean"}
    for argv in calls:
        assert not (banned & set(argv)), argv


def test_a_dirty_tree_after_the_probe_forces_error(repo, monkeypatch):
    repo_path, before_ref, after_ref = repo
    dirty_delta = reviewer_worktree.Delta(added=[], modified=["calc.py"], deleted=[])
    monkeypatch.setattr(
        mutation_probe.reviewer_worktree, "compare", lambda *a, **k: dirty_delta)

    result = mutation_probe.run_mutation_probe(repo_path, before_ref, after_ref)

    assert result.verdict == "error"
    assert result.tree_intact is False
    assert not any(p.verdict == "killed" for p in result.probes)
    assert result.reasons


# --------------------------------------------------------------------------- #
# AC4 — exactly one executable-code node changes per mutation                 #
# --------------------------------------------------------------------------- #


def test_exactly_one_executable_node_changes_per_mutation():
    source = CALC_SOURCE
    candidates = mutation_probe._mutations_for(source, "add")
    assert candidates
    for m in candidates:
        mutated = mutation_probe._apply_one(source, m)
        if mutated is None:
            continue
        assert ast.dump(ast.parse(mutated)) != ast.dump(ast.parse(source))
        start = mutation_probe._pos_to_offset(source, m.lineno, m.col_offset)
        end = mutation_probe._pos_to_offset(source, m.end_lineno, m.end_col_offset)
        tail_len = len(source) - end
        # Everything OUTSIDE the mutation's own recorded range is untouched...
        assert source[:start] == mutated[:start]
        assert source[end:] == mutated[len(mutated) - tail_len:]
        # ...and something INSIDE it did change.
        assert source[start:end] != mutated[start:len(mutated) - tail_len]


def test_a_string_or_comment_edit_is_not_accepted_as_a_mutation():
    source = (
        "def f():\n"
        '    """docstring here"""\n'
        "    x = 1  # a comment\n"
        "    return x\n"
    )
    doc_stmt = ast.parse(source).body[0].body[0]
    doc_mutation = mutation_probe._Mutation(
        doc_stmt.lineno, doc_stmt.col_offset,
        doc_stmt.end_lineno, doc_stmt.end_col_offset,
        "pass", "doc edit",
    )
    assert mutation_probe._apply_one(source, doc_mutation) is None

    comment_line = source.splitlines()[2]
    comment_col = comment_line.index("#")
    comment_mutation = mutation_probe._Mutation(
        3, comment_col, 3, len(comment_line), "", "comment edit",
    )
    assert mutation_probe._apply_one(source, comment_mutation) is None

    # A target whose only statements are a docstring plus a `return` constant
    # yields no candidate that overlaps the docstring at all.
    func_source = (
        "def g():\n"
        '    """just a docstring"""\n'
        "    return 1\n"
    )
    candidates = mutation_probe._mutations_for(func_source, "g")
    assert candidates
    doc_stmt2 = ast.parse(func_source).body[0].body[0]
    doc_start = mutation_probe._pos_to_offset(
        func_source, doc_stmt2.lineno, doc_stmt2.col_offset)
    doc_end = mutation_probe._pos_to_offset(
        func_source, doc_stmt2.end_lineno, doc_stmt2.end_col_offset)
    for m in candidates:
        m_start = mutation_probe._pos_to_offset(func_source, m.lineno, m.col_offset)
        m_end = mutation_probe._pos_to_offset(func_source, m.end_lineno, m.end_col_offset)
        assert m_end <= doc_start or m_start >= doc_end


def test_declarations_and_imports_are_never_removed():
    source = (
        "class Foo:\n"
        "    def target(self):\n"
        "        import sys\n"
        "        def inner():\n"
        "            return 1\n"
        "        if sys.path:\n"
        "            return inner()\n"
        "        return 0\n"
    )
    candidates = mutation_probe._mutations_for(source, "target")
    assert candidates
    lines = source.splitlines()
    for m in candidates:
        for lineno in range(m.lineno, m.end_lineno + 1):
            stripped = lines[lineno - 1].strip()
            assert not stripped.startswith("import ")
            assert not stripped.startswith("def ")
            assert not stripped.startswith("class ")


# --------------------------------------------------------------------------- #
# AC5 — a review that cannot perform the check reports that, never silently   #
# --------------------------------------------------------------------------- #


def test_an_unmappable_test_is_undetermined_not_passed(tmp_path):
    repo_path, before_ref, after_ref = _repo_with_test(
        tmp_path,
        "def test_pure_arithmetic():\n    assert 1 + 1 == 2\n",
    )
    result = mutation_probe.run_mutation_probe(repo_path, before_ref, after_ref)
    assert len(result.probes) == 1
    probe = result.probes[0]
    assert probe.verdict == "undetermined"
    assert "could not determine" in probe.reason


def test_a_non_python_test_file_is_reported_not_skipped(tmp_path):
    before_ref = _init_repo(tmp_path)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "thing.test.js").write_text("test('x', () => {});\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "add js test")
    after_ref = _rev(tmp_path)

    result = mutation_probe.run_mutation_probe(tmp_path, before_ref, after_ref)
    assert len(result.probes) == 1
    probe = result.probes[0]
    assert probe.verdict == "undetermined"
    assert "pytest-only" in probe.reason


def test_a_missing_interpreter_errors_rather_than_passing(repo, monkeypatch):
    repo_path, before_ref, after_ref = repo
    monkeypatch.setattr(mutation_probe, "_pytest_python", lambda repo_path: None)

    result = mutation_probe.run_mutation_probe(repo_path, before_ref, after_ref)
    assert result.verdict == "error"
    assert not any(p.verdict == "killed" for p in result.probes)
    assert result.reasons


def test_zero_collection_is_never_read_as_a_kill(repo, monkeypatch):
    repo_path, before_ref, after_ref = repo
    real = mutation_probe._run_pytest_proc
    state = {"n": 0}

    def fake(tests, cwd, env, python):
        state["n"] += 1
        if state["n"] == 1:
            return real(tests, cwd, env, python)  # baseline: must genuinely pass
        return 5, "no tests ran"

    monkeypatch.setattr(mutation_probe, "_run_pytest_proc", fake)
    result = mutation_probe.run_mutation_probe(repo_path, before_ref, after_ref)

    assert len(result.probes) == 1
    # A run that collected nothing is never mistaken for proof the test failed.
    assert result.probes[0].verdict != "killed"
    assert state["n"] > 1  # the fake was actually exercised on a mutated run


def test_the_budget_cap_reports_the_unprobed_tests(tmp_path):
    before_ref = _init_repo(tmp_path)
    (tmp_path / "calc.py").write_text(CALC_SOURCE)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text(
        "from calc import add\n\n\ndef test_a():\n    assert add(1, 1) == 2\n"
    )
    (tmp_path / "tests" / "test_b.py").write_text(
        "from calc import add\n\n\ndef test_b():\n    assert add(2, 2) == 4\n"
    )
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "add two tests")
    after_ref = _rev(tmp_path)

    result = mutation_probe.run_mutation_probe(tmp_path, before_ref, after_ref, max_tests=1)
    assert len(result.probes) == 2
    budgeted = [p for p in result.probes if "probe budget exhausted" in p.reason]
    assert len(budgeted) == 1
    assert budgeted[0].verdict == "undetermined"


# --------------------------------------------------------------------------- #
# One mutation active at a time                                               #
# --------------------------------------------------------------------------- #


def test_one_mutation_at_a_time(repo, monkeypatch):
    repo_path, before_ref, after_ref = repo
    pristine = (repo_path / "calc.py").read_text()
    real = mutation_probe._run_pytest_proc
    seen: list[str] = []

    def fake(tests, cwd, env, python):
        target = Path(cwd) / "calc.py"
        seen.append(target.read_text())
        return real(tests, cwd, env, python)

    monkeypatch.setattr(mutation_probe, "_run_pytest_proc", fake)
    mutation_probe.run_mutation_probe(repo_path, before_ref, after_ref)

    assert len(seen) >= 2  # baseline run, plus at least one mutated run
    assert seen[0] == pristine  # the baseline run sees the pristine file
    for mutated in seen[1:]:
        assert mutated != pristine
        start = 0
        while start < len(pristine) and start < len(mutated) and pristine[start] == mutated[start]:
            start += 1
        end_p, end_m = len(pristine), len(mutated)
        while end_p > start and end_m > start and pristine[end_p - 1] == mutated[end_m - 1]:
            end_p -= 1
            end_m -= 1
        # A single, bounded region differs — not a rewrite of the whole file.
        assert start < end_p or start < end_m
