"""When base refresh skips the merge for a conflict, the coder must be told
to MERGE the pinned base and resolve conflicts — never to rebase.

Incident: a coder ran `git rebase origin/main` / `git rebase --continue` on a
branch whose tip was already pushed, after the harness's base-refresh skipped
a merge for a conflict. Nothing in the event text or the coder's rules block
said "merge, don't rebase" — the coder reached for the familiar tool, rewrote
the pushed commits, and delivery refused the branch afterward ('remote tip
... is not an ancestor of the reviewed sha').

`prompt_blocks.base_merge_conflict_instruction` is the ONE wording shared by
exactly four call sites in the tree (verified by
`test_every_merge_instruction_site_is_exercised_and_the_count_matches_the_tree`
below, not merely asserted in prose):
  - `prompt_blocks.py`'s `build_rules_block`, via its `base_merge_conflict=`
    kwarg (the "Rules:" section the coder reads every turn),
  - `Orchestrator._refresh_stale_base`'s event text (`"merge skipped
    (conflict)"` is pinned verbatim by other tests — kept as-is here),
  - `Orchestrator._build_implement_prompt`'s staleness preamble, and
  - `WakeWatcher._check_pr_conflict`'s send-back message and `pr_conflict`
    event text — the wake rung's own textual-conflict round, a separate
    call site from the three above.

These tests exercise all four call sites.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from no_human.core.orchestrator import Orchestrator
from no_human.core.prompt_blocks import base_merge_conflict_instruction, build_rules_block
from no_human.core.task import Task, TaskStatus
from no_human.vcs import derived_conflict as dc

# Reuse the real bare-remote fixtures and attempt-driving helper from the
# sibling pushed-branch staleness test file rather than duplicating them.
from tests.test_base_staleness_pushed_branch import (  # noqa: F401
    _attempt,
    _git,
    _make_pushed_conflicting_branch,
    _staleness_events,
    origin,
    repo,
)

# Reuse the wake rung's own task/watcher builders from its dedicated test
# file rather than duplicating them — that file's `_resolvable_conflicting_
# paths` autouse fixture is file-local and does NOT travel with the import,
# so it is replicated below via an explicit `monkeypatch.setattr`.
from tests.test_wake_conflict import _approval_task, _watcher  # noqa: F401


def _orch():
    orch = object.__new__(Orchestrator)
    orch.config = {}
    orch.ci_runner = None
    orch._active_profile = None
    orch._active_memories = None
    return orch


def _task(**kw):
    defaults = dict(
        id="aaa", source="test", title="Fix bug",
        status=TaskStatus.IMPLEMENTING,
        acceptance_criteria=["Bug is fixed"],
    )
    defaults.update(kw)
    return Task(**defaults)


# --------------------------------------------------------------------------- #
# The rules block: byte-identical when unset, names the merge when set.
# --------------------------------------------------------------------------- #

def test_the_rules_block_carries_the_merge_instruction_only_when_asked():
    baseline = build_rules_block("pytest", "", None)
    unset = build_rules_block("pytest", "", None, base_merge_conflict=None)
    assert baseline == unset, (
        "build_rules_block must stay byte-identical when base_merge_conflict "
        "is not given — no new bytes for every attempt that never hits a "
        "base-merge conflict"
    )

    conflicted = build_rules_block(
        "pytest", "", None, base_merge_conflict="abc123def456")
    assert conflicted != baseline
    assert "`git merge abc123def456`" in conflicted
    assert "Do NOT rebase" in conflicted
    # The override must be explicit about superseding the blanket "no git"
    # rule, or a careful coder reading top-to-bottom stops at the first rule.
    assert "Do NOT run any git" in conflicted


# --------------------------------------------------------------------------- #
# The base_staleness event: drives the REAL `_refresh_stale_base` end to end
# on a bare remote where the merge genuinely conflicts.
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_the_conflict_event_text_tells_the_coder_to_merge_not_rebase(
    repo, tmp_path, store, monkeypatch,
):
    remote_tip = _make_pushed_conflicting_branch(repo, "no-human/t7")
    ctx = {"pr_branch": "no-human/t7"}

    t, events, orch = await _attempt(repo, tmp_path, store, monkeypatch, ctx)

    evs = _staleness_events(events)
    assert len(evs) == 1
    ev = evs[0]
    assert "merge skipped (conflict)" in ev["text"], (
        "existing tests pin this exact substring — must not regress"
    )
    assert "git merge " in ev["text"]
    assert "Do NOT rebase" in ev["text"]

    staleness = t.context["base_staleness"]
    assert staleness.get("merge_conflict") is True
    # `base_pin` must be threaded through from the real `_refresh_stale_base`
    # run, and the event text must carry it as the merge target.
    assert staleness["base_pin"]
    assert staleness["base_pin"] in ev["text"]


# --------------------------------------------------------------------------- #
# The implement-prompt preamble: synthetic context, no subprocess needed —
# same pattern as `tests/test_retry_base_staleness.py`'s preamble tests.
# --------------------------------------------------------------------------- #

def test_the_implement_prompt_preamble_says_merge_not_rebase():
    t = _task()
    t.context = {"base_staleness": {
        "commits_behind": 2, "was_behind": 2, "rebased": False,
        "merge_conflict": True, "base_pin": "deadbee123",
    }}
    prompt = _orch()._build_implement_prompt(t, "/tmp/repo")

    assert "git merge deadbee123" in prompt
    assert "Do NOT rebase" in prompt
    assert "2 COMMIT(S) BEHIND" in prompt
    # The old generic rebase-conflict wording must NOT appear for this case
    # — it says the opposite of what's true here (no rebase was attempted).
    assert "a rebase was attempted" not in prompt

    # And the Rules: block itself carries the same instruction, since
    # `_build_implement_prompt` threads `base_merge_conflict` through to
    # `build_rules_block` whenever `merge_conflict` is set.
    assert "`git merge deadbee123`" in prompt


def test_the_implement_prompt_preamble_falls_back_when_base_pin_is_missing():
    t = _task(id="ddd")
    t.context = {"base_staleness": {
        "commits_behind": 2, "was_behind": 2, "rebased": False,
        "merge_conflict": True,
    }}
    prompt = _orch()._build_implement_prompt(t, "/tmp/repo")
    assert "Do NOT rebase" in prompt
    assert "the current base" in prompt


# --------------------------------------------------------------------------- #
# The wake pr_conflict round: the fourth call site, and the one an earlier
# review round accidentally shipped with ZERO assertions pinning it — a
# revert to the old hardcoded "Rebase onto origin/main..." wording would not
# have turned anything red. Drives the real `_check_open_pr` ->
# `_check_pr_conflict` path end to end, not a synthetic call to
# `base_merge_conflict_instruction` in isolation.
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_the_pr_conflict_round_tells_the_coder_to_merge_not_rebase(
    store, monkeypatch,
):
    # `_check_pr_conflict` enumerates conflicting paths for the real
    # repo_path it's given; `_approval_task` points at a fake, non-existent
    # "/tmp/x", so enumeration must be stubbed the same way
    # `test_wake_conflict.py`'s (file-local, non-travelling) autouse fixture
    # does.
    async def fake_conflicting_paths(repo_path, base_tip, branch):
        return {"src/unrelated.py"}
    monkeypatch.setattr(dc, "conflicting_paths", fake_conflicting_paths)

    t = await _approval_task(store)
    events = []
    w = _watcher(store, mergeable="CONFLICTING", merge_state="DIRTY", events=events)
    out = await w._check_open_pr(t)
    assert out == "resumed"

    fresh = await store.get_task(t.id)
    message = fresh.context["send_back_feedback"][-1]["message"]
    assert base_merge_conflict_instruction("origin/main") in message, (
        "the send-back message must carry the shared merge-not-rebase "
        "wording verbatim — reverting to a hardcoded rebase string must "
        "turn this assertion red"
    )
    assert "git merge origin/main" in message
    assert "Rebase onto origin/main" not in message
    assert "Rebase onto" not in message

    ev_texts = [text for kind, text in events if kind == "pr_conflict"]
    assert ev_texts, "a pr_conflict event must be emitted"
    assert "merge round" in ev_texts[0]
    assert "rebase round" not in ev_texts[0]


# --------------------------------------------------------------------------- #
# Criterion 2: reverting the pr_conflict instruction in blockers/wake.py to
# the rebase wording must turn a named test red — that's
# `test_the_pr_conflict_round_tells_the_coder_to_merge_not_rebase` above, via
# its `"Rebase onto origin/main" not in message` assertion. This test is that
# assertion's own cross-check: it counts every real invocation of
# `base_merge_conflict_instruction(` in the tree (excluding the def itself
# and the comments that merely name the function) and pins the count and the
# exact file:line set, so a fifth call site added anywhere — or one of the
# four silently deleted — is caught here rather than discovered by chance.
# --------------------------------------------------------------------------- #

_CALL_RE = re.compile(r"(?<!def )\bbase_merge_conflict_instruction\(")


def _merge_instruction_call_sites() -> list[str]:
    root = Path(__file__).resolve().parent.parent / "src" / "no_human"
    hits: list[str] = []
    for path in sorted(root.rglob("*.py")):
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if _CALL_RE.search(line):
                hits.append(f"{path.relative_to(root.parent.parent)}:{lineno}")
    return hits


def test_every_merge_instruction_site_is_exercised_and_the_count_matches_the_tree():
    sites = _merge_instruction_call_sites()
    assert sites == [
        "src/no_human/blockers/wake.py:2343",
        "src/no_human/core/orchestrator.py:3865",
        "src/no_human/core/orchestrator.py:18806",
        "src/no_human/core/prompt_blocks.py:958",
    ], (
        "the shared merge-not-rebase wording is called from exactly four "
        "places; this list must be kept in sync by hand whenever a call "
        "site moves, is added, or is removed — that's the point of pinning "
        "it here rather than trusting the module docstring's prose count"
    )
    # Each site is exercised by a test in this module (or its sibling
    # `test_base_staleness_pushed_branch.py`, whose staleness event test is
    # reused verbatim above): prompt_blocks.py:958 by
    # `test_the_rules_block_carries_the_merge_instruction_only_when_asked`
    # (directly) and by the two end-to-end tests below it (through
    # `build_rules_block`'s `base_merge_conflict=` kwarg);
    # orchestrator.py:3865 by
    # `test_the_conflict_event_text_tells_the_coder_to_merge_not_rebase`;
    # orchestrator.py:18806 by
    # `test_the_implement_prompt_preamble_says_merge_not_rebase` and its
    # fallback sibling; wake.py:2343 by
    # `test_the_pr_conflict_round_tells_the_coder_to_merge_not_rebase`.


def test_no_hardcoded_rebase_onto_wording_survives_anywhere_in_src():
    """The old hardcoded string this fix replaced. `runner.py`'s incident
    docstring talks ABOUT a coder being told to merge and resolve conflicts
    but never spells this exact phrase, so it is not a false positive here;
    if a future edit reintroduces the literal wording anywhere under `src/`
    — not just at the one call site this incident was about — this must
    catch it.
    """
    root = Path(__file__).resolve().parent.parent / "src" / "no_human"
    hits = []
    for path in sorted(root.rglob("*.py")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if "Rebase onto" in line:
                hits.append(f"{path.relative_to(root.parent.parent)}:{lineno}: {line.strip()}")
    assert hits == [], hits
