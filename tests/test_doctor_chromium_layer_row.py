"""`nh doctor`'s visual-proof-walks row must be honest about the chromium
layer, not just the playwright-package import layer.

`testing.ui_evidence.playwright_available()` is deliberately import-only
(loop-safe; `tests/test_ui_evidence_playwright_probe_parity.py` pins that
contract and the ban on any live `sync_playwright()` reference). That means
an install where the `playwright` package is present but the chromium
browser was never downloaded (an interrupted `--fix-walks`, or a user who
`pip install`ed playwright themselves) used to show a green "available" row
with no remedy. This file pins the fix: a filesystem-only, pure-path-logic
check of playwright's browser registry directory that either verifies the
browser cheaply or honestly reports "not verified" — never guesses, never
starts a driver.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest
from click.testing import CliRunner

import no_human.agent.backend_check as bc_mod
import no_human.doctor as doctor_mod
from no_human.cli import commands as cmd_mod
from no_human.doctor import (
    WALKS_AVAILABLE_LINE,
    WALKS_CHROMIUM_MISSING_LINE,
    WALKS_NOT_VERIFIED_LINE,
    WALKS_UNAVAILABLE_LINE,
    visual_walks_row,
)
from no_human.testing import ui_evidence


def _flat(text: str) -> str:
    """Collapse whitespace (including Rich's own word-wrap newlines) so a
    membership check on a rendered line survives wrapping at the runner's
    terminal width."""
    return " ".join(text.split())


def _doctor(monkeypatch, tmp_path, *args):
    class _Cfg:
        data: dict = {}
        db_path = tmp_path / "doctor.db"
        utility_model = "claude-haiku-4-5"

        def get(self, key, default=None):
            return self.data.get(key, default)

    async def _live(**kw):
        return None

    monkeypatch.setattr(cmd_mod, "load_config", lambda *a, **k: _Cfg())
    monkeypatch.setattr(bc_mod, "check_backend", lambda **kw: bc_mod.BackendStatus(
        cli_path="/fake/claude", token_present=True))
    monkeypatch.setattr(bc_mod, "verify_credential_live", _live)
    return CliRunner().invoke(cmd_mod.doctor, list(args))


# ---------------------------------------------------------------------------
# visual_walks_row — the three (+fallback) states
# ---------------------------------------------------------------------------

def test_row_available_when_package_and_browser_present(monkeypatch):
    monkeypatch.setattr(doctor_mod, "_resolve_playwright_chromium_status", lambda: "present")
    monkeypatch.setattr(ui_evidence, "_import_playwright", lambda: object())

    row = visual_walks_row()
    assert row == {"available": True, "chromium": "present", "line": WALKS_AVAILABLE_LINE}
    assert WALKS_AVAILABLE_LINE == "visual-proof walks: available"


def test_row_names_missing_chromium_and_the_fix_walks_remedy(monkeypatch):
    monkeypatch.setattr(doctor_mod, "_resolve_playwright_chromium_status", lambda: "missing")
    monkeypatch.setattr(ui_evidence, "_import_playwright", lambda: object())

    row = visual_walks_row()
    assert row["line"] == WALKS_CHROMIUM_MISSING_LINE
    assert WALKS_CHROMIUM_MISSING_LINE == (
        "visual-proof walks: package installed, chromium missing - "
        "run nh doctor --fix-walks")
    assert "chromium" in WALKS_CHROMIUM_MISSING_LINE
    assert "--fix-walks" in WALKS_CHROMIUM_MISSING_LINE
    assert WALKS_CHROMIUM_MISSING_LINE != WALKS_AVAILABLE_LINE
    assert WALKS_CHROMIUM_MISSING_LINE != WALKS_UNAVAILABLE_LINE


def test_row_unchanged_copy_when_package_absent(monkeypatch):
    monkeypatch.setattr(ui_evidence, "_import_playwright", lambda: None)

    row = visual_walks_row()
    assert row["line"] == WALKS_UNAVAILABLE_LINE
    assert row["chromium"] is None
    assert WALKS_UNAVAILABLE_LINE == (
        "visual-proof walks: unavailable - playwright not installed "
        "(~120MB to install playwright + chromium)")


def test_row_reports_not_verified_rather_than_guessing(monkeypatch):
    monkeypatch.setattr(doctor_mod, "_resolve_playwright_chromium_status", lambda: "not-verified")
    monkeypatch.setattr(ui_evidence, "_import_playwright", lambda: object())

    row = visual_walks_row()
    assert row["line"] == WALKS_NOT_VERIFIED_LINE
    assert "available" not in WALKS_NOT_VERIFIED_LINE
    assert "not verified" in WALKS_NOT_VERIFIED_LINE


# ---------------------------------------------------------------------------
# _resolve_playwright_chromium_status — never raises, degrades honestly
# ---------------------------------------------------------------------------

def test_resolver_never_raises_and_degrades_to_not_verified(monkeypatch):
    def _boom():
        raise RuntimeError("registry path resolution exploded")

    monkeypatch.setattr(doctor_mod, "_playwright_registry_dir", _boom)
    assert doctor_mod._resolve_playwright_chromium_status() == "not-verified"


def test_resolver_degrades_to_not_verified_on_permission_error(monkeypatch):
    # pathlib.Path subclassing quirks differ across Python versions; use a
    # plain object with the minimal surface `_resolve_playwright_chromium_status`
    # actually touches instead of subclassing Path directly.
    class _ForbiddenDir:
        def is_dir(self):
            return True

        def iterdir(self):
            raise PermissionError("denied")

    monkeypatch.setattr(doctor_mod, "_playwright_registry_dir", lambda: _ForbiddenDir())
    assert doctor_mod._resolve_playwright_chromium_status() == "not-verified"


# ---------------------------------------------------------------------------
# _playwright_registry_dir — pure path logic, per platform
# ---------------------------------------------------------------------------

def test_registry_dir_is_pure_path_logic_per_platform(monkeypatch):
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)

    monkeypatch.setattr(doctor_mod.sys, "platform", "darwin")
    monkeypatch.setattr(doctor_mod.Path, "home", lambda: Path("/Users/dev"))
    assert doctor_mod._playwright_registry_dir() == Path(
        "/Users/dev/Library/Caches/ms-playwright")

    monkeypatch.setattr(doctor_mod.sys, "platform", "linux")
    monkeypatch.setattr(doctor_mod.Path, "home", lambda: Path("/home/user"))
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    assert doctor_mod._playwright_registry_dir() == Path("/home/user/.cache/ms-playwright")

    monkeypatch.setenv("XDG_CACHE_HOME", "/custom/cache")
    assert doctor_mod._playwright_registry_dir() == Path("/custom/cache/ms-playwright")
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)

    monkeypatch.setattr(doctor_mod.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", "C:\\Users\\fake\\AppData\\Local")
    assert doctor_mod._playwright_registry_dir() == Path(
        "C:\\Users\\fake\\AppData\\Local") / "ms-playwright"
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    assert doctor_mod._playwright_registry_dir() is None

    monkeypatch.setattr(doctor_mod.sys, "platform", "some-unheard-of-os")
    assert doctor_mod._playwright_registry_dir() is None

    monkeypatch.setattr(doctor_mod.sys, "platform", "darwin")
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "0")
    assert doctor_mod._playwright_registry_dir() is None

    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "/explicit/override")
    assert doctor_mod._playwright_registry_dir() == Path("/explicit/override")


# ---------------------------------------------------------------------------
# _resolve_playwright_chromium_status through real tmp_path fixtures
# ---------------------------------------------------------------------------

def test_status_from_real_tmp_dirs(monkeypatch, tmp_path):
    present_dir = tmp_path / "present"
    (present_dir / "chromium-1140").mkdir(parents=True)
    monkeypatch.setattr(doctor_mod, "_playwright_registry_dir", lambda: present_dir)
    assert doctor_mod._resolve_playwright_chromium_status() == "present"

    firefox_only_dir = tmp_path / "firefox_only"
    (firefox_only_dir / "firefox-1400").mkdir(parents=True)
    monkeypatch.setattr(doctor_mod, "_playwright_registry_dir", lambda: firefox_only_dir)
    assert doctor_mod._resolve_playwright_chromium_status() == "missing"

    absent_dir = tmp_path / "does_not_exist"
    monkeypatch.setattr(doctor_mod, "_playwright_registry_dir", lambda: absent_dir)
    assert doctor_mod._resolve_playwright_chromium_status() == "missing"


# ---------------------------------------------------------------------------
# CLI — nh doctor prints each state
# ---------------------------------------------------------------------------

def test_doctor_cli_prints_each_state(monkeypatch, tmp_path):
    monkeypatch.setattr(ui_evidence, "_import_playwright", lambda: object())
    monkeypatch.setattr(doctor_mod, "_resolve_playwright_chromium_status", lambda: "present")
    result = _doctor(monkeypatch, tmp_path)
    assert _flat(WALKS_AVAILABLE_LINE) in _flat(result.output), result.output
    assert "(nh doctor --fix-walks to enable)" not in result.output

    monkeypatch.setattr(doctor_mod, "_resolve_playwright_chromium_status", lambda: "missing")
    result = _doctor(monkeypatch, tmp_path)
    assert _flat(WALKS_CHROMIUM_MISSING_LINE) in _flat(result.output), result.output
    assert "(nh doctor --fix-walks to enable)" not in result.output

    monkeypatch.setattr(ui_evidence, "_import_playwright", lambda: None)
    result = _doctor(monkeypatch, tmp_path)
    assert _flat(WALKS_UNAVAILABLE_LINE) in _flat(result.output), result.output
    assert "(nh doctor --fix-walks to enable)" in result.output


# ---------------------------------------------------------------------------
# walks_colour — the green-vs-yellow guard in _print_visual_walks itself.
# CliRunner strips ANSI/no-tty markup, so a rendered-output membership check
# can never observe styling; this drives _print_visual_walks directly with a
# fake console and inspects the raw markup string it builds, which is where
# `walks_ok = wrow["available"] and wrow.get("chromium") == "present"` lives.
# If that guard regressed to `walks_ok = wrow["available"]`, the
# chromium-missing/not-verified cases below would render green and these
# tests would catch it.
# ---------------------------------------------------------------------------

class _FakeConsole:
    def __init__(self):
        self.lines = []

    def print(self, *args, **kwargs):
        self.lines.append(args[0] if args else "")


class _FakeDoctorState:
    ui_evidence: list = []


def test_walks_colour_is_green_only_when_available_and_chromium_present(monkeypatch):
    monkeypatch.setattr(ui_evidence, "_import_playwright", lambda: object())

    monkeypatch.setattr(doctor_mod, "_resolve_playwright_chromium_status", lambda: "present")
    fake = _FakeConsole()
    cmd_mod._print_visual_walks(fake, _FakeDoctorState())
    assert fake.lines[0].startswith("[green]"), fake.lines[0]


def test_walks_colour_is_yellow_when_chromium_missing_even_though_package_available(monkeypatch):
    # This is the guard the review flagged: package available but chromium
    # missing must NOT render green, even though wrow["available"] is True.
    monkeypatch.setattr(ui_evidence, "_import_playwright", lambda: object())
    monkeypatch.setattr(doctor_mod, "_resolve_playwright_chromium_status", lambda: "missing")

    fake = _FakeConsole()
    cmd_mod._print_visual_walks(fake, _FakeDoctorState())
    assert fake.lines[0].startswith("[yellow]"), fake.lines[0]
    assert not fake.lines[0].startswith("[green]")


def test_walks_colour_is_yellow_when_chromium_not_verified(monkeypatch):
    monkeypatch.setattr(ui_evidence, "_import_playwright", lambda: object())
    monkeypatch.setattr(doctor_mod, "_resolve_playwright_chromium_status", lambda: "not-verified")

    fake = _FakeConsole()
    cmd_mod._print_visual_walks(fake, _FakeDoctorState())
    assert fake.lines[0].startswith("[yellow]"), fake.lines[0]


def test_walks_colour_is_yellow_when_package_absent(monkeypatch):
    monkeypatch.setattr(ui_evidence, "_import_playwright", lambda: None)

    fake = _FakeConsole()
    cmd_mod._print_visual_walks(fake, _FakeDoctorState())
    assert fake.lines[0].startswith("[yellow]"), fake.lines[0]


# ---------------------------------------------------------------------------
# AC3 — playwright_available() and the PR-body disclosure stay untouched
# ---------------------------------------------------------------------------

def test_playwright_available_is_byte_unchanged_import_only():
    src = inspect.getsource(ui_evidence.playwright_available)
    tree = ast.parse(src)
    func = tree.body[0]
    assert isinstance(func, ast.FunctionDef)
    stmts = func.body
    # First statement is the docstring (an Expr/Constant str); the rest is code.
    code_stmts = [s for s in stmts if not (
        isinstance(s, ast.Expr) and isinstance(getattr(s, "value", None), ast.Constant)
        and isinstance(s.value.value, str))]
    assert len(code_stmts) == 1
    only_stmt = code_stmts[0]
    assert isinstance(only_stmt, ast.Return)
    assert ast.unparse(only_stmt) == "return _import_playwright() is not None"
    # "chromium" itself already appears in this function's pre-existing
    # docstring (historical prose explaining the removed binary check) —
    # not evidence of an edit. "registry" and the resolver/seam names this
    # task introduces are the actual tells of scope creep into this
    # byte-unchanged function.
    assert "registry" not in src.lower()
    assert "_playwright_registry_dir" not in src
    assert "_resolve_playwright_chromium_status" not in src


def test_pr_body_disclosure_untouched():
    assert ui_evidence.MISSING_PLAYWRIGHT_REASON == (
        "playwright not installed (uv sync --group e2e)")

    import no_human.core.orchestrator as orchestrator_mod

    src = inspect.getsource(orchestrator_mod)
    lines = src.splitlines()
    matches = [
        i for i, line in enumerate(lines, start=1)
        if line.strip() == "if not ui_evidence.playwright_available():"
    ]
    assert matches, "orchestrator must still gate the UI-evidence skip on playwright_available()"


# ---------------------------------------------------------------------------
# AC2 — no sync driver spawn anywhere on the doctor chromium path
# ---------------------------------------------------------------------------

def test_no_driver_spawn_in_doctor_chromium_path():
    src = Path(doctor_mod.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)

    target_funcs = {"_playwright_registry_dir", "_resolve_playwright_chromium_status"}
    found_funcs = set()
    offenders = []

    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in target_funcs:
            found_funcs.add(node.name)
            for inner in ast.walk(node):
                if isinstance(inner, ast.Name) and inner.id == "sync_playwright":
                    offenders.append(f"{node.name}: Name(sync_playwright)")
                if isinstance(inner, ast.Attribute) and inner.attr == "sync_playwright":
                    offenders.append(f"{node.name}: Attribute(.sync_playwright)")
                if isinstance(inner, ast.Call):
                    callee = inner.func
                    dotted = None
                    if isinstance(callee, ast.Attribute):
                        dotted = callee.attr
                    elif isinstance(callee, ast.Name):
                        dotted = callee.id
                    if dotted in {"run", "Popen", "system", "execv", "execve", "execl"}:
                        offenders.append(f"{node.name}: call to {dotted!r}")

    assert found_funcs == target_funcs, (
        f"positive control: both target functions must exist in doctor.py, "
        f"found {found_funcs}")
    assert offenders == [], offenders

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or ""):
            parts = node.module.split(".")
            assert "sync_api" not in parts, f"import from {node.module!r}"
            if parts and parts[0] == "playwright":
                pytest.fail(f"doctor.py must not import playwright: {node.module!r}")
        if isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                assert "sync_api" not in parts, f"import {alias.name!r}"
                if parts and parts[0] == "playwright":
                    pytest.fail(f"doctor.py must not import playwright: {alias.name!r}")
