"""A diff that trips the target repo's OWN `tests/test_structural_budget.py`
still passes review — the reviewer is not this gate — and only fails later,
in TESTING's full-suite run. That is a whole extra attempt spent discovering
what is mechanically a one-line budget fix: a frozen entry GREW (three tasks,
2026-09-03: bf645f3a, c5ae50d8, c5b24230), a brand-NEW offender crossed a
`MAX_*` limit the guard has never frozen before, or a frozen entry has gone
STALE (shrank below its frozen value, or its file vanished).

FIX: `Orchestrator._structural_budget_preflight` runs after the repro gate and
before the draft PR / review. Zero LLM spend for the check itself
(`structural_budget.frozen_paths`/`scanned_root` are each one `ast.parse` of
the guard file, `touched_frozen`/`touches_scanned_root` one set-or-prefix
comparison against this attempt's changed files — all fail-open to
`set()`/`None`/`[]`). `touched_frozen` alone only ever catches a GROWN entry
(a new offender is in no `FROZEN_*` dict yet; a stale entry's own path may not
even be among the changed files) — `touches_scanned_root` closes that gap by
firing whenever the diff changes ANY `.py` file under the guard's own scanned
root, and then the WHOLE guard file runs (not just the growth node id), so a
new-offender or stale-entry failure is caught by the guard's own other tests
too. Only if the guard fails does this buy ONE bounded round
(`_repro_corrective_round`, reused, with its own `event_kind` so a
budget-cause round is not mis-attributed as a repro-gate one) to fix it, on
the SAME branch, before review.

Same house pattern as `tests/test_declared_repro_files_committed_preflight.py`
(not modified by this file): a real bare-repo checkout + a scripted backend,
driving `orch._run_attempt` directly so the whole pipeline — gate, corrective
round, commit, tamper check — runs for real except for the LLM call itself.
The fixture repo ships its own miniature structural-budget guard so the
mechanism is proven repo-agnostically, without touching this repo's own
`tests/test_structural_budget.py`.

A separate, non-integration section below (AC1) reproduces the stale-bytecode
race `structural_budget.invalidate_guard_cache` exists to close: a same-second,
same-length rewrite of the guard file can be served from a stale compile of
itself to the very pytest re-run that rewrote it — driven directly with
`subprocess`/`os.utime`, deterministically, not by racing the wall clock.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from no_human.agent.claude_backend import AgentEvent, AgentResult
from no_human.config import load_config
from no_human.core.infra_breaker import infra_breaker
from no_human.core.orchestrator import Orchestrator, structural_budget_send_back_message
from no_human.core.task import Task, TaskStatus
from no_human.notify.slack import SlackNotifier
from no_human.testing import runner as runner_mod
from no_human.testing import structural_budget
from no_human.vcs import GitRepo


@pytest.fixture(autouse=True)
def _clean_infra_breaker_singleton():
    """The breaker is a process-wide singleton; reset it around every test in
    this file so one test's infra failures can never leak into the next
    one's assertions — copied from `test_repro_waived_corrective_round.py`."""
    infra_breaker().reset()
    yield
    infra_breaker().reset()


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


# The fixture repo's own product file — frozen by its miniature guard below.
_MOD_BASELINE = "def foo():\n    return 1\n"
_MOD_BASELINE_LINES = len(_MOD_BASELINE.splitlines())  # 2

# Grown past the frozen budget — what the coder's first turn writes.
_MOD_GROWN = "def foo():\n    return 1\n\n\ndef bar():\n    return 2\n"
_MOD_GROWN_LINES = len(_MOD_GROWN.splitlines())  # 6

# A brand-new file, never frozen, over the fixture guard's own
# MAX_FILE_LINES — the NEW-offender blind spot `touched_frozen` cannot see.
_EXTRA_OVER_LIMIT = "def a():\n    return 1\n\n\ndef b():\n    return 2\n"
_EXTRA_OVER_LIMIT_LINES = len(_EXTRA_OVER_LIMIT.splitlines())  # 6

# Shrunk below the frozen budget — a STALE entry, the opposite-direction
# blind spot `bounded_growth_command`'s growth-only test cannot see.
_MOD_SHRUNK = "x = 1\n"
_MOD_SHRUNK_LINES = len(_MOD_SHRUNK.splitlines())  # 1


# Threshold used by the fixture guard's own new-offender check below —
# arbitrary, small on purpose: `mod.py`'s frozen sizes (2/6/1) never cross it
# on their own since `mod.py` is always a FROZEN entry (excluded from that
# check by construction); only a deliberately-added, never-frozen file is
# meant to cross it.
_MAX_FILE_LINES = 4


def _guard_text(frozen: dict[str, int], *, max_file_lines: int = _MAX_FILE_LINES,
                 src_dir: str = "pkg") -> str:
    """A self-contained structural-budget guard, deliberately much smaller
    than this repo's own `tests/test_structural_budget.py` (no `scan_tree`/
    CC machinery needed) — `structural_budget.frozen_paths`/`scanned_root`
    only need the `SRC = REPO_ROOT / "pkg"`-shaped assignment and a
    `FROZEN_*` dict literal to read it correctly. Three tests, one per AC2
    failure mode: a frozen entry GREW, a file over *max_file_lines* was
    never frozen (a NEW offender), or a frozen entry no longer matches
    reality — shrank below its frozen value, or its file vanished (STALE).

    *src_dir* defaults to `"pkg"` (every existing caller's fixture layout,
    unchanged) but may be a `/`-joined relative path such as `"tests/pkg"` —
    used by the AC1 measure-then-edit fixtures below so the scanned product
    file itself sits under a `tests/` path component and is therefore IN
    SCOPE for `_repro_corrective_round`'s own scope guard, letting a second,
    same-round edit to it survive to commit instead of being discarded.
    """
    frozen_repr = ",\n".join(f'    "{rel}": {lines}' for rel, lines in frozen.items())
    src_expr = " / ".join(f'"{part}"' for part in src_dir.split("/"))
    return f'''"""Miniature structural-budget guard — fixture only."""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / {src_expr}

MAX_FILE_LINES = {max_file_lines}

# Frozen today's known size for each entry — this budget only ratchets down.
FROZEN_FILE_LINES = {{
{frozen_repr},
}}


def test_no_frozen_entry_has_grown():
    grown = []
    for rel, frozen in FROZEN_FILE_LINES.items():
        path = SRC / rel
        if not path.exists():
            continue
        current = len(path.read_text().splitlines())
        if current > frozen:
            grown.append(
                f"{{rel}}: frozen {{frozen}}, now {{current}} "
                f"(+{{current - frozen}}); this budget only ratchets down"
            )
    assert not grown, "\\n".join(grown)


def test_no_new_offender_over_the_limit():
    offenders = []
    for path in sorted(SRC.glob("*.py")):
        rel = path.name
        if rel in FROZEN_FILE_LINES:
            continue
        current = len(path.read_text().splitlines())
        if current > MAX_FILE_LINES:
            offenders.append(
                f"{{rel}}: {{current}} lines, over MAX_FILE_LINES="
                f"{{MAX_FILE_LINES}} and not yet frozen"
            )
    assert not offenders, "\\n".join(offenders)


def test_no_frozen_entry_is_stale():
    stale = []
    for rel, frozen in FROZEN_FILE_LINES.items():
        path = SRC / rel
        if not path.exists():
            stale.append(f"{{rel}}: frozen entry's file no longer exists")
            continue
        current = len(path.read_text().splitlines())
        if current < frozen:
            stale.append(
                f"{{rel}}: frozen {{frozen}}, now {{current}}; stale entry, "
                "delete or shrink it"
            )
    assert not stale, "\\n".join(stale)
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
    (work / "tests").mkdir()
    (work / "tests" / "test_structural_budget.py").write_text(
        _guard_text({"mod.py": _MOD_BASELINE_LINES})
    )
    (work / "README.md").write_text("fixture repo\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "init")
    _git(work, "remote", "add", "origin", str(bare))
    _git(work, "push", "-u", "origin", "main")
    return work


@pytest.fixture
def bare_repo_relocated_product(tmp_path):
    """Same shape as `bare_repo`, except the scanned product file lives at
    `tests/pkg/mod.py` instead of `pkg/mod.py` — a path with a `tests` path
    component, so `_repro_corrective_round`'s own scope guard
    (`_repro_round_out_of_scope`, not modified by this file) treats a
    second, same-round edit to it as IN SCOPE. Used only by the AC1
    measure-then-edit fixtures below, where the corrective round's coder
    needs to legitimately touch the scanned file itself a second time
    without that edit — and the guard's own re-anchor alongside it — being
    discarded by the blanket out-of-scope revert."""
    bare = tmp_path / "remote2.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True,
                   capture_output=True)
    work = tmp_path / "work2"
    work.mkdir()
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.email", "u@e.com")
    _git(work, "config", "user.name", "u")
    (work / "tests" / "pkg").mkdir(parents=True)
    (work / "tests" / "pkg" / "mod.py").write_text(_MOD_BASELINE)
    (work / "tests" / "test_structural_budget.py").write_text(
        _guard_text({"mod.py": _MOD_BASELINE_LINES}, src_dir="tests/pkg")
    )
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
    drive `_run_attempt` directly, mirroring
    `test_declared_repro_files_committed_preflight.py`'s
    `_run_one_bugfix_attempt`. `kind="feature"` by default: this gate does
    not care about `task.kind` (unlike the repro gate's `enforced` check),
    and staying off the repro-gate's enforced path keeps these fixtures
    focused on the structural-budget mechanism alone.
    """
    cfg = _config(tmp_path)
    events = []
    orch = Orchestrator(store, cfg.data, backend, SlackNotifier(None),
                        event_sink=events.append)
    task = Task.new("grow bar()", repo_path=str(bare_repo), kind=kind)
    task.acceptance_criteria = ["bar() exists in pkg/mod.py"]
    await store.create_task(task)
    await store.set_status(task, TaskStatus.CONTEXT)
    await store.set_status(task, TaskStatus.PLANNING)
    repo = GitRepo(bare_repo)
    return orch, task, repo, events


def _is_review_boundary(event: dict) -> bool:
    """Any event that means the reviewer itself actually ran (or was
    explicitly skipped in its place) — the boundary the AC requires
    `structural_budget_grown` / `repro_corrective_round` to land strictly
    before. Deliberately NOT the `state: "reviewing"` stage-label event: that
    fires right after the coder's own commit, before the repro gate or this
    preflight ever run — it announces the upcoming stage, not that the
    reviewer agent has actually been invoked."""
    kind = event.get("kind", "")
    return kind == "review_advisory" or kind.startswith("review_")


# --------------------------------------------------------------------------- #
# AC1 (RED-first) — a diff that grows a frozen entry buys ONE corrective      #
# round, before review, on the SAME branch/attempt.                          #
# --------------------------------------------------------------------------- #


class _GrowsFrozenFileThenReanchorsBackend:
    """Turn 1: grows `pkg/mod.py` past its frozen budget and commits — a real
    change, no test, no manifest. Turn 2 (the structural-budget corrective
    round): re-anchors `FROZEN_FILE_LINES["mod.py"]` in the fixture's own
    `tests/test_structural_budget.py` to the new, grown value."""

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
            cwd.joinpath("pkg", "mod.py").write_text(_MOD_GROWN)
            return AgentResult(final_text="added bar()", num_turns=2, is_error=False,
                               tokens_used=100, session_id="s1", stop_reason="end_turn")
        if on_event is not None:
            on_event(AgentEvent("tool_use", tool_name="Edit",
                                tool_input={"file_path": "tests/test_structural_budget.py"}))
        cwd.joinpath("tests", "test_structural_budget.py").write_text(
            _guard_text({"mod.py": _MOD_GROWN_LINES})
        )
        return AgentResult(final_text="re-anchored mod.py's frozen line count",
                           num_turns=1, is_error=False, tokens_used=10,
                           session_id="s2", stop_reason="end_turn")


async def test_a_frozen_file_growth_buys_one_corrective_round_before_review(
        bare_repo, tmp_path, store):
    """RED before the fix: today nothing runs the guard's own growth test
    before review, so this diff would sail through review and only fail much
    later, in TESTING's full-suite run — burning a whole extra attempt on a
    one-line budget re-anchor."""
    backend = _GrowsFrozenFileThenReanchorsBackend()
    orch, task, repo, events = await _run_one_task_attempt(store, bare_repo, tmp_path, backend)

    outcome = await orch._run_attempt(task, repo, 1, "main")

    assert outcome.status is TaskStatus.AWAITING_APPROVAL, outcome.detail
    assert backend.calls == 2

    kinds = [e["kind"] for e in events]
    assert kinds.count("structural_budget_grown") == 1, events
    assert kinds.count("structural_budget_corrective_round") == 1, events

    grown_idx = kinds.index("structural_budget_grown")
    round_idx = kinds.index("structural_budget_corrective_round")
    review_idx = next(i for i, e in enumerate(events) if _is_review_boundary(e))

    # Both fire, and both fire strictly before review starts (or is skipped).
    assert grown_idx < review_idx, events
    assert round_idx < review_idx, events
    assert grown_idx < round_idx, events

    # The corrective round's instruction carries the guard's own growth line
    # and names the guard file — the coder is never left to re-derive either.
    instruction = backend.prompts[1]
    assert "tests/test_structural_budget.py" in instruction
    assert f"frozen {_MOD_BASELINE_LINES}, now {_MOD_GROWN_LINES}" in instruction
    assert "mod.py" in instruction

    # The re-anchor really landed, committed, on THIS attempt's branch — not
    # a second attempt, not a dangling uncommitted edit.
    committed = subprocess.run(
        ["git", "show", "HEAD:tests/test_structural_budget.py"], cwd=repo.path,
        check=True, capture_output=True, text=True,
    ).stdout
    assert f'"mod.py": {_MOD_GROWN_LINES},' in committed

    attempts = await store.list_attempts(task.id)
    assert len(attempts) == 1


async def test_a_re_entered_attempt_does_not_buy_a_second_round(bare_repo, tmp_path, store):
    """Once-per-attempt: a second pass over the SAME attempt id (e.g. a
    resumed attempt re-entering the loop head) must not buy a second round
    even if reached again."""
    backend = _GrowsFrozenFileThenReanchorsBackend()
    orch, task, repo, events = await _run_one_task_attempt(store, bare_repo, tmp_path, backend)

    outcome = await orch._run_attempt(task, repo, 1, "main")
    assert outcome.status is TaskStatus.AWAITING_APPROVAL, outcome.detail
    assert backend.calls == 2

    attempts = await store.list_attempts(task.id)
    assert len(attempts) == 1
    attempt_id = attempts[0]["id"]

    again = await orch._structural_budget_preflight(
        task, repo, base="main", attempt_id=attempt_id, branch="main",
        attempt_n=1, tamper_before=repo.head_sha(),
    )

    assert again is None
    assert backend.calls == 2, "a re-entered attempt must not dispatch a third backend call"
    assert len(
        [e for e in events if e["kind"] == "structural_budget_corrective_round"]
    ) == 1, events


# --------------------------------------------------------------------------- #
# AC2 — a brand-new offender (never yet frozen) crossing MAX_FILE_LINES buys  #
# the same one corrective round, before review.                              #
# --------------------------------------------------------------------------- #


class _AddsNewOffenderThenFreezesItBackend:
    """Turn 1: adds a brand-new file, `pkg/extra.py`, over the fixture
    guard's own `MAX_FILE_LINES` — a file `FROZEN_FILE_LINES` has never
    heard of, so `touched_frozen`'s frozen-path intersection is structurally
    blind to it; only `touches_scanned_root` (any `.py` file under the
    guard's scanned root) can see it. Turn 2 (the corrective round): freezes
    it, budget for budget, in the fixture's own
    `tests/test_structural_budget.py`."""

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
                                    tool_input={"file_path": "pkg/extra.py"}))
            cwd.joinpath("pkg", "extra.py").write_text(_EXTRA_OVER_LIMIT)
            return AgentResult(final_text="added extra.py", num_turns=2, is_error=False,
                               tokens_used=100, session_id="s1", stop_reason="end_turn")
        if on_event is not None:
            on_event(AgentEvent("tool_use", tool_name="Edit",
                                tool_input={"file_path": "tests/test_structural_budget.py"}))
        cwd.joinpath("tests", "test_structural_budget.py").write_text(
            _guard_text({
                "mod.py": _MOD_BASELINE_LINES,
                "extra.py": _EXTRA_OVER_LIMIT_LINES,
            })
        )
        return AgentResult(final_text="froze extra.py's line count",
                           num_turns=1, is_error=False, tokens_used=10,
                           session_id="s2", stop_reason="end_turn")


async def test_a_new_file_over_the_limit_buys_a_corrective_round_before_review(
        bare_repo, tmp_path, store):
    """RED before the AC2 widening: `touched_frozen` alone never fires here —
    `pkg/extra.py` is in no `FROZEN_*` dict yet — so today this diff sails
    through review and only fails later, in TESTING's full-suite run."""
    backend = _AddsNewOffenderThenFreezesItBackend()
    orch, task, repo, events = await _run_one_task_attempt(store, bare_repo, tmp_path, backend)

    outcome = await orch._run_attempt(task, repo, 1, "main")

    assert outcome.status is TaskStatus.AWAITING_APPROVAL, outcome.detail
    assert backend.calls == 2

    kinds = [e["kind"] for e in events]
    assert kinds.count("structural_budget_grown") == 1, events
    assert kinds.count("structural_budget_corrective_round") == 1, events

    grown_idx = kinds.index("structural_budget_grown")
    round_idx = kinds.index("structural_budget_corrective_round")
    review_idx = next(i for i, e in enumerate(events) if _is_review_boundary(e))
    assert grown_idx < review_idx, events
    assert round_idx < review_idx, events

    instruction = backend.prompts[1]
    assert "tests/test_structural_budget.py" in instruction
    assert "extra.py" in instruction

    committed = subprocess.run(
        ["git", "show", "HEAD:tests/test_structural_budget.py"], cwd=repo.path,
        check=True, capture_output=True, text=True,
    ).stdout
    assert f'"extra.py": {_EXTRA_OVER_LIMIT_LINES},' in committed

    attempts = await store.list_attempts(task.id)
    assert len(attempts) == 1


# --------------------------------------------------------------------------- #
# AC2 — a frozen entry that no longer matches reality (shrank below its      #
# frozen value) buys the same one corrective round, before review.           #
# --------------------------------------------------------------------------- #


class _ShrinksFrozenFileThenReanchorsBackend:
    """Turn 1: shrinks `pkg/mod.py` below its frozen value — a STALE entry,
    the opposite-direction blind spot to a grown one: `touched_frozen`
    already fires here (`pkg/mod.py` IS a frozen path and IS touched), but
    today's `bounded_growth_command` only re-runs `test_no_frozen_entry_has_
    grown` (`current > frozen`), which trivially PASSES on a shrink; only
    running the guard's WHOLE file reaches `test_no_frozen_entry_is_stale`
    (`current < frozen`). Turn 2 (the corrective round): re-anchors the
    frozen value down to match."""

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
            cwd.joinpath("pkg", "mod.py").write_text(_MOD_SHRUNK)
            return AgentResult(final_text="shrank mod.py", num_turns=2, is_error=False,
                               tokens_used=100, session_id="s1", stop_reason="end_turn")
        if on_event is not None:
            on_event(AgentEvent("tool_use", tool_name="Edit",
                                tool_input={"file_path": "tests/test_structural_budget.py"}))
        cwd.joinpath("tests", "test_structural_budget.py").write_text(
            _guard_text({"mod.py": _MOD_SHRUNK_LINES})
        )
        return AgentResult(final_text="re-anchored mod.py's frozen line count down",
                           num_turns=1, is_error=False, tokens_used=10,
                           session_id="s2", stop_reason="end_turn")


async def test_a_frozen_entry_that_shrank_below_threshold_buys_a_corrective_round(
        bare_repo, tmp_path, store):
    """RED before the AC2 widening: `touched_frozen` already fires (`mod.py`
    is a frozen path and is touched) — but the OLD bounded command only ran
    the growth-only test, which passes trivially on a shrink; only running
    the whole guard file reaches the stale-entry test."""
    backend = _ShrinksFrozenFileThenReanchorsBackend()
    orch, task, repo, events = await _run_one_task_attempt(store, bare_repo, tmp_path, backend)

    outcome = await orch._run_attempt(task, repo, 1, "main")

    assert outcome.status is TaskStatus.AWAITING_APPROVAL, outcome.detail
    assert backend.calls == 2

    kinds = [e["kind"] for e in events]
    assert kinds.count("structural_budget_grown") == 1, events
    assert kinds.count("structural_budget_corrective_round") == 1, events

    grown_idx = kinds.index("structural_budget_grown")
    round_idx = kinds.index("structural_budget_corrective_round")
    review_idx = next(i for i, e in enumerate(events) if _is_review_boundary(e))
    assert grown_idx < review_idx, events
    assert round_idx < review_idx, events

    committed = subprocess.run(
        ["git", "show", "HEAD:tests/test_structural_budget.py"], cwd=repo.path,
        check=True, capture_output=True, text=True,
    ).stdout
    assert f'"mod.py": {_MOD_SHRUNK_LINES},' in committed

    attempts = await store.list_attempts(task.id)
    assert len(attempts) == 1


# --------------------------------------------------------------------------- #
# AC3 — the structural-budget corrective round emits its OWN event kind      #
# (and a `cause`), never the generic repro-gate one, so telemetry never      #
# mis-attributes a budget-cause round to the repro gate.                     #
# --------------------------------------------------------------------------- #


async def test_the_budget_corrective_round_emits_its_own_event_kind(
        bare_repo, tmp_path, store):
    """RED before the fix: `_structural_budget_preflight` called
    `_repro_corrective_round` with no `event_kind`/`cause` override, so
    every budget-cause round emitted the generic `repro_corrective_round`
    event — indistinguishable, in `_recall_failures`/telemetry, from an
    actual repro-gate round."""
    backend = _GrowsFrozenFileThenReanchorsBackend()
    orch, task, repo, events = await _run_one_task_attempt(store, bare_repo, tmp_path, backend)

    outcome = await orch._run_attempt(task, repo, 1, "main")
    assert outcome.status is TaskStatus.AWAITING_APPROVAL, outcome.detail

    generic = [e for e in events if e["kind"] == "repro_corrective_round"]
    assert not generic, events

    budget_rounds = [e for e in events if e["kind"] == "structural_budget_corrective_round"]
    assert len(budget_rounds) == 1, events
    assert budget_rounds[0].get("cause") == "structural_budget", budget_rounds[0]


# --------------------------------------------------------------------------- #
# AC1 (measure-then-edit) — a corrective round that re-freezes a frozen      #
# entry, then makes a FURTHER edit in the SAME round that changes the        #
# measured quantity again, must still commit a value matching the tree AS    #
# COMMITTED — not the value it happened to measure mid-round. Bugfix for     #
# "budget preflight fires up to 5 times per task and still dies at review"   #
# (dogfood case 92e48491a7: app.py frozen at 6196 but measured 6198;         #
# scheduler.py frozen at 3177 but measured 3180).                            #
# --------------------------------------------------------------------------- #


# One more growth pass on top of `_MOD_GROWN` (6 lines) — the further edit a
# round can make AFTER it has already re-anchored the guard to `_MOD_GROWN`'s
# count, in the SAME turn, making that re-anchor stale before it is committed.
_MOD_GROWN_AGAIN = _MOD_GROWN + "def baz():\n    return 3\n"
_MOD_GROWN_AGAIN_LINES = len(_MOD_GROWN_AGAIN.splitlines())  # 8


class _ReanchorsThenEditsAgainBackend:
    """Turn 1: grows `tests/pkg/mod.py` to `_MOD_GROWN` (6 lines) and
    commits. Turn 2 (the corrective round) reproduces the measure-then-edit
    mistake IN ORDER: first re-anchors `FROZEN_FILE_LINES["mod.py"]` to
    `_MOD_GROWN_LINES` — the value it measured a moment ago — and only
    THEN, in the SAME turn, edits `tests/pkg/mod.py` again, growing it
    further to `_MOD_GROWN_AGAIN` (8 lines). The value the round wrote to
    the guard is now stale before the round's own commit even happens.

    The scanned product file lives under `tests/pkg/` (not `pkg/`,
    `bare_repo`'s layout) specifically so this second edit has a `tests`
    path component and is IN SCOPE for `_repro_corrective_round`'s own
    scope guard (`_repro_round_out_of_scope`, not modified by this file) —
    otherwise the edit (and, via the guard's blanket revert-on-out-of-scope
    behavior, the re-anchor alongside it) would be discarded before ever
    reaching a commit, and this fixture would not exercise the
    measure-then-edit bug at all."""

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
                                    tool_input={"file_path": "tests/pkg/mod.py"}))
            cwd.joinpath("tests", "pkg", "mod.py").write_text(_MOD_GROWN)
            return AgentResult(final_text="added bar()", num_turns=2, is_error=False,
                               tokens_used=100, session_id="s1", stop_reason="end_turn")
        if on_event is not None:
            on_event(AgentEvent("tool_use", tool_name="Edit",
                                tool_input={"file_path": "tests/test_structural_budget.py"}))
        cwd.joinpath("tests", "test_structural_budget.py").write_text(
            _guard_text({"mod.py": _MOD_GROWN_LINES}, src_dir="tests/pkg")
        )
        if on_event is not None:
            on_event(AgentEvent("tool_use", tool_name="Edit",
                                tool_input={"file_path": "tests/pkg/mod.py"}))
        cwd.joinpath("tests", "pkg", "mod.py").write_text(_MOD_GROWN_AGAIN)
        return AgentResult(final_text="re-anchored mod.py, then grew it further",
                           num_turns=2, is_error=False, tokens_used=10,
                           session_id="s2", stop_reason="end_turn")


async def test_a_round_that_edits_after_re_anchoring_still_commits_the_final_count(
        bare_repo_relocated_product, tmp_path, store):
    """RED before the fix: without a pre-commit reconcile, the corrective
    round commits the STALE `_MOD_GROWN_LINES` (6) it measured mid-round,
    even though `tests/pkg/mod.py` is really `_MOD_GROWN_AGAIN_LINES` (8)
    lines by the time of the commit — the exact mechanism dogfood case
    92e48491a7 demonstrates. The attempt would sail through review and only
    fail later, in TESTING's full-suite run, on the guard's own growth test."""
    backend = _ReanchorsThenEditsAgainBackend()
    orch, task, repo, events = await _run_one_task_attempt(
        store, bare_repo_relocated_product, tmp_path, backend)

    outcome = await orch._run_attempt(task, repo, 1, "main")

    assert outcome.status is TaskStatus.AWAITING_APPROVAL, outcome.detail
    assert backend.calls == 2

    committed_guard = subprocess.run(
        ["git", "show", "HEAD:tests/test_structural_budget.py"], cwd=repo.path,
        check=True, capture_output=True, text=True,
    ).stdout
    assert f'"mod.py": {_MOD_GROWN_AGAIN_LINES},' in committed_guard
    assert f'"mod.py": {_MOD_GROWN_LINES},' not in committed_guard

    committed_mod = subprocess.run(
        ["git", "show", "HEAD:tests/pkg/mod.py"], cwd=repo.path,
        check=True, capture_output=True, text=True,
    ).stdout
    assert len(committed_mod.splitlines()) == _MOD_GROWN_AGAIN_LINES

    reconciled = [
        e for e in events
        if e["kind"] == "structural_budget_grown" and e.get("reconciled_at_commit")
    ]
    assert len(reconciled) == 1, events

    attempts = await store.list_attempts(task.id)
    assert len(attempts) == 1


async def test_the_reconcile_corrects_upward_rather_than_tolerating_an_under_value(
        bare_repo_relocated_product, tmp_path, store):
    """The reconcile must set the frozen value to the tree's ACTUAL current
    measurement, not merely move it in the corrective direction: landing
    even one line UNDER the true count is still a ratchet-down violation —
    `offenders()` treats `current > frozen` as grown regardless of how the
    gap narrowed. Reuses the measure-then-edit fixture above (the tree
    really is `_MOD_GROWN_AGAIN_LINES` == 8 lines by commit time) and then
    pins the negative directly: an under-value of 7 is NOT tolerated by the
    guard itself."""
    backend = _ReanchorsThenEditsAgainBackend()
    orch, task, repo, events = await _run_one_task_attempt(
        store, bare_repo_relocated_product, tmp_path, backend)

    outcome = await orch._run_attempt(task, repo, 1, "main")
    assert outcome.status is TaskStatus.AWAITING_APPROVAL, outcome.detail

    committed_guard = subprocess.run(
        ["git", "show", "HEAD:tests/test_structural_budget.py"], cwd=repo.path,
        check=True, capture_output=True, text=True,
    ).stdout
    assert _MOD_GROWN_AGAIN_LINES == 8
    assert f'"mod.py": {_MOD_GROWN_AGAIN_LINES},' in committed_guard

    under_value = _MOD_GROWN_AGAIN_LINES - 1
    repo.path.joinpath("tests", "test_structural_budget.py").write_text(
        _guard_text({"mod.py": under_value}, src_dir="tests/pkg")
    )
    # Without this, a same-second, same-length rewrite of the guard file
    # (plausible here: the round's own commit already wrote it once) can
    # serve a stale `__pycache__` compile of the PRE-under-value guard —
    # exactly the race `test_a_same_second_same_length_guard_rewrite_...`
    # pins below — and this assertion would then see the wrong version.
    structural_budget.invalidate_guard_cache(repo.path)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q",
         "tests/test_structural_budget.py::test_no_frozen_entry_has_grown"],
        cwd=repo.path, capture_output=True, text=True,
    )
    assert result.returncode != 0, result.stdout + result.stderr


# --------------------------------------------------------------------------- #
# Per-mode NEGATIVE controls: a diff that touches a scanned file but leaves  #
# that mode's own check clean must fire nothing and spend nothing beyond     #
# the one bounded guard run — the positive-firing twin for each mode is      #
# already pinned above (frozen growth, new offender, stale entry).          #
# --------------------------------------------------------------------------- #


# Same line count as `_MOD_BASELINE` (2) — a content-only edit that keeps
# `pkg/mod.py` exactly at its frozen size.
_MOD_SAME_SIZE = "def foo():\n    return 2\n"
assert len(_MOD_SAME_SIZE.splitlines()) == _MOD_BASELINE_LINES

# Under the fixture guard's own MAX_FILE_LINES (4) — a brand-new file that
# never crosses the new-offender limit.
_SMALL_NEW_FILE = "x = 1\ny = 2\n"
assert len(_SMALL_NEW_FILE.splitlines()) <= _MAX_FILE_LINES


class _EditsFrozenFileWithoutGrowingItBackend:
    """One turn: rewrites `pkg/mod.py`'s CONTENT but keeps its line count
    exactly at its frozen value — `touched_frozen` fires (the file is
    frozen and touched, so the guard's whole file still runs) but neither
    the growth mode (`current > frozen`) nor the stale mode
    (`current < frozen`) trips, since `current == frozen`."""

    def __init__(self):
        self.calls = 0

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        self.calls += 1
        cwd = Path(cwd)
        if on_event is not None:
            on_event(AgentEvent("tool_use", tool_name="Edit",
                                tool_input={"file_path": "pkg/mod.py"}))
        cwd.joinpath("pkg", "mod.py").write_text(_MOD_SAME_SIZE)
        return AgentResult(final_text="tweaked foo()'s body", num_turns=1, is_error=False,
                           tokens_used=10, session_id="s", stop_reason="end_turn")


async def test_no_fire_when_growth_is_not_tripped(bare_repo, tmp_path, store):
    backend = _EditsFrozenFileWithoutGrowingItBackend()
    orch, task, repo, events = await _run_one_task_attempt(store, bare_repo, tmp_path, backend)

    outcome = await orch._run_attempt(task, repo, 1, "main")

    assert outcome.status is TaskStatus.AWAITING_APPROVAL, outcome.detail
    assert backend.calls == 1
    assert not [e for e in events if e["kind"] == "structural_budget_grown"], events
    assert not [e for e in events if e["kind"] == "structural_budget_corrective_round"], events


class _AddsSmallNewFileUnderTheLimitBackend:
    """One turn: adds a brand-new file under the guard's own
    `MAX_FILE_LINES` — `touches_scanned_root` fires (any new `.py` file
    under the scanned root) but the new-offender mode itself stays clean."""

    def __init__(self):
        self.calls = 0

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        self.calls += 1
        cwd = Path(cwd)
        if on_event is not None:
            on_event(AgentEvent("tool_use", tool_name="Edit",
                                tool_input={"file_path": "pkg/small.py"}))
        cwd.joinpath("pkg", "small.py").write_text(_SMALL_NEW_FILE)
        return AgentResult(final_text="added small.py", num_turns=1, is_error=False,
                           tokens_used=10, session_id="s", stop_reason="end_turn")


async def test_no_fire_when_new_offender_is_not_tripped(bare_repo, tmp_path, store):
    backend = _AddsSmallNewFileUnderTheLimitBackend()
    orch, task, repo, events = await _run_one_task_attempt(store, bare_repo, tmp_path, backend)

    outcome = await orch._run_attempt(task, repo, 1, "main")

    assert outcome.status is TaskStatus.AWAITING_APPROVAL, outcome.detail
    assert backend.calls == 1
    assert not [e for e in events if e["kind"] == "structural_budget_grown"], events
    assert not [e for e in events if e["kind"] == "structural_budget_corrective_round"], events


async def test_no_fire_when_stale_is_not_tripped(bare_repo, tmp_path, store):
    """Same fixture as the growth negative control above: `pkg/mod.py`
    rewritten at exactly its frozen size is simultaneously the growth AND
    the stale negative control, since `current == frozen` trips neither
    `current > frozen` (grown) nor `current < frozen` (stale)."""
    backend = _EditsFrozenFileWithoutGrowingItBackend()
    orch, task, repo, events = await _run_one_task_attempt(store, bare_repo, tmp_path, backend)

    outcome = await orch._run_attempt(task, repo, 1, "main")

    assert outcome.status is TaskStatus.AWAITING_APPROVAL, outcome.detail
    assert backend.calls == 1
    assert not [e for e in events if e["kind"] == "structural_budget_grown"], events
    assert not [e for e in events if e["kind"] == "structural_budget_corrective_round"], events


# --------------------------------------------------------------------------- #
# Bound reached — the one corrective round `_structural_budget_corrected`   #
# already allows per attempt is spent and the guard is STILL red: the       #
# attempt must fail HERE, with the budget as the cause, rather than fall    #
# through to review and die later on a generic, unattributed red suite.     #
# --------------------------------------------------------------------------- #


class _GrowsFrozenFileThenDoesNothingUsefulBackend:
    """Turn 1: grows `pkg/mod.py` past its frozen budget and commits. Turn 2
    (the ONE bounded corrective round this attempt gets): edits only
    `README.md` — never touches the guard, never touches `mod.py` — so the
    guard is still red after the round. This is the class of case the
    dogfood DB shows dying later at review on a generic red suite; the fix
    must report the budget as the cause and fail the attempt HERE instead,
    bounded to exactly this one round."""

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
            cwd.joinpath("pkg", "mod.py").write_text(_MOD_GROWN)
            return AgentResult(final_text="added bar()", num_turns=2, is_error=False,
                               tokens_used=100, session_id="s1", stop_reason="end_turn")
        if on_event is not None:
            on_event(AgentEvent("tool_use", tool_name="Edit",
                                tool_input={"file_path": "README.md"}))
        cwd.joinpath("README.md").write_text("fixture repo — unrelated edit\n")
        return AgentResult(final_text="edited README instead", num_turns=1, is_error=False,
                           tokens_used=10, session_id="s2", stop_reason="end_turn")


async def test_when_the_bounded_round_fails_the_attempt_reports_the_budget_as_the_cause(
        bare_repo, tmp_path, store):
    """RED before the fix: today a still-red guard after the round falls
    through to review, where the attempt dies later on a generic red suite
    with no cause attached — the dogfood pattern this bugfix exists to
    close. The fix must fail the attempt HERE, bounded to the one round
    `_structural_budget_corrected` already allows, with the budget named as
    the cause via `STRUCTURAL_BUDGET_CAUSE`."""
    backend = _GrowsFrozenFileThenDoesNothingUsefulBackend()
    orch, task, repo, events = await _run_one_task_attempt(store, bare_repo, tmp_path, backend)

    outcome = await orch._run_attempt(task, repo, 1, "main")

    assert backend.calls == 2, "must stay bounded to exactly one corrective round"
    assert outcome.status is TaskStatus.FAILED, outcome.detail
    assert outcome.detail.startswith(structural_budget.STRUCTURAL_BUDGET_CAUSE), outcome.detail

    attempts = await store.list_attempts(task.id)
    assert len(attempts) == 1
    assert attempts[0]["failure_reason"].startswith(
        structural_budget.STRUCTURAL_BUDGET_CAUSE
    ), attempts[0]

    bound_events = [
        e for e in events
        if e["kind"] == "structural_budget_grown" and e.get("bound_reached")
    ]
    assert len(bound_events) == 1, events
    assert bound_events[0].get("cause") == structural_budget.STRUCTURAL_BUDGET_CAUSE, (
        bound_events[0]
    )

    assert not any(_is_review_boundary(e) for e in events), events


# --------------------------------------------------------------------------- #
# AC2 — a diff that touches no scanned file: no extra test run, nothing      #
# spent.                                                                     #
# --------------------------------------------------------------------------- #


class _TouchesNonFrozenFileBackend:
    """One turn, edits a file the guard freezes no budget for."""

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


async def test_a_diff_touching_no_scanned_file_runs_no_extra_test(
        bare_repo, tmp_path, store, monkeypatch):
    recorded_cmds = []
    orig_run_tests = runner_mod.run_tests

    def _spy_run_tests(repo_path, test_cmd, *, cwd=None, source_repo=None):
        recorded_cmds.append(test_cmd)
        return orig_run_tests(repo_path, test_cmd, cwd=cwd, source_repo=source_repo)

    monkeypatch.setattr(runner_mod, "run_tests", _spy_run_tests)

    backend = _TouchesNonFrozenFileBackend()
    orch, task, repo, events = await _run_one_task_attempt(store, bare_repo, tmp_path, backend)

    outcome = await orch._run_attempt(task, repo, 1, "main")

    assert outcome.status is TaskStatus.AWAITING_APPROVAL, outcome.detail
    assert backend.calls == 1

    assert not [e for e in events if e["kind"] == "structural_budget_grown"], events
    assert not [e for e in events if e["kind"] == "structural_budget_corrective_round"], events

    # The guard's own file was never invoked, bounded or otherwise — the
    # whole point of the fail-open, zero-diff-intersection short-circuit —
    # neither the single growth node id NOR the whole-file command AC2 adds.
    assert not any(structural_budget.GROWTH_NODE_ID in (c or "") for c in recorded_cmds), (
        recorded_cmds
    )
    assert not any(structural_budget.GUARD_RELPATH in (c or "") for c in recorded_cmds), (
        recorded_cmds
    )


# --------------------------------------------------------------------------- #
# AC1 (stale-bytecode race) — a same-second, same-length rewrite of the      #
# guard file can be served from a stale compile of ITSELF, exactly what the  #
# corrective round above does to the fixture's own guard. Reproduced         #
# directly with `subprocess`/`os.utime`, deterministically — no wall-clock   #
# race, no dependency on this test's own execution speed.                    #
# --------------------------------------------------------------------------- #


_STALE_MARKER_V1 = "one"
_STALE_MARKER_V2 = "two"


def _stale_bytecode_guard(marker: str, out_path: Path) -> str:
    """A minimal guard-shaped module whose only test writes *marker* to
    *out_path* as a side effect before asserting on it — so which version's
    CODE actually ran is observable directly, via *out_path*'s content,
    independent of the assertion's own outcome (a naive
    `returncode`-only check cannot tell "stale bytecode served" apart from
    "recompiled but coincidentally passes": if the WHOLE module is stale,
    the assertion and the value it compares against come from the same old
    compile and trivially agree either way). *marker* is written both as the
    literal value written to *out_path* AND as the literal compared against
    in the assertion — kept intentionally equal-length across V1/V2 so the
    rewritten file matches the ORIGINAL in both mtime-worthy byte count and
    (forced via `os.utime`) mtime itself."""
    return textwrap.dedent(f'''\
        """Stale-bytecode-race fixture guard."""

        OUT_PATH = {str(out_path)!r}
        MARKER = {marker!r}


        def test_marks_which_version_ran():
            with open(OUT_PATH, "w") as fh:
                fh.write(MARKER)
            assert MARKER == {marker!r}
    ''')


def _run_guard_once(guard: Path, tmp_path: Path) -> None:
    """Run *guard* under pytest, in a fresh subprocess, with bytecode writing
    forced ON regardless of the ambient environment — the race this section
    reproduces depends on a `.pyc` actually being written and later read."""
    env = dict(os.environ)
    env.pop("PYTHONDONTWRITEBYTECODE", None)
    subprocess.run(
        [sys.executable, "-m", "pytest", "-q", str(guard)],
        cwd=tmp_path, env=env, capture_output=True, text=True, check=False,
    )


def test_a_same_second_same_length_guard_rewrite_serves_the_stale_compile(tmp_path):
    """Demonstrates the race with no fix in play: write V1, run it (compiles
    and caches its bytecode), force the rewrite to V2 to land at the exact
    same mtime and byte length as V1 (`os.utime`, not a wall-clock race), run
    again with no cache invalidation in between — the second run still
    reports V1, the STALE compile."""
    guard = tmp_path / structural_budget.GUARD_RELPATH
    guard.parent.mkdir(parents=True, exist_ok=True)
    out = tmp_path / "out.txt"

    v1 = _stale_bytecode_guard(_STALE_MARKER_V1, out)
    v2 = _stale_bytecode_guard(_STALE_MARKER_V2, out)
    assert len(v1) == len(v2), "the fixture's two versions must be same-length"

    guard.write_text(v1)
    _run_guard_once(guard, tmp_path)
    assert out.read_text() == _STALE_MARKER_V1

    stat = guard.stat()
    guard.write_text(v2)
    os.utime(guard, (stat.st_atime, stat.st_mtime))  # force identical (mtime, size)

    _run_guard_once(guard, tmp_path)
    # No `invalidate_guard_cache` call — the race: the stale compile of V1
    # is served, so the side file is never updated to V2.
    assert out.read_text() == _STALE_MARKER_V1, (
        "if this fails, the race stopped reproducing on this platform/"
        "interpreter — the fix below would then be proving nothing"
    )


def test_invalidate_guard_cache_makes_the_second_write_the_one_that_executes(tmp_path):
    """The fix: the same same-second, same-length rewrite as above, but with
    `structural_budget.invalidate_guard_cache` called in between — the
    second run must now report V2. RED without the fix (or with a no-op
    `invalidate_guard_cache`): this assertion would see V1 instead."""
    guard = tmp_path / structural_budget.GUARD_RELPATH
    guard.parent.mkdir(parents=True, exist_ok=True)
    out = tmp_path / "out.txt"

    v1 = _stale_bytecode_guard(_STALE_MARKER_V1, out)
    v2 = _stale_bytecode_guard(_STALE_MARKER_V2, out)
    assert len(v1) == len(v2), "the fixture's two versions must be same-length"

    guard.write_text(v1)
    _run_guard_once(guard, tmp_path)
    assert out.read_text() == _STALE_MARKER_V1

    stat = guard.stat()
    guard.write_text(v2)
    os.utime(guard, (stat.st_atime, stat.st_mtime))  # force identical (mtime, size)

    structural_budget.invalidate_guard_cache(tmp_path)
    _run_guard_once(guard, tmp_path)
    assert out.read_text() == _STALE_MARKER_V2


# --------------------------------------------------------------------------- #
# Helper units — pure, no git, no backend (mirrors the declared-files        #
# preflight file's bottom section).                                          #
# --------------------------------------------------------------------------- #


def _write_guard(root: Path, *, src: str = "src/no_human") -> None:
    (root / "tests").mkdir(parents=True, exist_ok=True)
    (root / "tests" / "test_structural_budget.py").write_text(f'''from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "{src}"

FROZEN_FUNCTION_LINES = {{
    "core/orchestrator.py:Orchestrator._run_attempt": 2186,
}}
FROZEN_FUNCTION_CC = {{
    "core/orchestrator.py:Orchestrator._run_attempt": 255,
}}
FROZEN_FILE_LINES = {{
    "core/orchestrator.py": 21320,
}}
''')


def test_frozen_paths_reads_all_three_frozen_dicts_src_prefixed(tmp_path):
    _write_guard(tmp_path)
    paths = structural_budget.frozen_paths(tmp_path)
    assert paths == {
        "src/no_human/core/orchestrator.py",
    }


def test_frozen_paths_with_no_guard_file_is_empty(tmp_path):
    assert structural_budget.frozen_paths(tmp_path) == set()


def test_frozen_paths_with_an_unparseable_guard_is_empty(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_structural_budget.py").write_text("def broken(:\n")
    assert structural_budget.frozen_paths(tmp_path) == set()


def test_touched_frozen_is_the_sorted_intersection():
    frozen = {"pkg/a.py", "pkg/b.py", "pkg/c.py"}
    changed = ["pkg/b.py", "pkg/z.py", "pkg/a.py"]
    assert structural_budget.touched_frozen(frozen, changed) == ["pkg/a.py", "pkg/b.py"]
    assert structural_budget.touched_frozen(frozen, ["pkg/z.py"]) == []


def test_bounded_growth_command_is_none_for_a_non_pytest_command():
    assert structural_budget.bounded_growth_command("npm test") is None
    assert structural_budget.bounded_growth_command(None) is None


def test_bounded_growth_command_appends_the_growth_node_id():
    cmd = structural_budget.bounded_growth_command("pytest -q")
    assert cmd is not None
    assert cmd.startswith("pytest -q ")
    assert structural_budget.GROWTH_NODE_ID in cmd


def test_scanned_root_reads_the_src_assignment(tmp_path):
    _write_guard(tmp_path)
    assert structural_budget.scanned_root(tmp_path) == "src/no_human"


def test_scanned_root_with_no_guard_file_is_none(tmp_path):
    assert structural_budget.scanned_root(tmp_path) is None


def test_scanned_root_with_an_unparseable_guard_is_none(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_structural_budget.py").write_text("def broken(:\n")
    assert structural_budget.scanned_root(tmp_path) is None


def test_touches_scanned_root_is_the_sorted_py_files_under_the_root():
    changed = [
        "src/no_human/a.py", "src/no_human/sub/b.py", "src/other/c.py",
        "src/no_human/README.md", "README.md",
    ]
    assert structural_budget.touches_scanned_root("src/no_human", changed) == [
        "src/no_human/a.py", "src/no_human/sub/b.py",
    ]
    assert structural_budget.touches_scanned_root(None, changed) == []


def test_bounded_guard_command_is_none_for_a_non_pytest_command():
    assert structural_budget.bounded_guard_command("npm test") is None
    assert structural_budget.bounded_guard_command(None) is None


def test_bounded_guard_command_appends_the_whole_guard_file():
    cmd = structural_budget.bounded_guard_command("pytest -q")
    assert cmd is not None
    assert cmd.startswith("pytest -q ")
    assert structural_budget.GUARD_RELPATH in cmd
    assert structural_budget.GROWTH_NODE_ID not in cmd


def test_send_back_message_names_the_guard_and_the_touched_paths_and_forbids_widening():
    msg = structural_budget_send_back_message(
        ["pkg/mod.py"], "mod.py: frozen 2, now 6 (+4); this budget only ratchets down",
    )
    assert "pkg/mod.py" in msg
    assert structural_budget.GUARD_RELPATH in msg
    assert "frozen 2, now 6 (+4)" in msg
    assert "tampering" in msg.lower() or "not an option" in msg.lower()
    assert "MAX_FUNCTION_LINES" in msg
