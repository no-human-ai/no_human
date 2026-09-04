"""Refile of db605dd0 (38.5M tok/week measured): the utility and supervisor
tiers' single-turn advisory calls (intake evaluation, context distillation,
the supervisor course-corrector) used to be built with a plain
``ClaudeBackend(...)``, the same construction the CODER uses — which ships
the full built-in tool schema and the coding harness's system prompt on
every call, even though none of these calls ever uses a tool.

``agent/advisory.py::advisory_backend`` is the one call-construction seam
that fixes this. These tests pin: no tool definitions serialize (AC1), the
role's own system prompt replaces the coding harness's (AC2), every advisory
call site actually goes through the seam, and the one load-bearing exception
(``grill_spec``, which explores a real repo) is untouched.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch as _patch

import pytest
from click.testing import CliRunner

# The option-building tests exercise the REAL ClaudeBackend class over its
# own `_options()` — no network, but exempt from the hermetic stub by the
# same convention as tests/test_backend.py.
pytestmark = pytest.mark.real_backend

import no_human
from no_human.agent.advisory import ADVISORY_ROLE_PROMPTS, advisory_backend
from no_human.agent.claude_backend import AgentResult, ClaudeBackend

ROLES = ("intake", "distill", "supervisor")


# --------------------------------------------------------------------------- #
# AC1 — zero tool definitions                                                 #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("role", ROLES)
def test_advisory_options_carry_no_tools(tmp_path, role):
    opts = advisory_backend("claude-haiku-4-5", role=role)._options(tmp_path, 1)
    assert opts.tools == []
    assert opts.allowed_tools == []


def test_serialized_cli_command_has_no_tool_definitions(tmp_path):
    transport_mod = pytest.importorskip(
        "claude_agent_sdk._internal.transport.subprocess_cli",
        reason="private SDK transport path moved",
    )
    SubprocessCLITransport = transport_mod.SubprocessCLITransport

    adv_opts = advisory_backend(
        "claude-haiku-4-5", role="intake")._options(tmp_path, 1)
    transport = SubprocessCLITransport("prompt", adv_opts)
    transport._cli_path = "/usr/bin/true"
    cmd = transport._build_command()
    assert "--tools" in cmd
    assert cmd[cmd.index("--tools") + 1] == ""
    assert "--allowedTools" not in cmd

    coder_opts = ClaudeBackend(model="claude-sonnet-5")._options(tmp_path, 40)
    coder_transport = SubprocessCLITransport("prompt", coder_opts)
    coder_transport._cli_path = "/usr/bin/true"
    coder_cmd = coder_transport._build_command()
    assert "--tools" not in coder_cmd


# --------------------------------------------------------------------------- #
# AC2 — the role's own prompt, not the coding harness's                       #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("role", ROLES)
def test_system_prompt_is_the_role_prompt(tmp_path, role):
    opts = advisory_backend("claude-haiku-4-5", role=role)._options(tmp_path, 1)
    assert isinstance(opts.system_prompt, str)
    assert opts.system_prompt == ADVISORY_ROLE_PROMPTS[role]
    assert opts.system_prompt.strip() != ""
    # Not a preset dict — a real string is what forces `--system-prompt
    # <text>` rather than the CLI's own default/append behaviour.
    assert not isinstance(opts.system_prompt, dict)


def test_unknown_role_raises():
    with pytest.raises(ValueError):
        advisory_backend("claude-haiku-4-5", role="bogus")


# --------------------------------------------------------------------------- #
# Scope guard: grill_spec keeps its tools                                     #
# --------------------------------------------------------------------------- #

def test_grill_spec_keeps_its_tools(tmp_path):
    """`grill_spec` explores a real repo (max_turns=8) — it must still get
    the full harness, never the toolless seam."""
    from no_human.agent.claude_backend import ClaudeBackend as _CB
    backend = _CB(model="claude-haiku-4-5", readonly=True)
    opts = backend._options(tmp_path, 8)
    assert opts.tools is None
    assert opts.system_prompt is None


# --------------------------------------------------------------------------- #
# Call-site wiring — the thing that actually saves tokens                     #
# --------------------------------------------------------------------------- #

class _FakeAdvisory:
    def __init__(self, text=""):
        self.text = text

    async def run(self, prompt, **kw):
        return AgentResult(final_text=self.text, num_turns=1, is_error=False,
                           tokens_used=1, session_id="s", stop_reason="end")


async def test_intake_evaluator_uses_the_toolless_seam(monkeypatch):
    from no_human.intake import evaluator

    calls = []

    def _recorder(model, *, role):
        calls.append(role)
        return _FakeAdvisory()

    monkeypatch.setattr("no_human.agent.advisory.advisory_backend", _recorder)
    # Regression guard: if a call site reverted to a bare ClaudeBackend(...),
    # this raiser would fire instead of the recorder above.
    monkeypatch.setattr(
        "no_human.agent.claude_backend.ClaudeBackend",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("bare ClaudeBackend constructed on an advisory path")),
    )

    await evaluator.evaluate_spec("t", "d", ["ac"])
    await evaluator.resolve_assumptions("t", "d", ["ac"])
    await evaluator.generate_grill_questions("t", "d", ["ac"])

    assert calls == ["intake", "intake", "intake"]


def _orch_min():
    from no_human.core.orchestrator import Orchestrator
    orch = object.__new__(Orchestrator)
    orch.config = {}
    orch._sink = lambda e: None
    orch.store = None
    orch._attempt_usage = {}
    return orch


async def test_orchestrator_advisory_calls_use_the_toolless_seam(
    store, tmp_path, monkeypatch,
):
    from no_human.core.orchestrator import Orchestrator
    from no_human.core.task import Task
    from no_human.notify.slack import SlackNotifier

    class _NoBackend:
        async def run(self, *a, **k):  # pragma: no cover
            raise AssertionError("the coder backend should not run")

    calls = []

    def _recorder(model, *, role):
        calls.append(role)
        return _FakeAdvisory("CONTINUE\nlooks fine")

    monkeypatch.setattr("no_human.core.orchestrator.advisory_backend", _recorder)
    # Regression guard: the module-top alias `orchestrator.ClaudeBackend` must
    # not be what these three sites construct any more.
    monkeypatch.setattr(
        "no_human.core.orchestrator.ClaudeBackend",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("bare ClaudeBackend constructed on an advisory path")),
    )

    from no_human.config import load_config
    cfg = load_config(tmp_path / "config.yaml")
    cfg.data.setdefault("planning", {})["enabled"] = False
    orch = Orchestrator(store, cfg.data, _NoBackend(), SlackNotifier(None))

    # _generate_stuck_hypothesis (distill role)
    t = Task.new("fix it", repo_path=str(tmp_path))
    t.context = {"attempt_log": ["attempt 1: failed", "attempt 2: failed"]}
    await orch._generate_stuck_hypothesis(t)

    # _distill_review_lesson (distill role)
    await orch._distill_review_lesson(t, "summarize this finding")

    # sv_llm_call, reached via the real SupervisorHook wiring (supervisor role)
    hook = orch._build_supervisor(t, str(tmp_path))
    assert hook is not None
    await hook.preflight("a plan")

    assert "intake" not in calls
    assert calls.count("distill") == 2
    assert calls.count("supervisor") == 1


# --------------------------------------------------------------------------- #
# AC1 — exhaustive enumeration: every remaining bare construction is named    #
# --------------------------------------------------------------------------- #

#: Every file under `src/no_human` that still constructs a bare
#: `ClaudeBackend(...)`, on purpose: the coder/reviewer/planner/eval tiers
#: (genuinely multi-turn and tool-using), `grill_spec` (explores a real repo,
#: `max_turns=8`), `verify_credential_live` (must exercise the production
#: construction path), the reviewer-tier transcript analyzer in `api/app.py`
#: (routing it would falsify advisory.py's own "coder/reviewer/planner
#: untouched" docstring claim) — and `agent/advisory.py` itself, the one seam
#: allowed to construct a bare backend. A NEW un-routed utility/supervisor
#: call site shows up as a file OUTSIDE this set and fails loudly.
_NOT_ROUTED_ALLOWLIST = frozenset({
    "no_human/agent/advisory.py",
    "no_human/agent/backend.py",
    "no_human/agent/backend_check.py",
    "no_human/api/app.py",
    "no_human/cli/commands.py",
    "no_human/core/orchestrator.py",
    "no_human/eval/funnel_eval.py",
    "no_human/eval/judge.py",
    "no_human/intake/evaluator.py",
    "no_human/review/reviewer.py",
})


def _bare_construction_files() -> set[str]:
    """AST-based, not regex-over-source: only real `ClaudeBackend(...)` call
    expressions count, so a docstring/comment mentioning the name (several
    files have one) is never mistaken for a construction site."""
    root = Path(no_human.__file__).resolve().parent
    hits: set[str] = set()
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = (
                    func.id if isinstance(func, ast.Name)
                    else getattr(func, "attr", None)
                )
                if name == "ClaudeBackend":
                    hits.add(str(path.relative_to(root.parent)).replace("\\", "/"))
                    break
    return hits


def test_no_utility_call_site_constructs_a_bare_backend():
    assert _bare_construction_files() == set(_NOT_ROUTED_ALLOWLIST)


# --------------------------------------------------------------------------- #
# AC2 — split_proposal.py:107 (the reviewer's named blocker)                  #
# --------------------------------------------------------------------------- #

async def test_split_proposal_uses_the_toolless_seam(monkeypatch):
    from no_human.intake import split_proposal

    calls = []

    def _recorder(model, *, role):
        calls.append(role)
        return _FakeAdvisory("SPLIT_JSON_START\n[]\nSPLIT_JSON_END")

    monkeypatch.setattr("no_human.agent.advisory.advisory_backend", _recorder)
    # Regression guard: if the call site reverted to a bare ClaudeBackend(...),
    # this raiser would fire instead of the recorder above.
    monkeypatch.setattr(
        "no_human.agent.claude_backend.ClaudeBackend",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("bare ClaudeBackend constructed on an advisory path")),
    )

    task = SimpleNamespace(title="t", description="d", acceptance_criteria=["ac"])
    await split_proposal.generate_split_proposal(task)

    assert calls == ["intake"]


def test_split_proposal_request_carries_no_tool_payload(tmp_path):
    """`generate_split_proposal` now routes through `role="intake"` — the
    same seam `intake/evaluator.py` uses — so its options carry no tool
    payload. Built directly off `advisory_backend`, exactly the shape
    `split_proposal.py` constructs at its call site."""
    transport_mod = pytest.importorskip(
        "claude_agent_sdk._internal.transport.subprocess_cli",
        reason="private SDK transport path moved",
    )
    SubprocessCLITransport = transport_mod.SubprocessCLITransport

    opts = advisory_backend("claude-haiku-4-5", role="intake")._options(tmp_path, 1)
    assert opts.tools == []
    assert opts.allowed_tools == []
    assert opts.system_prompt == ADVISORY_ROLE_PROMPTS["intake"]

    transport = SubprocessCLITransport("prompt", opts)
    transport._cli_path = "/usr/bin/true"
    cmd = transport._build_command()
    assert "--tools" in cmd
    assert cmd[cmd.index("--tools") + 1] == ""
    assert "--allowedTools" not in cmd
    assert not any("tool_use" in c or "tool_call" in c for c in cmd)


# --------------------------------------------------------------------------- #
# AC2 — cli/commands.py:4301 (learnings-curate) and :4424 (correction         #
# distillation)                                                               #
# --------------------------------------------------------------------------- #

def _no_bare_backend_guard(monkeypatch):
    monkeypatch.setattr(
        "no_human.agent.claude_backend.ClaudeBackend",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("bare ClaudeBackend constructed on an advisory path")),
    )


def test_learnings_curate_llm_uses_the_toolless_seam(tmp_path, monkeypatch):
    from no_human.cli import commands as cmd_mod

    calls = []

    def _recorder(model, *, role):
        calls.append(role)
        return _FakeAdvisory("")

    monkeypatch.setattr("no_human.agent.advisory.advisory_backend", _recorder)
    _no_bare_backend_guard(monkeypatch)
    monkeypatch.setattr(
        "no_human.cli.commands._bootstrap",
        lambda **kw: (
            SimpleNamespace(utility_model="claude-haiku-4-5",
                             db_path=tmp_path / "nh.db"),
            None,
        ),
    )

    async def _fake_curate(store, llm_call=None, apply=False):
        await llm_call("prompt")
        return SimpleNamespace(
            duplicates_archived=0, llm_archive_proposed=[],
            llm_consolidate_proposed=[], llm_applied=False,
        )

    monkeypatch.setattr("no_human.learning.curator.curate", _fake_curate)

    result = CliRunner().invoke(cmd_mod.cli, ["learnings-curate"])
    assert result.exit_code == 0, result.output
    assert calls == ["distill"]


def test_correction_distillation_uses_the_toolless_seam(tmp_path, monkeypatch):
    from no_human.cli import commands as cmd_mod
    from no_human.learning import queue as queue_mod

    calls = []

    def _recorder(model, *, role):
        calls.append(role)
        return _FakeAdvisory("a distilled lesson")

    monkeypatch.setattr("no_human.agent.advisory.advisory_backend", _recorder)
    _no_bare_backend_guard(monkeypatch)
    monkeypatch.setattr(
        "no_human.cli.commands._bootstrap",
        lambda **kw: (
            SimpleNamespace(utility_model="claude-haiku-4-5",
                             db_path=tmp_path / "nh.db"),
            None,
        ),
    )

    async def _fake_harvest(self, *, project=None, distill=None, note=None):
        await distill("prompt")
        return []

    monkeypatch.setattr(
        queue_mod.LearningQueue, "harvest_supervisor_corrections", _fake_harvest,
    )

    result = CliRunner().invoke(cmd_mod.cli, ["learnings", "--harvest"])
    assert result.exit_code == 0, result.output
    assert calls == ["distill"]

    # The fail-open `except` must still return "" when the seam itself raises
    # — a distillation failure degrades the lesson to the verbatim
    # corrections, it never aborts the harvest.
    def _raiser(model, *, role):
        raise RuntimeError("no credential configured")

    monkeypatch.setattr("no_human.agent.advisory.advisory_backend", _raiser)
    captured = {}

    async def _fake_harvest_raising(self, *, project=None, distill=None, note=None):
        captured["result"] = await distill("prompt")
        return []

    monkeypatch.setattr(
        queue_mod.LearningQueue, "harvest_supervisor_corrections",
        _fake_harvest_raising,
    )

    result2 = CliRunner().invoke(cmd_mod.cli, ["learnings", "--harvest"])
    assert result2.exit_code == 0, result2.output
    assert captured["result"] == ""


# --------------------------------------------------------------------------- #
# AC2 — orchestrator.py:8164 `_distill_large_chunks` (shipped untested on     #
# the branch)                                                                 #
# --------------------------------------------------------------------------- #

async def test_distill_large_chunks_uses_the_toolless_seam(store, tmp_path, monkeypatch):
    from no_human.context.base import ContextChunk
    from no_human.core.orchestrator import Orchestrator
    from no_human.core.task import Task
    from no_human.notify.slack import SlackNotifier

    class _NoBackend:
        async def run(self, *a, **k):  # pragma: no cover
            raise AssertionError("the coder backend should not run")

    calls = []

    def _recorder(model, *, role):
        calls.append(role)
        return _FakeAdvisory("a short summary")

    monkeypatch.setattr("no_human.core.orchestrator.advisory_backend", _recorder)
    monkeypatch.setattr(
        "no_human.core.orchestrator.ClaudeBackend",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("bare ClaudeBackend constructed on an advisory path")),
    )

    from no_human.config import load_config
    cfg = load_config(tmp_path / "config.yaml")
    cfg.data.setdefault("planning", {})["enabled"] = False
    orch = Orchestrator(store, cfg.data, _NoBackend(), SlackNotifier(None))

    task = Task.new("fix it", repo_path=str(tmp_path))
    chunk = ContextChunk(source="codebase", title="big file", content="x" * 2500)

    await orch._distill_large_chunks([chunk], task)

    assert calls == ["distill"]
    assert chunk.content == "[distilled] a short summary"


# --------------------------------------------------------------------------- #
# AC2 — orchestrator.py:1993 `_distill_attempt_state` diff-compression path   #
# (a NEW utility call site that landed on main after the branch this task     #
# refiles was written — same class as the four above, so it routes too)      #
# --------------------------------------------------------------------------- #

async def test_attempt_state_distill_uses_the_toolless_seam(store, tmp_path, monkeypatch):
    from no_human.core.orchestrator import Orchestrator
    from no_human.core.task import Task
    from no_human.notify.slack import SlackNotifier

    class _NoBackend:
        async def run(self, *a, **k):  # pragma: no cover
            raise AssertionError("the coder backend should not run")

    class _FakeRepo:
        def diff(self, base):
            # Longer than _ATTEMPT_DIFF_DISTILL_THRESHOLD so the compression
            # branch (the one that constructs a backend) actually runs.
            return "diff --git a/f.py b/f.py\n" + ("+x" * 8000)

        def changed_files(self, ref):
            return ["f.py"]

    calls = []

    def _recorder(model, *, role):
        calls.append(role)
        return _FakeAdvisory("f.py: added a helper")

    monkeypatch.setattr("no_human.core.orchestrator.advisory_backend", _recorder)
    # Regression guard: if the call site reverted to a bare ClaudeBackend(...),
    # this raiser would fire instead of the recorder above.
    monkeypatch.setattr(
        "no_human.core.orchestrator.ClaudeBackend",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("bare ClaudeBackend constructed on an advisory path")),
    )

    from no_human.config import load_config
    cfg = load_config(tmp_path / "config.yaml")
    cfg.data.setdefault("planning", {})["enabled"] = False
    orch = Orchestrator(store, cfg.data, _NoBackend(), SlackNotifier(None))

    task = Task.new("fix it", repo_path=str(tmp_path))
    task.context = {"attempt_log": ["attempt 1: failed"]}

    await orch._distill_attempt_state(task, _FakeRepo(), 2, "main")

    assert calls == ["distill"]
    assert task.context.get("distilled_state_attempt") == 2


# --------------------------------------------------------------------------- #
# AC2 — every role's serialized request carries no harness payload field      #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("role", ROLES)
def test_serialized_request_has_no_harness_payload(tmp_path, role):
    opts = advisory_backend("claude-haiku-4-5", role=role)._options(tmp_path, 1)
    assert opts.tools == []
    assert opts.allowed_tools == []
    assert opts.mcp_servers == {}
    assert opts.agents is None
    assert opts.skills is None
    assert opts.setting_sources == []

    transport_mod = pytest.importorskip(
        "claude_agent_sdk._internal.transport.subprocess_cli",
        reason="private SDK transport path moved",
    )
    SubprocessCLITransport = transport_mod.SubprocessCLITransport
    transport = SubprocessCLITransport("prompt", opts)
    transport._cli_path = "/usr/bin/true"
    joined = " ".join(transport._build_command())
    assert "tool_use" not in joined
    assert "tool_call" not in joined


# --------------------------------------------------------------------------- #
# AC4 — verification-only: every shipped zero-harness claim maps to a named,  #
# collected test (never regexing the prose itself as the proof)               #
# --------------------------------------------------------------------------- #

def test_advisory_docstring_claims_are_tested():
    import inspect
    import re

    from no_human.agent import advisory as advisory_mod
    from no_human.agent import claude_backend as backend_mod

    advisory_doc = advisory_mod.__doc__ or ""
    # Comments wrap across lines (e.g. "byte-\n# byte unchanged"), so compare
    # against whitespace-collapsed, comment-marker-stripped source rather
    # than a raw substring — this is a claim-presence check, not a source-
    # text regex guard on behavior.
    backend_init_src = re.sub(
        r"-\s+", "-",
        re.sub(
            r"\s+", " ",
            inspect.getsource(backend_mod.ClaudeBackend.__init__).replace("#", ""),
        ),
    )

    claim_to_test = {
        "no tool schemas serialized": "test_advisory_options_carry_no_tools",
        "grill_spec": "test_grill_spec_keeps_its_tools",
    }
    for claim, test_name in claim_to_test.items():
        assert claim in advisory_doc, f"claim {claim!r} no longer in advisory.py's docstring"
        fn = globals().get(test_name)
        assert callable(fn), f"claim {claim!r} names a missing test: {test_name}"

    coder_claim = "coder/reviewer/planner path is byte-for-byte unchanged"
    assert coder_claim in backend_init_src
    fn = globals().get("test_serialized_cli_command_has_no_tool_definitions")
    assert callable(fn), f"claim {coder_claim!r} names a missing test"
