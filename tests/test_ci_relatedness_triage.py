"""Relatedness triage for `_ci_failure_unrelated` (Phase 6.3, ticket 429/E1A):
job-label-vs-test-id. A GitHub Actions check-run NAME ("Python", "build",
"test (3.12)") is a CI job label, not a test identifier — matching it
against changed-file stems produces false "unrelated" verdicts that
escalate a build broken by our own change instead of retrying it. These
tests pin that a failing name must actually look like a test identifier
before the function is allowed to declare "unrelated"; anything opaque
routes to the fix loop (returns None) instead."""

from __future__ import annotations

import pytest

from no_human.ci.base import CIResult, JobResult, PipelineStatus
from no_human.core.orchestrator import _ci_failure_unrelated

# Tests here reach ``config.load_env_var``, which reads the operator's real
# ``~/.no_human/.env`` BEFORE the process env. Requested by NAME through
# `usefixtures` — never an autouse marker; see tests/conftest.py for why the
# spelling is load-bearing (and note that even quoting the marker in a comment
# is enough to score this file as a cheat signal, which is how that was found).
pytestmark = pytest.mark.usefixtures("isolated_env_file")


def _ci_with_failures(names):
    return CIResult(
        pipeline_id="7", pipeline_url="https://b/7", status=PipelineStatus.FAILED,
        jobs=[JobResult(name=n, status="failed") for n in names],
    )


def test_generic_job_name_with_changed_test_file_is_not_unrelated():
    ci = _ci_with_failures(["Python"])
    changed = ["tests/test_wake.py"]
    assert _ci_failure_unrelated(ci, changed) is None

    ci2 = _ci_with_failures(["build", "test (3.12)"])
    assert _ci_failure_unrelated(ci2, changed) is None


def test_pytest_node_id_that_matches_nothing_is_still_unrelated():
    ci = _ci_with_failures(["tests/test_billing.py::test_dunning"])
    changed = ["src/no_human/blockers/wake.py"]
    evidence = _ci_failure_unrelated(ci, changed)
    assert evidence is not None
    assert "test_billing" in evidence


def test_dotted_junit_id_that_matches_nothing_is_still_unrelated():
    ci = _ci_with_failures(["com.acme.billing.InvoiceIT.testTotals"])
    changed = ["src/test/java/com/acme/analytics/AnalyticsE2EIT.java"]
    evidence = _ci_failure_unrelated(ci, changed)
    assert evidence is not None
    assert "InvoiceIT" in evidence


def test_bare_test_class_name_that_matches_nothing_is_still_unrelated():
    ci = _ci_with_failures(["InvoiceServiceTest"])
    changed = ["src/main/java/com/acme/analytics/Analytics.java"]
    evidence = _ci_failure_unrelated(ci, changed)
    assert evidence is not None


def test_a_real_test_id_that_does_match_the_diff_is_still_related():
    ci = _ci_with_failures(["tests/test_wake.py::test_x"])
    changed = ["tests/test_wake.py"]
    assert _ci_failure_unrelated(ci, changed) is None


def test_mixed_opaque_and_test_id_names_route_to_the_fix_loop():
    ci = _ci_with_failures(["Python", "com.acme.billing.InvoiceIT.testTotals"])
    changed = ["tests/test_wake.py"]
    assert _ci_failure_unrelated(ci, changed) is None
