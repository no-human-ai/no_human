"""0.4: a cleanup that partly fails must record what it left behind instead
of discarding the fact silently (``shutil.rmtree(..., ignore_errors=True)``
used to do exactly that), and both `eval/harness.py` sandbox-cleanup sites
must behave identically — neither fixed in isolation."""
from __future__ import annotations

import inspect
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from no_human.core.orchestrator import Orchestrator
from no_human.eval import harness
from no_human.eval.harness import CLEANUP_MARKER, _remove_sandbox


def _tiny_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (["init", "-b", "main"],
                 ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    (repo / "f.txt").write_text("x")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, capture_output=True)
    return repo


def _backend_factory(_golden):
    return None


class _QuickOutcome:
    class status:
        value = "completed"


async def _quick_run_task(self, task):
    return _QuickOutcome()


@pytest.mark.asyncio
async def test_cleanup_that_cannot_remove_everything_records_what_is_left(tmp_path):
    if os.name != "posix":
        pytest.skip("chmod-based permission test needs POSIX")
    if hasattr(os, "getuid") and os.getuid() == 0:
        pytest.skip("root ignores directory permission bits")

    base = tmp_path / "sbx"
    sub = base / "sub"
    sub.mkdir(parents=True)
    victim = sub / "f.txt"
    victim.write_text("x")
    os.chmod(sub, 0o500)  # r-x: unlinking a file inside sub now fails
    try:
        result = _remove_sandbox(base)

        assert result, "a survivor should have been reported, not swallowed"
        assert any("f.txt" in item for item in result), result

        marker = base / CLEANUP_MARKER
        assert marker.exists(), "an incomplete cleanup must leave a marker"
        marker_text = marker.read_text()
        assert any(item in marker_text for item in result)
    finally:
        os.chmod(sub, 0o700)
        shutil.rmtree(base, ignore_errors=True)


@pytest.mark.asyncio
async def test_cleanup_never_raises_and_never_masks_the_propagating_error(tmp_path):
    base = tmp_path / "sbx"
    base.mkdir()
    (base / "f.txt").write_text("x")

    async def _boom():
        try:
            raise RuntimeError("original")
        finally:
            _remove_sandbox(base)

    with pytest.raises(RuntimeError, match="original"):
        await _boom()
    assert not base.exists(), "a fully-removable sandbox should still be gone"

    # A sandbox that never existed (e.g. removed already) is success, not an
    # error to raise or a failure to report.
    assert _remove_sandbox(Path("/nonexistent/nh-shadow-does-not-exist")) == []


@pytest.mark.asyncio
async def test_a_fully_successful_cleanup_records_nothing(tmp_path):
    base = tmp_path / "sbx"
    (base / "sub").mkdir(parents=True)
    (base / "sub" / "f.txt").write_text("x")

    result = _remove_sandbox(base)

    assert result == []
    assert not base.exists()
    assert not (tmp_path / (base.name + ".cleanup-incomplete")).exists()


@pytest.mark.asyncio
async def test_both_harness_sandbox_sites_use_the_shared_removal(tmp_path, monkeypatch):
    calls: list[Path] = []

    def _fake_remove(base_tmp, on_event=None):
        calls.append(Path(base_tmp))
        return []

    monkeypatch.setattr(harness, "_remove_sandbox", _fake_remove)

    await harness.run_eval({}, backend_factory=_backend_factory, golden_tasks=[])
    assert len(calls) == 1
    assert calls[0].name.startswith("nh-eval-"), calls

    repo = _tiny_repo(tmp_path)
    monkeypatch.setattr(Orchestrator, "run_task", _quick_run_task)
    await harness.run_shadow(
        {}, repo_path=str(repo), task_title="t", backend=object(),
    )
    assert len(calls) == 2
    assert calls[1].name.startswith("nh-shadow-"), calls

    src = inspect.getsource(harness)
    assert "ignore_errors=True" not in src, (
        "both sandbox cleanup sites must go through the shared, "
        "failure-recording removal, not the old discard-and-forget rmtree"
    )
    assert src.count("shutil.rmtree(") == 1, (
        "shutil.rmtree must be called from exactly one place — _remove_sandbox"
    )


@pytest.mark.asyncio
async def test_a_caller_supplied_workdir_is_never_removed(tmp_path, monkeypatch):
    eval_workdir = tmp_path / "eval_wd"
    eval_workdir.mkdir()
    await harness.run_eval(
        {}, backend_factory=_backend_factory, golden_tasks=[], workdir=eval_workdir,
    )
    assert eval_workdir.exists()

    repo = _tiny_repo(tmp_path)
    shadow_workdir = tmp_path / "shadow_wd"
    shadow_workdir.mkdir()
    monkeypatch.setattr(Orchestrator, "run_task", _quick_run_task)
    await harness.run_shadow(
        {}, repo_path=str(repo), task_title="t", backend=object(),
        workdir=shadow_workdir,
    )
    assert shadow_workdir.exists()
