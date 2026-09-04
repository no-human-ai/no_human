"""The published wheel must not resolve an MCP SDK without the module we import.

`intake/mcp_bridge.py` imports `mcp.server.mcpserver`; the SDK introduced that
path in 2.0.0 and removed `mcp.server.fastmcp`. The requirement is `mcp>=2` so
that a fresh install resolves 2.x where `mcp.server.mcpserver` exists.

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
