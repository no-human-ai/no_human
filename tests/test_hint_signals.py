"""Hint-only signal families the pre-flight card sees but tier GATES do not.

`compute_tier`'s own signal set and thresholds are frozen — the MoA fan-out
and extended-thinking gates in orchestrator.py read it directly, and a new
signal folded straight into that set would silently re-tier live tasks (a
prior attempt measured ~19.4% of a 62-task population moving into `complex`).
`hint_signals` is a SEPARATE, pure function: legacy signals plus hint-only
families (today just `multi_family`) that only the card in feasibility.py
consumes.
"""

import logging

from no_human.core import feasibility
from no_human.core.complexity import compute_tier, hint_signals
from no_human.core.feasibility import estimate_feasibility
from no_human.core.task import Task, TaskStatus


def _task(**kw):
    defaults = dict(id="aaa", source="test", title="t",
                     status=TaskStatus.PENDING, acceptance_criteria=[])
    defaults.update(kw)
    return Task(**defaults)


_PLAIN_BULLETS = "\n".join([
    "- Update the README with new install steps",
    "- Add a changelog entry",
    "- Bump the version number",
    "- Run the test suite",
    "- Tag the release",
])

_FIX_ISSUE_PART_BULLETS = "\n".join([
    "- Fix 1: correct the off-by-one in the parser",
    "- Issue 2: handle the empty-input edge case",
    "- Part 3: update the docs to match",
])

_ORDINAL_BULLETS = "\n".join([
    "1. First, do the thing",
    "2. Second, do the other thing",
])

_SINGLE_FIX_BULLET = "\n".join([
    "- Fix the typo in the README",
    "- Update the changelog",
    "- Bump the version",
])


# ------------------------------------------------------------- tier freeze --
#
# Derived by running CURRENT MAIN's compute_tier (pre-edit) on each case
# below and pasting its literal (tier, signals) output.
_FROZEN = [
    (
        "bare quick task naming a prose file",
        dict(title="Update notes/positioning.md", description="tiny docs tweak",
             acceptance_criteria=["done"]),
        "trivial", [],
    ),
    (
        "tiny task naming no file",
        dict(title="Add a small helper", description="small helper",
             acceptance_criteria=["works"]),
        "simple", [],
    ),
    (
        "plain 5-bullet description",
        dict(title="Release prep", description=_PLAIN_BULLETS,
             acceptance_criteria=["done"]),
        "simple", [],
    ),
    (
        "2500-char description",
        dict(description="x" * 2500),
        "standard", ["long-spec"],
    ),
    (
        "long-spec + linked_repos",
        dict(description="x" * 2500, linked_repos=["/other/repo"]),
        "complex", ["multi-repo", "long-spec"],
    ),
    (
        "decompose-verdict context",
        dict(context={"eval_result": {"verdict": "decompose"}}),
        "complex", ["ambiguous-spec"],
    ),
    (
        "original_criteria of 6",
        dict(context={"original_criteria": [f"c{i}" for i in range(6)]}),
        "standard", ["many-criteria"],
    ),
    (
        "spec.files_to_change of 5 + plan_size_warning",
        dict(context={"spec": {"files_to_change": ["a", "b", "c", "d", "e"]},
                       "plan_size_warning": True}),
        "complex", ["many-files", "large-plan"],
    ),
    (
        "ordinal/Fix-Issue-Part bulleted description",
        dict(title="Multi-part fix", description=_FIX_ISSUE_PART_BULLETS,
             acceptance_criteria=["done"]),
        "simple", [],
    ),
]


def test_compute_tier_output_is_frozen():
    assert len(_FROZEN) >= 8
    for name, kwargs, expected_tier, expected_signals in _FROZEN:
        t = _task(**kwargs)
        tier, signals = compute_tier(t)
        assert (tier, signals) == (expected_tier, expected_signals), name


# ------------------------------------------------------------- multi_family --

def test_multi_family_fires_on_three_families():
    t = _task(description="x" * 2500, linked_repos=["/other/repo"],
               context={"eval_result": {"verdict": "clarify"}})
    signals, reasons = hint_signals(t)
    assert "multi_family" in signals
    assert reasons
    joined = " ".join(reasons)
    assert "repos" in joined and "spec-length" in joined and "ambiguity" in joined


def test_multi_family_fires_on_fix_issue_part_lead_ins():
    t = _task(title="Multi-part fix", description=_FIX_ISSUE_PART_BULLETS,
               acceptance_criteria=["done"])
    signals, reasons = hint_signals(t)
    assert "multi_family" in signals
    assert reasons


def test_multi_family_fires_on_ordinal_lead_ins():
    t = _task(title="numbered plan", description=_ORDINAL_BULLETS,
               acceptance_criteria=["done"])
    signals, reasons = hint_signals(t)
    assert "multi_family" in signals
    assert reasons


def test_multi_family_silent_on_a_plain_bullet_list():
    t = _task(title="Release prep", description=_PLAIN_BULLETS,
               acceptance_criteria=["done"])
    signals, reasons = hint_signals(t)
    assert signals == []
    assert reasons == []


def test_a_single_fix_bullet_is_not_multi_family():
    t = _task(title="single fix", description=_SINGLE_FIX_BULLET,
               acceptance_criteria=["done"])
    signals, reasons = hint_signals(t)
    assert "multi_family" not in signals
    assert reasons == []


# ---------------------------------------------------------------- off-switch --

def test_hint_signals_disabled_returns_only_legacy_signals():
    t = _task(description="x" * 2500, linked_repos=["/other/repo"],
               context={"eval_result": {"verdict": "clarify"}})
    _, legacy = compute_tier(t)
    signals, reasons = hint_signals(
        t, config={"feasibility": {"hint_signals_enabled": False}})
    assert signals == legacy
    assert reasons == []


def test_hint_signals_enabled_by_default():
    t = _task(description="x" * 2500, linked_repos=["/other/repo"],
               context={"eval_result": {"verdict": "clarify"}})
    signals_none, _ = hint_signals(t, config=None)
    assert "multi_family" in signals_none
    signals_empty, _ = hint_signals(t, config={})
    assert "multi_family" in signals_empty


# --------------------------------------------------------- feasibility card --

def test_a_broken_task_logs_and_falls_back(monkeypatch, caplog):
    def _boom(*a, **kw):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(feasibility, "hint_signals", _boom)
    t = _task(context={"eval_result": {"verdict": "decompose"}})
    with caplog.at_level(logging.WARNING, logger="no_human.core.feasibility"):
        hint = estimate_feasibility(t, {"complex": 38})
    assert hint is not None
    assert hint.band == feasibility.BAND_TOO_LARGE
    assert hint.signals == ["ambiguous-spec"]
    assert hint.hint_reasons == []
    assert any("hint signals skipped" in r.message for r in caplog.records)


def test_the_card_carries_the_hint_only_signals():
    t = _task(description="x" * 2500, linked_repos=["/other/repo"],
               context={"eval_result": {"verdict": "decompose"}})
    hint = estimate_feasibility(t, {"complex": 38})
    assert hint is not None
    assert "multi_family" in hint.signals
