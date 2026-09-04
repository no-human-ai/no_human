"""Tests for the test-layer model (Phase 6a)."""

import json
from pathlib import Path

import pytest

from no_human.testing.test_layers import (
    Gating,
    Runner,
    TestLayer,
    TestPlan,
)
from no_human.project_model import Project

# `test_ci_from_layer_jenkins` reaches ``config.load_env_var``, which reads the
# operator's real ``~/.no_human/.env`` BEFORE the process env. Requested by
# NAME through `usefixtures` — never an autouse marker; see tests/conftest.py.
pytestmark = pytest.mark.usefixtures("isolated_env_file")


def test_layer_roundtrip():
    layer = TestLayer(
        name="unit", command="pytest -q", repo="/code",
        runner=Runner.LOCAL, gating=Gating.BLOCKING, timeout=120,
    )
    d = layer.to_dict()
    restored = TestLayer.from_dict(d)
    assert restored.name == "unit"
    assert restored.runner == Runner.LOCAL
    assert restored.gating == Gating.BLOCKING
    assert restored.timeout == 120


def test_plan_ordering():
    plan = TestPlan(layers=[
        TestLayer(name="e2e", command="make e2e", depends_on=["integration"]),
        TestLayer(name="unit", command="pytest"),
        TestLayer(name="integration", command="mvn verify", depends_on=["unit"]),
    ])
    ordered = plan.ordered()
    names = [l.name for l in ordered]
    assert names == ["unit", "integration", "e2e"]


def test_plan_blocking_and_advisory():
    plan = TestPlan(layers=[
        TestLayer(name="unit", command="pytest", gating=Gating.BLOCKING),
        TestLayer(name="e2e", command="make e2e", gating=Gating.ADVISORY),
    ])
    assert len(plan.blocking_layers()) == 1
    assert plan.blocking_layers()[0].name == "unit"
    assert len(plan.advisory_layers()) == 1
    assert plan.advisory_layers()[0].name == "e2e"


def test_plan_json_roundtrip():
    plan = TestPlan(layers=[
        TestLayer(name="unit", command="pytest"),
        TestLayer(name="integration", command="mvn verify", depends_on=["unit"]),
    ])
    raw = plan.to_json()
    restored = TestPlan.from_json(raw)
    assert len(restored.layers) == 2
    assert restored.layers[1].depends_on == ["unit"]


def test_plan_empty():
    plan = TestPlan.from_json("[]")
    assert plan.layers == []
    assert plan.ordered() == []


def test_from_profile():
    class FakeProfile:
        test_cmd = "uv run pytest"
        integration_test_cmd = "mvn verify"
        repo_path = "/code"

    plan = TestPlan.from_profile(FakeProfile())
    assert len(plan.layers) == 2
    assert plan.layers[0].name == "unit"
    assert plan.layers[1].name == "integration"
    assert plan.layers[1].depends_on == ["unit"]


def test_project_test_plan():
    layers = [
        TestLayer(name="unit", command="pytest").to_dict(),
        TestLayer(name="e2e", command="make e2e", depends_on=["unit"]).to_dict(),
    ]
    p = Project.new("test-project")
    p.test_layers = json.dumps(layers)
    plan = p.test_plan
    assert len(plan.layers) == 2
    ordered = plan.ordered()
    assert ordered[0].name == "unit"
    assert ordered[1].name == "e2e"


# --------------------------------------------------------------------------- #
# PR4: plan_runner tests                                                       #
# --------------------------------------------------------------------------- #

from no_human.testing.plan_runner import LayerResult, PlanResult, run_test_plan


def test_plan_runner_blocking_pass(tmp_path):
    """A plan with a passing blocking layer succeeds."""
    # Create a trivial test script.
    (tmp_path / "test.sh").write_text("#!/bin/sh\necho '1 passed'\nexit 0\n")
    plan = TestPlan(layers=[
        TestLayer(name="unit", command="sh test.sh", gating=Gating.BLOCKING),
    ])
    result = run_test_plan(plan, tmp_path)
    assert result.ok
    assert len(result.layer_results) == 1
    assert result.layer_results[0].result.ok


def test_plan_runner_blocking_fail_stops(tmp_path):
    """A failing blocking layer stops execution; subsequent layers are skipped."""
    (tmp_path / "fail.sh").write_text("#!/bin/sh\necho '1 failed'\nexit 1\n")
    (tmp_path / "pass.sh").write_text("#!/bin/sh\necho '1 passed'\nexit 0\n")
    plan = TestPlan(layers=[
        TestLayer(name="unit", command="sh fail.sh", gating=Gating.BLOCKING),
        TestLayer(name="e2e", command="sh pass.sh", gating=Gating.BLOCKING, depends_on=["unit"]),
    ])
    result = run_test_plan(plan, tmp_path)
    assert not result.ok
    assert len(result.layer_results) == 1  # e2e was skipped


def test_plan_runner_advisory_fail_does_not_fail_plan(tmp_path):
    """Advisory layer failure doesn't fail the overall plan."""
    (tmp_path / "pass.sh").write_text("#!/bin/sh\necho '1 passed'\nexit 0\n")
    (tmp_path / "fail.sh").write_text("#!/bin/sh\necho '1 failed'\nexit 1\n")
    plan = TestPlan(layers=[
        TestLayer(name="unit", command="sh pass.sh", gating=Gating.BLOCKING),
        TestLayer(name="lint", command="sh fail.sh", gating=Gating.ADVISORY),
    ])
    result = run_test_plan(plan, tmp_path)
    assert result.ok  # advisory failure doesn't block
    assert len(result.layer_results) == 2
    assert not result.layer_results[1].result.ok  # lint did fail


def test_plan_runner_wake_gated_deferred(tmp_path):
    """Wake-gated layers are deferred, not executed."""
    plan = TestPlan(layers=[
        TestLayer(name="ci", command="unused", gating=Gating.WAKE_GATED),
    ])
    result = run_test_plan(plan, tmp_path)
    assert result.ok
    assert result.has_deferred
    assert result.layer_results[0].deferred


def test_plan_runner_cross_repo(tmp_path):
    """A layer with a different repo runs in that repo's directory."""
    code_repo = tmp_path / "code"
    test_repo = tmp_path / "tests"
    code_repo.mkdir()
    test_repo.mkdir()
    (test_repo / "run.sh").write_text("#!/bin/sh\necho '5 passed'\nexit 0\n")
    plan = TestPlan(layers=[
        TestLayer(
            name="integration",
            command="sh run.sh",
            repo=str(test_repo),
            gating=Gating.BLOCKING,
        ),
    ])
    result = run_test_plan(plan, code_repo)
    assert result.ok
    assert result.layer_results[0].result.passed == 5


def test_plan_runner_callbacks(tmp_path):
    """on_layer_start and on_layer_done callbacks fire."""
    (tmp_path / "ok.sh").write_text("#!/bin/sh\nexit 0\n")
    starts, dones = [], []
    plan = TestPlan(layers=[
        TestLayer(name="unit", command="sh ok.sh", gating=Gating.BLOCKING),
    ])
    run_test_plan(
        plan, tmp_path,
        on_layer_start=lambda l: starts.append(l.name),
        on_layer_done=lambda l, lr: dones.append(lr.summary),
    )
    assert starts == ["unit"]
    assert len(dones) == 1


def test_layer_result_error_is_not_ok():
    """LayerResult with error set must be not-ok (regression: was treated as skipped)."""
    lr = LayerResult(layer_name="broken", gating=Gating.BLOCKING, error="boom")
    assert not lr.ok
    assert "ERROR" in lr.summary


# --- PR-C: credentials + freshness ---------------------------------------- #


def test_layer_env_secret_ref_roundtrip():
    """env and secret_ref survive to_dict/from_dict."""
    layer = TestLayer(
        name="it", command="pytest", env={"FOO": "bar"},
        secret_ref=["SECRET_TOKEN"],
    )
    d = layer.to_dict()
    assert d["env"] == {"FOO": "bar"}
    assert d["secret_ref"] == ["SECRET_TOKEN"]
    restored = TestLayer.from_dict(d)
    assert restored.env == {"FOO": "bar"}
    assert restored.secret_ref == ["SECRET_TOKEN"]


def test_layer_env_secret_ref_defaults():
    """Layers without env/secret_ref default to empty."""
    layer = TestLayer.from_dict({"name": "unit", "command": "pytest"})
    assert layer.env == {}
    assert layer.secret_ref == []


def test_resolve_credentials_all_present(monkeypatch):
    """When all secret_ref vars are set, resolved_env has them and missing is empty."""
    from no_human.testing.plan_runner import _resolve_credentials
    monkeypatch.setenv("MY_TOKEN", "secret123")
    layer = TestLayer(
        name="it", command="pytest",
        env={"FOO": "bar"}, secret_ref=["MY_TOKEN"],
    )
    merged, missing = _resolve_credentials(layer)
    assert missing == []
    assert merged["FOO"] == "bar"
    assert merged["MY_TOKEN"] == "secret123"


def test_resolve_credentials_missing(monkeypatch):
    """When a secret_ref var is absent, it appears in missing list."""
    from no_human.testing.plan_runner import _resolve_credentials
    monkeypatch.delenv("ABSENT_VAR", raising=False)
    layer = TestLayer(
        name="it", command="pytest", secret_ref=["ABSENT_VAR"],
    )
    merged, missing = _resolve_credentials(layer)
    assert missing == ["ABSENT_VAR"]


def test_missing_cred_produces_missing_access(tmp_path, monkeypatch):
    """A layer with a missing secret_ref is deferred with MISSING_ACCESS error."""
    monkeypatch.delenv("REQUIRED_CRED", raising=False)
    plan = TestPlan(layers=[
        TestLayer(name="needs-cred", command="echo ok",
                  secret_ref=["REQUIRED_CRED"]),
    ])
    pr = run_test_plan(plan, tmp_path)
    assert len(pr.layer_results) == 1
    lr = pr.layer_results[0]
    assert not lr.ok  # deferred=True but has error → not ok checked via error
    assert "MISSING_ACCESS" in (lr.error or "")
    assert lr.deferred


# --------------------------------------------------------------------------- #
# ci_from_layer factory tests                                                   #
# --------------------------------------------------------------------------- #

from no_human.ci import ci_from_layer, GitLabCI, JenkinsCI


def test_ci_from_layer_gitlab():
    """ci_from_layer builds a GitLabCI from a layer ci dict."""
    ci = ci_from_layer({
        "backend": "gitlab",
        "project": "ci_gate-integration-suite",
        "hostname": "gitlab.acme.net",
        "variables": {"METHOD": "create", "TEST": "true"},
        "timeout_minutes": 45,
    })
    assert isinstance(ci, GitLabCI)
    assert ci.project == "ci_gate-integration-suite"
    assert ci.hostname == "gitlab.acme.net"
    assert ci.variables == {"METHOD": "create", "TEST": "true"}
    assert ci.timeout_minutes == 45


def test_ci_from_layer_jenkins():
    """ci_from_layer builds a JenkinsCI from a layer ci dict."""
    ci = ci_from_layer({
        "backend": "jenkins",
        "job": "job/metrics-core/job/query-service",
        "mode": "human_gated",
    })
    assert isinstance(ci, JenkinsCI)
    assert ci.mode == "human_gated"


def test_ci_from_layer_empty():
    """ci_from_layer returns None for empty or missing config."""
    assert ci_from_layer({}) is None
    assert ci_from_layer(None) is None


def test_ci_from_layer_no_project_raises():
    """A layer that NAMES a backend but no target is broken, not absent.

    It used to return None — the same value an empty `ci` block returns — so
    `_run_ci_layer` reported "CI config incomplete" without saying which key.
    Now it raises and `plan_runner` names the key in the layer result.
    """
    from no_human.ci import CIMisconfigured
    with pytest.raises(CIMisconfigured, match="ci.project"):
        ci_from_layer({"backend": "gitlab"})


# --------------------------------------------------------------------------- #
# _run_ci_layer tests (mocked CI backend)                                       #
# --------------------------------------------------------------------------- #

from no_human.testing.plan_runner import _run_ci_layer
from no_human.ci.base import CIResult, PipelineStatus, HumanGatedCI


def test_run_ci_layer_no_config():
    """CI layer without ci dict → deferred."""
    layer = TestLayer(name="ci-test", command="", runner=Runner.CI, ci={})
    lr = _run_ci_layer(layer)
    assert lr.deferred
    assert lr.ok  # deferred is not a failure


def test_run_ci_layer_with_a_backend_but_no_target_is_not_ok():
    """The third caller of ci_from_config, through ci_from_layer.

    A layer with NO `ci` dict asked for nothing and defers (above). A layer
    that names a backend and no pipeline target is BROKEN, and the two must
    not land in the same place: `ok` is False, and the message names the key,
    so a gating layer fails instead of quietly deferring past the gate.
    """
    layer = TestLayer(name="ci-test", command="", runner=Runner.CI,
                      ci={"backend": "gitlab"})
    lr = _run_ci_layer(layer)
    assert not lr.ok
    assert not lr.deferred
    assert "ci.project" in (lr.error or ""), lr.error


def test_run_ci_layer_success(monkeypatch):
    """CI layer with a successful pipeline → ok."""
    fake_result = CIResult(
        pipeline_id="123", pipeline_url="https://gitlab/p/123",
        status=PipelineStatus.SUCCESS,
    )
    def fake_from_layer(ci_dict):
        class FakeCI:
            name = "gitlab"
            async def trigger(self, branch, extra_vars=None):
                return fake_result
        return FakeCI()
    monkeypatch.setattr("no_human.ci.ci_from_layer", fake_from_layer)

    layer = TestLayer(name="ci_gate-e2e", command="", runner=Runner.CI, ci={
        "backend": "gitlab", "project": "ci_gate-analytics-export",
    })
    lr = _run_ci_layer(layer)
    assert lr.ok
    assert not lr.deferred
    assert lr.result is not None
    assert lr.result.ok


def test_run_ci_layer_failure(monkeypatch):
    """CI layer with a failed pipeline → not ok."""
    fake_result = CIResult(
        pipeline_id="456", pipeline_url="https://gitlab/p/456",
        status=PipelineStatus.FAILED,
    )
    def fake_from_layer(ci_dict):
        class FakeCI:
            name = "gitlab"
            async def trigger(self, branch, extra_vars=None):
                return fake_result
        return FakeCI()
    monkeypatch.setattr("no_human.ci.ci_from_layer", fake_from_layer)

    layer = TestLayer(name="ci_gate-e2e", command="", runner=Runner.CI, ci={
        "backend": "gitlab", "project": "ci_gate-analytics-export",
    })
    lr = _run_ci_layer(layer)
    assert not lr.ok
    assert lr.result is not None
    assert not lr.result.ok


def test_run_ci_layer_human_gated(monkeypatch):
    """CI layer with human_gated Jenkins → deferred + HUMAN_GATED error."""
    def fake_from_layer(ci_dict):
        class FakeCI:
            name = "jenkins"
            async def trigger(self, branch, extra_vars=None):
                raise HumanGatedCI("build image first", wake_hint="Jenkins build #42")
        return FakeCI()
    monkeypatch.setattr("no_human.ci.ci_from_layer", fake_from_layer)

    layer = TestLayer(name="docker-build", command="", runner=Runner.CI, ci={
        "backend": "jenkins", "job": "job/metrics-core", "mode": "human_gated",
    })
    lr = _run_ci_layer(layer)
    assert lr.deferred
    assert "HUMAN_GATED" in (lr.error or "")


def test_run_ci_layer_wake_gated_triggers_but_defers(monkeypatch):
    """CI layer with wake_gated triggers the pipeline but returns deferred immediately."""
    triggered = {}
    def fake_from_layer(ci_dict):
        class FakeCI:
            name = "gitlab"
            variables = {}
            def _trigger(self, branch, variables):
                triggered["branch"] = branch
                triggered["vars"] = variables
                return ("999", "https://gitlab/p/999")
            async def trigger(self, branch, extra_vars=None):
                raise AssertionError("should not call full trigger for wake_gated")
        return FakeCI()
    monkeypatch.setattr("no_human.ci.ci_from_layer", fake_from_layer)

    layer = TestLayer(name="ci_gate-e2e", command="", runner=Runner.CI,
                      gating=Gating.WAKE_GATED, ci={
        "backend": "gitlab", "project": "ci_gate-analytics-export",
        "branch": "main",
        "variables": {"METHOD": "create", "DESTROY": "true"},
    })
    lr = _run_ci_layer(layer)
    assert lr.deferred
    assert "TRIGGERED" in (lr.error or "")
    assert "999" in (lr.error or "")
    assert triggered["branch"] == "main"
    assert triggered["vars"] == {"METHOD": "create", "DESTROY": "true"}


def test_run_ci_layer_uses_variables(monkeypatch):
    """CI layer passes variables from layer.ci to the trigger."""
    captured = {}
    def fake_from_layer(ci_dict):
        class FakeCI:
            name = "gitlab"
            async def trigger(self, branch, extra_vars=None):
                captured["branch"] = branch
                captured["vars"] = extra_vars
                return CIResult(
                    pipeline_id="789", pipeline_url="",
                    status=PipelineStatus.SUCCESS,
                )
        return FakeCI()
    monkeypatch.setattr("no_human.ci.ci_from_layer", fake_from_layer)

    layer = TestLayer(name="ci_gate", command="", runner=Runner.CI, ci={
        "backend": "gitlab", "project": "ci_gate-analytics-export",
        "branch": "main",
        "variables": {"METHOD": "create", "DESTROY": "true"},
    })
    lr = _run_ci_layer(layer)
    assert lr.ok
    assert captured["branch"] == "main"
    assert captured["vars"] == {"METHOD": "create", "DESTROY": "true"}


# --------------------------------------------------------------------------- #
# Plan runner: CI layer integration in full plan                                #
# --------------------------------------------------------------------------- #


def test_plan_runner_ci_layer_triggers(tmp_path, monkeypatch):
    """A plan with local + CI layers runs local first, then triggers CI."""
    (tmp_path / "test.sh").write_text("#!/bin/sh\necho '3 passed'\nexit 0\n")

    fake_result = CIResult(
        pipeline_id="100", pipeline_url="https://gitlab/p/100",
        status=PipelineStatus.SUCCESS,
    )
    def fake_from_layer(ci_dict):
        class FakeCI:
            name = "gitlab"
            async def trigger(self, branch, extra_vars=None):
                return fake_result
        return FakeCI()
    monkeypatch.setattr("no_human.ci.ci_from_layer", fake_from_layer)

    plan = TestPlan(layers=[
        TestLayer(name="unit", command="sh test.sh", gating=Gating.BLOCKING),
        TestLayer(name="ci_gate", command="", runner=Runner.CI, gating=Gating.BLOCKING,
                  depends_on=["unit"], ci={"backend": "gitlab", "project": "ci_gate"}),
    ])
    result = run_test_plan(plan, tmp_path)
    assert result.ok
    assert len(result.layer_results) == 2
    assert result.layer_results[0].layer_name == "unit"
    assert result.layer_results[0].ok
    assert result.layer_results[1].layer_name == "ci_gate"
    assert result.layer_results[1].ok


def test_plan_runner_ci_fail_blocks(tmp_path, monkeypatch):
    """A failing CI blocking layer stops the plan."""
    (tmp_path / "test.sh").write_text("#!/bin/sh\necho '3 passed'\nexit 0\n")

    fake_result = CIResult(
        pipeline_id="101", pipeline_url="",
        status=PipelineStatus.FAILED,
    )
    def fake_from_layer(ci_dict):
        class FakeCI:
            name = "gitlab"
            async def trigger(self, branch, extra_vars=None):
                return fake_result
        return FakeCI()
    monkeypatch.setattr("no_human.ci.ci_from_layer", fake_from_layer)

    plan = TestPlan(layers=[
        TestLayer(name="unit", command="sh test.sh", gating=Gating.BLOCKING),
        TestLayer(name="ci_gate", command="", runner=Runner.CI, gating=Gating.BLOCKING,
                  depends_on=["unit"], ci={"backend": "gitlab", "project": "ci_gate"}),
        TestLayer(name="after", command="sh test.sh", gating=Gating.BLOCKING,
                  depends_on=["ci_gate"]),
    ])
    result = run_test_plan(plan, tmp_path)
    assert not result.ok
    assert len(result.layer_results) == 2  # "after" was skipped


@pytest.mark.asyncio
async def test_find_project_by_repo(store):
    """Store.find_project_by_repo finds a project by its repo_paths."""
    p = Project.new("my-project", repo_paths=["/home/user/repo1", "/home/user/repo2"])
    await store.create_project(p)

    found = await store.find_project_by_repo("/home/user/repo1")
    assert found is not None
    assert found.name == "my-project"

    not_found = await store.find_project_by_repo("/home/user/other")
    assert not_found is None
