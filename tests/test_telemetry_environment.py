"""Telemetry attribution: `environment` tag + per-env sentinel instance ids.

Companion to `tests/test_telemetry.py`. Pins the acceptance criteria for
"tag every event with an environment (real/bench/test/ci/dev), and fix the
mint-every-run instance_id so real installs are countable":

  - every allowed event kind accepts an `environment` prop;
  - `environment()` classifies pytest/bench/CI/real contexts correctly;
  - `ensure_instance_id` returns a CONSTANT per-env sentinel for
    bench/test/ci (never persisted to config.yaml), and keeps today's
    mint-and-persist-once behaviour for real (unchanged location/mechanism);
  - the Lambda wire path strips `environment`; PostHog keeps it, and
    `distinct_id` still equals the (possibly sentinel) instance_id.
"""
from __future__ import annotations

import contextlib
import json
import uuid
from pathlib import Path

import pytest

from no_human import telemetry

_ENABLED = {
    "enabled": True,
    "endpoint": "https://ingest.invalid/collect",
    "instance_id": "11111111-2222-3333-4444-555555555555",
    "posthog_publishable": "phc_test",
}


@pytest.fixture
def temp_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def no_thread(monkeypatch):
    """Make record() deterministic: no background flush thread in tests."""
    monkeypatch.setattr(telemetry, "_spawn_flush", lambda section: None)


@pytest.fixture
def no_network(monkeypatch):
    calls = []

    def _urlopen(req, timeout=None):
        calls.append((req, timeout))
        return contextlib.nullcontext()

    monkeypatch.setattr("urllib.request.urlopen", _urlopen)
    return calls


def _queue_lines(temp_home) -> list[dict]:
    path = temp_home / ".no_human" / "telemetry-queue.jsonl"
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


# --------------------- every event accepts `environment` ------------------ #

_MIN_PROPS = {
    "app_started": {},
    "task_created": {"source": "cli"},
    "task_completed": {"status": "done", "duration_bucket": "<10m", "attempts": 1},
    "task_failed": {"category": "timeout"},
    "approve_clicked": {},
    "feature_used": {"name": "bench"},
}


def test_every_allowed_event_accepts_environment(temp_home, no_thread, monkeypatch):
    # Defensive: an earlier test's `bench` CliRunner invocation sets NH_ENV
    # directly on the real process env (no monkeypatch involved on its
    # side), which would otherwise leak across tests in the same session.
    monkeypatch.delenv("NH_ENV", raising=False)
    for kind, allowed in telemetry._ALLOWED_EVENTS.items():
        assert "environment" in allowed
        # An explicit caller-passed environment= is itself a valid prop and
        # must not raise for any event kind.
        telemetry.record(kind, config={"telemetry": _ENABLED},
                         environment="test", **_MIN_PROPS[kind])
    lines = _queue_lines(temp_home)
    assert len(lines) == len(telemetry._ALLOWED_EVENTS)
    for line in lines:
        assert "environment" in line["props"]


# ------------------------------ pytest context ----------------------------- #

def test_pytest_context_tags_test_and_uses_constant_id(temp_home, no_thread, monkeypatch):
    # Defensive: see test_every_allowed_event_accepts_environment — an
    # earlier test's `bench` CliRunner invocation can leave NH_ENV=bench on
    # the real process env, which (correctly) outranks PYTEST_CURRENT_TEST.
    monkeypatch.delenv("NH_ENV", raising=False)
    # PYTEST_CURRENT_TEST is set natively by pytest for the duration of this
    # test — no monkeypatch needed to exercise the "test" branch.
    telemetry.record("app_started", config={"telemetry": _ENABLED})
    [line] = _queue_lines(temp_home)
    assert line["props"]["environment"] == "test"

    first = telemetry.ensure_instance_id({})
    second = telemetry.ensure_instance_id({})
    assert first == second == telemetry._ENV_SENTINEL_IDS["test"]
    assert uuid.UUID(first).version == 4


# ------------------------------- bench context ----------------------------- #

def test_bench_context_tags_bench_and_constant_id(temp_home, no_thread, monkeypatch):
    monkeypatch.setenv("NH_ENV", "bench")
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    cfg_path = temp_home / ".no_human" / "config.yaml"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text("telemetry:\n  enabled: true\n")
    from no_human import config as config_mod
    monkeypatch.setattr(config_mod, "CONFIG_PATH", cfg_path)

    telemetry.record("app_started", config={"telemetry": _ENABLED})
    [line] = _queue_lines(temp_home)
    assert line["props"]["environment"] == "bench"

    first = telemetry.ensure_instance_id({})
    second = telemetry.ensure_instance_id({})
    assert first == second == telemetry._ENV_SENTINEL_IDS["bench"]

    import yaml
    on_disk = yaml.safe_load(cfg_path.read_text())["telemetry"]
    assert "instance_id" not in on_disk  # sentinel never persisted


# --------------------------------- CI context ------------------------------ #

@pytest.mark.parametrize("marker", [
    "GITHUB_ACTIONS", "GITLAB_CI", "CIRCLECI", "TRAVIS", "JENKINS_HOME",
])
def test_ci_markers_tag_ci(marker, monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("NH_ENV", raising=False)
    monkeypatch.setenv(marker, "1")
    assert telemetry.environment() == "ci"


# -------------------------------- real context ------------------------------ #

def test_real_context_persists_one_uuid4_and_reuses_it(temp_home, no_thread, monkeypatch):
    monkeypatch.setenv("NH_ENV", "real")

    cfg_path = temp_home / ".no_human" / "config.yaml"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text("telemetry:\n  enabled: true\n")
    from no_human import config as config_mod
    monkeypatch.setattr(config_mod, "CONFIG_PATH", cfg_path)

    section = {"enabled": True, "endpoint": "https://ingest.invalid/collect",
               "instance_id": ""}
    telemetry.record("app_started", config={"telemetry": section})
    telemetry.record("approve_clicked", config={"telemetry": section})

    lines = _queue_lines(temp_home)
    assert len(lines) == 2
    for line in lines:
        assert line["props"]["environment"] == "real"

    shipped_id = telemetry.ensure_instance_id(section)
    assert uuid.UUID(shipped_id).version == 4

    import yaml
    on_disk = yaml.safe_load(cfg_path.read_text())["telemetry"]
    assert on_disk["instance_id"] == shipped_id

    # A second call on a freshly-read section (persisted id present) must
    # reuse it, not re-mint.
    fresh_section = dict(on_disk)
    again = telemetry.ensure_instance_id(fresh_section)
    assert again == shipped_id


# ------------------------------ bench entrypoint ---------------------------- #

def test_bench_group_sets_nh_env(monkeypatch):
    monkeypatch.delenv("NH_ENV", raising=False)
    from click.testing import CliRunner
    from no_human.cli.commands import bench
    runner = CliRunner()
    # A bare `--help` on the group short-circuits before the group callback
    # body runs (click's eager help option), so exercise the callback via a
    # no-op subcommand invocation instead (`report --help`: the group
    # callback runs first, then the subcommand's own --help exits it).
    result = runner.invoke(bench, ["report", "--help"])
    assert result.exit_code == 0
    import os
    assert os.environ.get("NH_ENV") == "bench"


def test_bench_group_does_not_clobber_explicit_nh_env(monkeypatch):
    # setdefault, not assignment: an outer explicit NH_ENV still wins.
    monkeypatch.setenv("NH_ENV", "real")
    from click.testing import CliRunner
    from no_human.cli.commands import bench
    runner = CliRunner()
    result = runner.invoke(bench, ["report", "--help"])
    assert result.exit_code == 0
    import os
    assert os.environ.get("NH_ENV") == "real"


# -------------------------------- wire paths -------------------------------- #

def test_lambda_body_omits_environment_but_posthog_keeps_it(
        temp_home, no_network, no_thread, monkeypatch):
    monkeypatch.setenv("NH_ENV", "real")
    path = temp_home / ".no_human" / "telemetry-queue.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"name": "app_started", "ts": 1786889836,
                    "props": {"environment": "real"}}) + "\n")

    lambda_section = {
        "enabled": True,
        "endpoint": "https://ingest.invalid/collect",
        "instance_id": "11111111-2222-3333-4444-555555555555",
    }
    assert telemetry.flush(lambda_section) == 1
    [(req, _)] = no_network
    [event] = json.loads(req.data.decode())["events"]
    assert "environment" not in event["props"]

    # Re-queue the same event and flush again, this time to PostHog only.
    path.write_text(
        json.dumps({"name": "app_started", "ts": 1786889836,
                    "props": {"environment": "real"}}) + "\n")
    posthog_section = {
        "enabled": True,
        "instance_id": "11111111-2222-3333-4444-555555555555",
        "posthog_publishable": "phc_test",
        "posthog_host": "https://ph.invalid",
    }
    assert telemetry.flush(posthog_section) == 1
    [(req2, _), (req3, _)] = no_network
    body = json.loads(req3.data.decode())
    assert body["batch"][0]["properties"]["environment"] == "real"
    assert body["batch"][0]["distinct_id"] == posthog_section["instance_id"]
