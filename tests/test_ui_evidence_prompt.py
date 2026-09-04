"""PROMPT sub-area (no-human-67): ``ui_evidence_block`` and its gating inside
``Orchestrator._build_implement_prompt``.

Two properties matter, mirroring the L4 style in ``tests/test_brain_invariants.py``:

1. OFF BY DEFAULT — a profile with no ``ui_evidence`` opinion (or an explicit
   ``enabled: False``) produces a prompt BYTE-IDENTICAL to one built with no
   ``ui_evidence`` attribute at all (i.e. as if this feature did not exist).
   Anything less and every unconfigured repo's coder prompt silently changed
   the day this field was added.
2. GATED ON UI WORK — with the feature ON, the block appears only when the
   PLAN's declared FILES TO CHANGE/CREATE match one of the profile's
   ``ui_paths`` globs (``Orchestrator._build_implement_prompt``'s own
   ``parse_plan_files`` + ``fnmatch`` gate, ANY-match). No plan, no matching
   file, or the feature off: the block must be absent.

This renders REAL prompts via a real (bare-constructed) ``Orchestrator`` —
never reads source — the same harness shape as
``tests/test_export_gate_rule.py``'s ``_prompt_orchestrator``.
"""
from __future__ import annotations

import copy
from pathlib import Path

from no_human.config import DEFAULT_CONFIG, Config
from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task, TaskStatus
from no_human.profile import ProjectProfile

_MARKER = "UI EVIDENCE"

_PLAN_WITH_UI_FILE = """## FILES TO CHANGE/CREATE
- web/src/App.jsx — add the new widget

## APPROACH
Do the thing.

## TEST PLAN
Run the tests.

## OUT OF SCOPE
Nothing else.
"""

_PLAN_WITHOUT_UI_FILE = """## FILES TO CHANGE/CREATE
- src/no_human/core/widget.py — add the new widget

## APPROACH
Do the thing.

## TEST PLAN
Run the tests.

## OUT OF SCOPE
Nothing else.
"""


def _prompt_orchestrator():
    """A real Orchestrator, far enough constructed to build a coder prompt
    (mirrors tests/test_export_gate_rule.py's ``_prompt_orchestrator``)."""
    data = copy.deepcopy(DEFAULT_CONFIG)
    config = Config(data=data, path=Path("/nonexistent/config.yaml"))
    orch = Orchestrator.__new__(Orchestrator)
    orch.config = config
    orch.ci_runner = None
    orch._active_profile = None
    orch._active_playbook = None
    orch._repo_hints = None
    orch._active_memories = []
    orch._brain_watermark = None
    return orch


def _prompt(orch, *, plan: str = "", repo_path: str = "/tmp/repo") -> str:
    task = Task(
        id="ui-evidence-test", source="test", title="Add a widget",
        status=TaskStatus.IMPLEMENTING, description="A description",
        acceptance_criteria=["it works"], repo_path=repo_path,
    )
    if plan:
        task.context = {"plan": plan}
    return orch._build_implement_prompt(task, work_dir=repo_path)


def _profile(**ui_overrides) -> ProjectProfile:
    prof = ProjectProfile(repo_path="/tmp/repo")
    if ui_overrides:
        prof.ui_evidence.update(ui_overrides)
    return prof


def test_L4_no_profile_opinion_matches_no_ui_evidence_attribute_at_all():
    """BYTE-IDENTICAL: a profile that never mentions ``ui_evidence`` (the
    shape every profile had before this field existed, simulated here by
    deleting the attribute after construction) must render exactly the same
    prompt as today's default-constructed ``ProjectProfile`` (``enabled:
    False`` out of the box)."""
    orch = _prompt_orchestrator()
    default_prof = _profile()
    orch._active_profile = default_prof
    with_default_field = _prompt(orch, plan=_PLAN_WITH_UI_FILE)

    pre_feature_prof = _profile()
    del pre_feature_prof.ui_evidence  # simulate "field never existed"
    orch._active_profile = pre_feature_prof
    as_if_deleted = _prompt(orch, plan=_PLAN_WITH_UI_FILE)

    assert with_default_field == as_if_deleted
    assert _MARKER not in with_default_field


def test_L4_disabled_is_byte_identical_regardless_of_matching_plan():
    """OFF BY DEFAULT, second half: even a plan that clearly touches UI files
    must not change the prompt by a single byte while ``enabled`` is False."""
    orch = _prompt_orchestrator()
    orch._active_profile = _profile()  # enabled: False (the field default)
    no_plan = _prompt(orch)
    with_ui_plan = _prompt(orch, plan=_PLAN_WITH_UI_FILE)
    with_non_ui_plan = _prompt(orch, plan=_PLAN_WITHOUT_UI_FILE)

    assert _MARKER not in no_plan
    assert _MARKER not in with_ui_plan
    assert _MARKER not in with_non_ui_plan


def test_enabled_and_plan_declares_a_matching_ui_path_shows_the_block():
    orch = _prompt_orchestrator()
    orch._active_profile = _profile(
        enabled=True, start_cmd="npm run dev", base_url="http://127.0.0.1:5173",
    )
    prompt = _prompt(orch, plan=_PLAN_WITH_UI_FILE)
    assert _MARKER in prompt
    assert "http://127.0.0.1:5173" in prompt


def test_enabled_but_no_plan_hides_the_block():
    """No plan ⇒ no declared files ⇒ nothing to match against, even ON."""
    orch = _prompt_orchestrator()
    orch._active_profile = _profile(
        enabled=True, start_cmd="npm run dev", base_url="http://127.0.0.1:5173",
    )
    assert _MARKER not in _prompt(orch)


def test_enabled_but_plan_files_do_not_match_ui_paths_hides_the_block():
    orch = _prompt_orchestrator()
    orch._active_profile = _profile(
        enabled=True, start_cmd="npm run dev", base_url="http://127.0.0.1:5173",
    )
    prompt = _prompt(orch, plan=_PLAN_WITHOUT_UI_FILE)
    assert _MARKER not in prompt


def test_enabled_but_missing_start_cmd_or_base_url_hides_the_block():
    """``ui_evidence_block`` itself refuses to render with nothing runnable —
    covered directly here (not just through the orchestrator gate) since a
    profile can be enabled with incomplete config (e.g. mid-``nh onboard``)."""
    orch = _prompt_orchestrator()
    orch._active_profile = _profile(enabled=True)  # no start_cmd/base_url
    prompt = _prompt(orch, plan=_PLAN_WITH_UI_FILE)
    assert _MARKER not in prompt


def test_part2_wires_the_attempt_time_walk():
    """D1.2 ("part 2") replaces the part-1 absence pin above: the
    attempt-time browser walk now has a real caller. `orchestrator_module.
    ui_evidence` is the module `_maybe_capture_ui_evidence`/
    `_deliver_ui_evidence` call `.run()`/`.default_out_dir()`/`.MANIFEST`
    on — see tests/test_ui_evidence_attempt_hook.py for the behavioral
    (not just hasattr) proof that it actually runs after tests pass."""
    from no_human.core import orchestrator as orchestrator_module
    from no_human.core.orchestrator import Orchestrator

    assert hasattr(Orchestrator, "_maybe_capture_ui_evidence")
    assert hasattr(Orchestrator, "_deliver_ui_evidence")
    assert hasattr(orchestrator_module, "ui_evidence")
    assert hasattr(orchestrator_module.ui_evidence, "dev_server")


def test_block_says_the_harness_boots_the_dev_server():
    """D2 (2026-09-02): with ``start_cmd`` configured, the block must tell the
    coder the harness boots the dev server itself (and stops it afterwards)
    instead of the old unconditional "does NOT start your dev server"
    sentence — the coder no longer needs to leave a server running by hand.
    Both sentences that must survive verbatim regardless of wording (the
    "proving nothing" caveat and the "never blocks the attempt" caveat) must
    still be present."""
    orch = _prompt_orchestrator()
    orch._active_profile = _profile(
        enabled=True, start_cmd="npm run dev", base_url="http://127.0.0.1:5173",
        ready_path="/",
    )
    prompt = _prompt(orch, plan=_PLAN_WITH_UI_FILE)
    assert _MARKER in prompt
    assert (
        "The harness boots your dev server itself (`npm run dev`) "
        "unless one already answers at http://127.0.0.1:5173/, and "
        "stops it when the walk ends"
    ) in prompt
    assert "does NOT start your dev server" not in prompt
    assert "proving nothing beyond what each screenshot shows" in prompt
    assert "either way this never blocks the attempt" in prompt


def test_block_shows_an_example_manifest_and_the_bare_landing_fallback():
    """Default-walk fallback (2026-09-04): the block must show the coder a
    concrete, copy-pasteable manifest example, and must say plainly that
    skipping the file does NOT skip the walk — it degrades to a bare
    landing screenshot from the harness's own default walk, not to
    nothing. Visual proof must not depend on coder compliance."""
    orch = _prompt_orchestrator()
    orch._active_profile = _profile(
        enabled=True, start_cmd="npm run dev", base_url="http://127.0.0.1:5173",
        ready_path="/",
    )
    prompt = _prompt(orch, plan=_PLAN_WITH_UI_FILE)
    assert _MARKER in prompt
    assert '"base_url"' in prompt
    assert '{"shot": "landing"}' in prompt
    assert (
        "Writing no manifest does not skip the walk: the harness ships "
        "only a bare landing screenshot from its own default walk instead "
        "of the one you control"
    ) in prompt
    assert "either way this never blocks the attempt" in prompt
