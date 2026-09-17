"""Consent-first provisioning for visual-proof walks.

`nh doctor` must be able to SAY playwright/chromium are missing (the same
honesty floor `tests/test_ui_evidence_missing_playwright_pr_line.py` pins on
the PR-body side) and `nh doctor --fix-walks` must be able to FIX that — but
never spend a customer's disk/network on a ~120MB download without an
explicit "y" first, and never for real inside this test suite. Every test
here either stays at the pure `doctor.py`/`walks_provision.py` layer (no
subprocess spawned) or goes through the CLI with `walks_provision.install_walks`
monkeypatched to a fake — the real thing is only exercised through an
injected `runner` spy, which never touches the network.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

import no_human.agent.backend_check as bc_mod
import no_human.doctor as doctor_mod
import no_human.walks_provision as walks_provision
from no_human.cli import commands as cmd_mod
from no_human.doctor import (
    WALKS_AVAILABLE_LINE,
    WALKS_DOWNLOAD_SIZE,
    WALKS_UNAVAILABLE_LINE,
    visual_walks_row,
    walks_install_plan,
    walks_plan_description,
)


def _flat(text: str) -> str:
    """Collapse whitespace (including the console's own word-wrap
    newlines) so a membership check on a rendered Rich line survives
    wrapping at the runner's terminal width — `WALKS_UNAVAILABLE_LINE`
    (103 chars) wraps under `CliRunner`'s default width where the old,
    shorter text did not."""
    return " ".join(text.split())


def _doctor(monkeypatch, tmp_path, *args, live=None, config=None):
    class _Cfg:
        data: dict = config if config is not None else {}
        db_path = tmp_path / "doctor.db"
        utility_model = "claude-haiku-4-5"

        def get(self, key, default=None):
            return self.data.get(key, default)

    async def _live(**kw):
        return live

    monkeypatch.setattr(cmd_mod, "load_config", lambda *a, **k: _Cfg())
    monkeypatch.setattr(bc_mod, "check_backend", lambda **kw: bc_mod.BackendStatus(
        cli_path="/fake/claude", token_present=True))
    monkeypatch.setattr(bc_mod, "verify_credential_live", _live)
    result = CliRunner().invoke(cmd_mod.doctor, list(args))
    return result


# ---------------------------------------------------------------------------
# visual_walks_row / walks_install_plan / walks_plan_description — pure layer
# ---------------------------------------------------------------------------

def test_visual_walks_row_unavailable_line_is_exact():
    row = visual_walks_row(available=False)
    assert row == {"available": False, "chromium": None, "line": WALKS_UNAVAILABLE_LINE}
    assert WALKS_UNAVAILABLE_LINE == (
        "visual-proof walks: unavailable - playwright not installed "
        "(~120MB to install playwright + chromium)")
    assert WALKS_DOWNLOAD_SIZE == "~120MB"


def test_visual_walks_row_available_line():
    row = visual_walks_row(available=True, chromium="present")
    assert row == {"available": True, "chromium": "present", "line": WALKS_AVAILABLE_LINE}


def test_visual_walks_row_never_raises_when_the_probe_itself_is_broken(monkeypatch):
    """A diagnostic must never crash the command that prints it — mirrors
    `codex_row`'s own contract for an invalid config."""
    import no_human.testing.ui_evidence as ui_evidence_mod

    def _boom():
        raise RuntimeError("import machinery exploded")

    monkeypatch.setattr(ui_evidence_mod, "playwright_available", _boom)
    row = visual_walks_row()
    assert row["available"] is False
    assert row["line"] == WALKS_UNAVAILABLE_LINE


def test_visual_walks_row_docstring_does_not_claim_purity():
    """`visual_walks_row` used to advertise "Pure and read-only, same
    contract as `codex_row`" and a package-AND-binary check that no longer
    exists. The docstring must name the probe it actually performs (import
    only) rather than a stronger claim it can't back up."""
    doc = visual_walks_row.__doc__ or ""
    assert "Pure and read-only" not in doc, doc
    assert "import" in doc.lower(), doc


def test_fix_walks_gate_and_doctor_row_use_the_same_probe(monkeypatch, tmp_path):
    """The customer-visible three-way contradiction this bug caused: `nh
    doctor` (async) and `nh doctor --fix-walks` (sync) must agree, by
    construction, because both read `visual_walks_row`, which reads the
    same `_import_playwright` seam regardless of caller context."""
    import no_human.testing.ui_evidence as ui_evidence_mod

    monkeypatch.setattr(ui_evidence_mod, "_import_playwright", lambda: object())
    monkeypatch.setattr(doctor_mod, "_resolve_playwright_chromium_status", lambda: "present")

    def _forbidden(**kw):
        raise AssertionError("install_walks must not run when already available")

    monkeypatch.setattr(walks_provision, "install_walks", _forbidden)

    plain = _doctor(monkeypatch, tmp_path)
    assert WALKS_AVAILABLE_LINE in plain.output, plain.output

    fix = CliRunner().invoke(cmd_mod.doctor, ["--fix-walks"])
    assert fix.exit_code == 0, fix.output
    assert "already available" in fix.output.lower(), fix.output

    monkeypatch.setattr(ui_evidence_mod, "_import_playwright", lambda: None)

    plain2 = _doctor(monkeypatch, tmp_path)
    assert _flat(WALKS_UNAVAILABLE_LINE) in _flat(plain2.output), plain2.output

    fix2 = CliRunner().invoke(cmd_mod.doctor, ["--fix-walks", "--dry-run"])
    assert fix2.exit_code == 0, fix2.output
    assert _flat(WALKS_UNAVAILABLE_LINE) in _flat(fix2.output), fix2.output


def test_fix_walks_already_available_names_the_chromium_remedy(monkeypatch, tmp_path):
    """Narrowing the probe to import-only means a package-present/
    binary-missing install now hits the "already available" early return
    with nothing left to fix — close that dead end by naming the residual
    chromium remedy derived from `walks_install_plan()`."""
    monkeypatch.setattr(doctor_mod, "visual_walks_row",
                         lambda: {"available": True, "line": WALKS_AVAILABLE_LINE})

    def _forbidden(**kw):
        raise AssertionError("already-available must never invoke the installer")

    monkeypatch.setattr(walks_provision, "install_walks", _forbidden)

    result = CliRunner().invoke(cmd_mod.doctor, ["--fix-walks"])
    assert result.exit_code == 0, result.output
    assert "already available" in result.output.lower()
    assert "playwright install chromium" in _flat(result.output), result.output


def test_walks_install_plan_is_uv_first_when_uv_and_a_checkout_are_present(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/uv" if name == "uv" else None)
    monkeypatch.setattr(doctor_mod, "_running_checkout", lambda: Path("/repo"))
    plan = walks_install_plan()
    assert plan[0][:4] == ["uv", "sync", "--group", "e2e"]
    assert plan[-1][-3:] == ["playwright", "install", "chromium"]


def test_walks_install_plan_falls_back_to_pip_without_uv_or_a_checkout(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr(doctor_mod, "_running_checkout", lambda: None)
    plan = walks_install_plan()
    assert plan[0][1:] == ["-m", "pip", "install", "playwright>=1.50"]
    assert plan[-1][-3:] == ["playwright", "install", "chromium"]


def test_walks_plan_description_has_one_line_per_step():
    desc = walks_plan_description()
    assert len(desc.splitlines()) == len(walks_install_plan())


# ---------------------------------------------------------------------------
# walks_provision.install_walks — the executor, always through an injected
# runner; never a real subprocess.
# ---------------------------------------------------------------------------

def test_install_walks_dry_run_never_invokes_the_runner():
    calls = []

    def spy(*a, **kw):
        calls.append((a, kw))
        raise AssertionError("dry_run must never invoke the runner")

    ok, messages = walks_provision.install_walks(runner=spy, dry_run=True)
    assert ok is True
    assert calls == []
    assert messages == walks_plan_description().splitlines()


def test_install_walks_runs_every_plan_step_in_order_through_the_injected_runner(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/uv" if name == "uv" else None)
    monkeypatch.setattr(doctor_mod, "_running_checkout", lambda: Path("/repo"))

    seen = []

    def spy(argv, **kw):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    ok, messages = walks_provision.install_walks(runner=spy)
    assert ok is True
    assert seen == walks_install_plan()
    assert all(m.startswith("OK:") for m in messages)


def test_install_walks_pins_utf8_and_replace_on_every_runner_call(monkeypatch):
    """`install_walks` invokes its injected `runner` with `text=True` —
    decoding the child's output. Without an explicit `encoding=`, that
    decode falls back to the host's locale codepage; on a non-UTF-8 Windows
    codepage (Hebrew cp1255, Cyrillic cp1251, ...), a stray non-ASCII byte
    in a package manager's or playwright's stderr kills the reader thread
    (see `tests/test_subprocess_decodes_utf8.py`'s module docstring for the
    full CPython mechanism) and `result.stderr` comes back `None` — silently
    swallowed to `""` by `install_walks`'s own `(getattr(result, "stderr",
    "") or "")`, losing the FAILED message's diagnostic. Pinning both
    `encoding="utf-8"` and `errors="replace"` on the call itself is the only
    fix that keeps the diagnostic without ever raising or returning `None`."""
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/uv" if name == "uv" else None)
    monkeypatch.setattr(doctor_mod, "_running_checkout", lambda: Path("/repo"))

    seen_kwargs = []

    def spy(argv, **kw):
        seen_kwargs.append(kw)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    ok, _messages = walks_provision.install_walks(runner=spy)
    assert ok is True
    assert seen_kwargs, "the plan must run at least one step for this to be meaningful"
    for kw in seen_kwargs:
        assert kw.get("text") is True
        assert kw.get("encoding") == "utf-8", (
            "install_walks's runner call must name encoding='utf-8' -- "
            "text=True alone decodes with the host's ANSI codepage"
        )
        assert kw.get("errors") == "replace", (
            "install_walks's runner call must name an explicit errors= "
            "policy alongside encoding= -- errors= alone (with no "
            "encoding=) still decodes through the wrong codec"
        )


def test_install_walks_stops_at_the_first_failing_step_with_no_rollback(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr(doctor_mod, "_running_checkout", lambda: None)

    seen = []

    def spy(argv, **kw):
        seen.append(argv)
        if len(seen) == 1:
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="boom")

    ok, messages = walks_provision.install_walks(runner=spy)
    assert ok is False
    # Only the two plan steps exist; the failure is on the second, and
    # nothing runs a third "undo" command — there is no rollback mechanism.
    assert len(seen) == 2
    assert messages[0].startswith("OK:")
    assert "FAILED" in messages[1] and "boom" in messages[1]


def test_install_walks_survives_a_missing_command(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr(doctor_mod, "_running_checkout", lambda: None)

    def spy(argv, **kw):
        raise FileNotFoundError(argv[0])

    ok, messages = walks_provision.install_walks(runner=spy)
    assert ok is False
    assert "not found" in messages[0]


# ---------------------------------------------------------------------------
# `nh doctor --fix-walks` — consent-first CLI wiring
# ---------------------------------------------------------------------------

def test_doctor_dry_run_reports_unavailable_and_the_plan_without_installing(
        monkeypatch, tmp_path):
    monkeypatch.setattr(doctor_mod, "visual_walks_row",
                         lambda: {"available": False, "line": WALKS_UNAVAILABLE_LINE})

    def _forbidden(**kw):
        raise AssertionError("install_walks must not be called under --dry-run")

    monkeypatch.setattr(walks_provision, "install_walks", _forbidden)

    result = _doctor(monkeypatch, tmp_path, "--fix-walks", "--dry-run")
    assert result.exit_code == 0, result.output
    flat = _flat(result.output)
    assert _flat(WALKS_UNAVAILABLE_LINE) in flat, result.output
    assert "playwright" in flat and "chromium" in flat


def test_doctor_dry_run_without_fix_walks_is_a_usage_error(monkeypatch, tmp_path):
    result = _doctor(monkeypatch, tmp_path, "--dry-run")
    assert result.exit_code != 0
    assert "--fix-walks" in result.output


def test_doctor_fix_walks_prompts_with_the_exact_consent_text_and_aborts_on_no(
        monkeypatch, tmp_path):
    monkeypatch.setattr(doctor_mod, "visual_walks_row",
                         lambda: {"available": False, "line": WALKS_UNAVAILABLE_LINE})

    def _forbidden(**kw):
        raise AssertionError("declining consent must not install anything")

    monkeypatch.setattr(walks_provision, "install_walks", _forbidden)

    result = CliRunner().invoke(cmd_mod.doctor, ["--fix-walks"], input="n\n")
    assert (
        "Visual-proof walks require playwright and chromium "
        f"({WALKS_DOWNLOAD_SIZE}). Install now? [y/n]"
    ) in result.output, result.output
    assert "aborted" in result.output.lower()


def test_doctor_fix_walks_consent_yes_runs_the_installer(monkeypatch, tmp_path):
    monkeypatch.setattr(doctor_mod, "visual_walks_row",
                         lambda: {"available": False, "line": WALKS_UNAVAILABLE_LINE})

    calls = []

    def fake_install_walks(**kw):
        calls.append(kw)
        return True, ["OK: `uv sync --group e2e`", "OK: `playwright install chromium`"]

    monkeypatch.setattr(walks_provision, "install_walks", fake_install_walks)

    result = CliRunner().invoke(cmd_mod.doctor, ["--fix-walks"], input="y\n")
    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert "installed" in result.output.lower()


def test_doctor_fix_walks_reports_a_failing_step_and_exits_nonzero(monkeypatch, tmp_path):
    monkeypatch.setattr(doctor_mod, "visual_walks_row",
                         lambda: {"available": False, "line": WALKS_UNAVAILABLE_LINE})
    monkeypatch.setattr(walks_provision, "install_walks",
                         lambda **kw: (False, ["OK: `uv sync --group e2e`",
                                               "FAILED: `playwright install chromium` — boom"]))

    result = CliRunner().invoke(cmd_mod.doctor, ["--fix-walks"], input="y\n")
    assert result.exit_code != 0
    assert "failed" in result.output.lower()
    # No rollback claim anywhere in the output — partial state is disclosed,
    # not silently undone.
    assert "rollback" in result.output.lower() or "retry" in result.output.lower()


def test_doctor_fix_walks_already_available_skips_the_prompt_entirely(monkeypatch, tmp_path):
    monkeypatch.setattr(doctor_mod, "visual_walks_row",
                         lambda: {"available": True, "line": WALKS_AVAILABLE_LINE})

    def _forbidden(**kw):
        raise AssertionError("already-available must never invoke the installer")

    monkeypatch.setattr(walks_provision, "install_walks", _forbidden)

    # No input fed at all: a prompt here would hang/abort the CliRunner.
    result = CliRunner().invoke(cmd_mod.doctor, ["--fix-walks"])
    assert result.exit_code == 0, result.output
    assert "already available" in result.output.lower()


def test_doctor_plain_run_shows_the_unavailable_line_without_prompting(monkeypatch, tmp_path):
    monkeypatch.setattr(doctor_mod, "visual_walks_row",
                         lambda: {"available": False, "line": WALKS_UNAVAILABLE_LINE})
    result = _doctor(monkeypatch, tmp_path)
    assert _flat(WALKS_UNAVAILABLE_LINE) in _flat(result.output), result.output


# ---------------------------------------------------------------------------
# Lean-stack pin: playwright stays an opt-in `e2e` dependency group — never a
# core/default dependency, and `pyproject.toml` itself is not touched by
# this change.
# ---------------------------------------------------------------------------

def test_playwright_is_only_in_the_e2e_dependency_group():
    import tomllib

    root = Path(__file__).resolve().parents[1]
    with open(root / "pyproject.toml", "rb") as f:
        data = tomllib.load(f)

    core_deps = data.get("project", {}).get("dependencies", [])
    assert not any("playwright" in dep.lower() for dep in core_deps), core_deps

    for extra_group in data.get("project", {}).get("optional-dependencies", {}).values():
        assert not any("playwright" in dep.lower() for dep in extra_group), extra_group

    groups = data.get("dependency-groups", {})
    e2e = groups.get("e2e", [])
    assert any("playwright" in dep.lower() for dep in e2e), (
        f"expected playwright pinned in [dependency-groups].e2e, got {groups}")
