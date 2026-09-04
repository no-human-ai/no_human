"""Review of e79c5fc5 (the no-full-suite hint) found that the feature/bugfix/
test_gap entries of _KIND_DIRECTIVES still told the coder to run the full
(unit) test suite and paste the output — contradicting both the .no_human.yml
routing hint (never hand-run the repo-wide suite) and the routed-test prompt
branch in prompt_blocks.py ('your job is the scoped evidence, not the
marathon'). A hand-run of this repo's ~8k tests exceeds tool timeouts."""

from __future__ import annotations

import pytest

from no_human.config import load_config
from no_human.core.orchestrator import Orchestrator
from no_human.core.prompt_blocks import build_rules_block
from no_human.core.task import Task
from no_human.notify.slack import SlackNotifier

from .test_e2e_orchestrator import FakeBackend

SCOPED_KINDS = ("feature", "bugfix", "test_gap")

REPO_WIDE_PHRASES = (
    "full test suite",
    "full unit test suite",
    "run all tests",
    "entire suite",
    "complete suite",
    "whole suite",
    "repo-wide",
)


def _directive(orch, kind, tmp_path):
    t = Task.new(f"do a {kind} thing", repo_path=str(tmp_path))
    t.kind = kind
    return orch._kind_directive(t)


@pytest.mark.parametrize("kind", SCOPED_KINDS)
async def test_kind_directives_never_demand_a_repo_wide_run(store, tmp_path, kind):
    cfg = load_config(tmp_path / "c.yaml")
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None))
    low = _directive(orch, kind, tmp_path).lower()
    for phrase in REPO_WIDE_PHRASES:
        assert phrase not in low, f"{kind!r} directive still says {phrase!r}"


@pytest.mark.parametrize("kind", SCOPED_KINDS)
async def test_kind_directives_demand_change_scoped_tests(store, tmp_path, kind):
    cfg = load_config(tmp_path / "c.yaml")
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None))
    low = _directive(orch, kind, tmp_path).lower()
    assert "your change" in low
    assert "scope" in low or "the tests you" in low


@pytest.mark.parametrize("kind", SCOPED_KINDS)
async def test_kind_directives_require_command_and_output_as_evidence(store, tmp_path, kind):
    cfg = load_config(tmp_path / "c.yaml")
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None))
    low = _directive(orch, kind, tmp_path).lower()
    assert "paste" in low
    assert "output" in low
    assert "command" in low


@pytest.mark.parametrize("kind", SCOPED_KINDS)
async def test_kind_directives_show_a_concrete_scoped_command(store, tmp_path, kind):
    cfg = load_config(tmp_path / "c.yaml")
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None))
    directive = _directive(orch, kind, tmp_path)
    assert "uv run pytest" in directive


async def test_prompt_blocks_routed_branch_still_says_scope_not_marathon():
    block = build_rules_block("uv run pytest -q -n 4", "", None)
    assert "FINAL GATE" in block
    assert "exactly ONCE" in block
    assert "your job is the scoped evidence, not the marathon" in block


async def test_other_kind_directives_unchanged(store, tmp_path):
    cfg = load_config(tmp_path / "c.yaml")
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None))
    ci_fix = _directive(orch, "ci_fix", tmp_path).lower()
    assert "never weaken, skip, or delete a" in ci_fix

    traceability = _directive(orch, "traceability", tmp_path)
    assert "Run the test suite to" in traceability

    investigation = _directive(orch, "investigation", tmp_path)
    assert "Do NOT guess" in investigation

    design_doc = _directive(orch, "design_doc", tmp_path)
    assert "WRITE THE DOCUMENT TO A FILE" in design_doc
