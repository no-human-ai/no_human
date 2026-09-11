"""Held-out parity test for `playwright_available()` / `visual_walks_row()`.

The bug this pins: `playwright_available()` used to resolve the chromium
binary's path via `playwright.sync_api.sync_playwright()`. That facade
raises whenever it is started inside a running `asyncio` event loop, and
BOTH production call sites (`orchestrator._maybe_capture_ui_evidence`,
async; `nh doctor`'s `_go`, run under `asyncio.run`) run under one — so the
probe returned `False` for every provisioned user, unconditionally, while
sync `nh doctor --fix-walks` saw `True`. Three call sites, three different
answers, from the SAME environment.

Every other test file in this suite that exercises `playwright_available()`
monkeypatches it (or its `_import_playwright`/`_chromium_executable_path`
seams) — necessary for testing the callers, but structurally blind to a bug
IN the probe itself: a stubbed probe cannot catch its own inversion. This
file is the one place that calls the REAL, unstubbed probe and proves it
agrees with itself across contexts. **No `monkeypatch` anywhere in this
file** — that is the point.

`pyproject.toml:204` sets `asyncio_mode = "auto"`, so every `async def` test
function here would already be running inside an event loop and could not
call `asyncio.run()` itself (a loop cannot be started from inside a loop).
Every test in this file is therefore a plain `def`.

The skip guard is derived independently of the code under test —
`importlib.util.find_spec`, not `ui_evidence.playwright_available()` — so a
bug that makes the probe lie about its own availability cannot also hide
the test that would catch it.
"""
from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import pytest

from no_human import doctor as doctor_mod
from no_human.cli import commands as cmd_mod
from no_human.testing import ui_evidence

# Parent package first: `find_spec("playwright.async_api")` RAISES
# ModuleNotFoundError (rather than returning None) when `playwright` itself
# is absent — measured 2026-09-02 as a collection ERROR on the public CI,
# which installs no e2e group. A provisioned dev machine can never see this.
_PLAYWRIGHT_ABSENT = (
    importlib.util.find_spec("playwright") is None
    or importlib.util.find_spec("playwright.async_api") is None
)
_skip_if_absent = pytest.mark.skipif(
    _PLAYWRIGHT_ABSENT,
    reason="playwright not installed in this environment (uv sync --group e2e)",
)


@_skip_if_absent
def test_probe_agrees_between_sync_and_async_contexts():
    """The customer-visible bug, pinned directly: call the real probe once
    synchronously and once from inside a genuinely running event loop, and
    require the SAME, TRUE answer both times."""

    async def _probe():
        # Demonstrate the loop is actually running before the probe runs —
        # a bug that only manifests once the loop has yielded control at
        # least once would otherwise slip through.
        await asyncio.sleep(0)
        return ui_evidence.playwright_available()

    sync_result = ui_evidence.playwright_available()
    async_result = asyncio.run(_probe())

    assert sync_result == async_result, (
        f"sync={sync_result!r} async={async_result!r} — the probe must "
        "return the same value regardless of caller context")
    assert sync_result is True, (
        "playwright is installed in this environment (skip guard says so); "
        "the probe must report it available")


@_skip_if_absent
def test_import_seam_is_loop_safe():
    """Positive control for the test above: prove the agreement isn't
    trivially "both False" by checking the underlying import seam directly
    returns a non-None factory from inside a running loop."""

    async def _probe():
        await asyncio.sleep(0)
        return ui_evidence._import_playwright()

    result = asyncio.run(_probe())
    assert result is not None


def test_no_sync_playwright_reference_survives():
    """No `sync_playwright()` call may remain on any path reachable from a
    running event loop — now checked across every module that touches the
    walks-availability probe: `ui_evidence.py` (the probe itself),
    `doctor.py` (the two-layer row and the filesystem-only chromium
    resolver added on top of it) and `cli/commands.py` (the CLI printer).
    This test has NO skipif — it reads source, not behavior, so it must run
    even when playwright itself is absent.

    AST-based, not a raw substring scan: `playwright_available`'s own
    docstring names `playwright.sync_api.sync_playwright()` in prose, to
    explain the bug this fix removes — a plain `"sync_playwright" not in
    src` check would flag that legitimate historical explanation as a
    survival of the call itself. What must actually be gone is any `import`
    of the sync facade or any `Name`/`Attribute` node invoking it — i.e. code,
    not commentary.

    An absence claim needs a positive control: also assert `async_api` IS
    still referenced in actual code (scoped to `ui_evidence.py`, the only
    module that legitimately imports it), so this can't be vacuously
    passing against a module that was gutted or renamed out from under it.
    `doctor.py`/`commands.py` must not import ANY `playwright.*` module at
    all — the chromium layer added there is pure path logic, not a
    playwright-package consumer.
    """
    import ast

    modules = {
        "ui_evidence": Path(ui_evidence.__file__),
        "doctor": Path(doctor_mod.__file__),
        "commands": Path(cmd_mod.__file__),
    }

    # NOTE: plain substring matching is wrong here — "async_api" ENDS with
    # "sync_api" (a-'sync_api'), so a naive `"sync_api" in module` check
    # would flag the very import this test must treat as the positive
    # control. Match dotted module paths by exact component instead.
    offenders = []
    playwright_imports = []
    saw_async_api_import = False
    for name, path in modules.items():
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or ""):
                parts = node.module.split(".")
                if "sync_api" in parts:
                    offenders.append(f"{name}: import from {node.module!r}")
                if "async_api" in parts:
                    if name == "ui_evidence":
                        saw_async_api_import = True
                    else:
                        offenders.append(f"{name}: import from {node.module!r}")
                if parts[0] == "playwright" and name in ("doctor", "commands"):
                    playwright_imports.append(f"{name}: import from {node.module!r}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    parts = alias.name.split(".")
                    if "sync_api" in parts:
                        offenders.append(f"{name}: import {alias.name!r}")
                    if "async_api" in parts:
                        if name == "ui_evidence":
                            saw_async_api_import = True
                        else:
                            offenders.append(f"{name}: import {alias.name!r}")
                    if parts[0] == "playwright" and name in ("doctor", "commands"):
                        playwright_imports.append(f"{name}: import {alias.name!r}")
            elif isinstance(node, ast.Name) and node.id == "sync_playwright":
                offenders.append(f"{name}: Name(sync_playwright) at line {node.lineno}")
            elif isinstance(node, ast.Attribute) and node.attr == "sync_playwright":
                offenders.append(f"{name}: Attribute(.sync_playwright) at line {node.lineno}")

    assert offenders == [], (
        "sync_playwright() raises inside a running asyncio loop; no live "
        f"reference to it may survive in any touched module's code: {offenders}")
    assert saw_async_api_import, (
        "positive control: the loop-safe async_api import must still be "
        "present in ui_evidence.py's code — otherwise the assertions above "
        "are vacuous")
    assert playwright_imports == [], (
        "doctor.py/commands.py must never import any playwright.* module — "
        f"the chromium layer is pure path logic: {playwright_imports}")


@_skip_if_absent
def test_doctor_row_agrees_across_contexts():
    """The customer-visible three-way contradiction, pinned end to end: the
    real `visual_walks_row()` (no `available=` override — the actual probe
    `nh doctor` and `nh doctor --fix-walks` both call) must agree with
    itself sync vs. under `asyncio.run`, exactly like `nh doctor`'s async
    `_go` vs. `nh doctor --fix-walks`'s sync call do in production."""

    async def _probe():
        await asyncio.sleep(0)
        return doctor_mod.visual_walks_row()

    sync_row = doctor_mod.visual_walks_row()
    async_row = asyncio.run(_probe())

    assert sync_row["available"] == async_row["available"], (
        f"sync={sync_row!r} async={async_row!r}")
    assert sync_row["available"] is True


def test_docstrings_do_not_claim_sync_facade_is_loop_safe():
    """AC3: the falsified claim that the sync driver is "safe to call...
    running asyncio loop" must be gone from `playwright_available`'s
    docstring, replaced by an honest description naming the loop-safe
    `async_api` seam instead."""
    doc = ui_evidence.playwright_available.__doc__ or ""
    lowered = doc.lower()
    assert not ("safe to call" in lowered and "running" in lowered
                and "asyncio" in lowered and "loop" in lowered), doc
    assert "async_api" in doc, (
        "positive control: the docstring must name the replacement, "
        f"loop-safe seam it actually uses — got: {doc!r}")


def test_playwright_is_not_a_default_dependency():
    """Lean-stack pin, independently derived from `pyproject.toml` (not
    from the probe under test): playwright stays opt-in, in the `e2e`
    dependency group only — this fix must not change that."""
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
