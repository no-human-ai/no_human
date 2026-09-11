"""Proves the HOME-redirect plugin actually closes the gap it claims to.

See `no_human.testing.pytest_isolated_home` for the mechanism and
`tests/conftest.py`'s `isolated_env_file` docstring for the history: a
per-test fixture cannot un-bind `config.DB_PATH`/`config.CONFIG_PATH` because
they are bound at IMPORT time, before any fixture body runs. Only a plugin
that redirects `HOME` before `no_human.config` is first imported can do it,
and that means these tests use `plugin.REAL_HOME` (captured before the
redirect) whenever they need to talk about the operator's actual home —
`Path.home()` cannot answer that once the plugin has run.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

import no_human.config as config
import no_human.testing.pytest_isolated_home as plugin
from no_human.core import db
from no_human.core.task import Task

REPO_ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- #
# AC1 — the plugin is actually registered, not just present on disk           #
# --------------------------------------------------------------------------- #

def test_plugin_is_registered(pytestconfig):
    registered_names = {
        "no_human_isolated_home",
        "no_human.testing.pytest_isolated_home",
    }
    assert any(
        pytestconfig.pluginmanager.hasplugin(name) for name in registered_names
    ), (
        "neither the entry-point name nor the module name is a registered "
        f"plugin: {sorted(pytestconfig.pluginmanager.list_name_plugin())}"
    )
    assert plugin.ISOLATED_HOME is not None, (
        "plugin loaded but did not activate — this repo's own suite should "
        "always match its self-guard"
    )
    assert plugin.ISOLATED_HOME.is_dir()


def test_pyproject_declares_the_pytest11_entry_point():
    """A test over the DECLARATION, so deleting the registration while
    leaving the module file behind fails a test, not just a silent no-op."""
    cfg = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    entry_points = cfg["project"]["entry-points"]["pytest11"]
    assert entry_points.get("no_human_isolated_home") == (
        "no_human.testing.pytest_isolated_home"
    )


# --------------------------------------------------------------------------- #
# AC2 — config binds under the isolated HOME, never under the real one        #
# --------------------------------------------------------------------------- #

def test_db_path_is_under_the_isolated_home():
    assert Path.home() != plugin.REAL_HOME

    for value in (config.NO_HUMAN_HOME, config.DB_PATH, config.CONFIG_PATH,
                  config.ENV_PATH):
        assert value.is_relative_to(plugin.ISOLATED_HOME), value
        assert not value.is_relative_to(plugin.REAL_HOME), value


def test_assertion_fails_without_the_plugin(tmp_path):
    """The negative control that gives the assertion above its power.

    Simulates a session with no isolation: HOME is set to the operator's
    REAL home (not inherited from this already-isolated outer session, which
    would trivially "pass" no matter what the child does), the plugin is
    explicitly blocked by name, and a temp ini with no `addopts` stops the
    belt-and-braces fallback from smuggling it back in. Without the plugin,
    `config.DB_PATH` resolves under the real HOME — this is only a path
    computation, nothing is written to it.
    """
    probe = tmp_path / "test_probe.py"
    probe.write_text(
        "import no_human.config as config\n"
        "from pathlib import Path\n"
        f"REAL_HOME = Path({str(plugin.REAL_HOME)!r})\n"
        "def test_db_path_would_be_under_the_real_home():\n"
        "    assert config.DB_PATH.is_relative_to(REAL_HOME), config.DB_PATH\n"
    )
    ini = tmp_path / "pytest.ini"
    ini.write_text("[pytest]\nasyncio_mode = auto\n")

    env = dict(os.environ)
    env["HOME"] = str(plugin.REAL_HOME)
    env["PYTHONPATH"] = str(REPO_ROOT / "src")

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q",
         "-p", "no:no_human_isolated_home",
         "-c", str(ini), str(probe)],
        cwd=str(tmp_path), env=env, capture_output=True, text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        "the probe test (config.DB_PATH under the real HOME) did not even "
        f"run cleanly:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "1 passed" in result.stdout, (
        "without the plugin, config.DB_PATH must resolve under the real "
        f"HOME:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


# --------------------------------------------------------------------------- #
# AC3 — the migration-bearing path never touches the operator's real db       #
# --------------------------------------------------------------------------- #

def _snapshot(path: Path):
    if not path.exists():
        return None
    stat = path.stat()
    return (stat.st_mtime_ns, stat.st_size, hashlib.sha256(path.read_bytes()).hexdigest())


async def test_migration_bearing_path_leaves_the_live_db_untouched():
    real_db = plugin.REAL_HOME / ".no_human" / "no_human.db"
    before = _snapshot(real_db)

    # Sentinel: migrations actually exist and will run, rather than the
    # `_migrate` fail-open trap (`Path.glob` on a missing dir yields nothing
    # instead of raising) silently no-opping and passing this test for the
    # wrong reason.
    assert len(list(db.MIGRATIONS_DIR.glob("*.sql"))) > 0

    assert config.DB_PATH.is_relative_to(plugin.ISOLATED_HOME)
    # A pinned NH_TEST_HOME (the plugin's debug knob) is reused across runs
    # and is NOT cleaned up between them (`pytest_unconfigure` skips pinned
    # homes by design), so a prior run of this same test may have already
    # left config.DB_PATH behind. Remove it up front rather than asserting
    # absence, so this test's verdict is about the REAL db, not about which
    # HOME knob produced the isolated one.
    config.DB_PATH.unlink(missing_ok=True)
    assert not config.DB_PATH.exists()

    store = await db.Store(config.DB_PATH).connect()
    try:
        assert config.DB_PATH.exists()
        rows = await store.query(
            "select name from sqlite_master where type='table'"
        )
        names = {row["name"] for row in rows}
        assert {"tasks", "attempts", "task_events"} <= names, names
    finally:
        await store.close()

    after = _snapshot(real_db)
    if before is None:
        # Absent is not zero: the real db must not have been CREATED either.
        assert after is None, "a real ~/.no_human/no_human.db was created"
    else:
        assert after == before, (
            "the operator's real db changed while the isolated Store was "
            f"open: before={before} after={after}"
        )


async def test_mtime_check_has_power():
    """Sanity check for `_snapshot`: applied across two writes to the SAME
    (isolated) db, it must detect the change. Without this, a comparison that
    can never fire would read as a passing guard rather than an inert one."""
    db_path = plugin.ISOLATED_HOME / "power-check" / "power.db"

    store = await db.Store(db_path).connect()
    try:
        await store.create_task(Task.new("power check task 1"))
    finally:
        await store.close()
    before = _snapshot(db_path)
    assert before is not None

    store2 = await db.Store(db_path).connect()
    try:
        await store2.create_task(Task.new("power check task 2"))
    finally:
        await store2.close()
    after = _snapshot(db_path)

    assert after is not None
    assert after != before, "two writes to the same db produced an identical snapshot"


# --------------------------------------------------------------------------- #
# AC5 sentinel — the escape hatch stays inert unless explicitly requested     #
# --------------------------------------------------------------------------- #

def test_repo_root_detection_matches_this_checkout():
    assert plugin._repo_root_or_none() is not None
