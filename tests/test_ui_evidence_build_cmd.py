"""UI-evidence walk: build the web UI from the WORKTREE before a dist-serving
walk (feature: `ui_evidence.build_cmd`).

A FRESH task worktree has no `node_modules`/`web/dist` — a `start_cmd` like
`npm run preview` (serving a pre-built `dist/`) would 404 forever without a
build step first. `ui_evidence.build_cmd` (optional) runs in the worktree,
`shell=False`, BEFORE `start_cmd` — but only when `dev_server` has already
decided a server is genuinely about to be booted (nothing pre-existing at the
manifest `base_url`). A build failure/timeout is a DISCLOSED walk skip
through the same honesty-floor path every other empty-outcome case already
uses — never an exception, never a silently empty `## UI evidence` section.

Seam tests only — no real subprocess, network, or browser. Tests 1-9 mirror
`tests/test_ui_evidence.py`'s `FakeClock`/`FakeProc`/`_spawn_factory`/
`_no_sleep` idiom directly against `dev_server`, adding a `build_run=` fake
for the new build seam; tests 10-12 exercise `nh onboard`'s derivation
(`detect_dev_server`/`ui_evidence_suggestion`/`apply_ui_evidence_suggestion`);
test 13 exercises `Orchestrator._maybe_capture_ui_evidence`'s new reason
branches the same way `tests/test_ui_evidence_hermetic_backend.py`'s
orchestrator-level tests do (a faked `dev_server` yielding the outcome
directly — no real hermetic backend, no real git).
"""
from __future__ import annotations

import json
import subprocess


from no_human.core import orchestrator as orch_mod
from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task
from no_human.notify.slack import SlackNotifier
from no_human.config import load_config
from no_human.onboard import (
    DeclarationDeriver,
    apply_ui_evidence_suggestion,
    detect_dev_server,
    ui_evidence_suggestion,
)
from no_human.profile import ProjectProfile
from no_human.testing import ui_evidence

# `asyncio_mode = "auto"` (pyproject.toml) — every `async def test_*` here
# runs under pytest-asyncio with no marker needed.


# ─────────────────────────── shared fake seams ──────────────────────────── #
# (independent copies of tests/test_ui_evidence.py's idiom — deliberately not
# shared across test files, per that file's own convention.)


class FakeClock:
    def __init__(self, values):
        self._values = list(values)
        self._last = 0.0

    def __call__(self):
        if self._values:
            self._last = self._values.pop(0)
        return self._last


class FakeProc:
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


def _spawn_factory(proc_obj=None, *, raises=None, order=None):
    calls = []

    def _spawn(argv, **kwargs):
        calls.append((argv, kwargs))
        if order is not None:
            order.append("spawn")
        if raises is not None:
            raise raises
        return proc_obj

    _spawn.calls = calls
    return _spawn


async def _no_sleep(_seconds):
    return None


class FakeCompletedProcess:
    """Stand-in for `subprocess.CompletedProcess` — the only attributes
    `_run_build` reads are `.returncode` and `.stdout`."""

    def __init__(self, returncode=0, stdout=b""):
        self.returncode = returncode
        self.stdout = stdout


def _build_run_factory(results=None, *, order=None):
    """Fake `build_run=` seam mirroring `subprocess.run`'s call shape.
    `results` is a list of `FakeCompletedProcess`/exception INSTANCES,
    consumed one per call in order; once exhausted, a zero-exit
    `FakeCompletedProcess` is returned (a benign default)."""
    calls = []
    queue = list(results or [])

    def _run(argv, **kwargs):
        calls.append((argv, kwargs))
        if order is not None:
            order.append("build")
        result = queue.pop(0) if queue else FakeCompletedProcess()
        if isinstance(result, Exception):
            raise result
        return result

    _run.calls = calls
    return _run


_DEFAULT_UI_CONF = {"start_cmd": "npm run dev", "ready_timeout_s": 60}


# ───────────────────────── dev_server: build_cmd ─────────────────────────── #


async def test_build_cmd_runs_in_the_worktree_before_start_cmd(tmp_path):
    """AC1: `build_cmd` set + a server about to boot -> the build runs in
    `repo_path`, `shell=False`, strictly before `start_cmd` is spawned."""
    order = []
    build_run = _build_run_factory(order=order)
    proc_obj = FakeProc(poll_sequence=[None])
    spawn = _spawn_factory(proc_obj, order=order)
    reach_calls = {"n": 0}

    def reachable(url):
        reach_calls["n"] += 1
        return reach_calls["n"] > 1  # False for pre-existing check, True in the loop

    clock = FakeClock([0.0, 0.1, 0.1])
    async with ui_evidence.dev_server(
        tmp_path,
        {**_DEFAULT_UI_CONF, "build_cmd": "npm run build"},
        "http://127.0.0.1:5173", tmp_path,
        spawn=spawn, reachable=reachable, clock=clock, sleep=_no_sleep,
        kill=lambda p: None, build_run=build_run,
    ) as srv:
        assert srv.mode == "booted"

    assert order == ["build", "spawn"]
    assert len(build_run.calls) == 1
    argv, kwargs = build_run.calls[0]
    assert argv == ["npm", "run", "build"]
    assert kwargs["cwd"] == str(tmp_path)
    assert kwargs.get("shell") is not True  # never shell=True — argv-split


async def test_build_cmd_runs_each_ampersand_segment_in_order(tmp_path):
    """AC1: `a && b` -> two sequential, individually-argv-split spawns."""
    build_run = _build_run_factory(results=[
        FakeCompletedProcess(0, b"installed\n"),
        FakeCompletedProcess(0, b"built\n"),
    ])
    proc_obj = FakeProc(poll_sequence=[None])
    spawn = _spawn_factory(proc_obj)

    clock = FakeClock([0.0, 100.0, 100.0])
    async with ui_evidence.dev_server(
        tmp_path,
        {**_DEFAULT_UI_CONF, "build_cmd": "npm ci && npm run build"},
        "http://127.0.0.1:5173", tmp_path,
        spawn=spawn, reachable=lambda url: False, clock=clock, sleep=_no_sleep,
        kill=lambda p: None, build_run=build_run,
    ) as srv:
        assert srv.mode == "boot-failed"  # timed out — irrelevant to this test

    assert [argv for argv, _kw in build_run.calls] == [
        ["npm", "ci"], ["npm", "run", "build"],
    ]


async def test_build_nonzero_exit_yields_disclosed_skip_and_never_spawns(tmp_path):
    """AC2/AC3: a failing build -> `boot-failed`/`build-failed`, `start_cmd`
    never spawns, `exit_code` is the BUILD's own exit code."""
    build_run = _build_run_factory(results=[
        FakeCompletedProcess(returncode=1, stdout=b"ERROR: build broke\n"),
    ])
    spawn = _spawn_factory()

    async with ui_evidence.dev_server(
        tmp_path,
        {**_DEFAULT_UI_CONF, "build_cmd": "npm run build"},
        "http://127.0.0.1:5173", tmp_path,
        spawn=spawn, reachable=lambda url: False, build_run=build_run,
    ) as srv:
        assert srv.mode == "boot-failed"
        assert srv.cause == "build-failed"
        assert srv.exit_code == 1

    assert spawn.calls == []


async def test_build_timeout_yields_disclosed_skip_and_never_spawns(tmp_path):
    """AC2/AC3: a build that blows its budget -> `boot-failed`/`build-timeout`,
    `start_cmd` never spawns, `exit_code` is `None` (no build exit to report)."""
    timeout_exc = subprocess.TimeoutExpired(
        cmd=["npm", "run", "build"], timeout=1, output=b"still compiling\n")
    build_run = _build_run_factory(results=[timeout_exc])
    spawn = _spawn_factory()

    async with ui_evidence.dev_server(
        tmp_path,
        {**_DEFAULT_UI_CONF, "build_cmd": "npm run build", "build_timeout_s": 1},
        "http://127.0.0.1:5173", tmp_path,
        spawn=spawn, reachable=lambda url: False, build_run=build_run,
    ) as srv:
        assert srv.mode == "boot-failed"
        assert srv.cause == "build-timeout"
        assert srv.exit_code is None

    assert spawn.calls == []


async def test_build_detail_carries_exit_code_and_last_lines(tmp_path):
    """AC4 (dev_server layer): `detail` is LOG-ONLY, but it must actually
    carry the exit code and the tail of the combined output — the raw
    material the orchestrator's advisory (not the PR body) renders."""
    build_run = _build_run_factory(results=[
        FakeCompletedProcess(returncode=2, stdout=b"line1\nline2\nUNIQUE_MARKER_42\n"),
    ])
    spawn = _spawn_factory()

    async with ui_evidence.dev_server(
        tmp_path,
        {**_DEFAULT_UI_CONF, "build_cmd": "npm run build"},
        "http://127.0.0.1:5173", tmp_path,
        spawn=spawn, reachable=lambda url: False, build_run=build_run,
    ) as srv:
        assert "2" in srv.detail
        assert "UNIQUE_MARKER_42" in srv.detail

    # Real file I/O: the combined build output actually landed on disk too.
    log_path = tmp_path / "ui-build.log"
    assert log_path.exists()
    assert b"UNIQUE_MARKER_42" in log_path.read_bytes()


async def test_no_build_cmd_spawns_immediately_and_writes_no_build_log(tmp_path):
    """No `build_cmd` configured -> byte-identical to today: not one new
    statement runs, and no `ui-build.log` is ever created."""
    order = []
    build_run = _build_run_factory(order=order)
    proc_obj = FakeProc(poll_sequence=[None])
    spawn = _spawn_factory(proc_obj, order=order)
    reach_calls = {"n": 0}

    def reachable(url):
        reach_calls["n"] += 1
        return reach_calls["n"] > 1

    clock = FakeClock([0.0, 0.1, 0.1])
    async with ui_evidence.dev_server(
        tmp_path, dict(_DEFAULT_UI_CONF),  # no "build_cmd" key at all
        "http://127.0.0.1:5173", tmp_path,
        spawn=spawn, reachable=reachable, clock=clock, sleep=_no_sleep,
        kill=lambda p: None, build_run=build_run,
    ) as srv:
        assert srv.mode == "booted"

    assert build_run.calls == []
    assert order == ["spawn"]
    assert not (tmp_path / "ui-build.log").exists()


async def test_pre_existing_server_never_runs_the_build(tmp_path):
    """Something already answers at `base_url` -> `pre-existing`, and the
    build never runs (mirrors: never spawned/killed either)."""
    build_run = _build_run_factory()
    spawn = _spawn_factory()

    async with ui_evidence.dev_server(
        tmp_path,
        {**_DEFAULT_UI_CONF, "build_cmd": "npm run build"},
        "http://127.0.0.1:5173", tmp_path,
        spawn=spawn, reachable=lambda url: True, build_run=build_run,
    ) as srv:
        assert srv.mode == "pre-existing"

    assert build_run.calls == []
    assert spawn.calls == []


async def test_unparsable_build_cmd_is_disclosed_and_never_spawns(tmp_path):
    """An empty `&&` segment (or otherwise unparsable chain) is treated the
    same as a build failure — disclosed, not an exception, no spawn."""
    spawn = _spawn_factory()
    build_run = _build_run_factory()

    async with ui_evidence.dev_server(
        tmp_path,
        {**_DEFAULT_UI_CONF, "build_cmd": "npm run build && "},
        "http://127.0.0.1:5173", tmp_path,
        spawn=spawn, reachable=lambda url: False, build_run=build_run,
    ) as srv:
        assert srv.mode == "boot-failed"
        assert srv.cause == "build-failed"

    assert build_run.calls == []
    assert spawn.calls == []


# ───────────────────────── onboard derivation ────────────────────────────── #


def _vite_repo_with_web_build(root):
    """A root-level `npm run dev` (vite) convention PLUS a nested
    `web/package.json` declaring a `build` script — the exact shape a repo
    with a separately-built frontend subdirectory has."""
    (root / ".git").mkdir(parents=True, exist_ok=True)
    (root / "package.json").write_text(json.dumps({
        "name": "app", "version": "0.0.0",
        "scripts": {"dev": "vite"},
        "devDependencies": {"vite": "^5.0.0"},
    }))
    web = root / "web"
    web.mkdir()
    (web / "package.json").write_text(json.dumps({
        "name": "web", "version": "0.0.0",
        "scripts": {"build": "vite build", "dev": "vite"},
    }))
    return root


def _vite_repo_without_web_build(root):
    (root / ".git").mkdir(parents=True, exist_ok=True)
    (root / "package.json").write_text(json.dumps({
        "name": "app", "version": "0.0.0",
        "scripts": {"dev": "vite"},
        "devDependencies": {"vite": "^5.0.0"},
    }))
    return root


def test_onboard_derives_the_ci_plus_build_chain_from_web_package_json(tmp_path):
    """`web/package.json` declaring a `build` script -> the derived
    `ui_evidence` block gains `build_cmd`; the existing keys are untouched."""
    repo = _vite_repo_with_web_build(tmp_path / "repo")
    detected = detect_dev_server(repo)
    assert detected is not None
    assert detected["build_cmd"] == "npm --prefix web ci && npm --prefix web run build"
    assert detected["start_cmd"] == "npm run dev"
    assert detected["base_url"] == "http://localhost:5173"

    # `DeclarationDeriver.derive` must carry the SAME fact — not a second
    # detector living only in this test (mirrors
    # tests/test_ui_evidence_provisioning.py's equality assertion).
    derived = DeclarationDeriver().derive(repo)
    assert derived.dev_server == detected

    prof = ProjectProfile(repo_path=str(repo), ecosystem="node")
    sug = ui_evidence_suggestion(prof, repo)
    assert sug is not None
    assert sug["build_cmd"] == "npm --prefix web ci && npm --prefix web run build"

    apply_ui_evidence_suggestion(prof, sug)
    assert prof.ui_evidence["build_cmd"] == "npm --prefix web ci && npm --prefix web run build"
    assert prof.ui_evidence["start_cmd"] == "npm run dev"


def test_no_build_cmd_key_without_a_web_build_script(tmp_path):
    """No `web/package.json` (or one without a `build` script) -> no
    `build_cmd` key at all — not an empty string, no key."""
    repo = _vite_repo_without_web_build(tmp_path / "repo")
    detected = detect_dev_server(repo)
    assert detected is not None
    assert "build_cmd" not in detected

    prof = ProjectProfile(repo_path=str(repo), ecosystem="node")
    sug = ui_evidence_suggestion(prof, repo)
    assert sug is not None
    assert "build_cmd" not in sug

    apply_ui_evidence_suggestion(prof, sug)
    assert "build_cmd" not in prof.ui_evidence


def test_no_build_cmd_key_when_web_package_json_has_no_build_script(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True, exist_ok=True)
    (repo / "package.json").write_text(json.dumps({
        "scripts": {"dev": "vite"}, "devDependencies": {"vite": "^5.0.0"},
    }))
    web = repo / "web"
    web.mkdir()
    (web / "package.json").write_text(json.dumps({"scripts": {"test": "vitest"}}))
    detected = detect_dev_server(repo)
    assert detected is not None
    assert "build_cmd" not in detected


# ─────────────────────── orchestrator: reason wording ────────────────────── #


def _config(tmp_path):
    cfg = load_config(tmp_path / "config.yaml")
    cfg.data.setdefault("planning", {})["enabled"] = False
    cfg.data.setdefault("reviewer", {})["allow_advisory"] = True
    cfg.data.setdefault("blockers", {})["challenge"] = False
    cfg.data["isolation"]["enabled"] = False
    return cfg


class _FakeRepo:
    def __init__(self, path):
        self.path = str(path)

    def changed_files(self, ref="HEAD~1"):
        return ["web/App.jsx"]  # matches UI_EVIDENCE_DEFAULT_GLOBS


def _make_repo_with_manifest(tmp_path, base_url="http://127.0.0.1:5173"):
    repo_dir = tmp_path / "repo"
    manifest_dir = repo_dir / ".no_human"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "ui_evidence.json").write_text(
        f'{{"base_url": "{base_url}", "steps": [{{"goto": "/"}}, {{"shot": "loaded"}}]}}'
    )
    return _FakeRepo(repo_dir)


async def _drive_maybe_capture(
    tmp_path, store, monkeypatch, srv_outcome, *, run_shots=None,
):
    """Shared driver for the two reason-wording tests below: fakes
    `hermetic_backend` armed, `dev_server` yielding `srv_outcome` directly
    (no real subprocess), and `run` returning zero shots — so the only thing
    under test is the reason string `_maybe_capture_ui_evidence` builds."""
    import contextlib

    monkeypatch.setattr(orch_mod.ui_evidence, "playwright_available", lambda: True)

    @contextlib.asynccontextmanager
    async def _fake_hermetic_backend_armed(out_dir, **kwargs):
        yield ui_evidence.HermeticBackend(
            mode="armed", home=str(tmp_path / "hb-home"), port=54321,
            api_target="http://127.0.0.1:54321")

    monkeypatch.setattr(orch_mod.ui_evidence, "hermetic_backend", _fake_hermetic_backend_armed)

    @contextlib.asynccontextmanager
    async def _fake_dev_server(repo_path, ui_conf, base_url, out_dir, **kwargs):
        yield srv_outcome

    monkeypatch.setattr(orch_mod.ui_evidence, "dev_server", _fake_dev_server)

    async def _spy_run(repo_path, out_dir, **kwargs):
        return ui_evidence.UiEvidenceResult(verdict="not_run", reason="unused")

    monkeypatch.setattr(orch_mod.ui_evidence, "run", _spy_run)

    events = []
    cfg = _config(tmp_path)
    orch = Orchestrator(store, cfg.data, object(), SlackNotifier(None), event_sink=events.append)
    repo = _make_repo_with_manifest(tmp_path)
    task = Task.new("touch the UI", repo_path=repo.path)

    section = await orch._maybe_capture_ui_evidence(task, repo, "task-branch", "HEAD~1")
    advisories = [e["text"] for e in events if e.get("kind") == "advisory"]
    return section, advisories


async def test_rendered_skip_section_names_the_build_failure(tmp_path, store, monkeypatch):
    """AC5: a `build-failed` cause renders a skip sentence naming the build
    and its exit code — not the generic 'did not answer' timeout sentence."""
    srv = ui_evidence.DevServerOutcome(
        mode="boot-failed", base_url="http://127.0.0.1:5173", ready_timeout_s=60,
        exit_code=1, cause="build-failed",
        detail="build exit 1: UNIQUE_LOG_TAIL_MARKER npm ERR!",
    )
    section, advisories = await _drive_maybe_capture(tmp_path, store, monkeypatch, srv)

    assert "Visual proof skipped:" in section
    assert "UI build command failed" in section
    assert "exit 1" in section
    # LOG-ONLY: the raw log tail must never reach the rendered section...
    assert "UNIQUE_LOG_TAIL_MARKER" not in section
    # ...only the advisory (nh logs), and there it must be the FULL detail.
    assert any("UNIQUE_LOG_TAIL_MARKER" in a for a in advisories), advisories


async def test_rendered_skip_section_names_the_build_timeout(tmp_path, store, monkeypatch):
    """AC5: a `build-timeout` cause renders a skip sentence naming the build
    timeout — distinct wording from both `build-failed` and the original
    dev-server-timeout sentence."""
    srv = ui_evidence.DevServerOutcome(
        mode="boot-failed", base_url="http://127.0.0.1:5173", ready_timeout_s=60,
        exit_code=None, cause="build-timeout",
        detail="build timed out: UNIQUE_TIMEOUT_MARKER still compiling",
    )
    section, advisories = await _drive_maybe_capture(tmp_path, store, monkeypatch, srv)

    assert "Visual proof skipped:" in section
    assert "UI build command timed out" in section
    assert "UNIQUE_TIMEOUT_MARKER" not in section
    assert any("UNIQUE_TIMEOUT_MARKER" in a for a in advisories), advisories


async def test_rendered_skip_section_failed_to_start_wording_unchanged(tmp_path, store, monkeypatch):
    """Byte-identical guard: the pre-existing `failed-to-start` sentence
    (never touched by this feature) still renders exactly as before."""
    srv = ui_evidence.DevServerOutcome(
        mode="boot-failed", base_url="http://127.0.0.1:5173", ready_timeout_s=60,
        cause="failed-to-start", detail="refused: non-loopback host",
    )
    section, _advisories = await _drive_maybe_capture(tmp_path, store, monkeypatch, srv)
    assert "the dev server failed to start for http://127.0.0.1:5173 (boot-failed)" in section


async def test_rendered_skip_section_timeout_wording_unchanged(tmp_path, store, monkeypatch):
    """Byte-identical guard: the pre-existing (empty/`"timeout"`-cause)
    fallback sentence still renders exactly as before."""
    srv = ui_evidence.DevServerOutcome(
        mode="boot-failed", base_url="http://127.0.0.1:5173", ready_timeout_s=42,
        cause="timeout",
    )
    section, _advisories = await _drive_maybe_capture(tmp_path, store, monkeypatch, srv)
    assert "the dev server did not answer at http://127.0.0.1:5173 within 42s (boot-failed)" in section
