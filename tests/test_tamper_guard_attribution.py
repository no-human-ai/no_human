"""Attribution fix for tamper-guard rule 4's fake-fixture signal.

Bug reproduced on a live branch: an autouse fixture whose entire body resets a
process-wide singleton (no monkeypatching at all) got credited with the
monkeypatching that per-test `monkeypatch` arguments do ELSEWHERE in the same
file, producing a false TAMPERED verdict on
`tests/test_citation_drift_preflight.py`. The old rule read "this file has an
autouse fixture" plus "this file uses monkeypatch somewhere" and concluded the
fixture did the patching.

All sample sources below are DATA held in string literals, which is also why
this file does not trip the guard on itself (its own only "autouse" fixture,
if any, is not real Python — it is text inside a `'''...'''` block).
"""

import pytest

from no_human.testing import tamper_guard


def _isolation_only_autouse_file() -> str:
    """Shape of the live false positive: an autouse fixture that patches
    nothing, plus per-test `monkeypatch` arguments used only on external
    boundaries."""
    return (
        "import sys\n"
        "import pytest\n"
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
        "    monkeypatch.setattr(mymod, 'reanchor_command', lambda *a: None)\n"
    )


def test_autouse_fixture_that_patches_nothing_is_not_a_fake_fixture():
    """AC 1 (negative control): the live shape must score zero and `check()`
    must not report a fake-fixture reason."""
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
        import ast

        ast.parse(invalid_python)

    assert tamper_guard.count_faking_fixtures(invalid_python) >= 1
    assert tamper_guard.count_skips(invalid_python) >= 1
