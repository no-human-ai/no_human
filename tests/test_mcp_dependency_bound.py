"""The published wheel must not resolve an MCP SDK without the module we import.

2026-08-20, found by running the exact command the official MCP Registry
publishes for this package:

    $ uvx no-human mcp-serve
    ModuleNotFoundError: No module named 'mcp.server.fastmcp'

`intake/mcp_bridge.py` then imported `mcp.server.fastmcp`; the SDK removed that
path in 2.0.0. The requirement was `mcp>=1.28.0` with no upper bound, so a fresh
install from PyPI resolved 2.0.0 and `nh mcp-serve` — a documented entry point,
the Claude Code plugin's command, and the registry listing's command — died at
import. Nothing caught it because every lane that runs the code resolves
through `uv.lock`, which pinned 1.29.0: CI, the MCP container, the desktop
bundles and every dev checkout were all testing a version the user never got.

2026-09-05 (public issue #16): the bridge was ported to `mcp.server.mcpserver`
and the requirement moved to `mcp>=2,<3`. The floor is what the import needs;
the cap is the same lesson again, one major up — when 3.0 moves the module,
the cap keeps a fresh install on a version the bridge imports, and the tests
below are what notice a cap that quietly went away.

These tests are about the DECLARED bound, not the locked one, because the
declared bound is the only thing a `pip install no-human` obeys.
"""
from __future__ import annotations

import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[1]

#: The first SDK version that ships `mcp.server.mcpserver`. Lowering the
#: bound below this without changing the import would re-open ModuleNotFoundError.
FIRST_SDK_WITH_MCPSERVER = Version("2.0.0")

#: The first major the bridge has NOT been ported to. Admitting it without
#: porting the import re-opens the 2026-08-20 bug one major up.
FIRST_UNPORTED_MAJOR = Version("3.0.0")


def _declared_mcp_requirement() -> Requirement:
    deps = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["dependencies"]
    mcp = [d for d in deps if Requirement(d).name == "mcp"]
    assert len(mcp) == 1, f"expected exactly one mcp requirement, got {mcp}"
    return Requirement(mcp[0])


def test_the_declared_mcp_requirement_excludes_sdks_before_2():
    """The bound must reject 1.x versions since they lack mcp.server.mcpserver."""
    req = _declared_mcp_requirement()
    for candidate in ("1.27.1", "1.28.0", "1.29.0", "1.29.1"):
        assert not req.specifier.contains(Version(candidate), prereleases=True), (
            f"`{req}` admits mcp {candidate}, which has no mcp.server.mcpserver")


def test_the_declared_mcp_requirement_admits_sdk_with_mcpserver():
    req = _declared_mcp_requirement()
    assert req.specifier.contains(FIRST_SDK_WITH_MCPSERVER, prereleases=True), (
        f"`{req}` excludes mcp {FIRST_SDK_WITH_MCPSERVER}, which ships "
        "`mcp.server.mcpserver`.")


def test_the_declared_mcp_requirement_excludes_the_next_major_and_its_prereleases():
    """The cap must reject 3.x, pre-releases included, not only 3.0.0 final.

    PEP 440's exclusive `<3` already does (`SpecifierSet("<3").contains("3.0.0rc1",
    prereleases=True)` is False), so this passes today without a wider bound —
    it exists to catch the mutation that WOULD re-open the hole: dropping the
    cap, or raising it (`<4` admits every 3.x, pre-releases included).
    """
    req = _declared_mcp_requirement()
    for candidate in ("3.0.0a1", "3.0.0rc1", str(FIRST_UNPORTED_MAJOR), "3.1.0"):
        assert not req.specifier.contains(Version(candidate), prereleases=True), (
            f"`{req}` admits mcp {candidate}, a major the bridge has not been "
            "ported to — a fresh `pip install no-human` would resolve it and "
            "`nh mcp-serve` could die at import. Cap the requirement, or port "
            "`no_human/intake/mcp_bridge.py` first.")


def test_the_declared_mcp_requirement_still_admits_the_locked_version():
    """The bound must admit what we actually lock in uv.lock."""
    req = _declared_mcp_requirement()
    lock = (ROOT / "uv.lock").read_text()
    marker = '\nname = "mcp"\nversion = "'
    locked = Version(lock.split(marker, 1)[1].split('"', 1)[0])
    assert req.specifier.contains(locked), (
        f"`{req}` excludes the locked mcp {locked} — the declared bound and the "
        "lockfile disagree about what this package runs on.")


def test_the_module_the_bridge_imports_exists_in_the_installed_sdk():
    """A control: the bound is only meaningful while this import is the one we
    make. If the bridge is ported to a different module path, this test — and
    the constant above — must move with it."""
    import importlib.util

    bridge = (ROOT / "src/no_human/intake/mcp_bridge.py").read_text()
    assert "from mcp.server.mcpserver import" in bridge, (
        "the bridge no longer imports mcp.server.mcpserver — update "
        "FIRST_SDK_WITH_MCPSERVER and this file to the new import")
    assert importlib.util.find_spec("mcp.server.mcpserver") is not None, (
        "the installed mcp SDK does not provide mcp.server.mcpserver")
