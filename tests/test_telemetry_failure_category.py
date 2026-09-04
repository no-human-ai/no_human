"""`task_failed.reason_category` — the closed-enum failure category.

Pins `telemetry.failure_reason_category` as a pure LOOKUP (never a parser of
free text), pins `Orchestrator._telemetry_hook`'s "failed" branch to only
ever send an enum member no matter what free-form text a blocker or an
explicit category carries, statically pins every `self._fail(...)` call
site in orchestrator.py to pass an enum value (not a computed/free string),
and pins that `_raise_blocker`'s max-attempts / tamper-block / review-
stagnation terminal sites reach `task_failed` telemetry in PRODUCTION even
though those routes land on ESCALATED, not FAILED (routing is unchanged —
this is a telemetry-only additional emission).
"""
from __future__ import annotations

import ast
import asyncio
from pathlib import Path

from no_human import telemetry
from no_human.blockers import Blocker, BlockerCategory
from no_human.config import load_config
from no_human.core.db import Store
from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task, TaskStatus
from no_human.notify.slack import SlackNotifier

ORCHESTRATOR_PATH = (
    Path(__file__).resolve().parent.parent
    / "src" / "no_human" / "core" / "orchestrator.py"
)


class _Stub:  # only what the hook touches
    config: dict = {}
    _telemetry_hook = Orchestrator._telemetry_hook


class _DeadBackend:
    async def run(self, *a, **k):  # pragma: no cover
        raise AssertionError("backend should not run in this test")


async def _new_orch_and_task(store, tmp_path, title="a task"):
    cfg = load_config(tmp_path / "config.yaml")
    orch = Orchestrator(store, cfg.data, _DeadBackend(), SlackNotifier(None))
    t = Task.new(title, repo_path="/r")
    t.acceptance_criteria = ["keep me"]
    await store.create_task(t)
    return orch, t


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


# ------------------- escalated-route production emission ------------------- #
#
# max_attempts / tamper_blocked / review_failed all route to ESCALATED, not
# FAILED (taxonomy unchanged by this ticket — see BlockerCategory routing).
# Without an ADDITIONAL telemetry-only emission inside `_raise_blocker`,
# `reason_category` for these three categories is wired but never actually
# reaches `telemetry.record` in production, because `_telemetry_hook`'s
# "failed" branch only fires for the app-level `kind == "failed"` event. Each
# test below drives the real `_raise_blocker` funnel and asserts BOTH halves:
# routing is untouched (status stays ESCALATED) AND telemetry fired anyway.

def test_escalated_max_attempts_still_emits_task_failed_telemetry(
        tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(
        telemetry, "record",
        lambda kind, config=None, **props: sent.append((kind, props)))

    async def _run():
        async with Store(tmp_path / "nh.db") as store:
            orch, t = await _new_orch_and_task(store, tmp_path)
            blocker = Blocker(
                category=BlockerCategory.NOVEL_UNKNOWN, transient=False,
                confidence=0.4, goal="do the thing",
                root_cause_hypothesis="max_attempts (5) reached",
                evidence="ev", question="what now?",
            )
            await orch._raise_blocker(
                t, blocker, escalate_now=True, fail_category="max_attempts")
            got = await store.get_task(t.id)
            assert got.status is TaskStatus.ESCALATED, got.status
    asyncio.run(_run())

    failed = [props for kind, props in sent if kind == "task_failed"]
    assert len(failed) == 1, sent
    assert failed[0] == {"category": "failed", "reason_category": "max_attempts"}


def test_escalated_tamper_blocked_still_emits_task_failed_telemetry(
        tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(
        telemetry, "record",
        lambda kind, config=None, **props: sent.append((kind, props)))

    async def _run():
        async with Store(tmp_path / "nh.db") as store:
            orch, t = await _new_orch_and_task(store, tmp_path)
            blocker = Blocker(
                category=BlockerCategory.AMBIGUITY, transient=False,
                confidence=0.4, goal="do the thing",
                root_cause_hypothesis="test tampering detected",
                evidence="ev", question="restore or accept?",
            )
            await orch._raise_blocker(
                t, blocker, escalate_now=True, fail_category="tamper_blocked")
            got = await store.get_task(t.id)
            assert got.status is TaskStatus.ESCALATED, got.status
    asyncio.run(_run())

    failed = [props for kind, props in sent if kind == "task_failed"]
    assert len(failed) == 1, sent
    assert failed[0] == {"category": "failed", "reason_category": "tamper_blocked"}


def test_escalated_review_stagnation_still_emits_task_failed_telemetry(
        tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(
        telemetry, "record",
        lambda kind, config=None, **props: sent.append((kind, props)))

    async def _run():
        async with Store(tmp_path / "nh.db") as store:
            orch, t = await _new_orch_and_task(store, tmp_path)
            blocker = Blocker(
                category=BlockerCategory.STAGNATION, transient=False,
                confidence=0.9, goal="do the thing",
                root_cause_hypothesis="review pass rate stuck",
                evidence="ev", question="revise, decompose, or investigate?",
            )
            await orch._raise_blocker(
                t, blocker, escalate_now=True, fail_category="review_failed")
            got = await store.get_task(t.id)
            assert got.status is TaskStatus.ESCALATED, got.status
    asyncio.run(_run())

    failed = [props for kind, props in sent if kind == "task_failed"]
    assert len(failed) == 1, sent
    assert failed[0] == {"category": "failed", "reason_category": "review_failed"}


def test_escalated_route_without_fail_category_sends_no_task_failed(
        tmp_path, monkeypatch):
    """An ordinary escalation (no `fail_category`) must NOT gain a new
    `task_failed` telemetry event — the additional emission is opt-in per
    call site, never a blanket "every escalation is a failure"."""
    sent = []
    monkeypatch.setattr(
        telemetry, "record",
        lambda kind, config=None, **props: sent.append((kind, props)))

    async def _run():
        async with Store(tmp_path / "nh.db") as store:
            orch, t = await _new_orch_and_task(store, tmp_path)
            blocker = Blocker(
                category=BlockerCategory.MISSING_ACCESS, transient=False,
                confidence=0.9, goal="do the thing",
                root_cause_hypothesis="need creds", evidence="ev",
                question="which creds?",
            )
            await orch._raise_blocker(t, blocker, escalate_now=True)
    asyncio.run(_run())

    assert not [props for kind, props in sent if kind == "task_failed"], sent


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


def test_every_raise_blocker_fail_category_is_an_enum_literal():
    """Every `self._raise_blocker(..., fail_category=...)` call in
    orchestrator.py must pass a literal string that is a member of
    `FAILURE_REASON_CATEGORIES` — guards future call sites from smuggling a
    computed/free-form value into the additional ESCALATED-route emission."""
    tree = ast.parse(ORCHESTRATOR_PATH.read_text())
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_raise_blocker"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "self"
    ]
    assert calls, "no self._raise_blocker(...) call sites found"
    with_fail_category = [
        call for call in calls
        if any(k.arg == "fail_category" for k in call.keywords)
    ]
    assert len(with_fail_category) >= 3, (
        "expected at least the max_attempts/tamper_blocked/review_failed "
        f"call sites to pass fail_category=, found {len(with_fail_category)}")
    for call in with_fail_category:
        kw = next(k for k in call.keywords if k.arg == "fail_category")
        assert isinstance(kw.value, ast.Constant) and isinstance(
            kw.value.value, str), (
            f"self._raise_blocker(...) at line {call.lineno} fail_category= "
            f"must be a literal string, not a computed value")
        assert kw.value.value in telemetry.FAILURE_REASON_CATEGORIES, (
            f"self._raise_blocker(...) at line {call.lineno} fail_category="
            f"{kw.value.value!r} is not in FAILURE_REASON_CATEGORIES")
