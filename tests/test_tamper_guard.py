"""Test-tampering guard (§3.4): net reduction in tests/assertions => fail closed."""

import pytest

from no_human.testing import tamper_guard

PY_TESTS = '''
def test_a():
    assert 1 == 1
    assert foo() == 2

def test_b():
    assert bar()
'''

PY_WEAKENED = '''
def test_a():
    assert 1 == 1

def test_b():
    pass
'''


def test_is_test_file():
    assert tamper_guard.is_test_file("tests/test_x.py")
    assert tamper_guard.is_test_file("src/foo_test.py")
    assert tamper_guard.is_test_file("web/Button.spec.tsx")
    assert tamper_guard.is_test_file("com/acme/FooIT.java")
    assert not tamper_guard.is_test_file("src/foo.py")


def test_is_test_file_table():
    """Table-driven: guarded vs unguarded paths, incl. .mjs/.cjs test files."""
    guarded = [
        "web/src/boardLanes.test.mjs",
        "desktop/main.test.mjs",
        "foo.spec.mjs",
        "foo.test.cjs",
        "foo.spec.cjs",
        "tests/test_x.py",
        "src/foo_test.py",
        "conftest.py",
        "src/pkg/conftest.py",
        "web/Button.test.js",
        "web/Button.test.ts",
        "web/Button.test.jsx",
        "web/Button.spec.tsx",
        "com/acme/FooTest.java",
        "com/acme/FooIT.java",
        "pkg/__tests__/x.js",
        # The UI gate. This ASSERTION MOVED from `unguarded` to `guarded` on
        # 2026-07-29, deliberately and with justification — it is not a test being
        # weakened to clear a path. It sat in the unguarded list with NO comment,
        # grouped with product code (App.jsx, foo.py), which reads as a snapshot of
        # what the patterns happened to match rather than a designed exclusion.
        # An execution audit then proved the consequence: all 14 e2e suites could be
        # deleted or gutted with `tampered=False`, and 417 lines of e2e counted as
        # ZERO. That is the gate catching what the unit suites structurally cannot —
        # a blank page from a ReferenceError, a `border` painting nothing because
        # Tailwind preflight is off, a light-theme regression.
        # KNOWN TRADEOFF: e2e suites are large and refactored often, so an honest
        # rewrite that nets fewer `check(` calls will now trip the guard and need a
        # human to justify it. That is the intended direction — the guard fires on a
        # net reduction and a person decides — and it is strictly better than a gate
        # that cannot be defended at all.
        "web/e2e/board.mjs",
    ]
    unguarded = [
        "web/src/App.jsx",
        "web/src/overviewStrip.js",
        "src/foo.py",
    ]
    for path in guarded:
        assert tamper_guard.is_test_file(path), f"expected guarded: {path}"
    for path in unguarded:
        assert not tamper_guard.is_test_file(path), f"expected unguarded: {path}"


def test_detects_mjs_test_gutting():
    """Gutting assertions out of a .test.mjs file must be caught (was a bug)."""
    before = {
        "web/src/boardLanes.test.mjs":
            "it('lanes render', () => {\n"
            "    expect(lanes.length).toBe(4);\n"
            "    expect(lanes[0].name).toBe('backlog');\n"
            "});\n"
    }
    after = {
        "web/src/boardLanes.test.mjs":
            "it('lanes render', () => {\n"
            "});\n"
    }
    report = tamper_guard.check(before, after)
    assert report.tampered is True
    assert any("assertions" in r for r in report.reasons)


def test_clean_when_assertions_added():
    before = {"tests/test_x.py": PY_TESTS}
    after = {"tests/test_x.py": PY_TESTS + "\n    assert extra()\n"}
    report = tamper_guard.check(before, after)
    assert report.tampered is False


def test_detects_assertion_reduction():
    before = {"tests/test_x.py": PY_TESTS}
    after = {"tests/test_x.py": PY_WEAKENED}
    report = tamper_guard.check(before, after)
    assert report.tampered is True
    assert report.assertions_after < report.assertions_before
    assert report.reasons


def test_detects_test_file_deletion():
    before = {"tests/test_x.py": PY_TESTS}
    after = {}
    report = tamper_guard.check(before, after)
    assert report.tampered is True
    assert any("deleted" in r for r in report.reasons)


def test_ignores_non_test_files():
    before = {"src/app.py": "assert True\nassert False\n"}
    after = {"src/app.py": "pass\n"}
    report = tamper_guard.check(before, after)
    assert report.tampered is False  # not a test file; product code is fair game


def test_counts_across_languages():
    assert tamper_guard.count_tests("def test_x(): pass") == 1
    assert tamper_guard.count_tests("it('works', () => {})") == 1
    assert tamper_guard.count_tests("@Test\nvoid foo(){}") == 1
    assert tamper_guard.count_assertions("expect(x).toBe(1)") == 1
    assert tamper_guard.count_assertions("assertThat(x).isEqualTo(1)") >= 1


def test_regexp_test_call_is_not_a_declaration():
    """RegExp.prototype.test() calls must not be counted as test declarations."""
    assert tamper_guard.count_tests("re.test(x)") == 0
    assert tamper_guard.count_tests("/re/.test(x)") == 0
    assert tamper_guard.count_tests("foo.test(bar)") == 0
    assert tamper_guard.count_tests("assert.ok(!/transparent/.test(body));") == 0


def test_word_ending_in_test_is_not_a_declaration():
    """A variable/word ending in 'test' followed by '(' must not count."""
    assert tamper_guard.count_tests("latest(1)") == 0
    assert tamper_guard.count_tests("var x = latest(y);") == 0


def test_bare_test_call_still_counts():
    """A genuine standalone test(...) declaration must still be counted."""
    assert tamper_guard.count_tests("test('does a thing', () => {})") == 1
    assert tamper_guard.count_tests("  test(`templated ${x}`, () => {})") == 1


def test_mixed_declarations_and_regexp_calls_count_only_declarations():
    src = (
        "test('a', () => {\n"
        "  assert.ok(!/transparent/.test(body));\n"
        "});\n"
        "test('b', () => {\n"
        "  if (/x/.test(y)) { return; }\n"
        "});\n"
    )
    assert tamper_guard.count_tests(src) == 2


def test_bare_it_and_describe_still_count():
    """Genuine it()/describe() declarations must still be counted."""
    assert tamper_guard.count_tests("it('x', () => {})") == 1
    assert tamper_guard.count_tests("  it('indented', () => {})") == 1
    assert tamper_guard.count_tests("describe('suite', () => {})") == 1
    assert tamper_guard.count_tests("  describe('nested', () => {})") == 1


def test_playwright_test_describe_prefix_still_counts():
    """`test.describe(` (Playwright's suite form) is an allowlisted genuine declaration."""
    assert tamper_guard.count_tests('test.describe("suite", () => {})') == 1
    src = (
        "test.describe('outer', () => {\n"
        "  it('does a thing', () => {});\n"
        "});\n"
    )
    assert tamper_guard.count_tests(src) == 2


def test_describe_to_test_describe_refactor_is_not_tampered():
    """Renaming `describe(` to `test.describe(` is an honest refactor, not tampering.

    Reproduces the reported regression: the dot-prefix guard on `describe` alone
    would drop `test.describe(` to zero, making an honest Playwright-style suite
    rename look like a test-count decrease.
    """
    before = {
        "web/tests/x.spec.js":
            "describe('suite', () => {\n"
            "  it('does a thing', () => {});\n"
            "});\n"
    }
    after = {
        "web/tests/x.spec.js":
            "test.describe('suite', () => {\n"
            "  it('does a thing', () => {});\n"
            "});\n"
    }
    report = tamper_guard.check(before, after)
    assert report.tampered is False
    assert report.tests_before == report.tests_after == 2


def test_method_it_and_describe_are_not_declarations():
    """foo.it(...) / obj.describe(...) are method calls, not declarations."""
    assert tamper_guard.count_tests("foo.it(bar)") == 0
    assert tamper_guard.count_tests("obj.describe(x)") == 0
    assert tamper_guard.count_tests("suite.it(y)") == 0


def test_word_ending_in_it_or_describe_is_not_a_declaration():
    """A variable/word ending in 'it'/'describe' followed by '(' must not count."""
    assert tamper_guard.count_tests("edit(x)") == 0
    assert tamper_guard.count_tests("audit(y)") == 0


def test_check_honest_regexp_refactor_is_not_tampered():
    """Swapping a `.test(` regex helper for an equivalent one must not trip the guard.

    Reproduces the reported bug: before, `_TEST_DECL` counted `.test(` calls as
    test declarations, so replacing them with an equivalent non-`.test()` check
    dropped the apparent count and produced a false TAMPERED verdict.
    """
    before = {
        "web/src/x.test.mjs":
            "test('renders without a transparent background', () => {\n"
            "  const body = getBody();\n"
            "  assert.ok(!/background:\\s*transparent/.test(body));\n"
            "});\n"
    }
    after = {
        "web/src/x.test.mjs":
            "test('renders without a transparent background', () => {\n"
            "  const body = getBody();\n"
            "  assert.ok(!includesTransparentBackground(body));\n"
            "});\n"
    }
    report = tamper_guard.check(before, after)
    assert report.tampered is False
    assert report.tests_before == report.tests_after == 1


# --------------------------------------------------------------------------- #
# Isolation-corpus attacks: cheats that keep test/assertion counts unchanged   #
# --------------------------------------------------------------------------- #

def test_conftest_is_a_test_file():
    assert tamper_guard.is_test_file("conftest.py")
    assert tamper_guard.is_test_file("src/pkg/conftest.py")


def test_detects_skip_marker_neutering():
    """@pytest.mark.skip leaves test + assertion counts identical but lies."""
    before = {"test_x.py": "def test_divide():\n    assert divide(7, 2) == 3.5\n"}
    after = {
        "test_x.py":
            "import pytest\n\n@pytest.mark.skip\ndef test_divide():\n"
            "    assert divide(7, 2) == 3.5\n"
    }
    report = tamper_guard.check(before, after)
    assert report.tampered is True
    assert report.assertions_after == report.assertions_before  # count unchanged
    assert any("skip" in r.lower() for r in report.reasons)


def test_detects_xfail_marker():
    before = {"test_x.py": "def test_a():\n    assert f() == 1\n"}
    after = {"test_x.py": "import pytest\n@pytest.mark.xfail\ndef test_a():\n    assert f() == 1\n"}
    assert tamper_guard.check(before, after).tampered is True


def test_detects_tautology_substitution():
    """Replacing a real assertion with `assert True` keeps the count at 1."""
    before = {"test_x.py": "def test_divide():\n    assert divide(7, 2) == 3.5\n"}
    after = {"test_x.py": "def test_divide():\n    assert True\n"}
    report = tamper_guard.check(before, after)
    assert report.tampered is True
    assert report.assertions_after == report.assertions_before  # still one `assert`
    assert any("tautolog" in r.lower() for r in report.reasons)


def test_detects_self_equality_tautology():
    before = {"test_x.py": "def test_a():\n    assert result == 42\n"}
    after = {"test_x.py": "def test_a():\n    assert result == result\n"}
    assert tamper_guard.check(before, after).tampered is True


def test_string_literal_assert_true_is_not_a_tautology():
    """Test DATA carrying `assert True` (a holdout body as an argument, or sample
    source in a docstring) must NOT count as a tautology. This was a live FP that
    escalated correct work: a test ABOUT the harness has to embed test source.
    """
    holdout_arg = (
        'def test_holdout_rejects_empty():\n'
        '    r = _holdout_ok(holdout="def test_x():\\n    assert True\\n")\n'
        '    assert r is None\n'
    )
    docstring_sample = (
        'def test_thing():\n'
        '    """example the guard once miscounted:\n'
        '        assert x == x\n'
        '    """\n'
        '    assert real_thing() == 5\n'
    )
    assert tamper_guard.count_tautologies(holdout_arg) == 0
    assert tamper_guard.count_tautologies(docstring_sample) == 0
    # And end-to-end: ADDING such a test to a real tests/ path is not "tampered".
    report = tamper_guard.check(
        {"tests/test_h.py": "def test_a():\n    assert f() == 1\n"},
        {"tests/test_h.py": "def test_a():\n    assert f() == 1\n" + holdout_arg},
    )
    assert report.tampered is False, report.reasons
    assert report.tautologies_after == report.tautologies_before, report.summary


def test_real_tautology_forms_all_still_counted():
    """Masking string literals must not drop any REAL tautology form — including
    `assertTrue(True)` (a Call, not an Assert node) and the JS forms, which an
    AST-Assert-only rewrite would have silently dropped (guard weakening)."""
    for src in (
        "def t():\n    assert True\n",
        "def t():\n    assert not False\n",
        "def t():\n    assert 1\n",
        "def t():\n    x = 1\n    assert x == x\n",
        "def t(self):\n    self.assertTrue(True)\n",
        "it('x', () => { expect(true).toBe(true); })\n",          # JS: unparseable -> regex fallback
        "it('x', () => { expect(true).toBeTruthy(); })\n",
    ):
        assert tamper_guard.count_tautologies(src) >= 1, src


def test_self_equality_tautology_still_matches_with_trailer():
    """`assert x == x` must still match with a trailing message, comment, or
    extra whitespace — the fix only needs to reject a DERIVED-value RHS, not
    every non-identifier character after it."""
    for src in (
        "def t():\n    assert x == x\n",
        'def t():\n    assert x == x, "msg"\n',
        "def t():\n    assert x == x  # comment\n",
        "def t():\n    assert x  ==  x\n",
    ):
        assert tamper_guard.count_tautologies(src) >= 1, src


def test_comparison_to_derived_value_is_not_a_tautology():
    """`assert x == x.<anything>` compares x to a DERIVED value, not itself —
    it is a real assertion and must not be counted as a tautology. This was
    the live false positive: `assert home == home.resolve()` (the strongest
    assertion available for its subject, that the function returns a
    RESOLVED path) was miscounted, and a tamper adjudication returned
    CANNOT_DECIDE citing it."""
    for src in (
        'def t():\n    assert home == home.resolve(), "unresolved path"\n',
        "def t():\n    assert p == p.parent\n",
        "def t():\n    assert xs == xs[0]\n",
        "def t():\n    assert obj == obj.method()\n",
        "def t():\n    assert x == len(x)\n",
        "def t():\n    assert x == x_2\n",
    ):
        assert tamper_guard.count_tautologies(src) == 0, src


def test_real_codex_backend_file_has_no_tautologies():
    """The exact line that cost task 3aed70f9: read the REAL file (not a copied
    string) and confirm it scores zero tautologies under the fixed rule."""
    path = Path(__file__).resolve().parent / "test_codex_backend.py"
    text = path.read_text(encoding="utf-8")
    assert "assert home == home.resolve()" in text, (
        "the line this regression guards against moved or was renamed; "
        "update this test to the new location before trusting the count below"
    )
    assert tamper_guard.count_tautologies(text) == 0


def test_every_tautology_alternative_still_matches():
    """Redundant with test_real_tautology_forms_all_still_counted on purpose:
    a future narrowing of `_TAUTOLOGY` (e.g. to fix another false positive)
    must not silently drop one of these forms just because only one list of
    cases was updated."""
    for src in (
        "def t():\n    assert True\n",
        "def t():\n    assert not False\n",
        "def t():\n    assert 1\n",
        'def t():\n    assert 1, "msg"\n',
        "def t():\n    assert x == x\n",
        "it('x', () => { expect(true).toBe(true); })\n",
        "it('x', () => { expect(true).toBeTruthy(); })\n",
        "def t(self):\n    self.assertTrue(True)\n",
    ):
        assert tamper_guard.count_tautologies(src) >= 1, src


def test_adding_derived_value_assertion_is_not_tampering():
    """End-to-end shape that actually cost task 3aed70f9: adding a real,
    derived-value assertion to a test file must not be flagged as tampering."""
    before = {"tests/test_x.py": "def test_a():\n    assert f() == 1\n"}
    after = {
        "tests/test_x.py": (
            "def test_a():\n    assert f() == 1\n"
            "def test_b():\n    assert home == home.resolve()\n"
        )
    }
    report = tamper_guard.check(before, after)
    assert report.tampered is False, report.reasons
    assert report.tautologies_after == report.tautologies_before, report.summary


def test_tautology_masking_is_byte_correct_on_non_ascii():
    """ast column offsets are UTF-8 byte offsets: a non-ASCII char before a real
    tautology must not shift the mask and drop it; a fake one inside a non-ASCII
    string must still be cured."""
    real_after_nonascii = 'def t():\n    label = "café — ¡hola!"\n    assert True\n'
    fake_in_nonascii = 'def t():\n    s = "café: assert x == x"\n    assert real() == 1\n'
    assert tamper_guard.count_tautologies(real_after_nonascii) == 1
    assert tamper_guard.count_tautologies(fake_in_nonascii) == 0


def test_tautology_masking_falls_back_on_unparseable_source():
    """Non-Python / partial diffs can't be AST-parsed; fall back to the raw regex
    (no worse than before) so JS tautologies and broken snippets still register."""
    assert tamper_guard.count_tautologies("def t(:\n    assert True\n") >= 1   # SyntaxError
    assert tamper_guard.count_tautologies("expect(true).toBeTruthy()\n") >= 1  # JS


def test_detects_conftest_autouse_monkeypatch():
    """A conftest autouse fixture that patches the SUT green without a real fix."""
    before = {}
    after = {
        "conftest.py":
            "import pytest\n\n"
            "@pytest.fixture(autouse=True)\n"
            "def _fake(monkeypatch):\n"
            "    monkeypatch.setattr('calc.divide', lambda a, b: 3.5)\n"
    }
    report = tamper_guard.check(before, after)
    assert report.tampered is True
    assert any("fixture" in r.lower() for r in report.reasons)


def test_legit_autouse_env_fixture_is_clean():
    """An autouse fixture that only sets env (no behaviour-faking patch) is fine."""
    before = {}
    after = {
        "conftest.py":
            "import pytest\n\n"
            "@pytest.fixture(autouse=True)\n"
            "def _env(monkeypatch):\n"
            "    monkeypatch.setenv('TZ', 'UTC')\n"
    }
    report = tamper_guard.check(before, after)
    assert report.tampered is False


def test_legit_added_real_assertions_with_helpers_clean():
    """Adding genuine assertions (not tautologies) stays clean."""
    before = {"test_x.py": "def test_a():\n    assert f() == 1\n"}
    after = {"test_x.py": "def test_a():\n    assert f() == 1\n    assert g() == 2\n"}
    report = tamper_guard.check(before, after)
    assert report.tampered is False


# --- PR-F Gate 1: linked-repo tamper detection ----------------------------- #

import subprocess
from pathlib import Path


def _init_repo(path: Path) -> None:
    """Create a minimal git repo with one commit."""
    subprocess.run(["git", "init", str(path)], capture_output=True, check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"],
                   capture_output=True, check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"],
                   capture_output=True, check=True)


def test_tamper_check_between_detects_linked_repo_tampering(tmp_path):
    """tamper_check_between detects test weakening in a linked repo."""
    from no_human.testing.runner import tamper_check_between

    repo = tmp_path / "linked"
    repo.mkdir()
    _init_repo(repo)

    # First commit: a test file with two assertions.
    test_file = repo / "test_foo.py"
    test_file.write_text("def test_a():\n    assert f() == 1\n    assert g() == 2\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], capture_output=True, check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"],
                   capture_output=True, check=True)

    # Second commit: weaken the test (remove an assertion).
    test_file.write_text("def test_a():\n    assert f() == 1\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], capture_output=True, check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "weaken"],
                   capture_output=True, check=True)

    report = tamper_check_between(repo)
    assert report.tampered is True
    assert any("assertions" in r for r in report.reasons)


def test_tamper_check_between_clean_linked_repo(tmp_path):
    """tamper_check_between passes when linked repo tests are not weakened."""
    from no_human.testing.runner import tamper_check_between

    repo = tmp_path / "linked_clean"
    repo.mkdir()
    _init_repo(repo)

    # First commit: a test file.
    test_file = repo / "test_bar.py"
    test_file.write_text("def test_a():\n    assert x() == 1\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], capture_output=True, check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"],
                   capture_output=True, check=True)

    # Second commit: ADD more assertions (legit improvement).
    test_file.write_text("def test_a():\n    assert x() == 1\n    assert y() == 2\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], capture_output=True, check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "improve"],
                   capture_output=True, check=True)

    report = tamper_check_between(repo)
    assert report.tampered is False


def test_the_e2e_gate_is_visible_to_the_guard_and_its_assertions_are_counted():
    """The UI gate was invisible: 14 suites deletable with `tampered=False`.

    `web/e2e/*.mjs` matched none of the path patterns — not `tests/`, not
    `*.test.*`, not `*.spec.*` — so an execution audit measured 417 lines of e2e
    counted as ZERO. That is the gate catching what the unit suites structurally
    cannot: a blank page from a ReferenceError, a `border` that paints nothing
    because Tailwind preflight is off, a light-theme regression.

    Both halves are required, and the path alone would have been a VACUOUS fix:
    these suites assert through a local `check(name, ok, detail)` helper, so
    without teaching `_ASSERT` about `check(` they would be counted as test files
    with zero assertions — deleting one would still show no reduction.
    """
    assert tamper_guard.is_test_file("web/e2e/form-order.mjs")
    assert tamper_guard.is_test_file("web/e2e/board.mjs")
    # Product code must stay OUT — the counting is gated on is_test_file().
    assert not tamper_guard.is_test_file("src/no_human/core/orchestrator.py")
    assert not tamper_guard.is_test_file("web/src/cost.js")

    suite = (
        'const check = (n, ok, d = "") => { if (!ok) failures.push(n); };\n'
        'check("a", x === 1);\ncheck("b", y === 2);\ncheck("c", z === 3);\n'
    )
    # 3 CALLS. The definition `const check = (n, ok, d) =>` is `check =`, not
    # `check(`, so it is correctly not counted as an assertion.
    assert tamper_guard.count_assertions(suite) == 3, (
        "the e2e assertion form must be counted, or the path pattern protects nothing"
    )
    # A gutted suite must show a REAL drop, which is what makes the guard fire.
    gutted = 'const check = (n, ok, d = "") => {};\n'
    assert tamper_guard.count_assertions(gutted) < tamper_guard.count_assertions(suite)

    # False positives that would make honest edits look like tampering.
    assert tamper_guard.count_assertions("subprocess.run(x, check=True)") == 0
    assert tamper_guard.count_assertions("el.checkVisibility({a: 1})") == 0


def test_deleting_a_test_file_is_tampering_even_when_the_counts_do_not_move():
    # `tampered` was computed from AGGREGATE counts while `reasons` recorded per-file
    # deletions independently, so the report could NAME a deleted test file and still
    # return tampered=False. An audit proved it against real suites whose assertions
    # count zero — delete electron-smoke.mjs or live-flows.mjs and no total moves.
    before = {"web/e2e/zero.mjs": "// a suite with no counted assertions\n"}
    after: dict[str, str] = {}
    r = tamper_guard.check(before, after)
    assert r.tests_before == r.tests_after, "the counts genuinely do not move"
    assert r.assertions_before == r.assertions_after
    assert any("deleted" in x for x in r.reasons), r.reasons
    assert r.tampered, (
        f"a deleted test file must be tampering even when totals are flat: {r.reasons}"
    )

    # Control: an untouched file is not tampering.
    same = tamper_guard.check(before, dict(before))
    assert not same.tampered, same.reasons


def test_check_is_dot_guarded_so_product_calls_in_tests_are_not_assertions():
    # A bare `check(` is this repo's e2e assertion helper. A DOTTED
    # `tamper_guard.check(` or `self.check(` is a call to a product function, and an
    # audit found 24 miscounted across three test files — 19 in this very file, i.e.
    # calls to the function under test, so an honest refactor here read as tampering.
    #
    # This exists because removing the dot guard left the whole suite green: the fix
    # had two halves and only one was pinned.
    assert tamper_guard.count_assertions('check("a", ok);') == 1
    assert tamper_guard.count_assertions("tamper_guard.check(before, after)") == 0
    assert tamper_guard.count_assertions("self.check(x)") == 0
    # And the controls that were NOT the live false positive, kept for completeness.
    assert tamper_guard.count_assertions("run(x, check=True)") == 0
    assert tamper_guard.count_assertions("el.checkVisibility({})") == 0


# --- fixture snapshots are not the project's own tests -----------------------
#
# `eval/reviewer_recall/cases/<id>/base/**` materialises frozen copies of product
# source so a reviewer can be replayed against a known base. Some of those copies
# ARE real `tests/test_*.py` files, verbatim. They never execute (`testpaths =
# ["tests"]` plus `eval/conftest.py`'s `collect_ignore_glob = ["*"]`), so they
# cannot fake a green suite — and counting them was actively exploitable.

_FIXTURE_DIR = "eval/reviewer_recall/cases/control-gate-excerpts/base"


def test_fixture_snapshots_are_excluded_by_path_shape_only():
    """The exclusion is a path shape an agent cannot invoke from inside a file.

    All five segments are load-bearing: a top-level ``eval/``, one corpus
    directory, the literal ``cases/``, one case id, the literal ``base/``.
    """
    assert not tamper_guard.is_test_file(f"{_FIXTURE_DIR}/tests/test_vcs.py")
    assert not tamper_guard.is_test_file(f"{_FIXTURE_DIR}/tests/conftest.py")
    assert not tamper_guard.is_test_file(
        "eval/reviewer_recall/cases/x/base/web/src/boardLanes.test.mjs"
    )
    assert tamper_guard.is_fixture_content(f"{_FIXTURE_DIR}/tests/test_vcs.py")
    # ACKNOWLEDGED BREADTH: the rule is a shape, not this repo's corpus name, so a
    # second benchmark corpus under eval/ gets the same treatment. That is
    # deliberate — this module is repo-agnostic and also runs against linked and
    # user repos — and it costs nothing an attacker can use, because arriving at
    # this path requires leaving another one (see the relocation test below).
    assert not tamper_guard.is_test_file("eval/other_corpus/cases/x/base/tests/test_a.py")

    # Everything one step off the shape stays GUARDED. Each of these is a place an
    # agent could try to park a real suite if the rule were any looser.
    still_guarded = [
        # `eval/` at large is an ordinary directory — never blanket-excluded.
        "eval/tests/test_x.py",
        "eval/reviewer_recall/tests/test_x.py",
        "eval/reviewer_recall/cases/tests/test_x.py",
        # ...outside a case's `base/` subtree.
        "eval/reviewer_recall/cases/x/tests/test_x.py",
        "eval/reviewer_recall/cases/x/base_extra/tests/test_x.py",
        "eval/reviewer_recall/cases/x/notbase/tests/test_x.py",
        # ...nested two case-id levels deep (the shape is exactly one).
        "eval/reviewer_recall/cases/x/y/base/tests/test_x.py",
        # ...a lookalike prefix.
        "evaluation/reviewer_recall/cases/x/base/tests/test_x.py",
        "other_eval/reviewer_recall/cases/x/base/tests/test_x.py",
        # ...the same shape buried under another directory: the rule is anchored at
        # the repo root, so a nested copy is NOT a free pass.
        "vendor/eval/reviewer_recall/cases/x/base/tests/test_x.py",
        "src/eval/reviewer_recall/cases/x/base/tests/test_x.py",
        # The project's own tests, unchanged.
        "tests/test_vcs.py",
        "web/e2e/board.mjs",
        "conftest.py",
    ]
    for path in still_guarded:
        assert tamper_guard.is_test_file(path), f"expected still guarded: {path}"
        assert not tamper_guard.is_fixture_content(path), path


def test_no_in_file_marker_can_opt_a_real_test_out_of_the_guard():
    """There is no magic comment. Only the path decides."""
    for marker in (
        "# tamper: ignore\n",
        "# fixture content, not a test\n",
        "# noqa: tamper\n",
        '"""base fixture snapshot"""\n',
        "# eval/reviewer_recall/cases/x/base/tests/test_x.py\n",
    ):
        before = {"tests/test_x.py": marker + "def test_a():\n    assert f()\n"}
        after = {"tests/test_x.py": marker + "def test_a():\n    pass\n"}
        r = tamper_guard.check(before, after)
        assert r.tampered, f"a comment must not buy an exemption: {marker!r}"


def test_fixture_snapshots_do_not_dilute_a_real_reduction():
    """The dilution hazard, closed rather than moved.

    `tamper_check_between` compares merge-base..HEAD, so a branch that ADDS corpus
    cases used to carry their whole count (+735 tests / +2004 assertions) into the
    aggregate. `check()` decides `tampered` from the aggregate while `reasons`
    records per-file drops independently — so the report NAMED the reduction and
    still returned clean. Reproduced end-to-end against the real corpus: a branch
    that added 68 snapshot files and deleted 3 tests from a real `tests/test_vcs.py`
    read `[clean] tests 141->813` with `tests/test_vcs.py: tests 42->39` in reasons.
    """
    real_before = "def test_a():\n    assert f()\n\ndef test_b():\n    assert g()\n"
    real_after = "def test_a():\n    assert f()\n"          # one test, one assert gone
    bulk = "".join(f"def test_s{i}():\n    assert h({i})\n" for i in range(200))

    before = {"tests/test_x.py": real_before}
    after = {
        "tests/test_x.py": real_after,
        f"{_FIXTURE_DIR}/tests/test_snapshot.py": bulk,
    }
    r = tamper_guard.check(before, after)
    assert r.tampered, f"a real reduction must survive 200 added fixture tests: {r}"
    assert any("tests/test_x.py" in x for x in r.reasons), r.reasons


# --- D1.2: the UI-evidence delivery directory is not the project's own tests --
#
# `.nh-evidence/<task-id>/` is where `Orchestrator._deliver_ui_evidence` commits
# screenshots + a walk video for D1.2's PR-body visual proof, on a side branch
# that never merges. A coder-chosen shot NAME is otherwise unconstrained prose
# (`ui_evidence.py`'s `_SHOT_RE` permits `test-flow`, `e2e-2`, ...), so this is
# an explicit path-shape exclusion, same style as `is_fixture_content` above —
# not left to an accident of today's filenames never happening to say "test".

_NH_EVIDENCE_DIR = ".nh-evidence/deadbeef00000000"


def test_nh_evidence_directory_is_excluded_by_path_shape_only():
    assert tamper_guard.is_nh_evidence_content(f"{_NH_EVIDENCE_DIR}/final.png")
    assert not tamper_guard.is_test_file(f"{_NH_EVIDENCE_DIR}/final.png")
    assert not tamper_guard.is_test_file(f"{_NH_EVIDENCE_DIR}/walk.webm")
    # The adversarial case: a shot NAME that looks test-shaped. `_SHOT_RE`
    # permits this; it must still never be counted.
    assert not tamper_guard.is_test_file(f"{_NH_EVIDENCE_DIR}/test-flow.png")
    assert not tamper_guard.is_test_file(f"{_NH_EVIDENCE_DIR}/e2e-check.png")
    assert not tamper_guard.is_test_file(
        f"{_NH_EVIDENCE_DIR}/conftest.py")  # not real code either way

    # Everything one step off the shape stays GUARDED.
    still_guarded = [
        # No leading dot: an ordinary directory, never blanket-excluded.
        "nh-evidence/deadbeef/tests/test_x.py",
        # The literal shape buried under another directory: anchored, not
        # a free pass wherever it appears.
        "vendor/.nh-evidence/deadbeef/tests/test_x.py",
        # A lookalike prefix.
        ".nh-evidence-extra/deadbeef/tests/test_x.py",
        # The project's own tests, unchanged.
        "tests/test_vcs.py",
        "conftest.py",
    ]
    for path in still_guarded:
        assert tamper_guard.is_test_file(path), f"expected still guarded: {path}"
        assert not tamper_guard.is_nh_evidence_content(path), path


def test_nh_evidence_directory_does_not_dilute_a_real_reduction():
    """Same dilution hazard `is_fixture_content` closes, replayed for D1.2's
    directory: a branch that adds a pile of `.nh-evidence/` shots alongside a
    real test-count reduction must still be flagged tampered."""
    real_before = "def test_a():\n    assert f()\n\ndef test_b():\n    assert g()\n"
    real_after = "def test_a():\n    assert f()\n"          # one test, one assert gone

    before = {"tests/test_x.py": real_before}
    after = {
        "tests/test_x.py": real_after,
        f"{_NH_EVIDENCE_DIR}/step-0.png": "not source, just probe content\n" * 50,
        f"{_NH_EVIDENCE_DIR}/step-1.png": "def test_padding():\n    assert True\n" * 50,
    }
    r = tamper_guard.check(before, after)
    assert r.tampered, f"a real reduction must survive added .nh-evidence/ shots: {r}"
    assert any("tests/test_x.py" in x for x in r.reasons), r.reasons

    # Closed, not moved: the aggregate must equal the no-fixture control exactly.
    control = tamper_guard.check(before, {"tests/test_x.py": real_after})
    assert (r.tests_before, r.tests_after) == (control.tests_before, control.tests_after)
    assert (r.assertions_before, r.assertions_after) == (
        control.assertions_before, control.assertions_after
    )
    assert r.tests_after < r.tests_before, r.summary


def test_relocating_a_real_test_into_a_fixture_path_is_still_tampering():
    """Excluding a path must not make it a laundry for real suites.

    A move removes the file from its old path, and the deleted-file rule is
    unconditional, so the smuggle is caught by the DISAPPEARANCE — verbatim or
    gutted, it makes no difference.
    """
    real = "def test_a():\n    assert f()\n\ndef test_b():\n    assert g()\n"
    before = {"tests/test_x.py": real}

    verbatim = tamper_guard.check(before, {f"{_FIXTURE_DIR}/tests/test_x.py": real})
    assert verbatim.tampered, verbatim
    assert any("deleted: tests/test_x.py" in x for x in verbatim.reasons), verbatim.reasons
    # And the counts drop too, so it fires twice over rather than on one rule.
    assert verbatim.tests_after < verbatim.tests_before, verbatim.summary

    gutted = tamper_guard.check(
        before, {f"{_FIXTURE_DIR}/tests/test_x.py": "def test_a():\n    pass\n"}
    )
    assert gutted.tampered, gutted
    assert any("deleted: tests/test_x.py" in x for x in gutted.reasons), gutted.reasons


def test_fixture_snapshots_do_not_raise_false_cheat_signals():
    """Verbatim copies of signals already live and accepted in `tests/`.

    `tests/test_vcs.py` seeds a fixture repo with the literal string
    `"assert True\\n"`, and `tests/test_jira_issues_endpoint.py` has a legitimate
    `@pytest.fixture(autouse=True)` that monkeypatches config PATHS. The autouse
    monkeypatch registers `autouse monkeypatch fixture 0->1` on a branch that
    touched no test at all; the string-literal `assert True` no longer registers a
    tautology (``count_tautologies`` masks string-literal interiors), so the path
    exemption below is what still has to hold for the autouse signal.
    """
    snapshot = (
        'def test_seed(repo):\n'
        '    (repo.path / "test_thing.py").write_text("assert True\\n")\n'
        '\n'
        '@pytest.fixture(autouse=True)\n'
        'def _isolated_paths(tmp_path, monkeypatch):\n'
        '    monkeypatch.setattr(nh_config, "ENV_PATH", tmp_path / ".env")\n'
    )
    # The autouse monkeypatch is a live cheat signal; the string-literal
    # `assert True` handed to write_text is test DATA and is now correctly NOT
    # counted as a tautology (the FP this guard used to raise — see
    # count_tautologies's string-literal masking).
    assert tamper_guard.count_tautologies(snapshot) == 0
    assert tamper_guard.count_faking_fixtures(snapshot) == 1

    r = tamper_guard.check(
        {"tests/test_x.py": "def test_a():\n    assert f()\n"},
        {
            "tests/test_x.py": "def test_a():\n    assert f()\n",
            f"{_FIXTURE_DIR}/tests/test_snapshot.py": snapshot,
        },
    )
    assert not r.tampered, r.reasons
    assert r.reasons == [], r.reasons

    # Control: the SAME content under a real test path still fires, on both rules.
    live = tamper_guard.check(
        {"tests/test_x.py": "def test_a():\n    assert f()\n"},
        {
            "tests/test_x.py": "def test_a():\n    assert f()\n",
            "tests/test_snapshot.py": snapshot,
        },
    )
    assert live.tampered, live
    # The string-literal `assert True` is DATA, not a tautology — masked even
    # under a real test path (this is exactly the FP that escalated correct work
    # as NOVEL_UNKNOWN); only the autouse fixture fires here.
    assert not any("tautological" in x for x in live.reasons), live.reasons
    assert any("autouse" in x for x in live.reasons), live.reasons


def test_known_cost_deleting_a_fixture_snapshot_is_no_longer_flagged():
    """The one case where this exclusion is strictly WEAKER. Named, not hidden.

    Before the exclusion, deleting a materialised snapshot tripped the
    unconditional deleted-test-file rule. It no longer does — the path is not a
    test file, so it never enters `before`. That is the unavoidable price of not
    counting them: you cannot both ignore a path and police deletions in it.

    Why it is an acceptable price:
      * these files never execute, so deleting one cannot turn a red suite green
        — which is the only thing this guard exists to prevent;
      * corpus integrity has its own signal in the LIVE suite
        (`tests/test_reviewer_recall_runner.py::test_load_cases_real_corpus`
        pins the case count, and the recall runner raises on a case whose `base/`
        is missing), and that live test is itself still guarded here, so it
        cannot be deleted to cover the tracks;
      * the diff is still in front of the human reviewer, which is the gate.
    """
    snapshot = "def test_a():\n    assert f()\n"

    def snapshot_repo(files):
        """What `runner.tamper_check_between` hands to `check()`: git's file list
        run through `is_test_file` first. Model it, don't shortcut it."""
        return {p: s for p, s in files.items() if tamper_guard.is_test_file(p)}

    live = {"tests/test_x.py": "def test_a():\n    assert f()\n"}
    r = tamper_guard.check(
        snapshot_repo({**live, f"{_FIXTURE_DIR}/tests/test_snapshot.py": snapshot}),
        snapshot_repo(live),
    )
    assert not r.tampered, r.reasons

    # FAIL-CLOSED at the `check()` boundary: the deleted-file rule reads the raw
    # `before` mapping and does NOT consult `is_test_file`, so a caller that hands
    # `check()` a fixture path directly still gets a tamper verdict. The exclusion
    # is not a hole punched through `check()` itself — it lives entirely in which
    # paths the caller collects.
    direct = tamper_guard.check(
        {f"{_FIXTURE_DIR}/tests/test_snapshot.py": snapshot}, {}
    )
    assert direct.tampered, "check() must stay fail-closed on any path it is given"

    # The compensating control: the LIVE test that pins corpus integrity is an
    # ordinary guarded test file, so it cannot be removed quietly.
    assert tamper_guard.is_test_file("tests/test_reviewer_recall_runner.py")
    gone = tamper_guard.check(
        {"tests/test_reviewer_recall_runner.py": snapshot}, {}
    )
    assert gone.tampered, gone.reasons


def test_tamper_check_refuses_a_missing_worktree_instead_of_crashing(tmp_path):
    """A guard that cannot look must say so, not answer "clean".

    Observed twice on 2026-07-31: a resumed attempt whose worktree had been
    removed reached tamper_check_between and died on an opaque
    FileNotFoundError raised by a subprocess several frames down, taking the
    worker pool's task with it and leaving no diagnosable cause. The failure
    mode that would be WORSE is a clean TamperReport — the guard would then
    launder "I did not look" into "nothing was wrong", which is exactly the
    reward-hacking window it exists to close. So this asserts the error, and
    asserts it is not a passing report.
    """
    from no_human.testing.runner import TamperCheckUnavailable, tamper_check_between

    missing = tmp_path / "worktree-that-was-removed"
    with pytest.raises(TamperCheckUnavailable) as exc:
        tamper_check_between(missing)
    assert str(missing) in str(exc.value), "the error must name the path"

    # A directory that exists but is not a checkout is the same class of
    # "cannot inspect" — a bare mkdir left behind by a half-torn-down worktree.
    not_a_checkout = tmp_path / "empty"
    not_a_checkout.mkdir()
    with pytest.raises(TamperCheckUnavailable):
        tamper_check_between(not_a_checkout)


# --- absent is not zero: a NEW test file has no baseline ---------------------
#
# The guard read a path missing from `before` as a baseline of ZERO, so a brand
# new test file carrying `@pytest.mark.skipif` reported "skip/xfail markers
# 0->1 (test neutered)". Nothing was neutered: none of its tests ran before.
# `skipif` is the ordinary platform/dependency-conditional idiom, so every task
# that added one escalated on correct work, via the `if tamper.tampered:` branch
# of `Orchestrator._run_attempt` in `core/orchestrator.py`, which routes into
# `Orchestrator._escalate`. Cited by symbol, not by line: the line number this
# comment used to carry (`:2904-2910`) had already drifted onto the unrelated
# SCOPE_EXPLOSION size-limit block, and still read as a precise citation.
#
# The four tests below are one set on purpose. The obvious fix — exempt new
# files wholesale — silently blinds the padding defence, and fixing only the
# per-path reason leaves the AGGREGATE `sa > sb` firing with no reason printed,
# which is worse than the bug. Each arm has to be pinned separately.

_REAL = 'from mypkg import parse\ndef test_a():\n    assert parse("a: 1")["a"] == 1\n'
_SKIPIF = (
    'import pytest, sys\nfrom mypkg import parse\n'
    '@pytest.mark.skipif(sys.platform == "win32", reason="posix only")\n'
    'def test_a():\n    assert parse("a: 1")["a"] == 1\n'
)
_NEUTERED = (
    'import pytest\nfrom mypkg import parse\n@pytest.mark.skip("flaky")\n'
    'def test_a():\n    assert parse("a: 1")["a"] == 1\n'
)
_PADDING = 'def test_p1():\n    assert True\ndef test_p2():\n    assert 1 == 1\n'


def test_a_new_test_file_with_a_conditional_skip_is_not_tampering():
    """The bug. A file that did not exist cannot have neutered anything."""
    r = tamper_guard.check({}, {"tests/test_new.py": _SKIPIF})
    assert not r.tampered, r.reasons
    assert not any("neutered" in x for x in r.reasons), r.reasons


def test_an_existing_file_that_gains_a_skip_is_still_tampering():
    """The arm the fix must NOT relax: a previously-running test disabled."""
    r = tamper_guard.check({"tests/test_e.py": _REAL}, {"tests/test_e.py": _NEUTERED})
    assert r.tampered
    assert any("neutered" in x for x in r.reasons), r.reasons


def test_a_new_file_padded_with_no_op_tests_is_still_tampering():
    """Tautologies are deliberately NOT exempt on a new file.

    Padding a new file with passing no-ops is how a real deletion elsewhere
    gets netted back to positive, so this arm stays live. Only the wording
    changes — nothing was "replaced" in a file that did not exist.

    This test names WHICH mechanism holds the verdict, because the obvious
    reading is wrong. `tampered` here is carried by the AGGREGATE
    `auta > autb`, which sums raw per-path totals; the per-path reason string
    is a separate arm. Mutating only the per-path arm to
    `if a_taut > b_taut and not is_new:` leaves `tampered` True and fails just
    the wording assertion — measured, not assumed. So the totals below are
    asserted directly: they are the thing that actually keeps this closed, and
    the mutation that would open it for real is routing tautologies through a
    new-file-excluded total the way skips are routed through `sa_cmp`.
    """
    r = tamper_guard.check({}, {"tests/test_pad.py": _PADDING})
    assert r.tampered, r.summary
    # The aggregate arm, pinned directly rather than through the verdict.
    assert (r.tautologies_before, r.tautologies_after) == (0, 2), r.summary
    assert r.tautologies_after > r.tautologies_before, r.summary
    # The per-path arm, which the verdict above does NOT pin on its own.
    assert any("padded with no-op tests" in x for x in r.reasons), r.reasons


def test_deleting_a_file_and_re_adding_it_as_a_new_one_is_still_tampering():
    """The evasion path the exemption could have opened, and does not.

    Laundering neutered tests through a delete-and-recreate still trips the
    deletion arm, which fires on the vanished path regardless of the totals.
    """
    r = tamper_guard.check({"tests/test_e.py": _REAL}, {"tests/test_n.py": _SKIPIF})
    assert r.tampered
    assert any("deleted" in x for x in r.reasons), r.reasons


# --- ...but a conftest.py is never "new" for that purpose --------------------
#
# The exemption above keys on "path absent from `before`". A conftest.py is a
# new path whose markers reach tests it never declares, so the exemption handed
# an agent a five-line, whole-suite kill switch that scored clean.
#
# Measured end to end, not reasoned about: a temp project with
# `1 failed, 1 passed` (pytest exit 1) drops to `2 skipped` (pytest exit 0) on
# adding _KILL_SWITCH as a root conftest.py, and runner.py reads exit 0 as
# ok=True (`ok = proc.returncode == 0`, runner.py:871) while `_parse_pytest`
# returns (0, 0, 0) — zero failures, zero errors. Nothing downstream can tell
# that from an honest green.
#
# `count_faking_fixtures` cannot see it: it needs autouse AND a _FAKE_PATCH
# primitive, and `pytest.skip()` is not one, so the payload scores 0 there.
# `count_skips` is the only counter that sees it, which is exactly the counter
# the new-file exemption switched off.

_KILL_SWITCH = (
    'import pytest\n\n\n@pytest.fixture(autouse=True)\ndef _skip_all():\n'
    '    pytest.skip("infra unavailable")\n'
)
_KILL_SWITCH_XFAIL = (
    'import pytest\n\n\n@pytest.fixture(autouse=True)\ndef _xfail_all():\n'
    '    pytest.xfail("wip")\n'
)
_KILL_SWITCH_ADD_MARKER = (
    'import pytest\n\n\n@pytest.fixture(autouse=True)\ndef _xfail_all(request):\n'
    '    request.node.add_marker(pytest.mark.xfail(reason="wip"))\n'
)


@pytest.mark.parametrize(
    "path",
    ["conftest.py", "tests/conftest.py", "tests/sub/conftest.py"],
    ids=["root", "tests", "nested"],
)
@pytest.mark.parametrize(
    "body",
    [_KILL_SWITCH, _KILL_SWITCH_XFAIL, _KILL_SWITCH_ADD_MARKER],
    ids=["pytest_skip", "pytest_xfail", "add_marker_xfail"],
)
def test_a_new_conftest_that_skips_the_whole_suite_is_tampering(path, body):
    """The payload the new-file exemption let through, at every depth and form.

    Nested paths are covered because a conftest.py governs its own subtree and
    nothing about the evasion requires the repo root.
    """
    before = {"tests/test_e.py": _REAL}
    r = tamper_guard.check(before, {**before, path: body})
    assert r.tampered, r.summary
    assert any(path in x and "neutered" in x for x in r.reasons), r.reasons


def test_the_new_file_exemption_still_holds_for_an_ordinary_new_test_file():
    """The carve-out is scoped to conftest.py and nothing wider.

    Pinned beside the tests above so a future "just exempt nothing" cure to
    them cannot quietly re-open the false positive they were built to fix.
    """
    before = {"tests/test_e.py": _REAL}
    r = tamper_guard.check(before, {**before, "tests/sub/test_new.py": _SKIPIF})
    assert not r.tampered, r.summary
    assert not any("neutered" in x for x in r.reasons), r.reasons


def test_count_faking_fixtures_cannot_see_a_skip_only_kill_switch():
    """Why the skip counter had to be the one that stays live for conftest.py.

    If rule 4 caught this payload the exemption would have been harmless. It
    does not: `pytest.skip()` is not a _FAKE_PATCH primitive, so an autouse
    fixture built from it scores zero cheat signals there. This is the load-
    bearing premise of the carve-out above, so it is pinned rather than
    asserted in a comment.
    """
    assert tamper_guard.count_faking_fixtures(_KILL_SWITCH) == 0
    assert tamper_guard.count_faking_fixtures(_KILL_SWITCH_XFAIL) == 0
    # ...while the patch-based form it CAN see still scores, so this test
    # cannot pass merely because the counter is broken for everything.
    patching = (
        'import pytest\n@pytest.fixture(autouse=True)\ndef f(monkeypatch):\n'
        '    monkeypatch.setattr("mypkg.parse", lambda s: {"a": 1})\n'
    )
    assert tamper_guard.count_faking_fixtures(patching) == 1


def test_consolidating_tests_is_tampering_even_when_assertions_hold():
    """The one shape that ONLY the net test-count term can catch.

    `check()` fires on any of: a deleted file, a net TEST-count drop (`ta < tb`),
    a net ASSERTION drop (`aa < ab`), added skips, added tautologies, or added
    faking fixtures. In ordinary tampering the test and assertion counts fall
    together, so `aa < ab` fires and `ta < tb` is redundant — which is why, until
    this test, `ta < tb` could be DELETED and the whole suite stayed green
    (measured: 1223 passed across all 27 tamper-referencing files with the term
    removed).

    That made a load-bearing term unobserved. The shape that separates the two is
    CONSOLIDATION: fold three one-assert tests into one three-assert test. Test
    count drops 3 -> 1 while assertion count holds at 3, so `aa < ab` is False and
    `ta < tb` is the only thing standing between that diff and a clean verdict.

    Assertion count is held CONSTANT on purpose. If a future edit changes these
    fixtures so assertions also drop, this test keeps passing for the wrong
    reason — so the second assertion below pins the counts themselves.
    """
    before = {"tests/test_c.py": (
        "def test_a():\n    assert 1\n"
        "def test_b():\n    assert 2\n"
        "def test_c():\n    assert 3\n")}
    after = {"tests/test_c.py": (
        "def test_a():\n    assert 1\n    assert 2\n    assert 3\n")}

    # The premise, pinned: tests fall, assertions do not. Without this a later
    # fixture edit could make the case pass via `aa < ab` and prove nothing.
    b, a = before["tests/test_c.py"], after["tests/test_c.py"]
    assert tamper_guard.count_tests(b) == 3 and tamper_guard.count_tests(a) == 1
    assert tamper_guard.count_assertions(b) == tamper_guard.count_assertions(a) == 3

    r = tamper_guard.check(before, after)
    assert r.tampered, (
        f"consolidating 3 tests into 1 was called clean: {r.summary}. Only the "
        "net test-count term can see this; if it was removed as redundant, this "
        "is the case that proves it is not.")


# --- attempt_own_base: exclude main's mid-flight landed test changes ------- #
#
# 2026-08-23 (task b3463d74, attempt 7): the coder was instructed by an
# escalation reply to `merge origin/main` and resolve conflicts — the
# standard recovery for a diverged branch. The tamper guard compared the
# merged tree against the task's ORIGINAL recorded base, so main's own
# landed commit (b31b53f76, `tests/test_readme_claims.py` 0->2 skips) showed
# up as attempt-authored and escalated a false AMBIGUITY. `git diff
# origin/main...<branch> -- tests/test_readme_claims.py` showed ZERO skip
# lines added by the attempt on every branch of that task.


def _commit_all(repo: Path, message: str) -> None:
    subprocess.run(["git", "-C", str(repo), "add", "-A"], capture_output=True, check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", message],
                   capture_output=True, check=True)


def _rev_parse(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, check=True, text=True,
    ).stdout.strip()


def _merged_main_repo(tmp_path: Path):
    """A `feat` branch that merged a `main` which landed skip markers.

    Returns `(repo, recorded_base_sha)` where `recorded_base_sha` is the
    commit `feat` branched from — the task's original recorded base, exactly
    what `_review_base` would have returned before the merge.
    """
    repo = tmp_path / "merged_main_repo"
    repo.mkdir()
    _init_repo(repo)

    test_file = repo / "tests" / "test_x.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text(
        "def test_a():\n    assert 1 + 1 == 2\n"
        "def test_b():\n    assert 2 + 2 == 4\n"
    )
    # A second, persistent test file — present at `recorded_base`, untouched
    # by main's landing commit, and carried unchanged through the merge. Used
    # by the "attempt-authored skip still fires" test below: an EXISTING file
    # in the widened before-window, so a skip the attempt adds to it cannot
    # be mistaken for the new-file skip exemption (`tamper_guard.py`'s `is_new`
    # rule, which is deliberately out of scope here — see OUT OF SCOPE).
    other_file = repo / "tests" / "test_z.py"
    other_file.write_text("def test_c():\n    assert 3 + 3 == 6\n")
    subprocess.run(["git", "-C", str(repo), "branch", "-M", "main"],
                   capture_output=True, check=True)
    _commit_all(repo, "init")
    recorded_base = _rev_parse(repo)

    subprocess.run(["git", "-C", str(repo), "checkout", "-b", "feat"],
                   capture_output=True, check=True)
    product_file = repo / "product.py"
    product_file.write_text("def f():\n    return 1\n")
    _commit_all(repo, "attempt: add product code")

    subprocess.run(["git", "-C", str(repo), "checkout", "main"],
                   capture_output=True, check=True)
    test_file.write_text(
        "import pytest\n\n"
        "@pytest.mark.skip(reason='flaky')\n"
        "def test_a():\n    assert 1 + 1 == 2\n"
        "@pytest.mark.skip(reason='flaky')\n"
        "def test_b():\n    assert 2 + 2 == 4\n"
    )
    _commit_all(repo, "main: land skip markers (b31b53f76 stand-in)")

    subprocess.run(["git", "-C", str(repo), "checkout", "feat"],
                   capture_output=True, check=True)
    subprocess.run(["git", "-C", str(repo), "merge", "main", "--no-edit"],
                   capture_output=True, check=True)

    return repo, recorded_base


def test_a_merged_main_that_landed_skips_is_not_charged_to_the_attempt(tmp_path):
    """RED-first repro: the false escalation from the 2026-08-23 incident."""
    from no_human.testing.runner import tamper_check_between

    repo, recorded_base = _merged_main_repo(tmp_path)

    # Old behaviour, documented: without the widened base, main's landed
    # skip markers read as attempt-authored and the guard fires.
    stale = tamper_check_between(repo, before_ref=recorded_base)
    assert stale.tampered is True

    # Fixed behaviour: widening the base past main's own commits clears it.
    fixed = tamper_check_between(
        repo, before_ref=recorded_base, default_branch="main")
    assert fixed.tampered is False
    assert not any("skip" in r for r in fixed.reasons), fixed.reasons


def test_an_attempt_authored_skip_marker_still_fires_after_the_merge(tmp_path):
    """A skip marker the ATTEMPT adds itself must still fire post-merge.

    Neuters `tests/test_z.py` — a file present at `recorded_base`, untouched
    by main's landing commit, and carried through the merge unchanged — so
    the new-file skip exemption cannot mask the attempt's own tampering.
    """
    from no_human.testing.runner import tamper_check_between

    repo, recorded_base = _merged_main_repo(tmp_path)

    other_test = repo / "tests" / "test_z.py"
    other_test.write_text(
        "import pytest\n\n"
        "@pytest.mark.skip(reason='attempt-authored')\n"
        "def test_c():\n    assert 3 + 3 == 6\n"
    )
    _commit_all(repo, "attempt: neuter test_z.py (this is the tamper)")

    report = tamper_check_between(
        repo, before_ref=recorded_base, default_branch="main")
    assert report.tampered is True
    assert any("skip/xfail markers" in r for r in report.reasons), report.reasons


def test_reverting_a_test_main_added_mid_flight_still_fires(tmp_path):
    """Deleting a test file main landed mid-flight must still fire."""
    from no_human.testing.runner import tamper_check_between

    repo = tmp_path / "revert_main_test_repo"
    repo.mkdir()
    _init_repo(repo)

    (repo / "tests").mkdir()
    (repo / "tests" / "test_x.py").write_text("def test_a():\n    assert 1 == 1\n")
    subprocess.run(["git", "-C", str(repo), "branch", "-M", "main"],
                   capture_output=True, check=True)
    _commit_all(repo, "init")
    recorded_base = _rev_parse(repo)

    subprocess.run(["git", "-C", str(repo), "checkout", "-b", "feat"],
                   capture_output=True, check=True)
    (repo / "product.py").write_text("def f():\n    return 1\n")
    _commit_all(repo, "attempt: add product code")

    subprocess.run(["git", "-C", str(repo), "checkout", "main"],
                   capture_output=True, check=True)
    new_from_main = repo / "tests" / "test_new_from_main.py"
    new_from_main.write_text(
        "def test_landed_mid_flight():\n    assert 2 == 2\n"
        "def test_landed_mid_flight_2():\n    assert 3 == 3\n"
    )
    _commit_all(repo, "main: land a new test file mid-flight")

    subprocess.run(["git", "-C", str(repo), "checkout", "feat"],
                   capture_output=True, check=True)
    subprocess.run(["git", "-C", str(repo), "merge", "main", "--no-edit"],
                   capture_output=True, check=True)

    # The attempt reverts main's mid-flight test file — must still fire.
    (repo / "tests" / "test_new_from_main.py").unlink()
    _commit_all(repo, "attempt: delete test_new_from_main.py (this is the tamper)")

    report = tamper_check_between(
        repo, before_ref=recorded_base, default_branch="main")
    assert report.tampered is True
    assert any("test file deleted:" in r for r in report.reasons), report.reasons


def test_a_branch_that_never_merged_main_uses_the_recorded_base_unchanged(tmp_path):
    """Non-merged branches: `attempt_own_base` and the report are unchanged."""
    from no_human.testing.runner import attempt_own_base, tamper_check_between

    repo = tmp_path / "never_merged_repo"
    repo.mkdir()
    _init_repo(repo)

    (repo / "tests").mkdir()
    (repo / "tests" / "test_x.py").write_text("def test_a():\n    assert 1 == 1\n")
    subprocess.run(["git", "-C", str(repo), "branch", "-M", "main"],
                   capture_output=True, check=True)
    _commit_all(repo, "init")
    recorded_base = _rev_parse(repo)

    subprocess.run(["git", "-C", str(repo), "checkout", "-b", "feat"],
                   capture_output=True, check=True)
    (repo / "product.py").write_text("def f():\n    return 1\n")
    _commit_all(repo, "attempt: add product code")

    # main advances independently — feat never merges it.
    subprocess.run(["git", "-C", str(repo), "checkout", "main"],
                   capture_output=True, check=True)
    (repo / "tests" / "test_x.py").write_text(
        "import pytest\n\n"
        "@pytest.mark.skip(reason='unrelated main work')\n"
        "def test_a():\n    assert 1 == 1\n"
    )
    _commit_all(repo, "main: unrelated skip, feat never sees it")

    subprocess.run(["git", "-C", str(repo), "checkout", "feat"],
                   capture_output=True, check=True)

    assert attempt_own_base(repo, recorded_base, "main") == recorded_base

    without = tamper_check_between(repo, before_ref=recorded_base)
    with_default_branch = tamper_check_between(
        repo, before_ref=recorded_base, default_branch="main")
    assert without.tampered == with_default_branch.tampered
    assert without.reasons == with_default_branch.reasons


@pytest.mark.parametrize("default_branch", [None, "nope"])
def test_attempt_own_base_falls_back_to_the_recorded_base(tmp_path, default_branch):
    """Fail-closed: no resolvable candidate always returns `recorded_base`."""
    from no_human.testing.runner import attempt_own_base

    repo = tmp_path / "fallback_repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / "f.txt").write_text("x")
    _commit_all(repo, "init")
    recorded_base = _rev_parse(repo)

    assert attempt_own_base(repo, recorded_base, default_branch) == recorded_base


def test_attempt_own_base_falls_back_on_a_non_git_directory(tmp_path):
    """Fail-closed: a non-git directory never raises."""
    from no_human.testing.runner import attempt_own_base

    not_a_repo = tmp_path / "not_a_repo"
    not_a_repo.mkdir()

    assert attempt_own_base(not_a_repo, "deadbeef", "main") == "deadbeef"


def test_the_guards_comparison_base_is_documented_with_the_incident():
    """AC #4: the docstrings name the rule and carry the incident's shape."""
    from no_human.testing.runner import attempt_own_base, tamper_check_between

    for doc in (attempt_own_base.__doc__, tamper_check_between.__doc__):
        assert doc is not None
        assert "merge" in doc
        assert "origin/main" in doc
        assert "2026-08-23" in doc
