"""Default UI walk when the coder writes no manifest (2026-09-04).

Task 9058bf10 / PR #44: a live case where `ui_evidence` was enabled with
`start_cmd`/`base_url` set, the prompt gate fired, but the coder wrote no
`.no_human/ui_evidence.json` — the PR shipped only "Visual proof skipped:
the coder wrote no walk manifest." Honest, but gives NO visual proof
whenever the coder ignores the advisory prompt. Visual proof must not
depend on coder compliance: evidence degrades to a DEFAULT walk (goto ->
wait for network idle -> a `landing` screenshot, plus a settled shot when
the diff touched `web/src/`), never to nothing.

Two layers are tested, mirroring how `ui_evidence.py` splits the concern:

1. `ui_evidence.default_manifest` — a pure unit, plus an end-to-end drive
   through `ui_evidence.run` against a `FakePage` (the exact fixture
   `tests/test_ui_evidence.py` already stubs `run` with).
2. `Orchestrator._maybe_capture_ui_evidence`/`_default_walk_manifest`/
   `_deliver_ui_evidence` — the seam that decides WHEN the default walk
   fires (no coder manifest) and how it's labeled in the PR body, stubbed
   the same way `tests/test_ui_evidence_build_cmd.py`'s
   `_drive_maybe_capture` already stubs the browser/dev-server seams.

The coder-authored manifest, when present, keeps full precedence — none of
this changes behavior for a repo whose coder actually wrote the file.
"""
from __future__ import annotations

import contextlib
import struct

import pytest

from no_human.config import load_config
from no_human.core import evidence_ledger as evidence_ledger_mod
from no_human.core import orchestrator as orch_mod
from no_human.core.db import Store
from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task
from no_human.notify.slack import SlackNotifier
from no_human.testing import ui_evidence

# `asyncio_mode = "auto"` (pyproject.toml) — every `async def test_*` here
# runs under pytest-asyncio with no marker needed.

# A structurally-real 1x1 PNG (independent copy of tests/test_ui_evidence.py's
# PNG_1x1 — deliberately not shared across test files, per that file's own
# convention).
PNG_1x1 = (
    b"\x89PNG\r\n\x1a\n"
    + b"\x00\x00\x00\x0d"
    + b"IHDR"
    + struct.pack(">II", 1, 1)
    + b"\x08\x06\x00\x00\x00"
    + b"\x00\x00\x00\x00"
    + b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


# ─────────────────────── layer 1: default_manifest / run ─────────────────── #


def test_default_manifest_none_when_base_url_fails_loopback_rule():
    """Same rejection `_base_url_problem` enforces for a coder-authored
    manifest — a bad/unparsable profile `base_url` degrades to the caller's
    disclosed skip, never an exception."""
    assert ui_evidence.default_manifest("http://evil.example.com") is None
    assert ui_evidence.default_manifest("") is None


def test_default_manifest_landing_only_shape():
    m = ui_evidence.default_manifest("http://127.0.0.1:5173", "/app")
    assert m is not None
    assert m.base_url == "http://127.0.0.1:5173"
    actions = [(s.action, s.value) for s in m.steps]
    assert actions == [
        ("goto", "/app"),
        ("wait_idle", "networkidle"),
        ("shot", "landing"),
    ]
    assert m.raw["_source"] == "default-walk"


def test_default_manifest_adds_a_settled_shot_when_web_src_touched():
    m = ui_evidence.default_manifest("http://127.0.0.1:5173", "/", web_src=True)
    assert m is not None
    names = [s.value for s in m.steps if s.action == "shot"]
    assert names == ["landing", "landing-settled"]
    idle_count = sum(1 for s in m.steps if s.action == "wait_idle")
    assert idle_count == 2


class _FakeContext:
    async def close(self):
        return None


class _FakeKeyboard:
    pass


class FakePage:
    """No `wait_for_load_state` method on purpose — the `wait_idle` action
    must tolerate a page that doesn't have it (see `_dispatch`'s
    `contextlib.suppress` + `getattr(..., None)` guard)."""

    def __init__(self):
        self.calls = []
        self.context = _FakeContext()
        self.keyboard = _FakeKeyboard()

    def on(self, event, handler):
        return None

    async def goto(self, url):
        self.calls.append(("goto", url))

    async def screenshot(self):
        self.calls.append(("screenshot",))
        return PNG_1x1


def make_launch(page):
    async def _launch(out_dir, viewport):
        return page

    return _launch


async def test_default_manifest_drives_a_fake_page_end_to_end(tmp_path, monkeypatch):
    """The default manifest, handed straight to `run(..., manifest=...)`,
    must execute against a stubbed page exactly like a coder-authored one —
    no exception from the `wait_idle` action even though `FakePage` has no
    `wait_for_load_state`, and the `landing`/`landing-settled` shots both
    land alongside the always-taken `final` shot."""
    monkeypatch.setattr(ui_evidence, "_reachable", lambda url: True)
    manifest = ui_evidence.default_manifest(
        "http://127.0.0.1:5173", "/", web_src=True)
    assert manifest is not None
    out_dir = tmp_path / "evidence"
    page = FakePage()
    result = await ui_evidence.run(
        tmp_path, out_dir, launch=make_launch(page), manifest=manifest)

    assert result.verdict == "ran", result.reason
    names = [s["name"] for s in result.shots]
    assert names == ["landing", "landing-settled", "final"]
    assert (out_dir / "landing.png").exists()
    assert (out_dir / "landing-settled.png").exists()


async def test_default_manifest_not_used_when_coder_manifest_present(tmp_path, monkeypatch):
    """`manifest=None` (the byte-identical default) means `run` still reads
    `.no_human/ui_evidence.json` off disk, never the injected fallback."""
    monkeypatch.setattr(ui_evidence, "_reachable", lambda url: True)
    d = tmp_path / ".no_human"
    d.mkdir()
    (d / "ui_evidence.json").write_text(
        '{"base_url": "http://127.0.0.1:5173", '
        '"steps": [{"goto": "/"}, {"shot": "coder-shot"}]}'
    )
    page = FakePage()
    result = await ui_evidence.run(tmp_path, tmp_path / "evidence", launch=make_launch(page))
    names = [s["name"] for s in result.shots]
    assert "coder-shot" in names
    assert "landing" not in names


# ───────────────────── layer 2: _deliver_ui_evidence labeling ────────────── #


class _FakeRepoForDeliver:
    def __init__(self, path, remote="https://github.com/acme/widget.git"):
        self.path = str(path)
        self._remote = remote

    def remote_url(self):
        return self._remote


def test_deliver_ui_evidence_default_walk_labels_the_section(tmp_path, monkeypatch):
    """AC2: the PR section renders these shots labeled 'default walk (no
    coder manifest)' — the existing skip text is replaced by real images,
    not merely a different sentence."""
    monkeypatch.setattr(evidence_ledger_mod, "deliver", lambda *a, **k: True)
    orch = Orchestrator.__new__(Orchestrator)
    repo = _FakeRepoForDeliver(tmp_path / "repo")
    out_dir = tmp_path / "evidence"
    out_dir.mkdir()
    (out_dir / "landing.png").write_bytes(PNG_1x1)
    result = ui_evidence.UiEvidenceResult(
        verdict="ran", shots=[{"name": "landing", "path": "landing.png"}])

    section = orch._deliver_ui_evidence(
        repo, "task123", out_dir, result, default_walk=True)

    assert "## UI evidence" in section
    assert "default walk (no coder manifest)" in section
    assert "![default walk (no coder manifest): landing](" in section
    assert "Visual proof skipped" not in section


def test_deliver_ui_evidence_without_default_walk_flag_is_unlabeled(tmp_path, monkeypatch):
    """`default_walk` defaults to `False` — every pre-existing call site
    (a real coder manifest) renders byte-identical to before this
    parameter existed: no 'default walk' label anywhere."""
    monkeypatch.setattr(evidence_ledger_mod, "deliver", lambda *a, **k: True)
    orch = Orchestrator.__new__(Orchestrator)
    repo = _FakeRepoForDeliver(tmp_path / "repo")
    out_dir = tmp_path / "evidence"
    out_dir.mkdir()
    (out_dir / "loaded.png").write_bytes(PNG_1x1)
    result = ui_evidence.UiEvidenceResult(
        verdict="ran", shots=[{"name": "loaded", "path": "loaded.png"}])

    section = orch._deliver_ui_evidence(repo, "task123", out_dir, result)

    assert "default walk (no coder manifest)" not in section
    assert "![loaded](" in section


# ─────────────────── layer 2: _maybe_capture_ui_evidence seam ────────────── #


def _config(tmp_path):
    cfg = load_config(tmp_path / "config.yaml")
    cfg.data.setdefault("planning", {})["enabled"] = False
    cfg.data.setdefault("reviewer", {})["allow_advisory"] = True
    cfg.data.setdefault("blockers", {})["challenge"] = False
    cfg.data["isolation"]["enabled"] = False
    return cfg


class _FakeRepo:
    def __init__(self, path, remote="https://github.com/acme/widget.git"):
        self.path = str(path)
        self._remote = remote

    def changed_files(self, ref="HEAD~1"):
        return ["web/App.jsx"]  # matches UI_EVIDENCE_DEFAULT_GLOBS

    def remote_url(self):
        return self._remote


def _make_repo(tmp_path, *, with_manifest, base_url="http://127.0.0.1:5173"):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    if with_manifest:
        manifest_dir = repo_dir / ".no_human"
        manifest_dir.mkdir(parents=True, exist_ok=True)
        (manifest_dir / "ui_evidence.json").write_text(
            f'{{"base_url": "{base_url}", "steps": [{{"goto": "/"}}, {{"shot": "loaded"}}]}}'
        )
    return _FakeRepo(repo_dir)


class _Profile:
    def __init__(self, **ui_overrides):
        self.ui_evidence = {
            "enabled": True, "start_cmd": "npm run dev",
            "base_url": "http://127.0.0.1:5173", "ready_path": "/",
            **ui_overrides,
        }


@pytest.fixture
async def store(tmp_path):
    s = await Store(tmp_path / "nh.db").connect()
    yield s
    await s.close()


async def _drive(
    tmp_path, store, monkeypatch, srv_outcome, *, with_manifest,
    run_result_shots=(("landing", "landing.png"),),
):
    """Shared driver: fakes `hermetic_backend` armed, `dev_server` yielding
    `srv_outcome`, `evidence_ledger.deliver` succeeding, and spies on
    `ui_evidence.run`'s kwargs (the thing under test — did the caller pass
    the harness's own default `Manifest`, or `manifest=None` to defer to
    the coder's file on disk?)."""
    monkeypatch.setattr(orch_mod.ui_evidence, "playwright_available", lambda: True)
    monkeypatch.setattr(evidence_ledger_mod, "deliver", lambda *a, **k: True)

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

    run_calls = []

    async def _spy_run(repo_path, out_dir, **kwargs):
        run_calls.append(kwargs)
        out_dir.mkdir(parents=True, exist_ok=True)
        shots = []
        for name, path in run_result_shots:
            (out_dir / path).write_bytes(PNG_1x1)
            shots.append({"name": name, "path": path})
        if not shots:
            return ui_evidence.UiEvidenceResult(verdict="not_run", reason="server not reachable")
        return ui_evidence.UiEvidenceResult(verdict="ran", shots=shots)

    monkeypatch.setattr(orch_mod.ui_evidence, "run", _spy_run)

    events = []
    cfg = _config(tmp_path)
    orch = Orchestrator(store, cfg.data, object(), SlackNotifier(None), event_sink=events.append)
    orch._active_profile = _Profile()
    repo = _make_repo(tmp_path, with_manifest=with_manifest)
    task = Task.new("touch the UI", repo_path=repo.path)

    section = await orch._maybe_capture_ui_evidence(task, repo, "task-branch", "HEAD~1")
    advisories = [e["text"] for e in events if e.get("kind") == "advisory"]
    return section, advisories, run_calls


async def test_no_coder_manifest_and_configured_profile_invokes_the_default_walk(
        tmp_path, store, monkeypatch):
    """AC1: no manifest + enabled profile => the default walk is invoked
    (`ui_evidence.run` receives a real `Manifest`, not `manifest=None`) and
    the rendered section carries the 'default walk' label, not the old
    unconditional skip text."""
    srv = ui_evidence.DevServerOutcome(
        mode="booted", start_cmd="npm run dev", base_url="http://127.0.0.1:5173",
        ready_timeout_s=60,
    )
    section, advisories, run_calls = await _drive(
        tmp_path, store, monkeypatch, srv, with_manifest=False)

    assert len(run_calls) == 1
    assert run_calls[0]["manifest"] is not None
    assert isinstance(run_calls[0]["manifest"], ui_evidence.Manifest)

    assert "## UI evidence" in section
    assert "default walk (no coder manifest)" in section
    assert "![default walk (no coder manifest): landing](" in section
    # The OLD unconditional skip sentence is gone — this is a real,
    # embedded-image section now, not a disclosed-skip one.
    assert "Visual proof skipped" not in section


async def test_coder_manifest_present_takes_full_precedence(tmp_path, store, monkeypatch):
    """AC3: a coder-authored manifest means the default path is NOT taken —
    `ui_evidence.run` gets `manifest=None` (its byte-identical default,
    deferring to the file on disk), and the rendered section carries no
    'default walk' label."""
    srv = ui_evidence.DevServerOutcome(
        mode="booted", start_cmd="npm run dev", base_url="http://127.0.0.1:5173",
        ready_timeout_s=60,
    )
    section, advisories, run_calls = await _drive(
        tmp_path, store, monkeypatch, srv, with_manifest=True,
        run_result_shots=(("loaded", "loaded.png"),))

    assert len(run_calls) == 1
    assert run_calls[0]["manifest"] is None

    assert "## UI evidence" in section
    assert "default walk (no coder manifest)" not in section
    assert "![loaded](" in section


async def test_default_walk_boot_failure_still_yields_the_disclosed_skip(
        tmp_path, store, monkeypatch):
    """No coder manifest AND the (default-walk-targeted) dev server refuses
    to boot: still a DISCLOSED skip — never an exception out of
    `_maybe_capture_ui_evidence`, and never delivered as if it were a real
    walk (the `run` call happens — `dev_server`'s `boot-failed` mode does
    not short-circuit the call site — but with the server unreachable it
    reports no shots, which is what actually triggers the skip)."""
    srv = ui_evidence.DevServerOutcome(
        mode="boot-failed", base_url="http://127.0.0.1:5173", ready_timeout_s=60,
        exit_code=None, cause="failed-to-start",
        detail="ECONNREFUSED",
    )
    section, advisories, run_calls = await _drive(
        tmp_path, store, monkeypatch, srv, with_manifest=False,
        run_result_shots=())

    assert "Visual proof skipped:" in section
    assert "the dev server failed to start" in section
    assert "default walk (no coder manifest)" not in section


async def test_no_coder_manifest_and_no_configured_base_url_still_skips(
        tmp_path, store, monkeypatch):
    """Belt-and-suspenders: with NEITHER a coder manifest NOR a usable
    profile `base_url`, the default walk itself has nothing to walk
    against — the original disclosed skip still fires, never an exception."""
    monkeypatch.setattr(orch_mod.ui_evidence, "playwright_available", lambda: True)

    events = []
    cfg = _config(tmp_path)
    orch = Orchestrator(store, cfg.data, object(), SlackNotifier(None), event_sink=events.append)
    orch._active_profile = _Profile(base_url="")  # nothing usable to fall back to
    repo = _make_repo(tmp_path, with_manifest=False)
    task = Task.new("touch the UI", repo_path=repo.path)

    section = await orch._maybe_capture_ui_evidence(task, repo, "task-branch", "HEAD~1")

    assert "Visual proof skipped:" in section
    assert "the default walk could not run" in section
