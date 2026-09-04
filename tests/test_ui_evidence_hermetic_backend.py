"""D2-hermetic: a `ui_evidence` walk's dev server used to proxy `/api`
straight at the operator's LIVE `:8420` board (`web/vite.config.js`'s
hardcoded proxy target) — a walk step that clicks Save/Reset-to-defaults
could PUT into the real `~/.no_human/config.yaml`. `hermetic_backend()`
closes that: an isolated, throwaway-HOME `nh start` is armed first, and
`dev_server` only ever hands the booted `start_cmd` THAT api_target, never
the real one.

Seam tests only — no real subprocess, network, or browser. Tests 1-7 mirror
`tests/test_ui_evidence.py` lines 836-1101's `FakeClock`/`FakeProc`/
`_spawn_factory`/`_no_sleep` idiom directly against `hermetic_backend`/
`dev_server`; tests 8-9 exercise `Orchestrator._maybe_capture_ui_evidence`'s
new hermetic branching with a minimal fake repo (no real git needed — that
end-to-end wiring already has its own coverage in
`tests/test_ui_evidence_attempt_hook.py`).
"""
from __future__ import annotations

import contextlib
import gc
import warnings
from pathlib import Path

import pytest
import yaml

from no_human import config as config_mod
from no_human.config import load_config
from no_human.core import orchestrator as orch_mod
from no_human.core.db import Store
from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task
from no_human.notify.slack import SlackNotifier
from no_human.testing import ui_evidence

# `asyncio_mode = "auto"` (pyproject.toml) — every `async def test_*` here
# runs under pytest-asyncio with no marker needed.


# ─────────────────────────── shared fake seams ──────────────────────────── #
#
# Deliberately NOT imported from `tests/test_ui_evidence.py` /
# `tests/test_ui_evidence_attempt_hook.py`: the three suites intentionally
# do not share test fixtures (see the latter's `_StepClock` docstring).


class FakeClock:
    """Deterministic monotonic clock: pops one value per call, repeating the
    last value forever once the scripted list is exhausted."""

    def __init__(self, values):
        self._values = list(values)
        self._last = 0.0

    def __call__(self):
        if self._values:
            self._last = self._values.pop(0)
        return self._last


class FakeProc:
    """Stand-in for a `subprocess.Popen` handle. `poll_sequence` is consumed
    one value per `.poll()` call; the last value repeats once exhausted."""

    def __init__(self, poll_sequence=(None,), pid=4242):
        self.pid = pid
        self._seq = list(poll_sequence)
        self._n = 0

    def poll(self):
        v = self._seq[self._n] if self._n < len(self._seq) else self._seq[-1]
        self._n += 1
        return v

    def wait(self, timeout=None):
        return self.poll()


def _spawn_factory(proc_obj=None, *, raises=None, log_bytes=None):
    """A fake `spawn=` seam recording every call; `proc_obj` is what a
    successful call returns, `raises` (an exception instance) is raised
    instead when set. `log_bytes`, when given, is written to the real log
    file handle `hermetic_backend` opens and passes as `stdout=` — lets a
    test simulate the spawned process having written to its own log before
    `_log_tail` reads it back."""
    calls = []

    def _spawn(argv, **kwargs):
        calls.append((argv, kwargs))
        if raises is not None:
            raise raises
        if log_bytes is not None:
            stdout = kwargs.get("stdout")
            if stdout is not None:
                stdout.write(log_bytes)
                stdout.flush()
        return proc_obj

    _spawn.calls = calls
    return _spawn


async def _no_sleep(_seconds):
    return None


# ───────────────────────────── hermetic_backend ──────────────────────────── #


async def test_hermetic_backend_spawns_the_api_server_with_a_throwaway_home_and_ephemeral_port(
        tmp_path):
    proc_obj = FakeProc(poll_sequence=[None])
    spawn = _spawn_factory(proc_obj)
    async with ui_evidence.hermetic_backend(
        tmp_path, spawn=spawn, pick_port=lambda: 54321,
        reachable=lambda url: True, sleep=_no_sleep, home_root=tmp_path,
    ) as hb:
        assert hb.mode == "armed"
        assert hb.port == 54321
        assert hb.api_target == "http://127.0.0.1:54321"
        # Assert the throwaway HOME while it is still alive — teardown (the
        # `async with` block's exit) removes it, checked separately below.
        assert len(spawn.calls) == 1
        _argv, kwargs = spawn.calls[0]
        env = kwargs["env"]
        home = Path(env["HOME"])
        assert env["USERPROFILE"] == str(home)
        # `NO_HUMAN_HOME` is deliberately NOT set in the child env: isolation
        # comes from HOME/USERPROFILE alone, and `config.NO_HUMAN_HOME` is
        # computed once at import time from the PARENT's own environment —
        # setting it here would name the parent's directory in the child's
        # env and do nothing useful with it (see `hermetic_backend`'s
        # docstring/inline comment in `ui_evidence.py`).
        assert "NO_HUMAN_HOME" not in env
        assert home != Path.home()
        assert home.is_dir()  # still there — the `async with` body is running inside it
        assert (home / ".no_human" / "config.yaml").is_file()

    argv, kwargs = spawn.calls[0]
    assert "--port" in argv and argv[argv.index("--port") + 1] == "54321"


async def test_hermetic_backend_teardown_kills_the_process_and_removes_the_home(tmp_path):
    proc_obj = FakeProc(poll_sequence=[None])  # never exits on its own
    spawn = _spawn_factory(proc_obj)
    kill_calls = []
    home_seen: list[Path] = []

    async with ui_evidence.hermetic_backend(
        tmp_path, spawn=spawn, pick_port=lambda: 54322,
        reachable=lambda url: True, sleep=_no_sleep, home_root=tmp_path,
        kill=lambda p: kill_calls.append(p),
    ) as hb:
        assert hb.mode == "armed"
        home_seen.append(Path(hb.home))
        assert home_seen[0].is_dir()

    assert kill_calls == [proc_obj]  # exactly once
    assert not home_seen[0].exists()  # throwaway HOME removed at teardown


async def test_hermetic_backend_teardown_runs_even_when_the_body_raises(tmp_path):
    proc_obj = FakeProc(poll_sequence=[None])  # still running when the body raises
    spawn = _spawn_factory(proc_obj)
    kill_calls = []
    home_seen: list[Path] = []

    with pytest.raises(RuntimeError, match="boom"):
        async with ui_evidence.hermetic_backend(
            tmp_path, spawn=spawn, pick_port=lambda: 54323,
            reachable=lambda url: True, sleep=_no_sleep, home_root=tmp_path,
            kill=lambda p: kill_calls.append(p),
        ) as hb:
            assert hb.mode == "armed"
            home_seen.append(Path(hb.home))
            raise RuntimeError("boom")

    assert kill_calls == [proc_obj]  # exactly once, even after the raise
    assert not home_seen[0].exists()  # HOME still removed


async def test_hermetic_backend_spawn_oserror_is_failed_with_cause_spawn_failed(tmp_path):
    spawn = _spawn_factory(raises=OSError("no such file"))
    home_seen: list[Path] = []
    async with ui_evidence.hermetic_backend(
        tmp_path, spawn=spawn, pick_port=lambda: 54324,
        reachable=lambda url: True, sleep=_no_sleep, home_root=tmp_path,
    ) as hb:
        assert hb.mode == "failed"
        assert hb.cause == "spawn-failed"
        assert "OSError" in hb.detail
        home_seen.append(Path(hb.home))
    assert len(spawn.calls) == 1
    assert not home_seen[0].exists()  # HOME removed even on this early a failure


async def test_hermetic_backend_never_ready_is_failed_with_cause_timeout(tmp_path):
    proc_obj = FakeProc(poll_sequence=[None])  # never exits, never answers
    spawn = _spawn_factory(proc_obj)
    kill_calls = []
    reachable_calls = []
    clock = FakeClock([0.0, 61.0, 61.0])  # started=0.0, first check already >= 60s timeout

    async with ui_evidence.hermetic_backend(
        tmp_path, spawn=spawn, pick_port=lambda: 54325,
        reachable=lambda url: (reachable_calls.append(url), False)[1],
        clock=clock, sleep=_no_sleep, home_root=tmp_path,
        kill=lambda p: kill_calls.append(p),
    ) as hb:
        assert hb.mode == "failed"
        assert hb.cause == "timeout"
        # AC2 (D2-hermetic bugfix): `detail` used to be stamped in the
        # post-yield `finally` block, AFTER the orchestrator already read
        # `hb.detail` during this `yield` — so the "timeout" cause silently
        # carried an empty detail. It must be set BEFORE the yield.
        assert hb.detail, "detail must be non-empty for the timeout cause"
        assert "no response at" in hb.detail
        assert "60s" in hb.detail
    assert kill_calls == [proc_obj]  # still running at teardown -> killed


async def test_hermetic_backend_exited_early_detail_carries_exit_code_and_log_tail(
        tmp_path):
    proc_obj = FakeProc(poll_sequence=[7])  # exits immediately with code 7
    spawn = _spawn_factory(proc_obj, log_bytes=b"boom: address already in use\n")

    async with ui_evidence.hermetic_backend(
        tmp_path, spawn=spawn, pick_port=lambda: 54326,
        reachable=lambda url: False, sleep=_no_sleep, home_root=tmp_path,
    ) as hb:
        assert hb.mode == "failed"
        assert hb.cause == "exited-early"
        # AC2 (D2-hermetic bugfix): same as the timeout cause above, this
        # used to be stamped post-yield and so read empty.
        assert hb.detail, "detail must be non-empty for the exited-early cause"
        assert "exit code 7" in hb.detail
        assert "boom: address already in use" in hb.detail


async def test_hermetic_backend_port_unavailable_detail_is_non_empty(tmp_path):
    def _pick_port():
        raise OSError("no free ports")

    async with ui_evidence.hermetic_backend(
        tmp_path, spawn=_spawn_factory(FakeProc()), pick_port=_pick_port,
        reachable=lambda url: True, sleep=_no_sleep, home_root=tmp_path,
    ) as hb:
        assert hb.mode == "failed"
        assert hb.cause == "port-unavailable"
        assert hb.detail, "detail must be non-empty for the port-unavailable cause"
        assert "no free ports" in hb.detail


async def test_hermetic_backend_home_seed_failure_detail_is_non_empty(tmp_path):
    bad_home_root = tmp_path / "not-a-directory"
    bad_home_root.write_text("x")  # a FILE, so mkdtemp(dir=...) must raise

    async with ui_evidence.hermetic_backend(
        tmp_path, spawn=_spawn_factory(FakeProc()), pick_port=lambda: 54327,
        reachable=lambda url: True, sleep=_no_sleep, home_root=bad_home_root,
    ) as hb:
        assert hb.mode == "failed"
        assert hb.cause == "home-seed-failed"
        assert hb.detail, "detail must be non-empty for the home-seed-failed cause"


async def test_hermetic_backend_closes_the_log_file_on_spawn_failure(tmp_path):
    """AC4: `fh` (the hermetic API log file handle) must be closed on the
    spawn-failed path. Asserted directly on the handle's own `closed` state:
    a ResourceWarning/gc probe is not reliable here — the async-generator
    frame that `async with` drives keeps `fh` referenced through the yield,
    so `gc.collect()` never frees it and no ResourceWarning fires either
    way, closed or not. `_spawn_factory` records the exact kwargs `spawn`
    was called with, so the real `stdout=fh` object `hermetic_backend`
    opened and passed in is recovered from that recorded call."""
    spawn = _spawn_factory(raises=OSError("no such file"))

    async with ui_evidence.hermetic_backend(
        tmp_path, spawn=spawn, pick_port=lambda: 54328,
        reachable=lambda url: True, sleep=_no_sleep, home_root=tmp_path,
    ) as hb:
        assert hb.mode == "failed"
        assert hb.cause == "spawn-failed"

    assert spawn.calls, "spawn was never called"
    captured_stdout = spawn.calls[-1][1]["stdout"]
    assert captured_stdout.closed is True, (
        "the hermetic API log file handle leaked (not closed) on the "
        "spawn-failed path")


# ───────────────────────── AC3: seeded llm.auth_mode ─────────────────────── #


def _seeded_llm_auth_mode(home: str) -> dict:
    doc = yaml.safe_load(
        (Path(home) / ".no_human" / "config.yaml").read_text())
    return doc.get("llm") or {}


async def test_hermetic_backend_seeds_llm_auth_mode_api_key_into_the_throwaway_config(
        tmp_path):
    proc_obj = FakeProc(poll_sequence=[None])
    spawn = _spawn_factory(proc_obj)
    async with ui_evidence.hermetic_backend(
        tmp_path, spawn=spawn, pick_port=lambda: 54329,
        reachable=lambda url: True, sleep=_no_sleep, home_root=tmp_path,
        auth_mode="api_key",
    ) as hb:
        assert _seeded_llm_auth_mode(hb.home)["auth_mode"] == "api_key"


async def test_hermetic_backend_defaults_to_subscription_auth_mode_in_the_throwaway_config(
        tmp_path):
    proc_obj = FakeProc(poll_sequence=[None])
    spawn = _spawn_factory(proc_obj)
    async with ui_evidence.hermetic_backend(
        tmp_path, spawn=spawn, pick_port=lambda: 54330,
        reachable=lambda url: True, sleep=_no_sleep, home_root=tmp_path,
    ) as hb:
        assert _seeded_llm_auth_mode(hb.home)["auth_mode"] == "subscription"


async def test_hermetic_backend_normalizes_an_unknown_auth_mode_to_subscription(
        tmp_path):
    proc_obj = FakeProc(poll_sequence=[None])
    spawn = _spawn_factory(proc_obj)
    async with ui_evidence.hermetic_backend(
        tmp_path, spawn=spawn, pick_port=lambda: 54331,
        reachable=lambda url: True, sleep=_no_sleep, home_root=tmp_path,
        auth_mode="bogus-mode",
    ) as hb:
        assert _seeded_llm_auth_mode(hb.home)["auth_mode"] == "subscription"


async def test_hermetic_backend_never_seeds_an_auth_profile(tmp_path):
    proc_obj = FakeProc(poll_sequence=[None])
    spawn = _spawn_factory(proc_obj)
    async with ui_evidence.hermetic_backend(
        tmp_path, spawn=spawn, pick_port=lambda: 54332,
        reachable=lambda url: True, sleep=_no_sleep, home_root=tmp_path,
        auth_mode="api_key",
    ) as hb:
        assert "auth_profile" not in _seeded_llm_auth_mode(hb.home)


async def test_the_seeded_config_lets_an_api_key_install_boot_instead_of_exiting_2(
        tmp_path, monkeypatch):
    """AC3's real claim, proven against `config.py`'s own (unedited) auth
    gate — the exact function the hermetic child's `nh start` runs through
    at boot (`cli/commands.py::_bootstrap`): a hermetic child under a
    sanctioned `api_key` install must resolve `auth_mode="api_key"` from the
    seeded config and pass `_assert_api_key_mode` (no exit 2) — never fall
    back to the `subscription` default and hit `assert_subscription_mode`'s
    strict refusal of an inherited `ANTHROPIC_API_KEY` (fa053f7da's bug: the
    seeded config only ever wrote a `server:` block, never `llm:`)."""
    proc_obj = FakeProc(poll_sequence=[None])
    spawn = _spawn_factory(proc_obj)
    async with ui_evidence.hermetic_backend(
        tmp_path, spawn=spawn, pick_port=lambda: 54333,
        reachable=lambda url: True, sleep=_no_sleep, home_root=tmp_path,
        auth_mode="api_key",
    ) as hb:
        home = Path(hb.home)
        resolved_auth_mode = _seeded_llm_auth_mode(str(home))["auth_mode"]

    assert resolved_auth_mode == "api_key"

    env_path = home / ".no_human" / ".env"  # never created — no credential on disk
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key-not-real")

    # The fix: resolving "api_key" from the seeded config boots clean.
    report = config_mod.assert_subscription_mode(
        env_path=env_path, auth_mode=resolved_auth_mode)
    assert report is not None

    # Contrast with the exact bug fa053f7da shipped: an unseeded (or
    # unfixed) child that defaulted to "subscription" hits the strict
    # refusal for this same environment and `sys.exit(2)`s instead of
    # booting — silently skipping every UI-evidence walk forever.
    with pytest.raises(config_mod.AuthError):
        config_mod.assert_subscription_mode(
            env_path=env_path, auth_mode="subscription")


async def test_maybe_capture_ui_evidence_passes_the_configured_auth_mode_to_the_hermetic_backend(
        tmp_path, store, monkeypatch):
    monkeypatch.setattr(orch_mod.ui_evidence, "playwright_available", lambda: True)

    captured_kwargs = {}

    @contextlib.asynccontextmanager
    async def _fake_hermetic_backend(out_dir, **kwargs):
        captured_kwargs.update(kwargs)
        yield ui_evidence.HermeticBackend(
            mode="failed", cause="spawn-failed", detail="x")

    monkeypatch.setattr(orch_mod.ui_evidence, "hermetic_backend", _fake_hermetic_backend)

    events = []
    cfg = _config(tmp_path)
    cfg.data.setdefault("llm", {})["auth_mode"] = "api_key"
    orch = Orchestrator(store, cfg.data, object(), SlackNotifier(None), event_sink=events.append)
    repo = _make_repo_with_manifest(tmp_path)
    task = Task.new("touch the UI", repo_path=repo.path)

    await orch._maybe_capture_ui_evidence(task, repo, "task-branch", "HEAD~1")

    assert captured_kwargs.get("auth_mode") == "api_key"


async def test_orchestrator_hands_the_dev_server_the_hermetic_vite_api_target(
        tmp_path, store, monkeypatch):
    """AC1 (D2-hermetic review follow-up): `test_dev_server_passes_vite_api_target_in_the_child_env`
    below only proves `dev_server` forwards a given `extra_env` into the
    child process — it calls `dev_server` directly, never through
    `Orchestrator._maybe_capture_ui_evidence`, so it cannot catch a
    regression at `orchestrator.py`'s own call site (the `extra_env={...} if
    hb else None` wiring that hands `dev_server` the *hermetic* backend's
    `api_target`). This test closes that gap end to end: an armed hermetic
    backend must result in `dev_server` being called with
    `extra_env == {"VITE_API_TARGET": hb.api_target}`."""
    monkeypatch.setattr(orch_mod.ui_evidence, "playwright_available", lambda: True)

    @contextlib.asynccontextmanager
    async def _fake_hermetic_backend_armed(out_dir, **kwargs):
        yield ui_evidence.HermeticBackend(
            mode="armed", home=str(tmp_path / "hb-home"), port=54321,
            api_target="http://127.0.0.1:54321")

    monkeypatch.setattr(orch_mod.ui_evidence, "hermetic_backend", _fake_hermetic_backend_armed)

    captured_kwargs = {}

    @contextlib.asynccontextmanager
    async def _fake_dev_server(repo_path, ui_conf, base_url, out_dir, **kwargs):
        captured_kwargs.update(kwargs)
        yield ui_evidence.DevServerOutcome(mode="booted", base_url=base_url)

    monkeypatch.setattr(orch_mod.ui_evidence, "dev_server", _fake_dev_server)

    async def _fake_run(repo_path, out_dir, **kwargs):
        return ui_evidence.UiEvidenceResult(verdict="ran", shots=["loaded.png"])

    monkeypatch.setattr(orch_mod.ui_evidence, "run", _fake_run)

    events = []
    cfg = _config(tmp_path)
    orch = Orchestrator(store, cfg.data, object(), SlackNotifier(None), event_sink=events.append)
    monkeypatch.setattr(orch, "_deliver_ui_evidence", lambda *a, **kw: "<delivered-marker>")
    repo = _make_repo_with_manifest(tmp_path)
    task = Task.new("touch the UI", repo_path=repo.path)

    section = await orch._maybe_capture_ui_evidence(task, repo, "task-branch", "HEAD~1")

    assert section == "<delivered-marker>"
    assert captured_kwargs.get("extra_env") == {"VITE_API_TARGET": "http://127.0.0.1:54321"}


# ────────────────────────── dev_server + extra_env ───────────────────────── #


async def test_dev_server_passes_vite_api_target_in_the_child_env(tmp_path):
    proc_obj = FakeProc(poll_sequence=[None])
    spawn = _spawn_factory(proc_obj)
    reach_calls = {"n": 0}

    def reachable(url):
        reach_calls["n"] += 1
        return reach_calls["n"] > 1  # False for the pre-existing check, True in the loop

    clock = FakeClock([0.0, 0.1, 0.2])
    async with ui_evidence.dev_server(
        tmp_path, {"start_cmd": "npm run dev", "ready_timeout_s": 60},
        "http://127.0.0.1:5173", tmp_path,
        spawn=spawn, reachable=reachable, clock=clock, sleep=_no_sleep,
        kill=lambda p: None,
        extra_env={"VITE_API_TARGET": "http://127.0.0.1:54321"},
    ) as srv:
        assert srv.mode == "booted"
    assert len(spawn.calls) == 1
    _argv, kwargs = spawn.calls[0]
    assert kwargs["env"]["VITE_API_TARGET"] == "http://127.0.0.1:54321"
    # `{**os.environ, **extra_env}` — the child still inherits the parent's
    # environment, this is additive, not a replacement.
    import os
    assert kwargs["env"].get("PATH") == os.environ.get("PATH")


async def test_dev_server_without_extra_env_passes_no_env_kwarg(tmp_path):
    """The customer path (no hermetic backend involved, `extra_env=None`,
    `dev_server`'s existing default) must stay byte-identical: no `env=`
    kwarg at all, so the child spawns with a plain inherited environment
    exactly as it did before this feature existed."""
    proc_obj = FakeProc(poll_sequence=[None])
    spawn = _spawn_factory(proc_obj)
    reach_calls = {"n": 0}

    def reachable(url):
        reach_calls["n"] += 1
        return reach_calls["n"] > 1

    clock = FakeClock([0.0, 0.1, 0.2])
    async with ui_evidence.dev_server(
        tmp_path, {"start_cmd": "npm run dev", "ready_timeout_s": 60},
        "http://127.0.0.1:5173", tmp_path,
        spawn=spawn, reachable=reachable, clock=clock, sleep=_no_sleep,
        kill=lambda p: None,
    ) as srv:
        assert srv.mode == "booted"
    assert len(spawn.calls) == 1
    _argv, kwargs = spawn.calls[0]
    assert "env" not in kwargs


# ───────────────────── _maybe_capture_ui_evidence wiring ─────────────────── #


def _config(tmp_path):
    cfg = load_config(tmp_path / "config.yaml")
    cfg.data.setdefault("planning", {})["enabled"] = False
    cfg.data.setdefault("reviewer", {})["allow_advisory"] = True
    cfg.data.setdefault("blockers", {})["challenge"] = False
    cfg.data["isolation"]["enabled"] = False
    return cfg


class _FakeRepo:
    """The minimal duck-typed surface `_maybe_capture_ui_evidence` reads off
    `repo`: `.path` and `.changed_files(base)`. No real git needed — the
    end-to-end wiring (real git repo, real branch push) is already covered
    by `tests/test_ui_evidence_attempt_hook.py`; these two tests are only
    about the hermetic-backend branching added to this method."""

    def __init__(self, path):
        self.path = str(path)

    def changed_files(self, ref="HEAD~1"):
        return ["web/App.jsx"]  # matches UI_EVIDENCE_DEFAULT_GLOBS


@pytest.fixture
async def store(tmp_path):
    s = await Store(tmp_path / "nh.db").connect()
    yield s
    await s.close()


def _make_repo_with_manifest(tmp_path, base_url="http://127.0.0.1:5173"):
    repo_dir = tmp_path / "repo"
    manifest_dir = repo_dir / ".no_human"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "ui_evidence.json").write_text(
        f'{{"base_url": "{base_url}", "steps": [{{"goto": "/"}}, {{"shot": "loaded"}}]}}'
    )
    return _FakeRepo(repo_dir)


async def test_maybe_capture_ui_evidence_skips_and_discloses_when_hermetic_init_fails(
        tmp_path, store, monkeypatch):
    monkeypatch.setattr(orch_mod.ui_evidence, "playwright_available", lambda: True)

    @contextlib.asynccontextmanager
    async def _fake_hermetic_backend_failed(out_dir, **kwargs):
        yield ui_evidence.HermeticBackend(
            mode="failed", cause="spawn-failed", detail="OSError: no such file")

    monkeypatch.setattr(orch_mod.ui_evidence, "hermetic_backend", _fake_hermetic_backend_failed)

    run_calls = []

    async def _spy_run(repo_path, out_dir, **kwargs):
        run_calls.append((repo_path, out_dir))
        raise AssertionError("ui_evidence.run must not be called on hermetic init failure")

    monkeypatch.setattr(orch_mod.ui_evidence, "run", _spy_run)

    events = []
    cfg = _config(tmp_path)
    orch = Orchestrator(store, cfg.data, object(), SlackNotifier(None), event_sink=events.append)
    repo = _make_repo_with_manifest(tmp_path)
    task = Task.new("touch the UI", repo_path=repo.path)

    section = await orch._maybe_capture_ui_evidence(task, repo, "task-branch", "HEAD~1")

    assert run_calls == []
    assert "Visual proof skipped:" in section
    assert "hermetic walk backend failed to start (spawn-failed)" in section
    advisories = [e["text"] for e in events if e.get("kind") == "advisory"]
    # AC2 (D2-hermetic bugfix): the advisory must carry the FULL, non-empty
    # `detail` text verbatim — exact match, not just a non-empty prefix —
    # because `detail` used to be stamped post-yield (AFTER the orchestrator
    # already read it here), so `exited-early`/`timeout` causes silently
    # carried an empty detail while this advisory's prefix still matched.
    assert (
        "walk_skip::hermetic_backend_init_failed: OSError: no such file" in advisories
    ), advisories


async def test_maybe_capture_ui_evidence_walks_a_pre_existing_dev_server_and_discloses(
        tmp_path, store, monkeypatch):
    """D2-hermetic bugfix (2026-09-03): fa053f7da made a pre-existing dev
    server SKIP the walk under hermetic mode (its proxy target is
    unknowable, so it can't be proven hermetic) — but that lost real, if
    not-provably-hermetic, evidence a task used to ship. The INTAKE
    resolution restores the pre-fa053f7da behavior: the walk still runs
    against whatever is already listening; only an advisory (and, in the
    delivered PR body, a disclosure sentence — covered separately by
    `tests/test_ui_evidence_attempt_hook.py::
    test_pre_existing_dev_server_is_disclosed`) says it was not hermetic.
    `run` here returns zero shots so this test only has to prove the walk
    RAN and the right advisory fired — delivery (`_deliver_ui_evidence`,
    which needs a real git repo) is exercised by that sibling test."""
    monkeypatch.setattr(orch_mod.ui_evidence, "playwright_available", lambda: True)

    @contextlib.asynccontextmanager
    async def _fake_hermetic_backend_armed(out_dir, **kwargs):
        yield ui_evidence.HermeticBackend(
            mode="armed", home=str(tmp_path / "hb-home"), port=54321,
            api_target="http://127.0.0.1:54321")

    monkeypatch.setattr(orch_mod.ui_evidence, "hermetic_backend", _fake_hermetic_backend_armed)

    @contextlib.asynccontextmanager
    async def _fake_dev_server_pre_existing(repo_path, ui_conf, base_url, out_dir, **kwargs):
        yield ui_evidence.DevServerOutcome(mode="pre-existing", base_url=base_url)

    monkeypatch.setattr(orch_mod.ui_evidence, "dev_server", _fake_dev_server_pre_existing)

    run_calls = []

    async def _spy_run(repo_path, out_dir, **kwargs):
        run_calls.append((repo_path, out_dir))
        return ui_evidence.UiEvidenceResult(verdict="ran", reason="no shots for this test")

    monkeypatch.setattr(orch_mod.ui_evidence, "run", _spy_run)

    events = []
    cfg = _config(tmp_path)
    orch = Orchestrator(store, cfg.data, object(), SlackNotifier(None), event_sink=events.append)
    repo = _make_repo_with_manifest(tmp_path, base_url="http://127.0.0.1:5173")
    task = Task.new("touch the UI", repo_path=repo.path)

    section = await orch._maybe_capture_ui_evidence(task, repo, "task-branch", "HEAD~1")

    assert len(run_calls) == 1, (
        f"a pre-existing dev server must still be walked (disclosed, not "
        f"skipped): {run_calls}")
    assert "Visual proof skipped:" in section
    assert "the walk captured no shots" in section
    advisories = [e["text"] for e in events if e.get("kind") == "advisory"]
    assert (
        "walk_nonhermetic::pre_existing_dev_server: http://127.0.0.1:5173"
        in advisories
    ), advisories
