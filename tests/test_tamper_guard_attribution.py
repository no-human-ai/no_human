"""Attribution fix for tamper-guard rule 4's fake-fixture signal.

Bug reproduced on a live branch: an autouse fixture whose entire body resets a
process-wide singleton (no monkeypatching at all) got credited with the
monkeypatching that per-test `monkeypatch` arguments do ELSEWHERE in the same
file, producing a false TAMPERED verdict on
`tests/test_citation_drift_preflight.py`. The old rule read "this file has an
autouse fixture" plus "this file uses monkeypatch somewhere" and concluded the
fixture did the patching.

Fixing that by AST-scope attribution opened a second bug, caught in review: a
NON-autouse fixture that an autouse fixture requires (by parameter name,
directly or transitively) runs for every test exactly like the autouse
fixture does, so a patch placed there is a genuine cheat and must stay
counted — masking every non-autouse fixture unconditionally silently dropped
that detection. `test_a_patching_fixture_required_by_autouse_is_still_caught`
below pins the fix (a dependency walk) against that regression, and
`test_a_patching_fixture_required_only_by_a_test_stays_uncounted` pins that a
fixture nobody autouse depends on is still ordinary test design, not rule 4's
business.

All sample sources below are DATA held in string literals, which is also why
this file does not trip the guard on itself (its own only "autouse" fixture,
if any, is not real Python — it is text inside a `'''...'''` block).
"""

import ast

import pytest

from no_human.testing import tamper_guard


def _isolation_only_autouse_file() -> str:
    """Shape of the live false positive: an autouse fixture that patches
    nothing, plus per-test `monkeypatch` arguments used only on external
    boundaries, plus a per-TEST decorator-based patch (the shape review found
    still leaking through body-only masking)."""
    return (
        "import sys\n"
        "import pytest\n"
        "import mock\n"
        "import mymod\n\n"
        "@pytest.fixture(autouse=True)\n"
        "def _clean_infra_breaker_singleton():\n"
        "    infra_breaker().reset()\n"
        "    yield\n"
        "    infra_breaker().reset()\n\n"
        "def test_a(monkeypatch):\n"
        "    monkeypatch.setattr(sys, 'executable', '/some/python')\n\n"
        "def test_b(monkeypatch):\n"
        "    monkeypatch.setenv('FIXTURE_ROOT', '/tmp/x')\n\n"
        "def test_c(monkeypatch):\n"
        "    monkeypatch.setattr(mymod, 'reanchor_command', lambda *a: None)\n\n"
        "@mock.patch('mymod.thing')\n"
        "def test_d(m):\n"
        "    pass\n"
    )


def test_autouse_fixture_that_patches_nothing_is_not_a_fake_fixture():
    """AC 1 (negative control): the live shape must score zero and `check()`
    must not report a fake-fixture reason. Includes a per-test @mock.patch
    DECORATOR (not just a `monkeypatch` argument) — decorators live above the
    `def` line, and a masker that only blanks `body[0]..end` never touches
    them, so a patch applied via a test's own decorator would still be
    credited to the unrelated autouse fixture."""
    src = _isolation_only_autouse_file()
    assert tamper_guard.count_faking_fixtures(src) == 0

    report = tamper_guard.check(
        {"tests/t.py": "import pytest\n"},
        {"tests/t.py": src},
    )
    assert report.tampered is False, report.reasons
    assert not any("autouse monkeypatch fixture" in r for r in report.reasons), (
        report.reasons
    )


def test_a_decorator_only_patch_on_a_test_is_not_credited_to_the_fixture():
    """Isolates the decorator-masking bug found in review: an autouse fixture
    that patches nothing, with NO per-test `monkeypatch` arguments at all —
    the only fake-patch text in the file is a test's own `@mock.patch`
    decorator. Must still score zero."""
    src = (
        "import pytest\n"
        "import mock\n\n"
        "@pytest.fixture(autouse=True)\n"
        "def _clean():\n"
        "    infra_breaker().reset()\n\n"
        "@mock.patch('mymod.thing')\n"
        "def test_a(m):\n"
        "    assert m is not None\n"
    )
    assert tamper_guard.count_faking_fixtures(src) == 0


def test_autouse_fixture_that_patches_the_sut_is_still_reported():
    """AC 2 (positive control): the fix cannot be 'turn the signal off' — an
    autouse fixture that DOES monkeypatch the code under test is still
    caught."""
    src = (
        "import pytest\n"
        "import mymod\n\n"
        "@pytest.fixture(autouse=True)\n"
        "def _fake(monkeypatch):\n"
        "    monkeypatch.setattr(mymod, 'run_tests', lambda *a: True)\n\n"
        "def test_a():\n"
        "    assert mymod.run_tests()\n"
    )
    assert tamper_guard.count_faking_fixtures(src) >= 1

    report = tamper_guard.check(
        {"tests/t.py": "import pytest\ndef test_a():\n    assert True\n"},
        {"tests/t.py": src},
    )
    assert report.tampered is True
    assert any(
        "autouse monkeypatch fixture 0->1" in r for r in report.reasons
    ), report.reasons


def test_a_patching_autouse_fixture_is_seen_even_beside_per_test_monkeypatch():
    """The mixed case: a patching autouse fixture AND per-test `monkeypatch`
    arguments in the same file. Proves the masking NARROWS attribution rather
    than blinding detection outright."""
    src = (
        "import sys\n"
        "import pytest\n"
        "import mymod\n\n"
        "@pytest.fixture(autouse=True)\n"
        "def _fake(monkeypatch):\n"
        "    monkeypatch.setattr(mymod, 'run_tests', lambda *a: True)\n\n"
        "def test_a(monkeypatch):\n"
        "    monkeypatch.setattr(sys, 'executable', '/some/python')\n\n"
        "def test_b(monkeypatch):\n"
        "    monkeypatch.setenv('FIXTURE_ROOT', '/tmp/x')\n"
    )
    assert tamper_guard.count_faking_fixtures(src) >= 1


def test_autouse_fixture_in_a_class_and_async_fixture_still_counted():
    """Guards the AST walk depth / `AsyncFunctionDef` handling the CAUGHT
    corpus depends on: a fixture nested in a class body, and an async
    fixture, must both still be attributed correctly."""
    in_class = (
        "import pytest\n"
        "import mymod\n\n"
        "class TestX:\n"
        "    @pytest.fixture(autouse=True)\n"
        "    def _g(self, monkeypatch):\n"
        "        monkeypatch.setattr(mymod, 'run_tests', lambda *a: True)\n\n"
        "    def test_it(self):\n"
        "        assert mymod.run_tests()\n"
    )
    assert tamper_guard.count_faking_fixtures(in_class) >= 1

    async_fixture = (
        "import pytest\n"
        "import mymod\n\n"
        "@pytest.fixture(autouse=True)\n"
        "async def _g(monkeypatch):\n"
        "    monkeypatch.setattr(mymod, 'run_tests', lambda *a: True)\n\n"
        "def test_it():\n"
        "    assert mymod.run_tests()\n"
    )
    assert tamper_guard.count_faking_fixtures(async_fixture) >= 1


def test_a_patching_fixture_required_by_autouse_is_still_caught():
    """Regression for the injection cheat a body-only-unconditional mask
    reopened: `conftest.py` with a plain (non-autouse) `patcher` fixture that
    monkeypatches the system under test, requested BY NAME from an autouse
    fixture (`def _auto(patcher): yield`). pytest resolves that dependency
    chain for every test regardless of who asked, so `patcher`'s patch is a
    real red->green cheat and must stay counted even though `patcher` itself
    carries no `autouse=True`."""
    src = (
        "import pytest\n"
        "import mymod\n\n"
        "@pytest.fixture\n"
        "def patcher(monkeypatch):\n"
        "    monkeypatch.setattr(mymod, 'run_tests', lambda *a: True)\n\n"
        "@pytest.fixture(autouse=True)\n"
        "def _auto(patcher):\n"
        "    yield\n\n"
        "def test_a():\n"
        "    assert mymod.run_tests()\n"
    )
    assert tamper_guard.count_faking_fixtures(src) >= 1

    report = tamper_guard.check(
        {"tests/t.py": "import pytest\ndef test_a():\n    assert True\n"},
        {"tests/t.py": src},
    )
    assert report.tampered is True
    assert any(
        "autouse monkeypatch fixture 0->1" in r for r in report.reasons
    ), report.reasons


def test_a_patching_fixture_required_transitively_by_autouse_is_still_caught():
    """Same shape, one hop further: autouse -> middle -> patcher. The
    dependency walk must not stop at direct requests only."""
    src = (
        "import pytest\n"
        "import mymod\n\n"
        "@pytest.fixture\n"
        "def patcher(monkeypatch):\n"
        "    monkeypatch.setattr(mymod, 'run_tests', lambda *a: True)\n\n"
        "@pytest.fixture\n"
        "def middle(patcher):\n"
        "    yield\n\n"
        "@pytest.fixture(autouse=True)\n"
        "def _auto(middle):\n"
        "    yield\n\n"
        "def test_a():\n"
        "    assert mymod.run_tests()\n"
    )
    assert tamper_guard.count_faking_fixtures(src) >= 1


def test_a_patching_fixture_required_only_by_a_test_stays_uncounted():
    """Companion negative control: a fixture that patches, but that no
    autouse fixture depends on — only a test requests it by name — is
    ordinary explicit test design (`test_a_non_autouse_fixture_is_not_
    rule_four_business` in test_tamper_guard_evasions.py), not rule 4's
    business, even when an UNRELATED autouse fixture also exists in the same
    file."""
    src = (
        "import pytest\n"
        "import mymod\n\n"
        "@pytest.fixture\n"
        "def _g(monkeypatch):\n"
        "    monkeypatch.setattr(mymod, 'run_tests', lambda *a: True)\n\n"
        "@pytest.fixture(autouse=True)\n"
        "def _clean():\n"
        "    infra_breaker().reset()\n\n"
        "def test_a(_g):\n"
        "    assert mymod.run_tests()\n"
    )
    assert tamper_guard.count_faking_fixtures(src) == 0


def test_skip_marker_inside_a_string_literal_is_not_counted():
    """AC 3: the live case — a test writes a GENERATED fixture file whose
    content contains the literal text of a skip marker. That text is test
    DATA the guard was built to catch when it lands as real code, not a
    skip in our own suite."""
    src = (
        "def test_writes_a_tampered_fixture(tmp_path):\n"
        "    generated = (\n"
        "        \"import pytest\\n\"\n"
        "        \"@pytest.mark.skip(reason='not now')\\n\"\n"
        "        \"def test_x():\\n\"\n"
        "        \"    pass\\n\"\n"
        "    )\n"
        "    (tmp_path / 'gen_test.py').write_text(generated)\n"
    )
    assert tamper_guard.count_skips(src) == 0


def test_a_real_skip_marker_is_still_counted():
    """AC 3 companion: a real `@pytest.mark.skip` decorating a real `def`
    still counts, and so does the programmatic `pytest.skip(...)` form used
    by the two legitimate platform guards from the live run."""
    decorator_form = (
        "import pytest\n\n"
        "@pytest.mark.skip(reason='flaky')\n"
        "def test_flaky():\n"
        "    assert True\n"
    )
    assert tamper_guard.count_skips(decorator_form) >= 1

    programmatic_form = (
        "import os\n"
        "import pytest\n\n"
        "def test_permission_bits():\n"
        "    if os.name != 'posix':\n"
        "        pytest.skip('posix permission bits only')\n"
        "    if os.geteuid() == 0:\n"
        "        pytest.skip('root ignores file permission bits')\n"
    )
    assert tamper_guard.count_skips(programmatic_form) >= 2


def test_unparseable_source_falls_back_to_raw_scanning():
    """Pins the `SyntaxError` fallback: invalid/partial Python (or non-Python
    content run through the same functions) must not silently disable
    counting. Both counters must still see their signals via the raw-text
    scan."""
    invalid_python = (
        "@pytest.fixture(autouse=True\n"  # unbalanced paren: SyntaxError
        "def _g(monkeypatch):\n"
        "    monkeypatch.setattr(x, 'y', 1)\n"
        "@pytest.mark.skip(reason='wip'\n"  # also unbalanced
        "def test_x():\n"
        "    pass\n"
    )
    with pytest.raises(SyntaxError):
        ast.parse(invalid_python)

    assert tamper_guard.count_faking_fixtures(invalid_python) >= 1
    assert tamper_guard.count_skips(invalid_python) >= 1
