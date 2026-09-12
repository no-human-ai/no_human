"""A citation is a `file.py:LINE` reference a doc makes into code; any edit
above that line drifts it. A coder's otherwise-correct change can pass review
with zero blocking findings and only fail later, in TESTING's full-suite run
of the target repo's own `tests/test_readme_claims.py` — a whole attempt
spent on drift the coder could not have known about at review time (task
7a9e7998, attempt 1, 2026-09-09: exactly this).

FIX: `Orchestrator._citation_drift_preflight` runs before review, alongside
`_structural_budget_preflight` (same shape, not modified by this file). It
shells out to the TARGET repo's own `scripts/reanchor_citations.py --apply`
(`src/no_human/testing/citation_drift.py`, also not modified by this file —
this file only exercises its PUBLIC behaviour, never its source text) and
either commits a mechanical re-anchor on THIS attempt's branch, or — when the
script names something it will not guess at — buys ONE bounded corrective
round (`_repro_corrective_round`, reused) before falling through to review.
Never consumes one of the task's `max_attempts`.

Two layers below, both behavioural — no test in this file reads
`citation_drift.py`'s or `orchestrator.py`'s own source text:

  * Layer 1 (module-level): drives `citation_drift.run_reanchor`/
    `should_run` directly against a miniature, self-contained
    `scripts/reanchor_citations.py` + `docs/cite.md` + `pkg/mod.py` fixture
    (real subprocess, real filesystem) — proves AC3's fail-closed modes
    (unreadable file, an erroring subprocess, a citation that does not
    occur exactly once), each observably distinct from a clean run.

  * Layer 2 (integration): the same house pattern as
    `tests/test_structural_budget_preflight.py` — a real bare-repo checkout
    + a scripted backend, driving `orch._run_attempt` directly so the whole
    pipeline (preflight, mechanical fix or corrective round, commit, tamper
    check, review) runs for real except for the LLM call itself. Proves
    AC1 (drift detected before the attempt is graded) and AC2 (fixed or
    named without spending one of the 8 attempts).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from no_human.agent.claude_backend import AgentEvent, AgentResult
from no_human.config import load_config
from no_human.core.infra_breaker import infra_breaker
from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task, TaskStatus
from no_human.notify.slack import SlackNotifier
from no_human.testing import citation_drift
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


# --------------------------------------------------------------------------- #
# Fixture convention: docs/cite.md contains exactly one `mod.py:N` substring, #
# which must equal the 1-based line number of `def foo():` in pkg/mod.py.    #
# A miniature, real, standalone `scripts/reanchor_citations.py` fixture      #
# script enforces it — the exact stdout contract                             #
# (`DRIFT:`/`FAIL:`/`applied N re-anchor(s)`/`VERDICT=OK|FAIL`) is real, own #
# process output, never mocked.                                              #
# --------------------------------------------------------------------------- #

_FIXTURE_REANCHOR_SCRIPT = r'''"""Miniature reanchor_citations.py -- fixture only, not the real script.

Convention: docs/cite.md contains exactly one substring `mod.py:N` that must
equal the 1-based line number of `def foo():` inside pkg/mod.py.

Failure-injection knob, fixture-only: FIXTURE_CRASH=1 in the environment
raises before any output is printed, simulating a crash with no VERDICT
marker -- exactly the shape `citation_drift.classify` cannot name.
"""
from __future__ import annotations

import argparse
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "cite.md"
MOD = ROOT / "pkg" / "mod.py"

CITE_RE = re.compile(r"mod\.py:(\d+)")


def _foo_line():
    lines = MOD.read_text().splitlines()
    for i, line in enumerate(lines, start=1):
        if line.strip().startswith("def foo("):
            return i
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true")
    group.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    if os.environ.get("FIXTURE_CRASH"):
        raise RuntimeError("fixture-injected crash")

    doc_text = DOC.read_text()
    actual = _foo_line()
    if actual is None:
        print("VERDICT=FAIL")
        print("FAIL: cite.md `mod.py:?` — foo() not found")
        return 1

    matches = list(CITE_RE.finditer(doc_text))
    if len(matches) != 1:
        raw = matches[0].group(0) if matches else "mod.py:?"
        print(f"FAIL: cite.md `{raw}` — does not occur exactly once")
        print("VERDICT=FAIL")
        return 1

    cur = int(matches[0].group(1))
    if cur == actual:
        print("VERDICT=OK")
        return 0

    old = f"mod.py:{cur}"
    new = f"mod.py:{actual}"
    verb = "re-anchoring" if args.apply else "would re-anchor"
    print(f"DRIFT: cite.md `{old}` -> `{new}` ({verb})")
    if args.apply:
        DOC.write_text(doc_text.replace(old, new, 1))
        print("applied 1 re-anchor(s)")
        print("VERDICT=OK")
        return 0
    print("VERDICT=FAIL")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
'''

_MOD_BASELINE = "def foo():\n    return 1\n"  # foo() at line 1
_DOC_BASELINE = "See mod.py:1 for foo().\n"  # matches — clean

# Drift: two lines inserted above foo(), so it now lives at line 5.
_MOD_DRIFTED = "def helper():\n    return 0\n\n\ndef foo():\n    return 1\n"

# Ambiguous: the citation string occurs twice — the script must refuse to
# guess which one to rewrite.
_DOC_DUPLICATE = "See mod.py:1 for foo(). Also mod.py:1 again.\n"

# Missing: the citation string occurs zero times.
_DOC_MISSING = "No citation here.\n"


def _write_fixture_layout(root: Path, *, mod_text: str, doc_text: str) -> None:
    (root / "pkg").mkdir(parents=True, exist_ok=True)
    (root / "pkg" / "mod.py").write_text(mod_text)
    (root / "docs").mkdir(parents=True, exist_ok=True)
    (root / "docs" / "cite.md").write_text(doc_text)
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    (root / "scripts" / "reanchor_citations.py").write_text(_FIXTURE_REANCHOR_SCRIPT)
    (root / "tests").mkdir(parents=True, exist_ok=True)
    # Existence only — this fixture's script never imports it; only
    # `citation_drift.convention_present` cares that the file is there.
    (root / "tests" / "test_readme_claims.py").write_text(
        "# placeholder checker — existence only\n"
    )


# --------------------------------------------------------------------------- #
# Layer 1 — `citation_drift` module behaviour, direct: a clean run, a        #
# mechanically fixable drift, and AC3's three fail-closed modes.             #
# --------------------------------------------------------------------------- #


def test_should_run_false_when_convention_absent(tmp_path):
    assert citation_drift.should_run(tmp_path) is False


def test_should_run_true_when_both_halves_present(tmp_path):
    _write_fixture_layout(tmp_path, mod_text=_MOD_BASELINE, doc_text=_DOC_BASELINE)
    assert citation_drift.should_run(tmp_path) is True


def test_inapplicable_when_only_the_script_is_present(tmp_path):
    _write_fixture_layout(tmp_path, mod_text=_MOD_BASELINE, doc_text=_DOC_BASELINE)
    (tmp_path / "tests" / "test_readme_claims.py").unlink()
    outcome = citation_drift.run_reanchor(tmp_path)
    assert outcome.status is citation_drift.Status.INAPPLICABLE
    assert outcome.blocking is False


def test_clean_tree_reports_clean_and_writes_nothing(tmp_path):
    _write_fixture_layout(tmp_path, mod_text=_MOD_BASELINE, doc_text=_DOC_BASELINE)
    before = (tmp_path / "docs" / "cite.md").read_text()
    outcome = citation_drift.run_reanchor(tmp_path)
    assert outcome.status is citation_drift.Status.CLEAN
    assert outcome.blocking is False
    assert (tmp_path / "docs" / "cite.md").read_text() == before


def test_drifted_citation_is_mechanically_reanchored(tmp_path):
    _write_fixture_layout(tmp_path, mod_text=_MOD_DRIFTED, doc_text=_DOC_BASELINE)
    outcome = citation_drift.run_reanchor(tmp_path)
    assert outcome.status is citation_drift.Status.REANCHORED
    assert outcome.blocking is False
    assert outcome.docs == ("docs/cite.md",)
    rewritten = (tmp_path / "docs" / "cite.md").read_text()
    assert "mod.py:5" in rewritten
    assert "mod.py:1" not in rewritten


def test_duplicate_citation_is_unfixable_and_distinguishable_from_clean(tmp_path):
    _write_fixture_layout(tmp_path, mod_text=_MOD_DRIFTED, doc_text=_DOC_DUPLICATE)
    before = (tmp_path / "docs" / "cite.md").read_text()
    outcome = citation_drift.run_reanchor(tmp_path)
    assert outcome.status is citation_drift.Status.UNFIXABLE
    assert outcome.blocking is True
    assert outcome.status is not citation_drift.Status.CLEAN
    assert outcome.failures, outcome
    assert (tmp_path / "docs" / "cite.md").read_text() == before, (
        "an ambiguous citation must never be guessed at"
    )


def test_missing_citation_is_unfixable_and_distinguishable_from_clean(tmp_path):
    _write_fixture_layout(tmp_path, mod_text=_MOD_DRIFTED, doc_text=_DOC_MISSING)
    outcome = citation_drift.run_reanchor(tmp_path)
    assert outcome.status is citation_drift.Status.UNFIXABLE
    assert outcome.blocking is True
    assert outcome.status is not citation_drift.Status.CLEAN


def test_unreadable_file_fails_closed_and_is_distinguishable_from_clean(tmp_path):
    """A real, unreadable file (mode 000) makes the fixture script's own
    `DOC.read_text()` raise `PermissionError`, uncaught — exactly the
    "crash with no VERDICT marker" shape `classify` cannot name and must not
    guess at. Skipped when running as root, where permission bits are not
    enforced (the file would still be readable and the test's premise would
    not hold)."""
    if os.name != "posix":
        pytest.skip("posix permission bits only")
    if os.geteuid() == 0:
        pytest.skip("root ignores file permission bits")
    _write_fixture_layout(tmp_path, mod_text=_MOD_DRIFTED, doc_text=_DOC_BASELINE)
    doc_path = tmp_path / "docs" / "cite.md"
    doc_path.chmod(0o000)
    try:
        outcome = citation_drift.run_reanchor(tmp_path)
    finally:
        doc_path.chmod(0o644)
    assert outcome.status is citation_drift.Status.UNKNOWN
    assert outcome.blocking is True
    assert outcome.status is not citation_drift.Status.CLEAN
    assert outcome.detail


def test_erroring_subprocess_fails_closed_and_is_distinguishable_from_clean(
        tmp_path, monkeypatch):
    """The fixture script's own `FIXTURE_CRASH` knob raises before printing
    anything — a real subprocess that exits non-zero with no `VERDICT=`
    marker on stdout. `subprocess.run` inherits the parent environment by
    default, so setting this in the test process reaches the child."""
    _write_fixture_layout(tmp_path, mod_text=_MOD_BASELINE, doc_text=_DOC_BASELINE)
    monkeypatch.setenv("FIXTURE_CRASH", "1")
    outcome = citation_drift.run_reanchor(tmp_path)
    assert outcome.status is citation_drift.Status.UNKNOWN
    assert outcome.blocking is True
    assert outcome.status is not citation_drift.Status.CLEAN
    assert outcome.detail


def test_subprocess_that_cannot_even_start_fails_closed(tmp_path, monkeypatch):
    """Distinct from the crash above: here the OS itself cannot start the
    process at all (`FileNotFoundError`, an `OSError` subclass) —
    `run_reanchor`'s own `except OSError` branch. Forced behaviourally, by
    monkeypatching the public `reanchor_command` helper to point at a
    nonexistent interpreter — never by reading or asserting on
    `run_reanchor`'s source."""
    _write_fixture_layout(tmp_path, mod_text=_MOD_BASELINE, doc_text=_DOC_BASELINE)
    monkeypatch.setattr(
        citation_drift, "reanchor_command",
        lambda repo_path, *, apply: [
            "/definitely/does/not/exist/no-such-python-xyz",
            str(repo_path / citation_drift.SCRIPT_RELPATH),
            "--apply",
        ],
    )
    outcome = citation_drift.run_reanchor(tmp_path)
    assert outcome.status is citation_drift.Status.UNKNOWN
    assert outcome.blocking is True
    assert outcome.status is not citation_drift.Status.CLEAN
    assert outcome.detail


# --------------------------------------------------------------------------- #
# Layer 2 — integration: `orch._run_attempt` against a real bare-repo        #
# checkout, driven by a scripted backend, mirroring                          #
# `test_structural_budget_preflight.py`'s house pattern.                     #
# --------------------------------------------------------------------------- #


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
    _write_fixture_layout(work, mod_text=_MOD_BASELINE, doc_text=_DOC_BASELINE)
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
    cfg = _config(tmp_path)
    events = []
    orch = Orchestrator(store, cfg.data, backend, SlackNotifier(None),
                         event_sink=events.append)
    task = Task.new("shift helper() above foo()", repo_path=str(bare_repo), kind=kind)
    task.acceptance_criteria = ["helper() exists in pkg/mod.py"]
    await store.create_task(task)
    await store.set_status(task, TaskStatus.CONTEXT)
    await store.set_status(task, TaskStatus.PLANNING)
    repo = GitRepo(bare_repo)
    return orch, task, repo, events


def _is_review_boundary(event: dict) -> bool:
    kind = event.get("kind", "")
    return kind == "review_advisory" or kind.startswith("review_")


class _DriftsThenLeavesItBackend:
    """One turn: shifts `foo()` down by inserting `helper()` above it, and
    commits — a real change, citation left stale. Never touches the doc
    itself; the preflight's own mechanical re-anchor must be what fixes it,
    with no second backend call needed at all."""

    def __init__(self):
        self.calls = 0
        self.prompts = []

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        self.calls += 1
        self.prompts.append(prompt)
        cwd = Path(cwd)
        if on_event is not None:
            on_event(AgentEvent("tool_use", tool_name="Edit",
                                tool_input={"file_path": "pkg/mod.py"}))
        cwd.joinpath("pkg", "mod.py").write_text(_MOD_DRIFTED)
        return AgentResult(final_text="added helper()", num_turns=2, is_error=False,
                           tokens_used=100, session_id="s1", stop_reason="end_turn")


async def test_a_drifted_citation_is_mechanically_reanchored_before_review_no_extra_attempt(
        bare_repo, tmp_path, store):
    """AC1 + AC2: the drift is caught and fixed BEFORE review even runs, and
    the whole attempt count stays at 1 — proven by the preflight's own
    behaviour (event emitted, commit landed, single attempt row), never by
    reading `_citation_drift_preflight`'s source."""
    backend = _DriftsThenLeavesItBackend()
    orch, task, repo, events = await _run_one_task_attempt(store, bare_repo, tmp_path, backend)

    outcome = await orch._run_attempt(task, repo, 1, "main")

    assert outcome.status is TaskStatus.AWAITING_APPROVAL, outcome.detail
    assert backend.calls == 1, "a mechanical re-anchor needs no extra coder turn"

    kinds = [e["kind"] for e in events]
    assert kinds.count("citation_drift") == 1, events
    assert "citation_drift_corrective_round" not in kinds, events

    drift_idx = kinds.index("citation_drift")
    review_idx = next(i for i, e in enumerate(events) if _is_review_boundary(e))
    assert drift_idx < review_idx, events

    committed = subprocess.run(
        ["git", "show", "HEAD:docs/cite.md"], cwd=repo.path,
        check=True, capture_output=True, text=True,
    ).stdout
    assert "mod.py:5" in committed
    assert "mod.py:1" not in committed

    attempts = await store.list_attempts(task.id)
    assert len(attempts) == 1


class _AmbiguousDriftThenFixesItBackend:
    """Turn 1: shifts `foo()` AND makes the citation ambiguous (duplicates
    it) — the script will not guess which occurrence to rewrite, so this
    needs the one bounded corrective round. Turn 2 (the round): rewrites the
    doc by hand to a single, correct citation."""

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
                on_event(AgentEvent("tool_use", tool_name="Edit",
                                    tool_input={"file_path": "docs/cite.md"}))
            cwd.joinpath("pkg", "mod.py").write_text(_MOD_DRIFTED)
            cwd.joinpath("docs", "cite.md").write_text(_DOC_DUPLICATE)
            return AgentResult(final_text="added helper(), touched doc", num_turns=2,
                               is_error=False, tokens_used=100, session_id="s1",
                               stop_reason="end_turn")
        if on_event is not None:
            on_event(AgentEvent("tool_use", tool_name="Edit",
                                tool_input={"file_path": "docs/cite.md"}))
        cwd.joinpath("docs", "cite.md").write_text("See mod.py:5 for foo().\n")
        return AgentResult(final_text="fixed the citation by hand", num_turns=1,
                           is_error=False, tokens_used=10, session_id="s2",
                           stop_reason="end_turn")


async def test_an_unfixable_citation_buys_one_corrective_round_before_review_no_extra_attempt(
        bare_repo, tmp_path, store):
    """AC1 + AC2, the other branch: the script names the drift but refuses
    to guess (ambiguous citation) — one bounded round, before review, still
    only ONE attempt row total."""
    backend = _AmbiguousDriftThenFixesItBackend()
    orch, task, repo, events = await _run_one_task_attempt(store, bare_repo, tmp_path, backend)

    outcome = await orch._run_attempt(task, repo, 1, "main")

    assert outcome.status is TaskStatus.AWAITING_APPROVAL, outcome.detail
    assert backend.calls == 2

    kinds = [e["kind"] for e in events]
    assert kinds.count("citation_drift") >= 1, events
    assert kinds.count("citation_drift_corrective_round") == 1, events

    round_idx = kinds.index("citation_drift_corrective_round")
    review_idx = next(i for i, e in enumerate(events) if _is_review_boundary(e))
    assert round_idx < review_idx, events

    # The corrective round's instruction names the citation the script would
    # not guess at — the coder is never left to re-derive it.
    instruction = backend.prompts[1]
    assert "cite.md" in instruction
    assert "mod.py:1" in instruction

    committed = subprocess.run(
        ["git", "show", "HEAD:docs/cite.md"], cwd=repo.path,
        check=True, capture_output=True, text=True,
    ).stdout
    assert "mod.py:5" in committed

    attempts = await store.list_attempts(task.id)
    assert len(attempts) == 1


async def test_a_re_entered_attempt_does_not_buy_a_second_round(bare_repo, tmp_path, store):
    """Once-per-attempt: a second pass over the SAME attempt id must not
    dispatch a second backend call or emit a second corrective-round event,
    mirroring the sibling preflights' own re-entrance test."""
    backend = _AmbiguousDriftThenFixesItBackend()
    orch, task, repo, events = await _run_one_task_attempt(store, bare_repo, tmp_path, backend)

    outcome = await orch._run_attempt(task, repo, 1, "main")
    assert outcome.status is TaskStatus.AWAITING_APPROVAL, outcome.detail
    assert backend.calls == 2

    attempts = await store.list_attempts(task.id)
    assert len(attempts) == 1
    attempt_id = attempts[0]["id"]

    again = await orch._citation_drift_preflight(
        task, repo, attempt_id=attempt_id, branch="main",
        attempt_n=1, tamper_before=repo.head_sha(),
    )

    assert again is None
    assert backend.calls == 2, "a re-entered attempt must not dispatch a third backend call"
    assert len(
        [e for e in events if e["kind"] == "citation_drift_corrective_round"]
    ) == 1, events


class _TouchesNothingCitedBackend:
    """A change that never comes near the citation convention at all — the
    preflight must stay silent and free."""

    def __init__(self):
        self.calls = 0

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        self.calls += 1
        cwd = Path(cwd)
        if on_event is not None:
            on_event(AgentEvent("tool_use", tool_name="Edit",
                                tool_input={"file_path": "README.md"}))
        cwd.joinpath("README.md").write_text("fixture repo — updated\n")
        return AgentResult(final_text="updated README", num_turns=1, is_error=False,
                           tokens_used=10, session_id="s", stop_reason="end_turn")


async def test_a_clean_tree_emits_no_citation_drift_event(bare_repo, tmp_path, store):
    backend = _TouchesNothingCitedBackend()
    orch, task, repo, events = await _run_one_task_attempt(store, bare_repo, tmp_path, backend)

    outcome = await orch._run_attempt(task, repo, 1, "main")

    assert outcome.status is TaskStatus.AWAITING_APPROVAL, outcome.detail
    assert backend.calls == 1
    kinds = [e["kind"] for e in events]
    assert "citation_drift" not in kinds, events
    assert "citation_drift_corrective_round" not in kinds, events

    attempts = await store.list_attempts(task.id)
    assert len(attempts) == 1
