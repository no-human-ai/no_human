"""Tests for ci.ghe_checkruns — the GHE check-runs reader (IMPROVEMENT_PLAN D).

Tests the pure conversion logic (check_runs_to_result) with fixture data
so no ``gh`` CLI or network access is needed.
"""

import asyncio

import pytest

from no_human.ci.ghe_checkruns import (
    GHECheckRunsCI,
    check_runs_to_result,
    merge_ci_results,
    statuses_to_result,
)
from no_human.ci.base import CIResult, PipelineStatus


def _run(name, status="completed", conclusion="success"):
    """Minimal check-run fixture."""
    return {
        "name": name,
        "status": status,
        "conclusion": conclusion if status == "completed" else None,
        "html_url": f"https://code.example.com/check/{name}",
    }


def test_all_success():
    runs = [_run("lint"), _run("unit-tests"), _run("integration")]
    result = check_runs_to_result(runs, ref="abc123")
    assert result.status == PipelineStatus.SUCCESS
    assert result.passed
    assert len(result.jobs) == 3


def test_one_failure():
    runs = [_run("lint"), _run("unit-tests", conclusion="failure"), _run("integration")]
    result = check_runs_to_result(runs, ref="abc123")
    assert result.status == PipelineStatus.FAILED
    assert not result.passed
    assert any(j.failure_reason == "failure" for j in result.jobs)


def test_in_progress():
    runs = [_run("lint"), _run("unit-tests", status="in_progress")]
    result = check_runs_to_result(runs, ref="abc123")
    assert result.status == PipelineStatus.RUNNING
    assert not result.status.is_terminal


def test_queued():
    runs = [_run("lint", status="queued")]
    result = check_runs_to_result(runs, ref="abc123")
    assert result.status == PipelineStatus.RUNNING


@pytest.mark.parametrize("status", ["waiting", "requested", "pending"])
def test_github_actions_in_progress_statuses_are_running(status):
    # GitHub Actions can set waiting/requested/pending on a check run that has
    # not completed (e.g. a job held at a deployment environment protection
    # rule, or awaiting a concurrency slot). These must read as RUNNING so the
    # poll loop keeps waiting, not as an indeterminate UNKNOWN that trigger()
    # returns immediately as a no-verdict.
    result = check_runs_to_result([_run("deploy", status=status)], ref="abc123")
    assert result.status == PipelineStatus.RUNNING
    assert not result.status.is_terminal


def test_empty_runs():
    result = check_runs_to_result([], ref="abc123")
    assert result.status == PipelineStatus.UNKNOWN


def test_cancelled():
    runs = [_run("lint", conclusion="cancelled")]
    result = check_runs_to_result(runs, ref="abc123")
    # Cancelled is a non-success outcome; the aggregate pipeline status is FAILED.
    assert result.status == PipelineStatus.FAILED
    assert not result.passed


def test_neutral_is_success():
    runs = [_run("advisory-check", conclusion="neutral")]
    result = check_runs_to_result(runs, ref="abc123")
    assert result.status == PipelineStatus.SUCCESS


def test_mixed_success_and_neutral():
    runs = [_run("lint"), _run("advisory", conclusion="neutral")]
    result = check_runs_to_result(runs, ref="abc123")
    assert result.status == PipelineStatus.SUCCESS


def test_timed_out_is_failure():
    runs = [_run("slow-test", conclusion="timed_out")]
    result = check_runs_to_result(runs, ref="abc123")
    assert result.status == PipelineStatus.FAILED


# --- indeterminate conclusions must NEVER read as green (safety invariant) --- #

@pytest.mark.parametrize("conclusion", ["action_required", "stale",
                                        "startup_failure", "", None])
def test_indeterminate_conclusion_is_not_green(conclusion):
    # action_required (awaiting manual approval), stale, startup_failure (the
    # workflow never ran the tests), and null/unknown conclusions must map to
    # UNKNOWN — NOT SUCCESS. Calling any of these green is false confidence.
    runs = [{"name": "ci", "status": "completed", "conclusion": conclusion,
             "html_url": ""}]
    result = check_runs_to_result(runs, ref="abc123")
    assert result.status == PipelineStatus.UNKNOWN
    assert not result.passed


def test_success_plus_indeterminate_is_not_green():
    # One green check does not clear an indeterminate sibling.
    runs = [_run("unit"), {"name": "deploy-gate", "status": "completed",
                           "conclusion": "action_required", "html_url": ""}]
    result = check_runs_to_result(runs, ref="abc123")
    assert result.status == PipelineStatus.UNKNOWN
    assert not result.passed


def test_skipped_only_is_still_success():
    # SKIPPED = didn't need to run (path filter); it is clean, not indeterminate.
    runs = [_run("optional", conclusion="skipped")]
    result = check_runs_to_result(runs, ref="abc123")
    assert result.status == PipelineStatus.SUCCESS


def test_failure_still_wins_over_indeterminate():
    runs = [{"name": "a", "status": "completed", "conclusion": "action_required",
             "html_url": ""}, _run("b", conclusion="failure")]
    result = check_runs_to_result(runs, ref="abc123")
    assert result.status == PipelineStatus.FAILED


def test_jobs_have_web_urls():
    runs = [_run("lint")]
    result = check_runs_to_result(runs, ref="abc123")
    assert result.jobs[0].web_url == "https://code.example.com/check/lint"


def test_ci_from_config_ghe_checkruns():
    from no_human.ci import ci_from_config
    cfg = {
        "ci": {
            "enabled": True,
            "backend": "ghe_checkruns",
            "repo": "org/my-repo",
            "hostname": "code.example.com",
        }
    }
    runner = ci_from_config(cfg)
    assert runner is not None
    assert runner.name == "ghe_checkruns"
    assert runner.repo == "org/my-repo"
    assert runner.hostname == "code.example.com"


def test_ci_from_config_ghe_no_repo_raises():
    """Was `is None`, i.e. indistinguishable from CI being switched off (KI-5).
    Only `enabled: false` may answer None."""
    from no_human.ci import CIMisconfigured, ci_from_config
    cfg = {"ci": {"enabled": True, "backend": "ghe_checkruns"}}
    with pytest.raises(CIMisconfigured, match="ci.repo"):
        ci_from_config(cfg)
    assert ci_from_config({**cfg, "ci": {**cfg["ci"], "enabled": False}}) is None


# --- CI.1b: Commit Status API (statuses_to_result + merge_ci_results) ---------

def _status_payload(state, entries):
    return {"state": state, "statuses": entries, "total_count": len(entries)}


def _s(context, state, url=""):
    return {"context": context, "state": state, "target_url": url}


def test_statuses_all_success():
    data = _status_payload("success", [_s("ci/jenkins", "success"),
                                       _s("ci/lint", "success")])
    result = statuses_to_result(data, ref="abc123")
    assert result.status == PipelineStatus.SUCCESS
    assert [j.name for j in result.jobs] == ["ci/jenkins", "ci/lint"]


def test_statuses_failure_is_failed():
    data = _status_payload("failure", [_s("ci/jenkins", "failure")])
    result = statuses_to_result(data, ref="abc123")
    assert result.status == PipelineStatus.FAILED
    assert result.failed


def test_statuses_error_state_is_failed_never_green():
    data = _status_payload("error", [_s("ci/jenkins", "error")])
    assert statuses_to_result(data).status == PipelineStatus.FAILED


def test_statuses_pending_with_entries_is_running():
    data = _status_payload("pending", [_s("ci/jenkins", "pending")])
    assert statuses_to_result(data).status == PipelineStatus.RUNNING


def test_statuses_empty_is_unknown_no_jobs_not_running():
    # GitHub reports state="pending" for a commit with ZERO statuses. That is
    # "no CI reported", NOT running — must be UNKNOWN with no jobs so the merge
    # treats it as "no opinion" and drops it.
    data = _status_payload("pending", [])
    result = statuses_to_result(data)
    assert result.status == PipelineStatus.UNKNOWN
    assert result.jobs == []


def test_statuses_unmappable_combined_state_with_entries_is_unknown_with_jobs():
    # An unexpected combined state, but there ARE statuses -> indeterminate, not
    # empty: UNKNOWN but WITH jobs so it is never silently dropped.
    data = _status_payload("weird", [_s("ci/jenkins", "success")])
    result = statuses_to_result(data)
    assert result.status == PipelineStatus.UNKNOWN
    assert len(result.jobs) == 1


def _empty():
    return CIResult(pipeline_id="", pipeline_url="", status=PipelineStatus.UNKNOWN)


def _ok(name="job"):
    from no_human.ci.base import JobResult
    return CIResult(pipeline_id="r", pipeline_url="", status=PipelineStatus.SUCCESS,
                    jobs=[JobResult(name=name, status="success")])


def _fail(name="job"):
    from no_human.ci.base import JobResult
    return CIResult(pipeline_id="r", pipeline_url="", status=PipelineStatus.FAILED,
                    jobs=[JobResult(name=name, status="failed")])


def test_merge_both_empty_is_unknown_never_green():
    m = merge_ci_results(_empty(), _empty(), ref="abc")
    assert m.status == PipelineStatus.UNKNOWN
    assert not m.passed


def test_merge_green_plus_empty_is_green():
    assert merge_ci_results(_ok(), _empty()).status == PipelineStatus.SUCCESS
    assert merge_ci_results(_empty(), _ok()).status == PipelineStatus.SUCCESS


def test_merge_failure_wins_over_success():
    assert merge_ci_results(_ok(), _fail()).status == PipelineStatus.FAILED
    assert merge_ci_results(_fail(), _ok()).status == PipelineStatus.FAILED


def test_merge_running_beats_success_but_not_failure():
    running = CIResult(pipeline_id="r", pipeline_url="",
                       status=PipelineStatus.RUNNING,
                       jobs=[__import__("no_human.ci.base", fromlist=["JobResult"]).JobResult(name="j", status="running")])
    assert merge_ci_results(_ok(), running).status == PipelineStatus.RUNNING
    assert merge_ci_results(_fail(), running).status == PipelineStatus.FAILED


@pytest.mark.parametrize("state", [PipelineStatus.MANUAL, PipelineStatus.PENDING,
                                   PipelineStatus.SKIPPED])
def test_merge_non_success_terminalish_state_never_greens(state):
    # LOW-1 guard: SUCCESS requires EVERY surviving source to be affirmatively
    # SUCCESS. A surviving source in any other state (MANUAL awaiting approval,
    # PENDING, a bare SKIPPED) must merge to UNKNOWN, never green — even paired
    # with a genuinely-green sibling.
    from no_human.ci.base import JobResult
    odd = CIResult(pipeline_id="r", pipeline_url="", status=state,
                   jobs=[JobResult(name="gate", status=state.value)])
    assert merge_ci_results(_ok(), odd).status == PipelineStatus.UNKNOWN
    assert not merge_ci_results(_ok(), odd).passed


def test_merge_flagged_unknown_is_not_dropped():
    # An access wall on one source (UNKNOWN + no jobs but access_failure) is NOT
    # "empty" — it must keep the merge UNKNOWN and propagate the flag, so a green
    # sibling never vouches for a commit we couldn't fully read.
    walled = CIResult(pipeline_id="r", pipeline_url="",
                      status=PipelineStatus.UNKNOWN,
                      access_failure=True, access_env_key="GH_TOKEN")
    m = merge_ci_results(_ok(), walled)
    assert m.status == PipelineStatus.UNKNOWN
    assert m.access_failure and m.access_env_key == "GH_TOKEN"
    assert not m.passed


def test_ghe_backend_merges_both_surfaces():
    # GHECheckRunsCI reads check-runs AND commit-statuses and merges them: a
    # failing external commit-status sinks green check-runs.
    async def fake_runs(repo, ref, *, hostname=""):
        return [_run("build", conclusion="success")]

    async def fake_statuses(repo, ref, *, hostname=""):
        return _status_payload("failure", [_s("ci/jenkins", "failure")])

    ci = GHECheckRunsCI("org/repo", fetch_runs=fake_runs, fetch_statuses=fake_statuses)
    result = asyncio.run(ci.check_status("deadbeef"))
    assert result.failed
    assert not result.passed


# --- error classification: a failed read must never raise out of GHECheckRunsCI --- #

def _raise(exc):
    async def _fetch(repo, ref, *, hostname=""):
        raise exc
    return _fetch


async def _ok_statuses(repo, ref, *, hostname=""):
    return _status_payload("success", [])


async def _ok_runs(repo, ref, *, hostname=""):
    return []


def test_ghe_auth_error_on_checkruns_is_access_failure():
    ci = GHECheckRunsCI(
        "org/repo",
        fetch_runs=_raise(RuntimeError("gh api failed (1): HTTP 401: Bad credentials")),
        fetch_statuses=_ok_statuses,
    )
    result = asyncio.run(ci.check_status("deadbeef"))
    assert result.access_failure
    assert result.access_env_key == "GH_TOKEN"
    assert result.status == PipelineStatus.UNKNOWN
    assert not result.passed
    assert not result.infra_failure


def test_ghe_auth_error_on_statuses_is_access_failure():
    ci = GHECheckRunsCI(
        "org/repo",
        fetch_runs=_ok_runs,
        fetch_statuses=_raise(
            RuntimeError("gh api failed (1): HTTP 403: Resource not accessible by integration")
        ),
    )
    result = asyncio.run(ci.check_status("deadbeef"))
    assert result.access_failure
    assert result.access_env_key == "GH_TOKEN"
    assert result.status == PipelineStatus.UNKNOWN
    assert not result.passed
    assert not result.infra_failure


def test_ghe_transient_error_is_infra_failure():
    ci = GHECheckRunsCI(
        "org/repo",
        fetch_runs=_raise(RuntimeError("gh api failed (1): HTTP 502 Bad Gateway")),
        fetch_statuses=_ok_statuses,
    )
    result = asyncio.run(ci.check_status("deadbeef"))
    assert result.infra_failure
    assert not result.access_failure
    assert not result.passed
    assert result.status == PipelineStatus.UNKNOWN


def test_ghe_successful_read_still_merges():
    async def fake_runs(repo, ref, *, hostname=""):
        return [_run("build", conclusion="success")]

    async def fake_statuses(repo, ref, *, hostname=""):
        return _status_payload("success", [_s("ci/jenkins", "success")])

    ci = GHECheckRunsCI("org/repo", fetch_runs=fake_runs, fetch_statuses=fake_statuses)
    result = asyncio.run(ci.check_status("deadbeef"))
    assert result.passed
    assert result.status == PipelineStatus.SUCCESS
    assert not result.access_failure
    assert not result.infra_failure


def test_ghe_and_gha_share_one_classifier():
    from no_human.ci.github_actions import _is_auth_error as g
    from no_human.ci.ghe_checkruns import _is_auth_error as h
    assert g is h
