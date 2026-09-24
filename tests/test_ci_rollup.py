"""Tests for `no_human.vcs.ci_rollup` — reducing a delivered PR's GitHub
check rollup (as normalized by `pr_watcher.default_pr_checks_and_required`)
into a single CI state + failing-check names + missing-required-context
names for `core.merge_policy`."""

from __future__ import annotations

import pytest

from no_human.core import merge_policy
from no_human.vcs import ci_rollup

SIX_REQUIRED = (
    "CLA ledger",
    "Container images build",
    "File inventory",
    "Python",
    "Web board",
    "Wheel carries the board",
)


def _check(name, status, required=False):
    return {"name": name, "status": status, "link": "", "required": required}


# --------------------------------------------------------------------- #
# aggregate_rollup — pure
# --------------------------------------------------------------------- #


def test_failure_wins_and_names_failing_checks():
    checks = [
        _check("File inventory", "fail"),
        _check("Build", "pass"),
        _check("Lint", "pending"),
    ]
    state, names, missing = ci_rollup.aggregate_rollup(checks)
    assert state == "failure"
    assert names == ("File inventory",)
    assert missing == ()


def test_only_required_checks_are_scoped_when_required_flags_present():
    checks = [
        _check("Optional flaky check", "fail", required=False),
        _check("Build", "pass", required=True),
        _check("Test", "pass", required=True),
    ]
    state, names, missing = ci_rollup.aggregate_rollup(checks)
    assert state == "success"
    assert names == ()
    assert missing == ()


def test_required_failure_is_named_even_with_a_passing_optional_check():
    checks = [
        _check("File inventory", "fail", required=True),
        _check("Optional check", "pass", required=False),
    ]
    state, names, missing = ci_rollup.aggregate_rollup(checks)
    assert state == "failure"
    assert names == ("File inventory",)
    assert missing == ()


def test_all_pass_is_success():
    checks = [_check("Build", "pass"), _check("Test", "pass")]
    state, names, missing = ci_rollup.aggregate_rollup(checks)
    assert state == "success"
    assert names == ()
    assert missing == ()


def test_pending_when_any_pending():
    checks = [_check("Build", "pass"), _check("Test", "pending")]
    state, names, missing = ci_rollup.aggregate_rollup(checks)
    assert state == "pending"
    assert names == ()
    assert missing == ()


@pytest.mark.parametrize("checks", [[], None])
def test_empty_rollup_is_none(checks):
    state, names, missing = ci_rollup.aggregate_rollup(checks)
    assert state is None
    assert names == ()
    assert missing == ()


def test_unknown_conclusion_vocabulary_is_never_success():
    checks = [{"name": "Weird", "status": "???", "link": "", "required": False}]
    state, names, missing = ci_rollup.aggregate_rollup(checks)
    assert state is None
    assert names == ()
    assert missing == ()


def test_multiple_failing_checks_are_named_sorted():
    checks = [
        _check("Zebra check", "fail"),
        _check("Alpha check", "fail"),
        _check("Build", "pass"),
    ]
    state, names, missing = ci_rollup.aggregate_rollup(checks)
    assert state == "failure"
    assert names == ("Alpha check", "Zebra check")
    assert missing == ()


# --------------------------------------------------------------------- #
# aggregate_rollup — the missing-required discrimination (this ticket)
# --------------------------------------------------------------------- #


def test_pr541_shape_with_required_names_blocks_the_gate():
    """PR#541's actual shape: only a non-required, always-green job ran and
    none of the six branch-protection-required contexts ever posted a
    status. `aggregate_rollup` alone still reports `state == "success"` —
    the discrimination rides in `missing_required`, not the state
    vocabulary (a new state string would leak into `core/pr_evidence.py`'s
    renderer, which is out of scope here). It is `_check_ci` that turns
    that into NOT READY."""
    checks = [{"name": "CLA nudge", "status": "pass", "required": False}]
    state, failing, missing = ci_rollup.aggregate_rollup(checks, SIX_REQUIRED)
    assert state == "success"
    assert failing == ()
    assert missing == SIX_REQUIRED  # already sorted

    facts = merge_policy.GateFacts(
        review_passed=True, ci_state=state, ci_missing_required=missing,
    )
    ready, detail = merge_policy._check_ci(facts, "success_or_unknown")
    assert ready is False
    assert "required checks never ran" in detail
    assert "CLA ledger" in detail


@pytest.mark.parametrize("required_names", [None, [], ()])
def test_no_required_ness_data_still_tolerated(required_names):
    """Empty/absent `required_names` means NO required-ness data was
    obtained — indistinguishable, from this function's inputs alone,
    between a failed `gh pr checks --required` lookup and a repo with
    genuinely zero required checks. The discriminator is the required-name
    SET, never the per-check `required=False` flags (which fail open on
    that same lookup failure) — so this must tolerate exactly like today."""
    checks = [{"name": "CLA nudge", "status": "pass", "required": False}]
    state, failing, missing = ci_rollup.aggregate_rollup(checks, required_names)
    assert state == "success"
    assert missing == ()

    facts = merge_policy.GateFacts(
        review_passed=True, ci_state=state, ci_missing_required=missing,
    )
    ready, detail = merge_policy._check_ci(facts, "success_or_unknown")
    assert (ready, detail) == (True, "ci: success")


def test_empty_rollup_with_known_required_names_names_them_all():
    state, failing, missing = ci_rollup.aggregate_rollup([], SIX_REQUIRED)
    assert state is None
    assert failing == ()
    assert missing == SIX_REQUIRED

    facts = merge_policy.GateFacts(
        review_passed=True, ci_state=state, ci_missing_required=missing,
    )
    ready, _detail = merge_policy._check_ci(facts, "success_or_unknown")
    assert ready is False


def test_required_contexts_present_and_passing_is_ready():
    checks = [_check(name, "pass", required=True) for name in SIX_REQUIRED]
    state, failing, missing = ci_rollup.aggregate_rollup(checks, SIX_REQUIRED)
    assert state == "success"
    assert failing == ()
    assert missing == ()

    facts = merge_policy.GateFacts(
        review_passed=True, ci_state=state, ci_missing_required=missing,
    )
    ready, detail = merge_policy._check_ci(facts, "success_or_unknown")
    assert (ready, detail) == (True, "ci: success")


def test_failing_required_check_still_not_ready():
    checks = [_check(name, "pass", required=True) for name in SIX_REQUIRED]
    checks[3] = _check("Python", "fail", required=True)
    state, failing, missing = ci_rollup.aggregate_rollup(checks, SIX_REQUIRED)
    assert state == "failure"
    assert failing == ("Python",)
    assert missing == ()

    facts = merge_policy.GateFacts(
        review_passed=True, ci_state=state, ci_failed_checks=failing,
        ci_missing_required=missing,
    )
    ready, detail = merge_policy._check_ci(facts, "success_or_unknown")
    assert ready is False
    assert "Python" in detail


# --------------------------------------------------------------------- #
# fetch_ci_rollup — GitHub-gated, degrades gracefully
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_fetch_degrades_to_none_when_checks_raise(monkeypatch):
    async def _boom(ref):
        raise RuntimeError("gh exploded")

    monkeypatch.setattr(ci_rollup.pr_watcher, "default_pr_checks_and_required", _boom)
    state, names, missing = await ci_rollup.fetch_ci_rollup(
        "https://github.com/acme/widgets/pull/122")
    assert state is None
    assert names == ()
    assert missing == ()


@pytest.mark.asyncio
async def test_fetch_aggregates_the_checks_from_default_pr_checks(monkeypatch):
    async def _fake(ref):
        return [_check("File inventory", "fail", required=True)], ()

    monkeypatch.setattr(ci_rollup.pr_watcher, "default_pr_checks_and_required", _fake)
    state, names, missing = await ci_rollup.fetch_ci_rollup(
        "https://github.com/acme/widgets/pull/122")
    assert state == "failure"
    assert names == ("File inventory",)
    assert missing == ()


@pytest.mark.asyncio
async def test_non_github_url_is_none():
    state, names, missing = await ci_rollup.fetch_ci_rollup(
        "https://gitlab.com/acme/widgets/-/merge_requests/9")
    assert state is None
    assert names == ()
    assert missing == ()


@pytest.mark.asyncio
async def test_unparseable_url_is_none():
    state, names, missing = await ci_rollup.fetch_ci_rollup("not-a-url")
    assert state is None
    assert names == ()
    assert missing == ()


@pytest.mark.parametrize("fake_kind", ["raise", "gitlab_url", "bad_url", "empty_list"])
@pytest.mark.asyncio
async def test_fetch_degradations_never_report_missing_required(monkeypatch, fake_kind):
    """Every documented degradation path — a `gh`/network failure, a
    non-GitHub ref, an unparseable ref, or an empty rollup — must return
    `missing_required == ()`. An outage can never manufacture missing-
    required evidence (`aggregate_rollup` only fires on a *non-empty*
    required-name set, and every degradation here yields `()` for it)."""
    if fake_kind == "raise":
        async def _fake(ref):
            raise RuntimeError("gh exploded")

        monkeypatch.setattr(
            ci_rollup.pr_watcher, "default_pr_checks_and_required", _fake)
        url = "https://github.com/acme/widgets/pull/122"
    elif fake_kind == "gitlab_url":
        url = "https://gitlab.com/acme/widgets/-/merge_requests/9"
    elif fake_kind == "bad_url":
        url = "not-a-url"
    else:
        async def _fake(ref):
            return [], ()

        monkeypatch.setattr(
            ci_rollup.pr_watcher, "default_pr_checks_and_required", _fake)
        url = "https://github.com/acme/widgets/pull/122"

    state, failing, missing = await ci_rollup.fetch_ci_rollup(url)
    assert missing == ()
    assert failing == ()
    assert state is None
