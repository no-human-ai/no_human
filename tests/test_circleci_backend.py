"""CircleCI CI backend — watch-mode polling of the PR-triggered pipeline.

Mirrors the gitlab/jenkins backend tests: status mapping, access-wall detection,
and the trigger-mode gate. All HTTP is injected (no network)."""

import pytest

from no_human.ci import ci_from_config
from no_human.ci.base import CIResult, HumanGatedCI, PipelineStatus
from no_human.ci.circleci import CircleCICI


class _Resp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


def _backend(monkeypatch, *, get=None, post=None, mode="watch", token="tok"):
    if token is None:
        monkeypatch.delenv("CIRCLECI_TOKEN", raising=False)
    else:
        monkeypatch.setenv("CIRCLECI_TOKEN", token)
    return CircleCICI(project_slug="gh/acme/svc", mode=mode,
                      _get=get or (lambda url, headers: _Resp(200, {})),
                      _post=post or (lambda url, headers, json: _Resp(201, {})))


@pytest.mark.asyncio
async def test_check_status_maps_success(monkeypatch):
    def get(url, headers):
        assert "/pipeline/PID/workflow" in url
        assert headers.get("Circle-Token") == "tok"
        return _Resp(200, {"items": [{"name": "build", "status": "success"},
                                     {"name": "test", "status": "success"}]})
    r = await _backend(monkeypatch, get=get).check_status("PID")
    assert r.status is PipelineStatus.SUCCESS
    assert r.passed is True
    assert [j.name for j in r.jobs] == ["build", "test"]


@pytest.mark.asyncio
async def test_check_status_branch_filtered_workflow_is_success(monkeypatch):
    # A ran workflow passed while a branch-filtered one is `not_run`; CircleCI
    # shows this pipeline as Success, so it must not demote to SKIPPED (which
    # would make `passed` False and block an otherwise-green PR).
    def get(url, headers):
        return _Resp(200, {"items": [{"name": "build-test", "status": "success"},
                                     {"name": "deploy", "status": "not_run"}]})
    r = await _backend(monkeypatch, get=get).check_status("PID")
    assert r.status is PipelineStatus.SUCCESS
    assert r.passed is True


@pytest.mark.asyncio
async def test_check_status_all_not_run_is_skipped(monkeypatch):
    # When nothing actually ran, the pipeline is genuinely skipped (not passed).
    def get(url, headers):
        return _Resp(200, {"items": [{"name": "deploy", "status": "not_run"}]})
    r = await _backend(monkeypatch, get=get).check_status("PID")
    assert r.status is PipelineStatus.SKIPPED
    assert r.passed is False


@pytest.mark.asyncio
async def test_check_status_maps_failure(monkeypatch):
    def get(url, headers):
        return _Resp(200, {"items": [{"name": "build", "status": "success"},
                                     {"name": "test", "status": "failed"}]})
    r = await _backend(monkeypatch, get=get).check_status("PID")
    assert r.status is PipelineStatus.FAILED
    assert r.failed is True


@pytest.mark.asyncio
async def test_check_status_running_is_not_terminal(monkeypatch):
    def get(url, headers):
        return _Resp(200, {"items": [{"name": "test", "status": "running"}]})
    r = await _backend(monkeypatch, get=get).check_status("PID")
    assert r.status is PipelineStatus.RUNNING
    assert r.status.is_terminal is False


@pytest.mark.asyncio
async def test_no_token_is_an_access_failure(monkeypatch):
    r = await _backend(monkeypatch, token=None).check_status("PID")
    assert r.access_failure is True
    assert r.access_env_key == "CIRCLECI_TOKEN"


@pytest.mark.asyncio
async def test_http_401_is_an_access_failure(monkeypatch):
    def get(url, headers):
        return _Resp(401, {})
    r = await _backend(monkeypatch, get=get).check_status("PID")
    assert r.access_failure is True


@pytest.mark.asyncio
async def test_watch_mode_finds_branch_pipeline_and_polls(monkeypatch):
    calls = []

    def get(url, headers):
        calls.append(url)
        if "/pipeline?branch=" in url or "branch=" in url:
            return _Resp(200, {"items": [{"id": "PID99", "state": "created"}]})
        return _Resp(200, {"items": [{"name": "test", "status": "success"}]})
    r = await _backend(monkeypatch, get=get, mode="watch").trigger("feature/x")
    assert r.passed is True
    assert any("branch=feature%2Fx" in c or "branch=feature/x" in c for c in calls)


@pytest.mark.asyncio
async def test_trigger_mode_posts_a_pipeline(monkeypatch):
    posted = {}

    def post(url, headers, json):
        posted.update(url=url, json=json)
        return _Resp(201, {"id": "NEWPID"})

    def get(url, headers):
        return _Resp(200, {"items": [{"name": "test", "status": "success"}]})
    r = await _backend(monkeypatch, get=get, post=post, mode="trigger").trigger("feature/x")
    assert r.passed is True
    assert "/project/gh/acme/svc/pipeline" in posted["url"]
    assert posted["json"] == {"branch": "feature/x"}


@pytest.mark.asyncio
async def test_watch_mode_never_posts(monkeypatch):
    def post(url, headers, json):
        raise AssertionError("watch mode must never POST")

    def get(url, headers):
        if "branch=" in url:
            return _Resp(200, {"items": [{"id": "PID", "state": "created"}]})
        return _Resp(200, {"items": [{"name": "t", "status": "success"}]})
    r = await _backend(monkeypatch, get=get, post=post, mode="watch").trigger("br")
    assert r.passed is True


def test_registered_in_ci_from_config(monkeypatch):
    monkeypatch.setenv("CIRCLECI_TOKEN", "t")
    be = ci_from_config({"ci": {"enabled": True, "backend": "circleci",
                                "project": "gh/acme/svc", "mode": "watch"}})
    assert isinstance(be, CircleCICI)
    assert be.project_slug == "gh/acme/svc"
    # disabled → None
    assert ci_from_config({"ci": {"enabled": False, "backend": "circleci"}}) is None
    # unknown backend still raises (regression guard)
    with pytest.raises(ValueError, match="unknown ci.backend"):
        ci_from_config({"ci": {"enabled": True, "backend": "nope"}})


@pytest.mark.asyncio
async def test_wait_retries_transient_infra_then_succeeds(monkeypatch):
    import no_human.ci.circleci as mod
    monkeypatch.setattr(mod, "_INFRA_BACKOFF_SECONDS", 0)
    monkeypatch.setenv("CIRCLECI_TOKEN", "tok")
    seq = iter([_Resp(503, {}),  # transient infra on the first poll
                _Resp(200, {"items": [{"name": "t", "status": "success"}]})])
    be = CircleCICI(project_slug="gh/acme/svc", poll_interval=0,
                    _get=lambda url, headers: next(seq))
    r = await be._wait("PID")
    assert r.passed is True          # recovered — a single blip must not abort
    assert r.infra_failure is False


@pytest.mark.asyncio
async def test_wait_gives_up_after_max_infra_retries(monkeypatch):
    import no_human.ci.circleci as mod
    monkeypatch.setattr(mod, "_INFRA_BACKOFF_SECONDS", 0)
    monkeypatch.setenv("CIRCLECI_TOKEN", "tok")
    be = CircleCICI(project_slug="gh/acme/svc", max_infra_retries=2,
                    _get=lambda url, headers: _Resp(503, {}))
    r = await be._wait("PID")
    assert r.infra_failure is True   # persisted after the retries (now truthfully)


@pytest.mark.asyncio
async def test_watch_mode_no_pipeline_raises_human_gated(monkeypatch):
    # Watch mode + no pipeline for the branch (CircleCI not wired to run on push,
    # or none yet) → HumanGatedCI so the orchestrator parks honestly, never fakes
    # a result. This is the core of the watch-first / honest-escalation behavior.
    r_get = lambda url, headers: _Resp(200, {"items": []})   # empty → no pipeline
    be = _backend(monkeypatch, get=r_get, mode="watch")
    with pytest.raises(HumanGatedCI, match="No CircleCI pipeline"):
        await be.trigger("feature/x")


@pytest.mark.asyncio
async def test_unknown_workflow_status_falls_through_to_unknown(monkeypatch):
    # A status CircleCI's v2 API might add that we don't map must degrade to
    # UNKNOWN (never silently PASS/FAIL).
    def get(url, headers):
        return _Resp(200, {"items": [{"name": "w", "status": "some_new_state"}]})
    r = await _backend(monkeypatch, get=get).check_status("PID")
    assert r.status is PipelineStatus.UNKNOWN
    assert r.passed is False and r.failed is False
