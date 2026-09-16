"""`core/profile_resolve.py` — store/orchestrator-free profile resolution.

Extracted from `Orchestrator` so a caller with only a `store` and a `config`
(`nh approve` in `cli/commands.py`, the board's Approve button in
`api/app.py`) can resolve the repo's proven test command without
constructing a full `Orchestrator` — needed so the merge-time gate
(`vcs/approve_merge.land_task`) can run the repo profile's own test_cmd
instead of unconditionally forcing `python -m pytest`. This module pins:
(1) `resolve_test_cmd`'s precedence and fallback behaviour standalone, (2)
`Orchestrator`'s own helpers stay thin wrappers over it, and (3) both real
merge callers actually wire `test_cmd=` through to `land_task`.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

from no_human.config import load_config
from no_human.core import profile_resolve
from no_human.core.orchestrator import Orchestrator
from no_human.notify.slack import SlackNotifier
from no_human.profile import ProjectProfile


class _Backend:
    async def run(self, *a, **k):  # pragma: no cover - not exercised here
        raise AssertionError("backend should not run in resolution tests")


def _usable_profile(repo_path):
    return ProjectProfile(
        repo_path=str(repo_path), ecosystem="node",
        install_cmd="npm ci", test_cmd="npm test",
        ci={"backend": "gitlab", "enabled": True, "project": "x/y"},
        derived_from=["package.json"], proven={"test_cmd": True}, confirmed=True,
    )


# --------------------------------------------------------------------------- #
# resolve_test_cmd — standalone (no Orchestrator)                            #
# --------------------------------------------------------------------------- #

async def test_resolve_test_cmd_prefers_the_explicit_config_override(store, tmp_path):
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    await store.upsert_profile(_usable_profile(repo_path))
    config = {"tests": {"command": "pytest -q --override"}}
    got = await profile_resolve.resolve_test_cmd(store, config, repo_path)
    assert got == "pytest -q --override"


async def test_resolve_test_cmd_falls_back_to_a_usable_profile(store, tmp_path):
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    await store.upsert_profile(_usable_profile(repo_path))
    got = await profile_resolve.resolve_test_cmd(store, {}, repo_path)
    assert got == "npm test"


async def test_resolve_test_cmd_returns_none_for_an_unusable_profile(store, tmp_path):
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    prof = _usable_profile(repo_path)
    prof.confirmed = False
    await store.upsert_profile(prof)
    got = await profile_resolve.resolve_test_cmd(store, {}, repo_path)
    assert got is None


# --------------------------------------------------------------------------- #
# Orchestrator's own helpers are thin wrappers over profile_resolve          #
# --------------------------------------------------------------------------- #

def _orch(store, tmp_path):
    cfg = load_config(tmp_path / "config.yaml")
    return Orchestrator(store, cfg.data, _Backend(), SlackNotifier(None))


async def test_orchestrator_helpers_are_thin_wrappers(store, tmp_path, monkeypatch):
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    sentinel_cmd = "SENTINEL: go test ./..."
    sentinel_profile = object()

    async def fake_resolve_test_cmd(store_, config_, repo_path_):
        assert repo_path_ == repo_path
        return sentinel_cmd

    async def fake_usable_profile(store_, config_, repo_path_, **kwargs):
        assert repo_path_ == repo_path
        return sentinel_profile

    monkeypatch.setattr(profile_resolve, "resolve_test_cmd", fake_resolve_test_cmd)
    monkeypatch.setattr(profile_resolve, "usable_profile", fake_usable_profile)

    orch = _orch(store, tmp_path)
    repo = SimpleNamespace(path=repo_path)
    assert await orch._resolve_test_cmd(repo) == sentinel_cmd
    assert await orch._usable_profile(repo_path) is sentinel_profile


# --------------------------------------------------------------------------- #
# Both real merge callers wire test_cmd= through to land_task                #
# --------------------------------------------------------------------------- #

def _land_task_call_nodes(tree):
    """Every AST Call node that invokes `land_task`, whether directly
    (`land_task(...)`, the CLI) or as the function argument to
    `asyncio.to_thread(land_task, ...)` (the API, off the event loop)."""
    calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name) and node.func.id == "land_task":
            calls.append(node)
        elif (node.args and isinstance(node.args[0], ast.Name)
              and node.args[0].id == "land_task"):
            calls.append(node)
    return calls


def test_both_merge_callers_pass_the_profile_test_command():
    import importlib

    import no_human.cli.commands as commands_mod
    # NOT `import no_human.api.app as app_mod`: `no_human/api/__init__.py`
    # does `from .app import app`, which rebinds the `app` attribute on the
    # `no_human.api` PACKAGE to the FastAPI instance — so a dotted `import
    # ... as` (an attribute-chain lookup under the hood) resolves to that
    # FastAPI object, not the submodule. `importlib.import_module` goes
    # through `sys.modules` directly and is immune to the shadowing.
    app_mod = importlib.import_module("no_human.api.app")

    for mod in (commands_mod, app_mod):
        src_path = Path(mod.__file__)
        tree = ast.parse(src_path.read_text(encoding="utf-8"), filename=str(src_path))
        calls = _land_task_call_nodes(tree)
        assert calls, f"no land_task call site found in {src_path}"
        for call in calls:
            kw_names = {kw.arg for kw in call.keywords}
            assert "test_cmd" in kw_names, (
                f"{src_path}:{call.lineno} calls land_task without "
                f"test_cmd= — the repo profile's own test command would "
                f"never reach the merge gate")
