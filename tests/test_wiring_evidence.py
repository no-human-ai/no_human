"""Wiring evidence: deterministic, polyglot, evidence — never a verdict.

Feeds the reviewer's goal-reachability judgment with the raw material of the
"implemented but never called by the production path" class. It must fail
toward silence: any error yields an empty or partial result, and the rendered
block states its own ceilings (static, Python and JS/TS, module level and
class bodies) so the prose never exceeds the mechanism.
"""

import subprocess

import pytest

from no_human.review.wiring_evidence import (
    collect_wiring_evidence,
    format_wiring_evidence,
)


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "u@e.com")
    _git(tmp_path, "config", "user.name", "u")
    (tmp_path / "app.py").write_text("def handle(req):\n    return {}\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_app.py").write_text("import app\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "base")
    return tmp_path


def _commit(repo, msg="change"):
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", msg)


def test_a_new_unreferenced_symbol_is_listed(repo):
    (repo / "rates.py").write_text("def volumetric(dims):\n    return 1.0\n")
    (repo / "tests" / "test_rates.py").write_text(
        "from rates import volumetric\n\ndef test_v():\n"
        "    assert volumetric(None) == 1.0\n")
    _commit(repo)
    out = collect_wiring_evidence(repo, "HEAD~1", "HEAD")
    assert out == [("rates.py", "volumetric")], (
        "a test-only reference is not production wiring")


def test_a_production_referenced_symbol_is_not_listed(repo):
    (repo / "rates.py").write_text("def volumetric(dims):\n    return 1.0\n")
    (repo / "app.py").write_text(
        "from rates import volumetric\n\ndef handle(req):\n"
        "    return {'w': volumetric(req)}\n")
    _commit(repo)
    assert collect_wiring_evidence(repo, "HEAD~1", "HEAD") == []


def test_a_symbol_used_only_inside_its_own_file_is_still_listed(repo):
    """The search is for references OUTSIDE the defining file — the block's
    header states exactly that, so this behaviour is the documented one."""
    (repo / "rates.py").write_text(
        "def _mul(a, b):\n    return a * b\n\n"
        "def volumetric(dims):\n    return _mul(1, 1)\n")
    _commit(repo)
    out = collect_wiring_evidence(repo, "HEAD~1", "HEAD")
    assert ("rates.py", "_mul") in out and ("rates.py", "volumetric") in out


def test_a_method_no_caller_reaches_is_listed_by_qualified_name(repo):
    """The class is wired and the method is not. Before issue #114 phase 4
    this collector read module top level only, so it saw neither."""
    (repo / "store.py").write_text(
        "class Store:\n    def recompute_totals(self):\n        return 0\n")
    (repo / "app.py").write_text(
        "from store import Store\n\ndef handle(req):\n    return Store()\n")
    _commit(repo)
    assert collect_wiring_evidence(repo, "HEAD~1", "HEAD") == [
        ("store.py", "Store.recompute_totals")]


def test_a_called_method_is_not_listed(repo):
    (repo / "store.py").write_text(
        "class Store:\n    def recompute_totals(self):\n        return 0\n")
    (repo / "app.py").write_text(
        "from store import Store\n\ndef handle(req):\n"
        "    return Store().recompute_totals()\n")
    _commit(repo)
    assert collect_wiring_evidence(repo, "HEAD~1", "HEAD") == []


def test_a_dunder_method_is_not_listed(repo):
    """The language calls it, so no reference search can find the call."""
    (repo / "store.py").write_text(
        "class Store:\n    def __init__(self):\n        self.rows = []\n")
    (repo / "app.py").write_text(
        "from store import Store\n\ndef handle(req):\n    return Store()\n")
    _commit(repo)
    assert collect_wiring_evidence(repo, "HEAD~1", "HEAD") == []


def test_a_definition_inside_a_function_is_not_listed(repo):
    """A closure has no caller outside its file by construction."""
    (repo / "app.py").write_text(
        "def handle(req):\n    def _inner():\n        return 1\n"
        "    return _inner()\n")
    _commit(repo)
    assert collect_wiring_evidence(repo, "HEAD~1", "HEAD") == []


def test_an_exported_component_nothing_renders_is_listed(repo):
    """A JS diff contributed nothing at all before phase 4, and this repo is
    a quarter JS by commit."""
    (repo / "web").mkdir()
    (repo / "web" / "Panel.jsx").write_text(
        "export function Panel() {\n  return null;\n}\n")
    (repo / "web" / "Board.jsx").write_text(
        "import { Panel } from './Panel.jsx';\n\n"
        "export function Board() {\n  return Panel();\n}\n")
    _commit(repo)
    assert collect_wiring_evidence(repo, "HEAD~1", "HEAD") == [
        ("web/Board.jsx", "Board")], "Panel is rendered; Board is not"


def test_a_js_test_file_is_neither_a_definer_nor_production_wiring(repo):
    (repo / "web").mkdir()
    (repo / "web" / "rates.mjs").write_text(
        "export function volumetric() {\n  return 1;\n}\n")
    (repo / "web" / "rates.test.mjs").write_text(
        "import { volumetric } from './rates.mjs';\n\n"
        "export function checkVolumetric() {\n  return volumetric();\n}\n")
    _commit(repo)
    assert collect_wiring_evidence(repo, "HEAD~1", "HEAD") == [
        ("web/rates.mjs", "volumetric")]


def test_an_unexported_js_binding_is_not_listed(repo):
    (repo / "web").mkdir()
    (repo / "web" / "rates.mjs").write_text(
        "const RATE = 2;\n\nfunction scale(n) {\n  return n * RATE;\n}\n"
        "export function volumetric(n) {\n  return scale(n);\n}\n")
    (repo / "web" / "app.mjs").write_text(
        "import { volumetric } from './rates.mjs';\n\n"
        "export function total(n) {\n  return volumetric(n);\n}\n")
    _commit(repo)
    assert collect_wiring_evidence(repo, "HEAD~1", "HEAD") == [
        ("web/app.mjs", "total")], (
        "RATE and scale are module-private and never collected")


def test_a_language_no_reader_claims_contributes_nothing_and_does_not_crash(
        repo):
    (repo / "notes.md").write_text("# nothing\n")
    (repo / "main.go").write_text("func Handle() {}\n")
    _commit(repo)
    assert collect_wiring_evidence(repo, "HEAD~1", "HEAD") == []


def test_changed_test_files_are_ignored_as_definers(repo):
    (repo / "tests" / "test_new.py").write_text("def helper():\n    pass\n")
    _commit(repo)
    assert collect_wiring_evidence(repo, "HEAD~1", "HEAD") == []


def test_bad_refs_fail_to_empty_never_raise(repo):
    assert collect_wiring_evidence(repo, "no-such-ref", "HEAD") == []


def test_unparseable_python_fails_to_empty(repo):
    (repo / "broken.py").write_text("def (\n")
    _commit(repo)
    assert collect_wiring_evidence(repo, "HEAD~1", "HEAD") == []


def test_an_exhausted_budget_yields_no_evidence_rather_than_an_exception(repo):
    """The deadline spans the whole pass, so a repo slow enough to burn it
    under-reports; it never raises into the review gate."""
    (repo / "rates.py").write_text("def volumetric(dims):\n    return 1.0\n")
    _commit(repo)
    assert collect_wiring_evidence(repo, "HEAD~1", "HEAD", timeout=0) == []


def test_format_block_is_labeled_hedged_and_empty_on_no_findings():
    assert format_wiring_evidence([]) == ""
    block = format_wiring_evidence(
        [("rates.py", "volumetric"), ("store.py", "Store.recompute_totals")])
    assert block.startswith(
        "WIRING EVIDENCE (deterministic, static — Python and JS/TS, module "
        "level and class bodies")
    assert "rates.py: volumetric" in block
    assert "store.py: Store.recompute_totals" in block
    assert "Evidence, not a verdict" in block
