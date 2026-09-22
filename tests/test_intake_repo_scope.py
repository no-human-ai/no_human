"""Intake grill probes with repo-scoped file discovery, not whole-filesystem
scans (PLAN.md: "Intake grill probes with whole-filesystem find instead of
repo-scoped commands").

Two prior attempts at this task shipped a guard that could be bypassed:
  1. `rg` was grouped with grep-family and only counted as recursive with an
     explicit `-r`/`-R` flag — but `rg` recurses into directories with no
     flag at all, so `rg TODO /` slipped through.
  2. For grep-family commands the first positional token was always consumed
     as "the pattern", even when the pattern was actually supplied via `-e`
     — so `grep -r -e TODO /` lost `/` to a phantom pattern slot and left no
     path operand for the guard to inspect.
Both cases get an explicit regression test below (`test_rg_root_scan_is_
blocked_with_no_flag` and `test_grep_pattern_via_flag_does_not_hide_the_
path_operand`).

Hermetic: fake backends only, mirroring `tests/test_intake_grill.py`'s
`_ScriptedBackend` and `tests/test_grill.py`'s `FakeBackend` patterns. No
existing test is touched — this file is purely additive.
"""

from __future__ import annotations

import subprocess

import pytest

from no_human.agent import guard

FORBIDDEN: list[str] = []
PROTECTED: list[str] = []


def _ev(cmd: str, cwd: str | None = None):
    return guard.evaluate("Bash", {"command": cmd}, forbidden_paths=FORBIDDEN,
                          never_push_to=PROTECTED, cwd=cwd)


# --------------------------------------------------------------------------- #
# AC 2 / AC 4a — filesystem-wide scans are rejected                          #
# --------------------------------------------------------------------------- #

def test_find_root_is_blocked():
    d = _ev("find / -name '*.py'")
    assert not d.allow
    assert "must be repo-scoped" in d.reason


def test_grep_recursive_root_is_blocked():
    d = _ev('grep -r "TODO" /')
    assert not d.allow
    assert "must be repo-scoped" in d.reason


def test_ls_recursive_root_is_blocked():
    d = _ev("ls -R /")
    assert not d.allow
    assert "must be repo-scoped" in d.reason


def test_system_dir_scans_are_blocked():
    assert not _ev("find /Users -name x").allow
    assert not _ev("grep -rn foo /etc").allow
    # bundled short-flag form
    d = _ev("grep -rn foo /")
    assert not d.allow
    assert "must be repo-scoped" in d.reason


def test_root_scan_blocked_inside_a_compound_command():
    d = _ev("cd /tmp && find / -name '*.py'")
    assert not d.allow
    assert "must be repo-scoped" in d.reason
    d2 = _ev("echo hi | find / -name '*.py'")
    assert not d2.allow
    assert "must be repo-scoped" in d2.reason


def test_pathless_recursive_scan_denied_when_cwd_unknown():
    d = guard.evaluate("Bash", {"command": "grep -r foo"},
                       forbidden_paths=FORBIDDEN, never_push_to=PROTECTED,
                       cwd=None)
    assert not d.allow
    assert "must be repo-scoped" in d.reason


def test_rg_root_scan_is_blocked_with_no_flag():
    """Regression: `rg` recurses into a directory target with NO flag at
    all — grouping it with grep's flag-gated recursion test (an earlier
    attempt at this task did) is a bypass."""
    d = _ev("rg TODO /")
    assert not d.allow
    assert "must be repo-scoped" in d.reason


def test_grep_pattern_via_flag_does_not_hide_the_path_operand():
    """Regression: the search pattern supplied via `-e` must not be confused
    with the path operand that follows it."""
    d = _ev("grep -r -e TODO /")
    assert not d.allow
    assert "must be repo-scoped" in d.reason


# --------------------------------------------------------------------------- #
# AC 4b — repo-scoped commands are allowed (false-positive controls)         #
# --------------------------------------------------------------------------- #

def test_repo_scoped_find_is_allowed(tmp_path):
    d = _ev(f"find {tmp_path}/ -name '*.py'", cwd=str(tmp_path))
    assert d.allow


def test_repo_scoped_grep_is_allowed(tmp_path):
    d = _ev(f'grep -r "def evaluate" {tmp_path}/', cwd=str(tmp_path))
    assert d.allow


def test_relative_and_pathless_scans_are_allowed_with_a_cwd(tmp_path):
    assert _ev("find . -name '*.py'", cwd=str(tmp_path)).allow
    assert _ev("grep -rn foo", cwd=str(tmp_path)).allow
    assert _ev("ls -R src", cwd=str(tmp_path)).allow


def test_tempdir_scans_are_allowed():
    assert _ev("find /tmp -name '*.py'").allow
    assert _ev("grep -r x /tmp/foo").allow


def test_non_scanning_commands_untouched():
    assert _ev("git status").allow
    assert _ev("pytest -q").allow
    assert _ev("cat /etc/hosts").allow


def test_repo_scoped_command_actually_returns_results(tmp_path):
    """The guard verdict alone doesn't prove the rewritten command WORKS —
    build a tiny repo, let the guard allow the repo-scoped find, then
    actually run it and check it names a real file."""
    for i in range(3):
        (tmp_path / f"mod{i}.py").write_text(f"# module {i}\n")
    (tmp_path / "README.md").write_text("hello\n")

    cmd = f"find {tmp_path}/ -name '*.py'"
    d = _ev(cmd, cwd=str(tmp_path))
    assert d.allow

    result = subprocess.run(cmd, shell=True, cwd=str(tmp_path),
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0
    assert "mod0.py" in result.stdout


# --------------------------------------------------------------------------- #
# AC 1 — prompts instruct repo-relative discovery, with the real cwd         #
# --------------------------------------------------------------------------- #

class _ScriptedGrillBackend:
    """Captures prompts/cwds; returns a canned `done` block."""

    def __init__(self, final_text: str):
        self._text = final_text
        self.prompts: list[str] = []
        self.cwds: list = []

    async def run(self, prompt, *, cwd=None, **kwargs):
        self.prompts.append(prompt)
        self.cwds.append(cwd)

        class _R:
            final_text = self._text
        return _R()


_DONE_BLOCK = (
    '```json\n{"type": "done", "title": "T", "description": "D", '
    '"acceptance_criteria": ["AC1"]}\n```'
)


@pytest.mark.asyncio
async def test_grill_prompt_instructs_repo_relative_discovery(tmp_path):
    from no_human.intake.grill import grill_step

    be = _ScriptedGrillBackend(_DONE_BLOCK)
    await grill_step("t", "d", str(tmp_path), [], be)

    resolved = str(tmp_path.resolve())
    assert f"find {resolved}/" in be.prompts[0]
    assert "grep -r" in be.prompts[0]
    assert "{repo_root}" not in be.prompts[0]
    assert be.cwds == [tmp_path.resolve()]


@pytest.mark.asyncio
async def test_grill_answers_prompt_instructs_repo_relative_discovery(tmp_path):
    from no_human.intake.evaluator import GrillQA, grill_spec

    answers_block = (
        "GRILL_ANSWERS_START\n"
        '{"answers": [{"i": 0, "answer": "src/app.py:1", '
        '"source": "repo-evidence"}]}\n'
        "GRILL_ANSWERS_END"
    )
    be = _ScriptedGrillBackend(answers_block)
    questions = [GrillQA(question="Which file?", decision_it_changes="target")]
    qa = await grill_spec("t", "d", ["c1"], tmp_path, backend=be,
                          questions=questions)

    assert qa is not None
    assert f"find {tmp_path}/" in be.prompts[0]
    assert "grep -r" in be.prompts[0]
    assert be.cwds == [tmp_path]


@pytest.mark.asyncio
async def test_grill_step_without_repo_path_does_not_explore_home():
    import tempfile
    from pathlib import Path

    from no_human.intake.grill import grill_step

    be = _ScriptedGrillBackend(_DONE_BLOCK)
    await grill_step("t", "d", None, [], be)

    assert be.cwds == [Path(tempfile.gettempdir())]
    assert be.cwds[0] != Path.home()


# --------------------------------------------------------------------------- #
# AC 3 + 4c — fast exploration on a small repo, with a non-vacuity control   #
# --------------------------------------------------------------------------- #

class _ExploringBackend:
    """Simulates one exploration turn: runs candidate commands through the
    real guard (denying the root scan, executing the repo-scoped rewrite),
    then returns a valid `done` block — never sleeps, so the whole step
    should complete quickly."""

    def __init__(self, repo: str):
        self._repo = repo

    async def run(self, prompt, *, cwd=None, **kwargs):
        candidates = ["find / -name '*.py'", f"find {self._repo}/ -name '*.py'"]
        for cmd in candidates:
            d = guard.evaluate("Bash", {"command": cmd}, forbidden_paths=[],
                               never_push_to=[], cwd=str(cwd) if cwd else None)
            if d.allow:
                subprocess.run(cmd, shell=True, cwd=str(cwd) if cwd else None,
                               capture_output=True, text=True, timeout=10)

        class _R:
            final_text = _DONE_BLOCK
        return _R()


@pytest.mark.asyncio
async def test_small_repo_exploration_completes_without_the_timeout_fallback(
    tmp_path, monkeypatch
):
    from no_human.intake import grill as grill_mod
    from no_human.intake.grill import GrillResult, grill_step

    for i in range(8):
        (tmp_path / f"f{i}.py").write_text(f"# {i}\n")

    # Spy on the actual timeout `grill_step` opens its `wait_for` with, so
    # this test proves the fast backend beat the REAL exploration budget
    # (grill_step's own `timeout` default) rather than merely completing
    # within some wall-clock literal chosen for this test — a load-independent
    # substitute for measuring elapsed time.
    opened_timeouts: list[float] = []
    original_wait_for = grill_mod.asyncio.wait_for

    async def _recording_wait_for(fut, timeout=None, **kwargs):
        opened_timeouts.append(timeout)
        return await original_wait_for(fut, timeout=timeout, **kwargs)

    monkeypatch.setattr(grill_mod.asyncio, "wait_for", _recording_wait_for)

    be = _ExploringBackend(str(tmp_path))
    result = await grill_step("t", "d", str(tmp_path), [], be)

    assert isinstance(result, GrillResult)
    text = getattr(result, "title", "") + getattr(result, "description", "")
    assert "codebase exploration took too long" not in text
    # Non-vacuity: confirms the run went through the same `wait_for` bound
    # production callers get (grill_step's `timeout` default), not some other,
    # unrelated code path that could never hit the fallback at all.
    import inspect

    default_timeout = inspect.signature(grill_step).parameters["timeout"].default
    assert opened_timeouts == [default_timeout]


@pytest.mark.asyncio
async def test_the_timeout_fallback_still_fires_when_the_session_hangs():
    """Non-vacuity control: without this, the fast test above would pass
    even if the timeout fallback were deleted entirely."""
    import asyncio

    from no_human.intake.grill import GrillQuestion, grill_step

    class _SlowBackend:
        async def run(self, prompt, *, cwd=None, **kwargs):
            await asyncio.sleep(999)

    result = await grill_step("t", "d", None, [], _SlowBackend(), timeout=0.05)
    assert isinstance(result, GrillQuestion)
    assert "codebase exploration took too long" in result.question.lower()
