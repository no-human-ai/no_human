"""`task_failed.reason_category` — the closed-enum failure category.

Pins `telemetry.failure_reason_category` as a pure LOOKUP (never a parser of
free text), pins `Orchestrator._telemetry_hook`'s "failed" branch to only
ever send an enum member no matter what free-form text a blocker or an
explicit category carries, and statically pins every `self._fail(...)` call
site in orchestrator.py to pass an enum value (not a computed/free string).
"""
from __future__ import annotations

import ast
from pathlib import Path

from no_human import telemetry
from no_human.core.orchestrator import Orchestrator

ORCHESTRATOR_PATH = (
    Path(__file__).resolve().parent.parent
    / "src" / "no_human" / "core" / "orchestrator.py"
)


class _Stub:  # only what the hook touches
    config: dict = {}
    _telemetry_hook = Orchestrator._telemetry_hook


# --------------------------- pure mapping ---------------------------------- #

def test_mapping_budget_exhausted():
    assert telemetry.failure_reason_category(
        None, "BUDGET_EXHAUSTED") == "budget_exhausted"


def test_mapping_infra():
    assert telemetry.failure_reason_category(None, "TRANSIENT_INFRA") == "infra"
    assert telemetry.failure_reason_category(None, "QUOTA") == "infra"
    # explicit arg, the `_fail(..., reason_category="infra")` path
    assert telemetry.failure_reason_category("infra", None) == "infra"


def test_mapping_max_attempts():
    # explicit beats the blocker-category table
    assert telemetry.failure_reason_category(
        "max_attempts", "NOVEL_UNKNOWN") == "max_attempts"


def test_mapping_review_failed():
    assert telemetry.failure_reason_category("review_failed", None) == "review_failed"
    assert telemetry.failure_reason_category(None, "STAGNATION") == "review_failed"


def test_unknown_and_garbage_map_to_other():
    assert telemetry.failure_reason_category(None, None) == "other"
    assert telemetry.failure_reason_category(None, "AMBIGUITY") == "other"
    assert telemetry.failure_reason_category(None, "NOT_A_CATEGORY") == "other"
    # a free-form string in the `explicit` slot must never leak through —
    # it isn't a member of FAILURE_REASON_CATEGORIES, so it falls through to
    # the blocker-category table (also unrecognised here) and lands on "other"
    assert telemetry.failure_reason_category(
        "budget exhausted; /Users/x/repo", None) == "other"


# ------------------------------ hook level ---------------------------------- #

def test_hook_sends_enum_from_blocker_meta(monkeypatch):
    sent = []
    monkeypatch.setattr(
        telemetry, "record",
        lambda kind, config=None, **props: sent.append((kind, props)))
    stub = _Stub()
    stub._telemetry_hook("failed", {
        "status": "failed",
        "blocker": {
            "category": "BUDGET_EXHAUSTED",
            "root_cause_hypothesis": (
                "lifetime budget exhausted: 3 attempts on repo /Users/x/secret"),
        },
    })
    [(kind, props)] = sent
    assert kind == "task_failed"
    assert props == {"category": "failed", "reason_category": "budget_exhausted"}


def test_hook_sends_enum_from_explicit_meta(monkeypatch):
    sent = []
    monkeypatch.setattr(
        telemetry, "record",
        lambda kind, config=None, **props: sent.append((kind, props)))
    stub = _Stub()
    stub._telemetry_hook("failed", {"status": "failed", "reason_category": "infra"})
    [(kind, props)] = sent
    assert props["reason_category"] == "infra"


def test_hook_clamps_a_forged_free_form_reason(monkeypatch):
    """Even if something upstream ever put free text into
    `meta["reason_category"]`, the hook must clamp it to "other" — never
    raise, never ship it."""
    sent = []
    monkeypatch.setattr(
        telemetry, "record",
        lambda kind, config=None, **props: sent.append((kind, props)))
    stub = _Stub()
    stub._telemetry_hook("failed", {
        "status": "failed",
        "reason_category": "could not create the task worktree at /Users/x/secret",
    })
    [(kind, props)] = sent
    assert props["reason_category"] == "other"


# --------------------------- static call-site pin ---------------------------- #

def test_every_fail_call_site_passes_an_enum_value():
    """Every `self._fail(...)` call in orchestrator.py must pass
    `reason_category` as a literal string that is a member of
    `FAILURE_REASON_CATEGORIES` — never a computed/free-form value."""
    tree = ast.parse(ORCHESTRATOR_PATH.read_text())
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_fail"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "self"
    ]
    assert calls, "no self._fail(...) call sites found — check the AST walk"
    for call in calls:
        kw = next(
            (k for k in call.keywords if k.arg == "reason_category"), None)
        assert kw is not None, (
            f"self._fail(...) at line {call.lineno} is missing "
            f"reason_category=")
        assert isinstance(kw.value, ast.Constant) and isinstance(
            kw.value.value, str), (
            f"self._fail(...) at line {call.lineno} reason_category= "
            f"must be a literal string, not a computed value")
        assert kw.value.value in telemetry.FAILURE_REASON_CATEGORIES, (
            f"self._fail(...) at line {call.lineno} reason_category="
            f"{kw.value.value!r} is not in FAILURE_REASON_CATEGORIES")
