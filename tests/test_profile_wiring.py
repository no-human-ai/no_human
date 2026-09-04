"""Orchestrator consumes a confirmed ProjectProfile: test_cmd resolution
precedence + usability gating (replaces the detect_command heuristic)."""

from types import SimpleNamespace


from no_human.config import load_config
from no_human.core.orchestrator import Orchestrator
from no_human.notify.slack import SlackNotifier
from no_human.profile import ProjectProfile


class _Backend:
    async def run(self, *a, **k):  # pragma: no cover - not exercised here
        raise AssertionError("backend should not run in resolution tests")


def _orch(store, tmp_path, *, tests_command=None):
    cfg = load_config(tmp_path / "config.yaml")
    if tests_command is not None:
        cfg.data.setdefault("tests", {})["command"] = tests_command
    return Orchestrator(store, cfg.data, _Backend(), SlackNotifier(None))


def _repo(path):
    return SimpleNamespace(path=path)


def _usable(repo_path):
    return ProjectProfile(
        repo_path=str(repo_path), ecosystem="node",
        install_cmd="npm ci", test_cmd="npm test",
        ci={"backend": "gitlab", "enabled": True, "project": "x/y"},
        derived_from=["package.json"], proven={"test_cmd": True}, confirmed=True,
    )


async def test_usable_profile_test_cmd_used(store, tmp_path):
    repo_path = tmp_path / "repo"; repo_path.mkdir()
    await store.upsert_profile(_usable(repo_path))
    orch = _orch(store, tmp_path)
    assert await orch._resolve_test_cmd(_repo(repo_path)) == "npm test"


async def test_explicit_config_command_wins_over_profile(store, tmp_path):
    repo_path = tmp_path / "repo"; repo_path.mkdir()
    await store.upsert_profile(_usable(repo_path))
    orch = _orch(store, tmp_path, tests_command="pytest -q --override")
    assert await orch._resolve_test_cmd(_repo(repo_path)) == "pytest -q --override"


async def test_unconfirmed_profile_is_not_usable(store, tmp_path):
    repo_path = tmp_path / "repo"; repo_path.mkdir()
    prof = _usable(repo_path); prof.confirmed = False
    await store.upsert_profile(prof)
    orch = _orch(store, tmp_path)
    # not usable → None → run_tests falls back to detect_command
    assert await orch._usable_profile(repo_path) is None
    assert await orch._resolve_test_cmd(_repo(repo_path)) is None


async def test_proven_but_unconfirmed_or_unproven_blocked(store, tmp_path):
    repo_path = tmp_path / "repo"; repo_path.mkdir()
    # confirmed but test_cmd NOT proven → still not usable (trust requires proof).
    prof = _usable(repo_path); prof.proven = {}
    await store.upsert_profile(prof)
    orch = _orch(store, tmp_path)
    assert await orch._usable_profile(repo_path) is None


async def test_auto_confirm_proven_makes_proven_unconfirmed_usable(store, tmp_path):
    # P1: a PROVEN but human-unconfirmed profile becomes usable when the
    # auto_confirm_proven policy is opted in.
    repo_path = tmp_path / "repo"; repo_path.mkdir()
    prof = _usable(repo_path); prof.confirmed = False
    await store.upsert_profile(prof)
    orch = _orch(store, tmp_path)
    assert await orch._usable_profile(repo_path) is None      # default: off
    orch.config.setdefault("profile", {})["auto_confirm_proven"] = True
    got = await orch._usable_profile(repo_path)
    assert got is not None and got.test_cmd == "npm test"
    assert await orch._resolve_test_cmd(_repo(repo_path)) == "npm test"


async def test_auto_confirm_proven_still_requires_proof(store, tmp_path):
    # P1: the policy removes the human click, NEVER the proof requirement.
    repo_path = tmp_path / "repo"; repo_path.mkdir()
    prof = _usable(repo_path); prof.confirmed = False; prof.proven = {}
    await store.upsert_profile(prof)
    orch = _orch(store, tmp_path)
    orch.config.setdefault("profile", {})["auto_confirm_proven"] = True
    assert await orch._usable_profile(repo_path) is None


async def test_no_profile_falls_back_to_none(store, tmp_path):
    repo_path = tmp_path / "repo"; repo_path.mkdir()
    orch = _orch(store, tmp_path)
    assert await orch._resolve_test_cmd(_repo(repo_path)) is None


async def test_profile_loaded_from_repo_yaml_when_not_in_store(store, tmp_path):
    repo_path = tmp_path / "repo"; repo_path.mkdir()
    _usable(repo_path).save()  # writes .no_human/project.yml only
    orch = _orch(store, tmp_path)
    prof = await orch._usable_profile(repo_path)
    assert prof is not None and prof.test_cmd == "npm test"


def _multilang(repo_path):
    """A profile with change-scoped test commands (web vs default)."""
    return ProjectProfile(
        repo_path=str(repo_path), ecosystem="python",
        install_cmd="uv sync", test_cmd="uv run pytest -q",
        test_commands=[{"glob": "web/**", "command": "node --test src/", "cwd": "web"}],
        derived_from=["pyproject.toml"], proven={"test_cmd": True}, confirmed=True,
    )


async def test_web_only_change_routes_to_web_tests(store, tmp_path):
    repo_path = tmp_path / "repo"; repo_path.mkdir()
    (repo_path / "web").mkdir()
    await store.upsert_profile(_multilang(repo_path))
    orch = _orch(store, tmp_path)
    orch._agent_edited_files = {str(repo_path / "web" / "src" / "App.jsx")}
    cmd, cwd = await orch._resolve_test_target(_repo(repo_path))
    assert cmd == "node --test src/"
    assert str(cwd) == str(repo_path / "web")


async def test_python_change_uses_the_default_command(store, tmp_path):
    repo_path = tmp_path / "repo"; repo_path.mkdir()
    await store.upsert_profile(_multilang(repo_path))
    orch = _orch(store, tmp_path)
    orch._agent_edited_files = {str(repo_path / "src" / "no_human" / "x.py")}
    cmd, cwd = await orch._resolve_test_target(_repo(repo_path))
    assert cmd == "uv run pytest -q" and cwd is None


async def test_mixed_change_falls_back_to_the_safe_default(store, tmp_path):
    """A change spanning web AND python is not unanimous — run the heavier
    default, never a partial validation."""
    repo_path = tmp_path / "repo"; repo_path.mkdir()
    await store.upsert_profile(_multilang(repo_path))
    orch = _orch(store, tmp_path)
    orch._agent_edited_files = {
        str(repo_path / "web" / "a.jsx"), str(repo_path / "src" / "b.py")}
    cmd, cwd = await orch._resolve_test_target(_repo(repo_path))
    assert cmd == "uv run pytest -q" and cwd is None


async def test_no_rules_is_identical_to_resolve_test_cmd(store, tmp_path):
    """Zero-degradation guarantee: a profile without test_commands behaves
    exactly as _resolve_test_cmd (no cwd, same command)."""
    repo_path = tmp_path / "repo"; repo_path.mkdir()
    await store.upsert_profile(_usable(repo_path))
    orch = _orch(store, tmp_path)
    orch._agent_edited_files = {str(repo_path / "web" / "App.jsx")}
    cmd, cwd = await orch._resolve_test_target(_repo(repo_path))
    assert cmd == "npm test" and cwd is None


async def test_resumed_web_change_routes_to_web_when_no_session_edits(store, tmp_path):
    """On RESUME the coder may make no new edits (the work is already committed
    at the [WIP-BLOCKED] checkpoint), so _agent_edited_files is empty. Routing
    must fall back to the attempt's committed change set — else a resumed web
    task wrongly runs the backend suite. This is the exact shape that stalled
    the dogfood tasks (537a1535)."""
    repo_path = tmp_path / "repo"; repo_path.mkdir()
    (repo_path / "web").mkdir()
    await store.upsert_profile(_multilang(repo_path))
    orch = _orch(store, tmp_path)
    orch._agent_edited_files = set()  # resume: nothing edited this session
    repo = SimpleNamespace(
        path=repo_path,
        changed_files=lambda: ["web/src/relativeTime.js",
                               "web/src/relativeTime.test.mjs"],
    )
    cmd, cwd = await orch._resolve_test_target(repo)
    assert cmd == "node --test src/"
    assert str(cwd) == str(repo_path / "web")


async def test_no_edits_and_no_committed_diff_uses_default(store, tmp_path):
    """No signal at all (empty session edits, empty diff) → the default."""
    repo_path = tmp_path / "repo"; repo_path.mkdir()
    await store.upsert_profile(_multilang(repo_path))
    orch = _orch(store, tmp_path)
    orch._agent_edited_files = set()
    repo = SimpleNamespace(path=repo_path, changed_files=lambda: [])
    cmd, cwd = await orch._resolve_test_target(repo)
    assert cmd == "uv run pytest -q" and cwd is None


async def test_changed_files_failure_falls_back_to_default(store, tmp_path):
    """A git error while reading the committed diff must never crash routing —
    it degrades to the default command."""
    repo_path = tmp_path / "repo"; repo_path.mkdir()
    await store.upsert_profile(_multilang(repo_path))
    orch = _orch(store, tmp_path)
    orch._agent_edited_files = set()

    def _boom():
        raise RuntimeError("git exploded")

    repo = SimpleNamespace(path=repo_path, changed_files=_boom)
    cmd, cwd = await orch._resolve_test_target(repo)
    assert cmd == "uv run pytest -q" and cwd is None
