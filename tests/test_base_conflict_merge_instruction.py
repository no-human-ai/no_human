"""When base refresh skips the merge for a conflict, the coder must be told
to MERGE the pinned base and resolve conflicts — never to rebase.

Incident: a coder ran `git rebase origin/main` / `git rebase --continue` on a
branch whose tip was already pushed, after the harness's base-refresh skipped
a merge for a conflict. Nothing in the event text or the coder's rules block
said "merge, don't rebase" — the coder reached for the familiar tool, rewrote
the pushed commits, and delivery refused the branch afterward ('remote tip
... is not an ancestor of the reviewed sha').

`prompt_blocks.base_merge_conflict_instruction` is the ONE wording shared by:
  - `build_rules_block`'s new `base_merge_conflict=` kwarg (the "Rules:"
    section the coder reads every turn),
  - `Orchestrator._refresh_stale_base`'s event text (`"merge skipped
    (conflict)"` is pinned verbatim by other tests — kept as-is here), and
  - `Orchestrator._build_implement_prompt`'s staleness preamble.

These tests exercise all three call sites.
"""
from __future__ import annotations

import pytest

from no_human.core.orchestrator import Orchestrator
from no_human.core.prompt_blocks import base_merge_conflict_instruction, build_rules_block
from no_human.core.task import Task, TaskStatus

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
