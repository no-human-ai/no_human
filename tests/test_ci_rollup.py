"""Tests for `no_human.vcs.ci_rollup` — reducing a delivered PR's GitHub
check rollup (as normalized by `pr_watcher.default_pr_checks`) into a single
CI state + failing-check names for `core.merge_policy`."""

from __future__ import annotations

import pytest

from no_human.vcs import ci_rollup


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
    state, names = ci_rollup.aggregate_rollup(checks)
    assert state == "failure"
    assert names == ("File inventory",)


def test_only_required_checks_are_scoped_when_required_flags_present():
    checks = [
        _check("Optional flaky check", "fail", required=False),
        _check("Build", "pass", required=True),
        _check("Test", "pass", required=True),
    ]
    state, names = ci_rollup.aggregate_rollup(checks)
    assert state == "success"
    assert names == ()


def test_required_failure_is_named_even_with_a_passing_optional_check():
    checks = [
        _check("File inventory", "fail", required=True),
        _check("Optional check", "pass", required=False),
    ]
    state, names = ci_rollup.aggregate_rollup(checks)
    assert state == "failure"
    assert names == ("File inventory",)


def test_all_pass_is_success():
    checks = [_check("Build", "pass"), _check("Test", "pass")]
    state, names = ci_rollup.aggregate_rollup(checks)
    assert state == "success"
    assert names == ()


def test_pending_when_any_pending():
    checks = [_check("Build", "pass"), _check("Test", "pending")]
    state, names = ci_rollup.aggregate_rollup(checks)
    assert state == "pending"
    assert names == ()


@pytest.mark.parametrize("checks", [[], None])
def test_empty_rollup_is_none(checks):
    state, names = ci_rollup.aggregate_rollup(checks)
    assert state is None
    assert names == ()


def test_unknown_conclusion_vocabulary_is_never_success():
    checks = [{"name": "Weird", "status": "???", "link": "", "required": False}]
    state, names = ci_rollup.aggregate_rollup(checks)
    assert state is None
    assert names == ()


def test_multiple_failing_checks_are_named_sorted():
    checks = [
        _check("Zebra check", "fail"),
        _check("Alpha check", "fail"),
        _check("Build", "pass"),
    ]
    state, names = ci_rollup.aggregate_rollup(checks)
    assert state == "failure"
    assert names == ("Alpha check", "Zebra check")


# --------------------------------------------------------------------- #
# fetch_ci_rollup — GitHub-gated, degrades gracefully
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_fetch_degrades_to_none_when_checks_raise(monkeypatch):
    async def _boom(ref):
        raise RuntimeError("gh exploded")

    monkeypatch.setattr(ci_rollup.pr_watcher, "default_pr_checks", _boom)
    state, names = await ci_rollup.fetch_ci_rollup(
        "https://github.com/acme/widgets/pull/122")
    assert state is None
    assert names == ()


@pytest.mark.asyncio
async def test_fetch_aggregates_the_checks_from_default_pr_checks(monkeypatch):
    async def _fake(ref):
        return [_check("File inventory", "fail", required=True)]

    monkeypatch.setattr(ci_rollup.pr_watcher, "default_pr_checks", _fake)
    state, names = await ci_rollup.fetch_ci_rollup(
        "https://github.com/acme/widgets/pull/122")
    assert state == "failure"
    assert names == ("File inventory",)


@pytest.mark.asyncio
async def test_non_github_url_is_none():
    state, names = await ci_rollup.fetch_ci_rollup(
        "https://gitlab.com/acme/widgets/-/merge_requests/9")
    assert state is None
    assert names == ()


@pytest.mark.asyncio
async def test_unparseable_url_is_none():
    state, names = await ci_rollup.fetch_ci_rollup("not-a-url")
    assert state is None
    assert names == ()
