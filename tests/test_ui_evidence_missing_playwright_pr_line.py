"""Honesty-floor pin: a customer install that never ran `uv sync --group
e2e` has no `playwright` package. Before this fix, `_maybe_capture_ui_evidence`
gated the walk on the diff alone (`ui_evidence_should_run`), tried to run
Playwright, and the resulting empty/failed evidence collapsed to `""` in the
PR body — a diff that qualified for a visual-proof walk read identically to
"nothing UI-shaped changed here." The gate must now say so instead of
staying silent.

Same real-git + fake-backend harness as `tests/test_ui_evidence_attempt_hook.py`
(trimmed to one scenario per test), with one extra monkeypatch:
`orch_mod.ui_evidence.playwright_available` — the actual `playwright` import
is never touched either way, so this suite passes identically whether or not
the dev/CI environment happens to have the `e2e` group installed.
"""
from __future__ import annotations

import contextlib
import json
import subprocess
from pathlib import Path

from no_human.agent.claude_backend import AgentEvent, AgentResult
from no_human.config import load_config
from no_human.core import orchestrator as orch_mod
from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task, TaskStatus
from no_human.notify.slack import SlackNotifier
from no_human.testing import ui_evidence
from no_human.vcs import PrResult
from no_human.vcs.git import GitRepo

import pytest


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


def _fake_ui_evidence_run(calls, shots=("loaded",), video="walk.webm"):
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


def _mutate_with_manifest(cwd):
    (Path(cwd) / "web" / "App.jsx").write_text(
        "export default function App() { return <div id=\"x\" />; }\n"
    )
    d = Path(cwd) / ".no_human"
    d.mkdir(parents=True, exist_ok=True)
    (d / "ui_evidence.json").write_text(json.dumps(
        {"base_url": "http://127.0.0.1:5173", "steps": [{"goto": "/"}, {"shot": "loaded"}]}
    ))


def _mutate_ui_no_manifest(cwd):
    (Path(cwd) / "web" / "App.jsx").write_text(
        "export default function App() { return <div id=\"y\" />; }\n"
    )


def _mutate_non_ui(cwd):
    (Path(cwd) / "calc.py").write_text(
        "def add(a, b):\n    return a + b  # comment only, no UI path touched\n"
    )


def _fake_ui_evidence_run_raises(calls, exc):
    async def fake_run(repo_path, out_dir, **kwargs):
        calls.append({"repo_path": Path(repo_path), "out_dir": Path(out_dir)})
        raise exc

    return fake_run


def _fake_ui_evidence_run_empty(calls, reason):
    async def fake_run(repo_path, out_dir, **kwargs):
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        calls.append({"repo_path": Path(repo_path), "out_dir": out_dir})
        return ui_evidence.UiEvidenceResult(
            verdict="not_run", shots=[], video=None,
            steps_run=0, steps_total=2, reason=reason,
        )

    return fake_run


async def _run_ui_task(repo_env, tmp_path, store, monkeypatch, *, playwright_available,
                        mutate=_mutate_with_manifest, run=None):
    calls: list[dict] = []
    monkeypatch.setattr(
        orch_mod.ui_evidence, "run",
        run(calls) if run is not None else _fake_ui_evidence_run(calls))
    monkeypatch.setattr(
        orch_mod.ui_evidence, "playwright_available", lambda: playwright_available)

    # The hermetic backend (landed after this file) would otherwise try to
    # boot a REAL isolated `nh start` inside the test and skip the walk on
    # its exit-2 (`walk_skip::hermetic_backend_init_failed`) — exactly the
    # failure CI showed on 201baa8. These tests pin the DISCLOSURE paths
    # around `run`, not the backend boot, so stub it armed; the backend's
    # own behavior is pinned by test_ui_evidence_attempt_hook's
    # `_wire_fake_hermetic_backend` with every seam faked.
    @contextlib.asynccontextmanager
    async def _armed_hermetic_backend(out_dir, **_kw):
        yield orch_mod.ui_evidence.HermeticBackend(
            mode="armed", api_target="http://127.0.0.1:1/")
    monkeypatch.setattr(
        orch_mod.ui_evidence, "hermetic_backend", _armed_hermetic_backend)
    monkeypatch.setattr(GitRepo, "remote_url",
                        lambda self, remote="origin": "https://github.com/acme/widget.git")
    fake_open_pr, opens = _fake_open_pr()
    monkeypatch.setattr(orch_mod, "open_pr", fake_open_pr)

    cfg = _config(tmp_path)
    orch = Orchestrator(store, cfg.data, FakeBackend(mutate),
                        SlackNotifier(None), event_sink=[].append)
    t = Task.new("touch the UI", repo_path=str(repo_env["work"]))
    t.acceptance_criteria = ["the button renders"]
    await store.create_task(t)

    outcome = await orch.run_task(t)
    assert outcome.status is TaskStatus.AWAITING_APPROVAL, outcome.detail
    return calls, opens[-1]["body"], t


async def test_missing_playwright_puts_the_skip_line_in_the_pr_body(
        repo_env, tmp_path, store, monkeypatch):
    """(a) The diff qualifies (`web/App.jsx` + a written manifest) but
    playwright isn't installed: the walk never runs, and the PR body carries
    the exact honesty-floor line — not an empty/missing section."""
    calls, body, _task = await _run_ui_task(
        repo_env, tmp_path, store, monkeypatch, playwright_available=False)

    assert calls == [], f"ui_evidence.run() must not be called: {calls}"
    assert "## UI evidence" in body, body
    assert (
        "Visual proof skipped: playwright not installed - run "
        "`nh doctor --fix-walks` to enable"
    ) in body, body


async def test_playwright_present_walk_path_is_unchanged(
        repo_env, tmp_path, store, monkeypatch):
    """(b) Mirror of (a) with playwright available: the walk runs exactly as
    it always has — true here only because `_run_ui_task` stubs
    `playwright_available` directly, which is necessary to isolate this
    scenario but means this test cannot see a bug IN the probe itself. That
    the REAL, unstubbed probe returns the SAME "available" answer whether
    called synchronously or from inside a running `asyncio` loop — the
    actual defect this whole ticket is about — is pinned separately, with
    no stub anywhere in that file, by
    `tests/test_ui_evidence_playwright_probe_parity.py::
    test_probe_agrees_between_sync_and_async_contexts`."""
    calls, body, _task = await _run_ui_task(
        repo_env, tmp_path, store, monkeypatch, playwright_available=True)

    assert len(calls) == 1, f"expected exactly one ui_evidence.run() call: {calls}"
    assert "## UI evidence" in body, body
    assert "Visual proof skipped" not in body, body
    assert "![loaded](" in body, body


async def test_no_manifest_discloses_the_skipped_walk(
        repo_env, tmp_path, store, monkeypatch):
    """AC4: a UI-touching diff with no written manifest used to render an
    empty `""` PR-body section — indistinguishable from "nothing UI-shaped
    changed here." It must now disclose, naming exactly what was missing,
    and `ui_evidence.run` must never be invoked (nothing to run)."""
    calls, body, _task = await _run_ui_task(
        repo_env, tmp_path, store, monkeypatch,
        playwright_available=True, mutate=_mutate_ui_no_manifest)

    assert calls == [], f"no manifest was written; run() must not be called: {calls}"
    assert "## UI evidence" in body, body
    assert (
        "Visual proof skipped: the coder wrote no "
        "`.no_human/ui_evidence.json` walk manifest"
    ) in body, body


async def test_walk_that_raises_discloses_the_exception_class_only(
        repo_env, tmp_path, store, monkeypatch):
    """AC4: `ui_evidence.run` raising used to collapse to `""`. It must now
    disclose the exception CLASS only — `str(exc)` can carry an absolute
    filesystem path or a multi-line traceback, and a PR body is a publish
    surface read by anyone with repo access."""
    exc = RuntimeError("/private/tmp/secret/path boom")
    calls, body, _task = await _run_ui_task(
        repo_env, tmp_path, store, monkeypatch, playwright_available=True,
        run=lambda calls: _fake_ui_evidence_run_raises(calls, exc))

    assert len(calls) == 1
    assert "Visual proof skipped: the walk errored (RuntimeError)" in body, body
    assert "secret/path" not in body, body
    assert "boom" not in body, body
    assert "Traceback" not in body, body
    assert body.count("## UI evidence") == 1, body


async def test_empty_shots_discloses_what_was_lost(
        repo_env, tmp_path, store, monkeypatch):
    """AC4: a walk that ran but produced zero shots (unreachable dev server,
    or — since the probe narrowed to import-only — a package-present/
    binary-missing playwright install) used to collapse to `""`. The
    disclosed reason must be single-line (no newline injection into the
    next PR-body section) and truncated, and the scratch out_dir must still
    be cleaned up on this path."""
    multiline_reason = "line one\nline two with a `backtick` and " + ("x" * 200)
    calls, body, task = await _run_ui_task(
        repo_env, tmp_path, store, monkeypatch, playwright_available=True,
        run=lambda calls: _fake_ui_evidence_run_empty(calls, multiline_reason))

    assert len(calls) == 1
    assert "\n" not in body.split("Visual proof skipped: ", 1)[1].split("\n\n", 1)[0]
    assert "`" not in body.split("## UI evidence", 1)[1].split("\n\n", 1)[0]
    assert body.count("## UI evidence") == 1, body
    # NOTE: `ui_evidence.default_out_dir(task.id)` mints a FRESH directory
    # via `tempfile.mkdtemp` on every call (see its docstring) — calling it
    # again here would create a brand-new, never-cleaned-up dir and always
    # pass regardless of whether the real one was cleaned up. The fake
    # records the ACTUAL `out_dir` the production code used and cleaned up.
    out_dir = calls[0]["out_dir"]
    assert not out_dir.exists(), f"scratch dir must be cleaned up: {out_dir}"


async def test_non_ui_diff_stays_silent(
        repo_env, tmp_path, store, monkeypatch):
    """Negative control: a diff that never touches a UI path fails the gate
    (`ui_evidence_should_run` says no) before any of the disclosure paths
    are even reached — that one exit stays silent (`""`), by design, since
    there is nothing to disclose."""
    calls, body, _task = await _run_ui_task(
        repo_env, tmp_path, store, monkeypatch,
        playwright_available=True, mutate=_mutate_non_ui)

    assert calls == [], f"run() must not be called for a non-UI diff: {calls}"
    assert "## UI evidence" not in body, body
