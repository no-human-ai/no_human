"""Mutation probe: does a changed test actually pin the behaviour it names?

These tests pin the core blockers from the send-back with real repros (a
tmp git repo + the actual `run_mutation_probe`/`_probe_one` pipeline, or a
direct unit call for the smaller primitives) rather than code-inspection
claims:

* BLOCKER 1 — a "survived" verdict must only be reported after every
  statically-ranked target has been tried (or the mutation budget spent),
  never after the first candidate alone. A decoy/real-target fixture proves
  both the exhaust-to-a-kill path and the honest "budget ran out early"
  reason text.
* BLOCKER 2 — the string/comment guard must reject only a mutation range
  CONTAINED IN a string/comment token, not merely one that overlaps/contains
  a string literal as a sub-expression.
* BLOCKER 3 — `_pos_to_offset` must convert `ast`'s byte column to a
  character offset; a non-ASCII character earlier on the same line must not
  shift a later mutation's slice.
* M6 — in one file with several test functions, only the ones whose source
  actually changed are probe candidates.
* M8 — a missing pytest interpreter is reported as an `"error"` verdict
  naming that cause, never a crash, and never reaches the pytest launcher.
* M17 — a token stream that cannot even be built (e.g. `tokenize.TokenError`,
  not the nonexistent `tokenize.TokenizeError`) fails CLOSED (rejects the
  mutation), never crashes.
"""

import ast
import subprocess
import tokenize

import pytest

from no_human.testing import mutation_probe


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "u@e.com")
    _git(tmp_path, "config", "user.name", "u")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "__init__.py").write_text("")
    return tmp_path


def _commit(repo, msg="change"):
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", msg)


# --------------------------------------------------------------------------
# BLOCKER 3: `_pos_to_offset` byte-vs-character offsets.
# --------------------------------------------------------------------------


def test_pos_to_offset_converts_byte_column_to_char_offset_on_non_ascii_line():
    line = "x = 1  # em—dash comment"  # the em-dash is 1 char, 3 UTF-8 bytes
    text = line + "\n" + "y = 2\n"
    byte_col = len(line.encode("utf-8"))
    char_col = len(line)
    assert byte_col != char_col, "fixture must actually exercise multi-byte chars"
    assert mutation_probe._pos_to_offset(text, 1, byte_col) == char_col


def test_pos_to_offset_is_a_noop_for_ascii_only_lines():
    text = "x = 1\ny = 2\n"
    assert mutation_probe._pos_to_offset(text, 2, 3) == len("x = 1\n") + 3


def test_apply_one_slices_correctly_when_non_ascii_precedes_mutation_on_same_line():
    """Regression for the byte/char offset bug: a statement-removal mutation
    targets the SECOND of two semicolon-joined statements on one line, where
    the first statement contains a multi-byte (em-dash) string literal. If
    `_pos_to_offset` did not convert AST's byte column to a character
    offset, the slice for the second statement would land in the wrong
    place and either corrupt the mutated text or wrongly refuse to apply."""
    source = (
        "def rounds_up(x):\n"
        '    marker = "note—here"; total = x * 2\n'
        "    return total\n"
    )
    mutations = mutation_probe._mutations_for(source, "rounds_up")
    target = next(m for m in mutations if "total = x * 2" in m.description)
    mutated = mutation_probe._apply_one(source, target)
    assert mutated is not None
    assert 'marker = "note—here"; pass' in mutated
    ast.parse(mutated)  # must still be syntactically valid


# --------------------------------------------------------------------------
# BLOCKER 2: string/comment CONTAINMENT, not overlap.
# --------------------------------------------------------------------------


def test_string_overlap_guard_allows_a_range_that_contains_a_string_literal():
    # The comparison `x == "foo"` CONTAINS the string literal as a
    # sub-expression — flipping the whole comparison must be allowed.
    text = 'if x == "foo":\n    pass\n'
    start = text.index('x == "foo"')
    end = start + len('x == "foo"')
    assert mutation_probe._edit_inside_string_or_comment(text, start, end) is False


def test_string_overlap_guard_rejects_a_range_inside_the_string_literal():
    text = 'if x == "foo":\n    pass\n'
    start = text.index("foo")
    end = start + len("foo")
    assert mutation_probe._edit_inside_string_or_comment(text, start, end) is True


def test_string_overlap_guard_rejects_a_range_inside_a_comment():
    text = "x = 1  # do not touch this\ny = 2\n"
    start = text.index("do not touch")
    end = start + len("do not touch")
    assert mutation_probe._edit_inside_string_or_comment(text, start, end) is True


def test_string_overlap_guard_allows_a_condition_that_trails_a_comment_line():
    # The comment is on its own line; the mutation range below is on the
    # NEXT line and does not touch it at all.
    text = "# a note\nif x > 0:\n    pass\n"
    start = text.index("x > 0")
    end = start + len("x > 0")
    assert mutation_probe._edit_inside_string_or_comment(text, start, end) is False


# --------------------------------------------------------------------------
# M17: the token stream cannot be built -> fail CLOSED, never crash.
# --------------------------------------------------------------------------


def test_string_overlap_guard_fails_closed_when_tokenize_raises_token_error(monkeypatch):
    def boom(_readline):
        raise tokenize.TokenError("simulated: EOF in multi-line statement")

    monkeypatch.setattr(mutation_probe.tokenize, "generate_tokens", boom)
    assert mutation_probe._edit_inside_string_or_comment("x = 1\n", 0, 1) is True


def test_string_overlap_guard_fails_closed_on_a_real_unterminated_token_stream():
    # An actually-unterminated triple-quoted string raises `tokenize.TokenError`
    # for real (not `tokenize.TokenizeError`, which does not exist on the
    # `tokenize` module) — this is the exact exception class regression.
    bad = 'x = """unterminated\n'
    with pytest.raises(tokenize.TokenError):
        list(tokenize.generate_tokens(__import__("io").StringIO(bad).readline))
    assert mutation_probe._edit_inside_string_or_comment(bad, 0, 1) is True


# --------------------------------------------------------------------------
# `_apply_one`'s second-layer guards.
# --------------------------------------------------------------------------


def test_apply_one_rejects_a_mutation_that_leaves_the_ast_unchanged():
    # `(x)` and `x` parse to the identical AST (the parens carry no node) —
    # this must be refused as a no-op mutation, not silently "applied."
    source = "def f(x):\n    return (x)\n"
    start = source.index("(x)")
    mutation = mutation_probe._Mutation(
        lineno=2, col_offset=11, end_lineno=2, end_col_offset=14,
        new_text="x", description="strip redundant parens",
    )
    assert mutation_probe._apply_one(source, mutation) is None


def test_apply_one_rejects_a_mutation_that_breaks_syntax():
    source = "def f(x):\n    return x\n"
    mutation = mutation_probe._Mutation(
        lineno=2, col_offset=11, end_lineno=2, end_col_offset=12,
        new_text=")(", description="break syntax",
    )
    assert mutation_probe._apply_one(source, mutation) is None


def test_apply_one_rejects_an_empty_range():
    source = "def f(x):\n    return x\n"
    mutation = mutation_probe._Mutation(
        lineno=2, col_offset=11, end_lineno=2, end_col_offset=11,
        new_text="y", description="empty range",
    )
    assert mutation_probe._apply_one(source, mutation) is None


# --------------------------------------------------------------------------
# `_mutations_for`: single-statement bodies are not blunt-force removed.
# --------------------------------------------------------------------------


def test_mutations_for_skips_statement_removal_on_a_single_statement_body():
    source = "def f(x):\n    return x * 2\n"
    mutations = mutation_probe._mutations_for(source, "f")
    # Only the return-negation candidate, never a "remove the only statement"
    # candidate that would reduce the whole function to `pass`.
    assert all("remove statement" not in m.description for m in mutations)
    assert any("negate return" in m.description for m in mutations)


def test_mutations_for_does_include_statement_removal_on_a_multi_statement_body():
    source = "def f(x):\n    y = x * 2\n    return y\n"
    mutations = mutation_probe._mutations_for(source, "f")
    assert any("remove statement" in m.description for m in mutations)


# --------------------------------------------------------------------------
# M6: mixed changed/unchanged test functions in the same file.
# --------------------------------------------------------------------------


def test_m6_only_the_actually_changed_test_function_in_a_file_is_a_candidate(repo):
    (repo / "pkg" / "calc.py").write_text("def double(x):\n    return x * 2\n")
    (repo / "tests" / "test_calc.py").write_text(
        "from pkg.calc import double\n\n"
        "def test_double_unchanged():\n"
        "    assert double(1) == 2\n\n"
        "def test_double_changed():\n"
        "    assert double(2) == 4\n"
    )
    _commit(repo)
    (repo / "tests" / "test_calc.py").write_text(
        "from pkg.calc import double\n\n"
        "def test_double_unchanged():\n"
        "    assert double(1) == 2\n\n"
        "def test_double_changed():\n"
        "    assert double(2) == 4\n"
        "    assert double(3) == 6\n"
    )
    _commit(repo, "change one test only")

    changed, pre_probes = mutation_probe.changed_test_functions(repo, "HEAD~1", "HEAD")
    node_ids = [c["node_id"] for c in changed]
    assert node_ids == ["tests/test_calc.py::test_double_changed"], (
        "the byte-identical sibling test must not be re-probed just because "
        "the file around it changed")
    assert pre_probes == []


def test_non_python_test_file_is_reported_undetermined_not_skipped(repo):
    (repo / "pkg" / "calc.py").write_text("def double(x):\n    return x * 2\n")
    (repo / "tests" / "test_calc.py").write_text("placeholder\n")
    _commit(repo)
    (repo / "tests" / "test_calc.js").write_text("// not python\n")
    _commit(repo, "add a non-python test file")

    changed, pre_probes = mutation_probe.changed_test_functions(repo, "HEAD~1", "HEAD")
    assert changed == []
    assert len(pre_probes) == 1
    assert pre_probes[0].verdict == "undetermined"
    assert pre_probes[0].node_id == "tests/test_calc.js"


# --------------------------------------------------------------------------
# M8: no pytest interpreter -> named "error" verdict, never a crash, never
# a pytest launch attempt.
# --------------------------------------------------------------------------


def _write_single_changed_test(repo):
    (repo / "pkg" / "calc.py").write_text("def double(x):\n    return x * 2\n")
    (repo / "tests" / "test_calc.py").write_text(
        "from pkg.calc import double\n\ndef test_double():\n"
        "    assert double(2) == 4\n"
    )
    _commit(repo)
    (repo / "tests" / "test_calc.py").write_text(
        "from pkg.calc import double\n\ndef test_double():\n"
        "    assert double(2) == 4\n    assert double(3) == 6\n"
    )
    _commit(repo, "change the test")


def test_m8_missing_interpreter_is_an_error_verdict_never_a_pytest_launch(repo, monkeypatch):
    _write_single_changed_test(repo)

    monkeypatch.setattr(mutation_probe, "_pytest_python", lambda _repo_path: None)

    def must_not_be_called(*_args, **_kwargs):
        raise AssertionError(
            "pytest must never be launched once the interpreter guard fires")

    monkeypatch.setattr(mutation_probe, "_run_pytest_proc", must_not_be_called)

    result = mutation_probe.run_mutation_probe(repo, "HEAD~1", "HEAD")
    assert result.verdict == "error"
    assert any("no python interpreter" in r.lower() for r in result.reasons)
    assert result.tree_intact is True


# --------------------------------------------------------------------------
# BLOCKER 1: exhaust every ranked target before declaring "survived"; never
# stop at the first candidate.
# --------------------------------------------------------------------------


@pytest.fixture
def decoy_and_real_target_repo(repo):
    """A test whose first-ranked static candidate (`pkg/calc.py:volumetric`,
    called but its return value discarded) is a DECOY — mutating it can
    never make the test fail. The test's actual assertion is against a
    second, lower-ranked candidate (`pkg/volumetric.py:rounds_up`). Any
    probe that stops after the first candidate will wrongly report
    "survived" forever; only one that tries every ranked candidate can ever
    reach the kill.
    """
    (repo / "pkg" / "calc.py").write_text(
        "def volumetric(x):\n"
        "    if x > 0:\n"
        "        return x * 2\n"
        "    return 0\n"
    )
    (repo / "pkg" / "volumetric.py").write_text(
        "def rounds_up(x):\n"
        "    if x > 0:\n"
        "        return x * 2\n"
        "    return 0\n"
    )
    (repo / "tests" / "test_calc.py").write_text(
        "from pkg.calc import volumetric\n"
        "from pkg import volumetric as volumetric_mod\n\n"
        "def test_volumetric_rounds_up():\n"
        "    volumetric(3)  # decoy call -- return value unused\n"
        "    assert volumetric_mod.rounds_up(3) == 6\n"
    )
    _commit(repo)
    (repo / "tests" / "test_calc.py").write_text(
        "from pkg.calc import volumetric\n"
        "from pkg import volumetric as volumetric_mod\n\n"
        "def test_volumetric_rounds_up():\n"
        "    volumetric(3)  # decoy call -- return value unused\n"
        "    assert volumetric_mod.rounds_up(3) == 6\n"
        "    assert volumetric_mod.rounds_up(3) == 6  # changed: duplicate assert\n"
    )
    _commit(repo, "touch the test so it is a probe candidate")
    return repo


def test_blocker1_exhausts_the_decoy_and_kills_at_the_real_target(decoy_and_real_target_repo):
    result = mutation_probe.run_mutation_probe(
        decoy_and_real_target_repo, "HEAD~1", "HEAD",
        max_tests=30, max_mutations=8, timeout=120,
    )
    assert result.tree_intact is True
    assert result.verdict == "pass"  # a killed probe is a good outcome
    assert len(result.probes) == 1
    probe = result.probes[0]
    assert probe.verdict == "killed", probe.reason
    assert probe.target == "pkg/volumetric.py:rounds_up"


def test_blocker1_reports_a_budget_limited_reason_not_an_overclaim(decoy_and_real_target_repo):
    # Only enough budget to exhaust the decoy target's own mutations —
    # the real target is never reached. The "survived" reason must say
    # this is a budget limit, not claim every target was tried.
    result = mutation_probe.run_mutation_probe(
        decoy_and_real_target_repo, "HEAD~1", "HEAD",
        max_tests=30, max_mutations=1, timeout=120,
    )
    assert result.tree_intact is True
    probe = result.probes[0]
    assert probe.verdict == "survived"
    assert probe.target == "pkg/calc.py:volumetric"
    assert "budget" in probe.reason.lower()
    assert "1 of" in probe.reason
    assert "never tried" in probe.reason


# --------------------------------------------------------------------------
# AC3: the reviewed tree is byte-identical after a real probe run.
# --------------------------------------------------------------------------


def test_tree_is_byte_identical_after_a_successful_probe_run(decoy_and_real_target_repo):
    before = (decoy_and_real_target_repo / "pkg" / "calc.py").read_text()
    before_vol = (decoy_and_real_target_repo / "pkg" / "volumetric.py").read_text()
    result = mutation_probe.run_mutation_probe(
        decoy_and_real_target_repo, "HEAD~1", "HEAD",
        max_tests=30, max_mutations=8, timeout=120,
    )
    assert result.tree_intact is True
    assert (decoy_and_real_target_repo / "pkg" / "calc.py").read_text() == before
    assert (decoy_and_real_target_repo / "pkg" / "volumetric.py").read_text() == before_vol


# --------------------------------------------------------------------------
# A genuinely-pinned test is reported "killed" on the simple/common path.
# --------------------------------------------------------------------------


def test_a_test_that_actually_pins_its_target_is_reported_killed(repo):
    (repo / "pkg" / "calc.py").write_text(
        "def double(x):\n    return x * 2\n"
    )
    (repo / "tests" / "test_calc.py").write_text(
        "from pkg.calc import double\n\ndef test_double():\n"
        "    assert double(2) == 4\n"
    )
    _commit(repo)
    (repo / "tests" / "test_calc.py").write_text(
        "from pkg.calc import double\n\ndef test_double():\n"
        "    assert double(2) == 4\n    assert double(3) == 6\n"
    )
    _commit(repo, "change the test")

    result = mutation_probe.run_mutation_probe(repo, "HEAD~1", "HEAD", max_tests=30)
    assert result.verdict == "pass"  # a killed probe is a good outcome
    assert result.probes[0].verdict == "killed"


def test_no_test_file_changed_is_reported_skipped(repo):
    (repo / "pkg" / "calc.py").write_text("def double(x):\n    return x * 2\n")
    _commit(repo)
    (repo / "pkg" / "calc.py").write_text("def double(x):\n    return x * 3\n")
    _commit(repo, "change production code only")

    result = mutation_probe.run_mutation_probe(repo, "HEAD~1", "HEAD")
    assert result.verdict == "skipped"
    assert result.tree_intact is True
