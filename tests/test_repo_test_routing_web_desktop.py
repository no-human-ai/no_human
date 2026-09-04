"""Change-aware test routing for no_human's OWN repo (web/desktop scopes).

no_human now SHIPS a real `.no_human.yml` at the repo root (commit 8b937bd)
with `test_commands` rules for `web/**` and `desktop/**`, so
`_resolve_test_target` (orchestrator.py:4750) no longer always falls back to
the default `test_cmd` for this repo — a web-only change routes to the fast
web command instead of paying the full ~2700-test Python suite. The agent
that authored this test file is hard-blocked from writing `.no_human.yml`
itself (src/no_human/agent/guard.py:250 — "the operator's contract with the
agent"), so that file is a human-owned change surface; this file only adds
test coverage for it.

Two things are exercised here:

  1. `test_real_repo_root_config_loads_scoped_rules` reads the ACTUAL shipped
     `.no_human.yml` at the real repo root via `load_repo_config`, asserting
     only its SHAPE (two scoped rules, each with a non-empty command) so a
     routine edit to that file's command text doesn't break this test.
  2. The rest of this file writes a small self-contained sample config (same
     schema, `web/**` + `desktop/**`) to a `tmp_path` repo and drives the real,
     unmodified `load_repo_config` / `_clean_rules` / `_is_scoped_glob` /
     `_resolve_test_target` code paths against it, to prove the routing
     MECHANISM: all-web routes to the web command, all-desktop to the desktop
     command, and any non-unanimous (mixed) change set falls back to the
     operator-owned default — regardless of the exact commands the real file
     happens to declare today.
"""

import re
from pathlib import Path
from types import SimpleNamespace


from no_human.config import load_config
from no_human.core.orchestrator import Orchestrator
from no_human.notify.slack import SlackNotifier
from no_human.profile import ProjectProfile
from no_human.project_config import (
    REPO_CONFIG_RELPATH,
    _is_scoped_glob,
    load_repo_config,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

# A small self-contained sample, same schema as the real shipped file, used to
# exercise the routing MECHANISM (see module docstring point 2).
SAMPLE_NO_HUMAN_YML = """\
test_commands:
  - glob: 'web/**'
    command: 'npm test'
    cwd: 'web'
  - glob: 'desktop/**'
    command: 'npm test'
    cwd: 'desktop'
"""


class _Backend:
    async def run(self, *a, **k):  # pragma: no cover - never runs here
        raise AssertionError("backend should not run in routing tests")


def _orch(store, tmp_path):
    cfg = load_config(tmp_path / "config.yaml")
    return Orchestrator(store, cfg.data, _Backend(), SlackNotifier(None))


def _write_proposed_config(repo_path):
    repo_path.mkdir(parents=True, exist_ok=True)
    (repo_path / REPO_CONFIG_RELPATH).write_text(SAMPLE_NO_HUMAN_YML)


def _no_human_profile(repo_path):
    """A profile shaped like no_human's own onboarded profile: python-pytest,
    no test_commands of its own (so the repo config is free to fill them —
    apply_repo_config only fills an EMPTY list, never overrides a DB one)."""
    return ProjectProfile(
        repo_path=str(repo_path), ecosystem="python-pytest",
        install_cmd="uv sync", test_cmd="uv run pytest -q",
        proven={"test_cmd": True}, confirmed=True,
    )


# --- schema validity: survives _is_scoped_glob / _clean_rules ------------- #

def test_proposed_config_globs_are_scoped_and_survive_clean_rules(tmp_path):
    repo = tmp_path / "repo"
    _write_proposed_config(repo)
    cfg = load_repo_config(repo)
    assert "test_commands" in cfg and cfg["test_commands"], cfg
    rules = cfg["test_commands"]
    assert [r["glob"] for r in rules] == ["web/**", "desktop/**"]
    assert [r["command"] for r in rules] == ["npm test", "npm test"]
    assert [r["cwd"] for r in rules] == ["web", "desktop"]
    # test_cmd itself must never be settable from this file.
    assert "test_cmd" not in cfg


def test_proposed_config_does_not_declare_test_cmd_override(tmp_path):
    """The whitelist only reads test_commands/playbook_hints/forbidden_paths_extra
    (project_config.py:44) — a `test_cmd` key in the file would be silently
    dropped, not honoured, but the config also must not even attempt it."""
    assert "test_cmd:" not in SAMPLE_NO_HUMAN_YML
    assert "test_cmd :" not in SAMPLE_NO_HUMAN_YML


# --- routing: web-only / desktop-only / mixed ----------------------------- #

async def test_web_only_change_routes_to_npm_test_under_web(store, tmp_path):
    repo = tmp_path / "repo"
    _write_proposed_config(repo)
    (repo / "web").mkdir(parents=True, exist_ok=True)
    await store.upsert_profile(_no_human_profile(repo))
    orch = _orch(store, tmp_path)
    orch._agent_edited_files = {
        str(repo / "web" / "src" / "App.jsx"),
        str(repo / "web" / "src" / "App.test.mjs"),
    }
    cmd, cwd = await orch._resolve_test_target(SimpleNamespace(path=repo))
    assert cmd == "npm test"
    assert str(cwd) == str(repo / "web")


async def test_desktop_only_change_routes_to_npm_test_under_desktop(store, tmp_path):
    repo = tmp_path / "repo"
    _write_proposed_config(repo)
    (repo / "desktop").mkdir(parents=True, exist_ok=True)
    await store.upsert_profile(_no_human_profile(repo))
    orch = _orch(store, tmp_path)
    orch._agent_edited_files = {
        str(repo / "desktop" / "main.mjs"),
        str(repo / "desktop" / "mainIpc.test.mjs"),
    }
    cmd, cwd = await orch._resolve_test_target(SimpleNamespace(path=repo))
    assert cmd == "npm test"
    assert str(cwd) == str(repo / "desktop")


async def test_mixed_web_and_python_change_falls_back_to_default(store, tmp_path):
    """A change touching BOTH web/ and python (e.g. src/no_human/...) is not
    unanimous under any single rule, so routing must fall back to the
    operator-owned default `test_cmd` — the full suite. Getting this wrong
    would let a Python regression skip the Python suite entirely."""
    repo = tmp_path / "repo"
    _write_proposed_config(repo)
    (repo / "web").mkdir(parents=True, exist_ok=True)
    await store.upsert_profile(_no_human_profile(repo))
    orch = _orch(store, tmp_path)
    orch._agent_edited_files = {
        str(repo / "web" / "src" / "App.jsx"),
        str(repo / "src" / "no_human" / "orchestrator.py"),
    }
    cmd, cwd = await orch._resolve_test_target(SimpleNamespace(path=repo))
    assert cmd == "uv run pytest -q" and cwd is None


async def test_mixed_web_and_desktop_change_falls_back_to_default(store, tmp_path):
    """web/** and desktop/** are separate scopes: a change spanning both is
    also non-unanimous and must fall back to the default, not silently pick
    one of the two areas."""
    repo = tmp_path / "repo"
    _write_proposed_config(repo)
    (repo / "web").mkdir(parents=True, exist_ok=True)
    (repo / "desktop").mkdir(parents=True, exist_ok=True)
    await store.upsert_profile(_no_human_profile(repo))
    orch = _orch(store, tmp_path)
    orch._agent_edited_files = {
        str(repo / "web" / "src" / "App.jsx"),
        str(repo / "desktop" / "main.mjs"),
    }
    cmd, cwd = await orch._resolve_test_target(SimpleNamespace(path=repo))
    assert cmd == "uv run pytest -q" and cwd is None


async def test_other_directory_change_falls_back_to_default(store, tmp_path):
    """A change outside both named scopes (docs/, tests/, root files) is not
    matched by either rule and must use the default suite."""
    repo = tmp_path / "repo"
    _write_proposed_config(repo)
    await store.upsert_profile(_no_human_profile(repo))
    orch = _orch(store, tmp_path)
    orch._agent_edited_files = {str(repo / "docs" / "README.md")}
    cmd, cwd = await orch._resolve_test_target(SimpleNamespace(path=repo))
    assert cmd == "uv run pytest -q" and cwd is None


# --- the REAL shipped .no_human.yml at the repo root ---------------------- #

def test_real_repo_root_config_loads_scoped_rules():
    """`load_repo_config` on THIS repo's actual root must return a non-empty
    `test_commands` list (not `{}`) — the whole point of this task. Kept
    shape-only (two scoped rules, each with a non-empty command) so a routine
    edit to the shipped file's command text doesn't break this test."""
    assert (REPO_ROOT / REPO_CONFIG_RELPATH).is_file(), (
        f"{REPO_CONFIG_RELPATH} must exist at the repo root"
    )
    cfg = load_repo_config(REPO_ROOT)
    assert cfg != {}
    rules = cfg["test_commands"]
    assert len(rules) == 2
    globs = {r["glob"] for r in rules}
    assert globs == {"web/**", "desktop/**"}
    for rule in rules:
        assert _is_scoped_glob(rule["glob"])
        assert isinstance(rule["command"], str) and rule["command"].strip()


def test_no_routed_command_names_a_test_file_that_is_absent():
    """A routed command may not name a test path this checkout does not carry.

    This shipped config used to run
    `pytest tests/test_no_banned_terms.py tests/test_identity_scrub_guard.py ...`
    by path. Both of those are export-DROPPED, so in any checkout built from the
    export the command died with file-not-found on every `web/**` and
    `desktop/**` change — and, because `_resolve_test_target` routes the
    harness's own gate through these same rules, that is the gate failing, not
    merely a coder's convenience run. They are now selected by `-m repoguard`.

    The assertion is deliberately about EXISTENCE, not about the marker: a
    future author may legitimately name a path here, but never one that is
    missing. `paths_seen` is asserted so this cannot pass by examining nothing
    — today it is legitimately empty of test paths, and that is what is pinned.
    """
    cfg = load_repo_config(REPO_ROOT)
    rules = cfg["test_commands"]
    assert rules, "no rules to examine"
    checked = 0
    for rule in rules:
        cmd = rule["command"]
        checked += 1
        for token in re.findall(r"(?<![\w/-])tests/[\w./-]+\.py", cmd):
            assert (REPO_ROOT / token).is_file(), (
                f"{REPO_CONFIG_RELPATH} rule {rule['glob']!r} names {token!r}, "
                f"which does not exist in this checkout. Select repo-wide "
                f"guards with `-m repoguard` instead of naming files."
            )
    assert checked == len(rules)


# --- untrusted-input contract: an unscoped glob is dropped, not honoured -- #

def test_unscoped_glob_rule_is_rejected_by_clean_rules(tmp_path):
    """A rule whose glob has no wildcard-free leading directory (e.g. one
    starting with a wildcard) must be dropped by `_clean_rules` — the
    untrusted-input contract (project_config.py:49-64) stays enforced even
    for a repo-shaped config that otherwise looks legitimate."""
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    (repo / REPO_CONFIG_RELPATH).write_text(
        "test_commands:\n"
        "  - glob: '*.py'\n"
        "    command: 'true'\n"
        "  - glob: 'web/**'\n"
        "    command: 'npm test'\n"
    )
    cfg = load_repo_config(repo)
    rules = cfg["test_commands"]
    assert [r["glob"] for r in rules] == ["web/**"]
    assert all(_is_scoped_glob(r["glob"]) for r in rules)
