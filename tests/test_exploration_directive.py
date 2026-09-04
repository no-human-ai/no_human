"""Phase C: exploration must route through the researcher sub-agent.

The advisory ("consider delegating") produced almost no delegation, so every
exploration byte landed in the coder's own context and was re-sent on every
subsequent turn (57.8k/turn measured across 76 attempts).
"""

from __future__ import annotations


from no_human.config import load_config
from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task
from no_human.notify.slack import SlackNotifier

from .test_e2e_orchestrator import FakeBackend


async def test_exploration_directive_reaches_the_coder(store, tmp_path):
    """It lands in .claude/instructions.md — auto-loaded EVERY turn, so the
    directive is in context exactly when the coder decides how to explore."""
    cfg = load_config(tmp_path / "c.yaml")
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None))
    t = Task.new("add a thing", repo_path=str(tmp_path))
    await store.create_task(t)

    orch._materialize_compact_instructions(tmp_path, t)
    prompt = (tmp_path / ".claude" / "instructions.md").read_text()

    # It must state the COST (why), name the tool (what), and draw the line
    # between searching (delegate) and targeted reading (direct).
    assert "RE-SENT on every remaining turn" in prompt
    assert "no_human_researcher" in prompt
    assert "delegate when you are searching" in prompt.lower()
    # The dead advisory phrasing must be gone.
    assert "consider delegating" not in prompt
