"""A diff that shifts a doc citation (`docs/security.md`/`docs/eval.md`/
`docs/KNOWN_ISSUES.md` plus its `tests/test_readme_claims.py` CITATION_TABLE
row) still passes review — the reviewer is not this gate — and only fails
later, in TESTING's full-suite run, on the citation tests themselves. That is
a whole extra attempt spent discovering what the target repo's own
`scripts/reanchor_citations.py --apply` fixes mechanically in seconds
(2026-09-09: task 7a9e7998 attempt 1, task 302012e3 round 2).

FIX: `Orchestrator._citation_drift_preflight` runs immediately after
`_structural_budget_preflight` and before the draft PR / review. Zero LLM
spend, and no subprocess call either, unless this attempt's diff actually
touched a file some citation points at (`no_human.testing.citations`'s
`cited_source_files`/`touched_cited`, both fail-open `ast`-only reads, same
doctrine as `structural_budget.py`). Only then does it shell out to the
script's own read-only `--check` and classify the result
(`citations.run_check`): clean -> nothing to do; drift-only -> ONE bounded
corrective round (`_repro_corrective_round`, reused, with its own
`event_kind`/`scope_note`/`scope_filter` overrides) instructing the coder to
run `--apply` itself and commit, on the SAME branch, before review;
unfixable or a script/runtime error -> the attempt fails immediately, no
round spent — an unfixable citation or a broken checker will not be fixed by
throwing tokens at it.

Same house pattern as `tests/test_structural_budget_preflight.py` (not
modified by this file, its own tests unaffected by anything here): a real
bare-repo checkout + a scripted backend, driving `orch._run_attempt` directly
so the whole pipeline — preflight, corrective round, commit, tamper check —
runs for real except for the LLM call itself. The fixture repo ships its own
miniature `scripts/reanchor_citations.py` and `tests/test_readme_claims.py`
(with a `CITATION_TABLE` citing `pkg/mod.py`), proving the mechanism
repo-agnostically without touching this repo's own citation tooling.

A separate, non-integration section below exercises `no_human.testing.
citations` directly (unit tests, no git/backend), and a pure-function section
exercises the `_repro_corrective_round` scope overrides and the message
builder.
"""

from __future__ import annotations

import inspect
import subprocess
import sys
from pathlib import Path

import pytest

from no_human.agent.claude_backend import AgentEvent, AgentResult
from no_human.config import load_config
from no_human.core.infra_breaker import infra_breaker
from no_human.core.orchestrator import (
    Orchestrator,
    _CITATION_ROUND_SCOPE_NOTE,
    _citation_round_out_of_scope,
    citation_send_back_message,
)
from no_human.core.task import Task, TaskStatus
from no_human.notify.slack import SlackNotifier
from no_human.testing import citations
from no_human.vcs import GitRepo


@pytest.fixture(autouse=True)
def _clean_infra_breaker_singleton():
    """The breaker is a process-wide singleton; reset it around every test in
    this file so one test's infra failures can never leak into the next
    one's assertions — copied from `test_structural_budget_preflight.py`."""
    infra_breaker().reset()
    yield
    infra_breaker().reset()


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


# The fixture repo's own product file — `def foo():` is what the fixture
# citation below cites, by line.
_MOD_BASELINE = "def foo():\n    return 1\n"
_CITED_LINE = 1  # "def foo():" is line 1 in the baseline.

# Three unrelated lines inserted above `def foo():` — the drift-causing edit.
# Content is untouched; only its line number moved.
_MOD_SHIFTED = "# header\n# more header\n# even more header\ndef foo():\n    return 1\n"
_SHIFTED_LINE = 4

_DOC_BASELINE = f"# Security\n\nSee `pkg/mod.py:{_CITED_LINE}` for details.\n"

_TABLE_BASELINE = (
    '"""Miniature CITATION_TABLE fixture -- mirrors '
    "tests/test_readme_claims.py's own shape, just the one column this "
    'module and the fixture script both need: doc, raw citation text, '
    'resolve path, token."""\n\n'
    "CITATION_TABLE = (\n"
    f'    ("security.md", "pkg/mod.py:{_CITED_LINE}", "pkg/mod.py", "def foo"),\n'
    ")\n"
)

# A self-contained, standalone reanchor script — NOT the real
# scripts/reanchor_citations.py, and it does not import anything from this
# fixture repo's own tests/test_readme_claims.py. It only needs to speak the
# real script's grammar (`DRIFT: `/`FAIL: `/`VERDICT=` lines, `--check`
# read-only, `--apply` writes) since that grammar — not this script's
# internals — is all `no_human.testing.citations.run_check` ever parses.
# Every invocation (both `--check` and `--apply`) appends one line to
# `_reanchor_calls.log`, at the repo root, so a test can tell the harness's
# own `--check`-only calls apart from the coder's one `--apply` call.
_REANCHOR_SCRIPT = '''#!/usr/bin/env python3
"""Miniature reanchor_citations.py — fixture only, real script's grammar."""
from __future__ import annotations

import argparse
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MOD = REPO / "pkg" / "mod.py"
DOC = REPO / "docs" / "security.md"
TABLE = REPO / "tests" / "test_readme_claims.py"
LOG = REPO / "_reanchor_calls.log"
CRASH_MARKER = REPO / "_crash_marker"
UNFIXABLE_MARKER = REPO / "_unfixable_marker"


def _actual_line():
    for i, line in enumerate(MOD.read_text().splitlines(), start=1):
        if line.strip() == "def foo():":
            return i
    return None


def _cited_line():
    m = re.search(r"pkg/mod\\.py:(\\d+)", DOC.read_text())
    return int(m.group(1)) if m else None


def main(argv=None):
    ap = argparse.ArgumentParser()
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)

    with LOG.open("a", encoding="utf-8") as f:
        f.write(("--apply" if args.apply else "--check") + "\\n")

    if CRASH_MARKER.exists():
        raise RuntimeError("simulated checker crash")

    if UNFIXABLE_MARKER.exists():
        print("FAIL: security.md `pkg/mod.py:%d` -- citation content not found"
              % (_cited_line() or 0))
        print("VERDICT=FAIL")
        return 1

    actual = _actual_line()
    cited = _cited_line()
    if actual is None or cited is None or actual == cited:
        print("VERDICT=OK")
        return 0

    old_raw = "pkg/mod.py:%d" % cited
    new_raw = "pkg/mod.py:%d" % actual
    verb = "re-anchoring" if args.apply else "would re-anchor"
    print("DRIFT: security.md `%s` -> `%s` (%s)" % (old_raw, new_raw, verb))

    if not args.apply:
        print("VERDICT=FAIL")
        return 1

    DOC.write_text(DOC.read_text().replace("`%s`" % old_raw, "`%s`" % new_raw))
    TABLE.write_text(TABLE.read_text().replace('"%s"' % old_raw, '"%s"' % new_raw))
    print("applied 1 re-anchor(s)")
    print("VERDICT=OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


@pytest.fixture
def bare_repo(tmp_path):
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True,
                   capture_output=True)
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.email", "u@e.com")
    _git(work, "config", "user.name", "u")
    (work / "pkg").mkdir()
    (work / "pkg" / "mod.py").write_text(_MOD_BASELINE)
    (work / "docs").mkdir()
    (work / "docs" / "security.md").write_text(_DOC_BASELINE)
    (work / "tests").mkdir()
    (work / "tests" / "test_readme_claims.py").write_text(_TABLE_BASELINE)
    (work / "scripts").mkdir()
    (work / "scripts" / "reanchor_citations.py").write_text(_REANCHOR_SCRIPT)
    (work / "README.md").write_text("fixture repo\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "init")
    _git(work, "remote", "add", "origin", str(bare))
    _git(work, "push", "-u", "origin", "main")
    return work


def _config(tmp_path):
    cfg = load_config(tmp_path / "config.yaml")
    cfg.data.setdefault("planning", {})["enabled"] = False
    cfg.data.setdefault("reviewer", {})["allow_advisory"] = True
    cfg.data.setdefault("blockers", {})["challenge"] = False
    return cfg


async def _run_one_task_attempt(store, bare_repo, tmp_path, backend, *, kind="feature"):
    """Walk a fresh task to PLANNING and hand back everything a test needs to
    drive `_run_attempt` directly — copied from
    `test_structural_budget_preflight.py`'s helper of the same name.
    `kind="feature"` by default: this gate does not care about `task.kind`,
    and staying off the repro-gate's enforced path keeps these fixtures
    focused on the citation-drift mechanism alone.
    """
    cfg = _config(tmp_path)
    events = []
    orch = Orchestrator(store, cfg.data, backend, SlackNotifier(None),
                        event_sink=events.append)
    task = Task.new("shift foo()", repo_path=str(bare_repo), kind=kind)
    task.acceptance_criteria = ["foo() still exists in pkg/mod.py"]
    await store.create_task(task)
    await store.set_status(task, TaskStatus.CONTEXT)
    await store.set_status(task, TaskStatus.PLANNING)
    repo = GitRepo(bare_repo)
    return orch, task, repo, events


def _is_review_boundary(event: dict) -> bool:
    """Any event that means the reviewer itself actually ran (or was
    explicitly skipped in its place) — copied verbatim from
    `test_structural_budget_preflight.py`."""
    kind = event.get("kind", "")
    return kind == "review_advisory" or kind.startswith("review_")


# --------------------------------------------------------------------------- #
# AC1 (RED-first) — a diff that shifts a cited line buys ONE corrective       #
# round, before review, on the SAME branch/attempt.                          #
# --------------------------------------------------------------------------- #


class _ShiftsCitedLineThenReanchorsBackend:
    """Turn 1: inserts three unrelated lines above `def foo():` in
    `pkg/mod.py` and commits — a real change, content untouched, only its
    line number moved. Turn 2 (the corrective round): runs the fixture's own
    `scripts/reanchor_citations.py --apply` for real, via subprocess — the
    same command the round's instruction names — exactly like a real coder
    would, never the harness itself."""

    def __init__(self):
        self.calls = 0
        self.prompts = []

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        self.calls += 1
        self.prompts.append(prompt)
        cwd = Path(cwd)
        if self.calls == 1:
            if on_event is not None:
                on_event(AgentEvent("tool_use", tool_name="Edit",
                                    tool_input={"file_path": "pkg/mod.py"}))
            cwd.joinpath("pkg", "mod.py").write_text(_MOD_SHIFTED)
            return AgentResult(final_text="added header lines above foo()",
                               num_turns=2, is_error=False, tokens_used=100,
                               session_id="s1", stop_reason="end_turn")
        subprocess.run(
            [sys.executable, str(cwd / "scripts" / "reanchor_citations.py"), "--apply"],
            cwd=cwd, check=True, capture_output=True, text=True,
        )
        if on_event is not None:
            on_event(AgentEvent("tool_use", tool_name="Edit",
                                tool_input={"file_path": "docs/security.md"}))
            on_event(AgentEvent("tool_use", tool_name="Edit",
                                tool_input={"file_path": "tests/test_readme_claims.py"}))
        return AgentResult(final_text="ran reanchor_citations.py --apply",
                           num_turns=1, is_error=False, tokens_used=10,
                           session_id="s2", stop_reason="end_turn")


async def test_a_diff_that_shifts_a_cited_line_buys_one_corrective_round_before_review(
        bare_repo, tmp_path, store):
    """RED before the fix: today nothing runs the citation script before
    review, so this diff — content untouched, only `def foo():`'s line
    number moved — would sail through review and only fail later, in
    TESTING's full-suite run of the citation tests, burning a whole extra
    attempt on a one-shot `--apply` re-anchor (2026-09-09: task 7a9e7998
    attempt 1, task 302012e3 round 2)."""
    backend = _ShiftsCitedLineThenReanchorsBackend()
    orch, task, repo, events = await _run_one_task_attempt(store, bare_repo, tmp_path, backend)

    outcome = await orch._run_attempt(task, repo, 1, "main")

    assert outcome.status is TaskStatus.AWAITING_APPROVAL, outcome.detail
    assert backend.calls == 2

    kinds = [e["kind"] for e in events]
    assert kinds.count("citation_drift") == 1, events
    assert kinds.count("citation_drift_corrective_round") == 1, events

    drift_idx = kinds.index("citation_drift")
    round_idx = kinds.index("citation_drift_corrective_round")
    review_idx = next(i for i, e in enumerate(events) if _is_review_boundary(e))

    # Both fire, and both fire strictly before review starts (or is skipped).
    assert drift_idx < review_idx, events
    assert round_idx < review_idx, events
    assert drift_idx < round_idx, events

    # The corrective round's instruction names the script and the exact
    # command — boilerplate only, per the intake resolution: no drift
    # specifics are embedded in the prompt.
    instruction = backend.prompts[1]
    assert "scripts/reanchor_citations.py" in instruction
    assert "--apply" in instruction

    # The re-anchor really landed, committed, on THIS attempt's branch — not
    # a second attempt, not a dangling uncommitted edit.
    committed_doc = subprocess.run(
        ["git", "show", "HEAD:docs/security.md"], cwd=repo.path,
        check=True, capture_output=True, text=True,
    ).stdout
    assert f"pkg/mod.py:{_SHIFTED_LINE}" in committed_doc

    committed_table = subprocess.run(
        ["git", "show", "HEAD:tests/test_readme_claims.py"], cwd=repo.path,
        check=True, capture_output=True, text=True,
    ).stdout
    assert f'"pkg/mod.py:{_SHIFTED_LINE}"' in committed_table

    attempts = await store.list_attempts(task.id)
    assert len(attempts) == 1


async def test_a_re_entered_attempt_does_not_buy_a_second_round(bare_repo, tmp_path, store):
    """Once-per-attempt: a second pass over the SAME attempt id (e.g. a
    resumed attempt re-entering the loop head) must not buy a second round
    even if reached again — mirrors
    `test_structural_budget_preflight.py::test_a_re_entered_attempt_does_not_buy_a_second_round`.
    """
    backend = _ShiftsCitedLineThenReanchorsBackend()
    orch, task, repo, events = await _run_one_task_attempt(store, bare_repo, tmp_path, backend)

    outcome = await orch._run_attempt(task, repo, 1, "main")
    assert outcome.status is TaskStatus.AWAITING_APPROVAL, outcome.detail
    assert backend.calls == 2

    attempts = await store.list_attempts(task.id)
    assert len(attempts) == 1
    attempt_id = attempts[0]["id"]

    again = await orch._citation_drift_preflight(
        task, repo, base="main", attempt_id=attempt_id, branch="main",
        attempt_n=1, tamper_before=repo.head_sha(),
    )

    assert again is None
    assert backend.calls == 2, "a re-entered attempt must not dispatch a third backend call"
    assert len(
        [e for e in events if e["kind"] == "citation_drift_corrective_round"]
    ) == 1, events


# --------------------------------------------------------------------------- #
# AC2 — no touched cited file: zero subprocess/LLM spend for the check.       #
# --------------------------------------------------------------------------- #


class _TouchesOnlyReadmeBackend:
    """The only turn: edits `README.md`, which no citation points at."""

    def __init__(self):
        self.calls = 0

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        self.calls += 1
        cwd = Path(cwd)
        if on_event is not None:
            on_event(AgentEvent("tool_use", tool_name="Edit",
                                tool_input={"file_path": "README.md"}))
        cwd.joinpath("README.md").write_text("fixture repo, updated\n")
        return AgentResult(final_text="updated README", num_turns=1, is_error=False,
                           tokens_used=10, session_id="s1", stop_reason="end_turn")


async def test_a_diff_touching_no_cited_file_makes_no_subprocess_and_no_llm_call(
        bare_repo, tmp_path, store, monkeypatch):
    """Pinned by patching `citations.run_check` itself: if the diff never
    touches a cited file, the preflight must return before ever calling it —
    not call it and discard the result. `backend.calls == 1` pins the "no
    extra LLM call" half of AC2 the same way."""
    calls = []
    monkeypatch.setattr(
        citations, "run_check",
        lambda *a, **k: calls.append((a, k)) or citations.CitationCheck(0, "", [], [], None),
    )
    backend = _TouchesOnlyReadmeBackend()
    orch, task, repo, events = await _run_one_task_attempt(store, bare_repo, tmp_path, backend)

    outcome = await orch._run_attempt(task, repo, 1, "main")

    assert outcome.status is TaskStatus.AWAITING_APPROVAL, outcome.detail
    assert backend.calls == 1
    assert calls == [], "run_check must not be called when no cited file was touched"

    kinds = [e["kind"] for e in events]
    assert "citation_drift" not in kinds
    assert "citation_drift_corrective_round" not in kinds


# --------------------------------------------------------------------------- #
# AC2 — unfixable drift, or a script/runtime error: fail immediately, no     #
# round spent.                                                                #
# --------------------------------------------------------------------------- #


class _ShiftsCitedLineButUnfixableBackend:
    """The only turn: shifts `def foo():` AND drops an `_unfixable_marker`
    file the fixture script treats as "citation content not found" — the
    unfixable case, distinct from a plain drift."""

    def __init__(self):
        self.calls = 0

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        self.calls += 1
        cwd = Path(cwd)
        if on_event is not None:
            on_event(AgentEvent("tool_use", tool_name="Edit",
                                tool_input={"file_path": "pkg/mod.py"}))
        cwd.joinpath("pkg", "mod.py").write_text(_MOD_SHIFTED)
        cwd.joinpath("_unfixable_marker").write_text("x")
        return AgentResult(final_text="shifted foo(), citation now unfixable",
                           num_turns=2, is_error=False, tokens_used=100,
                           session_id="s1", stop_reason="end_turn")


async def test_an_unfixable_drift_fails_the_attempt_without_spending_a_round(
        bare_repo, tmp_path, store):
    backend = _ShiftsCitedLineButUnfixableBackend()
    orch, task, repo, events = await _run_one_task_attempt(store, bare_repo, tmp_path, backend)

    outcome = await orch._run_attempt(task, repo, 1, "main")

    assert outcome.status is TaskStatus.FAILED
    assert "citation content not found" in outcome.detail
    assert backend.calls == 1

    kinds = [e["kind"] for e in events]
    assert "citation_drift_corrective_round" not in kinds

    attempts = await store.list_attempts(task.id)
    assert attempts[0]["status"] == "failed"
    assert attempts[0]["failure_reason"] == outcome.detail


class _ShiftsCitedLineButCrashesBackend:
    """The only turn: shifts `def foo():` AND drops a `_crash_marker` file
    the fixture script treats as "the checker itself is broken" — raises
    before printing any `DRIFT: `/`FAIL: ` line, so `citations.run_check`
    must classify it as `error`, never silently as clean or fixable."""

    def __init__(self):
        self.calls = 0

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        self.calls += 1
        cwd = Path(cwd)
        if on_event is not None:
            on_event(AgentEvent("tool_use", tool_name="Edit",
                                tool_input={"file_path": "pkg/mod.py"}))
        cwd.joinpath("pkg", "mod.py").write_text(_MOD_SHIFTED)
        cwd.joinpath("_crash_marker").write_text("x")
        return AgentResult(final_text="shifted foo(), checker now broken",
                           num_turns=2, is_error=False, tokens_used=100,
                           session_id="s1", stop_reason="end_turn")


async def test_a_script_error_fails_the_attempt_with_the_error_text(
        bare_repo, tmp_path, store):
    """Intake Q4: a runtime/script error is NOT auto-fixable drift — fail
    immediately with the error text, no round spent."""
    backend = _ShiftsCitedLineButCrashesBackend()
    orch, task, repo, events = await _run_one_task_attempt(store, bare_repo, tmp_path, backend)

    outcome = await orch._run_attempt(task, repo, 1, "main")

    assert outcome.status is TaskStatus.FAILED
    assert "simulated checker crash" in outcome.detail
    assert backend.calls == 1

    kinds = [e["kind"] for e in events]
    assert "citation_drift_corrective_round" not in kinds


# --------------------------------------------------------------------------- #
# AC3 — the harness never runs `--apply` itself; the round is the coder's,   #
# mirroring `_structural_budget_preflight`.                                  #
# --------------------------------------------------------------------------- #


def test_the_preflight_cites_the_structural_budget_precedent():
    src = inspect.getsource(Orchestrator._citation_drift_preflight)
    assert "_structural_budget_preflight" in src


async def test_the_harness_never_invokes_apply(bare_repo, tmp_path, store):
    """Static + dynamic: `_citation_drift_preflight`'s own source never
    spells `--apply` (it only ever runs `--check`), and across a whole drift
    -> corrective-round -> re-check attempt, the fixture script's own call
    log shows exactly one `--apply` invocation — the coder's, inside the
    round's backend turn — and every other invocation is the harness's own
    `--check`."""
    src = inspect.getsource(Orchestrator._citation_drift_preflight)
    assert "--apply" not in src

    backend = _ShiftsCitedLineThenReanchorsBackend()
    orch, task, repo, events = await _run_one_task_attempt(store, bare_repo, tmp_path, backend)

    outcome = await orch._run_attempt(task, repo, 1, "main")
    assert outcome.status is TaskStatus.AWAITING_APPROVAL, outcome.detail

    log_lines = (bare_repo / "_reanchor_calls.log").read_text().splitlines()
    assert log_lines.count("--apply") == 1, log_lines
    assert log_lines.count("--check") == len(log_lines) - 1, log_lines


# --------------------------------------------------------------------------- #
# Pure-function units — no git, no backend.                                   #
# --------------------------------------------------------------------------- #


def test_citation_round_out_of_scope_allows_docs_md_and_the_table_but_not_src():
    assert _citation_round_out_of_scope([
        "docs/security.md", "docs/eval.md", "tests/test_readme_claims.py",
    ]) == []
    assert _citation_round_out_of_scope([
        "src/no_human/core/orchestrator.py", "docs/security.txt", "README.md",
    ]) == ["README.md", "docs/security.txt", "src/no_human/core/orchestrator.py"]


def test_citation_send_back_message_names_the_script_and_carries_no_drift_details():
    msg = citation_send_back_message()
    assert citations.SCRIPT_RELPATH in msg
    assert "--apply" in msg
    assert "DRIFT:" not in msg
    assert "FAIL:" not in msg


def test_repro_corrective_round_defaults_are_unchanged():
    sig = inspect.signature(Orchestrator._repro_corrective_round)
    assert sig.parameters["scope_note"].default is None
    assert sig.parameters["scope_filter"].default is None


# --------------------------------------------------------------------------- #
# `no_human.testing.citations` units — pure/fail-open, no git, no backend.    #
# --------------------------------------------------------------------------- #


def _write_table(root: Path, table_body: str) -> None:
    (root / "tests").mkdir(parents=True, exist_ok=True)
    (root / "tests" / "test_readme_claims.py").write_text(
        '"""fixture"""\n\nCITATION_TABLE = (\n' + table_body + "\n)\n"
    )


def test_cited_source_files_reads_the_citation_table(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text("x = 1\n")
    _write_table(tmp_path, '    ("security.md", "pkg/mod.py:1", "pkg/mod.py", "x"),')
    assert citations.cited_source_files(tmp_path) == {"pkg/mod.py"}


def test_cited_source_files_with_no_table_file_is_empty(tmp_path):
    assert citations.cited_source_files(tmp_path) == set()


def test_cited_source_files_with_an_unparseable_table_is_empty(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_readme_claims.py").write_text("def broken(:\n")
    assert citations.cited_source_files(tmp_path) == set()


def test_script_path_is_none_when_absent(tmp_path):
    assert citations.script_path(tmp_path) is None
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "reanchor_citations.py").write_text("# x\n")
    assert citations.script_path(tmp_path) == tmp_path / "scripts" / "reanchor_citations.py"


def test_touched_cited_is_the_sorted_intersection():
    cited = {"pkg/a.py", "pkg/b.py", "pkg/c.py"}
    changed = ["pkg/b.py", "pkg/z.py", "pkg/a.py"]
    assert citations.touched_cited(cited, changed) == ["pkg/a.py", "pkg/b.py"]
    assert citations.touched_cited(cited, ["pkg/z.py"]) == []


def _write_check_script(root: Path, body: str) -> None:
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    (root / "scripts" / "reanchor_citations.py").write_text(body)


def test_run_check_classifies_ok_drift_unfixable_and_error(tmp_path):
    _write_check_script(tmp_path, "print('VERDICT=OK')\nraise SystemExit(0)\n")
    ok = citations.run_check(tmp_path)
    assert ok.returncode == 0
    assert not ok.drifts and not ok.unfixable and ok.error is None

    _write_check_script(tmp_path, (
        "print('DRIFT: doc.md `a.py:1` -> `a.py:2` (would re-anchor)')\n"
        "print('VERDICT=FAIL')\n"
        "raise SystemExit(1)\n"
    ))
    drift = citations.run_check(tmp_path)
    assert drift.returncode == 1
    assert drift.drifts and not drift.unfixable and drift.error is None

    _write_check_script(tmp_path, (
        "print('FAIL: doc.md `a.py:1` -- not found')\n"
        "print('VERDICT=FAIL')\n"
        "raise SystemExit(1)\n"
    ))
    unfixable = citations.run_check(tmp_path)
    assert unfixable.returncode == 1
    assert unfixable.unfixable and not unfixable.drifts and unfixable.error is None

    _write_check_script(tmp_path, "raise RuntimeError('boom')\n")
    error = citations.run_check(tmp_path)
    assert error.error is not None
    assert not error.drifts and not error.unfixable
