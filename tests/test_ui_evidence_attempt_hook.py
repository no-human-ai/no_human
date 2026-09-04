"""D1.2 attempt-integration test: after tests pass on a task whose diff
touches `web/`, the harness actually invokes `testing/ui_evidence.py`'s
`run()` (the wiring `tests/test_ui_evidence_prompt.py`'s part-1 absence pin
used to guarantee did NOT exist), delivers the captured shots + video to a
side branch (never the task branch itself — see the D1.2 decision-gate test
in `tests/test_approve_merge.py`), and embeds them in the PR body. A task
whose diff never touches a UI path gets none of this.

Real git end-to-end (bare origin + a working clone), a fake coding backend
(no LLM spend), and a fake `ui_evidence.run` (no real browser/Playwright) —
the same harness shape as `tests/test_e2e_orchestrator.py`, trimmed to only
what this hook needs. `open_pr` is faked (mirrors that file's `0a` tests) so
no real `gh` call is made; `GitRepo.remote_url` is faked to a github.com URL
so the raw-image-embed path (github.com only, by design) is exercised for
real, while the actual git push/branch-create/commit run against the real
local bare repo underneath it.
"""
from __future__ import annotations

import functools
import json
import subprocess
from pathlib import Path

import pytest

from no_human.agent.claude_backend import AgentEvent, AgentResult
from no_human.config import load_config
from no_human.core import orchestrator as orch_mod
from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task, TaskStatus
from no_human.notify.slack import SlackNotifier
from no_human.profile import ProjectProfile
from no_human.testing import ui_evidence
from no_human.vcs import PrResult
from no_human.vcs.git import GitRepo


def _git(cwd, *args, check=True):
    return subprocess.run(
        ["git", "-c", "user.email=t@t.t", "-c", "user.name=t",
         "-c", "commit.gpgsign=false", *args],
        cwd=str(cwd), capture_output=True, text=True, check=check,
    )


@pytest.fixture
def repo_env(tmp_path):
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))

    work = tmp_path / "work"
    _git(tmp_path, "init", "-q", "-b", "main", str(work))
    (work / "web").mkdir()
    (work / "web" / "App.jsx").write_text("export default function App() { return null; }\n")
    (work / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (work / "test_calc.py").write_text(
        "from calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n"
    )
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "init")
    _git(work, "remote", "add", "origin", str(origin))
    _git(work, "push", "-q", "origin", "main")
    return {"origin": origin, "work": work, "tmp_path": tmp_path}


class FakeBackend:
    def __init__(self, mutate):
        self.mutate = mutate

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        if on_event:
            on_event(AgentEvent("tool_use", tool_name="Edit",
                                tool_input={"file_path": "calc.py"}))
        self.mutate(cwd)
        return AgentResult(final_text="done", num_turns=2, is_error=False,
                           tokens_used=100, session_id="s", stop_reason="end_turn")


def _config(tmp_path):
    cfg = load_config(tmp_path / "config.yaml")
    cfg.data.setdefault("planning", {})["enabled"] = False
    cfg.data.setdefault("reviewer", {})["allow_advisory"] = True
    cfg.data.setdefault("blockers", {})["challenge"] = False
    cfg.data["isolation"]["enabled"] = False  # repo IS repo_env["work"]
    return cfg


def _fake_open_pr(url="https://github.com/acme/widget/pull/1"):
    opens = []

    def fake_open_pr(repo, branch, title, body, **kwargs):
        opens.append({"body": body, "branch": branch})
        return PrResult(url=url, kind="github", branch=branch, pushed_sha=repo.head_sha())

    return fake_open_pr, opens


def _fake_ui_evidence_run(calls, shots=("loaded", "final"), video="walk.webm"):
    async def fake_run(repo_path, out_dir, **kwargs):
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        calls.append({"repo_path": Path(repo_path), "out_dir": out_dir})
        shot_dicts = []
        for name in shots:
            (out_dir / f"{name}.png").write_bytes(b"\x89PNGDATA" + name.encode())
            shot_dicts.append({"name": name, "path": f"{name}.png", "step_index": 0})
        if video:
            (out_dir / video).write_bytes(b"WEBMDATA")
        return ui_evidence.UiEvidenceResult(
            verdict="ran", shots=shot_dicts, video=video,
            steps_run=len(shots), steps_total=len(shots),
        )

    return fake_run


def _fake_ui_evidence_run_empty(calls, reason="no shots"):
    """Mirrors `test_ui_evidence_missing_playwright_pr_line.py`'s helper of
    the same name: a walk that ran (or was attempted) but captured nothing,
    the shape `_maybe_capture_ui_evidence` needs to reach its dev-server-
    aware skip-reason branch."""
    async def fake_run(repo_path, out_dir, **kwargs):
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        calls.append({"repo_path": Path(repo_path), "out_dir": out_dir})
        return ui_evidence.UiEvidenceResult(
            verdict="not_run", shots=[], video=None,
            steps_run=0, steps_total=2, reason=reason,
        )

    return fake_run


class _FakeDevServerProc:
    """Stand-in for a `subprocess.Popen` handle: never exits on its own."""

    def __init__(self, pid=9999):
        self.pid = pid

    def poll(self):
        return None

    def wait(self, timeout=None):
        return None


def _spawn_recorder(calls, proc_obj, log_bytes=None, written_log=None):
    """`log_bytes`, when given, is written to `kwargs["stdout"]` — the real
    file handle `dev_server` opened for the dev-server log — and flushed, so
    a boot-failed test's log actually contains recognizable text on disk
    instead of an empty file. `written_log`, when given, is a list this
    appends the bytes read straight back off disk to (a positive control
    that the write really landed, not just that `.write()` was called)."""
    def _spawn(argv, **kwargs):
        calls.append({"argv": argv, "kwargs": kwargs})
        if log_bytes is not None:
            fh = kwargs["stdout"]
            fh.write(log_bytes)
            fh.flush()
            if written_log is not None:
                written_log.append(Path(fh.name).read_bytes())
        return proc_obj

    return _spawn


def _kill_recorder(calls):
    def _kill(proc):
        calls.append(proc)

    return _kill


async def _no_sleep(_seconds):
    return None


class _StepClock:
    """Deterministic monotonic clock: pops one value per call, repeating the
    last value forever once the scripted list is exhausted — same shape as
    `tests/test_ui_evidence.py`'s `FakeClock`, kept local to this file since
    the two suites intentionally do not share test fixtures."""

    def __init__(self, values):
        self._values = list(values)
        self._last = 0.0

    def __call__(self):
        if self._values:
            self._last = self._values.pop(0)
        return self._last


def _wire_fake_dev_server(monkeypatch, *, spawn, reachable, kill=None, clock=None):
    """Patch `orch_mod.ui_evidence.dev_server` to the REAL function with only
    its spawn/reachable/kill/clock seams overridden — `_maybe_capture_ui_
    evidence` still calls `ui_evidence.dev_server(repo_path, ui_conf,
    base_url, out_dir)` exactly as in production and still branches on the
    `DevServerOutcome` it gets back; only the OS subprocess and the network
    probe underneath it are faked, so the wiring itself is never bypassed."""
    real_dev_server = ui_evidence.dev_server
    fake = functools.partial(
        real_dev_server, spawn=spawn, reachable=reachable,
        kill=kill or _kill_recorder([]), clock=clock or (lambda: 0.0),
        sleep=_no_sleep,
    )
    monkeypatch.setattr(orch_mod.ui_evidence, "dev_server", fake)


def _wire_fake_hermetic_backend(monkeypatch, *, spawn, reachable, pick_port=None,
                                kill=None, clock=None, home_root=None):
    """Patch `orch_mod.ui_evidence.hermetic_backend` to the REAL function with
    only its spawn/reachable/pick_port/kill/clock seams overridden — same
    shape as `_wire_fake_dev_server`: `_maybe_capture_ui_evidence` still
    calls the real state machine (and its real `dev_server` afterwards) and
    only the OS subprocess, ephemeral-port bind, and network probe
    underneath `hermetic_backend` are faked."""
    real_hermetic_backend = ui_evidence.hermetic_backend
    fake = functools.partial(
        real_hermetic_backend, spawn=spawn, reachable=reachable,
        pick_port=pick_port or (lambda: 54321),
        kill=kill or _kill_recorder([]), clock=clock or (lambda: 0.0),
        sleep=_no_sleep, home_root=home_root,
    )
    monkeypatch.setattr(orch_mod.ui_evidence, "hermetic_backend", fake)


def _wire_armed_hermetic_backend(monkeypatch, tmp_path):
    """Every manifest with a `base_url` now arms a hermetic backend before
    `dev_server` even runs (D2-hermetic) — a test whose point is `dev_
    server`'s own behavior, not the hermetic backend's, wires this so that
    backend arms instantly on its first readiness probe and never touches a
    real subprocess/port/HOME. `home_root=tmp_path` confines the (faked, so
    never actually created by a real `nh start`) throwaway HOME bookkeeping
    to this test's own tmpdir, never the real machine."""
    _wire_fake_hermetic_backend(
        monkeypatch,
        spawn=_spawn_recorder([], _FakeDevServerProc(pid=8888)),
        reachable=lambda url: True,
        home_root=tmp_path,
    )


async def test_ui_touching_diff_invokes_the_walk_and_embeds_the_pr_media(
        repo_env, tmp_path, store, monkeypatch):
    def mutate(cwd):
        (Path(cwd) / "web" / "App.jsx").write_text(
            "export default function App() { return <div id=\"x\" />; }\n"
        )
        d = Path(cwd) / ".no_human"
        d.mkdir(parents=True, exist_ok=True)
        (d / "ui_evidence.json").write_text(json.dumps(
            {"base_url": "http://127.0.0.1:5173", "steps": [{"goto": "/"}, {"shot": "loaded"}]}
        ))

    calls: list[dict] = []
    monkeypatch.setattr(orch_mod.ui_evidence, "run", _fake_ui_evidence_run(calls))
    # This test exercises the walk actually running — pin playwright as
    # present so it passes identically whether or not the `e2e` dependency
    # group happens to be installed on the machine running the suite (the
    # honesty-floor gate added in `_maybe_capture_ui_evidence` has its own
    # dedicated coverage in `test_ui_evidence_missing_playwright_pr_line.py`).
    monkeypatch.setattr(orch_mod.ui_evidence, "playwright_available", lambda: True)
    monkeypatch.setattr(GitRepo, "remote_url",
                        lambda self, remote="origin": "https://github.com/acme/widget.git")
    fake_open_pr, opens = _fake_open_pr()
    monkeypatch.setattr(orch_mod, "open_pr", fake_open_pr)
    _wire_armed_hermetic_backend(monkeypatch, tmp_path)

    cfg = _config(tmp_path)
    orch = Orchestrator(store, cfg.data, FakeBackend(mutate), SlackNotifier(None),
                        event_sink=[].append)
    t = Task.new("touch the UI", repo_path=str(repo_env["work"]))
    t.acceptance_criteria = ["the button renders"]
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.AWAITING_APPROVAL, outcome.detail
    # The walk actually ran, against THIS attempt's own repo checkout.
    assert len(calls) == 1, f"expected exactly one ui_evidence.run() call, got {calls}"
    assert calls[0]["repo_path"] == repo_env["work"]

    # Delivered on a SIDE branch, never the task branch: the D1.2 ruling,
    # checked here against a real git remote rather than merely asserted.
    evidence_branch = f"nh-evidence/{t.id}"
    branches = _git(repo_env["origin"], "branch", "--list").stdout
    assert evidence_branch in branches, branches
    names = _git(repo_env["origin"], "ls-tree", "-r", "--name-only",
                evidence_branch).stdout
    assert f".nh-evidence/{t.id}/loaded.png" in names, names
    assert f".nh-evidence/{t.id}/final.png" in names, names
    assert f".nh-evidence/{t.id}/walk.webm" in names, names

    # The task branch itself never carries the evidence directory. Checked
    # against the LOCAL clone's object database, not origin: `open_pr` is
    # faked here (no `gh` in this test environment), so the task branch's
    # own push never happens — only `_deliver_ui_evidence`'s side-branch
    # push is real, which is exactly the mechanism under test.
    task_branch = opens[-1]["branch"]
    task_names = _git(repo_env["work"], "ls-tree", "-r", "--name-only",
                      task_branch).stdout
    assert ".nh-evidence" not in task_names, task_names

    # The PR body embeds the media, via github.com raw URLs on the evidence
    # branch — never an absolute local filesystem path.
    body = opens[-1]["body"]
    assert "## UI evidence" in body, body
    expected_prefix = (
        f"https://raw.githubusercontent.com/acme/widget/{evidence_branch}/"
        f".nh-evidence/{t.id}/")
    assert f"![loaded]({expected_prefix}loaded.png)" in body, body
    assert f"[walk video]({expected_prefix}walk.webm)" in body, body

    # The working tree is left on the task branch, not stranded on the side
    # branch, once the attempt finishes.
    assert _git(repo_env["work"], "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() \
        == task_branch


async def test_non_ui_diff_never_invokes_the_walk_or_adds_a_media_section(
        repo_env, tmp_path, store, monkeypatch):
    def mutate(cwd):
        (Path(cwd) / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a * b\n"
        )
        (Path(cwd) / "test_calc.py").write_text(
            "from calc import add, mul\n\n"
            "def test_add():\n    assert add(1, 2) == 3\n\n"
            "def test_mul():\n    assert mul(2, 3) == 6\n"
        )
        # No `.no_human/ui_evidence.json` manifest either — a non-UI task
        # would not have been shown the coder prompt block that suggests one.

    calls: list[dict] = []
    monkeypatch.setattr(orch_mod.ui_evidence, "run", _fake_ui_evidence_run(calls))
    fake_open_pr, opens = _fake_open_pr()
    monkeypatch.setattr(orch_mod, "open_pr", fake_open_pr)

    cfg = _config(tmp_path)
    orch = Orchestrator(store, cfg.data, FakeBackend(mutate), SlackNotifier(None),
                        event_sink=[].append)
    t = Task.new("add mul()", repo_path=str(repo_env["work"]))
    t.acceptance_criteria = ["mul(a,b) returns product"]
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.AWAITING_APPROVAL, outcome.detail
    assert calls == [], f"ui_evidence.run() must not run for a non-UI diff: {calls}"
    branches = _git(repo_env["origin"], "branch", "--list").stdout
    assert f"nh-evidence/{t.id}" not in branches, branches
    assert "## UI evidence" not in opens[-1]["body"], opens[-1]["body"]


async def test_a_second_attempts_delivery_does_not_lose_evidence_to_a_non_fast_forward(
        repo_env, tmp_path, store, monkeypatch):
    """`_deliver_ui_evidence` recreates `nh-evidence/<task-id>` from the task
    branch's CURRENT tip every call (`create_branch` is `checkout -B`) — a
    task's second attempt produces history unrelated to the first under the
    SAME branch name. A plain push would be a non-fast-forward rejection on
    every retry, silently losing evidence from attempt 2 onward (caught by
    `_deliver_ui_evidence`'s own `except Exception`, so nothing crashes —
    the delivery just quietly returns no evidence). `force_with_lease` on
    that push is what this test pins directly, calling the delivery method
    twice against the same real repo without going through a full attempt."""
    monkeypatch.setattr(GitRepo, "remote_url",
                        lambda self, remote="origin": "https://github.com/acme/widget.git")
    cfg = _config(tmp_path)
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None), event_sink=[].append)
    repo = GitRepo(repo_env["work"])
    task_id = "cafef00d00000000"

    out_dir1 = ui_evidence.default_out_dir(task_id)
    (out_dir1 / "loaded.png").write_bytes(b"FIRST-ATTEMPT")
    section1 = orch._deliver_ui_evidence(
        repo, task_id, out_dir1,
        ui_evidence.UiEvidenceResult(verdict="ran",
                                     shots=[{"name": "loaded", "path": "loaded.png"}]),
    )
    assert section1, "the first attempt's delivery must succeed"

    out_dir2 = ui_evidence.default_out_dir(task_id)
    (out_dir2 / "loaded.png").write_bytes(b"SECOND-ATTEMPT")
    section2 = orch._deliver_ui_evidence(
        repo, task_id, out_dir2,
        ui_evidence.UiEvidenceResult(verdict="ran",
                                     shots=[{"name": "loaded", "path": "loaded.png"}]),
    )
    assert section2, (
        "the second attempt's re-delivery must not be silently lost to a "
        "non-fast-forward push rejection")

    evidence_branch = f"nh-evidence/{task_id}"
    content = _git(repo_env["origin"], "show",
                   f"{evidence_branch}:.nh-evidence/{task_id}/loaded.png").stdout
    assert content == "SECOND-ATTEMPT", (
        "the branch must reflect the LATEST attempt's content, not be stuck "
        f"on the first: {content!r}")
    assert _git(repo_env["work"], "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() \
        == "main", "the working tree must be back on the original branch"


async def test_ui_touching_diff_with_no_manifest_written_skips_the_walk(
        repo_env, tmp_path, store, monkeypatch):
    """The gate is the DIFF (web/**), but `ui_evidence.run()` is only worth
    calling when the coder actually left a manifest — see
    `_maybe_capture_ui_evidence`'s cheap early-out."""
    def mutate(cwd):
        (Path(cwd) / "web" / "App.jsx").write_text(
            "export default function App() { return <div id=\"y\" />; }\n"
        )
        # UI file touched, but no `.no_human/ui_evidence.json` written.

    calls: list[dict] = []
    monkeypatch.setattr(orch_mod.ui_evidence, "run", _fake_ui_evidence_run(calls))
    # Isolate "no manifest" from the (separately-tested) "no playwright"
    # honesty-floor path — pin playwright as present so this test's
    # empty-section assertion below is about the manifest, not the gate.
    monkeypatch.setattr(orch_mod.ui_evidence, "playwright_available", lambda: True)
    fake_open_pr, opens = _fake_open_pr()
    monkeypatch.setattr(orch_mod, "open_pr", fake_open_pr)

    cfg = _config(tmp_path)
    orch = Orchestrator(store, cfg.data, FakeBackend(mutate), SlackNotifier(None),
                        event_sink=[].append)
    t = Task.new("tweak the UI, no walk", repo_path=str(repo_env["work"]))
    t.acceptance_criteria = ["it renders"]
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.AWAITING_APPROVAL, outcome.detail
    assert calls == [], f"no manifest was written; run() must not be called: {calls}"
    # The walk is skipped, but the honesty floor means skipped is disclosed,
    # not silently rendered as an empty section.
    assert "## UI evidence" in opens[-1]["body"], opens[-1]["body"]
    assert "no `.no_human/ui_evidence.json` walk manifest" in opens[-1]["body"], \
        opens[-1]["body"]


def _profile_with_ui_evidence(repo_path, *, start_cmd, base_url,
                              ready_path="/", ready_timeout_s=30):
    prof = ProjectProfile(repo_path=str(repo_path))
    prof.ui_evidence = {
        **prof.ui_evidence,
        "enabled": True,
        "start_cmd": start_cmd,
        "base_url": base_url,
        "ready_path": ready_path,
        "ready_timeout_s": ready_timeout_s,
    }
    return prof


async def test_booted_dev_server_is_disclosed_in_the_pr_body_and_stopped(
        repo_env, tmp_path, store, monkeypatch):
    """D2 (2026-09-02): when nothing already answers at the manifest's
    `base_url` and the profile has a `start_cmd` configured, the harness
    boots it through `ui_evidence.dev_server` — never bypassed here, only
    its spawn/reachable/kill seams are faked — and the PR body discloses
    that it booted and stopped the server itself."""
    base_url = "http://127.0.0.1:5199"
    start_cmd = "python -m http.server 5199"

    def mutate(cwd):
        (Path(cwd) / "web" / "App.jsx").write_text(
            "export default function App() { return <div id=\"z\" />; }\n"
        )
        d = Path(cwd) / ".no_human"
        d.mkdir(parents=True, exist_ok=True)
        (d / "ui_evidence.json").write_text(json.dumps(
            {"base_url": base_url, "steps": [{"goto": "/"}, {"shot": "loaded"}]}
        ))

    spawn_calls: list[dict] = []
    kill_calls: list[object] = []
    fake_proc = _FakeDevServerProc()

    def reachable(url):
        # Never already up; ready the instant the readiness probe fires
        # (base_url + ready_path) so the loop exits on its first check.
        return url == base_url + "/"

    _wire_fake_dev_server(
        monkeypatch,
        spawn=_spawn_recorder(spawn_calls, fake_proc),
        reachable=reachable,
        kill=_kill_recorder(kill_calls),
    )
    _wire_armed_hermetic_backend(monkeypatch, tmp_path)

    calls: list[dict] = []
    monkeypatch.setattr(orch_mod.ui_evidence, "run", _fake_ui_evidence_run(calls))
    monkeypatch.setattr(orch_mod.ui_evidence, "playwright_available", lambda: True)
    monkeypatch.setattr(GitRepo, "remote_url",
                        lambda self, remote="origin": "https://github.com/acme/widget.git")
    fake_open_pr, opens = _fake_open_pr()
    monkeypatch.setattr(orch_mod, "open_pr", fake_open_pr)

    prof = _profile_with_ui_evidence(repo_env["work"], start_cmd=start_cmd,
                                     base_url=base_url)

    async def fake_usable_profile(self, repo_path):
        return prof

    monkeypatch.setattr(Orchestrator, "_usable_profile", fake_usable_profile)

    cfg = _config(tmp_path)
    orch = Orchestrator(store, cfg.data, FakeBackend(mutate), SlackNotifier(None),
                        event_sink=[].append)
    t = Task.new("touch the UI, boot the dev server", repo_path=str(repo_env["work"]))
    t.acceptance_criteria = ["the button renders"]
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.AWAITING_APPROVAL, outcome.detail
    assert len(calls) == 1, f"expected exactly one ui_evidence.run() call, got {calls}"
    assert len(spawn_calls) == 1, spawn_calls
    assert spawn_calls[0]["argv"] == ["python", "-m", "http.server", "5199"]
    assert len(kill_calls) == 1, "the harness must stop the server it booted"

    body = opens[-1]["body"]
    assert "## UI evidence" in body, body
    assert (
        f"Dev server booted by the harness for this walk (`{start_cmd}`), "
        "stopped afterwards." in body
    ), body
    assert "already running" not in body, body


async def test_booted_dev_server_receives_the_hermetic_backends_vite_api_target(
        repo_env, tmp_path, store, monkeypatch):
    """D2-hermetic bugfix (2026-09-03): `_maybe_capture_ui_evidence` only
    ever hands `dev_server` the ARMED hermetic backend's own `api_target`
    (`extra_env={"VITE_API_TARGET": hb.api_target} if hb else None` in
    `core/orchestrator.py`) — never the manifest's real `base_url` — so the
    booted `npm run dev`/`start_cmd` proxies `/api` at the throwaway,
    isolated `nh start`, never the operator's live `:8420` board. That line
    had NO test going through the orchestrator path before this one: every
    prior assertion on a booted dev server's spawn env only exercised
    `ui_evidence.dev_server` directly (`tests/test_ui_evidence_hermetic_
    backend.py`), so mutating this call site to `extra_env=None` left every
    existing test green. This test goes red on that exact mutation."""
    base_url = "http://127.0.0.1:5198"
    start_cmd = "python -m http.server 5198"

    def mutate(cwd):
        (Path(cwd) / "web" / "App.jsx").write_text(
            "export default function App() { return <div id=\"z\" />; }\n"
        )
        d = Path(cwd) / ".no_human"
        d.mkdir(parents=True, exist_ok=True)
        (d / "ui_evidence.json").write_text(json.dumps(
            {"base_url": base_url, "steps": [{"goto": "/"}, {"shot": "loaded"}]}
        ))

    spawn_calls: list[dict] = []
    kill_calls: list[object] = []
    fake_proc = _FakeDevServerProc()

    def reachable(url):
        return url == base_url + "/"

    _wire_fake_dev_server(
        monkeypatch,
        spawn=_spawn_recorder(spawn_calls, fake_proc),
        reachable=reachable,
        kill=_kill_recorder(kill_calls),
    )
    # `_wire_armed_hermetic_backend` leaves `pick_port` at its default
    # (`lambda: 54321`), so an armed backend's `api_target` is deterministic:
    # `http://127.0.0.1:54321`.
    _wire_armed_hermetic_backend(monkeypatch, tmp_path)

    calls: list[dict] = []
    monkeypatch.setattr(orch_mod.ui_evidence, "run", _fake_ui_evidence_run(calls))
    monkeypatch.setattr(orch_mod.ui_evidence, "playwright_available", lambda: True)
    monkeypatch.setattr(GitRepo, "remote_url",
                        lambda self, remote="origin": "https://github.com/acme/widget.git")
    fake_open_pr, opens = _fake_open_pr()
    monkeypatch.setattr(orch_mod, "open_pr", fake_open_pr)

    prof = _profile_with_ui_evidence(repo_env["work"], start_cmd=start_cmd,
                                     base_url=base_url)

    async def fake_usable_profile(self, repo_path):
        return prof

    monkeypatch.setattr(Orchestrator, "_usable_profile", fake_usable_profile)

    cfg = _config(tmp_path)
    orch = Orchestrator(store, cfg.data, FakeBackend(mutate), SlackNotifier(None),
                        event_sink=[].append)
    t = Task.new("touch the UI, boot the dev server against the hermetic backend",
                 repo_path=str(repo_env["work"]))
    t.acceptance_criteria = ["the button renders"]
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.AWAITING_APPROVAL, outcome.detail
    assert len(spawn_calls) == 1, spawn_calls
    env = spawn_calls[0]["kwargs"].get("env") or {}
    assert env.get("VITE_API_TARGET") == "http://127.0.0.1:54321", env
    # Never the manifest's real base_url — that's the whole point of the
    # hermetic backend.
    assert env.get("VITE_API_TARGET") != base_url


async def test_pre_existing_dev_server_is_disclosed(
        repo_env, tmp_path, store, monkeypatch):
    """D2-hermetic bugfix (2026-09-03): when something already answers at the
    manifest's `base_url`, the harness cannot verify that pre-existing
    server proxies at the hermetic backend rather than the operator's live
    `:8420` board — the exact blast radius the hermetic backend exists to
    close. fa053f7da made that case SKIP the walk entirely, but that lost
    real (if not-provably-hermetic) evidence a task used to ship — the
    INTAKE resolution for this bugfix is to RESTORE the walk-and-disclose
    behavior pre-fa053f7da: the walk still runs against whatever is
    listening, it just never spawns or kills anything (that server was
    already there), and the PR body honestly says this walk was not
    hermetic instead of silently rendering as if it were."""
    base_url = "http://127.0.0.1:5299"
    start_cmd = "python -m http.server 5299"

    def mutate(cwd):
        (Path(cwd) / "web" / "App.jsx").write_text(
            "export default function App() { return <div id=\"w\" />; }\n"
        )
        d = Path(cwd) / ".no_human"
        d.mkdir(parents=True, exist_ok=True)
        (d / "ui_evidence.json").write_text(json.dumps(
            {"base_url": base_url, "steps": [{"goto": "/"}, {"shot": "loaded"}]}
        ))

    spawn_calls: list[dict] = []
    kill_calls: list[object] = []
    _wire_fake_dev_server(
        monkeypatch,
        spawn=_spawn_recorder(spawn_calls, _FakeDevServerProc()),
        reachable=lambda url: True,  # already up, at every URL asked about
        kill=_kill_recorder(kill_calls),
    )
    _wire_armed_hermetic_backend(monkeypatch, tmp_path)

    calls: list[dict] = []
    monkeypatch.setattr(orch_mod.ui_evidence, "run", _fake_ui_evidence_run(calls))
    monkeypatch.setattr(orch_mod.ui_evidence, "playwright_available", lambda: True)
    monkeypatch.setattr(GitRepo, "remote_url",
                        lambda self, remote="origin": "https://github.com/acme/widget.git")
    fake_open_pr, opens = _fake_open_pr()
    monkeypatch.setattr(orch_mod, "open_pr", fake_open_pr)

    prof = _profile_with_ui_evidence(repo_env["work"], start_cmd=start_cmd,
                                     base_url=base_url)

    async def fake_usable_profile(self, repo_path):
        return prof

    monkeypatch.setattr(Orchestrator, "_usable_profile", fake_usable_profile)

    cfg = _config(tmp_path)
    orch = Orchestrator(store, cfg.data, FakeBackend(mutate), SlackNotifier(None),
                        event_sink=[].append)
    t = Task.new("touch the UI, server already running", repo_path=str(repo_env["work"]))
    t.acceptance_criteria = ["the button renders"]
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.AWAITING_APPROVAL, outcome.detail
    assert len(calls) == 1, f"a pre-existing server must still be walked " \
        f"(disclosed, not skipped): {calls}"
    assert spawn_calls == [], f"a pre-existing server must never be spawned over: {spawn_calls}"
    assert kill_calls == [], f"a pre-existing server must never be killed: {kill_calls}"

    body = opens[-1]["body"]
    assert "## UI evidence" in body, body
    assert (
        f"Dev server was already running at {base_url} before the walk; "
        "the harness did not start it, did not verify which checkout it "
        "serves, and could not bind it to this walk's hermetic backend "
        "— this walk was not hermetic." in body
    ), body
    assert "booted by the harness" not in body, body


async def test_pre_existing_disclosure_sanitizes_a_coder_controlled_base_url(
        repo_env, tmp_path, store, monkeypatch):
    """`base_url` is the CODER-written manifest's, not the (trusted) profile's
    — `_base_url_problem` only checks scheme + loopback hostname, so a value
    carrying a backtick or an embedded newline in its path still passes
    validation (`urlsplit` ignores those characters for `.hostname`, but the
    raw string keeps them). D2-hermetic bugfix (2026-09-03): a pre-existing
    server is walked again (see `test_pre_existing_dev_server_is_disclosed`)
    — this is the regression test for the "Dev server was already running"
    disclosure sentence's own sanitization, which must sanitize the
    coder-controlled `base_url` exactly like the sibling `booted`/
    `boot-failed` disclosures sanitize their own coder/manifest-controlled
    strings: strip newlines, replace backticks, and cap the length — never
    inject the raw manifest string into the PR body."""
    raw_base_url = "http://127.0.0.1:5299/evil`)\ninjected"
    start_cmd = "python -m http.server 5299"

    def mutate(cwd):
        (Path(cwd) / "web" / "App.jsx").write_text(
            "export default function App() { return <div id=\"w\" />; }\n"
        )
        d = Path(cwd) / ".no_human"
        d.mkdir(parents=True, exist_ok=True)
        (d / "ui_evidence.json").write_text(json.dumps(
            {"base_url": raw_base_url, "steps": [{"goto": "/"}, {"shot": "loaded"}]}
        ))

    spawn_calls: list[dict] = []
    kill_calls: list[object] = []
    _wire_fake_dev_server(
        monkeypatch,
        spawn=_spawn_recorder(spawn_calls, _FakeDevServerProc()),
        reachable=lambda url: True,  # already up, at every URL asked about
        kill=_kill_recorder(kill_calls),
    )
    _wire_armed_hermetic_backend(monkeypatch, tmp_path)

    calls: list[dict] = []
    monkeypatch.setattr(orch_mod.ui_evidence, "run", _fake_ui_evidence_run(calls))
    monkeypatch.setattr(orch_mod.ui_evidence, "playwright_available", lambda: True)
    monkeypatch.setattr(GitRepo, "remote_url",
                        lambda self, remote="origin": "https://github.com/acme/widget.git")
    fake_open_pr, opens = _fake_open_pr()
    monkeypatch.setattr(orch_mod, "open_pr", fake_open_pr)

    prof = _profile_with_ui_evidence(repo_env["work"], start_cmd=start_cmd,
                                     base_url="http://127.0.0.1:5299")

    async def fake_usable_profile(self, repo_path):
        return prof

    monkeypatch.setattr(Orchestrator, "_usable_profile", fake_usable_profile)

    cfg = _config(tmp_path)
    orch = Orchestrator(store, cfg.data, FakeBackend(mutate), SlackNotifier(None),
                        event_sink=[].append)
    t = Task.new("touch the UI, server already running, hostile base_url",
                 repo_path=str(repo_env["work"]))
    t.acceptance_criteria = ["the button renders"]
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.AWAITING_APPROVAL, outcome.detail
    assert len(calls) == 1, f"a pre-existing server must still be walked " \
        f"(disclosed, not skipped): {calls}"
    assert spawn_calls == [], f"a pre-existing server must never be spawned over: {spawn_calls}"
    assert kill_calls == [], f"a pre-existing server must never be killed: {kill_calls}"

    body = opens[-1]["body"]
    assert "## UI evidence" in body, body
    assert "\ninjected" not in body, body
    assert "`)\ninjected" not in body, body
    assert "evil`)" not in body, body
    assert (
        "Dev server was already running at "
        "http://127.0.0.1:5299/evil') injected "
        "before the walk; the harness did not start it, did not verify "
        "which checkout it serves, and could not bind it to this walk's "
        "hermetic backend — this walk was not hermetic." in body
    ), body


async def test_boot_failed_skip_line_names_the_url_not_the_log(
        repo_env, tmp_path, store, monkeypatch):
    """A dev server that never answers before `ready_timeout_s` elapses is a
    boot failure: the walk still runs (and captures nothing useful), and the
    skip line the PR body shows must name the URL/timeout/mode — never the
    dev-server log content or any filesystem path."""
    base_url = "http://127.0.0.1:5399"
    start_cmd = "python -m http.server 5399"
    ready_timeout_s = 5

    def mutate(cwd):
        (Path(cwd) / "web" / "App.jsx").write_text(
            "export default function App() { return <div id=\"q\" />; }\n"
        )
        d = Path(cwd) / ".no_human"
        d.mkdir(parents=True, exist_ok=True)
        (d / "ui_evidence.json").write_text(json.dumps(
            {"base_url": base_url, "steps": [{"goto": "/"}, {"shot": "loaded"}]}
        ))

    spawn_calls: list[dict] = []
    kill_calls: list[object] = []
    written_log: list[bytes] = []
    # A real dev-server log body, naming a filesystem path AND the log's own
    # filename — the positive control for the two "not in body" assertions
    # below: if `_maybe_capture_ui_evidence` ever rendered `srv.detail`
    # instead of `srv.cause`, this text would leak into the PR body and
    # those assertions would fail for real, not vacuously.
    log_bytes = (
        f"Traceback: EADDRINUSE while writing {tmp_path}/dev-server.log\n"
    ).encode()
    # started=0.0, then the loop's first condition check reads 999.0 —
    # `999.0 - 0.0 < 5` is False, so the poll loop never runs at all and the
    # `else:` (timeout-exhausted) branch fires immediately: deterministic,
    # no real waiting.
    _wire_fake_dev_server(
        monkeypatch,
        spawn=_spawn_recorder(spawn_calls, _FakeDevServerProc(),
                              log_bytes=log_bytes, written_log=written_log),
        reachable=lambda url: False,  # never already up, never answers
        kill=_kill_recorder(kill_calls),
        clock=_StepClock([0.0, 999.0]),
    )
    _wire_armed_hermetic_backend(monkeypatch, tmp_path)

    calls: list[dict] = []
    monkeypatch.setattr(
        orch_mod.ui_evidence, "run",
        _fake_ui_evidence_run_empty(calls, reason="page never loaded"))
    monkeypatch.setattr(orch_mod.ui_evidence, "playwright_available", lambda: True)
    monkeypatch.setattr(GitRepo, "remote_url",
                        lambda self, remote="origin": "https://github.com/acme/widget.git")
    fake_open_pr, opens = _fake_open_pr()
    monkeypatch.setattr(orch_mod, "open_pr", fake_open_pr)

    prof = _profile_with_ui_evidence(repo_env["work"], start_cmd=start_cmd,
                                     base_url=base_url,
                                     ready_timeout_s=ready_timeout_s)

    async def fake_usable_profile(self, repo_path):
        return prof

    monkeypatch.setattr(Orchestrator, "_usable_profile", fake_usable_profile)

    cfg = _config(tmp_path)
    orch = Orchestrator(store, cfg.data, FakeBackend(mutate), SlackNotifier(None),
                        event_sink=[].append)
    t = Task.new("touch the UI, dev server never answers", repo_path=str(repo_env["work"]))
    t.acceptance_criteria = ["the button renders"]
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.AWAITING_APPROVAL, outcome.detail
    assert len(calls) == 1, f"the walk still runs even on a boot failure: {calls}"
    assert len(spawn_calls) == 1, spawn_calls
    assert len(kill_calls) == 1, "a still-running process must be killed on teardown"

    # The fake spawn's write must have actually landed on disk — otherwise
    # the "not in body" assertions below would be vacuously true (the
    # forbidden text was never anywhere to leak from in the first place).
    assert written_log and written_log[0] == log_bytes, (
        "the dev-server log write never reached disk — the positive "
        f"control below cannot prove anything: {written_log}"
    )

    body = opens[-1]["body"]
    assert "## UI evidence" in body, body
    assert (
        f"the dev server did not answer at {base_url} within "
        f"{ready_timeout_s}s (boot-failed)" in body
    ), body
    # Never the dev-server log, never a filesystem path — even though this
    # attempt's real log on disk contains both (asserted above).
    assert "dev-server.log" not in body, body
    assert str(tmp_path) not in body, body
    assert "page never loaded" not in body, body
    assert "EADDRINUSE" not in body, body


async def test_boot_failed_skip_line_says_failed_to_start_when_the_spawn_fails(
        repo_env, tmp_path, store, monkeypatch):
    """A dev server that never even becomes a polling process (here: `spawn`
    itself raises `OSError`, e.g. the configured `start_cmd`'s binary is
    missing) is a DIFFERENT boot failure than a timeout — the skip line must
    say so, and must still never leak the log content, the command, or a
    filesystem path."""
    base_url = "http://127.0.0.1:5398"
    start_cmd = "npm run dev"
    ready_timeout_s = 5

    def mutate(cwd):
        (Path(cwd) / "web" / "App.jsx").write_text(
            "export default function App() { return <div id=\"z\" />; }\n"
        )
        d = Path(cwd) / ".no_human"
        d.mkdir(parents=True, exist_ok=True)
        (d / "ui_evidence.json").write_text(json.dumps(
            {"base_url": base_url, "steps": [{"goto": "/"}, {"shot": "loaded"}]}
        ))

    spawn_calls: list[dict] = []
    kill_calls: list[object] = []

    def _spawn_raises(argv, **kwargs):
        spawn_calls.append({"argv": argv, "kwargs": kwargs})
        raise OSError("No such file or directory: 'npm'")

    _wire_fake_dev_server(
        monkeypatch,
        spawn=_spawn_raises,
        reachable=lambda url: False,
        kill=_kill_recorder(kill_calls),
    )
    _wire_armed_hermetic_backend(monkeypatch, tmp_path)

    calls: list[dict] = []
    monkeypatch.setattr(
        orch_mod.ui_evidence, "run",
        _fake_ui_evidence_run_empty(calls, reason="page never loaded"))
    monkeypatch.setattr(orch_mod.ui_evidence, "playwright_available", lambda: True)
    monkeypatch.setattr(GitRepo, "remote_url",
                        lambda self, remote="origin": "https://github.com/acme/widget.git")
    fake_open_pr, opens = _fake_open_pr()
    monkeypatch.setattr(orch_mod, "open_pr", fake_open_pr)

    prof = _profile_with_ui_evidence(repo_env["work"], start_cmd=start_cmd,
                                     base_url=base_url,
                                     ready_timeout_s=ready_timeout_s)

    async def fake_usable_profile(self, repo_path):
        return prof

    monkeypatch.setattr(Orchestrator, "_usable_profile", fake_usable_profile)

    cfg = _config(tmp_path)
    orch = Orchestrator(store, cfg.data, FakeBackend(mutate), SlackNotifier(None),
                        event_sink=[].append)
    t = Task.new("touch the UI, dev server binary is missing",
                 repo_path=str(repo_env["work"]))
    t.acceptance_criteria = ["the button renders"]
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.AWAITING_APPROVAL, outcome.detail
    assert len(calls) == 1, f"the walk still runs even on a boot failure: {calls}"
    assert len(spawn_calls) == 1, spawn_calls
    assert kill_calls == [], "spawn never returned a process, so there is nothing to kill"

    body = opens[-1]["body"]
    assert "## UI evidence" in body, body
    assert (
        f"the dev server failed to start for {base_url} (boot-failed)" in body
    ), body
    assert "did not answer" not in body, body
    assert "npm" not in body, body
    assert "dev-server.log" not in body, body
    assert str(tmp_path) not in body, body
    assert "page never loaded" not in body, body
