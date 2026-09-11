"""The project's standing review constraint — evidence-based review, never
numeric self-scoring — forbids a numeric self-score ('score 1-10, continue
if 10' gate). The hardcoded standing rules injected into every coder session
used to say exactly that -- on TWO independent sites that both reach the coder:

  1. ``Orchestrator._materialize_compact_instructions`` -- writes
     ``.claude/instructions.md``, which survives context compaction.
  2. ``prompt_blocks.build_rules_block`` -- feeds the primary implement
     system prompt via ``Orchestrator._build_implement_prompt``
     (``rules = build_rules_block(...)`` at orchestrator.py).

An earlier fix only reworded the compacted echo in
``.claude/instructions.md``'s source text and missed site 2, so the coder's
actual system prompt kept shipping the banned phrase even though the
compaction file no longer did. Both sites are asserted here so neither can
regress independently: the number is gone, and the "close the gap before
proceeding" intent survives it.
"""
from __future__ import annotations

import pytest

from no_human.core.prompt_blocks import build_rules_block
from no_human.core.task import Task

_BANNED = ("1–10", "1-10")


@pytest.fixture
def compact_instructions(tmp_path):
    """Render `.claude/instructions.md` for a task and hand back its text."""
    from no_human.core.orchestrator import Orchestrator

    def _render(task: Task):
        o = Orchestrator.__new__(Orchestrator)
        o.config = {}
        o.store = None
        o._active_memories = []
        o._discovered_skills_info = []
        o._active_profile = None
        o._materialize_compact_instructions(tmp_path, task)
        path = tmp_path / ".claude" / "instructions.md"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    return _render


def test_compact_instructions_have_no_numeric_self_score(compact_instructions, tmp_path):
    task = Task.new("fix the parser", repo_path=str(tmp_path / "repo"))
    text = compact_instructions(task)
    for banned in _BANNED:
        assert banned not in text, (
            f"{banned!r} in .claude/instructions.md — numeric self-score gate "
            "still injected into the compacted echo"
        )
    assert "unresolved gap" in text


def test_implement_prompt_rules_block_has_no_numeric_self_score():
    """The site the first fix attempt missed: `build_rules_block` feeds
    `_build_implement_prompt` directly (`rules = build_rules_block(...)`),
    independent of the compacted-instructions path above."""
    r = build_rules_block("", "", None)
    for banned in _BANNED:
        assert banned not in r, (
            f"{banned!r} in build_rules_block() output — this string is what "
            "the coder's primary system prompt actually contains"
        )
    assert "unresolved gap" in r
