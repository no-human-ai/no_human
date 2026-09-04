"""CI module: parsers, GitLab trigger/poll logic, orchestrator wiring."""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from no_human.ci.base import CIResult, JobResult, PipelineStatus
from no_human.ci.gitlab import GitLabCI, _is_infra_failure, _parse_trigger_output
from no_human.ci.parser import parse_pytest, parse_results, parse_surefire
from no_human.ci import ci_from_config


# --------------------------------------------------------------------------- #
# Result parsers                                                               #
# --------------------------------------------------------------------------- #

def test_parse_pytest_summary():
    text = "42 passed, 3 failed, 1 error in 12.3s"
    assert parse_pytest(text) == (42, 3, 1)


def test_parse_pytest_no_results():
    assert parse_pytest("no output") == (0, 0, 0)


def test_parse_surefire_single_class():
    text = "Tests run: 5, Failures: 1, Errors: 0, Skipped: 0"
    passed, failed, errors = parse_surefire(text)
    assert passed == 4
    assert failed == 1
    assert errors == 0


def test_parse_surefire_multiple_classes():
    text = (
        "Tests run: 10, Failures: 0, Errors: 0\n"
        "Tests run: 5, Failures: 2, Errors: 1\n"
    )
    passed, failed, errors = parse_surefire(text)
    assert passed == 12   # 15 total - 2 failures - 1 error
    assert failed == 2
    assert errors == 1


def test_parse_results_dispatch():
    pytest_text = "5 passed"
    surefire_text = "Tests run: 5, Failures: 0, Errors: 0"
    assert parse_results(pytest_text, "pytest")[0] == 5
    assert parse_results(surefire_text, "surefire")[0] == 5


# --------------------------------------------------------------------------- #
# Trigger output parsing                                                       #
# --------------------------------------------------------------------------- #

def test_parse_trigger_url():
    text = "Created pipeline https://gitlab.acme.net/group/repo/-/pipelines/12345\n"
    pid, url = _parse_trigger_output(text)
    assert pid == "12345"
    assert "12345" in url


def test_parse_trigger_id_only():
    pid, url = _parse_trigger_output("Pipeline #42 created")
    assert pid == "42"


def test_parse_trigger_no_match():
    pid, url = _parse_trigger_output("Error: not found")
    assert pid == ""
    assert url == ""


# --------------------------------------------------------------------------- #
# Infra failure detection                                                      #
# --------------------------------------------------------------------------- #

def test_infra_failure_all_infra_reasons():
    jobs = [
        JobResult("test", "failed", "runner_system_failure"),
        JobResult("build", "failed", "stuck_or_timeout_failure"),
    ]
    assert _is_infra_failure(jobs) is True


def test_infra_failure_mixed_reasons():
    jobs = [
        JobResult("test", "failed", "runner_system_failure"),
        JobResult("coverage", "failed", None),  # real failure
    ]
    assert _is_infra_failure(jobs) is False


def test_infra_failure_no_failed_jobs():
    jobs = [JobResult("test", "success", None)]
    assert _is_infra_failure(jobs) is False


def test_infra_failure_no_reason_is_real():
    jobs = [JobResult("test", "failed", None)]
    assert _is_infra_failure(jobs) is False


# --------------------------------------------------------------------------- #
# PipelineStatus                                                               #
# --------------------------------------------------------------------------- #

def test_pipeline_status_terminal():
    assert PipelineStatus.SUCCESS.is_terminal
    assert PipelineStatus.FAILED.is_terminal
    assert not PipelineStatus.RUNNING.is_terminal
    assert not PipelineStatus.PENDING.is_terminal


def test_ci_result_summary_pass():
    r = CIResult("123", "https://x/123", PipelineStatus.SUCCESS)
    assert "PASS" in r.summary
    assert "123" in r.summary


def test_ci_result_summary_infra():
    r = CIResult("", "", PipelineStatus.FAILED, infra_failure=True)
    assert "INFRA" in r.summary


# --------------------------------------------------------------------------- #
# GitLabCI with a fake subprocess runner                                       #
# --------------------------------------------------------------------------- #

class FakeRunner:
    """Scripted sequence of responses to CI API calls."""

    def __init__(self, responses: list[str]):
        self._responses = list(responses)
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str]) -> str:
        self.calls.append(cmd)
        if self._responses:
            return self._responses.pop(0)
        return ""


def _make_pipeline_response(status: str) -> str:
    return json.dumps({"id": 99, "status": status, "web_url": "https://x/99"})


def _make_jobs_response(jobs: list[dict]) -> str:
    return json.dumps(jobs)


def test_gitlab_trigger_argv_is_api_post_never_ci_run():
    """Regression guard: the trigger must use the proven `glab api --method
    POST projects/<p>/pipeline --input <json>` form. `glab ci run` defaults to
    gitlab.com (401) and drops variables on gitlab.acme.net."""
    captured = {}

    def fake(cmd):
        captured["cmd"] = cmd
        body_path = cmd[cmd.index("--input") + 1]
        with open(body_path) as f:
            captured["body"] = json.load(f)
        return json.dumps({"id": 99, "web_url": "https://x/-/pipelines/99"})

    ci = GitLabCI("g/p", hostname="gitlab.acme.net", _run_cmd=fake)
    pid, url = ci._trigger("my-branch", {"K": "V"})
    cmd = captured["cmd"]
    assert pid == "99" and url == "https://x/-/pipelines/99"
    assert cmd[1] == "api"
    assert "--method" in cmd and cmd[cmd.index("--method") + 1] == "POST"
    assert cmd[cmd.index("--hostname") + 1] == "gitlab.acme.net"
    assert "projects/g%2Fp/pipeline" in cmd
    assert "ci" not in cmd and "run" not in cmd
    assert not any(a == "--variables" for a in cmd)
    assert captured["body"] == {"ref": "my-branch",
                                "variables": [{"key": "K", "value": "V"}]}


def test_parse_trigger_json_response():
    pid, url = _parse_trigger_output(
        json.dumps({"id": 9876543, "web_url": "https://h/p/-/pipelines/9876543"})
    )
    assert pid == "9876543"
    assert url == "https://h/p/-/pipelines/9876543"


def test_gitlab_ci_success():
    fake = FakeRunner([
        # trigger response (URL fallback parse path)
        "Created pipeline https://gitlab.acme.net/g/p/-/pipelines/99\n",
        # poll 1: running
        _make_pipeline_response("running"),
        # poll 2: success
        _make_pipeline_response("success"),
        # get jobs
        _make_jobs_response([{"name": "test", "status": "success", "failure_reason": None,
                               "web_url": ""}]),
    ])
    ci = GitLabCI("g/p", hostname="gitlab.acme.net", poll_interval=0, _run_cmd=fake)
    result = ci._trigger_and_wait("no-human/branch", {})
    assert result.passed
    assert result.pipeline_id == "99"
    assert not result.infra_failure


def test_gitlab_ci_real_failure():
    fake = FakeRunner([
        "https://gitlab.acme.net/g/p/-/pipelines/5\n",
        _make_pipeline_response("success"),  # status poll returns success
        # ... but then we call jobs and find failures with real reasons
    ])
    # Simulate a pipeline that reports failed with real test failures.
    fake2 = FakeRunner([
        "https://gitlab.acme.net/g/p/-/pipelines/5\n",
        _make_pipeline_response("failed"),
        _make_jobs_response([{"name": "test", "status": "failed",
                               "failure_reason": None, "web_url": ""}]),
    ])
    ci = GitLabCI("g/p", hostname="gitlab.acme.net", poll_interval=0, _run_cmd=fake2)
    result = ci._trigger_and_wait("branch", {})
    assert result.failed
    assert not result.infra_failure


def test_gitlab_ci_infra_failure():
    fake = FakeRunner([
        "https://gitlab.acme.net/g/p/-/pipelines/7\n",
        _make_pipeline_response("failed"),
        _make_jobs_response([{"name": "runner", "status": "failed",
                               "failure_reason": "runner_system_failure",
                               "web_url": ""}]),
    ])
    ci = GitLabCI("g/p", hostname="gitlab.acme.net", poll_interval=0, _run_cmd=fake)
    result = ci._trigger_and_wait("branch", {})
    assert result.failed
    assert result.infra_failure


def test_gitlab_ci_trigger_no_output_is_infra():
    # No pipeline ID in output → infra failure.
    fake = FakeRunner(["Error connecting to GitLab"])
    ci = GitLabCI("g/p", hostname="gitlab.acme.net", poll_interval=0, _run_cmd=fake)
    result = ci._trigger_and_wait("branch", {})
    assert result.infra_failure


async def test_gitlab_ci_infra_retry_succeeds_on_second_try():
    """Infra failure on first attempt, real success on second → passes."""
    call_count = [0]

    def fake_run(cmd):
        call_count[0] += 1
        if "ci" in cmd and "run" in cmd:
            return "https://x/pipelines/1\n"
        # First pipeline poll returns infra failure.
        if call_count[0] <= 3:
            if "pipelines/1" in (cmd[-1] if cmd else ""):
                return json.dumps({"status": "failed"})
            return json.dumps([{"name": "t", "status": "failed",
                                 "failure_reason": "runner_system_failure",
                                 "web_url": ""}])
        # After backoff trigger again.
        if "pipelines/2" in (cmd[-1] if cmd else ""):
            return json.dumps({"status": "success"})
        return json.dumps([{"name": "t", "status": "success",
                             "failure_reason": None, "web_url": ""}])

    # Use a scripted sequence instead — simpler to reason about.
    sequence = [
        # Trigger attempt 1: pipeline 10
        "https://x/pipelines/10\n",
        # Poll: failed
        json.dumps({"id": 10, "status": "failed"}),
        # Jobs: infra
        json.dumps([{"name": "t", "status": "failed",
                      "failure_reason": "runner_system_failure", "web_url": ""}]),
        # Trigger attempt 2 (after backoff): pipeline 11
        "https://x/pipelines/11\n",
        # Poll: success
        json.dumps({"id": 11, "status": "success"}),
        # Jobs: success
        json.dumps([{"name": "t", "status": "success",
                      "failure_reason": None, "web_url": ""}]),
    ]
    fake = FakeRunner(sequence)
    ci = GitLabCI("g/p", hostname="gitlab.acme.net", poll_interval=0,
                  max_infra_retries=1, _run_cmd=fake)

    # Patch asyncio.sleep to be instant in the test
    import unittest.mock
    with unittest.mock.patch("asyncio.sleep", return_value=None):
        result = await ci.trigger("branch", {})

    assert result.passed
    assert not result.infra_failure


async def test_gitlab_ci_infra_exhausted_returns_infra_result():
    """Infra failure on all attempts → CIResult with infra_failure=True."""
    def infra_sequence():
        while True:
            yield "https://x/pipelines/1\n"
            yield json.dumps({"status": "failed"})
            yield json.dumps([{"name": "t", "status": "failed",
                                "failure_reason": "stuck_or_timeout_failure",
                                "web_url": ""}])

    gen = infra_sequence()
    fake = FakeRunner([next(gen) for _ in range(9)])  # enough for 3 attempts
    ci = GitLabCI("g/p", hostname="gitlab.acme.net", poll_interval=0,
                  max_infra_retries=2, _run_cmd=fake)

    import unittest.mock
    with unittest.mock.patch("asyncio.sleep", return_value=None):
        result = await ci.trigger("branch", {})

    assert result.infra_failure


# --------------------------------------------------------------------------- #
# Infra outage on the jobs poll must retry, never bill the coder (audit       #
# top-8 #8): a 502/network wall on `_get_jobs` used to disappear into `[]`,   #
# which `_is_infra_failure` reads as "no failed jobs" — so an outage on a     #
# terminal `failed` pipeline landed on the coder with zero retries.           #
# --------------------------------------------------------------------------- #

async def test_gitlab_502_on_jobs_poll_is_retried_then_classified_infra():
    """A 502 on the jobs endpoint must retry within max_infra_retries and only
    then surface honestly as infra — never a silent pass to the coder."""
    from no_human.ci.gitlab import GitLabInfraError

    calls = []

    def fake(cmd):
        calls.append(cmd)
        if "--method" in cmd:  # trigger POST
            return "https://x/pipelines/1\n"
        if cmd and cmd[-1].endswith("/jobs"):
            raise GitLabInfraError("502 Bad Gateway (HTTP 502)")
        return json.dumps({"status": "failed"})

    ci = GitLabCI("g/p", hostname="gitlab.acme.net", poll_interval=0,
                  max_infra_retries=1, _run_cmd=fake)

    import unittest.mock
    with unittest.mock.patch("asyncio.sleep", return_value=None):
        result = await ci.trigger("branch", {})

    assert result.infra_failure is True
    assert result.passed is False
    trigger_calls = [c for c in calls if "--method" in c]
    assert len(trigger_calls) == 2, (
        f"max_infra_retries=1 should retry once (2 attempts total), got {trigger_calls}")


async def test_gitlab_real_pipeline_failure_is_still_the_coders_red():
    """A genuine script_failure must NOT be swallowed into a retry — the fix
    must not turn a real red build into an infra blip."""
    calls = []

    def fake(cmd):
        calls.append(cmd)
        if "--method" in cmd:  # trigger POST
            return "https://x/pipelines/1\n"
        if cmd and cmd[-1].endswith("/jobs"):
            return json.dumps([{"name": "t", "status": "failed",
                                 "failure_reason": "script_failure", "web_url": ""}])
        return json.dumps({"status": "failed"})

    ci = GitLabCI("g/p", hostname="gitlab.acme.net", poll_interval=0,
                  max_infra_retries=2, _run_cmd=fake)

    import unittest.mock
    with unittest.mock.patch("asyncio.sleep", return_value=None):
        result = await ci.trigger("branch", {})

    assert result.infra_failure is False
    assert result.failed
    trigger_calls = [c for c in calls if "--method" in c]
    assert len(trigger_calls) == 1, f"a real failure must not retry, got {trigger_calls}"


def test_gitlab_jobs_api_returning_nothing_is_infra_not_a_pass_to_the_coder():
    """The silent-`[]` half of the hole: the jobs call returns "" (not an
    exception) — `_get_jobs` must distinguish that from a genuinely empty jobs
    list, so a terminal `failed` pipeline with an unreadable jobs response is
    never read as "no failed jobs found"."""
    fake = FakeRunner([
        "https://x/pipelines/1\n",
        json.dumps({"status": "failed"}),
        "",  # jobs call returns nothing
    ])
    ci = GitLabCI("g/p", hostname="gitlab.acme.net", poll_interval=0, _run_cmd=fake)
    result = ci._trigger_and_wait("branch", {})
    assert result.infra_failure is True


def test_gitlab_jobs_poll_blip_on_a_success_pipeline_is_not_infra():
    """The jobs-is-None infra branch must be scoped to a FAILED pipeline: jobs
    are only load-bearing for the infra-vs-coder classification when the
    pipeline itself failed. A jobs-poll blip on an already-SUCCESS pipeline
    must not flip infra_failure=True — that would trip the retry loop into
    re-triggering a brand-new, non-idempotent pipeline for a run that already
    passed."""
    fake = FakeRunner([
        "https://x/pipelines/1\n",
        json.dumps({"status": "success"}),
        "",  # jobs call returns nothing on an already-successful pipeline
    ])
    ci = GitLabCI("g/p", hostname="gitlab.acme.net", poll_interval=0, _run_cmd=fake)
    result = ci._trigger_and_wait("branch", {})
    assert result.passed is True
    assert result.infra_failure is False


def test_subprocess_run_separates_502_from_401_from_a_plain_failure():
    """Unit-level classification: a 502 reads as infra (never access), a 401
    reads as access (never infra), and operator-chosen argv text echoed back
    into an otherwise unclassified failure manufactures neither verdict — the
    same `_signal_view` redaction `_is_access_error` already relies on."""
    from no_human.ci.gitlab import _is_access_error, _is_infra_error, _operator_echoes

    body_502 = "glab: 502 Bad Gateway (HTTP 502)\n"
    assert _is_infra_error(body_502)
    assert not _is_access_error(body_502)

    body_401 = "glab: 401 Unauthorized (HTTP 401)\n"
    assert _is_access_error(body_401)
    assert not _is_infra_error(body_401)

    # A project literally named "eof-canary" would, unredacted, make ANY
    # failure that echoes it back bare (outside a URL) read as infra — "eof"
    # is one of the bare, no-space `_INFRA_SIGNALS`.
    argv = _glab_argv("grp%2Feof-canary")
    echoes = _operator_echoes(argv)
    plain_failure = (
        '  Get "https://gitlab.acme.net/api/v4/projects/grp%2Feof-canary'
        '/pipeline": some transient glitch\n'
        '  request to projects/grp/eof-canary/pipeline failed, please retry\n'
    )
    # Non-vacuity: without the echo redaction this really would trip the
    # infra signal purely off the operator's own project name.
    assert _is_infra_error(plain_failure, echoed=()), (
        "test is vacuous: the unredacted text does not contain the signal")
    assert not _is_infra_error(plain_failure, echoes), (
        "the operator's own project name must not manufacture an infra verdict")
    assert not _is_access_error(plain_failure, echoes)


# --------------------------------------------------------------------------- #
# ci_from_config                                                               #
# --------------------------------------------------------------------------- #

def test_ci_from_config_disabled():
    assert ci_from_config({"ci": {"enabled": False}}) is None
    assert ci_from_config({}) is None


def test_ci_from_config_gitlab():
    cfg = {
        "ci": {
            "enabled": True,
            "backend": "gitlab",
            "project": "ci_gate/subgroup/metrics-core",
            "hostname": "gitlab.acme.net",
            "timeout_minutes": 30,
            "max_infra_retries": 1,
            "poll_interval": 10,
            "variables": {"ENV": "test"},
            "result_parser": "surefire",
        }
    }
    ci = ci_from_config(cfg)
    assert ci is not None
    assert ci.project == "ci_gate/subgroup/metrics-core"
    assert ci.hostname == "gitlab.acme.net"
    assert ci.max_infra_retries == 1
    assert ci.variables == {"ENV": "test"}
    assert ci.result_parser == "surefire"


# --------------------------------------------------------------------------- #
# CI OFF vs CI ON-but-broken: two different answers, and they must LOOK          #
# different. `ci_from_config` returned a bare None for both, so a caller could   #
# not tell "the operator declined CI" from "the operator asked for CI and the    #
# block is broken" — and the silent, ungated reading is the one every caller     #
# defaulted to. See KNOWN_ISSUES KI-5.                                           #
# --------------------------------------------------------------------------- #

def _answer(ci_conf):
    """What a caller actually observes from ci_from_config: a returned value or
    a raised exception. Deliberately symbol-free so it pins the DISTINCTION,
    not the mechanism that provides it."""
    try:
        return ("returned", ci_from_config({"ci": ci_conf}))
    except Exception as exc:  # noqa: BLE001 — the observation IS the point
        return ("raised", type(exc).__name__)


def test_misconfigured_ci_is_distinguishable_from_disabled():
    """The defect, stated as the smallest thing a caller can see.

    `ci.enabled: false` is a supported configuration and must stay a quiet
    `None`. `ci.enabled: true` with no pipeline target is a gate the user
    believes in and does not have, and it must NOT produce the same answer.
    """
    disabled = _answer({"enabled": False, "backend": "gitlab", "project": ""})
    assert disabled == ("returned", None), disabled

    broken = _answer({"enabled": True, "backend": "gitlab", "project": ""})
    assert broken != disabled, (
        "CI-off and CI-on-but-unbuildable give the CALLER the same answer "
        f"({broken!r}); a caller cannot distinguish them, so it proceeds "
        "UNGATED while the user believes the advertised gate ran")


@pytest.mark.parametrize("backend,missing", [
    ("gitlab", "project"),
    ("github_actions", "repo"),
    ("jenkins", "job"),
    ("ghe_checkruns", "repo"),
    ("circleci", "project"),
])
def test_every_backend_rejects_an_enabled_block_with_no_target(backend, missing):
    """All five backends had the identical hole; all five are closed the same
    way. The message must name the key the user has to set — a blocker that
    says "something is wrong" costs the same minute this project spends its
    escalation-precision budget on avoiding."""
    from no_human.ci import CIMisconfigured

    with pytest.raises(CIMisconfigured) as exc:
        ci_from_config({"ci": {"enabled": True, "backend": backend}})
    assert missing in str(exc.value), str(exc.value)
    assert backend in str(exc.value), str(exc.value)
    assert exc.value.backend == backend
    assert missing in exc.value.missing


def test_misconfigured_is_a_valueerror_so_old_handlers_still_catch_it():
    """`unknown ci.backend` has always raised ValueError and callers guard on
    it. CIMisconfigured subclasses ValueError so widening what is raised never
    narrows what is caught."""
    from no_human.ci import CIMisconfigured

    assert issubclass(CIMisconfigured, ValueError)
    with pytest.raises(ValueError):
        ci_from_config({"ci": {"enabled": True, "backend": "gitlab"}})


def test_whitespace_only_target_is_not_a_target():
    """A `project: "   "` used to build a backend pointed at whitespace, which
    fails later, remotely, as an unrelated-looking CI error."""
    from no_human.ci import CIMisconfigured

    with pytest.raises(CIMisconfigured):
        ci_from_config({"ci": {"enabled": True, "backend": "gitlab",
                               "project": "   "}})


def test_ci_from_config_no_project_raises_not_none():
    cfg = {"ci": {"enabled": True, "backend": "gitlab", "project": ""}}
    from no_human.ci import CIMisconfigured
    with pytest.raises(CIMisconfigured):
        ci_from_config(cfg)


def test_gitlab_check_status_terminal():
    """check_status returns terminal status with jobs when pipeline is done."""
    fake = FakeRunner([
        _make_pipeline_response("success"),
        _make_jobs_response([{"name": "test", "status": "success",
                               "failure_reason": None, "web_url": ""}]),
    ])
    ci = GitLabCI("g/p", hostname="gitlab.acme.net", poll_interval=0, _run_cmd=fake)
    result = ci._check_pipeline("99")
    assert result.status == PipelineStatus.SUCCESS
    assert result.passed
    assert len(result.jobs) == 1


def test_gitlab_check_status_running():
    """check_status returns non-terminal status when pipeline is still running."""
    fake = FakeRunner([
        _make_pipeline_response("running"),
    ])
    ci = GitLabCI("g/p", hostname="gitlab.acme.net", poll_interval=0, _run_cmd=fake)
    result = ci._check_pipeline("99")
    assert result.status == PipelineStatus.RUNNING
    assert not result.status.is_terminal


def test_gitlab_check_status_api_failure():
    """check_status returns UNKNOWN when the API call fails."""
    fake = FakeRunner([""])  # empty response → _glab_api returns None
    ci = GitLabCI("g/p", hostname="gitlab.acme.net", poll_interval=0, _run_cmd=fake)
    result = ci._check_pipeline("99")
    assert result.status == PipelineStatus.UNKNOWN
    assert result.infra_failure


def test_gitlab_is_a_ci_backend():
    from no_human.ci.base import CIBackend
    assert issubclass(GitLabCI, CIBackend)
    assert GitLabCI(project="p").name == "gitlab"


def test_ci_from_config_github_actions_readonly():
    from no_human.ci import GitHubActionsCI
    cfg = {"ci": {"enabled": True, "backend": "github_actions",
                  "repo": "dev/query-service", "workflow": "ci.yml"}}
    ci = ci_from_config(cfg)
    assert isinstance(ci, GitHubActionsCI)
    assert ci.repo == "dev/query-service"
    # CI.1: no longer a seam — it's a READ-ONLY reader (max_infra_retries=0,
    # never `gh workflow run`). Read behavior is covered by the test_gha_* suite;
    # here we just confirm routing + that it does not retry a read.
    assert ci.max_infra_retries == 0


def test_ci_from_config_jenkins_defaults_to_watch(isolated_env_file):
    # Phase 6: Jenkins is now a real backend. The default mode is read-only
    # `watch` (poll the PR-triggered build); it is NOT human-gated by default.
    from no_human.ci import JenkinsCI
    cfg = {"ci": {"enabled": True, "backend": "jenkins", "job": "metrics-core-image"}}
    ci = ci_from_config(cfg)
    assert isinstance(ci, JenkinsCI)
    assert ci.mode == "watch"


def test_ci_jenkins_human_gated_mode_still_parks(isolated_env_file):
    # The image-build prerequisite is preserved as an OPT-IN mode that parks the
    # task with a wake hint rather than faking the step (constraint §3.4).
    from no_human.ci import HumanGatedCI, JenkinsCI
    cfg = {"ci": {"enabled": True, "backend": "jenkins", "job": "metrics-core-image",
                  "mode": "human_gated"}}
    ci = ci_from_config(cfg)
    assert isinstance(ci, JenkinsCI)
    with pytest.raises(HumanGatedCI) as exc:
        asyncio.run(ci.trigger("no-human/x"))
    assert exc.value.wake_hint  # carries a wake hint for the orchestrator to park on


def test_ci_from_config_unknown_backend_raises():
    with pytest.raises(ValueError, match="unknown ci.backend"):
        ci_from_config({"ci": {"enabled": True, "backend": "bamboo"}})


def test_all_backends_expose_max_infra_retries(isolated_env_file):
    # The orchestrator reads ci_runner.max_infra_retries unconditionally; every
    # backend must have it (constraint #5: never crash). Jenkins is now a real
    # backend that retries infra failures like the others.
    from no_human.ci import GitHubActionsCI, JenkinsCI
    assert GitLabCI(project="p").max_infra_retries >= 0
    assert GitHubActionsCI(repo="o/r").max_infra_retries >= 0
    assert JenkinsCI(job="j").max_infra_retries >= 0


# --------------------------------------------------------------------------- #
# Orchestrator + CI integration (fake CI runner)                              #
# --------------------------------------------------------------------------- #

import subprocess as _subprocess

from no_human.agent.claude_backend import AgentResult
from no_human.config import load_config
from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task, TaskStatus
from no_human.notify.slack import SlackNotifier
from no_human.review.reviewer import ReviewDecision
from no_human.review.selfcheck import ChecklistItem


def _git(cwd, *args):
    _subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def bare_repo(tmp_path):
    bare = tmp_path / "remote.git"
    _subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True,
                    capture_output=True)
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.email", "u@e.com")
    _git(work, "config", "user.name", "u")
    (work / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (work / "test_calc.py").write_text(
        "from calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n"
    )
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "init")
    _git(work, "remote", "add", "origin", str(bare))
    _git(work, "push", "-u", "origin", "main")
    return work


class FakeBackend:
    def __init__(self, mutate=None):
        self._mutate = mutate or (lambda cwd: None)

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None, on_event=None,
                  supervisor_hook=None, **kwargs):
        self._mutate(cwd)
        return AgentResult(final_text="done", num_turns=2, is_error=False,
                           tokens_used=100, session_id="s", stop_reason="end_turn")


class FakeReviewer:
    def __init__(self, decision):
        self._decision = decision

    async def review(self, task, *, repo_path, **kw):
        return self._decision


class FakeCI:
    name = "fake-ci"

    def __init__(self, result: CIResult):
        self._result = result
        self.calls: list[str] = []
        self.max_infra_retries = 2

    async def trigger(self, branch, extra_variables=None):
        self.calls.append(branch)
        return self._result


def _good_mutate(cwd):
    (cwd / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a * b\n"
    )
    (cwd / "test_calc.py").write_text(
        "from calc import add, mul\n\n"
        "def test_add():\n    assert add(1, 2) == 3\n\n"
        "def test_mul():\n    assert mul(2, 3) == 6\n"
    )


def _passing_review():
    return ReviewDecision(passed=True, checklist=[
        ChecklistItem("ok", True, "calc.py:3"),
    ])


@pytest.mark.slow  # EH1: >45s of real subprocess work — runs in `run_tests.sh full`/`slow`
async def test_ci_pass_leads_to_awaiting_approval(bare_repo, tmp_path, store):
    """CI passes → AWAITING_APPROVAL (PR opened)."""
    ci_result = CIResult("42", "https://x/42", PipelineStatus.SUCCESS)
    cfg = load_config(tmp_path / "config.yaml")
    fake_ci = FakeCI(ci_result)
    orch = Orchestrator(
        store, cfg.data,
        FakeBackend(_good_mutate),
        SlackNotifier(None),
        reviewer=FakeReviewer(_passing_review()),
        ci_runner=fake_ci,
    )
    t = Task.new("add mul()", repo_path=str(bare_repo))
    t.acceptance_criteria = ["mul(a,b) returns product"]
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.AWAITING_APPROVAL
    assert outcome.pr_url is not None
    assert fake_ci.calls  # CI was triggered
    # Attempt records CI fields.
    attempts = await store.list_attempts(t.id)
    assert attempts[-1]["ci_pipeline_id"] == "42"
    assert attempts[-1]["ci_status"] == "success"


@pytest.mark.slow
async def test_ci_real_failure_loops_to_escalate(bare_repo, tmp_path, store):
    """CI real test failure → attempt FAILED → loops → ESCALATED after max_attempts."""
    ci_result = CIResult(
        "77", "https://x/77", PipelineStatus.FAILED, infra_failure=False,
        parsed_output="3 failed, 0 passed",
    )
    cfg = load_config(tmp_path / "config.yaml")
    fake_ci = FakeCI(ci_result)
    orch = Orchestrator(
        store, cfg.data,
        FakeBackend(_good_mutate),
        SlackNotifier(None),
        reviewer=FakeReviewer(_passing_review()),
        ci_runner=fake_ci,
    )
    t = Task.new("fix tests", repo_path=str(bare_repo))
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.ESCALATED
    assert "CI failed" in outcome.detail
    # CI was called once per attempt (3 attempts default).
    assert len(fake_ci.calls) == 3


@pytest.mark.slow
async def test_ci_infra_failure_escalates_immediately(bare_repo, tmp_path, store):
    """CI infra failure (after internal retries) → ESCALATED immediately, not looped."""
    ci_result = CIResult(
        "", "", PipelineStatus.FAILED, infra_failure=True,
        parsed_output="runner_system_failure",
    )
    cfg = load_config(tmp_path / "config.yaml")
    fake_ci = FakeCI(ci_result)
    call_count = []
    original_trigger = fake_ci.trigger

    async def counting_trigger(branch, **kw):
        call_count.append(branch)
        return await original_trigger(branch, **kw)

    fake_ci.trigger = counting_trigger
    orch = Orchestrator(
        store, cfg.data,
        FakeBackend(_good_mutate),
        SlackNotifier(None),
        reviewer=FakeReviewer(_passing_review()),
        ci_runner=fake_ci,
    )
    t = Task.new("fix stuff", repo_path=str(bare_repo))
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.ESCALATED
    assert "infra" in outcome.detail.lower()
    # Infra failure escalates from the first attempt — no point burning attempts
    # on a scheduler that's down.
    assert len(call_count) == 1


async def test_no_ci_runner_skips_ci(bare_repo, tmp_path, store):
    """No ci_runner configured → pipeline runs without CI, reaches AWAITING_APPROVAL."""
    cfg = load_config(tmp_path / "config.yaml")
    orch = Orchestrator(
        store, cfg.data,
        FakeBackend(_good_mutate),
        SlackNotifier(None),
        reviewer=FakeReviewer(_passing_review()),
        ci_runner=None,
    )
    t = Task.new("add mul()", repo_path=str(bare_repo))
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.AWAITING_APPROVAL


async def test_ci_unrelated_failure_escalates_immediately(bare_repo, tmp_path, store):
    """Phase 6.3: a red build whose failing tests are not in any changed file is a
    pre-existing/monorepo failure → escalate from the first attempt with cited
    evidence, NOT loop trying to fix code we didn't write."""
    ci_result = CIResult(
        "88", "https://x/88", PipelineStatus.FAILED, infra_failure=False,
        jobs=[JobResult(name="com.acme.billing.InvoiceIT.testTotals",
                        status="failed", failure_reason="pre-existing")],
        parsed_output="1 failing test: com.acme.billing.InvoiceIT.testTotals",
    )
    cfg = load_config(tmp_path / "config.yaml")
    fake_ci = FakeCI(ci_result)
    orch = Orchestrator(
        store, cfg.data, FakeBackend(_good_mutate), SlackNotifier(None),
        reviewer=FakeReviewer(_passing_review()), ci_runner=fake_ci,
    )
    t = Task.new("add mul()", repo_path=str(bare_repo))  # touches calc.py/test_calc.py
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.ESCALATED
    assert len(fake_ci.calls) == 1  # did NOT burn 3 attempts
    t2 = await store.get_task(t.id)
    assert "InvoiceIT" in (t2.blocker or {}).get("evidence", "")


@pytest.mark.slow  # EH1: >45s of real subprocess work — runs in `run_tests.sh full`/`slow`
async def test_concurrency_worktree_mode_opens_pr_and_cleans_up(bare_repo, tmp_path, store):
    """Phase 7.2: with concurrency.enabled the task runs in its own worktree,
    still reaches AWAITING_APPROVAL with a PR, and the worktree is removed."""
    from no_human.vcs import GitRepo
    cfg = load_config(tmp_path / "config.yaml")
    cfg.data["concurrency"] = {"enabled": True,
                               "worktree_root": str(tmp_path / "wt")}
    orch = Orchestrator(
        store, cfg.data, FakeBackend(_good_mutate), SlackNotifier(None),
        reviewer=FakeReviewer(_passing_review()), ci_runner=None,
    )
    t = Task.new("add mul()", repo_path=str(bare_repo))
    t.acceptance_criteria = ["mul works"]
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.AWAITING_APPROVAL
    assert outcome.pr_url is not None
    # Worktree cleaned up: only the primary checkout remains.
    main = GitRepo(bare_repo)
    assert all("/wt/" not in w for w in main.list_worktrees())
    # Worktree directories are named `<task_id>.<owner_pid>.<token>` — one per
    # RUN. Probing the bare `<task_id>` path would now pass without looking at
    # anything, so match every directory the task could have left behind.
    assert not list((tmp_path / "wt").glob(f"{t.id}*"))
    # The agent worked in the worktree, never the primary checkout.
    assert "mul" not in (bare_repo / "calc.py").read_text()


@pytest.mark.slow  # EH1: >45s of real subprocess work — runs in `run_tests.sh full`/`slow`
async def test_ci_access_failure_routes_to_missing_access(bare_repo, tmp_path, store):
    """Phase 6 / (a): a credential/permission wall (not infra, not code) parks as
    MISSING_ACCESS with an ask, and is NOT retried."""
    ci_result = CIResult(
        "", "https://x/jenkins/job", PipelineStatus.FAILED,
        access_failure=True,
        parsed_output="Jenkins denied access (401/403). Set JENKINS_API_TOKEN …",
    )
    cfg = load_config(tmp_path / "config.yaml")
    fake_ci = FakeCI(ci_result)
    orch = Orchestrator(
        store, cfg.data, FakeBackend(_good_mutate), SlackNotifier(None),
        reviewer=FakeReviewer(_passing_review()), ci_runner=fake_ci,
    )
    t = Task.new("add mul()", repo_path=str(bare_repo))
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.ESCALATED
    assert len(fake_ci.calls) == 1  # access is not retried
    t2 = await store.get_task(t.id)
    assert (t2.blocker or {}).get("category") == "MISSING_ACCESS"
    assert "access" in (t2.blocker or {}).get("question", "").lower()


@pytest.mark.slow
async def test_ci_related_failure_threads_into_next_prompt(bare_repo, tmp_path, store):
    """Phase 6.2: a real failure in a file we changed feeds the next attempt's
    prompt so the agent fixes the ACTUAL remote failure."""
    ci_result = CIResult(
        "91", "https://x/91", PipelineStatus.FAILED, infra_failure=False,
        jobs=[JobResult(name="calc.test_mul", status="failed",
                        failure_reason="AssertionError: mul(2,3)==6")],
        parsed_output="1 failing test: calc.test_mul",
    )
    cfg = load_config(tmp_path / "config.yaml")
    fake_ci = FakeCI(ci_result)
    prompts: list[str] = []

    class CapturingBackend(FakeBackend):
        async def run(self, prompt, **kw):
            prompts.append(prompt)
            return await super().run(prompt, **kw)

    orch = Orchestrator(
        store, cfg.data, CapturingBackend(_good_mutate), SlackNotifier(None),
        reviewer=FakeReviewer(_passing_review()), ci_runner=fake_ci,
    )
    t = Task.new("add mul()", repo_path=str(bare_repo))
    await store.create_task(t)

    await orch.run_task(t)

    # First attempt got no CI context; the second (after the red build) does.
    assert len(prompts) >= 2
    assert "remote ci build" in prompts[1].lower()
    assert "calc.test_mul" in prompts[1]


# --------------------------------------------------------------------------- #
# CI.1 — GitHub Actions READ-ONLY reader (Phase 2C)                            #
# --------------------------------------------------------------------------- #
from no_human.ci.github_actions import GitHubActionsCI, _is_auth_error  # noqa: E402


def _cr(name, conclusion, status="completed", url=""):
    """Build one raw GitHub check-run dict."""
    return {"name": name, "conclusion": conclusion, "status": status, "html_url": url}


def _statuses(state="pending", entries=None):
    """A raw ``/commits/{ref}/status`` payload. Default = zero statuses (no CI on
    that surface), which reads as UNKNOWN/no-jobs and is dropped by the merge."""
    entries = entries or []
    return {"state": state, "statuses": entries, "total_count": len(entries)}


def _st(context, state, url=""):
    """Build one raw commit-status entry."""
    return {"context": context, "state": state, "target_url": url}


def _gha(runs=None, *, raises=None, poll_interval=0, statuses=None,
         statuses_raises=None):
    """A GitHubActionsCI whose fetches are stubbed — never touches the network.

    ``statuses`` defaults to an empty commit-status payload so existing check-run
    tests keep their exact verdict (the empty status surface is dropped by the
    merge)."""
    async def fake_fetch(repo, ref, *, hostname=""):
        if raises is not None:
            raise raises
        return runs or []

    async def fake_statuses(repo, ref, *, hostname=""):
        if statuses_raises is not None:
            raise statuses_raises
        return statuses if statuses is not None else _statuses()

    return GitHubActionsCI("owner/repo", poll_interval=poll_interval,
                           fetch_runs=fake_fetch, fetch_statuses=fake_statuses)


def test_gha_all_success_is_pass():
    ci = _gha([_cr("build", "success"), _cr("test", "success")])
    result = asyncio.run(ci.trigger("branch"))
    assert result.passed
    assert result.status == PipelineStatus.SUCCESS
    assert not result.infra_failure and not result.access_failure


def test_gha_any_failure_is_fail_not_infra():
    ci = _gha([_cr("build", "success"), _cr("test", "failure")])
    result = asyncio.run(ci.trigger("branch"))
    assert result.failed
    assert not result.infra_failure  # a real test failure is NOT infra
    assert not result.passed


def test_gha_no_checks_is_unknown_never_green():
    # THE no-CI disambiguation: a repo with no checks must never read as green.
    ci = _gha([])
    result = asyncio.run(ci.trigger("branch"))
    assert result.status == PipelineStatus.UNKNOWN
    assert not result.passed
    assert not result.infra_failure and not result.access_failure


def test_gha_missing_token_is_access_failure_not_infra():
    ci = _gha(raises=RuntimeError("gh api failed (1): HTTP 401: Bad credentials"))
    result = asyncio.run(ci.trigger("branch"))
    assert result.access_failure
    assert result.access_env_key == "GH_TOKEN"
    assert not result.infra_failure  # a missing token is NOT transient — don't retry
    assert not result.passed


def test_gha_forbidden_is_access_failure():
    ci = _gha(raises=RuntimeError("gh api failed (1): HTTP 403: Resource not accessible by personal access token"))
    result = asyncio.run(ci.trigger("branch"))
    assert result.access_failure and not result.infra_failure


def test_gha_transient_error_is_infra_failure():
    ci = _gha(raises=RuntimeError("gh api failed (1): HTTP 502 Bad Gateway"))
    result = asyncio.run(ci.trigger("branch"))
    assert result.infra_failure
    assert not result.access_failure  # a 5xx IS transient — retryable
    assert not result.passed


def test_gha_check_status_in_progress_is_running():
    ci = _gha([_cr("test", "", status="in_progress")])
    result = asyncio.run(ci.check_status("branch"))
    assert result.status == PipelineStatus.RUNNING
    assert not result.passed


# --- CI.1b: Commit-Status API merged into the reader -------------------------

def test_gha_commit_status_only_green_is_pass():
    # No check-runs at all, but the Commit Status API reports success -> green.
    ci = _gha([], statuses=_statuses("success", [_st("jenkins", "success")]))
    result = asyncio.run(ci.trigger("branch"))
    assert result.passed
    assert [j.name for j in result.jobs] == ["jenkins"]


def test_gha_checkruns_green_and_no_statuses_is_green():
    # Green check-runs + zero commit-statuses (the common case) stays green:
    # the empty status surface is "no opinion", not a failure.
    ci = _gha([_cr("build", "success")], statuses=_statuses("pending", []))
    result = asyncio.run(ci.trigger("branch"))
    assert result.passed


def test_gha_commit_status_failure_overrides_checkrun_pass():
    # A failing external status must sink an otherwise-green check-run set — a
    # green on one surface may NEVER hide a failure on the other.
    ci = _gha([_cr("build", "success")],
              statuses=_statuses("failure", [_st("jenkins", "failure")]))
    result = asyncio.run(ci.trigger("branch"))
    assert result.failed
    assert not result.passed
    assert not result.infra_failure


def test_gha_both_surfaces_empty_is_unknown_never_green():
    # No check-runs AND no commit-statuses -> UNKNOWN, never a false pass.
    ci = _gha([], statuses=_statuses("pending", []))
    result = asyncio.run(ci.trigger("branch"))
    assert result.status == PipelineStatus.UNKNOWN
    assert not result.passed
    assert not result.infra_failure and not result.access_failure


def test_gha_pending_commit_status_is_running():
    # A real pending status (>=1 entry) is RUNNING — distinct from the empty
    # "pending with zero statuses" that means no-CI.
    ci = _gha([_cr("build", "success")],
              statuses=_statuses("pending", [_st("jenkins", "pending")]))
    result = asyncio.run(ci.check_status("branch"))
    assert result.status == PipelineStatus.RUNNING
    assert not result.passed


def test_gha_status_auth_wall_is_not_dropped_to_green():
    # If the commit-status read hits an auth wall it must NOT be silently dropped
    # to let green check-runs vouch for the commit — the wall keeps it UNKNOWN.
    ci = _gha([_cr("build", "success")],
              statuses_raises=RuntimeError("gh api failed (1): HTTP 401: Bad credentials"))
    result = asyncio.run(ci.check_status("branch"))
    assert result.access_failure
    assert result.status == PipelineStatus.UNKNOWN
    assert not result.passed


def test_gha_trigger_is_read_only_never_raises_notimplemented():
    # The old stub raised NotImplementedError; the read-only path must not.
    ci = _gha([_cr("test", "success")])
    result = asyncio.run(ci.trigger("branch"))  # would raise if still a stub
    assert result.passed


def test_gha_is_a_ci_backend_and_routes_from_config():
    from no_human.ci.base import CIBackend
    ci = ci_from_config({"ci": {"enabled": True, "backend": "github_actions",
                                 "repo": "owner/repo"}})
    assert isinstance(ci, GitHubActionsCI)
    assert isinstance(ci, CIBackend)


def test_is_auth_error_classifier():
    # Genuine auth walls -> True.
    assert _is_auth_error("gh api failed (1): HTTP 401: Bad credentials")
    assert _is_auth_error("gh api failed (1): HTTP 403: forbidden")
    assert _is_auth_error("Resource not accessible by integration")
    # Transient conditions that carry a 403/404 -> NOT auth (must stay retryable).
    assert not _is_auth_error("HTTP 403: API rate limit exceeded for user 12345")
    assert not _is_auth_error("HTTP 403: You have exceeded a secondary rate limit")
    assert not _is_auth_error("HTTP 404: No commit found for SHA deadbeef")
    # Bare digits inside byte counts / ms timers must NOT match (substring bug).
    assert not _is_auth_error("gh api failed: i/o timeout after 40130ms")
    assert not _is_auth_error("gh api failed: read 1403 bytes then reset")
    assert not _is_auth_error("HTTP 502 Bad Gateway")
    assert not _is_auth_error("connection reset by peer")


def test_gha_startup_failure_is_not_green():
    # A workflow that failed to START (invalid YAML, etc.) never ran the tests —
    # it must NOT read as green through the reader.
    ci = _gha([_cr("ci", "startup_failure")])
    result = asyncio.run(ci.trigger("branch"))
    assert result.status == PipelineStatus.UNKNOWN
    assert not result.passed


def test_gha_rate_limit_403_is_infra_not_access():
    ci = _gha(raises=RuntimeError("gh api failed (1): HTTP 403: API rate limit exceeded"))
    result = asyncio.run(ci.trigger("branch"))
    assert result.infra_failure       # transient -> retryable
    assert not result.access_failure  # NOT a permanent human park
    assert not result.passed


def test_gha_no_commit_404_is_infra_not_access():
    ci = _gha(raises=RuntimeError("gh api failed (1): HTTP 404: No commit found for SHA x"))
    result = asyncio.run(ci.trigger("branch"))
    assert result.infra_failure
    assert not result.access_failure  # a token won't fix a missing SHA


# --------------------------------------------------------------------------- #
# CI backend resolution: which source wins, and what happens when none works.  #
#                                                                             #
# The defect these pin: the global `ci:` block was documented in config.py and #
# docs/configuration.md and read by NOTHING. The profile path was dead too —   #
# `ci_from_config({"ci": prof.ci})` needs `enabled`, and nothing in onboard.py #
# or profile.py ever writes one. A user configuring CI exactly as documented   #
# got no gate, no warning and no diagnostic.                                    #
# --------------------------------------------------------------------------- #

from no_human.profile import ProjectProfile  # noqa: E402


def _resolve(cfg_ci=None, prof_ci=None, *, injected=None):
    """Run _resolve_ci_runner in isolation; return (runner, events)."""
    import copy

    from no_human.config import DEFAULT_CONFIG

    data = copy.deepcopy(DEFAULT_CONFIG)
    if cfg_ci is not None:
        data["ci"] = cfg_ci
    events: list[dict] = []
    orch = Orchestrator(
        None, data, None, SlackNotifier(None),
        event_sink=events.append, ci_runner=injected,
    )
    prof = None
    if prof_ci is not None:
        prof = ProjectProfile(repo_path="/tmp/r", ci=prof_ci)
    orch._resolve_ci_runner(prof)
    return orch.ci_runner, events


def _advisories(events):
    return [e["text"] for e in events if e["kind"] == "advisory"]


def test_global_ci_block_is_actually_read():
    """THE defect: a documented, enabled global `ci:` block builds a backend."""
    runner, events = _resolve(
        cfg_ci={"enabled": True, "backend": "gitlab", "project": "grp/repo"})
    assert isinstance(runner, GitLabCI)
    assert runner.project == "grp/repo"
    assert not _advisories(events), "a working config must not warn"
    origins = [e for e in events if e["kind"] == "ci_backend"]
    assert origins and origins[0]["origin"] == "global config"


def test_no_ci_config_anywhere_changes_nothing():
    """Devil's advocate: an install that never configured CI is untouched.

    Asserted against DEFAULT_CONFIG, not a loaded config — load_config()
    deep-merges the operator's own ~/.no_human/config.yaml, so a check against
    the loaded config would only prove something about this machine.
    """
    from no_human.config import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["ci"]["enabled"] is False, "the shipped default is off"
    runner, events = _resolve()
    assert runner is None
    assert not events, "no CI configured => not one event, not one warning"


def test_profile_ci_builds_a_backend_at_all():
    """Regression pin: prof.ci has no `enabled` key (onboard.py never writes
    one), so the pre-fix call returned None for every profile that ever existed.
    """
    runner, events = _resolve(prof_ci={"backend": "gitlab", "project": "grp/proj"})
    assert isinstance(runner, GitLabCI)
    assert runner.project == "grp/proj"
    assert not _advisories(events)


def test_profile_beats_global_config():
    """Precedence: the more specific, human-confirmed source wins."""
    runner, events = _resolve(
        cfg_ci={"enabled": True, "backend": "gitlab", "project": "global/repo"},
        prof_ci={"backend": "gitlab", "project": "profile/repo"},
    )
    assert runner.project == "profile/repo"
    assert [e for e in events if e["kind"] == "ci_backend"][0]["origin"] \
        == "project profile"


def test_global_is_a_fallback_when_the_profile_names_no_target():
    """`nh onboard` writes a bare {"backend": "gitlab"} on seeing a
    .gitlab-ci.yml. That is a detection hint, not a claim: it must fall through
    to the global block WITHOUT warning, or every GitLab repo warns every run.
    """
    runner, events = _resolve(
        cfg_ci={"enabled": True, "backend": "gitlab", "project": "global/repo"},
        prof_ci={"backend": "gitlab"},
    )
    assert runner.project == "global/repo"
    assert not _advisories(events), "a detection hint must not be reported as broken"


def test_enabled_but_targetless_global_block_warns():
    """The signal that never existed: configured-but-unusable is now VISIBLE."""
    runner, events = _resolve(cfg_ci={"enabled": True, "backend": "gitlab",
                                      "project": ""})
    assert runner is None
    adv = _advisories(events)
    assert len(adv) == 1
    assert "UNUSABLE" in adv[0] and "global config" in adv[0]
    assert "NO CI gate" in adv[0]


def test_unknown_backend_warns_instead_of_killing_the_run():
    runner, events = _resolve(cfg_ci={"enabled": True, "backend": "travis",
                                      "project": "grp/repo"})
    assert runner is None, "an unknown backend yields no gate"
    adv = _advisories(events)
    assert len(adv) == 1
    # The advisory a human reads must name the typo, not the exception class.
    assert "unknown ci.backend" in adv[0] and "travis" in adv[0]


def test_explicit_injection_still_wins():
    """The constructor arg must override both config sources (test/embedder seam)."""
    sentinel = FakeCI(CIResult("1", "u", PipelineStatus.SUCCESS))
    runner, events = _resolve(
        cfg_ci={"enabled": True, "backend": "gitlab", "project": "grp/repo"},
        prof_ci={"backend": "gitlab", "project": "p/r"},
        injected=sentinel,
    )
    assert runner is sentinel
    assert not events


def test_disabled_global_block_is_not_a_source():
    """`enabled: false` is the operator's own switch — honoured before the wrap."""
    runner, events = _resolve(cfg_ci={"enabled": False, "backend": "gitlab",
                                      "project": "grp/repo"})
    assert runner is None
    assert not events


# --------------------------------------------------------------------------- #
# The resolver's RETURN value: the run's own answer to "may I proceed?".        #
# An advisory made the failure visible; it did not make it BINDING. The reason  #
# string is what run_task escalates on, so the two no-CI outcomes are           #
# distinguishable at the one call site where proceeding ungated happens.        #
# --------------------------------------------------------------------------- #

def _resolve_reason(cfg_ci=None, prof_ci=None, *, injected=None):
    import copy

    from no_human.config import DEFAULT_CONFIG

    data = copy.deepcopy(DEFAULT_CONFIG)
    if cfg_ci is not None:
        data["ci"] = cfg_ci
    orch = Orchestrator(None, data, None, SlackNotifier(None),
                        event_sink=lambda e: None, ci_runner=injected)
    prof = ProjectProfile(repo_path="/tmp/r", ci=prof_ci) if prof_ci is not None else None
    return orch._resolve_ci_runner(prof)


def test_resolver_reports_no_reason_when_nothing_asked_for_ci():
    assert _resolve_reason() is None


def test_resolver_reports_no_reason_when_ci_is_deliberately_off():
    assert _resolve_reason(cfg_ci={"enabled": False, "backend": "gitlab",
                                   "project": "grp/repo"}) is None


def test_resolver_reports_no_reason_when_a_backend_was_built():
    assert _resolve_reason(cfg_ci={"enabled": True, "backend": "gitlab",
                                   "project": "grp/repo"}) is None


def test_resolver_returns_the_reason_when_every_claiming_source_failed():
    reason = _resolve_reason(cfg_ci={"enabled": True, "backend": "gitlab",
                                     "project": ""})
    assert reason, "an unbuildable, CI-claiming config must yield a reason"
    assert "global config" in reason and "project" in reason


@pytest.mark.parametrize("prof_ci", [
    {"backend": "gitlab"},
    {"backend": "gitlab", "project": ""},
    {"backend": "gitlab", "project": "   "},
    {"backend": "travis"},
    {"backend": "gitlab", "projct": "typo/key"},
])
def test_profile_ci_with_no_target_and_no_global_block_is_not_a_source(prof_ci):
    """PINS A KNOWN GAP ON PURPOSE — read this before "fixing" it.

    None of these reaches `ci_from_config` at all: `_resolve_ci_runner` admits
    a profile as a source only when one of `_CI_TARGET_KEYS` is non-blank, so
    with no global `ci:` block there is no source, no advisory, no reason, and
    the run proceeds on the local suite. The last row is a MISTYPED KEY, which
    is the shape the escalation exists for — so this gap is real and it is
    recorded in KNOWN_ISSUES KI-5, not hidden.

    Why it is not closed here: `nh onboard` writes a bare
    `{"backend": "gitlab"}` the moment it sees a `.gitlab-ci.yml`. That is a
    detection hint, not a request for a gate. Treating it as a claim would
    escalate every onboarded GitLab repo on its first run — a worse failure
    than the one being fixed, and the cure needs onboarding to record intent,
    which is a different change.

    The test exists so the behaviour is DELIBERATE rather than incidental: a
    future widening of `_CI_TARGET_KEYS` would otherwise park every onboarded
    repo with the whole suite still green.
    """
    runner, events = _resolve(prof_ci=prof_ci)
    assert runner is None
    assert not events, "a detection hint must stay silent"
    assert _resolve_reason(prof_ci=prof_ci) is None, \
        "this run is ungated and does NOT escalate — see the docstring"


def test_resolver_reports_no_reason_when_a_later_source_recovers():
    """A broken profile block plus a working global block is a WORKING install:
    the advisory is right, escalating would not be."""
    reason = _resolve_reason(
        cfg_ci={"enabled": True, "backend": "gitlab", "project": "global/repo"},
        prof_ci={"backend": "travis", "project": "p/r"},
    )
    assert reason is None


# --------------------------------------------------------------------------- #
# GitLab: a permissions wall is NOT a network blip                             #
#                                                                              #
# Every sibling backend (base/circleci/ghe_checkruns/github_actions/jenkins)   #
# distinguishes the two; gitlab.py had zero `access_failure` paths, so a 401   #
# was retried twice at 120 s and then escalated as "infra" — the one wording   #
# that does NOT tell the operator which key to set.                            #
#                                                                              #
# The stderr strings below are VERBATIM from a real `glab` 1.92.1 against      #
# gitlab.com (bad token / no credential / unresolvable host), not invented.    #
# --------------------------------------------------------------------------- #

GLAB_401 = "glab: 401 Unauthorized (HTTP 401)\n"
GLAB_UNAUTHENTICATED = "          \n   ERROR  \n          \n  Unauthenticated.    \n\n"
GLAB_DNS = ('          \n   ERROR  \n          \n  Get "https://h/api/v4/projects/1": '
            'dial tcp: lookup h: no such    \n  host.    \n\n')


def _fake_proc(returncode: int, stdout: str = "", stderr: str = ""):
    from types import SimpleNamespace
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


#: A DNS failure is a blip and must be retried. `glab` reports one by echoing
#: the whole request URL back, which carries two pieces of OPERATOR-CHOSEN
#: text — the project path and the hostname. Substring-matching auth signals
#: over that text let the operator's own naming decide the verdict: these
#: parked as MISSING_ACCESS and summoned a human for a failure that would have
#: healed itself, which is the precise harm the auth-wall split exists to stop.
GLAB_DNS_AUTH_WORD_IN_PROJECT = (
    '          \n   ERROR  \n          \n  Get "https://gitlab.acme.net/api/v4/'
    'projects/grp%2Funauthenticated-proxy/pipeline": dial tcp: lookup '
    'gitlab.acme.net: no such    \n  host.    \n\n')
GLAB_DNS_AUTH_WORD_IN_HOST = (
    '          \n   ERROR  \n          \n  Get "https://unauthenticated.acme.net'
    '/api/v4/projects/grp%2Fsvc/pipeline": dial tcp: lookup '
    'unauthenticated.acme.net: no such    \n  host.    \n\n')
#: The signal legitimately lives inside quotes here, which is why the redaction
#: strips URL SPANS and not "anything quoted" — this must stay a true positive.
GLAB_OAUTH_CHALLENGE = 'Bearer realm="gitlab", error="insufficient_scope"\n'
#: The hardest case: the redaction must not blind us. The project is named
#: `unauthenticated` AND the wall is real.
GLAB_401_AUTH_WORD_IN_PROJECT = "glab: 401 Unauthorized (HTTP 401)\n"

#: argv as `GitLabCI` really builds it (see `_trigger_pipeline`), so the
#: redaction is exercised against the tokens glab actually echoes.
def _glab_argv(project: str = "grp%2Fsvc", host: str = "gitlab.acme.net"):
    return ["glab", "api", "--hostname", host, "--method", "POST",
            f"projects/{project}/pipeline", "--input", "/tmp/body.json"]


@pytest.mark.parametrize("stderr,expect_access", [
    (GLAB_401, True),
    (GLAB_UNAUTHENTICATED, True),
    (GLAB_DNS, False),          # control: a network failure must stay infra
])
def test_glab_nonzero_exit_separates_an_auth_wall_from_a_network_failure(
    monkeypatch, isolated_env_file, stderr, expect_access,
):
    """`_subprocess_run` discarded stderr entirely and returned "" for every
    nonzero exit, so no caller could ever tell the two apart."""
    from no_human.ci import gitlab as glmod

    monkeypatch.setattr(
        glmod.subprocess, "run",
        lambda *a, **k: _fake_proc(1, stdout="", stderr=stderr),
    )
    if expect_access:
        with pytest.raises(glmod.GitLabAccessError) as exc:
            glmod._subprocess_run(["glab", "api", "x"])
        assert "401" in str(exc.value) or "nauthenticated" in str(exc.value)
    else:
        assert glmod._subprocess_run(["glab", "api", "x"]) == ""


@pytest.mark.parametrize("label,argv,stderr", [
    ("project named unauthenticated-proxy",
     _glab_argv("grp%2Funauthenticated-proxy"), GLAB_DNS_AUTH_WORD_IN_PROJECT),
    ("host named unauthenticated.acme.net",
     _glab_argv(host="unauthenticated.acme.net"), GLAB_DNS_AUTH_WORD_IN_HOST),
])
def test_a_network_failure_stays_infra_even_when_the_operators_own_names_read_as_auth(
    monkeypatch, isolated_env_file, label, argv, stderr,
):
    """The operator's naming must never be able to synthesize an auth verdict.

    Both of these are DNS failures. Before the redaction they raised
    GitLabAccessError purely because the project path (or hostname) glab echoed
    back contained the substring "unauthenticated" — so the task parked on a
    human instead of retrying, for a blip. `GLAB_DNS` above is the control:
    same failure, ordinary names, correctly infra.
    """
    from no_human.ci import gitlab as glmod

    monkeypatch.setattr(glmod.subprocess, "run",
                        lambda *a, **k: _fake_proc(1, stdout="", stderr=stderr))
    assert glmod._subprocess_run(argv) == "", (
        f"a DNS failure with an auth word in the {label} must retry as infra")


@pytest.mark.parametrize("stderr", [GLAB_OAUTH_CHALLENGE, GLAB_401_AUTH_WORD_IN_PROJECT])
def test_redacting_the_echoed_url_does_not_blind_the_auth_wall(
    monkeypatch, isolated_env_file, stderr,
):
    """Non-vacuity for the test above: a test that just stopped raising would
    pass it. These two are the cases the redaction could plausibly break — a
    signal that lives INSIDE quotes, and a real 401 on a project whose name is
    itself an auth word (so the redaction is actively removing that word from
    the text while the verdict must still be "wall")."""
    from no_human.ci import gitlab as glmod

    monkeypatch.setattr(glmod.subprocess, "run",
                        lambda *a, **k: _fake_proc(1, stdout="", stderr=stderr))
    with pytest.raises(glmod.GitLabAccessError):
        glmod._subprocess_run(_glab_argv("grp%2Funauthenticated"))


def test_an_auth_word_inside_a_url_is_ignored_with_no_argv_context_at_all():
    """The URL strip is a SECOND, independent defense and needs its own pin.

    `_is_access_error`'s `echoed` argument defaults to empty, so any caller
    without an argv to offer relies on the URL strip alone. Deleting the strip
    left every test above green — `_subprocess_run` always supplies argv, so
    those tests cannot tell the two defenses apart, and the strip would have
    rotted unobserved.

    Only the in-URL case is asserted. In `GLAB_DNS_AUTH_WORD_IN_HOST` the host
    is also echoed BARE ("lookup unauthenticated.acme.net: no such host"),
    outside any URL — that one genuinely needs the argv redaction, which is
    what `test_a_network_failure_stays_infra_...` covers.
    """
    from no_human.ci.gitlab import _is_access_error

    assert not _is_access_error(GLAB_DNS_AUTH_WORD_IN_PROJECT), (
        "the project path appears ONLY inside the echoed URL here")
    # Non-vacuity: a function that had simply stopped returning True passes the
    # line above. A real wall, through the same no-argv call, must still fire.
    assert _is_access_error(GLAB_401)


def test_the_quoted_blocker_line_names_the_real_wall_not_the_echoed_url(
    monkeypatch, isolated_env_file,
):
    """`_access_reason` picks the line to show the human, and it must agree
    with `_is_access_error` about WHICH line is the wall.

    Here the first line matches only because the operator's project is called
    `unauthenticated-proxy`; the actual 401 is on the second. Matching on the
    raw line would quote the URL line — so the operator opens a MISSING_ACCESS
    blocker and reads a network error, for a genuine credentials problem. That
    is the same "the product said something untrue" class as the rest of this
    branch, one layer further out.
    """
    from no_human.ci import gitlab as glmod

    stderr = (
        '  Get "https://gitlab.acme.net/api/v4/projects/'
        'grp%2Funauthenticated-proxy/pipeline": retrying\n'
        "  glab: 401 Unauthorized (HTTP 401)\n"
    )
    monkeypatch.setattr(glmod.subprocess, "run",
                        lambda *a, **k: _fake_proc(1, stdout="", stderr=stderr))
    with pytest.raises(glmod.GitLabAccessError) as exc:
        glmod._subprocess_run(_glab_argv("grp%2Funauthenticated-proxy"))
    assert "401 Unauthorized" in str(exc.value), str(exc.value)
    assert "Get \"https" not in str(exc.value), (
        f"quoted the echoed URL instead of the wall: {exc.value}")


def test_argv_that_is_part_of_a_signals_wording_is_never_redacted():
    """`glab` is argv[0] AND a substring of the `glab auth login` signal.
    Redacting every argv token blindly would delete the signal from the text
    and turn a genuine "you are not logged in" into an infra retry."""
    from no_human.ci.gitlab import _is_access_error, _operator_echoes

    echoes = _operator_echoes(_glab_argv())
    assert "glab" not in echoes, f"argv[0] must survive redaction: {echoes}"
    assert "gitlab.acme.net" in echoes, f"the hostname must be redacted: {echoes}"
    assert _is_access_error("  run glab auth login to authenticate\n", echoes)


def test_the_percent_decoded_spelling_of_a_signal_is_never_redacted_either():
    """The same guard, on the SECOND echo `_operator_echoes` emits.

    Each argv token contributes two echoes — itself and `unquote(itself)` —
    and the signal-wording clause used to be applied only to the first. So a
    token that carries no signal RAW can manufacture one when decoded:
    `glab%20auth%20login` is not a substring of any signal, passes the raw
    check, and decodes to the whole of `glab auth login`. Measured before the
    fix: echoes `('glab%20auth%20login', 'glab auth login')`, and a real
    `run glab auth login` wall then classified False — an infra retry, twice,
    for a credentials problem no retry can clear. That is the exact harm the
    redaction exists to prevent, running in the other direction.

    Driven through `_operator_echoes` directly, not `_subprocess_run`, because
    today's argv cannot carry it: `_glab_argv` above is the whole shape, a
    project path is `[A-Za-z0-9_.-]` joined by `/` and reaches argv only as
    `%2F`, and hostnames cannot hold `%`. This pins the property so a future
    caller that does put operator text on the command line cannot reopen it.
    """
    from no_human.ci.gitlab import _is_access_error, _operator_echoes

    echoes = _operator_echoes(["glab", "api", "glab%20auth%20login"])
    assert "glab auth login" not in echoes, (
        f"the decoded spelling of a signal must survive redaction: {echoes}")
    assert _is_access_error("  run glab auth login to authenticate\n", echoes)

    # Non-vacuity: the decode itself is untouched for ordinary operator text,
    # so this is a carve-out and not a deletion of the second echo.
    ordinary = _operator_echoes(["glab", "api", "grp%2Fsvc"])
    assert "grp/svc" in ordinary, ordinary


def test_glab_zero_exit_is_unchanged(monkeypatch, isolated_env_file):
    """Non-vacuity control: the success path must still return the output."""
    from no_human.ci import gitlab as glmod

    monkeypatch.setattr(glmod.subprocess, "run",
                        lambda *a, **k: _fake_proc(0, stdout='{"id": 1}', stderr=""))
    assert glmod._subprocess_run(["glab", "api", "x"]) == '{"id": 1}'


def test_gitlab_token_already_in_the_environment_is_not_overwritten(
    tmp_path, monkeypatch,
):
    """An exported GITLAB_TOKEN beats the .env file — the operator's shell wins.

    `config.load_env_var` overwrites unconditionally (`if value:
    os.environ[name] = value`), so the early return in `_load_gitlab_token` is
    the ONLY thing that makes the shell authoritative. That guard was
    unguarded: deleting it left the whole of tests/test_ci.py green at 97
    passed, so an upgrade could have silently started billing a run against a
    stale on-disk token while the operator watched their exported one.
    """
    import no_human.config as nh_config
    from no_human.ci import gitlab as glmod

    env_path = tmp_path / ".env"
    env_path.write_text("GITLAB_TOKEN=glpat-STALE-from-the-file\n")
    env_path.chmod(0o600)
    monkeypatch.setattr(nh_config, "ENV_PATH", env_path)
    monkeypatch.setenv("GITLAB_TOKEN", "glpat-LIVE-from-the-shell")
    monkeypatch.setattr(glmod.subprocess, "run",
                        lambda *a, **k: _fake_proc(0, stdout="{}", stderr=""))

    glmod._subprocess_run(["glab", "api", "x"])
    assert os.environ["GITLAB_TOKEN"] == "glpat-LIVE-from-the-shell"


def test_gitlab_poll_auth_wall_is_access_failure_naming_the_env_key():
    from no_human.ci.gitlab import GitLabAccessError, _ACCESS_ENV_KEY

    def fake(cmd):
        raise GitLabAccessError("401 Unauthorized (HTTP 401)")

    ci = GitLabCI("g/p", hostname="gitlab.acme.net", poll_interval=0, _run_cmd=fake)
    result = ci._check_pipeline("42")
    assert result.access_failure is True, "a 401 must park, not retry"
    assert result.infra_failure is False, "an auth wall is not an infra blip"
    assert result.access_env_key == _ACCESS_ENV_KEY == "GITLAB_TOKEN"
    assert not result.passed
    assert "GITLAB_TOKEN" in result.parsed_output


def test_gitlab_trigger_auth_wall_is_access_failure_not_infra():
    from no_human.ci.gitlab import GitLabAccessError

    def fake(cmd):
        raise GitLabAccessError("401 Unauthorized (HTTP 401)")

    ci = GitLabCI("g/p", hostname="gitlab.acme.net", poll_interval=0, _run_cmd=fake)
    result = ci._trigger_and_wait("branch", {})
    assert result.access_failure is True
    assert result.infra_failure is False


async def test_gitlab_access_failure_is_never_retried_on_the_120s_backoff():
    """`trigger` retries `infra_failure` up to max_infra_retries with a 120 s
    sleep between attempts. An access wall must exit on the FIRST attempt —
    retrying a 401 only delays the human who has to fix it."""
    from no_human.ci.gitlab import GitLabAccessError

    calls = []

    def fake(cmd):
        calls.append(cmd)
        raise GitLabAccessError("401 Unauthorized (HTTP 401)")

    slept = []

    async def no_sleep(seconds):
        slept.append(seconds)

    ci = GitLabCI("g/p", hostname="gitlab.acme.net", poll_interval=0,
                  max_infra_retries=2, _run_cmd=fake)
    import no_human.ci.gitlab as glmod
    orig = glmod.asyncio.sleep
    glmod.asyncio.sleep = no_sleep
    try:
        result = await ci.trigger("branch")
    finally:
        glmod.asyncio.sleep = orig
    assert result.access_failure is True
    assert slept == [], f"an auth wall must not sleep on the infra backoff: {slept}"
    assert len(calls) == 1, f"one attempt only, got {len(calls)}"


def test_gitlab_token_is_loaded_from_the_env_file_so_the_named_key_is_actionable(
    tmp_path, monkeypatch,
):
    """The MISSING_ACCESS ask says "set GITLAB_TOKEN in ~/.no_human/.env".
    Nothing in the package loaded that key, so following the instruction would
    have changed nothing: `glab` reads GITLAB_TOKEN from the PROCESS env.
    """
    import no_human.config as nh_config
    from no_human.ci import gitlab as glmod

    env_path = tmp_path / ".env"
    env_path.write_text("GITLAB_TOKEN=glpat-from-the-env-file\n")
    env_path.chmod(0o600)
    monkeypatch.setattr(nh_config, "ENV_PATH", env_path)
    monkeypatch.delenv("GITLAB_TOKEN", raising=False)
    monkeypatch.setattr(glmod.subprocess, "run",
                        lambda *a, **k: _fake_proc(0, stdout="{}", stderr=""))

    glmod._subprocess_run(["glab", "api", "x"])
    assert os.environ.get("GITLAB_TOKEN") == "glpat-from-the-env-file"


# --------------------------------------------------------------------------- #
# CI.2 — cross-adapter conformance: unreachable CI is UNKNOWN, never FAILED   #
# --------------------------------------------------------------------------- #
class TestInfraStatusConformance:
    """An unreachable/inaccessible CI is a park-or-retry signal, not a verdict.

    Every backend must report ``status is PipelineStatus.UNKNOWN`` (with
    ``infra_failure`` or ``access_failure`` carrying the real signal) for the
    identical "I could not reach CI" condition. This class pins that contract
    across every ``CIBackend`` subclass so a future (sixth) adapter can't
    re-diverge the way ``jenkins.py`` once did."""

    def test_jenkins(self):
        from no_human.ci.jenkins import JenkinsCI

        ci = JenkinsCI(
            "job/x", mode="watch", poll_interval=0, max_infra_retries=0,
            user="u", token="t", _run_cmd=lambda cmd: None,
        )
        result = asyncio.run(ci.trigger("PR-1"))
        assert result.status is PipelineStatus.UNKNOWN
        assert result.infra_failure or result.access_failure
        assert not result.passed and not result.failed

    def test_github_actions(self):
        ci = _gha(raises=RuntimeError("gh api failed (1): HTTP 502 Bad Gateway"))
        result = asyncio.run(ci.trigger("branch"))
        assert result.status is PipelineStatus.UNKNOWN
        assert result.infra_failure or result.access_failure
        assert not result.passed and not result.failed

    def test_circleci(self, monkeypatch):
        from no_human.ci.circleci import CircleCICI

        monkeypatch.delenv("CIRCLECI_TOKEN", raising=False)
        ci = CircleCICI(project_slug="gh/acme/svc", poll_interval=0)
        result = asyncio.run(ci.trigger("branch"))
        assert result.status is PipelineStatus.UNKNOWN
        assert result.infra_failure or result.access_failure
        assert not result.passed and not result.failed

    def test_gitlab(self):
        # NOTE: this exercises the `_run_cmd` raising `GitLabInfraError` path
        # (gitlab.py:297's `_infra_result`, already UNKNOWN-based). It does
        # NOT exercise `_trigger_and_wait_inner`'s separate "no pipeline ID"
        # branch (gitlab.py:381-387), which returns `FAILED + infra_failure`
        # — a latent, structurally-identical smell in an out-of-scope file;
        # see the PR body follow-up note.
        from no_human.ci.gitlab import GitLabInfraError

        def fake(cmd):
            raise GitLabInfraError("502 Bad Gateway")

        ci = GitLabCI("g/p", hostname="gitlab.acme.net", poll_interval=0, _run_cmd=fake)
        result = ci._trigger_and_wait("branch", {})
        assert result.status is PipelineStatus.UNKNOWN
        assert result.infra_failure or result.access_failure
        assert not result.passed and not result.failed

    def test_ghe_checkruns(self):
        # A fifth adapter, read-only: this one already conformed before this
        # change (see tests/test_ghe_checkruns.py) — included here so the
        # cross-adapter contract lives in one place.
        from no_human.ci.ghe_checkruns import GHECheckRunsCI

        async def raising(repo, ref, *, hostname=""):
            raise RuntimeError("gh api failed (1): HTTP 502 Bad Gateway")

        ci = GHECheckRunsCI("owner/repo", poll_interval=0,
                            fetch_runs=raising, fetch_statuses=raising)
        result = asyncio.run(ci.trigger("branch"))
        assert result.status is PipelineStatus.UNKNOWN
        assert result.infra_failure or result.access_failure
        assert not result.passed and not result.failed
