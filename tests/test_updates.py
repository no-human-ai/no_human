"""The CLI update check must be invisible until it has something to say.

These observe the ARTIFACTS the check produces — the notice string a user would
read, the JSON file on disk, whether the network function was called at all —
rather than re-deriving expectations from the implementation. Every test below
goes red if the corresponding wire is cut while the individual helpers stay
correct.
"""

from __future__ import annotations

import json
import os
import stat

import pytest

from no_human import updates


@pytest.fixture(autouse=True)
def _update_check_enabled_for_this_module(monkeypatch):
    """conftest.py disables the check suite-wide (NH_NO_UPDATE_CHECK=1) so no
    other module's assertions depend on what PyPI holds. This module is the
    one that tests the check itself, so undo the suite-wide default here —
    and only here; `monkeypatch` restores it after each test, so nothing
    leaks into any other module."""
    monkeypatch.delenv(updates.DISABLE_ENV_VAR, raising=False)


# --------------------------------------------------------------------------- #
# version comparison
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "latest,current,expected",
    [
        ("0.2.0", "0.1.0", True),
        ("0.1.0", "0.1.0", False),          # equal is not newer
        ("0.0.9", "0.1.0", False),          # a downgrade is not an update
        ("0.10.0", "0.9.0", True),          # string compare gets this backwards
        ("1.0", "1.0.0", False),            # missing segments are zero
        ("1.0.1", "1.0", True),
        ("v0.2.0", "0.1.0", True),          # a leading v is tolerated
    ],
)
def test_is_newer_orders_releases(latest, current, expected):
    assert updates.is_newer(latest, current) is expected


@pytest.mark.parametrize(
    "junk",
    [None, "", "latest", "<!doctype html>", "0.1.x", 3, [], "..", "0..1"],
)
def test_unparseable_versions_are_never_announced_as_updates(junk):
    """A 404 page or an empty body must not read as "a new version exists"."""
    assert updates.parse_version(junk) is None
    assert updates.is_newer(junk, "0.1.0") is False


# --------------------------------------------------------------------------- #
# the notice
# --------------------------------------------------------------------------- #

def test_notice_names_both_versions_and_the_upgrade_command():
    notice = updates.update_notice("0.1.0", {"latest": "0.2.0"})
    assert notice is not None
    assert "0.2.0" in notice and "0.1.0" in notice
    # The distribution name, not the import name — `pip install no_human`
    # would send the operator to a package that does not exist. Sourced from
    # the one constant so the literal here cannot drift from pyproject.toml.
    assert f"pip install --upgrade {updates.DIST_NAME}" in notice


def test_no_notice_when_current_or_cache_is_empty():
    assert updates.update_notice("0.1.0", {"latest": "0.1.0"}) is None
    assert updates.update_notice("0.1.0", {}) is None
    assert updates.update_notice("0.1.0", {"latest": None}) is None


# --------------------------------------------------------------------------- #
# cache
# --------------------------------------------------------------------------- #

def test_cache_round_trips_through_a_real_file(tmp_path):
    assert updates.write_cache({"latest": "0.2.0", "last_check": 5}, tmp_path) is True
    assert (tmp_path / updates.CACHE_NAME).is_file(), "the cache must reach disk"
    assert updates.read_cache(tmp_path)["latest"] == "0.2.0"


def test_a_corrupt_cache_reads_as_empty_rather_than_raising(tmp_path):
    (tmp_path / updates.CACHE_NAME).write_text("{not json", encoding="utf-8")
    assert updates.read_cache(tmp_path) == {}


@pytest.mark.parametrize("junk", ["null", "[1,2]", '"a string"', "42"])
def test_valid_json_that_is_not_an_object_reads_as_empty(tmp_path, junk):
    (tmp_path / updates.CACHE_NAME).write_text(junk, encoding="utf-8")
    assert updates.read_cache(tmp_path) == {}


def test_a_missing_cache_directory_reads_as_empty(tmp_path):
    assert updates.read_cache(tmp_path / "nope") == {}


def test_an_unwritable_cache_reports_failure_instead_of_raising(tmp_path):
    """A read-only home must never break a command."""
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        assert updates.write_cache({"latest": "0.2.0"}, locked / "sub") is False
    finally:
        locked.chmod(stat.S_IRWXU)


def test_the_cache_write_is_atomic_and_leaves_no_temp_files(tmp_path):
    """A daemon thread can die mid-write; a partial file would poison every
    later run, so the write must be a rename, not an in-place truncate."""
    updates.write_cache({"latest": "0.2.0", "last_check": 1}, tmp_path)
    updates.write_cache({"latest": "0.3.0", "last_check": 2}, tmp_path)
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != updates.CACHE_NAME]
    assert leftovers == [], f"temp files left behind: {leftovers}"
    assert json.loads((tmp_path / updates.CACHE_NAME).read_text(encoding="utf-8"))["latest"] == "0.3.0"


# --------------------------------------------------------------------------- #
# is_published — evidence the distribution actually exists on the channel
# --------------------------------------------------------------------------- #

def test_is_published_false_without_cache(tmp_path):
    assert updates.is_published(tmp_path) is False


def test_is_published_false_on_unparsable_latest(tmp_path):
    updates.write_cache({"latest": "<html>"}, tmp_path)
    assert updates.is_published(tmp_path) is False


def test_is_published_true_on_real_version(tmp_path):
    updates.write_cache({"latest": "0.2.0"}, tmp_path)
    assert updates.is_published(tmp_path) is True


def test_is_published_never_raises(tmp_path):
    """An unreadable cache directory must read as "not proven published",
    never as a traceback."""
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(stat.S_IWUSR | stat.S_IXUSR)  # write+exec, no read
    try:
        assert updates.is_published(locked) is False
    finally:
        locked.chmod(stat.S_IRWXU)


# --------------------------------------------------------------------------- #
# throttling
# --------------------------------------------------------------------------- #

def test_due_for_check_allows_the_first_and_blocks_a_same_day_second():
    day = updates.CHECK_INTERVAL_SECONDS
    assert updates.is_due(None, 1_000) is True, "never checked = due"
    assert updates.is_due("garbage", 1_000) is True, "unreadable state = due"
    assert updates.is_due(1_000, 1_000 + day - 1) is False
    assert updates.is_due(1_000, 1_000 + day) is True


def test_a_recent_check_makes_no_network_call_at_all(tmp_path):
    """The throttle must prevent the FETCH, not merely the notice."""
    calls = []
    updates.write_cache({"last_check": 1_000, "latest": "0.1.0"}, tmp_path)
    updates.check_for_update(
        "0.1.0", directory=tmp_path, now=1_000 + 60,
        fetch=lambda: calls.append("hit") or "0.9.9", background=False)
    assert calls == [], "a same-day invocation must not touch the network"


def test_a_failed_fetch_still_records_the_attempt(tmp_path):
    """Otherwise an offline machine re-checks on every single invocation."""
    updates.refresh_cache(now=500.0, directory=tmp_path, fetch=lambda: None)
    assert updates.read_cache(tmp_path)["last_check"] == 500.0
    assert "latest" not in updates.read_cache(tmp_path)


# --------------------------------------------------------------------------- #
# never block, never fail
# --------------------------------------------------------------------------- #

def test_a_network_failure_produces_no_notice_and_no_exception(tmp_path):
    def boom():
        raise OSError("getaddrinfo ENOTFOUND")

    assert updates.check_for_update("0.1.0", directory=tmp_path, now=1_000,
                                    fetch=boom, background=False) is None


def test_fetch_returns_none_on_a_dead_host():
    """The real fetcher, against an address that cannot resolve."""
    got = updates.fetch_latest_version(
        "http://no-such-host.invalid/pypi/json", timeout=1.0)
    assert got is None


def test_fetch_returns_none_when_the_body_is_not_the_expected_shape(monkeypatch):
    """PyPI returning an HTML error page must not be read as a version."""
    import urllib.request

    class _Resp:
        status = 200

        def read(self, *_a):
            return b"<html>502 Bad Gateway</html>"

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _Resp())
    assert updates.fetch_latest_version("http://example.invalid") is None


def test_the_notice_appears_only_after_a_prior_run_cached_a_newer_version(tmp_path):
    """End to end on the REAL (background) path, as an operator experiences it.

    Run 1 has a cold cache, so it must say nothing at all even though a newer
    version exists — the answer arrives on a thread nobody waited for. Run 2
    reads what run 1 left behind.
    """
    import time

    first = updates.check_for_update("0.1.0", directory=tmp_path, now=1_000,
                                     fetch=lambda: "0.2.0", background=True)
    assert first is None, "a cold cache must produce no notice, even so"

    # Wait for the daemon thread's atomic write to land.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if updates.read_cache(tmp_path).get("latest"):
            break
        time.sleep(0.02)
    assert updates.read_cache(tmp_path)["latest"] == "0.2.0", \
        "the background refresh never reached disk"

    # Run 2, a day later. It must report from cache without fetching again.
    calls = []
    second = updates.check_for_update("0.1.0", directory=tmp_path,
                                      now=1_000 + 60,
                                      fetch=lambda: calls.append(1) or "0.2.0",
                                      background=True)
    assert second is not None and "0.2.0" in second
    assert calls == [], "the second run must be served entirely from cache"


def test_the_synchronous_path_uses_the_fresh_result_immediately(tmp_path):
    """background=False is the testable/scriptable path: it must not require a
    second invocation to report what it just fetched."""
    got = updates.check_for_update("0.1.0", directory=tmp_path, now=1_000,
                                   fetch=lambda: "0.2.0", background=False)
    assert got is not None and "0.2.0" in got


def test_background_mode_returns_without_waiting_for_the_fetch(tmp_path):
    """The default path must not block on the network."""
    import threading
    import time

    release = threading.Event()

    def slow():
        release.wait(5)
        return "0.2.0"

    start = time.monotonic()
    result = updates.check_for_update("0.1.0", directory=tmp_path, now=1_000,
                                      fetch=slow, background=True)
    elapsed = time.monotonic() - start
    release.set()
    assert result is None
    assert elapsed < 1.0, f"the check blocked the command for {elapsed:.2f}s"


def test_the_DEFAULT_call_does_not_block_on_the_network(tmp_path):
    """Same guarantee, but with `background` left unspecified.

    The CLI calls check_for_update() without that keyword, so a change to its
    DEFAULT is the mutation that actually reaches an operator — and a test that
    always passes the argument explicitly cannot see it.
    """
    import threading
    import time

    release = threading.Event()

    def slow():
        release.wait(5)
        return "0.2.0"

    start = time.monotonic()
    result = updates.check_for_update("0.1.0", directory=tmp_path, now=1_000,
                                      fetch=slow)          # <- no background kwarg
    elapsed = time.monotonic() - start
    release.set()
    assert elapsed < 1.0, (
        f"the default call blocked the command for {elapsed:.2f}s — "
        "check_for_update must default to the background path")
    assert result is None, "a cold cache must report nothing on the first run"


# --------------------------------------------------------------------------- #
# the disable switch
# --------------------------------------------------------------------------- #

def test_the_env_var_disables_the_check_entirely(tmp_path, monkeypatch):
    calls = []
    updates.write_cache({"latest": "9.9.9", "last_check": 0}, tmp_path)
    monkeypatch.setenv(updates.DISABLE_ENV_VAR, "1")
    result = updates.check_for_update("0.1.0", directory=tmp_path, now=10**9,
                                      fetch=lambda: calls.append(1) or "9.9.9",
                                      background=False)
    assert result is None, "a disabled check must not print even a cached notice"
    assert calls == [], "a disabled check must not touch the network"


def test_the_env_var_set_to_zero_does_not_disable(tmp_path, monkeypatch):
    monkeypatch.setenv(updates.DISABLE_ENV_VAR, "0")
    updates.write_cache({"latest": "9.9.9", "last_check": 10**9}, tmp_path)
    assert updates.check_for_update("0.1.0", directory=tmp_path, now=10**9,
                                    background=False) is not None


def test_config_can_disable_the_check(tmp_path, monkeypatch):
    monkeypatch.delenv(updates.DISABLE_ENV_VAR, raising=False)

    class _Cfg:
        def get(self, key, default=None):
            return {"enabled": False} if key == "updates" else default

    updates.write_cache({"latest": "9.9.9", "last_check": 10**9}, tmp_path)
    assert updates.check_for_update("0.1.0", config=_Cfg(), directory=tmp_path,
                                    now=10**9, background=False) is None


def test_a_bare_updates_key_in_config_does_not_crash(monkeypatch):
    """`updates:` with a commented-out body deep-merges to None, not a dict."""
    monkeypatch.delenv(updates.DISABLE_ENV_VAR, raising=False)

    class _Cfg:
        def get(self, key, default=None):
            return None if key == "updates" else default

    assert updates.is_disabled(_Cfg()) is False


def test_the_shipped_default_config_declares_the_updates_section():
    """The setting must be discoverable in a generated config.yaml."""
    from no_human.config import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["updates"]["enabled"] is True
    assert DEFAULT_CONFIG["updates"]["interval_seconds"] == 86400


def test_the_pypi_url_uses_the_distribution_name_not_the_import_name():
    """`no_human` (underscore) 404s on PyPI forever, which reads as "no update"."""
    assert "/no-human/" in updates.PYPI_JSON_URL
    assert updates.PYPI_JSON_URL.startswith("https://")
