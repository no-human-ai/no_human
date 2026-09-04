"""The repro gate hard-fails a Python bugfix that ships no
`.no_human/repro_tests.json` (orchestrator._run_attempt treats the "waived"
verdict as blocking as "fail"), but no prompt ever named the file or the
consequence — so the coder learned the requirement by burning a whole attempt
(db9bdeb7: 37 turns / ~19k tokens on attempt #1, once per Python bugfix on any
repo without a manifest). These tests pin the instruction to what the gate
actually enforces, in both directions."""

from __future__ import annotations


from no_human.config import load_config
from no_human.core.orchestrator import Orchestrator
from no_human.core.prompt_blocks import build_rules_block
from no_human.core.task import Task
from no_human.notify.slack import SlackNotifier
from no_human.testing.repro_gate import MANIFEST

from .test_e2e_orchestrator import FakeBackend


def _orch(store, tmp_path, mode=None):
    cfg = load_config(tmp_path / "c.yaml")
    if mode is not None:
        cfg.data.setdefault("repro_gate", {})["mode"] = mode
    return Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None))


def _bugfix(tmp_path):
    t = Task.new("fix the off-by-one", repo_path=str(tmp_path))
    t.kind = "bugfix"
    return t


async def test_bugfix_directive_names_the_manifest_path_schema_and_consequence(
        store, tmp_path):
    d = _orch(store, tmp_path)._kind_directive(_bugfix(tmp_path))
    # The exact path the gate reads. Bound to the gate's own constant so the
    # prompt tracks the gate; note this assertion alone is NOT drift-proof
    # (both sides derive from MANIFEST) — test_every_prompt_surface_tracks_the
    # _manifest_constant below is what actually catches a hard-coded literal.
    assert MANIFEST in d
    # The schema, so the coder does not have to guess the key.
    assert '{"tests":' in d.replace('{"tests": ', '{"tests":')
    # Both directions the gate proves.
    low = d.lower()
    assert "fail on the unfixed code" in low
    assert "pass with your fix" in low
    # The consequence — the part whose absence cost a whole attempt.
    assert "failed" in low and "sent back" in low
    # And it must land in THIS attempt, not a later one.
    assert "this same attempt" in low


async def test_bugfix_directive_is_scoped_to_python_changes(store, tmp_path):
    """A JS/CSS-only bugfix cannot have a pytest repro. The gate enforces only
    when the change touched .py (orchestrator: ``changed_py``); demanding a
    manifest unconditionally is the 2026-07-11 regression that made every web
    bugfix uncompletable."""
    d = _orch(store, tmp_path)._kind_directive(_bugfix(tmp_path))
    assert "WHEN YOUR FIX CHANGES PYTHON" in d


async def test_required_mode_does_not_understate_the_scope(store, tmp_path):
    """``required`` enforces for EVERY change, so the Python-only clause would
    be a lie there — a JS-only bugfix is failed for the missing manifest too."""
    d = _orch(store, tmp_path, mode="required")._kind_directive(_bugfix(tmp_path))
    assert MANIFEST in d
    assert "REQUIRED FOR THIS FIX" in d
    assert "WHEN YOUR FIX CHANGES PYTHON" not in d


async def test_unknown_mode_reads_as_advisory(store, tmp_path):
    """Nothing validates repro_gate.mode, so a config typo is reachable. The
    gate treats anything that is neither "off" nor "required" as advisory, and
    the prompt must degrade the same way rather than promising required's
    wider scope."""
    d = _orch(store, tmp_path, mode="banana")._kind_directive(_bugfix(tmp_path))
    assert "WHEN YOUR FIX CHANGES PYTHON" in d


async def test_no_manifest_demand_when_the_gate_is_off(store, tmp_path):
    """mode=off never runs the gate, so asking for the file would request
    something nothing reads."""
    d = _orch(store, tmp_path, mode="off")._kind_directive(_bugfix(tmp_path))
    assert MANIFEST not in d
    # The rest of the bugfix directive survives.
    assert "root cause" in d.lower()


async def test_other_kinds_do_not_get_the_bugfix_manifest_directive(
        store, tmp_path):
    orch = _orch(store, tmp_path)
    t = Task.new("add a feature", repo_path=str(tmp_path))
    t.kind = "feature"
    assert "REPRO MANIFEST — REQUIRED" not in orch._kind_directive(t)


async def test_unknown_kind_stays_empty(store, tmp_path):
    """The directive is only ever appended to a real one — an unknown kind must
    not become a manifest-only instruction with no task context."""
    orch = _orch(store, tmp_path)
    t = Task.new("something", repo_path=str(tmp_path))
    t.kind = "not_a_kind"
    assert orch._kind_directive(t) == ""


def test_rules_block_states_the_consequence_only_when_required():
    """Under mode=required the hard-fail applies to EVERY kind, so the shared
    rules block is where that consequence belongs."""
    req = build_rules_block("pytest", "", None, repro_mode="required")
    assert MANIFEST in req
    assert "FAILS this" in req

    adv = build_rules_block("pytest", "", None, repro_mode="advisory")
    assert MANIFEST in adv
    # Advisory mode blocks bugfixes only — the bugfix directive carries that;
    # a blanket "fails every attempt" here would be false for other kinds.
    assert "FAILS this" not in adv


def test_required_bullet_does_not_excuse_non_pytest_repos():
    """The bullet opened with "if this repo's tests run with pytest" in every
    mode — but required fails a non-pytest repo's attempt too, so that clause
    excused exactly what the gate punishes."""
    req = build_rules_block("pytest", "", None, repro_mode="required")
    assert "if this repo's tests run with pytest" not in req
    assert "required" in req
    # The conditional is correct for advisory, where the gate only bites a
    # bugfix that edited Python.
    adv = build_rules_block("pytest", "", None, repro_mode="advisory")
    assert "if this repo's tests run with pytest" in adv


def test_rules_block_drops_the_manifest_bullet_when_the_gate_is_off():
    off = build_rules_block("pytest", "", None, repro_mode="off")
    assert MANIFEST not in off
    # Also reject a bare basename: `MANIFEST not in off` alone would pass on a
    # mention without the .no_human/ prefix, which is still a manifest demand.
    assert MANIFEST.rsplit("/", 1)[-1] not in off
    # Neighbouring rules are untouched.
    assert "NEVER weaken, skip, or delete a test" in off


def test_rules_block_default_matches_the_orchestrator_default():
    """The default must equal config's ``repro_gate.mode`` default (advisory),
    or a caller that omits the argument silently changes the prompt."""
    assert build_rules_block("pytest", "", None) == build_rules_block(
        "pytest", "", None, repro_mode="advisory")


def test_every_prompt_surface_tracks_the_manifest_constant(monkeypatch):
    """Interpolating MANIFEST in one surface and hard-coding the literal in
    another is exactly the drift this file exists to catch: the #179 review
    proved the kind-directive assertion could not detect a moved path because
    prompt_blocks.py still carried the literal. Repoint the constant and every
    surface must follow."""
    import no_human.core.orchestrator as orch
    import no_human.core.prompt_blocks as pb

    moved = ".no_human/repro_tests_v2.json"
    monkeypatch.setattr(pb, "REPRO_MANIFEST", moved)
    monkeypatch.setattr(orch, "REPRO_MANIFEST", moved)
    for mode in ("advisory", "required"):
        block = pb.build_rules_block("pytest", "", None, repro_mode=mode)
        assert moved in block, f"{mode} bullet still hard-codes a literal path"
        assert MANIFEST + " —" not in block
    # The send-back message is the highest-stakes surface: the coder reads it
    # AFTER the gate already cost it an attempt, so a stale path here makes an
    # already-blocked coder write the wrong file on every retry.
    msg = orch.repro_send_back_message("repro gate waived: no manifest")
    assert moved in msg, "send-back message still hard-codes a literal path"
    assert MANIFEST + " " not in msg
